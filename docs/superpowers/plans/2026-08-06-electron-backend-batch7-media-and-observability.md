# Electron 后端批次 7 — 通用附件与媒体链路 + 发布收敛 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 BE-070/071/081——通用附件与媒体链路（稳定附件 ID、filetype 真值校验、短期签名下载 URL、Range、引用计数+软删 GC、内置表情）+ 健康/版本/可观测性收敛。

**Architecture:** 新增 MongoDB `attachments` 集合（AttachmentRepository）+ 内存 AttachmentFileResolver（同步视觉解析）+ `POST /api/uploads` 通用上传 + `/api/attachments/{id}` 签名下载 + `/api/attachments/{id}/sign` 签发 + `/api/stickers` 表情 + 启动 GC 扫描；改造 `/health`/`/ready`（真依赖检查）+ version build_sha + 日志收敛 + 关联 ID。契约单源 `frontend_contract/` 先动，`make check-types` 门禁。

**Tech Stack:** Python 3.11+ / aiohttp / motor(Mongo) / filetype（新依赖，纯 wheel）/ Pydantic / frontend_contract gen_ts。

## Global Constraints

- 契约单源：所有 wire 类型在 `frontend_contract/` 定义，后端发送点构造契约模型 + `wire_dict()`；`make gen-types` 重生成、`make check-types` 漂移门禁（CI 强制）。
- 禁相对导入（ruff TID252），一律绝对导入；代码注释用中文。
- 测试分层：新测试必须加 marker（unit/service/integration/slow）；`make test-quick`（unit or service）/ `make test-normal`（not slow）。
- 每项完成必须沿注册入口 → Handler → Service → Repository → 序列化 → 测试闭环。
- 错误信封统一 `ProtocolError`（code/message/retryable/requestId/details），`details` 禁密钥/正文/路径。
- 敏感信息经环境变量；附件签名密钥 `ATTACHMENT_SIG_SECRET`（app_config 新 ConfigField）。
- filetype 加入 `requirements.txt`（纯 wheel，`pip install filetype`）。
- 附件 ID 复用 `core.utils.message_id._new_id("att")`（att_ + 26-hex）。
- `process_message` 返回类型 None→bool（Task 5 内完成，7 个调用方忽略返回值，向下兼容）。
- 错误码 32→40：新增 ATTACHMENT_NOT_FOUND / SIGNATURE_EXPIRED / INVALID_SIGNATURE / UNSUPPORTED_MEDIA_TYPE / TOO_MANY_FILES / ATTACHMENT_SIGNATURE_DISABLED / STORAGE_UNAVAILABLE / QUEUE_FULL。
- **测试运行环境（plan review Minor 6 统一）**：所有 `python -m pytest` / `make` 命令在 conda env Elysia 下运行（`conda activate Elysia`，等价 `PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH"`）。
- **执行顺序（plan review III-A）**：Task 5（消息关联）的 sticker 测试依赖 Task 6 的 `sticker_registry.py` 与 `static/stickers/` PNG——**执行顺序为 Task 1-4 → Task 6（sticker）→ Task 5（消息关联）→ Task 7-12**。Task 5 头部亦注前置。

---

### Task 1: 契约层 — part.py + rest.py + ws_message.py + gen-types + round-trip

**Files:**
- Modify: `core/schema/part.py`（PartType 加 STICKER、FileMime 补 ZIP/MD、四 Part 加 attachment_id、新增 StickerPart）
- Modify: `frontend_contract/rest.py`（AttachmentInfo / AttachmentUploadResponse / AttachmentSignResponse）
- Modify: `frontend_contract/ws_message.py`（ContentPart 联合扩展 + AttachmentRef + V2TextMessage.attachments/stickerId + ServerInfoMessage.build_sha + ServerInfoFeatures.media_upload + ErrorMessage.requestId）
- Test: `frontend_contract/tests/test_batch7_contract.py`（新建）
- Modify: `core/input/tests/test_server.py:776`（ContentPart 扩展破坏既有用例，替换素材）

**Interfaces:**
- Consumes: `core/schema/part.py` 现状（ImagePart/FilePart/AudioPart/VideoPart + MIME 枚举 + 判别联合 `Part`）；`frontend_contract/ws_message.py`（V2TextMessage:525-536、ServerInfoMessage:167-183、ServerInfoFeatures:156-164、ErrorMessage:418-423、ContentPart:39）；`frontend_contract/rest.py`（ImageUploadResponse 等）。
- Produces: `PartType.STICKER`；`FileMime.APPLICATION_ZIP/TEXT_MARKDOWN`；`ImagePart/FilePart/AudioPart/VideoPart` 各加 `attachment_id: Optional[str]`；`StickerPart`（sticker_id/src，无 attachment_id）；`AttachmentRef{attachmentId,kind,mime}`；`V2TextMessage.attachments: Optional[list[AttachmentRef]]` + `stickerId: Optional[str]`；`ServerInfoMessage.build_sha: Optional[str]`；`ServerInfoFeatures.media_upload: bool`；`ErrorMessage.requestId: Optional[str]`；`rest.py` 三模型。

- [ ] **Step 1: 写 failing round-trip 测试**

创建 `frontend_contract/tests/test_batch7_contract.py`：

```python
"""批次 7 契约 round-trip（marker=unit，无 I/O）。"""
import pytest

from frontend_contract.rest import (
    AttachmentInfo, AttachmentUploadResponse, AttachmentSignResponse,
    StickerEntry, StickerListResponse,
)
from frontend_contract.ws_message import (
    AttachmentRef, ContentPart, ErrorMessage, ServerInfoFeatures,
    ServerInfoMessage, V2TextMessage, FilePart, AudioPart, VideoPart,
    StickerPart, ImagePart,
)

pytestmark = pytest.mark.unit


def test_attachment_upload_response_roundtrip():
    resp = AttachmentUploadResponse(attachments=[
        AttachmentInfo(attachmentId="att_abc", kind="image", mime="image/png",
                       size=100, url="/api/attachments/att_abc",
                       signedUrl="/api/attachments/att_abc?sig=x&exp=1",
                       filename="a.png"),
    ])
    d = resp.wire_dict()
    assert d["attachments"][0]["attachmentId"] == "att_abc"
    assert AttachmentUploadResponse.model_validate(d) == resp


def test_v2_text_attachments_roundtrip():
    ref = AttachmentRef(attachmentId="att_abc", kind="image", mime="image/png")
    msg = V2TextMessage(content="hi", clientRequestId="req_1",
                        attachments=[ref], stickerId="st_happy")
    d = msg.wire_dict()
    assert d["attachments"][0]["attachmentId"] == "att_abc"
    assert d["stickerId"] == "st_happy"
    assert V2TextMessage.model_validate(d).attachments[0].attachmentId == "att_abc"


def test_content_part_union_now_includes_media():
    # file part 现在是合法 ContentPart（既有 test_server.py:776 断言反转依据）
    assert ContentPart.model_validate({"type": "file", "src": "/api/attachments/att_abc",
                                       "attachment_id": "att_abc"}).type == "file"
    assert ContentPart.model_validate({"type": "sticker", "sticker_id": "st_happy",
                                       "src": "/static/stickers/st_happy.png"}).type == "sticker"


def test_image_part_attachment_id():
    # 既有 ImagePart 同加 attachment_id（spec §7.2 Minor 8，plan review Minor 6）
    img = ImagePart(src="/api/attachments/att_x", mime="image/png", attachment_id="att_x")
    assert img.wire_dict()["attachment_id"] == "att_x"


def test_sticker_list_response_roundtrip():
    # 契约单源（plan review I6）——/api/stickers 响应须有模型
    r = StickerListResponse(stickers=[
        StickerEntry(stickerId="st_happy", src="/static/stickers/st_happy.png"),
    ])
    assert StickerListResponse.model_validate(r.wire_dict()) == r


def test_server_info_build_sha_and_media_upload():
    info = ServerInfoMessage(
        protocol_version="2.0", server_version="0.1.0+gabcdef",
        features=ServerInfoFeatures(media_upload=True),
        services={},
    )
    assert info.build_sha is None  # 可选缺省
    d = info.wire_dict()
    assert d["server_version"] == "0.1.0+gabcdef"
    assert d["features"]["media_upload"] is True


def test_error_message_request_id():
    m = ErrorMessage(message="x", code="X", requestId="req_1")
    assert m.wire_dict()["requestId"] == "req_1"
    assert ErrorMessage(message="x").wire_dict().get("requestId") is None


def test_attachment_sign_response_roundtrip():
    r = AttachmentSignResponse(attachmentId="att_abc", url="/api/attachments/att_abc?sig=x",
                               expiresAt="2026-08-07T00:00:00Z")
    assert AttachmentSignResponse.model_validate(r.wire_dict()) == r
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /home/yomu/elysia-agent && PATH="/home/yomu/miniconda3/envs/Elysia/bin:$PATH" python -m pytest frontend_contract/tests/test_batch7_contract.py -q`
Expected: FAIL（ImportError：AttachmentRef 等不存在）

- [ ] **Step 3: 扩展 part.py**

在 `core/schema/part.py`：
- `PartType` 加 `STICKER = "sticker"`。
- `FileMime` 加 `APPLICATION_ZIP = "application/zip"`、`TEXT_MARKDOWN = "text/markdown"`。
- `ImagePart`/`FilePart`/`AudioPart`/`VideoPart` 各加 `attachment_id: str | None = None`。
- 新增 `StickerPart`（`type: Literal[PartType.STICKER]`、`sticker_id: str = ""`、`src: Src = ""`）。
- 判别联合 `Part` 加 `StickerPart`。

- [ ] **Step 4: 扩展 rest.py**

在 `frontend_contract/rest.py` 加三个模型：

```python
class AttachmentInfo(ContractModel):
    attachmentId: str
    kind: str
    mime: str
    size: int
    url: str
    signedUrl: Optional[str] = None      # fail-closed 省略
    filename: str = ""


class AttachmentUploadResponse(ContractModel):
    attachments: list[AttachmentInfo]


class AttachmentSignResponse(ContractModel):
    attachmentId: str
    url: str
    expiresAt: str


class StickerEntry(ContractModel):
    """内置表情条目（plan review I6——契约单源，禁止 plain dict）。"""
    stickerId: str
    src: str


class StickerListResponse(ContractModel):
    stickers: list[StickerEntry]
```

- [ ] **Step 5: 扩展 ws_message.py**

- `ContentPart` 联合加 `FilePart/AudioPart/VideoPart/StickerPart`（镜像 part.py；**`ImagePart`/`FilePart`/`AudioPart`/`VideoPart` 各带 `attachment_id: Optional[str]`，`StickerPart` 无**——spec §7.2 Minor 8，plan review III-M6；既有 `ImagePart` 同加）。
- 新增 `AttachmentRef`（attachmentId/kind/mime）。
- `V2TextMessage` 加 `attachments: Optional[list[AttachmentRef]]` + `stickerId: Optional[str]`。
- `ServerInfoMessage` 加 `build_sha: Optional[str]`；`ServerInfoFeatures` 加 `media_upload: bool = False`。
- `ErrorMessage` 加 `requestId: Optional[str]`。

- [ ] **Step 6: 更新既有测试 test_server.py:776**

`core/input/tests/test_server.py` 的「file part 判非法丢弃」用例（:776 附近）——ContentPart 扩展后 file part 变合法，替换素材为真正非法 part `{"type": "unknown"}`；`:783` 注释「ContentPart 判别联合只认 text/image」同步改。

- [ ] **Step 7: 运行 round-trip + gen-types 门禁**

Run: `cd /home/yomu/elysia-agent && PATH="...Elysia/bin:$PATH" python -m pytest frontend_contract/tests/ core/input/tests/test_server.py -q` 然后 `make gen-types && make check-types`
Expected: 全部 PASS + check-types 退出 0。

- [ ] **Step 8: Commit**

```bash
git add core/schema/part.py frontend_contract/rest.py frontend_contract/ws_message.py frontend_contract/tests/test_batch7_contract.py core/input/tests/test_server.py frontend/src/types/contract.generated.ts
git commit -m "feat(batch7): 契约层——媒体 Part/附件模型/签名响应/stickerId/build_sha/media_upload/requestId"
```

---

### Task 2: 存储层 — AttachmentRepository + AttachmentFileResolver

**Files:**
- Create: `core/storage/attachment_repo.py`
- Create: `core/schema/adapters/attachment_resolver.py`
- Test: `core/storage/tests/test_attachment_repo.py`（新建，service marker）

**Interfaces:**
- Consumes: `Module.database.documentdb.DocumentDBBackend`/`DocumentDBFactory`；`core.storage.base.Repository`（**含 abstract `connect/close/health_check`——方法清单须含 `close()`，否则子类无法实例化**，plan review I3）；`core.utils.message_id._new_id`；`core.schema.utils.now_iso`；`ProjectPath.DATA_UPLOADS`。
- Produces: `AttachmentRepository(db=None, *, database_name="default", base_url=None)`——`connect()->bool`、`close()->None`、`health_check()->bool`、`create(doc)->None`（fail-closed：离线 raise `StorageUnavailableError`）、`get(attachment_id)->dict|None`（离线 raise）、`add_ref(attachment_id, message_id)->None`、`remove_ref(attachment_id, message_id)->None`、`mark_deleted(attachment_id)->None`、**`delete_record(attachment_id)->None`（物理删记录，GC 规则 1 用，plan review II-1）**、`find_by_status(status)->list[dict]`、`find_unreferenced_older_than(iso)->list[dict]`（**比对 `created_at`**——未关联 7 天回收，plan review III-M4）。`AttachmentFileResolver`：模块级 `register(attachment_id, kind, stored_name)` / `unregister` / `load(records)` / `resolve(attachment_id)->Path|None`。
- **repo 实例归属（plan review C2，Task 2 定一）**：repo 归 **`agent.attachment_repo`（Agent 新公开 property，lazy 初始化到 `self._attachment_repo`）**；`Agent.start()` 调 `await repo.connect()`（失败记 warning 不阻塞启动）、`Agent.stop()` 调 `close()`。所有 handler 经 `request.app["agent"].attachment_repo` 取同一实例；**测试注入：直接覆写 `agent._attachment_repo = fake`（`# noqa: SLF001`，Agent property 只读无 setter，plan review Minor 2）**。Task 3-7 均走此路径，不再各自发明 wiring。

- [ ] **Step 1: 写 failing 测试**

创建 `core/storage/tests/test_attachment_repo.py`，用内存 fake（仿 generation_repo 测试模式）验证：create/get 离线 raise StorageUnavailableError、add_ref/remove_ref 维护 refs 与 ref_count、mark_deleted 置 status、**delete_record 物理删记录（GC 规则 1，plan review II-1）**、find_unreferenced_older_than、**close() 可用**。resolver 测试：register→resolve 返回路径、unregister 后 None。**Agent 归属测试**：`agent._attachment_repo = fake` 后 `agent.attachment_repo` 返回 fake（plan review Minor 2）。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/storage/tests/test_attachment_repo.py -q`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 AttachmentRepository**

仿 `GenerationRepository`：`connect()` 建 `attachments` 集合 + unique index `attachment_id` + `ref_count`/`status` 索引 + `close()` 关闭连接；**所有写/读在 `self._db is None` 时 raise `StorageUnavailableError`（fail-closed，区别于 GenerationRepository 的静默 no-op）**。`create` 用 `update_one(upsert=True)`；**`delete_record` 用 `delete_one`（物理删，GC 规则 1，plan review II-1）**。

- [ ] **Step 4: 实现 AttachmentFileResolver + Agent 归属**

模块级 dict + register/unregister/load/resolve。`resolve` 返回 `data/uploads/{kind}/{stored_name}` 的 Path（基于 `ProjectPath.DATA_UPLOADS`）。**Agent 加公开 `attachment_repo` property（lazy 初始化到 `self._attachment_repo`，plan review Minor 2）**，`Agent.start()`（实现在 `core/agent/lifecycle.py`）调 `await repo.connect()`、`Agent.stop()` 调 `close()`。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest core/storage/tests/test_attachment_repo.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/storage/attachment_repo.py core/schema/adapters/attachment_resolver.py core/storage/tests/test_attachment_repo.py core/agent/agent.py core/agent/lifecycle.py
git commit -m "feat(batch7): 存储层——AttachmentRepository(fail-closed, 含 delete_record) + AttachmentFileResolver + agent.attachment_repo 归属"
```

---

### Task 3: 上传端点 — POST /api/uploads + filetype 校验 + 中间件前置

**Files:**
- Modify: `core/input/api_routes.py`（新增通用上传 handler）
- Modify: `core/input/server.py`（payload 中间件豁免 /api/uploads、max_request_bytes 8→64MB、静态挂载收敛、sticker 注册表 load）
- Modify: `core/config/app_config.py` + `config.yaml`（attachment_sig_secret、max_request_bytes 默认 64MB）
- Modify: `requirements.txt`（filetype）
- Test: `core/input/tests/test_upload.py`（新建，service marker）

**Interfaces:**
- Consumes: `AttachmentRepository`；`filetype`；`AttachmentUploadResponse`；`_protocol_error_response`。
- Produces: `POST /api/uploads` handler（multipart files[] + kind，0-6 步校验，返回 AttachmentUploadResponse）；`server.py` 中间件豁免 + 64MB + 静态挂载收敛。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_upload.py`（service）：用 aiohttp test client + 内存 fake AttachmentRepository（经 `agent.attachment_repo` 注入，plan review C2）：
- 伪造类型：改名 `.png` 的 JPEG/文本 → 415 `UNSUPPORTED_MEDIA_TYPE`。
- 数量超限（11 文件）→ 400 `TOO_MANY_FILES`。
- kind=sticker → 400 `CONTRACT_VALIDATION`。
- 成功：返回 attachmentId + **signedUrl 含 `sig=` 与 `exp=` 参数**（plan review C1 真签名断言）+ 落盘 + 写 attachments + **`AttachmentFileResolver` 已注册**。
- 离线：repo 未连接 → 503 `STORAGE_UNAVAILABLE`。
- **`/api/uploads` 无 Bearer → 401**（plan review I8，配置 AGENT_ACCESS_TOKEN 时）。
- **整体 multipart >64MB → 413 `REQUEST_TOO_LARGE`**（plan review I8，即使单文件合规）。
- **filename 脱敏**：上传 `../../evil.png` → 返回 filename 不含路径分隔符（plan review I5）。
- **fail-closed 省略 signedUrl**：AGENT_ACCESS_TOKEN 配 + SIG 未配 → 成功响应 signedUrl 缺省（plan review C1）。
- **整体 multipart >64MB → 413 `REQUEST_TOO_LARGE`（Content-Length 预检）**：aiohttp 3.14.3 实测 `read_chunk()` 不强制 `client_max_size`（plan review III-B）——整体上限由**保留的中间件 `Content-Length > cms` 预检**承担（`server.py:108`），非 transport 异常。per-part 超限由 handler read_chunk 自限。
- **desktop 上传 >64MB → 413 `REQUEST_TOO_LARGE`**（同 Content-Length 预检，非 500，plan review II-2/III-B）。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_upload.py -q`
Expected: FAIL（handler 不存在）。

- [ ] **Step 3: requirements.txt 加 filetype + app_config 加字段**

`requirements.txt` 加 `filetype>=1.2`；`app_config.py` server 段加 `attachment_sig_secret: str = ConfigField(default="")`，`max_request_bytes` 默认 `8*1024*1024` → `64*1024*1024`；config.yaml 同步。

- [ ] **Step 4: 实现签名工具 + 通用上传 handler**

`api_routes.py` 新增 **`_sign_url(secret, attachment_id) -> (url, expires_unix)`**——**HMAC 构造钉死（plan review III-M3）**：`sig = hmac_sha256(secret, f"{attachment_id}:{exp_unix}").hexdigest()`，`url = f"/api/attachments/{attachment_id}?sig={sig}&exp={exp_unix}"`，24h（`exp_unix = int(time.time()) + 86400`；**返回 unix 整数，sign_handler 负责转 ISO `expiresAt`，plan review Minor 3**——下载 query 用 unix `exp`，勿两处各造一套，plan review C1 提前到本任务——上传返回真签名，Task 4 复用）+ `upload_handler`（`POST /api/uploads`）：multipart `files[]` + `kind`（可选）；按 spec §4.1 步骤 0-6（kind 枚举预检 → 数量 → 初步大小 → filetype 魔数 → 扩展名一致 → kind 归位含 webm tiebreaker → 归位后大小复核 → **filename 脱敏**（去路径分隔符/截断 255/剥控制字符，plan review I5）→ 写盘 + sha256 + 写 attachments，写失败删文件 503）。kind 目录基于 `ProjectPath.DATA_UPLOADS` 下 `{kind}/`（image/video/audio/file）。**上传成功后 `AttachmentFileResolver.register(attachment_id, kind, stored_name)`**（plan review I1——否则新传图片喂 LLM 立即死链）。返回每文件 AttachmentInfo + **真 `signedUrl`**（fail-closed 模式省略，plan review C1）。

- [ ] **Step 5: server.py 64MB + 静态挂载收敛（保留中间件预检，plan review III-B）**

- **`max_request_bytes` 默认提到 64MB**（`app_config.py` `max_request_bytes: 8MB→64MB` + config.yaml）——修「10MB 图片被 8MB Content-Length 预检拦」的隐患。
- **`_make_payload_middleware` 保留**（不豁免 /api/uploads）：`Content-Length > cms` → 413 `REQUEST_TOO_LARGE` 承担整体 64MB 聚合上限（aiohttp 3.14.3 实测 `read_chunk()` 不强制 `client_max_size`，transport 不抛 `HTTPRequestEntityTooLarge`——**plan 的 spec 原「整段跳过中间件 + handler 自 catch transport 413」机制在 3.14.3 下是死代码，改为保留预检**，记录在案，文档阶段同步 spec §4.1）。per-part 超限由各 handler `read_chunk` 自限：generic → 413 `REQUEST_TOO_LARGE`；desktop（`api_routes.py:670-673` 既有）→ 保持 `INVALID_REQUEST`（413，desktop 既有契约）。
- `add_static("/uploads/", ...)` 收敛为 `add_static("/uploads/images/", images_dir)` + `add_static("/uploads/desktop/", desktop_dir)`。
- `server.py:150` 注释在 read_chunk 语义下**本来就正确**（multipart 手工限制不受 cms 约束），无需「修正」——保留原样。

- [ ] **Step 6: 运行验证通过（含静态收敛回归）**

Run: `python -m pytest core/input/tests/test_upload.py core/input/tests/test_server.py -q`
Expected: PASS。**补静态收敛断言（plan review I8）**：`GET /uploads/image/{uuid}.png`（新 kind 单数目录）→ 404；`GET /uploads/images/{legacy}` → 200。

- [ ] **Step 7: Commit**

```bash
git add requirements.txt core/config/app_config.py config.yaml core/input/api_routes.py core/input/server.py core/input/tests/test_upload.py
git commit -m "feat(batch7): 通用上传 /api/uploads + filetype 真值校验 + 中间件豁免/64MB/静态收敛"
```

---

### Task 4: 签名下载 + 签发 — /api/attachments/{id} + /sign + Range

**Files:**
- Create: `core/input/attachment_routes.py`（下载 + 签发 handler，或并入 api_routes.py）
- Modify: `core/input/server.py`（注册 /api/attachments 路由、中间件精确豁免 `not endswith("/sign")`、下载不记 access log）
- Test: `core/input/tests/test_attachment_download.py`（新建，service marker）

**Interfaces:**
- Consumes: `AttachmentRepository`（经 `agent.attachment_repo`）；`ATTACHMENT_SIG_SECRET`；`aiohttp.web.FileResponse`；**Task 3 的 `_sign_url`（plan review C1 复用，不重复实现）**。
- Produces: `GET /api/attachments/{id}?sig=&exp=` handler（fail-closed 早退 → sig 格式 → active → exp 区间 → compare_digest → FileResponse 带 Range → **校验成功后 `AttachmentFileResolver.register`（再预热，plan review I4）**）；`GET /api/attachments/{id}/sign`（Bearer 保护，签发 24h URL，Mongo 离线 503，顺带 register）；fail-closed 判定（AGENT_ACCESS_TOKEN 配而 SIG 未配 → 403 ATTACHMENT_SIGNATURE_DISABLED）。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_attachment_download.py`（service）。**配置矩阵夹具（plan review I7）**——每场景显式设 token/SIG 两布尔：
- `(token=配, SIG=配)`：
  - 无 sig → 403 `INVALID_SIGNATURE`；sig 篡改 → 403 `INVALID_SIGNATURE`；exp 过期 → 403 `SIGNATURE_EXPIRED`；软删附件 → 404 `ATTACHMENT_NOT_FOUND`；有效签名 → 200 + Content-Type + 支持 Range（`Range: bytes=0-9` → 206 + Content-Range）；`/sign` 无 Bearer → 401、有 Bearer → 200 `{attachmentId, url, expiresAt}`（**含 attachmentId，plan review I7**）；**尾斜杠 `/sign/` → 404 不绕过**（plan review I8）。
  - `(token=配, SIG=空)`：下载 → 403 `ATTACHMENT_SIGNATURE_DISABLED`；sign → 403 `ATTACHMENT_SIGNATURE_DISABLED`（fail-closed）。
  - `(token=空, SIG=任意)`：下载跳过 sig 校验但 status=active 仍校验（软删 404）；sign 正常（fail-open）。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_attachment_download.py -q`
Expected: FAIL（路由不存在）。

- [ ] **Step 3: 实现下载/签发 handler**

`attachment_routes.py`：`download_handler`（fail-closed 早退 → ①②③④ 校验序 → `web.FileResponse(path, headers={'Content-Type': mime})`，aiohttp 原生 Range/206 → **校验成功 `AttachmentFileResolver.register`**（plan review I4））；`sign_handler`（Bearer 由中间件保证 → 查记录 active → `_sign_url` 签 URL → **`expires_unix` 转 ISO `expiresAt`（plan review Minor 3）** → register）。`/sign` 路由挂 Bearer 保护（中间件精确豁免 `startswith("/api/attachments/") and not endswith("/sign")`）。

- [ ] **Step 4: server.py 注册 + 中间件豁免**

`create_app` 注册 `add_get("/api/attachments/{attachment_id}", download_handler)` + `add_get("/api/attachments/{attachment_id}/sign", sign_handler)`；`_make_access_auth_middleware` 豁免条件改精确；下载路径不记 access log（若启用则剔 query）。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest core/input/tests/test_attachment_download.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/input/attachment_routes.py core/input/server.py core/input/tests/test_attachment_download.py
git commit -m "feat(batch7): 附件签名下载 + Range + 签发端点 /sign + fail-closed"
```

---

### Task 5: 消息关联 — V2TextMessage.attachments/stickerId + process bool + refs + 删除联动 + 视觉适配

> **前置（plan review III-A）**：本 Task 的 sticker 测试依赖 Task 6 的 `sticker_registry.py` 与 `static/stickers/` PNG——执行顺序为 Task 6 之后。若实现者按编号顺序执行，先做 Task 6 再做本 Task。

**Files:**
- Modify: `core/input/ws_handler.py`（_route_v2_text 扩展 + 入口门放宽 + 幂等摘要含附件）
- Modify: `core/agent/agent.py`（process_message 返回 bool）
- Modify: `core/schema/adapters/message_adapter.py`（_resolve_image_source 接 resolver）
- Modify: `core/schema/adapters/attachment_resolver.py`（Task 2 已有，补接线）
- Modify: `core/input/session_routes.py`（资源索引软删过滤 + sticker 分类填充；会话删除 `:437` refs 递减）
- Modify: `core/input/debug_routes/archive.py`（四轮 review Important——`archive_delete_handler`/`archive_clear_handler`/`session_delete_handler`/`sessions_clear_handler` 删消息/会话时 refs 递减）
- Test: `core/input/tests/test_attachment_message.py`（新建，service）

**Interfaces:**
- Consumes: `V2TextMessage`（attachments/stickerId）、`AttachmentRepository`、`AttachmentFileResolver`、`process_message`。
- Produces: v2 text 附件分支 0-5 步（校验→lookup→落 entry→构建 parts→process+ACK+refs→入口门）；`process_message` 返回 bool；`_resolve_image_source` 解析 `/api/attachments/{id}` → data URI；删除联动递减 refs；资源索引过滤软删/填充 sticker。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_attachment_message.py`（service）：
- v2 text 带 attachments → 校验通过 → process_message 被调且 content 为 dict 列表 → ack accepted → refs+1。
- 附件 404 → ack rejected、不落幂等 entry。
- stickerId 不在注册表 → 400 CONTRACT_VALIDATION。
- 空文本 sticker-only → 进门 → 触发生成。
- 队列满 drop（mock process 返回 False）→ 删 entry + ack rejected QUEUE_FULL + refs 不递增。
- 同文本不同附件 → 摘要不同 → 正常处理。
- 删除消息 → refs 递减 → ref_count=0 → mark_deleted。
- `_resolve_image_source("/api/attachments/att_x")` → data URI（注册映射后）。
- **资源索引（plan review III-M8）**：含 StickerPart 的消息 → `GET /api/sessions/{id}/resources?category=sticker` 返回条目（不再恒空）；含软删附件 → 过滤（`/api/attachments/` 前缀项），legacy `/uploads/images/*` 不过滤。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_attachment_message.py -q`
Expected: FAIL。

- [ ] **Step 3: process_message 返回 bool**

`agent.py:304-351`：空消息 → `return False`；队列满 → `return False`；入队/直发 → `return True`。**5 个非重写调用方不动**（`ws_handler.py:537/541`、`api_routes.py:723`、`core/agent/server.py:493`（REPL run_server，非 core/input/server.py，四轮 Minor 3）、`bench.py:159`，均忽略返回值，plan review III-M7；`ws_handler.py:446` 在 Task 5 本步一并重写）。

- [ ] **Step 4: _route_v2_text 附件分支**

`ws_handler.py`：入口门放宽 `text_content.strip() or data.get("stickerId") or data.get("attachments")`；附件/sticker 校验（先于 lookup）→ 幂等摘要含附件 → lookup → 落 entry → 构建 parts（`[p.model_dump() for p in parts]`，**StrEnum 序列化注记：`model_dump()` 对 `type` 字段输出 StrEnum 成员，`parts_from_dicts` 判别用相等性成立，可接受**——plan review Minor 10）→ `await process_message(..., content=content)` → True 则 ack accepted + refs+1（`add_ref`），False 则删 entry + ack rejected QUEUE_FULL。**refs 写盘失败**（发送前校验已读 Mongo，窗口极小）→ 记 warning，由 GC 规则 3（7 天未关联）兜底（plan review Minor 5）。附件消息 ACK 延后到 process 后；纯文本保持现状。

- [ ] **Step 5: _resolve_image_source 接 resolver**

`message_adapter.py:_resolve_image_source`：src 匹配 `/api/attachments/{attachment_id}` → `AttachmentFileResolver.resolve(attachment_id)` → `_to_data_uri`；未命中原样返回。（上传侧 register 已在 Task 3 Step 4；本步仅接线视觉。）

- [ ] **Step 6: 删除联动 + 资源索引**

- 会话删除（`session_routes.py:437`）/消息删除/归档删除（`debug_routes/archive.py` 的 archive_delete/archive_clear/session_delete/sessions_clear handler，四轮 review Important）路径：遍历消息附件 part，`remove_ref(attachment_id, message_id)`；ref_count==0 → `mark_deleted` + **`AttachmentFileResolver.unregister`**（plan review Minor 3）。
- `session_routes.py` 资源索引（plan review Minor 4 具体改动）：① **删掉 `:610-613` 的 sticker 恒空硬编码分支**（不再提前短路）；② `_extract_from_message`（:202-229）加 `elif category == "sticker"` 扫描 StickerPart → **`{kind, url=/static/stickers/{id}.png}`**（messageId/timestamp 由 `_items_from_message` 补，镜像 image 分支，四轮 Minor 7）；③ 软删过滤仅对 `/api/attachments/` 前缀项（legacy `/uploads/images/*` 不过滤）。

- [ ] **Step 7: 运行验证通过**

Run: `python -m pytest core/input/tests/test_attachment_message.py core/input/tests/test_server.py -q`
Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add core/input/ws_handler.py core/agent/agent.py core/schema/adapters/message_adapter.py core/schema/adapters/attachment_resolver.py core/input/session_routes.py core/input/debug_routes/archive.py core/input/tests/test_attachment_message.py
git commit -m "feat(batch7): 附件/sticker 消息关联——v2 wire + process bool + refs + 删除联动（含归档）+ 视觉适配"
```

---

### Task 6: 表情包 — sticker_registry + StickerPart 接线 + /api/stickers

**Files:**
- Create: `core/input/sticker_registry.py`
- Create: `static/stickers/`（10-20 个占位 PNG，可用最小 PNG 生成）
- Modify: `core/input/api_routes.py`（GET /api/stickers + load_stickers 调用）
- Test: `core/input/tests/test_stickers.py`（新建，service）

**Interfaces:**
- Consumes: `ProjectPath.STATIC`。
- Produces: `core/input/sticker_registry.py` 模块级 `dict[sticker_id, path]` + `load_stickers()`（扫描 `static/stickers/*.png`，id = 去扩展名文件名）+ `get(sticker_id)`；`GET /api/stickers` 返回 `[{stickerId, src}]`；`create_app` 内 `load_stickers()`。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_stickers.py`（service）：
- `load_stickers()` 扫描 `static/stickers/` 注册表非空（**直接断言真 PNG 提交后非空，不 monkeypatch**——plan review Minor 8）。
- `GET /api/stickers` 返回 `StickerListResponse`（**契约模型，plan review I6**）列表。
- `get("st_happy")` → 路径存在。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_stickers.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现 registry + 端点**

生成最小 1x1 PNG（base64 常量）到 `static/stickers/st_happy.png`、`st_heart.png` 等（10-20 个，可同图复制）；实现 `sticker_registry.py`；`GET /api/stickers` handler 构造 **`StickerListResponse`** + `wire_dict()`（plan review I6 契约单源）。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest core/input/tests/test_stickers.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/sticker_registry.py static/stickers/ core/input/api_routes.py core/input/server.py core/input/tests/test_stickers.py
git commit -m "feat(batch7): 内置表情包——sticker_registry + /api/stickers（契约模型，create_app 加载）"
```

---

### Task 7: GC — 启动兜底扫描

**Files:**
- Create: `core/storage/attachment_gc.py`
- Modify: `core/agent/lifecycle.py` 或 `core/agent/server.py`（启动调用）
- Test: `core/storage/tests/test_attachment_gc.py`（新建，service）

**Interfaces:**
- Consumes: `AttachmentRepository`、`ProjectPath.DATA_UPLOADS`。
- Produces: `run_attachment_gc(repo, *, deleted_retention=24h, unreferenced_retention=7d) -> dict`（统计）——四 kind 目录、规则 1-4、Mongo 离线跳过记日志。

- [ ] **Step 1: 写 failing 测试**

`core/storage/tests/test_attachment_gc.py`（service，临时目录 + 内存 fake repo）：
- deleted 超 24h → 删文件 + 删记录。
- active 文件缺失 → 标记 deleted。
- active ref_count=0 超 7 天 → 软删。
- 磁盘孤儿（无记录）→ 删文件。
- `data/uploads/images/`、`desktop/` 遗留目录不被扫。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/storage/tests/test_attachment_gc.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现 GC**

`attachment_gc.py`：扫描四 kind 目录；规则 1-4（**规则 1「deleted 超 24h → 删文件 + 删记录」调 `repo.delete_record(attachment_id)`**，plan review II-1）；**清理时同步 `AttachmentFileResolver.unregister`**（plan review Minor 3）；每类计数记日志；离线跳过。

- [ ] **Step 4: 启动接线**

`Agent.start()`（core/agent/lifecycle.py）后台任务调用 `run_attachment_gc(repo)`——**在 `repo.connect()` 成功之后启动（或 GC 自身离线跳过兜底），plan review Minor 4**；不阻塞启动；`AttachmentFileResolver.load` 预热。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest core/storage/tests/test_attachment_gc.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/storage/attachment_gc.py core/agent/lifecycle.py core/storage/tests/test_attachment_gc.py
git commit -m "feat(batch7): 启动 GC 兜底扫描——deleted/孤儿/未引用清理（限四 kind 目录）"
```

---

### Task 8: 版本 — build_sha + media_upload feature + ServerInfoMessage

**Files:**
- Modify: `core/version.py`（get_build_sha 模块级缓存）
- Modify: `core/agent/agent.py`（Agent.start 调 get_build_sha，存 agent 或全局）
- Modify: `core/input/capabilities.py`（build_server_info 用 build_sha + media_upload=True）
- Test: `core/tests/test_build_info.py`（新建，unit；plan review III-M2 统一——与既有 `core/input/tests/test_version.py`（WS 握手版）分离）

**Interfaces:**
- Consumes: `core.version.__version__`。
- Produces: `core.version.get_build_sha() -> str | None`（subprocess git rev-parse HEAD，失败 None，模块级缓存）；`build_server_info` 的 `server_version = __version__ + f"+{sha[:7]}"`；`features.media_upload=True`。

- [ ] **Step 1: 写 failing 测试**

新建 `core/tests/test_build_info.py`（unit，**plan review Minor 2——与既有 `core/input/tests/test_version.py`（WS 握手版）分离**）：
- `get_build_sha()` 非 None 或 None（git 环境相关，断言不抛异常 + 缓存复用）。
- `build_server_info`（用最小 agent fixture：llm/tts/stt config + session）的 `server_version` 含 `__version__`、`build_sha` 上 wire 非 None（若 git 可用）、`features.media_upload` True。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/tests/test_build_info.py -q`
Expected: FAIL（media_upload 不存在）。

- [ ] **Step 3: 实现**

`core/version.py` 加 `get_build_sha()`；`capabilities.py` 构造 `server_version = __version__ + f"+{sha[:7]}"`、**`build_sha=sha`（字段上 wire，非仅拼版本串，plan review Minor 7）**、`features.media_upload=True`；`Agent.start` 调 `get_build_sha()` 缓存。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest core/tests/test_build_info.py core/input/tests/test_server.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/version.py core/agent/agent.py core/input/capabilities.py core/tests/test_build_info.py
git commit -m "feat(batch7): 版本——git build_sha（版本串+build_sha 字段）+ media_upload 如实声明"
```

---

### Task 9: 健康 — /health//ready 拆分 + 真依赖检查

**Files:**
- Modify: `core/input/server.py`（health_handler 拆两个 + 依赖检查 + 版本）
- Modify: `core/input/tests/test_server.py:801-822`（fixture AsyncMock）
- Test: `core/input/tests/test_health.py`（新建，service）

**Interfaces:**
- Consumes: `agent.session.health_check`、`agent.long_term.health_check`、`get_build_sha`、`agent.status.current_name`。
- Produces: `/health` 恒 200（status ok/degraded + **`fsm` 保留**（plan review III-M1——既有 `test_server.py:833` 断言 `data["fsm"] == "idle"`）+ version/build_sha/uptime/request_id/dependencies）；`/ready` 依赖不 ok → 503；依赖检查 `asyncio.wait_for(2s)`。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_health.py`（service）：
- fixture `agent.session.health_check`/`agent.long_term.health_check` 设 AsyncMock(True) → /health status ok、/ready 200。
- 依赖一 false → /health degraded、/ready 503。
- 既有 `test_server.py` health 测试同步更新 fixture。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_health.py -q`
Expected: FAIL（handler 未拆）。

- [ ] **Step 3: 实现拆分**

`server.py`：`health_handler`（恒 200 + dependencies/version/build_sha/uptime/request_id）+ `ready_handler`（依赖不 ok → 503）；`asyncio.wait_for(..., 2)` 包 health_check；`app.router.add_get` 分别注册。

- [ ] **Step 4: 更新既有 fixture**

`test_server.py:801-822` 的 MagicMock → `agent.session.health_check = AsyncMock(return_value=True)` 等。

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest core/input/tests/test_health.py core/input/tests/test_server.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add core/input/server.py core/input/tests/test_health.py core/input/tests/test_server.py
git commit -m "feat(batch7): /health//ready 拆分 + 真依赖检查 + 版本/uptime/request_id"
```

---

### Task 10: 日志收敛 — 正文打印降级 + 敏感扫描测试

**Files:**
- Modify: `core/input/ws_handler.py`（:512/:538/:906）
- Modify: `core/cognitive/loop.py`（:450）
- Test: `core/input/tests/test_log_sensitivity.py`（新建，unit/service）

**Interfaces:**
- Consumes: 现状 logger 调用。
- Produces: 四处正文打印收敛为长度/ID；`test_log_sensitivity.py` 用 caplog 限定 logger 断言不含正文/密钥/路径。

- [ ] **Step 1: 写 failing 测试**

`core/input/tests/test_log_sensitivity.py`：构造触发发送路径，**caplog 同时限定 `core.input.ws_handler` 与 `core.cognitive.loop`**（四轮 Minor 6——loop.py 的用户输入日志走 `core.cognitive.loop` logger，第四处收敛须有断言），断言不含用户正文、不含 `/uploads/` 本地路径、不含 `ATTACHMENT_SIG_SECRET`。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_log_sensitivity.py -q`
Expected: FAIL（当前打印正文）。

- [ ] **Step 3: 收敛四处**

- `ws_handler.py:538`（WS 收到文本）→ 只记 `len` + message_id。
- `ws_handler.py:512`（voice.commit 成功）→ 只记 recordingId + len。
- `ws_handler.py:906`（STT 识别）→ 只记长度/段 ID。
- `loop.py:450`（四轮 review 实测 `logger.info(">>> 用户输入 ...")` 在 :450，:449 是 `emo_tag =` 赋值）（用户输入）→ 只记长度/ID。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest core/input/tests/test_log_sensitivity.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/ws_handler.py core/cognitive/loop.py core/input/tests/test_log_sensitivity.py
git commit -m "fix(batch7): 日志正文打印收敛 + 敏感扫描测试"
```

---

### Task 11: 关联 ID — ErrorMessage.requestId + GenerationRegistry.request_id

**Files:**
- Modify: `frontend_contract/ws_message.py`（ErrorMessage.requestId，Task 1 已加，补发送点）
- Modify: `core/input/ws_handler.py`（WS 错误发送点补 requestId）
- Modify: `core/cognitive/generation.py`（GenerationContext 加 request_id）
- Modify: `core/cognitive/generation_registry.py`（register 记 request_id）
- Modify: `core/cognitive/loop.py`（GenerationContext 构造传 request_id）
- Test: `core/input/tests/test_request_id.py`（**新建**——plan review I2：既有 `core/tests/test_request_id.py` 测的是 `core.utils` 的 ContextVar，与本任务无关，勿扩展）

**Interfaces:**
- Consumes: `UserInputData.request_id`、`GenerationContext`。
- Produces: `ErrorMessage.requestId` 携带 WS 侧 request_id；`GenerationContext.request_id` + `GenerationRegistry.register` 记 `request_id`。

- [ ] **Step 1: 写 failing 测试**

**新建** `core/input/tests/test_request_id.py`：WS 错误发送点构造 ErrorMessage 带 requestId；generation register 后 doc 含 request_id。

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest core/input/tests/test_request_id.py -q`
Expected: FAIL。

- [ ] **Step 3: 实现**

`generation.py` GenerationContext 加 `request_id: str | None`；loop 构造时传 `UserInputData.request_id`；`generation_registry.register` 落 `request_id`；WS 错误发送点（`ws_handler.py:273,276,350,820-853`、`voice_sessions.py:119`）补 `requestId`（有则带、无则 None）。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest core/input/tests/test_request_id.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend_contract/ws_message.py core/input/ws_handler.py core/input/voice_sessions.py core/cognitive/generation.py core/cognitive/generation_registry.py core/cognitive/loop.py core/input/tests/test_request_id.py
git commit -m "feat(batch7): 关联 ID——WS 错误带 requestId + GenerationRegistry 记 request_id"
```

---

### Task 12: 文档与验收同步

**Files:**
- Modify: `docs/electron-backend-protocol-v2.md`（§13 媒体契约、§9.1 错误码 32→40、§9 资源索引 sticker 行、CONTRACT_VALIDATION 场景、INVALID_REQUEST 413 漂移、§1 features、ID 表 att_）
- Modify: `docs/electron-backend-security.md`（§1.2 /api/attachments 豁免、签名 URL 豁免说明、SIG fail-closed 表）
- Modify: `docs/electron-backend-progress.md`（批次 7 完成 + 验收）
- Modify: `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-070/071/081 ✅、完成度 21/24→24/24）
- Modify: `README.md`（core.input 新端点、附件子系统）
- Modify: `CLAUDE.md`（core.storage 附件、core.input 新路由、ServerMessage/契约模型）

**Interfaces:**
- Consumes: 前 11 个任务产物。
- Produces: 文档与代码事实一致（含新增错误码、features、端点、附件模型）。

- [ ] **Step 1: 核对契约事实**

读 `frontend_contract/` 实际模型 + `make check-types` 通过后，逐文档核对。

- [ ] **Step 2: 更新 protocol-v2.md**

§13 新增「通用附件与媒体」契约（/api/uploads、/api/attachments 下载/签发、错误码 8 个、MIME 矩阵、签名 URL）；§9.1 错误码 32→40；§9 资源索引 sticker 行改「经 StickerPart 填充」；CONTRACT_VALIDATION 场景追加；INVALID_REQUEST 413 漂移注；§1 features 加 media_upload；§3 ID 表登记 att_。

- [ ] **Step 3: 更新 security.md**

§1.2 附件豁免说明（/api/attachments 非 /sign 免 Bearer）+ 签名 URL query 豁免；SIG fail-closed 决策表；`/uploads/` 静态挂载收敛说明。

- [ ] **Step 4: 更新 progress.md + docs/14 + README + CLAUDE.md**

- [ ] **Step 5: 核对**

Run: `make check-types` + 快速测试
Expected: 全部通过。

- [ ] **Step 6: Commit**

```bash
git add docs/electron-backend-protocol-v2.md docs/electron-backend-security.md docs/electron-backend-progress.md docs/14-Cyrene-后端配合改造任务清单-v0.1.md README.md CLAUDE.md
git commit -m "docs(batch7): 协议/安全/进度/任务清单/README/CLAUDE 同步——媒体契约 + 错误码 40 + 附件安全"
```

---

## Self-Review

- **Spec coverage**：BE-070（附件存储/上传/签名下载/Range/消息关联/表情/GC）→ Task 1-7；BE-071（缩略图=Range 直出不做服务端、Range、清理）→ Task 4+7；BE-081（版本/健康/日志/关联 ID）→ Task 8-11；文档/验收 → Task 12。12 项决策全部覆盖。
- **Plan review 修复（首轮）**：C1（签名工具提前 Task 3 + upload_handler 真 signedUrl + 成功用例断言 sig=/exp= + fail-closed 省略）、C2（repo 归 `agent.attachment_repo`，Task 2 定一，Agent.start 建连）、I1（上传 register）、I2（Task 11 新建测试）、I3（repo 补 close）、I4（下载 register 再预热）、I5（filename 脱敏）、I6（StickerEntry/StickerListResponse 契约）、I7（Task 4 配置矩阵 + sign 响应补 attachmentId + fail-open 分支）、I8（64MB/静态收敛/尾斜杠/desktop 413/无 Bearer 401 测试）、Minor 1-10（命名统一、build_sha 上 wire、unregister、资源索引具体改动、refs 写盘失败、ImagePart attachment_id、monkeypatch 简化、desktop 双错误码、model_dump 注记）。
- **Plan review 修复（二轮）**：II-1（delete_record + Task 7 规则 1 + 测试）、II-2（desktop transport 413，经三轮 III-B 改法解决）、Minor 1-6（git add 补 lifecycle/voice_sessions、_attachment_repo 注入、_sign_url unix/ISO、GC 接线顺序、conda env 统一）。
- **Plan review 修复（三轮）**：III-A（Task 5 依赖 Task 6——执行顺序注记 + Task 5 前置说明）、III-B（aiohttp 3.14.3 实测 `read_chunk` 不强制 cms——**弃「整段跳过中间件 + handler 自 catch transport 413」死代码机制，改保留中间件 Content-Length 预检承担整体 64MB + per-part read_chunk 自限**；记录在案，文档阶段同步 spec §4.1）、M1-M8（/health 保留 fsm、Task 8 测试文件统一、_sign_url HMAC 构造钉死、GC 比对 created_at、loop.py:449、Task 1 措辞排除 StickerPart、调用方 5 个、资源索引 failing 测试）。
- **Placeholder scan**：无 TBD/TODO；签名工具实现细节（`_sign_url`）Task 3 已给；PNG 占位用 base64 常量生成。
- **Type consistency**：`AttachmentRepository`（create/get/add_ref/remove_ref/mark_deleted/**delete_record**/find_by_status/find_unreferenced_older_than/close）跨 Task 2/3/4/5/7 一致且归 `agent.attachment_repo`；`AttachmentFileResolver`（register/unregister/load/resolve）跨 Task 2/3/4/5/7 一致；`_sign_url` 由 Task 3 定义、Task 4 复用；`process_message` bool 在 Task 5 定义、Task 5 内完成；契约模型（AttachmentInfo/UploadResponse/SignResponse/StickerListResponse）在 Task 1/3/4/6 一致。
