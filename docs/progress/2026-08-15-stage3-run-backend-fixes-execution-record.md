# 阶段 3 真实联调后端复验修复执行记录（2026-08-15）

## 批次状态

| 项 | 值 |
|---|---|
| 上游需求 | `docs/review/2026-08-15-阶段3真实联调后端复验问题清单.md`（BE-S3-RUN-P0-01/P0-02） |
| spec | `docs/superpowers/specs/2026-08-15-stage3-run-backend-fixes-design.md`（commit `80cad486`，**单一 spec commit**） |
| plan | `docs/superpowers/plans/2026-08-15-stage3-run-backend-fixes.md`（commit `04635d67`，**单一 plan commit**） |
| 执行 | subagent（general-purpose）并行执行 Task 1+4 → Task 2+3 → Task 5（文档）主审查完成 |
| feature commits | `df165e30`（P0-02 写路径 + 规范化单源）、`43d4c829`（P0-02 读路径 + 存量兼容 + warning）、`3cedfafb`（P0-02 下载 Content-Type）、`e5bd3a75`（P0-01 会话换绑） |
| 收尾 commits | `9bdd110d`（注释收敛）、`5b1da8ba`（协议文档 §13.5）、`fc8101df`（文档同步）、`b5bf4a25`（final review L5/L6 跟进） |
| 门禁 | test-quick **3600 passed** / test-normal **3708 passed**（基线 3581/3689 + 19 新增，含 final review L5 replay 参数化）/ ruff All passed / pyright **0 errors** / check-types 全绿 |

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
- **最终 commit review**（Phase 8，1 轮）：general-purpose agent 审 `ce6f0092^..e16af728`（原 5 代码 commit），结论 **yes 可验收**（无 high/medium）。六处 low 全为测试判别深度与文档/注释细节，大部分被 plan review 预记消化。两处顺手修：
  - **low-5** replay 一致性测试只覆盖 WAV——参数化 `test_new_record_replay_mime_consistent` 补 WebM audio（`[("voice.wav", WAV, "audio/wav"), ("voice.webm", WEBM, "audio/webm")]`），正对 spec §4.3 case 3。
  - **low-6** 协议文档 `Content-Type = 记录 mime` 对存量记录不精确——改「规范化后 mime（经 normalize_attachment_mime，存量 audio 别名兼容）」。
  - 其余 low（L1 行为锁非判别测由 TestNormalizeAttachmentMime + 下载红测锁定；L2 不残留测试红来自首轮；L3 harness 脆弱点注释说明；L4 取消路径由无条件 per-turn 换绑设计保证）接受不改码。
- **文档合并**（Phase 9）：rebase 收敛——spec 收为单 commit `80cad486`（§7.5 验收口径修订并入）、plan 保持单 commit `04635d67`；rebase 前后内容 diff 为空（零丢失）；origin/main 因此与本地分叉（未 push，见后续衔接）。

## 执行偏差（已解决，非 spec 语义变更）

- **Task 4 测试基建 bug（agent 发现，非生产 bug）**：`_ToolCallingLLM.calls` 是实例级计数器，跨 `process()` turn 共享——turn A 耗掉「首轮发 tool_call」后，turn B 变纯文本、describe_image 不再执行。按 plan 意图在 test 2/test 4 第二轮前补 `loop._llm = _ToolCallingLLM()` 重置（test 5 本就用了此模式），使 A/B 切换与「不残留」真正触发工具执行。生产代码仅含计划的换绑块，未动。
- **`audio/x-wav` 字面量收敛（执行中发现）**：规范化实现后，2 个生产注释（`ws_v2.py`/`attachment_routes.py`）与协议文档 MIME 规范化说明仍含字面量，使 spec §7.5 grep 门禁失效。收敛：注释/文档改「WAV 探测别名」，spec §7.5 口径修订为「生产非测试仅两处白名单文件 + 测试文件可含（断言规范化）」。不改变任何行为。
- **subagent 可靠**：4 个执行 agent（Task 1/2/3/4）全部按时返回（2-6 分钟），未卡住；spec/plan 两轮 review agent 均按时返回。
- **门禁范围**：执行 agent 各自跑定向测试 + 文件级回归；主审查跑全量 test-quick（3599）+ test-normal（3707）+ ruff + pyright + check-types。

## 验收结论（Phase 10，subagent 独立验收）

- **结论：本批可标记「已开发/已验证」（复验清单 §6 定义）；不得标记「已真实联调」**——真实 Electron 回归（修复后后端 SHA + 修复后前端 SHA）尚未执行，后端阶段 3 标记停留在「部分联调通过」级别。
- 6 个维度全通过：功能符合性（P0-01 换绑 + P0-02 三消费点 + 存量兼容 + 响亮 warning，20 定向测试全绿）、契约零漂移（`make check-types` 全绿 + TS 快照零改动）、四门禁（test-quick **3600** / test-normal **3708** / ruff / pyright 0 errors）、文档同步（CLAUDE.md/README/architecture/todo/执行记录/协议文档 §13.5）、决策点 D0-D6 可追溯、完成定义逐条核对。
- Phase 9 rebase 完整性：spec 单 commit `80cad486` / plan 单 commit `04635d67`，rebase 前后内容 diff 为空（零丢失）。
- 验收复跑计数较主审查 +1（3600/3708 vs 3599/3707）——差异来自 final review L5 replay 用例参数化（WAV 1 例 → WAV+WebM 2 例）在门禁记录后加入，已在本节修正。

## 后续衔接

- 本批修复 + 真实 Electron 回归通过后，后端阶段 3 才可标记「已真实联调」——真实回归脚本（复验清单 §6 完成定义）：PNG 视觉（新建非默认会话发图 → describe_image 读到）、WAV/WebM 首次发送与切会话/重载/重启后恢复类型一致，音频不再显示「不支持的文件」；
- 前端配套问题（签名相对 URL 解析等）见 `elysia-frontend` 前端复验清单（FE-S3-RUN-P0-01），不在本批后端范围。
