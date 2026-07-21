"""CharacterState 新增字段的单元测试。"""

import pytest


@pytest.mark.unit
class TestSpeakingStyleC6:
    def test_default_empty_string(self):
        """新字段默认值为空串。"""
        from core.schema.character import CharacterState
        s = CharacterState(session_id="test")
        assert s.speaking_style_c6 == ""

    def test_assignable(self):
        """字段可赋值和持久化。"""
        from core.schema.character import CharacterState
        s = CharacterState(session_id="test", speaking_style_c6="新的说话风格")
        assert s.speaking_style_c6 == "新的说话风格"
        # 验证序列化往返
        data = s.model_dump()
        assert data["speaking_style_c6"] == "新的说话风格"
