"""Agent 公开只读接口测试。

验证 27 个 @property 存在且返回正确的内部对象，以及状态写方法。
因实例化 Agent（不调 start()），标记 service。
注：token_tracker/prompt_recorder/response_recorder 在 __init__ 初始化为 None
（start() 才创建），pre-start 断言 is None；started_at 仅 start() 赋值，断言 AttributeError。
"""

import pytest
from core.agent import Agent, RunState
from core.config import AppConfig

pytestmark = pytest.mark.service

# (property 名, 内部属性名, 是否启动期创建)
# 注：session 特例——批次 3 起直通 session_manager.active，单独测试
# 注：character_engine/character_reflector 为 M3 computed（经 registry 解析 instance），
#    非 attr 直通——下方 TestCharacterM3Properties 单独验证。
SUBSYSTEM_PROPS = [
    ("long_term", "_long_term", False),
    ("message_repo", "_message_repo", False),
    ("session_repo", "_session_repo", False),
    ("memory_repo", "_memory_repo", False),
    ("token_repo", "_token_repo", False),
    ("tool_adapter", "_tool_adapter", False),
    ("output", "_output", False),
    ("llm", "_llm", False),
    ("tts", "_tts", False),
    ("config", "_config", False),
    ("bus", "_bus", False),
    ("cognitive", "_cognitive", False),
    ("idle", "_idle", False),
    ("scheduler", "_scheduler", False),
    ("consolidator", "_consolidator", False),
    ("memory_reflector", "_memory_reflector", False),
    ("character", "_character", False),
]
STARTUP_PROPS = [
    ("token_tracker", "_token_tracker"),
    ("prompt_recorder", "_prompt_recorder"),
    ("response_recorder", "_response_recorder"),
    ("started_at", "_started_at"),
]
SCALAR_PROPS = [("user_name", "_user_name"), ("state", "_state"), ("stt_ok", "_stt_ok")]


@pytest.fixture
def agent():
    return Agent(config=AppConfig())


class TestSubsystemProperties:
    @pytest.mark.parametrize("pub,priv,_", SUBSYSTEM_PROPS)
    def test_returns_internal_object(self, agent, pub, priv, _):
        assert getattr(agent, pub) is getattr(agent, priv)

    def test_session_points_to_session_manager_active(self, agent):
        """批次 3：session property 直通 session_manager.active。"""
        assert agent.session is agent.session_manager.active
        assert agent.active_session_id == "default"


class TestCharacterM3Properties:
    """M3（Task 11）：character_engine/character_reflector 经 registry 解析 instance。

    非 attr 直通（此前 _character_engine 全局单例已删）——character_engine 返回
    当前 instance 的 engine，character_reflector 返回其 engine.reflector。
    """

    def test_character_engine_resolves_via_registry(self, agent):
        """character_engine 返回 registry 中默认 instance 的 engine（非全局单例）。"""
        binding = agent.session_manager.active.character_binding
        assert binding == {"character_id": "chr_builtin_elysia",
                           "instance_id": "chi_builtin_elysia"}
        engine = agent.character_engine
        assert engine is not None
        # 经 registry 解析 → 同一 instance 命中同一 runtime，多次访问同 engine
        assert agent.character_engine is engine

    def test_character_reflector_resolves_from_engine(self, agent):
        """character_reflector 返回当前 instance engine 的 reflector。"""
        assert agent.character_reflector is agent.character_engine._reflector


class TestStartupProperties:
    @pytest.mark.parametrize("pub,priv", [p for p in STARTUP_PROPS if p[0] != "started_at"])
    def test_none_before_start(self, agent, pub, priv):
        # __init__ 中初始化为 None（start() 才创建），getter 返回 None 不抛异常
        assert getattr(agent, pub) is None

    def test_started_at_raises_before_start(self, agent):
        # _started_at 仅在 start() 赋值，pre-start 访问抛 AttributeError
        with pytest.raises(AttributeError):
            agent.started_at


class TestScalarProperties:
    @pytest.mark.parametrize("pub,priv", SCALAR_PROPS)
    def test_returns_value(self, agent, pub, priv):
        assert getattr(agent, pub) == getattr(agent, priv)


class TestWriteMethods:
    def test_request_shutdown_sets_state(self, agent):
        agent.request_shutdown()
        assert agent.state == RunState.STOPPING

    def test_request_restart_sets_state(self, agent):
        agent.request_restart()
        assert agent.state == RunState.RESTARTING


class TestPurgeMessagePublic:
    def test_purge_is_public_method(self, agent):
        assert callable(agent.purge_message_from_session)


class TestSttPublicInterface:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stt_slot_public_interface(self, agent):
        """agent.stt 公开只读槽位 + stt_transcribe 空守卫 + 三态回退。"""
        assert agent.stt is None                 # 初始 None
        assert agent.stt_enabled is False        # 三态回退：_stt_enabled=None → 有效 = config.stt.enabled
                                                 #（AppConfig 默认 False；不用 bool 默认 true——spec 明令禁止）
        assert isinstance(agent.stt_emotion_state, dict)
        assert await agent.stt_transcribe(b"audio") == ""   # None 空守卫，不 AttributeError

    @pytest.mark.unit
    def test_stt_is_readonly(self, agent):
        """agent.stt 只读无 setter——写入抛 AttributeError（注入用 agent._stt）。"""
        with pytest.raises(AttributeError):
            agent.stt = None

    @pytest.mark.unit
    def test_enabled_three_state_fallback(self, agent):
        """三态回退：_tts_enabled=None → 有效 = config.tts.enabled（默认 False）。"""
        assert agent.tts_enabled is False
