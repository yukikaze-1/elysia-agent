"""env 过滤 + /api/providers 响应组装测试。"""
import os
from unittest.mock import patch

import pytest

from core.input.provider_registry import build_providers_response


@pytest.mark.unit
def test_local_providers_always_shown():
    """local: true 的 provider 无论 env 如何都进 data[service]。"""
    with patch.dict(os.environ, {}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "ollama" in resp["llm"]            # local，即使 OLLAMA_URL 未设
        assert "sensevoice" in resp["stt"]
        assert "gpt-sovits" in resp["tts"]


@pytest.mark.unit
def test_env_key_or_url_configured():
    """env_key 或 url_key 任一命中即已配置。"""
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "deepseek" in resp["llm"]
    with patch.dict(os.environ, {"MIMO_URL": "http://mimo.local"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "mimo" in resp["llm"]


@pytest.mark.unit
def test_unconfigured_goes_to_unconfigured():
    """env 未配置且非 local → 进 unconfigured，env_vars 列出缺失变量。"""
    with patch.dict(os.environ, {}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        names = {e["name"] for e in resp["unconfigured"]["llm"]}
        assert "openai" in names
        openai_entry = next(e for e in resp["unconfigured"]["llm"] if e["name"] == "openai")
        assert "OPENAI_API_KEY" in openai_entry["env_vars"]


@pytest.mark.unit
def test_empty_models_and_custom_skipped_everywhere():
    """NotImplemented（gemini/deepgram/openai-stt）与 custom 不进下拉、不进 unconfigured。"""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "x", "DEEPGRAM_API_KEY": "y"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        # gemini 有 env_key 且命中，但 models 空 → 跳过
        assert "gemini" not in resp["llm"]
        assert "gemini" not in resp["unconfigured"]["llm"]
        # deepgram models 空 → 跳过
        assert "deepgram" not in resp["stt"]
        assert "deepgram" not in resp["unconfigured"]["stt"]
        # custom 永不在 data[service] / unconfigured
        assert "custom" not in resp["llm"]
        assert all(e["name"] != "custom" for e in resp["unconfigured"]["llm"])


# ---- 批次 5：ProviderEntry 新字段（credentialConfigured/runtime/capabilities） ----


@pytest.mark.unit
def test_builtin_entries_have_new_fields():
    """内置已配置条目含 credentialConfigured/runtime/capabilities 新字段。"""
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
    entry = resp["llm"]["deepseek"]
    assert entry["credentialConfigured"] is True
    assert entry["runtime"] == "backend"
    # review 修正：内置 capabilities 从 providers.yaml 带出已知键（spec D11）——supports_prefix/supports_multimodal
    assert entry["capabilities"]["supports_prefix"] is True
    assert "supports_multimodal" in entry["capabilities"]


@pytest.mark.unit
def test_custom_entries_enriched_secret_configured(tmp_path):
    """custom 条目 enrich credentialConfigured（secret_path 注入）+ runtime/capabilities。"""
    from core.storage import secret_store
    secret_store.set_secret("llm", "myllm", "sk-secret", path=tmp_path)
    custom_data = {
        "llm": [
            {"name": "myllm", "base_url": "https://example.com", "models": ["m1"]},
            {"name": "nokey", "base_url": "https://other.com", "models": ["m2"]},
        ],
        "stt": [],
        "tts": [],
    }
    resp = build_providers_response(custom_data, secret_path=tmp_path)
    entries = {e["name"]: e for e in resp["custom"]["llm"]}
    assert entries["myllm"]["credentialConfigured"] is True
    assert entries["nokey"]["credentialConfigured"] is False
    assert entries["myllm"]["runtime"] == "backend"
    assert entries["myllm"]["capabilities"] == {}


@pytest.mark.unit
def test_custom_desktop_runtime_credential_configured_false(tmp_path):
    """review 修正：desktop/local 条目 credentialConfigured 恒 False（密钥归 Electron 管，
    即使 Secret Store 有同名字符串或 api_key_env env 命中也不报 True——后端不管理本机密钥）。"""
    from core.storage import secret_store
    secret_store.set_secret("llm", "desk", "sk-secret", path=tmp_path)
    with patch.dict(os.environ, {"DESK_KEY": "sk-env"}, clear=True):
        custom_data = {
            "llm": [
                {"name": "desk", "base_url": "", "runtime": "desktop", "api_key_env": "DESK_KEY"},
            ],
            "stt": [],
            "tts": [],
        }
        resp = build_providers_response(custom_data, secret_path=tmp_path)
    entries = {e["name"]: e for e in resp["custom"]["llm"]}
    assert entries["desk"]["runtime"] == "desktop"
    assert entries["desk"]["credentialConfigured"] is False


@pytest.mark.unit
def test_custom_env_key_configured(tmp_path):
    """custom 条目 api_key_env 命中 env → credentialConfigured True。"""
    custom_data = {
        "llm": [
            {"name": "e1", "base_url": "https://e.com", "api_key_env": "MY_CUSTOM_KEY",
             "models": ["m"]},
        ],
        "stt": [],
        "tts": [],
    }
    with patch.dict(os.environ, {"MY_CUSTOM_KEY": "sk"}, clear=True):
        resp = build_providers_response(custom_data, secret_path=tmp_path)
    entry = resp["custom"]["llm"][0]
    assert entry["credentialConfigured"] is True
