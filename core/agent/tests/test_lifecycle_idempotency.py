# 测试文件：core/agent/tests/test_lifecycle_idempotency.py
"""idempotency 生命周期接线测试（plan Task 5）— 健康探测 + 恢复 reconcile + shutdown close。

覆盖 plan Task 5：
- `_db_health_loop` probes 含 `"idempotency"`（真实 ping）；
- False→True 恢复边界触发 `_recover_dependency("idempotency")` → `repo.connect()` +
  `store.recover()`（内存→Mongo reconcile，durable 翻回 True）；
- 运行中（先健康）断开再恢复同样通过；
- `_stop_persistence` 关闭幂等仓库；两轮 start/stop 无悬挂。

Run: python -m pytest core/agent/tests/test_lifecycle_idempotency.py -v --asyncio-mode=auto
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent._lifecycle_persistence import _LifecyclePersistence
from core.input.idempotency import (
    IdempotencyEntry,
    configure_store,
    get_store,
    reset_store,
)
from core.storage.idempotency_repo import (
    ClaimResult,
    IdempotencyRepository,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    StorageUnavailableError,
)
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


@pytest.fixture
def fresh_store():
    """隔离进程级幂等表单例（配置/内存表在用例间互不污染）。"""
    reset_store()
    yield
    reset_store()


class _FlipIdemRepo:
    """可用性可翻转的幂等 repo fake（lifecycle 用例）。

    available=True → 委托真实 IdempotencyRepository（内存 FakeDocumentDBBackend），
    `store.recover()` 可真实 reconcile；False → health_check/connect 失败、操作 raise
    StorageUnavailableError（模拟 Mongo 断连）。记录 connect/close 调用供断言。
    """

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.connect_calls = 0
        self.close_calls = 0
        self._backend = FakeDocumentDBBackend()
        self._impl = IdempotencyRepository(db=self._backend)

    @property
    def is_connected(self) -> bool:
        return self.available

    async def health_check(self) -> bool:
        return self.available

    async def connect(self) -> bool:
        self.connect_calls += 1
        return self.available

    async def close(self) -> None:
        self.close_calls += 1

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    claimed_at: float | None = None,
                    expires_at: object = None) -> ClaimResult:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.claim(
            conversation_id, client_request_id,
            content_digest=content_digest, message_id=message_id, status=status,
            claimed_at=claimed_at, expires_at=expires_at)

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.get(conversation_id, client_request_id)

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       status: str = STATUS_ACCEPTED,
                       payload: dict | None = None) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.complete(conversation_id, client_request_id,
                                  status=status, payload=payload)

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     error: dict, payload: dict | None = None) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.reject(conversation_id, client_request_id,
                                error=error, payload=payload)

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.release(conversation_id, client_request_id)

    async def restore(self, doc: dict) -> dict:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.restore(doc)


class _SplitIdemRepo:
    """is_connected 与 health_check 解耦的 repo fake（BE-P1-07 首轮直接 true 用例）。

    connected: 启动建连状态（configure 的 durable 初值依据——False = 启动离线）。
    ping_ok:   当前 Mongo 可达性（health_check/connect/操作 gate——False = 断网）。
    两者独立翻转：connected=False + ping_ok=True 表达「启动建连失败、首轮探测前 Mongo
    已恢复」——首轮 health_check 直接 true、durable 仍 False（未 recover）。
    """

    def __init__(self, connected: bool, ping_ok: bool) -> None:
        self.connected = connected
        self.ping_ok = ping_ok
        self.connect_calls = 0
        self._backend = FakeDocumentDBBackend()
        self._impl = IdempotencyRepository(db=self._backend)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def health_check(self) -> bool:
        return self.ping_ok

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self.ping_ok:
            self.connected = True
        return self.ping_ok

    async def close(self) -> None:
        pass

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    claimed_at: float | None = None,
                    expires_at: object = None) -> ClaimResult:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.claim(
            conversation_id, client_request_id,
            content_digest=content_digest, message_id=message_id, status=status,
            claimed_at=claimed_at, expires_at=expires_at)

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.get(conversation_id, client_request_id)

    async def restore(self, doc: dict) -> dict:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.restore(doc)

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       status: str = STATUS_ACCEPTED,
                       payload: dict | None = None) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.complete(conversation_id, client_request_id,
                                  status=status, payload=payload)

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     error: dict, payload: dict | None = None) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.reject(conversation_id, client_request_id,
                                error=error, payload=payload)

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.release(conversation_id, client_request_id)


def _entry(message_id: str = "msg_1", digest: str = "abc") -> IdempotencyEntry:
    """内存 entry：created_at 用 monotonic 基准（不立即过期）。"""
    return IdempotencyEntry(user_message_id=message_id, content_digest=digest,
                            created_at=time.monotonic())


def _make_health_agent(repo: _FlipIdemRepo) -> _LifecyclePersistence:
    """构造 `_db_health_loop` 可用的最小 fake agent（真实 mixin 实例）。

    用真实 `_LifecyclePersistence` 实例而非 MagicMock——否则 `self._recover_dependency`
    会被 mock 自动属性遮蔽（`await MagicMock()` 抛 TypeError，测试失真）。
    session/ltm/message/token/version 恒 health_check=False（不触发各自恢复 hook），
    仅 idempotency 参与恢复边界。optional repo（attachment/generation/desktop）置 None。
    """
    agent = _LifecyclePersistence()
    for name in ("_session_manager", "_long_term", "_message_repo",
                 "_token_repo", "_version_repo"):
        m = MagicMock()
        m.health_check = AsyncMock(return_value=False)
        setattr(agent, name, m)
    for name in ("_attachment_repo", "_generation_registry", "_desktop_bridge"):
        setattr(agent, name, None)
    agent.idempotency_repo = repo
    return agent


def _make_stop_agent(repo: _FlipIdemRepo) -> _LifecyclePersistence:
    """构造 `_stop_persistence` 可用的最小 fake agent（真实 mixin 实例，其余 repo close no-op）。

    真实实例不自动建属性（对比 MagicMock）——每个子 repo 须显式 materialize。
    """
    agent = _LifecyclePersistence()
    agent.idempotency_repo = repo
    agent._desktop_bridge = None
    agent._session_manager = MagicMock()
    agent._session_manager.save_all = AsyncMock()
    agent._session_manager.drain_all_archives = AsyncMock()
    agent._long_term = MagicMock()
    agent._long_term.close = AsyncMock()
    agent._session_repo = MagicMock()
    agent._session_repo.close = AsyncMock()
    agent._message_repo = MagicMock()
    agent._message_repo.close = AsyncMock()
    agent._token_repo = MagicMock()
    agent._token_repo.close = AsyncMock()
    agent._version_repo = MagicMock()
    agent._version_repo.close = AsyncMock()
    return agent


async def _run_health_rounds(agent: object, n_rounds: int,
                             flip: Callable[[int], None] | None = None) -> None:
    """在单次 `_db_health_loop` 任务内跑 n 轮探测/恢复。

    asyncio.sleep 替换为「前 n 轮立即返回、此后抛 CancelledError」——每轮 = 一次完整
    探测 + 恢复边界判定。flip(round_no) 可选：每轮探测前回调（模拟运行中 Mongo 切换）。
    """
    rounds_done = 0

    async def _fast_sleep(*args: object, **kwargs: object) -> None:
        nonlocal rounds_done
        if rounds_done >= n_rounds:
            raise asyncio.CancelledError
        rounds_done += 1
        if flip is not None:
            flip(rounds_done)

    with mock.patch("core.agent._lifecycle_persistence.asyncio.sleep",
                    side_effect=_fast_sleep):
        task = asyncio.create_task(_LifecyclePersistence._db_health_loop(agent))
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass
    assert rounds_done == n_rounds, "health loop 未跑满预期轮次"


class TestHealthLoopProbeRecovery:
    """_db_health_loop 含 idempotency 探测；False→True 恢复边界触发 reconcile。"""

    @pytest.mark.asyncio
    async def test_offline_start_recovers_after_availability_flip(self, fresh_store):
        """Mongo 离线启动 durable=False；不重启翻转可用 → health loop 检测 False→True 后恢复。

        协调器语义：False→True 边界触发恢复（首轮离线入 prev，二轮翻转可用触发
        reconcile）；BE-P1-07 后首轮直接 healthy 也会经 _dependency_needs_recovery
        （configured and not durable）当轮触发。离线期间 dev 降级 store 的内存 entry
        经 recover() 补落库（Mongo 无 → restore 完整文档）。
        """
        repo = _FlipIdemRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is False  # 离线启动 → 非 durable

        # 离线降级窗口：store 一条内存 entry（recover 时 reconcile 用）
        result = await store.store("default", "crid_1", _entry("msg_1", "abc"))
        assert result.acquired is True
        assert store.durable is False

        agent = _make_health_agent(repo)
        # 不重启：首轮离线（prev 记录 False）→ 翻转可用 → 第二轮 False→True 触发恢复
        states = [False, True]

        def _flip(round_no: int) -> None:
            repo.available = states[round_no - 1]

        await _run_health_rounds(agent, n_rounds=2, flip=_flip)

        assert repo.connect_calls >= 1, "恢复边界应触发 repo.connect"
        # recover() reconcile：内存 entry 已补落 Mongo（Mongo 无 → claim/restore）
        doc = await repo.get("default", "crid_1")
        assert doc is not None and doc["message_id"] == "msg_1"
        # recover 末尾清两标志（_needs_recovery + _in_memory_fallback）→ durable 翻回 True
        assert store.needs_recovery is False
        assert store.durable is True

    @pytest.mark.asyncio
    async def test_healthy_disconnect_then_recover(self, fresh_store):
        """运行中（先健康）断开再恢复：健康循环感知断开，重连后再次 reconcile。"""
        repo = _FlipIdemRepo(available=True)
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is True
        agent = _make_health_agent(repo)

        # 单次 loop 三轮回合：健康基线 → 断开 → 恢复（仅第 3 轮 False→True 触发 recover）
        states = [True, False, True]

        def _flip(round_no: int) -> None:
            repo.available = states[round_no - 1]

        await _run_health_rounds(agent, n_rounds=3, flip=_flip)

        assert repo.connect_calls == 1, "仅断开后恢复那轮触发一次 connect"
        assert store.durable is True

    @pytest.mark.asyncio
    async def test_first_round_healthy_recovers(self, fresh_store):
        """BE-P1-07：离线启动 + 首轮探测前 Mongo 已恢复 → 首轮 health_check 直接 true，
        当轮执行 recover（不因 prev={} 基线跳过），记录落库、durable=true。"""
        repo = _SplitIdemRepo(connected=False, ping_ok=False)  # 启动离线（configure durable=False）
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is False
        # 降级窗口写一条内存记录
        result = await store.store("default", "crid_1", _entry("msg_1", "abc"))
        assert result.acquired is True
        assert store.needs_recovery is True
        # Mongo 在首轮探测前恢复（ping_ok 翻 true，connect 未跑）；同时另一 key 的
        # store() 成功（S2：fallback 清零但 needs_recovery 保持 → durable 仍 False）
        repo.ping_ok = True
        r2 = await store.store("default", "other_key", _entry("msg_2", "abc"))
        assert r2.acquired is True
        assert store._in_memory_fallback is False  # noqa: SLF001
        assert store.needs_recovery is True
        assert store.durable is False  # S2：无关成功操作不静默清零
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)  # 首轮直接 true
        # 当轮已 recover（needs_recovery/durable 触发，非边界）
        assert repo.connect_calls >= 1, "首轮直接 true 应触发 recover"
        doc = await repo.get("default", "crid_1")
        assert doc is not None and doc["message_id"] == "msg_1"
        assert store.durable is True
        assert store.needs_recovery is False

    @pytest.mark.asyncio
    async def test_consecutive_recover_failures_retried(self, fresh_store):
        """报告 §9#3：recover 连续失败后重试——前两轮失败、第三轮成功，每轮都重试。"""
        class _UpsertFailFake(FakeDocumentDBBackend):
            """upsert update_one 前 N 次抛 RuntimeError（restore 失败注入）。"""

            def __init__(self, fail_times: int) -> None:
                super().__init__()
                self.fail_times = fail_times

            async def update_one(self, collection, filter_expr, update, upsert=False):
                if upsert and self.fail_times > 0:
                    self.fail_times -= 1
                    raise RuntimeError("Mongo down")
                return await super().update_one(collection, filter_expr, update, upsert=upsert)

        class _RestoreFailRepo(_FlipIdemRepo):
            """restore 的 upsert 可注入失败次数的 _FlipIdemRepo。"""

            def __init__(self, fail_times: int = 0, available: bool = True) -> None:
                super().__init__(available=available)
                self._backend = _UpsertFailFake(fail_times)
                self._impl = IdempotencyRepository(db=self._backend)

        repo = _RestoreFailRepo(fail_times=2, available=False)  # 启动离线 + restore 前两次失败
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid_1", _entry("msg_1", "abc"))  # 降级
        repo.available = True
        agent = _make_health_agent(repo)
        # 三轮：r1 restore 失败、r2 失败、r3 成功——每轮都重试（不被 prev 污染跳过）
        await _run_health_rounds(agent, n_rounds=3)
        doc = await repo.get("default", "crid_1")
        assert doc is not None and doc["message_id"] == "msg_1"
        assert store.durable is True
        assert repo.connect_calls >= 3, "连续失败每轮都应重试（connect+recover）"

    @pytest.mark.asyncio
    async def test_healthy_startup_no_duplicate_compensation(self, fresh_store):
        """启动即健康且无降级记录 → 不产生破坏性重复补偿（recover no-op、不触发）。"""
        repo = _FlipIdemRepo(available=True)
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is True  # 健康启动
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)
        assert store.durable is True
        assert repo.connect_calls == 0, "durable=True → 不触发 recover（无破坏性补偿）"

    @pytest.mark.asyncio
    async def test_unconfigured_store_does_not_spin(self, fresh_store):
        """S4：store 未配置（repo=None）→ 健康循环 probe true 也不触发（configured guard）。"""
        reset_store()  # store 未配置
        repo = _FlipIdemRepo(available=True)  # agent 侧 repo 已连接
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=2)
        assert repo.connect_calls == 0, "_dependency_needs_recovery = configured and not durable"

    @pytest.mark.asyncio
    async def test_first_round_recovers_terminal_states_across_restart(self, fresh_store):
        """§8.2#7（P1-06+P1-07 组合）：首轮直接恢复 accepted/rejected → 重启后逐字一致。"""
        repo = _SplitIdemRepo(connected=False, ping_ok=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        # 降级窗口：accepted + rejected 各一条（完整负载）
        await store.store("default", "crid_a", _entry("msg_a", "da"))
        await store.complete("default", "crid_a", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        await store.store("default", "crid_r", _entry("msg_r", "dr"))
        await store.reject("default", "crid_r", code="RATE_LIMITED", message="限流",
                           retryable=True, received_at="2026-08-12T00:00:01Z",
                           client_message_id="cm2", conversation_id_wire="wire-2",
                           retry_after_ms=30000)
        # 首轮探测前 Mongo 已恢复 → 首轮直接 recover
        repo.ping_ok = True
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)
        assert store.durable is True
        assert store.needs_recovery is False
        # 重启：清内存、同 repo 重配 → 从 Mongo 逐字重放
        reset_store()
        configure_store(repo, product_mode=False)
        ea = await get_store().lookup("default", "crid_a")
        er = await get_store().lookup("default", "crid_r")
        assert ea is not None and ea.status == STATUS_ACCEPTED
        assert ea.received_at == "2026-08-12T00:00:00Z"
        assert ea.client_message_id == "cm1" and ea.conversation_id_wire == "wire-1"
        assert er is not None and er.status == STATUS_REJECTED
        assert er.error == {"code": "RATE_LIMITED", "message": "限流",
                            "retryable": True, "retryAfterMs": 30000}
        assert er.received_at == "2026-08-12T00:00:01Z"


class TestStopPersistenceClose:
    """shutdown close：_stop_persistence 关闭幂等仓库；两轮 start/stop 无悬挂。"""

    @pytest.mark.asyncio
    async def test_stop_persistence_closes_idempotency_repo(self):
        """close 被调用（try/except 包裹，repo 异常不阻断关停）。"""
        repo = _FlipIdemRepo(available=True)
        agent = _make_stop_agent(repo)
        await _LifecyclePersistence._stop_persistence(agent)
        assert repo.close_calls == 1

    @pytest.mark.asyncio
    async def test_two_start_stop_rounds_no_hang(self, fresh_store):
        """两轮 start/stop：_recover_dependency(connect+recover) 与 close 幂等可反复、无悬挂。"""
        repo = _FlipIdemRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is False
        agent = _make_stop_agent(repo)

        for _ in range(2):
            repo.available = True
            ok = await _LifecyclePersistence._recover_dependency(agent, "idempotency")
            assert ok is True
            assert store.durable is True
            await _LifecyclePersistence._stop_persistence(agent)
            repo.available = False

        assert repo.connect_calls == 2
        assert repo.close_calls == 2
