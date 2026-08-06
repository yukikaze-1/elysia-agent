"""附件引用释放共享 helpers——归档/会话删除时释放附件引用（批次 8a D5）。

消息/会话删除时联动释放附件引用（refs 递减 + 软删 + resolver 注册清除），
供 debug 归档/会话路由与 REST 会话删除共用（闭合 BE-030 + BE-070 的删除闭环）：

- `release_message_attachments`：单条消息删除联动
- `release_messages_attachments`：批量消息删除联动（清空/会话删除用）
- `release_all_attachments`：释放全部归档消息的附件 refs（分页遍历）
- `release_session_attachments`：释放单会话归档消息的附件 refs（_id 游标遍历）

仅依赖 `core.schema.adapters.attachment_resolver` + `core.storage.attachment_repo`
（`StorageUnavailableError`），不反向依赖 core.input → 无循环导入。
"""

import logging

from core.schema.adapters import attachment_resolver as _ar
from core.storage.attachment_repo import StorageUnavailableError

logger = logging.getLogger(__name__)

_ATTACHMENT_RELEASE_BATCH = 200


def attachment_ids_from_message(msg) -> list[str]:
    """提取消息 content 中附件 Part 的 attachment_id（ImagePart/FilePart/AudioPart/VideoPart）。

    兼容 Part 对象与 dict part（归档消息统一为 Part 对象，防御性兼容 dict）。
    """
    ids: list[str] = []
    for p in (getattr(msg, "content", None) or []):
        aid = p.get("attachment_id") if isinstance(p, dict) else getattr(p, "attachment_id", None)
        if aid:
            ids.append(aid)
    return ids


async def release_message_attachments(agent, msg) -> None:
    """单条消息删除联动：附件 remove_ref(message_id)；refs 归零 → 取消 resolver 注册。

    remove_ref 在 refs 归零时已写 status=deleted + deleted_at（repo 语义，spec §3.1），
    此处补充进程内 AttachmentFileResolver.unregister（Mongo 离线定位兜底不再可用）。
    """
    ids = attachment_ids_from_message(msg)
    if not ids:
        return
    repo = agent.attachment_repo
    message_id = str(getattr(msg, "message_id", "") or "")
    for aid in ids:
        try:
            await repo.remove_ref(aid, message_id)
            doc = await repo.get(aid)
            if doc is None or (doc.get("ref_count") or 0) == 0:
                _ar.unregister(aid)
        except StorageUnavailableError:
            logger.warning("附件存储不可用，跳过 remove_ref: %s", aid)
        except Exception as e:
            logger.warning("附件 refs 释放失败 aid=%s: %s", aid, e)


async def release_messages_attachments(agent, messages) -> None:
    """批量消息删除联动（清空/会话删除用）。"""
    for msg in messages:
        await release_message_attachments(agent, msg)


async def release_all_attachments(agent) -> None:
    """释放全部归档消息的附件 refs（archive/sessions 清空场景，分页遍历）。"""
    skip = 0
    while True:
        msgs, _ = await agent.message_repo.query_offset(
            skip=skip, limit=_ATTACHMENT_RELEASE_BATCH)
        if not msgs:
            break
        await release_messages_attachments(agent, msgs)
        skip += len(msgs)
        if len(msgs) < _ATTACHMENT_RELEASE_BATCH:
            break


async def release_session_attachments(agent, sid) -> None:
    """释放单会话归档消息的附件 refs（会话删除场景，_id 游标遍历全部消息）。"""
    before = None
    while True:
        msgs, has_more = await agent.message_repo.query_page(
            sid, before=before, limit=_ATTACHMENT_RELEASE_BATCH)
        if not msgs:
            break
        await release_messages_attachments(agent, msgs)
        if not has_more:
            break
        before = msgs[-1].metadata.get("_mongo_id")
