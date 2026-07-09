"""环境预检 — app.py --check 调用。验证 Python 版本、.env、pip 依赖、API key、连通性。"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    ver = f"{vi[0]}.{vi[1]}.{vi[2]}"
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
        _pass("LLM env ok (ollama 无需 API key)")
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

async def _probe_http(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0
) -> bool:
    """HTTP GET 探活，返回 True 表示可达。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(url, headers=headers)
            return resp.status_code < 500
    except Exception:
        return False


async def _check_llm_connectivity(config: Any) -> None:
    """LLM 服务连通性探活。"""
    from Module.config.providers import load_provider_defaults

    model_id = config.llm.model
    provider = _parse_provider(model_id)
    defaults = load_provider_defaults("llm").get(provider, {})
    base_url_env_key = defaults.get("env_key", "")
    base_url = os.getenv(base_url_env_key, defaults.get("base_url", ""))
    api_key = os.getenv(_LLM_PROVIDER_ENV.get(provider, ""), "")

    if not base_url:
        _warn(f"无法确定 LLM base_url (provider={provider})，跳过连通性检查")
        return

    url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    ok = await _probe_http(url, headers=headers, timeout=10.0)
    if ok:
        _pass(f"LLM 连通 ok ({url})")
    else:
        _warn(f"LLM 不可达 ({url})")


async def _check_tts_connectivity(config: Any) -> None:
    """TTS 服务连通性探活（轻量，不合成音频）。"""
    if not config.tts.enabled:
        _pass("TTS 未启用，跳过连通性检查")
        return

    from Module.config.providers import load_provider_defaults

    provider = _parse_provider(config.tts.model)
    defaults = load_provider_defaults("tts").get(provider, {})
    base_url = os.getenv(
        defaults.get("env_key", ""),
        defaults.get("base_url", ""),
    )

    if not base_url:
        _warn(f"无法确定 TTS base_url (provider={provider})，跳过连通性检查")
        return

    # 各 provider 的轻量探活端点
    if provider == "gpt-sovits":
        url = f"{base_url.rstrip('/')}/health"
        ok = await _probe_http(url, timeout=5.0)
    elif provider == "minimax":
        # MiniMax 用 POST /v1/get_voice，但简单 GET base_url 即可验证可达性
        ok = await _probe_http(base_url, timeout=5.0)
    else:
        # OpenAI 兼容: GET /v1/models
        url = f"{base_url.rstrip('/')}/v1/models"
        api_key = os.getenv(_TTS_PROVIDER_ENV.get(provider, ""), "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        ok = await _probe_http(url, headers=headers, timeout=5.0)

    if ok:
        _pass(f"TTS 连通 ok ({base_url})")
    else:
        _warn(f"TTS 不可达 ({base_url})")


async def _check_stt_connectivity(config: Any) -> None:
    """STT 服务连通性探活（轻量，不转录音频）。"""
    if not config.stt.enabled:
        _pass("STT 未启用，跳过连通性检查")
        return

    from Module.config.providers import load_provider_defaults

    provider = _parse_provider(config.stt.model)
    if provider in _STT_STUB_PROVIDERS:
        _pass(f"STT '{provider}' 暂未实现，跳过连通性检查")
        return

    defaults = load_provider_defaults("stt").get(provider, {})
    base_url = os.getenv(
        defaults.get("env_key", ""),
        defaults.get("base_url", "http://localhost:20042"),
    )

    if not base_url:
        _warn(f"无法确定 STT base_url (provider={provider})，跳过连通性检查")
        return

    if provider == "sensevoice":
        url = f"{base_url.rstrip('/')}/health"
        ok = await _probe_http(url, timeout=5.0)
    else:
        url = f"{base_url.rstrip('/')}/v1/models"
        api_key = os.getenv(_STT_PROVIDER_ENV.get(provider, ""), "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        ok = await _probe_http(url, headers=headers, timeout=5.0)

    if ok:
        _pass(f"STT 连通 ok ({base_url})")
    else:
        _warn(f"STT 不可达 ({base_url})")


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

    # Phase 3: 连通性 (--full 才执行)
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
