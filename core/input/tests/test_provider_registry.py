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
