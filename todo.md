# 待做

> **CI 配置**：`Module/workflow_engine/` 尚未完善、非当前开发重点，`test-quick` 和 `test-normal` 已通过 Makefile 跳过该目录（`--ignore=Module/workflow_engine/tests`）。完整跑 `test-full` 或手动指定路径时仍会执行。

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **LLM 有时不输出 <mood> 标签** | deepseek-v4-flash 遵循结构化输出不稳定，部分轮次 `<mood>` 缺失，回退到关键词 fallback。需强化 prompt 或在 F2 加更强制性的约束 |
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 低 | **`test_integration_global_constraints_respected` 偶发失败** | `Module/workflow_engine/tests/integration/test_delegation_preparer.py`: 全量跑偶发 FAIL，单独跑 PASS，疑似 WorkflowEngine 集成测试时序/资源竞争 |
| 低 | **`test_multi_step_task` 偶发失败** | `Module/workflow_engine/tests/integration/test_planner.py`: DeepSeek API 偶发返回截断 JSON (`Unterminated string`)，LLM 输出不稳定导致 `PlanParseError` |
| 中 | **TTS 密集短句导致 wait_all 阻塞** | TTS 连续 5+ 短句密集入队时 MiniMax 部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING。已缩短 read timeout 120s→30s 缓解，根因（wait_all 无超时）待重现后修复。 |
| 低 | **`flush_after_response` 超时层不匹配** | 内层 `loop.py:897` 每 task 5s，外层 `lifecycle.py:191` 整体 10s。3+ task 同时超时可能被外层截断。实际 pending 典型 0-2 个、亚秒级完成，impact 极低。修法：外层 timeout 改为 `max(10.0, len(pending) * 5.0 + 1.0)` |
| 低 | **`ConfirmModal` @keydown 在不可聚焦 `<div>`** | `frontend/src/debug/components/shared/ConfirmModal.vue:35` — overlay div 无 `tabindex`，依赖按钮聚焦后事件冒泡。弹窗内只有文字+两个按钮，点击文字不会失焦，现有行为已可靠。加固：加 `tabindex="0"` |
| ~~中~~ ✅ | **STT 运行时切换不生效** | 2026-08-03 已修（服务运行时状态特性）：`switch_stt` 真正建后端（先建后关 + 1s 连通性测试 + 写 ServiceState）；`stt_transcribe` 收进 agent 运行时动态读 `agent.stt` 槽位，热切换对既有 WS 连接生效（不再依赖启动期闭包） |
| 低 | **`core/tests/test_phase2.py`/`test_phase3.py` 裸 `setdefault("DEEPSEEK_API_KEY","test")`** | 与 `test_task_scheduler._ensure_api_key` 同模式（后者已改 monkeypatch，2026-08-02）。当前 test-quick collection 顺序下未触发，但属进程 env 污染隐患——collection 顺序变化或并行跑时可能污染真实 LLM 集成测试。建议统一改 monkeypatch |
| ~~中~~ ✅ | **空 LLM 响应被静默吞掉** | 2026-08-02 已修：`_think_loop` 空响应守卫——无文本+无工具调用 → 注入 nudge 系统消息重试一次 → 仍空发兜底回复。兜底同时注入 `all_sentences` 保证 session 归档与显示一致。见 `core/cognitive/loop.py` `_EMPTY_RESPONSE_NUDGE`/`_EMPTY_RESPONSE_FALLBACK` + `core/cognitive/tests/test_empty_response_guard.py` |
| 低 | **NORMAL 态 `_pending`/`_held` 缓冲无界** | `tokenizer.py:288/300/306` — `_pending += c`（普通文本/行中反引号）、`_held += c`（行首反引号前缀）无上界。C 节加固（2026-08-02）只覆盖 tag 态 7 缓冲，NORMAL 态路径漏网：长无换行段落可把 `_pending` 撑到 MB 级、巨型反引号行撑 `_held`。既有行为非回归，final review 判 defer（load-bearing：分支成为"内存已全有界"引用来源但不覆盖此路径）。修法：给 `_pending`/`_held` 加上界（如 `FENCE_MAX_CONTENT` 同量级），超限按 SPEECH 落盘 |
| 低 | **STT env 变量陈旧：`SENSEVOICE_URL` vs `STT_API_URL`** | `.env.example:49` 与 `Module/config/preflight.py:47` 用 `SENSEVOICE_URL`，但 `Module/stt/config.py` 实际读 `STT_API_URL`（默认 http://localhost:20042）。照 .env.example 配置会设一个代码不读的变量，preflight 会误报。README.md:146 写的是对的（STT_API_URL）。修法：统一到 `STT_API_URL`，删 `.env.example` 的 SENSEVOICE_URL 行 + preflight 改 key（doc-review 2026-08-02 #27） |

## 模块化评估（2026-08-01）

基于 import 图分析（AST 解析全仓 28 个包、~340 py 文件、118 条包间依赖边）。总体分层健康（`core → Module` 正向为主）。**已修部分归档**：硬环拆除 + Agent 公开接口见 [docs/completed.md](docs/completed.md) 2026-08-01。

### 反向依赖（Module → core，分层违规，未修）

| 优先级 | 位置 | 说明 |
|--------|------|------|
| ~~中~~ ✅ | [Module/tool/protocol.py:15](Module/tool/protocol.py#L15) | eager `from core.schema.error_type import ErrorType` — **已评估：接受**。ErrorType 是跨层类型（`core.schema.message.ToolResultMessage` + `Module.tool.ToolResult` 双侧消费），不能挪进 Module/tool（会反向拖累 schema）；core.schema 是叶子无反向依赖、无环。唯一 test 级反向边已修复：`core/schema/tests/test_message.py` 改从 `core.schema.error_type` 正典导入 |
| ~~中~~ ✅ | [core/cognitive/delegation.py:20-21](core/cognitive/delegation.py#L20) | eager `from core.schema.message import Message` 等 — **已修**：文件迁至 `core/cognitive/delegation.py`（workflow_engine 分离 Task 9），core 引用随之合法，Module/workflow_engine 生产代码现零 core 依赖 |
| 低 | [Module/tool/safety.py:618](Module/tool/safety.py#L618) | lazy `from core.utils import new_message_id` |
| 低 | [Module/bench/bench.py:129-139](Module/bench/bench.py#L129) | lazy `core.config`/`core.agent`（bench 是 dev 工具，可豁免） |

根因：`core.schema` 名义在 core/ 下但承担跨层共享类型职责，Module.tool/workflow_engine 引用它 = Module 层无法独立存在。评估结论：可接受（schema 本就是共享契约层、叶子节点），仅修掉 test 级反向边；若未来要求 Module 层作为独立库发布，才需将 ErrorType/Message 下移到根级共享包。

### 软环（lazy import 运行时解决，无需处理）

- `Module._common → Module.config → Module.llm/stt/tts → Module._common` — [preflight.py](Module/config/preflight.py) 函数内懒加载后端
- `Module.system_prompt → core.adapter → core.memory/personality/storage → Module.system_prompt` — ✅ 已消除：`__init__.py` 的 deprecated MessageBuilder lazy import 已删除（无消费方），Module/system_prompt 生产代码现零 core 依赖
- ✅ `core.schema._utils` 跨包导入已消除：改名 `core.schema.utils`（`now_iso` 被 7 个包共享），私有 helper `_normalize_iso_ts`/`_validate_src` 保持下划线前缀仅 schema 内部使用

## token 统一结构（2026-07-31）遗留可接受项

全链路统一 `TokenUsage` 模型的最终 review（opus）**APPROVE FOR MERGE** 后的可接受 residual，均为低优先级。详见 [docs/superpowers/specs/archive/2026-07/2026-07-31-token-usage-unified-structure-design.md](docs/superpowers/specs/archive/2026-07/2026-07-31-token-usage-unified-structure-design.md)。

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 低 | **TokenLabel 显示 `cached:0`/`think:0`** | `frontend/src/components/chat/TokenLabel.vue` `v-if="round.cached_tokens != null"` 对字面 0 也显示（`0 != null` 为 true）。仅 provider 显式上报 0 才出现（Ollama 等无 details 返回 None 不显示）。如需隐藏改 `v-if="round.cached_tokens"`，但会连真实 0 一起藏 |
| 低 | **flush_pending 部分失败可能重复插入** | `core/monitoring/token_tracker.py` flush_pending：`insert_many` 部分成功失败时保留文件，下次启动重导 → 已插入记录重复。insert_many 非事务，需按 (session_id+timestamp) 去重或建唯一索引 |
| 低 | **role 判别行为变更** | `core/schema/adapters/message_adapter.py` `_MESSAGE_ADAPTER` 改 discriminated union 后，role 不在 {user,assistant,tool} 的 dict raise ValidationError（原静默 UserMessage）。更安全但更严格，未来加新消息类型需同步补 `Tag` |
| 低 | **`_MESSAGE_ADAPTER` 注解不精确** | `message_adapter.py:46` 注解 `TypeAdapter[Message]` 实为 `TypeAdapter(_MessageByRole)`，cosmetic 仅阅读困惑 |
| 低 | **debug current_turn 无前端消费者** | `frontend/src/debug/types.ts` `TokenRoundItem`/`current_turn` 已定义但 `OverviewTab.vue` 只渲染 session_total/per_model。接入调试明细 UI 时启用 |

## 来自 code-review-2026-07-31 的遗留问题

已修复 42/44（详情见 [docs/completed.md](docs/completed.md) 2026-07-31 + [docs/review/code-review-2026-07-31.md](docs/review/code-review-2026-07-31.md)）。2 项遗留：

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **中** | **M19 resume 后再次 pause 被销毁** | [workflow_engine.py:384-393](Module/workflow_engine/workflow_engine.py#L384)。缺 PAUSED early-return 保护，mailbox 消费者永久阻塞 |
| **中** | **M20 cancel 在规划阶段不推终态事件** | [lifecycle.py:157-167](Module/workflow_engine/lifecycle.py#L157) + [planning.py:143-145](Module/workflow_engine/core/sub_agent/planning.py#L143)。`cancel()` 尚无调用方接线，随任务系统接入时修复 |

> M19/M20 随 WorkflowEngine 接入。

## 功能改进

- **README 截图**: 添加聊天界面 / debug 面板 / 启动流程截图，让新用户第一眼就知道项目长什么样
- **Live2D 集成**: Cubism SDK v5 需 TypeScript + npm + bundler 构建，当前仅 SDK 文件到位、默认关闭
- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **MiniMax SSE 真流式**: 已尝试并回退 (2026-07-10)。MiniMax SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复，只取最后 event 又等于同步下载。待 MiniMax API 支持增量 SSE 或换方案（WebSocket）
- **MiniMax 异步长文本合成**: `t2a_async_v2`，适合 >3000 chars。需创建任务 → 轮询状态 → 下载文件
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **Claude 原生持久记忆**: ✅ 已启用（2026-08-01 写入首条：流式状态机验证教训，含 MEMORY.md 索引）。后续可把项目关键决策/教训持续写入
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **CharacterEngine mood 升级**（原 EmotionEngine 升级）: LLM `<mood>` 标签驱动已实现。生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充等后续 Phase 待做。
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **多角色 prompt 模板**: `PromptAssembler` 通过 `SystemPromptManager` + 12 `PromptSection` 渲染，多角色时加载不同 `CharacterConfig` 即可切换 section 数据源
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + SenseVoice + GPT-SoVits）。数据库已完成 docker-compose.yml（MongoDB + Milvus）
- **F2 代码块格式重新启用**: `f2_output_format.j2` 中 `<|code|>` 和 `<|markdown|>` 格式已注释。等需要代码/Markdown 渲染功能时取消注释即可启用。**注（2026-08-01）**：解析器已支持 ```` ``` ```` 围栏 + `<|code|>`/`<|markdown|>`（core/text tokenizer），LLM 主动输出代码块时即可正确分拣（不进 TTS、finalize parts）；仅需 F2 模板取消注释 + 前端代码渲染确认
- **意图路由 + 渐进式工具披露**: 当前 12 个工具全量暴露给 LLM，每人次 ~1,643 schema tokens。大部分对话只用 2–4 个工具，~70% schema token 是浪费的。评估结论：12 个工具未到临界点，暂不做。触发条件：工具数 > 15 考虑规则分类，> 20 必须引入分类层，或累计 ≥ 3 次工具误用事件。推荐方案：关键词匹配分组（filesystem/web/memory/utility），零延迟零 token 成本，无命中则全量兜底。
- **CharacterEngine Phase 2**（原 PersonalityEngine Phase 2）:
  - CharacterReflector.j2 模板 ~140 行偏长，LLM 注意力可能被稀释，后续观察产出质量决定是否精简
  - C3 情绪传染规则动态化
  - C7 主动性动态化
  - L4→L3 情绪汇聚统计（需 CharacterEngine 改接口）
  - 用户话题检测触发 C9（目前只看状态存在+冷却）


## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:97`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 246 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |
| 低 | **FilePart/AudioPart/VideoPart 预留代码** | `part.py` 130 行 + `message_adapter.py` 3 个 helper + `Part` union。`message_adapter.py:188` 注释明确 Phase 2。暂保留 |

## 模块化改进

| 优先级 | 目标 | 说明 |
|--------|------|------|
| ~~低~~ ❌ | **`Module/tool/safety.py`（618 行）** | 评估结论：不拆 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` 仍含真实 API Key | DeepSeek、MiMo、Tavily、MiniMax 的真实 key 仍在 `.env` 文件中。已有 `.env.example` + `setup.sh` + `app.py --check`，但 `.env` 不应提交真实密钥。建议：将 `.env` 中 key 替换为占位符，真实 key 通过 shell 环境变量或密钥管理器注入 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |
| **高** | **无数据库迁移机制** | MongoDB 索引在 `connect()` 时 ad-hoc 创建，Milvus collection 硬编码创建。若字段/索引/维度需变更，无 upgrade path。`memory_repo.clear_all()` 是破坏性操作无安全守卫 |
| 低 | **`character/cyrene/` 不可用** | 缺少 `config.yaml` 和 `personality.md`，仅含 GPT 参考材料，无法作为角色加载 |
| 低 | **依赖无版本上限** | `requirements.txt` 中 pydantic、openai、torch 等均无上限约束，上游 breaking change 可导致不可预期问题。`torch` 是 2GB+ 重依赖 |
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |

## 多前端支持（2026-08-01 评估，场景 A：新前端连运行中的服务端）

**评估结论：不加中间层（YAGNI）。** `core/input/` 已是传输适配层（WS `/ws` + HTTP `/api/*` + Debug `/debug/api/*`），Agent 公开接口已是服务层（ws_handler 全部走公开接口），WS 协议 UI 无关（20+ 语义事件，非 DOM 操作）——Qt 等新前端可直接连同一 `/ws`。CLI 走进程内驱动（`run_repl` 不经过网络层，两套 driver 是已接受的现状）。若做 Qt 前端，需先补齐协议契约成熟度：

| 优先级 | 事项 | 说明 |
|--------|------|------|
| 低 | **协议契约语言中立化** | 契约目前只存在于 `frontend/src/types/protocol.ts`（TS 类型），需产出 JSON Schema / OpenAPI / protocol.md 供非 TS 客户端实现 |
| 低 | **协议版本化** | WS/HTTP 消息加 `protocol_version` 字段，服务端升级协议时不静默破坏旧客户端 |
| 低 | **多 session / 身份** | 关联上表「单 Session 多客户端共享」——新前端需要独立 session 身份 |
| 低 | **公开契约文档** | 20+ WS 服务端消息 + 5 入站类型 + HTTP/Debug 路由写为 protocol.md |

> 触发条件：确定做 Qt 客户端（或任一第二前端）时再动，现仅记录评估结论。

## 来自 code-review-report-2026-06-22 的遗留问题

### 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN `integrity`、Content-Security-Policy |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线换新 API |

### 中低危（待排期）

| 问题 | 文件 |
|------|------|
| llm_adapter close fire-and-forget | `core/adapter/llm_adapter.py:140-143` |
| openai_tts test() 假阴性 | `tts/openai_tts.py:73` |
| TTS 取消+HTTP 流清理交互可能泄漏 | `tts/gpt_sovits.py:134-150` |
| 缺失测试覆盖 | DescribeImagePlugin (0)、CognitiveLoop 边界、OutputManager 错误隔离 |

## 测试补充

- `core/input/audio_buffer.py` (3)
- `core/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时
- 上述覆盖率缺口中标记为"关键"和"高"的路径

> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
