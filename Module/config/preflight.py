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
#
# ⚠️ 这两张表只回答「该 provider 的凭据/地址用哪些 env key」与「这个名字认不认识」，
# **不回答「本仓实现了没有」**——后者是 capability 事实，唯一权威在各 Module 的
# registry（`LLMFactory.is_implemented` / `STTFactory.is_implemented`）。
# Batch 5 / N-10 之前这里还有张 `_STT_STUB_PROVIDERS`，是 registry 内联元组的第二份
# 副本；已删除，判据改问 registry（§90：不要第二套静态真相表）。

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

_CRITICAL_PACKAGES = ["aiohttp", "httpx", "openai", "pydantic", "jinja2", "yaml", "dotenv"]


# ---- helpers ----

def _parse_provider(model_id: str) -> str:
    """从 'provider/model' 解析 provider 部分。"""
    return model_id.split("/")[0] if "/" in model_id else model_id


def _llm_provider_implemented(provider: str) -> bool:
    """问运行期权威：本仓实现了该 LLM provider 吗。

    只查 registry 的 capability 元数据，**不建连、不发网络请求**（PCT-3：静态预检
    network-free）。`Module.llm` 会拉起 openai SDK，故 `run()` 把 `_check_pip_deps()`
    排在 `_check_llm_env()` 之前——缺包时由它给出可读的 [FAIL]，
    而不是让这里抛裸 ImportError。
    """
    from Module.llm import LLMFactory

    return LLMFactory.is_implemented(provider)


def _stt_provider_implemented(provider: str) -> bool:
    """问运行期权威：本仓实现了该 STT provider 吗（同 `_llm_provider_implemented`）。"""
    from Module.stt import STTFactory

    return STTFactory.is_implemented(provider)


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
    """根据 LLM provider 检查实现可用性与 API key / base URL（判据与运行期一致）。

    三步，顺序即语义（§79：错误类别必须与真实失败层一致）：

    1. **capability**（本仓实现了没有）——未实现 ⇒ FAIL 并**就此打住**。理由：LLM 是
       启动必需子系统，`LLMAdapter.__init__` 无条件 `LLMFactory.create`，未实现的
       provider 会让进程在启动期抛 `NotImplementedError`。此时再去报 "API key 已设置 /
       URL ok" 是**假绿**（N-10 的原始形态：13/13 passed 然后启动崩）。
    2. 凭据。
    3. 地址（按运行期回退链解析）。

    判据来源是 `LLMFactory.is_implemented`（运行期权威），不是本模块的静态表——
    本模块不维护"哪些 provider 未实现"的副本。
    """
    model_id = config.llm.model
    provider = _parse_provider(model_id)
    if provider == "ollama":
        _pass("LLM env ok (ollama 无需 API key)")
        return
    key = _LLM_PROVIDER_ENV.get(provider)
    if key is None:
        # 未知 provider：**保持 fail-open**（WARN 不 FAIL）。它在 providers.yaml 里
        # 有没有实现由 registry 决定，本模块不猜；缺 metadata ≠ 未实现。
        _warn(f"未知 LLM provider: {provider}")
        return

    if not _llm_provider_implemented(provider):
        _fail(
            f"LLM provider '{provider}' 在本仓尚未实现 — "
            f"运行期 LLMFactory.create('{model_id}') 会抛 NotImplementedError，"
            f"进程将在启动期失败。请改用已实现的 provider，"
            f"或先在 Module/llm/ 里补上实现"
        )
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


def _provider_url_note(provider_key: str, module: str) -> tuple[str, str, str]:
    """按运行期同一回退链解析某 provider 的地址与凭据需求。

    返回 `(解析出的地址, 来源说明, 运行期凭据 env key)`。第三项取自
    `providers.yaml` 的 `env_key` —— 与运行期 `resolve_api_key(cfg["env_key"], …)`
    **同一个字段**：空串即"该 provider 不需要凭据"（本地服务，如 gpt-sovits /
    sensevoice），非空即"缺它运行期就建不出后端"（如 minimax）。

    空地址表示该 provider 在任何一层都没有地址（运行期同样建不出后端）。
    """
    from Module._common.registry_helpers import resolve_base_url
    from Module.config.providers import load_provider_defaults

    defaults = load_provider_defaults(module).get(provider_key, {})
    url_key = str(defaults.get("url_key", "") or "")
    resolved = resolve_base_url(url_key, defaults)
    if not resolved:
        return "", "无任何可用地址", str(defaults.get("env_key", "") or "")
    if url_key and os.getenv(url_key):
        return resolved, f"{url_key} 已设置", str(defaults.get("env_key", "") or "")
    return resolved, "providers.yaml 默认值", str(defaults.get("env_key", "") or "")


def _judge_optional_provider(provider: str, module: str, label: str) -> None:
    """可选子系统（TTS/STT）单 provider 判定：地址与凭据**逐项**判，全部齐备才 PASS。

    ⚠️ 两处刻意的设计，都是被实测反例逼出来的：

    1. **不"地址有了就提前 PASS"**：`minimax` 的地址在 `.env` 里有
       （`MINIMAX_TTS_URL`），而 `MINIMAX_API_KEY` 缺了运行期是
       `ValueError: MiniMax TTS 需要 api_key` —— 提前 PASS 会把一个**建不出来的
       后端**报成可用（Batch 3.5 Challenger 实测复现）。
    2. **凭据需求只认 provider 自己声明的 `env_key`**，不回退本模块的静态表：
       静态表 `_TTS_PROVIDER_ENV["gpt-sovits"]` 存的是 **`GPT_SOVITS_URL`（地址，
       不是凭据！）**，把它当成"凭据 key"会让一个无需凭据的本地 provider
       被要求设 URL 环境变量——正是本次要修的 B3-CB-3 原病。
       故 `env_key` 为空串 ⇒ 本地服务、无需凭据（gpt-sovits / sensevoice）。
    """
    resolved, source, cred_key = _provider_url_note(provider, module)
    if not cred_key:
        # 本地 provider：只判地址即可（gpt-sovits 靠 providers.yaml 默认值即可建出）
        if resolved:
            _pass(f"{label} 可用 ({provider} → {resolved}，{source}；本地服务无需凭据)")
        else:
            _warn(f"{provider} 没有可用地址 — {label} ('{provider}') 将不可用")
        return
    if not os.getenv(cred_key, ""):
        _warn(f"{cred_key} 未设置 — {label} ('{provider}') 将不可用")
        return
    if resolved:
        _pass(f"{label} 可用 ({provider} → {resolved}，{source})")
    else:
        _warn(f"{provider} 没有可用地址 — {label} ('{provider}') 将不可用")


def _check_tts_env(config: Any) -> None:
    """根据 TTS provider 检查可用性（WARN 级别——可选子系统不阻断启动）。

    判据与运行期一致：**地址与凭据都齐备**才算可用，而不是"env 变量是否被设置"。
    `gpt-sovits` 是本地 provider（`env_key: ""`），地址来自
    `providers.yaml::tts.gpt-sovits.base_url`，缺 `GPT_SOVITS_URL` 环境变量时运行期
    照常可用——旧实现却报"将不可用"。
    """
    if not config.tts.enabled:
        _pass("TTS 未启用，跳过 env 检查")
        return
    provider = _parse_provider(config.tts.model)
    key = _TTS_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 TTS provider: {provider}")
        return
    _judge_optional_provider(provider, "tts", "TTS")


def _check_stt_env(config: Any) -> None:
    """根据 STT provider 检查可用性（WARN 级别——可选子系统不阻断启动）。

    同 `_check_tts_env`：判据是"运行期能否建出后端"（地址 + 凭据逐项判），
    不是"env 变量是否被设置"。**"本仓实现了没有"问 registry**（此前是本模块自带的
    `_STT_STUB_PROVIDERS` 副本表——同一事实的第二份副本，漂移即假绿）。
    """
    if not config.stt.enabled:
        _pass("STT 未启用，跳过 env 检查")
        return
    provider = _parse_provider(config.stt.model)
    key = _STT_PROVIDER_ENV.get(provider)
    if key is None:
        _warn(f"未知 STT provider: {provider}")
        return
    if not _stt_provider_implemented(provider):
        _warn(f"STT provider '{provider}' 暂未实现（factory 会抛出 NotImplementedError）")
        return
    _judge_optional_provider(provider, "stt", "STT")


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

    if not _llm_provider_implemented(provider):
        # capability 缺失 ≠ 连通性问题（§79）。这里不再去 `LLMFactory.create` 撞一次
        # NotImplementedError 再把它报成"连通异常"——那会让读日志的人去查网络，
        # 而真正的原因（本仓没实现）已在 `_check_llm_env` 判 FAIL。
        _warn(f"LLM provider '{provider}' 未实现，跳过连通性检查")
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
    if not _stt_provider_implemented(provider):
        # §34：unsupported 不得被"跳过"并报成成功。STT 是可选子系统，判 WARN
        # （与 `_check_stt_env` 同级别），不上 FAIL——否则 --full 会拒绝一个
        # 合法跑纯文本模式的部署。
        _warn(f"STT provider '{provider}' 未实现，无法探活")
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
    # pip 依赖排在 provider 检查之前：`_check_llm_env` 会 import Module.llm（拉起 openai
    # SDK），缺包时应当由 `_check_pip_deps` 给出可读的 [FAIL]（"缺失 pip 包: openai"），
    # 而不是让静态检查抛裸 ImportError。
    _check_pip_deps()
    _check_llm_env(config)
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
