> ⚠️ 已归档（2026-08-09）：历史记录，不再随代码库更新。当前有效文档见 [../README.md](../README.md)。

# Electron 后端迁移 / 升级说明（v1 → v2）

**文档版本：** v0.1（批次 7 产出）
**关联：** `docs/electron-backend-baseline.md`（v1 现状基线）、`docs/electron-backend-protocol-v2.md`（v2 事件契约 + 错误码目录）、`docs/electron-backend-security.md`（认证/签名/敏感纪律）、`docs/electron-backend-progress.md`（批次 0-7 进度与验收）

本文档面向 Electron 前端联调方与部署方：后端协议从 v1 演进到 v2（批次 0-7 累计），以及批次 7 的具体升级动作。

> **兼容硬约束：** v1 路径**逐字节不变**——不升级前端的部署依然可用，只是停留在 v1 能力集（无版本协商/ACK/结构化生成/多会话/媒体上传等）。v2 由 WS 握手头 `X-Protocol-Version: 2` 判定；未带版本头走 v1。

## 1. v1 → v2 协议演进摘要（批次 0-7 累计）

| 批次 | 交付 | v1 现状 | v2 新增 |
|---|---|---|---|
| 0 | 基线测绘 | `session`→`capabilities`→…→`delta`→`message`(空帧分句)→`state:idle`；错误 `❌` 文本；单会话 default | —（仅事实基线 `electron-backend-baseline.md`） |
| 1 | 认证 / 版本协商 / 统一错误 | `/ws`、`/api/*`、`/uploads/*` 匿名 | `AGENT_ACCESS_TOKEN` 产品认证（Bearer）；`X-Protocol-Version: 2` 握手 → `server_info`；`ProtocolError` 错误信封（code/message/retryable/requestId）；`max_ws_connections` / `max_request_bytes` 限制 |
| 2 | 结构化聊天 | delta 无 ID/ACK、完成靠 `state:idle`、错误 `❌` 文本 | `user_message.ack`（正式 messageId + 幂等 clientRequestId）+ `generation.*` 六事件（started/segment.started/delta/segment.completed/completed/failed）+ `msg_`/`gen_`/`seg_` ID 三概念；`generation.completed` 为唯一终态 |
| 3 | 多会话 / 历史 / 搜索 / 资源 | 单活动会话 default | `SessionManager` + `activate_session` + v2 `text` 带 conversationId 路由；REST：`/api/sessions` CRUD/activate、`{id}/messages`（before/after）、`{mid}/context`、`/api/search/messages`、`{id}/resources`、`{id}/generations`、`/api/generations/{gid}`、`{id}/pending`；`GenerationRegistry` 持久化 |
| 4 | Desktop 工具桥 | Backend 沙盒工具 + tool_approval | `tool.request`/`tool.status` 六态；`X-Client-Platform: electron` 握手 + `server_info.connectionId`；`GET /api/tools` + `calls/{callId}` + `/api/uploads/desktop/{callId}`；`desktop.command.run` 不注册 |
| 5 | 模型 Profile / TTS 声音 | providers 明文密钥落盘风险 | `/api/providers` 扩展（runtime/SSRF/Secret Store/`credentialConfigured`）；`/api/tts/voices` + `/api/tts/preview` + WS `set_tts_voice`；`generation.model` 实际模型字段 |
| 6 | 三种语音 + TTS 播放控制 | PCM+VAD 自动提交单一链路 | `voice.record.*`/`voice_session.*`/`dictation.*` + `audio.cancel` + TTS 播放契约（audio_start/audio_cancelled + stream_id playback 语义）；输入法转写与 Agent 完全隔离 |
| 7 | **通用附件与媒体 / 版本与可观测性** | 仅图片上传（`/api/images`） | `POST /api/uploads` 通用上传 + `GET /api/attachments/{id}` 签名下载 + `/{id}/sign` 签发 + `GET /api/stickers` + 附件消息关联 + `build_sha`/`server_version` 后缀 + `/health`/`/ready` 拆分 + 错误码 40 |

**v2 握手**：Electron Main 连 `/ws` 带 `Authorization: Bearer <access_token>`（远程）+ `X-Protocol-Version: 2` + `X-Client-Platform: electron`（批次 4 起）。建连帧：`session`（回放）→ `server_info`（协议版本/能力/服务形状 + connectionId + build_sha）→ `expression`。

## 2. 批次 7 具体变更

### 2.1 新端点

| 端点 | 认证 | 说明 |
|---|---|---|
| `POST /api/uploads` | Bearer | 通用附件上传（`files[]` + 可选 `kind`），服务端校验序为权威（kind 枚举 → 数量 ≤10 → filetype 魔数白名单 → 扩展名一致性 → kind 归位 + 大小复核 → 写盘 + 落记录） |
| `GET /api/attachments/{attachment_id}` | 免 Bearer（签名即授权） | 签名下载：`?sig=&exp=`（HMAC-SHA256(`attachment_id:exp`)，24h）；校验序见 protocol-v2 §13.2 |
| `GET /api/attachments/{attachment_id}/sign` | Bearer | 签发短期签名 URL（渲染层 C-1 签名解析：稳定路径持久化 + 签发端点解析 + 缓存复用） |
| `GET /api/stickers` | Bearer | 内置表情列表（公共静态资源 `/static/stickers/{id}.png`，非附件） |

### 2.2 错误码（批次 1-7 累计 40 码）

批次 7 新增 8 码：`ATTACHMENT_NOT_FOUND`(404) / `SIGNATURE_EXPIRED`(403) / `INVALID_SIGNATURE`(403) / `UNSUPPORTED_MEDIA_TYPE`(415) / `TOO_MANY_FILES`(400) / `ATTACHMENT_SIGNATURE_DISABLED`(403) / `STORAGE_UNAVAILABLE`(503, retryable) / `QUEUE_FULL`(429, retryable，WS ACK rejected)。完整目录见 `docs/electron-backend-protocol-v2.md §9.1`。

### 2.3 Config / 依赖变更

| 项 | 变更 | 说明 |
|---|---|---|
| `max_request_bytes` | 默认 **8MB → 64MB**（`ServerConfig`） | 通用上传聚合体 + 既有 10MB 图片；超限 413 `REQUEST_TOO_LARGE`（app `client_max_size` + payload 中间件 Content-Length 预检 + chunked handler 累计强制） |
| `attachment_sig_secret` | **新增**（env `ATTACHMENT_SIG_SECRET`，`secret=True`） | 附件签名 HMAC 密钥；fail-closed 决策见 security §3.5 |
| `requirements.txt` | **新增** `filetype>=1.2` | 上传魔数真值判定（不信任扩展名/Content-Type） |

### 2.4 行为变更

- **`/uploads/` 静态挂载收敛**：只公开 legacy `images/` 与 `desktop/` 子目录（兼容既有 `/api/images` 与批次 4 桌面截图链路）；新 kind 单数目录 `data/uploads/{image,video,audio,file}` **不静态暴露**——只能经 `/api/attachments/{id}` 签名下载。
- **`/api/attachments/{id}` 免 Bearer 豁免**：唯一免 Bearer 的业务路径（短时签名 URL 即授权）；`/sign` 签发端点保持 Bearer。认证中间件豁免先于 `/api/` 前缀判断，`/sign` 排除。
- **fail-closed 签名**：`AGENT_ACCESS_TOKEN` 配置而 `ATTACHMENT_SIG_SECRET` 未配置 → 上传响应省略 `signedUrl`、下载/签发一律 403 `ATTACHMENT_SIGNATURE_DISABLED`；token 未配置（本地开发）→ fail-open，下载跳过签名仅校验 active。
- **`/health`/`/ready` 拆分**：`/health` = liveness（恒 200，status ok/degraded）；`/ready` = readiness（依赖不 ok → 503）。探针负载含真依赖检查（mongo/milvus，2s 超时）+ fsm/llm/memory/tools/version/build_sha/uptime/request_id。
- **日志收敛**：`ws_handler`/`loop` 正文打印只记长度/ID（不打印聊天正文）；`conversation.log` 明文保留（调试审计通道，文档标注风险）；`ErrorMessage.requestId` + `GenerationRegistry.request_id` 关联。
- **附件 GC**（启动兜底扫描，限四 kind 目录）：软删超 24h 物理删 / active 记录文件缺失软删 / ref_count=0 超 7 天软删 / 磁盘孤儿删文件；Mongo 离线 skipped（附件操作 fail-closed）。

## 3. 部署 / 升级检查清单

**新增部署（批次 7 起）：**

- [ ] `requirements.txt` 安装 `filetype>=1.2`（`pip install -r requirements.txt` 或按依赖文件安装）。
- [ ] 远程部署（绑定非回环）：配 `AGENT_ACCESS_TOKEN`；并配 `ATTACHMENT_SIG_SECRET`（生产密钥，勿入 YAML/git）——否则附件上传返回省略 `signedUrl`、下载/签发 403。
- [ ] 确认 MongoDB / Milvus 连通（附件元数据落 Mongo `attachments` 集合，离线则上传/下载/签发 503 `STORAGE_UNAVAILABLE`）。
- [ ] `config.yaml` 可按需调整 `server.max_request_bytes`（默认已 64MB）；旧配置若显式写了 8MB 需上修。
- [ ] 探针接入：liveness 用 `/health`、readiness 用 `/ready`（Docker/K8s 滚动发布语义）。

**旧前端兼容（未升级 Electron 客户端）：**

- [ ] v1 路径不受影响：无版本头连接走 v1，`/ws`、`/api/images`、`/uploads/images/` 行为不变。
- [ ] 旧 `/api/images` 上传链路保留（不纳入附件签名/GC 体系），图片迁移由前端切换驱动。

**联调注意：**

- [ ] v2 客户端必带 `clientRequestId`（幂等键），缺省 → `user_message.ack{status:rejected, code:CONTRACT_VALIDATION}`。
- [ ] 附件 `<img>/<video>` 渲染：用稳定路径 `/api/attachments/{id}` 持久化，渲染层调 `/sign` 拿短期签名 URL（缓存 + 24h 复用 + 过期重取）。
- [ ] 上传 MIME 白名单以服务端 filetype 判定为准（客户端 kind 仅作 webm 歧义 tiebreaker）。
- [ ] `QUEUE_FULL` 为 WS ACK `rejected`（无 HTTP 信封），retryable=true——队列恢复后重试允许重新进入。
