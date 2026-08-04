"""SessionManager — 多会话缓存池 + 活动指针。

批次 3 (multi-session) 核心组件。职责：
  - 内存缓存池（LRU 顺序，最多 max_sessions），每会话一个 SessionMemory 实例；
  - 活动会话指针 active_id（默认 "default"，eager 构造空实例）；
  - resolve/switch/CRUD/list/evict/save_all 生命周期编排；
  - 委托 repo（SessionRepository）+ msg_repo（MessageRepository）。

resolve 规则（协议 v2 起支持多会话）：
  - protocol_version < 2            → 恒 "default"
  - protocol_version >= 2 且 conversation_id → 该会话
  - protocol_version >= 2 且 None    → 当前活动会话
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections import OrderedDict

from core.schema.utils import now_iso
from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository, SessionSummary
from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)

DEFAULT_SESSION = SessionId("default")
"""活动会话回落目标（删除活动会话时）。"""


def _encode_cursor(updated_at: str, session_id: str) -> str:
    """编码 keyset 游标：base64url(`updated_at|session_id`)。"""
    raw = f"{updated_at}|{session_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """解码 keyset 游标 → (updated_at, session_id)。格式非法抛 ValueError。"""
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    updated_at, _, sid = raw.partition("|")
    if not updated_at or not sid:
        raise ValueError("游标格式非法")
    return updated_at, sid


class SessionManager:
    """多会话缓存池 + 活动指针。"""

    def __init__(self, repo: SessionRepository, msg_repo,
                 max_sessions: int = 20, max_messages: int = 20):
        """初始化 SessionManager。

        Args:
            repo: SessionRepository（或等价 fake）——会话持久化。
            msg_repo: MessageRepository（或等价 fake）——消息归档。
            max_sessions: 缓存池会话数上限，超出触发逐出。
            max_messages: 每个 SessionMemory 的消息窗口上限。
        """
        self._repo = repo
        self._msg_repo = msg_repo
        self._max_sessions = max_sessions
        self._max_messages = max_messages
        # OrderedDict：访问/插入移到尾部，头部即最久未用（LRU）
        self._sessions: OrderedDict[SessionId, SessionMemory] = OrderedDict()
        self._loaded: set[SessionId] = set()
        self._active_id = DEFAULT_SESSION
        self._character_engine = None
        # eager default 空实例
        self._sessions[DEFAULT_SESSION] = SessionMemory(
            max_messages=max_messages,
            session_id=DEFAULT_SESSION,
            repo=repo,
            msg_repo=msg_repo,
        )

    # ---- 活动指针 ----

    @property
    def active_id(self) -> SessionId:
        return self._active_id

    @property
    def active(self) -> SessionMemory:
        return self._sessions[self._active_id]

    # ---- 缓存访问 ----

    def get(self, sid) -> SessionMemory:
        """内存命中返回；未命中构造空实例并缓存（不触发 load）。"""
        sid = SessionId(sid)
        session = self._sessions.get(sid)
        if session is None:
            session = SessionMemory(
                max_messages=self._max_messages,
                session_id=sid,
                repo=self._repo,
                msg_repo=self._msg_repo,
            )
            self._sessions[sid] = session
        else:
            self._sessions.move_to_end(sid)
        return session

    async def ensure_loaded(self, sid) -> SessionMemory:
        """首次调用 await load（Mongo → JSONL fallback）；重复调用不重复 load。"""
        sid = SessionId(sid)
        session = self.get(sid)
        if sid not in self._loaded:
            self._loaded.add(sid)
            await session.load(sid)
        return session

    # ---- resolve / switch ----

    async def has_session(self, sid) -> bool:
        """会话是否存在：内存缓存命中 或 持久层有 doc。

        activate_session 404 判定用：`switch` 对未知 sid 会构造空会话（懒加载语义），
        因此必须在切 active 前用此方法区分「已知会话」与「未知会话」。
        内存缓存覆盖已构造/已加载会话；持久层覆盖已落库但缓存逐出的会话。
        """
        sid = SessionId(sid)
        if sid in self._sessions:
            return True
        if self._repo is not None:
            try:
                return await self._repo.load(sid) is not None
            except Exception:
                logger.debug("has_session: repo.load 失败 sid=%s", sid, exc_info=True)
        return False

    async def resolve(self, conversation_id, protocol_version) -> SessionMemory:
        """按协议版本 + conversation_id 解析目标会话并确保加载。"""
        version = int(protocol_version)
        if version < 2:
            target = DEFAULT_SESSION
        elif conversation_id:
            target = SessionId(conversation_id)
        else:
            target = self._active_id
        return await self.ensure_loaded(target)

    async def switch(self, sid) -> SessionMemory:
        """切换到目标会话。

        顺序：resolve/ensure-loaded → outgoing._pre_save_hook()（同步 reflector
        buffer）→ 换 active 指针 → bind 的 engine.attach_session(target)。
        """
        sid = SessionId(sid)
        target = await self.ensure_loaded(sid)
        outgoing = self._sessions.get(self._active_id)
        if outgoing is not None and outgoing._pre_save_hook is not None:
            outgoing._pre_save_hook()
        self._active_id = sid
        if self._character_engine is not None:
            self._character_engine.attach_session(target)
        return target

    def bind_character_engine(self, engine) -> None:
        """绑定 CharacterEngine；切换会话时对 target 调用 attach_session。"""
        self._character_engine = engine
        engine.attach_session(self._sessions[self._active_id])

    # ---- CRUD ----

    async def create(self, title: str = "") -> SessionSummary:
        """创建空会话：conv_<uuid4 hex[:12]>，落库（repo.create）。"""
        sid = SessionId(f"conv_{uuid.uuid4().hex[:12]}")
        await self._repo.create(sid, title)
        return SessionSummary(
            session_id=sid,
            message_count=0,
            updated_at=now_iso(),
            title=title,
            active=False,
        )

    async def list(self, *, cursor: str | None = None, limit: int = 50,
                   search: str = "") -> tuple[list[SessionSummary], bool, str | None]:
        """列出会话摘要。

        数据源 repo.list_all()（≤200，updated_at DESC）；内存过滤：
          - search 仅匹配 title（大小写不敏感），不混消息文本；
          - keyset 游标 base64(`updated_at|session_id`)；
          - items 填 active（按 active_id 比较）。

        Returns:
            (items, has_more, next_cursor)。
        """
        summaries = await self._repo.list_all()
        if search:
            q = search.lower()
            summaries = [s for s in summaries if q in s.title.lower()]
        # 排序：updated_at DESC，同 updated_at 时 session_id ASC tiebreak
        # （稳定排序：先 session_id ASC，再 updated_at DESC 主键）
        summaries.sort(key=lambda s: s.session_id)
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        if cursor:
            c_ts, c_sid = _decode_cursor(cursor)
            summaries = [
                s for s in summaries
                if s.updated_at < c_ts or (s.updated_at == c_ts and s.session_id > c_sid)
            ]
        has_more = len(summaries) > limit
        items = summaries[:limit]
        for s in items:
            s.active = (s.session_id == self._active_id)
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.updated_at, last.session_id)
        return items, has_more, next_cursor

    async def rename(self, sid, title: str) -> bool:
        """重命名会话，返回是否更新成功。"""
        return await self._repo.rename(SessionId(sid), title)

    async def delete(self, sid) -> int:
        """删除会话：msg_repo 清归档 + repo 删文档 + 丢缓存。

        若删除的是活动会话，active 回落 "default"。返回删除的归档消息数。
        """
        sid = SessionId(sid)
        deleted_messages = 0
        if self._msg_repo is not None:
            deleted_messages = await self._msg_repo.delete_by_session(sid)
        await self._repo.delete(sid)
        self._sessions.pop(sid, None)
        self._loaded.discard(sid)
        if sid == self._active_id:
            self._active_id = DEFAULT_SESSION
        return deleted_messages

    # ---- 持久化编排 ----

    async def save_all(self) -> None:
        """持久化所有已加载会话。"""
        for sid in list(self._sessions):
            if sid in self._loaded:
                await self._sessions[sid].save()

    async def connect_db(self) -> bool:
        """建立 repo 连接。"""
        return await self._repo.connect()

    async def health_check(self) -> bool:
        """repo 健康检查（断开时尝试重连）。"""
        return await self._repo.health_check()

    async def load_active(self) -> None:
        """加载当前活动会话（启动时调用）。"""
        await self.ensure_loaded(self._active_id)

    def evict_if_needed(self) -> None:
        """超出 max_sessions 时逐出最久未用非活动会话（逐出前 save）。

        同步方法：save 经 fire-and-forget task 调度（无运行事件循环时跳过）。
        """
        while len(self._sessions) > self._max_sessions:
            evicted: tuple[SessionId, SessionMemory] | None = None
            for sid, session in list(self._sessions.items()):
                if sid == self._active_id:
                    continue
                evicted = (sid, session)
                break
            if evicted is None:
                break  # 只剩活动会话，无法逐出
            sid, session = evicted
            if sid in self._loaded:
                try:
                    asyncio.ensure_future(session.save())
                except RuntimeError:
                    # 无运行事件循环：跳过保存（会话数据已在前次 save 落库）
                    logger.debug("evict: 无运行事件循环，跳过 save session=%s", sid)
            self._sessions.pop(sid, None)
            self._loaded.discard(sid)
