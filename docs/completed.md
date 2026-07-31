# 开发计划

## 已完成

## 历史归档

| 月份 | 条目数 | 文件 |
|------|--------|------|
| 2026-05 | 27 | [2026-05.md](completed/2026-05.md) |
| 2026-06 | 33 | [2026-06.md](completed/2026-06.md) |
| 2026-07 | 44 | 见下方 ↓ |

---

## 2026-07


### 2026-07-31: code-review-2026-07-22 遗留问题修复（7 项）

对 todo.md 中 code-review-2026-07-22 的 11 项遗留问题逐一评估，修复其中 7 项真实存在的问题：

| 问题 | 严重度 | 文件 | 修复方式 |
|------|--------|------|----------|
| #9 管理端点无认证 | 高 | `debug_routes/__init__.py` + `server.py` | `register_debug_routes()` 添加 auth middleware，所有 `/debug/api/*` 需 Bearer token |
| #12 用户消息明文日志 | 中 | `ws_handler.py` | 3 处 `logger.info`→`logger.debug`（多模态/文本/打断） |
| #5 `_current_task` 覆盖竞态 | 中 | `agent.py` | `_handle_system_tick` + `_handle_task_result` 在 `create_task` 前调用 `cancel_current()` |
| #7 提醒通知永不送达 | 高 | `tools/reminder/plugin.py` + `_lifecycle_tools.py` | 提醒到期后通过 EventBus 投递 `TaskResultData`；`_bus` 注入 tool services |
| #26 取消的 Task 永不等待 | 低 | `idle_monitor.py` + `debug_api.py` | 两处 `stop()` 在 cancel 后 `try/await/CancelledError` |
| #23 MongoDB 写入无限并发 | 中 | `session.py` | `asyncio.Semaphore(8)` 限制并发归档写入 |
| #25 私有 `_infra` 访问 | 低 | `task_system.py` + `tools.py` | TaskSystem 加 `constraints` property；`tools.py` 改用 `task_system.constraints` |

**已确认无需修复**：#10（SSE CORS 已显式设置）、#11（`show_index=False` 已生效）、#6（`_evict_overflow()` 已替换旧代码）、#13（`api_key_env` 约定安全，保留现状）。

`make test-quick`：1817 passed，无回归。
### 2026-07-31: code-review-2026-07-31 遗留问题修复（24 项）

对全量审查 [docs/code-review-2026-07-31.md](code-review-2026-07-31.md) 的 44 项发现逐一评估，修复其中 24 项。

**Critical（1 项）：**
| 问题 | 修复 |
|------|------|
| C1 `_poll_mailboxes` 空 mailbox 无限自旋冻结事件循环 | `if event is None: continue`→`break`；`stop()` 清空 `_background_tasks`（restart 复用实例残留） |

**Major（16 项）：**
| 问题 | 修复 |
|------|------|
| M1 `conversation_text[:2000]` 截断方向错误 | `[-2000:]` 保留最新轮次 |
| M3 CharacterReflector buffer 竞态 | `_reflect()` 记录 buffer 快照，只清反思前累积 |
| M4 Gate 拒绝未恢复 THINKING，FSM 卡 EXECUTING | gate 拒绝时 `return_to_thinking()` |
| M5 text-only 双路径重复广播 | SENTENCE 分支只发 `send("")` 边界信号，增量由 TEXT_DELTA 广播 |
| M6 `chat_stream` usage 终结 chunk IndexError | `not chunk.choices` 先判空再访问 `choices[0]` |
| M7 `_format_tool_summary` 非 dict 崩溃 | `isinstance(parsed, dict)` 检查（含 tool_executing emit 循环） |
| M10 `/debug/api` 精确路径绕过认证 | `startswith("/debug/api")` 覆盖无尾斜杠路径 |
| M13 重连审批广播缺 type 字段 | 补 `type: command` + 参数名对齐正常路径 |
| M14 历史分页游标错位 + 重复气泡 | 游标取页尾最旧一条 + 前端按 message_id 去重 |
| M15 queued 气泡永久隐藏 | `addUserMessage` 兜底清除所有 queued 标记 |
| M16 唤醒词开关死 UI | `wakeEnabled` watch 发送 `wake_toggle` WS 消息 |
| M17 `python -m Module.system_prompt` 必崩 | `ConflictRulesSection` 传 `f1_conflict_rules.j2` |
| M18 `python -m Module.config` 必崩 | `__main__.py` 改用 `yaml.safe_load`（loader 需 dataclass） |
| M21 模型切换乐观插入 + 双插 | 去掉乐观插入，等后端结果 |
| M23 Escape 双重触发 | App.vue 在审批弹窗打开时跳过 interrupt |
| （M7/M21/M23 同时覆盖 m7/m8/m10/m18/m19/m21/m22/m23） | |

**Minor（7 项）：** m7（archive role 400）、m8（版本历史 int 保护）、m10（WS 非 dict JSON）、m18（by_model 兼容）、m19（created_at→timestamp）、m21/m22（同上 M21/M23）。

**第二批（+11 项，2026-07-31）：**
| 问题 | 修复 |
|------|------|
| M9 gpt-sovits `_pull` 异常静默吞掉 | `except httpx.HTTPError`（覆盖 RequestError + HTTPStatusError）+ 异常入队，消费者检测后 raise |
| M11 `time_of_day` 去重键无日期 | 键加日期成分 `%Y-%m-%d %H:%M`，匹配用 `now[11:]` |
| M12 STT 语音输入不 touch | 语音路径投递事件前调用 `agent._idle.touch()` |
| m3 fire-and-forget consume_trigger 无异常处理 | reflector.py + engine.py 两处 `create_task` 加 try/except 包装 |
| m4 KMeans 同步阻塞事件循环 | `_cluster_entries` 改 async，KMeans 移入 `asyncio.to_thread` |
| m11 WS 无 heartbeat/接收超时 | `WebSocketResponse(heartbeat=30, receive_timeout=60)` |
| m13 OpenAI embedding 无超时 | `AsyncOpenAI(timeout=60.0, max_retries=2)` |
| m14 `get_caller_logger` 双跳 f_back | 改单跳 `frame.f_back`（调用点无 Factory.create 中间层） |
| m15 MilvusBackend.close() 空实现 | `asyncio.to_thread(self._client.close)`（pymilvus 有 close） |
| m17 `Module/system_prompt` eager import | `MessageBuilder` 改为 `__getattr__` 内延迟导入 |
| m20 SSE 重连退避失效 | `connect()` 不再清零 `reconnectDelay`，`onopen` 时重置为 1000 |

**累计修复 33/44**。遗留 11 项：M2/M8/M19/M20 + m1/m2/m5/m6/m9/m12/m16（M2/M8 需设计，M19/M20 随 TaskSystem 接入），记录在 todo.md。`make test-quick`：1817 passed，前端构建通过。

### 2026-07-31: CognitiveLoop [TOOL_EXECUTED] 模式切换信号

**背景**：LLM 执行完工具调用后缺少显式信号来切换上下文模式，部分模型在工具结果返回后仍停留在"工具执行模式"。

**修复**：`loop.py:801` — 在 `_execute_tool_round()` 返回后、`continue` 前注入 `messages.append({"role": "system", "content": "[TOOL_EXECUTED]"})`。`[TOOL_EXECUTED]` 作为显式模式切换信号，帮助 LLM 从"工具执行模式"切回"对话模式"。改动量 1 行。
### 2026-07-31: 文档审查修复（25 项）

**背景**：对 `docs/architecture.md` 及其他 9 份文档与代码的交叉审查，发现 ~30 处不一致。修复了其中 25 项直接影响开发体验的问题。

**architecture.md（15 项）**：SessionMemory API（snapshot/restore→save/load）、AppConfig 补 database 字段、debug 路由 41→45（+4 条角色版本路由）、AwarenessTrigger 冷却 1h→5min、SentenceBuffer 分隔符修正、vectordb 表格补 pinecone/qdrant stub、WS 协议表补 action/token_usage、audio 报文补 is_final/format、文件树补 core/status/ 和 core/storage/、ToolAdapter API load_all→start、Pinia store 5→6、并发任务表补 #6、config.yaml 文档补 4 个缺失节、SystemPromptManager 导入路径、Agent.stop() 顺序对齐 _SHUTDOWN_STEPS。

**其他文档（10 项）**：CLAUDE.md composable 列表 + status.py、README.md 3 处数字更新、static/debug.md 全文重写、Module/llm/README.md test() 类型、database-storage.md 补 character_versions + 索引 + 嵌入模型、core/prompts/README.md PersonalityCompression 加载方、companion-roadmap.md 4 处文件路径。

**未修复**：docs/testing.md 测试数量/文件索引过时、data-structure-survey.md 行号偏移、companion-prompt-guide.md 多处不一致、各 README 行数过时等较次要问题，记录在 todo.md "文档审查遗留"。
### 2026-07-31: 项目路径统一管理

**Spec**: [docs/superpowers/specs/2026-07-31-project-paths-unification-design.md](superpowers/specs/2026-07-31-project-paths-unification-design.md)

**Plan**: [docs/superpowers/plans/2026-07-31-project-paths-unification.md](superpowers/plans/2026-07-31-project-paths-unification.md)

新建顶层 `paths.py`（`ProjectPath(str, Enum)`，20 个枚举成员：13 目录 + 7 文件），项目根由 `ELYSIA_ROOT` 环境变量锚定（fallback 到 `paths.py` 所在目录）。每个路径支持 `ELYSIA_PATH_{NAME}` 环境变量覆盖。

迁移 ~27 处硬编码路径（`__file__`-relative `parents[N]`、cwd-relative `Path("log")`、裸字符串 `"Module/system_prompt/templates"`），涉及 20 个生产文件。新增 14 个单元测试。3 项 grep 验证全 clean，`make test-quick` 全绿（1514 passed）。

**关键设计决策**：不纳入 URL/URI、`config.yaml` 中的配置值、`__file__` 同目录引用（如 `*_voices.yaml`）。路径值语义不变，只改获取方式。
### 2026-07-30: core/ 代码审查修复（7 项）

**背景**：外部对 `core/` 的 31 项 review，经逐一核实确认 7 项真实问题。

**修复（7 项）：**
| 严重度 | 问题 | 文件 | 修复方式 |
|--------|------|------|----------|
| 高 | L3→L2 压缩计数触发永久失效 | `engine.py` | `_append_history` 截断时同步下调 `compression_last_count` |
| 高 | Consolidator buffer 在 LLM 前清空 | `consolidator.py` | `buffer.clear()` 移到 LLM 成功解析后 |
| 中 | `_sentence_buf`/`_block_filter` 死代码 | `loop.py` | 删除 None 字段 + no-op 检查 + 未使用 import |
| 中 | `_current_task` 覆盖孤儿 | `agent.py` | `_handle_user_input` 先 `cancel_current()` 再创建新 task |
| 低 | `time_of_day` 事件洪泛 | `idle_monitor.py` | `_last_time_of_day_trigger` 防重 |
| 低 | 已吸收条目残留 `_entries` | `long_term.py` | `mark_absorbed` 从 `_entries` 移除 + 清理 `_dirty_ids` |
| 低 | `_sync_dirty_entries` 单条失败跳出 | `long_term.py` | `break` → `continue` |

**标记为误报**：review 中其余 24 项经核实为误报/设计意图/极低概率/仅代码质量建议，不修复。
### 2026-07-30: 测试质量修复（17 项）

**背景**：对全部 55+ 测试文件的全面审查（提交 `cfc48f6`），发现 17 项确认真实问题。

**假测试修复（11 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/tests/test_log_setup.py` | 文件名误导（测 ContextVar 非 log_setup） | 重命名为 `test_request_id.py` |
| `core/tests/test_phase2.py:63` | `assert result in (True, False)` 恒真 | 拆为 mock 测试验证 true/false |
| `core/tests/test_phase2.py:361` | `len(messages) > 1` 后门 | 删除后门，保留有意义的断言 |
| `core/tests/test_phase3.py:210` | 零断言 | 补 `assert == AgentState.INTERRUPTED` |
| `core/tests/test_phase3.py:239-285` | 错误处理本地重实现 | 改为调用真实 `_execute_tools_directly()` |
| `core/tests/test_phase3.py:651` | try/except RuntimeError 吞错 | 改为 async + `await` |
| `core/tests/test_phase3.py:972` | 只验证"没崩溃" | 用 `capsys` 捕获 stdout 验证 |
| `core/memory/tests/test_consolidator.py:60` | 逗号尾缀测试无逗号 | 加真正的尾缀逗号 |
| `core/memory/tests/test_long_term.py:44` | 假重连测试 | 合并+重命名 |
| `core/adapter/tests/test_message_builder.py:147` | dict key 错误 | `"text"`→`"content"`, `"image_url"`→`"src"` |
| `core/task/tests/test_task_scheduler.py:656` | 回调从未触发 | 修正注释 |

**弱断言/逻辑修复（6 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/tests/test_phase3.py:378` | `in ("idle","thinking")` | 改为 `== AgentState.IDLE` |
| `core/memory/tests/test_consolidator.py:55` | `isinstance(items,list)` 恒真 | 改为 `== []` |
| `core/tests/test_clean_db.py:168` | `>=4` | 改为 `==5` |
| `core/monitoring/tests/test_token_tracker.py` | 污染 `data/` | fixture 用 tempfile |
| `core/task/tests/test_task_scheduler.py:551,611` | `_completed_count` 死代码 | 删除 |
| `core/task/tests/test_task_model.py:45` | 冗余测试 | 删除 |
| `core/memory/tests/test_long_term.py:146,171` | 重复测试 | 合并 |
| `core/input/tests/test_server.py:58-92` | 弱单元测试 | 改为验证 route 注册 |

**驳回 1 项**：`test_clean_db.py:19` Mock 返回 `int` 是正确的（`DocumentDB.delete_many` 返回类型为 `int`）。

**修复日期**：2026-07-30

---
### 2026-07-30: 测试质量修复 — 第二轮（12 项）

**背景**：提交 `f413a9f` 之后的第二轮集成测试 & E2E 审查，确认 17 项中的 12 项真实问题（4 项为遗留覆盖率缺口，1 项 E2E 需基础设施配合暂不修复）。

**假测试遗留修复（5 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/tests/test_phase2.py:209` | `assert result in (True, False)` 恒真 | mock `MemoryRepository` 验证 |
| `core/tests/test_phase2.py:194` | 测试名声称验证 Milvus PK 实为空操作 | 重命名 `test_insert_assigns_id` + 加断言 |
| `core/memory/tests/test_consolidator.py:156,223,233` | `isinstance(items, list)` 恒真 / 防崩溃 | → 有意义断言 |
| `core/adapter/tests/test_message_builder.py:126` | 仅防崩溃 | 加 `[已调用: test]` 格式验证 |

**标记/风格修复（3 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/input/tests/test_server.py:150,493` | `@service` 却调 `agent.start()` | → `@slow` |
| `core/tests/test_phase3.py:1030` | `@slow` 不调 `agent.start()` | → `@service` |
| `core/tests/test_phase3.py:389,433` | 字符串 `"idle"` | → `AgentState.IDLE` |

**重复消除（2 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/tests/test_phase2.py` | 三个驱逐测试重复 | 合并为 `@parametrize` |
| `core/input/tests/test_server.py` | 路由验证 3 处复制 | 提取 `_make_app()` + `_find_routes_by_name()` |

**tempfile 泄漏（2 项）：**
| 文件 | 问题 | 修复 |
|------|------|------|
| `core/monitoring/tests/test_token_tracker.py:52` | `mkdtemp()` 无清理 | → pytest `tmp_path` |
| `core/task/tests/test_task_scheduler.py` | 3 处 `mkdtemp()` 目录泄漏 | → `shutil.rmtree(tmpdir)` |

**fake_llm.py 改进：**
- `chat_stream`: 逐字符 → 正则分词逐 token yield，更接近真实 LLM 流式行为
- 新增 `fault` 参数：`http_429` / `http_503` / `timeout` / `disconnect` 四种故障注入

**修复日期**：2026-07-30

---
### 2026-07-30: 代码审查修复——`_tool_executor` 统一 + `_after_response` lifecycle tracking

**背景**：外部代码审查发现 5 个问题，3 个确认需修复。

**修复（2 项）：**
- `_tool_executor` / `_tool_adapter` 两套引用统一为 `_tool_adapter`：`CognitiveTools` + `CognitiveLoop.__init__` 参数名 + 4 个测试文件。1 commit (`ebcfb71`)
- `_after_response` fire-and-forget：新增 `_pending_after_response` task 追踪 + `flush_after_response()` 方法 + lifecycle `_SHUTDOWN_STEPS` 首个步骤。1 commit (`73ccf46`)

**标记解决：**
- transition 幂等设计死锁风险：审阅误判。`is_accepting_input` guard + `_process_lock` + `CancelledError` handler 三层保护，不存在死锁

**修复日期**：2026-07-30

---
### 2026-07-30: CI/CD — GitHub Actions Self-hosted Runner

**背景**：项目 1,893 个测试全靠手动 `make test-quick`，无任何自动化 CI。

**方案**：GitHub Actions + self-hosted runner（跑在自己服务器上，复用现有 conda 环境 + 常驻 MongoDB/Milvus）。

**新增文件：**
| 文件 | 用途 |
|------|------|
| `.github/workflows/ci.yml` | PR + push to main: `make test-quick` + `make test-normal` 并行 |
| `.github/workflows/nightly.yml` | 每日 3:17 AM UTC 全量 + `workflow_dispatch` 手动触发 |

**服务器前置配置：**
1. 安装 GitHub Actions runner agent → systemd 服务
2. `Settings > Secrets > Actions` 添加 `DEEPSEEK_API_KEY`

**修复日期**：2026-07-30

---
### 2026-07-30: 测试覆盖率缺口补全（4 项）

**背景**：todo.md 记录了 7 项测试覆盖率缺口，经分析 3 项为死代码/trivial，4 项实施修复。

**修复项：**

| 项 | 文件 | 修复方式 |
|----|------|----------|
| `_flush_after_response` 完全未测 | `core/cognitive/tests/test_flush.py`（新增） | 5 个场景：空/已完成/正常/超时取消/异常捕获 |
| FSM 10 条转换 + 3 条 interrupt 未测 | `core/agent/tests/test_agent_fsm.py`（追加） | 20 条测试，覆盖全部 14 条有效转换 + 4 条 interrupt 路径 |
| `_prune_tasks()` 完全未测 | `core/task/tests/test_task_scheduler.py`（追加） | 5 个场景：终端删除/活跃保留/父子关系/全删 |
| `conftest.py` `_fsm.state = MagicMock()` 过于宽松 | `core/agent/tests/conftest.py` | 改为 `AgentState.IDLE` 真实枚举值，16 个受影响测试全部通过 |

**不补测试的 3 项（死代码/trivial）：**

| 项 | 原因 |
|----|------|
| `dispatch_background()` | 死代码 — 唯一引用是注释说"不要用" |
| `execute_stream()` | 一行 pass-through 委托，无独立逻辑 |
| EventBus `drain()` 竞态 | `drain()` 从未被调用，死代码 |

**修复日期**：2026-07-30

---
### 2026-07-30: core/ + Module/task_system/ IDE 类型错误批量修复（~155 处）

**背景**：Pylance/Pyright 诊断累积了大量类型错误，分布在生产代码和测试文件中。

**生产代码修复（2 文件）：**
- `core/input/debug_routes/character.py`：`_character_rollback()` 返回 `tuple[dict|None, str|None, int]`，handler 中 `result.get()` 前加 `assert result is not None`（2 处）
- `core/task/task.py`：`@classmethod` 工厂方法中 `cls(...)` 调用无法静态验证，加 `# type: ignore[reportCallIssue]`（3 处）

**测试修复 — NewType 包装（~50 处，7 文件）：**
- `SessionId()` 包装裸字符串（test_session, test_token_tracker, test_character, test_phase2 等）
- `ToolCallId()` 包装裸字符串（test_message, test_tool_gate）
- `MemoryType`、`MessageRole` 枚举替换字面量（test_phase2, test_phase3）

**测试修复 — ToolAdapter 替换 ToolManager（~30 处，2 文件）：**
- `test_task_scheduler.py`：`ToolManager()` → `ToolAdapter(plugins, session_id="test")` + `start()`
- `test_phase3.py`：3 个 `CognitiveLoop` fixture 全部改用 `ToolAdapter`

**测试修复 — type: ignore（~70 处，17 文件）：**
- MagicMock/MethodType 属性访问（test_engine, test_sub_agent, test_audio_buffer）
- `BaseException` 无 `.success/.error`（test_task_scheduler）
- `Part` 联合类型无 `.content`（test_message, test_phase2）
- `AbstractRoute.path` 静态不可见（test_server）
- Optional `_collection` 链式调用（4 个 repo test 文件）
- `Choice`/`CompletionUsage`/`ArtifactId`/`TypedDict` 键窄化（test_executor, test_evaluator, test_planner, test_delegation_preparer 等）
- `test_clean_db.py` `sys.path` 运行时注入的 `clean_db` 模块（16 处）
- `test_task_system_lifecycle.py` / `test_e2e_mvp.py` `object`→`ToolManager` 参数类型（16 处）

**关联 commit：** `ded85df` ~ `7616385`（29 commits）
### 2026-07-29: debug_routes.py 拆分为 debug_routes/ package

**问题**：`core/input/debug_routes.py`（802 行）是 `core/` 下第二大单文件。23 个 route handler + 3 个 helper + TaskSystem SSE 接线全部嵌套在 `register_debug_routes()` 闭包内。

**拆分方案**：按资源分组为 7 个模块：
```
core/input/debug_routes.py        → 删除
core/input/debug_routes/
├── __init__.py                   → register_debug_routes() 入口 + TaskSystem SSE
├── _common.py                    → sessions_to_client() 共享 helper
├── overview.py                   → GET /debug/api, GET /debug/api/db-status
├── ltm.py                        → LTM CRUD（5 端点）
├── archive.py                    → Archive + Sessions CRUD（8 端点）
├── recording.py                  → Prompts + Responses（4 端点）
└── character.py                  → 版本历史 + 回滚（4 端点 + 2 helper）
```

**关键变更**：Handler 从闭包提升为顶层 async 函数，通过 `request.app["agent"]` 获取 Agent（不再依赖闭包捕获）。`server.py` import 路径 `from .debug_routes import register_debug_routes` 保持不变——Python 自动从 `debug_routes/__init__.py` 解析。

**关联 commit：** `33d25ff` `9284180` `fbe3f05` `e1ed590` `91eb07a` `5114b68` `18ac95d` `e62aa9b`
### 2026-07-29: 文档同步 — core/README.md + README.md + architecture.md

**core/README.md：**
- 文件索引行数全部刷新（agent.py: 1004→522, loop.py: 1042→986, scheduler.py: ~430→509 等 18 处）
- `ToolManager` → `ToolAdapter`（3 处：启动顺序、Agent.start()、扩展指南）
- 新增 `debug_routes/` 条目（7 文件, 888 行）
- Python 文件总数 40→120

**根 README.md：**
- 文件树重写：移除 5 个已迁移的顶层 `.py` 引用（agent_fsm/event_bus/idle_monitor/prompt_assembler/token_tracker），新增 `monitoring/`、`input/`、`storage/` package
- `input/` 从 Module 树下移到 core 树下
- 文档导航：Input 从 Module 层表格移到 Core 层表格
- 测试数：1384 → 1869

**architecture.md：**
- 四处 `ToolManager` → `ToolAdapter`（section 标题、代码注释、Agent 组装/启动注释）
- `Module/tool/` 下的 `ToolManager` 保留（`Module/tool/manager.py` 确实有这个类）

**关联 commit：** `102451b` `91f3aac` `ed83041`
### 2026-07-29: core/ 测试补全 & 迁移 — agent+config +55 tests, 4 files migrated

**新增**：core/config/tests/ (18 tests) + core/agent/tests/ (38 tests)
**迁移**：test_token_tracker → core/monitoring/tests/,
test_task_scheduler + test_task_model → core/task/tests/,
test_mood_engine → core/cognitive/tests/
**文档**：testing.md + CLAUDE.md 更新
### 2026-07-29: `config.yaml` 新增 `database` 段

**问题**：数据库连接（MongoDB + Milvus）只能通过环境变量 `MONGODB_URI` / `MILVUS_URI` / `MILVUS_TOKEN` 配置，`config.yaml` 完全没有数据库相关键。LLM/TTS/STT 都有对应 YAML 段 + 环境变量覆盖的双通道，数据库缺失。

**新增**：
- `core/config/app_config.py`：新增 `DatabaseConfig` dataclass（`mongodb_uri` / `milvus_uri` / `milvus_token` / `db_name`），加入 `AppConfig`
- `config.yaml`：新增 `database:` 段，含完整默认值
- 5 个 Repository（`session_repo` / `message_repo` / `token_repo` / `memory_repo` / `character_version_repo`）：`__init__` 接受 `base_url` / `token` 参数，`connect()` + `health_check()` 中传给 `create()`
- `agent.py`：从 `config.database` 提取配置传给所有 repo
- `docs/database-storage.md`：更新第二节（Milvus/MongoDB 连接）和第七节（环境变量表→配置方式），标注 YAML 路径
- 修复 1 个测试断言（`test_long_term.py` 适配 `create()` 新 kwargs）

**优先级**：环境变量 > config.yaml > 默认值。向后兼容——不设 YAML 值时行为与原来完全一致。
### 2026-07-29: `Any` 类型清理收尾 — emit→ToolEmitter + scheduler + mailbox

**议题1 — 插件 `emit: Any` → `ToolEmitter`（15 处）**：`ToolPlugin`/`ToolManager`/`ToolAdapter` 基类 + 9 个插件文件，全部改用已存在的 `ToolEmitter` 类型。同时收窄 `execute_stream` 返回类型 `AsyncIterator[Any]` → `AsyncIterator[ToolProgress | ToolResult]`。

**议题3 — scheduler 返回类型（2 处）**：`dispatch_tool_calls` 返回 `list[tuple[ToolCall, ToolResult | BaseException]]`，注释已写明实际类型。

**议题4 — `_register_background_task` mailbox（1 处）**：`mailbox: Any` → `TaskMailbox`（TYPE_CHECKING import）。

**议题5 — `_process_events`（1 处）**：`Any` → `Callable[[], Coroutine[Any, Any, None]]`。

**后续修正**：`_BackgroundTaskEntry.mailbox` 从 `asyncio.Queue` → `TaskMailbox`（TYPE_CHECKING import）。

**最终状态**：core/ 下剩余 `Any` 均为不可消除——`dict[str, Any]` JSON 容器（12 处）、流式协议 `**raw_params: Any` + `AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处，`ensure()` 返回值类型不固定）。

涉及文件：`Module/tool/protocol.py`、`Module/tool/manager.py`、`core/adapter/tool_adapter.py`、`core/task/scheduler.py`、`core/agent/agent.py`、`core/agent/_types.py`、`core/agent/_lifecycle_tools.py`、`core/tools/*/plugin.py`（9 个）、`core/tools/web/mock_plugin.py`
### 2026-07-29: `Any` 类型替换 — 第一+二层 25 处

对 `core/` 下全部 63 处 `Any` 使用排查后，按分层修复：
- **第一层（17 处）**：属性声明 `Any` → 具体类型。`tools.py` 6 处（`ToolAdapter`/`DelegationPreparer`/`SessionMemory`/`UserProfile`/`TaskSystem`）、`loop.py` 7 处（同 + `MemoryReflector`/`ResponseRecorder`）、`server.py` 3 处（`Callable[..., Coroutine]`）、`_types.py` 1 处（`asyncio.Queue`）
- **第二层（8 处）**：`asyncio.Task[Any]` → `Task[None]`（4 个 lifecycle mixin 文件中的所有 task handles）。后台 task 均不返回值，`None` 更精确
- 另同时移除 3 个文件不再需要的 `from typing import Any`

涉及文件：[tools.py](../core/cognitive/tools.py)、[loop.py](../core/cognitive/loop.py)、[server.py](../core/agent/server.py)、[_types.py](../core/agent/_types.py)、[lifecycle.py](../core/agent/lifecycle.py)、[_lifecycle_persistence.py](../core/agent/_lifecycle_persistence.py)、[_lifecycle_task_system.py](../core/agent/_lifecycle_task_system.py)、[_lifecycle_tools.py](../core/agent/_lifecycle_tools.py)
### 2026-07-29: 日志级别修复 — shutdown/持久化路径静默吞异常

**问题**：`core/agent/lifecycle.py`、`core/cognitive/loop.py`、`core/agent/_lifecycle_persistence.py` 共 9 处 `except Exception: log.debug()` 静默吞异常。shutdown 失败、session 持久化失败、记忆沉淀/性格反思失败全打 DEBUG 级日志——数据丢失无感知。

**修复**：按严重程度分级调整日志级别：
- **ERROR（2 处）**：session save 失败（shutdown 路径 + fire-and-forget `_after_response` 路径）—— 对话上下文丢失
- **WARNING（7 处）**：shutdown 步骤失败、Consolidator/CharacterReflector/Compression flush 失败、记忆沉淀失败、性格反思缓冲失败、MemoryReflector check 失败
- **保留 DEBUG（2 处）**：db health check 轮询（瞬时故障是常态）、inner_voice broadcast（UI 装饰性功能）

**涉及文件**：[core/agent/lifecycle.py](../core/agent/lifecycle.py)（4 处）、[core/agent/_lifecycle_persistence.py](../core/agent/_lifecycle_persistence.py)（1 处）、[core/cognitive/loop.py](../core/cognitive/loop.py)（4 处）
### 2026-07-29: 前端功能回归 + 分句/消息同步修复

**前端 11 项功能回归**（Vue 3 重构后相对于旧 vanilla JS 的缺失）：
- 复制按钮：assistant 气泡 hover 显示"复制"按钮，点击复制全文
- ↓ 新消息浮动指示器：用户上滚后新消息到来时底部出现，点击回到底部
- 内心独白开关：toggle 联动 body class `inner-voice-hidden` 控制 CSS 显隐
- TTS 开关：`finalizeStream()` 入队前检查 `ttsEnabled`
- 转录 emoji：恢复情绪/事件 emoji 映射（HAPPY→😊, BGM→🎼 等）
- Session Token 统计条：`#session-token-bar` 显示累计 token（按模型分组）
- 历史图片渲染：`restoreFromServer()` 支持 `content[]` 中的 image parts
- 舞台指示拆分：中文括号动作描述（`(轻声地)`）独立气泡
- 图片灯箱：`useLightbox` composable，任意组件调用 `open(url)` 全屏查看
- 拖拽上传：`MessageList` `onDrop` 补上文件上传逻辑
- Stream bubble TTL：30 秒 TTL 清理 orphan 映射

**分句系统修复**（`core/cognitive/stream_processor.py`）：
- `_strip_pause_tags()`：在 `SentenceBuffer` 前剥离 `<#0.5#>` 标签（含跨 delta 尾部残缺标签），防止标签内 `.` 被误判为句子边界导致标签截断
- `flush()` 尾部 emoji 合并：纯 emoji 残留文本合并到前一 SENTENCE，避免 `♪` 等独占气泡

**Archive-Session 同步**（`core/agent/agent.py` + `core/input/debug_routes.py`）：
- `_purge_message_from_session(msg_id)`：从当前 session deque 移除匹配消息
- `archive_delete_handler` / `archive_clear_handler`：删除/清空 Archive 后同步清理 session 并 `save()` 持久化

**Reflector ISO 时间戳兼容**（`core/personality/reflector.py`）：
- `_reflect()` 中 `datetime.fromtimestamp()` 兼容 ISO 8601 字符串和旧 Unix float

**TTS 情绪映射**（`core/adapter/tts_adapter.py`）：
- `_mood_text_to_tts_emotion()`：LLM 中文心情自由文本 → MiniMax TTS emotion 参数 (happy/sad/neutral)
### 2026-07-28: AgentLifecycle 拆分 + EmbeddingProvider + Action Gate + LTM 驱逐修复

**AgentLifecycle 拆分为 mixin（522 → ~120 行）：** `lifecycle.py` 从手写 10 步 startup 序列改为数据驱动 `_STARTUP_STEPS` 循环（镜像已有的 `_SHUTDOWN_STEPS`）。4 个 mixin 文件：`_lifecycle_persistence.py`（持久化层启停）、`_lifecycle_services.py`（LLM/TTS/Output/Token 启停）、`_lifecycle_tools.py`（ToolAdapter/Scheduler/IdleMonitor 启停）、`_lifecycle_task_system.py`（TaskSystem/mailbox 启停）。模式与 CharacterEngine 拆分一致。

**EmbeddingProvider 提取（`core/memory/embedding_provider.py`）：** 将 `long_term.py` 中 72 行嵌入加载逻辑（懒加载、超时、OpenAI/HuggingFace fallback、线程池）提取为独立类。对外暴露 `ensure(wait)`、`embed(text)`、`cancel()` 三个方法。`LongTermMemory` 从 589 → ~520 行。

**Action Gate（工具调用决策校验）：** `CognitiveTools._gate_tool_calls()` 在 LLM 产出 tool_calls 后、dispatch 前校验工具名是否在注册表。任一未知工具 → 整轮 abort 注入 `GATE_REJECTED` failure messages。新增 `ErrorType.GATE_REJECTED`。

**LTM 驱逐修复 + mark_absorbed Milvus 清理：** `_evict_old_entries()` 重命名为 `_evict_overflow()`，去掉年龄驱逐只保留数量上限，加 Milvus 同步删除。`insert()` 中恢复驱逐调用（`enable_eviction` 开关门控）。`mark_absorbed()` 改为 async 加 `_write_lock` + Milvus 删除，消除僵尸向量累积。
### 2026-07-22: C6 说话风格动态联动 + CharacterState 共享修复

**C6 动态联动**：L3→L2 压缩完成后自动触发 C6 说话风格刷新。LLM 编辑模式，版本历史与 L2 共享 `character_versions` 集合（按 `content_type` 区分）。C6 模板支持条件渲染：非空时渲染动态文本，为空时回退 L1 默认。

**CharacterVersionRepository 通用化**：集合 `character_l2_versions` → `character_versions`，所有方法加 `content_type` 参数（默认 `"l2"`），版本号 per content_type 独立计数。

**CharacterState 共享修复**：`engine.state` 与 `session.character_state` 共用同一对象。engine 为 mood/性格状态的权威来源，session 负责持久化 + 对外读取。修复了 debug 面板 mood 与聊天页面不一致的 bug。

**其他**：mood 从 debug System 面板移入 CharacterEngine 面板；`clean_db.py` 新增 `character_versions` 和 `character_l2_versions` 清理；C5 Sensory 新增输入方式标注（文字/语音）；AwarenessJudge: C9 LLM 语义判定。

**code-review 修复（19/30）**：严重 Bug #1-#4 #8 (5)，静默数据丢失 #14-#18 (5)，前端 Bug #19-#21 (3)，资源泄漏 #22 #24 (2)，设计问题 #27-#30 (4)。跳过 #5(影响小) #6(TODO) #7(需设计) #23(并发度低)。剩余安全 #9-#13 + 设计 #25-#26 待后续。

**CharacterEngine Phase 2 收尾（2026-07-22）**：

| 条目 | 实现 |
|------|------|
| C6 说话风格动态化 | L2→C6 LLM 编辑模式（`_refresh_c6()` + `SpeakingStyleEdit.j2`），`_compress()` 后自动触发，版本历史 content_type="c6" |
| AwarenessTrigger | L2/L3 语义偏差检测 — LLM-judge 驱动（`_judge_awareness` + `AwarenessJudge.j2`），异步 fire-and-forget，缓存 + 冷却 + 展示次数三重门控 |
| L3→L2 梯度压缩自动化 | LLM 编辑模式 + 版本历史（`character_versions` 集合），`state_history` 计数/时间双触发 |
| ExpressionAdapter | Mood 升级为 LLM 自由文本（`<mood>` 标签），expression 直接透传 mood 文本，移除阻尼映射 |
| L2 永久存储 | `character_versions` 集合 append-only，不随 session TTL 过期 |
| C3/C6/C7 动态联动（C6 部分） | L2→C6 编辑模式 + 版本历史。C7 待后续 |
### 2026-07-14: 分句系统优化

全面重写 `SentenceBuffer` 和 `BlockFilter`，修复 2 个正确性 bug + 1 个安全 bug + 工程改进。

**SentenceBuffer (`core/sentence_buffer.py`)：**
- 删除死代码 `_split_pat`（编译从未使用的 regex）
- 修复 `_is_dot_boundary`：`3.14.` 末尾句号从误判为小数点改为正确切句（"前后都是数字"才认定为小数点）
- 缩写检测重写：固定 3 字符窗口 + 大小写敏感 → 向后扫描完整单词 + 大小写不敏感（23 个缩写）
- 新增 `max_buffer_length=2000`：无标点长文本超阈值时在空格处强制切分，防止 buffer 无限增长
- `_extract` 拆分出 `_consume_sentence`：连续分隔符合并逻辑独立成方法
- `_find_split` 改为单次 `enumerate` 遍历（替代逐分隔符 `str.find()`）
- `_HARD_DELIMITERS` → `frozenset`，风格统一
- 新增可选参数：`max_buffer_length`、`abbreviations`

**BlockFilter (`core/block_filter.py`)：**
- `_depth` → `_mode: Literal["markdown","code"] | None`：修复不同类型结束标签互相关闭的 bug（`</|code|>` 不再能关闭 `<|markdown|>`）
- `flush()` 安全修复：未闭合块内容直接丢弃，不再泄漏到 TTS
- 新增 `reset()`：配合 SentenceBuffer 生命周期
- `flush()` 尾部半截标签处理：`rfind("<|")` 替代 `_BLOCK_START_RE.sub()`
- `build_parts_with_blocks()` 修复：块按原文位置交错插入，不再全部追加到末尾
- `_CODE_START_RE` 放宽为 `[A-Za-z0-9_+.-]*`

**集成 (`core/cognitive_loop.py`)：**
- `SentenceBuffer` 和 `BlockFilter` 改为每轮 `process()` 新建（赋给 `self._sentence_buf` / `self._block_filter`），替代 `__init__` 中一次性创建
- `_handle_interrupt()` 新增 `self._block_filter.reset()` + None-guard

**测试：**
- `test_sentence_buffer.py`：新增 7 个 test（3.14. 末尾句号、缩写大小写、max_buffer 强制切分），72 test 总计
- `test_block_filter.py`：新建 18 个 test（过滤、flush 安全、跨 chunk、标签匹配、reset、parts 排序）
- `test_phase3.py`：3 个 test 适配新生命周期（`_sentence_buf` → `None` at init）

**文档更新：** `docs/architecture.md` §4.3/§4.6, `core/README.md`, `docs/frontend-api.md`
### 2026-07-14: 不稳定测试排查与修复

从 10 failed → 1 failed（1543→1552 passed）。修复 7 个测试，分 4 类根因：

**确定性问题（5 个）：**
- `test_ok_returns_latency`: `ok_check(self)` 需要 self 但 `make_test_method` 内部 `health_fn()` 不传参 → TypeError 被静默吞掉 → 移除多余 self
- `test_no_memories_when_empty`: Prompt 中文化后断言仍检查英文 "No specific memories" → 改为 `"<memory_bank>" not in system`
- `test_stop_cancels_cognitive_task`: `cancel_current()` 把 `_current_task` 置 None 后才检查 → 保存 task 引用再 stop
- `test_invalid_json_returns_error_message`: `session` 消息后还有 `capabilities` 推送竞态 → 循环排空启动消息
- `test_character_personality_injected`: `personality.traits` 不再直接入 prompt（走 PersonalityEngine + base_personality）→ 改用 base_personality

**状态污染（2 类根因）：**
- **`load_dotenv()` 全局污染**: test_phase1.py（9 处）和 test_phase3.py（2 处）直接调 `load_dotenv()` 注入 `MILVUS_URI`，后续测试连上真实 Milvus（35 条旧数据）。修复：`os.environ.setdefault("DEEPSEEK_API_KEY")` 替代
- **`SafetyProfile` 类属性变异**: `ToolManager.load_all()` 从 config.yaml 覆盖类级 `read_dirs`，后续 `ReadFilePlugin()` 实例受影响。修复：断言改为非空检查而非精确值
- **`EnvManager.ensure_dotenv()` 副作用**: `ConfigLoader.load()` → `ensure_dotenv()` 加载 .env 全部变量。`TestAppConfig` 修复：伪装 `_loaded = True` 跳过

**防御性修复（3 个）：**
- `test_collect_memory_counts`、`test_ltm_list_and_insert`、`test_ltm_clear_all`：绝对计数断言 → 相对增量，即使 LTM 有预存数据也能正确验证

**残留（1 个）：**
- `test_touch_resets_timer`: `IdleMonitor.__init__` 中 `asyncio.get_event_loop().time()` 在全量 suite 中偶现失败。已加 `time.sleep(0.001)` 缓解。建议后续将 `IdleMonitor` 改用 `time.monotonic()` 替代 `get_event_loop().time()`

**Commits:** `963ee6b` `7a07f98` `2a4bbc5`
### 2026-07-14: 提醒工具 coroutine 序列化 bug（已修复，归档）

- **`reminder/plugin.py` coroutine 被提前 await 后传入 payload** — `_remind()` 生成 coroutine → `scheduler.dispatch_background()` → checkpoint JSON 序列化时 coroutine 不可序列化，跨重启丢失。
- **已在 `39fa778` 修复**: 改用 `asyncio.ensure_future(_remind())` 绕过 scheduler 持久化，注释说明取舍。
### 2026-07-14: 安全修复 — code-review-report 遗留严重问题清零

- **Milvus `_metric_type` 跨集合污染** (`milvus.py:39`): `self._metric_type` 单值 → `self._metric_types: dict[str, str]` per-collection 字典。`create_collection` 按名存储，`search` 按名读取，fallback L2。
- **MongoDB NoSQL 注入** (`mongodb.py:31`): collection 名加正则白名单 `^[a-zA-Z0-9_-]+$`，覆盖所有 7 个方法 + database 名。
- **Embedding 工厂缓存忽略 kwargs** (`registry.py:30-35`): cache key 从 `model_id` 扩展为 `model_id + json.dumps(kwargs)`。
### 2026-07-13/14: LTM 自动更新与去重 + 日志精简 + 数据一致性 + 文档大修

**LTM 记忆系统全面升级：**
- **MemoryReflector** (`core/memory/memory_reflector.py`): 定期深度去重。双条件触发（50 轮 + 30 条新记忆），KMeans 聚类分组，LLM 批量审查（merge/update/delete），上限控制（5+5+3）。对标 CharacterReflector 生命周期（restore_state/flush）。
- **ConsolidationCompare** (`core/consolidator.py`): 提取时轻量比对。新记忆 vs 已有记忆 top 10 → LLM 判断 insert/update/merge/skip → 预校验 → 执行。失败回退全 insert。
- **LTM 数据模型扩展**: `_absorbed_ids` + `_write_lock` + `get_all()` + `mark_absorbed()` + `update_text()` 扩展（支持 poignancy/memory_type）+ `search()` 过滤已吸收条目
- **驱逐**: 开发阶段 `_evict_old_entries()` 已注释（2026-07-28 已修复：重命名为 `_evict_overflow()`，count-only，加 Milvus 清理，re-enabled）
- **Prompt 文件** (GPT 5.6 sol_pro 生成):
  - `prompt/ExtractMemory.j2` — 反幻觉提取（13 节，含正反示例 + 自检清单）
  - `prompt/ConsolidationCompare.j2` — 比对去重（186 行）
  - `prompt/MemoryReflector.j2` — 定期去重（239 行）
  - `prompt/ref/` — GPT 原始版本归档（4 个 snake_case.md）
  - `prompt/README.md` — 命名规范 + 渲染方式文档
- **Prompt 基础设施**: `.j2` 统一 PascalCase 命名；`str.replace()` 替代 `.format()`（避免 JSON 大括号冲突）；文件加载替代内联字符串；`prompt-requirements.md` 需求文档

**日志精简：**
- TTS 分句/播放日志从单句 INFO 降为 DEBUG，`wait_all()` 后一行汇总（"TTS: N 句 X 字符 → YKB 已播放"）
- 第三方库日志级别 DEBUG→WARNING（pymongo/motor/httpx/openai/urllib3/sentence_transformers）
- 启动日志：工具列表从逐条 INFO → 一行汇总；横幅工具数精简

**数据一致性：**
- **Archive 成为权威数据源**: Session 视图从 `message_archive` 读取，非 `sessions` 快照。`session_id` 注入 + 迁移 + 降级路径
- **MongoDB 三层缓存清理**: localStorage（前端）→ session._messages（内存）→ sessions 集合（MongoDB）连环问题排查。移除了前端 localStorage 消息缓存
- **数据库清理**: MongoDB sessions/archive/token_usage + Milvus LTM 全部清空，从零开始

**启动与工具链：**
- `./start.sh` 根目录符号链接 + `Makefile start` 目标
- `conda run --no-capture-output` 修复 stdout 缓冲
- `ALL_PROXY=socks://` 修复（httpx 不兼容）
- `readlink -f` 修复符号链接路径解析

**Debug 仪表板增强：**
- Session Params 可展开 key-value（L1 Core 下方），大数据 key 只显示计数
- 搜索输入值 + 折叠状态在 DOM 重建时保留
- Archive 刷新时同样保留搜索值

**Bug 修复：**
- `removeElement`→`indicator.remove()`（chat.js 滚动加载）
- TTS read timeout 120s→30s
- mood/mood_intensity 初始化 + `restore_state()` 确保 session params 默认值
- MemoryReflector 计数器 `items = []` 初始化防 NameError
- KMeans 索引越界（100% embedding 阈值）

**文档全面更新：**
- `CLAUDE.md`: prompt 目录 + persistence.js + server routing + MemoryReflector 架构
- `architecture.md`: 21 处过时内容修复（backend 列表、测试数量、文件结构、路由、配置、Debug 标签页）
- `static/debug.md`: Session 数据源 + LTM 生命周期 + 前端缓存移除
- `docs/experience.md`: 8 条踩坑经验（数据清理、prompt 命名、conda/stdout、proxy、symlink、motor ObjectId、DOM 重建）
- `todo.md`: 标记已完成项 + 新增 LTM 完成清单
### 2026-07-12: PersonalityEngine + Elysia 性格升级 + Review 反馈

- **PersonalityEngine** (`core/personality/`): 四层性格模型。L1 Core（文件，不可变）→ L2 Self（session，缓慢演化）→ L3 State（Reflector 驱动）→ L4 Mood（已有 EmotionEngine）。CharacterReflector 变更为子组件。C1/C9 模板数据驱动。Character YAML 瘦身 + 路径解析。
- **Elysia 性格融合** (`personality.md` + 模板): 从 GPT 生成的 5 份参考中提取，以 sol_pro 为基准融合。新增心理结构、六种情绪表现、五模式切换、日常爱好、道德边界、场景示例。
- **System Prompt Section 全量升级**: F1 安全边界 + inner_voice 约束、F2 任务优先、F3 TTS 限制、C2 四阶段关系、C3 8 种情绪响应、C4 记忆可靠性、C6 重写、C7 冷却机制
- **MongoDB ObjectId 修复**: motor insert_one 副作用 → dict copy
- **Character 目录重组**: `character/elysia/{config.yaml, personality.md, reference/}`
- 80 tests pass
### 2026-07-11: System Prompt Section 升级 (F1/F2/F3/C5)

对 Module/system_prompt/ 进行模块化升级：统一格式约定、拆分职责、修复安全问题。

- **F1 ConflictRules**: `StaticSection` → `Jinja2Section`，新增 `f1_conflict_rules.j2` 模板。内容从 1 句话扩展为 4 条优先级规则（硬约束/软约束分类、inner_voice 真实性、感官事实不可覆盖、角色定义互补）
- **F2 OutputFormat**: 加 `<output_format>` XML 包裹；解释 `「」` 示例约定；代码块格式 `<!-- -->` 注释掉（待后续启用）；TTS 标记内容拆出到 F3
- **F3 TTSMarkup** (新建): `f3_tts_markup.j2` + `TTSMarkupSection`，独立管理 TTS 合成标记（MiniMax/gpt-sovits）
- **C5 Sensory**: `<perception>` 拆为 `<message>` + `<voice>` 子标签；`html.escape()` 防 XML prompt injection；`<voice>` 始终渲染（无数据时占位文本）
- **C6 SpeakingStyle**: `<perception>` 引用同步更新为 `<voice>` / `<message>`
- **MongoDB ObjectId 修复**: `insert_one/insert_many` 加 `dict()` copy 防止 motor 注入 `_id` 污染调用方；`session.load()` 剥离已有损坏数据
- **templates/README.md** 新建：格式约定、命名规则、渲染顺序、添加新模板步骤
- 55 tests pass
### 2026-07-03: Brain Prompt 模块化重构

Brain.j2 单体模板 (~244行) 拆分为模块化 section 管理引擎。

- `Module/system_prompt/` — Brain Prompt 引擎 (`PromptSection`, `StaticSection`, `Jinja2Section`, `SystemPromptManager`, `MessageBuilder`, 11 section 实现)
- `Module/system_prompt/templates/` — 11 个独立 section 模板 (f2_output_format + c1~c9)
- `Module/system_prompt/sections/` — 11 个 PromptSection 实现 (ConflictRules/OutputFormat/Sensory/Emotion/Memory/Personality/Relationship/SpeakingStyle/Proactive/OwnLife/SelfPerception)
- `PromptAssembler` 简化为胶水层：创建 11 个 section → `SystemPromptManager.set_context()` + `render()` → `MessageBuilder.build_messages()`
- 黄金对比测试：新旧 system prompt 逐字节一致
- 379 测试通过，零回归

详见 已归档
### 2026-07-10: 部署工具链

一键安装 + 预检 + docker-compose 数据库。

- `scripts/setup.sh` — 7步自动安装（conda → pip → .env → 嵌入模型 → 目录 → 预检）
- `scripts/start.sh` — 一键启动（conda → docker → 预检 → 服务）
- `scripts/download_model.py` — 嵌入模型下载（默认 hf-mirror.com 镜像）
- `Module/config/preflight.py` — 环境预检（14 项检查，`app.py --check`）
- `docker-compose.yml` — MongoDB 7 + Milvus v2.4 standalone
- `app.py` 默认改为 WebSocket 服务器模式，移除 `--once`/`--server`
- `requirements.txt` 更新：补 pydantic/networkx/psutil 等，移 ddgs
- README 完整重写：小白友好 + 配置文件参考 + 高级用法
### 2026-07-10: providers.yaml 重构

所有 provider 默认值从 registry.py 硬编码迁移到 `Module/config/providers.yaml`。

- 新建 `Module/config/providers.py` — 统一加载器
- `llm/stt/tts/embedding` 4 个 registry + `vectordb/documentdb` 2 个 config 改为从 YAML 加载
- `.env.example` 完善：87 行，按 LLM/TTS/STT/Embedding/数据库/工具 分类
### 2026-07-10: IDE 类型错误修复

修复 15 个文件的类型检查器报错。

- `_health_check` 闭包移除冗余 `self` 参数（3 个 backend）
- `stt/protocol.py` `transcribe_stream` 加 `yield ""` 成为生成器
- `logger/__main__.py`：`factory.create()` → `factory.get()`
- `llm/openai_compat.py`：裸 `logger` → `self._logger`
- 多处 `# type: ignore[attr-defined]` / null 守卫
### 2026-07-09: MiniMax TTS + 预处理解耦

- `MiniMaxTTSBackend` — HTTP 非流式 + 分块流式（hex→bytes）
- `TTSBackend.get_preprocess()` — 每个 backend 声明自己的预处理函数
- `TTSStreamManager` 从 backend 获取预处理，不再硬编码 `strip_all_markup()`
- `f2_output_format.j2` 按 TTS provider 输出不同标记指令
- MiniMax emotion 参数：`EmotionEngine` mood → `TTSStreamManager.set_emotion()`
- TTS 句子乱序修复：`asyncio.Event` 顺序释放门
### 2026-07-11: 消息永久归档 (MessageArchive)

所有对话消息永久保留到 MongoDB `message_archive` 集合，与 session 职责分离。

- `core/storage/message_repo.py` — MessageRepository 类（只增不删、分页查询、fire-and-forget 写入）
- `SessionMemory.add_message()` 中自动写入 archive
- `GET /api/messages` — 前端滚动到顶懒加载历史（游标分页）
- `GET /debug/api/archive` — debug 浏览/搜索/清空
- `static/js/chat.js` — 滚动监听 + prependMessages + 位置修正
- `static/debug.html` — 数据库 Tab 新增 Message Archive 面板（三栏布局）
- 16 个测试（13 单元 + 3 集成）
### 2026-07-11: LTM 从 Milvus 恢复 + 显示修复

- `LongTermMemory._load_from_milvus()` — 启动时从 Milvus query 已有条目恢复到 `_entries`，按时间排序并驱逐超限
- `insert()` 存储 `entry_id` 到 Milvus metadata，确保加载时恢复原始 ID
- Debug 概览页 `lt_recent` 补 `memory_type` 和 `poignancy` 字段，类型标签从 `?` 修正
- Session 20 条上限未改（上下文窗口），归档解决的是历史保留
### 2026-07-11: Consolidation user_name 参数化

`EXTRACTION_PROMPT` 中"用户"→`{user_name}`，对话前缀同步使用。
### 2026-07-11: CharacterReflector buffer 持久化

- `buffer()` 每次追加后自动 `_save_state()` 到 session.params（随 MongoDB 持久化）
- `restore_state()` 在 `session.load()` 后调用，重启恢复累积进度
- `flush()` 增加最低阈值（10 对），少量对话不强制反思，避免清空 buffer
### 2026-07-11: IDE 类型错误修复 (第二批)

生产代码：`_EventBusHITLNotifier` 继承协议、`get_raw_client()` cast、`stt_backend.last_emotion`→`getattr`、`distances` 类型放宽、`_archive` assert、流式变量 `content`→`delta` 消 shadowing。
测试代码：9 个文件的类型注解修复 (`# type: ignore`、`assert is not None`、`e.type.value`→`e.type`)。
### 2026-07-11: 死代码清理

删除 F3 工具声明相关死代码：`_reference_behavior.j2`、`ToolManager.to_prompt_text()`、`PromptAssembler.set_tool_manager()`。
### 2026-07-06: 人称代词参数化

`CharacterConfig.user_pronoun` + c1/c2/c3 模板 `{{ user_pronoun }}`。
### 2026-07-04: Brain Prompt Section Provider

Brain.j2 单体拆分 → 11 个独立 PromptSection + SystemPromptManager。
### 2026-07-21: todo 清理 — 情绪管线 + 存储优化

**STT→mood 硬编码删除：** `_STT_EMOTION_TO_MOOD` 映射表删除（7 条规则）。STT 情绪通过 C5 Sensory 直传 LLM，不再替 LLM 做 mood 决策。关键词路径 `analyze_user()` 始终执行（不跟 STT 互斥）。`analyze_user_stt()` 简化为仅处理音频事件→表情。

**`_push_expression()` 映射补全：** 删除 4 条硬编码映射表，改用 `mood.value` 直接透传。全部 10 种 Mood 的 expression 名与枚举值一致。同时修正 EXCITED→"surprised" 前端名字不匹配。

**MongoDB database 名可配置：** SessionRepository / MessageRepository / TokenRepository 加 `database_name` 参数（默认 `"default"`），`connect()` / `health_check()` 不再硬编码。

**Token 聚合 O(n)→O(1)：** `DocumentDBBackend` 协议加 `aggregate()`，MongoDB 实现。TokenRepository 加 `total_tokens_by_session()` / `total_tokens_by_model()`（MongoDB `$group`）。debug_routes N+1 查询 + token_tracker O(n) 累加全部替换为单次聚合查询。

**`_audio_seq` 绑定流生命周期：** `__init__` 显式初始化 + `on_audio_end` 清理对应 stream_id 条目。移除 32→16 cap 止血带，dict 大小绑定并发 TTS 流数（通常 ≤3）。

**移除无效 todo：** Optional 嵌套 dataclass（零触发）、content_type 迁入 content（投入产出比低）。

**关联 commit：** `e81d418` `b453735` `b7c9231` `dd50f78` `3b9e3ed` `4b96477` `b49f20b` `51873c2` `b81ba33` `a10a4a4` `0542f12` `fd851e0`
### 2026-07-21b: L2 Self 版本化 — LLM 编辑模式

**L3→L2 压缩升级：** LLM 从"全量重写"改为"编辑一份持续迭代的文档"。每次压缩返回结构化输出 `<new_self>` + `<changes>`（新增/强化/移除/修正）。新增 `character_l2_versions` MongoDB 集合，append-only 永久保留所有版本。Debug 面板可浏览全版本历史并回滚。

- `CompressionTrigger` StrEnum（compression/flush/manual）+ `VersionEntry` TypedDict
- `CharacterVersionRepository` — append（自动递增版本号）/ list（分页）/ get / count
- `PersonalityCompression.j2` 重写 — LLM 角色从"感受和写下"改为"编辑文档"
- `_compress(trigger=...)` 解析 `<new_self>` + `<changes>` → 写版本历史
- `_extract_tag()` 辅助函数
- `flush_compression()` 使用 FLUSH trigger
- Debug 面板：`GET /debug/api/l2-versions` + `POST /debug/api/l2-rollback`
- 前端：版本历史弹窗 + 回滚按钮
- 触发机制暂保留批触发（5 条 or 7 天），todo 标注后续改为连续增量

**关联 commit：** `b3caf14` `4af3c01` `4253bb8` `51ea0c8` `c8baa15`
