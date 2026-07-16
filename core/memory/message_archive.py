"""MessageArchive — 对话消息永久归档（MongoDB，只增不删）。"""

import logging
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from core.schema.message import Message, MessageRole
from core.schema.types import MessageId, SessionId

if TYPE_CHECKING:
    from core.storage.message_repo import MessageRepository

logger = logging.getLogger(__name__)

# TypeAdapter for Message union — cached at module level
_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def _message_to_dict(msg: Message) -> dict:
    """将 Message 对象转为前端兼容 dict，同时将 metadata._mongo_id 提升为顶层 _id。

    MongoDB 的 _id (ObjectId) 在 Pydantic Message 模型中没有对应字段，
    MessageRepository 将其暂存在 metadata._mongo_id 中。
    此函数在序列化时将其提升为顶层 _id，供前端分页/删除使用。
    """
    d = msg.model_dump(mode="json")
    mongo_id = d.get("metadata", {}).pop("_mongo_id", None)
    if mongo_id:
        d["_id"] = mongo_id
    return d


class MessageArchive:
    """所有对话消息的永久归档存储。

    与 SessionMemory 职责分离:
    - SessionMemory: LLM 上下文窗口，截断保留最近 N 条
    - MessageArchive: 永久归档，只增不删，分页查询

    使用方式:
        repo = MessageRepository()
        await repo.connect()
        archive = MessageArchive(repo=repo)
        await archive.append(msg)                # 每条消息 fire-and-forget
        messages, has_more = await archive.query(limit=50)  # 分页查询
    """

    def __init__(self, repo: "MessageRepository | None" = None):
        self._repo: "MessageRepository | None" = repo

    # ---- 写入 ----

    async def append(self, msg: dict) -> str:
        """追加一条消息。fire-and-forget — 调用方用 create_task 包装。返回 _id。"""
        if self._repo is None:
            raise RuntimeError("MessageArchive not connected")
        session_id = SessionId(msg.get("session_id", ""))
        # 从 dict 转换为 Message 后传给 repo
        msg_obj = _message_adapter.validate_python(msg)
        await self._repo.append(session_id, msg_obj)
        return msg.get("_id", "")

    # ---- 查询 ----

    async def query(
        self,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict], bool]:
        """分页查询消息，按 _id 降序（最新的在前）。

        before: 上一页最后一条的 _id（ObjectId 字符串），用于游标分页
        limit: 每页数量
        返回: (messages, has_more)
        """
        if self._repo is None:
            return [], False

        messages, has_more = await self._repo.query(before=before, limit=limit)
        return [_message_to_dict(msg) for msg in messages], has_more

    # ---- Offset 分页（支持跳页）----

    async def query_offset(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        search: str = "",
        role: MessageRole | None = None,
    ) -> tuple[list[dict], int]:
        """Offset 分页查询，按 _id 降序。支持搜索过滤。返回 (messages, total)。"""
        if self._repo is None:
            return [], 0
        messages, total = await self._repo.query_offset(skip=skip, limit=limit, search=search, role=role)
        return [_message_to_dict(msg) for msg in messages], total

    # ---- 管理 ----

    async def delete_one(self, msg_id: MessageId) -> bool:
        """删除单条消息。返回 True 表示成功。"""
        if self._repo is None:
            return False
        try:
            deleted = await self._repo.delete_one(msg_id)
            return deleted > 0
        except Exception:
            logger.debug("archive delete_one failed", exc_info=True)
            return False

    async def update_text(self, msg_id: MessageId, new_text: str) -> bool:
        """更新消息文本（保持其他字段不变）。返回 True 表示成功。"""
        if self._repo is None:
            return False
        try:
            modified = await self._repo.update_text(msg_id, new_text)
            return modified > 0
        except Exception:
            logger.debug("archive update_text failed", exc_info=True)
            return False

    async def count(self) -> int:
        """返回归档消息总数。"""
        if self._repo is None:
            return 0
        return await self._repo.count()

    async def clear_all(self) -> int:
        """清空全部归档。返回删除数量。"""
        if self._repo is None:
            return 0
        deleted = await self._repo.clear_all()
        logger.info("MessageArchive: 清空 %d 条消息", deleted)
        return deleted

    # ---- 按 session 查询 ----

    async def query_by_session(
        self, session_id: SessionId, *, skip: int = 0, limit: int = 200
    ) -> tuple[list[dict], int]:
        """按 session_id 查询消息，_id 降序（最新在前）。
        返回 (messages, total)。"""
        if self._repo is None:
            return [], 0
        messages, total = await self._repo.query_by_session(session_id, limit=limit, skip=skip)
        return [_message_to_dict(msg) for msg in messages], total

    async def count_by_session(self, session_id: SessionId) -> int:
        """按 session_id 统计消息数。"""
        if self._repo is None:
            return 0
        return await self._repo.count_by_session(session_id)
