"""CharacterEngine 单元测试。"""
import asyncio
import time
from core.schema.utils import now_iso
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.personality import CharacterEngine
from core.adapter.llm_adapter import ThoughtResult
from core.schema.character import CharacterState
from core.schema.types import SessionId


@pytest.fixture
def engine_with_mocks():
    """创建带完整 mock 的 CharacterEngine，供 service 层测试使用。"""
    s = CharacterState(session_id=SessionId("test-sid"))
    m = MagicMock()
    m.think = AsyncMock(return_value=ThoughtResult(text=""))
    o = MagicMock()
    o.expression = MagicMock(return_value=MagicMock())
    from core.config import CharacterConfig
    char = CharacterConfig(name="Elysia")
    engine = CharacterEngine(
        llm=m,
        state=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
        character_config=char,
        output=o,
    )
    # 确保 _pm 可用（tests/ → personality/ → core/ → prompts/）
    from pathlib import Path
    from Module.system_prompt import PromptManager
    _PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
    engine._pm = PromptManager(str(_PROMPTS_DIR))
    return engine


def _make_engine(state=None, llm=None, output=None):
    """创建测试用 CharacterEngine。"""
    s = state or CharacterState(session_id=SessionId("test-sid"))
    m = llm or MagicMock()
    m.think = AsyncMock(return_value=ThoughtResult(text=""))
    o = output or MagicMock()
    o.expression = MagicMock(return_value=MagicMock())
    from core.config import CharacterConfig
    char = CharacterConfig(name="Elysia")
    return CharacterEngine(
        llm=m,
        state=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
        character_config=char,
        output=o,
    )


@pytest.mark.unit
class TestCharacterEngineInit:
    def test_loads_l1_core(self):
        """初始化时存储 L1 Core。"""
        engine = _make_engine()
        assert engine.render_c1().core_personality == "你是 Elysia。温柔底色。"

    def test_l2_self_is_empty_initially(self):
        """初始 L2 Self 为空。"""
        engine = _make_engine()
        assert engine.render_c1().self_personality == ""

    def test_engine_instance_identity(self):
        """构造可传 instance_id/character_revision 身份标记（M1/M2 仅记录，不绑定 runtime）。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        m = MagicMock()
        m.think = AsyncMock(return_value=ThoughtResult(text=""))
        o = MagicMock()
        o.expression = MagicMock(return_value=MagicMock())
        from core.config import CharacterConfig
        char = CharacterConfig(name="Elysia")
        engine = CharacterEngine(
            llm=m,
            state=s,
            character_name="Elysia",
            user_name="妖梦",
            base_personality="你是 Elysia。温柔底色。",
            character_config=char,
            output=o,
            instance_id="chi_builtin_elysia",
            character_revision=1,
        )
        assert engine._instance_id == "chi_builtin_elysia"
        assert engine._character_revision == 1


@pytest.mark.unit
class TestCharacterEngineC9:
    def test_c9_empty_when_no_state(self):
        """无 L3 State 时 C9 返回空。"""
        engine = _make_engine()
        assert engine.render_c9().awareness_text == ""

    def test_c9_returns_state_when_triggered(self):
        """approved_awareness_text 存在且触发条件满足时 C9 返回内容。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        s.approved_awareness_text = "最近比较敷衍"
        engine = _make_engine(state=s)
        result = engine.render_c9()
        assert result.awareness_text == "最近比较敷衍"


@pytest.mark.unit
class TestCharacterEngineTrigger:
    def test_consume_trigger_updates_state_l3(self):
        """consume_trigger 更新 state 中的 state_l3 和 history。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        asyncio.run(engine.consume_trigger(
            "reflector",
            {"content": "最近变得敷衍了"},
        ))
        assert s.state_l3
        assert "敷衍" in s.state_l3
        assert len(s.state_history) == 1

    def test_consume_trigger_no_llm_for_simple_state(self):
        """简单状态更新不调用 LLM。"""
        engine = _make_engine()
        asyncio.run(engine.consume_trigger("test", {"content": "test state"}))
        engine._llm.think.assert_not_called()  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.unit
class TestCharacterEngineCompression:
    """L3→L2 压缩测试。"""

    @staticmethod
    def _fill_state_history(state, count: int, base_text: str = "状态变化"):
        """填充 state_history 到指定条数。"""
        for i in range(count):
            state.state_history.append({"timestamp": now_iso(), "content": f"{base_text} #{i+1}"})

    def test_should_compress_false_when_too_few_entries(self):
        """state_history 不足 3 条时不触发压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 2)
        assert not engine._should_compress()

    def test_should_compress_true_when_enough_new_entries(self):
        """state_history 新增 >= 5 条时触发压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        assert engine._should_compress()

    def test_should_compress_false_when_not_enough_new(self):
        """上次压缩后新增不足 5 条时不触发。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 7)
        s.compression_last_count = 3  # 仅 4 条新
        assert not engine._should_compress()

    def test_compress_updates_self_l2(self):
        """成功压缩后 self_l2 被写入。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        s.state_l3 = "最近变得温柔了"
        engine._llm.think.return_value = ThoughtResult(  # type: ignore[reportAttributeAccessIssue]
            text="<new_self>我似乎更在意妖梦的感受了</new_self>\n<changes>\n- 更在意妖梦了\n</changes>"
        )

        # 模拟 PromptManager
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        engine._refresh_c6 = AsyncMock()  # 隔离 _refresh_c6 测试
        asyncio.run(engine._compress())

        assert s.self_l2 == "我似乎更在意妖梦的感受了"
        assert s.compression_last_count == 5
        assert s.compression_last_at is not None
        assert s.compression_last_at > 0

    def test_compress_no_overwrite_on_empty_result(self):
        """LLM 返回空时更新追踪但不覆盖已有 L2。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        s.self_l2 = "已有自我认知"
        self._fill_state_history(s, 5)
        engine._llm.think.return_value = ThoughtResult(text="")  # type: ignore[reportAttributeAccessIssue]
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        engine._refresh_c6 = AsyncMock()  # 隔离 _refresh_c6 测试
        asyncio.run(engine._compress())

        assert s.self_l2 == "已有自我认知"  # 不变
        assert s.compression_last_count == 5  # 追踪更新

    def test_compress_llm_failure_preserves_state(self):
        """LLM 调用失败时追踪状态不变，下次重试。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        engine._llm.think.side_effect = RuntimeError("API 不可用")  # type: ignore[reportAttributeAccessIssue]
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        engine._refresh_c6 = AsyncMock()  # 隔离 _refresh_c6 测试
        asyncio.run(engine._compress())

        assert s.compression_last_count == 0  # 不变
        assert s.compression_last_at is None  # 不变

    def test_lock_prevents_concurrent_compression(self):
        """Lock 被占用时跳过本次压缩。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        engine = _make_engine(state=s)
        self._fill_state_history(s, 5)
        engine._llm.think.return_value = ThoughtResult(text="test")  # type: ignore[reportAttributeAccessIssue]
        mock_pm = MagicMock()
        mock_pm.render_macro.return_value = "rendered prompt"
        engine._pm = mock_pm

        # 手动抢占 lock
        held = asyncio.run(self._acquire_lock(engine._compression._compression_lock))
        assert held

        # 此时 _maybe_compress 应跳过
        asyncio.run(engine._maybe_compress())
        assert s.compression_last_count == 0  # 未被压缩

        # 释放后再次 trigger 应成功
        engine._compression._compression_lock.release()
        engine._refresh_c6 = AsyncMock()  # 隔离 _refresh_c6 测试
        asyncio.run(engine._maybe_compress())
        assert s.compression_last_count == 5

    @staticmethod
    async def _acquire_lock(lock) -> bool:
        await lock.acquire()
        return True


@pytest.mark.service
class TestC6Refresh:
    """_refresh_c6() 测试。"""

    @pytest.mark.asyncio
    async def test_refresh_c6_generates(self, engine_with_mocks):
        """_refresh_c6() 调用 LLM 并更新 state.speaking_style_c6。"""
        engine = engine_with_mocks
        # 模拟 LLM 返回
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<new_speaking_style>编辑后的风格</new_speaking_style>\n<changes>调整了语气</changes>"
        ))
        # 确保 pm 可用
        assert engine._pm is not None
        # 先设一个当前值
        engine.state.speaking_style_c6 = "原始风格"
        engine.state.self_l2 = "测试 L2"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "编辑后的风格"

    @pytest.mark.asyncio
    async def test_refresh_c6_first_time(self, engine_with_mocks):
        """speaking_style_c6 为空时首次生成。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<new_speaking_style>首次生成的风格</new_speaking_style>\n<changes>无变化</changes>"
        ))
        engine.state.speaking_style_c6 = ""
        engine.state.self_l2 = ""

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "首次生成的风格"

    @pytest.mark.asyncio
    async def test_refresh_c6_llm_empty_no_block(self, engine_with_mocks):
        """LLM 返回空时保留旧值，不抛异常。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(text=""))
        old = engine.state.speaking_style_c6 = "旧值"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "旧值"  # 不变

    @pytest.mark.asyncio
    async def test_refresh_c6_parse_failure_no_block(self, engine_with_mocks):
        """<new_speaking_style> 解析失败时保留旧值。"""
        engine = engine_with_mocks
        engine._llm.think = AsyncMock(return_value=MagicMock(
            text="<other_tag>内容</other_tag>"  # 无 <new_speaking_style>
        ))
        old = engine.state.speaking_style_c6 = "旧值"

        await engine._refresh_c6()

        assert engine.state.speaking_style_c6 == "旧值"

    @pytest.mark.asyncio
    async def test_render_c6_returns_data(self, engine_with_mocks):
        """render_c6() 返回正确的 C6Data。"""
        engine = engine_with_mocks
        engine.state.speaking_style_c6 = "动态风格"

        data = engine.render_c6()

        assert data.speaking_style_c6 == "动态风格"

    @pytest.mark.asyncio
    async def test_render_c6_empty_default(self, engine_with_mocks):
        """speaking_style_c6 为空时返回空串。"""
        engine = engine_with_mocks
        engine.state.speaking_style_c6 = ""

        data = engine.render_c6()

        assert data.speaking_style_c6 == ""


@pytest.mark.unit
class TestAwarenessJudge:
    """AwarenessJudge — LLM 语义判定测试。"""

    @staticmethod
    def _make_state(**kwargs):
        """创建测试用 CharacterState。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    # ---- _judge_awareness ----

    def test_judge_approves_meaningful_deviation(self):
        """LLM 返回 <approved> 时更新缓存。"""
        s = self._make_state(
            self_l2="我比较直接，不太会撒娇。",
            state_l3="最近我发现自己更愿意示弱了。",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<approved>我变得更愿意在妖梦面前示弱了。</approved>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "我变得更愿意在妖梦面前示弱了。"
        assert s.approved_awareness_at is not None
        assert s.awareness_show_count == 0

    def test_judge_skip_preserves_cache(self):
        """LLM 返回 <skip> 时保留旧缓存。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="没什么变化。",
            approved_awareness_text="旧的觉察文本",
            approved_awareness_at=time.time(),
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<skip>没有新的变化</skip>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "旧的觉察文本"

    def test_judge_no_l2_self_fallback(self):
        """L2 为空时降级：L3 原文直接缓存，不调 LLM。"""
        s = self._make_state(
            self_l2="",
            state_l3="最近感觉自己变得更温柔了。",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "最近感觉自己变得更温柔了。"
        assert s.approved_awareness_at is not None
        engine._llm.think.assert_not_called()  # type: ignore[reportAttributeAccessIssue]

    def test_judge_llm_failure_preserves_cache(self):
        """LLM 调用异常时保留旧缓存。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
            approved_awareness_text="旧文本",
            approved_awareness_at=time.time(),
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(side_effect=RuntimeError("API down"))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "旧文本"

    def test_judge_template_render_failure_no_crash(self):
        """模板渲染失败时不抛异常。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.side_effect = RuntimeError("模板错误")

        # 不应抛异常
        asyncio.run(engine._judge_awareness(s.state_l3))

    def test_judge_no_pm_returns_early(self):
        """_pm 为 None 时提前返回。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
        )
        engine = _make_engine(state=s)
        engine._pm = None
        engine._llm.think = AsyncMock()

        asyncio.run(engine._judge_awareness(s.state_l3))

        engine._llm.think.assert_not_called()  # type: ignore[reportAttributeAccessIssue]

    def test_judge_overwrites_old_cache_on_approve(self):
        """新 <approved> 替换旧缓存。"""
        s = self._make_state(
            self_l2="测试。",
            state_l3="新反思。",
            approved_awareness_text="旧觉察",
            approved_awareness_at=time.time() - 1000,
            awareness_show_count=2,
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<approved>新觉察文本</approved>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "新觉察文本"
        assert s.awareness_show_count == 0

    # ---- _maybe_judge_awareness ----

    def test_maybe_judge_empty_l3_skips(self):
        """L3 snapshot 为空时跳过。"""
        s = self._make_state(state_l3="")
        engine = _make_engine(state=s)
        # _schedule 不应该被调用
        engine._schedule = MagicMock()  # type: ignore[reportAttributeAccessIssue]

        engine._maybe_judge_awareness()

        engine._schedule.assert_not_called()  # type: ignore[reportAttributeAccessIssue]

    # ---- _awareness_allowed ----

    def test_allowed_empty_cache(self):
        """缓存为空时不允许。"""
        s = self._make_state(approved_awareness_text="")
        engine = _make_engine(state=s)
        assert not engine._awareness_allowed()

    def test_allowed_show_count_exceeded(self):
        """展示次数达到上限时不允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=3,
        )
        engine = _make_engine(state=s)
        assert not engine._awareness_allowed()

    def test_allowed_cooldown_not_met(self):
        """冷却未到时不允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine.last_awareness_time = time.time()  # 刚刚展示过
        assert not engine._awareness_allowed()

    def test_allowed_first_call_no_cooldown(self):
        """首次调用不受冷却限制。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        # _last_awareness_time == 0.0（默认），首次调用
        engine.last_awareness_time = 0.0
        assert engine._awareness_allowed()

    def test_allowed_cooldown_passed(self):
        """冷却过后允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine.last_awareness_time = time.time() - 301  # 5 分钟前
        assert engine._awareness_allowed()

    # ---- render_c9 ----

    def test_render_c9_returns_approved_text(self):
        """缓存有效时返回批准文本。"""
        s = self._make_state(
            approved_awareness_text="我变得更温柔了",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine.last_awareness_time = time.time() - 301

        result = engine.render_c9()

        assert result.awareness_text == "我变得更温柔了"
        assert s.awareness_show_count == 1
        assert s.last_awareness_time is not None

    def test_render_c9_empty_when_no_cache(self):
        """无缓存时返回空。"""
        s = self._make_state(approved_awareness_text="")
        engine = _make_engine(state=s)
        result = engine.render_c9()
        assert result.awareness_text == ""

    def test_render_c9_show_count_stops(self):
        """展示满 3 次后返回空。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=3,
        )
        engine = _make_engine(state=s)
        result = engine.render_c9()
        assert result.awareness_text == ""

    def test_render_c9_cooldown_blocks(self):
        """冷却期内返回空。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine.last_awareness_time = time.time()  # 刚刚展示
        result = engine.render_c9()
        assert result.awareness_text == ""


@pytest.mark.service
@pytest.mark.asyncio
async def test_buffer_message_fixed_reflector_not_rebound():
    """C3/P1-10：显式 reflector 固化——session switch 后 engine._reflector 变了，
    in-flight buffer 仍写到发起会话 reflector。"""
    from unittest.mock import AsyncMock, MagicMock

    engine = _make_engine()
    refl_a = MagicMock()
    refl_a.buffer = AsyncMock()
    refl_b = MagicMock()
    refl_b.buffer = AsyncMock()
    engine._reflector = refl_b  # noqa: SLF001 — 模拟已切到 B

    task = engine.buffer_message("u", "a", reflector=refl_a)
    assert task is not None
    await task

    refl_a.buffer.assert_awaited_once()  # A 会话内容只进 A
    refl_b.buffer.assert_not_awaited()   # B 的 reflector 不被污染


@pytest.mark.service
@pytest.mark.asyncio
async def test_buffer_message_returns_trackable_task():
    """C3/P1-10：buffer_message 返回 task（不再内部创建不可见 task）。"""
    from unittest.mock import AsyncMock, MagicMock

    engine = _make_engine()
    refl = MagicMock()
    refl.buffer = AsyncMock()
    engine._reflector = refl  # noqa: SLF001

    task = engine.buffer_message("u", "a")

    assert task is not None and not task.done()
    await task
    refl.buffer.assert_awaited_once()


"""去 attach_session 后 engine 构造不变 + sync_reflector_to_runtime_state。"""
from unittest.mock import MagicMock, AsyncMock
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.personality.engine import CharacterEngine
from core.config import CharacterConfig


def _make_runtime_engine(state: CharacterRuntimeState | None = None) -> CharacterEngine:
    s = state or CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("test-sid"))
    m = MagicMock(); m.think = AsyncMock(return_value=MagicMock(text="test"))
    o = MagicMock(); o.expression = MagicMock(return_value=MagicMock())
    return CharacterEngine(
        llm=m, state=s, character_name="Elysia", user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。", character_config=CharacterConfig(name="Elysia"), output=o,
    )


@pytest.mark.unit
def test_engine_no_attach_session():
    assert not hasattr(_make_runtime_engine(), "attach_session")


@pytest.mark.unit
def test_engine_state_is_runtime_state():
    assert isinstance(_make_runtime_engine().state, CharacterRuntimeState)


@pytest.mark.unit
def test_sync_reflector_to_runtime_state():
    eng = _make_runtime_engine()
    eng._reflector._buffer = [("u", "a")]
    eng.sync_reflector_to_runtime_state()
    assert eng.state.reflector_buffer == [("u", "a")]


# ══════════════════════════════════════════════════════════════════
# S3-18：self_perception_history 必须真的有写入者
# ══════════════════════════════════════════════════════════════════

@pytest.mark.service
class TestSelfPerceptionHistoryIsWritten:
    """该字段此前**全仓无写入者**（只有 `service.py` 的清空与三处读）：

    ① 反思 prompt 的 `last_perception` 恒为空串；
    ② `CharacterReflector._should_trigger` 的"从 history 读上次反思时间"分支
       **不可达**（history 恒空）；
    ③ `reflector.MAX_HISTORY = 50  # self_perception_history 上限` 这个常量
       **从未被读过**——正是"写入者没写"留下的化石。
    """

    def _engine(self):
        from core.personality.engine import CharacterEngine
        from core.schema.character import CharacterState

        engine = CharacterEngine.__new__(CharacterEngine)
        engine.state = CharacterState(session_id="test-sid")
        engine._awareness = MagicMock()
        engine._awareness.maybe_judge = MagicMock()
        engine._maybe_compress = AsyncMock()
        engine._set_state_l3 = MagicMock()
        engine._append_state_l3 = MagicMock()
        return engine

    @pytest.mark.asyncio
    async def test_reflector_trigger_records_history(self):
        """**核心**：reflector 产出必须落进 history（内容 + 时间戳）。"""
        engine = self._engine()

        await engine.consume_trigger("reflector", {"content": "我似乎更在意妖梦了"})

        history = engine.state.self_perception_history
        assert len(history) == 1, "reflector 产出未记入 self_perception_history"
        assert history[0]["content"] == "我似乎更在意妖梦了"
        assert isinstance(history[0]["timestamp"], float), (
            f"时间戳必须是浮点（_should_trigger 直接做减法），实得 "
            f"{type(history[0]['timestamp']).__name__}")

    @pytest.mark.asyncio
    async def test_history_is_bounded(self):
        """上限生效（对应原先从未被使用的 MAX_HISTORY 常量）。"""
        from core.personality.engine import CharacterEngine

        engine = self._engine()
        cap = CharacterEngine.SELF_PERCEPTION_HISTORY_MAX

        for i in range(cap + 10):
            await engine.consume_trigger("reflector", {"content": f"反思 {i}"})

        history = engine.state.self_perception_history
        assert len(history) == cap, f"未受限：{len(history)} > {cap}"
        # 保留最近的
        assert history[-1]["content"] == f"反思 {cap + 9}"
        assert history[0]["content"] == "反思 10"

    @pytest.mark.asyncio
    async def test_non_reflector_sources_do_not_record(self):
        """event/idle/feedback 走 L3 更新，**不**进 self_perception_history。"""
        engine = self._engine()

        await engine.consume_trigger("event", {"content": "用户出差了"})
        await engine.consume_trigger("idle", {"content": "有点无聊"})

        assert engine.state.self_perception_history == []


@pytest.mark.unit
class TestReflectorTimestampCoercion:
    """`_should_trigger` 直接 `time.time() - last_ts`；旧数据可能是 ISO 字符串 →
    `TypeError`。`_reflect` 内有兼容、`_should_trigger` 没有——**同一份数据两处解析
    口径不一致**。"""

    @pytest.mark.parametrize("value,expect_positive", [
        (1700000000.0, True),
        (1700000000, True),
        ("2026-01-01T00:00:00+00:00", True),
        ("垃圾数据", False),
        (None, False),
        ("", False),
    ])
    def test_coerce_timestamp(self, value, expect_positive):
        from core.personality.reflector import _coerce_timestamp

        out = _coerce_timestamp(value)
        assert isinstance(out, float)
        if expect_positive:
            assert out > 0
        else:
            # 解析不出来 → 0（很久以前）→ 倾向于触发，而不是把反思永久卡住
            assert out == 0.0

    def test_should_trigger_survives_iso_timestamp(self):
        """端到端：history 里是 ISO 字符串时不得抛 TypeError。"""
        import time as _time
        from unittest.mock import MagicMock

        from core.personality.reflector import CharacterReflector

        ref = CharacterReflector.__new__(CharacterReflector)
        ref._buffer = []                                  # noqa: SLF001
        ref._last_reflection_at = None                    # noqa: SLF001
        ref._buffer = [("u", "a")] * 3                    # noqa: SLF001
        engine = MagicMock()
        engine.state.self_perception_history = [
            {"timestamp": "2020-01-01T00:00:00+00:00", "content": "旧的反思"}]
        ref._engine = engine                              # noqa: SLF001

        # 不抛即通过（旧实现会 TypeError）；2020 年的时间戳 → 早该反思
        assert ref._should_trigger() is True              # noqa: SLF001
        assert _time.time() > 0
