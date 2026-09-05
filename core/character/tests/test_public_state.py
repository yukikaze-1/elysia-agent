"""阶段 G:make_public_state 映射 + 变化检测(service)。"""
import pytest
from unittest.mock import MagicMock

from core.character.public_state import make_public_state
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

pytestmark = pytest.mark.service


def _mock_runtime(mood="平静", mood_intensity=0.5, updated_at="2026-09-05T00:00:00Z"):
    rt = MagicMock()
    rt.state = CharacterRuntimeState(
        instance_id="chi_builtin_elysia", session_id=SessionId("default"),
        mood=mood, mood_intensity=mood_intensity, updated_at=updated_at)
    rt.instance = MagicMock()
    rt.instance.updatedAt = "2026-09-05T01:00:00Z"
    rt.revision = MagicMock()
    rt.revision.runtimeDefaults = {"proactive": {"enabled": True}}
    return rt


class TestMakePublicState:
    def test_maps_mood_and_intensity(self):
        rt = _mock_runtime(mood="开心", mood_intensity=0.9)
        view = make_public_state(rt)
        assert view.mood.label == "开心"
        assert view.mood.intensity == 0.9

    def test_last_interaction_from_instance_updated_at(self):
        rt = _mock_runtime()
        view = make_public_state(rt)
        assert view.lastInteractionAt == "2026-09-05T01:00:00Z"
        assert view.updatedAt == "2026-09-05T01:00:00Z"

    def test_proactive_from_runtime_defaults(self):
        rt = _mock_runtime()
        assert make_public_state(rt).proactiveEnabled is True

    def test_internal_state_not_exposed(self):
        rt = _mock_runtime()
        rt.state.self_l2 = "SECRET_L2"
        rt.state.state_l3 = "SECRET_L3"
        rt.state.self_perception_history = [{"a": 1}]
        view = make_public_state(rt)
        d = view.model_dump()
        assert "self_l2" not in d and "state_l3" not in d
        assert "self_perception_history" not in d


class TestRuntimePublicState:
    def test_has_public_state_changed_detects_mood(self):
        from core.character.runtime import CharacterRuntime
        from core.schema.character import CharacterRuntimeState
        rt = CharacterRuntime(
            instance=MagicMock(instanceId="chi_builtin_elysia", stateVersion=1,
                               updatedAt="t"),
            definition=MagicMock(), revision=MagicMock(revision=1),
            user_identity=MagicMock())
        rt.state = CharacterRuntimeState(
            instance_id="chi_builtin_elysia", session_id=SessionId("default"),
            mood="开心", mood_intensity=0.9)
        view1 = rt.public_state()
        # 首次(_last_public_state None)视为有变化——供第一次发射(与实现一致)
        assert rt.has_public_state_changed(view1) is True
        # 模拟发射后缓存 _last_public_state
        rt._last_public_state = view1
        # same mood → no change
        assert rt.has_public_state_changed(view1) is False
        # mood changed
        rt.state.mood = "难过"
        view2 = rt.public_state()
        assert rt.has_public_state_changed(view2) is True
