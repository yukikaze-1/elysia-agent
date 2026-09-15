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
        """预写非法 JSON → 读路径按"未配置"处理（不抛），但**打 ERROR**（S2-B7b）。

        原实现打的是 warning 且要求"必须被调用一次"；改成 ERROR 是因为它意味着
        **用户已配置的密钥读不出来了**，不是"顺手提示一下"。
        """
        _resolved(tmp_path).write_text("{ not valid json", encoding="utf-8")
        with mock.patch.object(secret_store, "logger") as mock_logger:
            assert get_secret(SERVICE, NAME, path=tmp_path) is None
            mock_logger.error.assert_called_once()
        assert all_configured(SERVICE, path=tmp_path) == set()
        assert has_secret(SERVICE, NAME, path=tmp_path) is False

    def test_corrupt_file_is_not_silently_overwritten(self, tmp_path):
        """**写路径 fail-closed**：损坏时拒绝写入，且文件内容逐字节不变。

        这是本条的核心：`set_secret` 是 load→改→整份 save，把损坏当空就会写出
        "只含本次一条"的新文件，**其余 provider 的密钥全部消失且没有任何副本**。
        原用例 `test_corrupt_file_can_be_overwritten` 恰好把这个不可逆行为钉成了预期。
        """
        target = _resolved(tmp_path)
        # 半截 JSON：原文件里其实有 llm/tts 两条密钥，但已经解析不出来
        broken = '{"llm": {"custom/keepme": "sk-keep"}, "tts": {"custom/x"'
        target.write_text(broken, encoding="utf-8")

        with pytest.raises(secret_store.SecretStoreCorruptError) as exc_info:
            set_secret(SERVICE, NAME, VALUE, path=tmp_path)

        assert str(target) in str(exc_info.value), "异常必须指出是哪个文件"
        assert target.read_text(encoding="utf-8") == broken, (
            "拒绝写入时文件必须原样保留——它是唯一副本")
        assert list(tmp_path.glob("*.tmp")) == [], "不该留下临时文件"

    def test_corrupt_file_does_not_block_delete(self, tmp_path):
        """删除**不写文件**，故损坏时静默返回（只打 ERROR），不抛。"""
        target = _resolved(tmp_path)
        target.write_text("{ broken", encoding="utf-8")
        with mock.patch.object(secret_store, "logger") as mock_logger:
            delete_secret(SERVICE, NAME, path=tmp_path)
            mock_logger.error.assert_called_once()
        assert target.read_text(encoding="utf-8") == "{ broken"

    def test_missing_file_returns_empty(self, tmp_path):
        """文件不存在 → 空 dict（与"损坏"是两种状态：前者正常，后者要人处理）。"""
        assert get_secret(SERVICE, NAME, path=tmp_path) is None
        assert all_configured(SERVICE, path=tmp_path) == set()
        # 不存在时写入必须正常（回归网：别把"不存在"也当成损坏拒绝掉）
        set_secret(SERVICE, NAME, VALUE, path=tmp_path)
        assert get_secret(SERVICE, NAME, path=tmp_path) == VALUE


@pytest.mark.unit
class TestSecretStoreConcurrency:
    def test_concurrent_set_no_lost_update(self, tmp_path):
        """并发写不同 key 不丢更新（threading.Lock 防御 TOCTOU）。"""
        from concurrent.futures import ThreadPoolExecutor
        from core.storage import secret_store

        path = tmp_path / "secrets.json"
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(
                lambda i: secret_store.set_secret("svc", f"k{i}", f"v{i}", path=path),
                range(8),
            ))
        data = secret_store.all_configured("svc", path=path)  # 或 _load
        assert len(data) == 8  # 无丢更新
