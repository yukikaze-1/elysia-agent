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

import hashlib
import hmac
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from core.agent import Agent

from paths import ProjectPath
from core.input.api_routes import _sign_url
from core.schema.adapters import attachment_resolver
from core.schema.types import AttachmentId
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
from frontend_contract import rest as rest_contract
from frontend_contract.rest import ProtocolError

logger = logging.getLogger(__name__)

# sig 合法格式：sha256 hexdigest 为 64 位小写 hex；上界 128 防超长串拖慢 compare_digest（DoS 防护）
_SIG_RE = re.compile(r"^[0-9a-f]{1,128}$")

# 签名有效期（秒，与 _sign_url 一致：24h）——exp 必须落在 [now, now+86400]
_SIG_TTL = 86400


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


def _server_config(agent: "Agent"):
    """读取 agent.config.server（容错 getattr，与 api_routes 既有取法一致）。

    Args:
        agent: Agent 组合根。

    Returns:
        ServerConfig 实例或 None。
    """
    return getattr(getattr(agent, "config", None), "server", None)


def _sig_disabled(agent: "Agent") -> bool:
    """fail-closed 早退判定：AGENT_ACCESS_TOKEN 配置 + 签名密钥未配置。

    Args:
        agent: Agent 组合根（读 config.server.access_token / attachment_sig_secret）。

    Returns:
        bool: True = 签名被禁用——下载/签发必须拒绝（fail-closed）。
    """
    cfg = _server_config(agent)
    access_token = getattr(cfg, "access_token", "") or ""
    sig_secret = getattr(cfg, "attachment_sig_secret", "") or ""
    return bool(access_token) and not sig_secret


async def _download_handler(request: web.Request) -> web.StreamResponse:
    """GET /api/attachments/{attachment_id} — 签名下载。

    校验序（spec §3.3，D3 重排——签名校验前置，防存在性枚举）：
      ① sig 存在且为合法 hex（缺失/格式不符 → 403 INVALID_SIGNATURE）；
      ③ exp 为整数且在 [now, now+86400]（否则 403 SIGNATURE_EXPIRED）；
      ④ hmac.compare_digest 验签（不匹配 → 403 INVALID_SIGNATURE）；
      ② repo.get 存在且 status=active（否则 404 ATTACHMENT_NOT_FOUND；
         Mongo 离线 → 503 STORAGE_UNAVAILABLE）。
    fail-open（token 空）→ 跳过 ①③④，仅 ② status=active 校验。
    成功 → 先检查落盘文件存在（记录在、文件删 → 404 不预热）→ FileResponse
    （原生 Range/206）+ AttachmentFileResolver 再预热（spec §4.5）。

    Args:
        request: aiohttp Request（?sig=...&exp=...）。

    Returns:
        web.StreamResponse: FileResponse（文件/206）或错误信封（Response）。
    """
    agent = request.app["agent"]
    cfg = _server_config(agent)
    access_token = getattr(cfg, "access_token", "") or ""
    sig_secret = getattr(cfg, "attachment_sig_secret", "") or ""
    attachment_id = request.match_info.get("attachment_id", "")

    # fail-closed 早退：token 配 + sig 空 → 拒绝（不进入校验序，spec §3.4）
    if access_token and not sig_secret:
        return _error_response(
            "ATTACHMENT_SIGNATURE_DISABLED", "附件签名未配置", 403)

    # fail-open（token 空）→ 跳过签名校验，仅 status=active 校验
    verify_sig = bool(access_token)

    if verify_sig:
        # ① sig 存在且合法 hex
        sig = request.query.get("sig", "") or ""
        if not _SIG_RE.fullmatch(sig):
            return _error_response("INVALID_SIGNATURE", "缺少或非法签名", 403)
        # 预解析 exp（非整数先记 -1，③ 范围判定兜底）
        try:
            exp = int(request.query.get("exp", "") or "")
        except ValueError:
            exp = -1
        # ③ exp 窗口 [now, now+86400]
        now = int(time.time())
        if exp < now or exp > now + _SIG_TTL:
            return _error_response("SIGNATURE_EXPIRED", "签名已过期", 403)
        # ④ HMAC 验签（compare_digest 防时序侧信道）
        expected = hmac.new(sig_secret.encode(),
                            f"{attachment_id}:{exp}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return _error_response("INVALID_SIGNATURE", "签名校验失败", 403)

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
    mime = doc.get("mime", "application/octet-stream")

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
    _sign_url 签发 → expires_unix 转 ISO UTC → AttachmentSignResponse →
    AttachmentFileResolver 预热。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: AttachmentSignResponse 或错误信封。
    """
    agent = request.app["agent"]
    cfg = _server_config(agent)
    access_token = getattr(cfg, "access_token", "") or ""
    sig_secret = getattr(cfg, "attachment_sig_secret", "") or ""
    attachment_id = request.match_info.get("attachment_id", "")

    # fail-closed：token 配 + sig 空 → 拒绝（spec §3.4）
    if access_token and not sig_secret:
        return _error_response(
            "ATTACHMENT_SIGNATURE_DISABLED", "附件签名未配置", 403)

    # 记录存在且 active
    try:
        doc = await agent.attachment_repo.get(AttachmentId(attachment_id))
    except StorageUnavailableError:
        return _error_response("STORAGE_UNAVAILABLE", "附件存储不可用", 503,
                               retryable=True)
    if doc is None or (doc.get("status") or "") != STATUS_ACTIVE:
        return _error_response("ATTACHMENT_NOT_FOUND", "附件不存在", 404)

    url, expires_unix = _sign_url(sig_secret, attachment_id)
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
