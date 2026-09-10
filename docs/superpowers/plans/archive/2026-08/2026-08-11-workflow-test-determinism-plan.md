# Workflow 测试确定性与外部 E2E 分层实施计划（E4）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建 fake-LLM 确定性测试基建（FakeLLM + with_deadline + CountingLLM）、`RUN_EXTERNAL_TESTS` 门禁、integration marker 补齐、coroutine warning 清零——为 E3 的 cancel/pause 确定性测试铺路，并让外部 E2E 只在显式 opt-in 时运行。

**Architecture:** `Module/workflow_engine/tests/helpers/` 提供 FakeLLM（对齐 `chat.completions.create` 访问面：tool_calls/content/usage）+ with_deadline（挂死防线）+ CountingLLM（外部成本计数）。`Module/conftest.py` 加 external skip hook（`item.add_marker(skipif)`）。`make test-external` 设 `RUN_EXTERNAL_TESTS=1`。两个真实 E2E 文件补 `integration` marker。

**Tech Stack:** Python 3.12 + pytest + asyncio + `Module/workflow_engine`。

## Global Constraints

- 测试文件豁免类型注解；注释中文。
- pytest strict 模式：async 测试 `async def` + `@pytest.mark.asyncio`。
- 新增测试标 `unit`/`service`（入 test-quick 门禁）。
- **执行顺序**：本 plan（E4）在 E2 之后、E3 之前执行——E2 完成则 mailbox 首事件（QUEUED）测试可绿；E3 的 cancel/pause 测试不在此 plan。

## 文件结构

| 文件 | 职责 |
|---|---|
| `Module/workflow_engine/tests/helpers/fake_llm.py` | **Create**：FakeLLM + with_deadline + CountingLLM |
| `Module/workflow_engine/tests/helpers/__init__.py` | 确认存在（helpers 目录已存在） |
| `Module/conftest.py` | 追加 external skip hook |
| `Makefile` | `test-external` 加 `RUN_EXTERNAL_TESTS=1` |
| `Module/workflow_engine/tests/integration/test_e2e_mvp.py` | pytestmark 补 `integration` |
| `Module/workflow_engine/tests/integration/test_workflow_engine_lifecycle.py` | pytestmark 补 `integration` |
| `Module/workflow_engine/tests/unit/test_fake_llm.py` | **Create**：fake-LLM 自身测试 + mailbox 首事件确定性测试 |

---

### Task 1: `helpers/fake_llm.py`——FakeLLM + with_deadline + CountingLLM

**Files:**
- Create: `Module/workflow_engine/tests/helpers/fake_llm.py`
- Test: `Module/workflow_engine/tests/unit/test_fake_llm.py`

**Interfaces:**
- Produces: `FakeLLM`（`chat`/`completions` 自引用 + `create(*, messages, **kwargs)`；`handler`/`gate`/`calls`/`prompts`）；`with_deadline(coro, timeout=10)`；`CountingLLM(real)`（`count` + 委托）。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_text_direct():
    fake = FakeLLM()
    fake.handler = lambda msgs: {"type": "text", "content": "hi"}
    resp = await fake.create(messages=[{"role": "user", "content": "x"}])
    assert resp.choices[0].message.content == "hi"
    assert resp.choices[0].message.tool_calls is None
    assert resp.usage.prompt_tokens == 1          # 属性访问
    assert fake.calls == 1

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_tool_match():
    fake = FakeLLM()
    fake.handler = lambda msgs: {"type": "tool", "name": "get_time", "arguments": {"tz": "UTC"}}
    resp = await fake.create(messages=[])
    tc = resp.choices[0].message.tool_calls[0]
    assert tc.function.name == "get_time"
    assert json.loads(tc.function.arguments) == {"tz": "UTC"}

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fake_llm_gate_blocks_until_released():
    fake = FakeLLM()
    fake.gate = asyncio.Event()
    task = asyncio.create_task(fake.create(messages=[]))
    await asyncio.sleep(0.02)
    assert fake.calls == 0                        # gate 未放行，未计数
    fake.gate.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.calls == 1

@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_deadline_times_out():
    with pytest.raises(asyncio.TimeoutError):
        await with_deadline(asyncio.sleep(5), timeout=0.05)

@pytest.mark.unit
@pytest.mark.asyncio
async def test_counting_llm_forwards_messages():
    class Real:
        def __init__(self):
            self.calls = []
            self.chat = self          # 属性自引用（非方法）——委托链 real.chat.completions.create 走属性
            self.completions = self
        async def create(self, *, messages, **kwargs):
            self.calls.append(messages); return "ok"
    real = Real()
    cl = CountingLLM(real)
    await cl.create(messages=[{"role": "user", "content": "x"}], model="m")
    assert cl.count == 1
    assert real.calls == [[{"role": "user", "content": "x"}]]   # messages 被透传
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_fake_llm.py -q -x --tb=short`
Expected: FAIL（ModuleNotFoundError: fake_llm）

- [ ] **Step 3: 最小实现**

按 spec §3.1 实现 `FakeLLM`/`FakeMessage`/`FakeToolCall`/`FakeUsage`/`FakeCompletion`；`with_deadline` = `asyncio.wait_for(coro, timeout=timeout)`；`CountingLLM`（spec §5.3，`chat`/`completions` 自引用 + `create(*, messages, **kwargs)` 先 count+=1 再 `real.chat.completions.create(messages=messages, **kwargs)`）。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/tests/helpers/fake_llm.py Module/workflow_engine/tests/unit/test_fake_llm.py
git commit -m "test(workflow): fake LLM harness with gate and counting wrapper (E4)"
```

---

### Task 2: `Module/conftest.py` external skip hook

**Files:**
- Modify: `Module/conftest.py`
- Test: 无（hook 行为用 `--collect-only` 验证）

**Interfaces:**
- Produces: collection hook——未设 `RUN_EXTERNAL_TESTS=1` 时 external 测试全 skip。

- [ ] **Step 1: 写 hook**

```python
# Module/conftest.py（追加）
import os
import pytest

def pytest_collection_modifyitems(config, items):
    """外部测试门禁：未显式 RUN_EXTERNAL_TESTS=1 → external 测试整体 skip。
    用 item.add_marker(skipif)——裸 pytest.skip() 在 collection 阶段会中断收集。"""
    if os.getenv("RUN_EXTERNAL_TESTS") == "1":
        return
    skip = pytest.mark.skipif(True, reason="需要 RUN_EXTERNAL_TESTS=1 显式 opt-in（真实 LLM/DB E2E）")
    for item in items:
        if "external" in item.keywords:
            item.add_marker(skip)
```

- [ ] **Step 2: 验证 skip 生效**（**不用 `--collect-only`**——收集阶段不评估 skipif marker，输出恒无 SKIPPED；需真实运行）

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/integration/ -q --tb=short 2>&1 | grep -ci "skipped"`
Expected: >0（未设 env 时 external 测试被 skip——**不会触网**，因 skip 在运行前生效）
Run: `conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest Module/workflow_engine/tests/integration/ --collect-only -q 2>&1 | grep -c "test"` 
Expected: >0（设 env 收集到测试节点；此处仅验证 env 门禁放行收集）

- [ ] **Step 3: 提交**

```bash
git add Module/conftest.py
git commit -m "test(workflow): external gate via RUN_EXTERNAL_TESTS skip hook (E4)"
```

---

### Task 3: `Makefile` test-external 加 env

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: 修改目标**

```makefile
test-external:
	conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short
```

- [ ] **Step 2: 验证**

Run: `make -n test-external`
Expected: 命令含 `RUN_EXTERNAL_TESTS=1`

- [ ] **Step 3: 提交**

```bash
git add Makefile
git commit -m "ci(workflow): external tests require RUN_EXTERNAL_TESTS=1 (E4)"
```

---

### Task 4: integration marker 补齐

**Files:**
- Modify: `Module/workflow_engine/tests/integration/test_e2e_mvp.py`
- Modify: `Module/workflow_engine/tests/integration/test_workflow_engine_lifecycle.py`

**Interfaces:**
- Produces: 两文件 pytestmark → `[slow, integration, external]`（满足计划 E4「真实 LLM 必须同时标 integration + external」）。

- [ ] **Step 1: 修改**

`test_e2e_mvp.py:43` 与 `test_workflow_engine_lifecycle.py:40`：
```python
pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.external]
```

- [ ] **Step 2: 验证**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/integration/ --collect-only -q | grep -c "e2e_mvp\|lifecycle"`
Expected: 文件仍收集（marker 补齐不破坏收集）

- [ ] **Step 3: 提交**

```bash
git add Module/workflow_engine/tests/integration/
git commit -m "test(workflow): mark real-LLM E2E as integration+external (E4)"
```

---

### Task 5: mailbox 首事件确定性测试（E2 依赖）

**Files:**
- Modify: `Module/workflow_engine/tests/unit/test_fake_llm.py`

**Interfaces:**
- Consumes: E2（submit 已非阻塞 + QUEUED 事件）、Task 1 FakeLLM。
- Produces: submit 首事件 QUEUED 的确定性测试（E3 的 cancel/pause 测试在 E3 plan 落地）。

- [ ] **Step 1: 写失败测试**（须 E2 完成后才绿——E4 在 E2 后执行）

> **E2 硬前置**：`WorkflowEventType.QUEUED`（E2 plan Task 1 新增）为编译前置——E2 未落地则该测试 AttributeError 而非「首事件非 QUEUED」。执行序 E2→E4 保证。
> **import 清单**：`AgentInfra`/`DelegationContext`/`SessionId`/`WorkflowEventType` 来自 `Module.workflow_contract`；`WorkflowMailbox` 来自 `Module.workflow_engine.mailbox`；`WorkflowEngine` 来自 `Module.workflow_engine.workflow_engine`；`FakeLLM`/`with_deadline` 来自 `Module.workflow_engine.tests.helpers.fake_llm`；`ConstraintConfig` 来自 `Module.workflow_contract`。

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_submit_first_event_is_queued():
    """submit 立即返回 mailbox，首事件为 QUEUED（E2 契约）。"""
    fake = FakeLLM()
    infra = AgentInfra(llm=fake, milvus=None, http=None,
                       tool_registry=MagicMock(), constraints=ConstraintConfig())
    engine = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    engine._semaphore.acquire_nowait()          # 占满槽 → 新任务排队
    try:
        ctx = DelegationContext(intent="t", session_id=SessionId("s1"))
        result = await with_deadline(engine.submit(ctx), timeout=1.0)
        assert isinstance(result, WorkflowMailbox)
        ev = await with_deadline(result.get(), timeout=1.0)   # 首事件
        assert ev.type == WorkflowEventType.QUEUED
    finally:
        engine._semaphore.release()             # 释放预占 permit——防失败路径遗留 worker 阻塞（loop 关闭告警）
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_fake_llm.py -q -k "first_event" -x --tb=short`
Expected: FAIL（若 E2 未完成：首事件非 QUEUED 或 AttributeError——E4 在 E2 后执行，此步应 PASS）。

- [ ] **Step 3: 补齐**（若 FAIL 因 E2 未完成，阻塞等待 E2；否则跳过）

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/tests/unit/test_fake_llm.py
git commit -m "test(workflow): deterministic mailbox first-event (QUEUED) (E4)"
```

---

### Task 6: coroutine warning 清零审计

**Files:**
- Modify: `Module/workflow_engine/tests/`（按需）

**Interfaces:**
- Produces: workflow 测试无 `coroutine .*was never awaited`（pytest.ini filterwarnings=error 门禁）。

- [ ] **Step 1: 审计**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/ -m "unit or service" -q --tb=short 2>&1 | grep -i "coroutine\|never awaited" | head`
Expected: 无（或列出现有）。有则修：
- AsyncMock 方法返回未 await 协程 → `MagicMock(return_value=...)` 或正确 `return_value`。
- fire-and-forget task 未 drain → teardown `await asyncio.sleep(0)` / `task.cancel()`。

- [ ] **Step 2: 修复直至无 warning**

- [ ] **Step 3: 提交**

```bash
git add Module/workflow_engine/tests/
git commit -m "fix(test): clear coroutine-never-awaited warnings in workflow tests (E4)"
```

---

### Task 7: 门禁 + 文档

**Files:**
- Modify: `todo.md`（E4 勾选 + 决策）
- Modify: `docs/`（测试分层/外部门禁说明）

- [ ] **Step 1: 全量门禁**

```bash
make lint && make pyright && make check-types
conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short
```
Expected: 全绿。

- [ ] **Step 2: 文档更新**

`todo.md` E4 完成 + 决策（RUN_EXTERNAL_TESTS 门禁、fake-LLM 形状、with_deadline 挂死防线、CountingLLM 成本）；`docs/testing.md` 补 external 门禁说明。

- [ ] **Step 3: 提交**

```bash
git add todo.md docs/
git commit -m "docs(workflow): E4 completion — deterministic test harness and external gate"
```

---

## 自审记录

- **Spec 覆盖**：Task 1→spec §3/§5.3、Task 2→§5.1、Task 3→§5.2、Task 4→§5.3、Task 5→§4 mailbox 行、Task 6→§6、Task 7→§7/§8。
- **类型一致**：FakeLLM/with_deadline/CountingLLM 命名与 spec 一致；CountingLLM 委托 `messages=messages` 显式透传。
- **顺序**：Task 1 先建 infra（Task 5 依赖）；Task 4 改 marker 不影响 Task 2 hook（hook 按 keyword 判断，marker 补全后仍判 external）。
- **无占位符**：每 Task 含可运行测试 + 实现。
