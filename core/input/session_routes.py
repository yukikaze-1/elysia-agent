"""业务 API 路由 — REST 会话路由（批次 3 BE-030/031/032 + BE-024）。

多会话 CRUD、会话内历史/增量、消息上下文、搜索、资源索引、generation 状态、pending 查询。
wire 字段统一 `conversationId`；`/api/sessions` 路径参数即 conversationId 值。
全部走批次 1 产品认证（`AGENT_ACCESS_TOKEN`，由 server.py app 级中间件覆盖 /api/*）。
"""

import asyncio
import base64
import logging
import re
import uuid
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from aiohttp import web
from bson import ObjectId
from bson.errors import InvalidId

if TYPE_CHECKING:
    from core.agent import Agent

from core.input.api_routes import _archive_to_client, _archive_msg_to_contract
from core.schema.message import Message, MessageRole
from core.schema.part import TextPart, ImagePart, FilePart, AudioPart, VideoPart, StickerPart
from core.schema.utils import now_iso
from core.schema.types import GenerationId, MessageId, SessionId
from core.storage.attachment_release import release_session_attachments
from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionSummary
from frontend_contract import rest as rest_contract
from frontend_contract.rest import FinishReason, ProtocolError, Role

logger = logging.getLogger(__name__)

# 链接 URL 提取统一正则（spec §8 link 类别；url 即下载引用，缩略图生成延后 BE-071）。
# 排除结尾标点/括号/引号，避免把句号吸收进 URL。
_LINK_URL_RE = re.compile(r"https?://[^\s<>\"'（）()]+")

_MAX_LIMIT = 100
_RESOURCE_CATEGORIES = {"image", "link", "video", "audio", "file", "sticker"}

# 资源扫描每批消息数（消息级扫描，非命中直接跳过；批次足够大避免过多往返）。
_RESOURCE_SCAN_BATCH = 200


# ── 统一错误信封（ProtocolError 形状，错误码目录稳定码） ──────────────────────


def _error_response(code: str, message: str, status: int = 400,
                    retryable: bool = False) -> web.Response:
    """构造统一错误信封响应（与 server.py 认证中间件同形状）。

    Args:
        code: 稳定错误码（SESSION_NOT_FOUND / GENERATION_NOT_FOUND / INVALID_TIMEZONE / CONTRACT_VALIDATION / AGENT_BUSY）。
        message: 公开说明。
        status: HTTP 状态码。
        retryable: 是否可重试（如 AGENT_BUSY——忙时拒绝后客户端可稍后重试）。

    Returns:
        web.Response: ProtocolError 信封 JSON。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)


# ── 参数解析 helpers ────────────────────────────────────────────────────────


def _parse_limit(request: web.Request, default: int = 20, cap: int = _MAX_LIMIT) -> int:
    """解析 limit 查询参数并夹取到 [1, cap]。非法值回退 default。"""
    try:
        limit = int(request.query.get("limit", str(default)))
    except ValueError:
        limit = default
    return max(1, min(limit, cap))


def _parse_int_param(request: web.Request, name: str, default: int) -> int:
    """解析整数查询参数（before/after 窗口等）。非法值回退 default。"""
    try:
        return int(request.query.get(name, str(default)))
    except ValueError:
        return default


def _encode_resource_cursor(doc_id: str, offset: int) -> str:
    """编码资源级游标：base64url(`_id|offset`)。

    批次 3 review 修复（链路#2）：消息级 cursor（base64(_id)）在 limit 命中某消息
    内部（一条消息含多个同类资源）时，下页 before 排除整条消息 → 后续资源永久丢失。
    资源级 cursor 记录「该消息内已返回的同类资源数 offset」，下页从 offset+1 续扫。
    """
    raw = f"{doc_id}|{offset}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _parse_resource_before(value: str | None) -> tuple[str, int] | None:
    """解析 resources `before` 游标 → (消息 _id, 消息内已返回资源数 offset)。

    新格式：base64url(`_id|offset`)（资源级）；兼容旧格式 base64(_id) / raw _id
    → offset=0（从头扫该消息，不再丢整条）。非法格式返回 None（从头扫）。
    """
    if not value:
        return None
    # 新格式：base64url(_id|offset)
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        raw_id, _, offset_str = decoded.partition("|")
        if re.fullmatch(r"[0-9a-fA-F]{24}", raw_id) and offset_str.isdigit():
            return raw_id, int(offset_str)
        if re.fullmatch(r"[0-9a-fA-F]{24}", decoded):
            return decoded, 0  # 兼容旧 base64(_id)
    except Exception:
        pass
    # 兼容旧格式：raw 24-hex _id 直接传入
    if re.fullmatch(r"[0-9a-fA-F]{24}", value):
        return value, 0
    return None


# ── 契约转换 helpers ────────────────────────────────────────────────────────


def _to_contract_summary(s: SessionSummary, active_id: str) -> rest_contract.SessionSummary:
    """仓库 SessionSummary dataclass → 契约 SessionSummary（title 空时缺省省略）。"""
    return rest_contract.SessionSummary(
        conversationId=str(s.session_id),
        title=(s.title or None),
        messageCount=s.message_count,
        updatedAt=s.updated_at,
        active=(str(s.session_id) == str(active_id)),
    )


def _contract_messages(messages: list[Message]) -> list[rest_contract.Message]:
    """core.schema.Message 列表 → 契约 Message 列表（逐条契约化，单条失败丢弃）。"""
    client_msgs = _archive_to_client(messages)
    return [m for m in (_archive_msg_to_contract(d) for d in client_msgs) if m]


async def _session_summary(agent: "Agent", sid: str,
                           message_count: int) -> rest_contract.SessionSummary | None:
    """构造单会话 SessionSummary：title/updatedAt 取 session doc（repo.list_all 匹配），
    messageCount 用归档计数（消息总数，比会话窗口更准确）。

    Returns:
        契约 SessionSummary；会话不在 repo 列表时返回 None。
    """
    repo = getattr(agent, "session_repo", None)
    active_id = getattr(agent, "active_session_id", "") or ""
    if repo is None:
        return None
    try:
        summaries = await repo.list_all()
    except Exception as e:
        logger.warning("session summary list_all 失败: %s", e)
        return None
    for s in summaries:
        if str(s.session_id) == str(sid):
            summary = _to_contract_summary(s, active_id)
            summary.messageCount = message_count
            return summary
    return None


# ── 会话存在性检查 ──────────────────────────────────────────────────────────


async def _session_exists(agent: "Agent", sid: str) -> bool:
    """会话是否存在（直通 SessionManager.has_session，区分已知/未知会话）。"""
    manager = getattr(agent, "session_manager", None)
    if manager is None:
        return False
    try:
        return await manager.has_session(sid)
    except Exception as e:
        logger.warning("has_session 失败 sid=%s: %s", sid, e)
        return False


def _generation_registry(agent: "Agent"):
    """取 GenerationRegistry（agent.cognitive._generation_registry），缺失返回 None。"""
    cognitive = getattr(agent, "cognitive", None)
    return getattr(cognitive, "_generation_registry", None)  # noqa: SLF001


# ── 文本/资源提取 helpers（spec §8） ────────────────────────────────────────


def _message_text(msg: Message) -> str:
    """提取消息纯文本（TextPart.content 拼接；兼容 dict part）。"""
    parts = getattr(msg, "content", None) or []
    out = []
    for p in parts:
        if isinstance(p, TextPart):
            out.append(p.content)
        elif isinstance(p, dict) and p.get("type") == "text":
            out.append(p.get("content", ""))
    return "".join(out)


def _extract_urls(text: str) -> list[str]:
    """从文本提取链接 URL（统一正则 helper，spec §8 link）。去重保序。"""
    return list(dict.fromkeys(_LINK_URL_RE.findall(text)))


def _extract_from_message(msg: Message, category: str) -> list[dict]:
    """按类别从单条消息提取资源项（{kind, url} 列表）。

    image → ImagePart.src；link → TextPart.content URL 正则；
    video/audio/file → 对应 Part.src；sticker → StickerPart.sticker_id
    → 静态路径 /static/stickers/{id}.png（批次 7，内置公开资源）。
    """
    parts = getattr(msg, "content", None) or []
    results: list[dict] = []
    if category == "image":
        for p in parts:
            if isinstance(p, ImagePart) and getattr(p, "src", ""):
                results.append({"kind": "image", "url": p.src})
    elif category == "link":
        for url in _extract_urls(_message_text(msg)):
            results.append({"kind": "link", "url": url})
    elif category == "video":
        for p in parts:
            if isinstance(p, VideoPart) and getattr(p, "src", ""):
                results.append({"kind": "video", "url": p.src})
    elif category == "audio":
        for p in parts:
            if isinstance(p, AudioPart) and getattr(p, "src", ""):
                results.append({"kind": "audio", "url": p.src})
    elif category == "file":
        for p in parts:
            if isinstance(p, FilePart) and getattr(p, "src", ""):
                results.append({"kind": "file", "url": p.src})
    elif category == "sticker":
        for p in parts:
            if isinstance(p, StickerPart) and getattr(p, "sticker_id", ""):
                results.append({"kind": "sticker",
                                "url": f"/static/stickers/{p.sticker_id}.png"})
    return results


async def _attachment_available(agent: "Agent", url: str) -> bool:
    """资源索引软删过滤：/api/attachments/{id} → 附件记录存在且 active；其余 URL 不过滤。

    Mongo 离线（附件 repo fail-closed）保守过滤——附件不可校验 → 不展示（避免
    指向已删/不可用文件）。legacy /uploads/images/* 非附件，不过滤。
    """
    if not url.startswith("/api/attachments/"):
        return True
    attachment_id = url.rsplit("/", 1)[-1]
    repo = getattr(agent, "attachment_repo", None)
    if repo is None:
        return True
    try:
        doc = await repo.get(attachment_id)
    except Exception:
        # 附件 repo fail-closed（StorageUnavailableError 等）：保守过滤不展示
        return False
    return doc is not None and (doc.get("status") or "") == STATUS_ACTIVE


def _items_from_message(msg: Message, category: str, start_offset: int = 0):
    """产出单条消息从 start_offset 起的同类资源 (item_dict, cursor) 序列。

    cursor = (消息 _id, 该消息内已返回资源数)，供资源级分页续扫。

    Args:
        msg: 归档消息对象（metadata 含 _mongo_id）。
        category: 资源类别。
        start_offset: 消息内起始资源下标（0 从头；续扫时承接上页 offset）。

    Yields:
        (资源项 dict, 资源级 cursor tuple)。
    """
    doc_id = msg.metadata.get("_mongo_id")
    for idx, res in enumerate(_extract_from_message(msg, category)[start_offset:],
                              start=start_offset):
        yield {
            "messageId": str(msg.message_id),
            "timestamp": str(msg.timestamp),
            "kind": res["kind"],
            "url": res["url"],
        }, (str(doc_id), idx + 1)


async def _next_hit(agent: "Agent", message_repo: MessageRepository, session_id: str,
                    category: str,
                    doc_id: str, offset: int) -> tuple[dict, tuple[str, int]] | None:
    """从 (doc_id, offset) 起找下一条命中资源（peek has_more 用）。

    先查 cursor 消息从 offset 起的剩余资源，再扫其后消息；软删附件项过滤。

    Returns:
        (资源项 dict, cursor) 或 None（无更多命中）。
    """
    msg = await message_repo.get_by_mongo_id(SessionId(session_id), doc_id)
    if msg is not None:
        for item, cursor in _items_from_message(msg, category, offset):
            if await _attachment_available(agent, item["url"]):
                return item, cursor
    scan_before = doc_id
    while True:
        msgs, _ = await message_repo.query_page(SessionId(session_id), before=scan_before,
                                                limit=_RESOURCE_SCAN_BATCH)
        if not msgs:
            return None
        for msg in msgs:
            did = msg.metadata.get("_mongo_id")
            if not did:
                continue
            scan_before = did
            for item, cursor in _items_from_message(msg, category):
                if await _attachment_available(agent, item["url"]):
                    return item, cursor
    return None


async def _scan_resources(agent: "Agent", message_repo: MessageRepository,
                          session_id: str, category: str,
                          before: tuple[str, int] | None,
                          limit: int) -> tuple[list[dict], bool, str | None]:
    """按需扫描归档提取资源（spec §8 分页，资源级 cursor）。

    从 `before`（(消息 _id, 消息内 offset)）继续按 _id 倒序扫描：先处理 cursor 消息
    从 offset 起的剩余资源，再扫其后消息，收集命中项直到凑够 limit；随后**额外 peek**
    一条命中资源判定 has_more（peek 不到 = 无更多）。软删附件项（/api/attachments/
    且附件记录 deleted/不存在）过滤不展示（批次 7）。

    批次 3 review 修复（链路#2）：cursor 由消息级（base64(_id)）改为**资源级**
    base64url(`_id|offset`)——limit 在某消息内部命中（一条消息含多个同类资源）时，
    下页从该消息 offset+1 续扫，不再排除整条消息导致后续资源永久丢失。

    Returns:
        (资源项 dict 列表, has_more, 下一页资源级游标)。
    """
    start_id, start_offset = before if before is not None else (None, 0)
    items: list[dict] = []
    last_cursor: tuple[str, int] | None = None
    scan_before = start_id

    # 阶段一：收集命中项直到凑够 limit
    # 续扫时先处理 cursor 消息从 start_offset 起的剩余资源（query_page 的 before
    # 是排他的，不含 cursor 消息本身，需按 _id 单独取回）
    if start_id is not None:
        cursor_msg = await message_repo.get_by_mongo_id(SessionId(session_id), start_id)
        if cursor_msg is not None:
            for item, cursor in _items_from_message(cursor_msg, category, start_offset):
                if not await _attachment_available(agent, item["url"]):
                    continue
                if len(items) >= limit:
                    break
                items.append(item)
                last_cursor = cursor
    # 再扫 cursor 消息之后的消息（首页 start_id=None 直接从这里开始）
    while len(items) < limit:
        msgs, _ = await message_repo.query_page(SessionId(session_id), before=scan_before,
                                                limit=_RESOURCE_SCAN_BATCH)
        if not msgs:
            break
        for msg in msgs:
            doc_id = msg.metadata.get("_mongo_id")
            if not doc_id:
                continue
            scan_before = doc_id
            for item, cursor in _items_from_message(msg, category):
                if not await _attachment_available(agent, item["url"]):
                    continue
                if len(items) >= limit:
                    break
                items.append(item)
                last_cursor = cursor
            if len(items) >= limit:
                break
    if len(items) < limit:
        # 未凑够 limit → 已扫到会话末尾，无更多命中
        return items, False, None
    if last_cursor is None:
        return items, False, None

    # 阶段二：peek 一条命中资源判定 has_more（只探不收集）
    has_more = await _next_hit(agent, message_repo, session_id, category, *last_cursor) is not None
    return items, has_more, _encode_resource_cursor(*last_cursor)


# ── 会话 CRUD（spec §4） ────────────────────────────────────────────────────


async def sessions_list_handler(request: web.Request) -> web.Response:
    """GET /api/sessions — 会话列表（cursor/limit/search，search 仅 title 大小写不敏感）。"""
    agent = request.app["agent"]
    cursor = request.query.get("cursor", "").strip() or None
    search = request.query.get("search", "").strip()
    limit = _parse_limit(request, default=50)
    manager = agent.session_manager
    # 畸形 cursor → 400 CONTRACT_VALIDATION（_decode_cursor 抛 ValueError，原未捕获 → 500）
    try:
        items, has_more, next_cursor = await manager.list(cursor=cursor, limit=limit, search=search)
    except ValueError:
        return _error_response("CONTRACT_VALIDATION", "cursor 格式非法")

    # review 修复（批次 C）：messageCount 统一为归档计数——与详情/历史 total 同源
    # （列表 repo.list_all 的 message_count 是 ≤20 窗口数，与详情 count_by_session 不一致）。
    # 原实现对每会话顺序 await count_by_session（N+1 串行，≤50 次）→ asyncio.gather
    # 并发统计，单条失败保留窗口数（与原有兜底语义一致）。

    async def _count(s: SessionSummary):
        try:
            return await agent.message_repo.count_by_session(s.session_id)
        except Exception:
            logger.debug("count_by_session 失败 sid=%s，保留窗口数", s.session_id, exc_info=True)
            return None

    counts = await asyncio.gather(*(_count(s) for s in items))
    summaries = []
    for s, count in zip(items, counts):
        summary = _to_contract_summary(s, manager.active_id)
        if count is not None:
            summary.messageCount = count
        summaries.append(summary)
    resp = rest_contract.SessionListResult(
        sessions=summaries, has_more=has_more, cursor=next_cursor)
    return web.json_response(resp.wire_dict())


async def sessions_create_handler(request: web.Request) -> web.Response:
    """POST /api/sessions — 创建空会话（conv_<uuid4 hex[:12]>，title 可空）。"""
    agent = request.app["agent"]
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return _error_response("CONTRACT_VALIDATION", "无效的 JSON 请求体")
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _error_response("CONTRACT_VALIDATION", "title 必须是字符串")
    summary = await agent.session_manager.create(title=title or "")
    resp = _to_contract_summary(summary, agent.active_session_id)
    return web.json_response(resp.wire_dict())


async def sessions_detail_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{conversationId} — 会话详情（summary + 第一页消息 + active）。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    total = await agent.message_repo.count_by_session(sid)
    summary = await _session_summary(agent, sid, total)
    if summary is None:
        # has_session 通过但 list_all 未命中（Mongo 离线等极端场景）→ 兜底空摘要
        summary = rest_contract.SessionSummary(
            conversationId=sid, messageCount=total, updatedAt="",
            active=(str(sid) == str(agent.active_session_id)))
    messages, _ = await agent.message_repo.query_page(sid, limit=20)
    resp = rest_contract.SessionDetail(
        session=summary, messages=_contract_messages(messages), active=summary.active)
    return web.json_response(resp.wire_dict())


async def sessions_rename_handler(request: web.Request) -> web.Response:
    """PATCH /api/sessions/{conversationId} — 重命名（title 非空才写；空串清除）。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return _error_response("CONTRACT_VALIDATION", "无效的 JSON 请求体")
    title = body.get("title")
    if title is None or not isinstance(title, str):
        return _error_response("CONTRACT_VALIDATION", "缺少 title 或类型错误")
    await agent.session_manager.rename(sid, title)
    total = await agent.message_repo.count_by_session(sid)
    summary = await _session_summary(agent, sid, total)
    if summary is None:
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    return web.json_response(summary.wire_dict())


async def sessions_delete_handler(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{conversationId} — 删除（联动归档清理 + active 回落 default）。

    保护（review 修复）：默认会话 "default" 不可删除——返回 409 SESSION_PROTECTED，
    不执行删除（删 default 会让整个 v1/多会话链路失去锚点）。
    """
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if sid == "default":
        return _error_response("SESSION_PROTECTED", "默认会话不可删除", 409)
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    # review 修复（批次 C）：删除活动会话且 agent 忙（生成/TTS 中）→ 409 AGENT_BUSY。
    # 删活动会话会 attach_session(default) 重绑 engine——忙时换绑与在途生成/TTS
    # 竞态，门禁与 activate 一致（防未保护 engine 换绑向量）。非活动会话删除
    # 无 engine 换绑，不设门禁。
    if str(agent.active_session_id) == str(sid):
        status = getattr(agent, "status", None)
        if status is not None and not status.is_accepting_input:
            return _error_response("AGENT_BUSY", "Agent 忙（生成/TTS 中），无法删除活动会话",
                                   409, retryable=True)
    # 批次 8a（D5）：删除前释放该会话归档消息的附件 refs（附件软删联动，
    # 闭合 BE-030 会话删除 + BE-070 附件引用释放闭环）。先 release 再 delete——
    # session_manager.delete 会清空该会话归档，附件 refs 需在归档清空前取到。
    try:
        await release_session_attachments(agent, SessionId(sid))
    except Exception as e:
        logger.warning("sessions_delete 附件释放失败 sid=%s: %s", sid, e)
    deleted = await agent.session_manager.delete(sid)
    return web.json_response({"ok": True, "deleted": deleted})


async def sessions_activate_handler(request: web.Request) -> web.Response:
    """POST /api/sessions/{conversationId}/activate — 仅切 active（回放走 WS activate_session）。

    批次 3 review 修复（F1-a）：切换前 FSM 门禁——agent 忙（生成/TTS 中，
    is_accepting_input=False）→ 409 AGENT_BUSY（retryable），不执行切换。
    """
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    status = getattr(agent, "status", None)
    if status is not None and not status.is_accepting_input:
        return _error_response("AGENT_BUSY", "Agent 忙（生成/TTS 中），无法切换会话",
                               409, retryable=True)
    await agent.switch_session(SessionId(sid))
    resp = rest_contract.SessionActivateResult(activeConversationId=str(sid))
    return web.json_response(resp.wire_dict())


# ── 会话内历史（spec §5.1） ─────────────────────────────────────────────────


async def sessions_messages_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{conversationId}/messages — 历史分页（before/limit）或增量（after=）。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    limit = _parse_limit(request, default=50, cap=200)
    after = request.query.get("after", "").strip() or None
    if after:
        messages, has_more = await agent.message_repo.query_after(sid, after, limit=limit)
    else:
        before = request.query.get("before", "").strip() or None
        # 畸形 before 游标 → 400 CONTRACT_VALIDATION（验收修复：原 ObjectId 未捕获 → 500）
        if before is not None:
            try:
                ObjectId(before)  # 仅校验 24-hex 合法性，不用于查询
            except InvalidId:
                return _error_response("CONTRACT_VALIDATION", "非法 before 游标", 400)
        messages, has_more = await agent.message_repo.query_page(sid, before=before, limit=limit)
    total = await agent.message_repo.count_by_session(sid)
    resp = rest_contract.MessagesPageResponse(
        messages=_contract_messages(messages), has_more=has_more, total=total)
    return web.json_response(resp.wire_dict())


# ── 消息上下文窗口（spec §5.2） ─────────────────────────────────────────────


async def message_context_handler(request: web.Request) -> web.Response:
    """GET /api/messages/{messageId}/context — 锚点消息前后窗口（conversationId 必填）。"""
    agent = request.app["agent"]
    mid = request.match_info["messageId"]
    conversation_id = request.query.get("conversationId", "").strip()
    if not conversation_id:
        return _error_response("CONTRACT_VALIDATION", "缺少 conversationId")
    before_n = _parse_int_param(request, "before", 5)
    after_n = _parse_int_param(request, "after", 5)
    anchor, pre, post = await agent.message_repo.find_context(
        conversation_id, mid, before=before_n, after=after_n)
    if anchor is None:
        # review 修复（兼容#4）：消息不存在语义用 RESOURCE_NOT_FOUND（错误目录码），
        # 非 SESSION_NOT_FOUND（会话不存在）。
        return _error_response("RESOURCE_NOT_FOUND", f"消息不存在: {mid}", 404)
    anchor_msgs = _contract_messages([anchor])
    if not anchor_msgs:
        return _error_response("RESOURCE_NOT_FOUND", f"消息契约化失败: {mid}", 404)
    resp = rest_contract.MessageContextResponse(
        anchor=anchor_msgs[0],
        before=_contract_messages(pre),
        after=_contract_messages(post),
        conversationId=conversation_id,
    )
    return web.json_response(resp.wire_dict())


# ── 搜索（spec §5.3） ────────────────────────────────────────────────────────


async def search_messages_handler(request: web.Request) -> web.Response:
    """GET /api/search/messages — 组合过滤 + keyset 分页搜索。"""
    agent = request.app["agent"]
    query = request.query.get("query", "").strip()
    conversation_id = request.query.get("conversationId", "").strip() or None
    date_from = request.query.get("date_from", "").strip() or None
    date_to = request.query.get("date_to", "").strip() or None
    tz_name = request.query.get("timezone", "UTC").strip() or "UTC"
    category = request.query.get("category", "").strip() or None
    cursor = request.query.get("cursor", "").strip() or None
    limit = _parse_limit(request, default=20, cap=100)

    # category 白名单校验：非空且不在已知取值 → 400 CONTRACT_VALIDATION
    # （D4/BE-031：未知 category 不能静默放行——query_search 只会恒空，掩盖请求错误）
    if category is not None and category not in (
            "text", "image", "video", "audio", "file", "link", "tool"):
        return _error_response("CONTRACT_VALIDATION", f"不支持的 category: {category}")

    # 时区校验：仅日期过滤时 zoneinfo 参与；未知时区 → 400 INVALID_TIMEZONE
    # （query_search 内部吞掉异常返回空，故在调用前显式校验）。
    if date_from or date_to:
        try:
            ZoneInfo(tz_name)
        except Exception:
            return _error_response("INVALID_TIMEZONE", f"未知时区: {tz_name}")

    messages, has_more, next_cursor = await agent.message_repo.query_search(
        session_id=conversation_id, query=query, date_from=date_from,
        date_to=date_to, timezone=tz_name, category=category, cursor=cursor, limit=limit)

    registry = _generation_registry(agent)
    items = []
    for msg in messages:
        role = getattr(msg, "role", None)
        # 批次 3 review 修复（链路#4）：category=tool 搜索时 query_search 设 role=tool
        # 过滤——handler 必须放行 tool 角色，否则 tool 消息全被丢弃恒空。契约
        # SearchMessageItem.role 已加 "tool" 枚举值（v1 类型不变，新枚举值）。
        if role not in (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL):
            continue
        if role is None:   # pyright 收窄：getattr 带默认值是 MessageRole | None
            continue
        response_id = None
        message_id = getattr(msg, "message_id", None)   # 已是 MessageId（Message 模型字段）
        if registry is not None and message_id:
            gen = await registry.find_by_message(MessageId(message_id))
            if gen:
                response_id = gen.get("generation_id")
        items.append(rest_contract.SearchMessageItem(
            conversationId=str(getattr(msg, "session_id", "")),
            messageId=str(message_id),
            responseId=response_id,
            segmentId=None,
            role=Role(role.value),
            timestamp=str(msg.timestamp),
            snippet=_message_text(msg)[:100],
        ).wire_dict())
    resp = rest_contract.SearchMessagesResponse(
        items=items, has_more=has_more, cursor=next_cursor)
    return web.json_response(resp.wire_dict())


# ── 资源索引（spec §8） ─────────────────────────────────────────────────────


async def sessions_resources_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{conversationId}/resources — 按需扫描资源索引。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    category = request.query.get("category", "").strip()
    if category == "text":
        # 非资源类：text 走搜索，不作为资源类别
        return _error_response("CONTRACT_VALIDATION", "不支持的资源类别: text（走搜索）")
    if category not in _RESOURCE_CATEGORIES:
        return _error_response("CONTRACT_VALIDATION", f"不支持的资源类别: {category or '(空)'}")
    before = _parse_resource_before(request.query.get("before", "").strip() or None)
    limit = _parse_limit(request, default=20)
    items, has_more, cursor = await _scan_resources(agent, agent.message_repo, sid,
                                                    category, before, limit)
    resources = [rest_contract.ResourceItem(**it) for it in items]
    resp = rest_contract.ResourceListResponse(items=resources, has_more=has_more, cursor=cursor)
    return web.json_response(resp.wire_dict())


# ── generation 状态（spec §7.2） ────────────────────────────────────────────


def _generation_status(doc: dict) -> rest_contract.GenerationStatus:
    """generation 存储文档 → 契约 GenerationStatus（防御归一 finishReason/error）。

    loop 侧对 failed 存 finish_reason="error"、error 为 str（非契约形状），
    此处归一：finishReason 只透传 stop/tool_calls，error 非 dict 时包装为信封。
    """
    finish_reason = doc.get("finish_reason") or None
    if finish_reason not in ("stop", "tool_calls"):
        finish_reason = None
    err = doc.get("error")
    if isinstance(err, dict):
        error = err
    elif err is not None:
        error = {"message": str(err), "retryable": False}
    else:
        error = None
    return rest_contract.GenerationStatus(
        generationId=doc.get("generation_id", ""),
        conversationId=doc.get("session_id", ""),
        messageId=doc.get("message_id", ""),
        userMessageId=doc.get("user_message_id", ""),
        status=doc.get("status", "running"),
        model=doc.get("model") or None,
        finishReason=FinishReason(finish_reason) if finish_reason is not None else None,
        error=error,
        createdAt=doc.get("created_at", ""),
        finishedAt=doc.get("finished_at") or None,
    )


async def sessions_generations_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{conversationId}/generations — generation 列表（status/cursor/limit）。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)
    status = request.query.get("status", "").strip() or None
    cursor = request.query.get("cursor", "").strip() or None
    limit = _parse_limit(request, default=20, cap=100)
    registry = _generation_registry(agent)
    if registry is None:
        resp = rest_contract.GenerationListResponse(generations=[], has_more=False, cursor=None)
        return web.json_response(resp.wire_dict())
    docs, has_more, next_cursor = await registry.query_by_session(
        SessionId(sid), status=status, cursor=cursor, limit=limit)
    generations = [_generation_status(d) for d in docs]
    resp = rest_contract.GenerationListResponse(
        generations=generations, has_more=has_more, cursor=next_cursor)
    return web.json_response(resp.wire_dict())


async def generation_detail_handler(request: web.Request) -> web.Response:
    """GET /api/generations/{generationId} — generation 详情（未知 → 404 GENERATION_NOT_FOUND）。"""
    agent = request.app["agent"]
    gid = request.match_info["generationId"]
    registry = _generation_registry(agent)
    if registry is not None:
        doc = await registry.get(GenerationId(gid))
    else:
        doc = None
    if doc is None:
        return _error_response("GENERATION_NOT_FOUND", f"generation 不存在: {gid}", 404)
    return web.json_response(_generation_status(doc).wire_dict())


# ── pending 查询（spec §7.3） ───────────────────────────────────────────────


async def sessions_pending_handler(request: web.Request) -> web.Response:
    """GET /api/sessions/{conversationId}/pending — pending 消息（按会话）+ 审批（全局）。"""
    agent = request.app["agent"]
    sid = request.match_info["conversationId"]
    if not await _session_exists(agent, sid):
        return _error_response("SESSION_NOT_FOUND", f"会话不存在: {sid}", 404)

    pending_msgs = []
    # F7：None 归属（v1/未归属）映射到活动会话——仅活动会话展示，非活动会话不泄漏；
    # active_id 为空（防御，无活动会话）时 None 归属不展示。
    active_id = getattr(getattr(agent, "session_manager", None), "active_id", None)
    for pm in getattr(agent, "pending_messages", None) or []:
        cid = pm.conversation_id
        effective = str(cid) if cid is not None else (str(active_id) if active_id else None)
        if effective != str(sid):
            continue
        message_id = getattr(pm, "message_id", None)
        if not message_id:
            continue  # 未预分配 message_id 的排队消息无稳定 ID，跳过
        # _PendingMessage 无创建时间戳字段 → createdAt 用查询时刻（best-effort）
        pending_msgs.append(rest_contract.PendingMessageItem(
            messageId=str(message_id),
            snippet=(getattr(pm, "text", "") or "")[:50],
            createdAt=now_iso(),
        ))

    approvals = []
    tool_manager = getattr(getattr(agent, "tool_adapter", None), "_manager", None)  # noqa: SLF001
    if tool_manager is not None and hasattr(tool_manager, "get_pending_approvals"):
        try:
            for request_id, tool, params in tool_manager.get_pending_approvals():
                approvals.append(rest_contract.PendingApprovalItem(
                    requestId=str(request_id), tool=str(tool),
                    params=dict(params) if isinstance(params, dict) else {}))
        except Exception as e:
            logger.debug("get_pending_approvals 失败: %s", e)

    resp = rest_contract.PendingQueryResponse(
        pendingMessages=pending_msgs, pendingApprovals=approvals)
    return web.json_response(resp.wire_dict())


# ── 注册 ────────────────────────────────────────────────────────────────────


def register_session_routes(app: web.Application) -> None:
    """注册全部 REST 会话路由到 aiohttp Application（走 app 级产品认证中间件）。"""
    app.router.add_get("/api/sessions", sessions_list_handler)
    app.router.add_post("/api/sessions", sessions_create_handler)
    app.router.add_get("/api/sessions/{conversationId}", sessions_detail_handler)
    app.router.add_patch("/api/sessions/{conversationId}", sessions_rename_handler)
    app.router.add_delete("/api/sessions/{conversationId}", sessions_delete_handler)
    app.router.add_post("/api/sessions/{conversationId}/activate", sessions_activate_handler)
    app.router.add_get("/api/sessions/{conversationId}/messages", sessions_messages_handler)
    app.router.add_get("/api/sessions/{conversationId}/resources", sessions_resources_handler)
    app.router.add_get("/api/sessions/{conversationId}/generations", sessions_generations_handler)
    app.router.add_get("/api/sessions/{conversationId}/pending", sessions_pending_handler)
    app.router.add_get("/api/messages/{messageId}/context", message_context_handler)
    app.router.add_get("/api/search/messages", search_messages_handler)
    app.router.add_get("/api/generations/{generationId}", generation_detail_handler)
