"""Secret Store 单元测试（tmp_path 注入，无真实 data/secrets）。"""

from pathlib import Path
from unittest import mock

import pytest

from core.storage import secret_store
from core.storage.secret_store import (
    all_configured,
    delete_secret,
    get_secret,
    has_secret,
    set_secret,
)

SERVICE = "llm"
NAME = "custom/openai"
VALUE = "sk-test-123"


def _resolved(tmp_path: Path) -> Path:
    """目录注入 → 实际存储文件路径。"""
    return tmp_path / "profiles.json"


@pytest.mark.unit
class TestSecretStoreRoundtrip:
    def test_set_get_roundtrip(self, tmp_path):
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        assert get_secret(SERVICE, NAME, path=tmp_path) == VALUE

    def test_get_nonexistent_returns_none(self, tmp_path):
        assert get_secret(SERVICE, NAME, path=tmp_path) is None

    def test_set_overwrite_same_name(self, tmp_path):
        set_secret(SERVICE, NAME, "v1", path=tmp_path)
        set_secret(SERVICE, NAME, "v2", path=tmp_path)
        assert get_secret(SERVICE, NAME, path=tmp_path) == "v2"

    def test_delete_removes_secret(self, tmp_path):
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        delete_secret(SERVICE, NAME, path=tmp_path)
        assert get_secret(SERVICE, NAME, path=tmp_path) is None
        # 重复删除不报错
        delete_secret(SERVICE, NAME, path=tmp_path)

    def test_has_three_states(self, tmp_path):
        assert has_secret(SERVICE, NAME, path=tmp_path) is False
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        assert has_secret(SERVICE, NAME, path=tmp_path) is True
        delete_secret(SERVICE, NAME, path=tmp_path)
        assert has_secret(SERVICE, NAME, path=tmp_path) is False

    def test_all_configured_only_names_with_secrets(self, tmp_path):
        assert all_configured(SERVICE, path=tmp_path) == set()
        set_secret(SERVICE, "custom/a", "ka", path=tmp_path)
        set_secret(SERVICE, "custom/b", "kb", path=tmp_path)
        assert all_configured(SERVICE, path=tmp_path) == {"custom/a", "custom/b"}
        delete_secret(SERVICE, "custom/a", path=tmp_path)
        assert all_configured(SERVICE, path=tmp_path) == {"custom/b"}
        # 其他 service 不受影响
        assert all_configured("tts", path=tmp_path) == set()


@pytest.mark.unit
class TestSecretStorePermissions:
    def test_dir_0700_file_0600(self, tmp_path):
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert _resolved(tmp_path).stat().st_mode & 0o777 == 0o600

    def test_no_tmp_residue_after_write(self, tmp_path):
        """原子写：目录里无 tmp 残留文件，旧值被替换。"""
        set_secret(SERVICE, NAME, "v1", path=tmp_path)
        set_secret(SERVICE, NAME, "v2", path=tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["profiles.json"]
        assert get_secret(SERVICE, NAME, path=tmp_path) == "v2"


@pytest.mark.unit
class TestSecretStoreCorruption:
    def test_corrupt_file_returns_empty_without_raising(self, tmp_path):
        """预写非法 JSON → 返回空 dict（不抛）+ logger.warning。"""
        _resolved(tmp_path).write_text("{ not valid json", encoding="utf-8")
        with mock.patch.object(secret_store, "logger") as mock_logger:
            assert get_secret(SERVICE, NAME, path=tmp_path) is None
            mock_logger.warning.assert_called_once()

    def test_corrupt_file_can_be_overwritten(self, tmp_path):
        """损坏文件可被 set 覆盖恢复。"""
        _resolved(tmp_path).write_text("{ not valid json", encoding="utf-8")
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        assert get_secret(SERVICE, NAME, path=tmp_path) == VALUE

    def test_missing_file_returns_empty(self, tmp_path):
        """文件不存在 → 空 dict。"""
        assert get_secret(SERVICE, NAME, path=tmp_path) is None
        assert all_configured(SERVICE, path=tmp_path) == set()
