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

    async def restore(self, doc: dict) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.restore(doc)


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

        协调器语义：首轮 prev={} 只记录基线（`name not in prev` 不触发恢复），恢复仅在
        「上一轮 False → 本轮 True」边界触发——故离线启动需两轮（第 1 轮离线入 prev，
        第 2 轮翻转可用触发 reconcile）。离线期间 dev 降级 store 的内存 entry 经
        recover() 补落库（Mongo 无 → claim）。
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
