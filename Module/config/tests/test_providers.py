"""providers.yaml 注册表结构测试 — models/local 字段是 env 过滤的前提。"""
import pytest

from Module.config.providers import load_provider_defaults


@pytest.mark.unit
def test_llm_providers_have_models():
    """每个可下拉的 LLM provider 必须有非空 models（除 NotImplemented 的 gemini）。"""
    cfg = load_provider_defaults("llm")
    for name in ("deepseek", "openai", "ollama", "mimo"):
        assert cfg[name]["models"], f"{name} 缺少 models"


@pytest.mark.unit
def test_local_services_marked():
    """ollama/sensevoice/gpt-sovits 标记 local: true（内置默认地址，无需配置即显示）。"""
    assert load_provider_defaults("llm")["ollama"]["local"] is True
    assert load_provider_defaults("stt")["sensevoice"]["local"] is True
    assert load_provider_defaults("tts")["gpt-sovits"]["local"] is True


@pytest.mark.unit
def test_custom_is_meta_provider():
    """custom 保留在注册表（工厂用它创建后端），models 留空由 custom_providers.yaml 填充。"""
    for module in ("llm", "stt", "tts"):
        cfg = load_provider_defaults(module)
        assert "custom" in cfg
        assert cfg["custom"]["models"] == []
