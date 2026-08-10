# Workflow 测试确定性与外部 E2E 分层设计（E4）

> **状态**：设计定稿（2026-08-11）
> **对应**：P2-11 部分（`todo.md` E4）、`docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md` Task E4
> **前置**：F1 已落地 `external` marker + `make test-external` + `test-workflow-engine` 目标；本 spec 补齐 fake-LLM 确定性基建 + `RUN_EXTERNAL_TESTS` 门禁 + 警告清零
> **相关 spec**：E3（强取消）的确定性测试依赖本 spec 的 fake-LLM

---

## 1. 问题（P2-11 部分）

1. **外部 E2E flaky**：全量 `pytest` 2 个失败均为带真实 `DEEPSEEK_API_KEY` 的 workflow 外部 E2E **首事件超时**（真实 LLM 延迟/波动）。F1 已用 `external` marker 把它们移出合并门禁，但 `make test-external` 直接跑真实 LLM——无显式凭据门禁。
2. **无 fake-LLM 确定性基建**：workflow 测试（`Module/workflow_engine/tests/`）无确定性 LLM stub。E3 的 cancel/pause/mailbox 首事件测试需要「可控制延迟、按事件释放、记录调用」的 fake LLM。
3. **integration conftest 自动加载 `.env`**（`Module/workflow_engine/tests/integration/conftest.py`）：测试收集即把真实 key 注入 `os.environ`——缺乏显式 opt-in 门禁，误跑/泄露风险。
4. **`coroutine was never awaited` warning**：workflow 测试部分 mock 用错 async shape；F4 part 1 清了 core/scripts，workflow 测试仍有残留。
5. **F1 遗留合规缺口**：`test_e2e_mvp.py`、`test_workflow_engine_lifecycle.py` 的 pytestmark 是 `[slow, external]`，**缺 `integration` 标记**——计划 E4 要求真实 LLM/公网测试必须同时标 `integration + external`。

## 2. 决策

- **fake-LLM 确定性基建**：`Module/workflow_engine/tests/helpers/` 建共享工具——`fake_llm.py`（FakeLLM，确定性 stub）、`with_deadline`（挂死防线）、`CountingLLM`（外部成本计数包装）——形状对齐 `AgentInfra.llm.chat.completions.create` 调用路径（含 `tool_calls`/`content`/`usage` 三访问面）。
- **`RUN_EXTERNAL_TESTS=1` 门禁**：所有 `external` 测试在未设该环境变量时 **skip**（`Module/conftest.py` collection hook 统一处理）；`make test-external` 显式设 `RUN_EXTERNAL_TESTS=1`。外部 E2E 受保护凭据 + 总 timeout（`asyncio.wait_for`，不引入新依赖）+ 成本上限；失败日志不输出 prompt/response。
- **warning 清零**：workflow 测试 mock 用正确 async shape，teardown drain task。

## 3. fake-LLM 确定性 stub

### 3.1 形状（对齐 executor 访问面）

`Module/workflow_engine/tests/helpers/fake_llm.py`：

```python
"""确定性 fake LLM——为 AgentInfra.llm 提供可控制的 chat.completions.create。

对齐 executor/planner 的访问面：
- executor._match_tool: choices[0].message.tool_calls[0].function.name/arguments
- executor._call_llm_direct / planner parser: choices[0].message.content
- token ledger: usage.prompt_tokens / usage.completion_tokens（属性访问）
"""

from __future__ import annotations

import json
from typing import Any


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments          # JSON 字符串（executor 用 json.loads 解析）


class FakeToolCall:
    def __init__(self, name: str, arguments: dict) -> None:
        self.function = FakeFunction(name, json.dumps(arguments, ensure_ascii=False))


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list[FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls        # None → executor 走 __llm_direct__ 直出路径


class FakeUsage:
    def __init__(self, prompt_tokens: int = 1, completion_tokens: int = 1) -> None:
        self.prompt_tokens = prompt_tokens  # 属性访问（非 dict）——对齐 executor.py:310-316
        self.completion_tokens = completion_tokens


class FakeCompletion:
    def __init__(self, message: FakeMessage, usage: FakeUsage | None = None) -> None:
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = usage or FakeUsage()


class FakeLLM:
    """确定性 LLM stub。

    - handler: Callable[[list[dict]], dict]  每次 create 调用返回结构化结果：
        {"type": "tool", "name": ..., "arguments": {...}} → 工具匹配
        {"type": "text", "content": "..."}              → 直出文本（planner/汇总节点）
    - gate:    asyncio.Event | None   若设置，create 先 await gate.wait() 再返回。
        无 timeout——cancel 测试靠「挂起直到被 CancelledError 打断」语义（gate 恒不释放）。
    - calls:   调用尝试数（gate 通过后 +1，**无论 handler 是否抛异常**）——「cancel 后不再消耗 token」断言基准；prompts 记录输入。
    """

    def __init__(self) -> None:
        self.chat = self                 # AgentInfra.llm.chat.completions.create(...) 路径
        self.completions = self
        self.handler = lambda msgs: {"type": "text", "content": "fake response"}
        self.gate: Any = None            # asyncio.Event | None
        self.calls = 0                   # 调用尝试数（gate 通过后 +1，无论 handler 是否抛）——断言基准
        self.prompts: list[list[dict]] = []

    async def create(self, *, messages: list[dict], **_: Any) -> FakeCompletion:
        if self.gate is not None:
            await self.gate.wait()       # 无 timeout：取消时 CancelledError 在此穿透
        self.calls += 1
        self.prompts.append(messages)
        result = self.handler(messages)
        if isinstance(result, dict) and result.get("type") == "tool":
            return FakeCompletion(
                FakeMessage(tool_calls=[FakeToolCall(result["name"], result.get("arguments", {}))])
            )
        content = result.get("content") if isinstance(result, dict) else str(result)
        return FakeCompletion(FakeMessage(content=content))
```

### 3.2 关键语义

- **`tool_calls` 必须存在**：executor `_match_tool` 先判 `if not msg.tool_calls:`（`executor.py:322-323`）——`FakeMessage.tool_calls` 为 `None`（直出）或 `list[FakeToolCall]`（工具匹配），**绝不能缺属性**（否则 AttributeError）。
- **`__llm_direct__` 哨兵不需要 handler 返回**：handler 返回 `{"type": "text", ...}` → `tool_calls=None` → executor 自然走直出路径（哨兵是 executor 内部产物，`executor.py:40/151`）。
- **`usage` 是对象非 dict**：token ledger 以属性访问 `usage.prompt_tokens`（`executor.py:310-316`）——`FakeUsage` 用属性；若接 TokenLedger，每次 fake 调用正常记账。
- **`calls` 在 gate 通过后 +1**：「慢 planner cancel」测试中 gate 恒不释放 → create 挂起未完成 → `calls` 保持 0——断言「cancel 后不再消耗 token」即 `calls` 不增长（挂起中取消，完成数不增）。
- **gate 无 timeout**：不释放即永久挂起直到取消（`owned.cancel()` 投递 CancelledError）。**不用超时转 TimeoutError**——否则 planner 视作 LLM 故障进入 retry，破坏 cancel 语义。
- **gated 测试挂死防线**：确定性测试的 gate 无 timeout，若 cancel 未到达会永久挂起整个套件。`tests/helpers/` 提供 `with_deadline(coro, timeout=10)`（`asyncio.wait_for` 封装），**所有 gated 取消测试的 await 都用它包裹**——cancel 未到达时 10s 后抛 TimeoutError 使该测试失败（而非挂死套件），并暴露是哪个用例。
- **per-test 实例**：fixture 每个测试新建 `FakeLLM()`——gate 状态/calls 不跨测试泄漏。

### 3.3 接线到 AgentInfra

`AgentInfra.llm: AsyncOpenAI`（`Module/workflow_contract/infra.py:42`）仅静态类型声明，运行时 duck-typing 满足 `chat.completions.create(model=..., messages=...)` 路径（`executor.py:298/382`）。测试构造 `AgentInfra(llm=fake_llm, ...)` 即可。pyright 排除 tests，无类型约束。

## 4. 确定性测试（E3 依赖）

| 用例（unit/service，fake-LLM） | 断言 |
|---|---|
| 慢 planner cancel | `gate` 挂起 planning → cancel → 任务 CANCELLED、`fake.calls` 保持 0、终态不回 RUNNING |
| planning retry 间 cancel | LLM 调用失败后、**orchestrator 重试前** cancel（retry 在 Orchestrator 层，非 planner 内）→ 不再重试、CANCELLED |
| tool 调用前 cancel | handler 返回 tool 匹配 → 工具调用前 cancel → **工具未调用**（`_check_cancelled` ② 拦截） |
| tool 调用中 cancel | shield 保护下工具调用完成（副作用不打断）、结果弃用、任务 CANCELLED |
| resume→pause→resume 循环 | 不销毁 task/mailbox、可循环多次 |
| mailbox 首事件 | submit 立即返回 mailbox，首事件为 QUEUED（E2）；终态事件唯一 |
| 乱序 cancel∥pause | 无论先后，终态 CANCELLED 唯一且不逆转 |

（与 E3 spec §7 测试表一致；E4 提供基建，E3 落地具体用例。）

## 5. `RUN_EXTERNAL_TESTS` 门禁

### 5.1 全局 skip hook（`Module/conftest.py`）

`Module/conftest.py`（覆盖全部 `Module/` 测试；F1 的 36 个 external 测试全在 `Module/workflow_engine/tests/integration/`，无 Module 外 external 标记——若未来 core 加 external，需在仓库根 conftest 同款 hook）：

```python
# Module/conftest.py（追加）
import os
import pytest

def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_EXTERNAL_TESTS") == "1":
        return                                          # 显式 opt-in → 不 skip
    skip = pytest.mark.skipif(
        True, reason="需要 RUN_EXTERNAL_TESTS=1 显式 opt-in（真实 LLM/DB E2E）",
    )
    for item in items:
        if "external" in item.keywords:                 # 未设 env → external 测试全部 skip
            item.add_marker(skip)
```

- **必须用 `item.add_marker(pytest.mark.skipif(...))`**——裸 `pytest.skip()` 在 collection 阶段抛 Skipped 中断收集，导致整个 session 提前终止；`add_marker` 逐项干净跳过。
- **不放在 `tests/integration/conftest.py`**——那只覆盖 integration 子树，unit/ 或未来新增目录的 external 用例会漏网。
- `test-workflow-engine`/`test-quick`/`test-normal` 用 `-m "unit or service"`/`-m "not external"` 已排除 external，hook 是**双保险**（防「环境恰有 key + 显式 `-m external` 但忘设 env」误跑）。

### 5.2 make 目标

```makefile
test-external:
	conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short
```

（`conda run -n Elysia VAR=1 python ...` 环境变量透传已验证可用。）

### 5.3 凭据、timeout、成本与日志保护

- **integration marker 补齐**：`test_e2e_mvp.py`、`test_workflow_engine_lifecycle.py` 的 pytestmark 补 `pytest.mark.integration` → `[slow, integration, external]`（满足计划 E4「同时标 integration + external」）。
- key 经 `monkeypatch.setenv` 注入、测试结束即清；`.env` 自动加载保留但不依赖——未设 env 则 skip。
- **总 timeout**：真实调用包 `asyncio.wait_for(..., timeout=60)`（**不引入 pytest-timeout 依赖**——项目 requirements/environment.lock 未含，用装饰器会 MissingPlugin）。每个外部测试模块统一 timeout 封装。
- **成本上限**：外部测试用计数包装器包真实客户端——`tests/helpers/` 提供 `CountingLLM(real: AsyncOpenAI)`：暴露 `chat`/`completions` 自引用 + `async def create(self, *, messages, **kwargs)` 先 `self.count += 1` 再**委托 `real.chat.completions.create(messages=messages, **kwargs)`**（`messages` 是具名 keyword-only 参数、不进 `**kwargs`——必须显式透传，否则真实客户端缺 messages 必抛）。测试 `AgentInfra(llm=CountingLLM(real), ...)`，E2E 后断言 `counting.count <= 10`（超限立即 fail）。确定性测试的成本断言基于 `FakeLLM.calls`（同样 ≤ 上限）——两路径共用「调用次数」语义。
- **日志脱敏**：外部测试 fixture 将 workflow/executor logger 级别设为 `WARNING`——`executor.py:292/374` 在 DEBUG 级输出完整 LLM prompt 与上游数据，排查时开 DEBUG 会泄 prompt/正文；外部测试强制 WARNING，失败断言消息只报状态/错误码/调用次数，不含 prompt/response 明文。

## 6. `coroutine was never awaited` 警告清零

审计 `Module/workflow_engine/tests/` 全部 `AsyncMock`/mock：

- **AsyncMock 方法调用返回未 await 协程**：mock 的 `async def` 方法须 `return_value` 为已 resolve 值或用 `MagicMock(return_value=...)`（非 async）替代（`get_preprocess` 类场景，F4 已清 core 同型）。
- **fire-and-forget task 未 drain**：测试 teardown `await asyncio.sleep(0)` 或显式 `task.cancel()` 让 `_schedule` 类任务完成。
- `pytest.ini` 的 `filterwarnings = error:coroutine .*was never awaited`（F4 已加）持续门禁；workflow 测试新写必须过该门禁。

## 7. Makefile / 契约影响

| 面 | 变更 |
|---|---|
| `make test-external` | 加 `RUN_EXTERNAL_TESTS=1` 前置 |
| `Module/conftest.py` | 追加 external skip hook |
| `Module/workflow_engine/tests/integration/test_e2e_mvp.py` | pytestmark 补 `integration` |
| `Module/workflow_engine/tests/integration/test_workflow_engine_lifecycle.py` | pytestmark 补 `integration` |
| `Module/workflow_engine/tests/helpers/fake_llm.py` | 新建 fake-LLM stub |
| pytest.ini | 无新增 marker（复用 `external`） |

## 8. 验收标准

1. 无网络/无 key 环境可稳定跑全部非 external 测试（`make test-quick`/`test-normal`）。
2. 同一 commit 连跑三次 `make test-quick` 结果一致且无 `coroutine was never awaited` warning。
3. 外部 E2E 仅在 `RUN_EXTERNAL_TESTS=1` 时运行；失败日志不含 prompt/response 明文（日志级别 WARNING 兜底）。
4. fake-LLM 确定性测试覆盖 E3 的 cancel/pause/mailbox 首事件（§4 表）。

## 9. 待记录决策

- **`RUN_EXTERNAL_TESTS=1` 为外部测试显式门禁**（`make test-external` 自动带；直接 pytest 不跑），skip hook 在 `Module/conftest.py`（非 integration/conftest）。
- **fake-LLM 放 `Module/workflow_engine/tests/helpers/fake_llm.py`**（workflow 私有）：不复用 `core/tests/fake_llm.py`——core 版是 `FakeLLMBackend(LLMBackend)`（`chat/chat_stream` 返回预设文本 + 故障注入，走 `Module.llm` 协议），而 workflow 版须对齐 `AsyncOpenAI.chat.completions` 访问面（`tool_calls`/`content`/`usage`），形状不同。
- **外部测试总 timeout 60s（`asyncio.wait_for`）+ LLM 调用次数上限 10 + 日志级别 WARNING**（防挂死、成本失控与 prompt 泄露；数值可按需调整）。
- **gate 无 timeout**（挂起直到取消），`calls` 在 gate 通过后计数——保证 cancel 语义不被 TimeoutError→retry 破坏。
