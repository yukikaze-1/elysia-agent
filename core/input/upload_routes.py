"""上传/表情 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging
import time
from typing import Any

from aiohttp import web
from paths import ProjectPath

from core.input.errors import _protocol_error_response
from core.schema.adapters import attachment_resolver
from core.schema.utils import now_iso
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
from core.utils import new_attachment_id
from frontend_contract import rest as rest_contract
from frontend_contract.rest import UploadKind
from core.input._common import _tool_error

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

async def upload_handler(request: web.Request) -> web.Response:
    """POST /api/uploads — 通用附件上传（多文件 + kind 意图）。

    multipart 字段：files[]（多个）+ kind（可选，客户端意图**不信任**）。
    校验顺序（spec §4.1）：kind 枚举预检（Task 14：read_chunk(65) 限长 64，超长即拒
    不读入内存）→ 数量 ≤10 → 每文件 filetype 魔数白名单（文本类 txt/md/json
    无魔数 → 扩展名白名单 + UTF-8 sanity，I6）→ 扩展名一致性 → kind 归位
    （webm 以客户端 kind tiebreaker）+ 归位后大小复核 → 写盘
    data/uploads/{kind}/{uuid}.{ext} + sha256 + attachments 落记录
    （fail-closed 离线 503，失败删已落盘文件不留孤儿）→ AttachmentFileResolver.register。
    Task 14 两阶段：先全部校验（阶段 A，失败不写盘）→ 再统一写盘/建记录/注册
    （阶段 B，任一失败清理本轮已写文件 + 已建记录 + 已注册映射，不留孤儿）。
    整体聚合上限 64MB：有 Content-Length 由 payload 中间件预检（server.py
    _make_payload_middleware），chunked（无 Content-Length）由本 handler 的
    total_bytes 累计强制（Critical #4）。
    """
    from core.schema.adapters import attachment_resolver
    from core.schema.utils import now_iso
    from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
    from core.utils.message_id import new_attachment_id

    agent = request.app["agent"]
    cfg = agent.config.server
    sig_secret = cfg.attachment_sig_secret or ""
    access_token_cfg = cfg.access_token or ""
    repo = agent.attachment_repo
    # 聚合上限（spec §4.1 Critical #4 强制）：chunked（无 Content-Length）时中间件
    # Content-Length 预检失效 → handler 内累计字节强制整体 ≤ cms。取 agent 配置同源
    # （create_app 的 client_max_size 也是它，缺省 64MB）。is not None 语义：0 → 0
    # 不限（与 server cms 同步），None/缺省 → 64MB（falsy 回落会误吞 0）。
    cms = cfg.max_request_bytes
    if cms is None:
        cms = 64 * 1024 * 1024

    # 收集 multipart 字段（kind 文本 + files[] 文件；前端每文件一组 kind+files 邻接配对）
    pending_kinds: list[str] = []   # 未消费 kind 栈（邻接配对：最近一个 kind 作用于其后首个文件）
    kind_count = 0                  # 批内 kind part 总数（单 kind 特判判定）
    files: list[tuple[str, bytes, str | None]] = []   # (原始文件名, 内容, kind_hint | None)
    # 收集阶段 read_chunk 上限取各 kind 上限最大值（50MB）——kind 缺省分类前防 DoS
    max_read = max(_KIND_LIMITS.values())
    total_bytes = 0  # 聚合字节累计（多文件跨 read_chunk 相加，超 cms 即中断）
    try:
        reader = await request.multipart()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return _protocol_error_response("INVALID_REQUEST", "无效的 multipart 请求", 400)
    while True:
        try:
            field = await reader.next()
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return _protocol_error_response("INVALID_REQUEST", "multipart 解析失败", 400)
        if field is None:
            break
        field = cast(BodyPartReader, field)
        name = field.name or ""
        if name == "kind":
            # kind 限长（Task 14）：read_chunk(65) 读上限 65 字节才能检出 >64 超长
            # （read() 整读无上限）——不读入超长字段内存即拒，限长分支在未知 kind
            # 预检之前。
            raw = await field.read_chunk(65)
            if len(raw) > 64:
                return _protocol_error_response("INVALID_REQUEST", "kind 字段超长", 400)
            if isinstance(raw, (bytes, bytearray)):
                decoded = bytes(raw).decode("utf-8", errors="replace").strip()
            else:
                decoded = str(raw or "").strip()
            # 邻接判定（MED-2）：前一 kind 未被文件消费即来新 kind → 顺序歧义 400
            if pending_kinds:
                return _protocol_error_response(
                    "CONTRACT_VALIDATION", "kind 未紧邻文件", 400)
            pending_kinds.append(decoded)
            kind_count += 1
        elif name.startswith("files"):
            # read_chunk 逐块读取自限内存（每文件 ≤50MB，整体由 total_bytes 累计兜底）
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_read + 1))
                if len(data) > max_read:
                    return _protocol_error_response(
                        "REQUEST_TOO_LARGE", "单个文件过大", 413)
            total_bytes += len(data)
            # cms=0 不限（与 app 级 client_max_size 同源语义）；cms>0 时累计强制整体上限
            if cms and total_bytes > cms:
                # 聚合超限：立即 413 中断，不继续读完 body（chunked 无 Content-Length
                # 时中间件拦不住，这里才是整体上限的强制点）
                return _protocol_error_response(
                    "REQUEST_TOO_LARGE", "整体上传超过聚合上限", 413)
            hint = pending_kinds.pop() if pending_kinds else None
            files.append((getattr(field, "filename", "") or "attachment",
                          bytes(data), hint))
        # 其他字段忽略

    # 步骤 0：数量 ≤10（保留现有，MED-1）
    if len(files) > 10:
        return _protocol_error_response("TOO_MANY_FILES", "单次上传文件数超限（≤10）", 400)
    # 步骤 0.5：kind part 总数防御上限（D6，defense-in-depth）。
    # 实际不可达：邻接判定（kind 未紧邻文件即 400）+ 悬空 400 + ≤10 文件上限使
    # kind_count ≤ 11；保留兜底防未来放宽邻接判定时回归，不单测（LOW-2）。
    if kind_count > 100:
        return _protocol_error_response("CONTRACT_VALIDATION", "kind 字段数量超限", 400)
    # 步骤 0.6：尾部悬空 kind（kind 后无文件被消费）→ 400（D5）
    if pending_kinds:
        return _protocol_error_response(
            "CONTRACT_VALIDATION", "kind 后缺少对应文件", 400)
    # 步骤 0.7：单 kind 特判（D1-A）——旧形态「单 kind + 多文件」整批统一；
    # 此分支下 hint=None 不可达（单 kind 一律视为整批共同提示）
    if kind_count == 1 and files:
        single_kind = next(h for h in (f[2] for f in files) if h is not None)
        files = [(name, data, single_kind) for name, data, _ in files]
    # 步骤 0.8：每 hint 枚举校验（未知 kind → 400；sticker 非附件白名单）
    for _, _, hint in files:
        if hint is not None and hint not in _UPLOAD_KIND_VALUES:
            return _protocol_error_response("CONTRACT_VALIDATION", f"未知 kind: {hint}", 400)

    # ---- 阶段 A：先全部校验（步骤 2-5：尺寸/魔数/扩展名/kind 归位）——
    # 任一失败立即 4xx，此时未写盘、未建记录，无需清理（Task 14 两阶段）。
    validated: list[dict] = []
    for filename_raw, data, hint in files:
        # 步骤 2：初步大小（hint 缺省跳过此步，仅以归位 kind 复核）
        if hint and len(data) > _KIND_LIMITS[hint]:
            return _protocol_error_response(
                "REQUEST_TOO_LARGE", f"文件过大，超过 {hint} 上限", 413)
        # 文件扩展名（步骤 3/4 复用；带点形式供文本白名单查表）
        ext_raw = os.path.splitext(filename_raw)[1].lower()
        ext = ext_raw.lstrip(".")
        # 步骤 3：类型判定（不信任扩展名/content_type，堵伪造）
        ft = filetype.guess(data)
        if ft is None:
            # 文本类（txt/md/json）无魔数：扩展名白名单 + UTF-8 内容 sanity（spec §4.1 I6）。
            # 非文本扩展名且魔数未知 → 仍 415。
            text_mime = _TEXT_EXT_TO_MIME.get(ext_raw)
            if text_mime is None:
                return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                # UTF-8 sanity 失败（内容非合法 UTF-8 文本）→ 拒绝
                return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
            mime = text_mime
        else:
            mime = ft.mime
            if mime != "video/webm" and mime not in _MIME_KIND:
                return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
        # 步骤 4：扩展名与判定一致（堵改名绕过；jpg/jpeg 归一化；文本类同样受白名单约束）
        if ext not in _MIME_EXTS.get(mime, set()):
            return _protocol_error_response(
                "UNSUPPORTED_MEDIA_TYPE", "扩展名与文件内容不一致", 415)
        # 步骤 5：kind 归位 + 归位后大小复核（webm tiebreaker）
        if mime == "video/webm":
            final_kind = "audio" if hint == "audio" else "video"
        else:
            final_kind = _MIME_KIND[mime]
        if len(data) > _KIND_LIMITS[final_kind]:
            return _protocol_error_response(
                "REQUEST_TOO_LARGE", f"文件过大，超过 {final_kind} 上限", 413)
        # filename 脱敏（Minor 9）：去路径分隔符、截断 255、剥控制字符；仅展示用
        filename = re.sub(r"[\\/]", "", filename_raw)
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
        filename = filename[:255] or "attachment"
        # 步骤 6 前半段：预计算写盘信息 + 记录文档（真正落盘留阶段 B 统一做）
        stored_name = f"{uuid.uuid4().hex[:16]}.{ext}"
        upload_dir = ProjectPath.DATA_UPLOADS.resolve() / final_kind
        stored_path = upload_dir / stored_name
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
        validated.append({
            "data": data,
            "stored_path": stored_path,
            "stored_name": stored_name,
            "final_kind": final_kind,
            "mime": mime,
            "attachment_id": attachment_id,
            "doc": doc,
        })

    # ---- 阶段 B：统一持久化（全部校验通过后才写盘 + 建记录 + 注册）——
    # 任一文件失败 → 清理本轮已写文件 + 已建记录 + 已注册映射（不留孤儿）。
    class _PersistAbort(Exception):
        """持久化中止信号：携带公开错误信封字段（str(e) 只进日志）。"""

        def __init__(self, code: str, message: str, status: int,
                     retryable: bool = False) -> None:
            super().__init__(message)
            self.code = code
            self.message = message
            self.status = status
            self.retryable = retryable

    results = []
    written: list[Path] = []   # 已写盘文件（失败清理用）
    created: list[AttachmentId] = []    # 已建记录 attachment_id（失败清理用）

    async def _cleanup() -> None:
        """失败清理：删本轮已写文件 + 已注册映射 + 已建记录。"""
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
            # 先登记再写盘：write_bytes 抛异常时本文件也在清理清单（review Minor）
            written.append(stored_path)
            stored_path.write_bytes(it["data"])
            try:
                await repo.create(it["doc"])
            except StorageUnavailableError:
                # fail-closed：离线 → 走统一清理（已写文件 + 已建记录）+ 503
                raise _PersistAbort(
                    "STORAGE_UNAVAILABLE", "附件存储不可用", 503, retryable=True)
            except Exception as e:
                logger.warning("附件记录写入失败: %s", e)
                raise _PersistAbort("INTERNAL", "附件记录写入失败", 500)
            created.append(it["attachment_id"])
            # 进程内映射注册（content_to_openai 同步定位落盘路径用，spec §4.5）
            attachment_resolver.register(
                it["attachment_id"], it["final_kind"], it["stored_name"])
            # signedUrl（fail-closed 省略）：AGENT_ACCESS_TOKEN 配 + SIG 未配 → 省略
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
        return _protocol_error_response(e.code, e.message, e.status, retryable=e.retryable)
    except Exception as e:
        # 兜底：写盘等意外失败同样清理不留孤儿，str(e) 只进日志
        logger.warning("附件持久化失败，已清理本轮写入: %s", e)
        await _cleanup()
        return _protocol_error_response("INTERNAL", "附件持久化失败", 500)

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
from typing import Any, TYPE_CHECKING, cast
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
    """HMAC 签名 URL（spec §3.3）。返回 (url, expires_unix)。"""
    import hmac

    expires_unix = int(time.time()) + 86400  # 24h
    sig = hmac.new(secret.encode(), f"{attachment_id}:{expires_unix}".encode(),
                   hashlib.sha256).hexdigest()
    url = f"/api/attachments/{attachment_id}?sig={sig}&exp={expires_unix}"
    return url, expires_unix

_IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()


def register_upload_routes(app: web.Application) -> None:
    """注册上传/表情路由（3 端点）。"""
    app.router.add_post("/api/images", image_upload_handler)
    app.router.add_post("/api/uploads", upload_handler)
    app.router.add_get("/api/stickers", stickers_handler)
