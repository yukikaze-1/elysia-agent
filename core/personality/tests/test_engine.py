"""PersonalityEngine 单元测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.personality import PersonalityEngine
from core.memory.session import SessionMemory


def _make_engine(session=None, llm=None):
    """创建测试用 PersonalityEngine。"""
    s = session or SessionMemory()
    m = llm or MagicMock()
    m.think = AsyncMock()
    return PersonalityEngine(
        llm=m,
        session=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
    )


@pytest.mark.unit
class TestPersonalityEngineInit:
    def test_loads_l1_core(self):
        """初始化时存储 L1 Core。"""
        engine = _make_engine()
        assert engine.render_c1()["core_personality"] == "你是 Elysia。温柔底色。"

    def test_l2_self_is_empty_initially(self):
        """初始 L2 Self 为空。"""
        engine = _make_engine()
        assert engine.render_c1()["self_personality"] == ""


@pytest.mark.unit
class TestPersonalityEngineC9:
    def test_c9_empty_when_no_state(self):
        """无 L3 State 时 C9 返回空。"""
        engine = _make_engine()
        assert engine.render_c9()["awareness_text"] == ""

    def test_c9_returns_state_when_triggered(self):
        """L3 State 存在且触发条件满足时 C9 返回内容。"""
        s = SessionMemory()
        engine = _make_engine(session=s)
        # 模拟 L3 state
        s.set_param("state_l3", "最近比较敷衍")
        s.set_param("state_history", [{"timestamp": 0, "content": ""}])
        # 手动触发一个事件以确保 awareness 激活
        asyncio.run(engine.consume_trigger("test", {"content": "测试触发"}))
        result = engine.render_c9()
        assert result["awareness_text"]


@pytest.mark.unit
class TestPersonalityEngineTrigger:
    def test_consume_trigger_updates_state_l3(self):
        """consume_trigger 更新 session 中的 state_l3 和 history。"""
        s = SessionMemory()
        engine = _make_engine(session=s)
        asyncio.run(engine.consume_trigger(
            "reflector",
            {"content": "最近变得敷衍了"},
        ))
        assert s.get_param("state_l3")
        assert "敷衍" in s.get_param("state_l3")
        history = s.get_param("state_history")
        assert len(history) == 1

    def test_consume_trigger_no_llm_for_simple_state(self):
        """简单状态更新不调用 LLM。"""
        engine = _make_engine()
        asyncio.run(engine.consume_trigger("test", {"content": "test state"}))
        engine._llm.think.assert_not_called()


@pytest.mark.unit
class TestPersonalityEngineRestore:
    def test_restore_state_rebuilds_from_session(self):
        """restore_state 从 session 恢复 L2/L3。"""
        s = SessionMemory()
        s.set_param("self_l2", "我变得更信任妖梦了")
        s.set_param("state_l3", "最近有点累")
        engine = _make_engine(session=s)
        engine.restore_state()
        assert engine.render_c1()["self_personality"] == "我变得更信任妖梦了"
        assert engine.render_c9()["awareness_text"]
