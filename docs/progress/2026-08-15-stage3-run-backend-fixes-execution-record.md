# 阶段 3 真实联调后端复验修复执行记录（2026-08-15）

## 批次状态

| 项 | 值 |
|---|---|
| 上游需求 | `docs/review/2026-08-15-阶段3真实联调后端复验问题清单.md`（BE-S3-RUN-P0-01/P0-02） |
| spec | `docs/superpowers/specs/2026-08-15-stage3-run-backend-fixes-design.md`（commit `1fd31717`） |
| plan | `docs/superpowers/plans/2026-08-15-stage3-run-backend-fixes.md`（commit `26d73b72`） |
| 执行 | subagent（general-purpose）并行执行 Task 1+4 → Task 2+3 → Task 5（文档）主审查完成 |
| feature commits | `ce6f0092`（P0-02 写路径 + 规范化单源）、`874be41b`（P0-02 读路径 + 存量兼容 + warning）、`59bd60fc`（P0-02 下载 Content-Type）、`f4200762`（P0-01 会话换绑） |
| 收尾 commits | `e16af728`（注释去 audio/x-wav 字面量）、`dc1077f8`（协议文档 §13.5 + spec §7.5 验收口径） |
| 门禁 | test-quick **3599 passed** / test-normal **3707 passed**（基线 3581/3689 + 18 新增）/ ruff All passed / pyright **0 errors** / check-types 全绿 |

## 决策点定案（D0-D6）

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批次组织 | **A 合并单批次** | 两项同挡 stage-3 联调门、共处一份复验清单；一次 9 阶段流程成本更低 |
| D1 换绑同步范围 | **A 只 `session` + `_session_msg_count`** | 每会话派生值就两个（复验清单 §3.3.4「至少」）；`_fsm_state` 启动快照同样 stale 但 Agent/turn 级，记遗留不扩 scope |
| D2 换绑机制/位置 | **A `process()` 内联写 dict** | 贴合既有 `tool_context.trace_id` 内联先例；复验清单 §3.3.5 要求 cognitive loop 统一 per-turn 边界 |
| D3 规范化函数归属 | **B `core/schema/utils.py`** | 共享纯函数之家（`_validate_src`/`now_iso` 同处），写路径 + 两读路径可无环导入 |
| D4 未知 mime 行为 | **A 结构化回退 + `logger.warning`** | 新记录都规范化过，读路径未知 mime 只可能是存量脏数据；warning 满足「可诊断」、回退满足「不炸消息页」 |
| D5 存量兼容方式 | **A 纯读路径规范化** | `_attachment_part` + 下载 Content-Type 过同一函数幂等恢复；不写迁移脚本 |
| D6 文档同步 | 更新 `electron-backend-protocol-v2.md` §13.5 | 冻结表不再出现 `audio/x-wav`，补 `audio/webm` |

## Review 记录

- **spec**：code-review 1 轮（general-purpose agent）结论 **yes（附条件）**——事实基线/消费点枚举/规范化表不漏全部核对通过，无 HIGH。2 MED + 6 LOW 全消化：**MED-1** §7.5 grep 验收字面不可满足（探测表 + 别名表必然保留 `audio/x-wav`）→ 改写为「文档无字面量 + 生产非测试仅两处白名单」；**MED-2** §4.3 case 3「上传即时与 replay 一致」测试无落点 → §6 `test_attachment_message.py` 补新记录 replay 一致用例；LOW×6（基建引用/克隆推理/off-by-one/行号/插入位置/回滚归属）。
- **plan**：code-review 1 轮结论 **no**（测试代码 4 HIGH + 1 MED + 6 LOW）——生产修复设计全部验证正确（含端到端复现换绑机制有效），但 Task 4 测试文件整体不可跑：**HIGH-1** `_build_loop` 签名缺 `vision` 位置实参 → 函数内构造；**HIGH-2** `_ToolCallingLLM` 用 `_model` 而 loop 访问 `self._llm.model` → 改公开；**HIGH-3** test 3 `_session_msg_count == len(messages)` 与 spec 记录的 off-by-one 矛盾 → 断言快照值 0；**HIGH-4** test 6 元组解包反转 + 缺 `loop._session` + `.model` 缺失 → 三修；**MED-1** `test_legacy_wav_x_wav_normalized` 非红测 → Task 1 补 `TestNormalizeAttachmentMime` 直测表；LOW×6（计数/async generator/失败推理/测试片段补全/grep 锚定/jsonl 污染标注）。
- **最终 commit review**（Phase 8）与 **验收**（Phase 10）：见文末（待回填）。

## 执行偏差（已解决，非 spec 语义变更）

- **Task 4 测试基建 bug（agent 发现，非生产 bug）**：`_ToolCallingLLM.calls` 是实例级计数器，跨 `process()` turn 共享——turn A 耗掉「首轮发 tool_call」后，turn B 变纯文本、describe_image 不再执行。按 plan 意图在 test 2/test 4 第二轮前补 `loop._llm = _ToolCallingLLM()` 重置（test 5 本就用了此模式），使 A/B 切换与「不残留」真正触发工具执行。生产代码仅含计划的换绑块，未动。
- **`audio/x-wav` 字面量收敛（执行中发现）**：规范化实现后，2 个生产注释（`ws_v2.py`/`attachment_routes.py`）与协议文档 MIME 规范化说明仍含字面量，使 spec §7.5 grep 门禁失效。收敛：注释/文档改「WAV 探测别名」，spec §7.5 口径修订为「生产非测试仅两处白名单文件 + 测试文件可含（断言规范化）」。不改变任何行为。
- **subagent 可靠**：4 个执行 agent（Task 1/2/3/4）全部按时返回（2-6 分钟），未卡住；spec/plan 两轮 review agent 均按时返回。
- **门禁范围**：执行 agent 各自跑定向测试 + 文件级回归；主审查跑全量 test-quick（3599）+ test-normal（3707）+ ruff + pyright + check-types。

## 验收结论（Phase 10，subagent 独立验收）

（待回填）

## 后续衔接

- 本批修复 + 真实 Electron 回归通过后，后端阶段 3 才可标记「已真实联调」——真实回归脚本（复验清单 §6 完成定义）：PNG 视觉（新建非默认会话发图 → describe_image 读到）、WAV/WebM 首次发送与切会话/重载/重启后恢复类型一致，音频不再显示「不支持的文件」；
- 前端配套问题（签名相对 URL 解析等）见 `elysia-frontend` 前端复验清单（FE-S3-RUN-P0-01），不在本批后端范围。
