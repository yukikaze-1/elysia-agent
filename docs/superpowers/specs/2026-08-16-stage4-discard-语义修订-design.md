# 阶段 4 真实联调：`voice.discard` 取消原语语义修订（BE-S4-RUN-P0-01）

**文档版本：** v0.1
**文档状态：** 待评审
**日期：** 2026-08-16
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** `docs/review/2026-08-16-阶段4真实联调后端整改清单.md`（BE-S4-RUN-P0-01）
**后端基线：** `main@fc5b45eceb4d4c55de6920423e8bedb190ff6743`
**前端对照：** `/home/yomu/elysia-frontend` `main@37a78e319700ec31d9e2a5eb55d67d0728c0bc93`（配套清单 FE-S4-RUN-P0-01/P1-01）
**前置决策：** 会话讨论定案 D0-D5（全部按推荐定案）

---

## 1. 背景

阶段 4 的 Profile、TTS 和三种语音模式已在真实 Windows Electron + Ubuntu 后端完成主链联调。
当前不能标记"阶段 4 完整真实联调通过"，唯一阻断是 `voice.discard` 的后端状态机语义与前端既有
取消语义不一致（**不是实体麦克风或 SenseVoice 不可用**）。

- **真实现象 A（采集启动失败残留）**：`voice.record.start` 被后端接受后，前端 `PcmCapture`
  `getUserMedia`/`AudioContext`/采集图初始化失败 → 前端按 fire-and-forget 清除本地 active voice
  并发送 `voice.discard` → 后端因 RECORDING 态拒绝（`CONTRACT_VALIDATION: 录音中，先 stop 再
  discard`）→ 再次 `voice.record.start` 返回 `VOICE_SESSION_CONFLICT`。任何真实麦克风权限拒绝/
  设备拔出/采集图失败都走此路径。
- **真实现象 B（空结果迟到 discard 伪错误）**：空/过短/空转写结果后端直接 `_reset_recording()`
  回 IDLE 且不发 `voice.transcript`；前端保留待发送条，用户点删除仍发 `voice.discard` → 后端
  异步回 `VOICE_RECORDING_NOT_FOUND` → `onChatError` 收到 `backendCode=VOICE_RECORDING_NOT_FOUND`
  + real-backend 映射 `voice.error`。问题是无意义错误/日志噪声/竞态契约，不是前端卡死。

跨仓冻结目标：**`voice.discard` 是普通录音的取消原语，应能取消当前录音的任意未提交阶段，并允许
结束后的重复/迟到调用幂等成功。** 不新增 wire 消息类型，不把取消改成 `stop` 后触发一次无意义 STT。

## 2. 目标

- **P0-01**：`voice.discard{recordingId}` 在 RECORDING/TRANSCRIBING/PENDING 任一未提交阶段都能
  安全取消当前录音回 IDLE；IDLE/重复/迟到调用幂等成功（不产生错误）；错误 recordingId 与其它
  语音模式（realtime/dictation）活跃时明确拒绝且绝不误清其它会话。
- 取消成功后不得调用 STT、不得发 `voice.transcript`、不得触发 generation；在途 STT 任务取消且
  正确收尾，迟到结果不得复活 PENDING。
- 不新增/修改任何 `frontend_contract` 模型（wire schema 无新字段，纯状态机语义修订）。
- **golden 门禁零 churn**：`ws-v2-recording.jsonl` 只录后端→前端输出帧（`voice.transcript`），
  从未含 discard 错误帧——语义修订不影响 `scripts/gen_contract_frames.py --check`。
- 已通过联调链路（§4 清单）不得回归。

## 3. 现状（事实基线）

### 3.1 生产状态机 `core/input/voice_sessions.py`

| 行 | 方法 | 现状 |
|---|---|---|
| `:336-343` | `discard_recording()` | ① `_mode != RECORDING` 或 `_recording_id != recording_id` → `VOICE_RECORDING_NOT_FOUND`；② `_rec_state == RECORDING` → `CONTRACT_VALIDATION`「先 stop 再 discard」（**P0 阻断**）；③ 取消在途 `_in_flight_stt` + `_reset_recording()` |
| `:188-199` | `_reset_recording()` | 清 `_recording_id/_rec_state/_rec_pcm/_rec_bytes/_pending_*` + 取消 `_in_flight_stt` + `_mode = IDLE` |
| `:300-301` | `_run_recording_stt()` | `_rec_state != TRANSCRIBING → return`——discard 已取消的迟到 STT 结果不复活（**既有防线，保留**） |
| `:266-289` | `stop_recording()` | RECORDING→TRANSCRIBING；`len(rec_pcm) < _FRAME_BYTES(640)` → 空转写直接 `_reset_recording()` 回 IDLE 不发 transcript |
| `:302-304` | `_run_recording_stt()` | 空/纯空白 STT 结果 → `_reset_recording()` 回 IDLE（不发 transcript） |

**现状对冻结目标的差距**：仅「RECORDING 态（未 stop）discard 拒绝」和「IDLE 态 discard 报
NOT_FOUND」两项不符；TRANSCRIBING/PENDING 态 discard 已支持、其它模式隔离已成立（NOT_FOUND 不
拆会话）。**代码改动 = 仅重构 `discard_recording()` 一个方法**（约 15 行）。

### 3.2 WS 路由 `core/input/ws_handler.py`

| 行 | 现状 |
|---|---|
| `:788-797` | `_dispatch_voice_discard()`：`voice_mgr.discard_recording(...)`，`VoiceSessionError` → `_send_voice_error(ws, e)`。**无需改动**——语义全在 manager 方法内 |

### 3.3 现有测试

| 文件 | 现状 |
|---|---|
| `core/input/tests/test_voice_sessions.py:177-181` | `test_recording_state_boundaries`：TRANSCRIBING discard 允许后「二次 discard 报 NOT_FOUND」——**须改**为幂等成功 |
| `core/input/tests/test_voice_sessions.py` | 全文件 45 个测试，仅 `test_recording_state_boundaries` 触碰 discard；其余 44 个不触碰 |
| `core/input/tests/test_voice_ws.py` | 26 个测试（commit/chunk/descriptor 边界），**无任何 discard 路由测试** |
| `scripts/tests/test_stage4_smoke.py` | 只断言 exit 0 / failed 0 / scenarios 非空——smoke 检查数变化安全 |

### 3.4 文档

| 文档 | 行 | 现状 |
|---|---|---|
| `docs/electron-backend-protocol-v2.md` | `:211` | `VOICE_RECORDING_NOT_FOUND` 行「IDLE 态收 stop/commit/discard（未知 recordingId）」——discard 部分须移除 |
| `docs/electron-backend-protocol-v2.md` | `:390` | 失败信封区分——discard 违规 → `error{code}`（保留，但 discard 成功/幂等路径须注明） |
| `docs/electron-backend-protocol-v2.md` | `:402` | `voice.discard` 注释「（丢弃，清缓冲）」——补语义 |
| `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md` | `:74-89` | RECORDING 状态机图 + 边界规则：「RECORDING 态收 discard → CONTRACT_VALIDATION」「IDLE 态收 discard → NOT_FOUND」——须修订 |
| `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md` | `:121` | `voice.discard` 契约行「丢弃 pending/在途录音」——补任意未提交阶段 + 幂等 |
| `scripts/stage4_smoke.py` | `:264-279` | discard 段走旧工作区 `start→stop→discard`，注释引用 `voice_sessions.py:339-340` 旧行号与旧语义——须改写 |
| `docs/superpowers/plans/2026-08-15-stage4-联调前整改.md` `:895-897` / `docs/progress/2026-08-15-...-execution-record.md` `:37` | — | 历史记录中「改 stop 后 discard」工作区——**史实不动**，由本 spec 记录被真实修复取代 |
| `docs/superpowers/plans/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md` `:761` | — | batch6 实现 plan 含旧语义代码「先 stop 再 discard」——**史实不动**（同 2026-08-15 plan 待遇） |
| `docs/archive/frontend-handoff-v2/traces/` + `ws-v2-recording.jsonl` | — | 只含 `voice.transcript` 输出帧，无 discard 错误帧——**golden 不动**（D2） |

## 4. 方案（D0-D5 定案）

### 4.1 生产状态机：`discard_recording()` 语义重构（`core/input/voice_sessions.py:336-343`）

```python
async def discard_recording(self, recording_id: str) -> None:
    if self._mode == VoiceMode.IDLE:
        return                    # 幂等成功：迟到/重复 discard 无活动录音，不发错误
    if self._mode != VoiceMode.RECORDING:
        raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话，无法 discard")  # realtime/dictation 隔离
    if self._recording_id != recording_id:
        raise VoiceSessionError(ERR_CONFLICT, "recordingId 不匹配，存在另一条活动录音")
    if self._in_flight_stt is not None and not self._in_flight_stt.done():
        self._in_flight_stt.cancel()   # TRANSCRIBING 态取消在途 STT
    self._reset_recording()            # RECORDING/TRANSCRIBING/PENDING 统一清态回 IDLE
```

转场表（对应 review §3.4，全部成立）：

| 后端当前状态 | recordingId | 目标行为 | 实现 |
|---|---|---|---|
| `RECORDING/RECORDING` | 匹配当前录音 | 立即清 PCM 回 IDLE；成功，不要求先 stop | 取消逻辑（在途无）→ `_reset_recording()` |
| `RECORDING/TRANSCRIBING` | 匹配 | 取消在途 STT、清态回 IDLE；成功 | cancel `_in_flight_stt` → `_reset_recording()` |
| `RECORDING/PENDING` | 匹配 | 丢弃字幕/情绪/events 回 IDLE；成功 | `_reset_recording()` 清 `_pending_*` |
| `IDLE` | 任意 | 幂等成功，不发错误 | 首行直接 return |
| 另一条普通录音活跃 | 不匹配 | 拒绝，绝不取消当前录音 | CONFLICT |
| realtime/dictation 活跃 | 任意 | 保持当前会话不变并返回明确错误 | CONFLICT（不拆会话） |

补充约束（review §3.4，已满足或本方案保证）：
1. discard 成功后不调 STT/不发 `voice.transcript`/不触发 generation——`_reset_recording()` 不触碰 STT 门禁，不 emit。
2. 在途 `_in_flight_stt` 取消 + 迟到结果不复活 PENDING——`_run_recording_stt:300-301` 既有防线保留。
3. 重复/迟到 discard 幂等成功，无新增成功 ACK，保持 send-only wire 形状。
4. 错误 recordingId 不能借幂等语义取消活跃的其它录音——RECORDING 分支先校验 `recording_id` 再取消。
5. 连接关闭既有清理（`close()`）保留。
6. 不通过前端先 `stop` 规避——`stop` 语义是"请求转写"，不得作为采集失败取消替身。

### 4.2 决策表

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批组织/跨仓边界 | **A 后端单批** | 前端配套（FE-S4-RUN-P0/P1-01）+ Windows Electron 复验属 elysia-frontend；§5 完成定义已明确"复验通过后才可标完整联调"，本批只可标「已开发/自动化已验证」 |
| D1 拒绝路径错误码 | **B 复用 `VOICE_SESSION_CONFLICT`** | IDLE-discard 改幂等后 `VOICE_RECORDING_NOT_FOUND` 语义自相矛盾；CONFLICT 精确描述"已有活动语音会话"，前端现象 A 已见并处理，错误目录零新增 |
| D2 golden 是否加样例 | **A 不动 golden** | golden 记录后端→前端输出帧；discard 成功 send-only 无帧可录；两真实序列由 smoke 覆盖；`--check` 门禁零 churn |
| D3 历史文档修订 | **A 只改活跃文档** | 就地修订 protocol-v2.md（操作契约）+ batch6 spec（设计源，review 明确列入"必须修改"）+ smoke 注释；历史 plan/execution-record 作史实不动，由本 spec §7 记录取代关系 |
| D4 smoke 变更 | **A 改写 discard 段 + 加空录音迟到删除** | 旧段是旧语义工作区（stop 后 discard 已无意义）；改写为 `start→discard` 才真正覆盖现象 A |
| D5 WS 路由级测试 | **A 加 discard 路由测试** | smoke 是 slow 不进 test-quick；把 review §3.6 场景 9/10 固化进日常门禁 |

**纪律**：golden、`Makefile`、`providers.yaml`、`frontend_contract/` 零改动。

## 5. 测试计划

### 5.1 Task 1：生产状态机 + 单元测试（`core/input/voice_sessions.py` + `test_voice_sessions.py`）

修改 `test_recording_state_boundaries`（二次 discard 改幂等成功），新增（对应 review §3.6）：

| # | 场景 | 断言要点 |
|---|---|---|
| 1 | `start → discard`（未 stop）成功，随后新 `start` 成功 | 不 raise、`mode==IDLE`、无 emit |
| 2 | `start → PCM → discard` | STT 未调用、无 `voice.transcript`、无 bus post、无 `user_message.ack` |
| 3 | `start → stop → TRANSCRIBING → discard` | 取消在途 STT、无 transcript、`mode==IDLE`、新 start 成功（迟到不复活） |
| 4 | `start → stop → PENDING → discard` | 清 `_pending_*`、commit 后 NOT_FOUND、可立即新 start |
| 5 | `<640B → stop → discard`（IDLE 态） | 幂等成功、无 raise |
| 6 | `STT empty → reset → discard`（IDLE 态） | 幂等成功、无 raise |
| 7 | `discard → discard` 重复调用 | 均成功 |
| 8 | 录音 A 活跃时 `discard(B)` | raise CONFLICT、A 不受影响（仍可 stop+commit） |
| 9 | realtime 活跃时 `discard` | raise CONFLICT、realtime 不受影响（仍可 feed+stop） |
| 10 | dictation 活跃时 `discard` | raise CONFLICT、dictation 不受影响（仍可 stop） |

> review §3.6 #11（commit 文本一致性、录音长度上限、STT timeout、连接关闭清理回归）由**既有测试 + 全量门禁**显式覆盖（§6），本批不新增。

### 5.2 Task 2：WS 路由级测试（`core/input/tests/test_voice_ws.py`）

| # | 场景 | 断言要点 |
|---|---|---|
| 10 | WS 序列 `start(A) → discard(A) → start(B)` | 无 `CONTRACT_VALIDATION`/`VOICE_RECORDING_NOT_FOUND`/`VOICE_SESSION_CONFLICT`；B 可正常 start |
| 9 | WS：realtime 活跃收 `voice.discard` | 收到 `error{code: VOICE_SESSION_CONFLICT}`；realtime 会话不受影响 |
| 11 | WS：无录音时 `voice.discard`（IDLE 幂等） | 不发任何 error 帧 |

### 5.3 Task 3：smoke 改写（`scripts/stage4_smoke.py`）

- **改写** discard 段（现 `:264-279`）为「采集启动失败清理」真实序列：`start(A) → discard(A)`
  （不 stop）→ 断言无 `CONTRACT_VALIDATION` error 帧 + `start(B)` 成功（无 `VOICE_SESSION_CONFLICT`）。
- **新增**「空录音迟到删除」序列：`start → stop`（零 PCM，F3 空转写自动 reset 回 IDLE）→ 迟到
  `discard` → 断言无 `VOICE_RECORDING_NOT_FOUND` error 帧。
- 更新注释（删除对 `voice_sessions.py:339-340` 旧行号/旧语义的引用）。
- 检查数 24 → 27（删旧段 1 check「discard 不创建消息」+ 新增 4 check；`scripts/tests/test_stage4_smoke.py` 只断言非空，安全）。

### 5.4 Task 4：文档同步

- `docs/electron-backend-protocol-v2.md`：`:211`（NOT_FOUND 行去 discard）、`:390`（补 discard
  成功 send-only/幂等注记）、`:402`（discard 注释补语义）、§12.2（补取消语义 bullet）。
- `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md`：
  `:74-81` 状态机图补 `RECORDING ──discard──▶ IDLE` 与 `IDLE ──discard──▶ IDLE（幂等）`、
  `:84`（RECORDING 态 discard 允许）、`:87`（IDLE 态 discard 幂等成功）、`:121`（契约行补语义）、
  `:290`（错误码目录行去 discard，仿 protocol-v2 `:211`）——全部加「阶段4真实联调修订」注记。
- 验证 grep（`先 stop 再 discard`/`IDLE 态收 stop/commit/discard`/`丢弃 pending/在途录音`）在活跃文档零命中；
  仅历史 plan（`2026-08-05` batch6 `:761` + `2026-08-15` stage4 `:895-897`）与 execution-record 的史实记录命中——不动。

## 6. 门禁

- `make test-quick`（unit+service）、`make test-normal`（排除 slow）、`make lint`（ruff）、
  `make pyright`（0 errors）、`make check-types`（含 golden `--check` 零 churn）、
  `conda run -n Elysia python scripts/stage4_smoke.py`（全部 PASS 退出 0）。
- 已通过联调链路回归：普通录音非空字幕/试听/确认提交/生成、空录音"未识别"展示、realtime 分段/
  ack/turn/stop、dictation final/上滑 cancel、TTS preview/自动朗读、Profile test/usable/switch、
  断开重连清理。

## 7. 验收（完成定义）

- `voice.discard` 在 RECORDING/TRANSCRIBING/PENDING 均能安全取消，在 IDLE/重复调用时幂等成功；
  错误 recordingId 及其它语音模式隔离仍严格（CONFLICT，不误清）。
- review §3.6 的 11 条测试全部落地（单元 + WS + smoke）；`make test-quick`、Ruff、Pyright、
  `make check-types` 全绿；smoke 全部 PASS 退出 0（含两条真实序列）。
- protocol-v2.md + batch6 spec + smoke 注释已同步；历史 plan/execution-record 不动，本 spec 是
  取代记录（旧工作区「改 stop 后 discard」被真实修复取代，见 §3.4 末行）。
- **标记**：本批可标「阶段 4 已开发/自动化已验证」；**不得**标「阶段 4 完整真实联调通过」——
  后者须与配套前端修复 SHA 做 Windows Electron 复验（采集启动失败后无需重连即可再次录音、
  空录音删除不再产生 `VOICE_RECORDING_NOT_FOUND`）后由前端/联调作业推进。

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 幂等 discard 吞掉客户端误发的 discard，掩盖真实采集问题 | 前端清单 FE-S4-RUN-P0-01 收敛清理 helper；后端 CONFLICT 仍对"另一条活跃录音/其它模式"显式报错 |
| CONFLICT 错误码对既有前端映射的兼容 | 前端现象 A 已见过 `VOICE_SESSION_CONFLICT` 并处理（`voice.error` 映射已存在） |
| `_run_recording_stt` 取消竞态 | 既有 `_rec_state != TRANSCRIBING → return` 防线保留，Task 1 场景 3 验证 |
| smoke 检查数变化 | `test_stage4_smoke.py` 只断言非空，安全 |
| golden 门禁误触 | 已验证 `ws-v2-recording.jsonl` 无 discard 帧，`--check` 零 churn（D2） |

## 9. 决策点记录（会话定案 D0-D5）

D0 后端单批（跨仓边界同前次 D6）；D1 复用 `VOICE_SESSION_CONFLICT`；D2 不动 golden；D3 只改活跃
文档；D4 smoke 改写 + 空录音序列；D5 加 WS 路由测试。全部按推荐定案，无用户推翻。

## 10. Review 记录

（spec 评审后填写）
