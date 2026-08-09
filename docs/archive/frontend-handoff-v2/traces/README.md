> ⚠️ 已归档（2026-08-09）：轨迹参考，仅作历史 wire 形状记录。

# Electron 后端 WS 轨迹（golden frames + 历史快照）

本目录 JSONL 分两类：

1. **契约源 golden frames**（`ws-v2-*.jsonl`，Task 4/9B-1 起）：由
   `scripts/gen_contract_frames.py` 从 `frontend_contract/` 契约模型（`ServerMessage`
   判别联合）确定性生成（固定测试 ID/时间戳），文件头 `#` 注释标注「非事实源，漂移门禁
   兜底」。`make check-types` 对它们做 `git diff` 漂移检测——改契约不重跑 gen-frames = CI 红。
2. **历史真实快照**（`ws-chat/cancel/tool-approval` + `ws-server-info` +
   `ws-v2-cancel/fail`）：批次 0~2 捕获时点的真实脱敏 WS 轨迹，由已停用的
   `scripts/capture_ws_trace.py` 生成（DEPRECATED），保留作历史参考。

## 重要：快照性质

真实轨迹是**捕获时点的诚实快照**——`server_info.features` 反映当时的后端能力，不反映
当前 HEAD。例如 `ws-v2-cancel.jsonl` / `ws-v2-fail.jsonl`（批次 2）`structured_generation:false`
——抓取于 4f27702 之前。golden frames 则由当前契约能力声明（features 全 true）。

**以当前契约/能力为准时，请以 `frontend_contract/` 源 + `docs/electron-backend-protocol-v2.md`
为准；轨迹仅作事件序列与 wire 形状参考。**

## 轨迹清单

| 文件 | 来源 | 内容 | 帧数 |
|---|---|---|---|
| `ws-v2-handshake.jsonl` | 契约源 golden | v2 握手（session→server_info→expression） | 3 |
| `ws-v2-session.jsonl` | 契约源 golden | v2 多会话（activate 回放 conversationId 贯通→generation.*） | 10 |
| `ws-v2-chat.jsonl` | 契约源 golden | v2 结构化聊天（ack→generation.* 完整序列→idle） | 13 |
| `ws-v2-tool.jsonl` | 契约源 golden | 桌面工具（tool.request→tool_approval→tool_calls 完成） | 4 |
| `ws-v2-voice.jsonl` | 契约源 golden | 实时语音（started→partial→turn→ack→audio→ended） | 7 |
| `ws-chat.jsonl` | 历史真实 | v1 完整聊天 | 16 |
| `ws-cancel.jsonl` | 历史真实 | v1 取消 | 15 |
| `ws-tool-approval.jsonl` | 历史真实 | v1 工具审批 | 24 |
| `ws-server-info.jsonl` | 历史真实 | v2 握手（全 false 时点） | 3 |
| `ws-v2-cancel.jsonl` | 历史真实 | v2 取消 | 14 |
| `ws-v2-fail.jsonl` | 历史真实 | v2 故障 | 12 |

## 再生成 / 漂移检测

```bash
python scripts/gen_contract_frames.py          # 重写 5 个 golden frames
python scripts/gen_contract_frames.py --check  # 漂移检测（CI，make check-types）
```

## 数据保护

- 全部内容为测试占位文本/测试 ID（`msg_/gen_/seg_/call_/vs_/req_` 前缀），
  session_id="default" 或隔离测试会话，无真实聊天正文、密钥、Token 或个人路径。
- golden frames 无网络、不连 Mongo，纯契约模型构造（确定性）。
