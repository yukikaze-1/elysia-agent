# Electron 后端真实 WS 轨迹（脱敏快照）

本目录的 JSONL 为各批次**捕获时点**的真实脱敏 WS 轨迹，由 `scripts/capture_ws_trace.py` 生成（aiohttp TestClient + 内存 fake 存储，不 bind 端口、不连真 Mongo、TTS/STT 禁用）。

## 重要：快照性质

每条轨迹是**捕获时点的诚实快照**——`server_info.features` 反映当时的后端能力，不反映当前 HEAD。例如：

- `ws-server-info.jsonl`（批次 1）features 全 false——当时确实全未实现；
- `ws-v2-cancel.jsonl` / `ws-v2-fail.jsonl`（批次 2）`structured_generation:false`——抓取于 `4f27702`（该 flag 置 true）之前，与 `ws-v2-chat.jsonl` 的 `true` 不一致，属轨迹数据质量，非协议正确性；
- `ws-v2-session.jsonl`（批次 3）`desktop_runtime:false`——批次 4 才实现。

**以当前契约/能力为准时，请以 `frontend_contract/` 源 + `docs/electron-backend-protocol-v2.md` 为准；轨迹仅作事件序列与 wire 形状参考。**

## 轨迹清单

| 文件 | 批次 | 内容 | 帧数 | features（捕获时点） |
|---|---|---|---|---|
| `ws-chat.jsonl` | 0 | v1 完整聊天（session→capabilities→…→inner_voice→delta→message×4→idle） | 16 | 无 server_info（v1） |
| `ws-cancel.jsonl` | 0 | v1 取消（第一句 delta→interrupted→idle） | 15 | 无 server_info（v1） |
| `ws-tool-approval.jsonl` | 0 | v1 Backend 工具审批（executing→tool_approval→tool_done→idle） | 24 | 无 server_info（v1） |
| `ws-server-info.jsonl` | 1 | v2 握手（session→server_info→expression） | 3 | 全 false |
| `ws-v2-chat.jsonl` | 2 | v2 结构化聊天（ack→started→inner_voice+4 段→completed） | 22 | structured_generation=true |
| `ws-v2-cancel.jsonl` | 2 | v2 取消（→generation.failed{CANCELLED}） | 14 | 全 false（4f27702 前） |
| `ws-v2-fail.jsonl` | 2 | v2 失败（→generation.failed{PROVIDER_UNAVAILABLE}） | 12 | 全 false（4f27702 前） |
| `ws-v2-session.jsonl` | 3 | v2 多会话（activate_session 切隔离会话→generation.* conversationId 贯通） | 23 | structured_generation + multi_conversation=true |

## 数据保护

- 全部内容为测试占位文本/测试 ID（`conv_trace_x`、`msg_/gen_/seg_` 测试前缀），session_id="default" 或隔离测试会话，无真实聊天正文、密钥、Token 或个人路径。
- 生成脚本对 `default` 会话只读、绝不 delete/clear（批次 3 纪律）。
