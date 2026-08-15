"""WS v2 深业务模块（2026-08-14 批次 B 拆分）。

_route_v2_text / _handle_voice_commit 及专属业务 helper 自 ws_handler.py 下移（函数体零改动）。
ws_v2 只依赖 core.schema / core.storage.idempotency / core.input._common 等，**不 import
ws_handler**（ws_handler 会 import ws_v2，反向 import 制造循环）。
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from core.input._common import _find_channel, _send_ack
from core.storage.idempotency import (
    GRACE_SECONDS,
    _IN_PROGRESS_RETRY_AFTER_MS,
    IdempotencyEntry,
    get_store,
)
from core.input.sticker_registry import get as get_sticker
from core.input.voice_sessions import VoiceSessionError
from core.memory.session_manager import SessionNotFoundError
from core.schema.event import UserInputData
from core.schema.part import AudioPart, FilePart, ImagePart, StickerPart, TextPart, VideoPart
from core.schema.utils import normalize_attachment_mime, now_iso
from core.schema.types import AttachmentId, SessionId
from core.storage.attachment_repo import StorageUnavailableError
from core.storage.idempotency_repo import STATUS_ACCEPTED, STATUS_REJECTED
from frontend_contract import V2TextMessage

if TYPE_CHECKING:
    from core.agent import Agent
    from core.input.audio_buffer import AudioBuffer
    from core.input.voice_sessions import VoiceSessionManager
    from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)


def _storage_unavailable_error(message: str) -> dict:
    """STORAGE_UNAVAILABLE rejected 错误负载（D3：details 携带当前 idempotency_durable）。

    设计 spec §2.1/D3：server_info 是握手快照、durable 翻转不推送能力事件；客户端凭
    STORAGE_UNAVAILABLE rejected ACK 的 details 取当前持久化能力（可重连取新快照）。
    """
    return {"code": "STORAGE_UNAVAILABLE", "message": message, "retryable": True,
            "details": {"idempotency_durable": get_store().durable}}


def _idempotency_unavailable_error() -> dict:
    """幂等存储不可用（MongoDB 离线）的 rejected 错误负载（D6c fail-closed + D3 durable）。"""
    return _storage_unavailable_error("幂等存储不可用（MongoDB 离线）")


def _in_progress_error() -> dict:
    """新鲜 CLAIMED 命中的 IN_PROGRESS 拒绝负载（§1.2：retryable + retryAfterMs 退避）。

    也用于 TAKEOVER 二次接管失败兜底（并发接管竞态/纯内存不可接管）——retryable，
    客户端按 retryAfterMs 退避重试。
    """
    return {"code": "IN_PROGRESS", "message": "请求处理中，请稍后重试",
            "retryable": True, "retryAfterMs": _IN_PROGRESS_RETRY_AFTER_MS}


async def _send_rejected(ws: "web.WebSocketResponse | None",
                         conversation_id: str, client_request_id: str,
                         client_message_id: str | None, error: dict, *,
                         received_at: str) -> None:
    """拒绝路径统一收口（Task 4）：``store.reject``（持久化完整 ACK 负载 + error）→
    rejected ACK（before ACK，received_at 同一值；重试重放原 rejected 逐字一致）。
    """
    await get_store().reject(
        conversation_id, client_request_id,
        code=error.get("code", ""), message=error.get("message", ""),
        retryable=bool(error.get("retryable")),
        received_at=received_at,
        client_message_id=client_message_id,
        conversation_id_wire=conversation_id,
        details=error.get("details"),
        retry_after_ms=error.get("retryAfterMs"),
    )
    await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                    "rejected", error=error, received_at=received_at)


async def _handle_idempotency_hit(
    agent: "Agent",
    ws: "web.WebSocketResponse | None",
    conversation_id: str,
    client_request_id: str,
    entry: IdempotencyEntry,
    digest: str,
    client_message_id: str | None,
    received_at: str,
) -> str:
    """统一幂等命中处理（spec §1.1/§1.2；lookup 命中与 store 返回 loser 共用，防双实现漂移）。

    按 entry 状态分支（同内容才有重放资格，异内容任意状态 → CONTRACT_VALIDATION）：
    - ACCEPTED → 重放原 accepted ACK（持久化字段逐字一致，含原 receivedAt）→ REPLAYED；
    - REJECTED → 重放原 rejected ACK（持久化 error 逐字一致）→ REPLAYED；
    - CLAIMED 新鲜（now - claimed_at ≤ GRACE_SECONDS）→ rejected IN_PROGRESS
      （retryable + retryAfterMs=_IN_PROGRESS_RETRY_AFTER_MS）→ REJECT；
    - CLAIMED 过期 + 提交证据（GenerationRegistry.find_by_user_message 命中）→
      ``store.complete`` 标记 ACCEPTED + 重放 accepted ACK → REPLAYED；
    - CLAIMED 过期 + 无证据 → TAKEOVER（调用方重新 claim 接管 stale CLAIMED 后进入业务流）。

    Returns:
        "REPLAYED"：已发送重放 ACK；"REJECT"：已发送 rejected ACK；
        "TAKEOVER"：未发 ACK——调用方按续跑点重新 claim（text/附件二次 store；
        voice 保持既有 reserve_work 从 commit_recording 续跑）。
    """
    store = get_store()
    if entry.content_digest != digest:
        # 异内容（任意状态）→ rejected CONTRACT_VALIDATION（同内容才有重放资格）
        await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                        "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "clientRequestId 冲突（内容摘要不同）",
                               "retryable": False},
                        received_at=received_at)
        return "REJECT"
    if entry.status == STATUS_ACCEPTED:
        # 同内容 + ACCEPTED → 重放原 accepted ACK（持久化字段逐字一致，含原 receivedAt）
        await _send_ack(ws, entry.user_message_id, client_request_id,
                        entry.client_message_id or client_message_id,
                        entry.conversation_id_wire or conversation_id,
                        "accepted",
                        received_at=entry.received_at or received_at)
        return "REPLAYED"
    if entry.status == STATUS_REJECTED:
        # 同内容 + REJECTED → 重放原 rejected ACK（持久化 error 逐字一致）
        await _send_ack(ws, "", client_request_id,
                        entry.client_message_id or client_message_id,
                        entry.conversation_id_wire or conversation_id,
                        "rejected", error=entry.error,
                        received_at=entry.received_at or received_at)
        return "REPLAYED"
    # CLAIMED：按 §1.2 恢复规则（新鲜 → IN_PROGRESS；过期 → 证据/接管）
    claimed_at = entry.claimed_at if entry.claimed_at is not None else 0
    if time.time() - claimed_at <= GRACE_SECONDS:
        # 新鲜 CLAIMED → rejected IN_PROGRESS（原请求大概率仍在途；retryable + 退避）
        await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                        "rejected", error=_in_progress_error(), received_at=received_at)
        return "REJECT"
    # 过期 CLAIMED + 提交证据（generation 已建档 → 已入队处理，重放 accepted 不重复生成）
    gen_registry = getattr(agent, "generation_registry", None)
    if gen_registry is not None:
        evidence = await gen_registry.find_by_user_message(entry.user_message_id)
    else:
        evidence = None  # 无注册表（测试 fake/未配置）→ 无证据
    if evidence is not None:
        # complete-before-ack：先标记 ACCEPTED（终态确定），再重放 accepted
        await store.complete(conversation_id, client_request_id,
                             received_at=received_at,
                             client_message_id=client_message_id,
                             conversation_id_wire=conversation_id)
        await _send_ack(ws, entry.user_message_id, client_request_id,
                        client_message_id, conversation_id, "accepted",
                        received_at=received_at)
        return "REPLAYED"
    # 过期 CLAIMED + 无证据 → TAKEOVER（重新 claim 接管；repo 内部按 claimed_at 判 stale）
    return "TAKEOVER"


async def _release_attachment_reservations(agent: "Agent", reserved: list[str],
                                           message_id: str) -> None:
    """幂等释放附件预留（B4/P1-04）：消息被拒/失败后回滚，不抛（尽力而为）。"""
    for aid in reserved:
        try:
            await agent.attachment_repo.release_reservation(
                AttachmentId(aid), message_id)
        except Exception:
            logger.warning("附件预留释放失败 aid=%s message=%s", aid, message_id)


async def _session_or_none(agent: "Agent", conversation_id: str | None) -> "SessionMemory | None":
    """9a3：v2 目标会话解析——显式 ID 先验证存在，未知返回 None；缺省 → 当前 active。

    禁止懒创建幽灵会话：显式 conversationId 必须经 ``has_session`` 确认存在才
    ensure_loaded（只读、不构造）；缺省（None）→ resolve(None, 2) 活动会话。
    """
    if conversation_id:
        if not await agent.session_manager.has_session(SessionId(conversation_id)):
            return None
        return await agent.session_manager.ensure_loaded(SessionId(conversation_id))
    return await agent.session_manager.resolve(None, 2)


# 附件 kind → Part 子类映射（spec §3.2 附件四类；未知 kind 跳过不构造）
_ATTACHMENT_PART_TYPES = {
    "image": ImagePart, "file": FilePart, "audio": AudioPart, "video": VideoPart,
}


def _attachment_part(kind: str, attachment_id: str, doc: dict):
    """附件记录 → 对应 Part（src=/api/attachments/{id} + attachment_id + 记录真值字段）。

    mime 以 attachments 记录为准（客户端提示值仅展示用，spec §3.1 Minor 10）；
    记录 mime 不在 Part 枚举时回退默认值（避免 pydantic 校验炸整条消息）。
    """
    cls = _ATTACHMENT_PART_TYPES.get(kind)
    if cls is None:
        logger.warning("未知附件 kind=%s，跳过构造 Part", kind)
        return None
    data: dict = {"src": f"/api/attachments/{attachment_id}", "attachment_id": attachment_id}
    # BE-S3-RUN-P0-02：读路径规范化——存量 kind=audio+video/webm → audio/webm、
    # WAV 探测别名 → audio/wav（spec §4.2 约束 3）；规范化后仍非法（未知/脏存量）→
    # 响亮 warning 回退默认 mime，不静默伪装（spec §4.2 约束 4，不炸消息页）。
    mime = normalize_attachment_mime(kind, doc.get("mime") or "")
    if mime:
        try:
            cls(**{**data, "mime": mime})
            data["mime"] = mime
        except Exception:
            logger.warning(
                "附件 kind=%s attachment_id=%s mime=%r 非契约值，回退默认 mime",
                kind, attachment_id, mime)
    if kind == "file":
        filename = doc.get("filename")
        if filename:
            data["filename"] = filename
        size = doc.get("size")
        if size is not None:
            try:
                data["size_bytes"] = int(size)
            except (TypeError, ValueError):
                pass
    return cls(**data)


def _build_media_parts(text: str, attachment_records: dict[str, dict],
                       sticker_id: str) -> list:
    """附件/表情消息的 Part 列表：TextPart + 附件 Part（按 kind）+ StickerPart。

    顺序：文本在前，附件按发送顺序，表情最后——喂给 LLM 前 content_to_openai
    会把 image 块移到 text 块之前（MIMO 要求），故此处仅保证 Part 齐全。
    """
    parts: list = []
    if text:
        parts.append(TextPart(content=text))
    for aid, doc in attachment_records.items():
        part = _attachment_part(doc.get("kind", ""), aid, doc)
        if part is not None:
            parts.append(part)
    if sticker_id:
        parts.append(StickerPart(sticker_id=sticker_id,
                                 src=f"/static/stickers/{sticker_id}.png"))
    return parts


@dataclass
class _V2TextContext:
    """v2 文本消息的会话/媒体/digest 上下文（校验后字段）。"""

    text: str
    client_request_id: str
    client_message_id: str | None
    ack_conversation_id: str
    received_at: str
    digest: str
    has_media: bool
    attachment_records: dict[str, dict]
    sticker_id: str


async def _prepare_v2_text_context(
    agent: "Agent",
    ws: "web.WebSocketResponse | None",
    model: V2TextMessage,
) -> tuple[_V2TextContext | None, bool]:
    """v2 文本消息的版本/会话/媒体校验与 digest 计算。

    Returns:
        (context, handled)：context=None 且 handled=True 表示已发送 ACK（或非 v2
        连接，调用方直接返回）；否则返回后续 claim 所需上下文。
    """
    if ws is None:
        return None, True
    channel = _find_channel(agent, ws)
    if channel is None or channel.protocol_version < 2:
        return None, True
    text = model.content.strip()
    client_request_id = model.clientRequestId
    client_message_id = model.clientMessageId
    conversation_id = model.conversationId

    if not client_request_id:
        await _send_ack(ws, "", "", client_message_id, conversation_id, "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "缺少 clientRequestId", "retryable": False})
        return None, True

    received_at = now_iso()

    try:
        target = await _session_or_none(agent, conversation_id)
        if target is None:
            await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                            "rejected", error={"code": "SESSION_NOT_FOUND",
                                               "message": f"会话不存在: {conversation_id}",
                                               "retryable": False},
                            received_at=received_at)
            return None, True
        ack_conversation_id = str(target.session_id)
    except Exception:
        logger.warning("_route_v2_text: 会话解析失败，回退回显 conversationId", exc_info=True)
        ack_conversation_id = conversation_id
    if not ack_conversation_id:
        ack_conversation_id = "default"

    attachments = model.attachments or []
    sticker_id = model.stickerId or ""
    has_media = bool(attachments) or bool(sticker_id)
    attachment_records: dict[str, dict] = {}
    if has_media:
        try:
            for att in attachments:
                aid = att.attachmentId
                if not aid:
                    await _send_ack(ws, "", client_request_id, client_message_id,
                                    ack_conversation_id, "rejected",
                                    error={"code": "CONTRACT_VALIDATION",
                                           "message": "attachments 缺 attachmentId",
                                           "retryable": False},
                                    received_at=received_at)
                    return None, True
                doc = await agent.attachment_repo.get(AttachmentId(aid))
                if doc is None or (doc.get("status") or "") != "active":
                    await _send_ack(ws, "", client_request_id, client_message_id,
                                    ack_conversation_id, "rejected",
                                    error={"code": "ATTACHMENT_NOT_FOUND",
                                           "message": f"附件不存在或已删除: {aid}",
                                           "retryable": False},
                                    received_at=received_at)
                    return None, True
                attachment_records[aid] = doc
        except StorageUnavailableError:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error=_storage_unavailable_error("附件存储不可用（MongoDB 离线）"),
                            received_at=received_at)
            return None, True
        if sticker_id and get_sticker(sticker_id) is None:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": f"未知 stickerId: {sticker_id}",
                                   "retryable": False},
                            received_at=received_at)
            return None, True
        ids = ",".join(sorted(attachment_records))
        digest = hashlib.sha256(
            text.encode("utf-8") + b"\x00" + ids.encode("utf-8")
            + b"\x00" + sticker_id.encode("utf-8")).hexdigest()
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return _V2TextContext(
        text=text,
        client_request_id=client_request_id,
        client_message_id=client_message_id,
        ack_conversation_id=ack_conversation_id,
        received_at=received_at,
        digest=digest,
        has_media=has_media,
        attachment_records=attachment_records,
        sticker_id=sticker_id,
    ), False


async def _claim_v2_text_id(
    agent: "Agent",
    ws: "web.WebSocketResponse | None",
    ctx: _V2TextContext,
) -> tuple[str | None, bool]:
    """幂等 lookup/claim 与 TAKEOVER 接管；返回实际生效的 message_id。

    Returns:
        (message_id, handled)：handled=True 表示已发送 ACK（调用方直接返回）。
    """
    store = get_store()
    entry: IdempotencyEntry | None = None
    try:
        entry = await store.lookup(ctx.ack_conversation_id, ctx.client_request_id)
    except StorageUnavailableError:
        await _send_ack(ws, "", ctx.client_request_id, ctx.client_message_id,
                        ctx.ack_conversation_id, "rejected",
                        error=_idempotency_unavailable_error(),
                        received_at=ctx.received_at)
        return None, True
    if entry is not None:
        action = await _handle_idempotency_hit(
            agent, ws, ctx.ack_conversation_id, ctx.client_request_id, entry, ctx.digest,
            ctx.client_message_id, ctx.received_at)
        if action != "TAKEOVER":
            return None, True
        message_id = entry.user_message_id
    else:
        from core.utils import new_message_id
        message_id = new_message_id()

    try:
        result = await store.store(
            ctx.ack_conversation_id, ctx.client_request_id,
            IdempotencyEntry(user_message_id=message_id, content_digest=ctx.digest,
                             created_at=time.monotonic(),
                             conversation_id=ctx.ack_conversation_id))
    except StorageUnavailableError:
        await _send_ack(ws, "", ctx.client_request_id, ctx.client_message_id,
                        ctx.ack_conversation_id, "rejected",
                        error=_idempotency_unavailable_error(),
                        received_at=ctx.received_at)
        return None, True
    if not result.acquired:
        action = await _handle_idempotency_hit(
            agent, ws, ctx.ack_conversation_id, ctx.client_request_id, result.entry,
            ctx.digest, ctx.client_message_id, ctx.received_at)
        if action != "TAKEOVER":
            return None, True
        try:
            result = await store.store(
                ctx.ack_conversation_id, ctx.client_request_id,
                IdempotencyEntry(user_message_id=result.entry.user_message_id,
                                 content_digest=ctx.digest,
                                 created_at=time.monotonic(),
                                 conversation_id=ctx.ack_conversation_id))
        except StorageUnavailableError:
            await _send_ack(ws, "", ctx.client_request_id, ctx.client_message_id,
                            ctx.ack_conversation_id, "rejected",
                            error=_idempotency_unavailable_error(),
                            received_at=ctx.received_at)
            return None, True
        if not result.acquired:
            action = await _handle_idempotency_hit(
                agent, ws, ctx.ack_conversation_id, ctx.client_request_id, result.entry,
                ctx.digest, ctx.client_message_id, ctx.received_at)
            if action == "TAKEOVER":
                await _send_ack(ws, "", ctx.client_request_id, ctx.client_message_id,
                                ctx.ack_conversation_id, "rejected",
                                error=_in_progress_error(), received_at=ctx.received_at)
            return None, True
    return result.entry.user_message_id, False


async def _finish_v2_text(
    agent: "Agent",
    ws: "web.WebSocketResponse | None",
    ctx: _V2TextContext,
    message_id: str,
    sent_message_ids: set | None,
) -> None:
    """claim 成功后的 process / ACK / refs 预留与回滚。"""
    store = get_store()
    if ctx.has_media:
        parts = _build_media_parts(ctx.text, ctx.attachment_records, ctx.sticker_id)
        content = [p.model_dump() for p in parts]
        reserved: list[str] = []
        try:
            for aid in ctx.attachment_records:
                await agent.attachment_repo.add_ref(AttachmentId(aid), message_id)
                reserved.append(aid)
        except Exception:
            logger.warning("附件 refs 预留失败——释放已预留并拒绝", exc_info=True)
            await _release_attachment_reservations(agent, reserved, message_id)
            await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                                 ctx.client_message_id,
                                 _storage_unavailable_error("附件引用持久化失败"),
                                 received_at=ctx.received_at)
            return
        try:
            accepted = await agent.process_message(
                text=ctx.text, content=content, message_id=message_id,
                protocol_version=2, conversation_id=ctx.ack_conversation_id)
        except SessionNotFoundError:
            await _release_attachment_reservations(agent, reserved, message_id)
            await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                                 ctx.client_message_id,
                                 {"code": "SESSION_NOT_FOUND",
                                  "message": f"会话不存在: {ctx.ack_conversation_id}",
                                  "retryable": False},
                                 received_at=ctx.received_at)
            return
        except Exception:
            logger.warning("附件消息 process_message 抛异常——reject 幂等 entry", exc_info=True)
            await _release_attachment_reservations(agent, reserved, message_id)
            await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                                 ctx.client_message_id,
                                 {"code": "INTERNAL", "message": "消息处理失败",
                                  "retryable": False},
                                 received_at=ctx.received_at)
            return
        if accepted:
            await store.complete(ctx.ack_conversation_id, ctx.client_request_id,
                                 received_at=ctx.received_at,
                                 client_message_id=ctx.client_message_id,
                                 conversation_id_wire=ctx.ack_conversation_id)
            await _send_ack(ws, message_id, ctx.client_request_id, ctx.client_message_id,
                            ctx.ack_conversation_id, "accepted", received_at=ctx.received_at)
            if sent_message_ids is not None:
                sent_message_ids.add(message_id)
        else:
            await _release_attachment_reservations(agent, reserved, message_id)
            await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                                 ctx.client_message_id,
                                 {"code": "QUEUE_FULL",
                                  "message": "Agent 忙，消息队列已满", "retryable": True},
                                 received_at=ctx.received_at)
        return

    try:
        accepted = await agent.process_message(
            text=ctx.text, message_id=message_id, protocol_version=2,
            conversation_id=ctx.ack_conversation_id)
    except SessionNotFoundError:
        await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                             ctx.client_message_id,
                             {"code": "SESSION_NOT_FOUND",
                              "message": f"会话不存在: {ctx.ack_conversation_id}",
                              "retryable": False},
                             received_at=ctx.received_at)
        return
    except Exception:
        logger.warning("process_message 抛异常——reject 幂等 entry，rejected", exc_info=True)
        await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                             ctx.client_message_id,
                             {"code": "INTERNAL", "message": "消息处理失败",
                              "retryable": False},
                             received_at=ctx.received_at)
        return
    if accepted:
        await store.complete(ctx.ack_conversation_id, ctx.client_request_id,
                             received_at=ctx.received_at,
                             client_message_id=ctx.client_message_id,
                             conversation_id_wire=ctx.ack_conversation_id)
        await _send_ack(ws, message_id, ctx.client_request_id, ctx.client_message_id,
                        ctx.ack_conversation_id, "accepted", received_at=ctx.received_at)
        if sent_message_ids is not None:
            sent_message_ids.add(message_id)
    else:
        await _send_rejected(ws, ctx.ack_conversation_id, ctx.client_request_id,
                             ctx.client_message_id,
                             {"code": "QUEUE_FULL",
                              "message": "Agent 忙，消息队列已满", "retryable": True},
                             received_at=ctx.received_at)


async def _route_v2_text(agent: "Agent", ws: "web.WebSocketResponse | None",
                         model: V2TextMessage,
                         sent_message_ids: set | None = None) -> bool:
    """v2 文本消息路由：ACK + 幂等 + 预分配 messageId（批次 7：附件/表情）。

    返回 True 表示已按 v2 处理（含 rejected），False 表示走 v1 路径（非 v2 连接）。
    """
    ctx, handled = await _prepare_v2_text_context(agent, ws, model)
    if handled or ctx is None:
        return True

    message_id, handled = await _claim_v2_text_id(agent, ws, ctx)
    if handled:
        return True
    if message_id is None:
        return True  # 防御：claim helper 返回 success 但缺 ID 不应发生

    await _finish_v2_text(agent, ws, ctx, message_id, sent_message_ids)
    return True


async def _handle_voice_commit(agent: "Agent", ws: "web.WebSocketResponse | None",
                               voice_mgr: "VoiceSessionManager | None", data: dict,
                               sent_message_ids: set | None = None) -> None:
    """voice.commit 处理顺序（spec §3.1 BE-P0-01#4 + §3.3 NEW-A/NEW-M/NEW-T）：
    ① 解析目标会话 ack_conv（幂等重放分支也用，对齐 _route_v2_text，review #15）
    ② 缺 clientRequestId → 直接 ack rejected（置于幂等与门禁之前）
    ③ 幂等表查重（digest = commit 携带 text）→ 命中经统一 `_handle_idempotency_hit`
       （重放/IN_PROGRESS/接管；接管从 commit_recording 续跑，不重复 reserve）
    ④ 未命中 → **claim 前先 reserve_work** → `store.store`（acquired=False/异常 →
       release_work + 对应 ACK，不消费录音）→ `commit_recording`（VoiceSessionError
       且 claim 已获取 → store.reject + release_work）→ `bus.post`（入队失败 →
       store.reject + release_work）→ `store.complete` → accepted ACK（complete-before-ack，
       bus.post 在 complete 前保证「已入队」语义）。

    Args:
        sent_message_ids: D1 归属接收集合——accepted 时登记 messageId（Task 9 消费）。
    """
    if voice_mgr is None:
        logger.debug("voice.commit 无 voice_mgr，忽略")
        return
    recording_id = data.get("recordingId", "")
    text = data.get("text", "")
    client_request_id = data.get("clientRequestId", "")
    client_message_id = data.get("clientMessageId")
    conversation_id = data.get("conversationId")

    # 9a3（Q9）：显式 ID 先 has_session 验证存在——未知会话走 ACK rejected
    # SESSION_NOT_FOUND（非 ErrorMessage）；不提交、不产生 messageId。
    try:
        target = await _session_or_none(agent, conversation_id)
        if target is None:
            await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                            "rejected", error={"code": "SESSION_NOT_FOUND",
                                               "message": f"会话不存在: {conversation_id}",
                                               "retryable": False})
            return
        ack_conv = str(target.session_id)
    except Exception:
        ack_conv = conversation_id
    if not ack_conv:
        # fallback 回显可能是 None → 归一到默认会话（幂等键维度）
        ack_conv = "default"

    if not client_request_id:
        await _send_ack(ws, "", "", client_message_id, ack_conv, "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "缺少 clientRequestId", "retryable": False})
        return

    # 接收时刻在 handler 一次性生成（now_iso）——reject/complete 与 ACK 用同一值
    received_at = now_iso()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store = get_store()
    # D6c：幂等 lookup——产品模式 + Mongo 离线 → fail-closed（ACK rejected + D3 durable）
    entry: IdempotencyEntry | None = None
    try:
        entry = await store.lookup(ack_conv, client_request_id)
    except StorageUnavailableError:
        await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                        "rejected", error=_idempotency_unavailable_error(),
                        received_at=received_at)
        return
    if entry is not None:
        action = await _handle_idempotency_hit(
            agent, ws, ack_conv, client_request_id, entry, digest,
            client_message_id, received_at)
        if action != "TAKEOVER":
            return  # REPLAYED/REJECT 已发 ACK
        # TAKEOVER：过期 CLAIMED 无证据 → 继续（下方 claim 接管 stale CLAIMED）
        message_id = entry.user_message_id
    else:
        from core.utils import new_message_id
        message_id = new_message_id()

    # 在 commit 清除 PENDING 录音前，先原子预占 ACK 已解析出的实际会话。删除若已
    # 开始则 rejected；预占成功后直到 CognitiveLoop 终态都不能删除该会话。
    try:
        reserved_target = await agent.session_manager.reserve_work(ack_conv, 2)
    except SessionNotFoundError:
        await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                        "rejected", error={"code": "SESSION_NOT_FOUND",
                                           "message": f"会话不存在: {ack_conv}",
                                           "retryable": False},
                        received_at=received_at)
        return
    reserved_id = SessionId(reserved_target.session_id)

    # claim（acquired=False → release_work + 对应 ACK，不消费录音）
    try:
        result = await store.store(
            ack_conv, client_request_id,
            IdempotencyEntry(user_message_id=message_id, content_digest=digest,
                             created_at=time.monotonic(), conversation_id=ack_conv))
    except StorageUnavailableError:
        agent.session_manager.release_work(reserved_id)
        await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                        "rejected", error=_idempotency_unavailable_error(),
                        received_at=received_at)
        return
    if not result.acquired:
        # 并发 loser/重放：恒不进入业务处理
        action = await _handle_idempotency_hit(
            agent, ws, ack_conv, client_request_id, result.entry, digest,
            client_message_id, received_at)
        if action != "TAKEOVER":
            agent.session_manager.release_work(reserved_id)
            return
        # TAKEOVER：保持既有 reserve_work（不重复 reserve），二次 claim 接管 stale
        # CLAIMED；不得重入顶部（防双重 reserve）
        try:
            result = await store.store(
                ack_conv, client_request_id,
                IdempotencyEntry(user_message_id=result.entry.user_message_id,
                                 content_digest=digest,
                                 created_at=time.monotonic(), conversation_id=ack_conv))
        except StorageUnavailableError:
            # 接管期间 Mongo 断连（产品模式 fail-closed）→ 幂等感知 rejected ACK；
            # 必须先 release_work（防会话 busy 计数泄漏），与首次 store 错误处理一致
            agent.session_manager.release_work(reserved_id)
            await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                            "rejected", error=_idempotency_unavailable_error(),
                            received_at=received_at)
            return
        if not result.acquired:
            # 防御：接管竞态输家（另一并发方刚接管）→ 发对应 ACK + 释放 work；纯内存不可
            # 接管 → 兜底 IN_PROGRESS（retryable，不静默无 ACK）
            action = await _handle_idempotency_hit(
                agent, ws, ack_conv, client_request_id, result.entry, digest,
                client_message_id, received_at)
            if action == "TAKEOVER":
                await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                                "rejected", error=_in_progress_error(),
                                received_at=received_at)
            agent.session_manager.release_work(reserved_id)
            return
    message_id = result.entry.user_message_id  # 并发同键时用实际生效的 message_id

    try:
        pending_text, emotion, events = voice_mgr.commit_recording(recording_id, text)
    except VoiceSessionError as e:
        # claim 已获取但 commit 失败 → reject（重试重放原 rejected）+ release_work
        await _send_rejected(ws, ack_conv, client_request_id, client_message_id,
                             {"code": e.code, "message": e.message,
                              "retryable": e.retryable},
                             received_at=received_at)
        agent.session_manager.release_work(reserved_id)
        return

    agent.idle.touch()
    # 事件携带预占后实际会话 ID；缺省 conversationId 不再留到消费时二次解析。
    try:
        agent.bus.post(UserInputData(
            text=pending_text, user_name=agent.user_name, emotion=emotion, events=events,
            request_id=agent.next_request_id(), message_id=message_id,
            protocol_version=2, conversation_id=reserved_id,
            session_work_reserved=True))
    except Exception:
        # 入队失败 → reject（重试重放原 rejected）+ release_work + rejected ACK（连接存活）
        logger.warning("voice.commit 入队失败——reject 幂等 entry", exc_info=True)
        await _send_rejected(ws, ack_conv, client_request_id, client_message_id,
                             {"code": "INTERNAL", "message": "事件入队失败",
                              "retryable": False},
                             received_at=received_at)
        agent.session_manager.release_work(reserved_id)
        return
    # complete-before-ack：bus 已入队 → 标记 ACCEPTED（终态确定）→ accepted ACK
    await store.complete(ack_conv, client_request_id,
                         received_at=received_at,
                         client_message_id=client_message_id,
                         conversation_id_wire=ack_conv)
    await _send_ack(ws, message_id, client_request_id, client_message_id, ack_conv,
                    "accepted", received_at=received_at)
    if sent_message_ids is not None:
        sent_message_ids.add(message_id)   # D1 归属（voice.commit 同样登记）
    # BE-081 敏感纪律：只记 recordingId + 文本长度，不记正文
    logger.debug("voice.commit 提交成功: %s (len=%d)", recording_id, len(pending_text))


