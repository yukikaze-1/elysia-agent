# Electron v2 前端接入前后端充分性复审

> 日期：2026-08-10
> 后端仓库：`/home/yomu/elysia-agent`
> 前端仓库（只读对照）：`/home/yomu/elysia-frontend`
> 审查基线：后端 `9fdbfc1`
> 状态：R1-R3 已修复并通过回归；R4-R5 保留为产品化门禁

## 1. 结论

后端已经具备 Electron 核心联调所需的主链：WS v2 握手与认证、结构化生成、
多会话 REST、历史/搜索、附件、模型 Profile、语音以及 Desktop 工具协议均已落地。
本次又关闭了 Desktop 重连交叠误投，并把取消/失败 golden 与公开文档更新到当前契约。
结论是：**后端已足够支撑 Electron 单机真实联调和完整本地 Demo**；这不代表已经满足
非回环远程生产发布条件，R4-R5 仍须在产品化前单独完成。

Electron 当前仍处于安全壳与 Mock 阶段：`BackendAdapter` 只有接口和
`MockBackend` 实现，尚无真实 REST/WS Adapter。因此“后端具备接入条件”不等于
“跨仓端到端已经验证”。前端需要在 Main 进程完成 raw `type/delta/mood` 到领域
`event/text/emotion` 的映射，Renderer 不应直接消费后端 raw 帧。

## 2. 分阶段可接入判断

| 范围 | 判断 | 前提/剩余项 |
|---|---|---|
| 阶段 2：新建会话、发送、结构化流式回复 | 可开始 | 使用 WS v2 精确协商；按 ACK/generation 双 ID 归一 |
| 阶段 3：历史、搜索、附件、多媒体 | 可开始 | MongoDB 可用；分页 cursor 只作 opaque 值回传 |
| 阶段 4：Profile、录音、实时语音、Dictation | 可开始 | 按 `server_info.services` 降级；输入固定 16k/mono/pcm_s16le |
| 阶段 6：Desktop P0 工具 | 可开始 | 已按 call 登记的 `connectionId` 精确投递，并覆盖双 WS 交叠测试 |
| 远程/产品化发布 | 尚未完成 | 非回环鉴权门禁、持久化幂等与实际限流仍在 `todo.md` |

## 3. Findings 与处置

### R1（P1）：Desktop 工具请求可能发给旧 Electron 连接

`DesktopToolBridge.on_client_connect()` 采用 last-writer 语义，把最新连接记为
`_connection_id`；`request_and_wait()` 也把这个 ID 写入 `target_connection`。但
Agent 的 `_deliver_tool_request()` 只取 `_channels` 中第一个
`client_platform == "electron"` 的通道，没有按 `connection_id` 匹配。

新旧连接在重连窗口同时存在时，已稳定复现：

```text
bridge_target: conn_new
old_received: 1
new_received: 0
```

旧客户端可能已经执行本地副作用，但其 `tool.status` 又会因发送连接不是登记目标而
被拒绝；新客户端没有收到请求，调用最终超时。修复要求：Bridge 投递回调显式携带
目标连接 ID，Agent 只向 `client_platform=electron && connection_id=target` 的通道
发送；找不到精确目标必须让 Bridge 立即走 `DESKTOP_UNAVAILABLE`，不能静默丢帧后
等待 120 秒。

### R2（P2）：取消/失败路径没有当前契约生成的 golden frames

`scripts/gen_contract_frames.py` 只生成 handshake/session/chat/tool/voice 五类文件。
归档中的 `ws-v2-cancel.jsonl`、`ws-v2-fail.jsonl` 是批次 2 历史快照，仍含旧
`token_stats.by_model`、缺少 `conversationId`，且 `server_info.features` 全 false。
README 虽标为历史，协议正文仍把它们列作“真实轨迹”并指向已停用的捕获脚本。

修复要求：新增当前契约模型生成的 cancel/fail golden，把两文件纳入 `--check` 和
“所有 generation 帧必须有 conversationId”的测试，并将协议正文改为明确的
“契约源 golden + 历史快照”说明。

### R3（P2）：公开文档与当前实现漂移

需同步修订以下事实：

1. 错误码目录漏记运行时已发送的 `STT_PROFILE_NOT_FOUND`；
2. MIME 矩阵漏记实际支持的 `txt/md/json`；
3. 安全文档仍称未知显式会话会 lazy-create，实际已在 ACK 前拒绝；
4. 协议正文仍记录旧附件签名校验顺序，而实现已先校验 sig/exp/HMAC、后查记录；
5. `ToolRequestMessage` 注释仍描述“任取 Electron 通道”，应改为精确目标连接。

### R4（上线门禁）：非回环认证默认仍是 fail-open 告警

默认 `server.host=0.0.0.0`，未配置产品/管理 token 时只输出显著日志而不拒绝启动。
Electron 同机联调应绑定 `127.0.0.1`；远程发布前应实现 `ALLOW_INSECURE_REMOTE`
显式 opt-in 或等价 fail-closed 门禁。该项已在 `todo.md` 登记，本轮不混入协议/工具
兼容修复，以免无迁移说明地改变既有本地和局域网启动行为。

### R5（产品化门禁）：幂等跨重启与速率限制

`clientRequestId` 表仍是进程内一小时 TTL，后端重启后无法阻止 ACK 丢失重试造成的
重复生成；`RATE_LIMITED` 只有目录项，没有限流器。这两项不阻塞单机 Demo 联调，
但不能据此宣称生产级 exactly-once 或抗滥用能力。保持在 `todo.md`，需独立设计存储
唯一约束、过期回收、代理 IP 信任边界与限流粒度后实施。

## 4. 本批修复范围

本批已直接关闭 R1、R2、R3，并补相应单元/集成/契约漂移测试。R4、R5 保持为明确的
产品化门禁，不伪装为已解决。

## 5. 接入运行条件

- Electron WS 请求头：`Authorization: Bearer ...`（启用 token 时）、
  `X-Protocol-Version: 2`、`X-Client-Platform: electron`；
- 完整能力联调以 `GET /ready` 返回 200 为准；`/health` 恒 200 只代表进程存活；
- MongoDB 离线时文本可降级，但永久历史/搜索/附件不满足完整产品语义；
- STT/TTS 是可选服务，前端必须读取 `server_info.services`，不可凭配置页面状态猜测；
- 当前 raw schema、生成 TS 与 golden 是后端事实源，前端领域 Schema 是 Adapter 输出，
  两者不可直接等同。

## 6. 实际修复

### 6.1 Desktop 精确连接投递（R1）

- `DesktopToolBridge` 的投递回调由 `deliver(frame)` 改为
  `deliver(target_connection, frame)`，使用创建 call 时快照的目标 ID；
- Agent 只向同时满足 `client_platform == "electron"` 和
  `connection_id == target_connection` 的通道发送 `tool.request`；
- 精确目标为空或已经消失时，投递回调抛错，由 Bridge 立即落 call failed，并返回
  `ToolResult(UNAVAILABLE)`；不会广播给其它 Electron，也不会静默等待 120 秒；
- 新增 callback 单测、Bridge 快速失败单测和真实 aiohttp 双 Electron WS 交叠 service
  测试。该修复不改变 `tool.request/tool.status` 的 wire 字段。

### 6.2 当前契约 cancel/fail golden（R2）

- `scripts/gen_contract_frames.py` 现在确定性生成七类文件：handshake、session、chat、
  cancel、fail、tool、voice；七类都进入 `--check` 漂移门禁；
- 当前 cancel/fail 帧使用完整 `server_info.features`、camelCase `byModel`、
  `conversationId`、`generation.started.model` 和唯一 generation 失败终态；
- 原 batch-2 文件分别保留为 `ws-v2-cancel-legacy-batch2.jsonl` 与
  `ws-v2-fail-legacy-batch2.jsonl`，避免覆盖历史证据；trace README 已区分当前 golden
  与历史快照。

### 6.3 契约与公开文档同步（R3）

- 协议补齐 `STT_PROFILE_NOT_FOUND`（错误码目录现为 41 项）、`txt/md/json` 上传类型、
  当前附件签名校验顺序及七类 golden 的事实源说明；
- 安全文档改为显式会话存在性校验现状，并记录 Desktop 精确目标/快速失败语义；
- `ToolRequestMessage` 描述同步后重新生成 JSON Schema、raw TS 与旧 Vue TS 快照。
  这使 `schemaHash` 更新，但 **wire 字段没有变化**；Electron 同步产物时应使用本次
  新生成版本，不能仅复制旧 hash。

## 7. 验证结果

以下命令均在 `/home/yomu/elysia-agent`、Elysia Python 环境执行通过：

| 门禁 | 结果 |
|---|---|
| Ruff（`core/`、`Module/`、`frontend_contract/`） | 通过 |
| `python -m frontend_contract --check` | 通过，无 TS/Schema 漂移 |
| `python scripts/gen_contract_frames.py --check` | 通过，无 golden 漂移 |
| Electron/契约/附件/语音专项测试 | `278 passed` |
| 全仓 `make test-quick` | `2900 passed, 2 skipped, 95 deselected` |
| Pytest 完整收集 | `3290 tests collected` |
| `make pyright` | `0 errors`；1 条既有 `IdleMonitor` `__all__` warning |
| `git diff --check` | 通过 |

测试仅出现仓库既有的 `webrtcvad/pkg_resources` 与部分 AsyncMock 未 await warning，本批
没有新增测试失败或类型错误。

## 8. 最终接入边界

- **现在可以做**：Electron Main 进程真实 REST/WS Adapter、文本/会话/附件/Profile/
  语音/Desktop 的单机端到端联调；
- **仍需前端完成**：raw contract 校验与 raw→domain mapper、ACK/generation ID 归一、
  golden replay，以及 Desktop `ToolResult` 领域模型扩展；
- **仍不能宣称**：公网或非可信局域网生产就绪、跨重启 exactly-once、应用层抗滥用。
  对应待办为非回环鉴权 fail-closed、幂等持久化和实际请求限流。
