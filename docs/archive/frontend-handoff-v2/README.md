> ⚠️ 已归档（2026-08-09）：交接包历史快照。当前契约为 frontend_contract 单源 + [docs/electron-backend-protocol-v2.md](../../electron-backend-protocol-v2.md)。轨迹仅作 wire 形状参考。

# Electron 后端交接包（frontend-handoff-v2）

**定位：** 后端 → Electron 前端交接快照。批次 0-6 的真实 WS 轨迹与捕获时点说明见 `traces/`（`traces/README.md`）；本文件为**批次 7 交付说明**（Commit / 能力矩阵 / 契约摘要 / 测试报告 / 兼容升级 / 数据保护）。

**关联文档：**

- 协议契约：`docs/electron-backend-protocol-v2.md`（v2 事件契约 + §9.1 错误码目录 40 码 + §13 通用附件与媒体）
- 迁移/升级说明：`docs/electron-backend-migration.md`
- 安全基线：`docs/electron-backend-security.md`（§3.5 附件下载与签名安全）
- 批次进度与验收：`docs/electron-backend-progress.md`

---

## 1. 批次 7 新 Commit

**收口 HEAD：** `5da726f`（`fix(batch7): 最终 review`）

**Spec/Plan：** `docs/superpowers/specs/2026-08-06-electron-backend-batch7-media-and-observability.md`（9 轮 review 收口）+ 同目录 plan（4 轮 review 收口）。

批次 7 实施提交（`git log 9782af2..HEAD`）：

| Commit | 内容 |
|---|---|
| `6e0cadf` | 契约层——媒体 Part/附件模型/签名响应/stickerId/build_sha/media_upload/requestId |
| `96dcb4e` | 存储层——AttachmentRepository（fail-closed）+ AttachmentFileResolver + `agent.attachment_repo` 归属 |
| `b688412` | 通用上传 `/api/uploads` + filetype 真值校验 + 签名 URL + 64MB/静态挂载收敛 |
| `35b182d` | 附件签名下载 + Range + 签发端点 `/sign` + fail-closed/fail-open |
| `366cadb` | 内置表情包——sticker_registry + `/api/stickers` |
| `19391f6` | 附件/sticker 消息关联——v2 wire + process bool + refs + 删除联动（含归档）+ 视觉适配 |
| `8fc909b` | 启动 GC 兜底扫描——deleted/孤儿/未引用清理（限四 kind 目录） |
| `5497e49` | 版本——git build_sha（版本串 + build_sha 字段）+ media_upload 如实声明 |
| `158c633` | `/health`/`/ready` 拆分 + 真依赖检查 + 版本/uptime/request_id |
| `38dde3a` | 日志正文打印收敛 + 敏感扫描测试 |
| `59d0927` | 关联 ID——WS 错误带 requestId + GenerationRegistry 记 request_id |
| `e888cbb` | docs(batch7)——协议/安全/进度/任务清单/README/CLAUDE 同步 |
| `5da726f` | 最终 review——文本文件上传（I6）+ chunked 聚合 64MB 强制 + fail-open 警告 + 下载 register 前置文件检查 |

## 2. 能力矩阵（`server_info.features` 全量，批次 7 时点）

`ServerInfoMessage`（v2 握手帧）features **如实声明**——每个字段反映后端当前真实能力：

| feature | 值 | 实现批次 |
|---|---|---|
| `structured_generation` | `true` | 2 |
| `multi_conversation` | `true` | 3 |
| `desktop_runtime` | `true` | 4 |
| `dictation` | `true` | 6 |
| `voice_recording` | `true` | 6 |
| `voice_realtime` | `true` | 6 |
| `media_upload` | `true` | **7**（`POST /api/uploads`） |

版本字段：`server_version` = `0.1.0+<build_sha[:7]>`（`core/version.py` `build_server_version()`，无 git 时仅 `0.1.0`）；`build_sha` = git HEAD 完整 SHA（`get_build_sha()`）。`services`（llm/stt/tts）复用 capabilities 同款 per-service 形状（available/enabled/model/default）。

## 3. 契约摘要（批次 7 新增）

### 3.1 REST 端点

| 端点 | 认证 | 说明 |
|---|---|---|
| `POST /api/uploads` | Bearer | 通用附件上传（multipart `files[]` + 可选 `kind`，服务端校验序为权威）→ `AttachmentUploadResponse{attachments: AttachmentInfo[]}` |
| `GET /api/attachments/{attachment_id}` | **免 Bearer**（签名即授权） | 签名下载：`?sig=&exp=` 校验序（sig hex → 记录 active → exp 窗口 → HMAC 验签）→ aiohttp `FileResponse`（原生 Range/206） |
| `GET /api/attachments/{attachment_id}/sign` | Bearer | 签发端点 → `AttachmentSignResponse{attachmentId, url, expiresAt(ISO UTC)}`（HMAC 24h 短签） |
| `GET /api/stickers` | Bearer | 内置表情列表 → `StickerListResponse{stickers: StickerEntry[]}`（sticker = 公共静态资源 `/static/stickers/{id}.png`） |

- `AttachmentInfo{attachmentId(att_), kind, mime, size, url(/api/attachments/{id}, 稳定不随签名过期), signedUrl?, filename}`；`signedUrl` 在 AGENT_ACCESS_TOKEN 配置而 ATTACHMENT_SIG_SECRET 未配置时 **fail-closed 省略**。
- 附件为**服务端托管资源**：字节落盘 `data/uploads/{kind}/{uuid}.{ext}` + 元数据落 MongoDB `attachments` 集合（fail-closed——Mongo 离线 503 `STORAGE_UNAVAILABLE`，上传已落盘文件删除不留孤儿）。
- MIME/编码矩阵（filetype 魔数真值判定，不信任扩展名/Content-Type；扩展名一致性校验堵改名绕过）：

| kind | MIME | 允许扩展名 | 大小上限 |
|---|---|---|---|
| `image` | png / jpeg / gif / webp | png / jpg·jpeg / gif / webp | 10MB |
| `video` | mp4 / webm | mp4 / webm | 50MB |
| `audio` | wav / mp3 / ogg | wav / mp3 / ogg | 20MB |
| `file` | pdf / zip | pdf / zip | 20MB |

- 文本类（txt/md/json）无魔数 → 扩展名白名单 + UTF-8 内容 sanity；整体聚合上限 **64MB**；单次 ≤10 文件；`kind=sticker` 拒绝走上传（sticker 非附件）。
- 旧 `/api/images`（legacy `/uploads/images/`）与批次 4 `/uploads/desktop/` 为遗留链路，不纳入附件体系（不签名、不 GC）。

### 3.2 WS 契约（消息关联，protocol-v2 §13.7）

- `V2TextMessage` 增可选 **`attachments: AttachmentRef[]`**（`AttachmentRef{attachmentId, kind, mime}`，kind/mime 为客户端提示值）+ **`stickerId: string`**（st_ 前缀）；附件/sticker-only 消息触发生成（content 可空）。
- `ContentPart` 判别联合 6 类：text/image/file/audio/video/sticker；Image/File/Audio/Video Part 均带 **`attachment_id`**；`StickerPart{sticker_id, src=/static/stickers/{id}.png}`。
- 删除联动：消息删除 `remove_ref` → refs 归零软删（`status=deleted`），签名 URL 对软删附件 404；GC 延后物理删。
- `ErrorMessage.requestId`（WS 侧请求 ID，P7-12，可缺省）。

### 3.3 新增错误码（批次 7，目录累计 32 → 40）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `ATTACHMENT_NOT_FOUND` | 404 | false | 附件不存在或已软删（下载/签发） |
| `SIGNATURE_EXPIRED` | 403 | false | 签名 exp 非整数或不在 [now, now+24h] 窗口 |
| `INVALID_SIGNATURE` | 403 | false | sig 缺失/非法 hex / HMAC 不匹配 |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | false | filetype 魔数无法判定 / 不在白名单 / 扩展名与内容不一致 |
| `TOO_MANY_FILES` | 400 | false | 单次上传文件数 > 10 |
| `ATTACHMENT_SIGNATURE_DISABLED` | 403 | false | fail-closed：AGENT_ACCESS_TOKEN 配置而 ATTACHMENT_SIG_SECRET 未配置 |
| `STORAGE_UNAVAILABLE` | 503 | true | 附件存储 Mongo 离线（上传/下载/签发 fail-closed） |
| `QUEUE_FULL` | 429 | true | Agent 忙队列满 → `user_message.ack{status:rejected}`（WS ACK，无 HTTP 信封） |

完整错误码目录（40 码）见 `docs/electron-backend-protocol-v2.md §9.1`。

## 4. 测试报告

- **批次 7 针对性测试：106 passed**（2026-08-06 实测，14.05s）：`core/input/tests/test_upload.py`、`test_attachment_download.py`、`test_attachment_message.py`、`test_stickers.py`、`test_health.py`、`test_log_sensitivity.py`、`test_request_id.py`、`core/storage/tests/test_attachment_repo.py`、`test_attachment_gc.py`、`core/tests/test_build_info.py`、`frontend_contract/tests/test_batch7_contract.py`。
- **全量 `test-normal`**（`-m "not slow"`，实测 2026-08-06，123.7s）：**2698 passed / 2 skipped / 39 deselected**。
- 门禁：`make lint` 通过；`make check-types` 无漂移（`contract.generated.ts` 纯增量，v1 类型字节不变）。

## 5. 兼容与升级说明

- **旧 `/api/images` 保留**：legacy 图片链路与附件体系并存，图片迁移由前端切换驱动；未切换期间旧图链路安全由全局 Bearer 维持（不在签名/GC 保护内）。
- **`/uploads/` 静态挂载收敛**：只公开 legacy `images/` 与 `desktop/` 子目录；新 kind 单数目录 `data/uploads/{image,video,audio,file}` 不静态暴露——只能经 `/api/attachments/{id}` 签名下载（新附件不产生永久匿名 URL）。
- **`max_request_bytes` 8 → 64MB**：`ServerConfig.max_request_bytes` 默认由 8MB 提升到 64MB（通用上传聚合体 + 既有 10MB 图片），超限 413 `REQUEST_TOO_LARGE`。
- **新依赖 `filetype`**：`filetype>=1.2` 加入 `requirements.txt`（上传魔数真值判定）。
- **`ATTACHMENT_SIG_SECRET` 新增**（`secret=True`，仅环境变量 `ATTACHMENT_SIG_SECRET`，不落 YAML/git）：fail-closed 决策见 security §3.5——token 配 + sig 未配 → 上传省略 `signedUrl`、下载/签发一律 403。
- **`/health`/`/ready` 拆分**：`/health`（liveness，恒 200，status ok/degraded）+ `/ready`（readiness，依赖不 ok → 503）；探针负载含真依赖检查（mongo/milvus，2s 超时）。
- 升级动作详情见 `docs/electron-backend-migration.md`。

## 6. 数据保护

本包不含 `.env`、密钥、真实聊天、个人路径、数据库、上传原文件或生产日志。批次 7 新增契约字段/错误码均为测试占位值；`traces/` 轨迹为测试 ID 占位文本（`conv_trace_x`、`msg_/gen_/seg_` 测试前缀），见 `traces/README.md`。
