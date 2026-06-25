# Module/_common/ 轻量共享层 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Module/_common/ 下提供轻量工具函数，消除 6 个 registry、5 个 backend test()、6 个 test_config.py、9 个 __main__.py 以及 config/ 内部的重复代码。

**Architecture:** 新增 Module/_common/ (6 文件) → 逐模块替换内联代码为共享函数调用 → 删 config/ 内重复函数。不改 backend 实现逻辑，不改大文件结构，不碰 task_system。

**Tech Stack:** Python 3.13, pytest

## Global Constraints

- 所有对外 API 签名不变
- 所有现有测试必须继续通过
- `_common/` 不暴露为公开 API（Module/__init__.py 不导出）
- 不改 backend 实现逻辑、不拆大文件、不引入类继承
- 使用 conda 环境：`conda activate Elysia`

---

### Task 1: 创建 Module/_common/ 目录和全部 6 个文件

**Files:**
- Create: `Module/_common/__init__.py`
- Create: `Module/_common/env.py`
- Create: `Module/_common/registry_helpers.py`
- Create: `Module/_common/test_helpers.py`
- Create: `Module/_common/config_test_helpers.py`
- Create: `Module/_common/main_runner.py`

**Interfaces:**
- Produces: `env.ensure_dotenv()`, `registry_helpers.resolve_api_key(env_key, provider_cfg, explicit_key) -> str`, `registry_helpers.resolve_base_url(env_key, provider_cfg, explicit_url) -> str`, `registry_helpers.inject_logger(name, explicit_logger) -> Logger`, `test_helpers.make_test_method(health_fn) -> Callable`, `config_test_helpers.make_config_test_classes(ConfigClass, defaults, overrides, env_key, yaml_key) -> tuple[type, type]`, `main_runner.run_module_main(async_fn, load_env=True) -> None`

- [ ] **Step 1: 创建 `Module/_common/__init__.py`（空文件）**

```bash
mkdir -p Module/_common
touch Module/_common/__init__.py
```

- [ ] **Step 2: 创建 `Module/_common/env.py`**

```python
"""dotenv 统一入口。所有 __main__.py 应通过此函数加载环境变量。"""
import os
from pathlib import Path


def ensure_dotenv() -> None:
    """加载 .env 文件（如果存在）。幂等——重复调用不重复加载。

    查找顺序：项目根目录。使用 load_dotenv(override=False) ——
    已存在的环境变量不被覆盖。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv 不是核心依赖，缺失时静默跳过

    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
```

- [ ] **Step 3: 创建 `Module/_common/registry_helpers.py`**

```python
"""Registry 共享工具：api_key 解析、base_url 解析、logger 注入。"""
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger


def resolve_api_key(
    env_key: str,
    provider_cfg: dict,
    explicit_key: str | None = None,
) -> str:
    """解析 API key：env var > explicit > provider 默认值。"""
    if explicit_key:
        return explicit_key
    return os.getenv(env_key, provider_cfg.get("default_api_key", ""))


def resolve_base_url(
    env_key: str,
    provider_cfg: dict,
    explicit_url: str | None = None,
) -> str:
    """解析 base URL：explicit > env var > provider 默认值。

    注意：explicit_url 用 is not None 检查（而非 truthiness），
    以区分"未传入"和"显式传入空字符串"。
    """
    if explicit_url is not None:
        return explicit_url
    env_val = os.getenv(env_key)
    if env_val is not None:
        return env_val
    return provider_cfg.get("base_url", "")


def inject_logger(
    module_name: str,
    explicit_logger: "Logger | None" = None,
) -> "Logger":
    """返回调用方 logger，允许显式覆盖。"""
    if explicit_logger is not None:
        return explicit_logger
    return logging.getLogger(module_name)
```

- [ ] **Step 4: 创建 `Module/_common/test_helpers.py`**

```python
"""test() 工厂：消除 5 个 backend 中逐字相同的 test() 实现。"""
import time
from collections.abc import Awaitable, Callable


def make_test_method(
    health_fn: Callable[[], Awaitable[bool]],
) -> Callable[[], Awaitable[dict]]:
    """返回标准 test() 协程：计时 + 调用 health_fn + {ok, latency_ms, error}。

    health_fn — 绑定到 self 的异步函数，返回 True 表示健康。
    调用方在 __init__ 末尾使用：
        self.test = make_test_method(self._health_check)
    """
    async def test(self) -> dict:
        t0 = time.perf_counter()
        try:
            ok = await health_fn(self)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": ok, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}

    return test
```

- [ ] **Step 5: 创建 `Module/_common/config_test_helpers.py`**

```python
"""Config 测试工厂：参数化生成 TestDefaults + TestFromConfig 两个 test class。"""
from __future__ import annotations

import tempfile
from typing import Any

import pytest
import yaml

from Module.config import ConfigLoader


def make_config_test_classes(
    ConfigClass: type,
    *,
    defaults: dict[str, Any],
    overrides: dict[str, Any],
    env_key: str = "",
    yaml_key: str = "",
) -> tuple[type, type]:
    """返回 (TestDefaults, TestFromConfig) 两个 test class。

    ConfigClass  — 被测 dataclass 配置类
    defaults     — 期望的默认值字典 {field_name: expected_value}
    overrides    — 用于测试覆盖的字段值 {field_name: override_value}
    env_key      — 环境变量名（用于 env override 测试，空字符串则跳过）
    yaml_key     — YAML key（用于 from_config 测试）
    """

    class TestDefaults:
        """默认值测试。"""

        def test_defaults(self):
            cfg = ConfigLoader(ConfigClass).load()
            for field, expected in defaults.items():
                actual = getattr(cfg, field)
                assert actual == expected, (
                    f"{field}: expected {expected!r}, got {actual!r}"
                )

    class TestFromConfig:
        """YAML + env override 测试。"""

        def test_from_config_passes_params(self):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": overrides}, f)
                f.flush()
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            for field, expected in overrides.items():
                actual = getattr(cfg, field)
                assert actual == expected, (
                    f"{field}: expected {expected!r}, got {actual!r}"
                )

        def test_overrides_take_priority(self):
            """YAML value > dataclass default."""
            val = next(iter(overrides.values()))
            field = next(iter(overrides.keys()))
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": {field: val}}, f)
                f.flush()
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            assert getattr(cfg, field) == val

        @pytest.mark.skipif(not env_key, reason="no env var to test")
        def test_env_override(self, monkeypatch):
            # 局限性：通用工厂不知道哪个字段绑定了 env_key，
            # 因此无法断言具体字段被覆盖为 env 值。
            # 仅验证加载不崩溃 + 得到非 None 的配置对象。
            # 各模块如需真正验证 env→field 绑定，可覆盖此方法。
            val = next(iter(overrides.values()))
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                yaml.dump({yaml_key or "test": {}}, f)
                f.flush()
                monkeypatch.setenv(env_key, str(val))
                cfg = ConfigLoader(
                    ConfigClass,
                    yaml_path=f.name,
                    yaml_key=yaml_key or "test",
                ).load()
            assert cfg is not None

    return TestDefaults, TestFromConfig
```

- [ ] **Step 6: 创建 `Module/_common/main_runner.py`**

```python
"""统一 __main__ 入口：ensure_dotenv + asyncio.run。"""
import asyncio
from collections.abc import Awaitable, Callable

from Module._common.env import ensure_dotenv


def run_module_main(
    async_fn: Callable[[], Awaitable[None]],
    *,
    load_env: bool = True,
) -> None:
    """加载 .env 并运行 async 主函数。

    用法（在 __main__.py 中）：
        from Module._common.main_runner import run_module_main

        async def main():
            ...

        if __name__ == "__main__":
            run_module_main(main)
    """
    if load_env:
        ensure_dotenv()
    asyncio.run(async_fn())
```

- [ ] **Step 7: 验证导入**

```bash
conda run -n Elysia python -c "
from Module._common.env import ensure_dotenv
from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module._common.test_helpers import make_test_method
from Module._common.config_test_helpers import make_config_test_classes
from Module._common.main_runner import run_module_main
print('All imports OK')
"
```

- [ ] **Step 8: Commit**

```bash
git add Module/_common/
git commit -m "feat: add Module/_common/ with env, registry_helpers, test_helpers, config_test_helpers, main_runner"
```

---

### Task 2: 6 个 registry 替换内联代码为共享函数

**Files:**
- Modify: `Module/llm/registry.py:78-89`
- Modify: `Module/stt/registry.py`
- Modify: `Module/tts/registry.py`
- Modify: `Module/embedding/registry.py`
- Modify: `Module/database/vectordb/registry.py`
- Modify: `Module/database/documentdb/registry.py`

**Interfaces:**
- Consumes: `resolve_api_key`, `resolve_base_url`, `inject_logger` (from Task 1)
- Produces: nothing new (factory.create() signatures unchanged)

- [ ] **Step 1: 读取一个 registry 确认当前模式**

llm/registry.py 的关键行（L78-89）：
```python
        cfg = _BACKENDS[provider]
        base_url = kwargs.pop("base_url", cfg["base_url"])
        beta_url = kwargs.pop("beta_url", cfg.get("beta_url", ""))

        env_key = cfg["env_key"]
        api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")

        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()
```

- [ ] **Step 2: 重构 llm/registry.py**

将 `Module/llm/registry.py` L78-89 替换为：
```python
        from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url("", cfg, kwargs.pop("base_url", None))
        beta_url = kwargs.pop("beta_url", cfg.get("beta_url", ""))

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)
```

同时移除不再需要的 import：`import os`（如果不再被其他地方使用）和 `from Module.logger import get_caller_logger`。

- [ ] **Step 3: 重构其他 5 个 registry**

用相同模式改造：stt/registry.py、tts/registry.py、embedding/registry.py、database/vectordb/registry.py、database/documentdb/registry.py。

每个 registry 的关键改动：
1. 添加 `from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger`
2. `kwargs.pop("api_key", "") or os.getenv(env_key, "")` → `resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))`
3. `kwargs.pop("base_url", cfg["base_url"])` → `resolve_base_url("", cfg, kwargs.pop("base_url", None))` （如有 env var 绑定，传 env_key 而非 ""）
4. `get_caller_logger()` → `inject_logger(__name__)`
5. 移除不再需要的 `import os` 和 `from Module.logger import get_caller_logger`

**例外：vectordb/registry.py** 的 L47 有 `token = kwargs.pop("token", "") or os.getenv("MILVUS_TOKEN", "")` — token 不是 api_key 也不是 base_url，不匹配任何共享函数。该 registry 必须保留 `import os`。

- [ ] **Step 4: 运行所有 registry 相关的测试**

```bash
conda run -n Elysia python -m pytest Module/llm/test_llm.py Module/stt/test_stt.py Module/tts/test_tts.py Module/embedding/test_embedding.py Module/database/vectordb/test_vectordb.py Module/database/documentdb/test_documentdb.py -v -x
```

- [ ] **Step 5: Commit**

```bash
git add Module/llm/registry.py Module/stt/registry.py Module/tts/registry.py Module/embedding/registry.py Module/database/vectordb/registry.py Module/database/documentdb/registry.py
git commit -m "refactor: use shared registry_helpers in all 6 registries"
```

---

### Task 3: 5 个 backend test() 方法替换为工厂

**Files:**
- Modify: `Module/llm/openai_compat.py:199-215`
- Modify: `Module/stt/sensevoice.py:127-141`
- Modify: `Module/stt/openai_stt.py:69-87`
- Modify: `Module/tts/gpt_sovits.py:152-164`
- Modify: `Module/tts/openai_tts.py:66-78`

**Interfaces:**
- Consumes: `make_test_method` (from Task 1)
- Produces: nothing new (test() signature unchanged)

- [ ] **Step 1: 读取一个 backend 的当前 test() 和 __init__**

以 `tts/gpt_sovits.py` 为例，test() 在 L152-164：
```python
    async def test(self) -> dict:
        """测试连通性：GET /health。5 秒超时。"""
        import asyncio as _asyncio
        import time as _time
        t0 = _time.perf_counter()
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=httpx.Timeout(5.0)) as client:
                resp = await _asyncio.wait_for(client.get("/health"), timeout=5.0)
                latency_ms = int((_time.perf_counter() - t0) * 1000)
                return {"ok": resp.status_code < 400, "latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((_time.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "error": str(e)}
```

- [ ] **Step 2: 重构 tts/gpt_sovits.py**

在 `__init__` 末尾（`self._base_url` 已设置之后）添加 `_health_check` 并替换 test()：

```python
        # 在 __init__ 末尾添加：
        from Module._common.test_helpers import make_test_method

        async def _health_check(self) -> bool:
            import asyncio as _asyncio
            import httpx
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(5.0)
            ) as client:
                resp = await _asyncio.wait_for(client.get("/health"), timeout=5.0)
                return resp.status_code < 400

        self.test = make_test_method(_health_check)
```

删除原来的 `async def test(self) -> dict:` 方法（L152-164）。

- [ ] **Step 3: 重构其他 4 个 backend**

按同样模式改造：

**stt/sensevoice.py** — `_health_check` 使用现有的 `self._client.get("/health")`：
```python
        async def _health_check(self) -> bool:
            resp = await self._client.get("/health")
            return resp.status_code < 400
```

**stt/openai_stt.py** — `_health_check` 使用现有的 OpenAI client 发最小 transcription：
```python
        async def _health_check(self) -> bool:
            import asyncio as _asyncio
            # 静默 WAV 最小请求
            silence_wav = (
                b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
                b"\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00"
                b"\x02\x00\x10\x00data\x00\x00\x00\x00"
            )
            await _asyncio.wait_for(
                self._client.audio.transcriptions.create(
                    model=self._model,
                    file=("silence.wav", silence_wav, "audio/wav"),
                ),
                timeout=5.0,
            )
            return True
```

**tts/openai_tts.py** — `_health_check` 创建独立 httpx client GET /health：
```python
        async def _health_check(self) -> bool:
            import httpx
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(5.0)
            ) as client:
                resp = await client.get("/health")
                return resp.status_code < 400
```

**llm/openai_compat.py** — `_health_check` 使用现有 OpenAI client 发最小 chat：
```python
        async def _health_check(self) -> bool:
            import asyncio as _asyncio
            await _asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                ),
                timeout=5.0,
            )
            return True
```

每个 backend 的 `__init__` 末尾加 `self.test = make_test_method(_health_check)`，并删除原来的 `async def test(self) -> dict:` 方法。

- [ ] **Step 4: 运行 backend 相关测试**

```bash
conda run -n Elysia python -m pytest Module/llm/test_llm.py Module/stt/test_stt.py Module/tts/test_tts.py -v -x
```

- [ ] **Step 5: Commit**

```bash
git add Module/llm/openai_compat.py Module/stt/sensevoice.py Module/stt/openai_stt.py Module/tts/gpt_sovits.py Module/tts/openai_tts.py
git commit -m "refactor: replace 5 backend test() methods with make_test_method factory"
```

---

### Task 4: 9 个 __main__.py 统一入口

**Files:**
- Modify: `Module/llm/__main__.py`
- Modify: `Module/stt/__main__.py`
- Modify: `Module/tts/__main__.py`
- Modify: `Module/embedding/__main__.py`
- Modify: `Module/output/__main__.py`
- Modify: `Module/config/__main__.py`
- Modify: `Module/prompt/__main__.py`
- Modify: `Module/input/__main__.py`
- Modify: `Module/bench/__main__.py`

**Interfaces:**
- Consumes: `run_module_main` (from Task 1)

- [ ] **Step 1: 重构 6 个 load_dotenv + asyncio.run 的 __main__.py**

改动模式（以 `Module/stt/__main__.py` 为例）：

改前：
```python
"""真实 API 集成测试。运行: python -m Module.stt"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from . import STTFactory
...
if __name__ == "__main__":
    asyncio.run(main())
```

改后：
```python
"""真实 API 集成测试。运行: python -m Module.stt"""

import os
import sys

from Module._common.main_runner import run_module_main
from . import STTFactory
...
if __name__ == "__main__":
    run_module_main(main)
```

对以下 6 个模块做相同改造：llm、stt、embedding、config、prompt、input。

- [ ] **Step 2: 重构 3 个仅 asyncio.run 的 __main__.py**

这 3 个没有 `load_dotenv()` 调用（tts、output、bench）。改动：

改前：
```python
if __name__ == "__main__":
    asyncio.run(main())
```

改后：
```python
from Module._common.main_runner import run_module_main
...
if __name__ == "__main__":
    run_module_main(main, load_env=False)
```

- [ ] **Step 3: 运行 smoke test 确认无回归**

```bash
conda run -n Elysia python -m Module.config 2>&1 | head -5
```

- [ ] **Step 4: Commit**

```bash
git add Module/llm/__main__.py Module/stt/__main__.py Module/tts/__main__.py Module/embedding/__main__.py Module/output/__main__.py Module/config/__main__.py Module/prompt/__main__.py Module/input/__main__.py Module/bench/__main__.py
git commit -m "refactor: use run_module_main in all 9 __main__.py files"
```

---

### Task 5: 6 个 test_config.py 替换为工厂

**Files:**
- Modify: `Module/llm/test_config.py`
- Modify: `Module/stt/test_config.py`
- Modify: `Module/tts/test_config.py`
- Modify: `Module/embedding/test_config.py`
- Modify: `Module/database/vectordb/test_config.py`
- Modify: `Module/database/documentdb/test_config.py`

**Interfaces:**
- Consumes: `make_config_test_classes` (from Task 1)

- [ ] **Step 1: 读取一个 test_config.py 确认当前模式**

`Module/stt/test_config.py` (35 行)：
```python
"""STTConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from Module.config import ConfigLoader
from Module.stt.config import STTConfig
from Module.stt.registry import STTFactory


class TestSTTConfigDefaults:
    def test_defaults(self):
        cfg = ConfigLoader(STTConfig).load()
        assert cfg.base_url == "http://localhost:20042"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("STT_API_URL", "http://192.168.1.18:20042")
        cfg = ConfigLoader(STTConfig).load()
        assert cfg.base_url == "http://192.168.1.18:20042"


class TestSTTFromConfig:
    def test_from_config_passes_params(self):
        cfg = STTConfig(base_url="http://test:20042")
        backend = STTFactory.from_config("sensevoice/sense-voice", cfg)
        assert backend._model == "sense-voice"
        assert str(backend._client.base_url).rstrip("/") == "http://test:20042"

    def test_overrides_take_priority(self):
        cfg = STTConfig(base_url="http://default:20042")
        backend = STTFactory.from_config(
            "sensevoice/custom", cfg, base_url="http://overridden:20042"
        )
        assert backend._model == "custom"
        assert str(backend._client.base_url).rstrip("/") == "http://overridden:20042"
```

- [ ] **Step 2: 重构 Group A — 部分替换（全部 6 个模块）**

全部 6 个 test_config.py 都包含 Factory 集成测试（如 `STTFactory.from_config(...)`），这些测试依赖 backend 实现，`make_config_test_classes` 无法生成。**仅替换 TestDefaults 类**，保留原有的 TestFromConfig。

以 stt/test_config.py 为例，改后：
```python
"""STTConfig 单元测试 — 默认值、env 覆盖、from_config()。"""

from __future__ import annotations

from Module._common.config_test_helpers import make_config_test_classes
from Module.config import ConfigLoader
from Module.stt.config import STTConfig
from Module.stt.registry import STTFactory

# 替换原来的 TestSTTConfigDefaults（工厂生成，取 [0]）
TestSTTConfigDefaults = make_config_test_classes(
    STTConfig,
    defaults={"base_url": "http://localhost:20042"},
    overrides={"base_url": "http://test:20042"},
    env_key="STT_API_URL",
    yaml_key="STT",
)[0]

# 保留原始 TestSTTFromConfig（含 Factory 集成测试）
class TestSTTFromConfig:
    def test_from_config_passes_params(self):
        cfg = STTConfig(base_url="http://test:20042")
        backend = STTFactory.from_config("sensevoice/sense-voice", cfg)
        assert backend._model == "sense-voice"
        assert str(backend._client.base_url).rstrip("/") == "http://test:20042"

    def test_overrides_take_priority(self):
        cfg = STTConfig(base_url="http://default:20042")
        backend = STTFactory.from_config(
            "sensevoice/custom", cfg, base_url="http://overridden:20042"
        )
        assert backend._model == "custom"
        assert str(backend._client.base_url).rstrip("/") == "http://overridden:20042"
```

其他 5 个模块同样模式：`make_config_test_classes(...)[0]` 取 TestDefaults，保留原始 TestFromConfig。

- [ ] **Step 3: 按上表参数重构其他 5 个 test_config.py**

每个模块的参数（defaults 取 ConfigClass 声明的所有字段默认值，overrides 取不同值）：

| 模块 | defaults | overrides | env_key | yaml_key |
|------|----------|-----------|---------|----------|
| llm | `{"model": "deepseek/deepseek-chat", "base_url": "https://api.deepseek.com", "api_key": ""}` | `{"model": "custom/model", "base_url": "http://test:8080", "api_key": "test-key"}` | `"LLM_API_BASE"` | `"LLM"` |
| tts | `{"model": "gpt-sovits/default", "base_url": "http://localhost:9880", "api_key": "", "voices_config": ""}` | `{"model": "openai/tts-1", "base_url": "http://test:9880", "api_key": "sk-test", "voices_config": "/tmp/voices.yaml"}` | `"TTS_API_URL"` | `"TTS"` |
| embedding | `{"base_url": "https://api.openai.com/v1"}` | `{"base_url": "http://test:8080/v1"}` | `"OPENAI_API_BASE"` | `"Embedding"` |
| vectordb | `{"base_url": "http://localhost:19530"}` | `{"base_url": "http://test:19530"}` | `"MILVUS_URI"` | `"VectorDB"` |
| documentdb | `{"base_url": "mongodb://localhost:27017"}` | `{"base_url": "mongodb://test:27017"}` | `"MONGODB_URI"` | `"DocumentDB"` |

- [ ] **Step 4: 运行所有 config 测试**

```bash
conda run -n Elysia python -m pytest Module/llm/test_config.py Module/stt/test_config.py Module/tts/test_config.py Module/embedding/test_config.py Module/database/vectordb/test_config.py Module/database/documentdb/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add Module/llm/test_config.py Module/stt/test_config.py Module/tts/test_config.py Module/embedding/test_config.py Module/database/vectordb/test_config.py Module/database/documentdb/test_config.py
git commit -m "refactor: use make_config_test_classes in all 6 test_config.py files"
```

---

### Task 6: config/ 内部去重 — 删除 validator.py 中的重复函数

**Files:**
- Modify: `Module/config/validator.py:147-158`

**Interfaces:**
- Consumes: `_safe_get_type_hints`, `_is_dataclass_instance` (from loader.py)

- [ ] **Step 1: 从 validator.py 删除重复函数，改为 import**

删除 `Module/config/validator.py` L147-158：
```python
    @staticmethod
    def _is_dataclass_instance(obj: Any) -> bool:
        return dataclasses.is_dataclass(obj) and not isinstance(obj, type)

    @staticmethod
    def _safe_get_type_hints(target_type: type) -> dict[str, type]:
        """Return resolved type hints for *target_type*, or empty dict on failure."""
        import typing
        try:
            return typing.get_type_hints(target_type)
        except Exception:
            return {}
```

在文件顶部添加 import：
```python
from Module.config.loader import _is_dataclass_instance, _safe_get_type_hints
```

在 `Validator` 类中将原 `@staticmethod` 调用改为引用导入的函数：
- `Validator._safe_get_type_hints(...)` → `_safe_get_type_hints(...)`
- `Validator._is_dataclass_instance(...)` → `_is_dataclass_instance(...)`

或保留为类方法但委托到导入的函数：
```python
    _is_dataclass_instance = staticmethod(_is_dataclass_instance)
    _safe_get_type_hints = staticmethod(_safe_get_type_hints)
```

- [ ] **Step 2: 运行 config 测试确认无回归**

```bash
conda run -n Elysia python -m pytest Module/config/test_config.py -v
```

- [ ] **Step 3: Commit**

```bash
git add Module/config/validator.py
git commit -m "refactor: deduplicate _safe_get_type_hints and _is_dataclass_instance in config/"
```

---

### Task 7: _common/ 自身的单元测试

**Files:**
- Create: `Module/_common/tests/__init__.py`
- Create: `Module/_common/tests/test_registry_helpers.py`
- Create: `Module/_common/tests/test_test_helpers.py`
- Create: `Module/_common/tests/test_config_test_helpers.py`
- Create: `Module/_common/tests/test_main_runner.py`

- [ ] **Step 1: 创建 `Module/_common/tests/__init__.py`**

```bash
mkdir -p Module/_common/tests
touch Module/_common/tests/__init__.py
```

- [ ] **Step 2: 创建 `Module/_common/tests/test_registry_helpers.py`**

```python
"""resolve_api_key / resolve_base_url / inject_logger 单元测试。"""
import logging
import os

import pytest

from Module._common.registry_helpers import (
    inject_logger,
    resolve_api_key,
    resolve_base_url,
)


class TestResolveApiKey:
    def test_explicit_key_wins(self):
        assert resolve_api_key("ENV_KEY", {}, explicit_key="explicit") == "explicit"
        assert resolve_api_key("ENV_KEY", {"default_api_key": "cfg"}, explicit_key="explicit") == "explicit"

    def test_env_var_second(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "from-env")
        assert resolve_api_key("TEST_KEY", {"default_api_key": "from-cfg"}) == "from-env"

    def test_falls_back_to_default(self):
        assert resolve_api_key("MISSING_KEY", {"default_api_key": "from-cfg"}) == "from-cfg"

    def test_returns_empty_when_nothing_set(self):
        assert resolve_api_key("MISSING_KEY", {}) == ""


class TestResolveBaseUrl:
    def test_explicit_url_wins(self):
        assert resolve_base_url("ENV", {"base_url": "cfg"}, explicit_url="http://explicit") == "http://explicit"

    def test_explicit_none_skips(self):
        assert resolve_base_url("ENV", {"base_url": "http://cfg"}) == "http://cfg"

    def test_env_var_second(self, monkeypatch):
        monkeypatch.setenv("TEST_URL", "http://env")
        assert resolve_base_url("TEST_URL", {"base_url": "http://cfg"}) == "http://env"

    def test_falls_back_to_default(self):
        assert resolve_base_url("MISSING", {"base_url": "http://cfg"}) == "http://cfg"

    def test_returns_empty_when_nothing_set(self):
        assert resolve_base_url("MISSING", {}) == ""


class TestInjectLogger:
    def test_uses_explicit_logger(self):
        explicit = logging.getLogger("explicit")
        result = inject_logger("module", explicit_logger=explicit)
        assert result is explicit

    def test_creates_logger_when_none(self):
        result = inject_logger("my_module")
        assert isinstance(result, logging.Logger)
        assert result.name == "my_module"
```

- [ ] **Step 3: 创建 `Module/_common/tests/test_test_helpers.py`**

```python
"""make_test_method 单元测试。"""
import pytest

from Module._common.test_helpers import make_test_method


class TestMakeTestMethod:
    @pytest.mark.asyncio
    async def test_ok_returns_latency(self):
        async def ok_check(self):
            return True

        test_fn = make_test_method(ok_check)
        # test_fn is async def test(self), bind self=None for unit test
        result = await test_fn(None)
        assert result["ok"] is True
        assert "latency_ms" in result
        assert isinstance(result["latency_ms"], int)
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unhealthy_returns_false(self):
        async def fail_check(self):
            return False

        test_fn = make_test_method(fail_check)
        result = await test_fn(None)
        assert result["ok"] is False
        assert "latency_ms" in result

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        async def boom(self):
            raise RuntimeError("connection refused")

        test_fn = make_test_method(boom)
        result = await test_fn(None)
        assert result["ok"] is False
        assert result["error"] == "connection refused"
        assert "latency_ms" in result
```

- [ ] **Step 4: 创建 `Module/_common/tests/test_config_test_helpers.py`**

```python
"""make_config_test_classes 单元测试。"""
from dataclasses import dataclass

from Module.config import ConfigField

from Module._common.config_test_helpers import make_config_test_classes


@dataclass
class FakeConfig:
    host: str = ConfigField(default="localhost")
    port: int = ConfigField(default=8080)


TestDefaults, TestFromConfig = make_config_test_classes(
    FakeConfig,
    defaults={"host": "localhost", "port": 8080},
    overrides={"host": "override-host", "port": 9090},
    env_key="",
    yaml_key="Fake",
)


class TestGeneratedDefaults(TestDefaults):
    """继承工厂生成的 TestDefaults，pytest 可发现。"""
    pass


class TestGeneratedFromConfig(TestFromConfig):
    """继承工厂生成的 TestFromConfig。"""
    pass
```

- [ ] **Step 5: 创建 `Module/_common/tests/test_main_runner.py`**

```python
"""run_module_main 单元测试。"""
import pytest

from Module._common.main_runner import run_module_main


class TestRunModuleMain:
    @pytest.mark.asyncio
    async def test_runs_async_fn(self):
        called = []

        async def my_main():
            called.append(True)

        run_module_main(my_main)
        assert called == [True]
```

- [ ] **Step 6: 运行 _common/ 测试**

```bash
conda run -n Elysia python -m pytest Module/_common/tests/ -v
```
Expected: all tests PASS (~10 tests)

- [ ] **Step 7: Commit**

```bash
git add Module/_common/tests/
git commit -m "test: add unit tests for Module/_common/ shared utilities"
```

---

### Task 8: 全量回归测试

- [ ] **Step 1: 运行 Module/ 全部测试**

```bash
conda run -n Elysia python -m pytest Module/ -v --ignore=Module/task_system -x
```
Expected: all tests pass, no regressions

- [ ] **Step 2: 运行 core/ 测试确认无连锁影响**

```bash
conda run -n Elysia python -m pytest core/tests/ -v -x
```
Expected: all tests pass (1 pre-existing failure unrelated)

- [ ] **Step 3: 更新 todo.md**

确认 todo.md 中与此重构相关的项已标记或移除。

- [ ] **Step 4: Commit**

```bash
git add todo.md
git commit -m "docs: mark Module/_common/ refactor as complete"
```
