# 全仓工程级 Code Review —— 架构地图 + Issue Ledger（工作稿）

> 本文件是 2026-09-12 起对 `/home/yomu/elysia-agent` 进行 **全面、系统、工程级 Code Review** 的
> 主 Agent 工作台账（objective 第 18/19 节要求的 architecture map + issue ledger）。
> 最终交付物是同目录/根目录的 `CODE_REVIEW_REPORT.md`；本文件为**过程证据**，非最终结论。

- 基线 revision：`4aa3f8ef`（branch `main`），工作树干净
- 审查日期：2026-09-12
- 审查方式：主 Agent 侦察 → 10 个 Subsystem Subagent（Round 1）→ 6 个 Cross-Cutting Subagent（Round 2）
  → 主 Agent 逐项验证 → 去重 → 分级 → 综合报告

---

## 1. 规模与代码量实测

`find` + `wc -l` 实测（排除 `__pycache__`，2026-09-12）：

| 顶层 | 文件数 | 总行数 |
|---|---:|---:|
| `core/` | 423 | 95,939 |
| `Module/` | 222 | 41,209 |
| `frontend_contract/` | 33 | 7,003 |
| `scripts/` | 30 | 7,344 |
| `tests/`（根） | 1 | 130 |
| **合计** | **712** | **~144,000** |

生产代码（排除 `tests/` 与 `test_*.py`）：约 **67,000 行**；测试代码约 **49,000 行**。

### 各子系统生产/测试 LOC

| 子系统 | 生产 | 测试 |
|---|---:|---:|
| `core/input` | 8,799 | 17,137 |
| `Module/workflow_engine` | 8,661 | 8,074 |
| `core/storage` | 5,729 | 5,336 |
| `core/cognitive` | 3,432 | 5,887 |
| `core/agent` | 3,403 | 3,522 |
| `core/memory` | 3,332 | 2,943 |
| `core/character` | 2,775 | 2,570 |
| `Module/tool` | 2,670 | 2,504 |
| `core/tools` | 1,957 | 2,123 |
| `core/personality` | 1,937 | 1,030 |
| `Module/output` | 1,540 | 1,605 |
| `core/text` | 1,519 | 2,618 |
| `core/schema` | 1,508 | 1,406 |
| `Module/database` | 1,382 | 1,041 |
| `Module/tts` | 1,217 | 1,491 |
| `core/adapter` | 1,210 | 649 |
| `Module/config` | 1,208 | 1,011 |
| `Module/system_prompt` | 1,108 | 881 |
| `Module/_common` | 836 | 756 |
| `core/task` | 793 | 1,220 |
| `Module/embedding` | 743 | 492 |
| `Module/llm` | 622 | 844 |
| `core/debug` | 636 | 118 |
| `Module/stt` | 576 | 533 |
| `core/prompt` | 514 | 1,032 |
| `core/desktop` | 513 | 796 |
| `Module/bench` | 508 | 0 |
| `core/monitoring` | 477 | 494 |
| `core/status` | 466 | 158 |
| `core/utils` | 399 | 0 |
| `core/config` | 311 | 205 |
| `core/providers` | 305 | 0 |
| `core/profile` | 272 | 215 |
| `core/network` | 180 | 0 |
| `core/services` | 160 | 0 |
| `Module/workflow_contract` | 697 | 96 |
| `frontend_contract` | ~5,000 | — |

### 最大的生产文件（Top 10）

| 行数 | 文件 |
|---:|---|
| 1,351 | `core/character/service.py` |
| 1,319 | `core/agent/agent.py` |
| 1,136 | `core/cognitive/loop.py` |
| 1,113 | `frontend_contract/rest.py` |
| 959 | `core/input/session_routes.py` |
| 942 | `Module/tool/manager.py` |
| 940 | `core/memory/long_term.py` |
| 821 | `Module/workflow_engine/workflow_engine.py` |
| 808 | `core/memory/session_manager.py` |
| 795 | `core/input/voice_sessions.py` |

---

## 2. Architecture Map

### 2.1 Runtime

| 维度 | 实测结论 |
|---|---|
| Python | **3.12.13**（conda env `Elysia`；`pyrightconfig.json` 声明 `pythonVersion: 3.12`；`requirements` 无 `python_requires` 强制） |
| Web framework | **aiohttp**（`core/input/server.py::create_app`；WS 用 `aiohttp.web.WebSocketResponse`） |
| ORM | **无 ORM**。MongoDB 走 **motor** 异步驱动 + 手写仓储（`core/storage/*_repo.py`）；Milvus 走 **pymilvus** |
| Database | MongoDB 8（docker-compose，`default` 库）；Milvus v2.6.22（+ etcd + minio） |
| Cache | **无独立缓存层**。`SessionManager` 会话缓存池（内存）；`core/tools/vision` 有 `data/cache/vision/*.json` 文件缓存；`InterpretationCache` 类机制（memory_reflector 侧） |
| Queue / Worker | 无外部队列。**进程内**：`core/task/scheduler.py`（5 worker 的 ReAct 工具调度池）、`Module/workflow_engine`（后台任务引擎，自建 queue + semaphore） |
| Scheduler | `Module/workflow_engine/lifecycle.py` + `core/monitoring/idle_monitor.py`（主动搭话）+ `core/tools/reminder`（定时提醒） |
| RPC | **无**。core ↔ workflow_engine 经 `Module/workflow_contract`（进程内类型契约 + `AgentInfra` 注入容器），非 RPC |
| WebSocket | `core/input/ws_handler.py` + `ws_v2.py` + `ws_handlers/`（v1/v2 双协议）；出站 `Module/output/websocket.py` |
| Event Bus | `core/monitoring/event_bus.py`（进程内 pub/sub） |
| AI / LLM | `Module/llm`（provider：`deepseek` / `openai` / `ollama` / `mimo` / `gemini` / `custom`）；`Module/embedding`（`huggingface` / `openai`）；`Module/stt`（`sensevoice` / `custom`）；`Module/tts`（`gpt-sovits` / `minimax` / `custom`）；vision 工具用 `mimo/mimo-v2.5` |
| 第三方服务 | Tavily（`web` 工具搜索）、MiniMax（TTS）、SenseVoice（STT）、GPT-SoVITS（TTS） |
| 文件存储 | 本地文件系统（`data/uploads/`、`data/cache/`、`data/secrets/`、`static/`），路径唯一定义点 `paths.py::ProjectPath` |
| 配置系统 | `Module/config`（`ConfigLoader`：env > YAML > dataclass default）+ `core/config`（`AppConfig` 10 个子 dataclass）+ `config.yaml` + `.env` |
| 日志系统 | `core/utils/log_setup.py` —— **按 logger 名分流到不同文件**；`Module/_common/registry_helpers.py` 提供 `inject_logger(__name__)` / `get_caller_logger()`（单跳 `f_back`）两种注入风格 |
| Deployment | 单进程 `python app.py`（`scripts/start.sh`）；数据服务 docker-compose（mongo/etcd/minio/milvus，仅 loopback 端口）；CI = GitHub Actions（PR gate on GitHub-hosted + push 到 main 用 self-hosted） |

### 2.2 Entry Points（实测清单）

| 类型 | 位置 |
|---|---|
| HTTP Server（WebSocket）| `app.py::main` → `Agent.run_server(app_factory=create_app)` → `core/input/server.py::create_app` |
| HTTP Server（裸） | `core/input/server.py::create_app`（被 `scripts/stage4_smoke.py` 进程内使用） |
| CLI（交互终端）| `app.py::run_interactive`（`--cli`） |
| CLI（预检）| `app.py::main` `--check` / `--full` → `Module/config/preflight.py::run` |
| 进程信号 | `SIGTERM` / `SIGINT`（`core/agent/server.py::run_server` 侧） |
| Startup Hook | `core/agent/lifecycle.py::AgentLifecycle.start()` + `_STARTUP_STEPS` |
| Shutdown Hook | `core/agent/lifecycle.py::AgentLifecycle.stop()` + `_SHUTDOWN_STEPS` + 3 个 flush |
| Worker（后台任务引擎）| `Module/workflow_engine/workflow_engine.py::WorkflowEngine`（由 `core/agent/_lifecycle_task_system.py` 启停） |
| Cron / Scheduler | `core/monitoring/idle_monitor.py`（主动搭话轮询）、`core/tools/reminder/plugin.py` |
| Consumer（WS 消息）| `core/input/ws_handler.py::handle_ws` → `_WS_HANDLERS` 装配点（`core/input/ws_handlers/__init__.py`） |
| Consumer（SSE）| `Module/workflow_engine/debug_api.py`（`/debug/api/tasks/stream`） |
| `python -m` 入口 | `Module/tool/__main__.py`、`Module/output/__main__.py`、`Module/database/documentdb/__main__.py`、`Module/llm/__main__.py`、`frontend_contract/__main__.py`、各 `Module/*/__main__.py` 集成冒烟 |
| scripts | `scripts/start.sh` / `setup.sh` / `check.sh` / `clean_db.py` / `backfill_*.py` / `dev/seed_*.py` / `dev/purge_*.py` / `stage4_smoke.py` / `capture_ws_trace.py` / `gen_contract_frames.py` / `check_*.py`（6 个门禁脚本） |
| 测试入口 | `conftest.py`（真实 DB 访问防线）+ `pytest.ini`（5 个 marker + 全局 300s timeout） |

### 2.3 Dependency Direction（实测）

```
外部输入 (HTTP/WS/CLI)
   ↓
core/input/            (传输层：aiohttp 路由 + WS handler + 语音会话)
   ↓
core/agent/            (组合根：生命周期 + 45 个公开 @property)
   ↓
core/cognitive/        (认知主循环 ReAct)
   ↓
core/character/ · core/personality/ · core/memory/ · core/prompt/  (领域)
   ↓
core/adapter/ · core/task/ · core/text/ · core/schema/ · core/status/
   ↓
core/storage/          (仓储)
   ↓
Module/database/       (motor / pymilvus)

横切：Module/output · Module/tool · Module/workflow_engine · Module/system_prompt
契约：frontend_contract（零依赖，被 Module/output 依赖——有意）
跨边契约：Module/workflow_contract（core ↔ workflow_engine 唯一类型来源）
```

**门禁实测（2026-09-12，全部通过）**：

- `ruff check core/ Module/ frontend_contract/ scripts/` → **All checks passed**（TID252 禁相对导入 + ANN 参数注解）
- `conda run -n Elysia pyright` → **0 errors, 0 warnings, 0 informations**
- `python scripts/check_architecture.py` → **通过**（R1 `Module→core` / R2 领域层 `→core.input` / R3 跨包私有符号 / R4 eager import 环 / R6 裸后台任务登记）；存量豁免仅 3 项 R1，全部在 allowlist 登记理由
- `pytest --collect-only -q` → **4,439 tests collected**

**反向依赖实测**：`Module/` 生产代码 import `core/` 仅 3 处，全部在 allowlist（`Module/bench/bench.py` ×2、`Module/database/documentdb/__main__.py` ×1）。
`Module/output/{manager,websocket}.py` 依赖 `frontend_contract`（文档声明为有意，单向无环）。
`core/` 有 21 个文件 import `frontend_contract`（契约层作为类型来源，符合设计）。

**循环依赖**：R4 检查通过（无新增 eager import 环）。

### 2.4 安全模型（实测事实）

| 项 | 事实 |
|---|---|
| 认证 token | 双域：`SERVER_AUTH_TOKEN`（admin/debug）、`AGENT_ACCESS_TOKEN`（产品域 `/ws` + 业务 `/api/*` + `/uploads/*`）；`ConfigField(secret=True)` |
| 附件下载 | HMAC 短签（`ATTACHMENT_SIG_SECRET`），免 Bearer；`GET /api/attachments/{id}/sign` 需 Bearer |
| SSRF | `Module/_common/ssrf.py`：`validate_endpoint()` 静态校验（scheme 白名单 + 字面量黑名单 + `getaddrinfo` 逐 IP 判类含 IPv4-mapped IPv6 与 CGNAT）+ `build_guarded_http_client()` 出站守卫传输层（每跳校验 + 跨源剥 `Authorization`）。消费方 6 处 core + `Module/llm/registry.py` + `core/tools/web/plugin.py` |
| 上传 | `filetype` 魔数校验 + 逐文件 `kind` + MIME 规范化单源 `core/schema/utils.py::normalize_attachment_mime` |
| 限流 | `core/network/rate_limiter.py`：6 类别（ws/messages/provider_test/upload/search/tools），**单进程语义**（不假装全局限流） |
| 绑定守卫 | `core/utils/server_guard.py::assert_secure_bind`：默认 `127.0.0.1`；非 loopback 需 `ALLOW_INSECURE_REMOTE` 或 token |
| compose 守卫 | `scripts/check_compose_security.py`（静态解析：仅 loopback 端口、无默认凭据） |
| 密钥落盘 | `.env` **未被 git 跟踪**；`.gitignore` 覆盖 `.env` / `data/secrets/` / `data/*.json` / `log/`；`git ls-files` 仅含 `.env.example` |
| 危险模式 | `shell=True` / `os.system` / `eval(` / `exec(` / `pickle.load` / `yaml.load(` 在**全仓生产代码 0 命中**；`except:` 裸 except 仅 1 处（`core/storage/secret_store.py:101`，`except BaseException`） |

### 2.5 高风险模式扫描统计（仅作线索，非 Issue）

| 模式 | 生产代码命中 |
|---|---:|
| `except Exception` / `except BaseException` | **283** |
| 其中 `except → pass/continue/return`（静默吞噬）| 见各 Agent 逐点判断 |
| 裸 `except:` / `except BaseException` | 1（`core/storage/secret_store.py:101`）|
| `contextlib.suppress` | 0 |
| `shell=True` / `os.system` / `eval` / `exec` / `pickle` | 0 |
| `assert`（生产）| 28（多为 `.assert self._x is not None` 的不可达兜底）|
| `type: ignore` / `noqa` | 集中在 `core/debug/info.py`(16)、`core/schema/__init__.py`(13)、`scripts/*`、`core/storage/__init__.py`(6) |
| TODO/FIXME/HACK/XXX | 极少（`Module/workflow_engine/schema/output.py` 5、`core/cognitive/delegation.py` 2、`core/agent/agent.py` 1、`core/cognitive/loop.py` 1）|

broad-except 最集中的文件：`core/agent/_lifecycle_persistence.py`(22)、`core/input/ws_handler.py`(15)、
`core/memory/long_term.py`(13)、`core/cognitive/loop.py`(11)、`core/memory/session_manager.py`(8)、
`core/input/ws_v2.py`(8)、`core/input/voice_sessions.py`(8)、`core/agent/lifecycle.py`(8)。

---

## 3. Review Partition（Round 1：子系统分工）

按 **subsystem / runtime responsibility / 依赖边界** 拆分，不按文件数平均：

| Agent | 承担子系统 | 生产 LOC | 侧重点 |
|---|---|---:|---|
| R1-A | `core/input` REST/HTTP 面（server/middleware/health/errors/`*_routes`/`debug_routes`/`server_guard`/`rate_limiter`）| ~4,600 | 认证授权覆盖、IDOR、上传/路径、HTTP 语义、分页、限流 |
| R1-B | `core/input` WS + 语音（ws_handler/ws_v2/ws_handlers/voice_sessions/audio_buffer/wakeword）| ~4,200 | 连接生命周期、幂等/ACK、v1/v2 一致性、语音状态机、背压、清理 |
| R1-C | `core/agent` + `core/utils/log_setup.py` + `app.py` | ~3,700 | 启停顺序与逆序、半初始化、热切换、后台任务所有权、重启幂等 |
| R1-D | `core/cognitive` + `core/status` + `core/task` + `core/monitoring` | ~5,100 | ReAct 循环出口、取消/超时、worker pool、FSM 单一真相、EventBus |
| R1-E | `core/storage` + `Module/database` | ~7,100 | 原子性/CAS、事务边界、幂等写入时机、索引、附件/GC、schema 漂移 |
| R1-F | `core/memory` + `core/personality` + `core/character` + `core/profile` + `Module/embedding` | ~8,600 | 多会话/多角色隔离、save/load 对称、向量一致性、巩固管线、4 层状态 |
| R1-G | `Module/workflow_engine` + `Module/workflow_contract` | ~9,400 | 图算法/DAG、子智能体生命周期、取消级联、持久化恢复、SSE |
| R1-H | `Module/tool` + `core/tools/*` + `core/adapter` + `core/desktop` | ~5,900 | 工具安全/HITL、参数校验、流式 tool_call 合并、TTS 门控、桥信任 |
| R1-I | `core/text` + `Module/output` + `Module/{tts,stt,llm}` + `core/prompt` + `Module/system_prompt` + `core/schema` | ~7,300 | 流式切分不变式、契约发送点、后端生命周期、prompt 注入/token 预算 |
| R1-J | `frontend_contract` + 配置系统 + `Module/_common` + `core/{providers,services,debug,network}` + `scripts` + 部署 | ~13,000 | 密钥、配置优先级/默认值、门禁可靠性、脚本破坏面、CI 隔离、依赖锁 |

## 4. Review Partition（Round 2：Cross-Cutting 专项）

| Agent | 专项 |
|---|---|
| R2-1 | Concurrency / Async / Lifecycle（跨全仓：create_task 所有权、取消传播、超时、锁） |
| R2-2 | Database / State Consistency（跨全仓：事务边界、幂等、缓存与 DB 同步、多真相源） |
| R2-3 | Security Boundary（External Input → API → Service → DB/FS/Network/Shell 全信任边界） |
| R2-4 | Failure Path（DB down / timeout / 外部 API 错 / 畸形数据 / 取消 / 关机 / 部分失败 / 重试） |
| R2-5 | Architecture（依赖、耦合、重复、抽象、职责） |
| R2-6 | Test Coverage / Test Quality（状态机、失败路径、并发、事务、边界、回归） |

## 5. Issue Ledger

字段：`ID | Severity | Confidence | Subsystem | Location | Description | Reporter | Verifier | Status | Duplicate Of`

Status 取值：`Candidate` / `Confirmed` / `Rejected` / `Merged` / `Needs Verification`。
**只有 `Confirmed` 进入最终正式 Issue。**

<!-- LEDGER_TABLE_START -->

### 主 Agent 独立发现（Lead 复核）

#### CR-01 — WorkflowEngine HITL 审批链路三处断裂，且两处静默丢事件

| 字段 | 值 |
|---|---|
| Severity | **S2**（v1 不可达的潜伏缺陷；v2 启用后静默失效） |
| Confidence | **High**（代码事实全部实测确认） |
| Subsystem | `Module/workflow_engine` × `core/hitl.py` × `core/agent` × `core/input` |
| Reporter | 主 Agent（R1-G 范围重叠，待 R1-G 报告交叉比对） |
| Verifier | 主 Agent |
| Status | **Confirmed** |

**证据链（逐条实测）**

1. **快通道把自己投成了非法事件类型**：`core/hitl.py:56-66` 的
   `EventBusHITLNotifier.request_approval` 调
   `self._bus.post(type="hitl_request", data=event_data.model_dump())`——
   `data` 是 **plain dict**，而 `EventBus.post` 的契约是 `data: EventData`
   （`core/monitoring/event_bus.py:39-55`），`Event.data: EventData | None`（:20）。
   `Agent._process_events`（`core/agent/agent.py:732-755`）只按
   `isinstance(data, InterruptData | UserInputData | SystemTickData | TaskResultData | StateChangeData)`
   分派——dict 不匹配**任何**分支，且**没有 else 分支**，事件被静默丢弃、零日志。
   前端也无 `hitl_request` 的 WS 订阅路径（WS 只有 `tool_approval`，`core/input/ws_handler.py:118-125`）。
2. **审计兜底通道同样不处理**：契约声明审批同时走 mailbox 审计流
   （`Module/workflow_contract/schema.py:366,379,402`，`WorkflowEventType.APPROVAL_NEEDED`），
   但 `Agent._poll_mailboxes`（`core/agent/agent.py:673-724`）只处理
   `completed` / `failed` / `cancelled` 三种 `event.type.value`——`approval_needed`
   落空、无日志、无 else。
3. **结果回传路由到错误的审批域**：`WorkflowEngine.submit_approval`
   （`Module/workflow_engine/workflow_engine.py:679-700`）→
   `lifecycle.submit_approval`（`Module/workflow_engine/lifecycle.py:203-231`）→
   `tool_registry.submit_approval(request_id=agent_id)` →
   `ToolManager`/`SafetyPipeline.submit_approval`（`Module/tool/manager.py:538-551`、
   `Module/tool/safety.py:332-348`）在 `self._hitl_requests` 里查 key——
   该字典的 key 是 **SafetyPipeline 自己生成的工具调用 request_id**，
   而 workflow 传的是 **WorkflowEngine 生成的 `agent_id`**
   （`Module/workflow_contract/schema.py:36`）。两者永不相交 → 恒返回 `False`。
   且无任何 WS/REST 入口把客户端审批路由到 `WorkflowEngine.submit_approval`
   （`rg submit_approval` 全仓仅 `core/input/ws_handlers/tool.py:28` 一处调用
   `tool_adapter.submit_approval`，走的是工具域）。

**为何当前不炸**：`Module/workflow_engine/core/finalize.py:115-133`
的 `needs_approval()` **无条件 `return False`**，且 Evaluator v1 从不产出
`EvaluatorAction.ASK_APPROVAL`（`core/evaluator.py:360-367` 仅 ABORT/CONTINUE；
`core/execution.py` 只**消费**该动作）。因此 `WorkflowStatus.WAITING_APPROVAL`
（`orchestrator.py:303-309` / `386-393`）在 v1 不可达。文档亦自述
「v1 不调用，仅持有引用」（`core/executor.py:96,101`）。

**为何仍要报**：接口面（`HITLNotifier` ABC、`HITLData`、`ApprovalId`、
`WorkflowEngine.submit_approval` 公开 API、`WorkflowStatus.WAITING_APPROVAL`、
`AgentStatus.AWAITING_APPROVAL`、前端 `PendingApprovalItem`、`/pending` 契约）
全部按"可用"建成并对外承诺，实现方 `core/hitl.py:30` 自述
「⚠️ 演示代码——不直接用于生产」，却在 `core/agent/_lifecycle_task_system.py:53-56`
被**注入生产 WorkflowEngine**。v2 一打开 `needs_approval`，任务会永久停在
`waiting_approval`，且**三处都静默**（丢事件无日志、路由失败仅返回 False 无日志），
排障成本极高。这属于「静默失效」而非「优雅缺失」。

**建议方向**：① 短期——两处分派加 `else` 明确日志（未知事件类型 warning，
含 type 与 task_id），把静默失效变成响亮失效；`request_approval` 改为构造真正的
`EventData`（或显式定义 `HITLRequestData` 并加入 `EventData` 联合）。② 中期——
把 workflow 审批域与工具审批域分开：`WorkflowEngine` 持有自己的
`_pending_approvals` 表，`ApprovalId` 只在该表内匹配，不复用 SafetyPipeline 的
`request_id` 命名空间；或明确在 v2 落地前将 `HITLNotifier` 标记为
`NotImplemented` 并让 `WorkflowEngine` 在 `hitl_notifier` 为占位实现时拒绝进入
`WAITING_APPROVAL`（fail-fast 而非静默挂起）。

**验证方式**：单测把 `needs_approval` monkeypatch 成 True，跑一个含审批节点的
workflow，断言 ① orchestrator 进入 `WAITING_APPROVAL`；② 前端（WS 或 `/pending`）
能收到该 `approval_id`；③ 客户端提交审批后任务恢复 RUNNING。当前这三条应全红。

---

### Round 1 回收 + 主 Agent 逐项验证结果

Status 图例：`✅Confirmed`（主 Agent 已独立复核）/ `🟡NeedsVerification`（机制成立但触发路径未证实）/
`🔁Merged`（与他项同根因，合并）/ `❌Rejected`（已排除，不进正式报告）

| ID | Sev | Conf | Subsystem | Location | 摘要 | Reporter | Verifier | Status |
|---|---|---|---|---|---|---|---|---|
| CR-01 | S0 | High | Security/Secrets | git history `docs/review-2026-06-01.md:115` @ `f1dcc803` | 真实 DeepSeek API key 明文在 git 历史中，且是 `origin/main` 祖先；**与 `.env` 现役 key 逐字节相同**（未轮换） | R1-J | 主 Agent | ✅Confirmed |
| CR-02 | S2 | High | Input/FSM | `core/status/fsm.py:188-191` + `core/schema/agent.py:27` | `finish_turn` 从 EXECUTING 收尾必抛 `ValueError`，抛后 FSM 停在 EXECUTING，`is_accepting_input` 恒 False→ 消息静默丢弃，唯一恢复是 `Agent.start()` | R1-D | 主 Agent（降级） | 🟡NeedsVerification |
| CR-03 | S1 | High | Lifecycle/Signal | 全仓（`app.py` / `core/agent/server.py`） | 全仓**零信号处理**：SIGTERM 直接硬杀，`stop()` 一步不执行（会话/session/归档/generation 终态全丢） | R1-C | 主 Agent | ✅Confirmed |
| CR-04 | S2 | High | Input/Auth | `core/input/server.py:252-261` | 只配 `AGENT_ACCESS_TOKEN` 时产品 token == 管理 token：可 `DELETE /debug/api/*?confirm=`、`/shutdown`、`/restart`、读全量 prompt | R1-A | 主 Agent | ✅Confirmed |
| CR-05 | S2 | High | Input/DataLoss | `core/input/debug_routes/archive.py:154-159` | `release_all_attachments()` 在 `confirm` 校验**之前**执行 → confirm 写错返回 400，但附件已全部软删，GC 24h 后物理删 | R1-A | 主 Agent | ✅Confirmed |
| CR-06 | S2 | High | Storage/DB | `core/storage/base.py:175-182` + `character_revision_repo.py:94` / `character_repo.py:98` / `user_identity_repo.py:102` | 良性 `DuplicateKeyError` 未走 `non_fatal` → 被当存储故障：503 + 整个仓储判死最长 60s | R1-E | 主 Agent | ✅Confirmed |
| CR-07 | S2 | High | Storage/DB | `mongodb.py:119-124` + `schema_drift.py:136-153` | `ensure_index` 裸 `create_index`：同名不同参 → `OperationFailure` → `connect()` 捕获 → `_db=None` → 整个仓储永久不可用；`schema_drift` 只比索引**名字**，且 `list_indexes` 异常返回 `[]` 被读成"集合不存在"→ **主动误报健康** | R1-E | 主 Agent（部分） | ✅Confirmed |
| CR-08 | S2 | High | Storage/Resource | `core/storage/base.py:129` + `core/storage/memory_repo.py:48-69` | Milvus 不可达时每 60s 泄漏一个 `MilvusClient`（`_mark_unavailable` 不 close 且 `connect()` 无"已连接"守卫）；pymilvus 3.0.1 MRO 无 `__del__` | R1-E | 主 Agent | ✅Confirmed |
| CR-09 | S2 | High | Text/Correctness | `core/text/tokenizer.py:94`(`_WS` 不含 `\r`) + `:549-568` | **CRLF 围栏代码块被整块静默丢弃**（实测：`'```python\r\ncode\r\n```\r\n'` → 0 token，blocks 空，`public_text` 空 → 还会被判"空响应"重试） | R1-I | 主 Agent（复现） | ✅Confirmed |
| CR-10 | S2 | High | Input/WS-Resource | `core/input/ws_handler.py:450-478` | `remove_channel` 排在两个可失败 await 之后且无 try/finally → flush 抛错即 channel **永久泄漏**（广播日志爆炸 + desktop 状态失真 + 断线取消失效） | R1-B | 主 Agent（结构） | ✅Confirmed |
| CR-11 | S2 | High | Input/Idempotency | `core/input/ws_v2.py:68-86` / `:133-140` / `:542-547` | `retryable=True` 的拒绝被落成终态 `REJECTED` 并逐字重放 → 同 `clientRequestId` **永久失败**（`QUEUE_FULL`/`STORAGE_UNAVAILABLE`）；`retryable` 字段语义在错误码间不一致 | R1-B | 主 Agent | ✅Confirmed |
| CR-12 | S2 | High | Input/Audio | `core/input/audio_buffer.py:246-284` | v1 VAD 段无上限（RECORDING 有 600s/18MiB，VAD 没有）→ 缓冲无界 + 一次性巨型 WAV + 30s 超时**整段丢弃** | R1-B | 主 Agent | ✅Confirmed |
| CR-13 | S2 | High | Text/TTS | `core/text/tags.py:27-46` + `Module/tts/text_preprocess.py:4-14` | emoji 判定不完整（VS16 / ⭐U+2B50 / 🫠U+1FAE0 / 旗帜 / ZWJ 序列）→ `strip_for_minimax('❤️')=='\ufe0f'` 非空 → "空句跳过"守卫失效，真的发一次纯不可见字符的合成请求；且不进 `_pending_emojis` 依附队列。**两份 `_EMOJI_RE` 覆盖集不同 = 同一语义两个真相源** | R1-I | 主 Agent（复现） | ✅Confirmed |
| CR-14 | S2 | High | Cognitive/Correctness | `core/cognitive/loop.py:868,890-904` | 循环跑 `MAX_TOOL_ROUNDS+1` 轮但只在 `< MAX_TOOL_ROUNDS` 执行：**第 5 轮工具调用被静默丢弃**，无日志；用户收到"走神了"兜底文案 | R1-D | 主 Agent（静态） | ✅Confirmed |
| CR-15 | S2 | High | Lifecycle/Flush | `core/agent/lifecycle.py:265-277` + `core/memory/consolidator.py:258-313` | 关停 flush 的 `wait_for` 预算 < 内部 LLM 调用耗时：超时取消后 batch 已 detach 且 `except Exception` 捕不到 `CancelledError` → **长期记忆提取结果永久丢失**，日志只说"超时" | R1-C | 主 Agent（静态） | ✅Confirmed |
| CR-16 | S2 | High | Lifecycle/Admission | `core/agent/agent.py:589,612-622` | `process_message` 不看 `_state`：shutdown 窗口内仍返回 True + post 事件，但消费者已停 → **消息被"确认收到"却永久丢失**（v2 幂等表落 accepted，客户端重试也不再处理） | R1-C | 主 Agent | ✅Confirmed |
| CR-17 | S2 | High | Lifecycle/Config | `core/agent/server.py:512-524` | `/restart` 只重载 `self._config`，组件图仍持旧值（db/角色/user_name/memory/llm.model）却打印"配置已重新加载"；且持久化 `service_state.llm_model` 会**反向覆盖** YAML 新模型 | R1-C | 主 Agent | ✅Confirmed |
| CR-18 | S2 | High | Storage/DataLoss | `core/storage/attachment_gc.py:152-170` vs `:218-221` | GC 规则 2 无"kind 目录缺失"守卫（规则 4 有）→ mongorestore 恢复 DB 未恢复 uploads 卷 → **全部附件记录被永久删除** | R1-E | 主 Agent | ✅Confirmed |
| CR-19 | S2 | High | Storage/Consistency | `core/storage/generation_repo.py:302-322` | `mark_stale` 写侧丢掉读谓词（TOCTOU）→ 正常完成的 generation 被覆写成 `interrupted`；返回值计的是迭代次数而非 `modified_count` | R1-E | 主 Agent | ✅Confirmed |
| CR-20 | S2 | Medium | Storage/Idempotency | `core/input/ws_v2.py:148-165` + `generation_registry.py:168-178` | 幂等"提交证据"探测 fail-open：连接不可用/查询异常/真不存在三态归一为"没处理过"→ TAKEOVER → **重复 generation**（D6c 机制被绕过） | R1-E | 待 Round 2 DB 专项 | 🟡NeedsVerification |
| CR-21 | S2 | High | Storage/Consistency | `core/storage/character_version_repo.py:186-204` | `append_version` 非原子 `max+1` + 裸 `insert_one`（绕过 `_call`）→ 唯一索引健康时未捕获 `DuplicateKeyError` 冒泡 500；索引降级后（**永不自我修复**）静默重复版本号污染 L2/C6 历史 | R1-E | 主 Agent | ✅Confirmed |
| CR-22 | S2 | High | Input/Auth-SSE | `core/input/ws_handler.py:236` | 服务端未关 `permessage-deflate`（aiohttp 默认 `compress=True`）→ 已有真实联调复现：空闲 75s 后首帧 `non-zero reserved bits` → 1002 断连 + text 静默丢失 | R1-B | 主 Agent（代码）+ 历史文档 | ✅Confirmed |
| CR-23 | S3 | High | Input/Validation | `provider_routes.py:331` / `session_routes.py:582` / `debug_routes/ltm.py:136` 等 5 处 | 非 object JSON body → 未捕获 `AttributeError` → 纯文本 500（脱离 `ProtocolError` 信封）；无兜底异常中间件 | R1-A | 主 Agent | ✅Confirmed |
| CR-24 | S3 | High | Input/Observability | `message_repo.py:327-328,510-511` | Mongo 离线（启动期）时历史接口 200 空页，与 `/ready` 的 fail-closed 相反；用户看到"历史空了"且无错误 | R1-A | 主 Agent | ✅Confirmed |
| CR-25 | S3 | High | Input/Performance | `session_routes.py:392-456` + `middleware.py:72-79` | `/api/sessions/{id}/resources` 单请求可全档案扫描 + 每候选一次串行 Mongo 往返，且**不在限流映射内** | R1-A | 待 Round 2 性能确认 | 🟡NeedsVerification |
| CR-26 | S3 | High | Input/Performance | `upload_routes.py:335` / `:151-193` | async handler 内同步 `write_bytes`（≤50MB）+ 请求体全量常驻内存（≤64MB × 60 并发） | R1-A | 主 Agent | ✅Confirmed |
| CR-27 | S3 | High | Lifecycle/Resource | `core/agent/_lifecycle_services.py:142-148` + `server.py:435-448` | TTS/STT"创建成功但连通性测试失败"分支直接丢弃对象不 `close()` → httpx 客户端泄漏（同文件 `switch_*` 已正确关闭 = 漏改） | R1-C | 主 Agent | ✅Confirmed |
| CR-28 | S3 | High | Storage/Query | `generation_repo.py:210-212` + `character_repo.py:135-138` | 幂等证据热路径 `find_by_user_message` 的 filter 字段**无索引** → COLLSCAN；多个 sort key 无覆盖。`EXPECTED_INDEXES` 只钉名字，天然看不见 | R1-E | 待在真实库复核 | 🟡NeedsVerification |
| CR-29 | S3 | High | Storage/Security | `message_repo.py:604-605` | debug 归档搜索把未转义、无限长的用户串拼进 Mongo `$regex`（同文件 `:126-129` 的 `query_search` 已 `re.escape`+200 上限 → 只修了一处） | R1-E | 主 Agent | ✅Confirmed |
| CR-30 | S3 | High | Storage/DataLoss | `secret_store.py:63-71,116-124` | 密钥文件损坏 → `_load` 返回 `{}` 仅记 warning → 下一次 `set_secret` 用只含一条的 dict **覆盖整份文件**，其余 provider 密钥静默消失 | R1-E | 主 Agent | ✅Confirmed |
| CR-31 | S3 | High | Cognitive/Observability | `core/agent/_lifecycle_tools.py:79` + `core/tools/self_state/plugin.py:29` | `services["_fsm_state"]` 只在启动写一次 → `get_self_state` 恒返回启动时状态（恒 idle），**工具系统性给出错误自述** | R1-D | 主 Agent | ✅Confirmed |
| CR-32 | S3 | High | Cognitive/Concurrency | `core/cognitive/finalize.py:314-324,342-395` | 回合收尾后半段读 loop 单例字段（`_current_runtime`/`_generation_ctx`）而非本回合局部量 → 与下一回合 `_prepare_turn` 交错时 A 的 runtime 不落库、state 版本错 | R1-D | 待 Round 2 并发专项 | 🟡NeedsVerification |
| CR-33 | S3 | High | Text/Streaming | `core/text/tokenizer.py` `_feed_normal` 的 `_pending` | TEXT_DELTA 粒度从"每 chunk"退化为"每空白/标签边界" → 无空格纯中文回复整段扣在 `_pending`，**TTFT 落到 EOF** | R1-I | 待 Round 2 复核 | 🟡NeedsVerification |
| CR-34 | S3 | High | Input/Audio-Perf | `core/input/ws_handler.py:440-444,506-509` | v1 `await audio_buf.feed()` 挂在消息循环体内 → 一次分段转写期间 interrupt/text 全排队（≤30s）；teardown 吞 `CancelledError` 后阻塞 ≤30s 才 `remove_channel` | R1-B | 主 Agent | ✅Confirmed |
| CR-35 | S3 | High | Input/API | `core/input/ws_handler.py:105-113` + `ws_handlers/voice.py:227-242` | voice 错误帧 `requestId` 取自 ContextVar 陈旧值 → 关联到**无关请求**（比省略更糟） | R1-B | 主 Agent | ✅Confirmed |
| CR-36 | S3 | High | Input/Resource | `frontend_contract/ws_message/chat.py:191` + `ws_v2.py:314-339` | WS `attachments` 无数量上限且不去重 → 16MB 帧可含 ~10 万条引用，逐条串行 Mongo 往返独占消息循环 | R1-B | 主 Agent | ✅Confirmed |
| CR-37 | S3 | High | Input/Resource | `core/input/debug_routes/archive.py:291-299,332-337` | debug 删除路径 `except Exception: warning` 吞掉附件释放失败，违反 `attachment_release.py:50-53` 文档化契约（"必须传播"）→ 会话已删、refs 残留、响应称成功 | R1-A | 主 Agent | ✅Confirmed |
| CR-38 | S3 | High | Input/Ops | `core/input/debug_routes/__init__.py:33-37` + `frontend/src/stores/debugStore.ts:22` | `/debug` 仪表盘无法携带 admin token（fetch 无 Authorization、`EventSource` 协议上加不了 header）→ 配 `SERVER_AUTH_TOKEN` 后整页 401 | R1-A | 主 Agent | ✅Confirmed |
| CR-39 | S3 | High | Docs/DRY | `CLAUDE.md` / `core/README.md` | 文档漂移：`core/README.md:83` 称"39 个 property"而 `CLAUDE.md` 称"45 个"；`CLAUDE.md` 称 full 套件 ~22min 而实测 **263s** | 主 Agent | 主 Agent | ✅Confirmed |

**Round 1 明确"未发现问题"的正面结论（供报告引用）**

- 认证覆盖面/路径归一化绕过（`//api/`、`/./api/`、`/%2e/`、`..%2f` 原始 socket 实测全 404/401）
- 路径穿越（`/uploads/images/..%2f..%2fconfig.yaml`、`/static/..%2f..%2fconfig.yaml`、`%2e%2e` 变体全 404）
- 上传魔数/kind 双向校验、SSRF（`validate_endpoint` + guarded client）、限流器内部实现
- `_WS_HANDLERS` 22 键与 `ClientMessage` 联合 22 tag **完全一致**（无 v2 漏改）
- `_ws_active` 计数 finally 对称无漏减；每连接严格串行无 ctx 交错改写
- 流式切分**跨 chunk 不变式实测成立**（7000+ 次差分 fuzz 含逐字符切分 + 全标签前缀，0 反例）
- 全仓生产代码 `shell=True`/`os.system`/`eval`/`exec`/`pickle` **0 命中**；裸 `except:` 仅 1 处
- `.env` 未被 git 跟踪、权限 600；工作树内 `rg 'sk-[A-Za-z0-9]{20,}'` **0 命中**；21,024 个历史 blob 全量扫描仅 1 个唯一 `sk-` 密钥、0 个 `tvly-`、0 个私钥
- 全仓无 `logger.*(api_key|access_token|auth_token|secret)` 命中；`ConfigField(secret=True)` 覆盖 4 个敏感项

**Round 1 排除的候选（❌ 不进正式报告）**

| 候选 | 排除理由 |
|---|---|
| pymongo/motor 客户端泄漏 | 实测否决：丢弃 5 个已发起操作的 `AsyncIOMotorClient` 后 `asyncio.all_tasks()` 增量为 0（motor 有 `__del__` 兜底） |
| IDOR / 越权读他人会话 | 单用户模型已冻结（access token ≠ user principal），报越权是假阳性 |
| Milvus filter 字符串插值注入 | `user_id`/`instance_id`/`visibility` 均非客户端可控（profile / uuid5 / 枚举） |
| `EventBus` 订阅者异常/慢订阅者/订阅表迭代 | `EventBus` 实为 `PriorityQueue`，**没有 subscribe API**——任务书假设与代码不符 |
| `log_setup.setup()` 重复调用 handler 翻倍 | 全仓仅 `app.py:116` 一处调用 → 无触发路径 |
| `_execute_tools_directly` 吞 `CancelledError` | 同款：只服务直接构造 loop 的测试路径，生产不可达 |
| `IdleMonitor` 裸 `create_task` | 已在 R6 登记；`stop()` 正确 cancel+await |
| `save()` 全文档 `$set` 覆盖 | 未证实交错；唯一调用方恒传完整 binding |
| `_scan_resources` 死循环 | 不可达（`query_page` 只回 Mongo 文档，必写 `_mongo_id`） |
| `/health` `/ready` 未鉴权暴露计数 | 价值低（仅 model 名与计数） |

**Unresolved（需 Round 2 / 产品确认）**

1. `permessage-deflate` 是否影响**浏览器**客户端（文档复现用 Node `ws`）——若命中则 CR-22 升 S1。
2. 生产库现存索引的 `unique`/`expireAfterSeconds` 是否与代码声明一致（CR-07 唯一未知量；需 `MONGODB_URI`）。
3. 是否严格单进程部署（`desktop_call_repo.mark()` 的迁移守卫基于进程内 cache，多 worker 下退化为最后写入者胜）。
4. `scripts/start.sh` 经 `conda run` 启动，SIGTERM 是否透传到 python（决定 CR-03 修复是否需在 `start.sh` 补 `exec`）。
5. 多客户端并存是否受支持（`activate_session` 改**全局** active 指针，B 切换后 A 的无 conversationId text 落到 B 会话且无通知）。
6. 后台任务终态是否要求跨重启不丢（当前 `AgentInfra` 无任何 repo）。
7. 打断后不归档半截回复是否为产品有意取舍。

### Round 1-G 回收（Module/workflow_engine + workflow_contract）

关键生产事实（影响定级）：core 侧只调用 `start/stop/submit/constraints/list_active_tasks/get_task_mailbox`
（`core/cognitive/tools.py:294`、`loop.py:1061`、`tool_phase.py:180`、`_lifecycle_task_system.py:57/87`）。
**`cancel/pause/resume/get_progress/get_task_dag/get_token_*/list_agents` 在生产代码中零调用方**（仅测试），
`WorkflowEngine.submit_approval` 被 `core/input/ws_handlers/tool.py:28` **绕过**（走 ToolAdapter→ToolManager）。
故 C 类（取消/超时）缺陷多为**潜在可达**。`submit/stop/plan/execute` 是热路径。

| ID | Sev | Conf | Location | 摘要 | Status |
|---|---|---|---|---|---|
| CR-40 | S2 | Medium | `sub_agent/replan.py:310-319` + `execution_loop.py:112-117` + `orchestrator.py:231-234` + `finalize.py:46-56` | **全量重建 Replan 丢弃合并态**：`graph.replace()` 后 `resume_from=None` → `_init_exec_state` 全 PENDING → 已 SUCCESS 节点**整图重跑**（重复副作用）。对照 DAGPatch 分支返回 `checkpoint.exec` 走 `merge_exec_state` 保留 SUCCESS = 设计意图本应保留 | ✅Confirmed（含测试固化） |
| CR-41 | S2 | High | `_lifecycle_task_system.py:83-89` + `workflow_engine.py:220,267-317` | 外层 `wait_for(stop(), 5.0)` **<** 内层 `stop(timeout=10.0)` → 只要有活跃任务就**必然**截断：`task.cancel()`、孤儿工具 3s 采集、兜底清理全部跳过；任务协程继续在已拆毁的资源上 await。代码自己的注释已知该风险但低估为"可能" | ✅Confirmed |
| CR-42 | S2 | High | `executor.py:164,315-340` + `execution.py:292-350` + `evaluator.py:380-388` | **重试是盲重跑**：`Executor.execute()` 签名不含 `NodeState.error` → 重试 prompt 与首次逐字节相同；无退避、无幂等键、不区分工具幂等性 → 副作用最坏 4 次（注释写"共 3 次"与实现不符，测试已固化 4 次）；叠加 ≤5 次 REPLAN（`merge_exec_state` 重置 attempt=0）→ 单节点最坏 ~20 次 | ✅Confirmed |
| CR-43 | S2 | High | `workflow_contract/schema.py:141-148` vs 全部读取点 | 声明为"**（硬）**约束"的上限在引擎侧几乎无执行点：`max_task_duration_s`/`max_total_tokens` **只在 NEEDS_REPLAN 时**检查；`max_mutations` 纯 prompt（`MUTATION_LIMIT_REACHED` 全仓无生产者）；`DAGNode.max_duration_s` 死字段；`_run_and_cleanup` 无 `wait_for` → 最坏 ~480 次节点执行且无看门狗 | ✅Confirmed |
| CR-44 | S2 | Medium | `plan_validator.py:87-176` + `graph.py:126-156` + `executor.py:241-266` | `input_bindings`（数据依赖）与 `edges`（执行依赖）**两套独立声明**，校验器无交叉一致性检查；`_resolve_inputs` 取不到产物只 `logger.warning` 后继续 → 节点带垃圾输入执行，LLM 自洽编造仍可能判成功 | ✅Confirmed |
| CR-45 | S2 | Medium | `graph.py:166-181` + `orchestrator.py:153-156` + `finalize.py:205-220` | **空 DAG 被判"全部 required 节点成功"（空真）** → 任务以 `COMPLETED`（0/0 节点）**假成功**收尾。实测复现：`remove_nodes` 清空图 → `is_terminal(empty)==True`、`progress=0.0`。`compute_progress` 与 `is_terminal` 语义冲突 | ✅Confirmed（复现） |
| CR-46 | S2 | High | `schema/internal.py:127-128` + `result_handlers.py:35-58` + `workflow_engine.py:439-452` + `planner/parser.py:80-82` | 任务级失败原因在契约面**丢失**：`WorkflowState.error`/`error_code` **全仓零写入**（只有 `NodeState.error` 有写点），但被 `WorkflowSummary`/debug 快照/前端 TaskCard 消费；`PLANNING_FAILED` 等 6 个 `WorkflowErrorCode` **不可达**；规划 LLM 空响应**无重试**（对照 Evaluator 有）→ 一次抖动判死整个任务，统一报 `internal_error` | ✅Confirmed |
| CR-47 | S3 | High | `graph.py:80-84`（丢边）+ `:257-265`（死检查） | `from_plan` 对端点不存在的边只 warning 后 `continue` → 边**静默删除**（依赖消失，可能提前执行）；而 `validate_structure` 的 `MISSING_DEPENDENCY` 分支遍历图内 predecessor → **恒不可达**（"有兜底、无来源"的假保护） | ✅Confirmed（复现） |
| CR-48 | S3 | High | `debug_api.py:140-206,210-232` + `workflow_engine.py:453-471` | debug SSE：任务一进终态即被 `_cleanup_task` 摘除而轮询是 500ms → 短任务**从未被观测**；补发的 `task_done` 带**旧状态**（前端把 running 任务归入"已完成"且 error 丢失） | ✅Confirmed |
| CR-49 | S3 | Medium | `debug_api.py:73-75,244` + `input/debug_routes/__init__.py:83-90` | `WorkflowDebugAPI.stop()` 用**阻塞** `await q.put(_SENTINEL)` 且无超时，而广播用 `put_nowait`+丢弃 → 慢/半开 SSE 客户端把 256 队列打满时 **shutdown 永久挂起**（外层无 `wait_for`） | ✅Confirmed |

**R1-G 明确排除**（❌ 不进报告）：环检测未覆盖自环（实测覆盖）；"失败节点下游永久挂起"（不可达，有死锁兜底）；
in_degree 重复递减（不做递减，每轮重算）；fan-out 打爆（v1 顺序执行）；子智能体无限嵌套（`submit_task` 不在 ToolManager 注册表，结构上不可能）；
父任务取消不级联（无子任务层级，在途工具按 E3 设计 shield）；`except Exception` 吞取消（3.12 下不吞）；
`execution_loop` 死循环（有 1000 轮上限 + meltdown）；`execution.py`/`executor.py` 职责重叠（无重复逻辑）；
mailbox 重复投递/乱序（SPSC + 同步 push + 终态唯一守卫）；重启后任务永久 running（全程内存，无持久化列表）。

**R1-G Unresolved**：① `_stop_task_system` 的 5s 是刻意还是参数漂移（决定 CR-41 修法）；
② `AgentInfra.llm` 是否与被 `Agent.stop()` 关闭的 LLM 客户端同一实例（若同，CR-41 后果升级为"在途 LLM 打到已关闭 client"）；
③ `ToolManager` 侧是否有任务级去重（决定 CR-42 爆炸半径）；
④ `parse_plan_from_dict` 的 `pydantic.ValidationError` 现实频率（穿透 `except PlanParseError`）。

<!-- LEDGER_TABLE_END -->

## 6. Unresolved Questions（待验证清单）

<!-- UNRESOLVED_START -->
（Round 1 结果回收后填充）
<!-- UNRESOLVED_END -->

## 7. 基线验证记录

| 检查 | 命令 | 结果（2026-09-12）|
|---|---|---|
| 测试收集 | `conda run -n Elysia python -m pytest --collect-only -q` | 4,439 collected，8s |
| Lint | `ruff check core/ Module/ frontend_contract/ scripts/` | pass |
| 类型 | `conda run -n Elysia pyright` | 0 errors |
| 架构门禁 | `python scripts/check_architecture.py` | pass（3 项 R1 豁免）|
| 单元+服务测试 | `conda run -n Elysia python -m pytest -m "unit or service" -q` | 见 §7.1 |
| 契约漂移 | `python -m frontend_contract --check` 等 | 见 §7.1 |

### 7.1 待填
### 7.1 实测结果（2026-09-12，基线 `4aa3f8ef`）

| 检查 | 命令 | 结果 |
|---|---|---|
| 测试收集 | `pytest --collect-only -q` | **4,439 collected**（8s）|
| 单元 + 服务层 | `pytest -m "unit or service" -q` | **4392 passed, 2 skipped, 45 deselected**，245.76s |
| 全量（非 external）| `pytest -m "not external" -q` | 见 job（后台）|
| Lint | `ruff check core/ Module/ frontend_contract/ scripts/` | **All checks passed!** |
| 类型 | `conda run -n Elysia pyright` | **0 errors, 0 warnings, 0 informations** |
| 架构门禁 | `python scripts/check_architecture.py` | **通过**（R1/R2/R3/R4/R6；3 项 R1 豁免）|
| 契约漂移（ts/schema/raw_ts）| `python -m frontend_contract --check` | 通过（无输出）|
| 契约 golden frames | `python scripts/gen_contract_frames.py --check` | 通过（无输出）|
| 错误码单源 | `python scripts/check_error_codes.py` | 通过（54 码与契约一致）|
| 依赖锁 | `python scripts/check_env_lock.py` | 通过（21 个顶层包均在锁中）|
| Compose 安全 | `python scripts/check_compose_security.py` | 通过（仅 loopback、无默认凭据）|
| CI 信任边界 | `python scripts/check_ci_isolation.py` | 通过（无 PR 可达 self-hosted）|
| Bandit | `bandit -r core/ Module/ frontend_contract/ scripts/ -q` | **11 项 Low / 0 Medium / 0 High**；56,870 LOC 扫描 |

> 结论：**项目的自动化门禁体系是真实有效且全绿的**。这意味着本次 Review 的价值不在
> "找门禁能发现的问题"，而在门禁**覆盖不到**的语义正确性、并发、失败路径与跨模块一致性。

Bandit 11 项明细（全部 Low，均为固定参数的 subprocess）：
`core/input/wakeword.py:138` B112、`core/storage/token_repo.py:113` B112、
`core/version.py:33` B607/B603、`frontend_contract/__main__.py:119` B607/B603、
`scripts/gen_contract_frames.py:461` B607/B603、`scripts/print_test_stats.py:21` B603、
`scripts/stage4_smoke.py:36` B607/B603。

**测试规模分布（分层）**：4,439 用例中 `unit or service` 占 4,392（99%），
45 个 `integration`、`external` 独立 opt-in。全量套件约 4 分钟（本次实测 245s
覆盖 99% 用例），与 CLAUDE.md 宣称的 "full ~22min" 不一致——**实际全量成本远低于文档**，
说明 `make test-full` 的文档描述已过期（S3 级文档漂移，由 R1-J/R2-6 复核）。
<!-- BASELINE_END -->
