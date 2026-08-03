# Electron 后端改造 — 批次 1：认证、版本协商、统一错误（Plan）

**Plan 版本：** v0.2（修订：吸收批次 1 plan review 全部发现）
**依据 Spec：** `docs/superpowers/specs/2026-08-04-electron-backend-batch1-auth-version-error.md`（v0.2）
**执行环境：** Elysia conda

---

## 概览

为远程部署建立认证 + 版本协商 + 统一错误。兼容硬约束：**未配置 access_token 时行为与现状完全一致**（v1 前端匿名可用、v1 错误体形状不变）；v2 由握手头 `X-Protocol-Version: 2` 判定。**TDD：每任务先写失败测试（含 marker）再实现。**

**批次 2 前置依赖（明确声明）**：Task 4 的 `protocol_version` 注入（`WebSocketChannel.protocol_version`）、`server_info`、Task 7 的错误信封均被批次 2 消费（`generation.failed` 用 code 目录、`protocol_version` 决定 v2 扇出）。

## 任务

### Task 1: 契约模型 + 错误码目录（frontend_contract）

**Files:** `frontend_contract/ws_message.py`、`frontend/src/types/contract.generated.ts`

- [ ] **RED**（unit）：新增 `ServerInfoMessage`（type=`server_info`，protocol_version/server_version/features/services）+ 统一错误信封模型（code/message/retryable/requestId/error/details）round-trip 测试
- [ ] 实现契约模型 + `gen_ts` 重新生成 TS 快照
- [ ] **错误码目录交付物**：`docs/electron-backend-protocol-v2.md`（或独立错误码文档）定义初始目录：`AUTH_REQUIRED`/`AUTH_INVALID`/`CONNECTION_LIMITED`/`REQUEST_TOO_LARGE`/`INVALID_REQUEST`（含 415 图片类型映射）/`INTERNAL` 本批实现；`RATE_LIMITED` 仅入目录；`CONTRACT_VALIDATION`/`GENERATION_*`/`TOOL_NOT_FOUND`/`RESOURCE_NOT_FOUND` 供批次 2/3/4 消费
- [ ] `make check-types` 通过；**验证 v1 类型字节不变**（`git diff frontend/src/types/contract.generated.ts` 只见新增）
- [ ] Commit `feat(contract)`

### Task 2: 配置字段（app_config）

- [ ] **RED**（unit）：`ServerConfig.access_token`（env `AGENT_ACCESS_TOKEN`，`secret=True`）、`max_ws_connections`、`max_request_bytes`
- [ ] 实现 ConfigField
- [ ] Commit `feat(config)`

### Task 3: 认证中间件（server.py + ws_handler.py）

- [ ] **RED**（service）：
  - 无 token 环境：匿名 `/api/*`、`/ws`、`/uploads/*` 全通（回归）
  - 有 token 环境：匿名 `/api/*`→401 信封 `AUTH_REQUIRED`；错误 token→401 `AUTH_INVALID`；带 token→通过；`/uploads/*` GET 无 token→401；`/ws` 握手无 token→401 拒绝升级
- [ ] 实现：**`create_app(agent, ..., auth_token="", access_token="")` 显式参数、默认 `""`、不得在 `create_app` 内部读取 `agent.config.server.access_token`**（防 `test_server.py` `_make_app()` / `test_providers_api.py` MagicMock 全量 401 翻红）；业务 API 中间件（401 + 信封）；`/uploads/*` 路由替换 `add_static` 为带鉴权处理；`handle_ws` 在 `ws.prepare()` 前校验握手头
- [ ] Commit `feat(auth)`

### Task 4: 版本协商 + server_info（ws_handler.py + capabilities.py + core/version.py）

- [ ] **RED**（service）：带 `X-Protocol-Version: 2` 握手 → 收到 `server_info` **且无 `capabilities`**；`features` 全 `false`；`services` 形状正确（available/enabled/model/default）；不带 → 收现有 `capabilities`、无 `server_info`
- [ ] 实现：`core/version.py`（`__version__="0.1.0"`）；`handle_ws` 读握手头判定 `protocol_version` 注入 `WebSocketChannel.protocol_version`；v2 连接发 `server_info`（取代 `capabilities`）
- [ ] Commit `feat(version)`

### Task 5: 启动守卫（core/agent/server.py）

- [ ] **RED**（service）：非回环 host + 无 access_token → ERROR 级告警输出（捕获日志断言）
- [ ] 实现：**`run_server`（`core/agent/server.py`，非 `core/input/server.py`）** 每轮重启读取 host 后检查，显著阻塞告警
- [ ] Commit `feat(auth)`

### Task 6: 限制保护（server.py）

- [ ] **RED**（service）：超限 body → 413 `REQUEST_TOO_LARGE`；并发 WS 超 `max_ws_connections` → 503 `CONNECTION_LIMITED`
- [ ] 实现：`client_max_size` **app 级** `web.Application(client_max_size=...)` 构造器参数（aiohttp 无按路由选项；multipart 除外，图片仍走 image_max_mb）；WS 并发计数（进入/退出增减，注意异常路径释放）
- [ ] Commit `feat(limit)`

### Task 7: 错误信封接入（api_routes 等）

- [ ] **RED**（service）：业务 API 错误路径（400/413/415/500）响应统一信封 + 正确 code + **`error` = `message` 兼容别名**；`requestId` 每请求独立 UUID（不复用 `next_request_id()`）；`details` 无敏感数据
- [ ] 实现：错误响应包装；`requestId` 生成（独立 UUID）；现有 `ApiFail`/`ImageUploadError`/`MessagesResponse` **不动**
- [ ] Commit `feat(error)`

### Task 8: 安全文档 + 轨迹 + 反代测试

- [ ] `docs/electron-backend-security.md`：token 配置、HTTPS/WSS 反向代理部署、防护范围、已知缺口（`/uploads/*` 匿名 GET 已拒，签名 URL 批次 7）
- [ ] **TLS/反向代理可重复测试**（service）：反代配置校验（`--check` 扩展或配置校验测试脚本）——spec §5/§7 明确要求，非仅文档
- [ ] 有 token 环境脱敏 WS 握手 + server_info 轨迹 → `frontend-handoff-v2/traces/ws-server-info.jsonl`
- [ ] 日志敏感扫描：断言 token 不出现在日志（测试）
- [ ] Commit `feat(security)`

### Task 9: 验收（subagent）

- [ ] 验收 subagent：核对批 1 全部需求（匿名拒绝、有效身份可用、server_info 无 capabilities、features 全 false、错误信封 + error 别名、启动守卫、限制、TLS 测试、无 token 环境回归、日志无 token）；`make test-quick` + `make check-types` 全绿

### Task 10: 提交策略（澄清）

- [ ] **每 Task 完成后即时 commit**（Task 1-8 内含 Commit 步骤），前缀规范：`feat(auth)`/`feat(version)`/`feat(error)`/`feat(config)`/`feat(contract)`/`feat(limit)`/`feat(security)`——**不是最后统一提交**

## 风险

- **v1 测试不翻红（最高）**：access_token 必须显式参数默认 `""`；实现者若从 `agent.config.server.access_token` 读取，现有 MagicMock 测试全量 401 翻红
- `/uploads/*` 鉴权破坏 v1 Vue `<img>`——已接受权衡（spec §3.1）；无 token 环境不受影响
- `client_max_size` 是 app 级（无按路由选项），默认从 1MB 升 8MB（无 token 环境也变，轻微行为变化，spec 已记录）
- WS 并发计数在 `handle_ws` 进入/退出增减，异常路径须释放

## 非目标

多会话（批 3）、v2 聊天事件（批 2）、速率限制实现（`RATE_LIMITED` 仅入目录）、Desktop 工具桥（批 4）、密钥 Secret Store（批 5）、签名 URL/Range/缩略图（批 7）。
