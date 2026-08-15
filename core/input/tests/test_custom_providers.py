"""custom_providers 解析统一测试 — runtime/能力字段 + Secret Store 密钥 + secret_path 穿线。"""
import os
from unittest.mock import patch

import pytest

from core.providers import custom_providers as cp
from core.storage.secret_store import get_secret, set_secret


@pytest.mark.unit
def test_builtin_returns_empty():
    """内置 provider 返回 {}（Factory 自行 env 解析）。"""
    assert cp.resolve_model_params("llm", "deepseek/deepseek-v4-flash") == {}


@pytest.mark.unit
def test_custom_resolves_from_yaml(tmp_path):
    """custom/name → 查 custom_providers.yaml[name] 的 base_url + api_key（api_key_env → os.getenv）+ runtime。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n"
        "  - name: myllm\n"
        "    base_url: http://myllm.local\n"
        "    api_key_env: MY_LLM_KEY\n"
        "    models: [m1]\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"MY_LLM_KEY": "secret"}, clear=True):
        with patch.object(cp, "_DEFAULT_PATH", yaml_path):
            params = cp.resolve_model_params("llm", "custom/myllm", secret_path=tmp_path)
    assert params == {"base_url": "http://myllm.local", "api_key": "secret", "runtime": "backend"}


@pytest.mark.unit
def test_custom_env_empty_skips_api_key(tmp_path):
    """api_key_env 存在但 env 未设 → 不传 api_key（避免空串泄漏进 switch/test 调用）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n"
        "  - name: nokeyval\n"
        "    base_url: http://nokey.local\n"
        "    api_key_env: UNSET_KEY\n"
        "    models: [m1]\n",
        encoding="utf-8",
    )
    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.dict(os.environ, {"UNSET_KEY": ""}, clear=True):
        params = cp.resolve_model_params("llm", "custom/nokeyval", secret_path=tmp_path)
    assert params == {"base_url": "http://nokey.local", "runtime": "backend"}
    assert "api_key" not in params


@pytest.mark.unit
def test_custom_missing_api_key_env_no_crash(tmp_path):
    """api_key_env 缺失/为空 → 不传 api_key（避免 os.getenv(None) 抛 TypeError）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n"
        "  - name: nokey\n"
        "    base_url: http://nokey.local\n"
        "    models: [m1]\n",
        encoding="utf-8",
    )
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        params = cp.resolve_model_params("llm", "custom/nokey", secret_path=tmp_path)
    assert params == {"base_url": "http://nokey.local", "runtime": "backend"}
    assert "api_key" not in params


@pytest.mark.unit
def test_custom_unknown_name_returns_empty(tmp_path):
    """未知 custom 名 → {}（Factory 会报未知 provider）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text("llm: []\n", encoding="utf-8")
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        assert cp.resolve_model_params("llm", "custom/nope", secret_path=tmp_path) == {}


# ── 批次 5：api_key 入 Secret Store（不入 YAML）+ runtime/能力 + D18 清理 + secret_path 穿线 ──


@pytest.mark.unit
def test_add_provider_api_key_goes_to_store_not_yaml(tmp_path):
    """add_provider 带 api_key → YAML 条目无 api_key 字段，secret_store 有值。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        cp.add_provider(
            "llm",
            {"name": "sk1", "base_url": "http://sk1.local", "api_key": "sk-secret", "models": ["m1"]},
            secret_path=secret_path,
        )
    assert get_secret("llm", "sk1", path=secret_path) == "sk-secret"
    saved = cp.load(yaml_path)["llm"][0]
    assert "api_key" not in saved
    assert saved["name"] == "sk1"


@pytest.mark.unit
def test_add_provider_upsert_without_api_key_keeps_secret(tmp_path):
    """同名 upsert 不带 api_key → 原 secret 保留（upsert 不丢密钥）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        cp.add_provider(
            "llm",
            {"name": "keep", "base_url": "http://keep.local", "api_key": "keep-secret", "models": ["m1"]},
            secret_path=secret_path,
        )
        cp.add_provider(
            "llm",
            {"name": "keep", "base_url": "http://keep2.local", "models": ["m2"]},
            secret_path=secret_path,
        )
    assert get_secret("llm", "keep", path=secret_path) == "keep-secret"
    # YAML 条目更新为新的 base_url，仍不含密钥
    saved = cp.load(yaml_path)["llm"][0]
    assert saved["base_url"] == "http://keep2.local"
    assert "api_key" not in saved


@pytest.mark.unit
def test_resolve_secret_store_precedes_env(tmp_path):
    """api_key 解析顺序：Secret Store 直存 > api_key_env env（D3）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    yaml_path.write_text(
        "llm:\n"
        "  - name: prec\n"
        "    base_url: http://prec.local\n"
        "    api_key_env: PREC_KEY\n"
        "    models: [m1]\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"PREC_KEY": "from-env"}, clear=True):
        with patch.object(cp, "_DEFAULT_PATH", yaml_path):
            set_secret("llm", "prec", "from-store", path=secret_path)
            params = cp.resolve_model_params("llm", "custom/prec", secret_path=secret_path)
    assert params == {"base_url": "http://prec.local", "api_key": "from-store", "runtime": "backend"}


@pytest.mark.unit
def test_resolve_no_secret_no_env_omits_api_key(tmp_path):
    """secret 与 api_key_env 均无 → 返回不含 api_key 键（仍含 runtime）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    yaml_path.write_text(
        "llm:\n"
        "  - name: nokey\n"
        "    base_url: http://nokey.local\n"
        "    api_key_env: NOPE_KEY\n"
        "    models: [m1]\n",
        encoding="utf-8",
    )
    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.dict(os.environ, {"NOPE_KEY": ""}, clear=True):
        params = cp.resolve_model_params("llm", "custom/nokey", secret_path=secret_path)
    assert params == {"base_url": "http://nokey.local", "runtime": "backend"}
    assert "api_key" not in params


@pytest.mark.unit
def test_runtime_change_backend_to_desktop_clears_secret(tmp_path):
    """D18：runtime backend→desktop/local → 清除该条目 Secret Store 密钥。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        # 先以 backend 保存并写入密钥
        cp.add_provider(
            "llm",
            {"name": "desk", "base_url": "http://desk.local", "api_key": "sk-desk", "models": ["m1"]},
            secret_path=secret_path,
        )
        assert get_secret("llm", "desk", path=secret_path) == "sk-desk"
        # 更新为 desktop runtime（不带 api_key）→ 密钥被清
        cp.add_provider(
            "llm",
            {"name": "desk", "runtime": "desktop", "base_url": "http://desk.local", "models": ["m1"]},
            secret_path=secret_path,
        )
    assert get_secret("llm", "desk", path=secret_path) is None


@pytest.mark.unit
def test_add_provider_persists_runtime_and_capabilities(tmp_path):
    """add_provider 支持 runtime/capabilities 键（缺省 backend/{}），透传进 YAML。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        data = cp.add_provider(
            "llm",
            {
                "name": "cap",
                "base_url": "http://cap.local",
                "runtime": "local_worker",
                "capabilities": {"vision": True},
                "models": ["m1"],
            },
            secret_path=secret_path,
        )
    entry = data["llm"][0]
    assert entry["runtime"] == "local_worker"
    assert entry["capabilities"] == {"vision": True}
    # 缺省条目 runtime 不落 YAML 键，resolve 读取时缺省 backend
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        data2 = cp.add_provider(
            "llm",
            {"name": "def", "base_url": "http://def.local", "models": ["m1"]},
            secret_path=secret_path,
        )
        default_entry = [e for e in data2["llm"] if e["name"] == "def"][0]
        assert default_entry.get("runtime", "backend") == "backend"
        params = cp.resolve_model_params("llm", "custom/def", secret_path=secret_path)
        assert params["runtime"] == "backend"


@pytest.mark.unit
def test_remove_provider_deletes_entry_and_secret(tmp_path):
    """remove_provider → 条目删除 + Secret Store 密钥清除。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    secret_path = tmp_path / "secrets"
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        cp.add_provider(
            "llm",
            {"name": "gone", "base_url": "http://gone.local", "api_key": "sk-gone", "models": ["m1"]},
            secret_path=secret_path,
        )
        assert get_secret("llm", "gone", path=secret_path) == "sk-gone"
        data = cp.remove_provider("llm", "gone", secret_path=secret_path)
    assert data["llm"] == []
    assert get_secret("llm", "gone", path=secret_path) is None
