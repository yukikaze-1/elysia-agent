"""业务 API 路由 — Providers, 图片上传, Chat REST, 消息归档, Desktop 注册表/附件。"""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader
from typing import cast

from core.schema.message import Message
from frontend_contract import rest as rest_contract

logger = logging.getLogger(__name__)


# ---- 共享 helper（debug_routes.py 也需要） ----

def _archive_to_client(msgs: list[Message]) -> list[dict]:
    """将 Message 列表转为前端兼容格式（补 parts + _id 字段）。

    Args:
        msgs: Message 对象列表。

    Returns:
        list[dict]: 前端兼容的消息字典列表，每项包含 _id、parts 等字段。
    """
    result = []
    for msg in msgs:
        entry = msg.model_dump(mode="json")
        mongo_id = entry.get("metadata", {}).pop("_mongo_id", None)
        if mongo_id:
            entry["_id"] = mongo_id
        content = entry.get("content", [])
        if isinstance(content, list):
            entry["parts"] = [
                {"content": p["content"]}
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
        elif isinstance(content, str):
            entry["parts"] = [{"content": content}]
        else:
            entry["parts"] = [{"content": ""}]
        result.append(entry)
    return result


def _archive_msg_to_contract(d: dict) -> rest_contract.Message | None:
    """客户端归档消息 dict → 契约 Message 视图（block_index → content_type 归一化）。

    保留 _archive_to_client 的全部输出键（parts/_id/timestamp 等，前端分页游标与渲染依赖），
    仅重命名 block_index 键对齐 session 回放 wire（SessionMessageItem.content_type）。
    单条校验失败返回 None（调用方丢弃该条并告警，避免整页归档加载失败——与 Task 3 M2
    逐条契约化兜底同一策略）。
    """
    wire = dict(d)
    if "block_index" in wire:
        wire["content_type"] = wire.pop("block_index")
    try:
        return rest_contract.Message.model_validate(wire)
    except Exception:
        logger.warning("Failed to contract-validate archive message: %s", d.get("message_id", "?"))
        return None


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


def register_api_routes(app: web.Application) -> None:
    """注册所有业务 API 路由到 aiohttp Application。"""

    # ---- Provider 管理 ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回过滤后的 provider 列表（providers.yaml 单一来源 + env 过滤 + custom）。"""
        from core.input.custom_providers import load as load_custom
        from core.input.provider_registry import build_providers_response
        return web.json_response(
            rest_contract.ProvidersResponse.model_validate(
                build_providers_response(load_custom())).wire_dict())

    async def providers_save_handler(request: web.Request) -> web.Response:
        """保存一个新的自定义 provider。

        Args:
            request: aiohttp Request，JSON body 包含 service 和 entry。

        Returns:
            web.Response: {"ok": True, "data": ...} 或错误信息。
        """
        from core.input.custom_providers import add_provider
        try:
            body = await request.json()
            service = body.get("service", "")
            entry = body.get("entry", {})
        except Exception:
            return web.json_response(rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
        if not entry.get("name") or not entry.get("base_url"):
            return web.json_response(rest_contract.ApiFail(error="缺少 name 或 base_url").wire_dict(), status=400)
        try:
            data = add_provider(service, entry)
            return web.json_response(rest_contract.ApiOk(data=data).wire_dict())
        except Exception as e:
            return web.json_response(rest_contract.ApiFail(error=str(e)).wire_dict(), status=500)

    async def providers_delete_handler(request: web.Request) -> web.Response:
        """删除一个自定义 provider。

        Args:
            request: aiohttp Request，URL 路径参数包含 service 和 name。

        Returns:
            web.Response: {"ok": True, "data": ...} 或错误信息。
        """
        from core.input.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
        try:
            data = remove_provider(service, name)
            return web.json_response(rest_contract.ApiOk(data=data).wire_dict())
        except Exception as e:
            return web.json_response(rest_contract.ApiFail(error=str(e)).wire_dict(), status=500)

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
            return web.json_response(rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)

        service = body.get("service", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)

        level = body.get("level", "connectivity")
        provider = body.get("provider", "")
        model = body.get("model", "")
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")

        # provider+model 均缺省 → 回退该服务当前模型（完整 provider/model，直接 split）
        if not provider and not model:
            agent = request.app.get("agent")
            if agent is None:
                return web.json_response(rest_contract.ApiFail(error="agent 未注入").wire_dict(), status=500)
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
                return web.json_response(rest_contract.ApiFail(error=f"当前模型格式无效: {model_id}").wire_dict())
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
            return web.json_response(rest_contract.ApiFail(error=str(e)).wire_dict())

        try:
            if level == "usable":
                result = await _run_usable_test(backend, service, request, body)
            else:
                result = await asyncio.wait_for(backend.test(), timeout=5.0)
            return web.json_response(rest_contract.ProviderTestResult(**result).wire_dict())
        except asyncio.TimeoutError:
            return web.json_response(rest_contract.ProviderTestResult(ok=False, error="测试超时").wire_dict())
        except Exception as e:
            return web.json_response(rest_contract.ProviderTestResult(ok=False, error=str(e)).wire_dict())
        finally:
            try:
                await backend.close()
            except Exception:
                pass

    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)

    # ---- 图片上传 ----

    from paths import ProjectPath

    _IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()

    async def image_upload_handler(request: web.Request) -> web.Response:
        """处理图片上传请求。

        Args:
            request: aiohttp Request，multipart form data 含图片字段。

        Returns:
            web.Response: {"url": ..., "mime": ..., "size": ...} 或错误信息。
        """
        import uuid as _uuid
        import os as _os

        agent = request.app["agent"]
        cfg = getattr(agent.config, "server", None)
        mb = getattr(cfg, "image_max_mb", None)
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = getattr(cfg, "image_allowed_types", None) or ["image/png", "image/jpeg", "image/gif", "image/webp"]

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response(
                    rest_contract.ImageUploadError(error="未找到图片字段").wire_dict(), status=400)

            # read_chunk() 逐块读取以限制内存——aiohttp 3.x 的 BodyPartReader.read()
            # 不接受位置 size 参数（仅关键字 decode），整块 read() 会把超大 body
            # 全部读入内存（multipart 原始流不受 client_max_size 约束，属 DoS 向量）。
            # 读到超限即中断返回 413，避免继续读完整个 body。
            field = cast(BodyPartReader, field)
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_bytes + 1))
                if len(data) > max_bytes:
                    return web.json_response(
                        rest_contract.ImageUploadError(
                            error=f"图片过大，上限 {max_bytes // 1024 // 1024}MB").wire_dict(),
                        status=413)

            filename = getattr(field, "filename", "") or "image.png"
            ext = _os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp"}
            mime = ext_to_mime.get(ext, "image/png")

            if mime not in allowed:
                return web.json_response(
                    rest_contract.ImageUploadError(error=f"不支持的图片类型: {mime}").wire_dict(),
                    status=415)

            _os.makedirs(str(_IMAGES_DIR), exist_ok=True)
            saved_name = f"{_uuid.uuid4().hex[:12]}{ext}"
            saved_path = _IMAGES_DIR / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/images/{saved_name}"
            return web.json_response(
                rest_contract.ImageUploadResponse(url=url, mime=mime, size=len(data)).wire_dict())
        except Exception as e:
            return web.json_response(rest_contract.ImageUploadError(error=str(e)).wire_dict(), status=500)

    app.router.add_post("/api/images", image_upload_handler)

    # ---- Desktop 工具注册表 + 附件上传（批次 4，BE-042/043） ----

    def _tool_error(code: str, message: str, status: int) -> web.Response:
        """统一错误信封（ProtocolError 形状，与 session_routes._error_response 同款）。

        Args:
            code: 稳定错误码（CALL_NOT_FOUND / INVALID_REQUEST / INTERNAL）。
            message: 公开说明（禁本地路径/参数值，敏感纪律 D-p）。
            status: HTTP 状态码。

        Returns:
            web.Response: ProtocolError 信封 JSON。
        """
        env = rest_contract.ProtocolError(
            code=code, message=message, retryable=False,
            requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
        ).wire_dict()
        return web.json_response(env, status=status)

    def _tool_metadata_to_contract(meta) -> rest_contract.ToolMetadata:
        """Module.tool.manager.ToolMetadata（snake_case dataclass）→ 契约 ToolMetadata（camelCase）。

        Args:
            meta: ToolManager.registry_metadata() 的单条元数据。

        Returns:
            rest_contract.ToolMetadata：wire 形状（camelCase）。
        """
        return rest_contract.ToolMetadata(
            name=meta.name,
            description=meta.description,
            runtime=meta.runtime,
            riskLevel=meta.risk_level,
            permissionRequired=meta.permission_required,
            approvalAuthority=meta.approval_authority,
            version=meta.version,
            inputSchema=meta.input_schema,
            outputSchema=meta.output_schema,
            available=meta.available,
        )

    def _desktop_call_to_status(doc: dict) -> rest_contract.DesktopCallStatus:
        """repo 调用文档 → 契约 DesktopCallStatus（status 用 repo 枚举，spec §9 映射权威）。

        Args:
            doc: DesktopCallRepository.get() 返回的调用文档。

        Returns:
            rest_contract.DesktopCallStatus：查询响应。
        """
        return rest_contract.DesktopCallStatus(
            callId=doc.get("call_id") or "",
            toolName=doc.get("tool_name") or "",
            toolVersion=doc.get("tool_version") or "",
            runtime=doc.get("runtime") or "",
            paramHash=doc.get("arguments_hash") or "",
            status=doc.get("status") or "pending",
            createdAt=doc.get("created_at") or "",
            expiresAt=doc.get("expires_at") or "",
            result=doc.get("result"),
            error=doc.get("error"),
        )

    async def tools_registry_handler(request: web.Request) -> web.Response:
        """GET /api/tools — 工具注册表导出（BE-042）。

        desktopAvailable 取 bridge.is_available()（同步布尔）；desktop 工具的
        available 随其翻转（ToolManager.registry_metadata 派生，BE-040 §3.2）。
        """
        agent = request.app["agent"]
        bridge = agent.desktop_bridge
        desktop_available = bridge.is_available()
        manager = agent.tool_adapter._manager  # noqa: SLF001 — 与 server.py/ws_handler 同款深度访问先例
        metadata = manager.registry_metadata(desktop_available=desktop_available)
        tools = [_tool_metadata_to_contract(m) for m in metadata]
        return web.json_response(
            rest_contract.ToolRegistryResponse(
                tools=tools, desktopAvailable=desktop_available).wire_dict())

    async def tools_call_status_handler(request: web.Request) -> web.Response:
        """GET /api/tools/calls/{callId} — 桌面调用状态查询（BE-043）。

        未知 callId → 404 CALL_NOT_FOUND；超时/过期态如实返回 status=expired，
        不抛 408（CALL_EXPIRED 语义在桥内以 error_type=TIMEOUT 呈现给 Agent，spec §6）。
        """
        call_id = request.match_info.get("callId", "")
        agent = request.app["agent"]
        doc = await agent.desktop_bridge.get_call(call_id)
        if doc is None:
            return _tool_error("CALL_NOT_FOUND", f"调用不存在: {call_id}", 404)
        return web.json_response(_desktop_call_to_status(doc).wire_dict())

    async def desktop_upload_handler(request: web.Request) -> web.Response:
        """POST /api/uploads/desktop/{callId} — desktop 附件字节上传（spec §5.4）。

        持有者校验（防伪附件/孤儿文件，review 修正）：上传前校验
        ① callId 存在（否则 404 CALL_NOT_FOUND）；
        ② 上传连接是 target_connection（否则 403 INVALID_REQUEST）；
        ③ call 处于可收结果状态（pending/accepted/running/offline，终态拒绝——
        防向已结束调用投伪附件）。校验失败不落盘。

        传输方式落地（brief 未定义）：用 `X-Client-Connection: <connection_id>` 头——
        Electron 上传时携带；后端在 WS 握手时已把 conn_id 记到 bridge._connection_id
        并写入 call 的 target_connection（spec §4.2），本 handler 校验头 == target_connection。

        类型/尺寸白名单：沿用 image_max_mb / image_allowed_types + application/octet-stream
        （desktop.file.* 非截图结果）；仿 image_upload_handler 结构 read_chunk 限流反 DoS。
        """
        from core.storage.desktop_call_repo import (
            STATUS_ACCEPTED, STATUS_OFFLINE, STATUS_PENDING, STATUS_RUNNING,
        )

        agent = request.app["agent"]
        bridge = agent.desktop_bridge
        call_id = request.match_info.get("callId", "")

        # ① callId 存在
        doc = await bridge.get_call(call_id)
        if doc is None:
            return _tool_error("CALL_NOT_FOUND", f"调用不存在: {call_id}", 404)

        # ② 上传连接是 target_connection
        conn_id = request.headers.get("X-Client-Connection", "")
        if conn_id != (doc.get("target_connection") or ""):
            return _tool_error("INVALID_REQUEST", "上传连接与调用目标连接不一致", 403)

        # ③ call 非终态（spec §5.4：pending/accepted/running/offline 可收，终态拒绝）
        accepting = {STATUS_PENDING, STATUS_ACCEPTED, STATUS_RUNNING, STATUS_OFFLINE}
        if (doc.get("status") or "") not in accepting:
            return _tool_error("INVALID_REQUEST", "调用已结束，拒绝接收附件", 403)

        # 类型/尺寸约束（对齐 image_upload_handler）
        cfg = getattr(agent.config, "server", None)
        mb = getattr(cfg, "image_max_mb", None)
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = list(getattr(cfg, "image_allowed_types", None)
                       or ["image/png", "image/jpeg", "image/gif", "image/webp"])
        allowed.append("application/octet-stream")

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return _tool_error("INVALID_REQUEST", "未找到附件字段", 400)

            # read_chunk() 逐块读取以限制内存——整块 read() 会把超大 body 全部读入内存
            # （multipart 原始流不受 client_max_size 约束，属 DoS 向量），读到超限即 413。
            field = cast(BodyPartReader, field)
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_bytes + 1))
                if len(data) > max_bytes:
                    return _tool_error(
                        "INVALID_REQUEST",
                        f"附件过大，上限 {max_bytes // 1024 // 1024}MB", 413)

            filename = getattr(field, "filename", "") or f"{call_id}.bin"
            ext = os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp",
                           ".txt": "text/plain", ".json": "application/json"}
            mime = ext_to_mime.get(ext, "application/octet-stream")
            if mime not in allowed:
                return _tool_error("INVALID_REQUEST", f"不支持的附件类型: {mime}", 415)

            upload_dir = ProjectPath.DATA_UPLOADS_DESKTOP.resolve()
            os.makedirs(str(upload_dir), exist_ok=True)
            # 落盘名用 callId（basename 兜底防路径穿越；callId 为服务端 call_ 前缀生成）
            saved_name = f"{os.path.basename(call_id)}{ext}"
            saved_path = upload_dir / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/desktop/{saved_name}"
            return web.json_response({"url": url})
        except Exception as e:
            return _tool_error("INTERNAL", str(e), 500)

    app.router.add_get("/api/tools", tools_registry_handler)
    app.router.add_get("/api/tools/calls/{callId}", tools_call_status_handler)
    app.router.add_post("/api/uploads/desktop/{callId}", desktop_upload_handler)

    # ---- Chat API（curl 测试用） ----

    async def chat_api_handler(request: web.Request) -> web.Response:
        """REST Chat API（curl 测试用）。

        Args:
            request: aiohttp Request，JSON body 包含 content/text 和可选的 user_name。

        Returns:
            web.Response: {"ok": True} 或 400 错误。
        """
        agent = request.app["agent"]
        try:
            body = await request.json()
            content = body.get("content")
            user_name = body.get("user_name", agent.user_name)
            text = body.get("text", "")
        except Exception:
            return web.json_response(rest_contract.ApiFail(error="无效的 JSON 请求体").wire_dict(), status=400)

        if not content and not text:
            return web.json_response(rest_contract.ApiFail(error="缺少 content 或 text").wire_dict(), status=400)

        await agent.process_message(text=text, user_name=user_name, content=content)
        return web.json_response(rest_contract.ApiOk().wire_dict())

    app.router.add_post("/api/chat", chat_api_handler)

    # ---- 消息归档 API ----

    async def messages_handler(request: web.Request) -> web.Response:
        """分页查询消息归档。

        Args:
            request: aiohttp Request，query 参数包含 before、limit。

        Returns:
            web.Response: {"messages": [...], "has_more": ..., "total": ...}。
        """
        agent = request.app["agent"]
        repo = agent.message_repo

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
            limit = max(1, min(limit, 200))
        except ValueError:
            limit = 50

        try:
            messages, has_more = await repo.query(before=before, limit=limit)
            total = await repo.count()
            client_msgs = _archive_to_client(messages)
            # 逐条转契约视图，单条失败丢弃（_archive_msg_to_contract 返回 None），避免整页加载失败
            contract_msgs = [m for m in (_archive_msg_to_contract(d) for d in client_msgs) if m]
            resp = rest_contract.MessagesResponse(
                messages=contract_msgs, has_more=has_more, total=total)
            return web.json_response(resp.wire_dict())
        except Exception as e:
            logger.warning("Failed to query message archive: %s", e)
            return web.json_response(
                rest_contract.MessagesResponse(
                    messages=[], has_more=False, total=0, error=str(e)).wire_dict(),
                status=500,
            )

    app.router.add_get("/api/messages", messages_handler)
