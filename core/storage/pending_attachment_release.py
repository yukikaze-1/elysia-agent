"""提交后附件引用释放失败 journal（#41 / CR-04 彻底版）。

**为什么需要它**：`core/input/debug_routes/archive.py` 的四条删除路径改成
「先提交主操作、再释放附件引用」之后，释放失败不再可能回滚（主操作已落库）。
若只记一条 warning，`refs` 就永久残留——`ref_count > 0` 让 GC 规则 3（未关联超
7 天）永不命中，附件记录与磁盘文件一起泄漏，且没有任何机制会再尝试释放。

本 journal 把「待释放的 `(message_id, attachment_ids)`」落盘，由
`retry(agent)` 在**启动时**与**Mongo 恢复边界**重试；释放是幂等的
（`AttachmentRepository.remove_ref` 以 `refs` 含该 message_id 为条件 update），
重复重试不会把 `ref_count` 减成负数。

格式（原子写；空集合删除文件，与 `memory_pending_deletes` /
`session_pending_cleanup` 同模式）：

    {"<message_id>": ["<attachment_id>", ...], ...}

路径经 `ProjectPath.PENDING_ATTACHMENT_RELEASE`（可用
`ELYSIA_PATH_PENDING_ATTACHMENT_RELEASE` 覆盖——测试据此隔离）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from core.schema.types import AttachmentId
from core.storage.attachment_release import RefSnapshot, release_attachment_refs
from paths import ProjectPath

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


class PendingAttachmentRelease:
    """待释放附件引用 journal（进程内实例 + 磁盘落盘）。"""

    def __init__(self, path: str | None = None):
        """初始化并从磁盘加载（损坏/缺失 → 视为空，不阻塞）。

        Args:
            path: journal 文件路径；None → `ProjectPath.PENDING_ATTACHMENT_RELEASE`。
        """
        self._path = path or str(ProjectPath.PENDING_ATTACHMENT_RELEASE.resolve())
        self._entries: dict[str, list[AttachmentId]] = {}
        self.load()

    @property
    def path(self) -> str:
        """journal 文件路径。"""
        return self._path

    def load(self) -> None:
        """启动时从 journal 加载待释放条目（损坏/缺失忽略）。"""
        try:
            path = Path(self._path)
            if not path.is_file():
                self._entries = {}
                return
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._entries = {
                str(mid): [AttachmentId(str(a)) for a in (aids or [])]
                for mid, aids in (data or {}).items()
            }
        except Exception:
            logger.warning("读取 pending attachment release journal 失败", exc_info=True)
            self._entries = {}

    def save(self) -> None:
        """持久化（原子写 `tmp` + `os.replace`；空集合删文件）。"""
        try:
            path = Path(self._path)
            if not self._entries:
                if path.is_file():
                    os.remove(path)
                return
            os.makedirs(str(path.parent), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {mid: [str(a) for a in aids] for mid, aids in self._entries.items()},
                    f, ensure_ascii=False,
                )
            os.replace(tmp, self._path)
        except Exception:
            logger.warning("写入 pending attachment release journal 失败", exc_info=True)

    def register(self, snapshot: RefSnapshot) -> int:
        """登记待释放条目（同 message_id 合并去重），返回 journal 内消息总数。"""
        for message_id, ids in snapshot:
            if not message_id or not ids:
                continue
            merged = self._entries.setdefault(message_id, [])
            for aid in ids:
                if aid not in merged:
                    merged.append(aid)
        self.save()
        return len(self._entries)

    def snapshot(self) -> RefSnapshot:
        """当前待释放条目（副本，调用方可安全消费）。"""
        return [(mid, list(aids)) for mid, aids in self._entries.items()]

    def pending_count(self) -> int:
        """待释放消息条数（0 表示无残留）。"""
        return len(self._entries)

    async def retry(self, agent: "Agent") -> int:
        """重试全部待释放条目，返回成功释放的条数。

        逐条独立：单条失败（存储仍不可用）保留在 journal 里下轮再试，不影响其他条目。
        全部成功时 journal 文件被删除（空集合语义）。
        """
        if not self._entries:
            return 0
        released = 0
        for message_id, ids in list(self._entries.items()):
            try:
                await release_attachment_refs(agent, message_id, list(ids))
            except Exception:
                logger.debug("待释放附件引用重试失败 message_id=%s", message_id,
                             exc_info=True)
                continue
            self._entries.pop(message_id, None)
            released += 1
        if released:
            self.save()
            logger.info("待释放附件引用重试：释放 %d 条消息的 refs（剩余 %d 条）",
                        released, len(self._entries))
        return released


# 模块级单例（进程内；path 变化时自动重建——测试用 ELYSIA_PATH_* 覆盖可隔离）
_journal: PendingAttachmentRelease | None = None


def get_journal() -> PendingAttachmentRelease:
    """返回进程级 journal；解析出的路径变化时重建（测试隔离用）。"""
    global _journal
    path = str(ProjectPath.PENDING_ATTACHMENT_RELEASE.resolve())
    if _journal is None or _journal.path != path:
        _journal = PendingAttachmentRelease(path)
    return _journal


def reset_journal() -> None:
    """丢弃进程级 journal（测试隔离）。"""
    global _journal
    _journal = None
