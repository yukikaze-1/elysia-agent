# TaskSystem Token 追踪 — 设计文档

日期：2026-06-16
状态：已确认

---

## 1. 目标

为 TaskSystem 的 3 个 LLM 调用组件（Planner、Executor、Evaluator）接入完整的 token 审计日志：
- 记录每次 LLM API 调用的 `prompt_tokens` + `completion_tokens` + `model`
- 按组件类型（planner / executor / evaluator）和节点（node_id）分类
- Write-through 写入 TaskState，支持暂停/恢复/异常终止场景
- 暴露 `get_token_usage(task_id)` 和 `get_token_stats(period)` 查询 API
- DelegationPreparer 不纳入 TaskSystem 追踪范围，自行返回 token 数据

注：已有的 `core/token_tracker.py`（2026-06-06 设计）负责 CognitiveLoop 侧 LLM 调用的 token 追踪，两个方案互补。

---

## 2. 新增组件：TokenLedger

### 2.1 文件

`Module/task_system/token_ledger.py` — 新建

### 2.2 类设计

```python
class TokenLedger:
    """单次任务 token 账本，write-through 到 TaskState"""

    def __init__(self, task_state: TaskState) -> None:
        self._ts = task_state

    def record(
        self,
        component: TokenComponentType,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        node_id: NodeId | None = None,
    ) -> None:
        """记录一次 LLM 调用，立即写入 TaskState"""
        entry = TokenComponent(
            component=component,
            node_id=node_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            model=model,
        )
        self._ts.token_entries.append(entry)
        key = component.value
        self._ts.token_usage[key] = self._ts.token_usage.get(key, 0) + entry.total_tokens

    def to_token_usage(self) -> TokenUsage:
        """导出 TokenUsage（供查询 API）"""
        return TokenUsage(
            task_id=self._ts.task_id,
            components=list(self._ts.token_entries),
        )

    @property
    def total_tokens(self) -> int:
        return sum(e.total_tokens for e in self._ts.token_entries)
```

### 2.3 关键行为

- **write-through**：`record()` 立即写 `TaskState.token_entries` 和 `TaskState.token_usage`，不攒到最后
- **暂停恢复**：暂停时 token 数据已在 TaskState 中，恢复后 ledger 从已有 entries 继续追加
- **异常终止**：stop() cancel 时 entries 已写入的不会丢失
- **v1 范围**：仅内存写入，不持久化到文件/DB（v2 checkpoint 会顺带持久化）

---

## 3. Schema 变更

### 3.1 TaskState

新增字段：

```python
token_entries: list[TokenComponent] = Field(default_factory=list)
# 完整审计日志，每次 LLM 调用追加一条
```

已有 `token_usage: dict[str, int]` 保持不变，作为实时汇总。

### 3.2 TaskSummary

```python
token_total: int = 0  # 新增：任务总 token 消耗
```

已有 `token_usage: dict[str, int]` 保持不变。

---

## 4. 组件 per-task 化

当前 TaskSystem.start() 创建 6 个共享组件。TokenLedger 是 per-task 的，需要把依赖它的组件改为 per-task 创建。

### 4.1 共享组件（保留在 TaskSystem.start()）

| 组件 | 说明 |
|------|------|
| `infra` (AgentInfra) | AsyncOpenAI + MilvusClient + HTTP + ToolRegistry，连接池创建成本高 |
| `hitl_notifier` (HITLNotifier) | 外部注入的审批推送实现 |
| `_semaphore` (BoundedSemaphore) | 并发控制 |
| `_tasks / _mailboxes / _task_futures` | 注册表 dict |

### 4.2 Per-task 组件（SubAgent.__init__ 创建）

| 组件 | 构造参数 | 原有共享范围 |
|------|---------|-------------|
| `TokenLedger` | `task_state` | 新增 |
| `Planner` | `infra, token_ledger` | 原共享 → per-task |
| `Executor` | `infra, artifact_store, hitl_notifier, token_ledger` | 原共享 → per-task |
| `Evaluator` | `infra, artifact_store, token_ledger` | 原共享 → per-task |
| `ArtifactStore` | 无 | 原共享 → per-task |
| `CheckpointManager` | 无 | 原共享 → per-task |
| `PlanValidator` | 无 | 原共享 → per-task |
| `Orchestrator` | `infra, hitl_notifier, executor, evaluator, checkpoint_mgr, artifact_store` | 已是 per-task |

### 4.3 优点

- TokenLedger 通过 `__init__` 注入，方法签名不变
- 一个任务的组件崩溃不影响其他任务
- TaskSystem.start() 大幅简化（仅剩 semaphore 初始化）

---

## 5. 各组件改动

### 5.1 Planner

```python
class Planner:
    def __init__(self, token_ledger: TokenLedger | None = None) -> None:
        self._token_ledger = token_ledger

    # plan() / replan() 中 LLM 调用后:
    if self._token_ledger and (usage := response.usage):
        try:
            self._token_ledger.record(
                component=TokenComponentType.PLANNER,
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                model=model_name,
            )
        except Exception:
            logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
```

`plan()` 和 `replan()` 两处 LLM 调用都加同样的 record 逻辑。

### 5.2 Executor

```python
class Executor:
    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
        hitl_notifier: HITLNotifier,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._token_ledger = token_ledger

    # _match_tool() 中 LLM 调用后:
    if self._token_ledger and (usage := response.usage):
        try:
            self._token_ledger.record(
                component=TokenComponentType.EXECUTOR,
                node_id=node.node_id,
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                model=self._infra.llm_model,
            )
        except Exception:
            logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
```

### 5.3 Evaluator

```python
class Evaluator:
    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._token_ledger = token_ledger

    # evaluate() 中 LLM 调用后:
    if self._token_ledger and (usage := response.usage):
        try:
            self._token_ledger.record(
                component=TokenComponentType.EVALUATOR,
                node_id=node.node_id,
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                model=self._infra.llm_model,
            )
        except Exception:
            logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
```

### 5.4 Orchestrator

不需要改动。Executor/Evaluator 已在 `__init__` 持有 ledger，其 `execute()` / `evaluate()` 方法签名不变，内部自行调用 `record()`。

### 5.5 SubAgent

```python
class SubAgent:
    def __init__(
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
        # 以下不再从外部注入，改为内部创建：
        # planner, plan_validator, executor, evaluator,
        # checkpoint_manager, artifact_store
    ) -> None:
        # per-task 组件在 run() 中创建（需要 task_state）
        ...

    async def run(self, context, mailbox):
        task_state = ...
        ledger = TokenLedger(task_state)

        artifact_store = ArtifactStore()
        checkpoint_mgr = CheckpointManager()
        plan_validator = PlanValidator()

        self._executor = Executor(
            infra=self._infra, artifact_store=artifact_store,
            hitl_notifier=self._hitl, token_ledger=ledger,
        )
        self._evaluator = Evaluator(
            infra=self._infra, artifact_store=artifact_store,
            token_ledger=ledger,
        )
        self._planner = Planner(token_ledger=ledger)

        self._orchestrator = Orchestrator(
            infra=self._infra, hitl_notifier=self._hitl,
            executor=self._executor, evaluator=self._evaluator,
            checkpoint_manager=checkpoint_mgr,
            artifact_store=artifact_store,
        )
        ...
```

### 5.6 DelegationPreparer（独立追踪）

DelegationPreparer 的 `prepare()` 返回结构新增 token 数据，由 CognitiveLoop 自行处理，不纳入 TaskSystem TokenLedger：

```python
@dataclass
class PreparationResult:
    context: DelegationContext
    token_usage: dict  # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
```

---

## 6. 查询 API 实现

### 6.1 get_token_usage

```python
def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
    """查询单任务的 token 消耗明细（含完整审计日志）"""
    sub_agent = self._tasks.get(task_id)
    ts = sub_agent._task_state if sub_agent else None
    if ts is None:
        return None
    return TokenUsage(
        task_id=task_id,
        components=list(ts.token_entries),
    )
```

### 6.2 get_token_stats

```python
def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
    """聚合 token 统计（按时间段 + 组件 + 模型）"""
    now = datetime.now(timezone.utc)
    # 按 period 计算起始时间
    cutoff = _period_cutoff(now, period)

    total = 0
    by_component: dict[str, int] = {}
    by_model: dict[str, int] = {}

    for sub_agent in self._tasks.values():
        ts = sub_agent._task_state
        if ts is None:
            continue
        # 按时间段过滤
        if ts.created_at and _parse_iso(ts.created_at) < cutoff:
            continue
        for entry in ts.token_entries:
            total += entry.total_tokens
            by_component[entry.component.value] = \
                by_component.get(entry.component.value, 0) + entry.total_tokens
            if entry.model:
                by_model[entry.model] = \
                    by_model.get(entry.model, 0) + entry.total_tokens

    return TokenStats(
        period=period,
        total_tokens=total,
        by_component=by_component,
        by_model=by_model,
    )
```

---

## 7. 错误处理

| 场景 | 处理 |
|------|------|
| `response.usage` 为 None（provider 不返回） | `record()` 调用前判空，跳过 |
| `ledger.record()` 自身异常 | try/except 包住，log warning，不影响任务执行 |
| LLM 调用抛异常（网络/超时） | 无 response.usage，自然不记录 |
| stop() cancel 任务 | entries 已 write-through 到 TaskState，不丢失 |
| 暂停恢复 | TokenLedger 从 `task_state.token_entries` 恢复，继续追加 |

---

## 8. 测试

### 8.1 新增文件

`Module/task_system/tests/unit/test_token_ledger.py`

| 测试 | 验证点 |
|------|--------|
| `record` 追加条目 | `token_entries` 长度 + TokenComponent 字段正确 |
| `record` 更新汇总 | `token_usage["executor"]` 累加 |
| `record` 多次调用 | 3 次 record → 3 条 entries + 汇总一致 |
| `to_token_usage` | 返回 TokenUsage 结构完整 |
| 暂停恢复场景 | 用已有 entries 初始化 → 追加 → entries 连续 |
| usage=None 安全 | `record(0, 0, ...)` 不崩溃 |
| `total_tokens` 属性 | 多条目求和正确 |

### 8.2 现有测试修改

- Executor / Planner / Evaluator 的 `__init__` 签名变更 → 对应测试更新构造调用
- 各组件的 mock 测试中，注入 `TokenLedger` mock，验证 `record()` 被调用
- TaskSystem.start() 不再创建 Planner/Executor/Evaluator → 相关测试更新

---

## 9. 涉及文件汇总

| 文件 | 操作 |
|------|------|
| `Module/task_system/token_ledger.py` | **新建** |
| `Module/task_system/schema.py` | 修改：TaskState 加 `token_entries`，TaskSummary 加 `token_total` |
| `Module/task_system/task_system.py` | 修改：start() 简化，submit() 不再注入 per-task 组件，实现 get_token_usage/get_token_stats |
| `Module/task_system/core/sub_agent.py` | 修改：__init__ 内部创建 per-task 组件 + TokenLedger |
| `Module/task_system/core/planner.py` | 修改：__init__ 加 `token_ledger` 参数，LLM 调用后 record |
| `Module/task_system/core/executor.py` | 修改：__init__ 加 `token_ledger` 参数，_match_tool() 后 record |
| `Module/task_system/core/evaluator.py` | 修改：__init__ 加 `token_ledger` 参数，evaluate() 后 record |
| `Module/task_system/delegation_preparer.py` | 修改：prepare() 返回 token_usage |
| `Module/task_system/tests/unit/test_token_ledger.py` | **新建** |
| `Module/task_system/tests/unit/test_*.py` | 修改：适配 __init__ 签名变更 + 验证 record 调用 |
| `Module/task_system/docs/PROGRESS.md` | 修改：更新 v1 未完成项 |

---

## 10. 与 CognitiveLoop TokenTracker 的关系

| 维度 | CognitiveLoop TokenTracker | TaskSystem TokenLedger |
|------|--------------------------|------------------------|
| 文件 | `core/token_tracker.py` | `Module/task_system/token_ledger.py` |
| 追踪范围 | 聊天轮次 LLM 调用 | 任务编排 LLM 调用 |
| 持久化 | MongoDB + JSONL | v1 仅内存（v2 checkpoint 顺带持久化） |
| 广播 | WebSocket → 前端 UI | TaskState → Debug WebUI / 查询 API |
| 消费者 | 用户聊天界面 | Debug 仪表盘 + LLM 工具 |

两者独立运行，不互相依赖。
