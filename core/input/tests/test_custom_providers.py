"""resolve_model_params 测试 — custom 切换/测试的参数解析。"""
import os
from unittest.mock import patch

import pytest

from core.input import custom_providers as cp


@pytest.mark.unit
def test_builtin_returns_empty():
    """内置 provider 返回 {}（Factory 自行 env 解析）。"""
    assert cp.resolve_model_params("llm", "deepseek/deepseek-v4-flash") == {}


@pytest.mark.unit
def test_custom_resolves_from_yaml(tmp_path):
    """custom/name → 查 custom_providers.yaml[name] 的 base_url + api_key（api_key_env → os.getenv）。"""
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
            params = cp.resolve_model_params("llm", "custom/myllm")
    assert params == {"base_url": "http://myllm.local", "api_key": "secret"}


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
        params = cp.resolve_model_params("llm", "custom/nokeyval")
    assert params == {"base_url": "http://nokey.local"}
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
        params = cp.resolve_model_params("llm", "custom/nokey")
    assert params == {"base_url": "http://nokey.local"}
    assert "api_key" not in params


@pytest.mark.unit
def test_custom_unknown_name_returns_empty(tmp_path):
    """未知 custom 名 → {}（Factory 会报未知 provider）。"""
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text("llm: []\n", encoding="utf-8")
    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        assert cp.resolve_model_params("llm", "custom/nope") == {}
