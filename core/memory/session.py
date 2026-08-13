"""会话上下文——内存队列，可选 MongoDB 持久化。

自动探测 MongoDB 可用性。可用时会话经 save_session() / load_session()
在进程重启后保留；否则回退为内存 deque（既有行为）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from paths import ProjectPath

from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.service_state import ServiceState
from core.schema.message import (
    AssistantMessage,
    ClientAssistantMessage,
    ClientMessageDict,
    ClientToolMessage,
    ClientUserMessage,
    Message,
    ToolResultMessage,
)
from core.schema.utils import now_iso
from core.schema.part import TextPart
from core.schema.types import SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.base import StorageUnavailableError

if TYPE_CHECKING:
    from core.memory.memory_reflector import MemoryReflector

logger = logging.getLogger(__name__)

# 限制并发 MongoDB 写入，防止 fire-and-forget 任务无限堆积
_archive_semaphore = asyncio.Semaphore(8)


async def _archive_with_limit(msg_repo: MessageRepository, session_id: SessionId,
                              msg: Message) -> None:
    """受并发限制的归档写入。

    Args:
        msg_repo: MessageRepository 实例。
        session_id: 会话 ID。
        msg: 要归档的 Message 对象。
    """
    async with _archive_semaphore:
        await msg_repo.append(session_id, msg)


def _archive_done(task: asyncio.Task) -> None:
    """fire-and-forget 归档回调：归档失败记录 warning（归档为 best-effort）。

    消息归档是 append-only 永久日志——失败必须可见，否则 MongoDB 故障期间
    对话消息静默不落 archive（配合 session deque 窗口挤出即永久丢失）。

    Args:
        task: 已完成的归档 asyncio.Task。
    """
    try:
        task.result()
    except Exception:
        logger.warning("消息归档失败（fire-and-forget）", exc_info=True)


class SessionMemory:
    """有界会话窗口，可选 MongoDB 持久化。"""

    def __init__(self, max_messages: int = 20,
                 session_id: SessionId = SessionId("default"),
                 repo: SessionRepository | None = None,
                 msg_repo: MessageRepository | None = None,
                 character_state: CharacterState | None = None,
                 service_state: ServiceState | None = None):
        """初始化会话记忆。

        Args:
            max_messages: 消息窗口最大长度。
            session_id: 会话唯一标识。
            repo: SessionRepository 实例，用于 MongoDB 持久化。
            msg_repo: MessageRepository 实例，用于消息归档。
            character_state: 角色状态对象，None 时自动创建默认实例。
            service_state: 服务运行时状态对象，None 时自动创建默认实例。
        """
        self._messages: deque[Message] = deque(maxlen=max_messages)
        self._max_messages = max_messages
        self._session_id: SessionId = session_id
        self._repo: SessionRepository | None = repo
        self._msg_repo: MessageRepository | None = msg_repo
        self._pending_file = str(ProjectPath.SESSION_PENDING.resolve(session_id=session_id))
        self.character_state = character_state or CharacterState(session_id=session_id)
        self.service_state = service_state or ServiceState()
        self.own_life: str = ""
        self._memory_reflector = None
        self._pre_save_hook: Callable[[], None] | None = None
        # B2/P1-01：最近一次 load 是否因存储不可用而失败（区别于「not found 新会话」）。
        # ensure_loaded 据此决定是否标记 loaded（存储不可用 → 不标记，允许重试；
        # not found → 合法新会话，标记 loaded 以便 save_all 持久化）。
        self._storage_unavailable = False
        # B3/P1-02：按会话登记的归档 task 集合 + 关闭标志——删除/shutdown 前可
        # drain，关闭后 add_message 不再创建新归档任务（防删除后迟到 append 复活）。
        self._archive_tasks: set[asyncio.Task] = set()
        self._archive_closed = False

    # ---- public properties ----

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def messages(self) -> deque[Message]:
        return self._messages

    @property
    def pending_file(self) -> str:
        return self._pending_file

    # ---- messages ----

    def add_message(self, msg: Message) -> None:
        """存储一条 Pydantic Message 对象，同时 fire-and-forget 归档到永久存储。

        B3/P1-02：归档 task 登记入 ``_archive_tasks`` 供删除/shutdown drain；
        ``_archive_closed`` 时不再创建新归档任务（删除后迟到 append 不复活记录）。

        Args:
            msg: 要存储的 Message 对象（UserMessage / AssistantMessage / ToolResultMessage）。
        """
        self._messages.append(msg)
        if self._msg_repo is not None and not self._archive_closed:
            task = asyncio.ensure_future(
                _archive_with_limit(self._msg_repo, self._session_id, msg)
            )
            self._archive_tasks.add(task)
            task.add_done_callback(self._on_archive_done)

    def _on_archive_done(self, task: asyncio.Task) -> None:
        """归档任务完成回调：移出登记集合并上报失败（复用模块级 _archive_done）。"""
        self._archive_tasks.discard(task)
        _archive_done(task)

    async def close_archive(self, *, timeout: float = 5.0) -> None:
        """关闭归档入口并 drain 在途归档任务（B3/P1-02 删除/shutdown 用）。

        关闭后 add_message 不再创建新归档任务；等待在途任务完成（超时后取消）。
        若删除中途失败，调用方应调 ``reopen_archive()`` 恢复。

        Args:
            timeout: 等待在途归档任务完成的总超时秒数。
        """
        self._archive_closed = True
        tasks = list(self._archive_tasks)
        self._archive_tasks.clear()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending)

    def reopen_archive(self) -> None:
        """重新打开归档入口（删除失败回滚时调用，B3）。"""
        self._archive_closed = False

    def recent_messages(self, n: int | None = None) -> list[Message]:
        """返回最近 n 条消息（Pydantic 对象）。

        Args:
            n: 返回的消息数量，None 时返回全部。

        Returns:
            Message 对象列表，从旧到新排列。
        """
        msgs = list(self._messages)
        if n is not None:
            msgs = msgs[-n:]
        return msgs

    def messages_for_client(self, n: int | None = None) -> list[ClientMessageDict]:
        """返回客户端友好的消息格式，从 Pydantic Message 转换为前端 dict。

        Args:
            n: 返回的消息数量，None 时返回全部。

        Returns:
            前端可用的 ClientMessageDict 列表。
        """
        msgs = self.recent_messages(n)
        result: list[ClientMessageDict] = []
        for msg in msgs:
            if isinstance(msg, ToolResultMessage):
                # 工具结果 — 平铺字段给前端渲染工具卡片
                text_content = "".join(
                    p.content for p in msg.content
                    if isinstance(p, TextPart)
                )
                # role 键运行时是 str（"tool"），与 TypedDict 的 Literal[MessageRole.TOOL]
                # 仅类型层面不一致（StrEnum 值序列化后即 "tool"），cast 收口。
                # D1/D2：输出消息自身 message_id/timestamp（复用已持久/派生 ID，绝不新生成），
                # 与 REST 身份一致、前端据此去重/时间分隔。
                tool_entry = cast(ClientToolMessage, {
                    "role": "tool",
                    "message_id": msg.message_id,
                    "timestamp": msg.timestamp,
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.tool_name,
                    "content": text_content,
                    "success": msg.success,
                    "error": msg.error,
                    "error_type": msg.error_type.value if msg.error_type else None,
                    "duration_ms": msg.duration_ms,
                    "truncated": msg.truncated,
                })
                result.append(tool_entry)
            else:
                # UserMessage / AssistantMessage 普通消息分支
                text_parts = [
                    {"content": p.content}
                    for p in msg.content
                    if isinstance(p, TextPart)
                ]
                chat_entry: ClientUserMessage | ClientAssistantMessage = {
                    "role": str(msg.role),
                    "parts": text_parts if text_parts else [{"content": ""}],
                    "content": [p.model_dump(mode="json") for p in msg.content],
                    "message_id": msg.message_id,
                    "timestamp": msg.timestamp,
                }
                if isinstance(msg, AssistantMessage):
                    _e = cast(ClientAssistantMessage, chat_entry)
                    if msg.inner_voice:
                        _e["inner_voice"] = msg.inner_voice
                    if msg.tool_calls:
                        _e["tool_calls"] = [tc.model_dump(mode="json") for tc in msg.tool_calls]
                    if msg.token_usage:
                        _e["token_usage"] = msg.token_usage.model_dump(mode="json")
                    if msg.block_index:
                        _e["content_type"] = msg.block_index
                result.append(chat_entry)
        return result

    # ---- memory reflector ----

    def set_memory_reflector(self, mr: "MemoryReflector") -> None:
        """设置 MemoryReflector 引用，供 save/load 时持久化反射器状态。

        Args:
            mr: MemoryReflector 实例。
        """
        self._memory_reflector = mr

    # ---- MongoDB persistence ----

    async def connect_db(self) -> bool:
        """尝试连接 MongoDB。

        Returns:
            True 表示连接成功，False 表示不可用。
        """
        if self._repo is None:
            self._repo = SessionRepository()
        return await self._repo.connect()

    async def health_check(self) -> bool:
        """尝试重连 MongoDB。

        Returns:
            True 表示已连接或重连成功，False 表示不可用。
        """
        if self._repo is None:
            return False
        return await self._repo.health_check()

    async def save(self) -> bool:
        """持久化当前会话。MongoDB 不可用时降级到本地 JSONL 文件。

        B2/P1-01 语义：repo 未连接时 repo.save 抛 StorageUnavailableError →
        落 JSONL（不再假成功）；Mongo durable 成功后删除本地 pending JSONL
        （冗余副本，只有 durable success 才删）。

        Returns:
            True 表示保存成功（Mongo 或 JSONL 至少一种 durable），
            False 表示所有存储方式均失败。
        """
        # 同步子系统的内存状态到 character_state（reflector buffer 等）
        if self._pre_save_hook:
            self._pre_save_hook()
        # 尝试通过 Repository 保存到 MongoDB
        if self._repo is not None:
            try:
                logger.debug("Session save to MongoDB: session=%s, messages=%d",
                            self._session_id, len(self._messages))
                await self._repo.save(
                    self._session_id,
                    list(self._messages),
                    character_state=self.character_state,
                    reflector_state=(self._memory_reflector.save_state()
                                     if self._memory_reflector
                                        and hasattr(self._memory_reflector, "save_state")
                                     else None),
                    service_state=self.service_state,
                )
                # Mongo durable → 本地 pending JSONL 冗余，删除（只有 durable success 才删）
                if os.path.exists(self._pending_file):
                    try:
                        os.remove(self._pending_file)
                        logger.debug("session Mongo durable，删除 pending %s",
                                     self._pending_file)
                    except OSError:
                        logger.warning("session 删除 pending 文件失败（无害）：%s",
                                       self._pending_file)
                logger.debug("Session save to MongoDB OK: %d messages", len(self._messages))
                return True
            except Exception:
                logger.warning("session MongoDB 写入失败", exc_info=True)
        # JSONL fallback — MongoDB 不可用
        try:
            doc = {
                "session_id": self._session_id,
                "messages": [msg.model_dump(mode="json") for msg in self._messages],
                "character_state": self.character_state.model_dump(mode="json"),
                "reflector_state": (self._memory_reflector.save_state().model_dump(mode="json")
                                    if self._memory_reflector
                                       and hasattr(self._memory_reflector, "save_state")
                                    else {}),
                "service_state": self.service_state.model_dump(mode="json"),
                "updated_at": now_iso(),
            }
            os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
            tmp = self._pending_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
            os.replace(tmp, self._pending_file)  # 原子替换，崩溃不丢数据
            logger.debug("MongoDB 不可用，session 保存到 %s", self._pending_file)
            return True
        except Exception:
            logger.warning("session JSON 写入失败", exc_info=True)
            return False

    async def load_from_jsonl(self, merge: bool = False) -> bool:
        """从本地 JSONL 文件恢复 session。

        Args:
            merge: True 时合并到现有消息，False 时替换现有消息。

        Returns:
            True 表示恢复成功，False 表示文件不存在或解析失败。
        """
        return await self._load_from_jsonl(merge=merge)

    async def _load_from_jsonl(self, merge: bool = False) -> bool:
        """从本地 JSONL 文件恢复 session。

        merge=False: 直接替换 _messages（用于 MongoDB 不可用时的独立恢复）
        merge=True:  合并到现有 _messages 后面（用于 MongoDB 恢复后补充 JSONL 数据）

        Args:
            merge: 是否合并模式（保留现有消息）。

        Returns:
            True 表示恢复成功，False 表示失败。
        """
        if not os.path.exists(self._pending_file):
            return False
        try:
            with open(self._pending_file) as f:
                doc = json.loads(f.readline())
            msgs_raw = doc.get("messages", [])
            converted = [validate_message_legacy_compat(m) for m in msgs_raw]
            if merge and self._messages:
                # 合并：现有消息（from MongoDB）+ JSONL 消息，按 message_id 去重
                existing_ids = {m.message_id for m in self._messages if m.message_id}
                new_msgs = [m for m in converted if m.message_id not in existing_ids]
                all_msgs = list(self._messages) + new_msgs
                self._messages = deque(all_msgs[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 合并 session（已有 %d + 新增 %d）",
                           self._pending_file, len(self._messages) - len(new_msgs), len(new_msgs))
            else:
                self._messages = deque(converted[-self._max_messages:], maxlen=self._max_messages)
                logger.info("从 %s 恢复 session（%d 条消息）", self._pending_file, len(converted))
            # 恢复 character_state（向后兼容也读 params 中的 mood/mood_intensity）
            cs_data = doc.get("character_state", {})
            if cs_data:
                cs = CharacterState.model_validate(cs_data)
                for field in type(cs).model_fields:
                    setattr(self.character_state, field, getattr(cs, field))
            else:
                # 向后兼容：旧 JSONL 文件用 params 存 mood
                legacy_params = doc.get("params", {})
                if "mood" in legacy_params:
                    self.character_state.mood = legacy_params["mood"]
                if "mood_intensity" in legacy_params:
                    self.character_state.mood_intensity = legacy_params["mood_intensity"]
            # 恢复 reflector_state
            rs_data = doc.get("reflector_state", {})
            if rs_data and self._memory_reflector and hasattr(self._memory_reflector, "restore_state"):
                rs = MemoryReflectorState.model_validate(rs_data)
                self._memory_reflector.restore_state(rs)
            # 恢复 service_state（MongoDB 不可用时的 JSONL 落盘同样带此字段）
            ss_data = doc.get("service_state", {})
            if ss_data:
                ss = ServiceState.model_validate(ss_data)
                for field in type(ss).model_fields:
                    setattr(self.service_state, field, getattr(ss, field))
            return True
        except Exception:
            logger.warning("从 %s 恢复 session 失败", self._pending_file, exc_info=True)
            return False

    async def load(self, session_id: SessionId | None = None) -> bool:
        """从 MongoDB 加载会话。MongoDB 不可用时尝试本地 JSONL 恢复。

        Args:
            session_id: 要加载的会话 ID，None 时使用当前 session_id。

        Returns:
            True 表示加载成功，False 表示所有方式均失败。
        """
        if self._repo is None:
            return await self._load_from_jsonl()
        sid = session_id or self._session_id
        self._storage_unavailable = False
        try:
            result = await self._repo.load(sid)
            if result is not None:
                messages, cs, rs, ss = result
                self._session_id = sid
                self._messages = deque(messages[-self._max_messages:], maxlen=self._max_messages)
                # 原地更新 character_state
                for field in type(cs).model_fields:
                    setattr(self.character_state, field, getattr(cs, field))
                # 恢复 reflector_state
                if rs is not None and self._memory_reflector is not None:
                    if hasattr(self._memory_reflector, "restore_state"):
                        self._memory_reflector.restore_state(rs)
                # 恢复 service_state
                if ss is not None:
                    for field in type(ss).model_fields:
                        setattr(self.service_state, field, getattr(ss, field))
                logger.info("SessionMemory: loaded session %s (%d messages)", sid, len(messages))
                return True
            return await self._load_from_jsonl()
        except StorageUnavailableError:
            # B2/P1-01：存储不可用（区别于 not found）——标记供 ensure_loaded 判定
            self._storage_unavailable = True
            logger.warning("SessionMemory load from MongoDB unavailable, trying JSONL")
            return await self._load_from_jsonl()
        except Exception:
            # review 复核 #2：repo 抛出的非 StorageUnavailable 连接异常（未及包装）
            # 同样视为存储不可用——否则 ensure_loaded 会把它当「存储可达 but not
            # found」永久标 loaded，之后用不完整的本地状态覆盖远端历史。
            self._storage_unavailable = True
            logger.warning("SessionMemory load from MongoDB failed, trying JSONL", exc_info=True)
            return await self._load_from_jsonl()

    def clear(self) -> None:
        logger.info("SessionMemory: cleared all messages (%d)", len(self._messages))
        self._messages.clear()
