# Electron v2 Windows 第三次真实联调结果

**日期：** 2026-08-14（Asia/Shanghai）

**结论：** 前端压缩修复后已完成 Windows Electron 真机复验，B-01 十项全部通过。**B-01 已封口，阶段 3 可转为主线。**

本报告不含 Token、API key、数据库密码或完整历史消息。

## 1. 冻结基线与拓扑

| 项 | 实际值 |
|---|---|
| 后端 SHA | `73ac7a32415aa96bbec7687ae0c9e674ab675438` |
| Electron 前端 SHA | `6b27e2e1ebf4e6ba3e335352379892e7c2679da2` |
| schemaHash | `e5d4dd2b9296b727` |
| 后端版本 | `0.1.0+73ac7a3` |
| Electron / Chrome | `43.3.0` / `150.0.7871.212` |
| 运行模式 | `REAL`，protocol `2.0` |
| 会话 | `conv_c658c8dc907f` / `B-01 第三次真实复跑 2026-08-14` |

后端仅监听 Ubuntu 回环 `127.0.0.1:8765`；Windows Electron 经 SSH 本地转发使用回环 Endpoint。CDP 仅在联调期间通过双端回环反向转发到 Ubuntu `127.0.0.1:19223`。

联调前 `/health` 与 `/ready` 均 HTTP 200，`chatReady=true`、`fullReady=true`，Mongo/Milvus/idempotency 均就绪。

证据继承口径：B-01 #1–#9 的首次第三轮证据来自前端父提交 `05f5af0`；`6b27e2e` 仅修改默认 WS 压缩选项、增加对应回归和报告。按本报告原定的修复后范围，已在 `6b27e2e` 上重跑受影响的空闲文本（#3）、Renderer reload（#5）、取消（#8）和全轮诊断（#10）；工具/replay 身份与存量历史项因 mapper、schema 和后端数据均未变，沿用同一轮原始证据。最终验收基线为 `6b27e2e + 73ac7a3`。

## 2. B-01 十项结果

| # | 结果 | 关键证据 |
|---:|---|---|
| 1 | 通过 | 双仓 SHA、运行时 `build_sha`、schemaHash 一致 |
| 2 | 通过 | Windows Electron 显示 `REAL` / `2.0` / `0.1.0+73ac7a3`；修复后 `connectionId=conn_240807de609a` |
| 3 | 通过 | 纯文本 ACK、流式、UI、Pinia 与 REST 身份一致 |
| 4 | 通过 | 连续两次真实 `get_time`，两组 call/message/timestamp 非空且不同 |
| 5 | 通过 | 原始回归及修复后静默 `76s` reload 均保持 Main `connectionId`；复验 REST 13 条 = DOM 13 行 |
| 6 | 通过 | UI 断开/重连后收到 10 条 `session replay`；两条 tool 的 replay/REST 三字段完全一致 |
| 7 | 通过 | Store 10 个唯一 ID，无 `<conversationId>:replay`，DOM 行数与 Store 一致 |
| 8 | 通过 | 原始回归保留 28 字 partial；修复后再验保留 62 字 partial，均为 `GENERATION_CANCELLED` → `interrupted`，无迟到 completed |
| 9 | 通过 | 旧 `default` 最近 50 条可读，50 个 ID 非空无重复，时间戳完整，`hasMore=true` |
| 10 | **通过** | 修复后仅 1 条 WS，0 断线、0 warning/error、0 RSV 错误；Renderer console/page error=0，`droppedEvents=0` |

### 2.1 纯文本身份

```text
clientRequestId = 3c5f81ae-b488-408f-8ce9-4ce36e027655
user messageId  = msg_7997f0b4df884e57ad21f0b189
generationId    = gen_813721d7e85e4d6b823e512fac
assistant ID    = msg_081846650ef5475bb6c7fa9df8
reply match     = true
```

### 2.2 工具身份

```text
tool #1
message_id   = msg_73400e7d4ccf4113af4dca6ead
timestamp    = 2026-08-13T18:39:22.623280+00:00
tool_call_id = call_00_SZ3JOlyYq99j7AhOBC9n9424

tool #2
message_id   = msg_1ab59f48644043f28ffafb849b
timestamp    = 2026-08-13T18:39:46.785030+00:00
tool_call_id = call_00_j7NJ7rjdEtdRUBFfVNWQ1060
```

两条数据在原始 WS `session` 帧、Electron replay 领域消息、REST 响应和 Pinia Store 中精确一致。

### 2.3 取消身份

```text
clientRequestId = cefcbcbc-aee8-46c0-b9b0-fbb71e6f066b
user messageId  = msg_731408eae4834a01b993d51fd1
generationId    = gen_b4eb3b79c70e468eabe6e6e13b
assistant ID    = msg_990931856c42427eb27f78520c
terminal code   = GENERATION_CANCELLED
partial length  = 28
```

## 3. 已关闭阻断：WS `permessage-deflate` 空闲后帧错误

### 3.1 Electron 真实证据

真实 Windows Electron 连接在静默一段时间后，第一条客户端业务帧（`text` 或 Renderer 恢复发出的 `activate_session`）触发：

```text
02:42:28.227 WS error: Received frame with non-zero reserved bits
02:44:18.593 WS error: Received frame with non-zero reserved bits
02:47:50.461 WS error: Received frame with non-zero reserved bits
```

后端立即关闭连接，Electron 随后自动重连。若失败帧是 `text`，用户消息没有进入后端，前端 15s 后显示 `user_message.ack 超时`。

### 3.2 Ubuntu 本机 A/B 隔离复现

为排除 Windows、SSH/CDP 和 UI，使用与 Electron 相同的 `ws` 客户端在 Ubuntu 直连 `127.0.0.1:8765`。两个探针建连后静默 75s，再发送无副作用的 `activate_session(default)`：

| 探针 | 握手扩展 | 初始 session | 发送后 session | 关闭码 |
|---|---|---:|---:|---:|
| 默认压缩 | `permessage-deflate` | 1 | 0 | `1002` |
| `perMessageDeflate:false` | 无 | 1 | 1 | `1000` |

后端在压缩探针发送时同步记录：

```text
02:51:23.681 WS error: Received frame with non-zero reserved bits
```

因此可排除 SSH 或 Windows 传输破坏。当前可确认的边界是：**Node `ws` 客户端与 aiohttp `WebSocketResponse(compress=True)` 协商 `permessage-deflate` 后，空闲连接的后续客户端帧出现兼容失败。** 尚未将责任下沉到某个上游库的具体实现行。

## 4. 实际修复与复验

1. 已在前端 `6b27e2e1ebf4e6ba3e335352379892e7c2679da2` 中将 Electron `RealBackend` 默认真实 `ws` 连接设为 `perMessageDeflate:false`，不改测试注入契约。
2. 已增加真实本地 `WebSocketServer` 握手回归，断言不发送 `Sec-WebSocket-Extensions`。
3. 已重跑静默 `76s` 后文本、静默 `76s` 后 Renderer reload / `activate_session`、B-01 #8 取消与 #10 全轮诊断。
4. 后端 `WebSocketResponse(..., compress=False)` 仍可作为纵深防护评估，但不再阻断 B-01。
5. 复验已全绿，README/runbook 可回填“B-01 已通过”。

`ws` 官方说明也明确记录：客户端默认开启 `permessage-deflate`，不需要时可通过 `perMessageDeflate:false` 显式关闭：<https://github.com/websockets/ws/blob/master/README.md#websocket-compression>

## 5. 修复后 Windows Electron 复验证据

### 5.1 空闲文本

- 基线：03:15:54，`connectionId=conn_240807de609a`，活动会话 `conv_c658c8dc907f`。
- 03:17:10（静默 `76s`）从真实 UI 输入框发送，连接 ID 不变，没有 reconnect/error state。
- user message=`msg_8ebf7bd7321c43f78d28a3752e`，generation=`gen_af19f970902c47cd8a013beff1`，assistant=`msg_44de6ad927754b9b973f7f353e`；ACK、completed、REST 落库均成功。
- UI 无失败标记/错误横幅，Renderer console/page error 为 0。

### 5.2 空闲 Renderer reload

- 基线：03:18:26，同一 `connectionId=conn_240807de609a`。
- 03:19:42（静默 `76s`）reload Renderer，Main WS 没有重建；reload 后仍为同一 connectionId 和活动会话。
- REST `13` 条、唯一 ID `13`、DOM `13` 行；空闲文本已水合，无 streaming/失败/连接横幅。
- 后端 03:19:45 收到正常 `activate_session`，未出现 RSV 错误或断线。

### 5.3 取消

```text
user messageId  = msg_8608dcc48dca4c89895c412deb
generationId    = gen_d2c2d545666c43b7a214fbbabd
assistant ID    = msg_b134619e8cbc4cb58b406bf17f
terminal code   = GENERATION_CANCELLED
UI status       = interrupted
partial length  = 62
late completed  = false
```

partial 位于默认折叠的 inner-voice 分段，已在 DOM `textContent` 中确认保留。取消后无失败/连接横幅，无迟到 completed。

### 5.4 全轮诊断

- 修复后时间窗只有 1 条 Windows Electron WS 连接，0 次断线、0 条 warning/error、0 次 `Received frame with non-zero reserved bits`。
- 最后一条 RSV 错误仍是修复前隔离探针的 `02:51:23.681`。
- Pinia message order `19`，唯一 ID `19`，无缺失 key，DOM 也为 `19` 行，`droppedEvents=0`。
- 最终连接状态 `connected`，无 active generation、无 `lastError`；`/health` 与 `/ready` 仍为 HTTP 200。

### 5.5 自动化门禁

- 定向 `RealBackend` 用例：`30/30`。
- 全量单测：`527/527`（47 files）。
- Electron 集成：`23/23`（6 files）。
- typecheck、lint、production build：通过。
- 跨仓 contract：WS v2 `13/13`，字段级帧 `63` 条完整保留。

## 6. 最终判定

B-01 #1–#10 均已按当前双仓 SHA 取得真实 Windows Electron 证据。阶段 2 主链可标注为“已真实联调”，下一主线转为阶段 3。
