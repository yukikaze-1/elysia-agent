# 待做

> **CI 配置**：WorkflowEngine 已用 `external` marker 取代 `--ignore`（F1）——unit/service 测试入 `test-quick`/`test-normal` 合并门禁，真实 LLM/DB 集成测试标 `external`、独立 `test-external` opt-in（`RUN_EXTERNAL_TESTS=1`）。`test-quick`/`test-normal` 不再跳过任何目录。

## 决策记录

> **任务中心（TaskSystem + TaskService）暂缓不做（2026-08-30）**：产品任务中心按当前决策继续暂缓。正式依据见
> [`docs/2026-08-29-task-system-overall-architecture-design.md`](docs/2026-08-29-task-system-overall-architecture-design.md)
> 与 [`docs/2026-08-29-task-system-backend-architecture-design.md`](docs/2026-08-29-task-system-backend-architecture-design.md)
> （Task/TaskStep/ToolExecution 三实体目标架构 + 分批实施 A~E + 待决策 TSA-P01~P10/BE-D01~D13）。
> `docs/2026-08-30-后端架构分层与模块化增量评估.md` 亦明确排除任务中心，聚焦收窄现有边界治理（即时工具调度语义收窄、应用用例边界、边界门禁、数据库迁移）。
> 暂缓期内不把普通工具调用并入 WorkflowEngine，不创建产品 TaskRecord，不动现有 wire 契约，不实现任务中心 REST/WS API。
> 恢复激励点：需要可查询/可控/可恢复展示的任务历史时，从批次 A（公共契约 + 持久读模型）启动。

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
| ~~中~~ ✅ **关闭（2026-08-11 决策：不做）** | **前端 WS 无法携带 Bearer token → `AGENT_ACCESS_TOKEN` 模式下浏览器前端 WS 连不上** | 业务中间件只认 `Authorization` 头，WS 握手不能设自定义 Header（浏览器限制）。**2026-08-11 决策**：v1 浏览器聊天已停用（`SERVE_CHAT` 默认关，见本批次）；Electron 用 Node `ws` 带 `Authorization` header（real-backend.ts:254-260，已核实）不受影响；Debug 页走 REST/SSE 无 WS。**无浏览器 WS 客户端 → WS query-param token 无消费方，不做**。原修法（`/ws?token=...`）保留为未来若恢复浏览器聊天时的备选 |
| ~~中~~ ✅ | **空 LLM 响应被静默吞掉** | 2026-08-02 已修：`_think_loop` 空响应守卫——无文本+无工具调用 → 注入 nudge 系统消息重试一次 → 仍空发兜底回复。兜底同时注入 `all_sentences` 保证 session 归档与显示一致。见 `core/cognitive/loop.py` `_EMPTY_RESPONSE_NUDGE`/`_EMPTY_RESPONSE_FALLBACK` + `core/cognitive/tests/test_empty_response_guard.py` |
| 低 | **NORMAL 态 `_pending`/`_held` 缓冲无界** | `tokenizer.py:288/300/306` — `_pending += c`（普通文本/行中反引号）、`_held += c`（行首反引号前缀）无上界。C 节加固（2026-08-02）只覆盖 tag 态 7 缓冲，NORMAL 态路径漏网：长无换行段落可把 `_pending` 撑到 MB 级、巨型反引号行撑 `_held`。既有行为非回归，final review 判 defer（load-bearing：分支成为"内存已全有界"引用来源但不覆盖此路径）。修法：给 `_pending`/`_held` 加上界（如 `FENCE_MAX_CONTENT` 同量级），超限按 SPEECH 落盘 |
| ~~低~~ ✅ | **STT env 变量陈旧：`SENSEVOICE_URL` vs `STT_API_URL`** | 2026-08-07 已修：统一到 `SENSEVOICE_URL`（运行时唯一生效路径——registry 经 `providers.yaml` 的 `url_key` 读取，preflight/.env.example/config 全部对齐）。原 todo 记录修法方向「统一到 `STT_API_URL`」**有误**——那是旧变量，照它配置的 env 不会被运行时读取（实测：`STTFactory.create` 只认 `SENSEVOICE_URL`）。修复：`Module/stt/config.py` + `core/config/app_config.py` 的 `env="STT_API_URL"` → `"SENSEVOICE_URL"`；`Module/bench/bench.py` 同改；`test_preflight.py` 测试删错变量 bug（`delenv("STT_API_URL")` → `delenv("SENSEVOICE_URL")`）；`test_config.py` env_key 同改 + 新增 env 绑定守护测试；`config_test_helpers.test_defaults` 加 env 隔离（防本地 .env 污染默认值断言） |

> **阶段 3 真实联调后端整改（2026-08-15，已闭环）**：前端阶段 3 已冻结 `HistoryCategory.sticker`、自然日日期发送与逐文件 `kind` multipart，后端补三处确定性契约错位（`docs/review/2026-08-15-阶段3真实联调后端整改清单.md`）：**BE-S3-INT-P0-01** `/api/search/messages` 支持 `category=sticker`（白名单 8 值枚举化，`{"type":"sticker"}` $elemMatch；`snippet` 空串合法）；**BE-S3-INT-P1-01** 日期参数 handler 层 fail-fast（严格 `YYYY-MM-DD`，非格式 400 `CONTRACT_VALIDATION`，不再吞 200 空结果；与前端 FE-S3-INT-P0-01 同步放行）；**BE-S3-INT-P0-02** `/api/uploads` 逐文件 `kind`（每文件一组 `kind+files` 邻接配对 + 单 kind 特判旧形态 + 悬空/歧义 400 + 保留 ≤10 文件与整批原子性）。category/kind 白名单收敛为契约层 `SearchCategory`（8 值）/`UploadKind`（4 值）StrEnum（D3-B，不被字段引用 → TS 快照零漂移）。设计见 [spec](docs/superpowers/specs/2026-08-15-stage3-integration-backend-fixes-design.md)（决策 D1-D9）、实现见 [plan](docs/superpowers/plans/2026-08-15-stage3-integration-backend-fixes.md)，执行记录见 `docs/progress/2026-08-15-stage3-integration-backend-fixes-execution-record.md`。commit `dc84415c..6287c05d`（4 feature）+ `c517221c`（final review low-2 测试补丁），`test-quick` 3581 / `test-normal` 3689 passed，ruff/pyright 0 errors/check-types 全绿。

> **阶段 3 真实联调后端复验修复（2026-08-15，已闭环）**：真实 Windows Electron 复验新发现 2 项后端 P0（`docs/review/2026-08-15-阶段3真实联调后端复验问题清单.md`）：**BE-S3-RUN-P0-01** 视觉工具会话换绑——`ToolContext.services["session"]` 启动注入后从不更新，`describe_image` 从旧会话读图 → `core/cognitive/loop.py::process()` 每轮目标会话解析后原子换绑 `services["session"]`+`["_session_msg_count"]`（`clone_for_call` 浅拷贝，本 turn 工具副本立即可见；失败/切换由下一轮覆盖）；**BE-S3-RUN-P0-02** 音频 MIME 规范化——探测值（WebM→`video/webm`、WAV→`audio/x-wav`）原样写记录/响应，`kind=audio + video/webm` 非法致前端判「不支持」+ 重载前后 mime 漂移 → `core/schema/utils.py::normalize_attachment_mime` 单源（audio+video/webm→audio/webm、audio+x-wav→audio/wav），写路径（上传）/读路径（消息恢复）/下载 Content-Type 三处共用，存量记录经读路径兼容，未知 mime 静默回退改 `logger.warning`。决策 D0-D5（合并单批/同步两派生值/process 内联/规范化函数放 schema/utils/存量纯读路径）。设计见 [spec](docs/superpowers/specs/2026-08-15-stage3-run-backend-fixes-design.md)、实现见 [plan](docs/superpowers/plans/2026-08-15-stage3-run-backend-fixes.md)，执行记录见 `docs/progress/2026-08-15-stage3-run-backend-fixes-execution-record.md`。commit `df165e30..e5bd3a75`（4 feature）+ `9bdd110d`（注释收敛）+ `5b1da8ba`（协议文档）+ `b5bf4a25`（验收跟进），`test-quick` 3600 / `test-normal` 3708 passed，ruff/pyright 0 errors/check-types 全绿。（注：「已闭环」指开发修复闭环，真实 Electron 回归通过前后端阶段 3 标记停留在「部分联调通过」。）

> **阶段 4 联调前后端整改（2026-08-15，已闭环）**：阶段 4（Profile/模型切换/三语音模式/TTS 播放）主体已实现，联调前清 4 项（`docs/review/2026-08-15-阶段4联调前后端整改清单.md`）：**BE-S4-INT-P0-01** 内置 SenseVoice Profile 测试丢失默认地址——`/api/providers/test` 走 `STTFactory.create` 从 `providers.yaml` 解析空 `base_url`，覆盖构造函数默认 → httpx `InvalidURL`；`sensevoice.py` 定义 `DEFAULT_BASE_URL` 单源（构造函数/`STTConfig`/`STTFactory` 三处引用），工厂内置分支空值兜底（custom 分支后，custom 仍 `raise ValueError`）——一处修复覆盖启动/切换/听写快照/Profile 测试 4 调用点（`server.py:221/422`、`voice_sessions.py:457`、`provider_routes.py:303`），优先级 body > env > providers.yaml > 内置默认；**BE-S4-INT-P0-02** 阶段 4 机器契约样例——`gen_contract_frames.py` 补 9 类缺帧（voice.transcript/dictation.*/audio/audio_cancelled/switch_model_result/service_mode_result/set_tts_voice_result）+ 5 REST 文件，共 17 golden 进 `--check` 漂移门禁，结果帧正反例键形状对齐模型注释；**BE-S4-INT-P1-01** `scripts/stage4_smoke.py` 确定性契约 smoke（进程内 `create_app` + 真 agent(fake LLM) + STT/TTS 桩，5 场景 24 检查——含 save/delete/PROFILE_IN_USE/switch_model/set_tts_voice，失败非零退出，`STAGE4_SMOKE_LIVE=1` opt-in 真实 provider）；**BE-S4-INT-P2-01** todo.md 能力说明更新（多会话/契约生成门禁/环境锁精确边界）。决策 D0-D6（合并单批/工厂层共享常量兜底/按流程拆 JSONL/裸 wire REST 样例/smoke 独立脚本/只改 7 行/后端产物前端另行消费）。设计见 [spec](docs/superpowers/specs/2026-08-15-stage4-联调前整改-design.md)、实现见 [plan](docs/superpowers/plans/2026-08-15-stage4-联调前整改.md)，执行记录见 `docs/progress/2026-08-15-stage4-联调前整改-execution-record.md`。commit `bd614239`（spec）+ `867d3f9b`（plan）+ `36f11449`（P0-01）+ `b17fa478`（P0-02）+ `7cc352eb`（smoke）+ `0d5fef91`（todo.md）+ `d3f03d3a`（pyright）+ `a7b96d56`（文档同步）+ `dfedfd51`（final review 补子场景），`test-quick` 3617 / `test-normal` 3725 passed，ruff/pyright 0 errors/check-types 全绿。（注：前端 checker 消费产物提升为 error 属 `elysia-frontend` 另行推进，见清单 §4.4；后端阶段 4 仅可标记「已开发/自动化已验证」，真实 Electron 联调通过前不得「已完整联调」。）

## Electron v2 联调：后端契约封口（2026-08-09 审计）

> 审计基线：后端 `911bbe0`、Electron 前端 `4bc7f01`。后端 Electron 专项测试 `576 passed`，所以下表主要是**跨仓库契约与产品化封口**，不是已实现主链路整体不可用。完整前端侧审计与 raw→domain 映射见 `/home/yomu/elysia-frontend/docs/18-Cyrene-Electron-真实后端契约差异与前端修复清单-2026-08-09.md`。本节取代下文“多前端支持（2026-08-01）”中已经过时的“契约只存在于手写 TS / 尚无多会话”前提。
>
> **Batch 9A+9B+9C（8 项）已于 2026-08-09 完成**：commit `9e96657..d58493d`（8 个 feature commit）。设计见 [spec](docs/superpowers/specs/2026-08-09-electron-v2-contract-9abc-design.md)（含 4 项用户拍板 + 探索定案 + 两轮 code-review 15 finding 记录），实现见 [plan](docs/superpowers/plans/2026-08-09-electron-v2-contract-9abc-plan.md)（两轮 review 19 finding 记录）。**跨仓 replay CI 本批未做（D2：仅后端侧产物 + golden frames 归档）**；产品化门禁 2 项（下表 38/39）defer 单独批次。
>
> **外部 code review（2026-08-10）**：`docs/review/code-review-2026-08-09-electron-v2-contract-9abc.md`（基线 a5fc620..0939eef）审出 4 P1 + 2 P2，复验两轮又 7 项。修复 commit：`fb66c30`（P1-1~P2-2 六项：离线 create 判幽灵 / profile dictation 全局 STT 门禁 / text 绕过校验 TypeError 断连 / TS ClientMessage UnknownMessage 放宽 / 删除竞态复活 / golden frames 语义）、`bfd5840`（R1-R5：v2 list-content docstring+测试 / RECORDING 超限帧毁转录 / delete pending 门禁 / error ACK 收口 / gen_schema fail-fast）、`0abd000`（S3+S6：段/WAV 不再 640 整帧截断 / 超限 error 移入终止分支）。**2026-08-10 后续复验修复**：原 S1「极窄残余窗口」已取消 defer——现用 per-session accepted-work 引用 + lifecycle lock + deleting/deleted tombstone 覆盖 EventBus、pending、生成、realtime voice 与异步 after-response/save，删除统一由 `SessionManager.delete()` 最终门禁；同时补齐任意 BINARY chunk 跨帧重组、Mongo 离线摘要 list/detail/rename 读后写一致，以及标准 JSONL golden/tool `conversationId`。同步报告见 `docs/review/electron-v2-backend-followup-fix-report-2026-08-10.md`。仍保留的明确决策：S2 握手 `"2.0"` 400 锁死为 9B-3 设计决策；S4 `<640B` **整段录音**视为空转写不发 transcript；S7 显式未缓存会话 resolve 两次 Mongo 往返（缓存命中零开销、显式未缓存低频）。
>
> **Electron 接入充分性复审（2026-08-10）**：基线 `9fdbfc1`，报告见 `docs/review/electron-v2-backend-integration-readiness-review-2026-08-10.md`。本批关闭 Desktop 新旧连接交叠误投、当前 cancel/fail golden 缺失以及协议/安全文档漂移；单机真实联调可开始。远程发布仍受下表 fail-closed 鉴权、持久化幂等和实际请求限流三项产品化门禁约束。
>
> **Electron 首次真实联调后端复核（2026-08-11 加固批次，已闭环）**：基线 `b5b7e6e`，报告见 `docs/review/electron-v2-first-integration-backend-review-2026-08-11.md`。P0×2（并发 claim 所有权 + DuplicateKey 不判死、idempotency 生命周期/readiness/durable 初值）+ P1×5（持久 claim 状态机与崩溃恢复、dev 降级连续性、WS 限流 clientRequestId 关联、ACK 判别联合 + 负面 golden、clean-start 会话发现）+ P2×3（探针分层、限流认证主体分桶、公开文档漂移）**全部闭环**。设计见 [spec](docs/superpowers/specs/2026-08-11-electron-v2-backend-hardening-design.md)（决策记录 D1-D9 + D2 执行期记录偏差：Rejected `clientRequestId` 无 min_length 允许空串——handler「缺少 clientRequestId」拒绝路径回显空串）；联调执行见 [runbook](docs/electron-v2-first-integration-runbook.md)（runtime SHA/schemaHash 待联调回填）。

> **Electron v2 后端加固后复核（2026-08-12，报告见 `docs/review/electron-v2-backend-post-hardening-review-2026-08-12.md`）**：BE-P1-06（recover 只补最小 claim、终态 ACK 负载丢失——重启后 rejected 变 `INTERNAL/未知错误`）+ BE-P1-07（离线启动首轮探测前 Mongo 恢复 → 首轮直接 true 漏补偿）**本批修复**：完整记录 restore 条件 upsert + store 显式 `needs_recovery`/durable 强化 + 协调器首轮补触发 + product `/ready` 门禁含补偿状态；BE-P2-04（runbook `server_info` 不携带 schemaHash）修正。**BE-RISK-01 接受风险保持开放**（accepted ACK 证明消息已入 EventBus、不等于业务工作已持久化——「ACCEPTED 后、loop `register()` 前 kill」窗口；首轮联调只能标记 happy path，生产封口需独立 outbox/可恢复 generation 证据 + 启动恢复重新入队机制）。设计见 [spec](docs/superpowers/specs/2026-08-12-electron-v2-backend-post-hardening-recovery-design.md)。
>
> **Electron v2 联调封口补强（2026-08-13）**：继续关闭完整记录恢复的两个并发边界：① `restore()` 在 get-first 后输掉 E11000 竞态时，现读回并返回 Mongo winner，`IdempotencyStore.recover()` 用 winner 刷新内存，避免保留 loser 却误报 durable；② 原“新增 key 集合”清位守卫升级为单调 recovery epoch，恢复 await 间隙即使是**同一 key** 的 `complete/reject` 写穿失败，也会保留 `needs_recovery` 到下一轮。回归覆盖 winner 读回、同 key 并发失败和再次恢复；整改报告见 `docs/review/electron-v2-integration-gap-fix-report-2026-08-13.md`。**BE-RISK-01 仍保持开放，本批未以单测替代 outbox/启动重入队的架构证据。**
>
> **Electron v2 首次真实联调消息 ID 修复（2026-08-13，已闭环）**：联调阻断——`ToolResultMessage` 默认空 `message_id`，两条写入路径不传、append 无守卫、契约无约束 → Electron REST 历史整页校验失败（`default` 最近 50 条工具消息全空 ID）。**BE-INT-P0-01**（新写入）：模型 `message_id` 必填+拒空、两处写入补 `new_message_id()`、append 归档守卫、契约 `min_length=1`；**BE-INT-P0-02**（历史）：读路径适配器对存量空 ID 派生稳定 ID（跨 archive/快照/JSONL 一致）+ 幂等回填脚本实跑 archive 69→0 空 ID、重跑零变化；**BE-INT-P1-01**：构造拒绝/归档/REST/搜索/JSONL/回填幂等全链测试 + `test-quick` 3542 passed。问题清单见 `docs/review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md`，设计见 [spec](docs/superpowers/specs/2026-08-13-electron-v2-first-integration-message-id-fix-design.md)，报告见 `docs/review/electron-v2-message-id-fix-report-2026-08-13.md`。待前端 `check:contract` 与真实 Electron 回归（问题清单 §7.2）验证。**BE-RISK-01 继续开放，与本批无关。**
>
> **Electron v2 二次真实联调 WS replay 身份修复（2026-08-13，已闭环）**：二次联调发现首轮消息 ID 修复遗漏独立出口——WS `session` replay 的 tool 分支未输出 `message_id`/`timestamp`，同一工具结果经 REST（`msg_7c0c72b0…`）与 WS replay（无身份）到达前端 → 重连后真实重复消息。**BE-INT2-P0-01**：`messages_for_client()` tool 分支输出消息自身身份（复用持久/派生 ID，不新生成）+ `ClientToolMessage` 必填；**BE-INT2-P1-01**：`SessionMessageItem.message_id/timestamp` 必填非空 + 三产物重生成 + 测试门禁（含 §5.3.5/5.3.6/5.3.7 补测试，`test-quick` 3548 passed）。整改清单见 `docs/review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md`，报告见 `docs/review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md`。真实 Electron 回归（清单 §7）与前端配套（Renderer 水合/replay fallback/取消码消费）由 `elysia-frontend` 验证。**BE-RISK-01 继续开放，与本批无关。**

| 优先级 | 后端待办 | 当前行为与风险 | 后端完成标准（DoD） |
|--------|----------|----------------|----------------------|
| ~~高~~ ✅ | **Desktop 工具请求按登记的 Electron `connectionId` 精确投递** | 复审时 Bridge 已把最新连接写入 `target_connection`，但 Agent 投递回调会取通道列表中的第一个 Electron；重连交叠时可能让旧实例执行副作用，新实例却收不到请求并最终超时 | Bridge 回调携带目标连接 ID；Agent 仅匹配 `client_platform=electron && connection_id=target`；精确目标消失时立即返回 `DESKTOP_UNAVAILABLE`；覆盖回调单测和两条真实 Electron WS 交叠 service 测试。**2026-08-10 已完成** |
| ~~高~~ ✅ | **`session` 回放携带明确 `conversationId`** | `SessionMessage` 目前只有 `messages/token_stats`；建连又先发 `session`、后发 `server_info`。Electron 重连时无法仅凭回放确认消息归属，只能额外分页查询 `/api/sessions` 的 `active`。见 `frontend_contract/ws_message.py:253`、`core/input/ws_handler.py:83` | 给 `SessionMessage` 增加必发 `conversationId`（旧客户端忽略新增字段即可）；建连与 `activate_session` 回放均填写实际目标会话；更新协议、生成 TS、契约测试与当前 golden trace；增加“活动会话切换后回放 ID/消息/token_stats 同源”测试 |
| ~~高~~ ✅ | **历史分页返回显式 opaque cursor，并消除 `before` 双 ID 歧义** | `GET /api/sessions/{id}/messages` 的 `before` 实际只接受 Mongo `_id`（24-hex），`after` 却接受稳定 `messageId(msg_)`；响应只有 `messages/has_more/total`，前端必须从私有 `_id` 猜下一游标。现有 Electron 文档已误写成 `before={messageId}`，真实接入会 400。见 `core/input/session_routes.py:544`、`core/storage/message_repo.py:297` | 响应增加显式 `cursor/nextCursor`（opaque，不要求客户端解释）；优先让 `before` 同时兼容返回 cursor 与稳定 `messageId`，或至少在协议中严格区分；保持旧 `_id` cursor 向后兼容；覆盖非法 cursor、两页无重/无漏、倒序 before、正序 after、删除锚点等测试 |
| ~~高~~ ✅ | **显式 `conversationId` 必须验证存在，禁止懒创建幽灵会话** | `SessionManager.resolve()` 对任意显式 ID 直接 `ensure_loaded()`；`_route_v2_text` 与 `voice.commit` 因而可能接受已删除/拼错的会话 ID，并重新构造空会话。CRUD/activate 已做存在性检查，但发消息路径没有。见 `core/memory/session_manager.py:161`、`core/input/ws_handler.py:541/704` | v2 的显式 conversation ID 在生成 messageId、写幂等表和 ACK accepted 前调用 `has_session()`；未知 ID 回 `user_message.ack rejected + SESSION_NOT_FOUND`，不得创建内存/持久化会话或增加附件 refs；缺省 ID 仍解析当前 active；文本、附件、voice.commit 均有正负测试 |
| ~~高~~ ✅ | **固定并可验证客户端输入音频契约** | 服务端实际把所有 v2 BINARY 当作 `16kHz/mono/pcm_s16le`；VAD 每 20ms=640 bytes 处理，小于一帧视为非语音，非整帧尾部被忽略。公开协议只写“PCM”，start 帧也不带 descriptor；Electron 很容易直接发 48k Float32 或 WebM/Opus，表现为静默无字幕/乱码。见 `core/input/audio_buffer.py:1/30/175`、`core/input/voice_sessions.py` | 在协议中固定输入格式，或给三种 start 帧增加 `audio{codec,sampleRate,channels}` 并由服务端校验；不支持格式/非法 chunk 返回稳定 `CONTRACT_VALIDATION`，不能静默吞；明确 stop 尾帧策略、最大 chunk/背压；增加真实 PCM fixture 覆盖 recording/realtime/dictation 与错误格式。**注（2026-08-09）**：descriptor 去掉 `chunkDurationMs`（固定 20ms 帧，客户端无需声明）；段边界整帧对齐扩到所有模式（含 REALTIME/DICTATION）；新增 `MAX_CHUNK_BYTES`（1 MiB）校验。**注（2026-08-10，S3/S6）**：段/WAV 边界**不再 640 整帧截断**（S3——截断丢真实尾音，整段含尾部原样送 STT，VAD 检测仍按 640B 整帧扫描，`<640B` 视为空转写）；超限 chunk 在 TRANSCRIBING/PENDING（录音已 stop、在途 STT/待 commit）窗口只丢弃不发 error（S6——error 帧只在真正终止的分支发） |
| ~~高~~ ✅（后端侧产物，跨仓 replay defer） | **发布 Electron 可消费的版本化机器契约并做跨仓 replay CI** | `frontend_contract` 是后端单源，但生成 TS 只落旧 Vue 目录；Electron 仓库手写领域 Zod，未持有 raw schema。归档 batch-7 traces 已过时（例如 `by_model`，当前为 `byModel`），双方各自测试全绿仍无法发现 wire 漂移 | 从 `frontend_contract` 生成并发布 JSON Schema/独立 TS raw contract 或可同步 artifact；产物带协议版本、后端 SHA/schema hash；生成当前 handshake/session/chat/tool/voice/cancel/fail 七类 golden frames；Electron CI 用同一 frames 做 raw schema + mapper replay；后端 CI 检查 artifact/trace 漂移，禁止归档 trace 重新成为事实源。**注（2026-08-09，D2）**：本批做**后端侧**——`contract.schema.json` + `contract.raw.ts`（transportVersion "2"/protocolVersion "2.0"/schemaHash）+ `scripts/gen_contract_frames.py` 契约源 golden frames + `--check` 漂移门禁 + `capture_ws_trace.py` 停用；**2026-08-10** 又将 cancel/fail 更新为当前契约源生成并保留旧 batch-2 快照；**跨仓 replay（改 elysia-frontend 消费）defer** 后续专项 |
| ~~中~~ ✅ | **Dictation 使用会话内 STT profile，不修改全局活动 STT** | `dictation.start` 只有 `clientRequestId`，实际使用全局活动 STT；Electron 的 `sttProfileId` 只能先 `switch_model`，会同时影响聊天/实时语音并与其他客户端竞争。见 `frontend_contract/ws_message.py:783`、`core/input/ws_handler.py:1021` | 给 `dictation.start` 增加可选 `sttProfileId`（或稳定 model/profile 标识）；start 时解析并快照到当前 dictation session，不修改全局 `agent.stt`；profile 不存在/不可用返回稳定错误；覆盖并发连接、途中全局切换不影响已开始 dictation、stop/cancel 资源释放 |
| ~~中~~ ✅ | **所有 Client→Server WS JSON 统一走 Pydantic 判别联合校验** | `handle_json_message()` 仍大量使用 `data.get()` 手工解析，并未对 `V2TextMessage/ToolStatusMessage/Voice*` 等执行统一 `model_validate`。错误类型可能被静默忽略，甚至在幂等 dict 等位置以 `TypeError` 断开单连接；顶层目前只捕获 `JSONDecodeError/ValidationError`。见 `core/input/ws_handler.py:298/757` | 按协议版本对完整 ClientMessage 判别联合做一次入口校验，再把模型交给 handler；拒绝 unknown type、错误枚举、错误字段类型、缺必填字段，统一返回 `ErrorMessage/ACK rejected CONTRACT_VALIDATION`；handler 不再依赖宽松 `data.get()` 默认值；补 fuzz/表驱动负面测试并保证畸形消息不关闭连接。**注（2026-08-09）**：`ClientMessage` 判别联合**不含 `TextMessage`**（type="text" 撞 discriminator）；非 text 走判别联合；顺带修 `:304` 单参 `ValidationError` 非 dict 帧断连 bug。**注（2026-08-10，P1-3）**：v2 text **完整帧 `V2TextMessage.model_validate`**（ValidationError → ACK rejected CONTRACT_VALIDATION，保留 ACK 通道；`attachments` 类型错/`clientRequestId` 非 str 等畸形字段不再抛 TypeError 断连）；`handle_ws` TEXT 加 `except Exception` 兜底 INTERNAL 不关闭连接 |
| ~~中~~ ✅ | **协议版本协商显式拒绝不兼容版本** | 握手头只有精确字符串 `"2"` 才走 v2；`"2.0"`、未来版本或拼写错误会静默降级 v1，而下发 `server_info.protocol_version` 又是 `"2.0"`，命名容易误用。见 `core/input/ws_handler.py:129`、`frontend_contract/ws_message.py:281` | 明确区分 transport negotiation token 与展示版本；对显式但不支持的版本在升级前返回 4xx/WS close code + 稳定错误，只有缺省头才兼容 v1；协议文档给出支持集合；覆盖 `2`、`2.0`、未知版本、缺省头与 Electron platform 组合测试。**注（2026-08-09）**：支持集合 {缺省/`"1"`→v1, `"2"`→v2}（显式 `"1"` 保留合法 v1，防现网回归）；`"2.0"`/`"3"`/乱写 → 升级前 400 ProtocolError |
| ~~中（部署门禁）~~ ✅ | **非回环生产绑定改为可配置 fail-closed 鉴权** | **2026-08-10 D6a 完成（已拍板）**：远程绑定仅认业务 token（`AGENT_ACCESS_TOKEN`），仅配 `SERVER_AUTH_TOKEN` 拒绝启动；默认 host 改回环 `127.0.0.1`；远程无完整鉴权 fail-closed（`assert_secure_bind`，`core/input/server.py`）。原问题见：默认可绑定 `0.0.0.0`，未设 token 时仅日志警告、产品 `/ws`/`/api/*`/`/uploads/*` 匿名可访问 | 增加显式开发模式或 `ALLOW_INSECURE_REMOTE`；非回环且无完整鉴权时默认拒绝启动，只有显式 opt-in 才允许；生产文档要求 HTTPS/WSS、双 token、附件签名；覆盖回环开发、非回环拒绝、显式 insecure opt-in、完整鉴权四种启动测试 |
| ~~低→产品化前中~~ ✅ | **幂等键跨进程持久化或明确限定重试窗口** | **2026-08-11 D6c 完成 + 加固批次扩展**：claim 持久化到 Mongo（`(conversation_id, client_request_id)` 唯一约束 + `expires_at` BSON Date TTL 1h）。产品模式（非回环绑定）+ Mongo 离线 → fail-closed `STORAGE_UNAVAILABLE`（已拍板）；本地 dev → 内存降级，`server_info.features.idempotency_durable` 暴露非 durable 窗口。**加固批次（2026-08-11）**：claim 三态 `CLAIMED/ACCEPTED/REJECTED` + 所有权（`ClaimResult.acquired`，并发 loser 恒不处理）+ complete 前置 + CLAIMED 残留恢复（新鲜→`IN_PROGRESS`、过期+generation 证据→重放 accepted、过期无证据→接管）+ dev 内存权威 overlay/写穿/reconcile + 生命周期接线（健康探测/恢复/关闭、durable 初值 `repo.is_connected`）。见 `core/storage/idempotency_repo.py`、`core/input/idempotency.py`。**BE-RISK-01 残余窗口未封口**（见 2026-08-12 复核 blockquote） | 覆盖 ACK 丢失、同键同内容、同键异内容、重启后重试、过期回收、claim 后 kill、Mongo 启停三序列 |
| ~~中（产品化门禁）~~ ✅ | **为公开 WS/REST/上传入口实现实际请求限流** | **2026-08-11 D6b 完成 + 加固批次扩展**：单进程 token-bucket 限流（已拍板，per-process 语义文档化，多实例需自建共享后端）。REST 高成本入口 429 `RATE_LIMITED` + Retry-After；WS 建连/消息 `RATE_LIMITED` + retryHint（连接存活）。key 认证主体优先、匿名回退 IP，默认不信任 `X-Forwarded-For`；内存有界 + 旧 key 清理。**加固批次（2026-08-11）**：text/voice.commit 超限走 rejected ACK（`clientRequestId` 关联 + `retryAfterMs`，立即 settle）；命令类 ErrorMessage 加可选 clientRequestId + 顶层 retryAfterMs；WS 建连 429 补 Retry-After；限流 key 仅认证成功按 `auth:<sha>` 分桶，缺失/无效 Authorization 统一 IP。见 `core/input/rate_limiter.py` | 覆盖 WS 建连与消息、REST、上传、并发突发、窗口恢复、代理头伪造、假 Token 轮换不挤占合法主体桶测试 |

### 不应登记成后端缺陷的前端适配项

- `generation.segment.completed.content` 是后端权威完整文本；inner voice 和 TTS 场景允许零 delta。应由 Electron Adapter/Accumulator 消费，后端不应为兼容错误前端而伪造 delta。
- `generation.completed.conversationUsage` 的真实定义是 `{total, byModel}`；Electron 不应虚构会话累计 `input/output`。若产品以后需要累计输入/输出拆分，应另立需求扩展 tracker 与契约。
- raw wire 使用 `type/delta/mood/generationId`，Electron 领域层使用 `event/text/emotion/responseId`，属于 Main Adapter 的正常改名职责。
- Desktop bridge 的 `tool.request/tool.status` 安全元数据、状态机和重连交叠精确投递已实现并通过专项测试；当前缺口主要是 Electron `ToolResult` 领域接口过窄。

### 建议批次顺序

1. ✅ **Batch 9A（文本/历史联调阻断）**：session conversationId、历史 cursor（严格区分）、未知会话拒绝——**2026-08-09 完成**（9e96657/931c35b/2711c90）；
2. ✅ **Batch 9B（契约工程化）**：机器契约（后端侧产物 + frames 归档，跨仓 replay defer）、统一入站校验、严格版本协商——**2026-08-09 完成**（0fac4da/d2f8178/ca74f0f）；
3. ✅ **Batch 9C（语音联调）**：输入音频 descriptor/验证、dictation profile 隔离——**2026-08-09 完成**（778c859/d58493d）；
4. ✅ **接入充分性复审收口**：Desktop 精确连接投递、当前 cancel/fail golden、公开文档同步——**2026-08-10 完成**；
5. ✅ **产品化门禁**：远程鉴权 fail-closed（D6a，2026-08-10）、实际请求限流（D6b，2026-08-11）、持久化幂等（D6c，2026-08-11）——**已全部完成**；
6. ✅ **Electron 首次真实联调后端复核整改（2026-08-11 加固批次）**：P0×2 + P1×5 + P2×3 闭环——ACK 判别联合 + 负面 golden、claim 三态/所有权/恢复、限流关联 + 认证主体分桶、clean-start 会话、探针分层、文档同步 + 联调 runbook（见上文 blockquote + hardening spec）——**已全部完成**。


## 全项目修复计划剩余项（2026-08-10）

> 源：`docs/review/full-project-review-2026-08-10.md` + 执行计划 `docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md`。
> 已执行 **23/41 Task**（全部 P0 + 全部 P1 + 约一半 P2），execution report 见 `docs/review/remediation-execution-report-2026-08-10.md`（含 review→fix 映射）。以下为**未做项**与**部分完成项**，记录以便后续继续。
>
> 每项执行时遵循计划纪律：先加失败测试 → 最小修复 → 回归 → 独立提交；完成后再把对应 plan 复选框改 `[x]` 并追加执行记录。

### A. 需要产品决策/外部依赖（不宜擅自拍板）

| 优先级 | 后端待办 | 当前行为与风险 | 完成标准（DoD） | 决策点 |
|--------|----------|----------------|----------------|--------|
| ~~中（产品化门禁）~~ ✅ | **跨重启持久化幂等（D6c）** | **2026-08-11 完成（已拍板：产品 fail-closed + dev 内存降级）** | 持久化 `(conversation_id, client_request_id, content_digest, message_id, status, expires_at)` + 唯一索引；同键同内容重试返回原 ACK、同键异内容拒绝；`expires_at` 用 BSON Date 真 TTL；Mongo 不可用产品模式 fail-closed/503、dev 内存降级 + `server_info.features.idempotency_durable` 暴露 | commit 本批次（D6b `75e7aee` 后的 D6c 提交）：`core/storage/idempotency_repo.py` + `core/input/idempotency.py` + ws_handler 接线 + contract 字段 |
| ~~中（产品化门禁）~~ ✅ | **公开入口实际请求限流（D6b）** | **2026-08-11 完成（已拍板：单进程 token-bucket，per-process 语义文档化）** | 可配置 token-bucket；key 优先认证主体、匿名回退 IP（默认不信任 X-Forwarded-For）；REST 429 / WS `RATE_LIMITED` + retry hint；limiter 内存有界、旧 key 清理 | commit `75e7aee`：`core/input/rate_limiter.py` + 中间件/WS 接线 + config |
| ~~中~~ ✅ | **Workflow submit 立即返回且有界排队（E2，P2-07）** | **2026-08-11 完成（已拍板：方案 A——立即返回 + 后台有界 QUEUED）** | submit 先注册 mailbox 立即返回；acquire 移到后台 runner；显式 QUEUED 状态 + 队列上限（max_queue_size=5）；PAUSED 释放 slot；resume 槽满入队（resume_pending 幂等）；队列满 QUEUE_FULL 快速失败 | commit 本批次（E2）：`submit`/`_drain_queue`/`resume`/`stop`/`cancel` 改造 + 契约 `QUEUED`/`QUEUE_FULL` + debug snapshot slots/queued |

### B. Workflow 独立子系统（工作量大，非 P1）

| 优先级 | 后端待办 | 当前行为与风险 | 完成标准（DoD） |
|--------|----------|----------------|----------------|
| ~~中~~ ✅ | **Workflow cancel/pause 终态单调性（E3，P2-08 + M19/M20）** | **2026-08-11 完成（已拍板：强取消——取消信号 = 任务状态，`WorkflowState` 不引入独立 cancelled 字段）** | cancel() 同步置 status=CANCELLED（权威终态）+ owned.cancel() 打断 in-flight LLM/planner；executor 四 `_check_cancelled` 检查点 + 工具 asyncio.shield（已开始工具完成、副作用不打断、结果弃用）+ `AgentInfra.orphan_tools` 共享孤儿容器 + stop() 采集（等 3s 超时 cancel）；planning/_init_exec 写 RUNNING 前终态检查（终态不可逆）；CancelledError 传播链统一收尾（except 不 release、finally 唯一释放点）；terminal event 只推一次（`_terminal_pushed` 守卫）；M19 resume→pause 不销毁 task/mailbox | commit 本批次（E3）：`bb69485`（_check_cancelled+终态守卫）→ `febad1a`（强取消）→ `e137e6b`（executor 签名穿透+shield）→ `4991a85`（终态不可逆）→ `0839bdf`（CANCELLED 分支+唯一 release）→ `4fbffc7`（M19+stop 孤儿采集）→ `5aa7368`（fake-LLM 确定性套件） |
| ~~中~~ ✅ | **Workflow 测试确定性与外部 E2E 分层（E4，P2-11 部分）** | **2026-08-11 完成（已拍板：`RUN_EXTERNAL_TESTS=1` 显式门禁 + fake-LLM 确定性基建）** | 新增 `external` marker（真实 LLM 必须同时标 `integration+external`）；cancel/pause/mailbox 首事件用 fake LLM 确定性测试；`make test-workflow-engine` 覆盖核心 unit/service；`make test-external` 单独跑；修 `coroutine was never awaited` warning | commit 本批次（E4）：`tests/helpers/fake_llm.py`（FakeLLM 对齐 `chat.completions.create` 访问面 tool_calls/content/usage + gate/calls/prompts）+ `with_deadline` 挂死防线 + `CountingLLM` 成本计数；`Module/conftest.py` external skip hook（未设 `RUN_EXTERNAL_TESTS=1` 全部 skip）；`make test-external` 显式带 env；两真实 E2E 文件补 `integration` marker；mailbox 首事件 QUEUED 确定性测试 |
| ~~中~~ ✅ | **工具热重载原子回滚（E1，P2-06）** | **2026-08-11 完成（已拍板：锁外构造/setup → 持锁原子 swap → 锁外排空，失败保留旧实例）** | reload 后从新 module 重新 discover class；共用统一 config 流程（`_load_safety_config`）；先构造/setup 新实例、成功后原子替换四注册表 dict、等旧实例 in-flight 排空再 teardown；setup 失败保留旧工具；`_reload_lock` 仅覆盖 swap 微秒窗口；`_inflight` 按实例快照排空（超时 10s 后台延迟 teardown）；`_teardown_tasks` 独立登记不被 cancel_all 取消、shutdown 显式 gather 收尾；调用点 `/tools/reload` 解包 `tuple[bool, str]` | commit 本批次（E1）：`bffef51`（reload 测试 fixture）→ `df19275`（共用 config helper）→ `ccafe7a`（锁 + in-flight/teardown 注册表）→ `abff339`（原子替换 + 回滚 + 新 class discover）→ `f4b871b`（排空 + 超时延迟 teardown）→ `d98fb86`（execute/execute_stream 锁 + in-flight 计数）→ `02bcc9b`（shutdown 持锁 + 收尾）→ `d72b90f`（server tuple 适配）→ `2811c12`（全表行为测试） |

### C. 工程化/文档（F 批次，正式 release candidate 前置）

| 优先级 | 后端待办 | 当前行为与风险 | 完成标准（DoD） |
|--------|----------|----------------|----------------|
| ~~中~~ ✅ | **真正全量离线 CI（F1）** | `test-all` 名不副实（漏若干 `core/*/tests`、`frontend_contract/tests`、`scripts/tests`）；workflow 测试不入合并门禁 | **2026-08-11 已完成**（`1a7cd94`）：external marker 取代 `--ignore`——WorkflowEngine unit/service 入合并门禁（test-quick 3324 / test-normal 3426）；`test-all` 真全仓非 external；`make test-external` + CI external job（workflow_dispatch 受信，self-hosted + 真实凭据）；pr-gate 去 ignore + ruff 含 scripts + coroutine warnings-as-errors |
| ~~中~~ ✅ | **依赖锁与安全扫描（F2，P2-12）** | `requirements.txt` 只有下限无 lock/hash；pytest/ruff/pyright 未完整声明；CI 复用宿主 conda env | **2026-08-11 已完成**（拍板：统一 conda）：`environment.yml`（runtime=requirements.txt + dev=requirements-dev.txt 单源）+ `environment.lock.yml` 精确锁（115 pip 包版本+hash）；`make lock-env` 重生成 / `make audit`（pip-audit + bandit）；CI pr-gate 锁漂移校验 + 安全扫描。**豁免**：PYSEC-2026-3447（setuptools 83 移除 pkg_resources，webrtcvad 依赖）、bandit 10 项已知良性（bandit.yaml 带理由）。**未来项**：迁移 uv（见下）；容器镜像/action digest 固定未做 |
| ~~中~~ ✅ | **Backend-only setup/start（F3，P2-13）** | `setup.sh`/`start.sh` 强制装 v1 前端（无 npm 就退出）；preflight 被 `\|\| true` 吞 | **2026-08-11 已完成**（`0df5753`）：backend-only 默认（无 Node 可安装/启动）；`--with-frontend`/`--legacy-ui` 显式构建 v1 UI（npm ci）；preflight（app.py --check）失败默认非零退出 + `--skip-preflight` 显式降级；shell smoke `scripts/tests/test_backend_only_smoke.sh`（11 断言） |
| 低 | **静态门禁扩展 + 关键入口拆分（F4，P3-01/P3-02）** | `scripts/` 未纳入 lint（17 个 ANN）；`core/monitoring/__init__.py` 无效 `IdleMonitor` export 致 pyright 1 warning；`api_routes/ws_handler` 复杂度高 | **部分完成（2026-08-11，`602c7ea`）**：scripts 纳入 Ruff/Pyright（17 ANN 清零）+ monitoring warning 清零 + coroutine 泄漏 warnings-as-errors。**剩余**：`register_api_routes`/`handle_json_message` 复杂度拆分 → **2026-08-14 已启动 input 层拆分批次 B/C/D**（spec 见 `docs/superpowers/specs/2026-08-14-input-split-batch-*`，行为锁定先行） |
| 低 | **文档/测试统计/Electron 交接（F5，P3-03）** | 活跃文档手写测试数 3186 vs 实际 3290+；错误码目录 40/41 不一致；contract 产物未发版 | **部分完成（2026-08-11，F5 part 1）**：`print_test_stats.py` 统计自动生成（3503）+ 错误码单源（`frontend_contract/errors.py` + `check_error_codes.py`，41 码一致）+ contract 发版记录 + Electron 变更清单。**剩余**：存储/GC/删除/TTL/restart 等运维说明更新 |
| 低 | **Desktop golden 从 runtime 单源（E6，P2-14）** | `gen_contract_frames.py` 生成的 desktop read 参数含 `offset/limit`，真实 runtime 只支持 `path`；`--check` 只查生成器自洽 | desktop 参数 schema/risk/hash 抽成无副作用单源，runtime 与 generator 共同消费；`--check` 比对 runtime 语义等价 |

### D. 部分完成项（不影响对应 review 项主结论，后续补齐）

| Review 项 | 已做 | 剩余 |
|--------|------|------|
| P2-01 | TTL 真实生效（BSON `ttl_expires_at`）+ reflector 持久化接线 | session 列表 keyset 分页下推（repo.list_all 仍预取 200，第 201+ 条不可分页/搜索）；显式 session 过期级联清理 worker |
| P2-02 | 搜索 query 长度/字面化、context before/after 钳制、/api/chat 队列满 429 | contract 层 content/ID/attachments 业务长度上限（附件最多 10 个）+ 附件 bounded batch 查询（一帧数千附件仍串行查 DB） |
| P2-09 | checkpoint 空则删 + 不可序列化跳过 + 版本隔离；time_of_day 未知 reason 告警 | EventBus lifecycle epoch（旧 epoch 事件潜在重放；stop 已 cancel event task，风险低） |



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

### 2026-08-14 复审发现（core 模块化，对照 08-01 基线）

文件级 SCC 全仓扫描：**core/ 内部文件级真实 eager 硬环 = 0 个**（adapter↔memory、schema↔utils、_common↔config 均为包级聚合伪环，文件级为 DAG，运行时 import 顺序实测全通）。确凿问题集中在 Module 层边界 + 近期功能提交引入的包级反向边。审查详见会话记录。

| 优先级 | 位置 | 说明 |
|--------|------|------|
| ~~中~~ ✅ | [Module/llm/registry.py:67](Module/llm/registry.py#L67) | lazy `from core.input.ssrf import validate_endpoint`（commit `963d1a8b`，2026-08-06 批次 8a）——**全仓唯一 Module→core 生产反向依赖**，晚于 08-02 分离基线。**已修（2026-08-14）**：`core/input/ssrf.py` 下沉 `Module/_common/ssrf.py`（零 core 依赖纯移动 + 测试随实现 + 4 处 mock 路径），Module 生产反向依赖清零 |
| ~~中~~ ✅ | [core/storage/idempotency_repo.py:62](core/storage/idempotency_repo.py#L62) | 函数内 lazy `from core.input.idempotency import GRACE_SECONDS`（注释自陈绕环）——storage 层反向依赖 input 层。**已修（2026-08-14）**：`GRACE_SECONDS` 单源下沉 `idempotency_repo.py` 自身，input 经门面反向 import（恢复 input→storage 方向） |
| ~~低~~ ✅ | [core/schema/adapters/message_adapter.py:21](core/schema/adapters/message_adapter.py#L21) | `from core.utils.message_id import stable_tool_message_id`（2026-08-13 消息 ID 批次 D2 引入）——闭合 schema↔utils 包级环，打破「core.schema 是叶子」文档断言。**已修（2026-08-14）**：ID 生成器迁 `core/schema/utils.py`（返回类型全为 NewType 属类型权威附属），`core/utils/message_id.py` 保留薄重导出 shim，`message_adapter` 改正典导入；`types.py` 对 utils 的 `_normalize_iso_ts` 改 lazy wrapper 打破包内环。schema 恢复叶子、零 core.utils 依赖 |
| ~~低~~ ✅ | [core/input/session_routes.py:190-193](core/input/session_routes.py#L190) | `getattr(agent,"cognitive")` + `cognitive._generation_registry` 绕开公开 property。**已修（2026-08-14）**：改走 `agent.generation_registry` 公开 property（与 `cognitive._generation_registry` 同一对象，agent.py:897） |
| ~~低~~ ✅ | [core/input/ws_handler.py:224,452](core/input/ws_handler.py#L224) | 输入层直调组合根私有 `agent._sync_desktop_available()`。**已修（2026-08-14）**：公开化为 `agent.sync_desktop_available()`，ws_handler 走公开接口 + noqa 清除 |
| ~~低~~ ✅ | [core/input/tts_profile.py:51,54](core/input/tts_profile.py#L51) | `backend._resolve_voice`/`backend._voice_id` 穿透 TTSBackend 私有。**已修（2026-08-14）**：`TTSBackend.default_voice()` 公开方法（默认返回 character），MiniMax override 复用 `_resolve_voice`，tts_profile 走公开接口 |
| 低 | [core/desktop/bridge.py:54](core/desktop/bridge.py#L54) | 顶层 `from core.storage.desktop_call_repo import _ACTIVE_STATES` 私有符号，建议改名公开 `ACTIVE_STATES` |
| ~~低~~ ✅ | input 层内聚拆分 | 原问题：`ws_handler` 1825 行 + `api_routes` 1182 行 = input 层 35%；`server↔api_routes`/`server↔debug_routes` 懒导入绕环。**2026-08-14 批次 ABCD 全部完成**：A（errors.py 下沉解绕环）、B（ws_handler 拆分：深业务下移 ws_v2.py + `_WS_HANDLERS` 注册表分发，1825→1166 行）、C（api_routes 拆 5 域 provider/tts/upload/tools/chat，退役删除）、D（server.py 纯装配：middleware.py/health.py 下沉，759→493 行）。懒导入绕环全部消除 |
| 低 | [core/adapter/prompt_assembler.py:30-31](core/adapter/prompt_assembler.py#L30) + [core/memory/consolidator.py:25](core/memory/consolidator.py#L25) | 包级伪环（文件级 DAG），两条边均为类型注解 + 构造注入；非近期引入。可选打磨：惰性化注解（TYPE_CHECKING）清除包级告警 |

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
| ~~**中**~~ ✅ | ~~**M19 resume 后再次 pause 被销毁**~~ | ~~[workflow_engine.py:384-393](Module/workflow_engine/workflow_engine.py#L384)。缺 PAUSED early-return 保护，mailbox 消费者永久阻塞~~ |
| ~~**中**~~ ✅ | ~~**M20 cancel 在规划阶段不推终态事件**~~ | ~~[lifecycle.py:157-167](Module/workflow_engine/lifecycle.py#L157) + [planning.py:143-145](Module/workflow_engine/core/sub_agent/planning.py#L143)。`cancel()` 尚无调用方接线，随任务系统接入时修复~~ |

> M19/M20 **已随 E3 完成**（2026-08-11）：M19 由 E2 PAUSED early-return 修复、E3 Task 6 确定性验证（resume→pause 循环）；M20 由 E3 强取消修复——cancel() 同步置 CANCELLED + CancelledError 传播链推唯一 CANCELLED 终态事件（不再缺终态事件）。

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
| ✅ | 单 Session 多客户端共享 | 批次 3 多会话已实现：`SessionManager` 会话缓存池 + `/api/sessions` CRUD/activate；`"default"` 仅作缺省活动会话，非硬编码 |
| ✅ | 会话管理 | `/api/sessions` 提供新建/切换/列出/历史/搜索/删除（`session_routes.py`）；WS `activate_session` 切 active + 重发回放 |
| **高** | **无数据库迁移机制** | MongoDB 索引在 `connect()` 时 ad-hoc 创建，Milvus collection 硬编码创建。若字段/索引/维度需变更，无 upgrade path。`memory_repo.clear_all()` 是破坏性操作无安全守卫 |
| 低 | **`character/cyrene/` 不可用** | 缺少 `config.yaml` 和 `personality.md`，仅含 GPT 参考材料，无法作为角色加载 |
| 低 | **依赖声明宽范围 vs 实际精确锁** | `requirements*.txt` 声明文件仍允许宽范围（pydantic/openai/torch 等无上限），但实际部署边界已由 `environment.lock.yml` 收敛（conda 30 + pip 133，全部 `==` 精确锁定，未启用逐包 hash；`make lock-env` 重生成 / `make audit` 漏洞扫描）。风险措辞：声明文件宽范围 + 锁文件精确边界，两者并存；上游 breaking change 需 `make lock-env` 重锁后验证 |
| 低 | **未来项：依赖管理迁移 uv** | **2026-08-11 记录（F2 拍板）**：当前统一 conda（环境锁 + 可复现），暂不迁移。后续熟悉 uv 后评估迁移（uv 锁文件更轻、安装更快、CI 更干净）；迁移前提：uv lock 覆盖全部 runtime+dev 依赖 + CI 切 uv clean-room + 对比 conda 环境差异，迁移后再删 conda 锁文件 |
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed/2026-07.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |

## 多前端支持（2026-08-01 评估，场景 A：新前端连运行中的服务端）

**评估结论：不加中间层（YAGNI）。** `core/input/` 已是传输适配层（WS `/ws` + HTTP `/api/*` + Debug `/debug/api/*`），Agent 公开接口已是服务层（ws_handler 全部走公开接口），WS 协议 UI 无关（20+ 语义事件，非 DOM 操作）——Qt 等新前端可直接连同一 `/ws`。CLI 走进程内驱动（`run_repl` 不经过网络层，两套 driver 是已接受的现状）。若做 Qt 前端，需先补齐协议契约成熟度：

| 优先级 | 事项 | 说明 |
|--------|------|------|
| ✅ | **协议契约语言中立化** | `frontend_contract` 可执行 Pydantic 模型 → 生成 `contract.schema.json`（JSON Schema）+ `contract.raw.ts`（raw wire 快照）+ `contract.generated.ts`；零依赖，语言中立，`make check-types` 漂移门禁（CI 强制） |
| ✅ | **协议版本化** | 协议 v2：`frontend_contract/version.py` 单源 `TRANSPORT_VERSION="2"` / `PROTOCOL_VERSION="2.0"`，握手头携带；服务端升级不静默破坏旧客户端 |
| ✅ | **多 session / 身份** | 批次 3 多会话 + v2 显式 `conversationId`（`resolve_session` 校验存在，禁止懒创建幽灵会话） |
| ✅ | **公开契约文档** | `docs/electron-backend-protocol-v2.md`（WS 消息 + REST + 错误码 + 错误信封）+ `docs/archive/frontend-handoff-v2/traces/`（契约源 golden frames） |

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
