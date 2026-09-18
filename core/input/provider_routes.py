"""Provider 管理 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

providers list/save/delete/test + 私有 helper。模块级 handler（对齐 session_routes 模式）。
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from core.input._common import _tool_error
from core.input.errors import _json_object
from core.storage.secret_store import SecretStoreCorruptError
from frontend_contract import rest as rest_contract

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def _custom_profile_in_use(agent: "Agent", service: str, name: str) -> bool:
    """判定 custom/{name} Profile 是否正被使用（活动会话 service_state / config / vision）。

    批次 5（Task 6）删除激活约束：活动会话 service_state.{svc}_model、config.{svc}.model、
    vision.model（LLM 场景——vision 工具走 LLM provider）任一以 custom/{name} 开头 →
    该 Profile 正被使用 → 409 PROFILE_IN_USE 拒绝删除。

    Args:
        agent: Agent 组合根（读 agent.session.service_state + agent.config）。
        service: "llm"/"stt"/"tts"。
        name: 自定义 provider 名（无 custom/ 前缀）。

    Returns:
        bool: True = 正在使用。
    """
    prefix = f"custom/{name}"
    # **运行期后端**（`agent.<svc>.model`）必须纳入判定，且三个服务对称。
    # 只读"活动会话的 service_state"会漏：`switch_session` 只换活动指针，
    # **有意不重放模型热切换**（见 `_apply_service_enabled` 的 S2-C3 取舍与
    # `test_*_model_hot_switch_is_deliberately_not_replayed`）——于是切到一个
    # `llm_model` 为空的会话后，service_state 说"没用"，而适配器仍在跑 `custom/x`，
    # 删除它会连带删掉密钥，把正在使用的会话打断。故以运行期值为第一真相源。
    runtime_model = getattr(getattr(agent, service, None), "model", "") or ""
    if service == "llm":
        candidates = (
            runtime_model,
            agent.session.service_state.llm_model,
            agent.config.llm.model,
            agent.config.vision.model,
        )
    elif service == "stt":
        candidates = (
            runtime_model,
            agent.session.service_state.stt_model,
            agent.config.stt.model,
        )
    else:  # tts
        candidates = (
            runtime_model,
            agent.session.service_state.tts_model,
            agent.config.tts.model,
        )
    return any(c.startswith(prefix) for c in candidates if c)


async def _run_usable_test(backend: Any, service: str, request: web.Request,
                           body: dict) -> dict:
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

def _strip_secret_keys(data: dict) -> dict:
    """响应数据剔除 api_key 键（验收 LOW#1 加固）。

    save/delete 响应的 data 是 load() 原始 custom 列表（裸 dict，不经契约白名单），
    防遗留条目（批次 5 前误存过 api_key 字段）回显密钥——与 GET 经 model_validate
    白名单丢弃的行为同口径。只处理 llm/stt/tts 三个已知 service 键，其余透传。
    """
    for svc in ("llm", "stt", "tts"):
        for entry in data.get(svc, []):
            if isinstance(entry, dict):
                entry.pop("api_key", None)
    return data
async def providers_list_handler(request: web.Request) -> web.Response:
    """返回过滤后的 provider 列表（providers.yaml 单一来源 + env 过滤 + custom）。"""
    from core.providers.custom_providers import load as load_custom
    from core.providers.provider_registry import build_providers_response
    return web.json_response(
        rest_contract.ProvidersResponse.model_validate(
            build_providers_response(load_custom())).wire_dict())

async def providers_save_handler(request: web.Request) -> web.Response:
    """保存一个新的自定义 provider（批次 5：runtime 门控 + SSRF + 密钥入 Store）。

    body: {service, entry:{name, base_url?, api_key?, api_key_env?, models?,
           runtime?, capabilities?}}。
    - runtime 非法枚举 → 400 PROFILE_INVALID_RUNTIME（ProtocolError）。
    - base_url 门控按 runtime：backend 缺 → v1 ApiFail（既有检查）；desktop/local
      缺 → 放行（D13）；backend → validate_endpoint（非空时，SSRF）。
    - 非 backend 带 api_key → 400 PROFILE_INVALID_RUNTIME（密钥仅 backend 后端执行可收）。
    - api_key → add_provider 内部直存 Secret Store（不入 YAML）；D18 清理。
    - 响应 = 全量 custom 列表（spec §3.3 步骤 8，前批次 wire 形状）；credentialConfigured
      经 GET /api/providers 获取（验收 F2：save 响应不含该布尔，避免偏离既有契约）。

    Returns:
        web.Response: {"ok": True, "data": 全量 custom 列表（api_key 键已剔除）} 或错误信封。
    """
    from core.providers.custom_providers import add_provider
    from Module._common.ssrf import SSRFError, validate_endpoint
    try:
        body = await _json_object(request)
        service = body.get("service", "")
        entry = body.get("entry", {})
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return web.json_response(rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)
    if service not in ("llm", "stt", "tts"):
        return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
    if not entry.get("name"):
        return web.json_response(rest_contract.ApiFail(error="缺少 name 或 base_url").wire_dict(), status=400)
    # runtime 解析：非法枚举 → PROFILE_INVALID_RUNTIME（新错误码走 ProtocolError 信封）
    runtime = entry.get("runtime", "backend")
    if runtime not in ("backend", "desktop", "local_worker"):
        return _tool_error("PROFILE_INVALID_RUNTIME", f"非法 runtime: {runtime}", 400)
    base_url = entry.get("base_url", "")
    if runtime == "backend":
        # backend 缺 base_url → v1 ApiFail（既有检查）；非空时 SSRF 校验
        if not base_url:
            return web.json_response(rest_contract.ApiFail(error="缺少 name 或 base_url").wire_dict(), status=400)
        try:
            validate_endpoint(base_url)
        except SSRFError as e:
            return _tool_error("SSRF_BLOCKED", str(e), 400)
    elif entry.get("api_key"):
        # desktop/local 由 Electron 执行，后端不收密钥（D18）
        return _tool_error("PROFILE_INVALID_RUNTIME", "非 backend runtime 不接受 api_key", 400)
    try:
        data = add_provider(service, entry)
    except SecretStoreCorruptError:
        # S2-B7b：本地密钥文件损坏是**非暂时性**失败——报"请稍后重试"会误导用户
        # （重试永远不会成功，必须先处理那个文件）。信封形状不变（仍是 ApiFail），
        # 文案不泄漏路径（沿用下面同一条纪律），但要说清"已拒绝覆盖"这个事实。
        logger.error("保存 provider 失败：本地密钥文件损坏，已拒绝写入以保护既有密钥")
        return web.json_response(rest_contract.ApiFail(
            error="本地密钥文件损坏，已拒绝覆盖以保护既有密钥；"
                  "请修复或移走该文件后重试").wire_dict(), status=500)
    except Exception as e:
        # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
        logger.warning("保存 provider 失败: %s", e)
        return web.json_response(rest_contract.ApiFail(error="保存失败，请稍后重试").wire_dict(), status=500)
    # review 修正：响应恢复全量 custom 列表（spec §3.3 步骤 8 + 前批次 wire 形状），
    # 不再返回单条目——避免偏离既有契约；credentialConfigured 经 GET /api/providers 获取。
    # 验收 LOW#1 加固：响应数据剔除 api_key 键——防遗留条目（批次 5 前误存过 api_key
    # 字段）回显密钥（GET 经契约白名单丢弃、save/delete 是裸 dict，须同口径）。
    return web.json_response(rest_contract.ApiOk(data=_strip_secret_keys(data)).wire_dict())

async def providers_delete_handler(request: web.Request) -> web.Response:
    """删除一个自定义 provider（批次 5：激活约束——活动会话/配置正在使用的不可删）。

    URL 路径参数包含 service 和 name。激活判定（_custom_profile_in_use）：活动会话
    service_state.{svc}_model / config.{svc}.model / vision.model 任一以 custom/{name}
    开头 → 409 PROFILE_IN_USE（ProtocolError）；非激活 → remove_provider（条目 +
    Secret Store 密钥一并清理）。

    Returns:
        web.Response: {"ok": True, "data": ...}、409 或 v1 ApiFail。
    """
    from core.providers.custom_providers import remove_provider
    service = request.match_info.get("service", "")
    name = request.match_info.get("name", "")
    if service not in ("llm", "stt", "tts"):
        return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
    agent = request.app.get("agent")
    if agent is not None and _custom_profile_in_use(agent, service, name):
        return _tool_error("PROFILE_IN_USE", f"custom/{name} 正在使用，无法删除", 409)
    try:
        data = remove_provider(service, name)
        # 验收 LOW#1：同 save，响应剔除 api_key 键（防遗留条目回显密钥）
        return web.json_response(rest_contract.ApiOk(data=_strip_secret_keys(data)).wire_dict())
    except Exception as e:
        # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
        logger.warning("删除 provider 失败: %s", e)
        return web.json_response(rest_contract.ApiFail(error="删除失败，请稍后重试").wire_dict(), status=500)

def _resolve_test_model(
    request: web.Request,
    service: str,
    provider: str,
    model: str,
) -> tuple[tuple[str, str], web.Response | None]:
    """provider/model 缺省回退：均缺省用**运行期在跑的那个**模型，仅 provider 缺省取 custom。

    "运行期在跑的那个"是三个子系统统一的权威（F6）：

    * `llm` → `agent.llm.model`（适配器持有的后端，热切换后即新值）；
    * `stt` → `agent.stt.model`（STTBackend.model 派生自 provider_name + `_model`）；
    * `tts` → `agent.tts.model`。

    后端**未连接**（None，或自述模型为空）才回退 `config.<svc>.model`（配置默认值）。
    修前 STT 分支直接读 `agent.config.stt.model`，而 `switch_stt` 成功时**有意不改写
    config**（config 是"默认"，`ServiceState` 是"当前"）⇒ 热切换后端点测的是
    bootstrap 模型，与实际在跑的后端无关。不要把这里改回"config 权威"。
    """
    if not provider and not model:
        agent = request.app.get("agent")
        if agent is None:
            return ("", ""), web.json_response(
                rest_contract.ApiFail(error="agent 未注入").wire_dict(), status=500)
        if service == "llm":
            model_id = agent.llm.model
        elif service == "stt":
            model_id = _runtime_model_or_default(agent.stt, agent.config.stt.model)
        else:
            model_id = _runtime_model_or_default(agent.tts, agent.config.tts.model)
        try:
            provider, model = model_id.split("/", 1)
        except ValueError:
            return ("", ""), web.json_response(
                rest_contract.ApiFail(error=f"当前模型格式无效: {model_id}").wire_dict())
    elif not provider:
        provider = "custom"
    return (provider, model), None


def _runtime_model_or_default(backend: Any, default_model: str) -> str:
    """运行期句柄自述的模型；句柄不存在（未连接）或自述为空时回退配置默认值。"""
    runtime_model = getattr(backend, "model", "") or ""
    return runtime_model or default_model


def _create_test_backend(
    service: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
) -> tuple[Any | None, web.Response | None]:
    """解析 custom 参数 + runtime/SSRF 守卫 + 创建待测后端。"""
    runtime = "backend"
    if provider == "custom":
        from core.providers.custom_providers import resolve_model_params
        params = resolve_model_params(service, f"custom/{model}")
        runtime = params.get("runtime", "backend")
        if not base_url:
            base_url = params.get("base_url", "")
        if not api_key:
            api_key = params.get("api_key", "")

    if runtime != "backend":
        return None, _tool_error(
            "PROFILE_RUNTIME_NOT_BACKEND",
            "该 Profile 由 Electron 执行，后端不发起测试", 400)

    from Module._common.ssrf import SSRFError, validate_endpoint
    if base_url:
        try:
            validate_endpoint(base_url)
        except SSRFError as e:
            return None, _tool_error("SSRF_BLOCKED", str(e), 400)

    create_kwargs: dict[str, Any] = {}
    if base_url:
        create_kwargs["base_url"] = base_url
    if api_key:
        create_kwargs["api_key"] = api_key
    if base_url:
        from Module._common.ssrf import build_guarded_http_client
        create_kwargs["http_client_factory"] = (
            lambda b=base_url: build_guarded_http_client(b))

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
        return None, web.json_response(rest_contract.ApiFail(error=str(e)).wire_dict())
    return backend, None


async def _run_test_backend(backend: Any, level: str, service: str,
                            request: web.Request, body: dict) -> web.Response:
    """执行 connectivity/usable 测试并统一超时/异常响应。"""
    try:
        if level == "usable":
            result = await _run_usable_test(backend, service, request, body)
        else:
            result = await asyncio.wait_for(backend.test(), timeout=5.0)
        return web.json_response(rest_contract.ProviderTestResult(**result).wire_dict())
    except asyncio.TimeoutError:
        return web.json_response(
            rest_contract.ProviderTestResult(ok=False, error="测试超时").wire_dict())
    except Exception as e:
        return web.json_response(
            rest_contract.ProviderTestResult(ok=False, error=str(e)).wire_dict())
    finally:
        try:
            await backend.close()
        except Exception:
            pass


async def providers_test_handler(request: web.Request) -> web.Response:
    """测试 provider 连通性（level=connectivity）或可使用性（level=usable）。

    body: {service, provider, model, level, base_url?, api_key?, character?}
    """
    try:
        body = await _json_object(request)
    except web.HTTPRequestEntityTooLarge:
        raise
    except Exception:
        return web.json_response(
            rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)

    service = body.get("service", "")
    if service not in ("llm", "stt", "tts"):
        return web.json_response(
            rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)

    level = body.get("level", "connectivity")
    (provider, model), response = _resolve_test_model(
        request, service, body.get("provider", ""), body.get("model", ""))
    if response is not None:
        return response

    backend, response = _create_test_backend(
        service, provider, model, body.get("base_url", ""), body.get("api_key", ""))
    if response is not None:
        return response
    return await _run_test_backend(backend, level, service, request, body)


def register_provider_routes(app: web.Application) -> None:
    """注册 Provider 管理路由（4 端点）。"""
    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)
