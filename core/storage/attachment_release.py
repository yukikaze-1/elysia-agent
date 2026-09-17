"""附件引用释放共享 helpers——归档/会话删除时释放附件引用（批次 8a D5）。

消息/会话删除时联动释放附件引用（refs 递减 + 软删 + resolver 注册清除），
供 debug 归档/会话路由与 REST 会话删除共用（闭合 BE-030 + BE-070 的删除闭环）：

- `release_message_attachments`：单条消息删除联动
- `release_messages_attachments`：批量消息删除联动（清空/会话删除用）
- `release_session_attachments`：释放单会话归档消息的附件 refs（_id 游标遍历）

**#41（CR-04 彻底版）追加的「快照 → 提交 → 释放」三件套**：

- `collect_all_attachment_refs` / `collect_session_attachment_refs`：提交**前**把
  `(message_id, attachment_ids)` 快照读出来（主操作提交后消息就没了，读不到了）；
- `release_refs_snapshot`：按快照释放（幂等——`remove_ref` 以 `refs` 含该
  message_id 为条件，重复释放是 no-op）；
- `release_after_commit`：提交后清理入口——失败**不抛**（主操作已提交，回滚不存在），
  改为落 `pending_attachment_release` journal + 返回"待清理"标志，由启动/恢复边界重试。

仅依赖 `core.schema.adapters.attachment_resolver` + `core.storage.attachment_repo`
（`StorageUnavailableError`），不反向依赖 core.input → 无循环导入。
"""

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.schema.adapters import attachment_resolver as _ar
from core.schema.message import Message
from core.schema.types import AttachmentId, SessionId
from core.storage.attachment_repo import StorageUnavailableError

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)

_ATTACHMENT_RELEASE_BATCH = 200

#: 快照条目——(message_id, attachment_ids)
RefSnapshot = list[tuple[str, list[AttachmentId]]]


def attachment_ids_from_message(msg: Message) -> list[AttachmentId]:
    """提取消息 content 中附件 Part 的 attachment_id（ImagePart/FilePart/AudioPart/VideoPart）。

    兼容 Part 对象与 dict part（归档消息统一为 Part 对象，防御性兼容 dict）。
    """
    ids: list[AttachmentId] = []
    for p in (getattr(msg, "content", None) or []):
        aid = p.get("attachment_id") if isinstance(p, dict) else getattr(p, "attachment_id", None)
        if aid:
            ids.append(aid)
    return ids


async def release_message_attachments(agent: "Agent", msg: Message) -> None:
    """单条消息删除联动：附件 remove_ref(message_id)；refs 归零 → 取消 resolver 注册。

    remove_ref 在 refs 归零时已写 status=deleted + deleted_at（repo 语义，spec §3.1），
    此处补充进程内 AttachmentFileResolver.unregister（Mongo 离线定位兜底不再可用）。

    Raises:
        StorageUnavailableError: 附件存储不可用——必须传播（review 复核 #5），否则
            caller（会话删除 manager、debug 路由）无法感知释放失败，refs 残留导致
            附件被 GC 永久跳过或删除「假成功」。调用方决定是 503 还是登记待清理。
    """
    ids = attachment_ids_from_message(msg)
    if not ids:
        return
    message_id = str(getattr(msg, "message_id", "") or "")
    await release_attachment_refs(agent, message_id, ids)


async def release_attachment_refs(
    agent: "Agent", message_id: str, ids: Sequence[AttachmentId],
) -> None:
    """释放 `message_id` 对 `ids` 的引用（低层实现，`release_message_attachments` 与
    快照路径共用——避免"消息对象走一条、快照走另一条"的两条路径不对称）。

    Raises:
        StorageUnavailableError: 附件存储不可用（调用方决定语义）。
    """
    repo = agent.attachment_repo
    for aid in ids:
        try:
            await repo.remove_ref(aid, message_id)
            doc = await repo.get(aid)
            if doc is None or (doc.get("ref_count") or 0) == 0:
                _ar.unregister(aid)
        except StorageUnavailableError:
            raise
        except Exception as e:
            logger.warning("附件 refs 释放失败 aid=%s: %s", aid, e)


async def release_messages_attachments(agent: "Agent", messages: list[Message]) -> None:
    """批量消息删除联动（清空/会话删除用）。"""
    for msg in messages:
        await release_message_attachments(agent, msg)


def _refs_of(messages: list[Message]) -> RefSnapshot:
    """抽出有附件的消息 → (message_id, attachment_ids)（无附件的整条跳过）。"""
    out: RefSnapshot = []
    for msg in messages:
        ids = attachment_ids_from_message(msg)
        if ids:
            out.append((str(getattr(msg, "message_id", "") or ""), ids))
    return out


async def collect_all_attachment_refs(agent: "Agent") -> RefSnapshot:
    """**提交前**快照全部归档消息的附件引用（清空归档/会话场景，分页遍历）。

    为什么必须在提交前读：`clear_all()` 之后消息已不存在，无法再反查它们引用了哪些
    附件——而 refs 不释放就会永久残留（`ref_count > 0` 让 GC 规则 3「未关联」永不命中，
    文件与记录一起泄漏）。快照只保留**有附件的**消息，体量远小于全量消息。
    """
    snap: RefSnapshot = []
    skip = 0
    while True:
        msgs, _ = await agent.message_repo.query_offset(
            skip=skip, limit=_ATTACHMENT_RELEASE_BATCH)
        if not msgs:
            break
        snap.extend(_refs_of(msgs))
        skip += len(msgs)
        if len(msgs) < _ATTACHMENT_RELEASE_BATCH:
            break
    return snap


async def collect_session_attachment_refs(agent: "Agent", sid: SessionId) -> RefSnapshot:
    """**提交前**快照单会话归档消息的附件引用（_id 游标遍历）。"""
    snap: RefSnapshot = []
    before = None
    while True:
        msgs, has_more = await agent.message_repo.query_page(
            sid, before=before, limit=_ATTACHMENT_RELEASE_BATCH)
        if not msgs:
            break
        snap.extend(_refs_of(msgs))
        if not has_more:
            break
        before = msgs[-1].metadata.get("_mongo_id")
    return snap


async def release_refs_snapshot(agent: "Agent", snapshot: RefSnapshot) -> int:
    """按快照释放附件引用，返回释放的消息条数。

    幂等：对已释放过的条目重复调用是 no-op（`remove_ref` 的条件 update）。

    Raises:
        StorageUnavailableError: 附件存储不可用（调用方决定语义）。
    """
    released = 0
    for message_id, ids in snapshot:
        await release_attachment_refs(agent, message_id, ids)
        released += 1
    return released


async def release_after_commit(agent: "Agent", snapshot: RefSnapshot) -> bool:
    """**提交后**清理入口：释放快照中的附件引用；失败不抛，改落 journal。

    为什么失败不抛：走到这里主操作（清空归档/会话、删除单条消息）已经提交，没有
    可回滚的东西——此时抛错只能被上层映射成"未删除（可重试）"，那是**谎报**（东西
    已经删了），而调用方按 503 重试时消息早已不在、快照为空，清理也不会再发生。
    #41 之前正是"先释放后提交"，于是 Mongo 故障时留下「refs 已软删、消息还在」的
    不可逆损失方向；现在失败方向变成「消息已删、refs 待释放」——由本 journal + 启动
    重试收口，最坏是延迟清理，不是数据丢失。

    Args:
        agent: Agent（取 attachment_repo）。
        snapshot: 提交前采集的 `(message_id, attachment_ids)` 快照。

    Returns:
        True 表示释放失败、已登记待重试（响应里如实暴露为 `cleanup_pending`）；
        False 表示无需清理或清理已完成。
    """
    if not snapshot:
        return False
    try:
        await release_refs_snapshot(agent, snapshot)
        return False
    except Exception as e:
        # 延迟导入：journal 模块 import 本模块（用 RefSnapshot 类型），顶层互相
        # import 会成环；此处只在失败路径用到。
        from core.storage.pending_attachment_release import get_journal

        pending = get_journal().register(snapshot)
        logger.warning(
            "附件引用释放失败——主操作已提交，登记待重试（%d 条消息 / journal 共 %d 条）: %s",
            len(snapshot), pending, e, exc_info=True,
        )
        return True


async def release_session_attachments(agent: "Agent", sid: SessionId) -> None:
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
