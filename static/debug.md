# Debug Dashboard

`GET /debug` — Elysia Agent 内部状态监控仪表板。

## 实现

Debug 页面由 Vite 多页构建生成，入口为 `frontend/debug.html` → `frontend/src/debug/main.ts`（Vue 3 + Pinia + TypeScript）。构建产物输出到 `static/debug.html`（15 行入口）+ `static/assets/`（JS/CSS bundle）。

**路由是条件注册的**：`/debug` 只在 `static/debug.html` 存在时才注册（`core/input/server.py::_register_static_routes`），未构建前端时恒 404；此时启动横幅会如实打印「未构建（缺 static/debug.html）—— cd frontend && npm run build」。

```bash
cd frontend && npm ci && npm run build     # 或 bash scripts/start.sh --with-frontend
```

## 凭据（S2-F3，2026-09-12）

`/debug/api/*` 属**管理面**，`_check_auth` **只认 `SERVER_AUTH_TOKEN`**——产品 token
（`AGENT_ACCESS_TOKEN`）按 CR-03 被刻意排除，它换不到管理面。

- **没配 `SERVER_AUTH_TOKEN`**（回环本地开发的常态）：管理面直接放行，页面零打扰，
  不会出现任何凭据提示。
- **配了 `SERVER_AUTH_TOKEN`**：数据面 401，页面弹出凭据闸门。填入同一个值即可。

闸门的三条约束：

| 约束 | 理由 |
|---|---|
| 存 `sessionStorage`，**不是** localStorage | 关掉标签页即失效，缩小残留窗口 |
| **绝不进 URL**（无 `?token=`） | 查询参数会进访问日志 / Referer / 浏览器历史（`docs/electron-backend-security.md` §1.3） |
| **绝不落日志** | 错误信息只带 HTTP 状态码 |

**所有请求必须经 `frontend/src/debug/auth.ts::debugFetch`**（它补 `Authorization` 头）。
`EventSource` 协议上无法设置请求头，故任务流的 SSE 改用 `frontend/src/debug/sse.ts` 的
`fetch` + `ReadableStream` 实现。这两条由 `make check-debug-auth` 静态守卫，漏改任何一处
调用都会红。

## 相关文档

- [CLAUDE.md](../CLAUDE.md) — Frontend / Debug 页面章节
- [../docs/electron-backend-security.md](../docs/electron-backend-security.md) — 管理面鉴权语义（CR-03 / S2-F3）
- [../Module/workflow_engine/docs/api.md](../Module/workflow_engine/docs/api.md) §9 — 任务流 SSE 端点
