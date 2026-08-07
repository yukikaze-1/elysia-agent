"""环境预检 — app.py --check 调用。验证 Python 版本、.env、pip 依赖、API key、连通性。"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paths import ProjectPath


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
    "sensevoice": "SENSEVOICE_URL",
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
    """记录一个失败项并打印 [FAIL] 消息。

    Args:
        msg: 失败描述信息。
    """
    _COUNTS.failed += 1
    print(f"  [FAIL] {msg}")


def _warn(msg: str) -> None:
    """记录一个警告项并打印 [WARN] 消息。

    Args:
        msg: 警告描述信息。
    """
    _COUNTS.warned += 1
    print(f"  [WARN] {msg}")


def _pass(msg: str) -> None:
    """记录一个通过项并打印 [PASS] 消息。

    Args:
        msg: 通过描述信息。
    """
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
    """根据 LLM provider 检查对应 env var（API key + URL）。"""
    from Module.config.providers import load_provider_defaults

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

    # 检查 URL
    defaults = load_provider_defaults("llm").get(provider, {})
    url_key = defaults.get("url_key", "")
    if url_key:
        url_val = os.getenv(url_key, "")
        if url_val:
            _pass(f"LLM URL ok ({url_key} 已设置)")
        else:
            _fail(f"{url_key} 未设置 — LLM provider '{provider}' 需要此环境变量")


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


def _check_mongo_env() -> None:
    """检查 MONGODB_URI 是否设置。"""
    val = os.getenv("MONGODB_URI", "")
    if val:
        _pass(f"数据库 env ok (MONGODB_URI 已设置)")
    else:
        _fail("MONGODB_URI 未设置 — 请在 .env 中设置，如 MONGODB_URI=mongodb://localhost:27017")


def _check_milvus_env() -> None:
    """检查 MILVUS_URI 是否设置。"""
    val = os.getenv("MILVUS_URI", "")
    if val:
        _pass(f"数据库 env ok (MILVUS_URI 已设置)")
    else:
        _fail("MILVUS_URI 未设置 — 请在 .env 中设置，如 MILVUS_URI=http://localhost:19530")


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


def _check_embedding_model() -> None:
    """检查嵌入模型是否已缓存。兼容三种目录布局。"""
    # HF hub 快照格式: models--BAAI--bge-large-zh-v1.5/snapshots/{hash}/config.json
    # 直接格式: BAAI/bge-large-zh-v1.5/config.json
    candidates = [
        ProjectPath.MODEL_CACHE.resolve() / "models--BAAI--bge-large-zh-v1.5" / "snapshots",
        ProjectPath.MODEL_CACHE.resolve() / "BAAI" / "bge-large-zh-v1.5",
        Path.home() / ".cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5/snapshots",
        Path.home() / ".cache/huggingface/hub/BAAI/bge-large-zh-v1.5",
    ]
    for d in candidates:
        if d.exists() and any(d.iterdir()):
            _pass("嵌入模型已缓存 (BAAI/bge-large-zh-v1.5)")
            return
    _fail("嵌入模型未缓存 — 运行 scripts/download_model.py 下载（约 1.3GB）")


def _check_writable_dirs() -> None:
    """检查 log/、data/ 目录是否可写。"""
    for d in (str(ProjectPath.LOG.resolve()), str(ProjectPath.DATA.resolve())):
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
# TTS 连通性：MiniMax → POST /v1/get_voice；GPT-SoVits → GET /health；OpenAI → GET /v1/models。
# STT 连通性：SenseVoice → GET /health；OpenAI → GET /v1/models。
# 每种 backend 的 test() 实现可能不同，但都是该 backend 的官方连通性验证方式。


async def _check_llm_connectivity(config: Any) -> None:
    """LLM 服务连通性探活 — 发送真实对话请求验证 API key。"""
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
    """TTS 服务连通性探活 — 通过 backend.test() 验证服务可用。"""
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
    """STT 服务连通性探活 — 通过 backend.test() 验证服务可用。"""
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


async def _check_milvus_connectivity() -> None:
    """Milvus 向量数据库连通性探活。"""
    uri = os.getenv("MILVUS_URI", "")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{uri.rstrip('/')}/health")
            if resp.status_code < 500:
                _pass(f"Milvus 连通 ok ({uri})")
            else:
                _fail(f"Milvus 不可达 ({uri}) — HTTP {resp.status_code}")
    except Exception as e:
        _fail(f"Milvus 不可达 ({uri}): {e}")


async def _check_mongodb_connectivity() -> None:
    """MongoDB 文档数据库连通性探活。"""
    uri = os.getenv("MONGODB_URI", "")
    try:
        import motor.motor_asyncio
    except ImportError:
        _fail("motor 未安装（pip install motor），跳过 MongoDB 连通性检查")
        return
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri, serverSelectionTimeoutMS=3000,
        )
        await client.server_info()
        client.close()
        _pass(f"MongoDB 连通 ok ({uri})")
    except Exception as e:
        _fail(f"MongoDB 不可达 ({uri}): {e}")


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
    _check_embedding_model()
    _check_mongo_env()
    _check_milvus_env()

    # 阶段 2：WARN
    _check_tts_env(config)
    _check_stt_env(config)
    _check_writable_dirs()

    # Phase 3: 连通性 (--full 才执行)
    if full:
        import asyncio
        asyncio.run(_check_llm_connectivity(config))
        asyncio.run(_check_tts_connectivity(config))
        asyncio.run(_check_stt_connectivity(config))
        asyncio.run(_check_milvus_connectivity())
        asyncio.run(_check_mongodb_connectivity())

    # 摘要
    print()
    total = _COUNTS.passed + _COUNTS.warned + _COUNTS.failed
    status = "通过" if _COUNTS.ok else "存在失败项"
    print(
        f"结果: {_COUNTS.passed}/{total} passed, "
        f"{_COUNTS.warned} warned, {_COUNTS.failed} failed — {status}"
    )
    return _COUNTS.ok
