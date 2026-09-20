# Batch 1 执行记录 —— correctness / durability 修复（2026-09-16）

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 上游：`docs/review/2026-09-16-架构职责分离与分层重构审计.md`（架构审计，未提交）。
> 本批**只**修审计确认的 4 个真实 correctness / durability 缺陷；不做架构重构。
> 4 个独立 commit，`main` 上 `524a04c0 → 14778be5`。

---

## 1. Summary

| Issue | 结论 | 一句话 |
|---|---|---|
| **A** `SystemPromptManager` 截断状态污染 | **Fixed** | 预算降级改为 render-local；梯子不再走 `set_context` 广播 |
| **B** `_handle_interrupt` 丢弃 `TaskResultData` | **Fixed** | durable ack 从"入队"移到"消费"；中断改为原位重投而非丢弃 |
| **C** `DelegationPreparer` provider guard 阻塞启动 | **Fixed**（归类 **Case 2**，并修正审计一处错误断言） | LLM 能力改为注入 `AgentInfra.llm`/`llm_model`；构造不再抛 |
| **D** `core.schema` eager 导入 `openai` | **Fixed**（审计的**价值框定被推翻**，见 §2-D） | `infra.py` 的 SDK 导入收进 `TYPE_CHECKING`，10 增 3 删 |

四项均有 regression test（**新增 42 条**），全部在修复前为红（§6.3）。**无 Reclassified**。
无"确认不应修改"项——但 D 的**理由**被重写（修复保留，价值按实测重述）。

---

## 2. Root Cause

### A. `request-local degradation` 泄漏为 `instance-global mutable state`

`render(budget_tokens=…)` 的截断梯子用 `self.set_context(memory_limit=limit)` 试探
"记忆条数降到 N 装不装得下"。但：

1. `SystemPromptManager.set_context` 是**广播**（打到全部 section），
2. 各 section 的实现是 `kwargs.get(key, default)`——**缺键即复位成默认值**。

于是这次**只带一个 key** 的广播把整片 per-request 上下文清掉。实测 8 个 section /
**12 个字段**被改写，其中 5 个属于 `_PROTECTED_SECTIONS`。两个后果：

- **溢出轮返回的 prompt 比设计更残缺**：保护节只是"没被整节丢弃"，正文却被清成空壳
  （`speaking_style_c6`、`user_name`、`tts_provider`、`stt_emotion` 全空）。
  `_PROTECTED_SECTIONS` 的"永不丢"在**实质层面**没有成立。
- **`_memory_limit` 永久粘住**：它只写不复位（正常 per-request 调用不带该 key），
  一次溢出后停在 `0` → `render()` 直接返回 `""` → **C4 记忆检索此后每轮都不再执行**，
  跨角色、跨会话，直到进程重启。同时梯子第一步就把 `_user_message` 清空，**从未真正
  检索过**，日志那句"记忆条数降为 N 后满足"是假日志。

同根第三处：`Emotion.set_context` 缺 `mood` 时回读
`self._session.character_state.mood`——平时不可达，恰好在截断重试时被走到，把本轮
心情换成**会话里持久化的陈旧值**（审计报告曾把它误判为死分支，Challenger 纠正、
本次实测确认可达）。

### B. durable ack 授予在"入队"，而"入队"不等于"被认领"；且中断会销毁队列

两半独立，缺一不可：

- **半 1**：`drain_mailboxes_once` 用 `consumer_alive = self._state == RunState.RUNNING`
  推断"投进总线就会被消费"，据此落库 `delivered=True`，而 `_persist_task_result`
  在落库成功后**立即** `wal.forget(...)` 清掉本地副本。于是 `bus.post` 返回时，这条
  结果**只**以一个"无人认领的队列元素"形态存在，而库说已投递、WAL 副本已清。
- **半 2**：`_handle_interrupt` `bus.drain()` 后只重投 `StateChangeData`，
  `TaskResultData` 落到分支之外**无声丢弃**。

**机制（不是 FIFO 竞争，而是插队）**：`EventBus` 是 `asyncio.PriorityQueue`，
`Event` 按 `(priority, seq)` 排序且 `priority` 先比；`INTERRUPT=0`、`NORMAL=2`，而
终态结果用**默认 NORMAL** 投递。所以**后到的中断会插到已排队的结果前面**，然后 drain
掉它。生产者三处：WS 聊天取消、语音取消、**WebSocket 断开**（刷新页面即可）。

净效果即禁止态 **`WAL removed ∧ delivered=True ∧ ¬accepted`**——三处皆无，
用户永远收不到"任务完成了"，重启也不补投（`list_pending` 查不到它）。

**触发面比"理论竞态"宽**：`_process_events` 只要处于任一 `await`（例如
`await _handle_state_change(...)`，或 mailbox 轮询里那次上限 2.0s 的 Mongo upsert），
窗口就存在。

### C. 局部 provider 假设被升级为全局启动前置（且路由本来就是错的）

`DelegationPreparer.__init__` 读 `os.environ["DEEPSEEK_API_KEY"]`，缺失即
`raise ValueError`；自建 `AsyncOpenAI(base_url="https://api.deepseek.com")`。
它由 `_start_task_system` 构造，而那是 `AgentLifecycle._STARTUP_STEPS` 的一步 →
启动步骤异常 re-raise 并把 Agent 回滚到 `STOPPED`。

**归类 Case 2**（需要一个 LLM 能力 → 取现有注入对象），依据：

- `_start_task_system` 在构造 preparer **之前**就建好了
  `AgentInfra(llm=self._llm.get_raw_client(), …, llm_model=…)`——所以模块 docstring 里
  "DelegationPreparer 在 WorkflowEngine 创建前运行，无法使用 AgentInfra"**在 HEAD 上
  已经是假的**；engine 的 Planner/Executor/Evaluator 正是用
  `infra.llm.chat.completions.create(model=infra.llm_model, …)` 这个形状调模型。
- **Case 1（删掉 guard 即可）被实测排除，且这纠正了审计的一条断言。** 审计说
  "`AsyncOpenAI(api_key='')` 不会抛，真正阻塞只是手写的那句 raise"。在本机
  `openai 2.53.0` 上**不成立**（本次实测，见 §6.4）：`api_key=''` 与 `None` 均抛
  `OpenAIError: Missing credentials`。所以**只删 guard 是把崩溃往下挪一行**。
- **Case 3 排除**：`WorkflowEngine.start()` 从不校验 LLM；
  `_delegation_preparer: DelegationPreparer | None` 与 `is None` 处的门控就是**既有**
  的"不可用"表示。可选/必需关系未改。
- **路由错误是独立缺陷**：6 个 provider 共用一个 `OpenAICompatBackend`，
  `raw_client` 恒指向**该 provider**；硬编码 `api.deepseek.com` 意味着非 DeepSeek
  部署的委派流量被发去 DeepSeek 端点。

**实测到的真实触发条件（比"所有非 DeepSeek 部署"更窄，值得记准）**：
`scripts/setup.sh` 会 `cp .env.example .env`，而 `.env.example:12` 的
`DEEPSEEK_API_KEY=sk-your-key-here` **未注释**。所以默认装配流程下该变量**恒非空**，
guard 不会触发——此时症状不是启动失败，而是**静默错路由**（拿占位 key 打
`api.deepseek.com`，运行时 401）。启动失败发生在用户**注释/删除**这个用不到的
DeepSeek key 时（对配了 OpenAI/Ollama 的人是自然操作），而 Ollama 更极端：
`providers.yaml` 里 `ollama.env_key: ""`、`openai_compat.py:86` 在空 key 时替换成占位值
`"ollama"`——它是**真正不需要 key** 的部署，却被硬挡住。

### D. 唯一装配点的 eager re-export × 子模块导入会先执行父包

真实链（实测，逐跳 file:line）：

```
core/schema/__init__.py:17   from core.schema.types import (…)
core/schema/types.py:7       from Module.workflow_contract.schema import SessionId
  ↳ 导入**子模块**会先执行父包 → Module/workflow_contract/__init__.py
Module/workflow_contract/__init__.py:32   from Module.workflow_contract.infra import AgentInfra
Module/workflow_contract/infra.py:17      from openai import AsyncOpenAI   ← 运行期导入
openai/__init__.py:12 → openai/_client.py:9   import httpx
```

审计点对了 hunk 4→5，**漏了关键性质**：进入装配点不是有人显式 `import
Module.workflow_contract`，而是 Python 执行**父包**的隐式行为——那份 eager re-export
收的是**每个子模块消费者**的税。

**审计的价值框定被实测推翻**：审计称本项价值是"让 CLI/生成器不必装 provider SDK"。
实测 `frontend_contract/` 生产代码**不**导入 `core.*`/`Module.*`，跑真实门禁命令
`python -m frontend_contract --check` 与 `gen_contract_frames.py --check` 时
`openai`/`core.schema` 命中数均为 **0**。真实受益方是 `core.schema` 导入图的消费者
（纯数据模型路径、debug/seed 脚本，以及这条不变量本身）。**修复正确且便宜，但影响面
按实测重述**——修复本身保留。

---

## 3. Changes

**18 个文件，+2346 / −116**；其中生产代码 9 个、测试 9 个（5 新增 + 4 修改）。
新增测试 **42 条**。

### Issue A（4 文件）

| 文件 | 关键逻辑 |
|---|---|
| `Module/system_prompt/protocol.py` | `PromptSection` 新增 opt-in 契约 `render_override_keys: frozenset[str] = frozenset()` + `override_for_render(key, value) -> Callable[[], None] | None`（默认返回 `None` = 什么都不接受，fail-safe）；`set_context` docstring 明确"缺键即复位"的广播语义 |
| `Module/system_prompt/sections/memory.py` | 声明 `render_override_keys = frozenset({"memory_limit"})` 并返回恢复闭包；`set_context(memory_limit=…)` 语义**保持不变** |
| `Module/system_prompt/system_prompt_manager.py` | `render()` 用 `_apply_render_overrides` + `try/finally` **逆序**恢复；**不再调用 `set_context`**；日志改为如实三分（上限 0 / 真检索到记忆 / 该节本轮为空） |
| `Module/system_prompt/tests/test_render_local_budget.py` | **新增 12 条**：A1–A6 + 全字段快照审计 + "`render()` 绝不调用 `set_context`"结构守卫 |

### Issue B（7 文件）

| 文件 | 关键逻辑 |
|---|---|
| `core/agent/_lifecycle_task_system.py` | 删 `consumer_alive`；落库**无条件** `delivered=False`；删掉入队处 `mark_delivered`；`_redeliver_pending_task_results` 只 post 不 mark；模块顶部写入**状态词汇与禁止迁移**（`persisted/queued/accepted/consumed/delivered/WAL removed`） |
| `core/agent/agent.py` | `_handle_task_result` → `async`，在 `_turns.spawn(...)` **成功后**才 `mark_delivered`（所有权转移 = ack 时点）；spawn 抛异常时刻意不标记；`_handle_interrupt` 对 `TaskResultData` **原位重投**（保 `priority`/`type`），drain 循环内同步完成 ⇒ 无丢失窗口 |
| `core/agent/tests/test_task_result_interrupt_delivery.py` | **新增 8 条**：B1–B5 + "INTERRUPT 越过已排队 NORMAL 事件"机制钉 |
| `core/agent/tests/test_task_result_durability.py` | 2 条用例**语义反演**（断言 `delivered is True`→`is False`、`trace` 去掉 `mark`）+ 重命名 + docstring；关停/WAL/硬杀/次序用例**一行未动** |
| `core/storage/background_task_repo.py` | 仅 docstring（"判据不是 `_state == RUNNING`" + 禁止态 + 指向状态机） |
| `Module/workflow_contract/infra.py` | 仅 3 行 docstring（`background_task_results` 字段语义） |
| `core/storage/tests/test_background_task_repo.py` | 仅 1 行注释 |

### Issue C（6 文件）

| 文件 | 关键逻辑 |
|---|---|
| `core/cognitive/delegation.py` | 构造签名 `__init__(infra: "AgentInfra | None" = None)`，只取 `infra.llm`/`infra.llm_model`；**不读 env、不自建 client、构造永不抛**；`prepare()` 无 client 时抛显式 `DelegationUnavailableError`；模型名改读 `self._model`（消掉与 `AgentInfra.llm_model` 重复的 `os.getenv`）；**`aclose()` 删除**（借来的 client，所有者是 `LLMAdapter`） |
| `core/agent/_lifecycle_task_system.py` | `raw_llm` 提到局部；`None` → `_delegation_preparer = None` + 明确 warning（既有不可用表示），否则 `DelegationPreparer(_ts_infra)`；`_stop_task_system` 删除 `aclose()` 调用（否则每次停机 `AttributeError` → 假 warning，且会掐掉后续停机步骤仍在用的连接池） |
| `core/cognitive/tools.py` | `_handle_submit_task` 包住 `prepare()`：显式捕 `DelegationUnavailableError` + provider/解析失败，均 `logger.warning(..., exc_info=True)` 后返回 `ToolResult.fail(…后台任务系统暂不可用…)`——`tool_phase.py` 是裸 `await`，链上无 try/except |
| `core/cognitive/tests/test_delegation_provider_agnostic.py` | **新增 13 条**：C1–C4 + 2 条 handler 边界 + `TASK_SYSTEM_MODEL` 仍生效 |
| `core/cognitive/tests/test_delegation_preparer.py` | fixture 由 `os.environ["DEEPSEEK_API_KEY"]="test-key"` 改为注入 `AgentInfra(llm=MagicMock(), …, llm_model="test-model")` |
| `Module/workflow_engine/tests/integration/test_e2e_mvp.py` | 4 处 `DelegationPreparer()` 抽成 `_build_delegation_preparer()`（external 用例，按旧行为等价物自建 client 但经 `AgentInfra` 显式注入） |

### Issue D（3 文件）

| 文件 | 关键逻辑 |
|---|---|
| `Module/workflow_contract/infra.py` | `import httpx` / `from openai import AsyncOpenAI` 移入**既有** `TYPE_CHECKING` 块（同 `pymilvus` 先例）；`__init__.py` **逐字节不变** |
| `core/schema/tests/test_import_isolation.py` | **新增 2 条**：D1 全新子进程断言 provider SDK 不在 `sys.modules`（非零退出即失败，不 skip）；D4 只记录耗时、不设墙钟阈值 |
| `Module/workflow_contract/tests/test_public_surface.py` | **新增 7 条**：D2 冻结 22 个公共名字 + 同一性 + `import *`；D3 真实 client 装配 + 把 PEP 563 前提本身钉住（`fields()["llm"].type == "AsyncOpenAI"` 以字符串成立且 `openai` 未加载） |

---

## 4. Invariants Added

1. **Prompt budget degradation is render-local.**
   一次 `render` 的预算降级只能影响本次 render，不得永久修改下一次 render 的 prompt
   上下文。机制：opt-in 覆盖 + `finally` 逆序恢复；`render()` 不再使用破坏性的广播。
2. **Task results cannot be lost after durable acknowledgement.**
   每一刻 `TaskResultData` 至少在**一处**是活的：WAL/仓储、EventBus 队列、消费方。
   禁止态 **`WAL removed ∧ delivered=True ∧ ¬accepted`**；durable ack 的唯一授予点是
   消费侧 spawn 成功之后（语义 = at-least-once）。
3. **Delegation does not assume DeepSeek.**
   `DelegationPreparer` 不读任何 provider 环境变量、不自建 client、构造永不抛；
   LLM 可用性由**配置的运行时**决定；拿不到可复用 client 时用既有的
   `_delegation_preparer is None` 显式降级，启动不崩；可选/必需关系不变。
4. **`core.schema` does not import provider SDKs.**
   `import core.schema` ⇒ `"openai" not in sys.modules`（`httpx`/`httpcore` 同理）。

---

## 5. Commits

```
d5965c79  fix(prompt): isolate budget truncation state per render
b6dba245  fix(agent): preserve durable task results across interrupts
bcadcc05  fix(workflow): remove provider-specific startup guard
14778be5  perf(schema): avoid eager openai import
```

每个 commit 单一主题、测试随修复同 commit、无格式化、无无关 rename、无架构迁移。

**`core/agent/_lifecycle_task_system.py` 被 B 与 C 同时改到，`Module/workflow_contract/infra.py`
被 B 与 D 同时改到** —— 整文件 `git add` 会让某个 commit 带上别人的改动、使逐 commit
bisect 失真。故用 `tmp/batch1/split_hunks.py` 按 hunk 择取：B 的 commit 只收
`_lifecycle_task_system.py` 的 hunk {0,1,4,5,6,7,8} 与 `infra.py` 的 hunk {1}；
C/D 各自的 commit 用 `git add`（彼时残留恰为它们自己的 hunk）。已验证每个 commit 的
staged 内容：B 的 commit 里 C 的标记数 = 0、`aclose` 仍在；C 的 commit 里 `raw_llm`
存在、`aclose` = 0、B 的状态机仍在；且都通过 AST 解析。

---

## 6. Test Results

### 6.1 最终 HEAD（14778be5）标准门禁 —— 全绿

| command | passed | failed | skipped | 其他 | duration |
|---|---|---|---|---|---|
| `make lint` | — | 0 | — | All checks passed | 秒级 |
| `make pyright` | — | **0 errors / 0 warnings / 0 informations** | — | — | ~1min |
| `make check-arch` | — | 0 | — | R1/R2/R3/R4/R6 通过 | 秒级 |
| `make check-types` | — | 0 | — | `frontend_contract --check` + `gen_contract_frames.py --check` + `check_error_codes`（54 码一致） | 秒级 |
| `make check-timeouts` | — | 0 | — | R1–R4 通过（叶子 8 条） | 秒级 |
| `make check-contract` | — | 0 | — | 23 预留 / 4 已知缺口在册 | 秒级 |
| `make check-test-sets` | — | 0 | — | `\|Quick ∪ remainder\| = 5738 == \|Normal\|`，收集 5785 | ~35s |
| `make check-ci-isolation` | — | 0 | — | 3 个 workflow 通过 | 秒级 |
| **`make test-quick`** | **5732** | **0** | **2** | 1 xfailed | **74.09s** |
| **`make test-normal`** | **5735** | **0** | **2** | 1 xfailed | **72.35s** |

### 6.2 逐 commit 隔离验证（在对应 sha 的独立 worktree 里跑）

| commit | 跑的套件 | 结果 |
|---|---|---|
| `d5965c79` (A) | `Module/system_prompt/tests/` + `core/prompt/tests/` | **183 passed** |
| `b6dba245` (B) | `core/agent/tests/` + `core/storage/tests/` | **866 passed** |
| `b6dba245` (B) | `core/cognitive/tests/`（该 commit 下 C 未落地） | **255 passed** |
| `bcadcc05` (C) | `core/cognitive/tests/` | **268 passed** |
| `14778be5` (D) | `core/schema/tests/` + `Module/workflow_contract/tests/` + `frontend_contract/tests/` | **399 passed** |

跨文件一致性另测：`Module/workflow_engine/tests/` **384 passed + 7 skipped**（7 = 无
`DEEPSEEK_API_KEY` 的 external opt-in）、`core/tests/` **303 passed + 2 skipped**、
`core/monitoring/tests/` **25 passed**。

### 6.3 可证伪性：新用例在**修复前**确实为红（关键证据）

在纯净 `524a04c0` 的独立 worktree 上跑本批新增用例：

| Issue | 方式 | 修复前 |
|---|---|---|
| A | 新增 12 条 | **12 failed**（行为断言，例如 `截断梯子改写了 section 字段：{…12 项…}`——不是"API 不存在"） |
| B | 新增 8 条 | **6 failed, 2 passed**（2 绿是有意的"两侧都必须过"钉子：总线排序事实 + spawn 失败不标记）；红灯如 `出现禁止态：WAL 副本已清 + 库里 delivered=True + 消费方从未取得所有权` |
| C | 把 C1 的**用例体**复刻成 pytest 用例跑在 `b6dba245`（pre-C）代码上 | **1 failed**：`ValueError: DEEPSEEK_API_KEY 环境变量未设置…` |
| D | 新增 D1 | **1 failed**：`import core.schema 拖入了 provider SDK：['httpx','openai']（共 1284 个模块）` |

> C 的这一条是主 Agent 复核时补做的。**过程中先出现了一次假线索**：一段顺序不对的探针
> （先 pop key、再 `import core.cognitive.delegation`）显示 pre-fix 也能构造成功，看似
> C1 是"假绿"。用与用例**完全相同的顺序**重跑后确认 C1 为真红——因为该用例在**收集期**
> 就已导入 `delegation`，`monkeypatch.delenv` 之后没有任何东西再触发 `.env` 重载。
> 该假线索顺带暴露了一个真实隐患，见 §8-9。

### 6.4 执行探针（修复前 → 修复后）

**A**（`tmp/batch1/probe_a_context_clobber.py`）：

| | 前 | 后 |
|---|---|---|
| 被梯子改写的 section 字段 | 12 | **0** |
| 溢出轮里保护节正文（`user_name`/`speaking_style_c6`） | 被清空 | **保留** |
| `Memory._memory_limit`（溢出轮后） | `0`（粘住） | `None` |
| 梯子期间 LTM `top_k` | `[5, 5]`（从未检索） | `[5, 5, 3, 2, 1]` |
| 下一轮正常预算 `top_k` | `[]` | `[5]` |
| 下一轮 prompt == 首轮 prompt | `False` | **`True`** |
| 正常路径 prompt（md5 / len） | `201dfe0d…` / 3143 | **相同**（逐字节不变） |

**B**（`tmp/batch1/probe_b_interrupt_loss.py`，纯净 HEAD）：排空后
`pending_count=0`、`delivered=True`、`wal.forgotten=[['wf-1']]`、注册表 `[]` ⇒
**「三处都没有」= True**；同一次运行证明 `bus.next()` 在已有 NORMAL 事件排队时先返回
`InterruptData`（插队机制）。

**C**（实测，`openai 2.53.0`）：`AsyncOpenAI(api_key='')` 与 `api_key=None` 均
**抛** `OpenAIError: Missing credentials`；而
`AsyncOpenAI(api_key='sk-…', base_url='https://api.deepseek.com')` **构造成功**——
即审计"不会抛"的断言不成立。另：无 `DEEPSEEK_API_KEY` 时 pre-fix
`DelegationPreparer()` 抛 `ValueError`，post-fix 构造成功且 `hasattr(d, "aclose") is False`。

**D**：`import core.schema` **1313 ms → 537 ms（−59%）**；加载模块数
**1284 → 338**；`openai`/`httpx` in `sys.modules` **True/True → False/False**。
`Module.workflow_contract` 约 1270 ms → 447 ms。

---

## 7. Remaining Risks

只列真实剩余风险（不求全）：

1. **at-least-once 的重复宣告窗口（有意选的取舍）**：在 `_turns.spawn(...)` 成功与
   `mark_delivered` 落库之间若崩溃 / 该写失败或超时（2.0s `_TASK_RESULT_STORE_TIMEOUT`）
   / Mongo 离线，下次启动会**重复宣告一次**同一终态结果。选它是因为 at-most-once
   （ack 早于消费）的失败模式是**静默永久丢失**——用户永远等不到且日志干净，
   而重复告知用户看得穿。
2. **委派仍未真正 provider 可移植**：`_call_llm_with_usage` 仍带 DeepSeek 专属参数
   （`reasoning_effort="high"`、`extra_body={"thinking": {"type": "enabled"}}`）与
   `deepseek-v4-pro` 默认模型名。非 DeepSeek 部署现在**启动成功**、失败**显式降级**
   （`submit_task` 返回"暂不可用"而不是莫名死回合），但委派本身仍不可用。
3. **A 的梯子在溢出轮现在真的会检索**（最多 4 次 LTM 搜索：5→3→2→1）——这是**文档化
   的原设计首次真正执行**，修复前因 `_user_message` 被清空而"零检索即收敛"。代价是
   小上下文 provider 上的额外负载：`ollama`（8192 → 子预算 1843，而保护节本身已 ≈2451）
   **每轮都会触发溢出** ⇒ 每轮最多 4 次 LTM 检索。这是**新暴露的既有配置问题**，
   不是本次引入的回归，但值得单独决策（子预算份额或该 provider 的窗口）。
4. **durable ack 现在是事件消费者循环内的一次 await Mongo 写（≤2.0s）**：慢 Mongo 会
   拖慢事件派发。已由 `_TASK_RESULT_STORE_TIMEOUT` 界定，且 mailbox 路径本来就有这个
   性质，但它是本次把 ack 后移的**直接代价**。
5. **C4（重启）没有端到端覆盖**：完整 `Agent.start()` 在单测里不现实（Mongo/Milvus/
   角色/WS），用例驱动的是最接近的可达接缝并按生命周期顺序执行，**这一点已写在用例
   docstring 里**，没有假装覆盖。
6. **`Agent.start()` 的真实装配路径在本批仍未被任何测试端到端跑过**（B 与 C 都只是
   分别覆盖各自步骤）。这是既有的测试空档，不是本批引入。
7. **审计遗留的两处"未闭环"仍未闭环**：`_DROPPABLE_SECTIONS` 丢弃循环里的死判据
   （见 §8-1）与 prompt 预算**只覆盖主生成路径**（reflector/consolidator/vision/
   delegation/workflow 的 8 条路径仍无预算）。本批未扩大范围。
8. **本批纠正了审计的两处错误断言**（`AsyncOpenAI(api_key='')` 行为；D 的价值框定），
   意味着那份审计的其余"实测事实"也应当按同样标准复核——其中**尚未复核的**部分仍是
   未知风险，不应默认可信。

---

## 8. Follow-up Findings

**本批发现但未修改的架构债。禁止偷偷修——以下全部只在报告里。**

### A 域
1. **`render()` 丢弃循环里的死判据**：`rendered = [(n,t) for n,t in rendered if n != name]`
   之后紧跟 `if any(n == name for n, _t in rendered): continue` **永假**，于是 `dropped`
   会无脑累积全部 5 个名字，warning 可能列出根本不在 prompt 里的节。（仅日志失真。）
2. **`MemorySection.set_context(memory_limit=…)` 仍是"只写不复位"的粘性 API**：本批后
   已无生产调用方，但未来任何调用方都会重新引入"永久 0"的隐患。候选：删除该 key 或
   收窄为 render override 单一入口。
3. **prompt 字节依赖墙钟**：`SensorySection`/`Memory` 的时间戳让"同上下文 ⇒ 同 prompt"
   的断言必须冻结时钟（本次已让一条用例因此偶发红）。

### B 域
4. **`_persist_task_result(delivered=…)` 与仓储写入期 `delivered=True` 在生产上已成死
   参数**（无调用方传 True）。候选清理：删参或收窄成 `Literal[False]`。
5. **`core/tools/reminder/plugin.py` 直接 post `TaskResultData` 但库里无对应行** ⇒
   `mark_delivered` 变成一次无匹配的 `update_one`（无害，但每次提醒多一次 Mongo 往返）。
6. **`_DROPPABLE_SECTIONS` 与 `terminal_payload` 之外，投递状态机只写在注释里**：
   没有把"禁止态"做成可执行守卫（新用例钉住了它，但守卫在测试侧而非代码侧）。

### C 域
7. **`TASK_SYSTEM_MODEL` 从未被设置**：`config.yaml:14` 选的是
   `deepseek/deepseek-v4-flash`，但没人设这个 env ⇒ 后台任务恒用 `deepseek-v4-pro`。
   本批只是把默认值来源**统一**到 `AgentInfra.llm_model`，效果未改。修它要决定
   "后台任务是否应跟随配置的模型"，是行为变更。
8. **`DelegationResult.token_usage` 只写不读**（死写入）。
9. **测试卫生**：`core/cognitive/tests/test_delegation_preparer.py:15-26` 在 import 期把
   真实项目 `.env` 灌进 `os.environ`。
10. **`core/cognitive/delegation.py:20` 的 `import re` 是既有未使用导入**（见 §8-15）。
11. **文档已失真**：`.env.example:12` 未注释的 `DEEPSEEK_API_KEY=sk-your-key-here` 与
    `scripts/setup.sh:122` 打印的"`DEEPSEEK_API_KEY (必需)`"。后者此前只是**因为这个 bug
    才碰巧为真**。准确措辞应是"与 `config.yaml::llm.model` 所选 provider 对应的 key"。
    另外 `setup.sh` 的 `cp .env.example .env` 会让占位 key **恒非空**，从而把本缺陷的
    症状从"启动失败"改成"静默错路由"——这也说明该缺陷长期没被发现。

### D 域
12. **下一个成本中心**：修复后 `import core.schema` 的 537 ms 里约 390 ms 在
    `Module.workflow_contract.schema`，其中约 270 ms 是
    `Module.tool.tool_descriptor` → `Module.tool` → `Module.tool.manager` →
    `Module.output.manager` → eager `frontend_contract.rest/debug/ws_message`（pydantic
    模型构造，不是导入机制）。同一个病往下一跳。

### 主 Agent 复核时新发现
13. **`ruff.toml` 只 select 了 `TID252/ANN001/ANN002/ANN003`——F401（未使用导入）不在
    门禁内**。实测生产代码里 `ruff check --select F401` 有 **334 处**可自动修复的未使用
    导入。`make lint` 全绿并不能说明没有死导入。（本批刻意没动其中任何一处。）
14. **`import` 的进程级副作用会重载 `.env`**：`Module/config/loader.py:153/171` 调
    `EnvManager.ensure_dotenv(self.env_path)`，而 `core.cognitive.delegation` 的传递导入
    会触发一次 config 加载 ⇒ `os.environ` 被 `.env` 补回。含义：任何"先 `monkeypatch.delenv`
    再触发一次新 config 加载"的用例都可能**静默拿回** `.env` 的值而变成假绿——这正是
    本批复核 C1 时遇到的假线索。建议的修法是让 `ensure_dotenv` 只在进程入口
    （`main_runner`）而非库代码里调用，或给它加"只跑一次/可禁用"的语义；**本批未改**。
15. **CI 实际有 3 个 workflow**（`ci.yml` / `contract-frames.yml` / `nightly.yml`），
    审计说的"两个"不准。前两个 `push:[main]`，第三个只有 `workflow_dispatch`（纯手工）。
    **仍然没有任何 `pull_request` 触发** ⇒ "无 PR 评审网"这个结论成立。
16. **仓库里有一处历史遗留的可清理 worktree 元数据**：
    `/tmp/elysia-preA`（`48ee925d`，目录已不存在，`git worktree list` 标 `prunable`）。
    非本批产生，未动；`git worktree prune` 可清。

---

## 9. Review 阶段（主 Agent 独立复核）

按要求逐项回答：

- **Issue A：是否只是"每轮手动 reset"，异常路径仍会污染？**
  不是。修法是**机制**而非约定：覆盖是 opt-in 的（未声明 `render_override_keys` 的
  section 一个字段都不会被碰），恢复在 `try/finally` 里且**逆序**执行。A5 专门注入一个
  在梯子中途抛异常的 section，断言异常仍向上传播且 `_memory_limit` 已还原。另有全字段
  快照断言与"`render()` 绝不调用 `set_context`"的结构守卫。

- **Issue B：是否修了丢失却引入重复投递？**
  引入了**有界的**重复：窗口精确为"spawn 成功 ↔ mark 落库"之间，且只在崩溃/写失败/
  Mongo 离线时发生，后果是下次启动**多宣告一次**。这是显式选择的 at-least-once，
  已在代码注释与 commit message 写明。**进程内不会重复**：mailbox 条目在投递后即 pop，
  drain 循环 per-task `break`，重投是**搬移不是复制**（`drain()` 已取出原件）；
  `TestNoDuplicateAnnouncement` 断言恰好一次宣告。

- **Issue C：是否只是删掉 guard，把错误推迟到更难理解的位置？**
  不是——这一条是复核中**唯一要求返工**的地方。C 初版只保证"构造不抛"，但
  `_handle_submit_task` 的 `prepare()` 失败会沿 `tool_phase.py` 的裸 `await` 冒进已
  spawn 的认知回合（表现为"这一轮生成莫名死掉"，真实原因只剩任务异常日志）——正是
  "推迟到更难理解的位置"。已要求并确认补上调用边界：失败收敛成 `ToolResult.fail(…
  后台任务系统暂不可用…)`，且新增 2 条 handler 边界用例（provider 抛错 → 返回失败
  `ToolResult`、异常不外泄）。

- **Issue D：是否为了 import latency 损害了 public API？**
  没有。修法是单文件 10 增 3 删，`Module/workflow_contract/__init__.py` **逐字节不变**
  ⇒ 22 个 `__all__` 名字、`dir()`、`hasattr`、`from … import *`、子模块属性访问全部
  原样。这两个名字在该文件里**只**出现在注解中（已 grep 全仓确认无人依赖从 `infra`
  重导出它们），且文件头有 `from __future__ import annotations`。D3 还把这个
  **PEP 563 前提本身**钉成断言——哪天有人去掉 future import，用例会红。

### 主 Agent 额外做的独立验证（不只读 subagent 报告）

- 对 4 个 issue 各写了**执行探针**并在纯净 HEAD 上跑出**修复前**证据（A 的 12 字段改写、
  B 的"三处都没有"、C 的 `ValueError`、D 的 1284 模块）。
- **逐 commit 建独立 worktree 验证**：确认每个 commit 只含自己的文件/hunk，且在该 commit
  的树上测试全绿（§6.2）。
- **逐 commit 检查归属**：B 的 commit 里 C 的标记数 = 0、`aclose` 仍在；C 的 commit 里
  `raw_llm` 存在、`aclose` = 0；D 的 commit 里 `infra.py` 已无运行期 SDK 导入。
- 复核 C 的每一条"修复前事实"，**其中一条证伪**（`AsyncOpenAI(api_key='')` 行为），
  **并据此确认 Case 1 确实被排除**（否则我们就只是把崩溃挪了一行）。
- 复核 C1 是否假绿，用**同顺序**的 pytest 复刻在 pre-C 代码上确认它真红（§6.3）。
- 复核 B 对既有测试的修改是否"反演而非削弱"（逐条读完 `test_task_result_durability.py`
  的 diff）：断言被**反演**、用例被**重命名**、无删除/无 skip，且正向的一半由新文件补齐。
- 复核 D 是否越界：`scripts/` 未改、`__init__.py` 未改、`frontend_contract` 未改。

---

## 10. 完成标准对照

| 标准 | 状态 |
|---|---|
| 4 个 issue 都经过真实代码复核 | ✅ 主 Agent 逐条读代码 + 探针 + 逐 commit worktree 验证 |
| 每个真实 bug 都有 regression test | ✅ 新增 42 条；A 12/12、B 6/8、C 1/1、D 1/1 在修复前为红 |
| 每个修改独立 commit | ✅ 4 个；跨 issue 共享的两个文件按 hunk 精确归属 |
| 无无关生产代码修改 | ✅ 生产改动仅 9 文件，全部在四个 issue 的直接路径上 |
| 标准门禁通过 | ✅ lint / pyright(0) / check-arch / check-types / check-timeouts / check-contract / check-test-sets / check-ci-isolation / test-quick / test-normal |
| `git status` 最终干净 | ✅ 仅剩 1 个明确说明的报告文件（见下） |
| 最终报告含 commit + tests + 未处理风险 | ✅ 本文件 |

**`git status` 的说明**：唯一未跟踪项是
`docs/review/2026-09-16-架构职责分离与分层重构审计.md`——**上一轮的架构审计报告**，
本批开始前就已存在且未提交，本批未改动它。本执行记录按仓库 `docs/review/` 惯例落档。

**报告文件**：`docs/review/2026-09-16-Batch1-correctness-durability-执行记录.md`（本文件）。
