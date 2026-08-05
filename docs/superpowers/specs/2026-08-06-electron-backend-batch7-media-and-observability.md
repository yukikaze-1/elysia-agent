# Electron 后端批次 7 — 通用附件与媒体链路 + 发布收敛

**Spec 版本：** v0.10（v0.9 基础上修复九轮 review 残留：调用方计数 7 + §5.4 引用修正 + step 0 补 STORAGE_UNAVAILABLE + 豁免措辞 + 竞态记录）  
**文档状态：** 待 review（已九轮通过，可进入 plan）  
**关联任务：** BE-070 通用附件和媒体链路 / BE-071 缩略图、Range 和资源清理 / BE-081 健康、版本和可观察性  
**任务清单：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md` §5 任务表 + §11（BE-070/071/081）  
**协议参考：** `docs/electron-backend-protocol-v2.md`（本 spec 落地后补 §13）  
**基线 Commit：** `2a9ffe1`（批次 6 收口 + CI 迁移修复）

---

## 1. 目标与范围

把当前唯一的「图片上传 `/api/images` → 消息 ImagePart → 静态目录直出」链路，扩展成图片/视频/音频/文件/表情通用的**稳定附件链路**：稳定附件 ID、filetype 真值校验、短期签名下载 URL、Range 播放、引用计数 + 软删 GC；并把健康/版本/可观测性收敛到可追踪、不泄漏敏感数据。

**不做（批次边界）：**
- 服务端缩略图（图片/视频首帧）——第一版媒体直出原图 + Range，前端 CSS/原生 poster 控制尺寸（P7-4）。
- 真流式视频转码 / 多码率 —— `FileResponse` Range（206）即满足「大资源可播放」。
- 自定义表情管理 UI —— 内置表情包 + 稳定 sticker_id，不引入用户上传自定义 sticker（P7-6）。
- `desktop.command.run` 或任何任意代码执行 —— 沿用批次 4 边界。
- 前端 Electron Main 进程代码 —— 本 spec 只定后端协议与行为（契约模型落 `frontend_contract`，前端 composable 属前端仓库）。

**验收目标（docs/15 批次 7 门槛）：** 媒体可以发送、历史加载、分类查看、播放/下载（Range）和跳回来源，匿名访问不可取得资源；多媒体、版本和回归通过。

## 2. 已确认决策（拍板结论）

| ID | 决策 | 结论 |
|---|---|---|
| P7-1 | 附件存储 | **引入独立 MongoDB `attachments` 集合**作为附件唯一事实来源。 |
| P7-2 | 下载鉴权 | **短期签名 URL**（HMAC + 过期）+ **服务端签发侧**（C-1 补齐）：新增 Bearer 保护的 `GET /api/attachments/{id}/sign` 签发端点；上传响应直接带 `signedUrl`；前端渲染层对稳定路径统一调签发端点解析当前签名（缓存 + 过期重取）。`/api/attachments/` 前缀**豁免全局 Bearer 中间件**（签名即授权，见 §5.2）。消息 part 持久化存稳定路径 `/api/attachments/{id}`。 |
| P7-3 | MIME 真值校验 | **引入 `filetype` 纯 Python 依赖**（1.2.0 纯 wheel，dry-run 已验证可装）。白名单 + filetype 魔数 + 扩展名一致三管齐下；文本类（txt/md/json）无魔数，走扩展名 + UTF-8 内容 sanity（I6）。 |
| P7-4 | 缩略图 | **第一版不做服务端缩略图**。媒体直出原图 + `FileResponse` 原生 Range（206）。 |
| P7-5 | 通用上传端点 | **新增通用 `POST /api/uploads`**（multipart、多文件、带 kind），图片迁到新链路；旧 `/api/images` **保留兼容**。 |
| P7-6 | 表情资源 | **内置表情包 + 稳定 ID，非附件 kind（四轮 review 定案）**：服务端预置 PNG 表情（sticker_id）走**公开静态** `/static/stickers/{id}.png`（表情是公共资源）。**sticker 不落 attachments 集合、不 ref_count、不参与 GC**——它是独立内置资源，不是上传附件（避免「StickerPart 走附件校验必 404」的死配置）。发送载体：v2 text 消息加可选 `stickerId` 字段。新增 `StickerPart`（`attachment_id` 可缺省）。 |
| P7-7 | 孤儿 GC | **引用计数 + 启动兜底扫描**：ref_count 归零 → 软删标记；启动扫描限定四个 kind 目录清理（C3，sticker 非附件不入 GC）。 |
| P7-8 | 删除联动 | **软删 + 延后清理**：删除消息/会话只解除引用、标记 deleted，文件由 GC 延后回收。 |
| P7-9 | 版本来源 | **semver + git sha 后缀**：`core/version.py` 单一来源 + `git rev-parse HEAD` 拼 build 后缀。 |
| P7-10 | /health 扩展 | **真依赖检查 + 版本**：status 派生、mongo/milvus 布尔、version/build/uptime/requestId；`/ready` 独立 handler 用真依赖（依赖不 ok → 503）。 |
| P7-11 | 日志敏感收敛 | **收敛既有的正文打印**：`ws_handler.py:538` / `loop.py:450` 只记长度/ID；`conversation.log` 保留为调试审计通道；新增「日志敏感扫描」测试。 |
| P7-12 | 关联 ID | **补关联不统一（I6/M6 修正）**：WS `ErrorMessage` 加 `requestId`；`GenerationRegistry` 记 request_id；REST 信封维持 requestId（不强行关联日志侧——`next_request_id` 仅 WS turn 有效，REST 侧无对应日志 id，放弃不可行的 logRequestId）。 |

**实现细节决策（spec 内定，按推荐选，记录在案）：**
- 签名 URL 有效期 **24h**，`exp` 校验整数 + 夹紧上限 24h（M5）；前端渲染层懒刷新。
- 上传单次上限：图片 10MB（沿用 `image_max_mb`）、视频 50MB、音频/文件 20MB，数量 ≤10/次。
- **File/Audio/Video/Sticker Part 不喂 LLM**（`content_to_openai` 维持只处理 text/image；File/Audio/Video/Sticker 仅作消息展示引用）。图片喂 LLM 的路径需适配新附件（I4，§4.5）。
- `POST /api/uploads` 上传不强制绑定消息（ref_count=0 起步，**active** 等待关联）；发消息时附件 part 携带 `attachment_id` 关联并 ref_count+1。
- `_sign_attachment_url` 用 stdlib `hmac`/`hashlib`，不引新依赖。
- 附件磁盘布局 `data/uploads/{kind}/{uuid}.{ext}`；kind 不信任客户端，由 filetype 判定 + 白名单推导（§4.1）。
- **`max_request_bytes` 默认 8MB → 64MB**（修复既有 10MB 图片被 8MB 截住的隐患，I3），上传 handler 内自限（§4.1）。

## 3. 附件数据模型

### 3.1 MongoDB `attachments` 集合

新增 `core/storage/attachment_repo.py`（`AttachmentRepository`，仿 `GenerationRepository` 模式）。**与 GenerationRepository 的关键差异（I10 + I-4.3 + Minor 6）：Mongo 未连接（离线）时附件相关操作统一 fail-closed**——写路径（上传落记录）503 `STORAGE_UNAVAILABLE`；**读路径同样 fail-closed**：下载校验（§3.3 查记录）、**签发端点（§3.3 查记录，离线 503 `STORAGE_UNAVAILABLE`）**、发送时附件校验（§4.4）、资源索引软删过滤、GC 启动扫描——读操作在离线时返回 503（下载/签发/发送）或跳过并记日志（GC 扫描），不做静默 no-op。附件上传写盘成功但无记录会造成「文件孤儿 + 消息校验失败」的不可解释失败。

```
_id: str                      # attachment_id，格式 att_<26 hex>（复用 core/utils/message_id.py 的 _new_id，M4）
kind: Literal[image|video|audio|file]  # 附件四类；sticker 非附件（内置资源，§4.3）
mime: str                     # filetype 真值判定结果
size: int                     # 字节
filename: str                 # 原始文件名（脱敏：仅展示用）
stored_name: str              # 落盘文件名 uuid.ext
sha256: str                   # 内容哈希
ref_count: int                # 引用计数（消息引用次数）
refs: list[str]               # 来源消息 message_id 列表（含归档消息）
status: Literal[active|deleted]  # 软删标记
created_at: str               # UTC ISO 8601
deleted_at: str | None        # 软删时间（GC 保留期依据）
```

**引用生命周期（I1 澄清，消除「上传 0 引用即软删」矛盾）：**
- **上传初始**：`refs=[], ref_count=0, status=active`（等待关联）。**不因 0 引用软删**。
- **消息关联**（发送/回放校验通过）：refs 加 message_id，ref_count+1。
- **删除消息/会话/归档消息**：从 refs 移除该 message_id，ref_count 递减（I2，§5.3）。
- **ref_count==0 且由「消息移除」归零** → `status=deleted, deleted_at=now`（文件保留，GC 延后删）。
- **ref_count==0 且一直未关联**（上传后 7 天未用于任何消息）→ 视为未使用上传，GC 软删（**§5.4 规则 3**，九轮 Minor 2 引用修正）。

**不变量：** `ref_count == len(refs)`；`status=active` 时文件应存在（GC 兜底扫描修复不一致，M8：文件缺失 → 标记 deleted + warning，定一）。

### 3.2 磁盘布局与附件 ID

- 目录：`data/uploads/{kind}/{uuid4hex}.{ext}`（kind 仅四个：image/video/audio/file）。`images/`、`desktop/` 为**历史遗留目录，不属于新附件体系**（C3）。sticker 在 `static/stickers/`（非附件，§4.3）。
- **静态挂载收敛（Critical #1）**：既有 `add_static("/uploads/", data/uploads)`（`server.py:361-363`）挂整棵树——若不收敛，新附件会经 `/uploads/{kind}/{uuid}.{ext}` 直出，**绕过签名/过期/软删**。**收敛为只挂两个 legacy 子目录**：`/uploads/images/` → `data/uploads/images/`、`/uploads/desktop/` → `data/uploads/desktop/`；`/uploads/` 下其余路径 404。新 kind 目录（image/video/audio/file/sticker，单数）**不静态暴露**，仅经 `/api/attachments/{id}` 签名下载。
- `attachment_id`：`att_` + 26-hex（复用 `_new_id`，protocol-v2 §3 ID 表登记 `att_`）。
- 稳定访问路径：`/api/attachments/{attachment_id}`（消息 part 持久化存此路径，不随签名过期失效）。

### 3.3 签名 URL（含签发侧）

**签发（新增，C-1）：** 服务端是 `ATTACHMENT_SIG_SECRET` 唯一持有者，签名只能由服务端生成。两个签发入口（四轮 review 定案，资源索引统一走渲染层，不单独带 signedUrl）：
1. **上传响应**（§4.1）直接带 `signedUrl`（24h）——前端上传后立即可预览。
2. **签发端点** `GET /api/attachments/{id}/sign`（**Bearer 保护**）→ `{attachmentId, url, expiresAt}`——前端渲染层对消息 part 与**资源索引附件项**中的稳定路径统一调用（缓存 + 过期重取），历史回放与资源查看随时可拿新签名。

**下载校验（已定义，Minor 9 修正顺序）：**
```
GET /api/attachments/{attachment_id}?sig=<hmac_sha256>&exp=<unix_ts>
```
- **fail-closed 早退（六轮 Minor 5）**：`AGENT_ACCESS_TOKEN` 配置而 SIG 未配置（§3.4）时，任何下载请求直接 403 `ATTACHMENT_SIGNATURE_DISABLED`，不进入 ①~④。
- `sig = hmac_sha256(ATTACHMENT_SIG_SECRET, f"{attachment_id}:{exp}").hexdigest()`。
- 校验顺序：① `sig` 存在且为合法 hex 串（**缺失/格式不符 → 403 `INVALID_SIGNATURE`**，不落「过期」语义）；② attachment_id 存在且 `status=active`（否则 404 `ATTACHMENT_NOT_FOUND`，**软删附件签名 URL 直接拒绝**，删除语义 = 不可再取）；③ `exp` 未过期且为整数且在 [now, now+24h] 内（否则 403 `SIGNATURE_EXPIRED`）；④ `sig` 匹配（`hmac.compare_digest`，否则 403 `INVALID_SIGNATURE`）。
- 响应：`aiohttp FileResponse`（原生支持 Range/206/If-Range）。Content-Type 用附件记录 mime（不依赖扩展名）。
- **不泄漏**：附件下载路径**不记录 query string**（若启用 aiohttp access log 会记完整 URL——对 `/api/attachments` 禁用 access log 或自定义 access log 剔除 query，M5；现状 `log_setup.py` 未配 `aiohttp.access` handler、access 日志被 root(WARNING) 抑制不产出，缓解措施为防御性，七轮 Minor 8）。日志只记 attachment_id。
- **下载存在性 oracle（七轮 Minor 7，记录在案不修）**：校验序 ②（404）在 ④（sig 匹配 403）之前，匿名可对已知 attachment_id 发 `sig=<合法hex>&exp=<int>` 区分「active（403）」与「不存在（404）」。attachment_id 为 104-bit 不可枚举、id 只经鉴权通道泄漏、单用户 demo，风险很低；如需闭合可把 ② 移后或合并 404/403，非本批次。

### 3.4 签名密钥策略（C2 修正——判据是 AGENT_ACCESS_TOKEN，非 SERVER_AUTH_TOKEN）

| AGENT_ACCESS_TOKEN | ATTACHMENT_SIG_SECRET | 行为 |
|---|---|---|
| 配置 | 配置 | 严格签名校验 |
| 配置 | **未配置** | **fail-closed**：签名校验拒绝（403 `ATTACHMENT_SIGNATURE_DISABLED`）。防「有鉴权部署却开了无签名下载」 |
| 未配置（无鉴权本地开发） | 任意 | **降级允许**：跳过 sig/exp 校验（等价于旧全匿名环境），**但第②步 status=active 校验仍执行**（软删附件仍 404，六轮 Minor 5 编号对齐）。启动打 warning「附件签名未启用，仅限无鉴权本地开发」 |

`SERVER_AUTH_TOKEN`（admin/debug）与附件签名**无关**，不作为判据。此表写入 `docs/electron-backend-security.md`。

**`ATTACHMENT_SIG_SECRET` 配置落点（Minor 3）：** `app_config.py` 新增 `attachment_sig_secret: str = ConfigField(default="")`（对应 env `ATTACHMENT_SIG_SECRET`）+ config.yaml 说明注释。判定用其非空即「已配置」。启用依赖检查顺序：`AGENT_ACCESS_TOKEN` 配置 → 必须配 SIG（fail-closed）→ 部署文档/启动警告覆盖。

**fail-closed 模式（token 已配 + SIG 未配）的签发行侧行为（Minor 7）：** sign 端点返回 403 `ATTACHMENT_SIGNATURE_DISABLED`；上传响应**省略 signedUrl**（前端不缓存必死签名）。fail-open 模式（无鉴权）sign 端点正常签发（无签名下载 = 等价旧全匿名，签名无意义但无害）。

## 4. 上传与下载协议

### 4.1 通用上传 `POST /api/uploads`

multipart form-data，字段：`files[]`（可多个）+ `kind`（可选，客户端意图，**不信任**）。

**端点级前置（I3 + Minor 6/#4/#8 + 七轮 Minor 9）：**
- `/api/uploads` **前缀整段跳过 payload 中间件**（九轮 Minor 5——Content-Length 预检 + `HTTPRequestEntityTooLarge` 异常兜底都跳过，否则异常兜底会接住 transport 413、handler 自 catch 无意义）——豁免前缀 `path.startswith("/api/uploads")`（**含** `/api/uploads/desktop/{callId}` 批次 4 路由，desktop 附件同为 multipart 大文件，一并受益）。豁免后 >`client_max_size`(64MB) 的请求在 transport 层抛 `HTTPRequestEntityTooLarge`，**通用上传 handler 与 desktop_upload_handler 均自 catch 该异常返回 413 信封**（desktop handler 现状 `except Exception → 500` 会把 413 变 500，须显式 catch，Minor #8）。**两条 413 路径错误码明确（七轮 Minor 9）**：transport `HTTPRequestEntityTooLarge` 自 catch → `REQUEST_TOO_LARGE`；desktop handler 既有 `read_chunk` 超限自限 → 现状 `INVALID_REQUEST`（413）**保持不动**（desktop 附件的既有契约），两者语义不同但都 413，文档注明。
- `max_request_bytes` 默认提升到 **64MB**（`app_config.py:40` + config.yaml 同步）——否则 aiohttp 底层 client_max_size 在读取 multipart 时仍按 8MB 拦截（既有 `/api/images` 10MB 也被 8MB 截住，属既有隐患，一并修复）。其他业务端点不受影响（JSON 请求远小于 64MB，各端点有参数校验）。
- **整体聚合上限（Critical #4）**：`client_max_size` 是 app 级整体 body 上限，作用于整个 multipart——**声明「单次 multipart 整体 ≤64MB，多文件总和超限即 413 `REQUEST_TOO_LARGE`」**（合法聚合 2×50MB 视频 = 100MB 会被 64MB 拦下，属有意取舍：单用户 demo 一次传 2 个 50MB 视频的概率低；如需放宽后续调大）。handler 流式 `read_chunk(max+1)` 自限（沿用现有 image_upload_handler 模式，不信任 client_max_size）。

**每文件校验（顺序）：**
0. **kind 枚举预检（六轮 Minor 2）**：kind 缺省或 ∈ {image,video,audio,file} → 通过；否则（sticker 与任意未知 kind 一视同仁）→ 400 `CONTRACT_VALIDATION`。
1. 数量 ≤10/次 → 否则 400 `TOO_MANY_FILES`（M2 固定）。
2. 大小 ≤ kind 上限 → 否则 413 `REQUEST_TOO_LARGE`（**初步校验；kind 缺省时跳过此步**（六轮 Minor 3），仅以归位 kind 复核；kind 非缺省时以客户端声明 kind 为基准——最终以上限更严的归位校验为准，Minor #6）。**kind 缺省时 handler 的 read_chunk 上限取各 kind 上限最大值（50MB）**（七轮 Minor 6——分类前仍需读取上限防 DoS，归位后再按最终 kind 复核）。
3. filetype 魔数判定 → 非白名单 → 415 `UNSUPPORTED_MEDIA_TYPE`（结构化错误）。
4. 扩展名与判定一致 → 不一致 415（堵改名绕过）。
5. **kind 归位**：kind 由 filetype 判定结果推导（不一致时以判定为准落判定 kind 目录，不误拒合法文件）。**webm 歧义（Minor #10 + 六轮 Minor 2 兜底）**：filetype 只能识别 webm 容器、无法区分音/视频轨道——audio 与 video 白名单都含 webm 时，**以客户端 `kind` 提示为 tiebreaker**（魔数命中 webm + 客户端 kind=audio → 落 audio；kind 缺失或不在 audio/video → 归 video，webm 惯例偏视频）。**归位后按最终 kind 复核大小上限（Minor #6）**——若文件超最终 kind 上限（如客户端声明 video=50MB 上限、filetype 落 file=20MB 上限）→ 413 `REQUEST_TOO_LARGE`（步骤 2 的初步校验不充分，最终以归位 kind 为准）。
6. 写盘（`data/uploads/{kind}/{uuid}.{ext}`）+ 算 sha256 + 写 attachments 集合（repo fail-closed，见 §3.1）。**写记录失败 → 立即删已落盘文件 + 503 `STORAGE_UNAVAILABLE`**（Minor 7，不留孤儿；即使漏删由 §5.4 规则 4 兜底）。

**`filename` 脱敏规则（Minor 9）：** 去路径分隔符（`/`、`\`）、截断至 255 字符、剥离控制字符；仅存展示名，不参与路径构造。

**返回：**
```
{
  "attachments": [
    {"attachmentId":"att_…","kind":"image","mime":"image/png","size":1234,
     "url":"/api/attachments/att_…","signedUrl":"/api/attachments/att_…?sig=…&exp=…",
     "filename":"photo.png"}
  ]
}
```
`signedUrl`（24h）供前端上传后立即可预览（§3.3 入口 1）；`url` 为稳定路径（消息 part 持久化用）。**条件说明（Minor 6）**：fail-closed 模式（AGENT_ACCESS_TOKEN 配而 SIG 未配）**省略 `signedUrl`**（§3.4），其余模式恒带。

**第一版 MIME/编码矩阵（白名单）：**

| kind | MIME 白名单 | 校验方式 | 上限 |
|---|---|---|---|
| image | png / jpeg / gif / webp | filetype 魔数 | 10MB |
| video | mp4 / webm | filetype 魔数 | 50MB |
| audio | wav / mp3 / ogg / webm | filetype 魔数 | 20MB |
| file | pdf / zip | filetype 魔数 | 20MB |
| file | txt / md / json | **扩展名白名单 + UTF-8 内容 sanity（文本类无魔数，I6）** | 20MB |

**sticker 不在上传白名单**（内置公共资源，非附件，§4.3）——`POST /api/uploads` 收到 kind=sticker → 400 `CONTRACT_VALIDATION`（对齐目录，五轮 Important #2）。

file 白名单含 zip/md → `FileMime` 枚举补 `APPLICATION_ZIP` / `TEXT_MARKDOWN`（I7）。非白名单 → 结构化错误 `UNSUPPORTED_MEDIA_TYPE`。

### 4.2 旧 `/api/images` 兼容

`/api/images` 端点**保留不变**（既有前端不破坏），**不纳入**附件 ID / 签名 / GC（C3：GC 扫描排除 `data/uploads/images/`）。图片迁新链路由前端切换驱动；后端两链路并存。记录在案：前端未切换期间，旧图片链路的安全由全局 Bearer 维持（见 §10 取舍）。

### 4.3 表情包（非附件 kind，四轮 review 定案）

- 内置表情：**放 `static/stickers/`**（`ProjectPath.STATIC` 的子目录，`server.py:201` 已挂载 `/static/` → 直接可访问，**无需 data/ → static/ 拷贝**，Minor 2 定一）。预置 10-20 个 PNG（文件名 = sticker_id，如 `st_happy.png`），**启动时扫描注册表**——注册表放 **`core/input/sticker_registry.py`**（模块级 `dict[sticker_id, path]` + `load_stickers()`，`/api/stickers` 与发送校验共用，七轮 Minor 4 定一）。**`load_stickers()` 调用点定一（八轮 M-2）：`create_app()` 内调用**（注册 `/api/stickers` 路由处，server.py 启动路径）。
- **公开静态**：`GET /static/stickers/{id}.png`（M7 定一——表情是公共资源，无隐私，不走鉴权；资源索引 sticker 分类返回该 URL）。
- **非附件**：sticker **不落 attachments 集合、不 ref_count、不参与 GC**。发送载体：v2 text 消息加可选 `stickerId: str` 字段（与 `attachments` 并列；sticker 消息通常无文本，`content` 可空）。服务端校验 `stickerId` 在内置注册表（不存在 → 400 `CONTRACT_VALIDATION`，对齐协议-v2 §9.1，五轮 Important #2）→ 构建 `StickerPart(sticker_id, src=/static/stickers/{id}.png)`。`StickerPart.attachment_id` 缺省（None）。
- 新增 `StickerPart`（`core/schema/part.py`）：`sticker_id` + `src`（`attachment_id` 可缺省）。资源索引 sticker 分类扫描 StickerPart → 终于非恒空。
- `GET /api/stickers`（**必做，八轮 M-5**——前端表情面板依赖它枚举内置表情，标可选则前端无枚举手段；返回 sticker_id + src 列表）→ 前端表情面板用。

### 4.4 消息关联与发送侧 wire（I5 定义）

**v2 附件消息入站结构：** `V2TextMessage`（`ws_message.py:525-536`）加可选字段：
```
attachments: Optional[list[AttachmentRef]] = Field(default=None)
AttachmentRef: {attachmentId: str, kind: str, mime: str}
```
`content` 保持 `str` 单值（不破坏现有 v2 语义）。幂等键仍 `clientRequestId`，但**幂等摘要输入须含附件与 sticker（Critical #3 + 五轮 Important #1）**：`not attachments and stickerId is None → digest = sha256(text)`（保持现状；**`attachments: []` 空数组等同缺省**，六轮 Minor 6，避免跨客户端序列化习惯不一致）；否则 `digest = sha256(text + "\x00" + ",".join(sorted(attachment_ids or [])) + "\x00" + (stickerId or ""))`——否则同文本不同附件/不同 sticker（换一张图/表情）会被误判为幂等重放、重发旧 ACK、新内容被静默吞掉。重试同摘要 → 重发 ACK；同 ID 异摘要 → rejected。`AttachmentRef.kind/mime` 为**客户端提示值，服务端以记录为准**（Minor 10，不校验提示与记录一致——校验失败会误拒合法文件，展示用记录值）。

**sticker 发送（§4.3）：** `V2TextMessage` 另加可选 `stickerId: str`——**不走附件校验**（内置资源非附件），服务端校验注册表存在（不存在 → 400 `CONTRACT_VALIDATION`，对齐目录，五轮 Important #2）→ 构建 StickerPart（无 attachment_id）。`attachments` 与 `stickerId` 可共存（多附件 + 一个 sticker）或独立。

**服务端处理（v2 text 带 attachments/stickerId 时，八轮 I-1 方案 A）：**
0. **附件/sticker 校验（先于幂等 lookup，六轮 Minor 4 + 七轮 Minor 2）**：每个 `attachmentId` 查 attachments 集合（存在 + `status=active`），`stickerId` 查注册表。任一失败 → `user_message.ack{rejected, code}`（`ATTACHMENT_NOT_FOUND` 404 / `CONTRACT_VALIDATION` 400；**Mongo 离线 → 503 `STORAGE_UNAVAILABLE`**，九轮 Minor 4 补 §3.1 fail-closed 枚举），**不落幂等 entry**（防永久坏附件反复成功）；重试同样复检（附件软删后重试 → 拒绝而非 stale accepted）。全部通过 → 继续。
1. **幂等 lookup**：digest（公式见上）→ `store.lookup(client_request_id)`——命中同摘要 → 重发原 ACK（end，不重复校验）；命中异摘要 → `ack{rejected, CONTRACT_VALIDATION}`（end）；未命中 → 继续。
2. **落幂等 entry**：预分配 message_id → `store.store(...)`。
3. **构建 parts**：`TextPart(content)` + 附件转对应 Part（**ImagePart/FilePart/AudioPart/VideoPart 统一携带 `attachment_id`**（Minor 5），`src=/api/attachments/{id}` 稳定路径）+ StickerPart（如有，无 attachment_id）；**`content=[p.model_dump() for p in parts]`（wire dict 形式）**——`process_message(content: list[dict])`（agent.py:304-305），下游 `parts_from_dicts(list[dict])`（message_adapter.py:67）/`content_text_from_content(list[dict])`（text_utils.py:8）均消费 dict；**传 Part 对象：`content_text_from_content` 的 `isinstance(p, dict)` 守卫会静默过滤返回空串、`parts_from_dicts` 走 Pydantic 校验也可能拒——不产生预期的 AttributeError 但链路静默坏，故必须 dict 形式**（七轮 Important-1 + 八轮 M-4 措辞修正）。
4. **进 Agent（ACK 延后，八轮 I-1）**：`accepted = await agent.process_message(text, content=content, message_id, protocol_version=2, conversation_id)`：
   - **`process_message` 返回类型改 `bool`**（`agent.py:304-351`：空消息/队列满 → False，正常入队或直接处理 → True；现状返回 None、现有 **7 个调用方**（app.py:67 / api_routes.py:723 / ws_handler.py:446,537,541 / agent/server.py:493 / bench.py:159，九轮 Minor 1 计数核实）均忽略返回值，改 bool 向下兼容）。
   - **True** → `_send_ack(accepted)` + **refs 递增**（refs 加预分配 message_id、ref_count+1，幂等去重保护内——每个 message_id 至多入 refs 一次）。
   - **False**（队列满 drop / 空消息）→ **删幂等 entry**（否则重试同 clientRequestId 会重放 stale accepted）+ `_send_ack(rejected, code=QUEUE_FULL)` + **refs 不递增**（保持 `ref_count==len(refs)` 不变量）。
   - **附件/sticker 消息的 ACK 延后到 `process_message` 返回后**（九轮 Minor 6——sticker-only 同走 step 0-4、同样需知 drop 结果；纯文本消息 ACK 时序保持现状——先 ack 再 process；延后因需知 drop 结果，v2 accepted 语义本为「受理确认」，延后更准确）。
   - **refs 写盘失败**（发送前校验已读 Mongo，窗口极小）：记 warning，由 GC 兜底——**§5.4 规则 3（7 天未关联回收）**（九轮 Minor 3 修正：规则 2 是「文件缺失」不对应；此场景文件在、记录在，仅 ref_count 停在 0，7 天后软删，期间历史正常、7 天后历史消息变「附件已删除」，记录在案）。
5. **入口门与生成**：入口门 `ws_handler.py:533` 放宽为 `text_content.strip() or data.get("stickerId") or data.get("attachments")` 才进 v2 路由（否则走 legacy）；`agent.py:324` 守卫 `if not text.strip() and not content: return` 对 `content=[…]` 放行。**sticker-only / 附件-only 消息触发生成**（与附件消息同语义，用户发了个表情/附件即一轮输入）；`content_to_openai` 对 Sticker/File/Audio/Video 跳过、TextPart 空文本——**空文本喂 LLM 记为已知取舍**（纯 sticker 低频，provider 对「空 user 内容」行为可接受；若需严格可要求前端 sticker 伴文本，非后端强校验，见 §10）。

**渲染层签名解析（C-1）：** 消息 part 持久化 `src=/api/attachments/{id}`（稳定路径，不随签名过期失效）。前端渲染层（useAttachmentUrl）对稳定路径调用 `GET /api/attachments/{id}/sign`（Bearer）拿签名 URL（缓存 + 24h 内复用 + 过期重取），`<img>/<video>` 用签名 URL。资源索引附件项**同样经渲染层调用签发端点解析**（不单独带 signedUrl，六轮 Minor 7）。**前端不接触 `ATTACHMENT_SIG_SECRET`，不自行计算签名。**

**v1 兼容：** v1 多模态消息（`content: list[ContentPart]`）继续走 legacy 路径，不含 `attachments` 字段。附件 part 带 `attachment_id` 校验仅 v2 attachments 路径执行；legacy 图片 part 仍按现状（无附件校验）。

### 4.5 LLM 视觉路径适配（I4）

`content_to_openai` 的 `_resolve_image_source`（`message_adapter.py:172-202`）目前只在 `_IMAGES_DIR`/`_STATIC_DIR` 按 basename 找文件。新附件在 `data/uploads/image/`，basename 不命中 → 会把未签名 `/api/attachments/{id}` 直接喂给 LLM（401/403 死图）。

**sync/async 边界（I-4.1 + Minor #5）：** `_resolve_image_source` / `content_to_openai` / `MessageBuilder.build_messages` 均为 **同步纯函数**，不能在同步链路里 `await` Mongo 查附件记录。修复机制——**`AttachmentFileResolver`（模块级注册表，同步可读）**：
- 放**新文件 `core/schema/adapters/attachment_resolver.py`**（七轮 Minor 4 定一，不落 storage）：模块级 `dict[attachment_id, (kind, stored_name)]`（**值含 kind**——Mongo 离线兜底时据此定位目录，四轮 review Important #2）+ `register/unregister/load`。`_resolve_image_source` 直接查该模块级 dict（无需注入，同步安全）。
- 异步层维护：上传/消息关联时 `register(attachment_id, kind, stored_name)`，软删/GC 清理时 `unregister`，启动 GC 扫描时 `load` 预热。
- **再预热钩子（Minor #11 + 五轮 Minor #7）**：Mongo 离线启动 → 映射空 → 附件图片喂 LLM 全变死链。§5.4 的 GC 仅启动一次，无周期性「下次扫描」——修复：**签发端点与下载成功路径顺带 `register`**（两者都查过附件记录、持有 kind/stored_name，命中即预热映射），Mongo 恢复后首个下载/签发自然刷新；启动 GC `load` 保留。懒加载兜底：`_resolve_image_source` 查映射未命中时维持原样返回（不试探，kind 未知无法定位）。
- `_resolve_image_source` 对 src 匹配 `/api/attachments/{attachment_id}` 时：从映射取 `(kind, stored_name)` → 读 `data/uploads/{kind}/{stored_name}` 转 data URI（沿用现状「本地图片转 data URI 喂模型」行为）。未命中 → 维持现状原样返回（LLM 侧按不可用处理）。
- **vision 插件复用（Minor #5）**：`core/tools/vision/plugin.py:133/214` 同样调用 `_resolve_image_source`——同一机制同步受益，无额外接线。
- File/Audio/Video/Sticker part 在 `content_to_openai` **跳过**（不喂 LLM，现状即跳过 Phase 2 类型）。

**测试（I-4.1 附带）：** 构造带 `/api/attachments/{id}` ImagePart.src 的消息 → 断言喂给 LLM 的是 data URI（注册映射后）与未签名 URL（未注册时）。

## 5. 安全

### 5.1 上传安全

- multipart 流式读防 DoS（§4.1 前置）。
- 数量/大小/MIME/扩展名四重校验；kind 以 filetype 判定归位。
- 上传端点认证：`/api/uploads` 在 `/api/*` 保护内（Bearer），匿名上传被拒。

### 5.2 下载安全（C1 修正——中间件豁免 + Critical #1/#2 精确化）

- `_make_access_auth_middleware`（`server.py:76-95`）对 `path.startswith("/api/attachments/")` **且不是 `/sign` 子路径**放行：`path.startswith("/api/attachments/") and not path.endswith("/sign")`（**Critical #2**——sign 端点 `/api/attachments/{id}/sign` 必须保持 Bearer 保护，全前缀豁免会连带免 Bearer）。其余 `/api/*`、`/uploads/*` 保持 Bearer。**尾斜杠（Minor 7）**：`/sign/` 不被 `endswith("/sign")` 豁免 → 走 Bearer → 路由不匹配 → 404，不构成绕过；落地测试补尾斜杠用例。
- **静态挂载收敛（Critical #1）**：`/uploads/` 静态挂载改为仅 `images/`、`desktop/` 两个 legacy 子目录；新 kind 目录不静态暴露，仅签名下载（§3.2）。
- **此豁免写入 `docs/electron-backend-security.md` §1.2**：`/api/attachments/`（非 `/sign`）是唯一免 Bearer 的业务路径，理由 = 短时 HMAC 签名授权；sign 端点走 Bearer。
- 签名 URL 的 sig 是**短时下载授权**（非账号凭据）——在 security 文档加豁免说明：不违反「凭据不放 URL 查询参数」（凭据 = 账号 token/API Key，签名是自签名一次性授权，M10）。
- `FileResponse` 默认安全（aiohttp 处理路径穿越）。
- **不泄漏**：下载路径不记录 query string（M5）；日志只记 attachment_id，不记签名。

### 5.3 删除联动（引用计数 + 软删，I2 闭环）

- **删除会话内消息 / 删除会话**：对消息的每个附件 part，从 refs 移除该 message_id → ref_count 递减（引用解除）。
- **删除归档消息（debug ArchivePanel edit/delete/clear，`/debug/api/archive`）**：同样递减 refs——否则附件 active + ref_count>0 永久泄漏。**归档级消息删除的 refs 递减路径必须实现**（I2）。
- **历史展示**：归档消息内容**不改写**（append-only），仍持有稳定路径 `/api/attachments/{id}`，前端渲染层经签发端点拿签名（§4.4 渲染层解析）。附件软删后，前端渲染收到 404 → **显示占位「附件已删除」**（前端兜底，本 spec 定义行为）；资源索引对软删附件**过滤**——**仅对 `/api/attachments/` 前缀项过滤**（`/uploads/images/*` 等 legacy 图片项无附件记录，不过滤，五轮 Minor #10）。
- ref_count==0 且由消息移除归零 → `status=deleted`、`deleted_at=now`（文件保留，P7-8 软删）。
- 软删后访问：签名 URL 校验第②步（attachment 存在且 active）即 404（§3.3，七轮 Minor 1 编号对齐）。

### 5.4 启动兜底扫描（GC，C3 限定范围）

启动时（后台任务，不阻塞启动）扫描**仅限四个新 kind 目录** `data/uploads/{image,video,audio,file}/`（**明确排除** `data/uploads/images/`、`data/uploads/desktop/` 历史遗留目录，防删光历史图片与批次 4 桌面截图；`static/stickers/` 为内置公共资源，不扫）。**Mongo 离线时 GC 扫描跳过并记日志**（读路径 fail-closed，I-4.3）：

1. `attachments` 集合 `status=deleted` 且 `deleted_at` 超 24h → 删文件 + 删记录。
2. `status=active` 且文件缺失 → 标记 deleted + warning（M8 定一）。
3. `status=active` 且 `ref_count=0` 且 `created_at` 超 7 天 → 软删（未使用上传回收，I1）。
4. 磁盘四个 kind 目录有文件但无 attachments 记录（孤儿）→ 删文件。
5. 扫描结果记日志（数量统计，不泄漏内容）。

## 6. BE-081 健康、版本和可观察性

### 6.1 版本

- `core/version.py`：`__version__` 保持 semver 单一事实来源；新增 `build_sha: str | None` 与 `get_build_sha()`（**模块级缓存，进程内只调一次 git，六轮 Minor 10 + 七轮 Minor 4 定一**）——调用点：**`Agent.start()` 顶部** `build_sha = get_build_sha()`（内部 `subprocess git rev-parse HEAD`，失败返回 None，不抛异常）。
- `build_server_info()`：`server_version = __version__ + (f"+{build_sha[:7]}" if build_sha)`。
- `frontend_contract/ws_message.py` `ServerInfoMessage`（:167-183）加 `build_sha: str | None`（可选字段，向后兼容）。
- `/health` 返回 `version`（同格式）。

### 6.2 /health、/ready（I8 修正——独立 handler）

- **拆成两个 handler**（现 `server.py:237-262` 共用一个）：
  - `/health`：进程存活探测，恒 200。返回 `{status, fsm, llm, memory, tools, version, build_sha, uptime, request_id, dependencies:{mongo, milvus}}`。`status` 派生：dependencies 全 true → "ok"，任一 false → "degraded"。
  - `/ready`：就绪探测，依赖不 ok → **503**（非 200），同样字段。
- 依赖检查通过公开接口 `agent.session.health_check()` / `agent.long_term.health_check()`（`session.py:228` / `long_term.py:138`；`/debug/api/db-status` 经 `memory_repo/session_repo`，此处走公开接口，Minor 8）。**依赖检查加超时（Minor #12）**：`health_check` 在 DB 断开时会尝试重连（`session_repo.py:108-116`），可能阻塞秒级——给每个检查包 `asyncio.wait_for(..., timeout=2s)`，超时视为依赖 down，保证 `/health` 是快速存活探针。
- **既有测试需同步更新（I8 + Minor 8/13）**：`core/input/tests/test_server.py` 的 fixture 在 :801-822（现为 `MagicMock`，不可 await `health_check`）→ 对 `agent.session.health_check` 与 `agent.long_term.health_check` 均设 `AsyncMock(return_value=True)`；:825-839 是测试方法本体，断言保持 `status=="ok"`。spec 不承诺「保持通过」，而承诺「测试同步更新后通过」。

### 6.3 日志敏感收敛

- `ws_handler.py:538` `logger.debug("WS 收到文本: %s", text_content[:100])` → 只记 `len(text_content)` + message_id，**不记正文**。
- `loop.py:450` `logger.info(">>> 用户输入 ... %s", log_text[:200])` → 同样收敛为长度/ID。
- **一并收敛 ws_handler 其余正文打印（八轮 M-1）**：`:512` `voice.commit 提交成功: %s`（记 `pending_text[:30]` 字幕正文）→ 只记 recordingId + 长度；`:906` `STT 识别: %s%s`（记 `text[:100]` 转写正文）→ 只记长度/段 ID。否则日志敏感扫描覆盖发送路径时 :512/:906 会误报或边界不一致。
- `conversation.log`（`log_setup.py:39-50` 的 `log_conversation` 函数，行号近似）**保留**——调试审计通道，有意设计；文档标注风险。
- **新增「日志敏感扫描」测试**：构造触发日志路径（上传、发送、签名），捕获 logger 输出断言不含正文/密钥/路径。**caplog 限定指定 logger**（`core.input.ws_handler` / `core.cognitive.loop` 等，排除 conversation.log 通道——后者全文落盘属有意保留，五轮 Minor #9），避免误失败。

### 6.4 关联 ID（M6 修正——放弃不可行的 logRequestId）

- **WS 侧**：`ErrorMessage`（`ws_message.py:418-423`）加 `requestId: str | None`；WS 错误发送点（`ws_handler.py` 若干）补 requestId（= 该次请求的 WS 侧 request_id，来源 `UserInputData.request_id`，有则带、无则 None）。
- **生成侧**：`GenerationRegistry.register` 加 `request_id` 字段（generation 可回溯到 WS 请求）。**数据来源（Minor #13）**：`GenerationContext`（`core/cognitive/generation.py`）已贯通 `generation_id/assistant_message_id/conversation_id`——加 `request_id` 字段，register 时写入。
- **REST 侧**：错误信封维持 `requestId`（req_uuid，现状）——**不做** `logRequestId`（`next_request_id` 仅 WS turn 有效，REST handler 无对应日志侧 id，强行关联是伪关联）。
- 四套 ID 不合并，能力边界如实声明：REST 请求有自己的 requestId；WS 请求有 request_id + generationId 关联；跨 REST↔WS 追踪非本批次目标。

## 7. 错误码与契约

### 7.1 新增错误码（并入 protocol-v2 §9.1 目录，32 → **40**，M1/I-4.2 + 八轮 QUEUE_FULL；补 retryable 列，Minor 5）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `ATTACHMENT_NOT_FOUND` | 404 | false | 附件 ID 不存在 / 已软删（§3.3、§4.4、M3 统一） |
| `SIGNATURE_EXPIRED` | 403 | false | 签名 URL 过期 |
| `INVALID_SIGNATURE` | 403 | false | 签名不匹配 / 篡改 |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | false | 文件类型不在白名单 / 扩展名不一致 |
| `TOO_MANY_FILES` | 400 | false | 单次上传文件数超限 |
| `ATTACHMENT_SIGNATURE_DISABLED` | 403 | false | 有鉴权部署但签名密钥未配置（fail-closed） |
| `STORAGE_UNAVAILABLE` | 503 | **true** | 附件 Mongo 未连接（上传/签发/下载 fail-closed，可重连后重试） |
| `QUEUE_FULL` | 429 | **true** | 附件/sticker 消息 process_message 队列满 drop（八轮 I-1，可稍后重试） |

### 7.2 契约变更

**`frontend_contract/rest.py`：**
- `AttachmentInfo`（attachmentId / kind / mime / size / url / signedUrl? / filename）——`signedUrl` 可缺省（fail-closed 省略，Minor 6）
- `AttachmentUploadResponse`（attachments: list[AttachmentInfo]）
- `AttachmentSignResponse`（attachmentId / url / expiresAt）——签发端点返回（C-1）
- 上传错误走统一 `ProtocolError` 信封（区别于旧 `ImageUploadError` 仅 error 键）
- **`ResourceItem` 不改**（四轮 review 定案）：资源索引附件项返回稳定 `url=/api/attachments/{id}`，前端渲染层统一走签发端点解析（与消息 part 同路径，省契约改动）

**`frontend_contract/ws_message.py`：**
- `ContentPart` 联合加 FilePart/AudioPart/VideoPart/StickerPart（wire 镜像 `core/schema/part.py`；**I9：此变更使 `test_server.py:776` 的「file part 判非法丢弃」用例变合法，该测试需同步更新——替换素材用真正的非法 part，如 `{"type":"unknown"}`，Minor 11**）。**前端镜像同步加 `attachment_id: Optional[str]`**（六轮 Minor 8——ImagePart/File/Audio/Video 统一，否则 gen_ts 生成的 TS 缺该字段，契约漂移）
- `AttachmentRef` 模型 + `V2TextMessage.attachments` 可选字段；**`V2TextMessage.stickerId` 可选字段**（§4.3）
- `ServerInfoMessage` + `build_sha`；`ServerInfoFeatures` + `media_upload`（如实声明能力）
- `ErrorMessage` + `requestId`

**`core/schema/part.py`：** File/Audio/Video 转正 + `attachment_id` 字段；**ImagePart 同加 `attachment_id`**（Minor 5，统一）；新增 `StickerPart` + **`PartType` 枚举加 `STICKER` 成员**（判别联合的判别值依赖，Minor #13）；`FileMime` 补 `APPLICATION_ZIP`/`TEXT_MARKDOWN`（I7）；`content_to_openai` 适配新附件图片路径（§4.5 AttachmentFileResolver），File/Audio/Video/Sticker 跳过。

**`core/input/capabilities.py`（Minor 1/#4 修正）：** `ServerInfoFeatures` 模型定义在 `frontend_contract/ws_message.py:156`，但 **`build_server_info`（`capabilities.py:86-95`）是 `ServerInfoFeatures` 唯一构造点**（`ws_handler.py` 经其产出 server_info）——**`build_server_info` 的 features 构造加 `media_upload=True`**（capabilities.py 改一行，非不改，否则 `media_upload` 恒 False = 能力欠声明，违反「如实声明能力」）。

## 8. 测试要求

每个批次必须（docs/15 §八）：
- Pydantic 契约 Round-trip（rest.py 新模型 + ws_message 新模型）。
- 生成 TypeScript 漂移检查（`make gen-types` + `make check-types`）。
- Handler/Service/Repository 单元与服务测试。
- 认证失败和权限边界测试。
- 日志敏感信息扫描测试（新增）。
- 旧 v1 客户端兼容测试（`/api/images` 保留 + WS v1 不受影响）。
- 新 v2 真实脱敏 JSONL/事件样例。

**高风险专项（含既有测试更新）：**
- 未认证 WS/API/上传（`/api/uploads` 无 Bearer → 401）。
- 上传类型伪造（改名 .png 的 exe → 415 `UNSUPPORTED_MEDIA_TYPE`）。
- 签名 URL 篡改（改 sig/exp → 403）与过期；**签发端点** `GET /api/attachments/{id}/sign` 无 Bearer → 401、附件不存在 → 404、软删附件 → 404（C-1）。**sign 端点 Bearer 精确豁免（Critical #2）**：sign 带 Bearer→200 / 无 Bearer→401 / 下载免 Bearer；**尾斜杠 `/sign/` → 404 不绕过**（Minor 7）。
- **静态挂载收敛（Critical #1）**：`/uploads/{kind}/{uuid}.{ext}` 新附件路径 → 404（不再静态暴露）；`/uploads/images/`、`/uploads/desktop/` legacy 路径仍可访问。
- 资源越权读取（无签名访问 `/api/attachments` → 403；软删附件 → 404）。
- 签名 URL 的 `signedUrl` 立即可用（上传响应）、渲染层经签发端点重取（C-1）；**资源索引附件项经渲染层解析拿签名**（四轮 review）。
- Range 请求（206 + Content-Range 正确）。
- 引用计数（上传→消息关联→删除消息→ref 归零→软删；多消息共享附件；归档消息删除递减 refs；**同 clientRequestId 重试不重复 refs 递增**，Minor 4；**同文本不同附件 → 摘要不同 → 正常处理、refs 递增**，Critical #3；**无附件 → digest=sha256(text) 不变**，Minor 4；**同文本不同 stickerId → 摘要不同**，五轮 Important #1）。
- **sticker（四轮 review）**：`stickerId` 发送 → 校验注册表 → 构建 StickerPart（无 attachment_id、不落附件、不 ref_count）；`POST /api/uploads` kind=sticker → 400 `CONTRACT_VALIDATION`；**空文本 sticker-only / 附件-only 消息能进门 → ACK accepted + 触发生成**（六轮 Important 1：`ws_handler.py:533` 入口门放宽后，`content="" + stickerId/attachments` 不再被丢）；**校验失败（附件 404/sticker 400）不落幂等 entry，重试不重发 accepted**（六轮 Minor 4）；**队列满 drop → 删幂等 entry + ack rejected(QUEUE_FULL) + refs 不递增**（八轮 I-1，`process_message` 返回 bool 后）。
- **整体 64MB 聚合上限（Critical #4）**：多文件总和超 64MB → 413 `REQUEST_TOO_LARGE`（即使单文件合规）。
- **webm tiebreaker（Minor #10）**：webm + 客户端 kind=audio → 落 audio。
- GC 兜底扫描（deleted 超期清理 / 孤儿文件清理 / **不误删 `images/`、`desktop/`**）。
- 视觉链路：带 `/api/attachments/{id}` ImagePart.src 的消息 → 喂 LLM 为 data URI（AttachmentFileResolver 注册后）/ 原样（未注册），I-4.1。
- **既有测试更新**：`test_server.py:776`（ContentPart 扩展 → 替换素材 `{"type":"unknown"}`，Minor 11；`:783` 注释「ContentPart 判别联合只认 text/image」需同步改）、`test_server.py:801-822` fixture（`agent.session`/`agent.long_term` 的 `health_check` 设 AsyncMock + /ready 独立 handler，Minor 8/13）。

## 9. 实施顺序（批次内部）

1. **契约层**：rest.py / ws_message.py / part.py 模型 + 生成 TS + Round-trip 测试。
2. **存储层**：`attachment_repo.py` + 引用计数/软删逻辑 + 单元测试。
3. **上传链路**：`POST /api/uploads` + filetype 校验 + 落盘 + 写 attachments + 服务测试 + 中间件豁免/限流前置。**`filetype` 加入 `requirements.txt`**（七轮 Minor 5）**。`/api/uploads/desktop/{callId}` 的 413 自 catch 与豁免改动同批落地**（Minor #8——desktop handler 现状 `except Exception → 500`，豁免后 transport 抛 `HTTPRequestEntityTooLarge` 会被吞成 500，须显式 catch 返回 413）。
4. **签名下载 + 签发**：`/api/attachments/{id}` 签名校验 + FileResponse Range + 安全测试；`GET /api/attachments/{id}/sign` 签发端点 + 上传响应带 signedUrl（C-1）。
5. **消息关联**：`V2TextMessage.attachments` + refs 递增（幂等防重） + 删除联动（含归档） + `AttachmentFileResolver` 内存映射 + `_resolve_image_source` 适配（I-4.1）。
6. **表情包**：内置 sticker 注册表（`static/stickers/`）+ StickerPart + `/api/stickers`。
7. **GC**：启动兜底扫描 + 清理逻辑（限四个 kind 目录，离线跳过）。
8. **BE-081**：版本 git sha + /health//ready 拆分 + 日志收敛 + 关联 ID。
9. **文档与验收**：含 **protocol-v2 §9 资源索引行同步**（sticker 分类由「恒空」改为「经 StickerPart 填充」，五轮 Minor #8）+ **§9.1 `CONTRACT_VALIDATION` 目录行场景追加**（「未知 stickerId / 上传 kind=sticker」，六轮 Minor 9）+ **§9.1 `INVALID_REQUEST` 行补 desktop 413 场景或注明漂移**（目录行 HTTP=403，desktop 上传超限用 INVALID_REQUEST+413，八轮 M-3）+ **§9.1 新增码登记（8 个，含 QUEUE_FULL）** + §13 语音/媒体契约补录。

每项完成必须沿注册入口 → Handler → Service → Repository → 序列化 → 测试闭环（docs/15 §五）。

## 10. 已知取舍与风险（记录在案）

- **签名 URL 签发侧依赖服务端 + 前端渲染层**（C-1）：前端不接触 SIG 密钥，经 `GET /api/attachments/{id}/sign`（Bearer）拿签名；上传响应带 signedUrl 省首轮请求。**资源索引附件项统一走渲染层签发解析**（不单独带 signedUrl）。
- **sticker 是内置公共资源，非附件**（四轮 review）：不落 attachments / 不 ref_count / 不 GC；经 v2 `stickerId` 发送；资源索引 sticker 分类扫描 StickerPart。
- **sticker-only / 附件-only 消息触发生成，空文本喂 LLM**（五轮 Important #3 取舍）：`content_to_openai` 跳过 Sticker/File/Audio/Video、TextPart 空文本——LLM 收到「空用户内容 + 历史上下文」，provider 行为各异但可接受；纯 sticker 低频。若需严格可要求前端 sticker 伴文本（非后端强校验）。
- **旧 `/api/images` 不纳入附件体系**：图片迁移靠前端切换，后端两链路并存。前端未切换期间旧图链路安全由全局 Bearer 维持（不在签名/GC 保护内）。前端切换后关闭兼容。
- **视频/音频/文件/表情不喂 LLM**：上下文/费用考量；图片喂 LLM 走 data URI（AttachmentFileResolver 内存映射，同步安全）。
- **软删 24h 保留期 + 未关联 7 天回收**：可恢复窗口；期间磁盘占用增加。单用户 demo 可接受。
- **`conversation.log` 明文不收敛**：调试审计通道，有意保留。日志敏感扫描测试覆盖新代码路径，不覆盖 conversation.log。
- **deleted 附件签名 URL 直接拒绝**：删除语义 = 不可再取；恢复靠 GC 前运维干预，产品不做恢复 UI。
- **签名 URL 免 Bearer + 豁免中间件**：签名即授权；密钥泄漏 = 可伪造签名，故 fail-closed（AGENT_ACCESS_TOKEN 配置而 SIG 未配置 → 拒绝）+ env 强推 + 24h 短窗口。这是 `/api/*` Bearer 的唯一豁免路径，写入 security 文档。
- **`max_request_bytes` 8MB → 64MB**：修复既有 10MB 图片隐患；各上传端点 handler 自限兜底。`server.py:150` 现有注释「multipart 手工限制不受其约束」与 client_max_size 实际作用于 multipart 的事实矛盾，落地时顺带修正（Minor 12）。
- **幂等重放与 drop 竞态（九轮 Minor 7，记录在案不修）**：重试恰在首条消息 process 返回 False 之前命中 entry 时，客户端先收 replay `accepted` 再收 `rejected(QUEUE_FULL)`（last-ACK-wins）。该竞态与现状纯文本流同源（store→ACK→process 期间重试即重放），非本批新引入；新流程把「静默丢消息」变「显式 rejected」，是改善。
- **REST↔WS 跨面追踪非本批次目标**：M6 放弃 logRequestId，如实声明能力边界。
