# Input 层拆分 批次 B：ws_handler 拆分（先提取后注册表）

**文档版本：** v0.1（初稿，待评审）
**文档状态：** 待评审
**日期：** 2026-08-14
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** 会话记录「core/input 内聚性评估」（2026-08-14）问题 1
**前置决策：** AskUserQuestion Q2（**先提取后注册表**）+ Q3（**helper 随业务下移 + 公共进 _common**）
**依赖批次：** 批次 A（errors.py）已先建立 `core/input/_common.py`/`errors.py` 模式

---

## 1. 背景

`core/input/ws_handler.py` 全仓最大（1825 行），协议分发与业务逻辑混杂：

- `handle_json_message`（1266-1698，**433 行**）是 **22 种消息类型**的巨型 if/elif 链；
- `_route_v2_text`（787-1088，**300 行**）+ `_handle_voice_commit`（1088-1266，**178 行**）是深业务逻辑
  （附件校验 → 幂等 → process_message → ACK/REJECT 流），却寄生在协议层文件；
- 14 个业务 helper（幂等命中/ACK 发送/附件构造/语音门控）与协议 framing 同文件。

近期 8 月 185 次提交触及 input 层，功能持续追加到该文件（批次 2-9D 的 v2 文本/幂等/语音逻辑都落在
ws_handler），已越过「健康增长」阈值。

## 2. 目标

- **第一步（提取，行为锁定）**：深业务函数 + 业务 helper 下移 `ws_v2.py`，`ws_handler.py` 只留
  协议 framing + 分发骨架（`handle_ws`/`handle_json_message` + 协议 helper）。
- **第二步（注册表）**：`handle_json_message` 的 if/elif 链改 `_WS_HANDLERS` 注册表分发。
- **零行为变更**（3548 测试为行为锁定安全网）。

## 3. 现状（事实基线）

| 项 | 值 |
|---|---|
| 消息类型 | **22 种**：`wake_toggle` / `text` / `interrupt` / `switch_model` / `set_tts_voice` / `set_service` / `tool_approval` / `activate_session` / `tool.status` / `voice.record.{start,stop}` / `voice.commit` / `voice.discard` / `voice_session.{start,pause,resume,stop,cancel}` / `dictation.{start,stop,cancel}` / `audio.cancel` |
| 分发函数 | `handle_json_message`（1266-1698，433 行）——if/elif 链 + 限流 + 判别联合校验 |
| 深业务函数 | `_route_v2_text`（787-1088，300 行）、`_handle_voice_commit`（1088-1266，178 行） |
| 业务 helper（跟随下移 ws_v2） | `_send_rejected`(10)、`_handle_idempotency_hit`(9)、`_idempotency_unavailable_error`(7)、`_storage_unavailable_error`(4)、`_in_progress_error`(4)、`_release_attachment_reservations`(5)、`_session_or_none`(3)、`_attachment_part`(2)、`_build_media_parts`(2)——仅被 v2 深业务用 |
| 跨业务共享（进 `_common.py`） | `_send_ack`(31——v2 文本/语音 + 保留分支 wake_toggle:1363/text:1374/set_service:1398 共用)、`_find_channel`(7——`_route_v2_text`:817 + 保留分支 1326/1533/1546 共用；进 _common 避免 ws_v2↔ws_handler 循环) |
| 留 ws_handler（保留分支仍用） | `_voice_stt_gate`(10)、`_voice_descriptor`(4)——voice.record/voice_session/dictation 分支**未移动**仍调用（1563-1668）；`_is_v2`(15——仅被保留分支 text/activate_session/voice 用，ws_v2 不用) |
| 协议 helper（留存） | `resolve_protocol_version`、`_current_request_id`(13)、`_extract_client_request_id`(2)、`_send_voice_error`(WS 错误信封) |
| 出口（不可破坏） | `server.py`: `handle_ws`/`resolve_protocol_version`；测试: `handle_json_message`/`_send_voice_error`/`_handle_activate_session`/`do_transcription`/`do_interrupt` |

## 4. 方案

### 4.1 第一步：下移 `ws_v2.py`（行为锁定）

新建 `core/input/ws_v2.py`，从 ws_handler 移动：

1. **深业务**：`_route_v2_text`、`_handle_voice_commit`（函数体零改动）；
2. **业务 helper**（仅 v2 深业务用）：`_send_rejected`、`_handle_idempotency_hit`、`_idempotency_unavailable_error`、
   `_in_progress_error`、`_storage_unavailable_error`、`_release_attachment_reservations`、`_session_or_none`、
   `_attachment_part`、`_build_media_parts`；
3. **跨业务共享**：`_send_ack`、`_find_channel` 进 `core/input/_common.py`（ws_v2 与 ws_handler 共用，
   避免 ws_v2↔ws_handler 循环，Q3）；`_is_v2` 留 ws_handler（仅保留分支用，ws_v2 不依赖）。

`ws_handler.py` 保留：`handle_ws`、`handle_json_message`（骨架）、`resolve_protocol_version`、
`_current_request_id`、`_extract_client_request_id`、`_find_channel`、`_send_voice_error`、
`_voice_stt_gate`、`_voice_descriptor`（voice 分支未移动仍用）、`do_interrupt`、`do_transcription`、
`send_wake`、`_handle_activate_session`（激活会话属 WS 连接生命周期，留存）。

> 注：第一步完成后，`handle_json_message` 的 22 分支中「深业务型」（text/voice.commit）调用
> `ws_v2` 函数；其余保留。此时 diff 是纯移动（+ `_send_ack` 迁 _common），可审可回滚。

### 4.2 第二步：注册表分发

`handle_json_message` 的 if/elif 链改为查表：

```python
_WS_HANDLERS: dict[str, Callable[..., Awaitable[None] | None]] = {
    "wake_toggle": _handle_wake_toggle,
    "text": _handle_text,
    "interrupt": _handle_interrupt,
    ...
}
```

- 每个分支体提取为模块级 `_handle_<type>(...)` 函数（统一签名，参数差异经 partial 或统一 kwargs 适配）；
- 未知 type → 走既有拒绝路径（ErrorMessage / CONTRACT_VALIDATION）；
- `handle_json_message` 保留：限流前缀、判别联合校验、查表分发、异常兜底。

### 4.3 `_common.py` 归属（Q3）

跨 v2 业务与协议共用的通用件（若第一步后发现跨 ws_v2/ws_handler 共享）放 `core/input/_common.py`；
批次 A 的 `errors.py` 保持错误信封专属。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D-B1 分发方式 | **两步走：先提取后注册表**（Q2 敲定） | 第一步纯移动行为锁定、diff 可审；第二步再改分发结构，风险最低 |
| D-B2 新模块命名 | `core/input/ws_v2.py` | 语义明确（WS v2 业务）；与 `voice_sessions.py`/`audio_buffer.py` 并列 |
| D-B3 helper 归属 | **随业务下移 + 公共进 _common**（Q3 敲定） | 各模块自包含、无反向依赖；跨业务共享件收敛到 `_common.py` |
| D-B4 协议 helper 留存 | `_is_v2`/`_find_channel` 随调用者下移 | 被 v2 业务大量引用（15/7 次），留在 ws_handler 会制造反向依赖 |

## 6. 波及面

- 新增：`core/input/ws_v2.py`
- 修改：`core/input/ws_handler.py`（瘦身 ~700 行）、可能 `core/input/_common.py`
- 测试：`core/input/tests/test_voice_ws.py`（import `handle_json_message`）、`test_request_id.py`（import
  `_send_voice_error`/`_handle_activate_session`）、`test_server.py`（import `do_transcription`/`do_interrupt`）——
  **出口保持稳定**，import 路径不变则零测试改动；若移动的 helper 被测试直接 import（如 `_send_voice_error`），
  跟随更新
- **无**：wire 行为、契约、错误码

## 7. 验收标准

1. `make test-quick` 全绿 + `make test-full`（3548 passed 基线）全绿；
2. `wc -l core/input/ws_handler.py` 显著下降（目标 <1200，深业务下移后）；
3. 22 种消息类型行为不变（`test_voice_ws.py`/`test_session_ws.py` 全绿）；
4. ruff + pyright 全绿（`ws_v2.py` 纳入门禁）；
5. 第二步后：`grep -n "msg_type ==" core/input/ws_handler.py` 无巨型 if/elif 链（查表分发）。

## 8. 风险

- **中（第一步）**：移动 12 个 helper + 2 深业务函数，import 依赖需逐一核对；3548 测试锁定；
  若 helper 被 ws_handler 其它分支也用（如 `_send_ack` 被 activate_session 用），需留引用或移 `_common.py`；
- **中（第二步）**：22 分支统一签名需适配参数差异（partial/kwargs）；查表后未知名拒绝路径需保留；
- 出口稳定：`server.py` 的 `handle_ws`/`resolve_protocol_version` 不移动。

## 9. 后续衔接

- 批次 C（api_routes 拆 5 文件）、批次 D（server.py 纯装配）与此批独立，可并行推进；
- 若第一步后 `ws_handler.py` 仍 >1200 行，第二步把协议层再细分（如 `ws_protocol.py` framing）作为可选后续。
