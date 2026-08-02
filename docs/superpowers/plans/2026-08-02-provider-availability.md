# Provider 可用性机制实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前端设置面板只展示已配置/本地的 provider，并提供连通性 + 可使用性测试与自定义模型切换修复。

**Architecture:** providers.yaml 成为唯一 provider 来源（新增 `models` + `local` 字段）；新增 `core/input/provider_registry.py` 做 env 过滤并组装 `/api/providers` 响应；`/api/providers/test` 扩展支持任意 provider + `level`；`resolve_model_params` 统一 custom 参数解析（switch + test 共用）。

**Tech Stack:** Python 3.11+（aiohttp / pytest）/ Vue 3 + TypeScript + Vite。

**Spec:** [docs/superpowers/specs/2026-08-02-provider-availability-design.md](../specs/2026-08-02-provider-availability-design.md)

## Global Constraints

- env 过滤规则（spec「env 过滤规则」）：`custom` 与 models 为空的 provider **跳过**（不进下拉、不进 unconfigured）；`local: true` 恒显示；`env_key` 或 `url_key` 任一在 `os.getenv` 命中 → 已配置；否则进 `unconfigured`（`env_vars` 列出非空项）。
- 测试端点超时：connectivity **5s**、usable **30s**。
- test 端点 base_url/api_key 解析优先级：**请求体非空 > custom_providers.yaml（复用 `resolve_model_params`）> env**；空串不覆盖 env 解析（`resolve_base_url` 用 `is not None`）。
- test 端点参数解析：`provider`+`model` 均缺省 → 回退该服务当前模型（LLM `agent.llm.model`；TTS `agent.tts.model if agent.tts is not None else agent.config.tts.model`；STT `agent.config.stt.model`），**不再套 provider→"custom" 默认**；仅 `provider` 缺省 → `"custom"`；`level` 缺省 → `"connectivity"`。
- `resolve_model_params`：custom → 查 custom_providers.yaml 的 `base_url` + `api_key`（`api_key_env` 缺失/为空时跳过，避免 `os.getenv(None)` 抛 TypeError）；内置 → `{}`。
- 分层约束：`Module/` 工厂不依赖 core。
- 中文注释；conda env `Elysia`。

---

### Task 1: providers.yaml 扩展（models + local 标记）

**Files:**
- Modify: `Module/config/providers.yaml`
- Test: `Module/config/tests/test_providers.py`（新建）

**Interfaces:**
- Consumes: `Module.config.providers.load_provider_defaults(module)`（既有，返回 `{provider: {env_key, url_key, ...}}`）。
- Produces: providers.yaml 每个 provider 含 `models: list[str]`；ollama/sensevoice/gpt-sovits 含 `local: true`。Task 2 的过滤依赖这些字段。

- [ ] **Step 1: 写失败测试**

```python
# Module/config/tests/test_providers.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest Module/config/tests/test_providers.py -q`
Expected: FAIL（`models` / `local` 键缺失）

- [ ] **Step 3: 实现 — 更新 providers.yaml**

将 `Module/config/providers.yaml` 全文替换为（保留既有字段，新增 `models` 与 `local`）：

```yaml
# 所有后端 provider 的默认配置
# 优先级: env var > YAML (config.yaml) > 此文件默认值
# 用户可通过 .env 或 config.yaml 覆盖，无需修改代码

llm:
  deepseek:
    base_url: ""
    url_key: "DEEPSEEK_URL"
    beta_url: ""
    beta_url_key: "DEEPSEEK_BETA_URL"
    env_key: "DEEPSEEK_API_KEY"
    supports_prefix: true
    supports_multimodal: false
    models: ["deepseek-v4-flash", "deepseek-v4-pro"]
  openai:
    base_url: ""
    url_key: "OPENAI_URL"
    env_key: "OPENAI_API_KEY"
    supports_prefix: false
    supports_multimodal: false
    models: ["gpt-4", "gpt-4o", "gpt-5"]
  ollama:
    base_url: ""
    url_key: "OLLAMA_URL"
    env_key: ""
    local: true
    supports_prefix: false
    supports_multimodal: false
    models: ["qwen2.5:3b"]
  gemini:
    base_url: ""
    url_key: "GEMINI_URL"
    env_key: "GEMINI_API_KEY"
    supports_prefix: false
    supports_multimodal: false
    models: []
  mimo:
    base_url: ""
    url_key: "MIMO_URL"
    env_key: "MIMO_API_KEY"
    supports_prefix: false
    supports_multimodal: true
    models: ["mimo-v2.5", "mimo-v2.5-pro"]
  custom:
    base_url: ""
    url_key: ""
    env_key: "CUSTOM_LLM_API_KEY"
    supports_prefix: false
    supports_multimodal: false
    models: []

tts:
  gpt-sovits:
    base_url: ""
    url_key: "GPT_SOVITS_URL"
    env_key: ""
    local: true
    models: ["default"]
  custom:
    base_url: ""
    url_key: ""
    env_key: "CUSTOM_TTS_API_KEY"
    models: []
  minimax:
    base_url: ""
    url_key: "MINIMAX_TTS_URL"
    env_key: "MINIMAX_API_KEY"
    models: ["speech-2.8-hd"]

stt:
  sensevoice:
    base_url: ""
    url_key: "SENSEVOICE_URL"
    env_key: ""
    local: true
    models: ["sense-voice"]
  openai:
    base_url: ""
    url_key: "OPENAI_STT_URL"
    env_key: "OPENAI_API_KEY"
    models: []
  deepgram:
    base_url: ""
    url_key: "DEEPGRAM_URL"
    env_key: "DEEPGRAM_API_KEY"
    models: []
  custom:
    base_url: ""
    url_key: ""
    env_key: "CUSTOM_STT_API_KEY"
    models: []

embedding:
  huggingface:
    base_url: ""
    url_key: ""
    env_key: ""
  openai:
    base_url: ""
    url_key: "OPENAI_API_BASE"
    env_key: "OPENAI_API_KEY"

database:
  milvus:
    base_url: ""
  mongodb:
    base_url: ""
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest Module/config/tests/test_providers.py -q`
Expected: PASS

- [ ] **Step 5: 回归 + Commit**

```bash
conda run -n Elysia python -m pytest Module/config/tests/ -q
git add Module/config/providers.yaml Module/config/tests/test_providers.py
git commit -m "feat(config): providers.yaml 增加 models 列表 + local 标记
（env 过滤的数据前提）"
```

---

### Task 2: provider_registry.py — env 过滤 + 响应组装

**Files:**
- Create: `core/input/provider_registry.py`
- Test: `core/input/tests/test_provider_registry.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 providers.yaml 字段；`Module.config.providers.load_provider_defaults`。
- Produces: `is_provider_configured(cfg: dict) -> bool`；`build_providers_response(custom_data: dict) -> dict`（Task 4 的 `/api/providers` 使用）。

- [ ] **Step 1: 写失败测试**

```python
# core/input/tests/test_provider_registry.py
"""env 过滤 + /api/providers 响应组装测试。"""
import os
from unittest.mock import patch

import pytest

from core.input.provider_registry import build_providers_response


def _clear_llm_env():
    """清掉 LLM 相关 env，保证测试确定性。"""
    for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MIMO_API_KEY",
              "DEEPSEEK_URL", "OPENAI_URL", "MIMO_URL", "OLLAMA_URL"):
        os.environ.pop(k, None)


@pytest.mark.unit
def test_local_providers_always_shown():
    """local: true 的 provider 无论 env 如何都进 data[service]。"""
    _clear_llm_env()
    with patch.dict(os.environ, {}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "ollama" in resp["llm"]            # local，即使 OLLAMA_URL 未设
        assert "sensevoice" in resp["stt"]
        assert "gpt-sovits" in resp["tts"]


@pytest.mark.unit
def test_env_key_or_url_configured():
    """env_key 或 url_key 任一命中即已配置。"""
    _clear_llm_env()
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "deepseek" in resp["llm"]
    _clear_llm_env()
    with patch.dict(os.environ, {"MIMO_URL": "http://mimo.local"}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        assert "mimo" in resp["llm"]


@pytest.mark.unit
def test_unconfigured_goes_to_unconfigured():
    """env 未配置且非 local → 进 unconfigured，env_vars 列出缺失变量。"""
    _clear_llm_env()
    with patch.dict(os.environ, {}, clear=True):
        resp = build_providers_response({"llm": [], "stt": [], "tts": []})
        names = {e["name"] for e in resp["unconfigured"]["llm"]}
        assert "openai" in names
        openai_entry = next(e for e in resp["unconfigured"]["llm"] if e["name"] == "openai")
        assert "OPENAI_API_KEY" in openai_entry["env_vars"]


@pytest.mark.unit
def test_empty_models_and_custom_skipped_everywhere():
    """NotImplemented（gemini/deepgram/openai-stt）与 custom 不进下拉、不进 unconfigured。"""
    _clear_llm_env()
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
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_provider_registry.py -q`
Expected: FAIL（`ModuleNotFoundError: core.input.provider_registry`）

- [ ] **Step 3: 实现**

```python
# core/input/provider_registry.py
"""Provider 注册表过滤 — 读 providers.yaml，组装 /api/providers 响应。

env 过滤规则（spec「env 过滤规则」）：
1. custom（meta-provider）与 models 为空的 provider → 跳过（不进下拉、不进 unconfigured）。
2. local: true → 恒显示（内置默认地址，无需配置）。
3. env_key 非空且 os.getenv 已设 → 已配置。
4. url_key 非空且 os.getenv 已设 → 已配置。
5. 否则 → 未配置，进 unconfigured[service]（env_vars 列出非空项）。
"""
from __future__ import annotations

import os

from Module.config.providers import load_provider_defaults

_SERVICES = ("llm", "stt", "tts")


def is_provider_configured(cfg: dict) -> bool:
    """判定 provider 是否已配置（local 恒 True；custom/空 models 由调用方跳过）。"""
    if cfg.get("local"):
        return True
    env_key = cfg.get("env_key", "")
    url_key = cfg.get("url_key", "")
    if env_key and os.getenv(env_key):
        return True
    if url_key and os.getenv(url_key):
        return True
    return False


def _missing_env_vars(cfg: dict) -> list[str]:
    """返回能让 provider 满足配置的非空 env_key/url_key 项（unconfigured 引导用）。"""
    out: list[str] = []
    for key in ("env_key", "url_key"):
        val = cfg.get(key, "")
        if val:
            out.append(val)
    return out


def build_providers_response(custom_data: dict) -> dict:
    """组装 /api/providers 响应：已配置 + local 进 data[service]，未配置进 unconfigured。

    Args:
        custom_data: custom_providers.load() 结果（{"llm": [...], "stt": [...], "tts": [...]}）。

    Returns:
        {"llm": {...}, "stt": {...}, "tts": {...}, "custom": custom_data, "unconfigured": {...}}。
    """
    result: dict = {"custom": custom_data, "unconfigured": {s: [] for s in _SERVICES}}
    for service in _SERVICES:
        providers = load_provider_defaults(service)
        service_dict: dict[str, dict] = {}
        unconf: list[dict] = []
        for name, cfg in providers.items():
            if name == "custom" or not cfg.get("models"):
                continue  # meta-provider / NotImplemented：不进下拉、不进 unconfigured
            if is_provider_configured(cfg):
                service_dict[name] = {"models": cfg.get("models", [])}
            else:
                unconf.append({"name": name, "env_vars": _missing_env_vars(cfg)})
        result[service] = service_dict
        result["unconfigured"][service] = unconf
    return result
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_provider_registry.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/input/provider_registry.py core/input/tests/test_provider_registry.py
git commit -m "feat(input): provider_registry env 过滤 + /api/providers 响应组装"
```

---

### Task 3: custom_providers.py — resolve_model_params

**Files:**
- Modify: `core/input/custom_providers.py`
- Test: `core/input/tests/test_custom_providers.py`（新建）

**Interfaces:**
- Consumes: 既有 `load(path=None) -> dict`（返回 `{"llm": [...], "stt": [...], "tts": [...]}`）。
- Produces: `resolve_model_params(service: str, model_id: str) -> dict`（Task 4 测试端点 + Task 5 switch 共用）。

- [ ] **Step 1: 写失败测试**

```python
# core/input/tests/test_custom_providers.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_custom_providers.py -q`
Expected: FAIL（`AttributeError: module 'core.input.custom_providers' has no attribute 'resolve_model_params'`）

- [ ] **Step 3: 实现**

在 `core/input/custom_providers.py` 末尾追加：

```python
def resolve_model_params(service: str, model_id: str) -> dict:
    """解析切换/测试模型所需参数（switch + /api/providers/test 共用）。

    Args:
        service: 服务类型，"llm"/"stt"/"tts"。
        model_id: 完整模型标识，如 "custom/myllm" 或 "deepseek/deepseek-v4-flash"。

    Returns:
        dict: "custom/name" → 查 custom_providers.yaml[name]，返回
            {"base_url": ..., "api_key": os.getenv(api_key_env)}（api_key_env 缺失/为空时
            跳过 api_key，避免 os.getenv(None) 抛 TypeError）；内置 provider → {}。
    """
    if "/" not in model_id or model_id.split("/", 1)[0] != "custom":
        return {}
    name = model_id.split("/", 1)[1]
    for entry in load()[service]:
        if entry.get("name") == name:
            params = {"base_url": entry.get("base_url", "")}
            env_name = entry.get("api_key_env", "")
            api_key = os.getenv(env_name, "") if env_name else ""
            # 空 api_key 不传——避免 "" 泄漏进 switch/test 调用（assert_called_once_with 严格相等会失败）
            if api_key:
                params["api_key"] = api_key
            return params
    return {}
```

（需在文件顶部加 `import os`，若未导入。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_custom_providers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/input/custom_providers.py core/input/tests/test_custom_providers.py
git commit -m "feat(input): resolve_model_params — custom 切换/测试参数统一解析"
```

---

### Task 4: api_routes.py — /api/providers 用 registry + /api/providers/test 扩展

**Files:**
- Modify: `core/input/api_routes.py`
- Modify: `paths.py`（加 `STT_SAMPLE_WAV` 条目）
- Create: `Module/stt/tests/fixtures/sample.wav`（STT usable 测试样例）
- Test: `core/input/tests/test_providers_api.py`（新建）

**Interfaces:**
- Consumes: Task 2 `build_providers_response`；Task 3 `resolve_model_params`；`request.app["agent"]`（create_app 注入）；`Module.llm/stt/tts` 的 `LLMFactory/STTFactory/TTSFactory.create`。
- Produces: `/api/providers`（GET）返回过滤后响应；`/api/providers/test`（POST）支持任意 provider + `level`。

- [ ] **Step 1: 生成 STT 样例 WAV + paths.py 注册路径 + 写失败测试**

在 `paths.py` 的 `ProjectPath` 类中加一条（与既有 `DATA_REF = "data/ref"` 同风格）：

```python
    STT_SAMPLE_WAV = "Module/stt/tests/fixtures/sample.wav"
```

```bash
# 生成 1s 440Hz 正弦波 WAV（16kHz mono int16）
mkdir -p Module/stt/tests/fixtures
conda run -n Elysia python -c "
import wave, struct, math
with wave.open('Module/stt/tests/fixtures/sample.wav', 'w') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b''.join(
        struct.pack('<h', int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(16000)))
"
ls -la Module/stt/tests/fixtures/sample.wav
```

```python
# core/input/tests/test_providers_api.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_providers_api.py -q`
Expected: FAIL（断言不匹配 / 行为缺失）

- [ ] **Step 3: 实现**

在 `core/input/api_routes.py` 顶部加 `import asyncio` 与 `import time`。

**providers_list_handler** 改为：

```python
    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回过滤后的 provider 列表（providers.yaml 单一来源 + env 过滤 + custom）。"""
        from core.input.custom_providers import load as load_custom
        from core.input.provider_registry import build_providers_response
        return web.json_response(build_providers_response(load_custom()))
```

**providers_test_handler** 整体替换为：

```python
    async def providers_test_handler(request: web.Request) -> web.Response:
        """测试 provider 连通性（level=connectivity）或可使用性（level=usable）。

        body: {service, provider, model, level, base_url?, api_key?, character?}
        参数解析顺序见 spec「/api/providers/test 扩展」：provider+model 均缺省 → 回退配置模型；
        仅 provider 缺省 → "custom"；level 缺省 → "connectivity"。
        base_url/api_key 优先级：请求体非空 > custom_providers.yaml > env。
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)

        service = body.get("service", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)

        level = body.get("level", "connectivity")
        provider = body.get("provider", "")
        model = body.get("model", "")
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")

        # provider+model 均缺省 → 回退该服务当前模型（完整 provider/model，直接 split）
        if not provider and not model:
            agent = request.app.get("agent")
            if agent is None:
                return web.json_response({"ok": False, "error": "agent 未注入"}, status=500)
            if service == "llm":
                model_id = agent.llm.model
            elif service == "tts":
                # disabled 态即 agent.tts is None——必须守卫，否则在 Factory.create 前抛 AttributeError
                tts = agent.tts
                model_id = tts.model if tts is not None else agent.config.tts.model
            else:  # stt
                model_id = agent.config.stt.model
            try:
                provider, model = model_id.split("/", 1)
            except ValueError:
                return web.json_response({"ok": False, "error": f"当前模型格式无效: {model_id}"})
        elif not provider:
            provider = "custom"

        # base_url/api_key：请求体非空 > custom_providers.yaml（resolve_model_params）
        if not base_url and provider == "custom":
            from core.input.custom_providers import resolve_model_params
            params = resolve_model_params(service, f"custom/{model}")
            base_url = params.get("base_url", "")
            if not api_key:
                api_key = params.get("api_key", "")

        create_kwargs = {}
        if base_url:
            create_kwargs["base_url"] = base_url
        if api_key:
            create_kwargs["api_key"] = api_key

        try:
            if service == "llm":
                from Module.llm import LLMFactory
                backend = LLMFactory.create(f"{provider}/{model}", **create_kwargs)
            elif service == "stt":
                from Module.stt import STTFactory
                backend = STTFactory.create(f"{provider}/{model}", **create_kwargs)
            else:
                from Module.tts import TTSFactory
                backend = TTSFactory.create(f"{provider}/{model}", **create_kwargs)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

        try:
            if level == "usable":
                result = await _run_usable_test(backend, service, request, body)
            else:
                result = await asyncio.wait_for(backend.test(), timeout=5.0)
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "测试超时"})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        finally:
            try:
                await backend.close()
            except Exception:
                pass
```

在 `register_api_routes` 外（文件级）加：

```python
async def _run_usable_test(backend, service: str, request: web.Request, body: dict) -> dict:
    """usable 级真实产出验证。LLM 发文本、TTS 合成音频、STT 转写样例。超时 30s。

    STT 成功标准为"transcribe 调用无异常"——内置 tone 样例未必产生非空文本，
    但能证明服务接受并处理音频（真实语音样本会返回文本，detail 报告实际识别结果）。
    """
    start = time.perf_counter()

    if service == "llm":
        text = await asyncio.wait_for(
            backend.chat([{"role": "user", "content": "你好，请回一句话"}]),
            timeout=30.0,
        )
        ok = bool(text.strip())
        return {
            "ok": ok,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "detail": text.strip()[:20] if ok else "",
        }

    if service == "tts":
        # character 缺省 → agent 角色名（agent.character.name，注意不是 config.agent.character——
        # 那是角色配置路径 "character/elysia/config.yaml"，传给 synthesize 当音色名必然失败）。
        # 该角色在 provider 无对应音色时后端抛"未知发音人"，detail 带出（spec 允许）。
        character = body.get("character", "")
        if not character:
            agent = request.app.get("agent")
            if agent is not None:
                character = getattr(getattr(agent, "character", None), "name", "") or ""
        audio = await asyncio.wait_for(
            backend.synthesize("你好，测试", character=character),
            timeout=30.0,
        )
        ok = bool(audio)
        return {
            "ok": ok,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "detail": f"audio {len(audio)} bytes" if ok else "",
        }

    # stt — 内置样例。路径走 ProjectPath（全项目路径唯一定义点，不用 Path(__file__) 硬编码跨目录）
    from paths import ProjectPath
    sample_path = ProjectPath.STT_SAMPLE_WAV.resolve()
    audio_bytes = sample_path.read_bytes()
    text = await asyncio.wait_for(backend.transcribe(audio_bytes), timeout=30.0)
    return {
        "ok": True,
        "latency_ms": int((time.perf_counter() - start) * 1000),
        "detail": text.strip()[:20],
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_providers_api.py -q`
Expected: PASS

- [ ] **Step 5: 回归 + Commit**

```bash
conda run -n Elysia python -m pytest core/input/tests/ Module/stt/tests/test_stt.py -q
# paths.py 必须一并提交——STT usable 路径依赖 ProjectPath.STT_SAMPLE_WAV，
# 漏掉则该提交快照的 _run_usable_test 在 .resolve() 前 AttributeError
git add core/input/api_routes.py paths.py Module/stt/tests/fixtures/sample.wav core/input/tests/test_providers_api.py
git commit -m "feat(input): /api/providers 走 registry + /api/providers/test 支持 level/任意 provider"
```

---

### Task 5: switch 修复 — llm_adapter kwargs + server.py switch 解析

**Files:**
- Modify: `core/adapter/llm_adapter.py:178`（`switch_model`）
- Modify: `core/agent/server.py:89-142`（`switch_llm` / `switch_tts`）
- Test: `core/input/tests/test_server.py` 追加

**Interfaces:**
- Consumes: Task 3 `resolve_model_params`。
- Produces: 切到 `custom/<name>` 时 Factory.create 带 base_url/api_key（内置行为不变）。

- [ ] **Step 1: 写失败测试（追加到 test_server.py）**

先给 `core/input/tests/test_server.py` 顶部 import 区加 `import os`（当前只有 `import asyncio`/`from unittest.mock import ...`/`import pytest`/`from aiohttp import web, WSMsgType`，无模块级 `os`——本测试的 `patch.dict(os.environ, ...)` 依赖它，缺了 NameError）。

```python
    @pytest.mark.asyncio
    async def test_switch_llm_resolves_custom_params(self, tmp_path):
        """switch_llm 切到 custom 时 resolve_model_params 解析 base_url 并传给 Factory。"""
        from unittest.mock import patch
        from core.input import custom_providers as cp
        from core.input.server import create_app
        import json as _json

        agent = _make_agent()
        await agent.start()

        yaml_path = tmp_path / "custom_providers.yaml"
        yaml_path.write_text(
            "llm:\n  - name: myllm\n    base_url: http://myllm.local\n    api_key_env: MY_LLM_KEY\n    models: [m1]\n",
            encoding="utf-8",
        )

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        # clear=True + 置空 MY_LLM_KEY → resolve 不返回 api_key，断言确定性
        with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
             patch.dict(os.environ, {"MY_LLM_KEY": ""}, clear=True), \
             patch.object(agent.llm, "switch_model") as mock_switch:
            async with client.session.ws_connect(client.make_url('/ws')) as ws:
                await ws.send_json({"type": "switch_model", "service": "llm", "model": "custom/myllm"})
                await asyncio.sleep(0.2)
        mock_switch.assert_called_once_with("custom/myllm", base_url="http://myllm.local")
        await agent.stop()
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py::TestWebSocketHandler::test_switch_llm_resolves_custom_params -q --asyncio-mode=auto`
Expected: FAIL（`switch_model` 未被调用带 base_url）

- [ ] **Step 3: 实现**

`core/adapter/llm_adapter.py` — `switch_model` 加 `**kwargs` 透传：

```python
    def switch_model(self, new_model_id: str, **kwargs) -> None:
        """热切换 LLM 模型。关闭当前后端，用新 model_id 创建后端。

        Args:
            new_model_id: 新的模型标识符（如 "deepseek/deepseek-v4-flash"）。
            **kwargs: 透传给 LLMFactory.create（如 custom provider 的 base_url/api_key）。
        """
        if hasattr(self._backend, 'close'):
            try:
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_running_loop()
                    loop.create_task(self._backend.close())
                except RuntimeError:
                    pass
            except Exception:
                pass
        self._requested_model = new_model_id
        self._backend = LLMFactory.create(new_model_id, **kwargs)
        logger.info("LLM switched to: %s", new_model_id)
```

`core/agent/server.py` — `switch_llm` 与 `switch_tts` 用 `resolve_model_params`：

```python
    async def switch_llm(self, model_id: str) -> dict:
        """热切换 LLM 模型。切换前打断当前 cognitive task 防止卡 thinking。

        custom/<name> 的 base_url/api_key 经 resolve_model_params 从 custom_providers.yaml 解析。
        """
        try:
            self._cognitive.cancel_current()
            from core.input.custom_providers import resolve_model_params
            params = resolve_model_params("llm", model_id)
            self._llm.switch_model(model_id, **params)
            logger.info("LLM switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("LLM switch failed: %s", e)
            return {"ok": False, "error": str(e)}
```

```python
    async def switch_tts(self, model_id: str) -> dict:
        """热切换 TTS 后端。更新 self._tts 和 self._cognitive._tts 引用。

        custom/<name> 的 base_url/api_key 经 resolve_model_params 从 custom_providers.yaml 解析。
        """
        from Module.tts import TTSFactory
        try:
            if self._tts:
                try:
                    await self._tts.close()
                except Exception:
                    pass
            from core.input.custom_providers import resolve_model_params
            params = resolve_model_params("tts", model_id)
            self._tts = TTSFactory.create(model_id, **params)
            self._cognitive.configure(tts=self._tts)  # 同步 CognitiveLoop 中的引用
            logger.info("TTS switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("TTS switch failed: %s", e)
            return {"ok": False, "error": str(e)}
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py::TestWebSocketHandler -q --asyncio-mode=auto`
Expected: PASS（含新测试）

- [ ] **Step 5: Commit**

```bash
git add core/adapter/llm_adapter.py core/agent/server.py core/input/tests/test_server.py
git commit -m "fix: custom provider 切换 — resolve_model_params 解析 base_url/api_key"
```

---

### Task 6: 前端 — ModelSelector 合并 + SettingsPanel 测试按钮/未配置引导

**Files:**
- Modify: `frontend/src/types/protocol.ts`
- Modify: `frontend/src/components/ui/ModelSelector.vue`
- Modify: `frontend/src/components/panels/SettingsPanel.vue`
- 验证：`npx vue-tsc --noEmit` + `npm run build`

**Interfaces:**
- Consumes: `/api/providers` 新响应（`data[service]` 无 `custom` 键、`unconfigured[service]`）；`POST /api/providers/test`（含 provider/model 缺省回退）。
- Produces: 下拉只含已配置/local + custom 合并选项；测试按钮行 + 未配置引导 + 配置入口弹窗。

- [ ] **Step 1: protocol.ts 加类型**

`frontend/src/types/protocol.ts` 的 `CustomProvider` 后追加：

```ts
export interface UnconfiguredEntry {
  name: string
  env_vars: string[]
}

export interface ProvidersResponse {
  llm: Record<string, { models: string[] }>
  stt: Record<string, { models: string[] }>
  tts: Record<string, { models: string[] }>
  custom?: {
    llm?: CustomProvider[]
    stt?: CustomProvider[]
    tts?: CustomProvider[]
  }
  unconfigured?: {
    llm?: UnconfiguredEntry[]
    stt?: UnconfiguredEntry[]
    tts?: UnconfiguredEntry[]
  }
}
```

- [ ] **Step 2: ModelSelector.vue — 合并 custom + 暴露 unconfigured**

将 `loadProviders` 的选项构建改为（删除 `provider === 'custom'` 分支）：

```ts
    const registry = data[props.service] as Record<string, { models: string[] }>
    const customEntries = (data.custom?.[props.service] as CustomEntry[] | undefined) ?? []

    const opts: { value: string; label: string }[] = []

    for (const [provider, info] of Object.entries(registry)) {
      if (!info.models || info.models.length === 0) continue
      for (const m of info.models) {
        const value = `${provider}/${m}`
        opts.push({ value, label: value })
      }
    }
    // 自定义 provider 从顶层 custom 键合并（custom 不在 data[service] 里）
    for (const entry of customEntries) {
      const value = `custom/${entry.name}`
      opts.push({ value, label: value })
    }

    options.value = opts
    unconfigured.value = data.unconfigured?.[props.service] ?? []
```

新增 `const unconfigured = ref<UnconfiguredEntry[]>([])`；在 `<script setup>` 顶部从 protocol.ts import `ProvidersResponse`、`UnconfiguredEntry`；删除本地 `interface ProvidersResponse`。

模板（`<select>` 后）加未配置引导 + 暴露事件：

```html
    <div
      v-if="unconfigured.length > 0"
      class="unconf-hint"
    >
      <span>还有 {{ unconfigured.length }} 个 provider 未配置（{{ unconfigured.map(e => e.name + ' → ' + e.env_vars.join('/')).join('、') }}）</span>
      <button class="unconf-link" @click="emit('open-config')">查看配置方式</button>
    </div>
```

`defineEmits` 加 `open-config: []`；`return { options, loading, unconfigured, refreshProviders }`（`<script setup>` 自动暴露顶层绑定，无需显式 return——确认现有写法后保持一致）。样式加：

```css
.unconf-hint { font-size: 11px; color: var(--muted); margin-top: 4px; display: flex; gap: 6px; align-items: center; }
.unconf-link { font-size: 11px; color: var(--accent); background: none; border: none; cursor: pointer; padding: 0; }
/* 测试按钮行：flex 内联，按钮宽度收敛（.panel-action-btn 默认 width:100%，不覆盖会整行堆叠） */
.test-row { display: flex; gap: 6px; align-items: center; margin-top: 4px; flex-wrap: wrap; }
.test-row .panel-action-btn { width: auto; padding: 4px 10px; font-size: 12px; }
```

- [ ] **Step 3: SettingsPanel.vue — 测试按钮行 + 配置入口弹窗**

`<script setup>` 加 state 与 handler：

```ts
// ---- Provider 测试 ----
const testResult = ref<Record<'llm' | 'stt' | 'tts', string>>({ llm: '', stt: '', tts: '' })
const testLoading = ref<Record<'llm' | 'stt' | 'tts', boolean>>({ llm: false, stt: false, tts: false })

function currentModelId(service: 'llm' | 'stt' | 'tts'): string {
  if (service === 'llm') return settings.llmModel
  if (service === 'stt') return settings.sttModel
  return settings.ttsModel
}

async function handleTest(service: 'llm' | 'stt' | 'tts', level: 'connectivity' | 'usable') {
  if (testLoading.value[service]) return
  testLoading.value[service] = true
  testResult.value[service] = '测试中…'
  const body: Record<string, string> = { service, level }
  const modelId = currentModelId(service)
  if (modelId && modelId.includes('/')) {
    const [provider, ...rest] = modelId.split('/')
    body.provider = provider
    body.model = rest.join('/')
  }
  // 未选中（disabled 态）→ 不带 provider/model，后端回退配置模型
  try {
    const res = await fetch('/api/providers/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    if (data.ok) {
      const detail = data.detail ? `（${data.detail}）` : ''
      testResult.value[service] = `✓ 连通 (${data.latency_ms}ms)${detail}`
    } else {
      testResult.value[service] = `✗ 失败: ${data.error || '未知错误'}`
    }
  } catch (e: any) {
    testResult.value[service] = `✗ 失败: ${e.message}`
  } finally {
    testLoading.value[service] = false
  }
}

// ---- 未配置引导弹窗 ----
const configModalOpen = ref(false)
const configService = ref<'llm' | 'stt' | 'tts'>('llm')
const configUnconfigured = ref<UnconfiguredEntry[]>([])

function openConfigModal(service: 'llm' | 'stt' | 'tts') {
  configService.value = service
  configModalOpen.value = true
  fetch('/api/providers')
    .then(r => r.json())
    .then((data) => { configUnconfigured.value = data.unconfigured?.[service] ?? [] })
    .catch(() => { configUnconfigured.value = [] })
}
```

模板中三个 ModelSelector 各加 `@open-config`，并在每个下拉下加测试按钮行。**STT/TTS 必须保留既有 `:disabled="!settings.xxxAvailable"` 绑定（a810bc7）**——否则未连接服务回归可点：

```html
      <!-- LLM：始终启用 -->
      <div class="setting-row">
        <span class="setting-label">LLM 模型</span>
        <ModelSelector
          service="llm"
          :current-model="settings.llmModel"
          @switch="handleModelSwitch('llm', $event)"
          @open-config="openConfigModal('llm')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.llm" @click="handleTest('llm', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.llm" @click="handleTest('llm', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.llm.startsWith('✓'), fail: testResult.llm.startsWith('✗') }">{{ testResult.llm }}</span>
      </div>

      <!-- STT：保留 disabled（未连接禁用下拉） -->
      <div class="setting-row">
        <span class="setting-label">STT 模型<span v-if="!settings.sttAvailable" class="unavailable-badge">（未连接）</span></span>
        <ModelSelector
          service="stt"
          :current-model="settings.sttModel"
          :disabled="!settings.sttAvailable"
          @switch="handleModelSwitch('stt', $event)"
          @open-config="openConfigModal('stt')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.stt" @click="handleTest('stt', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.stt" @click="handleTest('stt', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.stt.startsWith('✓'), fail: testResult.stt.startsWith('✗') }">{{ testResult.stt }}</span>
      </div>

      <!-- TTS：保留 disabled（同 STT） -->
      <div class="setting-row">
        <span class="setting-label">TTS 模型<span v-if="!settings.ttsAvailable" class="unavailable-badge">（未连接）</span></span>
        <ModelSelector
          service="tts"
          :current-model="settings.ttsModel"
          :disabled="!settings.ttsAvailable"
          @switch="handleModelSwitch('tts', $event)"
          @open-config="openConfigModal('tts')"
        />
      </div>
      <div class="test-row">
        <button class="panel-action-btn" :disabled="testLoading.tts" @click="handleTest('tts', 'connectivity')">测试连通性</button>
        <button class="panel-action-btn" :disabled="testLoading.tts" @click="handleTest('tts', 'usable')">测试可使用性</button>
        <span class="test-result" :class="{ success: testResult.tts.startsWith('✓'), fail: testResult.tts.startsWith('✗') }">{{ testResult.tts }}</span>
      </div>
```

配置入口弹窗（`.custom-modal` 同结构，新增一个）：

```html
  <!-- Unconfigured config modal -->
  <div v-if="configModalOpen" class="custom-modal-overlay" @click="configModalOpen = false">
    <div class="custom-modal" @click.stop>
      <h4>未配置的 {{ { llm: 'LLM', stt: 'STT', tts: 'TTS' }[configService] }} provider</h4>
      <p v-if="configUnconfigured.length === 0" class="test-result">无——已全部配置。</p>
      <div v-for="entry in configUnconfigured" :key="entry.name" class="modal-field">
        <label>{{ entry.name }}</label>
        <div class="env-copy-row">
          <code class="env-snippet">{{ entry.env_vars.map(v => `export ${v}=xxx`).join('\n') }}</code>
          <button class="panel-action-btn" @click="copyText(entry.env_vars.map(v => `export ${v}=xxx`).join('\n'))">复制</button>
        </div>
      </div>
      <p class="test-result">设置环境变量后刷新页面生效。</p>
      <div class="modal-btn-row">
        <button class="panel-action-btn" @click="configModalOpen = false">关闭</button>
      </div>
    </div>
  </div>
```

`copyText` helper（写入 clipboard）：

```ts
async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text) } catch { /* 忽略 */ }
}
```

`<script setup>` 顶部 import `UnconfiguredEntry` from `@/types/protocol`。

- [ ] **Step 4: 验证（类型 + 构建）**

Run:
```bash
cd frontend && npx vue-tsc --noEmit
npm run build
```
Expected: 无类型错误；构建成功输出到 `static/`。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/protocol.ts frontend/src/components/ui/ModelSelector.vue frontend/src/components/panels/SettingsPanel.vue
git commit -m "feat(web): 模型下拉按可用性过滤 + 测试按钮/未配置引导"
```

---

### Task 7: 全量回归 + 收尾

- [ ] **Step 1: 后端全量**

Run: `conda run -n Elysia make test-quick`
Expected: 全绿（Task 1-5 无回归）

- [ ] **Step 2: 前端构建产物确认**

Run: `cd frontend && npm run build`
Expected: `static/assets/` 出现新 hash 文件（构建产物 git-ignored，无需提交）

- [ ] **Step 3: 收尾提交（如有遗漏）**

```bash
git status --short
git add -A && git commit -m "chore: provider 可用性实现收尾" || echo "无待提交变更"
```

## Self-Review

**Spec 覆盖**：
- providers.yaml models/local → Task 1
- env 过滤 + 响应组装 → Task 2
- resolve_model_params → Task 3
- /api/providers 走 registry + test 扩展（level/任意 provider/回退/超时/custom 解析） → Task 4
- custom 切换修复（llm_adapter kwargs + server.py） → Task 5
- 前端（custom 合并、测试按钮兄弟行、disabled 态回退、未配置引导 + 配置入口） → Task 6
- 回归 → Task 7

**已知偏差**（实现时注意，与 spec 有两处务实调整）：
- STT usable 成功标准 = "transcribe 无异常"（spec 写"返回非空文本"）——内置 440Hz tone 样例未必产生非空文本，但能证明服务接受并处理音频。真实语音样本会返回文本，`detail` 报告实际识别结果。
- TTS usable 缺省 character = **agent 角色名**（`agent.character.name`，不是 spec 含糊的"provider 默认音色"）——该角色在 provider 无对应音色时后端报"未知发音人"并返回 ok:false，`detail` 带出（spec 允许在 detail 提示音色缺失）。

**类型一致性**：
- `build_providers_response(custom_data) -> dict`：Task 2 定义，Task 4 使用。
- `resolve_model_params(service, model_id) -> dict`：Task 3 定义，Task 4/5 使用。
- `is_provider_configured(cfg) -> bool`：Task 2 定义，仅内部测试使用。
- 前端 `ProvidersResponse`/`UnconfiguredEntry` 在 protocol.ts 定义，ModelSelector/SettingsPanel import。
