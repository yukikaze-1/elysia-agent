"""上传/表情 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from paths import ProjectPath

from core.application import AttachmentSigningService
from core.input.errors import _protocol_error_response
from core.schema.adapters import attachment_resolver
from core.schema.utils import normalize_attachment_mime, now_iso
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
from core.utils import new_attachment_id
from frontend_contract import rest as rest_contract
from frontend_contract.rest import UploadKind

logger = logging.getLogger(__name__)


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
    cfg = agent.config.server
    mb = cfg.image_max_mb
    max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
    allowed = cfg.image_allowed_types or ["image/png", "image/jpeg", "image/gif", "image/webp"]

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
        # D5/P2-03：未知扩展拒绝（不默认为 image/png）——防 HTML/SVG/JS/EXE
        # 以图片名义上传后经静态挂载被同源执行
        if ext not in ext_to_mime:
            return web.json_response(
                rest_contract.ImageUploadError(
                    error=f"不支持的图片扩展名: {ext or '(无扩展名)'}").wire_dict(),
                status=415)
        mime = ext_to_mime[ext]

        if mime not in allowed:
            return web.json_response(
                rest_contract.ImageUploadError(error=f"不支持的图片类型: {mime}").wire_dict(),
                status=415)

        # D5/P2-03：魔数校验——内容实际类型须与扩展一致（polyglot/伪装拒绝）
        detected = filetype.guess(bytes(data)) if data else None
        if detected is None or detected.mime != mime:
            return web.json_response(
                rest_contract.ImageUploadError(
                    error="图片内容与扩展名不符（已拒绝）").wire_dict(),
                status=415)

        _os.makedirs(str(_IMAGES_DIR), exist_ok=True)
        # D5/P2-03：服务端生成安全文件名（扩展名由白名单派生，不信任用户输入）
        saved_name = f"{_uuid.uuid4().hex[:12]}{ext}"
        saved_path = _IMAGES_DIR / saved_name
        saved_path.write_bytes(data)

        url = f"/uploads/images/{saved_name}"
        return web.json_response(
            rest_contract.ImageUploadResponse(url=url, mime=mime, size=len(data)).wire_dict())
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception as e:
        # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
        logger.warning("图片上传失败: %s", e)
        return web.json_response(rest_contract.ImageUploadError(error="图片上传失败，请稍后重试").wire_dict(), status=500)


# ---- 通用上传（批次 7，spec §4.1，BE-070） ----


class _PersistAbort(Exception):
    """持久化中止信号：携带公开错误信封字段（str(e) 只进日志）。"""

    def __init__(self, code: str, message: str, status: int,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


@dataclass
class _UploadParts:
    """multipart 收集结果：kind 栈 + 计数 + 文件内容。"""

    files: list[tuple[str, bytes, str | None]]
    kind_count: int
    pending_kinds: list[str]


async def _collect_upload_parts(
    request: web.Request,
    cms: int,
    max_read: int,
) -> tuple[_UploadParts, web.Response | None]:
    """读取 multipart 的 kind/files 字段（邻接配对 + 聚合/单文件大小限制）。"""
    pending_kinds: list[str] = []
    kind_count = 0
    files: list[tuple[str, bytes, str | None]] = []
    total_bytes = 0

    try:
        reader = await request.multipart()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
            "INVALID_REQUEST", "无效的 multipart 请求", 400)

    while True:
        try:
            field = await reader.next()
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
                "INVALID_REQUEST", "multipart 解析失败", 400)
        if field is None:
            break
        field = cast(BodyPartReader, field)
        name = field.name or ""
        if name == "kind":
            raw = await field.read_chunk(65)
            if len(raw) > 64:
                return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
                    "INVALID_REQUEST", "kind 字段超长", 400)
            if isinstance(raw, (bytes, bytearray)):
                decoded = bytes(raw).decode("utf-8", errors="replace").strip()
            else:
                decoded = str(raw or "").strip()
            if pending_kinds:
                return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
                    "CONTRACT_VALIDATION", "kind 未紧邻文件", 400)
            pending_kinds.append(decoded)
            kind_count += 1
        elif name.startswith("files"):
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_read + 1))
                if len(data) > max_read:
                    return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
                        "REQUEST_TOO_LARGE", "单个文件过大", 413)
            total_bytes += len(data)
            if cms and total_bytes > cms:
                return _UploadParts(files, kind_count, pending_kinds), _protocol_error_response(
                    "REQUEST_TOO_LARGE", "整体上传超过聚合上限", 413)
            hint = pending_kinds.pop() if pending_kinds else None
            files.append((getattr(field, "filename", "") or "attachment",
                          bytes(data), hint))
        # 其他字段忽略

    return _UploadParts(files, kind_count, pending_kinds), None


def _normalize_upload_kind_hints(
    files: list[tuple[str, bytes, str | None]],
    kind_count: int,
    pending_kinds: list[str],
) -> tuple[list[tuple[str, bytes, str | None]], web.Response | None]:
    """步骤 0.x：文件数/kind 数/悬空/单 kind 特判/枚举白名单。"""
    if len(files) > 10:
        return [], _protocol_error_response(
            "TOO_MANY_FILES", "单次上传文件数超限（≤10）", 400)
    if kind_count > 100:
        return [], _protocol_error_response(
            "CONTRACT_VALIDATION", "kind 字段数量超限", 400)
    if pending_kinds:
        return [], _protocol_error_response(
            "CONTRACT_VALIDATION", "kind 后缺少对应文件", 400)
    if kind_count == 1 and files:
        single_kind = next(h for h in (f[2] for f in files) if h is not None)
        files = [(name, data, single_kind) for name, data, _ in files]
    for _, _, hint in files:
        if hint is not None and hint not in _UPLOAD_KIND_VALUES:
            return [], _protocol_error_response(
                "CONTRACT_VALIDATION", f"未知 kind: {hint}", 400)
    return files, None


def _validate_upload_file(
    filename_raw: str,
    data: bytes,
    hint: str | None,
) -> tuple[dict, web.Response | None]:
    """阶段 A 单文件校验 + 预计算写盘信息/记录文档。"""
    if hint and len(data) > _KIND_LIMITS[hint]:
        return {}, _protocol_error_response(
            "REQUEST_TOO_LARGE", f"文件过大，超过 {hint} 上限", 413)

    ext_raw = os.path.splitext(filename_raw)[1].lower()
    ext = ext_raw.lstrip(".")
    ft = filetype.guess(data)
    if ft is None:
        text_mime = _TEXT_EXT_TO_MIME.get(ext_raw)
        if text_mime is None:
            return {}, _protocol_error_response(
                "UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return {}, _protocol_error_response(
                "UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
        mime = text_mime
    else:
        mime = ft.mime
        if mime != "video/webm" and mime not in _MIME_KIND:
            return {}, _protocol_error_response(
                "UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)

    if ext not in _MIME_EXTS.get(mime, set()):
        return {}, _protocol_error_response(
            "UNSUPPORTED_MEDIA_TYPE", "扩展名与文件内容不一致", 415)

    if mime == "video/webm":
        final_kind = "audio" if hint == "audio" else "video"
    else:
        final_kind = _MIME_KIND[mime]
    if len(data) > _KIND_LIMITS[final_kind]:
        return {}, _protocol_error_response(
            "REQUEST_TOO_LARGE", f"文件过大，超过 {final_kind} 上限", 413)
    mime = normalize_attachment_mime(final_kind, mime)

    filename = re.sub(r"[\/]", "", filename_raw)
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    filename = filename[:255] or "attachment"

    stored_name = f"{uuid.uuid4().hex[:16]}.{ext}"
    stored_path = ProjectPath.DATA_UPLOADS.resolve() / final_kind / stored_name
    attachment_id = new_attachment_id()
    doc = {
        "attachment_id": attachment_id,
        "kind": final_kind,
        "mime": mime,
        "size": len(data),
        "filename": filename,
        "stored_name": stored_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "ref_count": 0,
        "refs": [],
        "status": STATUS_ACTIVE,
        "created_at": now_iso(),
    }
    return {
        "data": data,
        "stored_path": stored_path,
        "stored_name": stored_name,
        "final_kind": final_kind,
        "mime": mime,
        "attachment_id": attachment_id,
        "doc": doc,
    }, None


def _validate_upload_batch(
    files: list[tuple[str, bytes, str | None]],
) -> tuple[list[dict], web.Response | None]:
    """阶段 A 批校验：任一文件失败立即 4xx，不写盘不建记录。"""
    validated: list[dict] = []
    for filename_raw, data, hint in files:
        item, response = _validate_upload_file(filename_raw, data, hint)
        if response is not None:
            return [], response
        validated.append(item)
    return validated, None


async def _persist_upload_files(
    agent: Any,
    validated: list[dict],
    sig_secret: str,
    access_token_cfg: str,
) -> tuple[list[rest_contract.AttachmentInfo], web.Response | None]:
    """阶段 B：写盘 + 建记录 + 注册映射；任一失败清理本轮副作用。"""
    repo = agent.attachment_repo
    results: list[rest_contract.AttachmentInfo] = []
    written: list[Path] = []
    created: list[AttachmentId] = []

    async def _cleanup() -> None:
        for stored_path in written:
            stored_path.unlink(missing_ok=True)
        for aid in created:
            attachment_resolver.unregister(aid)
            try:
                await repo.delete_record(aid)
            except Exception as ce:
                logger.warning("失败清理删除附件记录 %s 失败: %s", aid, ce)

    try:
        for it in validated:
            stored_path = it["stored_path"]
            os.makedirs(str(stored_path.parent), exist_ok=True)
            written.append(stored_path)
            stored_path.write_bytes(it["data"])
            try:
                await repo.create(it["doc"])
            except StorageUnavailableError:
                raise _PersistAbort(
                    "STORAGE_UNAVAILABLE", "附件存储不可用", 503, retryable=True)
            except Exception as e:
                logger.warning("附件记录写入失败: %s", e)
                raise _PersistAbort("INTERNAL", "附件记录写入失败", 500)
            created.append(it["attachment_id"])
            attachment_resolver.register(
                it["attachment_id"], it["final_kind"], it["stored_name"])
            signed_url = None
            if not ((not sig_secret) and access_token_cfg):
                signed_url, _ = _sign_url(sig_secret, it["attachment_id"])
            results.append(rest_contract.AttachmentInfo(
                attachmentId=it["attachment_id"],
                kind=it["final_kind"],
                mime=it["mime"],
                size=len(it["data"]),
                url=f"/api/attachments/{it['attachment_id']}",
                signedUrl=signed_url,
                filename=it["doc"]["filename"],
            ))
    except _PersistAbort as e:
        await _cleanup()
        return [], _protocol_error_response(
            e.code, e.message, e.status, retryable=e.retryable)
    except Exception as e:
        logger.warning("附件持久化失败，已清理本轮写入: %s", e)
        await _cleanup()
        return [], _protocol_error_response("INTERNAL", "附件持久化失败", 500)

    return results, None


async def upload_handler(request: web.Request) -> web.Response:
    """POST /api/uploads — 通用附件上传（多文件 + kind 意图）。

    校验顺序：multipart 收集（kind 邻接配对 + 大小限制）→ kind hint 归一 →
    阶段 A 全量校验（不写盘）→ 阶段 B 统一写盘/建记录/注册（失败清理）。
    """
    agent = request.app["agent"]
    cfg = agent.config.server
    sig_secret = cfg.attachment_sig_secret or ""
    access_token_cfg = cfg.access_token or ""

    cms = cfg.max_request_bytes
    if cms is None:
        cms = 64 * 1024 * 1024
    max_read = max(_KIND_LIMITS.values())

    parts, response = await _collect_upload_parts(request, cms, max_read)
    if response is not None:
        return response

    files, response = _normalize_upload_kind_hints(
        parts.files, parts.kind_count, parts.pending_kinds)
    if response is not None:
        return response

    validated, response = _validate_upload_batch(files)
    if response is not None:
        return response

    results, response = await _persist_upload_files(
        agent, validated, sig_secret, access_token_cfg)
    if response is not None:
        return response

    return web.json_response(
        rest_contract.AttachmentUploadResponse(attachments=results).wire_dict())


# ---- 内置表情（批次 7 Task 6，BE-071） ----

async def stickers_handler(request: web.Request) -> web.Response:
    """GET /api/stickers — 内置表情列表（契约单源）。

    sticker_id = 文件名去扩展名；src 走公开静态路径 /static/stickers/{id}.png
    （表情是公共资源，不落 attachments 集合）。列表按 id 排序保证稳定顺序。
    """
    from core.input.sticker_registry import load_stickers
    stickers = [
        rest_contract.StickerEntry(
            stickerId=sticker_id,
            src=f"/static/stickers/{sticker_id}.png",
        )
        for sticker_id in sorted(load_stickers())
    ]
    return web.json_response(
        rest_contract.StickerListResponse(stickers=stickers).wire_dict())



from aiohttp.multipart import BodyPartReader
import uuid
import filetype
import hashlib
import os
from pathlib import Path
import re
from typing import Any, cast
from core.schema.types import AttachmentId

# kind 白名单单源（D3-B 枚举派生）：值集合（wire str 成员判断，跨 3.11/3.12 无差异）。
_UPLOAD_KIND_VALUES = frozenset(m.value for m in UploadKind)

_KIND_LIMITS = {
    "image": 10 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}

_MIME_KIND = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video",
    "audio/x-wav": "audio",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "application/pdf": "file",
    "application/zip": "file",
    "text/plain": "file",
    "text/markdown": "file",
    "application/json": "file",
}

_MIME_EXTS = {
    "image/png": {"png"},
    "image/jpeg": {"jpg", "jpeg"},
    "image/gif": {"gif"},
    "image/webp": {"webp"},
    "video/mp4": {"mp4"},
    "video/webm": {"webm"},
    "audio/x-wav": {"wav"},
    "audio/mpeg": {"mp3"},
    "audio/ogg": {"ogg"},
    "application/pdf": {"pdf"},
    "application/zip": {"zip"},
    "text/plain": {"txt"},
    "text/markdown": {"md"},
    "application/json": {"json"},
}

_TEXT_EXT_TO_MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}

def _sign_url(secret: str, attachment_id: str) -> tuple[str, int]:
    """HMAC 签名 URL（spec §3.3）。返回 (url, expires_unix)。

    薄壳：规则单源在 `core.application.AttachmentSigningService`（TTL / 算法 /
    URL 形状），签发与验证共用同一常量——避免了"签发用 24h、验证用另一个数"
    这类只在生产暴露的错配。保留本函数名是因为上传路径与既有测试都以它为准。
    """
    return AttachmentSigningService(sig_secret=secret).sign(attachment_id)

_IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()


def register_upload_routes(app: web.Application) -> None:
    """注册上传/表情路由（3 端点）。"""
    app.router.add_post("/api/images", image_upload_handler)
    app.router.add_post("/api/uploads", upload_handler)
    app.router.add_get("/api/stickers", stickers_handler)
