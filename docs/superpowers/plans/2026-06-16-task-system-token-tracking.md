# TaskSystem Token 追踪 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 TaskSystem 的 Planner/Executor/Evaluator 接入完整 token 审计日志，支持 per-call 粒度追踪、write-through 到 TaskState、暂停恢复安全。

**Architecture:** 新建 `TokenLedger`（per-task 账本，write-through 到 TaskState），将 Planner/Executor/Evaluator/ArtifactStore/CheckpointManager/PlanValidator 从 TaskSystem.start() 共享实例改为 SubAgent.__init__ 内部创建（per-task），TokenLedger 通过 `__init__` 注入各组件，LLM 调用后立即 `record()`。

**Tech Stack:** Python 3.13, asyncio, pydantic, AsyncOpenAI

---

### Task 1: 创建 TokenLedger 类 + 单元测试

**Files:**
- Create: `Module/task_system/token_ledger.py`
- Create: `Module/task_system/tests/unit/test_token_ledger.py`

- [ ] **Step 1: 写 TokenLedger 的单元测试（TDD）**

```python
"""TokenLedger 单元测试——write-through 到 TaskState 的 token 账本。"""
import pytest
from Module.task_system.schema import (
    NodeId,
    SessionId,
    TaskId,
    TaskState,
    TaskStatus,
    TokenComponentType,
    TokenComponent,
    TokenUsage,
)
from Module.task_system.token_ledger import TokenLedger


def _make_task_state(task_id: str = "test-001") -> TaskState:
    return TaskState(
        task_id=TaskId(task_id),
        status=TaskStatus.PENDING,
        session_id=SessionId("sess-1"),
    )


def test_record_appends_entry():
    """record() 后 token_entries 新增一条 TokenComponent。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(
        component=TokenComponentType.EXECUTOR,
        prompt_tokens=100,
        completion_tokens=50,
        model="deepseek-v4",
        node_id=NodeId("search"),
    )
    assert len(ts.token_entries) == 1
    e = ts.token_entries[0]
    assert e.component == TokenComponentType.EXECUTOR
    assert e.prompt_tokens == 100
    assert e.completion_tokens == 50
    assert e.total_tokens == 150
    assert e.model == "deepseek-v4"
    assert e.node_id == NodeId("search")


def test_record_updates_usage_summary():
    """record() 同步更新 token_usage 汇总。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 200, 100, "deepseek-v4")
    ledger.record(TokenComponentType.PLANNER, 50, 30, "deepseek-v4")
    ledger.record(TokenComponentType.EXECUTOR, 80, 20, "openai/gpt-4o")
    assert ts.token_usage["planner"] == 300 + 80  # 200+100 + 50+30
    assert ts.token_usage["executor"] == 100  # 80+20


def test_record_multiple_calls():
    """多次 record → entries 长度正确 + 汇总一致。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    for i in range(5):
        ledger.record(TokenComponentType.EXECUTOR, 10, 5, "m", NodeId(f"n{i}"))
    assert len(ts.token_entries) == 5
    assert ts.token_usage["executor"] == 75  # 5 * 15


def test_to_token_usage():
    """to_token_usage() 返回完整的 TokenUsage 结构。"""
    ts = _make_task_state("task-42")
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 10, 2, "m1")
    usage = ledger.to_token_usage()
    assert isinstance(usage, TokenUsage)
    assert usage.task_id == TaskId("task-42")
    assert len(usage.components) == 1
    assert usage.components[0].total_tokens == 12


def test_resume_from_existing_entries():
    """已有 token_entries 时，TokenLedger 继续追加不破坏历史。"""
    ts = _make_task_state()
    ts.token_entries.append(TokenComponent(
        component=TokenComponentType.PLANNER,
        prompt_tokens=10, completion_tokens=5, total_tokens=15, model="m",
    ))
    ts.token_usage["planner"] = 15
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.EXECUTOR, 20, 10, "m")
    assert len(ts.token_entries) == 2
    assert ts.token_entries[0].total_tokens == 15  # 历史未变
    assert ts.token_entries[1].total_tokens == 30  # 新追加
    assert ts.token_usage["planner"] == 15
    assert ts.token_usage["executor"] == 30


def test_zero_tokens_safe():
    """prompt_tokens=0, completion_tokens=0 不崩溃。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.EVALUATOR, 0, 0, "m")
    e = ts.token_entries[0]
    assert e.total_tokens == 0


def test_total_tokens_property():
    """total_tokens 属性正确求和。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    assert ledger.total_tokens == 0
    ledger.record(TokenComponentType.PLANNER, 10, 5, "m")
    ledger.record(TokenComponentType.EXECUTOR, 20, 10, "m")
    assert ledger.total_tokens == 45


def test_node_id_none():
    """node_id=None 时正常存储（Planner 调用无节点上下文）。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 100, 50, "m", node_id=None)
    assert ts.token_entries[0].node_id is None
```

- [ ] **Step 2: 跑测试确认全部 FAIL**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_token_ledger.py -v`
Expected: 8 tests FAIL with "ModuleNotFoundError: No module named 'Module.task_system.token_ledger'"

- [ ] **Step 3: 实现 TokenLedger**

```python
"""TokenLedger — 单次任务 token 账本，write-through 到 TaskState。

每次 LLM 调用后立即 record()，直接写入 TaskState.token_entries + token_usage。
任务暂停/崩溃/取消时数据已持久在 TaskState 中，不依赖最终 flush。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from Module.task_system.schema import (
    NodeId,
    TokenComponent,
    TokenComponentType,
    TokenUsage,
)

if TYPE_CHECKING:
    from Module.task_system.schema import TaskState


class TokenLedger:
    """单次任务 token 账本，write-through 到 TaskState。

    用法:
        ledger = TokenLedger(task_state)
        # LLM 调用后:
        ledger.record(
            component=TokenComponentType.PLANNER,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            model="deepseek-v4",
        )
    """

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
        """记录一次 LLM 调用，立即写入 TaskState。

        Args:
            component:         消耗来源（planner / executor / evaluator）
            prompt_tokens:     输入 token 数
            completion_tokens: 输出 token 数
            model:             LLM 模型名
            node_id:           关联节点 ID（Executor/Evaluator 有，Planner 为 None）
        """
        total = prompt_tokens + completion_tokens
        entry = TokenComponent(
            component=component,
            node_id=node_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            model=model,
        )
        self._ts.token_entries.append(entry)
        key = component.value
        self._ts.token_usage[key] = self._ts.token_usage.get(key, 0) + total

    def to_token_usage(self) -> TokenUsage:
        """导出 TokenUsage（供查询 API）。"""
        return TokenUsage(
            task_id=self._ts.task_id,
            components=list(self._ts.token_entries),
        )

    @property
    def total_tokens(self) -> int:
        return sum(e.total_tokens for e in self._ts.token_entries)
```

- [ ] **Step 4: 跑测试确认全部 PASS**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_token_ledger.py -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/token_ledger.py Module/task_system/tests/unit/test_token_ledger.py
git commit -m "feat(token_tracking): TokenLedger — write-through token 账本 + 单元测试"
```

---

### Task 2: Schema 变更 — TaskState + TaskSummary

**Files:**
- Modify: `Module/task_system/schema.py:796-823` (TaskState), `schema.py:569-594` (TaskSummary)

- [ ] **Step 1: 在 TaskState 添加 token_entries 字段**

在 [schema.py:814](Module/task_system/schema.py#L814) `error_code` 字段之后插入：

```python
    # ── Token 追踪 ──
    token_entries: list[TokenComponent] = Field(default_factory=list)
    # 完整审计日志，每次 LLM 调用追加一条
    token_usage: dict[str, int] = Field(default_factory=dict)
    # 组件级汇总: {"planner": N, "executor": N, "evaluator": N}
```

注意：`token_usage` 已存在于 `TaskState.__init__` 的参数列表中（之前定义在别处但没有在 TaskState 类体中声明为 Field），需要确认并调整为带 default_factory 的 Field。检查 [schema.py:796-823](Module/task_system/schema.py#L796-L823) 确认当前字段列表，`token_usage` 当前未在 TaskState 中声明，需要新增。

- [ ] **Step 2: 在 TaskSummary 添加 token_total 字段**

在 [schema.py:585-586](Module/task_system/schema.py#L585-L586) `token_usage` 字段之后插入：

```python
    token_total: int = 0                    # 任务总 token 消耗（来自 TokenLedger.total_tokens）
```

- [ ] **Step 3: 跑现有 TaskSystem 测试确认无回归**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v -x`
Expected: all 252 tests PASS (token_entries 默认 factory=list 不影响现有逻辑)

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/schema.py
git commit -m "feat(token_tracking): TaskState 加 token_entries + token_usage，TaskSummary 加 token_total"
```

---

### Task 3: Planner — token_ledger 注入 + record

**Files:**
- Modify: `Module/task_system/core/planner.py:189-198` (`__init__`), `planner.py:202-250` (`plan()`), `planner.py:594-611` (`_call_llm`)

- [ ] **Step 1: 修改 Planner.__init__**

将 [planner.py:189-198](Module/task_system/core/planner.py#L189-L198) 替换为：

```python
    def __init__(self, token_ledger: TokenLedger | None = None) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 环境变量未设置，Planner 无法创建 DeepSeek client"
            )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        self._token_ledger = token_ledger
```

并在文件顶部 import 新区块添加 `TokenLedger` 的 TYPE_CHECKING import 和 `TokenComponentType` 的普通 import：

```python
from Module.task_system.schema import (
    ...
    TokenComponentType,
    ...
)
```

TYPE_CHECKING 块中添加：
```python
if TYPE_CHECKING:
    from Module.task_system.token_ledger import TokenLedger
```

- [ ] **Step 2: 在 _call_llm 中添加 token record**

修改 `_call_llm`（[planner.py:594-611](Module/task_system/core/planner.py#L594-L611)），在 `response.choices[0].message.content` 检查之前插入 record 逻辑：

```python
    async def _call_llm(self, system: str, user: str) -> str:
        """调用 LLM，推理模式 + JSON Output。模型名由环境变量 TASK_SYSTEM_MODEL 控制。"""
        model = os.getenv("TASK_SYSTEM_MODEL", "deepseek-v4-pro")
        response = await self._client.chat.completions.create(
            model=model,
            messages=[...],
            ...
        )
        # ── token 追踪 ──
        if self._token_ledger and (usage := response.usage):
            try:
                self._token_ledger.record(
                    component=TokenComponentType.PLANNER,
                    prompt_tokens=usage.prompt_tokens or 0,
                    completion_tokens=usage.completion_tokens or 0,
                    model=model,
                )
            except Exception:
                logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
        content = response.choices[0].message.content
        if not content:
            raise _PlanParseError("LLM 返回空响应")
        return content
```

注意：`_call_llm` 被 `plan()` 和 `replan()` 两者共用，加一次 record 覆盖两条路径。

- [ ] **Step 3: 更新 Planner 单元测试**

修改 `Module/task_system/tests/unit/test_planner.py`：
- 创建 `Planner()` 的地方改为 `Planner(token_ledger=MagicMock())` 或 `Planner(token_ledger=None)`
- 新增测试：验证 LLM 调用后 `token_ledger.record()` 被调用，参数正确

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_planner.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/core/planner.py Module/task_system/tests/unit/test_planner.py
git commit -m "feat(token_tracking): Planner 支持 TokenLedger 注入 + _call_llm 后 record"
```

---

### Task 4: Executor — token_ledger 注入 + record

**Files:**
- Modify: `Module/task_system/core/executor.py:71-86` (`__init__`), `executor.py:205-304` (`_match_tool`)

- [ ] **Step 1: 修改 Executor.__init__**

在 [executor.py:71-86](Module/task_system/core/executor.py#L71-L86) 添加 `token_ledger` 参数：

```python
    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
        hitl_notifier: HITLNotifier,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._infra = infra
        self._artifact_store = artifact_store
        self._hitl = hitl_notifier
        self._token_ledger = token_ledger
```

TYPE_CHECKING 块中添加：
```python
if TYPE_CHECKING:
    from Module.task_system.token_ledger import TokenLedger
```

- [ ] **Step 2: 在 _match_tool 中添加 token record**

`_match_tool` 中 LLM 调用在 [executor.py:270-279](Module/task_system/core/executor.py#L270-L279)。在 `response` 赋值之后，`msg = response.choices[0].message` 之前，插入 record：

```python
        response = await self._infra.llm.chat.completions.create(
            model=self._infra.llm_model,
            messages=[...],
            ...
        )

        # ── token 追踪 ──
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

        msg = response.choices[0].message
```

并在文件顶部 import 新增：
```python
from Module.task_system.schema import (
    ...
    TokenComponentType,
    ...
)
```

- [ ] **Step 3: 更新 Executor 单元测试**

修改 `Module/task_system/tests/unit/test_executor.py`：
- `Executor(...)` 调用加 `token_ledger=MagicMock()`
- 新增测试：验证 LLM 调用后 `token_ledger.record()` 被调用

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_executor.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/core/executor.py Module/task_system/tests/unit/test_executor.py
git commit -m "feat(token_tracking): Executor 支持 TokenLedger 注入 + _match_tool 后 record"
```

---

### Task 5: Evaluator — token_ledger 注入 + record

**Files:**
- Modify: `Module/task_system/core/evaluator.py:65-71` (`__init__`), `evaluator.py:229-266` (`_call_llm_for_judge`)

- [ ] **Step 1: 修改 Evaluator.__init__**

将 [evaluator.py:65-71](Module/task_system/core/evaluator.py#L65-L71) 替换为：

```python
    def __init__(
        self,
        infra: AgentInfra,
        artifact_store: ArtifactStore,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._infra = infra
        self._artifact_store = artifact_store
        self._token_ledger = token_ledger
```

TYPE_CHECKING 块中添加 `TokenLedger` import。

- [ ] **Step 2: 在 _call_llm_for_judge 中添加 token record**

`_call_llm_for_judge` 有两次 LLM 调用（retry 循环），每次 `response` 后都 record：

```python
            try:
                response = await self._infra.llm.chat.completions.create(
                    model=self._infra.llm_model,
                    messages=[...],
                    response_format={"type": "json_object"},
                    max_tokens=1024,
                )
                # ── token 追踪 ──
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
            except Exception:
                ...
```

注意：`_call_llm_for_judge` 方法签名当前不接收 `node`——需要改为 `_call_llm_for_judge(self, user_message: str, node: DAGNode) -> str | None`，因为 `node_id` 需要传入 record。

同时更新 `_judge_business` 中对此方法的调用（[evaluator.py:194](Module/task_system/core/evaluator.py#L194)）：
```python
content = await self._call_llm_for_judge(user_message, node)
```

- [ ] **Step 3: 更新 Evaluator 单元测试**

修改 `Module/task_system/tests/unit/test_evaluator.py`：
- `Evaluator(...)` 调用加 `token_ledger=MagicMock()`
- `_call_llm_for_judge` mock 适配新增的 `node` 参数

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_evaluator.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/core/evaluator.py Module/task_system/tests/unit/test_evaluator.py
git commit -m "feat(token_tracking): Evaluator 支持 TokenLedger 注入 + _call_llm_for_judge 后 record"
```

---

### Task 6: SubAgent + TaskSystem — 组件 per-task 化

**Files:**
- Modify: `Module/task_system/core/sub_agent.py:83-124` (`__init__`), `sub_agent.py:128-159` (`run`)
- Modify: `Module/task_system/task_system.py:103-140` (`start`), `task_system.py:183-283` (`submit`)

这是最大的一步重构——将 Planner/Executor/Evaluator/ArtifactStore/CheckpointManager/PlanValidator 从 TaskSystem.start() 共享改为 SubAgent 内部 per-task 创建。

- [ ] **Step 1: 修改 SubAgent.__init__ — 内部创建 per-task 组件**

将 [sub_agent.py:83-124](Module/task_system/core/sub_agent.py#L83-L124) 替换为：

```python
    def __init__(
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
    ) -> None:
        """SubAgent 构造——依赖简化为 infra + hitl_notifier 两个共享资源。

        所有 per-task 组件（TokenLedger / Planner / Executor / Evaluator /
        ArtifactStore / CheckpointManager / PlanValidator / Orchestrator）
        在 run() 中创建——因为 TokenLedger 需要 task_state。
        """
        self._infra = infra
        self._hitl = hitl_notifier

        # ── per-task 组件——run() 入口赋值，初始为 None ──
        self._planner: Planner | None = None
        self._plan_validator: PlanValidator | None = None
        self._executor: Executor | None = None
        self._evaluator: Evaluator | None = None
        self._orchestrator: Orchestrator | None = None
        self._checkpoint_mgr: CheckpointManager | None = None
        self._artifact_store: ArtifactStore | None = None
        self._token_ledger: TokenLedger | None = None

        # ── 运行时状态——run() 入口赋值，初始为 None ──
        self._task_state: TaskState | None = None
        self._graph: PlanGraph | None = None
```

- [ ] **Step 2: 修改 SubAgent.run() — 创建 TaskState 后立即创建所有 per-task 组件**

在 [sub_agent.py:141-156](Module/task_system/core/sub_agent.py#L141-L156) 创建 `task_state` 后，规划前，插入组件创建：

```python
        task_state = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tools=context.tools,
            session_id=context.session_id,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self._task_state = task_state

        # ── 创建 per-task 组件 ──
        from Module.task_system.artifact_store import ArtifactStore
        from Module.task_system.checkpoint import CheckpointManager
        from Module.task_system.core.evaluator import Evaluator
        from Module.task_system.core.executor import Executor
        from Module.task_system.core.orchestrator import Orchestrator
        from Module.task_system.core.planner import Planner
        from Module.task_system.core.plan_validator import PlanValidator
        from Module.task_system.token_ledger import TokenLedger

        self._token_ledger = TokenLedger(task_state)
        self._artifact_store = ArtifactStore()
        self._checkpoint_mgr = CheckpointManager()
        self._plan_validator = PlanValidator()
        self._planner = Planner(token_ledger=self._token_ledger)
        self._executor = Executor(
            infra=self._infra,
            artifact_store=self._artifact_store,
            hitl_notifier=self._hitl,
            token_ledger=self._token_ledger,
        )
        self._evaluator = Evaluator(
            infra=self._infra,
            artifact_store=self._artifact_store,
            token_ledger=self._token_ledger,
        )
        self._orchestrator = Orchestrator(
            infra=self._infra,
            hitl_notifier=self._hitl,
            executor=self._executor,
            evaluator=self._evaluator,
            checkpoint_manager=self._checkpoint_mgr,
            artifact_store=self._artifact_store,
        )
```

然后移除 `SubAgent.__init__` 中原来的 `Orchestrator` 创建代码（第 105-112 行）。所有组件引用 `self._planner` / `self._executor` 等改为由 `run()` 创建，`_plan_and_validate` / `_execute` 等方法中所有通过 `self._planner` 等的调用保持不变（现在它们指向 run() 创建的实例）。

- [ ] **Step 3: 简化 TaskSystem.start() — 移除组件创建**

将 [task_system.py:103-140](Module/task_system/task_system.py#L103-L140) 的 `start()` 简化为：

```python
    async def start(self) -> None:
        """启动 TaskSystem——初始化并发控制。

        调用方必须在 submit() 之前调用 start()。
        幂等——重复调用无副作用。
        """
        if self._started:
            logger.warning("TaskSystem.start: 已启动，跳过重复调用")
            return

        self._semaphore = asyncio.BoundedSemaphore(self._max_sub_agents)
        self._started = True
        logger.info("TaskSystem.start: max_sub_agents=%d", self._max_sub_agents)
```

同时移除 `TaskSystem.__init__` 中不再需要的组件字段声明（[task_system.py:91-96](Module/task_system/task_system.py#L91-L96)）：

```python
        # 移除以下字段（planner/plan_validator/executor/evaluator/checkpoint_mgr/artifact_store）
        # 它们现在是 per-task 的，SubAgent 内部创建
```

- [ ] **Step 4: 修改 TaskSystem.submit() — 适配新的 SubAgent 签名**

将 [task_system.py:225-234](Module/task_system/task_system.py#L225-L234) SubAgent 构造改为：

```python
        sub_agent = SubAgent(
            infra=self._infra,
            hitl_notifier=self._hitl,
        )
```

移除 submit() 中不再需要的 assert 检查（[task_system.py:202-209](Module/task_system/task_system.py#L202-L209) 中的 planner/plan_validator/executor/evaluator/checkpoint_mgr/artifact_store assert）。

保留 `_semaphore` assert（submit 仍需要信号量控制并发）。

- [ ] **Step 5: 更新 TaskSystem 内部调用**

`_get_debug_snapshot()` 中引用 `sub_agent._orchestrator` 的路径不变（SubAgent 仍持有 `self._orchestrator`）。`list_active_tasks()` 中引用 `self._checkpoint_mgr` 的路径需要改为 `sub_agent._checkpoint_mgr`（从 `self._checkpoint_mgr` 改为 per-task 引用）。

```python
# list_active_tasks() 中:
# 旧: await self._checkpoint_mgr.load(task_id)
# 新: await sub_agent._checkpoint_mgr.load(task_id) if sub_agent._checkpoint_mgr else None
```

`_cleanup_storage()` 中类似调整：
```python
# 旧: self._checkpoint_mgr / self._artifact_store
# 新: sub_agent._checkpoint_mgr / sub_agent._artifact_store
```

- [ ] **Step 6: 跑全部单元测试修复回归**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v 2>&1 | tail -40`
Expected: 大部分测试 FAIL（SubAgent 构造签名变了）。逐个修复测试文件中的 `SubAgent(...)` 调用。

涉及的文件：
- `tests/unit/test_sub_agent.py` — SubAgent 构造改为 `SubAgent(infra=..., hitl_notifier=...)`
- `tests/unit/test_task_system.py` — TaskSystem.start() 不再创建组件，测试需适配
- `tests/unit/test_orchestrator.py` — Orchestrator 仍从外部注入 executor/evaluator，测试不变

逐个跑测试修复：
```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v  # 先修这组
python -m pytest Module/task_system/tests/unit/test_task_system.py -v  # 再修这组
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v  # 应该直接过
```

- [ ] **Step 7: 全部单元测试通过**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v`
Expected: all ~260 tests PASS

- [ ] **Step 8: Commit**

```bash
git add Module/task_system/core/sub_agent.py Module/task_system/task_system.py \
        Module/task_system/tests/unit/test_sub_agent.py \
        Module/task_system/tests/unit/test_task_system.py
git commit -m "refactor(task_system): 组件 per-task 化——Planner/Executor/Evaluator 等改由 SubAgent 内部创建"
```

---

### Task 7: 查询 API — get_token_usage + get_token_stats

**Files:**
- Modify: `Module/task_system/task_system.py:481-491`

- [ ] **Step 1: 实现 get_token_usage**

替换 [task_system.py:481-483](Module/task_system/task_system.py#L481-L483) 的 `NotImplementedError`：

```python
    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
        """查询单任务的 token 消耗明细（含完整审计日志）。"""
        sub_agent = self._tasks.get(task_id)
        ts = sub_agent._task_state if sub_agent else None
        if ts is None:
            return None
        return TokenUsage(
            task_id=task_id,
            components=list(ts.token_entries),
        )
```

- [ ] **Step 2: 实现 get_token_stats**

替换 [task_system.py:489-491](Module/task_system/task_system.py#L489-L491) 的 `NotImplementedError`：

```python
    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
        """聚合 token 统计（按时间段 + 组件 + 模型）。

        遍历所有活跃任务，按 period 过滤，按 component/model 聚合。
        """
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        cutoff: datetime | None = None
        if period == TokenPeriod.TODAY:
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == TokenPeriod.WEEK:
            cutoff = now - timedelta(days=7)
        elif period == TokenPeriod.MONTH:
            cutoff = now - timedelta(days=30)
        # ALL: cutoff = None（不过滤）

        total = 0
        by_component: dict[str, int] = {}
        by_model: dict[str, int] = {}

        for sub_agent in self._tasks.values():
            ts = sub_agent._task_state
            if ts is None:
                continue
            # 按时间段过滤
            if cutoff and ts.created_at:
                try:
                    created = datetime.fromisoformat(ts.created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            for entry in ts.token_entries:
                total += entry.total_tokens
                key = entry.component.value
                by_component[key] = by_component.get(key, 0) + entry.total_tokens
                if entry.model:
                    by_model[entry.model] = by_model.get(entry.model, 0) + entry.total_tokens

        return TokenStats(
            period=period,
            total_tokens=total,
            by_component=by_component,
            by_model=by_model,
        )
```

并在文件顶部 import 加 `datetime` 和 `timezone`（已有 `datetime` 和 `timezone` import 但需要确认）。

- [ ] **Step 3: 更新 TaskSystem 单元测试**

在 `test_task_system.py` 中新增：
```python
def test_get_token_usage_returns_none_for_missing_task(self):
    """不存在的 task_id 返回 None。"""
    ...

def test_get_token_usage_returns_token_usage(self):
    """活跃任务的 token_usage 可通过查询 API 获取。"""
    ...

def test_get_token_stats_aggregates(self):
    """get_token_stats 跨任务聚合 token 数据。"""
    ...
```

- [ ] **Step 4: 跑测试**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_task_system.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/task_system.py Module/task_system/tests/unit/test_task_system.py
git commit -m "feat(token_tracking): 实现 get_token_usage / get_token_stats 查询 API"
```

---

### Task 8: DelegationPreparer — 独立返回 token_usage

**Files:**
- Modify: `Module/task_system/schema.py:393-416` (DelegationResult)
- Modify: `Module/task_system/delegation_preparer.py:124-171` (`prepare`), `delegation_preparer.py:194-203` (`_call_llm`)

- [ ] **Step 1: DelegationResult 加 token_usage 字段**

在 [schema.py:412](Module/task_system/schema.py#L412) `suggestion` 字段之后插入：

```python
    token_usage: dict | None = None
    # LLC 调用消耗: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    # status=ok 时始终有值；其他状态可能为 None
```

- [ ] **Step 2: prepare() 中捕获 token_usage**

在 `prepare()` 中 LLM 调用之后、解析之前捕获 usage：

```python
        # ── LLM 调用 ──
        logger.debug("DelegationPreparer user prompt:\n%s", user)
        raw, token_usage = await self._call_llm_with_usage(system, user)
        logger.debug("DelegationPreparer LLM 响应:\n%s", raw)
```

然后在构建 DelegationResult 时传入：
```python
        result = self._build_result(validated, intent, hints, tools, session_id)
        result.token_usage = token_usage
        return result
```

- [ ] **Step 3: 修改 _call_llm 返回 token_usage**

将 `_call_llm` 重命名为 `_call_llm_with_usage`，返回 `tuple[str, dict]`：

```python
    async def _call_llm_with_usage(self, system: str, user: str) -> tuple[str, dict]:
        """调用 LLM。返回 (响应文本, token_usage dict)。"""
        model = os.getenv("TASK_SYSTEM_MODEL", "deepseek-v4-pro")
        response = await self._client.chat.completions.create(
            model=model,
            messages=[...],
            response_format={"type": "json_object"},
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
            max_tokens=4096,
        )
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        content = response.choices[0].message.content
        if not content:
            raise _PrepParseError("LLM 返回空响应")
        return content, usage
```

注意：原 `_call_llm` 中有 `parse_error` / `_PrepParseError`，确认异常类名正确。

- [ ] **Step 4: 跑 DelegationPreparer 测试**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/test_delegation_preparer.py -v`
Expected: 适配后全部 PASS

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/schema.py Module/task_system/delegation_preparer.py \
        Module/task_system/tests/unit/test_delegation_preparer.py
git commit -m "feat(token_tracking): DelegationPreparer.prepare() 独立返回 token_usage"
```

---

### Task 9: 修复现有测试回归

**Files:**
- Modify: `Module/task_system/tests/unit/test_planner.py`, `test_executor.py`, `test_evaluator.py`, `test_sub_agent.py`, `test_task_system.py`, `test_orchestrator.py`, `test_graph.py`
- Modify: `Module/task_system/tests/integration/test_*.py`（5 个文件）

- [ ] **Step 1: 跑全部测试看哪些失败**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v 2>&1 | grep -E "PASSED|FAILED|ERROR" | tail -20`

- [ ] **Step 2: 逐个文件修复**

常见修复模式：
1. `Planner(token_ledger=MagicMock())` — 新参数
2. `Executor(..., token_ledger=MagicMock())` — 新参数
3. `Evaluator(..., token_ledger=MagicMock())` — 新参数
4. `SubAgent(infra=..., hitl_notifier=...)` — 移除多余的组件参数
5. TaskSystem 测试不再需要 mock `self._planner` 等共享组件

- [ ] **Step 3: 全部单元测试通过**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/unit/ -v`
Expected: all ~260 tests PASS

- [ ] **Step 4: 集成测试通过**

Run: `conda activate Elysia && python -m pytest Module/task_system/tests/integration/ -v`
Expected: 适配后全部 PASS（需 DEEPSEEK_API_KEY）

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/tests/
git commit -m "test(token_tracking): 适配 per-task 组件重构——更新所有测试签名"
```

---

### Task 10: 更新 PROGRESS.md

**Files:**
- Modify: `Module/task_system/docs/PROGRESS.md`

- [ ] **Step 1: 更新 v1 未完成项**

将 PROGRESS.md 中以下项目从 `⚠️ NotImplementedError` 改为 `✅ 已实现`：
- `get_token_usage()`
- `get_token_stats()`

将 "内部功能缺口" 中 "Token 追踪" 从 "类型定义已完，采集未实现" 改为 "✅ 已实现（TokenLedger write-through + per-call 审计日志）"

- [ ] **Step 2: 更新架构图**

架构图中添加 `TokenLedger` 组件：
```
TaskSystem
├── ...
├── TokenLedger             # per-task token 账本（NEW）
└── ...
```

- [ ] **Step 3: 新增本周修复条目**

在 "本周修复" 表格中新增 token 追踪相关条目。

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/docs/PROGRESS.md
git commit -m "docs(token_tracking): 更新 PROGRESS——token 追踪已完成"
```

---

## 执行顺序

```
Task 1 (TokenLedger) ── 独立，无依赖
    ↓
Task 2 (Schema) ── 独立，无依赖
    ↓
Task 3 (Planner) ── 依赖 Task 1
    ↓
Task 4 (Executor) ── 依赖 Task 1
    ↓
Task 5 (Evaluator) ── 依赖 Task 1
    ↓
Task 6 (per-task 重构) ── 依赖 Task 3-5
    ↓
Task 7 (查询 API) ── 依赖 Task 1,2,6
    ↓
Task 8 (DelegationPreparer) ── 独立
    ↓
Task 9 (修复测试) ── 依赖 Task 1-8
    ↓
Task 10 (PROGRESS.md) ── 依赖全部
```
