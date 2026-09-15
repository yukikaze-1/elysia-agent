"""#46 行 6：`generation` / `desktop` 两条 probe 分支的 down→up 覆盖（marker=service）。

**为什么单开一个文件**：`_db_health_round` 的恢复触发是 **False→True 边界**，而
`test_lifecycle_idempotency.py` 的 `_make_health_agent` 把 optional repo
（`_attachment_repo` / `_generation_registry` / `_desktop_bridge`）**一律置 None**——
于是这两条分支只有"探测表里有这个名字"的间接证据，**没有一条 down→up 用例**。
第一次坏掉不会有人知道。

两条分支的语义**不同**，故各自的守卫也不同：

- `generation` 有**显式恢复分支**：崩溃残留的 `running` 记录要按 5 分钟阈值标
  `interrupted`。**注意这是恢复边界的第二层**：启动路径的同类清理在
  `_start_persistence` 里另有一处（守卫见 `test_multi_session.py:335-382`），
  本文件覆盖的是"运行中掉线又恢复"那一层——此前无人覆盖。
- `desktop` **刻意没有分支**：`storage_health_check()` → `repo.health_check()` →
  基类 `_health_check()` 在 ping 失败时 `_mark_unavailable()` 并**自己重连**
  （重连会重建进程内缓存），所以补偿是"无需额外动作"。
  这里要钉的是那条**容易写错的不变量**：`_recover_dependency` 对没有分支的名字必须
  返回 `True`（= 补偿完成）。若返回 `False`，协调器会把 `results[name]` 翻回 False，
  下一轮又是 False→True 边界 → **每轮重复触发**、且该依赖在结果里永久显示不健康。
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent._lifecycle_persistence import _LifecyclePersistence

pytestmark = pytest.mark.service


def _make_agent() -> _LifecyclePersistence:
    """最小 fake agent（真实 mixin 实例）。

    必填依赖（session/ltm/message/token/version + idempotency）恒 health_check=False
    → 不触发各自的恢复 hook，把观测面收敛到本轮要测的两条 optional 分支。
    用真实 `_LifecyclePersistence` 而非 MagicMock：mock 的 `_recover_dependency`
    是自动属性，`await MagicMock()` 会 TypeError，测试会失真。
    """
    agent = _LifecyclePersistence()
    for name in ("_session_manager", "_long_term", "_message_repo",
                 "_token_repo", "_version_repo"):
        m = MagicMock()
        m.health_check = AsyncMock(return_value=False)
        setattr(agent, name, m)
    agent.idempotency_repo = MagicMock()
    agent.idempotency_repo.health_check = AsyncMock(return_value=False)
    agent._attachment_repo = None
    agent._generation_registry = None
    agent._desktop_bridge = None
    return agent


async def _rounds(agent: Any, n: int) -> dict[str, bool]:
    """直接驱动 n 轮探测/恢复（不 patch asyncio.sleep——会误伤其它后台循环）。"""
    prev: dict[str, bool] = {}
    for _ in range(n):
        prev = await _LifecyclePersistence._db_health_round(agent, prev)  # type: ignore[arg-type]
    return prev


class TestGenerationProbeRecovery:
    """"generation" 探测 + 崩溃残留清理的 down→up 边界。"""

    @pytest.mark.asyncio
    async def test_down_then_up_triggers_stale_cleanup_exactly_once(self):
        agent = _make_agent()
        registry = MagicMock()
        # 第 1 轮离线，第 2 轮起恢复
        registry.health_check = AsyncMock(side_effect=[False, True, True])
        registry.mark_stale_interrupted = AsyncMock(return_value=3)
        agent._generation_registry = registry

        results = await _rounds(agent, 3)

        assert "generation" in results, "generation 不在探测表里 → 掉线不可观测"
        assert results["generation"] is True
        # 恢复必须**只**在 False→True 边界触发一次（每轮都清 = 无谓的库往返）
        registry.mark_stale_interrupted.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_cutoff_is_five_minutes_before_now(self):
        """阈值是"5 分钟前仍 running"——它决定哪些崩溃残留会被标 interrupted。

        注意**必须走 down→up 边界**才能看到它：正常启动（首轮即 healthy）时
        `_db_health_round` 不会触发恢复，因为**启动路径的同类清理**在
        `_start_persistence` 里已经做过（其守卫在 `test_multi_session.py:335-382`）。
        两层各管一段：启动那一层管"上个进程的残留"，这一层管"运行中掉线又恢复"。
        """
        agent = _make_agent()
        registry = MagicMock()
        registry.health_check = AsyncMock(side_effect=[False, True])
        registry.mark_stale_interrupted = AsyncMock(return_value=0)
        agent._generation_registry = registry

        before = datetime.datetime.now(datetime.timezone.utc)
        await _rounds(agent, 2)
        after = datetime.datetime.now(datetime.timezone.utc)

        cutoff = registry.mark_stale_interrupted.await_args.args[0]
        parsed = datetime.datetime.fromisoformat(cutoff)
        assert before - datetime.timedelta(minutes=6) < parsed < after - datetime.timedelta(minutes=4), (
            f"cutoff 不是「5 分钟前」：{cutoff}")

    @pytest.mark.asyncio
    async def test_still_offline_does_not_clean(self):
        """离线时不得触发清理（那时 repo 不可达，调用只会白费一次往返）。"""
        agent = _make_agent()
        registry = MagicMock()
        registry.health_check = AsyncMock(return_value=False)
        registry.mark_stale_interrupted = AsyncMock(return_value=0)
        agent._generation_registry = registry

        results = await _rounds(agent, 3)

        assert results["generation"] is False
        registry.mark_stale_interrupted.assert_not_awaited()


class TestDesktopProbeRecovery:
    """"desktop" 探测 + "无分支即无需补偿"这条不变量的 down→up 边界。"""

    @pytest.mark.asyncio
    async def test_down_then_up_recovers_without_extra_compensation(self):
        agent = _make_agent()
        bridge = MagicMock()
        bridge.storage_health_check = AsyncMock(side_effect=[False, True, True])
        agent._desktop_bridge = bridge

        results = await _rounds(agent, 3)

        assert "desktop" in results, "desktop 不在探测表里 → 桌面存储掉线不可观测"
        assert results["desktop"] is True, (
            "desktop 没有显式恢复分支，`_recover_dependency` 必须返回 True——"
            "返回 False 会让协调器把该依赖翻回不健康并在下一轮重复触发")
        # 启动清理（mark_stale_expired）只在 bridge.connect_db() 里跑，不在恢复边界
        bridge.mark_stale_expired.assert_not_called()

    @pytest.mark.asyncio
    async def test_unhandled_dependency_recovery_returns_true(self):
        """钉住上一条的根因：**未知依赖名**的恢复必须报"完成"而不是"失败"。"""
        agent = _make_agent()
        agent._desktop_bridge = MagicMock()
        assert await agent._recover_dependency("desktop") is True  # noqa: SLF001
        assert await agent._recover_dependency("某个未来新增的依赖") is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_recovery_failure_still_reported_false(self):
        """反向对照：**真有分支且失败**时必须返回 False（否则"永不重试"）。

        用 identity 分支做对照——它 `store is None` 之外的失败路径会 `return False`。
        """
        agent = _make_agent()
        store = MagicMock()
        store.is_authoritative = True
        store.is_available = False
        store.connect = AsyncMock(return_value=None)   # 装载失败
        agent._user_identity_store = store
        agent._user_name = "u"

        assert await agent._recover_dependency("identity") is False  # noqa: SLF001
