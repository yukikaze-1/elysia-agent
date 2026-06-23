# TaskSystem submit_approval() v1 设计

> 日期: 2026-06-17
> 状态: 设计完成

---

## 1. 背景

TaskSystem 有两条独立的审批路径：

| | v1 工作路径 (ToolManager) | v2 规划路径 (TaskSystem) |
|---|---|---|
| 触发 | `SafetyProfile.requires_human_approval` 配置 | `DAGNode.risk_level="mutation"` Planner 标注 |
| 等待 | `asyncio.Event` + 60s 超时 | `asyncio.Future` 无超时 |
| 路由 | `request_id` 字符串 | `(task_id, agent_id)` |
| Orchestrator | 不感知——视为普通工具调用 | 返回 BLOCKED + save checkpoint |
| 状态 | RUNNING | WAITING_APPROVAL |

v1 的 Orchestrator **永远不会**进入 BLOCKED 路径。所有审批完全在 ToolManager 内部的 `SafetyPipeline` 消化。

## 2. 方案：薄桥接

`TaskSystem.submit_approval()` 把 `agent_id` 当作 `request_id`，直接委托给 `ToolManager.submit_approval()`。

```
前端审批卡片 → CognitiveLoop → TaskSystem.submit_approval(task_id, agent_id, approved)
                                      │
                                      │ agent_id 即 request_id
                                      ▼
                              ToolManager.submit_approval(request_id, approved)
                                      │
                                      ▼
                              SafetyPipeline.submit_approval(request_id, approved)
                                      │
                                      │ event.set()
                                      ▼
                              工具调用继续执行 / 返回 PERMISSION_DENIED
```

**签名不变**：保持 `(task_id, agent_id, approved, feedback)` 与架构文档一致。v2 只需改内部实现。

## 3. 运行时状态

审批期间整个调用栈挂在 `asyncio.Event.wait()` 上，不占 CPU：

```
ToolManager.execute()                         ← await event.wait()
  ↑ Executor._call_tool()
    ↑ Orchestrator.run() 循环
      ↑ SubAgent._execute()
        ↑ SubAgent.run()
          ↑ _run_and_cleanup
```

`task_state.status = RUNNING`。TaskSystem 完全不感知审批。

## 4. 实现

```python
async def submit_approval(
    self,
    task_id: TaskId,
    agent_id: AgentId,
    approved: bool,
    feedback: str | None = None,
) -> bool:
    """审批结果回传。v1 桥接到 ToolManager 的 SafetyPipeline。

    将 agent_id 作为 request_id 委托给 infra.tool_registry.submit_approval()。
    task_id / feedback 为 v2 预留（Orchestrator BLOCKED + Planner feedback 注入）。

    Returns:
        True:  审批请求已找到并处理
        False: 审批请求不存在（可能已超时）
    """
    tool_mgr = self._infra.tool_registry
    if not hasattr(tool_mgr, "submit_approval"):
        return False
    return tool_mgr.submit_approval(  # type: ignore[attr-defined]
        request_id=str(agent_id),
        approved=approved,
    )
```

## 5. 改动范围

| 文件 | 改动 |
|------|------|
| `task_system.py` | 替换 `submit_approval()` stub（~15 行） |
| `tests/unit/test_task_system.py` | 3 个单元测试 |
| `docs/PROGRESS.md` | 标记 submit_approval 已实现 |
| `docs/api.md` | 更新状态描述 |

## 6. v2 扩展点

当 Orchestrator BLOCKED 路径启用后，`submit_approval()` 需要：
- 按 `task_id` 查找等待审批的 Worker Agent
- 通过 `agent_id` 路由到具体 Worker
- 设置 Worker 的 `approval_future`（asyncio.Future，无超时）
- `approved=False` 时注入 `feedback` 到 Planner replan prompt
