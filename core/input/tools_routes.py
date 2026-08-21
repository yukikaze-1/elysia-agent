"""Desktop 工具 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging
from typing import Any

from aiohttp import web
from paths import ProjectPath

from core.desktop.bridge import DesktopToolBridge
from core.storage.desktop_call_repo import DesktopCallRepository
from core.utils import new_attachment_id
from frontend_contract import rest as rest_contract
from core.input._common import _tool_error

logger = logging.getLogger(__name__)


def _tool_metadata_to_contract(meta: Any) -> rest_contract.ToolMetadata:
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

    result 为 REST 边界转换：repo 存 snake 键（output/attachments/execution_summary，
    attachments[].mime_type），wire 归一 camelCase（executionSummary/mimeType）——
    repo 存储不改，仅出站转换。

    Args:
        doc: DesktopCallRepository.get() 返回的调用文档。

    Returns:
        rest_contract.DesktopCallStatus：查询响应。
    """
    result = doc.get("result")
    if isinstance(result, dict):
        result = dict(result)
        if "execution_summary" in result:
            result["executionSummary"] = result.pop("execution_summary")
        attachments = result.get("attachments")
        if isinstance(attachments, list):
            result["attachments"] = [
                {"url": a.get("url"),
                 "mimeType": a.get("mime_type", a.get("mimeType")),
                 "label": a.get("label")}
                for a in attachments if isinstance(a, dict)
            ]
    return rest_contract.DesktopCallStatus(
        callId=doc.get("call_id") or "",
        toolName=doc.get("tool_name") or "",
        toolVersion=doc.get("tool_version") or "",
        runtime=doc.get("runtime") or "",
        paramHash=doc.get("arguments_hash") or "",
        status=RepoCallStatus(doc.get("status") or "pending"),
        createdAt=doc.get("created_at") or "",
        expiresAt=doc.get("expires_at") or "",
        result=result,
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

    类型/尺寸白名单：沿用 image_max_mb / image_allowed_types，并放行
    application/octet-stream / text/plain / application/json（desktop.file.read
    >64 KiB 文本附件回传）；仿 image_upload_handler 结构 read_chunk 限流反 DoS。
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

    # 类型/尺寸约束：图片（对齐 image_upload_handler）+ 桌面文本附件（desktop.file.read
    # >64 KiB 走附件回传）+ 安全扩展的 octet-stream 兜底；危险活动内容扩展在下方显式拒绝。
    cfg = agent.config.server
    mb = cfg.image_max_mb
    max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
    allowed = list(cfg.image_allowed_types
                   or ["image/png", "image/jpeg", "image/gif", "image/webp"])
    allowed.append("application/octet-stream")
    allowed.append("text/plain")
    allowed.append("application/json")

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
        # D5/P2-03：危险活动内容扩展一律拒绝（HTML/SVG/JS/可执行），
        # 防经静态挂载同源执行；octet-stream 只保留安全扩展
        _dangerous_ext = {".html", ".htm", ".svg", ".xhtml", ".xml", ".js",
                          ".mjs", ".exe", ".sh", ".bat", ".cmd", ".php", ".jsp"}
        if ext in _dangerous_ext:
            return _tool_error("INVALID_REQUEST", "不允许的附件类型", 415)
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
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception as e:
        # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
        logger.warning("desktop 附件上传失败: %s", e)
        return _tool_error("INTERNAL", "上传失败，请稍后重试", 500)



from aiohttp.multipart import BodyPartReader
from typing import Any, TYPE_CHECKING, cast
from frontend_contract.rest import RepoCallStatus
import os

def register_tools_routes(app: web.Application) -> None:
    """注册 Desktop 工具路由（3 端点）。"""
    app.router.add_get("/api/tools", tools_registry_handler)
    app.router.add_get("/api/tools/calls/{callId}", tools_call_status_handler)
    app.router.add_post("/api/uploads/desktop/{callId}", desktop_upload_handler)
