"""WorkflowDebugAPI 轮询懒启动测试（2026-09-12）。

背景缺陷：``WorkflowDebugAPI.start()`` 曾在 aiohttp on_startup 无条件拉起
``_poll_loop``，于是后端每次开机就开始 2Hz 空转——每 500ms 遍历全部
task × node_states、抽 graph edges、做整表 diff——**哪怕没有任何订阅者**，
甚至哪怕 ``/debug`` 因缺构建产物而 404、根本没人能打开这个面板。

现在的契约：
- ``start()`` 只标记就绪，**不**启动轮询；
- 首个订阅者到达才拉起轮询；
- 最后一个订阅者离开即停止轮询并复位快照基线；
- ``stop()`` 之后不再拉起。
"""

import asyncio

import pytest

from Module.workflow_engine import debug_api as debug_api_module
from Module.workflow_engine.debug_api import _SENTINEL, WorkflowDebugAPI

pytestmark = pytest.mark.unit


class _FakeWorkflowEngine:
    """最小 WorkflowEngine 桩——只提供 _collect_snapshot 需要的接口。"""

    def __init__(self) -> None:
        self.snapshot_calls = 0

    def _get_debug_snapshot(self) -> dict:
        """记录调用次数，返回空任务表。"""
        self.snapshot_calls += 1
        return {"tasks": []}


@pytest.fixture
def ts():
    return _FakeWorkflowEngine()


@pytest.fixture
def api(ts):
    return WorkflowDebugAPI(ts)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """把轮询间隔压到 10ms，让「是否在轮询」能在毫秒级观察。"""
    monkeypatch.setattr(debug_api_module, "_POLL_INTERVAL", 0.01)


async def _settle(rounds: int = 8) -> None:
    """让出事件循环若干轮，给轮询协程机会推进。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


class TestNoPollingWithoutSubscriber:
    """核心回归：没有订阅者 ⇒ 一次快照都不收集。"""

    @pytest.mark.asyncio
    async def test_start_does_not_spawn_polling(self, api, ts):
        await api.start()
        assert api._poll_task is None
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == 0, "start() 之后仍在无订阅者轮询"

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, api, ts):
        await api.start()
        await api.start()
        assert api._poll_task is None
        assert ts.snapshot_calls == 0

    @pytest.mark.asyncio
    async def test_snapshot_handler_does_not_spawn_polling(self, api, ts):
        """一次性读取不该把常驻轮询带起来。"""
        await api.start()
        resp = await api.snapshot_handler(None)  # type: ignore[arg-type]
        assert resp.status == 200
        assert ts.snapshot_calls == 1     # 仅本次读取
        assert api._poll_task is None
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == 1


class TestPollingFollowsSubscribers:
    """轮询生命周期 = 订阅者生命周期。"""

    @pytest.mark.asyncio
    async def test_first_subscriber_starts_polling(self, api, ts):
        await api.start()
        await api.subscribe()
        assert api._poll_task is not None
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls > 1, "订阅后轮询没有推进"

    @pytest.mark.asyncio
    async def test_subscribe_pushes_initial_snapshot(self, api, ts):
        await api.start()
        _sid, q = await api.subscribe()
        event = q.get_nowait()
        assert event["event"] == "snapshot"
        assert event["data"] == []

    @pytest.mark.asyncio
    async def test_second_subscriber_does_not_spawn_second_loop(self, api, ts):
        await api.start()
        await api.subscribe()
        first = api._poll_task
        await api.subscribe()
        assert api._poll_task is first

    @pytest.mark.asyncio
    async def test_unsubscribe_one_of_two_keeps_polling(self, api, ts):
        await api.start()
        sid_a, _qa = await api.subscribe()
        _sid_b, _qb = await api.subscribe()
        await api.unsubscribe(sid_a)
        assert api._poll_task is not None
        before = ts.snapshot_calls
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls > before

    @pytest.mark.asyncio
    async def test_last_unsubscribe_stops_polling(self, api, ts, caplog):
        await api.start()
        sid, _q = await api.subscribe()
        await asyncio.sleep(0.05)
        with caplog.at_level("INFO"):
            await api.unsubscribe(sid)
        assert api._poll_task is None
        # 懒停止必须留下可验证的痕迹（否则线上无法确认它真的停了）
        assert any("轮询已停止" in r.message for r in caplog.records)
        frozen = ts.snapshot_calls
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == frozen, "无人订阅后轮询仍在跑"

    @pytest.mark.asyncio
    async def test_resubscribe_restarts_polling(self, api, ts):
        """停止后再订阅必须能重新拉起（不能用 aclose 封闭 scope）。"""
        await api.start()
        sid, _q = await api.subscribe()
        await api.unsubscribe(sid)
        assert api._poll_task is None

        await api.subscribe()
        assert api._poll_task is not None
        before = ts.snapshot_calls
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls > before

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_sid_is_noop(self, api, ts):
        await api.start()
        await api.subscribe()
        await api.unsubscribe("不存在")
        assert api._poll_task is not None


class TestBaselineReset:
    """最后一个订阅者离开时复位 diff 基线。"""

    @pytest.mark.asyncio
    async def test_baseline_cleared_on_last_unsubscribe(self, api, ts):
        await api.start()
        sid, _q = await api.subscribe()
        assert api._last_snapshot is not None
        await api.unsubscribe(sid)
        assert api._last_snapshot is None
        assert api._last_task_ids == set()

    @pytest.mark.asyncio
    async def test_next_subscriber_gets_fresh_baseline(self, api, ts):
        """复位后新订阅者拿到的是重新采集的快照，不与停止前那一刻做 diff。"""
        await api.start()
        sid, _q = await api.subscribe()
        await api.unsubscribe(sid)
        before = ts.snapshot_calls
        _sid2, q2 = await api.subscribe()
        assert ts.snapshot_calls == before + 1
        assert q2.get_nowait()["event"] == "snapshot"

    @pytest.mark.asyncio
    async def test_baseline_kept_while_other_subscriber_remains(self, api, ts):
        await api.start()
        sid_a, _qa = await api.subscribe()
        await api.subscribe()
        await api.unsubscribe(sid_a)
        assert api._last_snapshot is not None


class TestStopIsTerminal:
    """stop() 之后不再拉起轮询。"""

    @pytest.mark.asyncio
    async def test_stop_cancels_running_polling(self, api, ts):
        await api.start()
        await api.subscribe()
        await api.stop()
        assert api._poll_task is None
        frozen = ts.snapshot_calls
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == frozen

    @pytest.mark.asyncio
    async def test_stop_notifies_subscribers(self, api, ts):
        await api.start()
        _sid, q = await api.subscribe()
        q.get_nowait()                    # 丢掉首帧快照
        await api.stop()
        assert q.get_nowait() is _SENTINEL
        assert api._subscribers == {}

    @pytest.mark.asyncio
    async def test_subscribe_after_stop_does_not_poll(self, api, ts):
        await api.start()
        await api.stop()
        _sid, q = await api.subscribe()
        assert api._poll_task is None
        assert q.get_nowait()["event"] == "snapshot"   # 首帧仍给
        frozen = ts.snapshot_calls
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == frozen

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, api, ts):
        await api.start()
        await api.stop()
        await api.stop()
        assert api._poll_task is None


class TestSubscribeBeforeStart:
    """未 start() 就订阅：降级为首帧快照 + 显式 warning，而不是静默无更新。"""

    @pytest.mark.asyncio
    async def test_subscribe_before_start_does_not_poll(self, api, ts):
        _sid, q = await api.subscribe()
        assert api._poll_task is None
        assert q.get_nowait()["event"] == "snapshot"
        await asyncio.sleep(0.05)
        assert ts.snapshot_calls == 1

    @pytest.mark.asyncio
    async def test_subscribe_before_start_warns(self, api, caplog):
        with caplog.at_level("WARNING"):
            await api.subscribe()
        assert any("start() 之前订阅" in r.message for r in caplog.records)
