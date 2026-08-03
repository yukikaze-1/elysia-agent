# Electron 后端事实基线（2026-08-04）

**文档版本：** v0.1（批次 0 产出，BE-001）
**基线 Commit：** `1e62b58`（含 `docs/14`/`docs/15` PRD 文档）
**验证环境：** Elysia conda
**关联：** `docs/superpowers/specs/2026-08-04-electron-backend-batch0-baseline.md`

本文件是 Electron 前端改造后端的**事实基线**——所有结论来自真实代码与命令输出，作为批次 1-7 的设计依据。与 PRD（`docs/14`）的差异已在末尾列出。

---

## 1. 仓库状态

- HEAD `1e62b58`，branch `main`；批次 0 交付物加入前工作树干净。
- 基线测试（`make test-quick`，Elysia conda）：**1883 passed / 2 skipped / 58 deselected**，64.7s。
- 契约漂移（`python -m frontend_contract --check` / `make check-types`）：**无漂移**，exit 0。

## 2. 时间戳事实

- **核心数据（消息 / Token / 会话）统一 UTC ISO 8601**，经 `core/schema/utils.py:28-34` `now_iso()` = `datetime.now(timezone.utc).isoformat()`（`+00:00` 偏移）。
  - `UserMessage/AssistantMessage/ToolResultMessage.timestamp`：`default_factory=now_iso`（`core/schema/message.py`）。
  - `TokenUsage`/`CharacterState`/session 时间戳同样走 `now_iso()`。
- naive `datetime.now()`（无时区）共 **4 处**，均在展示/通知/工具路径，**不落库**：
  - `core/monitoring/idle_monitor.py:120`（状态显示时间）；
  - `core/cognitive/loop.py:1044`（`_build_wake_up_message` 主动搭话文案）；
  - `Module/system_prompt/sections/sensory.py:51`（系统提示当前时间上下文）；
  - `Module/tool/__main__.py:32`（工具演示）。
- **结论**：交接资料"UTC/本地矛盾"已消解——核心数据已 UTC；v2 契约沿用 `now_iso()` 格式，无需迁移旧数据（旧数据已是 UTC ISO）。展示路径不改（不影响落库）。

## 3. 事件面现状（WS 契约）

**Server→Client 判别联合 `ServerMessage`（`frontend_contract/ws_message.py:331-350`）18 成员：**

`session` `capabilities` `delta` `message` `token_usage` `expression` `state` `audio` `audio_end` `typing` `transcription` `action` `command` `wake` `switch_model_result` `service_mode_result` `heartbeat_ack` `error`

**Client→Server 6 类（`ws_message.py:285-327`）：** `text` `interrupt` `switch_model` `set_service` `tool_approval` `wake_toggle`

**现状关键语义（事件流测绘）：**
- 流式正文走 `delta`（增量）；`message content:""` 仅作**句子边界/气泡终止**（TTS 关闭时每个 SENTENCE 触发，`loop.py:1019` `send("")` → `on_message("")`）。
- 完成信号 = `state:"idle"`（经 EventBus → `on_state`，`agent.py:_handle_state_change`）。
- Inner Voice 经 `command:"inner_voice"`（`loop.py:989-998`，`<inner_voice>` META 标签闭合触发）。
- 心情经 `expression`（`<mood>` 标签 → `_apply_mood` → `_push_expression`，`loop.py:984-987`）。
- Token 经 `token_usage`（`TokenTracker.record`，**每次 LLM API 调用**发一次，非每句）。
- 生成错误以 `delta("\n❌ …")` 文本表示（`loop.py:366-373` except 路径）+ 空 `message` 收尾。
- 句子切分：`SpeechSplitter` 结构感知分句（硬分隔符 `。！？!?\n♪♡` + 点号/缩写保护），`StreamProcessor` 产出 SENTENCE + TEXT_DELTA。
- **无** protocol_version / generation / response / segment / message ACK 字段。

**一键回复事件序列**（实测 `ws-chat.jsonl`，纯文本、无工具、TTS 关）：

```
session → capabilities → expression(建连心情) → state thinking → typing true →
inner_voice command → token_usage → expression(mood 标签) → delta ×N →
state speaking → message("") ×N（每句边界）→ state idle
```

注：中文无空格文本经 tokenizer 的 `_pending` 缓冲（仅在空格/换行/`<`处 flush），delta 粒度是**整段聚合**而非逐字；句子边界由 splitter 的 SENTENCE（空 `message` 帧）提供——前端不依赖 delta 粒度切句。

## 4. 消息落库与会话结构

- **单活动会话**：Agent 组合根只创建**一个** `SessionMemory`（`core/memory/session.py:75`，`session_id="default"`）；全代码库无会话选择/切换接口。
- `SessionMemory`：deque（maxlen `session_max_messages`）+ 双写：
  - 进程内 deque（LLM 上下文窗口）；
  - fire-and-forget 归档到 MongoDB `message_archive`（`MessageRepository.append`，`core/storage/message_repo.py:95`，**已按 session_id 建索引**，`query_by_session` 已存在——批次 3 多会话利好）。
  - MongoDB 不可用降级 JSONL。
- 消息结构（`core/schema/message.py`）：`UserMessage`/`AssistantMessage`/`ToolResultMessage`，含 `message_id`（`msg_`+26hex，`core/utils/message_id.py`）、`role`、`content`（TextPart/ImagePart）、`timestamp`（UTC）、`metadata`；Assistant 另含 `inner_voice`/`tool_calls`/`token_usage`/`block_index`。
- 落库点：用户消息 `_build_turn_prompt`（`loop.py:415-420`）；助手消息 `_finalize_turn`（`loop.py:736-746`）；工具消息 `_execute_tool_round`。
- 会话回放：`session.messages_for_client()` → `SessionMessage(messages, token_stats)`，建连时发送（`ws_handler.py:84-94`），单条失败仅丢该条。

## 5. 工具与 Runtime

- **12 个进程内工具插件**（`core/tools/__init__.py` ALL_PLUGINS），全部 **Backend Runtime**：

  `memory_search` `memory_save` `time` `web_search` `web_fetch` `read_file` `list_dir` `write_file` `set_reminder` `runtime_config` `self_state` `describe_image`

- 服务器沙盒 `read_file`/`write_file`（`core/tools/filesystem/`）访问 `data/sandbox/{session_id}`，**属 Backend Runtime**。PRD 明确禁止映射成 `desktop.file.*`（`docs/14` §3.1/§5、`docs/15` §三.1）。本仓库文档**无"误标 Desktop"问题**——交接包中的误标已被这些文档纠正。
- HITL 审批：`Module/tool/manager.py` 广播 `command:"tool_approval"`，参数以 `str(params)[:200]` 截断作为兼容摘要（`ws_handler.py:127`）；重连时重播 pending 审批。
- 工具事件：`action:"tool_executing"/"tool_done"`（`loop.py:_execute_tool_round`）。

## 6. 认证与上传现状

- `auth_token`（`SERVER_AUTH_TOKEN`）仅护 `/shutdown` `/restart` `/tools` `/tools/reload` + `/debug/*`（`core/input/server.py:61-74` `_check_auth` + `register_debug_routes`）。`/ws`、`/api/*`、`/uploads/*`、`/health` `/ready` **匿名**。
- `/uploads/*`：`add_static` 公开静态（`server.py:251`），图片 URL `/uploads/images/{name}` 永久匿名可访问。
- 业务 REST（`core/input/api_routes.py`）：`/api/providers`（CRUD + test）、`/api/images`（上传）、`/api/chat`（fire-and-forget，无 ACK）、`/api/messages`（**全库 `_id` 游标分页**，无会话/日期/搜索维度）。
- 配置：`ServerConfig`（`core/config/app_config.py:32-37`）有 `host/port/auth_token/image_max_mb/image_allowed_types`；**无** access_token、无 HTTP 速率限制、无并发连接上限、无请求体大小上限（aiohttp 默认 client_max_size 1MB）。

## 7. 语音与 STT

- STT：PCM + VAD 后**自动提交**——`do_transcription`（`ws_handler.py:315-356`）识别即回显字幕 + `bus.post(UserInputData)`，无确认/草稿环节。
- TTS：`audio`/`audio_end`，chunk 带 `stream_id`/`seq`（`Module/output/websocket.py`）。
- 三场景（普通录音/实时语音/输入法转写）未独立——批次 6。

## 8. 真实 WS 轨迹

三份脱敏轨迹存放（批次 0 产出）：

- `frontend-handoff-v2/traces/ws-chat.jsonl` —— 建连 → 用户消息 → inner_voice → 多句 public_reply → token_usage → 心情 → 完成；
- `frontend-handoff-v2/traces/ws-cancel.jsonl` —— 生成中打断；
- `frontend-handoff-v2/traces/ws-tool-approval.jsonl` —— 工具审批往返（`tool_approval` 广播 + 回传）。

生成方式：`scripts/capture_ws_trace.py`（FakeLLM 带标签响应 + TTS 禁用；工具轨迹用带 `stream_think` 的 fake yield 指向 `save_memory`/`write_file` 的 tool_calls）。

## 9. 与 PRD 描述差异

| PRD（`docs/14` §2）断言 | 基线结论 |
|---|---|
| 12 个服务器进程内工具 | **一致**（12 插件类） |
| 单活动会话 | **一致** |
| 用户消息无 ACK、无 generation ID | **一致** |
| 句子 `message content:""` 间接分界 | **一致** |
| 完成靠 `state idle`、错误 `❌` 文本 | **一致** |
| Inner Voice = `command inner_voice`；Public = `delta/message` | **一致** |
| Token 真实统计；心情 `expression` | **一致** |
| TTS `audio/audio_end` + `stream_id/seq` | **一致** |
| `read_file/write_file` 只访问服务器沙盒 | **一致**（Backend Runtime，禁止伪装 Desktop） |
| 无 Desktop/Local Worker Runtime | **一致** |
| Provider 支持元数据/测试/切换/custom；custom 密钥只引用 env | **一致**（`custom_providers.yaml` 存 `api_key_env` 不存明文；但 `add_provider` 接受任意 dict，存在明文落盘风险——批次 5 修复） |
| STT 自动提交、无按住说话草稿 | **一致** |
| 只有图片上传完整 | **一致** |
| 存在 `frontend_contract/` + `contract.generated.ts` 生成流程 | **一致**（无漂移） |
| 时间戳"UTC/本地互相矛盾" | **已消解**：核心数据 UTC（`now_iso()`），naive 仅展示路径 |

## 10. 未决 / 已知缺口（供后续批次）

- `/uploads/*` 永久匿名、无短期签名 URL（批次 7）；
- 无产品级多会话 API（批次 3）；
- 无协议版本协商 / server_info（批次 1）；
- 无统一错误信封（批次 1）；
- `custom_providers.yaml` 明文落盘风险（批次 5 堵漏）。
