"""未完成整合的对话缓冲 journal（#10 / 决策 #10）。

**为什么需要它**：关机时 `Agent._flush_consolidator()` 只给 `flush()` **5 秒**
（`asyncio.wait_for`，见 `core/agent/lifecycle.py`）。一次记忆提取要调 LLM，
5 秒通常不够 → 超时取消 → `_do_consolidate` 的 `except BaseException` 把 batch
requeue 回缓冲（S2-A1 修的就是这个还原）——但**进程随即退出**，缓冲只活在内存里，
那最后 <6 轮对话的提取机会永久丢失（用户视角：关机前说的话没进长期记忆）。

owner 决策：**落盘，待下次启动补提**。于是：

- 关机（以及任何 requeue 失败路径）把缓冲写进本 journal；
- 下次启动构造 consolidator 时读回缓冲，且**跳过阈值**立刻补提一次
  （`_pending_restored` 标志，见 `MemoryConsolidator.consolidate`）；
- 补提失败 → 再次 requeue → 再次落盘，下次启动再试（不会静默丢）。

**格式**（单文件多 key，原子写；某 key 缓冲为空即从文件里移除）：

    {"<instance_id>": [["<user_msg>", "<assistant_msg>"], ...], ...}

用**单文件 + instance_id 作 key**、而不是"每 instance 一个文件"：instance_id 是
外部可控字符串（自定义实例），直接拼进文件名需要路径穿越防护，而
`sanitize_session_id` 会把非法字符整体降级成 `"default"` → 多实例**静默串到同一
文件**。做成 key 则既无穿越面也无串档（键不落路径）。

路径经 `ProjectPath.CONSOLIDATOR_PENDING`（可用
`ELYSIA_PATH_CONSOLIDATOR_PENDING` 覆盖——测试据此隔离）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from paths import ProjectPath

logger = logging.getLogger(__name__)

#: 缓冲条目类型——(user_msg, assistant_msg)
Turn = tuple[str, str]


class ConsolidatorPendingStore:
    """未完成整合缓冲的持久化（进程内实例 + 磁盘落盘）。"""

    def __init__(self, path: str | None = None):
        """初始化并从磁盘加载（损坏/缺失 → 视为空，不阻塞启动）。

        Args:
            path: journal 文件路径；None → `ProjectPath.CONSOLIDATOR_PENDING`。
        """
        self._path = path or str(ProjectPath.CONSOLIDATOR_PENDING.resolve())
        self._entries: dict[str, list[Turn]] = {}
        self.load()

    @property
    def path(self) -> str:
        """journal 文件路径。"""
        return self._path

    def load(self) -> None:
        """从磁盘加载（损坏/缺失忽略——journal 是尽力而为的补提通道）。"""
        try:
            path = Path(self._path)
            if not path.is_file():
                self._entries = {}
                return
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            entries: dict[str, list[Turn]] = {}
            for key, turns in (raw or {}).items():
                got: list[Turn] = []
                for item in turns or []:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        got.append((str(item[0]), str(item[1])))
                if got:
                    entries[str(key)] = got
            self._entries = entries
        except Exception:
            logger.warning("读取 consolidator pending journal 失败", exc_info=True)
            self._entries = {}

    def save(self) -> None:
        """持久化（原子写 `tmp` + `os.replace`；空则删文件）。"""
        try:
            path = Path(self._path)
            if not self._entries:
                if path.is_file():
                    os.remove(path)
                return
            os.makedirs(str(path.parent), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._entries, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            logger.warning("写入 consolidator pending journal 失败", exc_info=True)

    def put(self, key: str, turns: list[Turn]) -> None:
        """写入某 instance 的待整合缓冲（空列表 → 移除该 key 并落盘）。"""
        if not key:
            return
        if turns:
            self._entries[key] = [(str(u), str(a)) for u, a in turns]
        else:
            self._entries.pop(key, None)
        self.save()

    def get(self, key: str) -> list[Turn]:
        """读取某 instance 的待整合缓冲（副本；无则空列表）。"""
        return list(self._entries.get(key, []))

    def keys(self) -> list[str]:
        """当前有待整合缓冲的 instance key 列表。"""
        return list(self._entries.keys())

    def drop(self, key: str) -> None:
        """移除某 instance 的条目并落盘。"""
        if self._entries.pop(key, None) is not None:
            self.save()


# 模块级单例（进程内；path 变化时自动重建——测试用 ELYSIA_PATH_* 覆盖可隔离）
_store: ConsolidatorPendingStore | None = None


def get_store() -> ConsolidatorPendingStore:
    """返回进程级 journal；解析出的路径变化时重建（测试隔离用）。"""
    global _store
    path = str(ProjectPath.CONSOLIDATOR_PENDING.resolve())
    if _store is None or _store.path != path:
        _store = ConsolidatorPendingStore(path)
    return _store


def reset_store() -> None:
    """丢弃进程级 journal（测试隔离）。"""
    global _store
    _store = None
