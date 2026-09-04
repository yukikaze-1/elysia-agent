"""CharacterRuntimeState 扁平模型（与 CharacterState 同构，instance-scoped）。"""
from core.schema.character import CharacterRuntimeState
from core.character.instance import CharacterInstance
from core.schema.types import SessionId

class TestCharacterRuntimeState:
    def test_instance_session_ids_present(self):
        s = CharacterRuntimeState(instance_id="chi_builtin_elysia", session_id=SessionId("default"))
        assert s.instance_id == "chi_builtin_elysia"
        assert s.session_id == "default"
        assert s.source_revision == 1

    def test_flat_fields_match_character_state(self):
        s = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
        assert s.self_l2 == "" and s.state_l3 == ""
        assert s.mood == "平静" and s.mood_intensity == 0.5
        assert s.speaking_style_c6 == ""
        assert s.reflector_buffer == [] and s.self_perception_history == []
        assert s.last_awareness_time is None
        assert s.approved_awareness_text == "" and s.awareness_show_count == 0
        assert s.compression_last_at is None and s.compression_last_count == 0

    def test_instance_state_field(self):
        inst = CharacterInstance(instanceId="chi_x", characterId="chr_x")
        assert inst.state is None
        inst.state = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
        assert inst.state.instance_id == "chi_x"
