# 待做

> **CI 配置**：`Module/workflow_engine/` 尚未完善、非当前开发重点，`test-quick` 和 `test-normal` 已通过 Makefile 跳过该目录（`--ignore=Module/workflow_engine/tests`）。完整跑 `test-full` 或手动指定路径时仍会执行。

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **LLM 有时不输出 <mood> 标签** | deepseek-v4-flash 遵循结构化输出不稳定，部分轮次 `<mood>` 缺失，回退到关键词 fallback。需强化 prompt 或在 F2 加更强制性的约束 |
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 低 | **`test_integration_global_constraints_respected`/`test_integration_global_constraints_default_ok` 偶发失败** | `Module/workflow_engine/tests/integration/test_delegation_preparer.py`: 真实 LLM 集成测试，全量/单独跑均偶发 FAIL——根因是 **LLM 判定不稳定**（intent 缺收件人邮箱 → 时而 `too_vague` 时而不，非约束逻辑/时序）。2026-08-09 实测确认与 pyright 批次的 schema 泛型改动无关 |
| 低 | **`test_multi_step_task` 偶发失败** | `Module/workflow_engine/tests/integration/test_planner.py`: DeepSeek API 偶发返回截断 JSON (`Unterminated string`)，LLM 输出不稳定导致 `PlanParseError` |
| 中 | **TTS 密集短句导致 wait_all 阻塞** | TTS 连续 5+ 短句密集入队时 MiniMax 部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING。已缩短 read timeout 120s→30s 缓解，根因（wait_all 无超时）待重现后修复。 |
| 低 | **`flush_after_response` 超时层不匹配** | 内层 `loop.py:897` 每 task 5s，外层 `lifecycle.py:191` 整体 10s。3+ task 同时超时可能被外层截断。实际 pending 典型 0-2 个、亚秒级完成，impact 极低。修法：外层 timeout 改为 `max(10.0, len(pending) * 5.0 + 1.0)` |
| 低 | **`ConfirmModal` @keydown 在不可聚焦 `<div>`** | `frontend/src/debug/components/shared/ConfirmModal.vue:35` — overlay div 无 `tabindex`，依赖按钮聚焦后事件冒泡。弹窗内只有文字+两个按钮，点击文字不会失焦，现有行为已可靠。加固：加 `tabindex="0"` |
| ~~中~~ ✅ | **STT 运行时切换不生效** | 2026-08-03 已修（服务运行时状态特性）：`switch_stt` 真正建后端（先建后关 + 1s 连通性测试 + 写 ServiceState）；`stt_transcribe` 收进 agent 运行时动态读 `agent.stt` 槽位，热切换对既有 WS 连接生效（不再依赖启动期闭包） |
| 低 | **`core/tests/test_phase2.py`/`test_phase3.py` 裸 `setdefault("DEEPSEEK_API_KEY","test")`** | 与 `test_task_scheduler._ensure_api_key` 同模式（后者已改 monkeypatch，2026-08-02）。当前 test-quick collection 顺序下未触发，但属进程 env 污染隐患——collection 顺序变化或并行跑时可能污染真实 LLM 集成测试。建议统一改 monkeypatch |
| ~~中~~ ✅ | **空 LLM 响应被静默吞掉** | 2026-08-02 已修：`_think_loop` 空响应守卫——无文本+无工具调用 → 注入 nudge 系统消息重试一次 → 仍空发兜底回复。兜底同时注入 `all_sentences` 保证 session 归档与显示一致。见 `core/cognitive/loop.py` `_EMPTY_RESPONSE_NUDGE`/`_EMPTY_RESPONSE_FALLBACK` + `core/cognitive/tests/test_empty_response_guard.py` |
| 低 | **NORMAL 态 `_pending`/`_held` 缓冲无界** | `tokenizer.py:288/300/306` — `_pending += c`（普通文本/行中反引号）、`_held += c`（行首反引号前缀）无上界。C 节加固（2026-08-02）只覆盖 tag 态 7 缓冲，NORMAL 态路径漏网：长无换行段落可把 `_pending` 撑到 MB 级、巨型反引号行撑 `_held`。既有行为非回归，final review 判 defer（load-bearing：分支成为"内存已全有界"引用来源但不覆盖此路径）。修法：给 `_pending`/`_held` 加上界（如 `FENCE_MAX_CONTENT` 同量级），超限按 SPEECH 落盘 |
| ~~低~~ ✅ | **STT env 变量陈旧：`SENSEVOICE_URL` vs `STT_API_URL`** | 2026-08-07 已修：统一到 `SENSEVOICE_URL`（运行时唯一生效路径——registry 经 `providers.yaml` 的 `url_key` 读取，preflight/.env.example/config 全部对齐）。原 todo 记录修法方向「统一到 `STT_API_URL`」**有误**——那是旧变量，照它配置的 env 不会被运行时读取（实测：`STTFactory.create` 只认 `SENSEVOICE_URL`）。修复：`Module/stt/config.py` + `core/config/app_config.py` 的 `env="STT_API_URL"` → `"SENSEVOICE_URL"`；`Module/bench/bench.py` 同改；`test_preflight.py` 测试删错变量 bug（`delenv("STT_API_URL")` → `delenv("SENSEVOICE_URL")`）；`test_config.py` env_key 同改 + 新增 env 绑定守护测试；`config_test_helpers.test_defaults` 加 env 隔离（防本地 .env 污染默认值断言） |

## Electron v2 联调：后端契约封口（2026-08-09 审计）

> 审计基线：后端 `911bbe0`、Electron 前端 `4bc7f01`。后端 Electron 专项测试 `576 passed`，所以下表主要是**跨仓库契约与产品化封口**，不是已实现主链路整体不可用。完整前端侧审计与 raw→domain 映射见 `/home/yomu/elysia-frontend/docs/18-Cyrene-Electron-真实后端契约差异与前端修复清单-2026-08-09.md`。本节取代下文“多前端支持（2026-08-01）”中已经过时的“契约只存在于手写 TS / 尚无多会话”前提。
>
> **Batch 9A+9B+9C（8 项）已于 2026-08-09 完成**：commit `9e96657..d58493d`（8 个 feature commit）。设计见 [spec](docs/superpowers/specs/2026-08-09-electron-v2-contract-9abc-design.md)（含 4 项用户拍板 + 探索定案 + 两轮 code-review 15 finding 记录），实现见 [plan](docs/superpowers/plans/2026-08-09-electron-v2-contract-9abc-plan.md)（两轮 review 19 finding 记录）。**跨仓 replay CI 本批未做（D2：仅后端侧产物 + golden frames 归档）**；产品化门禁 2 项（下表 38/39）defer 单独批次。
>
> **外部 code review（2026-08-10）**：`docs/review/code-review-2026-08-09-electron-v2-contract-9abc.md`（基线 a5fc620..0939eef）审出 4 P1 + 2 P2，复验两轮又 7 项。修复 commit：`fb66c30`（P1-1~P2-2 六项：离线 create 判幽灵 / profile dictation 全局 STT 门禁 / text 绕过校验 TypeError 断连 / TS ClientMessage UnknownMessage 放宽 / 删除竞态复活 / golden frames 语义）、`bfd5840`（R1-R5：v2 list-content docstring+测试 / RECORDING 超限帧毁转录 / delete pending 门禁 / error ACK 收口 / gen_schema fail-fast）、`0abd000`（S3+S6：段/WAV 不再 640 整帧截断 / 超限 error 移入终止分支）。**已知权衡（未修代码，记录备查）**：S1 in-flight 删除竞态已收口（pending 门禁 + resolve 严格化），残余窗口极窄、完整闭环需 tombstone/epoch 后续；S2 握手 `"2.0"` 400 锁死为 9B-3 明确设计决策（缓解 = 拒绝 message 明示支持集合 + 产物 transportVersion）；S4 `<640B` 录音视为空转写不发 transcript（协议语义，文档 §12.2 已澄清）；S7 显式未缓存会话 resolve 两次 Mongo 往返（缓存命中零开销、显式未缓存低频）。

| 优先级 | 后端待办 | 当前行为与风险 | 后端完成标准（DoD） |
|--------|----------|----------------|----------------------|
| ~~高~~ ✅ | **`session` 回放携带明确 `conversationId`** | `SessionMessage` 目前只有 `messages/token_stats`；建连又先发 `session`、后发 `server_info`。Electron 重连时无法仅凭回放确认消息归属，只能额外分页查询 `/api/sessions` 的 `active`。见 `frontend_contract/ws_message.py:253`、`core/input/ws_handler.py:83` | 给 `SessionMessage` 增加必发 `conversationId`（旧客户端忽略新增字段即可）；建连与 `activate_session` 回放均填写实际目标会话；更新协议、生成 TS、契约测试与当前 golden trace；增加“活动会话切换后回放 ID/消息/token_stats 同源”测试 |
| ~~高~~ ✅ | **历史分页返回显式 opaque cursor，并消除 `before` 双 ID 歧义** | `GET /api/sessions/{id}/messages` 的 `before` 实际只接受 Mongo `_id`（24-hex），`after` 却接受稳定 `messageId(msg_)`；响应只有 `messages/has_more/total`，前端必须从私有 `_id` 猜下一游标。现有 Electron 文档已误写成 `before={messageId}`，真实接入会 400。见 `core/input/session_routes.py:544`、`core/storage/message_repo.py:297` | 响应增加显式 `cursor/nextCursor`（opaque，不要求客户端解释）；优先让 `before` 同时兼容返回 cursor 与稳定 `messageId`，或至少在协议中严格区分；保持旧 `_id` cursor 向后兼容；覆盖非法 cursor、两页无重/无漏、倒序 before、正序 after、删除锚点等测试 |
| ~~高~~ ✅ | **显式 `conversationId` 必须验证存在，禁止懒创建幽灵会话** | `SessionManager.resolve()` 对任意显式 ID 直接 `ensure_loaded()`；`_route_v2_text` 与 `voice.commit` 因而可能接受已删除/拼错的会话 ID，并重新构造空会话。CRUD/activate 已做存在性检查，但发消息路径没有。见 `core/memory/session_manager.py:161`、`core/input/ws_handler.py:541/704` | v2 的显式 conversation ID 在生成 messageId、写幂等表和 ACK accepted 前调用 `has_session()`；未知 ID 回 `user_message.ack rejected + SESSION_NOT_FOUND`，不得创建内存/持久化会话或增加附件 refs；缺省 ID 仍解析当前 active；文本、附件、voice.commit 均有正负测试 |
| ~~高~~ ✅ | **固定并可验证客户端输入音频契约** | 服务端实际把所有 v2 BINARY 当作 `16kHz/mono/pcm_s16le`；VAD 每 20ms=640 bytes 处理，小于一帧视为非语音，非整帧尾部被忽略。公开协议只写“PCM”，start 帧也不带 descriptor；Electron 很容易直接发 48k Float32 或 WebM/Opus，表现为静默无字幕/乱码。见 `core/input/audio_buffer.py:1/30/175`、`core/input/voice_sessions.py` | 在协议中固定输入格式，或给三种 start 帧增加 `audio{codec,sampleRate,channels}` 并由服务端校验；不支持格式/非法 chunk 返回稳定 `CONTRACT_VALIDATION`，不能静默吞；明确 stop 尾帧策略、最大 chunk/背压；增加真实 PCM fixture 覆盖 recording/realtime/dictation 与错误格式。**注（2026-08-09）**：descriptor 去掉 `chunkDurationMs`（固定 20ms 帧，客户端无需声明）；段边界整帧对齐扩到所有模式（含 REALTIME/DICTATION）；新增 `MAX_CHUNK_BYTES`（1 MiB）校验。**注（2026-08-10，S3/S6）**：段/WAV 边界**不再 640 整帧截断**（S3——截断丢真实尾音，整段含尾部原样送 STT，VAD 检测仍按 640B 整帧扫描，`<640B` 视为空转写）；超限 chunk 在 TRANSCRIBING/PENDING（录音已 stop、在途 STT/待 commit）窗口只丢弃不发 error（S6——error 帧只在真正终止的分支发） |
| ~~高~~ ✅（后端侧产物，跨仓 replay defer） | **发布 Electron 可消费的版本化机器契约并做跨仓 replay CI** | `frontend_contract` 是后端单源，但生成 TS 只落旧 Vue 目录；Electron 仓库手写领域 Zod，未持有 raw schema。归档 batch-7 traces 已过时（例如 `by_model`，当前为 `byModel`），双方各自测试全绿仍无法发现 wire 漂移 | 从 `frontend_contract` 生成并发布 JSON Schema/独立 TS raw contract 或可同步 artifact；产物带协议版本、后端 SHA/schema hash；生成当前 handshake/chat/session/tool/voice golden frames；Electron CI 用同一 frames 做 raw schema + mapper replay；后端 CI 检查 artifact/trace 漂移，禁止归档 trace 重新成为事实源。**注（2026-08-09，D2）**：本批做**后端侧**——`contract.schema.json` + `contract.raw.ts`（transportVersion "2"/protocolVersion "2.0"/schemaHash）+ `scripts/gen_contract_frames.py` 契约源 golden frames + `--check` 漂移门禁 + `capture_ws_trace.py` 停用；**跨仓 replay（改 elysia-frontend 消费）defer** 后续专项 |
| ~~中~~ ✅ | **Dictation 使用会话内 STT profile，不修改全局活动 STT** | `dictation.start` 只有 `clientRequestId`，实际使用全局活动 STT；Electron 的 `sttProfileId` 只能先 `switch_model`，会同时影响聊天/实时语音并与其他客户端竞争。见 `frontend_contract/ws_message.py:783`、`core/input/ws_handler.py:1021` | 给 `dictation.start` 增加可选 `sttProfileId`（或稳定 model/profile 标识）；start 时解析并快照到当前 dictation session，不修改全局 `agent.stt`；profile 不存在/不可用返回稳定错误；覆盖并发连接、途中全局切换不影响已开始 dictation、stop/cancel 资源释放 |
| ~~中~~ ✅ | **所有 Client→Server WS JSON 统一走 Pydantic 判别联合校验** | `handle_json_message()` 仍大量使用 `data.get()` 手工解析，并未对 `V2TextMessage/ToolStatusMessage/Voice*` 等执行统一 `model_validate`。错误类型可能被静默忽略，甚至在幂等 dict 等位置以 `TypeError` 断开单连接；顶层目前只捕获 `JSONDecodeError/ValidationError`。见 `core/input/ws_handler.py:298/757` | 按协议版本对完整 ClientMessage 判别联合做一次入口校验，再把模型交给 handler；拒绝 unknown type、错误枚举、错误字段类型、缺必填字段，统一返回 `ErrorMessage/ACK rejected CONTRACT_VALIDATION`；handler 不再依赖宽松 `data.get()` 默认值；补 fuzz/表驱动负面测试并保证畸形消息不关闭连接。**注（2026-08-09）**：`ClientMessage` 判别联合**不含 `TextMessage`**（type="text" 撞 discriminator）；非 text 走判别联合；顺带修 `:304` 单参 `ValidationError` 非 dict 帧断连 bug。**注（2026-08-10，P1-3）**：v2 text **完整帧 `V2TextMessage.model_validate`**（ValidationError → ACK rejected CONTRACT_VALIDATION，保留 ACK 通道；`attachments` 类型错/`clientRequestId` 非 str 等畸形字段不再抛 TypeError 断连）；`handle_ws` TEXT 加 `except Exception` 兜底 INTERNAL 不关闭连接 |
| ~~中~~ ✅ | **协议版本协商显式拒绝不兼容版本** | 握手头只有精确字符串 `"2"` 才走 v2；`"2.0"`、未来版本或拼写错误会静默降级 v1，而下发 `server_info.protocol_version` 又是 `"2.0"`，命名容易误用。见 `core/input/ws_handler.py:129`、`frontend_contract/ws_message.py:281` | 明确区分 transport negotiation token 与展示版本；对显式但不支持的版本在升级前返回 4xx/WS close code + 稳定错误，只有缺省头才兼容 v1；协议文档给出支持集合；覆盖 `2`、`2.0`、未知版本、缺省头与 Electron platform 组合测试。**注（2026-08-09）**：支持集合 {缺省/`"1"`→v1, `"2"`→v2}（显式 `"1"` 保留合法 v1，防现网回归）；`"2.0"`/`"3"`/乱写 → 升级前 400 ProtocolError |
| **中（部署门禁）** | **非回环生产绑定改为可配置 fail-closed 鉴权** | 默认可绑定 `0.0.0.0`，未设 `AGENT_ACCESS_TOKEN/SERVER_AUTH_TOKEN` 时只记录显著日志，产品 `/ws`、`/api/*`、`/uploads/*` 仍可匿名访问；日志警告容易在桌面打包/守护启动时被忽略。见 `core/input/server.py:44` | 增加显式开发模式或 `ALLOW_INSECURE_REMOTE`；非回环且无完整鉴权时默认拒绝启动，只有显式 opt-in 才允许；生产文档要求 HTTPS/WSS、双 token、附件签名；覆盖回环开发、非回环拒绝、显式 insecure opt-in、完整鉴权四种启动测试 |
| **低→产品化前中** | **幂等键跨进程持久化或明确限定重试窗口** | `clientRequestId` 幂等表是进程内 dict、TTL 1h，重启后丢失。Electron 在“消息已提交但 ACK 丢失 + 后端重启”后重试，可能重复生成；当前协议把它定义为 Demo 可接受限制。见 `core/input/idempotency.py` | 产品化前将 `(clientRequestId, contentDigest, messageId, conversationId, status, expiresAt)` 持久化并建唯一约束，或在 server_info/协议中明确暴露幂等能力与窗口；覆盖 ACK 丢失、同键同内容、同键异内容、重启后重试、过期回收 |

### 不应登记成后端缺陷的前端适配项

- `generation.segment.completed.content` 是后端权威完整文本；inner voice 和 TTS 场景允许零 delta。应由 Electron Adapter/Accumulator 消费，后端不应为兼容错误前端而伪造 delta。
- `generation.completed.conversationUsage` 的真实定义是 `{total, byModel}`；Electron 不应虚构会话累计 `input/output`。若产品以后需要累计输入/输出拆分，应另立需求扩展 tracker 与契约。
- raw wire 使用 `type/delta/mood/generationId`，Electron 领域层使用 `event/text/emotion/responseId`，属于 Main Adapter 的正常改名职责。
- Desktop bridge 的 `tool.request/tool.status` 安全元数据与状态机已实现并通过专项测试；当前缺口主要是 Electron `ToolResult` 领域接口过窄。

### 建议批次顺序

1. ✅ **Batch 9A（文本/历史联调阻断）**：session conversationId、历史 cursor（严格区分）、未知会话拒绝——**2026-08-09 完成**（9e96657/931c35b/2711c90）；
2. ✅ **Batch 9B（契约工程化）**：机器契约（后端侧产物 + frames 归档，跨仓 replay defer）、统一入站校验、严格版本协商——**2026-08-09 完成**（0fac4da/d2f8178/ca74f0f）；
3. ✅ **Batch 9C（语音联调）**：输入音频 descriptor/验证、dictation profile 隔离——**2026-08-09 完成**（778c859/d58493d）；
4. **产品化门禁（待做）**：远程鉴权 fail-closed、持久化幂等。

## 模块化评估（2026-08-01）

基于 import 图分析（AST 解析全仓 28 个包、~340 py 文件、118 条包间依赖边）。总体分层健康（`core → Module` 正向为主）。**已修部分归档**：硬环拆除 + Agent 公开接口见 [docs/completed/2026-08.md](docs/completed/2026-08.md)。

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

已修复 42/44（详情见 [docs/completed/2026-07.md](docs/completed/2026-07.md) 2026-07-31 + [docs/review/code-review-2026-07-31.md](docs/review/code-review-2026-07-31.md)）。2 项遗留：

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
- **意图路由 + 渐进式工具披露**: 当前 16 个工具全量暴露给 LLM，每人次 ~1,643 schema tokens。大部分对话只用 2–4 个工具，~70% schema token 是浪费的。评估结论：16 个工具未到临界点，暂不做。触发条件：工具数 > 15 考虑规则分类，> 20 必须引入分类层，或累计 ≥ 3 次工具误用事件。推荐方案：关键词匹配分组（filesystem/web/memory/utility），零延迟零 token 成本，无命中则全量兜底。
- **CharacterEngine Phase 2**（原 PersonalityEngine Phase 2）:
  - CharacterReflector.j2 模板 ~140 行偏长，LLM 注意力可能被稀释，后续观察产出质量决定是否精简
  - C3 情绪传染规则动态化
  - C7 主动性动态化
  - L4→L3 情绪汇聚统计（需 CharacterEngine 改接口）
  - 用户话题检测触发 C9（目前只看状态存在+冷却）

**前端（合并自 frontend_todo.md 归档，2026-07-30 前端审查遗留）**：
- **迁移至 AudioWorklet**: `InputBar.vue` 仍用已废弃的 `audioCtx.createScriptProcessor()`。阻塞原因：AudioWorklet 需独立 `.js` 文件 + 复杂重构
- **messageRouter 统一消息分发**: `useWebSocket.ts` 各 case 分支已显式类型收窄，建议进一步集中管理映射 + try/catch 边界
- **useLocalStorage composable**: `settingsStore.ts` 5 组 localStorage watch 模式完全相同，可提取复用
- **Vue 错误边界**: 目前无 `errorCaptured` 钩子或全局 `app.config.errorHandler`
- **L-#13 ArchivePanel 分页参数命名**: `ArchivePanel.vue` API 契约不变，仅命名建议
- **L-#15 硬编码颜色**: `variables.css:33` 绕过 CSS 变量，不影响功能


## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:97`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 246 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |
| 低 | **FilePart/AudioPart/VideoPart 预留代码** | `part.py` 130 行 + `message_adapter.py` 3 个 helper + `Part` union。`message_adapter.py:188` 注释明确 Phase 2。暂保留 |
| ~~低~~ ✅ | **衍生 ID 未类型化** | 2026-08-09 已修：AttachmentId/MemoryId/TaskId 立项 + 贯通（part/repo/long_term/schema/memory_reflector/task/scheduler/input）。spec/plan 见 [spec](docs/superpowers/specs/2026-08-09-derived-id-newtype-design.md) / [plan](docs/superpowers/plans/2026-08-09-derived-id-newtype-plan.md)。已知权衡：Task.parent_id 语义混乱留待未来、scheduler:159 死代码 ignore |

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
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed/2026-07.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |

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

> 已完成的工作按月归档在 [docs/completed/](docs/completed/)。
