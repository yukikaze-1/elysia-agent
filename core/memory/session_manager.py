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
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from paths import ProjectPath

from core.character.binding import CharacterBinding, normalize_character_binding
from core.schema.utils import now_iso
from core.schema.types import SessionId
from core.storage.base import MAX_SEARCH_LEN, StorageUnavailableError
from core.storage.session_repo import (
    SessionRepository,
    SessionSummary,
    derive_session_title,
)
from core.storage.message_repo import MessageRepository
from core.memory.session import SessionMemory
from Module._common.task_scope import AsyncTaskScope

if TYPE_CHECKING:
    from core.memory.memory_reflector import MemoryReflector

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


def _precedes(a: SessionSummary, b: SessionSummary) -> bool:
    """在 `updated_at DESC, session_id ASC` 序中，a 是否严格先于 b。"""
    if a.updated_at != b.updated_at:
        return a.updated_at > b.updated_at
    return str(a.session_id) < str(b.session_id)


def _after_cursor(s: SessionSummary, after: tuple[str, str]) -> bool:
    """s 是否严格位于游标之后（同一序）——契约谓词 `(U' < U) OR (U' = U AND S' > S)`。"""
    c_ts, c_sid = after
    return s.updated_at < c_ts or (s.updated_at == c_ts and str(s.session_id) > c_sid)


def _sort_summaries(items: list[SessionSummary]) -> None:
    """就地排序：`updated_at DESC`，同值时 `session_id ASC`（两遍稳定排序）。"""
    items.sort(key=lambda s: str(s.session_id))
    items.sort(key=lambda s: s.updated_at, reverse=True)


def _title_matches(title: str, search: str) -> bool:
    """title 的大小写不敏感子串匹配（size search 语义的唯一判据）。

    **与存储侧同族语义**：持久层那条路径由 Mongo `$regex` + `$options:"i"` 过滤，
    覆盖层（进程内本地摘要）没有 Mongo 可问——这里用 `re.IGNORECASE` 正则而非
    `str.lower()`，使两条路径都是"大小写不敏感正则"（内存 fake 的 `$regex` 也是
    Python `re`）。用 `lower()` 会与正则产生可观测分歧（如 U+017F 'ſ' 在
    `re.IGNORECASE` 下匹配 's'、在 `lower()` 下不匹配）——即同一条标题在持久层
    命中、在覆盖层不命中。
    """
    return re.search(re.escape(search), title, re.IGNORECASE) is not None


class SessionManager:
    """多会话缓存池 + 活动指针。"""

    def __init__(self, repo: SessionRepository, msg_repo: MessageRepository,
                 max_sessions: int = 20, max_messages: int = 20,
                 pending_cleanup_file: str | Path | None = None,
                 cascade_repos: "Sequence[tuple[str, Any]] | None" = None):
        """初始化 SessionManager。

        Args:
            repo: SessionRepository（或等价 fake）——会话持久化。
            msg_repo: MessageRepository（或等价 fake）——消息归档。
            max_sessions: 缓存池会话数上限，超出触发逐出。
            max_messages: 每个 SessionMemory 的消息窗口上限。
            pending_cleanup_file: 待清理 journal 路径（测试注入），默认
                ProjectPath.SESSION_PENDING_CLEANUP。
            cascade_repos: **会话域集合的级联删除清单**（#21b）——`(label, repo)`
                序列，每个 repo 必须有 `delete_by_session(session_id)`。删除会话
                提交后逐个调用（失败进 pending journal 重试）。
                **为什么要一份显式清单**：`token_usage` / `generations` /
                `character_versions` 都按 session_id 分区，漏掉任何一个都会留下
                永久不可达的孤儿行。清单由 `agent.py` 构造时从真实 repo 组装，
                并由 `core/memory/tests/test_session_cascade.py` 的覆盖守卫断言
                "所有带 delete_by_session 的仓储都在清单里"——**新增会话域集合
                忘了接线就红**，而不是等下次数据审计才发现。
        """
        self._repo = repo
        self._msg_repo = msg_repo
        # #21b：会话域集合的级联删除清单（label 只用于日志/诊断）
        self._cascade_repos: list[tuple[str, Any]] = list(cascade_repos or [])
        self._max_sessions = max_sessions
        self._max_messages = max_messages
        # B6/P2-01：共享 MemoryReflector（agent 注入）——挂到所有会话使 reflector
        # 状态经 save/load 持久化（真正的 per-session reflector 由 C3 收口）。
        self._shared_reflector = None
        # OrderedDict：访问/插入移到尾部，头部即最久未用（LRU）
        self._sessions: OrderedDict[SessionId, SessionMemory] = OrderedDict()
        self._loaded: set[SessionId] = set()
        # B2/P1-01：每会话 in-flight load future——并发 ensure_loaded 合并为一次 load；
        # 仅成功后加入 _loaded，失败移除 future 允许重试。
        self._load_futures: dict[SessionId, asyncio.Future[SessionMemory]] = {}
        # ARCH-INC-P2-03：逐出 save 的后台任务作用域。逐出后会话即离开缓存池，
        # 无法再被 save_all() 覆盖——必须由 scope 持有强引用 + 观察异常 + 关机 drain。
        self._evict_tasks = AsyncTaskScope("session-evictions")
        self._active_id = DEFAULT_SESSION
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
        # 上一次 `list()` 若降级为进程内摘要并给出了续页游标，把它记在这里：
        # 离线模式的多页遍历要能继续，而"从持久层页翻到一半失败"必须如实抛出
        # （不得伪装成 has_more=false）。见 `list()` 的降级分支。
        self._degraded_cursor: str | None = None
        # review 复核 #5 D2：提交后清理（附件 refs + 归档）由 agent 注入（经
        # set_attachment_cleanup 接 release_session_attachments）；失败登记
        # _pending_cleanup journal，健康循环 retry_pending_cleanup 重试。
        self._attachment_cleanup: Callable[[SessionId], Awaitable[None]] | None = None
        self._pending_cleanup: set[SessionId] = set()
        self._pending_cleanup_file_path = pending_cleanup_file
        self._load_pending_cleanup()
        # eager default 空实例
        self._sessions[DEFAULT_SESSION] = SessionMemory(
            max_messages=max_messages,
            session_id=DEFAULT_SESSION,
            repo=repo,
            msg_repo=msg_repo,
        )
        # clean-start 会话发现（BE-P1-05）：default 恒有合法本地摘要
        # （updated_at=now、title="", message_count=0）——空 Mongo 时 REST 列表/详情
        # 仍可见 default；合并时 max(updated_at) 保证新鲜本地摘要胜出陈旧 Mongo 文档
        # （见 _merged_summaries）。repo 此时未连接，落库交给 ensure_default_persisted
        # （由生命周期健康循环的 session 恢复边界触发）。
        self._local_summaries[DEFAULT_SESSION] = SessionSummary(
            session_id=DEFAULT_SESSION,
            message_count=0,
            updated_at=now_iso(),
            title="",
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
            if self._shared_reflector is not None:
                session.set_memory_reflector(self._shared_reflector)
            self._sessions[sid] = session
            self.evict_if_needed()
        else:
            self._sessions.move_to_end(sid)
        return session

    def bind_memory_reflector(self, mr: "MemoryReflector") -> None:
        """把 MemoryReflector 挂到所有（含未来）会话（B6/P2-01）。

        reflector 状态经 session save/load 持久化——不接线则反思状态重启丢失。
        """
        self._shared_reflector = mr
        for session in self._sessions.values():
            session.set_memory_reflector(mr)

    async def ensure_loaded(self, sid: SessionId) -> SessionMemory:
        """首次调用 await load（Mongo → JSONL fallback）；重复调用不重复 load。

        B2/P1-01：并发 caller 经 per-session in-flight future 合并为一次 load。
        loaded 标记规则：load 成功（Mongo/JSONL 有数据）或「存储可达但 not found」
        （合法新会话）都标记；存储不可用（load 失败）不标记——避免第二个并发
        caller 看到「已标记 loaded 的空会话」，也不让失败永久卡住（下次重试）。
        """
        sid = SessionId(sid)
        session = self.get(sid)
        if sid in self._loaded:
            return session
        future = self._load_futures.get(sid)
        if future is not None:
            await future
            return session
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._load_futures[sid] = future
        try:
            ok = await session.load(sid)
            if ok or not session._storage_unavailable:  # noqa: SLF001
                self._loaded.add(sid)
        finally:
            if not future.done():
                future.set_result(session)
            self._load_futures.pop(sid, None)
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

    def set_attachment_cleanup(
        self, cleanup: "Callable[[SessionId], Awaitable[None]] | None",
    ) -> None:
        """注入会话删除提交后的附件释放回调（review 复核 #5 D2）。

        agent 构造后接 `release_session_attachments`——删除会话 doc 成功后释放
        该会话归档消息的附件 refs；失败由 manager 登记待清理 journal 重试。
        """
        self._attachment_cleanup = cleanup

    async def switch(self, sid: SessionId) -> SessionMemory:
        """切换到目标会话。

        顺序：busy check（F1-a）→ resolve/ensure-loaded → 换 active 指针。
        Task 11（M3）：不再 bind engine.attach_session / outgoing._pre_save_hook——
        instance-scoped 状态按 runtime 权威，回合边界经 CharacterRuntime.flush()
        （registry）持久化，session 切换不重绑 engine。

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
            self._active_id = sid
            return target

    # ---- CRUD ----

    async def create(
        self,
        title: str = "",
        character_binding: object = None,
    ) -> SessionSummary:
        """创建空会话：conv_<uuid4 hex[:12]>，落库（repo.create）+ 立即入内存缓存。

        9abc P1-1：create 后追加 ``ensure_loaded(sid)``——把新会话放入内存缓存 +
        ``_loaded``，使 ``has_session()`` 立即可见。MongoDB 离线时 ``repo.create``
        静默不落库，但内存缓存仍保有该会话（JSONL fallback 降级语义），REST 创建
        成功后 text/附件/voice 携带该 conversationId 不再被严格检查判为幽灵会话。
        """
        sid = SessionId(f"conv_{uuid.uuid4().hex[:12]}")
        binding = normalize_character_binding(character_binding)
        try:
            await self._repo.create(sid, title, character_binding=binding)
        except StorageUnavailableError:
            # B2/P1-01：repo 未连接 → 进程内继续（local_summary 降级），不抛给 REST
            logger.debug("SessionManager.create: Mongo 离线，仅进程内创建 %s", sid)
        session = await self.ensure_loaded(sid)
        session.character_binding = binding
        summary = SessionSummary(
            session_id=sid,
            message_count=0,
            updated_at=now_iso(),
            title=title,
            active=False,
            character_binding=binding,
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
            character_binding=normalize_character_binding(summary.character_binding),
        )

    def _merge_summary(self, persisted: SessionSummary | None,
                       local: SessionSummary | None) -> SessionSummary | None:
        """合并"持久层摘要 + 进程内本地摘要"成一条**新**摘要（覆盖层语义单点）。

        规则与批次 3 一致（BE-P1-05 的合并优先级）：
          - `message_count` 取 `max`（本地窗口计数不得压低持久计数）；
          - `updated_at` 取 `max`——本地 `updated_at` 是**有效排序键**的一部分：
            `rename()` 把本地值设为 now，于是改过名的会话会被"抬"到列表最前；
            clean-start 的 default 本地摘要也因此胜出陈旧 Mongo 文档；
          - title 以本地覆盖为准（本地为空则沿用持久）；
          - binding 本地非空则覆盖。

        返回**新对象**：调用方（list/detail）会写 `active` 字段，不得污染
        repo 返回对象或 `_local_summaries` 里的缓存对象。
        """
        if persisted is None and local is None:
            return None
        base = persisted if persisted is not None else local
        assert base is not None  # 上面的分支已排除双 None
        merged = self._copy_summary(base)
        if persisted is None or local is None:
            return merged
        merged.message_count = max(persisted.message_count, local.message_count)
        merged.updated_at = max(persisted.updated_at, local.updated_at)
        if SessionId(local.session_id) in self._local_title_overrides or not merged.title:
            merged.title = local.title
        if local.character_binding is not None:
            merged.character_binding = normalize_character_binding(local.character_binding)
        return merged

    def _visible_local(self) -> dict[SessionId, SessionSummary]:
        """进程内本地摘要中未被删除 tombstone 遮蔽的部分。"""
        return {
            SessionId(sid): local
            for sid, local in self._local_summaries.items()
            if sid not in self._deleting and sid not in self._deleted
        }

    def _local_entries(self, persisted_by_id: dict[SessionId, SessionSummary],
                       after: tuple[str, str] | None,
                       search: str,
                       visible: dict[SessionId, SessionSummary] | None = None,
                       ) -> tuple[list[SessionSummary], set[str]]:
        """构造覆盖层侧的有序条目 + "同时存在于持久层"的 ID 集合。

        Args:
            persisted_by_id: 覆盖层对应持久记录的摘要（`summaries_by_ids` 结果）。
                离线降级时传 `{}`（此时全部本地条目都当本地-only 处理）。
            after: keyset 游标（None = 首页）。
            search: title 子串（大小写不敏感）；空串不过滤。
            visible: **调用方快照**的可见本地摘要。必须与 `persisted_by_id` 取自同
                一次快照——两次读取之间若有 `create()`/`rename()` 插进来，新条目
                会被当成"本地-only"（不在 `overlay_ids` 里），而它的持久记录又会被
                持久层流吐一次 ⇒ **同一条记录在一页里出现两次**。

        Returns:
            (按契约序排好的条目, 同时存在于持久层的会话 ID 集合)。后者用于把
            这些记录从持久层流里剔除——它们的**有效排序键**可能被本地值抬高，
            位置由覆盖层决定；若不剔除，同一条记录会在后续页**重复**出现。
        """
        entries: list[SessionSummary] = []
        overlay_ids: set[str] = set()
        source = self._visible_local() if visible is None else visible
        for sid, local in source.items():
            persisted = persisted_by_id.get(sid)
            if persisted is not None:
                overlay_ids.add(str(sid))
            merged = self._merge_summary(persisted, local)
            if merged is None:  # pragma: no cover - _visible_local 保证 local 非 None
                continue
            if search and not _title_matches(merged.title, search):
                continue
            if after is not None and not _after_cursor(merged, after):
                continue
            entries.append(merged)
        _sort_summaries(entries)
        return entries, overlay_ids

    async def _merged_page(self, after: tuple[str, str] | None, limit: int,
                           search: str) -> list[SessionSummary]:
        """持久层 keyset 分页 ⊕ 覆盖层的**归并**，返回至多 limit+1 条。

        为什么是归并而不是"先取全量再切片"（P2-01r 的成因）：
        持久层只逐页吐（`repo.list_page`，limit+1 探测），覆盖层（进程内写入的
        会话摘要，数量 = 本进程创建/改名/恢复过的会话数）在内存里是完整且有序的，
        两个有序序列做 k 路归并即可——内存占用与单页大小同阶，与数据集大小无关。

        **游标语义**：本方法的 `after` 是"客户端游标"（上页最后一条的**有效**键）。
        持久层的两条读都不带额外状态，因此每页都会把覆盖层重新合并一次；
        覆盖层里被抬高的记录若落在游标之前，会被 `_after_cursor` 正确滤掉，
        落在游标之后的则按有效键插回正确位置——不重不漏。
        """
        visible = self._visible_local()      # 快照一次，见 _local_entries 的 visible 说明
        persisted_by_id = await self._repo.summaries_by_ids(list(visible.keys()))
        entries, overlay_ids = self._local_entries(persisted_by_id, after, search,
                                                   visible=visible)
        out: list[SessionSummary] = []
        emitted: set[str] = set()            # 同一 session_id 只吐一次（见下）
        idx = 0
        stream_after = after
        stream_done = False
        while len(out) <= limit and not stream_done:
            window = await self._repo.list_page(limit=limit + 1, after=stream_after,
                                                search=search)
            if window:
                stream_after = (window[-1].updated_at, str(window[-1].session_id))
                # 未满窗（≤limit）⇒ 持久层已到末尾；满窗则可能还有下一页窗口。
                stream_done = len(window) <= limit
            else:
                stream_done = True
            k = 0
            while len(out) <= limit and (k < len(window) or idx < len(entries)):
                if k < len(window) and (idx >= len(entries)
                                        or _precedes(window[k], entries[idx])):
                    candidate = window[k]
                    k += 1
                    sid = str(candidate.session_id)
                    if sid in overlay_ids:
                        continue  # 由覆盖层代表（有效键/字段已在 entries 里合并）
                    if (SessionId(sid) in self._deleting
                            or SessionId(sid) in self._deleted):
                        continue  # 删除 tombstone：本地已删、持久层尚未消失
                    if sid in emitted:
                        # 同一 session_id 的**重复文档**（sessions 的 session_id 上没有
                        # 唯一索引，并发 upsert 可产生）。修前按 session_id 建字典天然
                        # 去重；下推分页后若不去重，列表会出现两条同 ID 记录。
                        logger.warning("session 列表发现重复文档 session_id=%s（已去重）", sid)
                        continue
                    emitted.add(sid)
                    out.append(candidate)
                else:
                    entry = entries[idx]
                    idx += 1
                    sid = str(entry.session_id)
                    if sid in emitted:  # pragma: no cover - 覆盖层条目自身唯一
                        continue
                    emitted.add(sid)
                    out.append(entry)
        return out

    def _local_page(self, after: tuple[str, str] | None, limit: int,
                    search: str) -> list[SessionSummary]:
        """Mongo 离线时的降级分页：只用进程内摘要，但合同（cursor/limit/search）不变。"""
        entries, _ = self._local_entries({}, after, search)
        return entries[:limit + 1]

    def set_character_binding(self, sid: SessionId, value: object) -> CharacterBinding | None:
        """设置缓存会话及本地摘要的规范化 binding；持久化仍由 create/save 边界负责。"""
        sid = SessionId(sid)
        binding = normalize_character_binding(value)
        self.get(sid).character_binding = binding
        summary = self._local_summaries.get(sid)
        if summary is not None:
            summary.character_binding = normalize_character_binding(binding)
        return binding

    async def get_summary(self, sid: SessionId) -> SessionSummary | None:
        """获取单会话摘要：**按 ID 直读持久层**（不经过任何列表窗口）+ 本地覆盖。

        修前实现遍历"最多 200 条"的合并列表 ⇒ 第 201+ 条会话的详情拿到 None，
        路由兜底成 `updatedAt: ""`（P2-01r 的第二出口）。Mongo 离线时仍返回
        本地创建/重命名结果。
        """
        sid = SessionId(sid)
        if sid in self._deleting or sid in self._deleted:
            return None
        local = self._local_summaries.get(sid)
        try:
            persisted = await self._repo.get_summary(sid)
        except Exception:
            logger.warning("session get_summary 读取失败，降级使用进程内摘要 sid=%s",
                           sid, exc_info=True)
            persisted = None
        merged = self._merge_summary(persisted, local)
        if merged is None:
            return None
        merged.active = (sid == self._active_id)
        return merged

    async def list(self, *, cursor: str | None = None, limit: int = 50,
                   search: str = "") -> tuple[list[SessionSummary], bool, str | None]:
        """列出会话摘要（**分页在存储层完成**，见 `SessionRepository.list_page`）。

        数据源 = 持久层 keyset 分页 ⊕ 进程内本地摘要覆盖层（`_merged_page`）；
        离线（仓储不可达）时显式降级为纯本地摘要（`_local_page`），合同不变：
          - search 仅匹配 title（大小写不敏感），不混消息文本；
          - keyset 游标 base64(`updated_at|session_id`)；
          - `has_more` 用 **limit+1** 探测（与设计文档 §4 一致）；
          - items 填 active（按 active_id 比较）。

        `limit` 是**单页上限**，与数据集大小无关：客户端沿 `next_cursor` 继续
        请求即可走遍全部可见记录（P2-01r / 不变量 PG1）。

        Raises:
            ValueError: 游标格式非法（路由折 400 CONTRACT_VALIDATION）。

        Returns:
            (items, has_more, next_cursor)。
        """
        after = _decode_cursor(cursor) if cursor else None
        # 契约：limit ∈ [1, cap]（路由 `_parse_limit` 已夹取）。这里再兜一层是为了
        # 让 `has_more`/`next_cursor` 的元数据恒自洽——limit<=0 时"还有更多"却给不出
        # 游标（items 为空）是自相矛盾的 metadata。
        if limit < 1:
            limit = 1
        if search and len(search) > MAX_SEARCH_LEN:
            # 与仓储 `search_regex_filter` 同判据：超长搜索词按"无匹配"处理
            # （不能只让持久层侧无命中、覆盖层侧仍有命中——那会自相矛盾）。
            logger.warning("会话搜索词超长（%d 字符）——按无匹配处理", len(search))
            return [], False, None
        degraded = False
        try:
            summaries = await self._merged_page(after, limit, search)
            self._degraded_cursor = None
        except Exception:
            # 降级（"仓储不可达 ⇒ 只报进程内摘要"）是**既有**的有意语义，但必须
            # 与"翻到一半失败"区分开：
            #   - 首页失败 → 整份列表退化为进程内视图（与修前一致）；
            #   - 后续页失败 → 只有当该游标**确实来自上一次降级页**时才继续降级
            #     （离线模式的多页遍历必须自洽）；否则如实抛出——绝不能让一次
            #     存储故障伪装成"列表到底了"（短页 + has_more=false）。
            if cursor is not None and cursor != self._degraded_cursor:
                raise
            logger.warning("session 分页读取失败，降级使用进程内摘要", exc_info=True)
            summaries = self._local_page(after, limit, search)
            degraded = True
        has_more = len(summaries) > limit
        items = summaries[:limit]
        for s in items:
            s.active = (SessionId(s.session_id) == self._active_id)
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.updated_at, last.session_id)
        # 记住"这一页是降级页"的游标，使离线模式的多页遍历自洽（见上面的 except）。
        self._degraded_cursor = next_cursor if (degraded and has_more) else None
        return items, has_more, next_cursor

    async def rename(self, sid: SessionId, title: str) -> bool:
        """重命名会话；Mongo 离线时更新进程内摘要并保持 REST 读后写一致。"""
        sid = SessionId(sid)
        if sid in self._deleting or sid in self._deleted:
            return False
        known_local = sid in self._sessions or sid in self._local_summaries
        try:
            persisted = await self._repo.rename(sid, title)
        except StorageUnavailableError:
            # **有意的离线容忍，现在显式可见**：Mongo 离线时仍更新进程内摘要
            # （REST 读后写一致）。此前它靠"repo 把离线也返回 False"隐式成立——
            # 那是把两种语义压进一个返回值里；现在仓库只对"会话不存在"返回 False，
            # 容忍必须在这里 catch 出来，读代码的人才知道这是选择而不是巧合。
            persisted = False
        if not persisted and not known_local:
            return False
        current = self._local_summaries.get(sid)
        if current is None:
            current = SessionSummary(
                session_id=sid, message_count=0, updated_at=now_iso(), title=title,
                character_binding=normalize_character_binding(
                    getattr(self._sessions.get(sid), "character_binding", None)),
            )
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
        """删除会话：提交点前置（先删会话 doc）+ 提交后清理（附件 refs + 归档）。

        保护（review 修复）：默认会话（"default"）不可删除——拒绝并返回 0。
        若删除的是活动会话，active 回落 "default"。Task 11（M3）不再重绑
        CharacterEngine（attach_session 已删）——instance-scoped 状态按 runtime
        权威，孤儿 state 由 registry/runtime 清除边界承担。返回删除的归档消息数。

        review 复核 #5 D2（提交点前置）：会话 doc 删除是持久化确认（commit），
        先执行——失败即回滚（重开归档 + 释放 tombstone），未破坏任何数据；
        成功后再尽力释放附件 refs + 删归档。清理失败**不阻塞删除**（用户要的
        删除已生效），登记待清理 journal 由健康循环 retry_pending_cleanup 重试
        ——不再出现「doc 未删但 refs/归档已清空，会话活着内容被掏空」。
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
            # tombstone 必须先于任何删除步骤：提交/清理期间到达的新消息也应被拒绝。
            self._deleting.add(sid)

        try:
            # B3/P1-02：删除前 drain 该会话在途归档 + 关闭归档入口——迟到 append
            # 不再进入，杜绝「先删记录、后写孤儿消息」。失败时 reopen_archive 回滚。
            session = self._sessions.get(sid)
            if session is not None:
                await session.close_archive()
            if before_delete is not None:
                await before_delete(sid)
            # 提交点前置：会话 doc 删除是删除的持久化确认。失败 → 回滚，路由 503，
            # 附件 refs / 归档均未动（不再有「doc 存活但内容被掏空」）。
            await self._repo.delete(sid)
        except Exception:
            async with self._lifecycle_lock:
                self._deleting.discard(sid)
            if session is not None:
                session.reopen_archive()
            raise

        # 提交后清理（尽力而为）：释放附件 refs + 删归档。失败不阻塞——doc 已删，
        # 只留可回收垃圾，登记 journal 由健康循环重试。
        deleted_messages = 0
        try:
            if self._attachment_cleanup is not None:
                await self._attachment_cleanup(sid)
            if self._msg_repo is not None:
                deleted_messages = await self._msg_repo.delete_by_session(sid)
            await self._cascade_delete_session_scoped(sid)
        except Exception as e:
            self._pending_cleanup.add(sid)
            self._save_pending_cleanup()
            logger.warning(
                "会话 %s 删除已提交，但提交后清理失败（refs/归档待重试）: %s", sid, e)

        async with self._lifecycle_lock:
            self._sessions.pop(sid, None)
            self._loaded.discard(sid)
            self._local_summaries.pop(sid, None)
            self._local_title_overrides.discard(sid)
            self._deleting.discard(sid)
            self._deleted.add(sid)
            if sid == self._active_id:
                self._active_id = DEFAULT_SESSION
                # Task 11（M3）：不再重绑 engine.attach_session(default)——instance-scoped
                # 状态按 runtime 权威，F4 孤儿 state 由 registry/runtime 清除边界承担。
                # active 指针已回落 default（v1 锚点），default 会话的 runtime 绑定不变。
        return deleted_messages

    # ---- 持久化编排 ----

    async def save_all(self) -> None:
        """持久化所有已加载会话。"""
        for sid in list(self._sessions):
            if sid in self._loaded:
                await self._sessions[sid].save()

    async def drain_all_archives(self, *, timeout: float = 5.0) -> None:
        """关闭全部会话归档入口并 drain 在途归档任务（B3/P1-02 shutdown 用）。

        必须在 message_repo.close() 之前调用，否则在途归档会在 repo 关闭后
        静默丢失记录（P1-02：shutdown 丢消息）。
        """
        for session in list(self._sessions.values()):
            await session.close_archive(timeout=timeout)

    def reopen_all_archives(self) -> None:
        """重新打开全部会话归档入口（review 复核 #3：热重启 start 用）。

        stop() 经 ``drain_all_archives`` 关闭全部归档；同一 Agent 实例 restart 时
        start() 必须重开——否则重启后 add_message 不再写 MessageRepository，
        新消息只在内存队列（随淘汰/退出丢失）。与 delete 回滚的
        ``session.reopen_archive()`` 对称。
        """
        for session in list(self._sessions.values()):
            session.reopen_archive()

    # ---- 待清理 journal（review 复核 #5 D2）----

    def _pending_cleanup_file(self) -> str:
        """待清理会话 journal 路径。"""
        return str(self._pending_cleanup_file_path or ProjectPath.SESSION_PENDING_CLEANUP.resolve())

    def _load_pending_cleanup(self) -> None:
        """启动时从 journal 加载待清理会话（doc 已删但 refs/归档未清）。"""
        try:
            path = self._pending_cleanup_file()
            if not Path(path).is_file():
                return
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._pending_cleanup = {SessionId(str(s)) for s in (data or [])}
        except Exception:
            logger.warning("读取 session pending-cleanup journal 失败", exc_info=True)
            self._pending_cleanup = set()

    def _save_pending_cleanup(self) -> None:
        """持久化待清理会话（原子写；空集合删除 journal）。"""
        try:
            path = self._pending_cleanup_file()
            if not self._pending_cleanup:
                if Path(path).is_file():
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(str(s) for s in self._pending_cleanup), f)
            os.replace(tmp, path)
        except Exception:
            logger.warning("写入 session pending-cleanup journal 失败", exc_info=True)

    async def _cascade_delete_session_scoped(self, sid: SessionId) -> None:
        """删除该会话在**其余会话域集合**里的行（#21b）。

        幂等：各仓储的 `delete_by_session` 对已删数据返回 0，故 journal 重试安全。
        失败**不吞**——抛给调用方（提交后清理块登记 pending journal 重试；
        retry 路径按会话记录 debug 并保留下轮重试）。
        """
        for label, repo in self._cascade_repos:
            try:
                removed = await repo.delete_by_session(sid)
            except Exception:
                logger.warning(
                    "会话 %s 级联删除失败（%s）——登记待重试", sid, label, exc_info=True)
                raise
            if removed:
                logger.info("会话 %s 级联删除：%s 删除 %d 行", sid, label, removed)

    async def retry_pending_cleanup(self) -> None:
        """重试待清理会话（健康循环 session 恢复时调用）。

        doc 已删但提交后清理失败（附件 refs / 归档）的会话按 journal 重试——
        幂等（release_session_attachments 重复 release 是条件 update；delete_by_session
        对已删归档返回 0）。会话 doc 若重新存在（删除后同 sid 重建）则跳过，避免
        误删新会话数据。失败保留 journal 下轮重试。
        """
        if not self._pending_cleanup:
            return
        for sid in list(self._pending_cleanup):
            try:
                if await self._repo.load(sid) is not None:
                    # doc 还在（会话被重建）——不动，避免误删新会话的归档
                    continue
                if self._attachment_cleanup is not None:
                    await self._attachment_cleanup(sid)
                if self._msg_repo is not None:
                    await self._msg_repo.delete_by_session(sid)
                await self._cascade_delete_session_scoped(sid)
                self._pending_cleanup.discard(sid)
            except Exception:
                logger.debug("待清理会话重试失败 sid=%s", sid, exc_info=True)
        self._save_pending_cleanup()

    async def connect_db(self) -> bool:
        """建立 repo 连接。"""
        return await self._repo.connect()

    async def health_check(self) -> bool:
        """repo 健康检查（断开时尝试重连）。"""
        return await self._repo.health_check()

    async def ensure_default_persisted(self) -> None:
        """确保 default 会话文档存在（clean-start best-effort，失败仅记日志）。

        BE-P1-05：repo 在 __init__ 时未连接，此处由生命周期健康循环的 session
        恢复边界（``_recover_dependency("session")``）在 repo 可用后调用。经
        ``repo.ensure_exists``（$setOnInsert）落库——已有文档（messages/标题）
        恒不被覆盖，重启不清空 LLM 上下文窗口。
        """
        try:
            inserted = await self._repo.ensure_exists(
                DEFAULT_SESSION,
                character_binding=self.active.character_binding,
            )
            if inserted:
                logger.debug("default 会话文档已新建（clean-start）")
        except Exception:
            # 失败仅记日志，不阻塞启动/恢复（本地摘要已保证 REST 可见性）
            logger.warning("default 会话 ensure_exists 失败（仅记日志，不阻塞）", exc_info=True)

    async def load_active(self) -> None:
        """加载当前活动会话（启动时调用）。"""
        await self.ensure_loaded(self._active_id)

    async def recover_pending_sessions(self, *, pending_dir: str | None = None,
                                       import_to_mongo: bool = True) -> int:
        """启动扫描所有 pending JSONL 会话（非活动也在内），恢复内存并导入 Mongo。

        B2/P2-01：此前只加载 active 的 pending；非活动会话的离线数据重启后不可见。
        这里 glob ``data/session_*.json``，逐个 ``load_from_jsonl`` 恢复并标记
        loaded；Mongo 可用时 ``save()``（Mongo durable 后自动删除 pending）。
        损坏/空文件忽略（不计数）。路径解析限制在 pending 根目录。

        Args:
            pending_dir: 覆盖 pending 目录（测试用）；默认取 SESSION_PENDING 目录。
            import_to_mongo: Mongo 可用时是否导入并删除 pending。

        Returns:
            成功恢复的会话数。
        """
        if pending_dir is None:
            root = Path(str(ProjectPath.SESSION_PENDING.resolve(session_id="x"))).parent
        else:
            root = Path(pending_dir)
        if not root.is_dir():
            return 0
        recovered = 0
        for f in sorted(root.glob("session_*.json")):
            sid = SessionId(f.name[len("session_"):-len(".json")])
            try:
                session = self.get(sid)
                # 指向扫描到的实际文件（生产=真实 pending 路径；测试=覆盖目录）
                session._pending_file = str(f)  # noqa: SLF001
                if not await session.load_from_jsonl(merge=True):
                    continue  # 损坏/空文件——忽略，不计数
                self._loaded.add(sid)
                # 非活动会话也进本地摘要，使离线 list/has_session 可见
                summary = self._local_summaries.get(sid)
                if summary is None:
                    summary = SessionSummary(
                        session_id=sid, message_count=len(session.messages),
                        updated_at=now_iso(), title=derive_session_title(session.messages),
                        character_binding=normalize_character_binding(session.character_binding),
                    )
                    self._local_summaries[sid] = summary
                else:
                    summary.message_count = len(session.messages)
                if import_to_mongo:
                    # save() 在 Mongo durable 成功后删除 pending；失败保留
                    await session.save()
                recovered += 1
            except Exception:
                logger.warning("pending 会话恢复失败: %s", f.name, exc_info=True)
        return recovered

    def evict_if_needed(self) -> None:
        """超出 max_sessions 时逐出最久未用非活动会话（逐出前 save）。

        同步方法：save 经 ``_evict_tasks`` 后台任务调度（无运行事件循环时记
        warning 并跳过——见 ``AsyncTaskScope.try_spawn``）。

        **ARCH-INC-P2-03 修复（数据丢失窗口）**：此前 ``ensure_future(session.save())``
        的 Task 被完全丢弃，而下一行就把会话弹出缓存池——于是 ``save_all()`` /
        ``drain_all_archives()`` 都不可能再覆盖它（它们只遍历 ``_sessions``）。
        逐出是该会话最后一次落库机会，却无人 await/取消/观察异常。现在任务登记
        入 scope，``drain_evictions()`` 在关机时等待它落定。

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
                self._evict_tasks.try_spawn(session.save(), name=f"evict-save-{sid}")
            self._sessions.pop(sid, None)
            self._loaded.discard(sid)

    async def drain_evictions(self, *, timeout: float = 5.0) -> int:
        """等待全部逐出 save 落定（ARCH-INC-P2-03）。

        必须在 ``_stop_persistence`` 关闭 session repo **之前**调用，否则仍在途的
        逐出 save 会写向已关闭的连接。

        Returns:
            超时前未落定的任务数（0 = 全部完成）。
        """
        return await self._evict_tasks.drain(timeout=timeout)
