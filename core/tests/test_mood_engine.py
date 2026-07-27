"""Mood 引擎新功能测试 — _apply_mood, _mood_text_to_tts_emotion, analyze_assistant."""
import pytest


@pytest.mark.unit
class TestApplyMood:
    def test_normal_with_intensity(self):
        """_apply_mood 正常解析 '心情描述|0.7'。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.state.mood = "平静"
        engine.state.mood_intensity = 0.5
        tts_mgr = MagicMock()

        _apply_mood("被夸奖后有点不好意思，但暗自开心|0.7", engine, tts_mgr)

        assert engine.state.mood == "被夸奖后有点不好意思，但暗自开心"
        assert engine.state.mood_intensity == 0.7
        engine._push_expression.assert_called_once_with(
            "被夸奖后有点不好意思，但暗自开心", 0.7)
        tts_mgr.set_emotion.assert_called_once_with(
            "被夸奖后有点不好意思，但暗自开心")

    def test_no_intensity_defaults(self):
        """无 | 分隔符时强度默认 0.5。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.state.mood = "平静"
        tts_mgr = MagicMock()

        _apply_mood("心情很复杂", engine, tts_mgr)

        assert engine.state.mood == "心情很复杂"
        assert engine.state.mood_intensity == 0.5

    def test_intensity_not_numeric(self):
        """强度非数字时默认 0.5。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("开心|很高", engine, tts_mgr)

        assert engine.state.mood == "开心"
        assert engine.state.mood_intensity == 0.5

    def test_intensity_clamped(self):
        """强度超出 [0.1, 1.0] 时 clamp。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("兴奋|1.5", engine, tts_mgr)
        assert engine.state.mood_intensity == 1.0

        _apply_mood("低落|0.0", engine, tts_mgr)
        assert engine.state.mood_intensity == 0.1

    def test_empty_mood_text(self):
        """空 mood 文本时不更新状态。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("|0.5", engine, tts_mgr)
        # mood unchanged, push_expression not called
        engine._push_expression.assert_not_called()


@pytest.mark.unit
class TestMoodToTtsEmotion:
    def test_happy_keywords(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("开心得像个小孩子") == "happy"
        assert _mood_text_to_tts_emotion("兴奋不已") == "happy"
        assert _mood_text_to_tts_emotion("心里充满喜悦") == "happy"

    def test_sad_keywords(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("有点难过呢") == "sad"
        assert _mood_text_to_tts_emotion("心情低落") == "sad"
        assert _mood_text_to_tts_emotion("很沮丧的感觉") == "sad"

    def test_neutral_default(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("平静如水") == "neutral"
        assert _mood_text_to_tts_emotion("被夸奖后暖暖的") == "neutral"

    def test_happy_wins_over_sad(self):
        """同时包含 happy 和 sad 关键词时 happy 优先。"""
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("虽然难过但还是很开心") == "happy"


@pytest.mark.unit
class TestAnalyzeAssistantDecay:
    def test_decay_only_no_mood_reset(self):
        """analyze_assistant 只衰减 intensity，不重置 mood 文本。"""
        from core.config import CharacterConfig
        from core.schema.character import CharacterState
        from core.schema.types import SessionId
        from Module.output import OutputManager
        from core.personality import CharacterEngine
        from unittest.mock import MagicMock, AsyncMock
        from core.adapter.llm_adapter import LLMAdapter

        char = CharacterConfig(name="Test")
        state = CharacterState(session_id=SessionId("test"))
        state.mood = "被夸奖后很开心"
        state.mood_intensity = 0.12  # below 0.2 — should trigger _push_idle
        output = OutputManager()
        output.expression = MagicMock()
        llm = MagicMock(spec=LLMAdapter)
        llm.think = AsyncMock()
        engine = CharacterEngine(
            llm=llm, state=state, character_name="Test",
            user_name="妖梦", base_personality="",
            character_config=char, output=output,
        )

        engine.analyze_assistant("好的")

        assert state.mood_intensity >= 0.1
        # mood text is NOT reset — LLM handles it next turn via <mood>
        assert state.mood == "被夸奖后很开心"


@pytest.mark.unit
class TestExtractTag:
    def test_normal_extraction(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>我是测试</new_self>", "new_self")
        assert result == "我是测试"

    def test_multiline(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>\n第一行\n第二行\n</new_self>", "new_self")
        assert result == "第一行\n第二行"

    def test_tag_not_found(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("没有标签", "new_self")
        assert result == ""

    def test_empty_tag(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self></new_self>", "new_self")
        assert result == ""

    def test_whitespace_trimmed(self):
        from core.personality.engine import _extract_tag
        result = _extract_tag("<new_self>  \n  内容  \n  </new_self>", "new_self")
        assert result == "内容"
