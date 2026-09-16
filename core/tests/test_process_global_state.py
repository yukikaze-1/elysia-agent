# 测试文件：core/tests/test_process_global_state.py
"""S3-24：进程级单例的**契约必须钉死**，而不是靠"记得调用 reset"。

两处模块级状态：
- `core/storage/idempotency.py::_store`（幂等表门面，进程级单例）
- `core/personality/_utils.py::_SCHEDULED`（personality 域 fire-and-forget 作用域）

报告说它们的行为"依赖显式 `reset_store()` / `drain_scheduled()` 调用点，非结构性保证"。
**实测核对后的结论**（不是重构，而是先把契约钉死）：

| 事实 | 判定 |
|---|---|
| `_SCHEDULED` 是否被 `Agent.stop()` 排空 | ✅ 是（实测 0 未落定任务）|
| 幂等条目跨 `stop()` 存活 | ✅ **有意**——重启后同 crid 仍须幂等重放；`configure_store` 刻意不清 |
| 跨测试隔离 | 靠 fixture 调 `reset_store()`——**这条确实是显式约定**，故在此钉住 |

**没有实测到活跃缺陷**，所以本轮不做"把单例改成注入"的大重构（那是另一个量级的改动，
且没有证据表明必要）。取而代之：把三条隐性契约变成会红的断言——
尤其第 3 条，它钉的是一个**看起来像 bug 的正确行为**，防止将来有人"顺手清理"掉它。
"""

from __future__ import annotations

import pytest

from core.personality._utils import _SCHEDULED, drain_scheduled, _schedule
from core.storage.idempotency import configure_store, get_store, reset_store

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
def _clean():
    reset_store()
    yield
    reset_store()


@pytest.fixture(autouse=True)
def _fake_deepseek_key(monkeypatch):
    """Agent 构造要求 `DEEPSEEK_API_KEY` 非空——本模块用例内提供假 key。

    原实现在 `test_agent_stop_drains_scope` 里裸写
    `os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")`：永不还原，
    且取值依赖进程历史。改 autouse + monkeypatch——Batch 1.5 全局状态审计。
    值保持 `test-key-not-real` 不变（本模块只用它构造 Agent，不发起真实调用）。

    **必须无条件覆盖**（复核时修正）：根 `conftest.py` 的入口引导保证 `.env` 恒被加载，
    故"缺失才设"的条件**恒假**——那会让本模块用到**开发者 `.env` 里的真实 key**
    （实测：工作树下 `len=35 prefix='sk-d6c…'`，摘掉引导才是 `len=4 prefix='test'`），
    既非本意也不密闭。无条件设成假 key 后取值确定、且真实凭据不进测试进程。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")


class TestIdempotencyStoreSingleton:
    def test_reset_fully_clears(self):
        """`reset_store()` 必须**彻底**回到未配置态（跨测试隔离的全部依据）。"""
        store = get_store()
        store._repo = object()                    # noqa: SLF001 模拟已配置
        store._needs_recovery = True              # noqa: SLF001
        store._product_mode = True                # noqa: SLF001

        reset_store()

        assert store._entries == {}               # noqa: SLF001
        assert store._repo is None                # noqa: SLF001
        assert store._needs_recovery is False     # noqa: SLF001
        assert store._product_mode is False       # noqa: SLF001
        assert store.durable is False

    @pytest.mark.asyncio
    async def test_configure_does_not_clear_entries(self):
        """**钉住一个看起来像 bug 的正确行为**：`configure_store` 不得清内存条目。

        热重启（`/restart` → 再次 `create_app`）会重新 `configure_store`。若它顺手清了
        内存表，**重启后同 `clientRequestId` 的幂等重放就失效**——客户端会看到重复
        生成，而不是重放原 ACK。`reset()` 的注释写着"configure 不清（防 S2 复开）"，
        本用例把这句话变成断言，防止将来被"顺手清理"。
        """
        from core.storage.idempotency import IdempotencyEntry

        store = get_store()
        await store.store("default", "crid_x", IdempotencyEntry(
            user_message_id="u1", content_digest="d1", created_at=0.0,
            conversation_id="default"))
        assert len(store._entries) == 1            # noqa: SLF001

        configure_store(None, product_mode=False)

        assert len(store._entries) == 1, (         # noqa: SLF001
            "configure_store 清了内存条目 → 热重启后幂等重放失效（重复生成）")


class TestPersonalityScope:
    @pytest.mark.asyncio
    async def test_schedule_and_drain(self):
        """回归网：调度进去的任务能被 `drain_scheduled()` 等到。"""
        import asyncio

        done = asyncio.Event()

        async def _work() -> None:
            done.set()

        _schedule(_work())
        remaining = await drain_scheduled(timeout=2.0)

        assert done.is_set(), "任务未被调度"
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_agent_stop_drains_scope(self):
        """**结构性契约**：`Agent.stop()` 必须把 personality 域的任务排空。

        这条把"关机要 drain"从**约定**变成**断言**——新增一个 fire-and-forget 站点时，
        若它挂到了别处（不受 stop 的 flush 步骤覆盖），这里会红。
        """
        from core.agent import Agent
        from core.config import AppConfig

        agent = Agent(config=AppConfig())
        await agent.start()
        try:
            assert _SCHEDULED is not None
        finally:
            await agent.stop()

        unsettled = getattr(_SCHEDULED, "_tasks", set())
        assert len(unsettled) == 0, (
            f"Agent.stop() 后仍有 {len(unsettled)} 个未落定的 personality 任务")
