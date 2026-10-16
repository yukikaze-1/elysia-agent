# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Elysia/Agent is a Python **3.12** AI companion application (authority: `environment.yml` + `pyrightconfig.json`; preflight 的 `>=3.11` 只是下界判据 —— 见 docs/operations.md §11). The codebase has three top-level packages:

- **`Module/`** — backend-agnostic library for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document databases, output channels, config, logging, system prompt engine, tool framework.
- **`core/`** — the Elysia Agent application: `Agent`, `CognitiveLoop`, `CharacterEngine`, `CharacterReflector`, `MemoryConsolidator`, `MemoryReflector`, `SessionMemory`, `LongTermMemory`, `IdleMonitor`, task scheduler, debug info, character config.
- **`frontend_contract/`** — 前后端契约单源（唯一类型来源 + 生成器 + 门禁）：可执行 Pydantic 契约模型（WS/REST/Debug）→ `gen_ts`/`gen_schema` 发射器 → `frontend/src/types/contract.generated.ts`（TS 只读快照）+ `contract.schema.json` + `contract.raw.ts`（Electron 机器契约，批次 9B）。后端发送点构造契约模型，`make check-types` 做漂移门禁。零依赖（仅 pydantic + stdlib）。

Plain Python（无 `pyproject.toml` 等构建系统）。前端 Vue 3 + Vite + TypeScript（无框架 → Vite 构建步骤）。`Makefile` 提供常用任务入口。

## Running tests

### 分层运行

**档位命令与实测数字的唯一权威是 [`docs/testing.md`](docs/testing.md)**
（含并行档位曲线、teardown 历史、marker 纪律的完整说明）。这里只留编码时的最小入口：

```bash
make test-quick      # 日常：unit + service（-n 8 --dist loadfile）
make test-normal     # 提交前：not slow and not external（**真正离线**；CI 只跑这一遍）
make test-full       # 含 slow（无界，慢；无自动执行点）
make test-external   # 真实 LLM/DB（RUN_EXTERNAL_TESTS=1，刻意 opt-in）
make test-real-mongo # 真实 MongoDB 层（先 docker compose -f docker-compose.test.yml up -d）
```

> **提交前要跑什么**：见本文件「Before you finish non-trivial work」——
> 那是 `docs/testing.md` §Required pre-push verification 的短摘要，**不是**完整清单。


### 按模块运行

```bash
# Personality engine tests
python -m pytest core/personality/tests/ -v

# System prompt tests
python -m pytest Module/system_prompt/tests/ -v

# Core tests
python -m pytest core/tests/ -v

# WorkflowEngine tests
python -m pytest Module/workflow_engine/tests/ -v

# Agent lifecycle tests
python -m pytest core/agent/tests/ -v

# Config tests
python -m pytest core/config/tests/ -v

# Monitoring tests
python -m pytest core/monitoring/tests/ -v

# Task tests
python -m pytest core/tool_runtime/tests/ -v

# 其他模块
python -m pytest Module/llm/tests/ -v
python -m pytest Module/tts/tests/ -v
python -m pytest Module/stt/tests/ -v
python -m pytest Module/embedding/tests/ -v
python -m pytest Module/output/tests/ -v
python -m pytest Module/config/tests/ -v
python -m pytest Module/system_prompt/tests/ -v
python -m pytest Module/database/vectordb/tests/ -v
python -m pytest Module/database/documentdb/tests/ -v
python -m pytest Module/tool/tests/ -v
python -m pytest frontend_contract/tests/ -v
```

### pytest markers

| marker | 标准 | 日常使用 |
|--------|------|----------|
| `unit` | 纯函数/类，无 I/O，无 Agent | `make test-quick`（`-m "unit or service"`） |
| `service` | 单模块 + mock/fake，不连真实依赖 | `make test-quick` |
| `integration` | 需真实 DB 或外部 API（本仓的 integration 用例**全部**与 slow/external 同标 ⇒ `-m "integration and not slow and not external"` 当前为空集） | `make test-full`（它含 slow 层）；**不在 Normal**，也不在 `test-external`（那些是 external-only） |
| `slow` | 完整管线或 >1s | `make test-full` 才跑 |
| `external` | 真实 LLM/DB 凭据（opt-in） | `make test-external`（须 `RUN_EXTERNAL_TESTS=1`）；**`make test-normal` 排除它** |
| `real_mongo` | 真实 MongoDB 测试层（S2-R6-1，非凭据：走容器） | `make test-real-mongo`（须 `RUN_REAL_MONGO_TESTS=1` + `ELYSIA_TEST_MONGODB_URI`；`tests/real_mongo/`）。**CI 为 push-only + self-hosted**（job 内自起 `docker-compose.test.yml`，不用按分钟计费的 GitHub-hosted `services:`）；PR 侧不跑 |

marker 纪律由根 `conftest.py` 的收集钩子强制：**每个测试必须有分层 marker**，且
**不得同时带 `(unit|service)` 与 `(slow|integration|external)`**（否则
`Quick ∪ remainder == Normal` 的互斥性被破坏）。违规 → 收集阶段报错，失败原因显式打 stderr。**退出码不是契约**：串行下 4，`-n N` 下 xdist 会把它转成 `INTERNALERROR`（退出码 3）——所以原因必须自己打出来，否则控制台只剩一个无辜测试的 nodeid。

## Before you finish non-trivial work

> **`docs/testing.md` §Required pre-push verification 是"提交前该跑什么"的唯一权威。**
> 它从 `Makefile` 与 CI workflow 反推而来。不要凭记忆跑一个子集，也不要把本文当成清单。

最小入口（完整清单见上面那条链接）：

```bash
make lint && make pyright && make check-types && make check-arch && make check-docs && make test-normal
cd frontend && npm ci && npm run build && npm test
```

## Where current truth lives（读之前先认权威）

| 想知道 | 读 |
|---|---|
| 当前架构 | [`docs/architecture.md`](docs/architecture.md)（系统级唯一权威） |
| 当前不变量 | [`docs/architecture/invariants.md`](docs/architecture/invariants.md) |
| 验证要求 | [`docs/testing.md`](docs/testing.md) |
| CI 语义 | [`docs/ci.md`](docs/ci.md) |
| 运维/配置/环境变量 | [`docs/operations.md`](docs/operations.md) |
| 待办 | [`todo.md`](todo.md) |
| 已定的事 | [`docs/decisions.md`](docs/decisions.md) |
| 文档导航 | [`docs/README.md`](docs/README.md) |

> ⚠️ **`docs/review/**`、`docs/archive/**`、`docs/superpowers/**` 里的报告与规格是历史证据，
> 不是当前 authority。** 它们描述的是**当时**的系统与结论（有些后来被推翻）。
> 冲突时**先验证 current code**，再以 `docs/architecture.md` 作为系统级表示——
> **不要**按"文件日期更新"裁决。

## Architecture

### Module/ layer (Protocol-Registry-Backend pattern)

```
Module/<service>/
├── protocol.py    # ABC (LLMBackend, STTBackend, TTSBackend, etc.)
├── registry.py    # Factory + _BACKENDS dict ("provider/model" → backend class)
├── <backend>.py   # Concrete implementation(s)
├── config.py      # Dataclass config using Module.config.ConfigField
├── __init__.py    # Re-exports Backend ABC, Factory, Config
└── __main__.py    # Integration smoke test
```

### core/ layer (Application)

**Coding Agent 只需要 ownership map，不需要完整文件树。** 逐文件/逐模块的权威是
[`core/README.md`](core/README.md) 的子系统表 + 各子包自己的 README；
**系统级架构**（层、数据流、组装、并发全景）权威是 [`docs/architecture.md`](docs/architecture.md)。

| 子包 | 归谁 | 一句话职责 |
|---|---|---|
| `core/agent/` | 组合根 | `Agent` = `AgentLifecycle` + `AgentServer` mixin；**46 个同名 `@property`** 是跨模块访问的唯一入口（完整枚举见 `core/README.md`，由门禁钉住） |
| `core/cognitive/` | 回合编排 | ReAct 主循环 + 流式/工具/收口三个 mixin + generation 上下文与注册表 |
| `core/status/` | 状态接口 | 只读 `AgentStatus` + 对内 `CognitivePhaseController`；底层 `AgentFSM` 不对外暴露 |
| `core/adapter/` | 后端封装 | 把 `Module/` 后端包成 core 可用的 LLM/TTS/Tool 适配器（含 tool_call 归一） |
| `core/prompt/` | prompt 组装 | `PromptAssembler`（12 section）→ `MessageBuilder`（历史 + 预算） |
| `core/character/` + `core/personality/` | 角色域 | Definition/Revision/Instance + RuntimeRegistry；四层性格（L1→L2→L3→L4） |
| `core/profile/` | 用户身份 | `UserIdentity`（Mongo 权威 + CAS） |
| `core/memory/` | 记忆 | Session 缓存池 / LTM（Milvus）/ 沉淀 / 反思 / 各类 pending journal |
| `core/storage/` | 持久化 | 各 Repository + schema 漂移自检 + 附件 GC + 秘密存储。**集合表权威在 [`docs/database-storage.md`](docs/database-storage.md)** |
| `core/input/` | 输入层 | WS + HTTP：`server`（纯装配）/ `ws_handler` + `ws_handlers/` / 各 `*_routes` / `debug_routes/`；限流器在 `core/network/`，bind 守卫在 `core/utils/server_guard.py` |
| `core/text/` | 文本流 | 标签注册表 → tokenizer → splitter → parts → 协调器（**句边界字符集单源在这里**） |
| `core/application/` | 窄用例层 | 跨 input/storage 的用例编排（会话激活、附件签名）；不 import aiohttp |
| `core/providers/` `core/services/` `core/network/` | 运行时能力 | provider 元数据 / 能力声明广播 / 限流 |
| `core/tool_runtime/` | 工具调度 | `QueuedToolCall` + `ToolCallDispatcher`（ARCH-INC-P1-01，原 `core/task/`） |
| `core/tools/` | 工具插件 | 每插件 `plugin.py` + `config.yaml` + `README.md` 三元组 |
| `core/debug/` `core/utils/` `core/prompts/` `core/schema/` `core/config/` | 支撑 | debug 聚合 / 通用工具（log、ID、守卫）/ prompt 模板 / 共享类型 / 应用配置 |

> **改这些包之前先读该包的 README**——它比本文更细，是那块内部的权威；
> 但它**不能**重新定义全局层级。


### frontend_contract/ layer (契约层)

**逐文件结构与成员集合的权威是 [`frontend_contract/README.md`](frontend_contract/README.md) +
代码本身**（本文不抄成员数/模型数——抄的那份不会被门禁发现）：

```
frontend_contract/
├── base.py             # ContractModel 基类 + TsNull marker + wire_key + wire_dict 序列化
├── version.py          # TRANSPORT_VERSION / PROTOCOL_VERSION —— 协议版本单源（批次 9B）
├── ws_message/         # WS 消息契约**包**（ARCH-INC-P2-02 物理拆分）
│   ├── __init__.py     #   **单一装配点**：唯一 ServerMessage / ClientMessage 联合定义位置
│   ├── _types.py       #   共享词汇：ContentPart 联合 + 全部非联合成员类型（零跨模块依赖）
│   ├── chat.py / session.py / tool.py / generation.py / voice.py / ack.py / character.py
│   └── （按领域分文件；新增消息加到对应领域模块并在 __init__ 的联合里登记）
├── rest.py             # REST 业务契约
├── debug.py            # Debug API 契约
├── errors.py           # 错误码单源（KNOWN_ERROR_CODES / KNOWN_ISSUE_CODES）
├── gen_ts.py           # TS 发射器（+ generate_raw_ts raw wire 快照）
├── gen_schema.py       # JSON Schema 发射器（含 schemaHash）
├── contract.schema.json / contract.raw.ts   # 生成产物（**不要手改**）
├── __main__.py         # python -m frontend_contract（生成三产物 / --check 漂移门禁）
└── tests/              # 契约建模测试（marker=unit，无 I/O）
```

契约单源：可执行 Pydantic 模型 → `gen_ts` 生成 `frontend/src/types/contract.generated.ts`（只读快照）
+ `gen_schema` 生成 `contract.schema.json` + `generate_raw_ts` 生成 `contract.raw.ts`。
后端发送点构造契约模型 + `wire_dict()`；`make gen-types` 重新生成、`make check-types` 漂移门禁
（= `--check` + `gen_contract_frames.py --check`（golden frames）+ `check_error_codes.py`（错误码目录））。

**`ws_message/` 的三条禁忌**：① 不要在领域模块里 import 联合本身（成环，R4 会红）；
② 不要在别处再定义一份联合；③ 不要手改生成产物。理由（TS 产物顺序）见包 README。

**依赖方向（有意，勿"修复"）**：本层是共享底座，**`Module/output` 依赖本层**
（直接构造 `ActionMessage`/`AudioMessage` 等契约模型并复用 `MessageChannel`/`SegmentKind`/
`AudioFormat`/`FinishReason` 枚举）。单向无环——若改成 Module 自建枚举再在 core 映射，
就是重复定义这些枚举，正是契约单源要消除的漂移源。代价仅是 Module 不再是可独立分发的自底向上包。

**阶段 4 确定性 smoke**：`conda run -n Elysia python scripts/stage4_smoke.py`——进程内
`create_app` + 真 agent(fake LLM) + STT/TTS 桩，覆盖 Profile/三语音模式/TTS 播放
（默认 stub 确定性、失败非零退出；`STAGE4_SMOKE_LIVE=1` 才接真实 provider，不进 CI）。


### Character config (`character/`)

```
character/
└── <name>/                  # One directory per character
    ├── config.yaml           # YAML config (name, wake_words, emotion.idle_expressions, proactive, etc.)
    ├── personality.md        # L1 Core personality text (natural language, loaded at startup)
    └── reference/            # GPT-generated reference materials (optional)
```

### System prompt engine (`Module/system_prompt/`)

12 sections rendered in fixed order by `SystemPromptManager`:

```
F1 ConflictRules    → Priority rules + safety borders
F2 OutputFormat     → inner_voice protocol + task priority
F3 TTSMarkup        → TTS synthesis markers + usage limits
C2 Relationship     → Relationship view (4-stage + boundaries)
C7 Proactive        → When to speak first + cooldown mechanism
C6 SpeakingStyle    → Speaking style (5-mode switching + praise/flirt/address)
C1 Personality      → L1 Core + L2 Self (data-driven)
C9 SelfPerception   → Self-awareness (AwarenessTrigger conditional)
C8 OwnLife          → Own activities (pass-through, may be empty)
C3 Emotion          → Mood + 8 emotional response rules
C4 Memory           → Long-term memory recall + reliability rules
C5 Sensory          → Current environment context
```

Templates at `Module/system_prompt/templates/` — format convention: XML wrapper + Markdown body. See `Module/system_prompt/templates/README.md` for full format docs.

`budget.py` — **prompt 预算的唯一执行点**（估算 + 换算 + 常量）：`render(budget_tokens=…)`
按预算裁剪 section（先降 C4 记忆条数，再按 `_DROPPABLE_SECTIONS` 整节丢，`_PROTECTED_SECTIONS`
永不丢）。预算来源与两侧策略见下方「prompt 预算」约定条目。

## Module index

**provider / backend 的详细说明归各模块 README**——本文只给架构形状，不维护副本
（§83：一个事实一个权威，其余链接）。

| Module | 职责 | README |
|---|---|---|
| `Module.llm` | LLM chat/streaming（含 `ollama` 的 native `/api/chat` 形状） | [llm/README.md](Module/llm/README.md) |
| `Module.stt` | 语音识别 | [stt/README.md](Module/stt/README.md) |
| `Module.tts` | 语音合成 | [tts/README.md](Module/tts/README.md) |
| `Module.embedding` | 文本向量化 | [embedding/README.md](Module/embedding/README.md) |
| `Module.output` | 多通道输出（Console / WebSocket）。**依赖 `frontend_contract`**，方向有意，勿"修复" | [output/README.md](Module/output/README.md) |
| `Module.config` | 配置基础设施 + `providers.yaml` 注册表单源 | [config/README.md](Module/config/README.md) |
| `Module.system_prompt` | 12 段 system prompt 引擎 + **预算唯一执行点** `budget.py` | [system_prompt/README.md](Module/system_prompt/README.md) |
| `Module.tool` | 工具执行框架（Plugin + SafetyPipeline） | [tool/README.md](Module/tool/README.md) |
| `Module.database.*` | Milvus（vectordb）/ MongoDB（documentdb）/ graphdb stub | [database/README.md](Module/database/README.md) |
| `Module.workflow_engine` | 后台任务引擎（原 `task_system`）：Planner / Executor / SubAgent | [workflow_engine/README.md](Module/workflow_engine/README.md) |
| `Module.workflow_contract` | core ↔ workflow_engine 的跨边契约层（唯一类型来源，零业务逻辑） | [workflow_contract/README.md](Module/workflow_contract/README.md) |
| `Module._common` | 共享工具：env / registry helpers（两种 logger 注入风格）/ **`task_scope` 后台协程所有权** | [_common/README.md](Module/_common/README.md) |
| `Module.bench` | 延迟基准 | [bench/README.md](Module/bench/README.md) |
| `frontend_contract` | 前后端契约单源（模型 + 生成器 + 门禁，零依赖） | [frontend_contract/README.md](frontend_contract/README.md) |
| `core.schema` | 共享 Pydantic 类型：EventData / ToolCall / Part 判别联合 / ID NewType / StrEnum | [core/schema/README.md](core/schema/README.md) |

> **逐 provider 的协议/端点/能力/限制不在本文维护**：权威是各模块 README +
> `Module/config/providers.yaml`；**"哪些已注册但未实现"问 `Factory.is_implemented()`**
> （预检、`/api/providers`、debug 面板都问它）。**不要在这里抄名单**——抄的那份没有门禁。


## Config system

Hierarchical: `env var > YAML file > dataclass default`. Main config at `config.yaml`.

- `agent.character` — path to character config (e.g. `character/elysia/config.yaml`)
- `agent.user_name` — user name injected into system prompt
- `llm.model` — format `<provider>/<model>` (e.g. `deepseek/deepseek-v4-flash`)
- `tts.model` — TTS provider (e.g. `minimax/speech-2.8-hd`). Voice mapping in `Module/tts/minimax_voices.yaml`
- `stt.model` — STT provider (e.g. `sensevoice/sense-voice`)

## Key patterns

- **Async-first**: All I/O is async. Backends manage their own `httpx.AsyncClient` or `openai.AsyncOpenAI`.
- **Logger injection**: 两种风格并存，都在 `Module/_common/registry_helpers.py`——① `inject_logger(__name__)`：**显式**传模块名，各 Module registry 在 `Factory.create()` 内用；② `get_caller_logger()`：经**单跳 `f_back`** 自动探测调用方模块，用于 `__init__` 这种拿不到显式名称的注入点（`Module/output/manager.py` / `websocket.py`）。**理由不是洁癖**：`core/utils/log_setup.py` 按 logger 名把日志分流到不同文件，名字错一层日志就进错文件。
- **env var precedence**: API keys and base URLs from `os.getenv()`, not YAML.
- **MongoDB**: motor async driver. `insert_one/insert_many` pass `dict()` copy to avoid motor mutating callers' dicts.
- **Session state**: 批次 3 起由 `SessionManager`（`core/memory/session_manager.py`）管理会话缓存池 + 活动指针（`agent.session_manager`；`agent.session` 仍指活动会话）。多会话路由：`agent.resolve_session(conversation_id, protocol_version)`——v1 恒 `"default"`、v2 显式 conversationId → 该会话、v2 缺省 → 活动会话；`activate_session` WS 消息切 active + 重发回放。运行时状态用 SessionMemory 的**专用类型字段**（`character_state` / `service_state`；反射器状态非字段，由 `session.save()` 经 `save_state()` 派生），经 `session.save()` 同步、`load()` 恢复（MongoDB 持久化 + JSONL fallback；2026-08-10 后 `sessions` 不再建 Mongo TTL 索引，删除走显式级联删除）。无 `set_param/get_param` 通用接口——新增状态仿 `character_state` 加字段 + save/load 两侧同步。`service_state`（服务运行时状态：当前模型 + STT/TTS 启用模式，enabled 三态 `bool | None`，None 回退 config）由 `agent.set_service` / `switch_*` 写、启动时经 `_apply_service_state` 应用。
- **inner_voice protocol**: Every reply has one `<inner_voice>` block (1-2 sentences: current feeling + response intent, no reasoning chains).
- **Project paths**: `from paths import ProjectPath, get_project_root` — 所有项目文件系统路径的唯一定义点。`ProjectPath.X.resolve()` 返回基于 `ELYSIA_ROOT` 环境变量（fallback 为项目根目录）的绝对 `Path`。
- **AgentStatus**: `agent.status`（`AgentStatus`）提供语义状态查询（`is_accepting_input` / `is_speaking` / `should_suppress_vad_interrupt` 等）；`agent.phases`（`CognitivePhaseController`）驱动阶段转换。底层 `AgentFSM` 不对外暴露。设计文档：[spec](docs/superpowers/specs/archive/2026-07/2026-07-30-agent-status-interface-design.md)。
- **Agent 公开接口**: `Agent` 作为组合根，通过 **46 个同名 `@property`** 公开子系统访问路径（`agent.session` / `agent.long_term` / `agent.tool_adapter` / `agent.output` / `agent.llm` / `agent.stt` / `agent.tts` / `agent.config` / `agent.bus` 等，可写性由子系统 API 自管）。状态写方法：`agent.request_shutdown()` / `agent.request_restart()`（设 `_state` 信号引导主循环退出，不做清理）；`agent.set_service(service, enabled)`（STT/TTS 后端模式开关）；`agent.purge_message_from_session(msg_id)`。跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 层深度内省用 `# noqa: SLF001` 显式豁免。设计文档：[spec](docs/superpowers/specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md)。
- **私有穿透的两种形态（R3 都看不见）**: ① 字符串形式 `getattr(obj, "_priv")`；② 点式深读 `obj.sub._priv`。`make check-arch` 的 R3 只管**跨包导入私有符号**，这两种都躲得过。规则是「每处私有穿透**要么改走公开 API，要么同行标注 `# noqa: SLF001` + 理由**」，由两道可执行守卫兜底：
  - `core/tests/test_cross_package_privacy.py` —— **全仓跨包守卫**。按「属性名归属包」近似判定跨包（同包内部私有访问是正常封装，不判违规）。**已知局限**：同名属性会漏报，故是回归网而非完备审计工具。
  - `core/input/tests/test_layer_discipline.py` —— 输入层守卫，比全仓规则更严（该层任何未标注穿透即红）。
  **判定的关键细节**：只看被访问属性的**直接宿主**——`self._engine._reflector` 的宿主是 `self._engine`（别的对象），属穿透；把它当成「根是 self 的自访问」放行，会让 `self._output._channels` 这类整批漏网（实测如此）。
- **子系统接口层**: 会被跨模块消费、且可能被单独迭代/替换的子系统，把消费方契约抽成 `typing.Protocol`（`core/personality/protocol.py` 是首个）。**刻意不用 ABC**：替换实现不该反向依赖原包。补偿是「pyright 结构校验 + `core/tests/test_character_engine_protocol.py` 逐项断言成员与签名」，并守住「实现**不得继承**协议」——继承会让协议里 `...` 的空方法体变成静默 no-op。
  > ⚠️ Protocol 无实例化期强制：新增接口成员后必须同步跑 `make pyright` 与契约测试；只加成员不加实现，静态检查会红，但运行期不会。

- **TTS/STT 后端契约（ABC，两层强制）**: `Module/{tts,stt}/protocol.py` 是 **ABC**（不是 `Protocol`），但**注入生产路径的对象大多不继承它**——测试替身是 duck-typed 普通类，而 `make pyright` 的 `exclude: ["**/tests/**"]` 整片跳过测试，`typeCheckingMode: basic` 下也**不检查覆写形态**（实测：把 `async def` 覆写成同步 `def`、把 `model` 覆写成返回 `None`，均报 `0 errors`；它唯一抓得住的是 `reportAbstractUsage`＝继承者漏实现抽象成员 → 实例化即红）。
  - **写替身一律继承标准替身**：`from Module.tts.testing import FakeTTSBackend` / `from Module.stt.testing import FakeSTTBackend`，只 override 关心的成员。**不要**手写"这条测试用得到的那几个成员"的裸类——历史缺陷正是替身缺 `model` 让 v2 `server_info` 整帧消失、缺 `close()` 让 `Agent.stop()` 的失败被 shutdown 步骤静默收走。
  - 运行期可执行的契约检查：`Module/_common/protocol_contract.py::violations(obj, protocol)`（协议面从协议类 `dir()` 推导，不手抄）；消费方是 `Module/{tts,stt}/tests/test_{tts,stt}_contract.py`（5 个生产后端 + 工厂路径 + 构造不触网 + 守卫非空转）。
  - **分层纪律**：`core/services/capabilities.py::_backend_model()` 的降级是**运行期韧性**（协议必须帧不许被一个字段拖死），**不是**"backend 可以不守协议"的许可证；"违反协议"必须在 contract test 里失败。两种判据必须同时存在；`_backend_model()` 的四种情形（缺成员 WARNING / 空串合法静默 / 非字符串 WARNING / getter 抛异常 **ERROR + traceback**）见 [docs/testing.md](docs/testing.md) §后端协议的两层强制。

## Prompt files (`core/prompts/`)

模板（`.j2`），PascalCase 命名。多数活跃模板使用 `str.replace()` 注入变量（因 JSON 示例中 `{}` 与 `.format()` 冲突）；`AwarenessJudge.j2` 和 `SpeakingStyleEdit.j2` 使用 Jinja2 宏，通过 `PromptManager.render_macro()` 渲染。通过 `Path(__file__)` 相对路径加载，不受 cwd 影响。详见 `core/prompts/README.md`。

```
core/prompts/
├── ExtractMemory.j2          # 对话提取记忆 (consolidator.py 加载)
├── ConsolidationCompare.j2   # 新记忆 vs 已有记忆比对 (consolidator.py 加载)
├── MemoryReflector.j2        # 定期深度去重 (memory_reflector.py 加载)
├── CharacterReflector.j2     # 性格反思 (reflector.py 加载，10 上下文变量)
├── PersonalityCompression.j2 # L3→L2 自我压缩 — LLM 编辑模式 (engine.py 加载)
├── ActiveSpeak.j2            # 主动搭话 prompt（参考模板，不活跃）
├── Amygdala.j2               # 情感分析 prompt（参考模板，不活跃）
├── AwarenessJudge.j2         # C9 觉察判定 prompt（活跃，Jinja2 宏渲染）
├── SpeakingStyleEdit.j2      # C6 说话风格编辑 prompt（活跃，Jinja2 宏渲染）
├── Utils.j2                  # 共享 Jinja2 工具宏
├── MacroReflector.j2         # 旧项目参考（不活跃）
├── MicroReflector.j2         # 旧项目参考（不活跃）
├── README.md                 # prompt 命名规范 + 渲染方式
└── ref/                      # GPT 生成的原始版本 + 需求文档（snake_case.md）
```

Prompt 需求文档：`core/prompts/ref/` 目录下的 `*-requirements.md` 和 `优化后的prompt-requirements.md`。

## Frontend (`frontend/`)

Vue 3 + Vite + Pinia + TypeScript。开发时 Vite（:5173）代理 WS/API 到 :8765；
生产构建（`npm run build` = `vue-tsc --noEmit && vite build`）直接输出到 `static/`。

**逐文件结构与组件清单的权威是 [`frontend/README.md`](frontend/README.md)**——本文不抄树。
上一版这里抄了一整棵树，里面的组件数/store 数已经和实际不符；那是"抄一份会静默过期的事实"的典型。
Coding Agent 需要知道的只有：

- 入口：`index.html`（聊天）+ `debug.html`（Debug 应用，Vite 多入口）
- `frontend/src/types/protocol.ts` 是**契约类型 re-export shim**——真源是 `frontend_contract/`
  生成的 `frontend/src/types/contract.generated.ts`（**禁止手写契约类型**）
- 状态：Pinia store（connection / chat / audio / settings / tools / debug）
- WS 路由集中在 `useWebSocket` composable（不要再写成巨型 switch）
- 构建产物在 `static/`，被 aiohttp serve；`outDir: ../static` + `emptyOutDir: false`
  ⇒ **CI 的密闭清空是承重的**（见 [docs/ci.md](docs/ci.md)）

### Debug page panels

**Overview tab**: 面板聚合（System / Memory / Latency / Process Memory / Scheduler / Token /
LLM Prompt / LLM Response / Recent Session / LTM Recent / Tools / Tool History / Consolidation /
Memory Reflector / Character Reflector / CharacterEngine）。**逐面板清单与使用说明见
[`static/debug.md`](static/debug.md)**。

**Database tab**: LtmPanel（Milvus CRUD）/ SessionPanel（MongoDB sessions）/ ArchivePanel（消息归档）。
破坏性操作一律经 ConfirmModal。


### Server routing (`core/input/` — `server.py` + `middleware.py` + `health.py` + `ws_handler.py` + `ws_v2.py` + `errors.py` + `provider_routes.py` + `tts_routes.py` + `upload_routes.py` + `tools_routes.py` + `chat_routes.py` + `attachment_routes.py` + `session_routes.py` + `debug_routes/` package)

`/` → `static/index.html`, `/debug` → `static/debug.html`, `/ws` → WebSocket, `/health`（liveness 恒 200）`/ready`（readiness，依赖不 ok → 503）`/shutdown` `/restart` `/api/chat` `/api/messages` `/api/images` `/api/providers` `/tools` `/tools/reload`。批次 3 多会话 REST（`session_routes.py`）: `/api/sessions`（CRUD/activate）、`/api/sessions/{id}/messages`（before/after 分页）、`/api/messages/{mid}/context`、`/api/search/messages`（category 白名单 8 值含 sticker 贴图历史，阶段 3；`date_from/date_to` 严格 `YYYY-MM-DD` fail-fast，非格式 400 `CONTRACT_VALIDATION`）、`/api/sessions/{id}/resources`（sticker 分类经 StickerPart 填充 + 附件软删过滤，批次 7）、`/api/sessions/{id}/generations`、`/api/generations/{gid}`、`/api/sessions/{id}/pending`。批次 7 附件/媒体（`upload_routes.py` + `attachment_routes.py`）: `POST /api/uploads`（通用附件上传，filetype 魔数校验 + 签名 URL；阶段 3 起逐文件 `kind`——每文件一组 `kind+files` 邻接配对，兼容旧单 kind 整批提示，悬空/歧义/未知 → 400；`kind` 确定后 MIME 规范化单源——audio+video/webm→audio/webm、WAV 探测别名→audio/wav，`core/schema/utils.py::normalize_attachment_mime`，写/读/下载三路径同值）、`GET /api/attachments/{id}`（HMAC 短签下载，免 Bearer，Range/206，Content-Type 同样过规范化）、`GET /api/attachments/{id}/sign`（签发，Bearer）、`GET /api/stickers`（内置表情包）、`POST /api/uploads/desktop/{callId}`（批次 4 桌面附件）。`/uploads/` 静态挂载收敛：仅 images/desktop 子目录（新 kind 目录不静态暴露）。Debug API: `/debug/api` `/debug/api/prompts/{n}` `/debug/api/responses/{n}` `/debug/api/db-status` `/debug/api/ltm` `/debug/api/sessions` `/debug/api/archive` `/debug/api/tasks/stream`

## CharacterEngine

4-layer model managing character personality state. See `core/personality/README.md` for full docs.

- L1 Core: file-loaded, immutable base personality (`character/<name>/personality.md`)
- L2 Self: session-persisted long-term self-image, evolves slowly via compression (Phase 2)
- L3 State: session-persisted short-term state, driven by CharacterReflector + events
- L4 Mood: emotion per-turn, driven by LLM `<mood>` tag in main response stream (free-text Chinese), keyword detection as fallback

CharacterReflector is a subcomponent — after LLM reflection, it submits events to `CharacterEngine.consume_trigger("reflector", ...)` instead of writing `self_perception_history` directly.

C1 renders L1 + L2. C9 conditionally renders L3 via `AwarenessTrigger` (cooldown: 5 minutes, persisted to session).

- **C6 SpeakingStyle 动态联动**: L2 演化后，LLM 编辑说话风格描述使与 L2 一致。版本历史与 L2 共享 `character_versions` 集合，per content_type 独立计数。

## Tool plugins (`core/tools/`)

Each plugin in its own directory: `plugin.py` + `config.yaml` + `README.md`. Plugins are imported in `__init__.py` and added to `ALL_PLUGINS`.

**阶段 6 工具双轨（2026-08-19，方案 B 已定案，后端代码批已完成）：** Desktop 工具走 `tool.request/tool.status`；Backend 工具 v2 发 `tool.started/tool.completed` 展示帧，v2 停发 `action{tool_executing/tool_done}`（v1 逐字节不变）；`tool.request` 必填 `sourceCallId`（LLM `tool_call_id`，经 scheduler 线程化到 `ctx.call_id`）并由桥填充 `requestId`。后端 B-6-01~09 落地（commit `c16e27d9..6a54fc25`），执行记录见 `docs/progress/archive/2026-08/2026-08-19-stage6-工具双轨-execution-record.md`；golden `ws-v2-tool.jsonl` 预期增 `sourceCallId`（D-GL-01）。前端 F-6 批已在 elysia-frontend 落地并完成真实 Windows Electron 联调（2026-08-21，阶段 6 已真实联调；D-§7-4/5 前端定，可选项 DEFERRED）。

## Vision（看图）备忘 — WebUI `describe_image` 优先

- **看用户/任务里提到的图片，优先用 WebUI（Agent 平台）自带的 `describe_image` 工具**：传图片本地绝对路径（如 `/home/yomu/elysia-agent/static/Elysia.png`）或 URL + `prompt`，即可拿到图片描述，无需经过项目后端。
- 同一平台的 `read_image`（图片直接喂给底层模型）**不可用**：当前底层模型 `deepseek-v4-pro` 未声明 image input，会报 `model ... does not declare image input`；看图一律走 `describe_image`。
- 实测记录（2026-08-19，`static/Elysia.png`）：WebUI `describe_image` 可正常查看并正确描述（粉色短发、紫瞳、精灵耳、粉白礼服少女）。偶发 timeout / 安全策略拒绝时，换更简短、中性的 `prompt` 重试即可。
- 项目后端也有同名工具 `core/tools/vision/plugin.py :: DescribeImagePlugin`（agent 运行时描述用户消息中的图片，与 WebUI 工具无关）：模型来自 `config.vision.model`（默认 `mimo/mimo-v2.5`），支持 `image_index` / `image_indices`，结果缓存到 `data/cache/vision/*.json`，本地路径 / `/static/*` 会转 base64 data URI。链路实测成功，但 MiMo 可能认错角色（把 Elysia 认成《原神》诺艾尔），只保证返回描述。
- 项目插件的运行/测试坑：环境带 `ALL_PROXY=socks://127.0.0.1:7897/` 时，httpx 报 `Unknown scheme for proxy URL`；先 `env -u ALL_PROXY -u all_proxy` 再跑（HTTP 代理可保留）。

## Memory & storage

**集合/索引/TTL/GC/删除语义的权威是 [`docs/database-storage.md`](docs/database-storage.md)**——
本文只留 Coding Agent 真正需要知道的**所有权规则**：

- **会话**：`sessions`（Mongo，`default.sessions`）**无 Mongo TTL**，删除走**显式级联删除**
  （`SessionManager.cascade_repos` 清单由 `core/memory/tests/test_session_cascade.py` 的 AST 守卫钉住）。
  运行时状态用 `SessionMemory` 的**专用类型字段**（`character_state` / `service_state`），
  经 `save()` / `load()` 两侧同步——**没有** `set_param/get_param` 通用接口。
- **归档**：`message_archive` 是 append-only 的永久消息日志（与 `sessions` 是**两份**数据，
  别把"会话窗口"和"历史记录"混为一谈）。
- **长期记忆**：Milvus（生产写 `long_term_memory_v2`，v1 只是 fallback 路径）——
  **向量库与会话库是两套独立存储**，没有跨库事务。
- **后台任务终态**：#6/决策 #6——先落 `background_task_results`（`task_id` 唯一 + 7d TTL）
  再投递；硬杀窗口内终态只在本地 WAL（`data/task_result_wal.jsonl`，fsync）里，
  启动 replay 进仓储且**库为权威**。
- **附件**：`attachments` 是 fail-closed 仓储（Mongo 离线 → 503），生命周期由 GC 四规则管，
  **不设 TTL**。
- **用户身份**：`user_identity_profiles` 以 **Mongo 为权威**（唯一 `user_id` + revision CAS），
  启动装载，重启不回到 bootstrap。

**Schema 变更（ARCH-INC-P1-04）**：**不做迁移框架**——新增集合/索引/字段由 `ensure_index`
（每次 `connect()` 幂等执行）自动覆盖；只有「删/改已有索引」需人工，用
`DocumentDBBackend.drop_index()`。启动步骤 `schema_drift` **只读**比对代码声明与库中实况，
孤儿索引报 warning 并给命令、**绝不自动删**。详见 `docs/database-storage.md` §五。


## Conventions

项目有以下约束文档，所有代码和文档变更必须遵守：

- **`docs/code-style-guide.md`** — 代码风格与类型约束。写代码前先读：ID NewType 体系 + 类型权威归属（core 定义 vs 契约层定义）+ StrEnum/coerce 用法 + getattr 使用纪律。
- **pyright 门禁** — `make pyright`（`pyrightconfig.json`：include `core/` + `Module/` + `frontend_contract/`、排除 `**/tests/**`、`typeCheckingMode: basic`、`pythonVersion: 3.12`）。须在 Elysia env 运行——base env 无 aiohttp 会漏报 ~25 处。
- **架构边界门禁** — `make check-arch`（`scripts/check_architecture.py`，ARCH-INC-P1-03/P2-03）。五条规则：R1 `Module/` 生产代码与 `frontend_contract/` 不得反向依赖；R2 领域/基础设施层不得依赖 `core.input`；R3 禁止**跨包**导入下划线私有符号；R4 不得新增 **eager** import 环；R6 裸 `create_task`/`ensure_future` 必须逐点登记所有权。存量泄漏（现仅 3×R1：bench/documentdb CLI 两处刻意豁免）在脚本 allowlist 逐项登记理由——**新增即红**；收口某项后须同步删除对应 allowlist 条目（R3 已于 2026-09-11 批次 A 清空）。函数内**懒导入**是打断 import 环的正当手段（R4 只算 eager，但 R1/R2/R3 仍算运行时依赖）。R6 记的是**期望条数**而非布尔——同一函数内追加裸 spawn、或清理后忘删条目，都会报；其 allowlist 只对真实仓库生效，测试用的合成 fixture 仓库不套用。`TYPE_CHECKING` 类型边**允许且不参与任何规则**（既定设计，`_collect_imports()` 直接跳过该块）。**历史 R5 已于 2026-09-17（Batch 2.1）整条删除**：`_type_only_edges()` 定义了却从未被 `main()` 调用，其 `_Module.type_edges` 桶也因此无消费者——那是「根本不存在却写在文档里」型假门禁（比“永远绿”更隐蔽，因为连绿都不用装）。刻意**不留** `--report` 之类的默认关闭开关：无消费者的代码正是 R5 当初退化成死规则的方式（2026-09-16 架构审计 §733 同结论，并明确反对把它默认接线）。
- **私有穿透守卫收私有方法**（2026-09-17 Batch 2.1 修复的假绿）— `core/tests/test_cross_package_privacy.py` 的归属表此前**只收赋值目标**（`ast.Assign`/`ast.AnnAssign`），`def _m` 一个都不收，于是 `_scan()` 里 `if defining and …` 恒假 ⇒ **跨包私有方法调用整类静默放行**（字段与方法不同权）。生产代码实测漏 9 处（`core/adapter/tts_adapter.py` 8 处 + `core/cognitive/streaming.py` 1 处调用 `Module/output/manager.py::OutputManager._broadcast`），修复后 0 → 9 且全为真实泄漏，已按「要么改走公开 API、要么同行 `# noqa: SLF001` + 理由」收口（tts_adapter 那 8 处收成一个带理由的 `_broadcast_text_delta` 豁免点）。**新增私有方法访问即红**。注意 `ruff.toml` **未启用** SLF001，故全仓 `# noqa: SLF001` 对 lint 是惰性的——真正执行这条纪律的是上面这个守卫；`core/input/tests/test_layer_discipline.py` 管输入层，比它更严且本来就认方法。
- **后台协程所有权** — 新增后台协程一律走 `Module/_common/task_scope.py` 的 `AsyncTaskScope`（按 owner 实例化：`spawn` 强引用 + done callback 统一记未处理异常 + `cancel_all`/`drain`/`aclose`）或 `cancel_and_drain(task, ...)`（单任务有序收尾）。`drain(timeout)` 是**总预算**而非逐任务超时。终态 owner（VoiceSession/ToolPlugin）用 `aclose()`；可重启 owner（Agent 同一实例 `stop()`→`start()`）用 `cancel_all()`+`drain()`——封闭 scope 会让重启后一律 `TaskScopeClosedError`。确需裸 `create_task` 的，须在 `_TASK_SPAWN_ALLOWLIST` 登记「任务存哪 / 谁取消 / 谁等待 / 异常谁看」。新 owner 停止后必须 drain 再关它依赖的资源（repo/客户端）。详见 `docs/review/2026-08-30-后端架构分层与模块化增量评估.md` §ARCH-INC-P2-03。
- **`docs/readme-conventions.md`** — README 编写规范。新增/修改模块时必须按模板写 README（面包屑 + 相关文档节），并在根 README 文档导航中注册。
- **`docs/ci.md`** — **CI 的唯一权威说明**：job 布局 / 为什么全部 job 跑在 self-hosted（GitHub 托管按分钟计费，额度见底后 job **根本不启动**，会挂与代码无关的红叉）/ `check_ci_isolation` 的信任边界规则（PR 可达 job 禁 self-hosted、禁死 job、禁表达式 `runs-on`、self-hosted 清理必须密闭受守卫且 fail-closed）/ **开放 PR 时的恢复清单（含分支保护 required check 陷阱）**。**本仓当前不开放 PR**：`ci.yml` / `contract-frames.yml` 只监听 `push: [main]`，`nightly.yml` 只监听 `workflow_dispatch`。改 CI 前先读它。
- **`docs/testing.md`** — 测试分层体系。新测试必须加 marker（unit/service/integration/slow），日常用 `make test-quick` / `make test-normal` / `make test-full`。
- **超时预算** — 任何**新的具名超时常量**被当作 `wait_for`/`wait`/`timeout` 的实参时，必须在 `scripts/check_timeout_budget.py` 表态：它覆盖谁（登记进 `_INVARIANTS`，外层 ≥ 内层），或者它是预算链最内层（登记进 `_LEAF_BUDGETS` + 理由）。门禁 `make check-timeouts`。**理由不是洁癖**：S2-A3 的成因就是「外层 5s < 内层 17s」，而当时两条预算的一致性只写在注释里——注释不是机制。同一函数内嵌套的等待预算由 R1 自动校验（内 ≤ 外），无需登记。**R4**：插件经 `SafetyProfile(request_timeout=…)` **自己声明**的预算必须 ≤ 调度器的 `_MAX_PLUGIN_REQUEST_TIMEOUT`（超出即红，逼着同步抬上界并重算 `_CALL_BUDGET`/`_DISPATCH_TIMEOUT`）。R4 补的是 R1–R3 都看不见的盲区——那个 120 是经**字段**传进管道的，不是"具名常量直接当 `timeout` 实参"，于是 desktop/vision 按设计声明的 120s 曾被外层 60s 悄悄截断（S2-G2）而三条规则全绿。
- **prompt 预算** — 组装后的 prompt 必须有上界：预算 = `providers.yaml::llm.<provider>.context_window` × 0.75，未声明用保守默认（32K）。唯一执行点 `Module/system_prompt/budget.py`（估算 + 换算 + 常量），两个截断点：`SystemPromptManager.render(budget_tokens=…)`（C4 记忆逐条降 → `_DROPPABLE_SECTIONS` 整节丢，`_PROTECTED_SECTIONS` 永不丢）与 `MessageBuilder.build_messages(prompt_budget_tokens=…)`（历史由旧到新逐条丢，system 与当前消息永不丢）。**理由**：超 context 不是被截断而是 provider **400**，而 400 不进重试集合 ⇒ 该会话对该角色永久失败，日志只显示 `GENERATION_PROVIDER_UNAVAILABLE`。**该模块必须留在 `Module/` 侧**——放 `core/` 会让 `Module/system_prompt` 反向依赖 core，`make check-arch` R1 会红（实测如此）。**Ollama 是双重语义（Batch 17 / KB-13 / AR-16）**：同一个 `context_window` 既是预算来源，也是 native `/api/chat` 的 `options.num_ctx` 请求值（OpenAI 兼容端点实测**静默忽略**它 ⇒ 只能在 native 表达）；显式声明（>0）才下发，缺失/非法一律**不发**——通用预算回退 32768 是预算兼容行为，**绝不**当运行时分配指令（那是真实显存分配，且 `num_ctx=0/-1` 会得到 4 token 的畸形 runner）。
- **契约可达性** — 契约/数据模型模块（`Module/workflow_contract/schema.py` + `Module/workflow_engine/schema/*.py`）里新增**枚举成员**必须有生产者（`EnumClass.MEMBER` 或其字符串值字面量出现在生产代码里），否则登记进 `_RESERVED_ENUM_MEMBERS` 写明"值域预留、为什么暂不产"**＋「解除条件：」（什么情况下该删这个值域）**——只写前者的登记表会退化成永久豁免单，读者分不清"有计划的预留"与"忘了删的残留"，故门禁强制两条都在（owner 2026-09-16 裁定「值域全部保留，但每条都要补解除条件」）；新增**模型字段**若被读取（`.field`）就必须有写入（`field=` / `"field":`），否则登记进 `_PENDING_WRITERS`。门禁 `make check-contract`。**理由（RC-12）**：pyright/`check-types` 只校形状，不会问"这个字段/枚举值真的有人生产吗"——`DAGNode.max_duration_s`、6 个 `WorkflowErrorCode` 就是这样长期无人发现的。登记项**陈旧也会报**（已补上生产者却不删登记 → 红）。
- **门禁的三种假绿** — ①**判据写错**（子串匹配 `ast.dump` 渲染出的 `IsNot()` 恒不命中）；②**守卫没装在改动上**（反证后全绿≠守卫强）；③**环境不满足**（判据本身无从成立）。第三种最隐蔽：浅克隆里查 git 历史、缺外部二进制、CI 与本地 checkout 深度不同……此时门禁必须**拒答**——非零退出 + **不打印通过标记**，而不是报绿（`check_secrets.py --all-refs` 在 `--depth 1` 下退出 2 即范例；CI 的 self-hosted `git fetch --depth 1` 与 `actions/checkout@v4` 默认都是浅克隆）。配套约束：**断言"某个缺陷存在"的用例必须把期望值现算**（如用 `git log -S` 判历史），否则 owner 修好它的那一刻用例就永久变红；无从判定处用显式 `pytest.skip` 并把原因写进 skip 消息，不用"看起来通过"糊过去。
- **system prompt 变量转义** — `Jinja2Section._render_template()` 是唯一渲染点，**递归转义所有字符串叶子**（含嵌套结构）。section 的 `render()` **不得**再手工包 `escape_authored_prompt`（二次转义，有 AST 守卫）；受信且需保留标记的文本用 `Trusted` **显式**豁免（当前仅内建角色 `source == "builtin"` 的 L1 正文/风格）。**理由**：逐点转义要求每个新增字段的作者都记得包一层，而"记得"不是机制——实测 10 个调用点里裸值 12 处、漏 3 处，其中 `displayName` 与 C6 说话风格两处**可实际闭合标签伪造 system prompt 区块**。详见 `Module/system_prompt/sections/authored.py`。
- **`core/prompts/README.md`** — Prompt 模板命名规范（PascalCase `.j2`）+ 渲染方式（`str.replace()` / Jinja2 `render_macro()`，通过 `PromptManager`）。
- **`Module/system_prompt/templates/README.md`** — System prompt 模板格式约定（XML wrapper + Markdown body，标签 snake_case）。

## Import 风格

- **禁相对导入**（`from .xxx` / `from ..xxx`），一律绝对导入：`from core.xxx import y` / `from Module.xxx import y`。用 `make lint`（ruff TID252）强制；`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免，见 `ruff.toml`。
- 循环导入规避：直接引子模块（`from core.monitoring.idle_monitor import IdleMonitor`），不要改经包重导出（`core.monitoring` 包用 lazy `__getattr__` 绕环）。

## 基础规则：

1. README 用中文写。
2. 代码注释用中文（docstring 同理；中英混合注释——中文嵌英文技术术语——可接受）。
3. 使用 conda 环境: `conda activate Elysia`。
4. 所有敏感信息必须通过环境变量传递。
5. 新增 core/tools 插件必须包含 `plugin.py` + `config.yaml` + `README.md` 三元组。
6. 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003；测试文件豁免——pytest fixture 动态类型惯例，`**kwargs: Any` / `*args: Any` 兜底）。
