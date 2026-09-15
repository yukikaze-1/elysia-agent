"""后台任务终态结果本地 WAL（#6 硬杀窗口）。

**为什么在 `background_task_results` 仓储之外还要一个本地 WAL**（#6 的残余窗口）：

`BackgroundTaskResultRepository`（决策 #6）把终态落库放在 Agent 的 **mailbox 轮询**
上，轮询周期 0.5s（`_poll_mailboxes` 的 `asyncio.sleep`）。于是"任务终态已产生、
但还没被排空落库"之间存在一个 ≤1 个轮询周期的窗口——**进程在这一窗口内被硬杀
（SIGKILL / 掉电 / OOM killer），终态事件随内存一起消失，用户永远等不到结果**。
优雅停机（`_stop_mailbox` 的同步排空）覆盖不到硬杀：硬杀没有机会跑任何代码。

**做法**：把"持久化点"从**消费时刻**前移到**产生时刻**，并且写成**同步**的：

1. `dispatch` 记录——Agent 登记后台任务时（`_register_background_task`，同步方法）
   落一条 `task_id → intent/dispatched_at`。它让"派了任务但进程死了"这件事在磁盘上
   可判定，并且补回了终态记录本身没有的 `original_intent`。
2. `terminal` 记录——`push_mailbox_event()` 推终态事件时（引擎唯一终态出口，已带
   终态唯一性守卫）落一条 `task_id → status/result`。

**为什么必须同步（不能 async）**：终态推送发生在任务协程里，若落盘是 `await`，那么
`push_mailbox_event` 返回前的任意 await 点被取消/进程死亡，落盘就不会发生——而
"终态已产生"这件事恰恰是此刻唯一需要被记住的。同步 `write + flush + fsync` 保证
`push_mailbox_event` 返回时记录已在盘上（代价：一次本地 fsync，每个任务一条）。

**启动replay（`read_all` + Agent 侧 upsert）**：
- 有 terminal 记录 → upsert 成该终态（`delivered=False`），下次启动补投；
- 只有 dispatch 记录（进程死在任务中途）→ upsert 成 `interrupted`，附上 intent；
- **库里有记录的一律跳过**——Mongo 是权威，WAL 只补"库里还没有"的那部分。
  这条规则同时消掉了"落库成功但 WAL 尚未清理"导致的重复告知。

**失败语义**：WAL 写失败绝不抛出（否则一条磁盘问题会把任务完成也弄挂），但**不是
静默**——逐条 warning + 失败计数（`failure_count`，诊断/测试可见）。WAL 只是
"跨硬杀的额外保障"，它坏了不影响正常路径（正常路径仍由仓储保证）。

存储格式（JSONL，一行一条，追加写；清理用 `tmp` + `os.replace` 原子重写）：

    {"kind": "dispatch", "task_id": "ab12", "intent": "...", "dispatched_at": "...", "ts": "..."}
    {"kind": "terminal", "task_id": "ab12", "event": "completed", "data": {"result": "..."}, "ts": "..."}

**为什么存原始 `data` 而不是措辞好的文本**：终态文本是 core 的呈现逻辑
（`任务执行失败: …` / `任务已取消: …` 的措辞与回退默认值都在 Agent 侧）。WAL 若在
本层把措辞固化下来，硬杀补投与正常排空就会**对同一条结果给出两种文本**——正是本项目
反复出现的「两条路径不对称」。故本层只记事实（事件类型 + 原始 data），措辞由 core 的
唯一实现点（`_lifecycle_task_system.terminal_payload`）负责。

路径经 `ProjectPath.TASK_RESULT_WAL`（可用 `ELYSIA_PATH_TASK_RESULT_WAL` 覆盖——测试
据此隔离）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from core.schema.utils import now_iso
from paths import ProjectPath

logger = logging.getLogger(__name__)

#: 记录种类
KIND_DISPATCH = "dispatch"
KIND_TERMINAL = "terminal"

#: 硬杀窗口内"只有派发记录、没有终态记录"的任务，replay 时的候选状态。
#: 与优雅停机（`_stop_mailbox`）的 `interrupted` 同一个值，但措辞不同——
#: 那条能确定是 `/restart` 或正常退出，这条只说明进程是异常终止的。
STATUS_INTERRUPTED = "interrupted"


class TaskResultWAL:
    """后台任务终态结果的本地 WAL（进程内单例，见 `get_wal`）。"""

    def __init__(self, path: str | None = None) -> None:
        """初始化。

        Args:
            path: WAL 文件路径；None → `ProjectPath.TASK_RESULT_WAL`。
        """
        self._path = path or str(ProjectPath.TASK_RESULT_WAL.resolve())
        self._failures = 0

    @property
    def path(self) -> str:
        """WAL 文件路径。"""
        return self._path

    @property
    def failure_count(self) -> int:
        """累计写失败次数（诊断/测试用；>0 表示跨硬杀保障已降级）。"""
        return self._failures

    # ---- 写（同步、追加、fsync）----

    def _append(self, record: dict[str, Any]) -> bool:
        """追加一条记录并 fsync，返回是否成功（失败只记 warning，绝不抛）。"""
        try:
            path = Path(self._path)
            os.makedirs(str(path.parent), exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                # fsync 是"跨硬杀"的全部意义所在：不 fsync 的话记录只在内核页缓存里，
                # 掉电照样丢。每个任务一次，成本可忽略。
                os.fsync(f.fileno())
            return True
        except Exception:
            self._failures += 1
            logger.warning(
                "后台任务 WAL 写入失败（跨硬杀保障降级，本次任务不受影响）path=%s",
                self._path, exc_info=True,
            )
            return False

    def record_dispatch(
        self, task_id: str, intent: str, dispatched_at: str,
    ) -> bool:
        """登记一条派发记录（Agent 登记后台任务时调用，同步）。

        Args:
            task_id: 任务 ID。
            intent: 原始意图（终态记录里没有，只有这里能留下）。
            dispatched_at: 派发时间（ISO）。

        Returns:
            True 表示已落盘。
        """
        return self._append({
            "kind": KIND_DISPATCH,
            "task_id": str(task_id),
            "intent": intent,
            "dispatched_at": dispatched_at,
            "ts": now_iso(),
        })

    def record_terminal(
        self, task_id: str, event: str, data: dict[str, Any],
    ) -> bool:
        """登记一条终态记录（`push_mailbox_event` 推终态事件时调用，同步）。

        Args:
            task_id: 任务 ID。
            event: 事件类型值（completed / failed / cancelled）。
            data: 终态事件的**原始** data（措辞是 core 的事，见模块 docstring）。

        Returns:
            True 表示已落盘。
        """
        return self._append({
            "kind": KIND_TERMINAL,
            "task_id": str(task_id),
            "event": event,
            "data": dict(data),
            "ts": now_iso(),
        })

    # ---- 读 ----

    def read_raw(self) -> list[dict[str, Any]]:
        """按写入顺序返回全部记录（缺文件 → 空；损坏行跳过并记 warning）。

        **按字节读、逐行解码**，不是整体按 utf-8 文本读：硬杀可能把最后一行从
        任意字节处切断，若恰好在多字节字符中间（中文意图、emoji），整体解码会抛
        `UnicodeDecodeError` 并让**整个文件**不可读——那等于把前面所有完好的记录
        一起丢掉。逐行解码把损伤限制在真正受损的那一行。
        """
        path = Path(self._path)
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(path, "rb") as f:
                for lineno, raw in enumerate(f, start=1):
                    if not raw.strip():
                        continue
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning(
                            "后台任务 WAL 第 %d 行 UTF-8 解码失败"
                            "（硬杀截断在多字节字符中间？），已跳过", lineno)
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # 硬杀可能让最后一行只写了一半——这不是异常情况，是预期的。
                        logger.warning(
                            "后台任务 WAL 第 %d 行损坏（硬杀截断？），已跳过", lineno)
                        continue
                    if isinstance(item, dict) and item.get("task_id"):
                        records.append(item)
        except Exception:
            logger.warning("读取后台任务 WAL 失败 path=%s", self._path, exc_info=True)
            return []
        return records

    def replay(self) -> list[dict[str, Any]]:
        """折叠成"每个任务一条候选事件"，供启动补投。

        折叠规则：
        - `dispatch` 记录**后写覆盖先写**（同 task_id 重复登记以最后一次为准）；
        - `terminal` 记录**先写为准**（与引擎 `push_mailbox_event` 的终态唯一性守卫
          同一语义：首个终态即定论，后续终态本就被丢弃、不会进 WAL）；
        - 只有 dispatch 的任务 → `event=None` + `status=interrupted`（进程死在任务中途）。

        Returns:
            候选记录列表（按首次出现顺序），字段：
            task_id / event（None=只有派发记录）/ data / status /
            original_intent / dispatched_at。
        """
        order: list[str] = []
        dispatch: dict[str, dict[str, Any]] = {}
        terminal: dict[str, dict[str, Any]] = {}
        for rec in self.read_raw():
            task_id = str(rec["task_id"])
            if task_id not in order:
                order.append(task_id)
            if rec.get("kind") == KIND_TERMINAL:
                terminal.setdefault(task_id, rec)
            elif rec.get("kind") == KIND_DISPATCH:
                dispatch[task_id] = rec
        candidates: list[dict[str, Any]] = []
        for task_id in order:
            term = terminal.get(task_id)
            disp = dispatch.get(task_id, {})
            data = term.get("data") if term is not None else None
            candidates.append({
                "task_id": task_id,
                "event": str(term.get("event")) if term is not None else None,
                "data": data if isinstance(data, dict) else {},
                "status": (str(term.get("event")) if term is not None
                           else STATUS_INTERRUPTED),
                "original_intent": str(disp.get("intent") or ""),
                "dispatched_at": str(disp.get("dispatched_at") or ""),
            })
        return candidates

    # ---- 清理 ----

    def forget(self, task_ids: Any) -> None:
        """丢弃这些任务的记录（已成功写进仓储 → WAL 里的副本冗余）。

        `task_ids` 为空、或清理后不再有记录时，**删除文件**（与
        `memory_pending_deletes` / `session_pending_cleanup` 同约定：空 journal
        不应该在磁盘上留下一个空壳）。

        Args:
            task_ids: 待丢弃的 task_id 集合。
        """
        done = {str(t) for t in task_ids}
        if not done:
            return
        records = [rec for rec in self.read_raw() if str(rec["task_id"]) not in done]
        path = Path(self._path)
        try:
            if not records:
                if path.is_file():
                    os.remove(path)
                return
            os.makedirs(str(path.parent), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp, self._path)
        except Exception:
            self._failures += 1
            logger.warning(
                "后台任务 WAL 清理失败（记录会在下次启动被当作候选，但会被仓储里"
                "已有记录挡下）path=%s", self._path, exc_info=True)

    def clear(self) -> None:
        """清空 WAL（测试/诊断用）。"""
        self.forget([str(rec["task_id"]) for rec in self.read_raw()])


_wal: TaskResultWAL | None = None


def get_wal() -> TaskResultWAL:
    """进程内单例（首次调用时按当前 `ELYSIA_PATH_TASK_RESULT_WAL` 解析路径）。"""
    global _wal
    if _wal is None:
        _wal = TaskResultWAL()
    return _wal


def reset_wal() -> None:
    """丢弃单例（测试隔离用——`ELYSIA_PATH_*` 是每次 resolve 才读的）。"""
    global _wal
    _wal = None
