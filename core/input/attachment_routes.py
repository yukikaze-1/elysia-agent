"""附件签名下载 + 签发端点（批次 7 Task 4，spec §3.3）。

- GET /api/attachments/{attachment_id}      — 签名下载：fail-closed 校验序
  （sig 格式 → exp 窗口 → HMAC 验签 → 记录存在且 active）/ fail-open 直读
  （仅 status=active 校验）。成功走 aiohttp FileResponse（原生 Range/206）。
- GET /api/attachments/{attachment_id}/sign — 签发端点：Bearer 由 server.py
  认证中间件保证（本模块不重复校验）；返回 AttachmentSignResponse
  （attachmentId + 短期签名 URL + ISO UTC 过期时间）。

fail-closed 语义（spec §3.4）：AGENT_ACCESS_TOKEN 配置而签名密钥未配置 →
下载/签发一律 403 ATTACHMENT_SIGNATURE_DISABLED（不进入校验序）。
Mongo 离线（repo.get raise StorageUnavailableError）→ 503 STORAGE_UNAVAILABLE。
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from core.agent import Agent

from paths import ProjectPath
from core.application import (
    AttachmentSigningService,
    signing_service_for_agent,
)
from core.schema.adapters import attachment_resolver
from core.schema.types import AttachmentId
from core.schema.utils import normalize_attachment_mime
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
from frontend_contract import rest as rest_contract
from frontend_contract.rest import ProtocolError

logger = logging.getLogger(__name__)

def _error_response(code: str, message: str, status: int = 400,
                    retryable: bool = False) -> web.Response:
    """统一错误信封（ProtocolError 形状，与 session_routes 同款）。

    Args:
        code: 稳定错误码（INVALID_SIGNATURE / SIGNATURE_EXPIRED /
            ATTACHMENT_NOT_FOUND / ATTACHMENT_SIGNATURE_DISABLED / STORAGE_UNAVAILABLE）。
        message: 公开说明（不泄露附件路径/密钥细节）。
        status: HTTP 状态码。
        retryable: 是否可重试（STORAGE_UNAVAILABLE=true——离线恢复后客户端可重试）。

    Returns:
        web.Response: ProtocolError 信封 JSON。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)


async def _download_handler(request: web.Request) -> web.StreamResponse:
    """GET /api/attachments/{attachment_id} — 签名下载。

    校验序（spec §3.3，D3 重排——签名校验前置，防存在性枚举）：
      ① sig 形态 / ③ exp 窗口 / ④ HMAC 验签 —— 三步都在用例层
         `core.application.AttachmentSigningService.verify`（可脱离 aiohttp 单测），
         本处只做错误码 → 403 的映射；
      ② repo.get 存在且 status=active（否则 404 ATTACHMENT_NOT_FOUND；
         Mongo 离线 → 503 STORAGE_UNAVAILABLE）。
    fail-open（token 空）→ ①③④ 恒通过（verify 返回 ok），仍需 ② 校验。
    成功 → 先检查落盘文件存在（记录在、文件删 → 404 不预热）→ FileResponse
    （原生 Range/206）+ AttachmentFileResolver 再预热（spec §4.5）。

    Args:
        request: aiohttp Request（?sig=...&exp=...）。

    Returns:
        web.StreamResponse: FileResponse（文件/206）或错误信封（Response）。
    """
    agent = request.app["agent"]
    signing = signing_service_for_agent(agent)
    attachment_id = request.match_info.get("attachment_id", "")

    # fail-closed 早退：token 配 + sig 空 → 拒绝（不进入校验序，spec §3.4）
    if signing.disabled:
        outcome = signing.disabled_outcome()
        return _error_response(outcome.code, outcome.message, 403)

    # 校验序（形态 → 时间窗口 → HMAC）在用例层，本处只做错误码 → 403 的映射。
    # fail-open（未配 token）时 outcome 恒 ok，仍需下面的记录存在性校验。
    outcome = signing.verify(
        attachment_id,
        request.query.get("sig", "") or "",
        request.query.get("exp", "") or "",
    )
    if not outcome:
        return _error_response(outcome.code, outcome.message, 403)

    # ② 记录存在且 active（Mongo 离线 → 503）
    try:
        doc = await agent.attachment_repo.get(AttachmentId(attachment_id))
    except StorageUnavailableError:
        return _error_response("STORAGE_UNAVAILABLE", "附件存储不可用", 503,
                               retryable=True)
    if doc is None or (doc.get("status") or "") != STATUS_ACTIVE:
        return _error_response("ATTACHMENT_NOT_FOUND", "附件不存在", 404)

    kind = doc.get("kind", "")
    stored_name = doc.get("stored_name", "")
    # BE-S3-RUN-P0-02：下载 Content-Type 过规范化（存量 kind=audio+video/webm →
    # audio/webm、WAV 探测别名 → audio/wav；新记录已写路径规范化，幂等）（spec §4.2）
    mime = normalize_attachment_mime(kind, doc.get("mime", "application/octet-stream"))

    # 先检查落盘文件存在（记录存在但文件被删——GC M8 场景）再 register：文件缺失 → 404
    # 且不预热进程内映射，避免 register 无效条目（review Minor 10）。
    local_path = ProjectPath.DATA_UPLOADS.resolve() / kind / stored_name
    if not local_path.is_file():
        return _error_response("ATTACHMENT_NOT_FOUND", "附件文件不存在", 404)
    # 文件存在 → 进程内映射再预热（spec §4.5）
    attachment_resolver.register(attachment_id, kind, stored_name)

    return web.FileResponse(local_path, headers={"Content-Type": mime})


async def _sign_handler(request: web.Request) -> web.Response:
    """GET /api/attachments/{attachment_id}/sign — 签发短期签名 URL。

    Bearer 由 server.py 认证中间件保证（本端点未豁免）；handler 只做业务校验：
    fail-closed（token 配 + sig 空）→ 403 ATTACHMENT_SIGNATURE_DISABLED；
    记录存在且 active（否则 404 ATTACHMENT_NOT_FOUND；Mongo 离线 → 503）→
    AttachmentSigningService.sign → expires_unix 转 ISO UTC → 响应 →
    AttachmentFileResolver 预热。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: AttachmentSignResponse 或错误信封。
    """
    agent = request.app["agent"]
    signing: AttachmentSigningService = signing_service_for_agent(agent)
    attachment_id = request.match_info.get("attachment_id", "")

    # fail-closed：token 配 + sig 空 → 拒绝（spec §3.4）。
    # 注意 fail-open 的不对称（**本批保持原行为，不改**）：token 空 + sig 配时，
    # 签发仍然可用，而下载根本不校验签名——即签出来的 URL 此刻并无保护作用。
    # 这是"本地开发免签名"的既有语义，属**候选加固项**而非本重构该动的地方。
    if signing.disabled:
        outcome = signing.disabled_outcome()
        return _error_response(outcome.code, outcome.message, 403)

    # 记录存在且 active
    try:
        doc = await agent.attachment_repo.get(AttachmentId(attachment_id))
    except StorageUnavailableError:
        return _error_response("STORAGE_UNAVAILABLE", "附件存储不可用", 503,
                               retryable=True)
    if doc is None or (doc.get("status") or "") != STATUS_ACTIVE:
        return _error_response("ATTACHMENT_NOT_FOUND", "附件不存在", 404)

    url, expires_unix = signing.sign(attachment_id)
    # expires_unix（int）→ ISO UTC（附件契约要求的 wire 格式）
    expires_at = datetime.fromtimestamp(expires_unix, tz=timezone.utc).isoformat()

    # 进程内映射预热（spec §4.5：上传同款 register）
    attachment_resolver.register(
        attachment_id, doc.get("kind", ""), doc.get("stored_name", ""))

    return web.json_response(
        rest_contract.AttachmentSignResponse(
            attachmentId=attachment_id, url=url, expiresAt=expires_at).wire_dict())


def register_attachment_routes(app: web.Application) -> None:
    """注册附件下载 + 签发路由到 aiohttp Application。

    Args:
        app: aiohttp Application。
    """
    app.router.add_get("/api/attachments/{attachment_id}", _download_handler)
    app.router.add_get("/api/attachments/{attachment_id}/sign", _sign_handler)
