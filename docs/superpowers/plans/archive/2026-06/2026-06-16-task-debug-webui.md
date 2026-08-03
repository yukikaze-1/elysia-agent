# TaskSystem Debug WebUI 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 TaskSystem 实时调试面板——SSE 推送任务状态变更，debug.html 新增「任务」tab 展示 DAG 进度

**Architecture:** 后端新建 TaskDebugAPI 类（内部 500ms 轮询 TaskSystem 内部状态 → diff → SSE 推送），前端用 EventSource 消费。SubAgent/Orchestrator 暴露运行时状态为实例属性供 debug API 直接读取。

**Tech Stack:** aiohttp StreamResponse (SSE), vanilla JS EventSource, 现有 debug.html 暗色主题 CSS

**Spec:** `docs/superpowers/specs/2026-06-16-task-debug-webui-design.md`

**状态：** Tasks 1-6 已实现（2026-06-16）。待 TaskSystem 接入应用层（`app['task_system']` 注册）后 Debug WebUI 激活。

---

## 文件结构

```
新建:
  Module/task_system/debug_api.py       # TaskDebugAPI 类（~200 行）

修改:
  Module/task_system/core/orchestrator.py   # self._exec_state 实例属性
  Module/task_system/core/sub_agent.py      # self._task_state + self._graph
  Module/task_system/task_system.py         # _get_debug_snapshot() + 3 查询方法
  core/input/server.py                    # 2 路由 + on_startup 启动
  static/debug.html                         # 新 tab（HTML + CSS + JS ~300 行）
```

---

### Task 1: Orchestrator — exec_state 提升为实例属性

**Files:**
- Modify: `Module/task_system/core/orchestrator.py`

- [ ] **Step 1: 在 run() 中将 exec_state 存为实例属性**

`orchestrator.py` 的 `run()` 方法中，`exec_state` 当前是局部变量。在创建/merge 后立即赋值 `self._exec_state`：

```python
# run() 方法内，约第 131-133 行
if resume_from is not None:
    exec_state = self._merge_exec_state(resume_from, graph)
else:
    exec_state = self._init_exec_state(graph)
# 新增：暴露给 debug API 实时读取
self._exec_state = exec_state
```

循环中 `exec_state` 会原地更新（`exec_state.updated_at = now_iso()` 等），`self._exec_state` 始终指向同一个可变对象，debug API 读取时自动看到最新值。

- [ ] **Step 2: 运行现有测试验证不破坏原有行为**

```bash
/home/yomu/data/anaconda3/envs/Elysia/bin/python -m pytest Module/task_system/tests/core/test_orchestrator.py -q
```

Expected: 所有测试 PASS。

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/core/orchestrator.py
git commit -m "refactor(orchestrator): exec_state 提升为 self._exec_state 实例属性

供 TaskDebugAPI 实时读取执行状态，无需通过 checkpoint 间接访问。"
```

---

### Task 2: SubAgent — task_state + graph 实例属性

**Files:**
- Modify: `Module/task_system/core/sub_agent.py`

- [ ] **Step 1: run() 入口保存 self._task_state**

在 `run()` 方法创建 `task_state` 后立即赋值（约第 137-143 行）：

```python
task_state = TaskState(
    task_id=task_id,
    status=TaskStatus.PENDING,
    tools=context.tools,
    session_id=context.session_id,
    created_at=now_iso(),
    updated_at=now_iso(),
)
# 新增：暴露给 debug API
self._task_state = task_state
```

- [ ] **Step 2: _plan_and_validate() 返回后保存 self._graph**

在 `run()` 方法中 `_plan_and_validate()` 返回后（约第 146 行）：

```python
graph = await self._plan_and_validate(context, task_state, mailbox)
if graph is None:
    return  # 校验 ABORT

# 新增：暴露给 debug API
self._graph = graph
```

- [ ] **Step 3: _handle_replan() 中更新 self._graph**

在 `_handle_replan()` 方法中，全量重建路径（约第 542 行 `graph.replace(new_graph)` 之后）：

```python
# 全量重建——用新 graph 替换旧 graph 内部状态
graph.replace(new_graph)
# 新增：debug API 引用指向新 graph
self._graph = graph
```

局部修复路径（约第 557 行 `graph.apply_patch(replan_patch)` 之后）：

```python
graph.apply_patch(replan_patch)
# 新增：debug API 引用指向更新后的 graph（原地修改，但显式赋值确保一致性）
self._graph = graph
```

- [ ] **Step 4: 运行相关测试**

```bash
/home/yomu/data/anaconda3/envs/Elysia/bin/python -m pytest Module/task_system/tests/core/test_sub_agent.py -q
```

Expected: 所有测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/core/sub_agent.py
git commit -m "refactor(sub_agent): task_state + graph 存为实例属性

供 TaskDebugAPI 直接读取运行时状态：
- self._task_state: TaskState（run() 入口赋值）
- self._graph: PlanGraph（首次规划后赋值，replan 后更新）"
```

---

### Task 3: TaskSystem — _get_debug_snapshot() + 公共查询方法

**Files:**
- Modify: `Module/task_system/task_system.py`

- [ ] **Step 1: 实现 _get_debug_snapshot()**

在 `TaskSystem` 类中新增方法（放在 `submit()` 方法之后，私有方法区）：

```python
def _get_debug_snapshot(self) -> dict:
    """供 TaskDebugAPI 内部轮询使用。

    线程安全说明：
        本方法在 TaskDebugAPI._poll_loop() 中调用，与 Orchestrator 主循环
        运行在同一个 asyncio event loop 中。CPython + asyncio 的协作式调度
        保证本方法在遍历 _tasks / _exec_state 期间不会在非 await 点被挂起——
        因此读取 exec_state.node_states 等可变 dict 时不需显式加锁。

        约束：本方法内禁止加 await。若将来需要异步 I/O，必须在 await 之前
        复制快照，await 之后重新获取 exec_state 引用。
    """
    tasks = []
    for task_id, sub_agent in self._tasks.items():
        # exec_state 可能为 None——任务刚创建、Orchestrator 尚未 run()
        try:
            exec_state = sub_agent._orchestrator._exec_state
        except AttributeError:
            exec_state = None
        # graph 可能为 None——尚未完成首次规划
        try:
            graph = sub_agent._graph
        except AttributeError:
            graph = None
        tasks.append({
            "task_state": sub_agent._task_state,
            "graph": graph,
            "exec_state": exec_state,
            "mailbox_depth": self._mailboxes[task_id].count(),
        })
    return {
        "tasks": tasks,
        "slots_total": self._max_sub_agents,
        "slots_used": len(self._tasks),
    }
```

- [ ] **Step 2: 实现 list_active_tasks()**

替换现有 `raise NotImplementedError` stub（约第 286 行）：

```python
async def list_active_tasks(self) -> list[TaskSummary]:
    """列出所有活跃任务（含终态但尚未 cleanup 的任务）。"""
    summaries: list[TaskSummary] = []
    for task_id, sub_agent in self._tasks.items():
        ts = sub_agent._task_state
        checkpoint = await self._checkpoint_mgr.load(task_id)
        exec_state = checkpoint.exec if checkpoint else None

        # 节点计数
        nodes_total = 0
        nodes_completed = 0
        nodes_failed = 0
        nodes_skipped = 0
        if exec_state:
            nodes_total = len(exec_state.node_states)
            for ns in exec_state.node_states.values():
                if ns.status in (NodeStatus.SUCCESS,):
                    nodes_completed += 1
                elif ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                    nodes_failed += 1
                elif ns.status == NodeStatus.SKIPPED:
                    nodes_skipped += 1

        summaries.append(TaskSummary(
            task_id=ts.task_id,
            status=ts.status,
            intent=ts.plan.dag.nodes[0].description if ts.plan and ts.plan.dag.nodes else "",
            created_at=ts.created_at,
            duration_ms=int(
                (datetime.now(UTC) - datetime.fromisoformat(ts.created_at)).total_seconds() * 1000
            ) if ts.created_at else 0,
            nodes={"total": nodes_total, "completed": nodes_completed,
                   "failed": nodes_failed, "skipped": nodes_skipped},
            tools_used=[n.description for n in ts.plan.dag.nodes] if ts.plan else [],
            token_usage={},  # v1: token 追踪未接入
            approvals={"requested": 0, "approved": 0, "denied": 0},  # v1: 审批未实现
            error=ts.error,
            error_code=ts.error_code,
            replans=ts.plan.plan_version - 1 if ts.plan else 0,
        ))
    return summaries
```

需要补 import（文件顶部）：

```python
from datetime import datetime, UTC
from Module.task_system.schema import (
    # ... 已有 imports ...
    NodeStatus,
    TaskSummary,
)
```

- [ ] **Step 3: 实现 get_task_dag()**

```python
async def get_task_dag(self, task_id: TaskId) -> TaskDAG | None:
    """获取任务的 DAG 可视化数据。"""
    sub_agent = self._tasks.get(task_id)
    if sub_agent is None:
        # 检查 checkpoint（任务已终态 cleanup）
        checkpoint = await self._checkpoint_mgr.load(task_id)
        if checkpoint is None or checkpoint.task.plan is None:
            return None
        from Module.task_system.core.graph import PlanGraph
        graph = PlanGraph.from_plan(checkpoint.task.plan)
        return graph.to_dag_view(checkpoint.exec.node_states if checkpoint.exec else {})

    try:
        graph = sub_agent._graph
    except AttributeError:
        return None
    try:
        exec_state = sub_agent._orchestrator._exec_state
    except AttributeError:
        exec_state = None
    return graph.to_dag_view(exec_state.node_states if exec_state else {})
```

- [ ] **Step 4: 实现 get_task_summary()**

```python
async def get_task_summary(self, task_id: TaskId) -> TaskSummary | None:
    """获取单个任务摘要。"""
    summaries = await self.list_active_tasks()
    for s in summaries:
        if s.task_id == task_id:
            return s
    return None
```

- [ ] **Step 5: 保留其余 stub 方法不动**

`get_progress()`, `get_token_usage()`, `list_agents()`, `get_token_stats()` 保持 `raise NotImplementedError`——debug API 不依赖它们。

- [ ] **Step 6: 运行测试**

```bash
/home/yomu/data/anaconda3/envs/Elysia/bin/python -m pytest Module/task_system/tests/ --ignore=Module/task_system/tests/core/test_e2e_mvp.py -q
```

Expected: 259 passed。

- [ ] **Step 7: Commit**

```bash
git add Module/task_system/task_system.py
git commit -m "feat(task_system): _get_debug_snapshot() + list_active_tasks/get_task_dag/get_task_summary

新增供 TaskDebugAPI 使用的内部快照方法 + 3 个公共查询方法实现。
_get_debug_snapshot 含 asyncio 线程安全约束注释。"
```

---

### Task 4: TaskDebugAPI — 新建 debug_api.py

**Files:**
- Create: `Module/task_system/debug_api.py`

- [ ] **Step 1: 创建文件骨架 + 类定义**

```python
"""TaskDebugAPI — TaskSystem 调试数据收集 + SSE 实时推送。

内部 500ms 轮询 TaskSystem 内部状态，diff 后通过 SSE 推送给前端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from Module.task_system.task_system import TaskSystem

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0   # 30s 无事件则发心跳
_POLL_INTERVAL = 0.5         # 500ms 内部轮询


class TaskDebugAPI:
    """收集 TaskSystem 运行时状态，diff 后 SSE 推送给前端。

    使用方式：
        api = TaskDebugAPI(task_system)
        await api.start()

        app.router.add_get('/debug/api/tasks', api.snapshot_handler)
        app.router.add_get('/debug/api/tasks/stream', api.sse_handler)

    生命周期：
        start()  → 启动 _poll_loop 后台协程
        stop()   → 取消 _poll_loop，清理 subscribers
    """

    def __init__(self, task_system: TaskSystem) -> None:
        self._ts = task_system
        self._subscribers: dict[str, asyncio.Queue[dict]] = {}
        self._last_snapshot: list[dict] | None = None
        self._last_task_ids: set[str] = set()
        self._last_event_time: float = time.monotonic()
        self._poll_task: asyncio.Task[None] | None = None
```

- [ ] **Step 2: 实现 start/stop**

```python
    async def start(self) -> None:
        if self._poll_task is not None:
            return
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("TaskDebugAPI: 轮询已启动 (interval=%ss)", _POLL_INTERVAL)

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        # 通知所有订阅者关闭连接
        for q in self._subscribers.values():
            await q.put(None)  # sentinel
        self._subscribers.clear()
        logger.info("TaskDebugAPI: 已停止")
```

- [ ] **Step 3: 实现 _collect_snapshot()——裁剪 node_states**

```python
    def _collect_snapshot(self) -> list[dict]:
        """从 TaskSystem._get_debug_snapshot() 获取原始数据，裁剪后返回。

        裁剪策略：
        - node_states 只保留 {node_id, status, attempt, summary, error}
        - 不序列化 evaluation（Planner replan 内部用，前端不需要）
        - graph 不序列化完整 PlanGraph——前端从 TaskDAG endpoint 按需获取
        """
        raw = self._ts._get_debug_snapshot()
        tasks = []
        for t in raw["tasks"]:
            ts = t["task_state"]
            exec_state = t["exec_state"]

            nodes = []
            if exec_state:
                for nid, ns in exec_state.node_states.items():
                    nodes.append({
                        "node_id": nid,
                        "status": ns.status.value if hasattr(ns.status, 'value') else str(ns.status),
                        "attempt": ns.attempt,
                        "summary": ns.summary if ns.summary else "",
                        "error": ns.error if ns.error else "",
                    })

            # edges: 从 graph 提取
            edges = []
            graph = t.get("graph")
            if graph is not None:
                try:
                    raw_edges = list(graph._graph.edges)  # NetworkX DiGraph
                    edges = [[str(e[0]), str(e[1])] for e in raw_edges]
                except Exception:
                    pass

            tasks.append({
                "task_id": str(ts.task_id),
                "status": ts.status.value if hasattr(ts.status, 'value') else str(ts.status),
                "plan_version": ts.plan.plan_version if ts.plan else 0,
                "created_at": ts.created_at,
                "error": ts.error if ts.error else "",
                "error_code": ts.error_code.value if ts.error_code else "",
                "nodes": nodes,
                "edges": edges,
                "mailbox_depth": t["mailbox_depth"],
                "phase": exec_state.current_phase.value if exec_state and hasattr(exec_state, 'current_phase') else "",
            })
        return tasks
```

- [ ] **Step 4: 实现 _diff()——比较快照产出 SSE 事件**

```python
    def _diff(self, old: list[dict] | None, new: list[dict]) -> list[dict]:
        """比较两次快照，产出 SSE 事件列表。"""
        if old is None:
            return []  # 首次轮询，等待第一个 subscribe 触发全量 snapshot

        events: list[dict] = []
        old_by_id = {t["task_id"]: t for t in old}
        new_by_id = {t["task_id"]: t for t in new}

        # 新增任务
        for tid in new_by_id:
            if tid not in old_by_id:
                events.append({"event": "task_new", "data": new_by_id[tid]})

        # 状态/进度变更
        for tid in new_by_id:
            if tid in old_by_id:
                nt = new_by_id[tid]
                ot = old_by_id[tid]
                if nt["status"] != ot["status"]:
                    events.append({"event": "task_update", "data": {
                        "task_id": tid,
                        "status": nt["status"],
                        "nodes": nt["nodes"],
                    }})
                    # 终态
                    if nt["status"] in ("completed", "failed", "cancelled"):
                        events.append({"event": "task_done", "data": {
                            "task_id": tid,
                            "status": nt["status"],
                            "error": nt["error"],
                        }})
                # 节点状态变更
                elif nt["nodes"] != ot["nodes"]:
                    old_nodes = {n["node_id"]: n for n in ot["nodes"]}
                    for nn in nt["nodes"]:
                        on = old_nodes.get(nn["node_id"])
                        if on and nn["status"] != on["status"]:
                            events.append({"event": "node_update", "data": {
                                "task_id": tid,
                                "node_id": nn["node_id"],
                                "status": nn["status"],
                                "attempt": nn["attempt"],
                            }})

        # 消失的任务——补发 task_done
        current_ids = set(new_by_id.keys())
        removed_ids = self._last_task_ids - current_ids
        for tid in removed_ids:
            ot = old_by_id.get(tid)
            if ot and ot["status"] not in ("completed", "failed", "cancelled"):
                events.append({"event": "task_done", "data": {
                    "task_id": tid,
                    "status": ot["status"],
                    "error": ot["error"],
                }})

        self._last_task_ids = current_ids
        return events
```

- [ ] **Step 5: 实现 _poll_loop()——核心循环**

```python
    async def _poll_loop(self) -> None:
        """500ms 间隔：收集快照 → diff → broadcast。"""
        while True:
            try:
                snapshot = self._collect_snapshot()
                events = self._diff(self._last_snapshot, snapshot)
                self._last_snapshot = snapshot

                now = time.monotonic()
                if events:
                    self._last_event_time = now
                    for event in events:
                        await self._broadcast(event)
                elif now - self._last_event_time >= _HEARTBEAT_INTERVAL:
                    await self._broadcast({"event": "heartbeat", "data": {}})
                    self._last_event_time = now

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TaskDebugAPI._poll_loop: 异常")

            await asyncio.sleep(_POLL_INTERVAL)
```

- [ ] **Step 6: 实现 subscribe/unsubscribe 和 broadcast**

```python
    async def subscribe(self) -> tuple[str, asyncio.Queue[dict]]:
        """新 SSE 客户端订阅。返回 (subscriber_id, queue)。"""
        import uuid
        sid = uuid.uuid4().hex[:8]
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._subscribers[sid] = q
        # 首条消息：全量快照
        if self._last_snapshot is not None:
            await q.put({"event": "snapshot", "data": self._last_snapshot})
        elif self._last_snapshot is None:
            # 首次 snapshot——立即收集
            self._last_snapshot = self._collect_snapshot()
            self._last_task_ids = {t["task_id"] for t in self._last_snapshot}
            await q.put({"event": "snapshot", "data": self._last_snapshot})
        logger.info("TaskDebugAPI: subscriber %s 已连接 (total=%d)", sid, len(self._subscribers))
        return sid, q

    async def unsubscribe(self, sid: str) -> None:
        self._subscribers.pop(sid, None)
        logger.info("TaskDebugAPI: subscriber %s 已断开 (total=%d)", sid, len(self._subscribers))

    async def _broadcast(self, event: dict) -> None:
        """推送给所有 subscriber。满队列的跳过（前端太慢）。"""
        for sid, q in list(self._subscribers.items()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("TaskDebugAPI: subscriber %s 队列满，跳过事件 %s", sid, event.get("event"))
```

- [ ] **Step 7: 实现 aiohttp handler**

```python
    async def snapshot_handler(self, request: web.Request) -> web.Response:
        """GET /debug/api/tasks —— 当前全量快照 JSON。"""
        snapshot = self._collect_snapshot()
        return web.json_response(snapshot)

    async def sse_handler(self, request: web.Request) -> web.StreamResponse:
        """GET /debug/api/tasks/stream —— SSE 事件流。"""
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(request)

        sid, q = await self.subscribe()
        try:
            while True:
                event = await q.get()
                if event is None:  # sentinel——stop()
                    break
                data = json.dumps(event["data"], ensure_ascii=False, default=str)
                await resp.write(
                    f"event: {event['event']}\ndata: {data}\n\n".encode("utf-8")
                )
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await self.unsubscribe(sid)
        return resp
```

- [ ] **Step 8: Commit**

```bash
git add Module/task_system/debug_api.py
git commit -m "feat(debug): TaskDebugAPI — SSE 实时推送 TaskSystem 状态

新增 TaskDebugAPI 类：
- 内部 500ms 轮询 TaskSystem._get_debug_snapshot()
- diff 新旧快照产出 SSE 事件（task_new/task_update/task_done/node_update/heartbeat）
- 订阅管理 + snapshot 首条全量 + 断连清理
- GET /debug/api/tasks (JSON) 和 /debug/api/tasks/stream (SSE) handler"
```

---

### Task 5: 路由注册 — server.py

**Files:**
- Modify: `core/input/server.py`

- [ ] **Step 1: 在 create_app() 中注册路由**

`server.py` 中 `create_app()` 函数已有 `app.router.add_get('/debug', ...)` 等路由。在附近新增两条：

```python
# 在 create_app() 函数内，约在 /debug/api 路由附近
from Module.task_system.debug_api import TaskDebugAPI

# 创建并启动 TaskDebugAPI（需要访问 task_system 实例）
# task_system 通常在 app['task_system'] 中
task_debug_api = TaskDebugAPI(app.get('task_system'))
app['task_debug_api'] = task_debug_api

app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
```

- [ ] **Step 2: 在 on_startup 中启动轮询**

```python
# 在已有的 on_startup 回调中追加
async def on_startup(app):
    # ... 现有启动逻辑 ...
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.start()
```

- [ ] **Step 3: 在 on_shutdown 中停止**

```python
async def on_shutdown(app):
    # ... 现有关闭逻辑 ...
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.stop()
```

- [ ] **Step 4: Commit**

```bash
git add core/input/server.py
git commit -m "feat(server): 注册 /debug/api/tasks 和 /debug/api/tasks/stream 路由

TaskDebugAPI 在 create_app() 中创建，on_startup 启动轮询，on_shutdown 停止。"
```

---

### Task 6: 前端 — debug.html 新增「任务」Tab

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 新增 tab 按钮（HTML）**

在现有 tab 栏中追加 `<button>`（约在「数据库」tab 按钮之后）：

```html
<button id="tab-tasks-btn" onclick="switchTab('tasks')">任务</button>
```

- [ ] **Step 2: 新增任务 tab 容器（HTML）**

在现有 `<section id="tab-database">` 之后追加：

```html
<section id="tab-tasks" style="display:none;">
  <!-- TaskSystem 概览条 -->
  <div id="tasks-overview" class="card" style="margin-bottom:1em; padding:0.5em 1em; font-size:0.9em; color:var(--muted);">
    <span id="tasks-stats">运行中: 0 | 可用槽位: -/- | SSE: ⏳连接中</span>
  </div>

  <!-- 运行中任务卡片（动态渲染） -->
  <div id="tasks-active"></div>

  <!-- 已完成/失败任务摘要（折叠） -->
  <details id="tasks-done-section" style="margin-top:1em;">
    <summary>已完成/失败的任务</summary>
    <div id="tasks-done-list"></div>
  </details>
</section>
```

- [ ] **Step 3: 新增 CSS 样式（追加在现有 `<style>` 标签末尾）**

```css
/* ── 任务 Tab ── */
.task-card {
  background: var(--surface);
  border-radius: 8px;
  padding: 0.8em 1em;
  margin-bottom: 0.6em;
  border-left: 3px solid var(--accent);
}
.task-card.status-running  { border-left-color: #4fc3f7; }
.task-card.status-completed { border-left-color: #66bb6a; opacity: 0.8; }
.task-card.status-failed   { border-left-color: #ef5350; }
.task-card.status-cancelled { border-left-color: #888; opacity: 0.6; }

.task-card .task-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 0.4em;
}
.task-card .task-header .task-id { font-family: monospace; font-size: 0.85em; color: var(--muted); }
.task-card .task-header .task-status { font-weight: bold; font-size: 0.85em; }
.task-card .task-header .task-status.status-running { color: #4fc3f7; }
.task-card .task-header .task-status.status-completed { color: #66bb6a; }
.task-card .task-header .task-status.status-failed { color: #ef5350; }

.task-card .task-intent { font-size: 0.9em; margin-bottom: 0.3em; }
.task-card .task-meta { font-size: 0.8em; color: var(--muted); margin-bottom: 0.4em; }

.task-card .task-dag {
  font-family: monospace; font-size: 0.82em; line-height: 1.6;
  white-space: pre; background: var(--bg); padding: 0.5em;
  border-radius: 4px; margin-top: 0.3em; overflow-x: auto;
}

.task-progress-bar {
  height: 4px; background: var(--bg); border-radius: 2px; margin: 0.4em 0;
}
.task-progress-bar .fill { height: 100%; border-radius: 2px; background: var(--accent); }

.task-done-row {
  font-size: 0.85em; padding: 0.3em 0;
  border-bottom: 1px solid rgba(255,255,255,0.05);
}
```

- [ ] **Step 4: 新增 switchTab('tasks') 支持**

现有 `switchTab()` 函数只处理 'overview' 和 'database'。追加 'tasks' 分支：

```javascript
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-overview').style.display = 'none';
  document.getElementById('tab-database').style.display = 'none';
  document.getElementById('tab-tasks').style.display = 'none';

  if (name === 'overview') {
    document.getElementById('tab-overview-btn').classList.add('active');
    document.getElementById('tab-overview').style.display = '';
  } else if (name === 'database') {
    document.getElementById('tab-database-btn').classList.add('active');
    document.getElementById('tab-database').style.display = '';
    loadDatabaseTab();
  } else if (name === 'tasks') {
    document.getElementById('tab-tasks-btn').classList.add('active');
    document.getElementById('tab-tasks').style.display = '';
    connectTasksSSE();
  }
}
```

- [ ] **Step 5: 新增 tasks SSE JS 逻辑（~250 行，追加在 `</script>` 前）**

```javascript
// ═══════════════════════════════════════════════════════════════════
// TaskSystem Debug Tab — SSE 实时推送
// ═══════════════════════════════════════════════════════════════════

let taskES = null;
let taskReconnectTimer = null;
let taskReconnectDelay = 1000;

function connectTasksSSE() {
  if (taskES && taskES.readyState <= 1) return; // 已连接或正在连接
  if (taskReconnectTimer) { clearTimeout(taskReconnectTimer); taskReconnectTimer = null; }

  document.getElementById('tasks-stats').textContent = '运行中: ... | SSE: ⏳连接中';

  taskES = new EventSource('/debug/api/tasks/stream');

  taskES.addEventListener('snapshot', function(e) {
    const tasks = JSON.parse(e.data);
    renderAllTasks(tasks);
    document.getElementById('tasks-stats').innerHTML =
      '运行中: ' + tasks.filter(t => t.status === 'running').length +
      ' | SSE: <span style="color:#66bb6a">●已连接</span>';
    taskReconnectDelay = 1000; // 成功连接，reset 退避
  });

  taskES.addEventListener('task_new', function(e) {
    const t = JSON.parse(e.data);
    insertTaskCard(t);
    updateTasksStats();
  });

  taskES.addEventListener('task_update', function(e) {
    const d = JSON.parse(e.data);
    updateTaskCard(d.task_id, d.status, d.nodes);
    updateTasksStats();
  });

  taskES.addEventListener('task_done', function(e) {
    const d = JSON.parse(e.data);
    moveTaskToDone(d.task_id, d.status, d.error);
    updateTasksStats();
  });

  taskES.addEventListener('node_update', function(e) {
    const d = JSON.parse(e.data);
    updateNodeInCard(d.task_id, d.node_id, d.status, d.attempt);
  });

  taskES.addEventListener('heartbeat', function(e) {
    // 静默——保持连接
  });

  taskES.onerror = function() {
    document.getElementById('tasks-stats').innerHTML =
      '运行中: ... | SSE: <span style="color:#ef5350">●已断开</span> (重连中...)';
    taskES.close();
    taskES = null;
    taskReconnectTimer = setTimeout(function() {
      connectTasksSSE();
      taskReconnectDelay = Math.min(taskReconnectDelay * 2, 30000);
    }, taskReconnectDelay);
  };
}

// ── DAG 渲染 ──

// 计算汇合节点（被多个前驱指向的节点）——用于绘制 ASCII 分支
function findMergeNodes(edges) {
  const counts = {};
  edges.forEach(function(e) { counts[e[1]] = (counts[e[1]] || 0) + 1; });
  const merges = new Set();
  for (var nid in counts) { if (counts[nid] > 1) merges.add(nid); }
  return merges;
}

function renderDAG(nodes, edges) {
  if (!nodes || nodes.length === 0) return '(空 DAG)';

  var merges = findMergeNodes(edges || []);
  var hasSplit = merges.size > 0;

  // 简单链状 DAG：一行一个节点
  if (!hasSplit || nodes.length <= 1) {
    return nodes.map(function(n) { return statusIcon(n.status) + ' ' + n.node_id; }).join('\n');
  }

  // 并行分支 DAG：按拓扑分层
  // 找出所有 merge 节点的前驱
  var mergePredecessors = {};
  (edges || []).forEach(function(e) {
    if (merges.has(e[1])) {
      if (!mergePredecessors[e[1]]) mergePredecessors[e[1]] = [];
      mergePredecessors[e[1]].push(e[0]);
    }
  });

  var lines = [];
  var byId = {};
  nodes.forEach(function(n) { byId[n.node_id] = n; });

  // 先渲染非 merge 节点，再渲染 merge 节点
  var rendered = new Set();
  // 渲染独立节点
  nodes.forEach(function(n) {
    if (merges.has(n.node_id)) return; // skip merge nodes for now
    if (rendered.has(n.node_id)) return;
    rendered.add(n.node_id);

    var isPredecessor = false;
    for (var mid in mergePredecessors) {
      if (mergePredecessors[mid].indexOf(n.node_id) >= 0) isPredecessor = true;
    }

    if (isPredecessor) {
      lines.push(statusIcon(n.status) + ' ' + n.node_id + ' ─────────────┐');
    } else {
      lines.push(statusIcon(n.status) + ' ' + n.node_id);
    }
  });

  // 渲染汇合节点
  merges.forEach(function(mid) {
    var node = byId[mid];
    if (!node) return;
    if (rendered.has(mid)) return;
    rendered.add(mid);
    lines.push(' '.repeat(6) + '├→ ' + statusIcon(node.status) + ' ' + node.node_id);
  });

  return lines.join('\n');
}

function statusIcon(status) {
  switch (status) {
    case 'success': return '✅';
    case 'running': return '⏳';
    case 'pending': return '⬜';
    case 'failed':
    case 'unrecoverable': return '⊗';
    case 'skipped': return '⏭️';
    default: return '⬜';
  }
}

// ── 渲染 ──

function renderAllTasks(tasks) {
  var active = document.getElementById('tasks-active');
  var doneList = document.getElementById('tasks-done-list');
  active.innerHTML = '';
  doneList.innerHTML = '';

  var hasActive = false, hasDone = false;

  tasks.forEach(function(t) {
    if (t.status === 'running' || t.status === 'pending' || t.status === 'planning') {
      insertTaskCardDOM(active, t);
      hasActive = true;
    } else {
      renderDoneRow(doneList, t);
      hasDone = true;
    }
  });

  document.getElementById('tasks-done-section').style.display = hasDone ? '' : 'none';
  updateTasksStatsFrom(tasks);
}

function insertTaskCard(t) {
  if (t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled') {
    renderDoneRow(document.getElementById('tasks-done-list'), t);
    document.getElementById('tasks-done-section').style.display = '';
    return;
  }
  insertTaskCardDOM(document.getElementById('tasks-active'), t);
}

function insertTaskCardDOM(container, t) {
  var existing = document.getElementById('task-card-' + t.task_id);
  if (existing) existing.remove();

  var progress = 0;
  if (t.nodes && t.nodes.length > 0) {
    var done = t.nodes.filter(function(n) { return n.status === 'success' || n.status === 'skipped'; }).length;
    progress = Math.round(done / t.nodes.length * 100);
  }

  var edgesJson = JSON.stringify(t.edges || []);
  var html = '<div class="task-card status-' + t.status + '" id="task-card-' + t.task_id + '" data-edges=\'' + edgesJson + '\'>' +
    '<div class="task-header">' +
      '<span class="task-id">' + t.task_id + '</span>' +
      '<span class="task-status status-' + t.status + '">' + t.status.toUpperCase() + '</span>' +
    '</div>' +
    '<div class="task-meta">Replan: ' + (t.plan_version - 1) + '/' + t.plan_version +
    ' | 节点: ' + (t.nodes ? t.nodes.length : 0) + ' | 队列: ' + (t.mailbox_depth || 0) + '</div>' +
    '<div class="task-progress-bar"><div class="fill" style="width:' + progress + '%"></div></div>' +
    '<div class="task-dag">' + renderDAG(t.nodes, t.edges) + '</div>' +
  '</div>';

  container.insertAdjacentHTML('afterbegin', html);
}

function updateTaskCard(taskId, newStatus, nodes) {
  var card = document.getElementById('task-card-' + taskId);
  if (!card) return;

  var statusEl = card.querySelector('.task-status');
  if (statusEl) {
    statusEl.textContent = newStatus.toUpperCase();
    statusEl.className = 'task-status status-' + newStatus;
  }
  card.className = 'task-card status-' + newStatus;

  if (nodes) {
    var dagEl = card.querySelector('.task-dag');
    if (dagEl) {
      // 从现有 card 获取 edges（data attribute 存储）
      var edges = [];
      try { edges = JSON.parse(card.getAttribute('data-edges') || '[]'); } catch(e) {}
      dagEl.textContent = renderDAG(nodes, edges);
    }
    var progress = 0;
    if (nodes.length > 0) {
      var done = nodes.filter(function(n) { return n.status === 'success' || n.status === 'skipped'; }).length;
      progress = Math.round(done / nodes.length * 100);
    }
    var fill = card.querySelector('.task-progress-bar .fill');
    if (fill) fill.style.width = progress + '%';
  }
}

function updateNodeInCard(taskId, nodeId, newStatus, attempt) {
  var card = document.getElementById('task-card-' + taskId);
  if (!card) return;
  var dagEl = card.querySelector('.task-dag');
  if (!dagEl) return;

  // 在 dag 文本中替换节点状态行
  var lines = dagEl.textContent.split('\n');
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].indexOf(nodeId) >= 0) {
      // 替换状态 icon（前两个字符）
      var rest = lines[i].substring(2);
      lines[i] = statusIcon(newStatus) + rest;
      // 追加 attempt
      if (attempt > 1) {
        lines[i] += ' (retry ' + attempt + ')';
      }
    }
  }
  dagEl.textContent = lines.join('\n');
}

function moveTaskToDone(taskId, status, error) {
  var card = document.getElementById('task-card-' + taskId);
  if (!card) return;
  card.remove();

  var doneList = document.getElementById('tasks-done-list');
  var msg = status.toUpperCase();
  if (error) msg += ' — ' + error;
  var row = document.createElement('div');
  row.className = 'task-done-row';
  row.innerHTML = '<b>' + taskId + '</b>: ' + msg;
  doneList.insertBefore(row, doneList.firstChild);
  document.getElementById('tasks-done-section').style.display = '';

  updateTasksStats();
}

function renderDoneRow(container, t) {
  var msg = t.status.toUpperCase();
  if (t.error) msg += ' — ' + t.error;
  var row = document.createElement('div');
  row.className = 'task-done-row';
  row.innerHTML = '<b>' + t.task_id + '</b>: ' + msg;
  container.appendChild(row);
}

function updateTasksStats() {
  var active = document.getElementById('tasks-active');
  var cards = active.querySelectorAll('.task-card');
  document.getElementById('tasks-stats').innerHTML =
    '运行中: ' + cards.length + ' | SSE: <span style="color:#66bb6a">●已连接</span>';
}

function updateTasksStatsFrom(tasks) {
  var running = tasks.filter(function(t) {
    return t.status === 'running' || t.status === 'pending' || t.status === 'planning';
  }).length;
  document.getElementById('tasks-stats').innerHTML =
    '运行中: ' + running +
    ' | SSE: <span style="color:#66bb6a">●已连接</span>';
}
```

- [ ] **Step 6: Commit**

```bash
git add static/debug.html
git commit -m "feat(debug): debug.html 新增「任务」tab——SSE 实时 DAG 进度

- 新增 tab 按钮 + tab-tasks 容器
- TaskTab SSE 连接管理（EventSource + 指数退避重连）
- DAG 文本渲染：支持链状和并行分支 ASCII 边
- 状态 emoji（✅⏳⬜⊗）+ 进度条
- 已完成/失败任务折叠摘要
- 复用现有暗色主题 CSS 变量"
```

---

## 验证

完成所有 Task 后，端到端验证：

```bash
# 1. 跑全部非 e2e 测试——确保无回归
/home/yomu/data/anaconda3/envs/Elysia/bin/python -m pytest Module/task_system/tests/ --ignore=Module/task_system/tests/core/test_e2e_mvp.py -q
# Expected: 259 passed

# 2. 启动应用
# → 浏览器打开 /debug → 点击「任务」tab
# → SSE 应显示「已连接」
# → 触发一个后台任务 → 卡片实时出现
# → 节点状态实时更新

# 3. 断连测试
# → 关闭 /debug 页面 → 服务端日志显示 subscriber 已断开
# → 重新打开 → snapshot 恢复
```
