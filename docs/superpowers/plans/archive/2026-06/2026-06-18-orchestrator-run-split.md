# Orchestrator.run() 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Orchestrator.run()` 主循环内逻辑块提取为 5 个私有方法，`run()` 从 ~280 行缩减到 ~45 行。

**Architecture:** 单文件 `orchestrator.py` 内部重构——提取私有方法，`run()` 每个步骤一行调用。`execution.py` / `finalize.py` 不动。

**Tech Stack:** Python 3.13, asyncio

## Global Constraints

- `run()` 签名和返回值不变——`SubAgent` 调用方零改动
- `self._exec_state = exec_state` 必须保留——`pause()` 通过它访问
- `_check_interrupts` 是 `async def`——含 `await self._save_checkpoint()`
- `is_terminal` 检查留在 `run()` 中（`break`），不放入 `_handle_no_ready`

---

### Task 1: 从 `run()` 提取 5 个私有方法

**Files:**
- Modify: `Module/task_system/core/orchestrator.py`

- [ ] **Step 1: 提取 `_init_exec`**

从 `run()` L128-148 提取初始化逻辑：

```python
def _init_exec(
    self,
    graph: PlanGraph,
    resume_from: ExecutionState | None,
    task_state: TaskState,
) -> ExecutionState:
    """初始化或合并 ExecutionState。复位移审批标志 + RESUMING→RUNNING。"""
    if resume_from is not None:
        exec_state = self._merge_exec_state(resume_from, graph)
    else:
        exec_state = self._init_exec_state(graph)
    exec_state.waiting_for_approval = False
    if task_state.status == TaskStatus.RESUMING:
        task_state.status = TaskStatus.RUNNING
        task_state.updated_at = now_iso()
    return exec_state
```

`run()` 中对应位置替换为：
```python
exec_state = self._init_exec(graph, resume_from, task_state)
self._exec_state = exec_state
```

- [ ] **Step 2: 提取 `_check_interrupts`**（async）

从 `run()` L155-179 提取中断检查：

```python
async def _check_interrupts(
    self,
    task_state: TaskState,
    exec_state: ExecutionState,
    task_id: TaskId,
) -> OrchestratorResult | None:
    """检查外部中断信号（pause / cancel）。返回结果或 None（继续）。"""
    if exec_state.pause_requested:
        logger.info("Orchestrator: task_id=%s 收到暂停请求", task_id)
        task_state.status = TaskStatus.PAUSED
        task_state.updated_at = now_iso()
        await self._save_checkpoint(task_state, exec_state)
        return OrchestratorResult(
            status=OrchestratorRunStatus.PAUSED,
            checkpoint_state=self._build_checkpoint(task_state, exec_state),
        )
    if exec_state.cancel_requested:
        logger.info("Orchestrator: task_id=%s 收到取消请求", task_id)
        task_state.status = TaskStatus.CANCELLED
        task_state.updated_at = now_iso()
        await self._save_checkpoint(task_state, exec_state)
        return OrchestratorResult(
            status=OrchestratorRunStatus.CANCELED,
            checkpoint_state=self._build_checkpoint(task_state, exec_state),
        )
    return None
```

`run()` 中替换为：
```python
if result := await self._check_interrupts(task_state, exec_state, task_id):
    return result
```

- [ ] **Step 3: 提取 `_handle_no_ready`**（sync）

从 `run()` L183-213 提取。is_terminal 留在 run() 中：

```python
def _handle_no_ready(
    self,
    task_state: TaskState,
    exec_state: ExecutionState,
    graph: PlanGraph,
    task_id: TaskId,
) -> OrchestratorResult | None:
    """无就绪节点且非终态——判定阻塞/失败/死锁。终态由 run() 外层的 break 处理。"""
    if self._has_blocked_nodes(exec_state):
        task_state.status = TaskStatus.WAITING_APPROVAL
        task_state.updated_at = now_iso()
        return OrchestratorResult(
            status=OrchestratorRunStatus.BLOCKED,
            checkpoint_state=self._build_checkpoint(task_state, exec_state),
        )
    if graph.has_failed_required(exec_state.node_states):
        failed_info = graph.collect_failure_info(exec_state.node_states)
        return OrchestratorResult(
            status=OrchestratorRunStatus.NEEDS_REPLAN,
            error_context=failed_info,
            checkpoint_state=self._build_checkpoint(task_state, exec_state),
        )
    logger.error("Orchestrator 死锁: task_id=%s", task_id)
    task_state.status = TaskStatus.FAILED
    task_state.updated_at = now_iso()
    return OrchestratorResult(
        status=OrchestratorRunStatus.FAILED,
        error_context={"reason": "检测到死锁——无可执行节点但任务未完成"},
    )
```

`run()` 中替换为：
```python
ready_ids = graph.get_ready_nodes(exec_state.node_states)
if not ready_ids:
    if graph.is_terminal(exec_state.node_states):
        break
    if result := self._handle_no_ready(task_state, exec_state, graph, task_id):
        return result
```

注意：`_handle_no_ready` 不再含 `save_checkpoint` 调用——原始代码中 BLOCKED 和 NEEDS_REPLAN 分支有 `await self._save_checkpoint`。需要加上。修正——`_handle_no_ready` 需要是 async，或者 run() 在调用前统一 save。考虑到原始代码中每个分支都有 save_checkpoint，最简单的是在 `_handle_no_ready` 返回前统一 save。但那样需要在 run() 中判断返回类型。

更简单的做法：`_handle_no_ready` 保持 sync，但不包含 save_checkpoint。让 run() 在收到非 None 结果后统一 save。但这改变了 save 的顺序（原始是先 save 再构造 result）。

最安全的做法：`_handle_no_ready` 在 BLOCKED / NEEDS_REPLAN 分支内部调用 `await self._save_checkpoint(...)`，因此它是 async。

修正为 async：
```python
async def _handle_no_ready(
    self, task_state: TaskState, exec_state: ExecutionState,
    graph: PlanGraph, task_id: TaskId,
) -> OrchestratorResult | None:
    ...
    # BLOCKED 分支:
    await self._save_checkpoint(task_state, exec_state)
    return OrchestratorResult(...)
    # NEEDS_REPLAN 分支:
    await self._save_checkpoint(task_state, exec_state)
    return OrchestratorResult(...)
```

- [ ] **Step 4: 提取 `_filter_approval`**（sync）

从 `run()` L215-228 提取：

```python
def _filter_approval(
    self,
    exec_state: ExecutionState,
    graph: PlanGraph,
    ready_ids: list[NodeId],
) -> list[DAGNode]:
    """审批检查 + 构建就绪节点列表。需要审批的节点排除并设标志。"""
    ready_nodes: list[DAGNode] = []
    for nid in ready_ids:
        node = graph.nodes[nid]
        if self._needs_approval(node):
            exec_state.waiting_for_approval = True
            logger.warning(
                "Orchestrator: node=%s 需要审批，跳过执行等待批准", node.node_id,
            )
            continue
        ready_nodes.append(node)
    return ready_nodes
```

`run()` 中替换为：
```python
ready_nodes = self._filter_approval(exec_state, graph, ready_ids)
```

- [ ] **Step 5: 提取 `_evaluate_and_dispatch`**（async）

从 `run()` L237-271 提取：

```python
async def _evaluate_and_dispatch(
    self,
    task_state: TaskState,
    exec_state: ExecutionState,
    results: list[NodeResult],
    graph: PlanGraph,
    mailbox: TaskMailbox,
) -> OrchestratorResult | None:
    """评估每个结果 → apply_evaluation → 根据 action 返回结果或 None。"""
    for result in results:
        node = graph.nodes[result.node_id]
        evaluation = await self._evaluate_node(result, node, task_state)
        action = await self._apply_evaluation(
            task_state, exec_state, evaluation, mailbox, graph,
        )
        if action == EvaluatorAction.REPLAN:
            failed_info = graph.collect_failure_info(exec_state.node_states)
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.NEEDS_REPLAN,
                error_context=failed_info,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        elif action == EvaluatorAction.ASK_APPROVAL:
            task_state.status = TaskStatus.WAITING_APPROVAL
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.BLOCKED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        elif action == EvaluatorAction.ABORT:
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.FAILED,
                error_context={"reason": evaluation.reason},
            )
    return None
```

`run()` 中替换为：
```python
if result := await self._evaluate_and_dispatch(
    task_state, exec_state, results, graph, mailbox,
):
    return result
```

- [ ] **Step 6: 运行测试**

```bash
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v
```

预期: 26 passed

- [ ] **Step 7: 提交**

```bash
git add Module/task_system/core/orchestrator.py
git commit -m "refactor: 拆分 Orchestrator.run() — 提取 5 个私有方法"
```
