# check_meltdown 补充 token 和时间预算检查

> 日期: 2026-06-22
> 状态: 设计

## 问题

`check_meltdown()` 只检查 `plan_version > max_failure_count`。
`ConstraintConfig` 已定义 `max_task_duration_s`（600s）和 `max_total_tokens`（100K），
但从未在熔断逻辑中使用。连续 Replan 每次都烧一次 LLM 调用，可能耗尽预算而不自知。

## 方案

`check_meltdown()` 新增三个检查维度：plan_version（已有）、时间预算、token 预算。
新增 4 个参数，调用方负责传入。

## 变动范围

2 文件：`replan.py` + `sub_agent.py`

---

## 1. `replan.py:check_meltdown()` — 签名 + 实现

### 改动前

```python
def check_meltdown(plan_version: int, max_failure_count: int) -> str | None:
    if plan_version > max_failure_count:
        return (
            f"已 Replan {plan_version - 1} 次（上限 {max_failure_count}），"
            "已触发熔断"
        )
    # TODO: 检查剩余 token
    # TODO: 检查剩余时间（max_task_duration_s）
    return None
```

### 改动后

```python
def check_meltdown(
    plan_version: int,
    max_failure_count: int,
    max_task_duration_s: float,
    created_at: str,
    token_ledger,           # TokenLedger | None
    max_total_tokens: int,
) -> str | None:
    """检查熔断——plan_version / 时间预算 / token 预算。

    Returns:
        None → 可以继续
        str  → 熔断原因（不能继续）
    """
    # 1. 计划版本（已有）
    if plan_version > max_failure_count:
        return (
            f"已 Replan {plan_version - 1} 次（上限 {max_failure_count}），"
            "已触发熔断"
        )

    # 2. 时间预算
    elapsed_s = _elapsed_seconds(created_at)
    if elapsed_s > max_task_duration_s:
        return (
            f"任务耗时已超过 {elapsed_s:.0f} 秒"
            f"（上限 {max_task_duration_s:.0f} 秒），已触发熔断"
        )

    # 3. Token 预算
    if token_ledger is not None:
        used = token_ledger.total_tokens
        if used > max_total_tokens:
            return (
                f"任务已消耗 {used} tokens"
                f"（上限 {max_total_tokens}），已触发熔断"
            )

    return None


def _elapsed_seconds(created_at: str) -> float:
    """从 IsoTimestamp 计算已过去秒数。"""
    from datetime import datetime, timezone
    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).total_seconds()
    except (ValueError, TypeError):
        return 0.0
```

---

## 2. `sub_agent.py` — 调用方传参

### `SubAgent._check_meltdown()`（第 364 行）

```python
def _check_meltdown(self, task_state: TaskState) -> str | None:
    assert task_state.plan is not None, "熔断检查前 plan 必须已赋值"
    return replan.check_meltdown(
        task_state.plan.plan_version,
        self._infra.constraints.max_failure_count,
        # ── 新增 ──
        self._infra.constraints.max_task_duration_s,
        task_state.created_at,
        self._token_ledger,
        self._infra.constraints.max_total_tokens,
    )
```

### `SubAgent._handle_replan()` → `handle_replan()`（第 358 行）

`handle_replan()` 内部直接调用 `check_meltdown()`，需要传入额外参数。
在 `_handle_replan()` 中添加 `self._token_ledger` 参数传递，
或让 `handle_replan()` 接受 `token_ledger` 参数。

```python
return await replan.handle_replan(
    result, task_state, graph, mailbox, context,
    self._replanner, self._plan_validator, self._orchestrator,
    self._checkpoint_mgr, self._infra.constraints, self._push_mailbox,
    self._token_ledger,   # 新增
)
```

### `handle_replan()` 签名更新

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
    token_ledger=None,     # 新增: TokenLedger | None
) -> tuple[bool, ExecutionState | None]:
```

内部的 `check_meltdown()` 调用更新为：

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

---

## 3. 测试

在 `test_sub_agent.py` 新增：

### `test_meltdown_time_budget()`
任务运行超过 `max_task_duration_s`，验证 `check_meltdown()` 返回时间相关熔断原因。

### `test_meltdown_token_budget()`
Token 消耗超过 `max_total_tokens`，验证返回 token 相关熔断原因。

### `test_meltdown_no_token_ledger()`
`token_ledger=None` 时跳过 token 检查，仅检查版本和时间。

---

## 验证

```bash
conda activate Elysia
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v -k "meltdown"
python -m pytest Module/task_system/tests/unit/ -v
```
