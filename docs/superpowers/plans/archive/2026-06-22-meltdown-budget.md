# check_meltdown 补充预算检查实施计划

> **For agentic workers:** TDD — test first, then implement.

**Goal:** `check_meltdown()` 新增时间和 token 预算检查，防止无限 replan 耗尽资源

**Architecture:** 2 文件改动——replan.py 改 `check_meltdown()`，sub_agent.py 改调用方传参

**Tech Stack:** Python 3.13

**Design spec:** `docs/superpowers/specs/2026-06-22-meltdown-budget-design.md`

## Global Constraints

- `check_meltdown` 保持纯函数（无副作用），`token_ledger=None` 时跳过 token 检查
- `_elapsed_seconds` 解析失败时返回 0.0（优雅降级，不阻断熔断）
- 所有现有熔断测试保持通过
- `TaskState.created_at` 为 `IsoTimestamp`（Annotated str），格式 `2026-06-22T02:26:18Z`

## File Map

| 操作 | 文件 |
|------|------|
| Modify | `Module/task_system/core/sub_agent/replan.py` |
| Modify | `Module/task_system/core/sub_agent/sub_agent.py` |

---

### Task 1: 新增 3 个熔断测试（TDD）

**Files:**
- Modify: `Module/task_system/tests/unit/test_sub_agent.py`

- [ ] **Step 1: 找到现有熔断测试位置，插入新测试**

```python
# 在 test_meltdown 相关测试附近插入：

def test_meltdown_time_budget():
    """任务超过 max_task_duration_s 时触发时间熔断。"""
    from datetime import datetime, timezone, timedelta

    # 创建 600s 前的 created_at
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=601)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reason = check_meltdown(
        plan_version=1,
        max_failure_count=5,
        max_task_duration_s=600.0,
        created_at=old_time,
        token_ledger=None,
        max_total_tokens=100_000,
    )
    assert reason is not None
    assert "600" in reason
    assert "601" in reason


def test_meltdown_token_budget():
    """Token 消耗超过 max_total_tokens 时触发 token 熔断。"""
    from unittest.mock import MagicMock

    mock_ledger = MagicMock()
    mock_ledger.total_tokens = 150_000

    reason = check_meltdown(
        plan_version=1,
        max_failure_count=5,
        max_task_duration_s=600.0,
        created_at="2026-06-22T00:00:00Z",  # 最近时间，不触发时间熔断
        token_ledger=mock_ledger,
        max_total_tokens=100_000,
    )
    assert reason is not None
    assert "150000" in reason
    assert "100000" in reason


def test_meltdown_no_token_ledger_skips_token_check():
    """token_ledger=None 时跳过 token 检查，只检查版本和时间。"""
    reason = check_meltdown(
        plan_version=1,
        max_failure_count=5,
        max_task_duration_s=600.0,
        created_at="2026-06-22T00:00:00Z",  # 最近时间
        token_ledger=None,
        max_total_tokens=100_000,
    )
    assert reason is None  # 一切正常，不熔断
```

- [ ] **Step 2: 运行新测试——预期 FAIL（check_meltdown 签名不匹配）**

```bash
conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v -k "meltdown_time or meltdown_token or meltdown_no_token"
```

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/unit/test_sub_agent.py
git commit -m "test: 新增熔断预算检查 3 个测试用例（当前 FAIL——等待实现）"
```

---

### Task 2: `replan.py` — 实现 check_meltdown 新参数

**Files:**
- Modify: `Module/task_system/core/sub_agent/replan.py`

- [ ] **Step 1: 替换 `check_meltdown()` 函数**

用 spec 中的完整代码替换。关键变更：+4 参数，+2 检查维度，+ `_elapsed_seconds()` helper。

- [ ] **Step 2: 新增 `_elapsed_seconds()` 辅助函数**

```python
def _elapsed_seconds(created_at: str) -> float:
    """从 IsoTimestamp 计算已过去秒数。"""
    from datetime import datetime, timezone
    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).total_seconds()
    except (ValueError, TypeError):
        return 0.0
```

- [ ] **Step 3: 更新 `handle_replan()` 内的 `check_meltdown()` 调用**

将第 94-96 行的调用改为传入新参数：
```python
meltdown_reason = check_meltdown(
    task_state.plan.plan_version,
    constraints.max_failure_count,
    constraints.max_task_duration_s,
    task_state.created_at,
    token_ledger,
    constraints.max_total_tokens,
)
```

- [ ] **Step 4: `handle_replan()` 签名加 `token_ledger` 参数**

```python
async def handle_replan(
    result: OrchestratorResult,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    replanner: Replanner,
    plan_validator: PlanValidator,
    orchestrator: Orchestrator,
    checkpoint_mgr: CheckpointManager,
    constraints: ConstraintConfig,
    push_mailbox_fn: Callable[..., Awaitable[None]],
    token_ledger=None,     # 新增
) -> tuple[bool, ExecutionState | None]:
```

- [ ] **Step 5: 运行测试——预期 3 个新测试 PASS**

```bash
conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v -k "meltdown"
```

- [ ] **Step 6: Commit**

```bash
git add Module/task_system/core/sub_agent/replan.py
git commit -m "feat: check_meltdown 新增时间和 token 预算检查"
```

---

### Task 3: `sub_agent.py` — 调用方传入新参数

**Files:**
- Modify: `Module/task_system/core/sub_agent/sub_agent.py`

- [ ] **Step 1: 更新 `_check_meltdown()` 调用**

```python
def _check_meltdown(self, task_state: TaskState) -> str | None:
    assert task_state.plan is not None, "熔断检查前 plan 必须已赋值"
    return replan.check_meltdown(
        task_state.plan.plan_version,
        self._infra.constraints.max_failure_count,
        self._infra.constraints.max_task_duration_s,
        task_state.created_at,
        self._token_ledger,
        self._infra.constraints.max_total_tokens,
    )
```

- [ ] **Step 2: 更新 `_handle_replan()` 传递给 `handle_replan()`**

```python
return await replan.handle_replan(
    result, task_state, graph, mailbox, context,
    self._replanner, self._plan_validator, self._orchestrator,
    self._checkpoint_mgr, self._infra.constraints, self._push_mailbox,
    self._token_ledger,
)
```

- [ ] **Step 3: 运行完整测试套件**

```bash
conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v
```

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/core/sub_agent/sub_agent.py
git commit -m "refactor: 更新 check_meltdown 调用方传入时间和 token 预算参数"
```
