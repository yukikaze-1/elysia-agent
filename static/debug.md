# Debug Dashboard

`GET /debug` — Elysia Agent 内部状态监控仪表板。

## 实现

Debug 页面由 Vite 多页构建生成，入口为 `frontend/debug.html` → `frontend/src/debug/main.ts`（Vue 3 + Pinia + TypeScript）。构建产物输出到 `static/debug.html`（15 行入口）+ `static/assets/`（JS/CSS bundle）。

详见 [CLAUDE.md](../CLAUDE.md) 的 Frontend / Debug 页面章节。
