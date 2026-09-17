"""/api/providers 与 /api/providers/test 端点测试。"""
import asyncio
import json
import os
import socket
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType  # noqa: F401  (保持与 test_server 一致导入风格)



def _make_app_agent():
    """真实 Agent（不 start，无 DB），提供 llm/config/tts 供 fallback 读取。"""
    from core.config import AppConfig
    from core.agent import Agent
    return Agent(config=AppConfig())


def _fake_getaddrinfo(ip="93.184.216.34"):
    """mock socket.getaddrinfo 返回指定 IP（SSRF 固定样例，不触网）。

    默认公网 IP（custom 出站放行）；传 127.0.0.1 模拟 localhost 解析 → SSRF 拦截。
    """
    def _resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return _resolve


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
    """custom 缺 body base_url → resolve_model_params 从 custom_providers.yaml 解析，出站走 guarded client。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n  - name: myllm\n    base_url: http://myllm.local\n    api_key_env: MY_LLM_KEY\n    models: [m1]\n",
        encoding="utf-8",
    )
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    # clear=True 清空全部 env + 显式置空 MY_LLM_KEY → resolve_model_params 不返回 api_key，
    # 隔离外部环境变量，保证断言确定性；secret_store._DEFAULT_DIR 隔离到 tmp（不碰真实 data/secrets/）；
    # mock getaddrinfo 返回公网 IP（myllm.local 不可解析 → 否则 NXDOMAIN SSRF 400）。
    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path), \
         patch.dict(os.environ, {"MY_LLM_KEY": ""}, clear=True), \
         patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo()), \
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
    # custom+backend → 注入出站守卫 http_client_factory（工厂，非实例——reconnect 可重建）
    mock_create.assert_called_once_with(
        "custom/myllm", base_url="http://myllm.local", http_client_factory=ANY)


# ---- 批次 5：保存/删除/test 守卫（runtime 门控 + SSRF + 密钥入 Store + 信封双轨） ----


@pytest.mark.service
@pytest.mark.asyncio
async def test_save_backend_with_api_key_stores_secret(aiohttp_client, tmp_path):
    """保存 backend + api_key → 200 + credentialConfigured=True + 密钥入 Secret Store 且响应无密钥。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    # 双隔离：cp._DEFAULT_PATH → tmp yaml、secret_store._DEFAULT_DIR → tmp（不碰真实 data/secrets/）
    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path), \
         patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo()):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "myllm", "base_url": "https://example.com", "api_key": "sk-secret"},
        })
        data = await resp.json()
        # 密钥已入 Secret Store（patch 后默认目录 = tmp），不入 YAML
        assert secret_store.get_secret("llm", "myllm") == "sk-secret"
        assert "sk-secret" not in yaml_path.read_text(encoding="utf-8")
        assert resp.status == 200
        assert data["ok"] is True
        # review 修正：save 响应恢复全量 custom 列表（spec §3.3 步骤 8，前批次 wire 形状）——
        # 条目在 data["data"]["llm"] 中；credentialConfigured 经 GET /api/providers 获取。
        saved = [e for e in data["data"]["llm"] if e.get("name") == "myllm"]
        assert saved and saved[0]["name"] == "myllm"
        # 响应无密钥（API 永不打印密钥）
        assert "sk-secret" not in json.dumps(data, ensure_ascii=False)
        # GET /api/providers 反映 credentialConfigured=True（密钥已入 Store，只回布尔；patch 仍在）
        list_resp = await client.get("/api/providers")
        list_data = await list_resp.json()
        custom_llm = list_data["custom"]["llm"]
        myllm = [e for e in custom_llm if e.get("name") == "myllm"]
        assert myllm and myllm[0]["credentialConfigured"] is True
        assert "sk-secret" not in json.dumps(list_data, ensure_ascii=False)


@pytest.mark.service
@pytest.mark.asyncio
async def test_save_desktop_with_api_key_rejected(aiohttp_client, tmp_path):
    """desktop runtime 带 api_key → 400 PROFILE_INVALID_RUNTIME（ProtocolError 信封）。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "dprof", "base_url": "https://example.com",
                      "runtime": "desktop", "api_key": "sk-x"},
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "PROFILE_INVALID_RUNTIME"
    assert data["error"] == data["message"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_save_desktop_without_base_url_allowed(aiohttp_client, tmp_path):
    """desktop 缺 base_url → 200 放行（base_url 门控按 runtime，D13）。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "dprof", "runtime": "desktop"},
        })
        data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is True
    # YAML 已写入（desktop 条目无 base_url）
    assert "dprof" in yaml_path.read_text(encoding="utf-8")


@pytest.mark.service
@pytest.mark.asyncio
async def test_save_backend_blocked_ssrf(aiohttp_client, tmp_path):
    """保存 backend base_url=127.0.0.1 → 400 SSRF_BLOCKED（ProtocolError 信封）。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "x", "base_url": "http://127.0.0.1"},
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "SSRF_BLOCKED"


@pytest.mark.service
@pytest.mark.asyncio
async def test_save_missing_base_url_backend_fail(aiohttp_client, tmp_path):
    """保存缺 base_url（无 runtime → backend）→ 400 v1 ApiFail（{ok:false, error}）。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "x"},
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["ok"] is False
    assert "error" in data


@pytest.mark.service
@pytest.mark.asyncio
async def test_delete_provider_in_use_only_by_runtime_backend_conflict(aiohttp_client, tmp_path):
    """#21c：**切会话**不得绕过 409——判定必须读运行期后端，不能只看活动会话的档位。

    背景：`switch_session` 只换活动指针，**有意不重放模型热切换**（S2-C3 的取舍，
    见 `_apply_service_enabled` 与 `test_*_model_hot_switch_is_deliberately_not_replayed`）。
    于是切到一个 `llm_model` 为空的会话后：

    - `agent.session.service_state.llm_model` == ""（只看它 ⇒ 判定"没在用"）
    - `agent.llm.model` == "custom/live_prof"（真正在跑的后端）

    只读前者 ⇒ 一次会话切换即可删掉仍在使用的 provider，**并连带删掉它的密钥**，
    把正在进行的会话打断。故运行期值是第一真相源。
    """
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    from core.schema.service_state import ServiceState
    from core.adapter.llm_adapter import LLMAdapter
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    agent.session.service_state = ServiceState()          # 活动会话"干净"（切会话后的形态）
    agent._llm = LLMAdapter(                              # 但运行期后端仍指向它
        model="custom/live_prof", _test_backend=MagicMock())
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path):
        resp = await client.delete("/api/providers/llm/live_prof")
        data = await resp.json()
    assert resp.status == 409, "运行期后端正在用它，切会话不该让删除变成允许"
    assert data["code"] == "PROFILE_IN_USE"


@pytest.mark.service
@pytest.mark.asyncio
async def test_delete_active_provider_conflict(aiohttp_client, tmp_path):
    """删除激活 provider（活动会话 service_state 指向 custom/x）→ 409 PROFILE_IN_USE。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    agent = _make_app_agent()
    agent.session.service_state.llm_model = "custom/active_prof"
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path):
        resp = await client.delete("/api/providers/llm/active_prof")
        data = await resp.json()
    assert resp.status == 409
    assert data["code"] == "PROFILE_IN_USE"


@pytest.mark.service
@pytest.mark.asyncio
async def test_delete_inactive_provider_cleans_secret(aiohttp_client, tmp_path):
    """删除非激活 provider → 200 + Secret Store 密钥清理。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n  - name: old_prof\n    base_url: https://example.com\n    models: [m1]\n",
        encoding="utf-8",
    )
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path):
        secret_store.set_secret("llm", "old_prof", "sk-old")
        assert secret_store.get_secret("llm", "old_prof") == "sk-old"  # 预置密钥生效
        resp = await client.delete("/api/providers/llm/old_prof")
        data = await resp.json()
        # 条目删除 + 密钥清理
        assert secret_store.get_secret("llm", "old_prof") is None
    assert resp.status == 200
    assert data["ok"] is True
    assert "old_prof" not in yaml_path.read_text(encoding="utf-8")


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_body_base_url_blocked_ssrf(aiohttp_client):
    """test 带 body base_url=127.0.0.1 → 400 SSRF_BLOCKED（无论 provider，含内置）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/providers/test", json={
        "service": "llm", "provider": "deepseek", "model": "deepseek-v4-flash",
        "level": "connectivity", "base_url": "http://127.0.0.1",
    })
    data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "SSRF_BLOCKED"


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_builtin_localhost_body_url_blocked(aiohttp_client):
    """本地内置 provider（gpt-sovits localhost:9880）test 带 body base_url → SSRF 拦截。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    # mock getaddrinfo 模拟 localhost 解析到 127.0.0.1（不触网）
    with patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo(ip="127.0.0.1")):
        resp = await client.post("/api/providers/test", json={
            "service": "tts", "provider": "gpt-sovits", "model": "default",
            "level": "connectivity", "base_url": "http://localhost:9880",
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "SSRF_BLOCKED"


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_desktop_runtime_blocked(aiohttp_client, tmp_path):
    """custom 条目 runtime=desktop → 400 PROFILE_RUNTIME_NOT_BACKEND。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n  - name: desktop_prof\n    base_url: https://example.com\n"
        "    runtime: desktop\n    models: [m1]\n",
        encoding="utf-8",
    )
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path), \
         patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo()):
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "custom", "model": "desktop_prof",
            "level": "connectivity",
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "PROFILE_RUNTIME_NOT_BACKEND"


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_desktop_local_base_url_not_ssrf(aiohttp_client, tmp_path):
    """review 修正：desktop 条目带本地 base_url（D13 允许）→ 必须 400 PROFILE_RUNTIME_NOT_BACKEND，
    而非 SSRF_BLOCKED——runtime 守卫先于 SSRF 校验（否则本地地址误报 SSRF）。"""
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    yaml_path.write_text(
        "llm:\n  - name: local_prof\n    base_url: http://127.0.0.1:3000\n"
        "    runtime: desktop\n    models: [m1]\n",
        encoding="utf-8",
    )
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path), \
         patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo()):
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "custom", "model": "local_prof",
            "level": "connectivity",
        })
        data = await resp.json()
    assert resp.status == 400
    assert data["code"] == "PROFILE_RUNTIME_NOT_BACKEND"


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


# ---- usable 级分派：真实产出验证（chat / synthesize / transcribe / 超时） ----

@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_usable_llm(aiohttp_client):
    """usable 级 LLM：走 backend.chat（非 test()），detail 报告返回文本前缀。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.llm.registry.LLMFactory.create") as mock_create:
        fake = AsyncMock()
        fake.chat = AsyncMock(return_value="你好，我在")
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "deepseek",
            "model": "deepseek-v4-flash", "level": "usable",
        })
        data = await resp.json()
    assert data["ok"] is True
    assert "你好，我在" in data["detail"]
    # 证明 usable 级确实分派到 chat 而非 connectivity 的 test()
    fake.chat.assert_awaited_once()
    fake.test.assert_not_awaited()


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_usable_tts(aiohttp_client):
    """usable 级 TTS：走 backend.synthesize，detail 报告音频字节数。

    缺省 character → agent 角色名（Elysia），synthesize 被 mock 不受音色影响。
    """
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.tts.registry.TTSFactory.create") as mock_create:
        fake = AsyncMock()
        fake.synthesize = AsyncMock(return_value=b"RIFFaudio")
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "tts", "provider": "minimax",
            "model": "speech-2.8-hd", "level": "usable",
        })
        data = await resp.json()
    assert data["ok"] is True
    assert f"audio {len(b'RIFFaudio')} bytes" in data["detail"]
    # 缺省 character 回退到 agent.character.name，验证 handler 的 fallback 逻辑
    fake.synthesize.assert_awaited_once()
    assert fake.synthesize.await_args.kwargs["character"] == "Elysia"


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_usable_stt(aiohttp_client):
    """usable 级 STT：读内置样例音频走 backend.transcribe，detail 报告识别文本。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.stt.registry.STTFactory.create") as mock_create:
        fake = AsyncMock()
        fake.transcribe = AsyncMock(return_value="识别文本")
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "usable",
        })
        data = await resp.json()
    assert data["ok"] is True
    assert data["detail"]
    assert "识别文本" in data["detail"]
    fake.transcribe.assert_awaited_once()


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_usable_timeout(aiohttp_client):
    """usable 级超时：backend.chat 抛 asyncio.TimeoutError → handler 捕获为"测试超时"。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch("Module.llm.registry.LLMFactory.create") as mock_create:
        fake = AsyncMock()
        fake.chat = AsyncMock(side_effect=asyncio.TimeoutError)
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "llm", "provider": "deepseek",
            "model": "deepseek-v4-flash", "level": "usable",
        })
        data = await resp.json()
    assert data["ok"] is False
    assert data["error"] == "测试超时"


# ---- 阶段 4 P0-01：SenseVoice 默认地址单源（BE-S4-INT-P0-01，spec §4.1） ----


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_sensevoice_connectivity_ok(aiohttp_client):
    """内置 sensevoice connectivity 成功：create 无 base_url kwarg（默认注入在工厂层）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    with patch("Module.stt.registry.STTFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {"ok": True, "latency_ms": 12}
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "connectivity",
        })
        data = await resp.json()
    assert data["ok"] is True
    assert data["latency_ms"] == 12
    mock_create.assert_called_once_with("sensevoice/sense-voice")


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_sensevoice_failure_diagnostic(aiohttp_client):
    """sensevoice 服务不可达 → 可诊断 ok=false，不报 URL 缺协议（spec §3.4 T6）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    with patch("Module.stt.registry.STTFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {
            "ok": False,
            "error": "无法连接 SenseVoice 服务 (http://localhost:20042): connection refused",
        }
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "connectivity",
        })
        data = await resp.json()
    assert data["ok"] is False
    assert "Request URL is missing" not in data["error"]
    assert "localhost:20042" in data["error"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_test_endpoint_body_base_url_override_passes_through(aiohttp_client):
    """合法 body base_url → create_kwargs explicit 传入工厂（spec §3.4 T8）。"""
    from core.input.server import create_app
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    with patch("Module.stt.registry.STTFactory.create") as mock_create:
        fake = AsyncMock()
        fake.test.return_value = {"ok": True, "latency_ms": 5}
        fake.close = AsyncMock()
        mock_create.return_value = fake
        resp = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "connectivity",
            "base_url": "http://1.2.3.4:20042",
        })
        data = await resp.json()
    assert data["ok"] is True
    mock_create.assert_called_once_with(
        "sensevoice/sense-voice", base_url="http://1.2.3.4:20042", http_client_factory=ANY)



@pytest.mark.service
@pytest.mark.asyncio
async def test_save_provider_with_corrupt_secret_file_reports_actionable_error(
        aiohttp_client, tmp_path):
    """S2-B7b：本地密钥文件损坏是**非暂时性**失败——不得报"请稍后重试"。

    写路径此时 fail-closed（`set_secret` 拒绝覆盖，保护既有密钥）；但若处理器沿用
    兜底的通用文案，用户会以为"过会儿再试就行"，而重试**永远不会成功**。
    信封形状保持 ApiFail（不改契约），只把失败语义说准。
    """
    from core.input.server import create_app
    from core.providers import custom_providers as cp
    from core.storage import secret_store
    yaml_path = tmp_path / "custom_providers.yaml"
    (tmp_path / secret_store._DEFAULT_FILE).write_text("{ broken json", encoding="utf-8")
    agent = _make_app_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
         patch.object(secret_store, "_DEFAULT_DIR", tmp_path), \
         patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo()):
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "myllm", "base_url": "https://example.com",
                      "api_key": "sk-secret"},
        })
        data = await resp.json()

    assert resp.status == 500
    assert "稍后重试" not in data["error"], "非暂时性失败被报成了「过会儿再试」"
    assert "损坏" in data["error"]
    assert "sk-secret" not in json.dumps(data, ensure_ascii=False), "响应不得回显密钥"
    # 现场必须原样保留（它是既有密钥的唯一副本）
    assert (tmp_path / secret_store._DEFAULT_FILE).read_text(encoding="utf-8") == "{ broken json"
