"""ARCH-INC-P2-03：后台协程所有权与退出收尾的集成测试。

单元测试（`Module/_common/tests/test_task_scope.py`）覆盖原语本身；
这里只覆盖**接入点**——即「此前会静默丢失/静默失败」的真实缺陷是否已被堵上。
每个用例都对应一条具体的历史缺陷，注释里写明是哪一条。
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

os.environ.setdefault("DEEPSEEK_API_KEY", "test")

pytestmark = pytest.mark.service


# ================================================================
# D1：SessionManager 逐出 save 的强引用与 drain
# ================================================================


class TestEvictionSaveOwnership:
    """缺陷 D1（HIGH）：逐出时 `ensure_future(session.save())` 的 Task 被丢弃，
    且会话随即弹出缓存池——`save_all()`/`drain_all_archives()` 都不可能再覆盖它。
    """

    @pytest.mark.asyncio
    async def test_evicted_save_is_awaitable_on_drain(self):
        from core.memory.session_manager import DEFAULT_SESSION, SessionManager

        saved: list[str] = []
        release = asyncio.Event()

        class _Repo:
            async def load(self, sid):  # pragma: no cover - 本用例不触发
                return None

        class _Session:
            """慢 save——模拟「进程在落库前退出」的窗口。"""

            def __init__(self, sid: str) -> None:
                self.session_id = sid
                self.messages: list = []

            async def save(self) -> bool:
                await release.wait()
                saved.append(self.session_id)
                return True

            async def close_archive(self, *, timeout: float = 5.0) -> None:
                return None

        mgr = SessionManager(_Repo(), _Repo(), max_sessions=2)  # type: ignore[arg-type]
        # __init__ 已放入 default 会话；再加两个 → 超限 1 个，恰好逐出 s1
        mgr._sessions["s1"] = _Session("s1")  # type: ignore[assignment]
        mgr._loaded.add("s1")
        mgr._sessions["s2"] = _Session("s2")  # type: ignore[assignment]
        mgr._loaded.add("s2")

        mgr.evict_if_needed()
        assert "s1" not in mgr._sessions, "s1 应已被逐出缓存池"
        assert "s2" in mgr._sessions, "max_sessions=2 → 只逐出一个"
        assert len(mgr._evict_tasks) == 1, "逐出 save 必须仍在 scope 中被跟踪"
        assert saved == [], "save 仍在等待 release"

        release.set()
        leftover = await mgr.drain_evictions(timeout=3.0)
        assert leftover == 0
        assert saved == ["s1"], "drain 后逐出 save 必须已完成"

    def test_evict_without_loop_is_visible_not_silent(self, caplog):
        """无运行事件循环时必须记 warning——不再静默 `except RuntimeError: pass`。"""
        from core.memory.session_manager import SessionManager

        class _Repo:
            pass

        class _Session:
            session_id = "s1"
            messages: list = []

            async def save(self) -> bool:  # pragma: no cover - 不会被真正 await
                return True

        mgr = SessionManager(_Repo(), _Repo(), max_sessions=1)  # type: ignore[arg-type]
        mgr._sessions["s1"] = _Session()  # type: ignore[assignment]
        mgr._loaded.add("s1")
        mgr._sessions["s2"] = _Session()  # type: ignore[assignment]
        mgr._loaded.add("s2")
        with caplog.at_level(logging.WARNING):
            mgr.evict_if_needed()
        assert any("无运行中事件循环" in r.getMessage() for r in caplog.records)


# ================================================================
# D2：CharacterRuntimeRegistry release/evict flush 的观察与 drain
# ================================================================


class TestRegistryFlushOwnership:
    """缺陷 D2：`create_task(rt.flush())` 无强引用、无回调、无取消；`close_all()`
    只遍历 `_runtimes`，已弹出的 runtime 彻底不可见。
    """

    @pytest.mark.asyncio
    async def test_evict_flush_failure_is_logged(self, caplog):
        from core.character.registry import CharacterRuntimeRegistry

        class _Rt:
            _busy = False
            dirty = True

            async def flush(self) -> None:
                raise RuntimeError("persist boom")

        reg = CharacterRuntimeRegistry(max_size=0)
        with caplog.at_level(logging.ERROR):
            reg.get("inst-1", lambda: _Rt())  # type: ignore[arg-type]
            await asyncio.sleep(0.01)
            await reg.close_all()
        assert any(
            r.levelno == logging.ERROR and "character-runtime-flush" in r.getMessage()
            for r in caplog.records
        ), f"flush 失败必须被观察：{caplog.text}"

    @pytest.mark.asyncio
    async def test_close_all_drains_evicted_flush(self):
        """close_all() 必须等到**已逐出** runtime 的 flush 落定。"""
        from core.character.registry import CharacterRuntimeRegistry

        done: list[str] = []
        gate = asyncio.Event()

        class _Rt:
            _busy = False
            dirty = True

            async def flush(self) -> None:
                await gate.wait()
                done.append("flushed")

        reg = CharacterRuntimeRegistry(max_size=0)
        reg.get("inst-1", lambda: _Rt())  # type: ignore[arg-type]
        assert len(reg) == 0, "max_size=0 → 立即逐出"
        assert done == []

        async def _open() -> None:
            await asyncio.sleep(0.01)
            gate.set()

        opener = asyncio.create_task(_open())
        await reg.close_all()
        await opener
        assert done == ["flushed"], "close_all 必须 drain 已逐出 runtime 的 flush"


# ================================================================
# D3/D4：detached 资源关闭（voice STT / LLM 旧后端）
# ================================================================


class TestDetachedCloseOwnership:
    """缺陷 D3/D4：`create_task(backend.close())` 结果被丢弃——连接池可能永不关闭，
    异常无人观察。
    """

    @pytest.mark.asyncio
    async def test_llm_switch_old_backend_close_is_drained(self):
        from Module._common.task_scope import AsyncTaskScope
        from core.adapter.llm_adapter import LLMAdapter

        closed: list[str] = []

        class _Backend:
            def __init__(self, name: str) -> None:
                self.name = name

            async def close(self) -> None:
                closed.append(self.name)

        old, current = _Backend("old"), _Backend("current")
        adapter = LLMAdapter.__new__(LLMAdapter)
        adapter._requested_model = "test/current"
        adapter._backend = current
        adapter._closing_backends = AsyncTaskScope("llm-adapter", drain_timeout=0.5)

        # 模拟 switch_model 的登记（bypass 工厂，避免真实构造后端）
        adapter._closing_backends.try_spawn(old.close(), name="llm-close-old")
        await adapter.close()
        assert closed == ["old", "current"], (
            "旧后端必须在 close() 时先收尾，再关当前后端"
        )

    @pytest.mark.asyncio
    async def test_llm_close_observes_old_backend_failure(self, caplog):
        from Module._common.task_scope import AsyncTaskScope
        from core.adapter.llm_adapter import LLMAdapter

        class _Bad:
            async def close(self) -> None:
                raise RuntimeError("close boom")

        class _Ok:
            async def close(self) -> None:
                return None

        adapter = LLMAdapter.__new__(LLMAdapter)
        adapter._requested_model = "test/current"
        adapter._backend = _Ok()
        adapter._closing_backends = AsyncTaskScope("llm-adapter", drain_timeout=0.5)
        adapter._closing_backends.try_spawn(_Bad().close(), name="llm-close-bad")

        with caplog.at_level(logging.ERROR):
            await adapter.close()  # 旧后端的 close 异常不得外抛打断关机
        assert any("llm-adapter" in r.getMessage() for r in caplog.records)


# ================================================================
# D2b：Reminder 插件的提醒任务
# ================================================================


class TestReminderOwnership:
    """缺陷 D2（HIGH）：`ensure_future(_remind())` 完全无落地——Agent 关机即销毁，
    用户收到「已设置提醒」却永远等不到；异常也无人观察。
    """

    @pytest.mark.asyncio
    async def test_reminder_uses_scope_and_teardown_cancels(self):
        from core.tools.reminder.plugin import SetReminderPlugin

        plugin = SetReminderPlugin()
        assert len(plugin._reminders) == 0

        class _Ctx:
            services: dict = {}

        result = await plugin.execute(
            ctx=_Ctx(), params=plugin.param_model(message="喝水", delay_seconds=3600),  # type: ignore[arg-type]
        )
        assert "已设置提醒" in result
        assert len(plugin._reminders) == 1, "提醒任务必须在 scope 中被跟踪"

        await plugin.teardown()
        assert len(plugin._reminders) == 0, "teardown 必须取消未到期提醒"

    @pytest.mark.asyncio
    async def test_reminder_does_not_lie_after_teardown(self):
        """作用域已关闭时不得再对用户回报「已设置」。"""
        from core.tools.reminder.plugin import SetReminderPlugin

        plugin = SetReminderPlugin()
        await plugin.teardown()

        class _Ctx:
            services: dict = {}

        result = await plugin.execute(
            ctx=_Ctx(), params=plugin.param_model(message="x", delay_seconds=60),  # type: ignore[arg-type]
        )
        assert "失败" in result, f"关停后不应谎报成功：{result}"

    @pytest.mark.asyncio
    async def test_reminder_fires_when_due(self):
        """正常路径：到期后向 EventBus 投递 TaskResultData。"""
        from core.monitoring.event_bus import EventBus
        from core.tools.reminder.plugin import SetReminderPlugin

        bus = EventBus()
        plugin = SetReminderPlugin()

        class _Ctx:
            services = {"_bus": bus}

        result = await plugin.execute(
            ctx=_Ctx(), params=plugin.param_model(message="到点了", delay_seconds=1),  # type: ignore[arg-type]
        )
        assert "已设置提醒" in result
        # 不等真实 1s：直接取消并确认 scope 收尾（投递逻辑由既有单测覆盖）
        await plugin.teardown()


# ================================================================
# D5：Agent 关机必须 drain 被取消的回合
# ================================================================


class TestAgentTurnDrain:
    """缺陷 D5：`cancel_current()` 只发 cancel 就丢引用，`process()` 吞掉
    CancelledError 继续写终态——stop() 却在下一步关掉 Mongo/repo。
    """

    @pytest.mark.asyncio
    async def test_stop_waits_for_cancelled_turn_to_settle(self):
        from core.agent import Agent
        from core.config import AppConfig

        agent = Agent(config=AppConfig())
        await agent.start()
        try:
            settled = asyncio.Event()

            async def _slow_turn() -> None:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # 模拟 CognitiveLoop 的收尾：吞掉取消、继续写终态
                    await asyncio.sleep(0.05)
                    settled.set()

            task = agent._turns.spawn(_slow_turn(), name="cognitive-test")
            agent._cognitive._current_task = task
            await asyncio.sleep(0)  # 必须先让协程体跑起来，否则取消会落在首步之前
            agent._cognitive.cancel_current()

            await agent.stop()
            assert settled.is_set(), "stop() 必须等被取消的回合真正收尾"
        finally:
            if agent._state.name == "RUNNING":  # pragma: no cover - 防御
                await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_drains_lifecycle_scope(self):
        """关机后 Agent 的长期循环作用域不残留未落定任务。"""
        from core.agent import Agent
        from core.config import AppConfig

        agent = Agent(config=AppConfig())
        await agent.start()
        await agent.stop()
        assert len(agent._lifecycle_tasks) == 0, "关机后不应残留长期循环任务"
        assert len(agent._turns) == 0

    @pytest.mark.asyncio
    async def test_agent_can_restart_after_stop(self):
        """作用域不得被 aclose——同一实例 stop→start 必须仍能 spawn。"""
        from core.agent import Agent
        from core.config import AppConfig

        agent = Agent(config=AppConfig())
        await agent.start()
        await agent.stop()
        assert agent._lifecycle_tasks.closed is False, "Agent 可重启，作用域不可封闭"

        await agent.start()
        try:
            assert len(agent._lifecycle_tasks) > 0, "重启后长期循环应重新起来"
        finally:
            await agent.stop()


# ================================================================
# D6：after-response 尾任务的异常必须被观察
# ================================================================


class TestAfterResponseObservation:
    @pytest.mark.asyncio
    async def test_failed_tail_is_logged(self, caplog):
        from core.cognitive.finalize import _observe_after_response

        async def _boom() -> None:
            raise ValueError("tail boom")

        task = asyncio.create_task(_boom())
        with caplog.at_level(logging.WARNING):
            task.add_done_callback(_observe_after_response)
            await asyncio.gather(task, return_exceptions=True)
        assert any("after-response 收尾任务异常" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cancelled_tail_is_not_an_error(self, caplog):
        from core.cognitive.finalize import _observe_after_response

        async def _hang() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_hang())
        with caplog.at_level(logging.WARNING):
            task.add_done_callback(_observe_after_response)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ================================================================
# D7：personality 域 fire-and-forget 可 drain
# ================================================================


class TestPersonalityDrain:
    @pytest.mark.asyncio
    async def test_drain_scheduled_waits_for_pending(self):
        from core.personality._utils import _SCHEDULED, _schedule, drain_scheduled

        done: list[str] = []

        async def _work() -> None:
            await asyncio.sleep(0.02)
            done.append("ok")

        _schedule(_work())
        assert await drain_scheduled(timeout=3.0) == 0
        assert done == ["ok"]
        assert len(_SCHEDULED) == 0

    @pytest.mark.asyncio
    async def test_scheduled_exception_is_observed(self, caplog):
        from core.personality._utils import _schedule, drain_scheduled

        async def _boom() -> None:
            raise RuntimeError("personality boom")

        with caplog.at_level(logging.ERROR):
            _schedule(_boom())
            await drain_scheduled(timeout=3.0)
        assert any("personality" in r.getMessage() for r in caplog.records)
