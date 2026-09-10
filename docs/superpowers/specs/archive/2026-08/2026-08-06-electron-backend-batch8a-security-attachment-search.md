# Electron 后端批次 8a — 安全加固 + 附件生命周期 + 搜索合规

**Spec 版本：** v0.2（v0.1 基础上吸收 review：D2 发送者身份设计修正（Blocking）+ asyncio.Lock→threading.Lock + add_ref/remove_ref 幂等条件 + 探活副作用并入 + 计数口径统一 + 措辞/引用修正）  
**文档状态：** 待 review  
**关联任务：** 批次 0-7 全量审查修复（`docs/electron-backend-audit-batches0-7.md`）的 A 安全 / B 附件 / C 搜索 三簇 + BE-030/BE-031/BE-070 三项合规缺口收口  
**任务清单：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md` §5（BE-030/031/070 验收项）  
**审计参考：** `docs/electron-backend-audit-batches0-7.md`（本批次覆盖 A/B/C 簇 + 探活共 20 条审计发现（同根合并为 18 组）+ 3 项合规缺口 BE-030/BE-031/BE-070）  
**基线 Commit：** `610b921`（批次 7 收口 + security.md 64MB 修正）  
**批次组织：** 8a 本 spec（安全+附件+搜索+探活）→ 8b（语音/WS 行为，D 簇）→ 8c（契约/信封/ID/杂项，E/F 簇）。8b/8c 各自独立 spec/plan/goal 流程，本批次不触碰其范围。

---

## 1. 目标与范围

把批次 0-7 全量审查发现的 **A 安全 / B 附件 / C 搜索** 三簇问题修复到 docs/14 合规：堵住远程部署管理面匿名缺口、Desktop 工具结果伪造、附件存在性枚举、SSRF site-local 绕过；补上会话删除的附件引用释放（BE-030+BE-070）、搜索分类组合过滤（BE-031）；收敛日志/错误体的敏感泄漏；并修掉 `/health` 探活的 request_id 副作用（审计 §6 安全类）。

**不做（批次边界，移交 8b/8c）：**
- D 簇 WS/语音行为（断连误杀 generation、队列满 ACK、语音状态机、envelope 字段）→ 8b。
- E/F 簇契约/信封/ID/杂项（admin 裸 error 信封、死契约面、requestId 贯通、Secret Store 加密评估、trace 脚本、声音 current、`session_routes.py:734` v1 排队消息跨会话可见等）→ 8c。
- 审查 REFUTED 4 条不修（delta 越句边界、messageId 未落库、find_context 参数钳制、beta_client 跨源剥头）。**其中 find_context 的残余真实 bug**（`before=-5` → `to_list(length=-5)` 未捕获 500，session_routes.py:75/553-556）不在 REFUTED 理由覆盖内 → 移交 8c 杂项，避免静默丢失。

**验收目标：** 8a 覆盖的发现 + 3 项合规缺口在 `docs/electron-backend-audit-batches0-7.md` 中逐条标记已修复；`make test-normal` 全绿。

## 2. 已确认决策（拍板结论）

| ID | 决策 | 结论 |
|---|---|---|
| D1 | 管理面认证缺口（server.py:295 恒放行） | **fallback 语义**：auth_token 空 + access_token 非空 → 管理端点（/debug/api/*、/shutdown、/restart、/tools/*）改用 access_token 校验；两者都空 → 匿名（本地开发不变）；两者都配 → 严格分离不变。**启动告警**：非回环绑定仅配一个 token → warning 提示补齐另一个。 |
| D2 | tool.status 伪造（bridge.py:255 不校验发送连接） | **每 v2 连接服务端分配稳定连接身份**（handle_ws 对每个 v2 连接生成 `conn_...`，存 channel），tool.status 路由**一律传非 None sender**；`handle_status` 在 `doc.target_connection` 存在且 sender 不匹配 → 拒绝。**移除 None 兼容放行**（仅测试构造可显式传 None 走单测兼容断言）——普通 web v2 连接有自己独立的 conn id，不等于 target_connection → 伪造被拒。残留「抢跑投递目标」向量（冒充 electron 抢先建连接收 tool.request）转 8c 或记录取舍。 |
| D3 | 下载存在性枚举（attachment_routes.py:133 先 repo.get 后验签） | **签名校验全部前置**：签名模式 ① sig hex → ③ exp → ④ HMAC 校验全部通过后才 repo.get；未认证者无有效 sig 恒 403，无法用 404/403 差异枚举附件 ID。 |
| D4 | 搜索分类组合过滤失效（message_repo.py:396 $elemMatch 合并） | **`$and` 两条 `$elemMatch`**（keyword 命中 text part 内容 + category 命中对应 part 类型，跨元素匹配）；**category=link 特判为 URL 内容正则**（链接在 TextPart 内，无 link Part 类型）；未知 category 由 handler 400。 |
| D5 | 会话删除附件引用泄漏（REST 路径漏 release） | **提取共享 helper** 到 `core/storage/attachment_release.py`，wire 进 `session_routes.sessions_delete_handler`（debug 归档路径复用同一 helper）→ 同时闭合 BE-030 + BE-070。 |

**记录在案（最终报告汇报）：** D1 的 fallback 意味着「仅配 AGENT_ACCESS_TOKEN 的远程部署」下，管理端点用产品 token 即可访问——这是 Demo 单 token 语义（docs/14 §6 BE-010「Demo 推荐：单用户访问 Token」）的刻意取舍：比「管理面完全匿名」安全，但严格双域分离需要两个 token 都配置。双 token 场景权限域分离不受影响。

## 3. 修复详述

### 3.1 管理面认证 fallback（D1）

**现状**：`core/input/server.py:286-299` `_check_auth` 在 `auth_token` 空时 `return True` 恒放行；/shutdown（:372）、/restart（:392）、/tools（:411）、/tools/reload（:430）handler **已调用 `_check_auth`**，但 auth_token 空时恒放行；`register_debug_routes(app, _check_auth)`（server.py:461）的 `_debug_auth_middleware`（debug_routes/__init__.py:27-34）同样只靠 `_check_auth`。访问中间件（server.py:108-131）只匹配 /api/、/uploads、/ws，不匹配 /debug。非回环绑定 + 仅配 access_token 的远程部署下，管理面恒放行（认证语义失效，而非无认证接线）。

**改法**：
- `_check_auth` 改为闭包捕获 `auth_token` + `access_token`：
  - `auth_token` 非空 → 校验 `Authorization: Bearer <auth_token>`（原逻辑）；
  - `auth_token` 空 + `access_token` 非空 → 校验 `Bearer <access_token>`（fallback）；
  - 两者都空 → `True`（匿名本地开发）。
- **管理域认证统一**：`register_debug_routes(app, _check_auth)` 的 `_debug_auth_middleware` 已覆盖 /debug/api（保留复用新 `_check_auth`）；/shutdown /restart /tools /tools/reload 的 handler 已包 `_check_auth` → 只需 `_check_auth` 语义修正即可生效，**无需新增管理域中间件**（避免 /debug/api 双重认证，M4）。确认 /shutdown 等 handler 均在调用 `_check_auth`。
- `warn_if_unsecured(host, access_token)`（server.py:43-65）签名加 `auth_token` 参数：非回环 + 仅配一个 token（access/auth 二缺一）→ warning「远程部署建议同时配置 AGENT_ACCESS_TOKEN 与 SERVER_AUTH_TOKEN，以保持管理域独立」；唯一调用点 `core/agent/server.py:371` 与测试 `core/input/tests/test_auth.py:140-158` 同步更新签名。

**测试**：远程姿势（access_token 配、auth_token 空）→ 匿名访问 /debug/api/*、/shutdown、/tools 401；带 access_token → 200；双 token → 严格分离（product token 访问 /shutdown 401，admin token 访问 /api/chat 401）；本地无 token → 匿名回归不变。

### 3.2 tool.status 发送连接校验（D2）

**现状**：`core/desktop/bridge.py:255-283` `handle_status` 只校验 callId 存在 + 篡改字段一致，不校验发送连接；`ws_handler.py:835-845` tool.status 路由不传连接身份；`ws_handler.py:136-138` 的 `desktop_conn_id` 仅 `X-Client-Platform: electron` 时生成，普通 web v2 连接无身份。任意 v2 WS 客户端可冒充桌面 Runtime 接收工具请求并伪造结果（file.select 的 paramHash=sha256('{}') 是公开常量，四字段篡改比对对 file.select 弱约束）。

**改法**：
- **每 v2 连接服务端分配稳定连接身份**：`handle_ws`（ws_handler.py:124-141 一带）对每个 v2 连接生成 `conn_<hex>`，存入 channel 属性（`WebSocketChannel.connection_id`），与现有 `X-Client-Platform: electron` 握手标记并存（electron 连接的 conn id 即 target_connection 的来源，保持 server_info.connectionId 下发语义不变）。
- `handle_status(self, status, *, sender_connection_id: str | None = None)`：
  - `doc.get("target_connection")` 存在且 `sender_connection_id != doc["target_connection"]` → 记日志 + `return False`（拒绝，不 resolve，不 mark failed——防主动造成副作用；与附件持有者校验语义一致）。
  - `sender_connection_id is None`：仅测试代码显式传 None 走单测兼容断言；生产路径一律非 None。
- `ws_handler.py:835` tool.status 路由：从连接上下文取 channel.connection_id 传入，v2 无身份 → 直接拒绝（不调 handle_status）。

**测试**：非 electron 普通 v2 连接对在途 desktop 调用发 tool.status → 拒绝（sender=自身 conn id ≠ target_connection）；错连接 ID → 拒绝；对连接 ID → 正常；`sender_connection_id=None` → 仅单测兼容断言（不覆盖生产行为）。

### 3.3 下载存在性枚举修复（D3）

**现状**：`core/input/attachment_routes.py:114-152` 校验序：① sig hex → ② repo.get（404/active）→ ③ exp → ④ HMAC。未认证调用者可先到 ②，用 404/403 差异枚举附件 ID。

**改法**：签名模式（verify_sig=True）下 ① sig hex → ③ exp → ④ HMAC **全部前置**，通过后才 ② repo.get + status 检查 + 文件存在检查。fail-open（token 空）路径不变（仅 ② status 校验）。

**测试**：匿名无 sig / 错 sig → 403 INVALID_SIGNATURE（不泄露存在性）；有效 sig + 不存在 ID → 404；有效 sig + 存在 → 200/206；exp 过期 → 403 SIGNATURE_EXPIRED（先于 404）。fail-open 行为回归不变。

### 3.4 搜索分类组合过滤（D4）

**现状**：`core/storage/message_repo.py:390-405` query + category 合并为**单条** `$elemMatch`——同一元素既要 content 正则又要 type 匹配，而非文本 Part 无 content 字段 → 组合恒空；category=link 命中 `_CATEGORY_PART_TYPES` 但无 link Part 类型 → 恒空；`session_routes.py:584` search handler 不校验 category 取值。

**改法**：
- `message_repo.query_search`：query 与 category 拆成两条 `$elemMatch` 用 `$and` 组合：
  - query → `content.$elemMatch` 匹配 text part 的 content 字段（`{"type": "text", "content": {"$regex": ...}}`）；
  - category ∈ {text,image,video,audio,file} → `content.$elemMatch` 匹配 `{"type": category}`；
  - category=link → query 正则特判为 `{"type": "text", "content": {"$regex": "(https?://|www\\.)", "$options": "i"}}`（链接在 TextPart 内）；仅 category=link（无 query）时同样生效；
  - 两者都有 → `$and` 两条 `$elemMatch`。
- `session_routes.search_messages_handler`：category 白名单 `{text,image,video,audio,file,link,tool}` 校验，未知值 → 400 CONTRACT_VALIDATION。

**测试**（沿用 FakeDocumentDB 实证）：含 https 消息 category=link 命中；query=关键词 + category=image 组合命中（不同元素）；未知 category → 400；既有 text/image 单条件回归不变。

### 3.5 会话删除附件引用释放（D5）

**现状**：REST `DELETE /api/sessions/{id}`（session_routes.py:471-493）→ `session_manager.delete`（session_manager.py:255-279）删归档+删文档，**无附件 remove_ref**；debug 路径 debug_routes/archive.py:85-105 `_release_session_attachments` + :339-344 已接线。

**改法**：
- 提取 `_release_session_attachments`（archive.py:85-96）+ `_release_messages_attachments` + `_release_message_attachments` + 依赖的 `_attachment_ids_from_message`（archive.py:29-39）+ `_ATTACHMENT_RELEASE_BATCH` 常量到新模块 `core/storage/attachment_release.py`（纯函数，agent 为参数）。`_release_all_attachments`（archive.py:71-82，clear 路径用）一并搬入（去重完整）。新模块只依赖 `core.schema.adapters.attachment_resolver` + `core.storage.attachment_repo`，不反向依赖 core.input → 无循环导入。
- `session_routes.sessions_delete_handler` 在 `session_manager.delete` 前调用 `release_session_attachments(agent, sid)`（与 debug 一致：先释放 refs 再删）。
- debug_routes/archive.py 改为 import 共享 helper（去重）。

**测试**：REST 删会话 → 会话消息引用的附件 ref_count 归零 + 软删/待 GC；debug 删会话回归不变；单条归档删除（archive_delete）回归不变。

### 3.6 SSRF site-local IPv6 绕过（A 簇 minor）

`core/input/ssrf.py:57-79` `_is_blocked_ip` 缺 `addr.is_site_local`（fec0::/10）。`_is_blocked_ip` 返回条件补 `or addr.is_site_local`。测试：`validate_endpoint('http://[fec0::1]/')` → SSRFError；既有私网/回环/CGNAT 回归不变。

### 3.7 认证失败无日志（A 簇 minor）

访问中间件（server.py:108-131）401 分支 + 管理域中间件 401 分支补 `logger.warning`（记 code、路径、不含 token/Authorization 原文）。测试：匿名访问受保护端点 → caplog 出现 AUTH_REQUIRED/AUTH_INVALID，日志不含 token。

### 3.8 GC 全路径日志收敛（A 簇 minor）

`core/storage/attachment_gc.py:130/152/186` 记绝对路径 → 改记 `attachment_id` + kind + 大小（不含本地路径）。测试：caplog 捕获 GC 日志不含 `/home/`、`data/uploads` 等路径片段。

### 3.9 错误信封异常文本泄漏（A 簇 minor）

`core/input/api_routes.py` 多处 `except Exception` 把 `str(e)` 放信封：`api_routes.py:960`（desktop 上传）、`:299/:330`（provider save/delete `ApiFail(error=str(e))`）、`:584` 一带（image 上传）→ **信封只出通用消息**（如「处理失败」），`str(e)` 只进 logger（`logger.warning`）。**完整清单：api_routes.py:299/330/584/960**。测试：模拟 I/O 异常（各点）→ 响应 body 不含文件系统路径/异常原文。

### 3.10 Inner Voice 写应用日志（A 簇 minor）

`core/cognitive/loop.py:949` `logger.info` 记 Inner Voice 正文 → 降为 `logger.debug`（或改记摘要/长度），正文只保留在 conversation.log 审计通道。测试：默认日志级别下 caplog 不含 inner_voice 正文。

### 3.11 Secret Store 并发锁（A 簇 minor，PLAUSIBLE）

`core/storage/secret_store.py:112` set_secret/delete_secret 无锁 read-modify-write → **模块级 `threading.Lock`** 包住 load→改→save（set/delete 为同步 `def`，asyncio.Lock 不可用，改 async 会破坏 custom_providers.py:93/96/130 等 4 处同步调用）。同一事件循环内无 await 不交错，锁防的是 `to_thread`/多线程/多进程路径。测试：并发 set/delete（线程池）不丢更新 + 既有调用方零改动回归。

### 3.12 LLM 初始配置 SSRF 守卫（A 簇 minor）

`core/adapter/llm_adapter.py:54` 初始配置路径（config.llm.model=custom/x）裸 create 不校验 base_url → 复用 `core.input.ssrf.validate_endpoint`（仅 custom 且 base_url 非空时）。测试：初始 custom 配内网 base_url → 初始化失败/SSRFError；内置 provider 不受影响。

### 3.13 GC unlink 失败处理（B 簇 minor）

`core/storage/attachment_gc.py` 规则1 在文件 unlink 抛 OSError 时仍删记录 → 制造磁盘孤儿 → **unlink 抛 OSError 才保留记录重试；文件缺失（path is None / not is_file()）或 unlink 成功 → 删记录**（文件本就不在时删记录是正确清理，不误伤）。测试：mock unlink 抛 OSError → 记录保留 + 日志；文件不存在 → 记录删除（回归不变）。

### 3.14 add_ref/remove_ref 原子化（B 簇 minor）

`core/storage/attachment_repo.py:193-202/224-227` add_ref/remove_ref 读改写（get → $set 全量 refs）→ 改原子更新，**保持幂等与软删语义**：
- **add_ref**：条件 update `{"_id": id, "refs": {"$ne": message_id}}` + `{"$addToSet": {"refs": message_id}, "$inc": {"ref_count": 1}}`——重复 add（发送重试）不双计，维持 `ref_count == len(refs)`。
- **remove_ref**：`{"$pull": {"refs": message_id}, "$inc": {"ref_count": -1}}` 一次 update；随后条件 update `{"_id": id, "ref_count": 0, "status": "active"}` → `{"$set": {"status": "deleted", "deleted_at": now}}`（归零软删，承接现有逻辑）。
- 两个方法各一次/两次 update 内完成，删除读改写竞态。

**测试**：并发 add_ref/remove_ref 后 `ref_count == len(refs)` 不变量成立；**重复 add_ref 两次 → ref_count 仍为 1**（幂等）；remove 归零 → 软删标记。

### 3.15 上传 kind 字段无 size cap（B 簇 minor）

`core/input/api_routes.py:639` `await field.read()`（multipart kind 字段）无上限 → 限 `await field.read(64)`，超长 400/413。测试：kind 字段塞长串 → 拒绝。

### 3.16 多文件上传孤儿清理（B 簇 minor）

`core/input/api_routes.py:670` 逐个写盘+建记录，后续文件校验失败 → 前面文件残留 → **先全部校验（魔数+尺寸+kind）再统一写盘**；中途写失败 → 清理已写文件 + 已建记录。测试：第 2 个文件非法 → 第 1 个文件不残留（目录为空 + repo 无记录）。

### 3.17 探活 request_id 副作用（审计 §6 安全类 minor）

`core/input/server.py:184` `/health` 与 `/ready` 探活负载调用 `agent.next_request_id()`，突变全局请求计数器并覆写 request_id ContextVar——探活探针不应有状态副作用。**改法**：health/ready handler 不再调用 `next_request_id()`（探活返回的 requestId 用独立 `req_<uuid>` 或不带，与计数器解耦）。测试：连续多次 /health → `next_request_id` 计数值不变（可注入计数 mock 断言）。

## 4. 错误码与契约影响

- 无新错误码（复用既有：AUTH_REQUIRED/AUTH_INVALID/INVALID_SIGNATURE/SIGNATURE_EXPIRED/CONTRACT_VALIDATION/STORAGE_UNAVAILABLE 等）。
- 无 wire 契约变更（tool.status 路由行为收紧为服务端侧，契约模型不变；搜索/上传/GC 均服务端内部行为）。
- `frontend_contract` 不动 → `contract.generated.ts` 无漂移。

## 5. 测试要求

- 每项修复 TDD（marker=service 为主，GC/repo 纯函数可 unit）。
- 既有相关测试回归：test_auth.py、test_attachment_download.py、test_upload.py、test_attachment_repo.py、test_attachment_gc.py、test_stickers.py、test_server.py、test_search（session_routes 搜索相关）。
- 全量 `make test-normal` 必须全绿。

## 6. 已知取舍（记录在案）

- **D1 fallback 语义**（见 §2 记录）：单 token 远程部署下管理域用产品 token 可访问——Demo 语义，双 token 严格分离不受影响。
- **D2 残留「抢跑投递目标」向量**：伪造 tool.status 已闭环（每 v2 连接独立身份 + target_connection 校验）；但「冒充 electron 抢先建连接、成为 tool.request 投递目标」的向量（audit bridge.py:255 发现链的 (a) 部分）未在本批次闭环——`X-Client-Platform` 头本身无认证，投递目标选择靠握手顺序。残留移交 8c/后续（如需一次性配对令牌）。**D2 无 None 兼容放行**：生产路径一律传非 None 连接身份，None 仅限单测兼容断言。
- **D4 category=link 特判**：以「TextPart 内容含 URL」近似「链接资源」——无 link Part 类型（审查已确认链接实际在 TextPart 内），符合 docs/14「链接」分类语义。
- **D3 校验序调整**：签名模式先验签后查库，比原序多一次 HMAC 计算成本（无 DB 查询）——换取存在性不泄露，可接受。
- **find_context 残余 bug 移交 8c**（REFUTED #3 的残余）：`before=-5` → `to_list(length=-5)` 未捕获 500（session_routes.py:75/553-556），本批次不修，8c 杂项接。
