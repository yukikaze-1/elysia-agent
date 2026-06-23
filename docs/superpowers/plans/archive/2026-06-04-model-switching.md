# 模型切换与自定义接入 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 LLM/TTS/STT 的 provider/model 可通过配置驱动，支持自定义 OpenAI 兼容端点，支持运行时热切换，并在前端设置面板提供统一管理界面。

**Architecture:** 三阶段推进。阶段一（配置驱动）：TTS/STT 硬编码 provider 移入 config，修复 LLMAdapter 硬编码 API key 检查。阶段二（custom provider）：三个模块注册表各新增 `custom` 条目，独立 `custom_providers.yaml` 管理。阶段三（热切换 + 前端）：协议层新增 `test()` 方法，Agent 层新增 `switch_*` 方法，REST + WS 端点，前端模型配置面板。

**Tech Stack:** Python 3.13, aiohttp, AsyncOpenAI, httpx, vanilla JS (ES modules), YAML

---

### Task 1: 扩展 AppConfig — 各模块新增 model/api_key/base_url 字段

**Files:**
- Modify: `core/app_config.py`
- Read: `Module/llm/config.py`, `Module/stt/config.py`, `Module/tts/config.py`

- [ ] **Step 1: 扩展 `core/app_config.py` 中的 `LLMConfig`、`STTConfig`、`TTSConfig`**

```python
# core/app_config.py — 替换 LLMConfig
@dataclass
class LLMConfig:
    model: str = ConfigField(default="deepseek/deepseek-chat")
    base_url: str = ConfigField(default="https://api.deepseek.com", env="LLM_BASE_URL")
    api_key: str = ConfigField(default="", secret=True, env="LLM_API_KEY")


# 替换 STTConfig
@dataclass
class STTConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="sensevoice/sense-voice")
    base_url: str = ConfigField(default="http://localhost:20042", env="STT_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="STT_API_KEY")


# 替换 TTSConfig
@dataclass
class TTSConfig:
    enabled: bool = ConfigField(default=False)
    model: str = ConfigField(default="gpt-sovits/default")
    base_url: str = ConfigField(default="http://localhost:9880", env="TTS_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="TTS_API_KEY")
    voices_config: str = ConfigField(default="")
```

- [ ] **Step 2: 同时更新 `Module/llm/config.py`，使模块层 config 与应用层一致**

```python
# Module/llm/config.py — 替换 LLMConfig
@dataclass
class LLMConfig:
    model: str = ConfigField(default="deepseek/deepseek-chat")
    base_url: str = ConfigField(default="https://api.deepseek.com", env="LLM_API_BASE")
    api_key: str = ConfigField(default="", secret=True, env="LLM_API_KEY")
```

- [ ] **Step 3: 更新 `Module/stt/config.py`**

```python
# Module/stt/config.py — 替换 STTConfig
@dataclass
class STTConfig:
    model: str = ConfigField(default="sensevoice/sense-voice")
    base_url: str = ConfigField(default="http://localhost:20042", env="STT_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="STT_API_KEY")
```

- [ ] **Step 4: 更新 `Module/tts/config.py`**

```python
# Module/tts/config.py — 替换 TTSConfig
@dataclass
class TTSConfig:
    model: str = ConfigField(default="gpt-sovits/default")
    base_url: str = ConfigField(default="http://localhost:9880", env="TTS_API_URL")
    api_key: str = ConfigField(default="", secret=True, env="TTS_API_KEY")
    voices_config: str = ConfigField(default="", help="音色配置 YAML 文件路径，为空则使用内置 voices.yaml")
```

- [ ] **Step 5: 更新 `config.yaml` 添加新字段**

```yaml
# config.yaml — 在 tts 和 stt 区块新增 model 字段
tts:
  enabled: true
  model: gpt-sovits/default

stt:
  enabled: true
  model: sensevoice/sense-voice
```

- [ ] **Step 6: 运行现有配置测试确保不破坏**

```bash
python -m pytest Module/config/ -v
python -m pytest core/tests/ -v -k "config or test_app"
```

- [ ] **Step 7: Commit**

```bash
git add core/app_config.py Module/llm/config.py Module/stt/config.py Module/tts/config.py config.yaml
git commit -m "feat: extend AppConfig with model/api_key/base_url fields for all services"
```

---

### Task 2: 修复 LLMAdapter — 移除硬编码 DEEPSEEK_API_KEY 检查

**Files:**
- Modify: `core/llm_adapter.py:49-52`

- [ ] **Step 1: 移除硬编码检查**

在 `core/llm_adapter.py` 删除 L49-52：

```python
# 删除以下 4 行（L49-52）:
        if not os.getenv("DEEPSEEK_API_KEY", ""):
            raise RuntimeError(
                "DEEPSEEK_API_KEY not set. Set the env var to use the LLM."
            )
```

`LLMFactory.create()` 在 registry 中已按 provider 查找对应的 env var（如 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`）。`OpenAICompatBackend.__init__` 中 `api_key or "ollama"` 允许 Ollama 无 key。删除后不同 provider 的 key 检查由 registry 层统一处理。

- [ ] **Step 2: 同时移除顶部未使用的 `import os`（如果只用于此检查）**

```python
# 检查 os 是否还被其他地方使用（L118-130 _extract_status 等不使用 os）。
# 如果只用于 `os.getenv("DEEPSEEK_API_KEY")`，删除 L10 的 `import os`。
```

- [ ] **Step 3: 运行 LLM 相关测试**

```bash
python -m pytest core/tests/ -v -k "llm or adapter"
python -m pytest Module/llm/ -v
```

- [ ] **Step 4: Commit**

```bash
git add core/llm_adapter.py
git commit -m "fix: remove hardcoded DEEPSEEK_API_KEY check from LLMAdapter"
```

---

### Task 3: Agent 使用 config 字段创建 TTS/STT

**Files:**
- Modify: `core/agent.py:87-93` (TTS), `core/agent.py:368-373` (STT)

- [ ] **Step 1: 修改 TTS 创建逻辑**

在 `core/agent.py` L90 将硬编码替换为 config：

```python
# 原来:
                self._tts = TTSFactory.create("gpt-sovits/default")
# 改为:
                self._tts = TTSFactory.create(config.tts.model)
```

- [ ] **Step 2: 修改 STT 创建逻辑**

在 `core/agent.py` L373 将硬编码替换为 config：

```python
# 原来:
                    stt_backend = STTFactory.create("sensevoice/sense-voice")
# 改为:
                    stt_backend = STTFactory.create(config.stt.model)
```

- [ ] **Step 3: 运行现有测试确保兼容**

```bash
python -m pytest core/tests/ -v -k "agent or tts or stt"
```

- [ ] **Step 4: Commit**

```bash
git add core/agent.py
git commit -m "feat: use config fields for TTS and STT provider selection"
```

---

### Task 4: LLM/STT/TTS 注册表新增 custom 条目

**Files:**
- Modify: `Module/llm/registry.py`
- Modify: `Module/stt/registry.py`
- Modify: `Module/tts/registry.py`

- [ ] **Step 1: LLM registry 新增 custom 条目**

在 `Module/llm/registry.py` 的 `_BACKENDS` dict 末尾新增：

```python
    "custom": {
        "base_url": "",      # 从 kwargs 或 config 读取
        "env_key": "CUSTOM_LLM_API_KEY",
        "supports_prefix": False,
    },
```

同时修改 `LLMFactory.create()` 处理 `custom` provider —— 当 provider 为 `custom` 时，`base_url` 必须由调用方提供：

```python
# 替换 L59-61
        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", cfg["base_url"])
        beta_url = kwargs.pop("beta_url", cfg.get("beta_url", ""))

        env_key = cfg["env_key"]
        api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")
```

- [ ] **Step 2: STT registry 新增 custom 条目**

在 `Module/stt/registry.py` 的 `_BACKENDS` dict 末尾新增：

```python
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_STT_API_KEY",
    },
```

修改 `STTFactory.create()` —— 当 provider 为 `custom` 时，允许通过 `custom` provider 创建。当前 STTFactory 只创建 `SenseVoiceBackend`，需要支持用 `OpenAICompatBackend` 风格创建自定义 STT。由于 STT 没有 OpenAI 兼容的通用后端类，需要新加一个：

在 `Module/stt/` 创建 `openai_stt.py`：

```python
"""OpenAI 兼容 STT 后端 — 用于 custom provider。"""

import logging
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from .protocol import STTBackend


class OpenAICompatSTTBackend(STTBackend):
    """OpenAI 兼容语音识别后端 (Whisper API 格式)。"""

    def __init__(
        self,
        model: str = "whisper-1",
        *,
        api_key: str = "",
        base_url: str = "",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncOpenAI(
            api_key=api_key or "stt",
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def transcribe(self, audio: bytes, **kwargs) -> str:
        from io import BytesIO
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=("audio.wav", BytesIO(audio), "audio/wav"),
            **kwargs,
        )
        return result.text

    async def transcribe_stream(self, audio: bytes, **kwargs) -> AsyncIterator[str]:
        text = await self.transcribe(audio, **kwargs)
        yield text

    async def close(self) -> None:
        await self._client.close()
```

然后在 `STTFactory.create()` 中添加 dispatch：

```python
# 替换 L46-63
        if provider in ("openai", "deepgram"):
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", cfg["base_url"])

        env_key = cfg["env_key"]
        api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")

        if provider == "custom":
            from .openai_stt import OpenAICompatSTTBackend
            if not base_url:
                raise ValueError("custom STT provider requires base_url")
            return OpenAICompatSTTBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return SenseVoiceBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
```

- [ ] **Step 3: TTS registry 新增 custom 条目**

在 `Module/tts/registry.py` 的 `_BACKENDS` dict 末尾新增：

```python
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_TTS_API_KEY",
    },
```

修改 `TTSFactory.create()` —— 当 provider 为 `custom` 时，创建 `GPTSoVitsBackend`（因为 TTS 没有 OpenAI 标准协议）。custom TTS 需要 base_url 由调用方提供：

```python
# TTSFactory.create() 中，替换 L38-44
        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", "") or os.getenv(cfg["env_key"], "") or cfg["base_url"]

        if provider == "custom" and not base_url:
            raise ValueError("custom TTS provider requires base_url")

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()

        return GPTSoVitsBackend(base_url=base_url, **kwargs)
```

- [ ] **Step 4: 运行相关测试**

```bash
python -m pytest Module/llm/ -v
python -m pytest Module/stt/ -v
python -m pytest Module/tts/ -v
```

- [ ] **Step 5: Commit**

```bash
git add Module/llm/registry.py Module/stt/registry.py Module/tts/registry.py Module/stt/openai_stt.py
git commit -m "feat: add custom provider support to LLM/STT/TTS registries"
```

---

### Task 5: 实现 custom_providers.yaml 读写

**Files:**
- Create: `core/custom_providers.py`

- [ ] **Step 1: 创建 `core/custom_providers.py`**

```python
"""自定义 provider 持久化 — 读写 custom_providers.yaml。"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("custom_providers.yaml")

# 文件初始骨架
_SKELETON = {"llm": [], "stt": [], "tts": []}


def load(path: str | Path | None = None) -> dict:
    """读取自定义 provider 配置。文件不存在时返回空骨架。"""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {k: list(v) for k, v in _SKELETON.items()}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.warning("Failed to load %s, using empty config", p, exc_info=True)
        return {k: list(v) for k, v in _SKELETON.items()}
    result = {}
    for key in _SKELETON:
        result[key] = list(data.get(key, []))
    return result


def save(data: dict, path: str | Path | None = None) -> None:
    """保存自定义 provider 配置到 YAML 文件。"""
    p = Path(path) if path else _DEFAULT_PATH
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 自定义接入端点 — 通过前端设置面板管理，不手动编辑\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def add_provider(service: str, entry: dict, path: str | Path | None = None) -> dict:
    """添加一个自定义 provider，返回更新后的完整数据。

    entry 格式: {"name": "siliconflow", "base_url": "...", "api_key_env": "...", "models": [...]}
    """
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    # 同名覆盖
    existing = [e for e in data[service] if e.get("name") == entry["name"]]
    if existing:
        data[service].remove(existing[0])
    data[service].append(entry)
    save(data, path)
    return data


def remove_provider(service: str, name: str, path: str | Path | None = None) -> dict:
    """删除一个自定义 provider，返回更新后的完整数据。"""
    data = load(path)
    if service not in data:
        raise ValueError(f"Unknown service: {service}")
    data[service] = [e for e in data[service] if e.get("name") != name]
    save(data, path)
    return data
```

- [ ] **Step 2: 运行 Python 语法检查**

```bash
python -c "from core.custom_providers import load, save, add_provider, remove_provider; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/custom_providers.py
git commit -m "feat: add custom_providers.yaml persistence layer"
```

---

### Task 6: 协议层新增 test() 抽象方法

**Files:**
- Modify: `Module/llm/protocol.py`
- Modify: `Module/stt/protocol.py`
- Modify: `Module/tts/protocol.py`

- [ ] **Step 1: LLMBackend 新增 test()**

在 `Module/llm/protocol.py` 的 `LLMBackend` 类中，`close()` 之前新增：

```python
    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
        ...
```

- [ ] **Step 2: STTBackend 新增 test()**

在 `Module/stt/protocol.py` 的 `STTBackend` 类中，`close()` 之前新增：

```python
    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
        ...
```

- [ ] **Step 3: TTSBackend 新增 test()**

在 `Module/tts/protocol.py` 的 `TTSBackend` 类中，`close()` 之前新增：

```python
    @abstractmethod
    async def test(self) -> dict:
        """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
        ...
```

- [ ] **Step 4: Commit**

```bash
git add Module/llm/protocol.py Module/stt/protocol.py Module/tts/protocol.py
git commit -m "feat: add test() abstract method to LLM/STT/TTS protocols"
```

---

### Task 7: 实现 test() — OpenAICompatBackend (LLM)

**Files:**
- Modify: `Module/llm/openai_compat.py`

- [ ] **Step 1: 添加 test() 实现**

在 `OpenAICompatBackend` 类的 `reconnect()` 方法之后添加：

```python
    async def test(self) -> dict:
        """测试连通性：发最小 chat 请求，测延迟。5 秒超时。"""
        import time
        t0 = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                ),
                timeout=5.0,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
```

需要在文件顶部添加 `import asyncio` 和 `import time`（time 不一定已导入）。

- [ ] **Step 2: 更新所有 mock 实现以匹配协议**

检查并更新 `Module/llm/test_llm.py` 中的 mock 类（如有实现 `LLMBackend` 的），添加 `async def test(self) -> dict: return {"ok": True, "latency_ms": 0}`。

```bash
grep -rn "class.*LLMBackend" Module/llm/ --include="*.py"
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest Module/llm/ -v
```

- [ ] **Step 4: Commit**

```bash
git add Module/llm/openai_compat.py Module/llm/test_llm.py
git commit -m "feat: implement test() in OpenAICompatBackend"
```

---

### Task 8: 实现 test() — SenseVoiceBackend (STT)

**Files:**
- Modify: `Module/stt/sensevoice.py`

- [ ] **Step 1: 添加 test() 实现**

在 `SenseVoiceBackend` 类的 `close()` 之前添加：

```python
    async def test(self) -> dict:
        """测试连通性：GET 根路径检查服务可用。5 秒超时。"""
        import asyncio
        import time
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._client.get("/"),
                timeout=5.0,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": resp.status_code < 500, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
```

- [ ] **Step 2: 为 OpenAICompatSTTBackend 添加 test()**

在 `Module/stt/openai_stt.py` 的 `OpenAICompatSTTBackend` 类中添加：

```python
    async def test(self) -> dict:
        """测试连通性：发最小转录请求（空音频）。5 秒超时。"""
        import asyncio
        import time
        t0 = time.perf_counter()
        try:
            # 生成 1 秒静音 WAV (16kHz, mono, 16-bit)
            silence = _make_silence_wav()
            from io import BytesIO
            await asyncio.wait_for(
                self._client.audio.transcriptions.create(
                    model=self._model,
                    file=("silence.wav", BytesIO(silence), "audio/wav"),
                ),
                timeout=5.0,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
```

在文件顶部添加辅助函数：

```python
import struct

def _make_silence_wav(duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    """生成静音 WAV 字节，用于连通性测试。"""
    num_samples = int(sample_rate * duration_sec)
    data = b"\x00\x00" * num_samples
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data),
        b"WAVE", b"fmt ", 16,
        1, 1, sample_rate,
        sample_rate * 2, 2, 16,
        b"data", len(data),
    )
    return header + data
```

- [ ] **Step 3: 运行测试**

```bash
python -m pytest Module/stt/ -v
```

- [ ] **Step 4: Commit**

```bash
git add Module/stt/sensevoice.py Module/stt/openai_stt.py
git commit -m "feat: implement test() in SenseVoice and OpenAI STT backends"
```

---

### Task 9: 实现 test() — GPTSoVitsBackend (TTS)

**Files:**
- Modify: `Module/tts/gpt_sovits.py`

- [ ] **Step 1: 添加 test() 实现**

在 `GPTSoVitsBackend` 类的 `close()` 之前添加：

```python
    async def test(self) -> dict:
        """测试连通性：GET 根路径检查服务可用。5 秒超时。"""
        import asyncio
        import time
        import httpx
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=httpx.Timeout(5.0)) as client:
                resp = await asyncio.wait_for(client.get("/"), timeout=5.0)
                latency_ms = int((time.perf_counter() - t0) * 1000)
                return {"ok": resp.status_code < 500, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
```

`asyncio` 和 `httpx` 已在文件顶部导入（httpx 在 L13、asyncio 在 L8），不需要新增 import。

- [ ] **Step 2: 运行 TTS 测试**

```bash
python -m pytest Module/tts/ -v
```

- [ ] **Step 3: Commit**

```bash
git add Module/tts/gpt_sovits.py
git commit -m "feat: implement test() in GPTSoVitsBackend"
```

---

### Task 10: LLMAdapter 新增 switch_model() 方法

**Files:**
- Modify: `core/llm_adapter.py`

- [ ] **Step 1: 在 LLMAdapter 类中添加 switch_model()**

在 `reconnect()` 方法之后、`model` property 之前添加：

```python
    def switch_model(self, new_model_id: str) -> None:
        """热切换 LLM 模型。关闭当前后端，用新 model_id 创建后端。"""
        if hasattr(self._backend, 'close'):
            try:
                # 同步关闭 — LLM backend 的 close 是 async，但热切换场景
                # 当前后端已不在使用中，可以 fire-and-forget
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_running_loop()
                    loop.create_task(self._backend.close())
                except RuntimeError:
                    pass
            except Exception:
                pass
        self._requested_model = new_model_id
        self._backend = LLMFactory.create(new_model_id)
        logger.info("LLM switched to: %s", new_model_id)
```

- [ ] **Step 2: 运行 LLM 相关测试**

```bash
python -m pytest core/tests/ -v -k "llm or adapter"
```

- [ ] **Step 3: Commit**

```bash
git add core/llm_adapter.py
git commit -m "feat: add switch_model() to LLMAdapter for hot-switching"
```

---

### Task 11: Agent 层新增 switch_llm/switch_stt/switch_tts 方法

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: 在 Agent 类中添加 switch_llm()**

在 `next_request_id()` 之后添加：

```python
    async def switch_llm(self, model_id: str) -> dict:
        """热切换 LLM 模型。返回 {"ok": bool, "error": str}"""
        try:
            self._llm.switch_model(model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("LLM switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def switch_stt(self, model_id: str) -> dict:
        """运行时更新 STT model_id（下次 transcribe 请求生效）。"""
        self._config.stt.model = model_id
        logger.info("STT model set to: %s (takes effect on next request)", model_id)
        return {"ok": True}

    async def switch_tts(self, model_id: str) -> dict:
        """热切换 TTS 后端。更新 self._tts 和 self._cognitive._tts 引用。"""
        from Module.tts import TTSFactory
        try:
            if self._tts:
                try:
                    await self._tts.close()
                except Exception:
                    pass
            self._tts = TTSFactory.create(model_id)
            self._cognitive._tts = self._tts  # 同步 CognitiveLoop 中的引用
            logger.info("TTS switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("TTS switch failed: %s", e)
            return {"ok": False, "error": str(e)}
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git add core/agent.py
git commit -m "feat: add switch_llm/switch_stt/switch_tts methods to Agent"
```

---

### Task 12: 新增 REST API 端点 — /api/providers

**Files:**
- Modify: `Module/input/server.py`

- [ ] **Step 1: 在 `create_app()` 中添加 provider 相关 route**

在 `create_app()` 函数中，`return app` 之前添加以下 handler 注册：

```python
    # ---- 模型切换 API ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回可用 provider 列表（注册表 + custom_providers.yaml）。"""
        from core.custom_providers import load as load_custom

        llm_registry = {
            "deepseek": {"models": ["deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro"]},
            "openai": {"models": ["gpt-4", "gpt-4o", "gpt-5"]},
            "ollama": {"models": ["qwen3:latest"]},
            "gemini": {"models": []},
            "custom": {"models": []},
        }
        stt_registry = {
            "sensevoice": {"models": ["sense-voice"]},
            "custom": {"models": []},
        }
        tts_registry = {
            "gpt-sovits": {"models": ["default"]},
            "custom": {"models": []},
        }

        custom_data = load_custom()
        # 合并 custom providers 到注册表
        for entry in custom_data.get("llm", []):
            name = entry["name"]
            llm_registry["custom"]["models"].append(name)
        for entry in custom_data.get("stt", []):
            name = entry["name"]
            stt_registry["custom"]["models"].append(name)
        for entry in custom_data.get("tts", []):
            name = entry["name"]
            tts_registry["custom"]["models"].append(name)

        return web.json_response({
            "llm": llm_registry,
            "stt": stt_registry,
            "tts": tts_registry,
            "custom": custom_data,
        })
    app.router.add_get("/api/providers", providers_list_handler)

    async def providers_save_handler(request: web.Request) -> web.Response:
        """保存自定义 provider 到 custom_providers.yaml。"""
        from core.custom_providers import add_provider
        try:
            body = await request.json()
            service = body.get("service", "")
            entry = body.get("entry", {})
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        if not entry.get("name") or not entry.get("base_url"):
            return web.json_response({"ok": False, "error": "缺少 name 或 base_url"}, status=400)
        try:
            data = add_provider(service, entry)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
    app.router.add_post("/api/providers", providers_save_handler)

    async def providers_delete_handler(request: web.Request) -> web.Response:
        """删除一个自定义 provider。"""
        from core.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        try:
            data = remove_provider(service, name)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)

    async def providers_test_handler(request: web.Request) -> web.Response:
        """测试 provider 连通性。"""
        try:
            body = await request.json()
            service = body.get("service", "")
            base_url = body.get("base_url", "")
            api_key = body.get("api_key", "")
            model = body.get("model", "")
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)

        try:
            if service == "llm":
                from Module.llm import LLMFactory
                backend = LLMFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "stt":
                from Module.stt import STTFactory
                backend = STTFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "tts":
                from Module.tts import TTSFactory
                backend = TTSFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            else:
                return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)

            result = await backend.test()
            try:
                await backend.close()
            except Exception:
                pass
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
    app.router.add_post("/api/providers/test", providers_test_handler)
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tests/test_phase3.py -v
```

- [ ] **Step 3: Commit**

```bash
git add Module/input/server.py
git commit -m "feat: add /api/providers REST endpoints for model management"
```

---

### Task 13: WebSocket 添加 switch_model 消息处理

**Files:**
- Modify: `Module/input/server.py` (`_handle_json` 函数)

- [ ] **Step 1: 在 `_handle_json()` 中添加 switch_model case**

在 `_handle_json()` 函数的 `elif msg_type == "tool_approval":` 之前添加：

```python
    elif msg_type == "switch_model":
        service = data.get("service", "")
        model = data.get("model", "")
        if service == "llm":
            result = await agent.switch_llm(model)
        elif service == "stt":
            result = await agent.switch_stt(model)
        elif service == "tts":
            result = await agent.switch_tts(model)
        else:
            result = {"ok": False, "error": f"未知 service: {service}"}
        from Module.output import WebSocketChannel
        for ch in agent._output._channels:
            if isinstance(ch, WebSocketChannel) and ch._ws is ws:
                try:
                    await ws.send_json({
                        "type": "switch_model_result",
                        "service": service,
                        "model": model,
                        **result,
                    })
                except Exception:
                    pass
                break
        logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))
```

注意：需要把 `_handle_json` 从 `async def _handle_json(data, agent, audio_buf=None)` 改为接受 `ws` 参数以便回复。检查函数签名和调用方。

当前调用方在 `_handle_ws` 中 L393：
```python
await _handle_json(data, agent, audio_buf)
```
需要改为：
```python
await _handle_json(data, agent, audio_buf, ws)
```

函数签名改为：
```python
async def _handle_json(data: dict, agent, audio_buf=None, ws=None) -> None:
```

- [ ] **Step 2: 运行测试**

```bash
python -m pytest core/tests/test_phase3.py -v
```

- [ ] **Step 3: Commit**

```bash
git add Module/input/server.py
git commit -m "feat: add switch_model WS message handling"
```

---

### Task 14: 前端 — 设置面板新增模型配置 UI

**Files:**
- Modify: `static/js/settings.js`
- Modify: `static/index.html` (可能需要新增 DOM 元素)
- Read: `static/js/ws.js`, `static/js/state.js`, `static/js/config.js`

- [ ] **Step 1: 读取 `static/index.html` 了解设置面板 HTML 结构**

```bash
# 确认设置面板的 DOM 结构，确定在哪里插入模型配置区块
```

- [ ] **Step 2: 在 `static/index.html` 设置面板中添加模型配置 HTML**

在 `<div id="settings-panel">` 内部、"清空对话"按钮之前添加：

```html
      <div class="section-title">── 模型配置 ──</div>

      <label>LLM</label>
      <select id="llm-model-select" style="width:100%;margin-bottom:8px;padding:4px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
        <option value="">加载中…</option>
      </select>

      <label>STT</label>
      <select id="stt-model-select" style="width:100%;margin-bottom:8px;padding:4px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
        <option value="">加载中…</option>
      </select>

      <label>TTS</label>
      <select id="tts-model-select" style="width:100%;margin-bottom:12px;padding:4px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
        <option value="">加载中…</option>
      </select>

      <div class="section-title">── 自定义端点 ──</div>
      <button id="add-custom-btn" style="margin-bottom:8px;">+ 添加自定义端点</button>
      <div id="custom-providers-list"></div>

      <!-- 自定义端点模态框 -->
      <div id="custom-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;align-items:center;justify-content:center;">
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;width:360px;max-height:80vh;overflow-y:auto;">
          <h3 style="margin-bottom:12px;">添加自定义端点</h3>
          <label>服务类型</label>
          <select id="custom-service" style="width:100%;margin-bottom:8px;padding:4px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
            <option value="llm">LLM</option>
            <option value="stt">STT</option>
            <option value="tts">TTS</option>
          </select>
          <label>名称</label>
          <input id="custom-name" placeholder="如 siliconflow" style="width:100%;margin-bottom:8px;padding:6px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
          <label>Base URL</label>
          <input id="custom-url" placeholder="https://api.siliconflow.cn/v1" style="width:100%;margin-bottom:8px;padding:6px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
          <label>API Key 环境变量名</label>
          <input id="custom-key-env" placeholder="SILICONFLOW_API_KEY" style="width:100%;margin-bottom:8px;padding:6px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
          <label>模型列表（逗号分隔）</label>
          <input id="custom-models" placeholder="Qwen/Qwen3-235B, deepseek-ai/DeepSeek-V3" style="width:100%;margin-bottom:12px;padding:6px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;">
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button id="custom-cancel-btn" style="padding:6px 16px;background:transparent;border:1px solid var(--border);color:var(--text);border-radius:4px;cursor:pointer;">取消</button>
            <button id="custom-test-btn" style="padding:6px 16px;background:transparent;border:1px solid var(--border);color:var(--text);border-radius:4px;cursor:pointer;">测试连接</button>
            <button id="custom-save-btn" style="padding:6px 16px;background:var(--accent);color:#fff;border:none;border-radius:4px;cursor:pointer;">保存</button>
          </div>
          <div id="custom-test-result" style="margin-top:8px;font-size:12px;"></div>
        </div>
      </div>
```

- [ ] **Step 3: 在 `static/js/settings.js` 中添加模型配置逻辑**

在文件末尾追加：

```javascript
// ---- 模型配置 ----

/** 从 /api/providers 加载可用 provider 列表并填充下拉框 */
async function loadProviders() {
  try {
    const res = await fetch('/api/providers');
    const data = await res.json();

    fillSelect('llm-model-select', data.llm, data.custom?.llm || [], 'llm');
    fillSelect('stt-model-select', data.stt, data.custom?.stt || [], 'stt');
    fillSelect('tts-model-select', data.tts, data.custom?.tts || [], 'tts');

    renderCustomProviders(data.custom);
  } catch (e) {
    console.warn('加载 provider 列表失败', e);
  }
}

function fillSelect(selectId, registry, customEntries, service) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.innerHTML = '';

  for (const [provider, info] of Object.entries(registry)) {
    if (!info.models || info.models.length === 0) continue;
    const models = provider === 'custom'
      ? customEntries.map(e => e.name)
      : info.models;
    for (const m of models) {
      const value = `${provider}/${m}`;
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = `${provider}/${m}`;
      sel.appendChild(opt);
    }
  }

  // 读取保存的当前选择
  const saved = localStorage.getItem(`model-${service}`);
  if (saved) sel.value = saved;

  sel.onchange = () => {
    const modelId = sel.value;
    if (!modelId) return;
    localStorage.setItem(`model-${service}`, modelId);
    send({ type: 'switch_model', service, model: modelId });
    addStatusMsg(`${service.toUpperCase()} 已切换至 ${modelId}`);
  };
}

function renderCustomProviders(customData) {
  const container = document.getElementById('custom-providers-list');
  if (!container) return;
  container.innerHTML = '';

  const all = [
    ...(customData?.llm || []).map(e => ({ ...e, service: 'llm' })),
    ...(customData?.stt || []).map(e => ({ ...e, service: 'stt' })),
    ...(customData?.tts || []).map(e => ({ ...e, service: 'tts' })),
  ];

  for (const entry of all) {
    const div = document.createElement('div');
    div.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:8px;margin-bottom:6px;font-size:12px;';
    div.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:600;">${escHtml(entry.name)}</span>
        <span style="color:var(--muted);font-size:10px;">${entry.service}</span>
      </div>
      <div style="color:var(--muted);margin:4px 0;">${escHtml(entry.base_url)}</div>
      <div style="color:var(--muted);">models: ${(entry.models || []).join(', ')}</div>
      <button data-delete="${entry.service}/${entry.name}" style="margin-top:4px;padding:2px 8px;font-size:11px;background:transparent;border:1px solid #a33;color:#f88;border-radius:4px;cursor:pointer;">删除</button>
    `;
    container.appendChild(div);
  }

  // 删除按钮事件
  container.querySelectorAll('button[data-delete]').forEach(btn => {
    btn.onclick = async () => {
      const [service, name] = btn.dataset.delete.split('/');
      if (!confirm(`确定删除 ${name}？`)) return;
      try {
        const res = await fetch(`/api/providers/${service}/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (res.ok) {
          addStatusMsg(`${name} 已删除`);
          loadProviders();
        }
      } catch (e) {
        addStatusMsg('删除失败');
      }
    };
  });
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---- 自定义端点模态框 ----

function setupCustomModal() {
  const modal = document.getElementById('custom-modal');
  const addBtn = document.getElementById('add-custom-btn');
  const cancelBtn = document.getElementById('custom-cancel-btn');
  const saveBtn = document.getElementById('custom-save-btn');
  const testBtn = document.getElementById('custom-test-btn');

  addBtn.onclick = () => { modal.style.display = 'flex'; };
  cancelBtn.onclick = () => { modal.style.display = 'none'; };

  saveBtn.onclick = async () => {
    const service = document.getElementById('custom-service').value;
    const name = document.getElementById('custom-name').value.trim();
    const base_url = document.getElementById('custom-url').value.trim();
    const api_key_env = document.getElementById('custom-key-env').value.trim();
    const modelsStr = document.getElementById('custom-models').value.trim();

    if (!name || !base_url) {
      alert('名称和 Base URL 不能为空');
      return;
    }

    const entry = {
      name, base_url, api_key_env,
      models: modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [],
    };

    try {
      const res = await fetch('/api/providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service, entry }),
      });
      const data = await res.json();
      if (data.ok) {
        addStatusMsg(`${name} 已保存`);
        modal.style.display = 'none';
        loadProviders();
      } else {
        alert('保存失败: ' + (data.error || '未知错误'));
      }
    } catch (e) {
      alert('保存失败: ' + e.message);
    }
  };

  testBtn.onclick = async () => {
    const service = document.getElementById('custom-service').value;
    const base_url = document.getElementById('custom-url').value.trim();
    const modelsStr = document.getElementById('custom-models').value.trim();
    const model = modelsStr ? modelsStr.split(',')[0].trim() : 'default';
    const resultEl = document.getElementById('custom-test-result');

    if (!base_url) {
      resultEl.innerHTML = '<span style="color:#f88;">请输入 Base URL</span>';
      return;
    }

    resultEl.innerHTML = '<span style="color:var(--muted);">测试中…</span>';
    try {
      const res = await fetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service, base_url, api_key: '', model }),
      });
      const data = await res.json();
      if (data.ok) {
        resultEl.innerHTML = `<span style="color:#4caf50;">✓ 连接成功 (${data.latency_ms}ms)</span>`;
      } else {
        resultEl.innerHTML = `<span style="color:#f88;">✕ ${escHtml(data.error || '连接失败')}</span>`;
      }
    } catch (e) {
      resultEl.innerHTML = `<span style="color:#f88;">✕ ${escHtml(e.message)}</span>`;
    }
  };
}
```

- [ ] **Step 4: 在 `setupSettings()` 末尾调用初始化**

在 `setupSettings()` 函数末尾添加：

```javascript
  loadProviders();
  setupCustomModal();
```

- [ ] **Step 5: 在 `static/index.html` 中确保 `<select>` 元素 ID 在 HTML 中已添加，且自定义模态框 HTML 已添加**

- [ ] **Step 6: 在 `static/js/main.js` 的消息路由中添加 switch_model_result 处理**

需要查看 `main.js` 确认 WS 消息路由位置，添加：

```javascript
    case 'switch_model_result':
      // 在 chat 区域显示状态消息
      const sm = msg;
      if (sm.ok) {
        addStatusMsg(`${sm.service.toUpperCase()} 已切换至 ${sm.model}`);
      } else {
        addStatusMsg(`${sm.service.toUpperCase()} 切换失败: ${sm.error}`);
      }
      break;
```

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/js/settings.js static/js/main.js
git commit -m "feat: add model switching UI to settings panel"
```

---

### Task 15: 前端 CSS — 模型配置样式

**Files:**
- Modify: `static/css/chat.css`

- [ ] **Step 1: 在 chat.css 末尾添加模型配置相关样式**

```css
/* ---- 模型配置 (设置面板内) ---- */
#settings-panel .section-title {
  font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 1px; margin: 12px 0 8px; padding-top: 8px;
  border-top: 1px solid var(--border);
}
#settings-panel select {
  width: 100%; margin-bottom: 8px; padding: 4px;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 4px;
}
#custom-providers-list { max-height: 180px; overflow-y: auto; }
```

- [ ] **Step 2: Commit**

```bash
git add static/css/chat.css
git commit -m "style: add model config panel CSS"
```

---

### 验证

```bash
# 全量单元测试
python -m pytest Module/ -v
python -m pytest core/tests/ -v

# 手动验证步骤:
# 1. 启动服务: python -m core.agent
# 2. 打开 http://localhost:8765，点击右上角齿轮图标
# 3. 确认 LLM/STT/TTS 下拉框已填充
# 4. 切换 LLM 模型 → 确认状态消息
# 5. 点击"添加自定义端点" → 填写表单 → 测试连接 → 保存
# 6. 确认 custom_providers.yaml 已生成
# 7. 删除自定义端点 → 确认从列表消失
# 8. 重启服务 → 确认选择持久化
```