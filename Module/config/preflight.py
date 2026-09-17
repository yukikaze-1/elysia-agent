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


def _resolved_llm_url(provider: str) -> tuple[str, str]:
    """按**运行期同一判据**解析 LLM base URL。

    返回 `(解析结果, env key)`——解析结果为空串表示该 provider **任何一层都没有**
    默认地址（此时运行期 `LLMFactory.create` 同样会拒绝建连）。

    为什么要复用 `resolve_base_url` 而不是自己看 env：本模块此前只判
    `os.getenv(url_key)`，而运行期的回退链是
    `explicit > env > providers.yaml::<provider>.base_url`。于是
    `providers.yaml` 已内置默认地址（deepseek/openai/ollama）时，
    只设 `DEEPSEEK_API_KEY`、不设 `DEEPSEEK_URL` 的配置**运行期完全可用**，
    预检却报 FAIL ⇒ `app.py --check` 退 1 ⇒ `scripts/start.sh` 拒绝启动一个
    本来能跑的部署（Batch 3.5 / B3-CB-3）。判据必须与实际启动条件同一权威，
    而不是"配置是否长得像某张静态表"。
    """
    from Module._common.registry_helpers import resolve_base_url
    from Module.config.providers import load_provider_defaults

    defaults = load_provider_defaults("llm").get(provider, {})
    url_key = str(defaults.get("url_key", "") or "")
    return resolve_base_url(url_key, defaults), url_key


def _check_llm_env(config: Any) -> None:
    """根据 LLM provider 检查 API key 与 base URL（判据与运行期一致）。"""
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

    # URL：按运行期回退链解析（env → providers.yaml 默认值），只有**真的没有**
    # 任何默认地址才 FAIL（运行期同样建不出后端）。
    resolved, url_key = _resolved_llm_url(provider)
    if resolved:
        source = f"{url_key} 已设置" if url_key and os.getenv(url_key) else "providers.yaml 默认值"
        _pass(f"LLM URL ok ({source} → {resolved})")
    else:
        _fail(
            f"LLM provider '{provider}' 没有可用的 base URL — "
            f"请设置 {url_key or '该 provider 的 URL 环境变量'}，"
            f"或在 Module/config/providers.yaml 里写默认值"
        )


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


def _check_mongo_env(config: Any) -> None:
    """检查 MongoDB 连接串（判据 = 运行期真正会用的那个值）。

    运行期**不看 env**：`Agent.__init__` 把 `config.database.mongodb_uri` 显式传给
    每个仓储（`agent.py:175` 等 → `DocumentDBFactory.create(base_url=…)`），而该值由
    `ConfigLoader` 按 `CLI > env > config.yaml > dataclass 默认` 解析。config.yaml
    自带 `mongodb_uri: mongodb://localhost:27017`，dataclass 默认也是它 ⇒
    **只缺 `MONGODB_URI` 环境变量从来不是启动阻碍**。旧实现只判
    `os.getenv("MONGODB_URI")` 并报 FAIL，会让 `scripts/start.sh` 拒绝一个本来能跑的
    部署（Batch 3.5 / B3-CB-3）。
    """
    val = str(getattr(getattr(config, "database", None), "mongodb_uri", "") or "")
    if val:
        _pass(f"MongoDB 连接串 ok ({val})")
    else:
        _fail(
            "MongoDB 连接串为空 — 请设置 MONGODB_URI 环境变量，"
            "或在 config.yaml 的 database.mongodb_uri 填写"
        )


def _check_milvus_env(config: Any) -> None:
    """检查 Milvus 端点（判据 = 运行期真正会用的那个值）。

    同 `_check_mongo_env`：运行期读 `config.database.milvus_uri`
    （`agent.py:197` 传给 `MemoryRepositoryV2`），config.yaml 与 dataclass 默认均为
    `http://localhost:19530`。
    """
    val = str(getattr(getattr(config, "database", None), "milvus_uri", "") or "")
    if val:
        _pass(f"Milvus 端点 ok ({val})")
    else:
        _fail(
            "Milvus 端点为空 — 请设置 MILVUS_URI 环境变量，"
            "或在 config.yaml 的 database.milvus_uri 填写"
        )


def _provider_url_note(provider_key: str, module: str) -> tuple[str, str]:
    """按运行期同一回退链解析某 provider 的 base URL。

    返回 `(解析结果, 人读来源说明)`。空串表示该 provider 在任何一层都没有地址
    （运行期同样建不出后端）。与 `_resolved_llm_url` 同判据，只是多一句来源描述，
    供 WARN/PASS 文案使用——避免"运行期有默认值、预检却说未设置"这种谎报。
    """
    from Module._common.registry_helpers import resolve_base_url
    from Module.config.providers import load_provider_defaults

    defaults = load_provider_defaults(module).get(provider_key, {})
    url_key = str(defaults.get("url_key", "") or "")
    resolved = resolve_base_url(url_key, defaults)
    if not resolved:
        return "", "无任何可用地址"
    if url_key and os.getenv(url_key):
        return resolved, f"{url_key} 已设置"
    return resolved, "providers.yaml 默认值"


def _check_tts_env(config: Any) -> None:
    """根据 TTS provider 检查可用性（WARN 级别——可选子系统不阻断启动）。

    判据与运行期一致：先看该 provider 的**凭据/地址**是否真的缺失，而不是
    "env 变量是否被设置"。`gpt-sovits` 是本地 provider（`env_key: ""`），
    地址来自 `providers.yaml::tts.gpt-sovits.base_url`，缺 `GPT_SOVITS_URL`
    环境变量时运行期照常可用——旧实现却报"将不可用"。
    """
    if not config.tts.enabled:
        _pass("TTS 未启用，跳过 env 检查")
        return
    provider = _parse_provider(config.tts.model)
    key = _TTS_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 TTS provider: {provider}")
        return
    resolved, source = _provider_url_note(provider, "tts")
    if resolved:
        _pass(f"TTS 可用 ({provider} → {resolved}，{source})")
        return
    # 地址本身就没有默认值（如 minimax）：此时凭据才是唯一判据
    val = os.getenv(key, "")
    if val:
        _pass(f"TTS env ok ({key} 已设置)")
    else:
        _warn(f"{key} 未设置 — TTS ('{provider}') 将不可用")


def _check_stt_env(config: Any) -> None:
    """根据 STT provider 检查可用性（WARN 级别——可选子系统不阻断启动）。

    同 `_check_tts_env`：判据是"运行期能否建出后端"，不是"env 变量是否被设置"。
    """
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
    resolved, source = _provider_url_note(provider, "stt")
    if resolved:
        _pass(f"STT 可用 ({provider} → {resolved}，{source})")
        return
    # sensevoice 的 base_url 是空串且 env_key 为空 ⇒ 只有 SENSEVOICE_URL 能给地址
    val = os.getenv(key, "")
    if val:
        _pass(f"STT env ok ({key} 已设置)")
    else:
        _warn(f"{key} 未设置 — STT ('{provider}') 将不可用")


def _check_embedding_model() -> None:
    """检查嵌入模型是否已缓存。

    **判据与加载器同一个实现**（`Module.embedding.huggingface.find_local_model`）——
    此前这里自己维护一份候选表，判据是「目录存在且非空」，而加载器要求「快照里有
    config.json」，两者永不相等：实测 preflight 报「✓ 已缓存」而加载器报
    「本地缓存缺失，拒绝加载」，agent 全程跑在无长期记忆的降级态、且只在
    `app-debug.log` 留一行 DEBUG（2026-09-12）。

    只做路径定位，不做 sha256 校验（1.3GB 约 9s，不适合每次预检）；哈希校验仍在
    加载前由 `_verified_local_path` 执行，失败会以 WARNING 落 app.log。
    """
    from Module.embedding.huggingface import find_local_model
    from Module.embedding.model_manifest import DEFAULT_MODEL_ID

    local = find_local_model(DEFAULT_MODEL_ID)
    if local:
        _pass(f"嵌入模型已缓存 ({DEFAULT_MODEL_ID}) → {local}")
        return
    _fail(f"嵌入模型未缓存 ({DEFAULT_MODEL_ID}) — "
          f"运行 scripts/download_model.py 下载（约 1.3GB）")


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


async def _check_milvus_connectivity(config: Any) -> None:
    """Milvus 连通性探活。

    端点取自 `config.database.milvus_uri`（运行期权威），不是 `os.getenv`——
    否则 config.yaml 里配了地址时会去连一个空串，报出与运行期无关的失败。
    """
    uri = str(getattr(getattr(config, "database", None), "milvus_uri", "") or "")
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


async def _check_mongodb_connectivity(config: Any) -> None:
    """MongoDB 连通性探活。

    连接串取自 `config.database.mongodb_uri`（运行期权威），理由同上。
    """
    uri = str(getattr(getattr(config, "database", None), "mongodb_uri", "") or "")
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
    _check_mongo_env(config)
    _check_milvus_env(config)

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
        asyncio.run(_check_milvus_connectivity(config))
        asyncio.run(_check_mongodb_connectivity(config))

    # 摘要
    print()
    total = _COUNTS.passed + _COUNTS.warned + _COUNTS.failed
    status = "通过" if _COUNTS.ok else "存在失败项"
    print(
        f"结果: {_COUNTS.passed}/{total} passed, "
        f"{_COUNTS.warned} warned, {_COUNTS.failed} failed — {status}"
    )
    return _COUNTS.ok
