# Pre-flight 环境预检 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 `app.py --check` 环境预检，首次部署时验证 Python 版本、.env 配置、pip 依赖、API key、连通性。

**Architecture:** `Module/config/preflight.py` 作为独立模块，`run(config: AppConfig, full: bool) -> bool` 入口函数。`app.py` 在 `ConfigLoader.load()` 后调用。检查结果分 FAIL（exit 1）和 WARN（打印但不阻止）两级；`--full` 追加连通性探活。

**Tech Stack:** Python stdlib + PyYAML + 各 backend Factory（LLMFactory/TTSFactory/STTFactory）+ backend.test()

## Global Constraints

- `--check` 始终 exit，不启动应用
- pip 包缺失 → FAIL（exit 1 并列出缺失包名）
- LLM/TTS/STT provider 检查均为 provider-aware（从 config 解析 provider 后查表）
- 连通性探活用各 backend 的 `test()` 方法，验证 API key 有效性 + 服务功能，不只看 HTTP 可达
- 移除 `.env.example` 中废弃的 `DEEPSEEK_API_BASE`、`DEEPSEEK_API_BETA`

---

### Task 1: Create `Module/config/preflight.py`

**Files:**
- Create: `Module/config/preflight.py`
- Test: `Module/config/test_preflight.py`

**Interfaces:**
- Produces: `run(config: AppConfig, full: bool = False) -> bool`

The module contains the `run()` entry point and private helpers for each check category. Result tracking uses a simple class `CheckResult` with `pass_count`, `warn_count`, `fail_count`.

- [ ] **Step 1: Create `Module/config/preflight.py`**

```python
"""环境预检 — app.py --check 调用。验证 Python 版本、.env、pip 依赖、API key、连通性。"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class _Counts:
    passed: int = 0
    warned: int = 0
    failed: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0


_COUNTS = _Counts()


# ---- provider 映射表 ----

_LLM_PROVIDER_ENV: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "custom": "CUSTOM_LLM_API_KEY",
    "mimo": "MIMO_API_KEY",
    "gemini": "GEMINI_API_KEY",
    # ollama 无需 API key
}

_TTS_PROVIDER_ENV: dict[str, str] = {
    "minimax": "MINIMAX_API_KEY",
    "gpt-sovits": "GPT_SOVITS_URL",
    "custom": "CUSTOM_TTS_API_KEY",
}

_STT_PROVIDER_ENV: dict[str, str] = {
    "sensevoice": "STT_API_URL",
    "custom": "CUSTOM_STT_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
}

# 暂未实现的 STT provider
_STT_STUB_PROVIDERS = {"openai", "deepgram"}

_CRITICAL_PACKAGES = ["aiohttp", "httpx", "openai", "pydantic", "jinja2", "yaml", "dotenv"]


# ---- helpers ----

def _parse_provider(model_id: str) -> str:
    """从 'provider/model' 解析 provider 部分。"""
    return model_id.split("/")[0] if "/" in model_id else model_id


def _fail(msg: str) -> None:
    _COUNTS.failed += 1
    print(f"  [FAIL] {msg}")


def _warn(msg: str) -> None:
    _COUNTS.warned += 1
    print(f"  [WARN] {msg}")


def _pass(msg: str) -> None:
    _COUNTS.passed += 1
    print(f"  [PASS] {msg}")


# ---- 检查函数 ----

def _check_python() -> None:
    """检查 Python >= 3.11。"""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 11):
        _pass(f"Python {ver}")
    else:
        _fail(f"Python {ver} — 需要 >= 3.11")


def _check_dotenv() -> None:
    """检查 .env 是否存在。"""
    if Path(".env").exists():
        _pass(".env 已配置")
    else:
        _fail(".env 不存在 — 请先运行: cp .env.example .env，然后编辑填入 API key")


def _check_llm_env(config: Any) -> None:
    """根据 LLM provider 检查对应 env var。"""
    model_id = config.llm.model
    provider = _parse_provider(model_id)
    if provider == "ollama":
        _pass(f"LLM env ok (ollama 无需 API key)")
        return
    key = _LLM_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 LLM provider: {provider}")
        return
    val = os.getenv(key, "")
    if val:
        _pass(f"LLM env ok ({key} 已设置)")
    else:
        _fail(f"{key} 未设置 — LLM provider '{provider}' 需要此环境变量")


def _check_pip_deps() -> None:
    """检查关键 pip 包是否可导入。"""
    missing: list[str] = []
    for pkg in _CRITICAL_PACKAGES:
        # dotenv 用 import 名与包名一致，但 yaml 对应 PyYAML
        import_name = "yaml" if pkg == "yaml" else pkg
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        _fail(f"缺失 pip 包: {', '.join(missing)}")
    else:
        _pass(f"pip 依赖完整 ({len(_CRITICAL_PACKAGES)}/{len(_CRITICAL_PACKAGES)})")


def _check_character_file(config: Any) -> None:
    """检查 character 配置文件是否存在。"""
    path = Path(config.agent.character)
    if path.exists():
        _pass(f"角色配置存在 ({config.agent.character})")
    else:
        _fail(f"角色配置文件不存在: {config.agent.character}")


def _check_tts_env(config: Any) -> None:
    """根据 TTS provider 检查对应 env var (WARN 级别)。"""
    if not config.tts.enabled:
        _pass("TTS 未启用，跳过 env 检查")
        return
    provider = _parse_provider(config.tts.model)
    key = _TTS_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 TTS provider: {provider}")
        return
    val = os.getenv(key, "")
    if val:
        _pass(f"TTS env ok ({key} 已设置)")
    else:
        _warn(f"{key} 未设置 — TTS ('{provider}') 将不可用")


def _check_stt_env(config: Any) -> None:
    """根据 STT provider 检查对应 env var (WARN 级别)。"""
    if not config.stt.enabled:
        _pass("STT 未启用，跳过 env 检查")
        return
    provider = _parse_provider(config.stt.model)
    key = _STT_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 STT provider: {provider}")
        return
    if provider in _STT_STUB_PROVIDERS:
        _warn(f"STT provider '{provider}' 暂未实现（factory 会抛出 NotImplementedError）")
        return
    val = os.getenv(key, "")
    if val:
        _pass(f"STT env ok ({key} 已设置)")
    else:
        _warn(f"{key} 未设置 — STT ('{provider}') 将不可用")


def _check_writable_dirs() -> None:
    """检查 log/、data/ 目录是否可写。"""
    for d in ("log", "data"):
        try:
            Path(d).mkdir(exist_ok=True)
            test_file = Path(d) / ".preflight_write_test"
            test_file.touch()
            test_file.unlink()
            _pass(f"目录可写: {d}/")
        except OSError:
            _warn(f"目录不可写: {d}/")


# ---- 连通性探活 (--full) ----
# 使用各 backend 的 test() 方法，实际验证 API key 有效性 + 服务功能。
# LLM: 发送 "hi" + max_tokens=1 的真实请求（~1 token）。
# TTS: MiniMax → POST /v1/get_voice；GPT-SoVits → GET /health。
# STT: SenseVoice → GET /health。


async def _check_llm_connectivity(config: Any) -> None:
    """LLM 连通性 — 真实对话请求验证 API key。"""
    provider = _parse_provider(config.llm.model)
    if provider == "ollama":
        _pass("LLM ollama 为本地服务，跳过连通性检查")
        return
    try:
        from Module.llm import LLMFactory
        backend = LLMFactory.create(config.llm.model)
        result = await backend.test()
        await backend.close()
        if result.get("ok"):
            _pass(f"LLM 连通 ok ({config.llm.model}) — {result.get('latency_ms', 0)}ms")
        else:
            _warn(f"LLM test() 失败 ({config.llm.model}): {result.get('error', 'unknown')}")
    except Exception as e:
        _warn(f"LLM 连通异常 ({config.llm.model}): {e}")


async def _check_tts_connectivity(config: Any) -> None:
    """TTS 连通性 — backend.test() 验证服务可用。"""
    if not config.tts.enabled:
        _pass("TTS 未启用，跳过连通性检查")
        return
    try:
        from Module.tts import TTSFactory
        backend = TTSFactory.create(config.tts.model)
        result = await backend.test()
        await backend.close()
        if result.get("ok"):
            _pass(f"TTS 连通 ok ({config.tts.model}) — {result.get('latency_ms', 0)}ms")
        else:
            _warn(f"TTS test() 失败 ({config.tts.model}): {result.get('error', 'unknown')}")
    except Exception as e:
        _warn(f"TTS 连通异常 ({config.tts.model}): {e}")


async def _check_stt_connectivity(config: Any) -> None:
    """STT 连通性 — backend.test() 验证服务可用。"""
    if not config.stt.enabled:
        _pass("STT 未启用，跳过连通性检查")
        return
    provider = _parse_provider(config.stt.model)
    if provider in _STT_STUB_PROVIDERS:
        _pass(f"STT '{provider}' 暂未实现，跳过连通性检查")
        return
    try:
        from Module.stt import STTFactory
        backend = STTFactory.create(config.stt.model)
        result = await backend.test()
        await backend.close()
        if result.get("ok"):
            _pass(f"STT 连通 ok ({config.stt.model}) — {result.get('latency_ms', 0)}ms")
        else:
            _warn(f"STT test() 失败 ({config.stt.model}): {result.get('error', 'unknown')}")
    except Exception as e:
        _warn(f"STT 连通异常 ({config.stt.model}): {e}")


# ---- 入口 ----

def run(config: Any, full: bool = False) -> bool:
    """执行环境预检。返回 True 表示通过（无 FAIL）。"""
    global _COUNTS
    _COUNTS = _Counts()

    print("\n=== Elysia Agent 环境预检 ===")
    if full:
        print("（含连通性探活）")
    print()

    # Phase 1: 必须通过
    _check_python()
    _check_dotenv()
    _check_llm_env(config)
    _check_pip_deps()
    _check_character_file(config)

    # Phase 2: WARN
    _check_tts_env(config)
    _check_stt_env(config)
    _check_writable_dirs()

    # Phase 3: 连通性 (-full 才执行)
    if full:
        import asyncio
        asyncio.run(_check_llm_connectivity(config))
        asyncio.run(_check_tts_connectivity(config))
        asyncio.run(_check_stt_connectivity(config))

    # 摘要
    print()
    total = _COUNTS.passed + _COUNTS.warned + _COUNTS.failed
    status = "通过" if _COUNTS.ok else "存在失败项"
    print(
        f"结果: {_COUNTS.passed}/{total} passed, "
        f"{_COUNTS.warned} warned, {_COUNTS.failed} failed — {status}"
    )
    return _COUNTS.ok
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
cd /home/yomu/Agent && python -c "from Module.config.preflight import run; print('import ok')"
```

- [ ] **Step 3: Commit**

```bash
git add Module/config/preflight.py
git commit -m "feat: add preflight environment check module"
```

---

### Task 2: Integrate `--check` and `--full` into `app.py`

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `--check` and `--full` arguments**

In `app.py`, after line 125 (`--verbose`), add:

```python
parser.add_argument("--check", action="store_true",
                    help="环境预检（通过后退出，不启动应用）")
parser.add_argument("--full", action="store_true",
                    help="预检包含连通性测试（需配合 --check）")
```

- [ ] **Step 2: Add preflight call after CLI overrides**

In `app.py`, after line 135 (`config.agent.character = args.character`), before line 137 (`if args.server:`), add:

```python
if args.check:
    from Module.config.preflight import run as preflight
    ok = preflight(config, full=args.full)
    sys.exit(0 if ok else 1)
```

- [ ] **Step 3: Verify syntax**

```bash
cd /home/yomu/Agent && python -c "
import ast
with open('app.py') as f:
    ast.parse(f.read())
print('syntax ok')
"
```

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: add --check/--full flags for preflight environment check"
```

---

### Task 3: Remove deprecated env vars from `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Remove `DEEPSEEK_API_BASE` and `DEEPSEEK_API_BETA`**

Remove lines 13-14:

```
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_API_BETA=https://api.deepseek.com/beta
```

These are never read via `os.getenv` — the defaults are managed by `Module/config/providers.yaml`.

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore: remove deprecated DEEPSEEK_API_BASE/BETA from .env.example"
```

---

### Task 4: Add `check` and `check-full` targets to `Makefile`

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add targets**

After the `test-quick` target, add:

```makefile
check:
	conda run -n $(CONDA_ENV) python app.py --check

check-full:
	conda run -n $(CONDA_ENV) python app.py --check --full
```

- [ ] **Step 2: Verify `make check` runs**

```bash
cd /home/yomu/Agent && make check
```

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat: add make check / check-full targets for preflight"
```

---

### Task 5: Update README quick start

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update step 2 (配置)**

Replace the existing step 2:

```
### 2. 配置

cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY
```

With:

```
### 2. 配置

cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY

# 环境预检
python app.py --check
# 或 make check
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add preflight check step to quick start"
```

---

### Task 6: Write unit tests

**Files:**
- Create: `Module/config/test_preflight.py`

- [ ] **Step 1: Write tests**

```python
"""preflight 模块单元测试。不依赖真实 API/服务。"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# 重新导入以获取干净的 _COUNTS
def _fresh_module():
    """在干净状态下重新导入 preflight。"""
    import importlib
    import Module.config.preflight
    importlib.reload(Module.config.preflight)
    return Module.config.preflight


@pytest.fixture
def mock_config():
    """构造一个最小 AppConfig。"""
    cfg = MagicMock()
    cfg.llm.model = "deepseek/deepseek-chat"
    cfg.tts.enabled = False
    cfg.stt.enabled = False
    cfg.agent.character = "character/elysia.yaml"
    return cfg


class TestCheckPython:
    def test_py311_or_later_passes(self, capsys):
        if sys.version_info < (3, 11):
            pytest.skip("test requires Python >= 3.11")
        from Module.config.preflight import _check_python, _Counts, _COUNTS
        _COUNTS.__init__()  # reset
        _check_python()
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert _COUNTS.failed == 0

    def test_py310_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "version_info", (3, 10, 0))
        from Module.config.preflight import _check_python, _COUNTS
        _COUNTS.__init__()
        _check_python()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "3.11" in captured.out
        assert _COUNTS.failed == 1


class TestCheckDotenv:
    def test_dotenv_exists_passes(self, tmp_path, monkeypatch, capsys):
        dotenv = tmp_path / ".env"
        dotenv.write_text("DEEPSEEK_API_KEY=test")
        monkeypatch.chdir(tmp_path)
        from Module.config.preflight import _check_dotenv, _COUNTS
        _COUNTS.__init__()
        _check_dotenv()
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_dotenv_missing_fails(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        from Module.config.preflight import _check_dotenv, _COUNTS
        _COUNTS.__init__()
        _check_dotenv()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "cp .env.example .env" in captured.out
        assert _COUNTS.failed == 1


class TestCheckLLMEnv:
    def test_deepseek_key_missing_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        from Module.config.preflight import _check_llm_env, _COUNTS
        _COUNTS.__init__()
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        _check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "DEEPSEEK_API_KEY" in captured.out

    def test_deepseek_key_present_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from Module.config.preflight import _check_llm_env, _COUNTS
        _COUNTS.__init__()
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        _check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_ollama_skips(self, capsys):
        from Module.config.preflight import _check_llm_env, _COUNTS
        _COUNTS.__init__()
        cfg = MagicMock()
        cfg.llm.model = "ollama/llama3"
        _check_llm_env(cfg)
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "ollama" in captured.out


class TestCheckPipDeps:
    def test_all_present_passes(self, capsys):
        from Module.config.preflight import _check_pip_deps, _COUNTS
        _COUNTS.__init__()
        _check_pip_deps()
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_missing_reported(self, monkeypatch, capsys):
        import importlib
        orig = importlib.import_module
        def fake(name):
            if name == "aiohttp":
                raise ImportError("no aiohttp")
            return orig(name)
        monkeypatch.setattr(importlib, "import_module", fake)
        from Module.config.preflight import _check_pip_deps, _COUNTS
        _COUNTS.__init__()
        _check_pip_deps()
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "aiohttp" in captured.out
        assert _COUNTS.failed == 1


class TestCheckTTSEnv:
    def test_tts_disabled_skips(self, capsys):
        from Module.config.preflight import _check_tts_env, _COUNTS
        _COUNTS.__init__()
        cfg = MagicMock()
        cfg.tts.enabled = False
        _check_tts_env(cfg)
        captured = capsys.readouterr()
        assert "跳过" in captured.out

    def test_minimax_key_missing_warns(self, monkeypatch, capsys):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        from Module.config.preflight import _check_tts_env, _COUNTS
        _COUNTS.__init__()
        cfg = MagicMock()
        cfg.tts.enabled = True
        cfg.tts.model = "minimax/speech-2.8-hd"
        _check_tts_env(cfg)
        captured = capsys.readouterr()
        assert "WARN" in captured.out
        assert _COUNTS.warned == 1


class TestRun:
    def test_run_returns_false_on_failure(self, tmp_path, monkeypatch):
        """无 .env 时 run() 应返回 False。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from Module.config.preflight import run
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        cfg.tts.enabled = False
        cfg.stt.enabled = False
        cfg.agent.character = "character/elysia.yaml"
        ok = run(cfg, full=False)
        assert ok is False  # 缺少 .env

    def test_run_returns_true_when_all_ok(self, tmp_path, monkeypatch):
        """配置齐全时 run() 应返回 True。"""
        dotenv = tmp_path / ".env"
        dotenv.write_text("DEEPSEEK_API_KEY=sk-test")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        from Module.config.preflight import run
        cfg = MagicMock()
        cfg.llm.model = "deepseek/deepseek-chat"
        cfg.tts.enabled = False
        cfg.stt.enabled = False
        cfg.agent.character = "character/elysia.yaml"
        ok = run(cfg, full=False)
        assert ok is True
```

- [ ] **Step 2: Run tests**

```bash
cd /home/yomu/Agent && python -m pytest Module/config/test_preflight.py -v
```

- [ ] **Step 3: Commit**

```bash
git add Module/config/test_preflight.py
git commit -m "test: add preflight unit tests"
```

---

### Verification

```bash
# 1. make check (配置完整时应该通过)
make check

# 2. make check-full
make check-full

# 3. 模拟无 .env 场景
cd /tmp && python /home/yomu/Agent/app.py --check
# 预期: FAIL ".env 不存在"

# 4. app.py 语法
python -c "import ast; ast.parse(open('app.py').read()); print('ok')"

# 5. 单元测试
python -m pytest Module/config/test_preflight.py -v
```
