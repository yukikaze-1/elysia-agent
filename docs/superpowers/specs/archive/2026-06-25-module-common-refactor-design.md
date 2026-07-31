# Module/_common/ 轻量共享层 — 设计文档

日期: 2026-06-25
状态: 已确认
范围: Module/ 全部子模块（除 task_system）

---

## 概述

对 Module/ 的 167 个 `.py` 文件全面扫描后发现：真正的设计错误很少，但**重复代码是系统性问题**。

核心问题：
- 6 个 registry 的 `.create()` 中有相同的 api_key/base_url 解析逻辑
- 5 个 backend 的 `test()` 方法逐字复制粘贴
- 6 个 `test_config.py` 结构完全一致
- 9 个 `__main__.py` 重复 `load_dotenv()` + `asyncio.run()` 样板
- `config/loader.py` 和 `config/validator.py` 中 `_safe_get_type_hints()` / `_is_dataclass_instance()` 逐字重复

方案：在 `Module/_common/` 下提供轻量工具函数，各模块调用替换内联代码。不引入类继承，不拆大文件，不改 backend 实现逻辑。

---

## 不变量

- 不做的事：拆大文件、引入 BaseRegistry 类继承、改 backend 实现、动 task_system
- 所有对外 API 签名不变
- 所有现有测试必须继续通过
- `_common/` 不暴露为公开 API（`Module/__init__.py` 不导出）

---

## Module/_common/ 布局

```
Module/_common/
├── __init__.py              # 空文件，不暴露顶层 API
├── env.py                   # dotenv 统一入口
├── registry_helpers.py      # api_key / base_url 解析 + logger 注入
├── test_helpers.py          # make_test_method() 工厂
├── config_test_helpers.py   # make_config_test_classes() 工厂
└── main_runner.py           # run_module_main() 统一 __main__ 入口
```

---

### 1. `_common/env.py` — dotenv 统一入口

收口 `Module/config/loader.py:EnvManager.ensure_dotenv()` 的逻辑。当前 9 个 `__main__.py` 中有 6 个各自调用 `from dotenv import load_dotenv; load_dotenv()`，而 `config/loader.py` 已有集中实现但无人使用。

```python
"""dotenv 统一入口。所有 __main__.py 应通过此函数加载环境变量。"""
import os
from pathlib import Path

def ensure_dotenv() -> None:
    """加载 .env 文件（如果存在）。幂等——重复调用不重复加载。

    查找顺序：当前目录 → 父目录 → 项目根目录。
    使用 load_dotenv(override=False) —— 已存在的环境变量不被覆盖。
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

---

### 2. `_common/registry_helpers.py` — Registry 共享工具

消除 6 个 registry 中重复的 api_key / base_url 解析 + logger 注入逻辑。

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

    注意：显式传入的空字符串被视为有效值（不是未指定），与 os.getenv 行为一致。
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
    """返回调用方 logger，允许显式覆盖。

    调用示例（在 registry.create() 中）：
        logger = inject_logger(__name__, logger)
    """
    if explicit_logger is not None:
        return explicit_logger
    return logging.getLogger(module_name)
```

**各 registry 改动示例（以 LLM 为例）：**

改前（`Module/llm/registry.py`）：
```python
api_key = explicit_api_key or os.getenv(env_key, info.get("default_api_key", ""))
base_url = explicit_base_url or os.getenv(env_url, info.get("base_url", ""))
if logger is None:
    logger = logging.getLogger(__name__)
```

改后：
```python
from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger

api_key = resolve_api_key(env_key, info, explicit_api_key)
base_url = resolve_base_url(env_url, info, explicit_base_url)
logger = inject_logger(__name__, logger)
```

---

### 3. `_common/test_helpers.py` — test() 工厂

消除 5 个 backend 中逐字相同的 `test()` 方法（llm/openai_compat、stt/sensevoice、stt/openai_stt、tts/gpt_sovits、tts/openai_tts）。

所有 5 个 backend 的实际 `test()` 都使用特定于客户端的调用（现有 httpx 客户端、现有 OpenAI 客户端，或新创建的 httpx 客户端）——没有一个从 URL 字符串构造简单的 GET。统一使用 `health_fn` 参数，赋值在 `__init__` 内（此时 `self._client` 或 `self._base_url` 已可用）。

```python
"""test() 工厂：消除 5 个 backend 中逐字相同的 test() 实现。"""
import time
from collections.abc import Awaitable, Callable


def make_test_method(
    health_fn: Callable[[], Awaitable[bool]],
) -> Callable[[], Awaitable[dict]]:
    """返回标准 test() 协程：计时 + 调用 health_fn + {ok, latency_ms, error}。

    health_fn — 异步函数，返回 True 表示健康。
    调用方在 __init__ 中将 health_fn 绑定到 self 之后调用：
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

**各 backend 改动模式：**

在 `__init__` 末尾添加 `_health_check` 方法 + `self.test = make_test_method(self._health_check)`。

| Backend | `_health_check` 实现 |
|---------|---------------------|
| `stt/sensevoice.py` | `await self._client.get("/health")` → `resp.status_code < 400` |
| `stt/openai_stt.py` | 发最小 transcription 请求，不抛异常即健康 |
| `tts/gpt_sovits.py` | `httpx.AsyncClient(base_url=self._base_url).get("/health")` → `resp.status_code < 400` |
| `tts/openai_tts.py` | `httpx.AsyncClient(base_url=self._base_url).get("/health")` → `resp.status_code < 400` |
| `llm/openai_compat.py` | 发最小 chat completion 请求 (max_tokens=1)，不抛异常即健康 |

每个 backend 删除原来的内联 `test()` 方法（13-19 行），在 `__init__` 末尾加 ~3 行 `_health_check` + 1 行 `make_test_method` 调用。

---

### 4. `_common/config_test_helpers.py` — Config 测试工厂

消除 6 个 `test_config.py` 的重复结构（llm/stt/tts/embedding/vectordb/documentdb）。

每个 `test_config.py` 都有 `TestDefaults` 和 `TestFromConfig` 两个 class，包含相同的 4 个测试方法名。

```python
"""Config 测试工厂：参数化生成 TestDefaults + TestFromConfig 两个 test class。"""
from typing import Any


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
    env_key      — 环境变量名（用于 env override 测试）
    yaml_key     — YAML key（用于 from_config 测试）
    """
    import os
    import tempfile
    import pytest

    class TestDefaults:
        """默认值测试。"""

        def test_defaults(self):
            cfg = ConfigClass()
            for field, expected in defaults.items():
                actual = getattr(cfg, field)
                assert actual == expected, f"{field}: expected {expected!r}, got {actual!r}"

    class TestFromConfig:
        """YAML + env override 测试。"""

        def test_from_config_passes_params(self):
            import yaml
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                yaml.dump({yaml_key or "test": overrides}, f)
                f.flush()
                from Module.config import ConfigLoader
                cfg = ConfigLoader(ConfigClass, yaml_path=f.name, yaml_key=yaml_key or "test").load()

            for field, expected in overrides.items():
                actual = getattr(cfg, field)
                assert actual == expected, f"{field}: expected {expected!r}, got {actual!r}"

        def test_overrides_take_priority(self):
            """YAML value > dataclass default."""
            import yaml
            val = next(iter(overrides.values()))
            field = next(iter(overrides.keys()))
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                yaml.dump({yaml_key or "test": {field: val}}, f)
                f.flush()
                from Module.config import ConfigLoader
                cfg = ConfigLoader(ConfigClass, yaml_path=f.name, yaml_key=yaml_key or "test").load()
            assert getattr(cfg, field) == val

        @pytest.mark.skipif(not env_key, reason="no env var to test")
        def test_env_override(self):
            val = next(iter(overrides.values()))
            field = next(iter(overrides.keys()))
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                yaml.dump({yaml_key or "test": {}}, f)
                f.flush()
                import os as _os
                _os.environ[env_key] = str(val)
                try:
                    from Module.config import ConfigLoader
                    cfg = ConfigLoader(ConfigClass, yaml_path=f.name, yaml_key=yaml_key or "test").load()
                    # env var 绑定的字段应取 env 值
                    # 注意：env 绑定通过 ConfigField(env=...) 声明，不是所有字段都走 env
                finally:
                    del _os.environ[env_key]

    return TestDefaults, TestFromConfig
```

---

### 5. `_common/main_runner.py` — 统一 __main__ 入口

替代 9 个 `__main__.py` 的样板代码：6 个使用 `load_dotenv()` + `asyncio.run()`（llm、stt、embedding、config、prompt、input），3 个仅使用 `asyncio.run()` 无 load_dotenv（bench、tts、output）。

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

**改动前后对比（以 STT `__main__.py` 为例）：**

改前：
```python
import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()

async def main():
    ...

if __name__ == "__main__":
    asyncio.run(main())
```

改后：
```python
import sys
from Module._common.main_runner import run_module_main

async def main():
    ...

if __name__ == "__main__":
    run_module_main(main)
```

---

### 6. `config/` 内去重

`Module/config/loader.py` 和 `Module/config/validator.py` 中 `_safe_get_type_hints()` 和 `_is_dataclass_instance()` 逐字重复。

**修复：** 从 `validator.py` 删除这两个函数，改为从 `loader.py` import。

```python
# validator.py 中：
from Module.config.loader import _safe_get_type_hints, _is_dataclass_instance
```

---

## 影响范围汇总

| 类别 | 新增 | 修改 |
|------|------|------|
| `_common/` 新建 | 6 个文件 | — |
| Registry (`registry.py`) | — | 6 个（llm/stt/tts/embedding/vectordb/documentdb） |
| Backend (`test()`) | — | 5 个（llm/stt×2/tts×2） |
| Config test (`test_config.py`) | — | 6 个（llm/stt/tts/embedding/vectordb/documentdb） |
| Smoke test (`__main__.py`) | — | 9 个（llm/stt/tts/embedding/output/config/prompt/input/bench） |
| `config/` 去重 | — | 2 个（loader.py + validator.py） |

`__main__.py` 改造覆盖 9 个模块：6 个使用 `load_dotenv` + `asyncio.run`（llm/stt/embedding/config/prompt/input），3 个仅使用 `asyncio.run`（tts/output/bench）。`logger/__main__.py` 不改造——它是同步代码，不匹配 `run_module_main(async_fn)` 模式。

不改内部结构的模块：config（infra）/ logger（utility）/ prompt（utility）/ schema（data）/ bench（tool）/ tool（plugin）/ input（server）/ graphdb（stub）——仅其 `__main__.py` 和 `test_config.py` 受机械性影响。

---

## 测试策略

- 所有现有测试必须在改动后继续通过
- `_common/` 每个新文件配对应的 `Module/_common/tests/test_*.py`
- `test_helpers.py` 的 `make_test_method` 用 mock `health_fn` 验证 {ok, latency_ms} 格式（成功和异常两条路径）
- `config_test_helpers.py` 的 `make_config_test_classes` 用一个假 dataclass 验证返回的 test class 能正常执行
- `registry_helpers.py` 的纯函数用参数化测试验证优先级顺序
