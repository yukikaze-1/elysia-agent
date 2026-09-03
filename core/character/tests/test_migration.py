# core/character/tests/test_migration.py
import pytest
pytestmark = pytest.mark.unit   # 纯哈希函数
from core.character.migration import legacy_instance_id, migrate_legacy_sessions


def test_legacy_instance_id_deterministic():
    id1 = legacy_instance_id("conv_abc")
    id2 = legacy_instance_id("conv_abc")
    assert id1 == id2   # 同 session 同 instance
    assert id1 != legacy_instance_id("conv_def")


def test_legacy_instance_id_from_hash():
    assert legacy_instance_id("conv_abc").startswith("chi_legacy_")


def test_migrate_legacy_sessions_writes_binding_and_records():
    """migrate 为每旧 session 写确定性 character_binding 并返回迁移记录。"""
    class _Session:
        def __init__(self, sid):
            self.session_id = sid
            self.character_binding = None

    s1 = _Session("conv_a")
    s2 = _Session("conv_b")
    records = migrate_legacy_sessions([s1, s2])

    assert s1.character_binding == {
        "instanceId": legacy_instance_id("conv_a"),
        "characterId": "legacy",
    }
    assert s2.character_binding["instanceId"] == legacy_instance_id("conv_b")
    assert s2.character_binding["characterId"] == "legacy"
    assert len(records) == 2
    assert records[0]["session_id"] == "conv_a"
    assert records[0]["legacy_instance_id"] == legacy_instance_id("conv_a")
    assert records[0]["character_id"] == "legacy"
