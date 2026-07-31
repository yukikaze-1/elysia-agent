# Orchestrator.run() 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/core/orchestrator.py` 的 `run()` 方法 (~280行) 拆为私有方法

## 目标

将 `run()` 主循环内的逻辑块提取为 `Orchestrator` 私有方法，`run()` 缩减到 ~45 行。

## 目标结构

`run()` 变为：

```python
async def run(self, task_state, graph, mailbox, resume_from=None):
    task_id = task_state.task_id
    exec_state = self._init_exec(graph, resume_from)
    self._exec_state = exec_state            # ★ 必须保留——pause() 通过它访问
    try:
        while not task_state.is_terminal():
            exec_state.updated_at = now_iso()
            if result := await self._check_interrupts(task_state, exec_state):
                return result
            ready_ids = graph.get_ready_nodes(exec_state.node_states)
            if not ready_ids:
                if graph.is_terminal(exec_state.node_states):
                    break                     # ★ is_terminal 留在 run() 中
                if result := self._handle_no_ready(task_state, exec_state, graph):
                    return result
            ready_nodes = self._filter_approval(exec_state, graph, ready_ids)
            exec_state.current_phase = ExecutionPhase.EXECUTING
            results = await self._execute_nodes(
                ready_nodes, exec_state, task_id, task_state.tools,
                task_state.session_id,
            )
            exec_state.current_phase = ExecutionPhase.EVALUATING
            if result := await self._evaluate_and_dispatch(
                task_state, exec_state, results, graph, mailbox,
            ):
                return result
            exec_state.current_phase = ExecutionPhase.EXECUTING
            await self._save_checkpoint(task_state, exec_state)
        if task_state.status == TaskStatus.RUNNING:
            exec_state.current_phase = ExecutionPhase.FINALIZING
            await self._finalize(task_state, exec_state, mailbox)
        return OrchestratorResult(
            status=OrchestratorRunStatus.SUCCESS,
            checkpoint_state=self._build_checkpoint(task_state, exec_state),
        )
    except asyncio.CancelledError: ...
    except Exception: ...
```

### 提取的 4 个私有方法

```python
def _init_exec(self, graph: PlanGraph, resume_from: ExecutionState | None) -> ExecutionState
    # resume_from is not None → merge_exec_state
    # resume_from is None → init_exec_state
    # 复位 waiting_for_approval = False
    # RESUMING → RUNNING 状态转换

async def _check_interrupts(
    self, task_state: TaskState, exec_state: ExecutionState,
) -> OrchestratorResult | None
    # pause_requested → save_checkpoint + PAUSED result  ★ 含 await
    # cancel_requested → save_checkpoint + CANCELED result ★ 含 await
    # 否则返回 None

def _handle_no_ready(
    self, task_state: TaskState, exec_state: ExecutionState, graph: PlanGraph,
) -> OrchestratorResult | None
    # is_terminal 已由 run() 处理——此方法不处理
    # has_blocked_nodes → BLOCKED result
    # has_failed_required → NEEDS_REPLAN result
    # 死锁 → FAILED result
    # (run() 保证调用前 ready_ids 为空且非 terminal)

def _filter_approval(
    self, exec_state: ExecutionState, graph: PlanGraph, ready_ids: list[NodeId],
) -> list[DAGNode]
    # 审批检查 + 构建就绪节点列表
    # needs_approval → 跳过，设 waiting_for_approval

async def _evaluate_and_dispatch(
    self, task_state: TaskState, exec_state: ExecutionState,
    results: list[NodeResult], graph: PlanGraph, mailbox: TaskMailbox,
) -> OrchestratorResult | None
    # 每个 result: evaluate_node → apply_evaluation
    # REPLAN → save_checkpoint + NEEDS_REPLAN result
    # ASK_APPROVAL → save_checkpoint + BLOCKED result
    # ABORT → save_checkpoint + FAILED result
    # 否则返回 None
```

### 不受影响

- `SubAgent`——唯一消费者，`run()` 签名和返回值不变
- 所有测试——`run()` 公开 API 不变
- `execution.py` / `finalize.py`——不动

## 验证

```bash
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v
```
