# Electron 后端改造 — 批次 2：结构化聊天 v2 事件模型（Plan）

**Plan 版本：** v0.1
**依据 Spec：** `docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch2-chat-v2.md`（v0.2）
**前置：** 批次 0 基线 + **批次 1（硬前置门禁：`WebSocketChannel.protocol_version` 注入、`server_info`、统一错误信封均依赖批次 1 交付，批次 1 未合入前本批次 Task 4/5/7 不可启动）**
**执行环境：** Elysia conda

---

## 概览

v2 结构化聊天：ACK/幂等/稳定 ID/结构化完成与失败。核心是**单一语义源、双 wire 呈现**——loop 发统一回合事件，OutputManager 按 channel.protocol_version 翻译（v1 逐字节复刻现状，v2 发 generation.*）。**TDD：每个任务先写失败测试。**

**硬约束**：v1 wire 逐字节不变；不修改任何 v1 契约模型；`make test-quick` 全绿。

## 任务

### Task 1: v2 契约模型（frontend_contract）

**Files:** `frontend_contract/ws_message.py`、`frontend/src/types/contract.generated.ts`

- [ ] **RED**（unit）：`frontend_contract/tests/` 新增测试——7 个 Server→Client v2 模型 + `V2TextMessage` 的 round-trip（`model_dump → model_validate`）与 wire 字段断言（type 判别、`exclude_none` 行为、`error` 别名）
- [ ] 实现契约模型（追加到 `ServerMessage` 联合；`V2TextMessage` 独立不进联合）：
  - `UserAckMessage`（type=`user_message.ack`：messageId/clientRequestId/clientMessageId/conversationId/receivedAt/status/error）
  - `GenerationStartedMessage`、`GenerationSegmentStartedMessage`、`GenerationDeltaMessage`、`GenerationSegmentCompletedMessage`、`GenerationCompletedMessage`（finishReason 限 stop|tool_calls）、`GenerationFailedMessage`
  - 错误信封模型复用批次 1（code/message/retryable/requestId/error/details）
- [ ] `python -m frontend_contract` 重新生成 TS 快照；`make check-types` 通过；**确认 v1 类型字节不变**（`git diff contract.generated.ts` 仅新增）
- [ ] Commit

### Task 2: ID 生成器 + GenerationContext

**Files:** `core/cognitive/generation.py`（新建）、`core/utils/message_id.py`

- [ ] **RED**（unit）：`core/cognitive/tests/test_generation.py`——`new_generation_id()`（`gen_` 前缀）、`new_segment_id()`（`seg_` 前缀）格式断言；`GenerationContext` 打开/关闭分段、递增 segmentIndex、deltaSequence、工具轮不重置
- [ ] 实现：`GenerationContext` dataclass：`user_message_id`、`assistant_message_id`、`generation_id`、`segment_index`、当前 `segment_id`、`delta_sequence`、`channel`；方法 `open_segment(channel, kind) -> segment_id`、`next_delta() -> seq`、`close_segment()`
- [ ] Commit

### Task 3: UserInputData 扩展 + messageId 贯通

**Files:** `core/schema/event.py`、`core/agent/_types.py`、`core/agent/agent.py`、`core/cognitive/loop.py`

- [ ] **RED**（service）：测试——`UserInputData` 新可选字段（`message_id`、`protocol_version`）；`_PendingMessage` 携带 messageId 且出队重建不丢；用户消息落库优先用预分配 ID
- [ ] 实现：`UserInputData` 加 `message_id: str | None`、`protocol_version: int = 1`（`core/schema/event.py`）；`_PendingMessage` 加 `message_id`
- [ ] **messageId 贯通链路（架构修正）**：`_build_turn_prompt` 签名无 `data` 参数——必须新增参数贯穿：`agent.process_message(message_id=…)` → `UserInputData(message_id=…)` → `agent._handle_user_input`（agent.py:454）→ `cognitive.process(message_id=…)` → `_think_loop` → `_build_turn_prompt(user_message_id=…)` → 落库点（loop.py:418）优先用预分配 ID；**不得**假设 `_build_turn_prompt` 能直接读 `data`
- [ ] Commit

### Task 4: WebSocketChannel v2 wire 方法 + protocol_version

**Files:** `Module/output/protocol.py`、`Module/output/websocket.py`

- [ ] **RED**（unit）：`Module/output/tests/`——每个 v2 方法产出正确 wire JSON（`on_generation_started` → `{"type":"generation.started",...}` 等）
- [ ] 实现：`OutputChannel` 加 `protocol_version: int = 1`；`WebSocketChannel` 加 7 个 `on_*_v2` 方法（构造对应契约模型 + `wire_dict()`）；`handle_ws` 注入（批次 1 已提供握手头读取）
- [ ] Commit

### Task 5: OutputManager 统一回合事件扇出（核心）

**Files:** `Module/output/manager.py`

- [ ] **RED**（service）：`Module/output/tests/test_manager_v2.py`——给定混合 v1/v2 channel，每个统一回合事件：v1 channel 收到现状 wire（delta/message/command/expression），v2 channel 收到 generation.* wire；v2 抑制 inner_voice command / token_usage / expression（mood 捕获）
- [ ] 实现：OutputManager 新增统一方法，按 channel.protocol_version 分发：
  - `turn_started(ctx)` → v2: on_generation_started；v1: 无
  - `segment_started(ctx)` → v2: on_generation_segment_started；v1: 无
  - `text_delta(text, ctx)` → v1: on_text_delta；v2: on_generation_delta
  - `segment_completed(text, ctx, tts_active)` → v1: tts_active 关→on_message("")；v2: on_generation_segment_completed
  - `inner_voice(text, ctx)` → v1: on_command("inner_voice")；v2: on_generation_segment_*(inner_voice)
  - `mood(expr, intensity)` → v1: on_expression；v2: 捕获 mood 不发
  - `turn_completed(finish_reason, usage, model, mood)` → **v1: NO-OP**（`TokenTracker.record` 已实时广播 v1 token_usage，`_finalize_turn` 再发会双发——防双发硬约束）；v2: on_generation_completed
  - `turn_failed(code, message, retryable, label)` → v1: `on_text_delta(f"\n❌ {label}: {message[:200]}")` + `on_message("")`（**逐字节复刻现状** loop.py:368-373，label 来自 `_is_llm_layer_error` 分类）；v2: on_generation_failed
- [ ] **text_delta TTS 门控归属**：loop 保留现状门控（`not (tts_mgr.active and enabled)` 时才发 text_delta），或 ctx 携带 `tts_active` 由 v1 翻译层抑制——**二选一写死在实现中，不得两处都无**（v1 回归高危）
- [ ] **mood 收口**：`_apply_mood`（loop.py:109-144）内部 `engine._push_expression` 的 expression 广播**改走统一 `output.mood()`**——v1 发 expression、v2 捕获不发；personality engine 的独立 expression（STT 情绪/空闲）照旧直发
- [ ] 抑制规则（spec §5.1）：v2 仍发 state/typing/audio/action/block
- [ ] Commit

### Task 6: loop 统一发射重构

**Files:** `core/cognitive/loop.py`、`core/text/stream_processor.py`（如需）

- [ ] **RED**（service）：`core/cognitive/tests/test_v2_generation.py`——FakeLLM 带标签响应，v2 连接：`generation.started → segment.started(inner_voice) → deltas → segment.completed → segment.started(public_reply) → … → completed`，断言 IDs/顺序/usage/model/mood
- [ ] **RED**（service）**v1 逐字节回归专项测试**（§10.5）：同 agent、v1 连接、FakeLLM 带标签响应 → 断言收到的帧序列（type+关键字段）与现状**逐帧相等**——`make test-quick` 降级为必要非充分条件（现有测试不覆盖全链路 wire，已核实）
- [ ] **RED**（service）**fault 测试**（§10.7）：FakeLLM fault → v2 收 `generation.failed{code}` 且无 `❌` delta；v1 仍收 `❌` delta
- [ ] 实现：loop 用 GenerationContext 替换直接发射（_dispatch_stream_event 各分支、_finalize_turn、异常路径）；assistant_message_id 在 generation 开始预分配并贯通落库（替换 `_finalize_turn` 的 `new_message_id()`）；`turn_completed` 读取 TokenTracker stats + agent.llm.model + mood；错误路径发 `turn_failed`（含 label 分类，v1 翻译层逐字节复刻 ❌ delta + send("")）；**`_finalize_turn` 的 `not speaking_started → send(msg)` 边路径**（loop.py:731-734）纳入统一发射（v1 复刻）
- [ ] **v1 回归**：`make test-quick` 全绿 + §10.5 专项通过——证明统一事件精确复刻 v1 wire
- [ ] Commit

### Task 7: ws_handler v2 路由 + ACK + 幂等

**Files:** `core/input/ws_handler.py`、`core/agent/agent.py`、`core/input/idempotency.py`（新建）

- [ ] **RED**（service）：`core/input/tests/test_v2_ws.py`——v2 文本消息：ACK（messageId/clientRequestId）；重复 requestId 同内容→重发 ACK 不重复生成；不同内容→rejected+error；缺 requestId→CONTRACT_VALIDATION；忙时排队 messageId 一致
- [ ] 实现：`handle_json_message` 按 `channel.protocol_version` 路由 v2 文本；`IdempotencyStore`（进程内 dict + TTL）；预分配用户消息 ID 并随 `UserInputData` 传循环；ACK 立即发
- [ ] Commit

### Task 8: 取消（generation.failed CANCELLED）

**Files:** `core/cognitive/loop.py`、`core/input/ws_handler.py`

- [ ] **RED**（service）：测试——v2 生成中 interrupt → `generation.failed{GENERATION_CANCELLED}`，无迟到帧，终态唯一（错误路径已发 failed 则中断不重发）
- [ ] **RED**（service）**TTS 边界测试**（§10.8）：TTS 开启时 v2 `segment.completed` 仍发、v1 行为不变（现状 TTS 开时 `send("")` 不发）
- [ ] 实现：`_handle_interrupt` 检测活动 generation + 未被错误路径覆盖 → 发 `turn_failed(CANCELLED)` + 终止 TTS；终态唯一守卫
- [ ] Commit

### Task 9: 重连最小语义 + 轨迹 + 文档

**Files:** `docs/electron-backend-protocol-v2.md`（新建）、`docs/electron-backend-baseline.md`（更新）、`frontend-handoff-v2/traces/`、`scripts/capture_ws_trace.py`（扩展）

- [ ] **RED**（service）v2 重连：`session` 回放含稳定 messageId；未完成 generation 标记 interrupted → `generation.failed{CANCELLED}`（向在线 channel）
- [ ] 三份 v2 脱敏 JSONL：完整生成、取消、失败（扩展抓包脚本或新脚本，含 `X-Protocol-Version: 2` 握手）
- [ ] `docs/electron-backend-protocol-v2.md`：v2 事件契约参考（字段表 + 序列图 + 幂等/取消/重连语义）
- [ ] 更新 `docs/electron-backend-baseline.md` 事件面章节 + `Module/output/README.md`、`core/cognitive/README.md`
- [ ] Commit

### Task 10: 全量验收（subagent）

- [ ] 验收 subagent：核对 §12 退出标准全部；`make test-quick` 全绿（Elysia conda）；`make check-types` 通过；三份 JSONL 可解析重放无敏感；v1 契约/发射语义未改

## 依赖

Task 4/5/7 依赖 Task 1（契约）+ Task 2（ID）；Task 6 依赖 2/3/5；Task 7 依赖 3/4/5。顺序执行 1→2→3→(4,5)→6→7→8→9→10。

## 风险

- **v1 回归**（最高）：Task 6 重构是 v1 行为等价的保证；`make test-quick` 的 1883 测试是安全网，任何 v1 wire 变化立即翻红
- **mood 双源**：`_apply_mood` 与 personality engine 独立 expression（STT/idle）——只统一 mood-tag 路径，独立 expression 照旧发 v1/v2
- **工具轮**：多轮 `_stream_llm_response` 共享同一 GenerationContext，segmentIndex 不重置；`finishReason=tool_calls` 仅在进入工具轮且最终未 stop 时
- **幂等存储**：进程内 + TTL，重启丢失（Demo 可接受）；不得引入 MongoDB 依赖

## 非目标

多会话（批 3）、续传/regenerate/游标查询（BE-024 后续）、Desktop 桥（批 4）、非 text 分段渲染（批 7）、语音（批 6）。
