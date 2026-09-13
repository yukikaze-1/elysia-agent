# Electron 后端安全部署指南

**文档版本：** v0.9（批次 1 产出 v0.1，BE-010；批次 4 补 Desktop 工具桥安全语义 §3.2 + 附件鉴权缺口；批次 5 补 §3.3 模型 Profile 密钥与 SSRF + 已知缺口更新；批次 6 补 §3.4 语音临时音频不落盘 + 已知缺口更新；批次 7 补 §1.2 附件免 Bearer 豁免 + §3.5 附件下载/签名安全 + 已知缺口更新；批次 8a 补 §1.1 管理面认证 fallback + §1.4 启动守卫双 token 检查 + §3.5 下载校验序前置；2026-08-10 接入复审同步会话与 Desktop 精确投递现状；2026-08-11 加固批次同步 §1.4 启动守卫 fail-closed + 默认 host 127.0.0.1 + §3.7 探针分层；2026-09-11 补 §1.2 角色/身份路径枚举 + 前缀放行口径 + §3.5 匿名静态挂载口径（/static/、/live2d/）+ §3.8 角色/身份新增安全面（Stage A–H））
**关联：** `docs/archive/electron-backend-baseline.md`、`docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch1-auth-version-error.md`、`docs/superpowers/specs/archive/2026-08/2026-08-05-electron-backend-batch4-desktop-tools.md`、`docs/superpowers/specs/archive/2026-08/2026-08-05-electron-backend-batch5-model-profile-keys-voices.md`、`docs/superpowers/specs/archive/2026-08/2026-08-06-electron-backend-batch7-media-and-observability.md`、`docs/superpowers/specs/archive/2026-08/2026-08-06-electron-backend-batch8a-security-attachment-search.md`

本文件说明 Elysia Agent 后端的**远程安全部署**方式：产品认证、TLS 终止、防护范围与已知缺口。远程部署必须遵循本指南；本地开发（回环 + 无 token）维持匿名。

---

## 1. 认证模型

### 1.1 两个权限域分离

| Token | 配置 | 保护范围 | 权限域 |
|---|---|---|---|
| `AGENT_ACCESS_TOKEN` | `ServerConfig.access_token`（env） | `/ws`、业务 `/api/*`、`/uploads/*` | **产品用户** |
| `SERVER_AUTH_TOKEN` | `ServerConfig.auth_token`（env） | `/shutdown` `/restart` `/tools` `/tools/reload` **`/debug/api/*`** | **管理/运维** |

两者独立配置、权限域分离——产品 token 不能访问管理端点，管理 token 不能用于产品 API。`AGENT_ACCESS_TOKEN` 标 `secret=True`（不落盘 YAML）。

**管理面认证（CR-03，2026-09-12，`9b41beb8` 起 fail-closed）**：`_check_auth` 闭包捕获 `auth_token` + `access_token`——`SERVER_AUTH_TOKEN` 已配 → 只认 `Bearer <auth_token>`；**仅配 `AGENT_ACCESS_TOKEN` → 管理端点一律 401（不再回退产品 token）**；两者都空 → 匿名本地开发（非回环绑定由 `assert_secure_bind` 在启动期 fail-closed 拦截；`ALLOW_INSECURE_REMOTE=1` 为显式豁免）。双 token 下产品 token 不能访问管理端点、管理 token 不能用于产品 API（不变）。

> ⚠️ **本段已于 2026-09-12 修正。** 此前语义为「仅配 `AGENT_ACCESS_TOKEN` → 管理端点**回退用 access_token 校验**」（批次 8a `2c5a293` 引入，自称「Demo 单 token 语义」）。该语义**不是修复，而是把一个授权漏洞换了个形状**：从「无需任何凭据」变成「任何持产品 token 的客户端即可」——而产品 token 是分发给前端（浏览器 / Electron）的，授权域仍然塌缩（评测报告 CR-03）。历史审计曾把 `2c5a293` 标注为「✅ 已修复」（`docs/archive/electron-backend-audit-batches0-7.md:52`），属**误判**；详见评测报告 §0.4。
>
> **影响（BREAKING）**：只配 `AGENT_ACCESS_TOKEN` 的部署，管理面现在不可用，需补配 `SERVER_AUTH_TOKEN`。注意 debug 面板前端**当前无法携带该 token**（`frontend/src/` 全目录 `Authorization` 零命中；且任务流用 `EventSource`，浏览器协议上无法设置请求头）——即评测报告 **S2-F3，尚未修**。在此之前该变更的实际影响接近零：面板 HTML 是构建产物，未构建前端时 `/debug` 本就 404。

### 1.2 认证语义

- `access_token` 为空 → **全匿名**（本地开发，v1 前端兼容）；
- 非空 → 以下全部要求 `Authorization: Bearer <access_token>`：
  - `/ws` 握手（拒绝升级返回 401，非 101）；
  - 全部业务 `/api/*`（`/api/chat` `/api/messages` `/api/providers` `/api/images` **`/api/tools` `/api/tools/calls/{callId}`**（批次 4，BE-042）**`/api/uploads` `/api/stickers` `/api/attachments/{id}/sign`**（批次 7，BE-070/071）**`/api/me` `/api/characters*` `/api/character-instances*`**（Stage A–H 角色/身份，`core/input/character_routes.py:526-539`））；
  - `/uploads/*` GET 资源读取（**含 `/uploads/desktop/*`**，批次 4 附件落盘目录）；
  - **受保护判定按前缀而非白名单**：中间件放行条件是 `path.startswith("/api/") or path.startswith("/uploads") or path == "/ws"`（`core/input/middleware.py:33`）——**任何** `/api/*` 路径（含上列未逐一列举的 `/api/sessions*` `/api/search/*` `/api/generations/*` 及上述角色/身份路径）都默认受保护。上列清单仅为示例，**不是豁免白名单**——勿据其反推「未列出即免 Bearer」。
- **唯一免 Bearer 的业务路径**：`/api/attachments/{attachment_id}`（**非 /sign**）——附件**签名下载**。短时 HMAC 签名（`?sig=&exp=`）即授权，属**短期下载授权、非账号凭据**（签名 URL 24h 过期、仅能下载该附件，见 §3.5）；`/api/attachments/{attachment_id}/sign`（签发端点）**保持 Bearer**。
- 未带凭据 → 401 `AUTH_REQUIRED`；凭据错误 → 401 `AUTH_INVALID`；均返回统一错误信封。**批次 8c（F2）**：admin/debug 端点（/shutdown、/restart、/tools、/tools/reload、/debug/api/*）认证失败与业务错误同样走统一信封（`error` = message 兼容别名），不再裸 `{"error":...}`。

**v1 网页聊天（2026-08-11）**：默认停用（`serve_chat=False`，env `SERVE_CHAT=1` 恢复），`/` 404。

**Debug 仪表盘的实际可达性（2026-09-12 核实）**：`/debug` 的 **HTML 壳**由 `_register_static_routes` 以「`static/debug.html` 存在」为条件注册，而该文件是 **Vite 构建产物**（`.gitignore:10-12`）——**未构建前端时该路由根本不注册，`/debug` 恒 404**。而 `/debug/api/*` 是**无条件注册**的（`register_debug_routes` 无守卫）——即 **UI 可缺、API 恒在**。这个不对称本身就是一处待收口项。

同一核实修正了两处「运行时可见的虚假陈述」（2026-09-12 已修）：

- **启动横幅曾无条件写「/debug 仪表盘可用」**，而该路由是条件注册的——于是它在页面恰好 404 的时候宣称页面可用。现改为读**路由表实际注册结果**（`AgentServer._debug_banner_line` + `_route_registered`），未构建时改印「未构建（缺 static/debug.html）—— cd frontend && npm run build」，不再给出打不开的地址。
- **`WorkflowDebugAPI` 的 500ms 轮询曾由 on_startup 无条件拉起**，无人订阅也空转（遍历全部 task × node_states + 抽 graph edges + 整表 diff）。现为**懒启动**：首个 SSE 订阅者才拉起，最后一个离开即停止。安全含义：这个常驻循环此前是「无鉴权面之外的持续资源消耗」，现在只在有人真的订阅读取时才存在。


- `/static/` 静态挂载（`server.py:119-120`）与 `/debug` 壳**均无鉴权**，构建产物存在时匿名可加载（**注意：文档 §1.1 表格曾写 `SERVER_AUTH_TOKEN` 护 `/debug/*`，实际只护 `/debug/api/*`**）；
- `/debug/api/*` 全部 25 条路由经 `_check_auth`（中间件判据 `path.startswith("/debug/api")`，`debug_routes/__init__.py:33`）；
- **前端凭据（S2-F3，2026-09-12 已修）**：此前 debug 前端**不携带任何凭据**（`Authorization` 零命中）→ 一旦配置 `SERVER_AUTH_TOKEN`，整个数据面 401、仪表盘退化成空壳。现在：
  - 所有请求经 `frontend/src/debug/auth.ts::debugFetch` 补 `Authorization: Bearer <token>`；
  - token 由页面弹出的**凭据闸门**输入，存 `sessionStorage`（关标签页即失效），**绝不进 URL、绝不落日志**——遵守 §1.3；
  - 闸门**只在后端返回 401 时出现**，未配 `SERVER_AUTH_TOKEN` 的回环开发零打扰；
  - 任务流的 `EventSource` 已换成 `frontend/src/debug/sse.ts` 的 `fetch` + `ReadableStream`（`EventSource` 协议上加不了请求头，这正是原先需要"另择载体"的原因——现无需 query token / 短签 / cookie）；
  - 两道规则由 `make check-debug-auth`（`scripts/check_debug_auth.py`）静态守卫：debug 前端内除 `auth.ts` 外**不得出现裸 `fetch(`**，全目录**不得出现 `EventSource`**。漏改任一处调用即红。
  - **不改变 CR-03 语义**：产品 token（`AGENT_ACCESS_TOKEN`）仍换不到管理面——实测带产品 token 请求 `/debug/api` 仍是 401 `AUTH_INVALID`。


### 1.3 凭据载体

- 凭据放 `Authorization` Header（REST）或 WS 握手 Header（`Authorization: Bearer`）——**绝不放 URL 查询参数**；
- Electron Main 持有凭据，Renderer 不接触明文 token；
- 日志、错误体、诊断包**不记录 token**（`secret=True` + 日志敏感扫描测试守护）。

### 1.4 启动守卫（fail-closed）

服务绑定**非回环地址**（非 `127.0.0.1/localhost/::1`）且未配置 `AGENT_ACCESS_TOKEN` 时，启动被 **fail-closed 拒绝**（`assert_secure_bind` 抛 `RuntimeError`，D6a）。默认 `server.host=127.0.0.1`（回环）——本地开发无需 token 即匿名放行；**远程绑定必须显式配置**（改 host + 配 token，或显式 `ALLOW_INSECURE_REMOTE=1` 开发开关）。

fail-closed 放行条件（任一满足即放行）：
- host 为回环地址（`127.0.0.1` / `localhost` / `::1`）；
- 已配置 `AGENT_ACCESS_TOKEN`（业务 `/ws`、`/api/*`、`/uploads/*` 的唯一放行凭据；**仅配 `SERVER_AUTH_TOKEN` 不构成放行**——管理 token 不保护业务端点，review 复核确认）；
- 显式设 `ALLOW_INSECURE_REMOTE=1`（仅本地开发开关，日志/health 明确 insecure）。

**批次 8a（`2c5a293`）**：放行后的安全提示由 `warn_if_unsecured` 输出——非回环绑定下仅配置一个 token（`AGENT_ACCESS_TOKEN` / `SERVER_AUTH_TOKEN` 缺一）→ 输出 **WARNING 级告警**，建议补齐另一个 token（否则管理端点回退用 access_token 校验或保持匿名）；两者都空 → ERROR 级告警（正常路径已被 fail-closed 前置拦截，除非显式 insecure opt-in）。

### 1.5 单用户语义（V1，BE-STAGEH-06）

**`AGENT_ACCESS_TOKEN` 是「获准的产品客户端」凭据，不是用户账号**——它不映射稳定 user principal：

- request 上没有 user 身份；`UserIdentity.userId` 固定 `usr_local_default`；
- character create/clone/list 等路径直接使用该常量，部分 get/update/archive 按 characterId 读取、无 current-owner 参数；
- 因此**再配置一个有效 token 不会得到第二个用户**：用第二 token 注入 `ownerUserId=usr_other` 声称完成「403/404 owner 隔离」验收是假绿。

V1 保持单用户产品范围：「无权访问其他 owner 数据」记为 **N/A — 单用户**；保留未知 character/instance、archived 角色与安全 404/409 的既有真实联调结果；repository 中 `owner_user_id` 字段与查询能力**保留**（未来多用户隔离预留，见架构稿 §13.3）。多用户是独立架构工作（principal 映射、request context 传播、全资源 owner 校验、防枚举、双 principal 集成测试、token 轮换/审计），不属 Stage H 范围。

**身份读取 fail-closed（P0-3）**：单用户身份同样**不做本地伪造**——装配 `UserIdentityRepository` 后 Mongo 不可用时 `UserIdentityStore.get()` 恒为 `None`（`is_available=false`），`/api/me` 读写 503、preview/会话创建 503、generation 以 `STORAGE_UNAVAILABLE` 失败，`/ready` 的 `chatReady` 为 false；Mongo 恢复由健康循环的 `identity` 探针触发重载，无需重启服务。`usr_local_default` 只是固定 userId 常量（scope/owner 字段用），不是可用的身份档案。

## 2. TLS / HTTPS-WSS 部署

后端**不内建 TLS**——生产由反向代理终止 TLS。推荐部署形态：

```
Electron Main ──(wss/https)──► 反向代理（TLS 终止 + 转发 Authorization）──► 后端 :8765
```

### 2.1 反向代理要求

- **TLS 终止**：Nginx/Caddy/Traefik 终止 TLS，转发到后端 `http://127.0.0.1:8765`（后端只在回环/内网监听）；
- **转发凭据**：代理把客户端 `Authorization` Header 原样转发给后端（认证决策在后端，不信任代理）；
- **跨 Origin 重定向不得转发凭据**：代理对重定向（3xx）剥离 `Authorization`，防凭据泄漏到其他 Origin；
- **WS 升级**：代理需支持 WebSocket Upgrade（`proxy_set_header Upgrade/Connection`）；`/ws` 路径同样要求客户端带 token；
- **示例（Nginx）**：

```nginx
server {
    listen 443 ssl;
    server_name agent.example.com;
    ssl_certificate     /etc/letsencrypt/live/agent.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agent.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 转发客户端 Authorization（认证在后端）
        proxy_set_header Authorization $http_authorization;
        # WebSocket 升级
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_http_version 1.1;
        # 跨域重定向剥离凭据（浏览器自动处理：Authorization 不跨 Origin 发送）
    }
}
```

### 2.2 后端监听建议

- 远程部署时后端 host 建议绑定 `127.0.0.1`（仅代理可达）或内网地址 + `AGENT_ACCESS_TOKEN`；
- 若绑定 `0.0.0.0` 且无 token，启动会被 **fail-closed 拒绝**（见 §1.4）——远程部署必须配置 token。

### 2.3 可重复测试

- `core/input/tests/test_auth.py`：有 token 环境匿名 `/api/*` `/ws` `/uploads/*` 被拒、有效身份可用（即远程部署安全姿势）；
- `core/input/tests/test_limits.py`：请求体超限、并发超限拒绝；
- 反向代理的 TLS 终止本身由代理层保证（后端无 TLS 职责），本仓库测试覆盖后端认证决策——部署后建议用 `curl -k https://…/health` 与 `wss` 连接各做一次冒烟。

## 3. 限制保护

- `max_request_bytes`（默认 64MB，批次 7 由 8MB 提高）：业务 JSON body 上限，超限 413 `REQUEST_TOO_LARGE`；上传端点经 handler 逐文件自限（10/50/20MB）+ 聚合 64MB 双重约束（批次 7 §13.1）；
- `max_ws_connections`（默认 64）：并发 WS 上限，超限 503 `CONNECTION_LIMITED`；
- 图片上传大小/类型沿用 `image_max_mb` / `image_allowed_types`（415 `INVALID_REQUEST` 映射）——含 `POST /api/uploads/desktop/{callId}`（批次 4 桌面附件，图片走同约束 + `application/octet-stream` 等通用文件白名单）。

### 3.1 受保护会话与数据完整性（批次 3 多会话）

- **`default` 会话禁删**：`DELETE /api/sessions/default` → 409 `SESSION_PROTECTED`（`default` 恒存在且受保护，禁删含其 append-only 消息归档与 character_state）；
- **忙时门禁**：生成/TTS 进行中 `activate`（WS/REST）或删除**活动会话** → 409 `AGENT_BUSY`（retryable），防 engine 换绑与异步延续竞态；
- **显式会话存在性**：v2 text、附件、`voice.commit`、realtime voice 对非空 `conversationId` 均在接受工作前验证登记状态；未知、拼错、已删除或处于删除 tombstone 的 ID 直接返回 `SESSION_NOT_FOUND`，不得 lazy-create 幽灵会话。缺省 ID 才解析为当前 active，并在 accepted 时固化归属。

### 3.2 Desktop 工具桥安全语义（批次 4，BE-040~043）

- **客户端标识与精确投递**：`X-Client-Platform: electron` 握手头**仅 v2 解析**（v1 伪冒隔离，负面隔离测试守护）；bridge 把最新握手 `connectionId` 登记为 call 的 `target_connection`，投递时同时匹配 `client_platform=electron` 与该 ID。重连期间新旧连接并存时旧连接不收新请求；精确目标不存在则立即失败为 `DESKTOP_UNAVAILABLE`，不广播、不静默丢帧。
- **附件持有者校验**：`POST /api/uploads/desktop/{callId}` 三重校验（① callId 存在否则 404 `CALL_NOT_FOUND`；② 上传连接 `X-Client-Connection` = 握手 `connectionId` = `target_connection` 否则 403 `INVALID_REQUEST`；③ call 处于可收结果状态，终态拒绝 403 防伪附件）——校验失败不落盘。
- **参数篡改拒绝（BE-043）**：`tool.status` 回传 `toolName/toolVersion/runtime/paramHash` 与登记不一致 → 立即 mark failed（`CALL_IDEMPOTENCY_CONFLICT`）+ resolve 失败态 + 丢弃（不挂超时）；paramHash = 规范化 arguments SHA-256。
- **断连/重传**：断连 → repo 标 `offline`；未接手（pending）call 立即 resolve UNAVAILABLE（无副作用已发生，早收尾安全）；已接手（accepted/running）call 不自动取消（Electron 可能已副作用），靠重连后重传 result 对账（`offline → 终态` 迁移）。**重连不自动重投执行请求**（防重复副作用）。
- **敏感纪律（D-p）**：`desktop_calls` 集合存完整 arguments（审计/幂等必需）；bridge/ws 层 logger 只记 callId/toolName/status，**不打印 arguments 值**；错误信封 `details` 不放本地路径/参数；附件只传后端托管 URL。
- **信任边界（显式认定）**：`desktop.file.read` 的 `path` 接受任意值（LLM 可凭空捏造路径）——来源追踪与本地路径校验属 **Electron 本地安全职责**（黑名单/沙盒/用户选择记录，docs/15 §三.2），后端只负责结构化传输 + paramHash 绑定，不额外做跨调用 path 白名单。

### 3.3 模型 Profile 密钥与 SSRF（批次 5，BE-050）

- **Secret Store 模型**：用户密钥存 `data/secrets/profiles.json`（目录 0700 + 文件 0600 + `.gitignore` + **不落 MongoDB**，防备份/导出泄漏）；原子写（tmp + `os.replace`）；**只写不可读**——所有响应/日志只出 `credentialConfigured` 布尔，错误信封 `details` 禁密钥。
- **密钥作用域按 runtime**：`backend` 类 Profile 由后端管密钥（Secret Store 直存 或 api_key_env env）；`desktop/local_worker` 类 Profile 仅声明占位、密钥归 Electron 管（后端提交 api_key → 400，防"保存成功"假象）；此类条目的 test/switch 后端不代理（400 `PROFILE_RUNTIME_NOT_BACKEND`）。
- **SSRF 防护（自定义 Endpoint）**：`validate_endpoint` 静态 + DNS 解析校验（scheme 缺省 https；拒绝云元数据/回环/链路本地/私网/保留/多播/CGNAT），作用于**保存、切换、测试三入口** + **`/api/providers/test` 请求体 base_url 无论 provider 一律校验**（堵死客户端任意拨号内网通道）；出站再校验——custom backend 的 HTTP 客户端注入校验传输层（openai `http_client=` / httpx transport），**重定向每跳校验**。
- **Header 允许名单**：自定义 backend 出站仅允许 `Authorization/Content-Type/Accept/User-Agent`；**跨源重定向剥除允许名单外全部 Header（含 Authorization）**——防 `X-API-Key` 等敏感头泄漏到内网重定向目标（docs/14 §10 显式要求）。
- **已知取舍（显式认定）**：出站守卫在连接前校验、随后 httpx 二次解析建连，存在 TOCTOU 窗口（活跃 DNS rebinding 需 IP pinning，批次 5 未承诺，见 §4 已知缺口）。SSRF 错误消息统一「目标地址被拒绝或无法解析」，**不泄露 host/IP**（防内网拓扑枚举）。
- **豁免范围**：内置 provider（含 `local: true` 如 ollama/gpt-sovits）为运维托管配置，不注入守卫；desktop/local 条目 base_url 后端不拨号、跳过 SSRF 且允许为空。

### 3.4 语音临时音频不落盘（批次 6，BE-060~062）

三种语音模式（普通录音 / 实时语音 / 输入法转写）的采集音频均为**内存缓冲**——不落盘、不入消息 content、不入数据库、不进日志：

- **RECORDING**：`VoiceSessionManager` 内部 `list[bytes]` 累积裸 PCM → `stop` 时 `pcm_to_wav()` 仅驻留内存 → STT 转写 → 字幕文本；音频 bytes 随 commit / discard / 断连即弃（`_reset_recording`）。
- **REALTIME / DICTATION**：`AudioBuffer` VAD 分段，段完成 → STT → 仅回字幕/轮次文本；buffer 随会话创建/销毁（`_teardown_session`）。
- **日志不记音频**：logger 只记文本摘要（如 commit 日志记 `pending_text[:30]`）；`message_archive` 只存消息文本——语音消息 = 纯文本消息（批次 7 BE-070 才做音频附件落盘，届时走现有附件上传鉴权路径）。

### 3.5 附件下载与签名安全（批次 7，BE-070/071）

**签名 URL 用 query 参数是刻意的豁免**：`sig`/`exp` 是**短时下载授权**（HMAC-SHA256(`attachment_id:exp`)，24h 过期、只授权该附件），不是账号凭据，故放查询参数走免 Bearer 下载路径（§1.2）；账号凭据（`AGENT_ACCESS_TOKEN`）仍只在 `Authorization` Header，绝不进 URL。

- **fail-closed 决策表**（`ATTACHMENT_SIG_SECRET`，`ServerConfig.attachment_sig_secret`，`secret=True`）：

| AGENT_ACCESS_TOKEN | ATTACHMENT_SIG_SECRET | 下载 / 签发 |
|---|---|---|
| 未配置 | 任意 | **fail-open**：下载跳过签名校验（仅 status=active 校验），本地开发匿名 |
| 已配置 | 未配置 | **fail-closed**：下载/签发一律 403 `ATTACHMENT_SIGNATURE_DISABLED`（拒绝进入校验序；`signedUrl` 上传响应同样省略）——拒绝「匿名下载到受保护附件」的静默降级 |
| 已配置 | 已配置 | 下载走签名校验序（sig 合法 hex → exp 窗口 → HMAC 验签 → 通过后才查记录 active），签发端点走 Bearer |

- **签名下载校验序**：① `sig` 存在且 64 位小写 hex（上界 128 防超长串拖慢 `compare_digest`，DoS 防护）→ 否则 403 `INVALID_SIGNATURE`；② `exp` 整数且在 `[now, now+86400]` → 否则 403 `SIGNATURE_EXPIRED`；③ HMAC 验签（`hmac.compare_digest` 防时序侧信道）→ 不匹配 403 `INVALID_SIGNATURE`；④ **前三步全部通过后才查询记录**——`status=active` 放行，否则 404 `ATTACHMENT_NOT_FOUND`（软删即拒绝，删除语义 = 不可再取；Mongo 离线 → 503 `STORAGE_UNAVAILABLE`）。**校验序前置（批次 8a，`7b0e0ba`）**：repo.get 移到验签之后——无效 sig 恒 403、不再经 404/403 差异枚举附件存在性（修复批次 7 记录在案不修的「下载存在性 oracle」）。**`_SIG_TTL` 恒 86400**（与签发 `_sign_url` 一致），exp 夹紧上限。
- **`/uploads/` 静态挂载收敛**：只公开 legacy `images/` 与 `desktop/` 两个子目录（既有 `/api/images` 与批次 4 桌面截图链路兼容）；**新 kind 单数目录 `data/uploads/{image,video,audio,file}` 不静态暴露**——只能经 `/api/attachments/{id}` 签名下载（新附件不产生永久匿名 URL）。`/api/attachments/` 免 Bearer 豁免在认证中间件中**先于 `/api/` 前缀判断**，且 `path.endswith("/sign")` 排除签发端点。**同层另有两条匿名静态挂载不在 `/uploads/` 收敛口径内**：`/static/`（`core/input/server.py:120`，仅 `serve_static=True` 且产物目录存在时挂载）与 `/live2d/`（`server.py:123`，映射 `data/live2d-assets`，**不受 `serve_static` 门控**——只要目录存在即挂载）——两者均**不在**认证中间件覆盖内（§1.2 中间件只护 `/api/`、`/uploads`、`/ws`），任何匿名请求可直接读取其下文件；故 live2d 模型等资产应视为**公开静态资源**，不得放入密钥/私有素材。
- **签名 URL 不记录 query string**：日志只记 `attachment_id`（不记 `sig`/`exp` 全文）；错误信封 `details` 默认空 dict——附件路径/密钥/正文不入错误体。旧 `/api/images` 链路安全由全局 Bearer 维持（不在签名/GC 保护内，见 §4）。
- **附件持有者语义**：签名 URL 授权对象为「该 attachment_id」而非「上传者」——单用户 Demo 可接受；多用户需按 owner 过滤（非本批次，见 §4）。

### 3.6 公开入口限流与幂等持久化（D6b/D6c，批次 9D，2026-08-11）

- **限流（D6b）**：单进程 token-bucket，覆盖 WS 建连、消息（text/voice）、provider test、upload、search、工具审批。REST 超限 → **429 `RATE_LIMITED`** + `Retry-After: 60`；WS 建连超限 → 升级前 429；WS 消息超限 → `error{RATE_LIMITED, retryable, retryHint}` 连接存活。key **认证主体优先**、匿名回退 IP；**默认不信任 `X-Forwarded-For`**（伪造代理头不改变 key，防限流绕过与高基数内存 DoS）；内存有界（max_keys）+ 旧 key 定期清理。**单进程语义**——多实例部署需自建共享后端，`RATE_LIMITED` 不代表全局配额（产品化门禁登记）。默认配额（`config.yaml` `server.rate_limit_*_per_min`，env `RATE_LIMIT_*_PER_MIN` 覆盖，0 关闭）：ws 120 / messages 120 / provider_test 30 / upload 60 / search 60 / tools 120。
- **幂等持久化（D6c）**：`clientRequestId` claim 持久化到 Mongo（`idempotency` 集合，`(conversation_id, client_request_id)` 唯一 + `expires_at` BSON Date TTL 1h）——ACK 丢失 + 后端重启后重试不重复生成。**产品模式 + Mongo 离线 → fail-closed**（v2 文本/`voice.commit` 幂等 lookup/claim 失败 → `user_message.ack{rejected, STORAGE_UNAVAILABLE, retryable}`，不生成 messageId）；**回环 dev + Mongo 离线 → 内存降级**，`server_info.features.idempotency_durable=false` 暴露非 durable 窗口（Mongo 恢复自动回 durable）。
- **幂等状态机与恢复（加固批次 2026-08-11）**：claim 三态 `CLAIMED/ACCEPTED/REJECTED` + claim 所有权（`ClaimResult.acquired`，并发 loser 恒不进入业务处理）；崩溃/拒绝残留不再被重放成幽灵 accepted（新鲜 CLAIMED→retryable `IN_PROGRESS`、过期+generation 证据→重放 accepted、过期+无证据→接管重执行）；dev 内存权威 overlay + Mongo 写穿 + 恢复 reconcile（进程内 Mongo 任意切换不去重）；idempotency 纳入健康探测/恢复/关闭生命周期。

### 3.7 探针分层（BE-P2-01，2026-08-11 加固批次）

- **`/health`（liveness）**：纯进程存活检查——FSM/uptime/build 运行信息，**零网络探针**（Mongo/Milvus/idempotency 一律不 `wait_for`），**恒 200**，任何依赖挂起都在小固定预算内返回；不携带依赖状态。
- **`/ready`（readiness）**：HTTP 门禁 = **核心 chat**：
  - `chatReady`（=HTTP 门禁）= mongo 三 repo（session+message+attachment）**并行真实 ping** 且 **用户身份权威存储可达**（`dependencies.identity`，P0-3：身份不可用时不得用占位身份继续生成/建会话）且 **lifecycle 为 RUNNING**（`dependencies.lifecycle`，CR-08：`Agent.stop()` 首行即置 STOPPED，停机窗口必须立刻摘除，否则负载均衡会把流量导进正在丢消息的实例）且（非 product 模式 或 idempotency 真实 ping）；
  - **product 模式 idempotency 门禁含补偿状态（BE-P1-07，2026-08-12）**：`idempotency_ok = ping AND not needs_recovery`——降级期内存幂等记录未 reconcile（`_needs_recovery=True`）时 chatReady 保持 false（用 needs_recovery 而非 durable，避免零降级瞬时窗口误伤）；
  - **Milvus 不门禁 HTTP**（文本可降级使用）——经 `fullReady`（=`chatReady` AND milvus）可观测；
  - **product 模式（非回环绑定）idempotency 失效或未 reconcile → `/ready` 503**（v2 send 不可用）；dev 模式（回环）允许 idempotency 降级不门禁；
  - payload：`status`（全依赖 ok→"ok" 否则 "degraded"，可观测非门禁）、`chatReady`、`fullReady`、`dependencies`（mongo/milvus/idempotency/identity/lifecycle 明细）。
- **状态源分歧可观测（CR-08）**：`/health` 与 `/ready` 的 payload 同时报 `fsm`（FSM 投影，决定 `status.is_accepting_input`）与 `run_state`（`Agent._state`，决定事件消费者存活）。停机窗口内二者**本就不同步**（fsm=idle、run_state=STOPPING）——两个都报，排障时无需猜测谁在说谎。
- **停机窗口的拒收语义（CR-08）**：`_state != RUNNING` 时 `process_message` 直接返回 False，且该判定发生在 v2 `reserve_work` **之前**（不留无人释放的登记）。调用方据此区分成因：WS 回 `AGENT_BUSY`（"正在关停"）、REST `/api/chat` 回 **503**；队列满仍是 `QUEUE_FULL` / 429。两者此前共用 `QUEUE_FULL`+"队列已满"，是把排障方向引到容量问题上的观测面说谎。
- **语义**：`/ready=200` ⇒ v2 send 可用；`/ready=503` ⇒ 读 payload 的 `chatReady`/`fullReady`/`dependencies` 判断文本可用/不可用原因。Electron 探活口径：`/health` 只看进程存活，`/ready` 才是文本可用门禁。

### 3.8 角色/身份新增安全面（Stage A–H，2026-09-11 补）

多角色（Stage A–H）在既有认证/SSRF/静态挂载之外引入以下安全面变化：

- **展示资源引用边界（防任意 URL 注入）**：角色展示引用经契约层校验——`CharacterPresentation.avatarAssetId` / `live2dModelId` 走 `_registry_ref`（`frontend_contract/rest.py:690-699`），拒绝 URL scheme（`scheme:` / `//`）、绝对路径 / 反斜杠 / Windows 盘符（`/` `\` `C:\`）、`..` 父目录穿越，且限 200 字符、无控制字符，只收**短不透明托管 ID**；`themeColor` 走 `_theme_color` 白名单（`rest.py:702-716`），仅接受 `#rgb/#rrggbb(aa)`、`rgb()/rgba()/hsl()/hsla()` 数字式与 `_SAFE_CSS_NAMES` 具名色，显式拒绝 `inherit/currentcolor/initial/unset/revert` 等可继承关键字。作用于 `CharacterPresentation`（`rest.py:770-779`）与 provider/voice 引用同名校验（`rest.py:836`）。这是**阻止客户端经展示字段注入任意 URL/路径**的边界。
- **角色预览出站入口（补 §3.3 口径）**：`POST /api/characters/preview`（`core/input/character_routes.py:532` → `core/character/service.py:814`）触发**单次出站 LLM 推理**（`preview_port.generate`，装配为 `PromptCharacterPreviewAdapter(agent._llm)`，`core/agent/agent.py:261`）——§3.3 SSRF 段列举的出站入口（保存/切换/测试/`/api/providers/test`）之外新增的一处出站面。**与 SSRF 的区别**：预览走**后端运行时 LLM 配置**而非客户端提交的 endpoint，无「客户端任意拨号内网」面，故不纳入 `validate_endpoint` 约束；仍属**出站流量面**须登记。预览**零副作用**（不建 session/instance、不写 archive、不读会话/LTM、不推进 revision/stateVersion、用量不入任何会话统计，`service.py:817-821`）；**权威身份不可用时 fail-closed**（503 `CHARACTER_STORAGE_UNAVAILABLE`，`service.py:829-835`）。
- **角色/身份路径错误码**：角色与身份路径新增 13 个稳定错误码，全部经统一错误信封（§1.2）——`CHARACTER_ARCHIVED` / `CHARACTER_BUSY` / `CHARACTER_IMMUTABLE` / `CHARACTER_INSTANCE_NOT_FOUND` / `CHARACTER_NOT_FOUND` / `CHARACTER_PREVIEW_FAILED` / `CHARACTER_REVISION_CONFLICT` / `CHARACTER_STATE_CONFLICT` / `CHARACTER_STORAGE_UNAVAILABLE` / `CHARACTER_TOO_LARGE` / `CHARACTER_VALIDATION` + `USER_PROFILE_NOT_FOUND` / `USER_PROFILE_REVISION_CONFLICT`（`core/input/character_routes.py`）。错误体 `details` 沿用空 dict 纪律（§5），不泄漏内部路径/密钥；完整码表以协议文档为准，此处仅登记其存在。
- **建连角色状态快照（新增数据外发面）**：v2 建连握手即向客户端推送角色状态快照——`agent.character_state_stream` **恒 true**（`core/agent/agent.py:1201-1207`），`_emit_state_snapshot` 在 `server_info` 之后**强制发射、不看 `last`**（`core/input/ws_handler.py:261-263`；重连/`activate_session` 切会话路径同发，`ws_handler.py:1116-1118`）。即**任何已认证 v2 连接**在握手（或切会话）后立即收到该会话绑定角色的运行时状态载荷——属新增外发面；载荷为角色运行时状态，非密钥/凭据。

## 4. 已知缺口（后置批次）

| 缺口 | 后置 |
|---|---|
| ~~`/uploads/*` 匿名 GET 已拒（有 token），但短期签名 URL / 鉴权下载未实现~~ **已闭合（批次 7）**：新附件走 `/api/attachments/{id}` 签名下载（HMAC 24h 短签，免 Bearer）+ `/sign` 签发端点（Bearer）；`/uploads/` 静态挂载收敛为仅 legacy images/desktop 子目录。**遗留**：legacy `/uploads/images/*`、`/uploads/desktop/*` 仍只由产品认证保护（无 per-call/per-owner 下载鉴权，callId/文件名即 URL 路径）——旧链路与批次 4 桌面截图兼容，未迁移到签名体系，批次 7 记录在案（见 §3.5） | 已闭合；遗留迁移随前端切换驱动 |
| 附件**多用户持有者过滤**：签名 URL 授权对象为 attachment_id 而非上传者，单用户 Demo 语义 | 批次 7+（多用户时按 owner 过滤） |
| ~~速率限制实现（`RATE_LIMITED` 仅入目录）~~ **已闭合（D6b，2026-08-11）**：单进程 token-bucket 落地（REST 429 + WS RATE_LIMITED + retryHint），见 §3.6。**遗留**：多实例共享后端未实现（单进程语义文档化） | 多实例部署时自建共享后端 |
| **Secret Store 已落地（批次 5）**：`data/secrets/` 明文 0600 + gitignore + 不落 MongoDB（防备份泄漏）；密钥**只写不可读**，只回 `credentialConfigured`。遗留：明文待批次 7+ 评估加密 | 批次 7+（加密评估） |
| DNS rebinding 完全闭合（IP pinning）——批次 5 出站守卫在连接前校验 + httpx 二次解析建连存在 TOCTOU 窗口（见 §3.3），未承诺防活跃 rebinding | 批次 7+（如需） |

## 5. 敏感信息纪律

- token/API key 一律环境变量（`AGENT_ACCESS_TOKEN`/`SERVER_AUTH_TOKEN`/`*_API_KEY`），不落 YAML/git；
- **用户提交的模型密钥**（批次 5）只进 Secret Store（`data/secrets/` 0600 + gitignore + 不落 MongoDB），响应/日志只出 `credentialConfigured` 布尔，**永不读回**；
- 错误信封 `details` 默认空 dict——禁止放入密钥、聊天正文、Inner Voice、未脱敏路径；SSRF 拒绝消息不带 host/IP（防内网拓扑枚举）；
- **附件日志**（批次 7）：签名 URL 不记录 query string，下载/签发/GC 日志只记 `attachment_id`（att_ 前缀，不可反查内容）；`ATTACHMENT_SIG_SECRET` 为 `secret=True` 不落 YAML/git、不进日志与错误体；
- 日志使用关联 `requestId`，不记录正文与凭据。
