# Electron 后端改造 — 批次 1：认证、版本协商、统一错误（Spec）

**Spec 版本：** v0.2（修订：吸收批次 1 spec review 全部发现）
**文档日期：** 2026-08-04
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-010/BE-020/BE-080）与 `docs/15-...`（批次 1）
**前置：** 批次 0 基线文档 `docs/electron-backend-baseline.md`

---

## 1. 目的

为远程部署建立**认证 + 版本协商 + 统一错误**三层基线。匿名访问被拒绝、有效身份可完整聊天、客户端能判断兼容性、错误可结构化。

**兼容硬约束**：本地开发（未配置 access_token）行为与现状完全一致；v1 前端在**未配置 token 的环境**继续匿名可用，且**v1 REST 错误体形状不变**（信封只新增、不动 `ApiFail`/`ImageUploadError` 等现有 v1 契约模型）。

## 2. 已定决策

| 决策 | 结论 |
|---|---|
| D2 | 新增产品 access token（`AGENT_ACCESS_TOKEN`），与 `SERVER_AUTH_TOKEN` 权限域分离 |
| D3 | 版本协商走 WS 握手 HTTP 头：`Authorization: Bearer` + `X-Protocol-Version` |
| D4 | v2 契约用新 type 字符串 / 新模型，**不修改现有 v1 契约模型** |
| D7 | 统一错误信封 + 初始错误码目录 |
| D8 | 请求大小 / 并发 / 认证失败保护配置化；**速率限制延后**（`RATE_LIMITED` 仅入目录，实际限流器批次 2+，避免批次 1 范围膨胀） |

## 3. 设计

### 3.1 认证（BE-010）

**配置：** `ServerConfig` 新增：

```python
access_token: str = ConfigField(default="", env="AGENT_ACCESS_TOKEN", secret=True)  # secret 防写入 YAML/git
max_ws_connections: int = ConfigField(default=64)
max_request_bytes: int = ConfigField(default=8 * 1024 * 1024)  # 业务 JSON body 上限
```

**`access_token` 传递（关键，防测试翻红）**：作为 `create_app(agent, ..., auth_token=..., access_token=...)` 的**显式参数、默认 `""`**；仅在 `run_server` 从 `config.server.access_token` 读取后传入。**不得**在 `create_app` 内部从 `agent.config.server.access_token` 读取——否则现有测试的 MagicMock agent 会触发 401 全量翻红（review 证据：`test_server.py` 大量 `_make_app()` 用 MagicMock）。

**强制策略：** `access_token` 为空 → 全匿名（本地开发，v1 兼容）；非空 → 以下全部要求 `Authorization: Bearer <access_token>`：

- `/ws` 握手（`ws.prepare()` 之前读 `request.headers`，不调用 prepare 直接返回 401 拒绝升级——aiohttp 已确认可行）；
- 全部业务 `/api/*`（含 `/api/chat` `/api/messages` `/api/providers` `/api/images`）；
- `/uploads/*` GET 资源读取（经 app 级中间件，对 static route 同样生效）。

**已接受权衡**：配置 access_token 的环境，v1 Vue 前端 `<img src="/uploads/...">` 与匿名 WS 不可用——预期：远程部署由 Electron（v2）使用；本地不配 token 一切如旧。`/uploads/*` 短期签名 URL 属批次 7（BE-070/071）。

**启动守卫：** `run_server` 每轮重启在读取 host 后检查：绑定非回环地址（host 非 `127.0.0.1/localhost/::1`）且 `access_token` 为空 → 输出**显著阻塞告警**（ERROR 级、多行、含设置 `AGENT_ACCESS_TOKEN` 指引）。**不拒绝启动**（config 默认 host=0.0.0.0，拒绝会破坏本地开发；"显著阻塞告警"满足 PRD"拒绝启动或输出显著阻塞告警"的"或"分支）。本地开发若要消除噪音，将 config host 改为 `127.0.0.1`。

**认证失败处理：** 401 + 统一错误信封（`AUTH_REQUIRED` / `AUTH_INVALID`）；不做指数退避（Demo 单用户 YAGNI）；认证失败记录日志。token 永不出现在日志/URL/错误体。

### 3.2 版本协商（BE-020）

**握手：** Electron Main 连 `/ws` 时带 `Authorization: Bearer <access_token>` + `X-Protocol-Version: 2`。

**路由：**
- 未带 `X-Protocol-Version` 或值为 1 → v1 路径（现状，零改动）；
- 值为 2 → v2 路径（批次 1 只做 `server_info` + 认证；批次 2 做 v2 聊天事件）。

**`server_info` 帧：** 新增契约模型 `ServerInfoMessage`（`type="server_info"`），仅对 v2 客户端发送，**不动现有 `CapabilitiesMessage`**；v2 客户端**用 `server_info` 取代 `capabilities`**（`services` 内嵌同形状，避免重复帧）：

```json
{
  "type": "server_info",
  "protocol_version": "2.0",
  "server_version": "0.1.0",
  "features": {
    "structured_generation": false,
    "multi_conversation": false,
    "desktop_runtime": false,
    "dictation": false
  },
  "services": { "llm": {...}, "stt": {...}, "tts": {...} }
}
```

- `services` 复用现有 per-service 形状（available/enabled/model/default），Electron 设置面板与 v1 共用渲染逻辑；
- `features` **必须如实反映现状**：批次 1 全部为 `false`（`structured_generation` 批次 2 后置 true，多会话/Desktop/dictation 分别批次 3/4/6 后置）。**严禁超前声明**。

**`server_version`：** 新增 `core/version.py`（`__version__ = "0.1.0"`），单一事实来源。

### 3.3 统一错误（BE-080）

**HTTP 错误信封**（新增错误类，**不改造现有 v1 错误响应体**）：

```json
{
  "code": "AUTH_REQUIRED",
  "message": "需要认证",
  "retryable": false,
  "requestId": "req_uuid...",
  "error": "需要认证",
  "details": {}
}
```

- **`error` 字段 = `message` 的兼容别名（关键）**：v1 前端 `useImageUpload.ts` 读 `err.error`，信封保留 `error` 键使现有错误消费逻辑不破坏；
- 现有 `ApiFail`/`ImageUploadError`/`MessagesResponse`（`frontend_contract/rest.py`）**不动**——它们是 v1 契约模型；信封是独立新模型，用于批次 1 新引入的错误（认证/限流/连接上限）以及批次 2 的 `generation.failed`；
- `requestId` **每请求独立 UUID**（不复用 `agent.next_request_id()` 三位计数器——其格式 `{NNN:03d}` 不唯一且带递增副作用）；
- `details` 公开安全（禁密钥/聊天正文/路径）。

**初始错误码目录：**

| 码 | 场景 | retryable | 本批次实现 |
|---|---|---|---|
| `AUTH_REQUIRED` | 未带凭据访问受保护端点 | false | ✅ |
| `AUTH_INVALID` | 凭据错误 | false | ✅ |
| `RATE_LIMITED` | 超出速率限制 | true | ⚠️ 仅入目录（限流器批次 2+） |
| `CONNECTION_LIMITED` | 超过并发 WS 连接上限 | true | ✅ |
| `REQUEST_TOO_LARGE` | 请求体超限 | false | ✅ |
| `INVALID_REQUEST` | 请求体/参数非法（含 415 图片类型，映射到 INVALID_REQUEST） | false | ✅ |
| `CONTRACT_VALIDATION` | 契约校验失败 | false | 批次 2 用 |
| `GENERATION_PROVIDER_UNAVAILABLE` | 模型服务不可用 | true | 批次 2 用 |
| `GENERATION_TIMEOUT` | 生成超时 | true | 批次 2 用 |
| `GENERATION_CANCELLED` | 生成被取消 | false | 批次 2 用 |
| `TOOL_NOT_FOUND` | 工具不存在 | false | 批次 4 用 |
| `RESOURCE_NOT_FOUND` | 资源/消息不存在 | false | 批次 3 用 |
| `INTERNAL` | 未归类内部错误 | false | ✅ |

**WS 认证错误：** 拒绝升级时返回 HTTP 401 + 信封；不升级为 WS 连接。

### 3.4 限制保护（BE-080 / BE-010）

- `max_request_bytes` → aiohttp **app 级** `client_max_size`（构造器参数，无按路由选项）；**multipart 原始流不受 client_max_size 约束**（`api_routes.py` 已有注释），图片上传仍走 `image_max_mb` 手工限制。`client_max_size` 从默认 1MB 升到 8MB（无 token 环境也变，属轻微行为变化，记录）；
- `max_ws_connections` → 并发 WS 连接上限，超出拒绝（HTTP 503 + `CONNECTION_LIMITED` 或握手拒绝）；
- 上传大小沿用现有 `image_max_mb`（图片）——通用媒体矩阵批次 7。

## 4. 兼容性分析

| 资产 | 影响 |
|---|---|
| v1 Vue 前端（本地无 token） | **零影响**（access_token 为空全匿名；`error` 兼容别名保错误消费） |
| v1 Vue 前端（远程有 token） | 不可用——预期权衡，由 Electron 替代 |
| `/api/*` 现有 curl 调用 | 无 token 环境不变；有 token 环境需带 `Authorization` |
| `CapabilitiesMessage`/`ApiFail`/`ImageUploadError` 等 v1 契约 | **不动**；新增 `ServerInfoMessage` + 错误信封模型 |
| `contract.generated.ts` | 追加新模型；`make check-types` 验证 v1 类型字节不变 |
| 现有测试 | 全部保留；`access_token` 走显式参数默认 `""`，无 token 环境行为不变，测试不翻红 |

## 5. 测试要求（TDD；marker 按 docs/testing.md）

- **认证**（service）：无 token 环境匿名全通（回归）；有 token 环境——匿名 `/api/*`→401、匿名 `/ws` 握手拒绝、带 token 全通、错误 token→401、`/uploads/*` GET 拒绝；
- **版本协商**（service）：带 `X-Protocol-Version: 2` 握手 → 收到 `server_info` 且**无 `capabilities`**（v2 用 server_info 取代）；不带 → 收现有 `capabilities`、无 `server_info`；
- **错误信封**（service）：401/503/413 响应体是统一信封 + 正确 code + `error` 别名；`details` 无敏感数据；
- **限制**（service）：超限 body → 413 `REQUEST_TOO_LARGE`；并发连接超限 → `CONNECTION_LIMITED`；
- **启动守卫**（service）：非回环 host + 无 token → ERROR 告警（捕获日志断言）；
- **TLS/反代可重复测试**（service）：反向代理配置校验（如 `--check` 检查或文档化配置的测试脚本）；
- **日志敏感扫描**（service）：token 不出现在日志；
- **回归**（unit/service）：`make test-quick` 全绿（Elysia conda）；`make check-types` 通过。

## 6. 产出交付物

1. 代码：`core/config/app_config.py`（ServerConfig 新字段）、`core/version.py`、`core/input/server.py`（auth 中间件 + 版本头路由 + 限制）、`core/agent/server.py`（`run_server` 启动守卫）、`core/input/ws_handler.py`（握手认证 + protocol_version 注入 + server_info）、`core/input/capabilities.py`（server_info 构建）、`frontend_contract/`（ServerInfoMessage + 错误信封模型）、`gen_ts` 重新生成 TS 快照；
2. 文档：`docs/electron-backend-security.md`（token 配置、HTTPS/WSS 反向代理部署、防护范围、已知缺口）+ 模块 README 更新；
3. 测试：§5 全部用例；
4. 轨迹：有 token 环境脱敏 WS 握手 + server_info 样例（`frontend-handoff-v2/traces/ws-server-info.jsonl`，若 gitignore 未覆盖需补充）。

## 7. 退出标准

- [ ] 匿名 WS/业务 REST/上传在配置 token 后均被拒（401/503），有效身份可完整聊天；
- [ ] v2 握手收到 `server_info`（含 features 全 false、`services` 形状），v1 握手行为不变（收 capabilities）；
- [ ] 错误响应统一信封 + 正确错误码 + `error` 兼容别名，日志无 token；
- [ ] 请求大小/并发限制生效（`REQUEST_TOO_LARGE`/`CONNECTION_LIMITED`）；
- [ ] 非回环 + 无 token 启动有显著阻塞告警；
- [ ] TLS/反向代理部署有可重复测试；
- [ ] `make test-quick` 全绿；`make check-types` 通过；
- [ ] 未修改任何 v1 契约模型；
- [ ] 无 token 环境行为与现状完全一致（含错误体形状）。

## 8. 非目标

- 不做速率限制实现（`RATE_LIMITED` 仅入目录；限流器批次 2+）；
- 不做多会话（批次 3）；
- 不做 v2 聊天事件 / generation 语义（批次 2）；
- 不做 Desktop 工具桥（批次 4）；
- 不做密钥 Secret Store（批次 5；本批次只保证"密钥不经匿名明文连接"）；
- 不做短期签名 URL / Range / 缩略图（批次 7）。
