> 当前契约 golden 与历史抓包同目录归档；两类文件用途不同，见下表。

# Electron 后端 WS 轨迹（golden frames + 历史快照）

本目录 JSONL 分两类：

1. **契约源 golden frames**（不带 `legacy` 的 `ws-v2-*.jsonl`，Task 4/9B-1 起）：由
   `scripts/gen_contract_frames.py` 从 `frontend_contract/` 契约模型（`ServerMessage`
   判别联合）确定性生成（固定测试 ID/时间戳）。文件保持标准 JSONL：每个非空行
   都可直接 `JSON.parse/json.loads`；「非事实源、漂移门禁兜底」元数据仅记录在本
   README。`make check-types` 对它们做 `git diff` 漂移检测——改契约不重跑 gen-frames = CI 红。
2. **历史真实快照**（`ws-chat/cancel/tool-approval` + `ws-server-info` +
   `ws-v2-*-legacy-batch2`）：批次 0~2 捕获时点的真实脱敏 WS 轨迹，由已停用的
   `scripts/capture_ws_trace.py` 生成（DEPRECATED），改名保留作历史参考。

## 重要：快照性质

真实轨迹是**捕获时点的诚实快照**——`server_info.features` 反映当时的后端能力，不反映
当前 HEAD。例如 `ws-v2-cancel-legacy-batch2.jsonl` /
`ws-v2-fail-legacy-batch2.jsonl` 的 `structured_generation:false`——抓取于
4f27702 之前。无 `legacy` 后缀的 golden frames 由当前契约能力声明（features 全 true）。

**以当前契约/能力为准时，请以 `frontend_contract/` 源 + `docs/electron-backend-protocol-v2.md`
为准；轨迹仅作事件序列与 wire 形状参考。**

## 轨迹清单

| 文件 | 来源 | 内容 | 帧数 |
|---|---|---|---|
| `ws-v2-handshake.jsonl` | 契约源 golden | v2 握手（session→server_info→expression） | 3 |
| `ws-v2-session.jsonl` | 契约源 golden | v2 多会话（activate 回放 conversationId 贯通→generation.*） | 10 |
| `ws-v2-chat.jsonl` | 契约源 golden | v2 结构化聊天（ack→generation.* 完整序列→idle→character.state.changed） | 14 |
| `ws-v2-cancel.jsonl` | 契约源 golden | v2 取消（partial segment→GENERATION_CANCELLED→interrupted→idle） | 14 |
| `ws-v2-fail.jsonl` | 契约源 golden | v2 Provider 故障（started→GENERATION_PROVIDER_UNAVAILABLE→idle） | 12 |
| `ws-v2-tool.jsonl` | 契约源 golden | 桌面工具（tool.request→tool_approval→tool_calls 完成） | 4 |
| `ws-v2-voice.jsonl` | 契约源 golden | 实时语音（started→partial→turn→ack→audio→ended） | 7 |
| `ws-v2-rejected.jsonl` | 契约源 golden | 7 负面 rejected ACK（CONTRACT_VALIDATION/SESSION_NOT_FOUND/ATTACHMENT_NOT_FOUND/QUEUE_FULL/STORAGE_UNAVAILABLE/RATE_LIMITED/IN_PROGRESS，Task 6） | 7 |
| `ws-v2-recording.jsonl` | 契约源 golden | 普通录音（stop→voice.transcript 字幕演进 ×2，commit 前待确认，阶段 4） | 2 |
| `ws-v2-dictation.jsonl` | 契约源 golden | 听写（partial 演进→final 成功 + partial→cancelled 取消，零消息副作用，阶段 4） | 5 |
| `ws-v2-audio.jsonl` | 契约源 golden | TTS 播放控制（audio_start→chunk→end 完成 + audio_start→chunk→audio_cancelled 取消，stream_id/seq 稳定，阶段 4） | 7 |
| `ws-v2-service.jsonl` | 契约源 golden | 服务切换结果（switch_model/service_mode/set_tts_voice 成功+失败，键形状对齐模型注释，阶段 4） | 6 |
| `rest-providers.jsonl` | 契约源 golden | GET /api/providers 响应（llm/stt/tts/custom/unconfigured，ProvidersResponse，阶段 4） | 1 |
| `rest-provider-test.jsonl` | 契约源 golden | POST /api/providers/test 响应（成功+失败，ProviderTestResult，阶段 4） | 2 |
| `rest-tts-voices.jsonl` | 契约源 golden | GET /api/tts/voices 响应（正常+未启用，TtsVoicesResponse，阶段 4） | 2 |
| `rest-tts-preview.jsonl` | 契约源 golden | POST /api/tts/preview 响应（成功+失败，TtsPreviewResult，阶段 4） | 2 |
| `rest-envelopes.jsonl` | 契约源 golden | 通用成功/失败信封（ApiOk/ApiFail/ProtocolError 三模型，行→模型按 ok/code 判别，阶段 4） | 3 |
| `ws-chat.jsonl` | 历史真实 | v1 完整聊天 | 16 |
| `ws-cancel.jsonl` | 历史真实 | v1 取消 | 15 |
| `ws-tool-approval.jsonl` | 历史真实 | v1 工具审批 | 24 |
| `ws-server-info.jsonl` | 历史真实 | v2 握手（全 false 时点） | 3 |
| `ws-v2-cancel-legacy-batch2.jsonl` | 历史真实 | 批次 2 v2 取消抓包 | 14 |
| `ws-v2-fail-legacy-batch2.jsonl` | 历史真实 | 批次 2 v2 故障抓包 | 12 |

## 再生成 / 漂移检测

```bash
python scripts/gen_contract_frames.py          # 重写 17 个 golden frames
python scripts/gen_contract_frames.py --check  # 漂移检测（CI，make check-types）
```

## 数据保护

- 全部内容为测试占位文本/测试 ID（`msg_/gen_/seg_/call_/vs_/req_` 前缀），
  session_id="default" 或隔离测试会话，无真实聊天正文、密钥、Token 或个人路径。
- golden frames 无网络、不连 Mongo，纯契约模型构造（确定性）。
- 阶段 4 REST/WS 样例含成功+失败正反例；`rest-envelopes` 混合 ApiOk/ApiFail/ProtocolError
  三模型，行→模型按 `ok`/`code` 判别。
