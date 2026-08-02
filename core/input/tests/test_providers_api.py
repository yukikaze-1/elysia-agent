"""/api/providers 与 /api/providers/test 端点测试。"""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import WSMsgType  # noqa: F401  (保持与 test_server 一致导入风格)

from core.input.server import create_app


def _make_app_agent():
    """真实 Agent（不 start，无 DB），提供 llm/config/tts 供 fallback 读取。"""
    import os as _os
    _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
    from core.config import AppConfig
    from core.agent import Agent
    return Agent(config=AppConfig())


@pytest.mark.service
@pytest.mark.asyncio
async def test_providers_list_uses_registry(aiohttp_client):
    """GET /api/providers 返回过滤后结构（unconfigured 存在、custom 顶层、无内置 custom）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/api/providers")
    data = await resp.json()
    assert resp.status == 200
    assert "unconfigured" in data
    assert "custom" in data
    # 内置 custom 不在 data[service]，在顶层 custom 键
    assert "custom" not in data["llm"]
    # local: true 恒显示
    assert "sensevoice" in data["stt"]
    assert "gpt-sovits" in data["tts"]
    # deepgram（NotImplemented）不进 unconfigured
    assert all(e["name"] != "deepgram" for e in data["unconfigured"]["stt"])


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_builtin_connectivity(aiohttp_client):
    """内置 provider connectivity 级：Factory.create 建 backend 调 test()。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.llm.registry.LLMFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {"ok": True, "latency_ms": 12, "error": ""}
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "deepseek",
            "model": "deepseek-v4-flash", "level": "connectivity",
        })
        data = await resp.json()
    assert data["ok"] is True
    assert data["latency_ms"] == 12
    mock_create.assert_called_once_with(
        "deepseek/deepseek-v4-flash",
    )


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_custom_resolves_from_yaml(aiohttp_client, tmp_path):
    """custom 缺 body base_url → resolve_model_params 从 custom_providers.yaml 解析。"""
    from core.input.server import create_app
    from core.input import custom_providers as cp
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n  - name: myllm\n    base_url: http://myllm.local\n    api_key_env: MY_LLM_KEY\n    models: [m1]\n",
        encoding="utf-8",
    )
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    # clear=True 清空全部 env + 显式置空 MY_LLM_KEY → resolve_model_params 不返回 api_key，
    # 隔离外部环境变量，保证断言确定性
    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.dict(os.environ, {"MY_LLM_KEY": ""}, clear=True), \
         patch("Module.llm.registry.LLMFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {"ok": True, "latency_ms": 5, "error": ""}
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "custom", "model": "myllm", "level": "connectivity",
        })
        data = await resp.json()
    assert data["ok"] is True
    mock_create.assert_called_once_with("custom/myllm", base_url="http://myllm.local")


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_fallback_to_configured_model(aiohttp_client):
    """provider/model 均缺省 → 回退该服务当前模型（LLM → agent.llm.model）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.llm.registry.LLMFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {"ok": True, "latency_ms": 1, "error": ""}
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "level": "connectivity",
        })
        data = await resp.json()
    assert data["ok"] is True
    # agent.llm.model 是完整 "provider/model"，直接 split，不套 custom 默认
    mock_create.assert_called_once_with(agent.llm.model)
