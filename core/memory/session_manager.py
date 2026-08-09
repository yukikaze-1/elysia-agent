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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.schema.utils import now_iso
from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository, SessionSummary
from core.storage.message_repo import MessageRepository
from core.memory.session import SessionMemory

if TYPE_CHECKING:
    from core.personality.engine import CharacterEngine

logger = logging.getLogger(__name__)

DEFAULT_SESSION = SessionId("default")
"""活动会话回落目标（删除活动会话时）。"""


class SessionBusyError(RuntimeError):
    """会话操作被拒——agent 忙，或会话仍有已接收/在途工作。

    批次 3 review 修复（F1-a）：activate 入口先经 FSM 门禁（is_accepting_input）
    拒绝忙时切换；SessionManager.switch 内置 busy check 作为双保险。删除路径还会
    检查按会话计数的 accepted work，避免 ACK 后到最终消费前被删除。
    """


class SessionNotFoundError(RuntimeError):
    """目标会话不存在（已删除/从未创建）——最终 resolver 严格化（9abc P2-1）。

    显式 v2 conversation_id 在最终消费（CognitiveLoop resolve）时必须先验证存在，
    禁止懒创建复活已删除会话（9A-3「已删除会话不得重建」）。缺省/`default` 不受影响。
    """


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

    def __init__(self, repo: SessionRepository, msg_repo: MessageRepository,
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
        # F1(a)：switch 忙时拒绝的检查回调（agent 注入 `not status.is_accepting_input`）
        self._busy_check = None
        # 会话生命周期锁 + accepted work 引用计数：消息在 ACK 前预占目标会话，
        # 直到 CognitiveLoop 终态（含取消/异常）才释放。删除先在同一锁下检查计数并
        # 写入 deleting tombstone，闭合 EventBus/pending/生成中的 TOCTOU 窗口。
        self._lifecycle_lock = asyncio.Lock()
        self._work_counts: dict[SessionId, int] = {}
        self._deleting: set[SessionId] = set()
        self._deleted: set[SessionId] = set()
        # Mongo 离线时 CRUD 仍需在当前进程内保持完整的 list/detail/rename 语义。
        # repo 恢复后，持久层摘要与这些本地摘要在读取时合并去重。
        self._local_summaries: dict[SessionId, SessionSummary] = {}
        self._local_title_overrides: set[SessionId] = set()
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
        # 防御（review 修复）：_active_id 指向已删会话时自愈回 default——
        # 正常路径 delete() 已负责回落，此处兜底外部直接操作缓存池的场景。
        if self._active_id not in self._sessions:
            logger.warning("active: _active_id=%s 已不在缓存池，自愈回 default", self._active_id)
            self._active_id = DEFAULT_SESSION
        return self._sessions[self._active_id]

    # ---- 缓存访问 ----

    def get(self, sid: SessionId) -> SessionMemory:
        """内存命中返回；未命中构造空实例并缓存（不触发 load）。

        F7：构造新会话后调用 evict_if_needed()——超 max_sessions 时逐出最旧非活动
        会话（此前 evict 无调用点，缓存无界增长）。
        """
        sid = SessionId(sid)
        if sid in self._deleting or sid in self._deleted:
            raise SessionNotFoundError(sid)
        session = self._sessions.get(sid)
        if session is None:
            session = SessionMemory(
                max_messages=self._max_messages,
                session_id=sid,
                repo=self._repo,
                msg_repo=self._msg_repo,
            )
            self._sessions[sid] = session
            self.evict_if_needed()
        else:
            self._sessions.move_to_end(sid)
        return session

    async def ensure_loaded(self, sid: SessionId) -> SessionMemory:
        """首次调用 await load（Mongo → JSONL fallback）；重复调用不重复 load。"""
        sid = SessionId(sid)
        session = self.get(sid)
        if sid not in self._loaded:
            self._loaded.add(sid)
            await session.load(sid)
        return session

    # ---- resolve / switch ----

    async def has_session(self, sid: SessionId) -> bool:
        """会话是否存在：内存缓存命中 或 持久层有 doc。

        activate_session 404 判定用：`switch` 对未知 sid 会构造空会话（懒加载语义），
        因此必须在切 active 前用此方法区分「已知会话」与「未知会话」。
        内存缓存覆盖已构造/已加载会话；持久层覆盖已落库但缓存逐出的会话。
        """
        sid = SessionId(sid)
        if sid in self._deleting or sid in self._deleted:
            return False
        if sid in self._sessions:
            return True
        if sid in self._local_summaries:
            return True
        if self._repo is not None:
            try:
                return await self._repo.load(sid) is not None
            except Exception:
                logger.debug("has_session: repo.load 失败 sid=%s", sid, exc_info=True)
        return False

    async def resolve(self, conversation_id: str | None, protocol_version: int) -> SessionMemory:
        """按协议版本 + conversation_id 解析目标会话并确保加载。

        9abc P2-1（最终 resolver 严格化）：显式 v2 conversation_id 先经
        ``has_session`` 验证存在（内存缓存命中零开销，未知才查持久层）——已删除/
        未知会话抛 ``SessionNotFoundError``，禁止懒创建复活幽灵会话；缺省/
        `default`/v1 不受影响。

        Raises:
            SessionNotFoundError: protocol_version>=2 且显式 conversation_id
                对应的会话不存在。
        """
        version = int(protocol_version)
        if version < 2:
            target = DEFAULT_SESSION
        elif conversation_id:
            target = SessionId(conversation_id)
        else:
            target = self._active_id
        if version >= 2 and conversation_id and not await self.has_session(target):
            raise SessionNotFoundError(target)
        return await self.ensure_loaded(target)

    async def reserve_work(self, conversation_id: str | None,
                           protocol_version: int) -> SessionMemory:
        """原子解析目标会话并登记一份已接收工作引用。

        v2 消息必须在对客户端确认 accepted 之前调用本方法。返回的会话 ID 是本次
        工作的固定归属；即使请求省略 ``conversationId``，后续活动会话切换也不会
        改变它。引用存续期间 ``delete`` 会返回 ``SessionBusyError``。

        Raises:
            SessionNotFoundError: 目标会话未知、已删除或正在删除。
        """
        async with self._lifecycle_lock:
            version = int(protocol_version)
            if version < 2:
                target = DEFAULT_SESSION
            elif conversation_id:
                target = SessionId(conversation_id)
            else:
                target = self._active_id
            if target in self._deleting or target in self._deleted:
                raise SessionNotFoundError(target)
            if version >= 2 and conversation_id and not await self.has_session(target):
                raise SessionNotFoundError(target)
            session = await self.ensure_loaded(target)
            self._work_counts[target] = self._work_counts.get(target, 0) + 1
            return session

    def release_work(self, sid: SessionId) -> None:
        """释放 ``reserve_work`` 建立的一份引用；重复/迟到释放安全 no-op。"""
        sid = SessionId(sid)
        count = self._work_counts.get(sid, 0)
        if count <= 1:
            self._work_counts.pop(sid, None)
        else:
            self._work_counts[sid] = count - 1

    def work_count(self, sid: SessionId) -> int:
        """返回会话当前 accepted/in-flight 工作数（删除门禁与诊断用）。"""
        return self._work_counts.get(SessionId(sid), 0)

    def clear_work(self) -> None:
        """生命周期停止后清空引用计数，避免同一 Agent restart 残留。"""
        self._work_counts.clear()

    def set_busy_check(self, check: "Callable[[], bool] | None") -> None:
        """注入忙时检查回调（agent 构造后设置：`not status.is_accepting_input`）。

        switch 命中忙（生成/TTS 中）→ 抛 SessionBusyError，不执行换绑（F1-a 双保险）。
        """
        self._busy_check = check

    async def switch(self, sid: SessionId) -> SessionMemory:
        """切换到目标会话。

        顺序：busy check（F1-a）→ resolve/ensure-loaded → outgoing._pre_save_hook()
        （同步 reflector buffer）→ 换 active 指针 → bind 的 engine.attach_session(target)。

        Raises:
            SessionBusyError: 已注入 busy check 且 agent 忙（生成/TTS 中）时拒绝切换。
        """
        sid = SessionId(sid)
        async with self._lifecycle_lock:
            if self._busy_check is not None and self._busy_check():
                logger.debug("switch 拒绝：agent 忙（busy check 命中），sid=%s", sid)
                raise SessionBusyError(sid)
            if sid in self._deleting or sid in self._deleted:
                raise SessionNotFoundError(sid)
            target = await self.ensure_loaded(sid)
            outgoing = self._sessions.get(self._active_id)
            if outgoing is not None and outgoing._pre_save_hook is not None:
                outgoing._pre_save_hook()
            self._active_id = sid
            if self._character_engine is not None:
                self._character_engine.attach_session(target)
            return target

    def bind_character_engine(self, engine: "CharacterEngine") -> None:
        """绑定 CharacterEngine；切换会话时对 target 调用 attach_session。"""
        self._character_engine = engine
        engine.attach_session(self._sessions[self._active_id])

    # ---- CRUD ----

    async def create(self, title: str = "") -> SessionSummary:
        """创建空会话：conv_<uuid4 hex[:12]>，落库（repo.create）+ 立即入内存缓存。

        9abc P1-1：create 后追加 ``ensure_loaded(sid)``——把新会话放入内存缓存 +
        ``_loaded``，使 ``has_session()`` 立即可见。MongoDB 离线时 ``repo.create``
        静默不落库，但内存缓存仍保有该会话（JSONL fallback 降级语义），REST 创建
        成功后 text/附件/voice 携带该 conversationId 不再被严格检查判为幽灵会话。
        """
        sid = SessionId(f"conv_{uuid.uuid4().hex[:12]}")
        await self._repo.create(sid, title)
        await self.ensure_loaded(sid)
        summary = SessionSummary(
            session_id=sid,
            message_count=0,
            updated_at=now_iso(),
            title=title,
            active=False,
        )
        self._local_summaries[sid] = summary
        if title:
            self._local_title_overrides.add(sid)
        return self._copy_summary(summary)

    @staticmethod
    def _copy_summary(summary: SessionSummary) -> SessionSummary:
        """复制摘要，避免 list 填 active 时修改 repo/local 缓存对象。"""
        return SessionSummary(
            session_id=SessionId(summary.session_id),
            message_count=summary.message_count,
            updated_at=summary.updated_at,
            title=summary.title,
            active=summary.active,
        )

    async def _merged_summaries(self) -> list[SessionSummary]:
        """合并 Mongo 摘要与进程内离线摘要，并过滤删除 tombstone。"""
        try:
            persisted = await self._repo.list_all()
        except Exception:
            logger.warning("session list_all 失败，降级使用进程内摘要", exc_info=True)
            persisted = []
        merged = {
            SessionId(s.session_id): self._copy_summary(s)
            for s in persisted
            if SessionId(s.session_id) not in self._deleting
            and SessionId(s.session_id) not in self._deleted
        }
        for sid, local in self._local_summaries.items():
            if sid in self._deleting or sid in self._deleted:
                continue
            current = merged.get(sid)
            if current is None:
                merged[sid] = self._copy_summary(local)
                continue
            current.message_count = max(current.message_count, local.message_count)
            current.updated_at = max(current.updated_at, local.updated_at)
            if sid in self._local_title_overrides or not current.title:
                current.title = local.title
        return list(merged.values())

    async def get_summary(self, sid: SessionId) -> SessionSummary | None:
        """获取单会话摘要；Mongo 离线时同样可返回本地创建/重命名结果。"""
        sid = SessionId(sid)
        for summary in await self._merged_summaries():
            if summary.session_id == sid:
                summary.active = (sid == self._active_id)
                return summary
        return None

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
        summaries = await self._merged_summaries()
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

    async def rename(self, sid: SessionId, title: str) -> bool:
        """重命名会话；Mongo 离线时更新进程内摘要并保持 REST 读后写一致。"""
        sid = SessionId(sid)
        if sid in self._deleting or sid in self._deleted:
            return False
        known_local = sid in self._sessions or sid in self._local_summaries
        persisted = await self._repo.rename(sid, title)
        if not persisted and not known_local:
            return False
        current = self._local_summaries.get(sid)
        if current is None:
            current = SessionSummary(
                session_id=sid, message_count=0, updated_at=now_iso(), title=title)
            self._local_summaries[sid] = current
        else:
            current.title = title
            current.updated_at = now_iso()
        self._local_title_overrides.add(sid)
        return True

    async def delete(
        self,
        sid: SessionId,
        *,
        before_delete: Callable[[SessionId], Awaitable[None]] | None = None,
    ) -> int:
        """删除会话：msg_repo 清归档 + repo 删文档 + 丢缓存。

        保护（review 修复）：默认会话（"default"）不可删除——拒绝并返回 0。
        若删除的是活动会话，active 回落 "default" 并重绑 CharacterEngine
        （attach_session(default)），防 F4 孤儿 state（engine.state 仍挂在
        已删会话的 character_state 上）。返回删除的归档消息数。
        """
        sid = SessionId(sid)
        async with self._lifecycle_lock:
            if sid == DEFAULT_SESSION:
                logger.debug("delete: 拒绝删除默认会话 default")
                return 0
            if sid in self._deleted:
                return 0
            if sid in self._deleting or self.work_count(sid) > 0:
                logger.debug("delete 拒绝：会话有 accepted/in-flight 工作 sid=%s count=%d",
                             sid, self.work_count(sid))
                raise SessionBusyError(sid)
            # tombstone 必须先于附件扫描/存储删除：扫描期间到达的新消息也应被拒绝。
            self._deleting.add(sid)

        try:
            if before_delete is not None:
                await before_delete(sid)
            deleted_messages = 0
            if self._msg_repo is not None:
                deleted_messages = await self._msg_repo.delete_by_session(sid)
            await self._repo.delete(sid)
        except Exception:
            async with self._lifecycle_lock:
                self._deleting.discard(sid)
            raise

        async with self._lifecycle_lock:
            self._sessions.pop(sid, None)
            self._loaded.discard(sid)
            self._local_summaries.pop(sid, None)
            self._local_title_overrides.discard(sid)
            self._deleting.discard(sid)
            self._deleted.add(sid)
            if sid == self._active_id:
                self._active_id = DEFAULT_SESSION
                # F4 修复：重绑 engine 到 default——防 _active_id 已回落但
                # engine.state 视图仍挂在已删会话的 character_state 上（孤儿 state）。
                if self._character_engine is not None:
                    self._character_engine.attach_session(self._sessions[DEFAULT_SESSION])
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
        保护（F7）：永不逐出默认会话 default（v1 锚点，与 delete 守卫同主题）。
        """
        while len(self._sessions) > self._max_sessions:
            evicted: tuple[SessionId, SessionMemory] | None = None
            for sid, session in list(self._sessions.items()):
                if sid == self._active_id or sid == DEFAULT_SESSION:
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
