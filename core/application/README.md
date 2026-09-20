# core/application — 窄应用用例层

> 文档导航：[项目文档](../../README.md)

`core/input`（传输层）与运行时组件（Agent / Repository / 工具子系统）之间的用例边界（ARCH-INC-P1-02）：只收已有第二消费者或含真实业务规则的用例，不做通用 Service 层。

## 关键机制

- **一个用例一个实现，多个入口共用**：会话激活原先在 WS handler 与 REST route 各写一遍，判定顺序不同（同一个未知会话在 WS 得到 `AGENT_BUSY`、在 REST 得到 `SESSION_NOT_FOUND`）。`SessionApplicationService.activate()` 统一为**存在性优先**（更具体的事实先判），失败以 `SessionUseCaseError` 抛出并携带契约错误码（`CONTRACT_VALIDATION` / `SESSION_NOT_FOUND` / `AGENT_BUSY` 且 `retryable=True`），由传输层映射状态码。
- **最小面注入**：`SessionApplicationService` 只收 `has_session` / `switch_session` / `is_accepting_input` 这几个 callable，不接收整个 Agent 做 Service Locator；`SessionManager.switch()` 内部抛出的 `SessionBusyError` 被捕获为同一个 `AGENT_BUSY`——这是"检查与切换之间"竞态窗口的双保险，各入口答案一致。
- **签名规则单源**：`AttachmentSigningService` 同时承担签发（`sign()` → 站内相对路径 + `expires_unix`）与验证（`verify()`）；TTL 与验证窗口共用同一个 `SIGNATURE_TTL_SECONDS`，避免"签出来立刻过期"这类只在生产暴露的错配。
- **校验序固定为 sig 形态 → exp 窗口 → HMAC**（`hmac.compare_digest` 防时序侧信道）；记录存在性由调用方查库、不在这里——签名前置以防存在性枚举。
- **运行态由配置组合决定**：`disabled`（配了 token 却没配密钥 → fail-closed，`disabled_outcome()` 给出 `ATTACHMENT_SIGNATURE_DISABLED`）；`verify_enabled`（两者都配 → 真正校验）；两者皆非（未配 token → fail-open，`verify()` 恒 ok，调用方仍须做存在性校验）。
- **只做规则，不做传输**：不 import aiohttp、不构造响应、不认识 HTTP 状态码；错误码是契约层已登记的稳定码，由调用方映射。
- **装配在组合根之外唯一一处**：`service_for_agent(agent)`（包内重导出为 `signing_service_for_agent` / `session_service_for_agent`）现建服务——签名侧只读 `agent.config.server` 的 `access_token` / `attachment_sig_secret`。

## 文件布局

```
core/application/
├── __init__.py            — 重导出（含别名 signing_service_for_agent / session_service_for_agent）
├── sessions.py            — SessionApplicationService + SessionUseCaseError + CODE_* + service_for_agent
├── attachment_signing.py  — AttachmentSigningService + VerifyOutcome + SIGNATURE_TTL_SECONDS + CODE_* + service_for_agent
└── tests/                 — 用例服务单测（marker=unit；脱离 aiohttp 服务即可断言规则，时钟可注入）
```

关键入口：`SessionApplicationService.activate(conversation_id)`（返回 `SessionMemory`，WS 侧还要用它做回放）；`AttachmentSigningService.sign(attachment_id)` / `verify(attachment_id, sig, exp_raw)` / `disabled` / `verify_enabled` / `disabled_outcome()`；`VerifyOutcome` 用 `__bool__` 表达 ok。

## 依赖与所有权

- **方向**：`core.input → core.application → core.memory`。`sessions.py` 只取 `core.memory.session_manager.SessionBusyError`（`SessionMemory` 仅 `TYPE_CHECKING`）；`attachment_signing.py` 只用 stdlib（`hmac` / `hashlib` / `re` / `time`）。本包不 import `core.input`，也不 import `Module/`。
- **谁消费**：`core/input` 的多个入口——`upload_routes._sign_url`（薄壳，保持既有函数名）、`attachment_routes`（下载验签 + 签发端点）、`session_routes` 与 `core/input/ws_handlers/session.py`（会话激活）。
- **谁构造**：无状态、按请求现建（`service_for_agent(agent)`），不缓存、不持会话对象；Agent 生命周期不感知本包。
- `make check-arch` 保证方向：R2 禁止领域 / 基础设施层反向依赖 `core.input`，本包处在被传输层依赖的一侧。

## 不属于本包

- 不做通用 Service 层、不引 DI 容器、不做 Repository 的无行为代理（分层评估稿的明确约束）。
- 不接收完整 Agent 后穿透其内部组件——只取所需的 callable / 配置值。
- 不解析 wire 与 HTTP：query、header、multipart、状态码映射、错误信封都留在 `core/input`。
- 不承载只有单一消费者的转发型用例（工具控制等仍是评估稿里的未做项）。
- 不管生命周期与连接状态：服务无状态，谁构造谁管理。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [core/input/README.md](../input/README.md) | 传输层——本包用例的调用方 |
| [core/memory/README.md](../memory/README.md) | `SessionManager` / `SessionBusyError` 的来源 |
| [分层评估 §ARCH-INC-P1-02](../../docs/review/2026-08-30-后端架构分层与模块化增量评估.md) | 本层的设立动因与约束 |
| [批次 7 spec](../../docs/superpowers/specs/archive/2026-08/2026-08-06-electron-backend-batch7-media-and-observability.md) | 附件签名 URL 的协议与密钥策略 |
