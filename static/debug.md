# Debug Dashboard — 使用说明

> 文档导航：[项目 README](../README.md)

`GET /debug` — Elysia Agent 内部状态监控仪表板。**回答「角色为什么这么说」「这条记忆进库了吗」「后台任务卡在哪」的窗口**——日志只给计数、测试只给绿灯、`/health` 只给健康位，这里是唯一的活体观测面。

## 快速开始

```bash
cd frontend && npm ci && npm run build   # 一次性；或 bash scripts/start.sh --with-frontend
bash scripts/start.sh                    # 或 SERVER_AUTH_TOKEN=<随机串> bash scripts/start.sh
```

打开 **http://localhost:8765/debug**。

**前提是前端已构建**：`/debug` 只在 `static/debug.html` 存在时才注册（`core/input/server.py::_register_static_routes`），未构建时恒 404。此时**启动横幅会如实告诉你**：

```
/debug 仪表盘: 未构建（缺 static/debug.html）—— cd frontend && npm run build
```

## 凭据闸门

`/debug/api/*` 属**管理面**，`_check_auth` **只认 `SERVER_AUTH_TOKEN`**——产品 token（`AGENT_ACCESS_TOKEN`）按 CR-03 被刻意排除，它换不到管理面。

| 后端配置 | 页面表现 |
|---|---|
| 没配 `SERVER_AUTH_TOKEN`（回环本地开发的常态） | 管理面直接放行，**零打扰**，不出现任何凭据提示 |
| 配了 `SERVER_AUTH_TOKEN` | 数据面 401 → 弹出**凭据闸门**，填入同一个值即可 |

闸门的三条约束：

| 约束 | 理由 |
|---|---|
| 存 `sessionStorage`，**不是** localStorage | 关掉标签页即失效，缩小残留窗口 |
| **绝不进 URL**（无 `?token=`） | 查询参数会进访问日志 / Referer / 浏览器历史（`docs/electron-backend-security.md` §1.3） |
| **绝不落日志** | 错误信息只带 HTTP 状态码 |

> 用全屏闸门盖住面板、而不是让面板显示一堆 0，是刻意的——「全 0」会被误读成真实状态。

## 界面总览

顶栏：**概览 | 数据库 | 任务** 三个 Tab，右侧 `⏸ 暂停` / `📋 JSON` / `📋 JSON (light)`（把当前全量数据复制到剪贴板，缩进 0 / 2）。

数据刷新方式**因 Tab 而异**，这决定了你看到的数字是不是「新鲜」的：

| Tab | 刷新方式 | 说明 |
|---|---|---|
| **概览** | 2s 轮询 `/debug/api` | 只有停在这个 Tab 才轮询，切走即停；失败按指数退避到最长 30s |
| **数据库** | 切过去时加载一次 | 之后由面板内操作触发重载（搜索、翻页、删除后），**没有手动刷新按钮** |
| **任务** | SSE 实时流 | 打开期间**服务端才会**启动 500ms 快照轮询，离开即停 |

`⏸ 暂停` 停掉概览轮询（顶栏出现「已暂停」），用来冻结画面读数。

## 概览 Tab — 16 个面板

| # | 面板 | 看什么 | 怎么读 |
|---|---|---|---|
| 1 | **System** | FSM 状态 / 上一状态 / Uptime / LLM / TTS / STT / WS 客户端数 / Proactive / 距上次交互 | `thinking` 或 `speaking` = 正在跑回合；`WS Clients 0` 说明没有客户端连着（Electron 未连时正常） |
| 2 | **Memory** | Session 消息数、**LTM 内存 / Milvus 双计数**、MongoDB / Milvus / Embedding 健康、Archive 条数、角色名、L1 人格摘要、Character State Raw | 见下方「LTM 双计数」；人格摘要与 State Raw 可展开 |
| 3 | **Latency** | 回合延迟 Count / Avg / P50 / P95 / P99 / Last | 无数据时整个面板隐藏 |
| 4 | **Process Memory** | RSS / VMS / CPU% / 线程数 | 查内存泄漏看 RSS 趋势 |
| 5 | **Scheduler** | 工具调度线程池 Pending / Running / Total | 卡在 Running 说明有工具没返回 |
| 6 | **Token Consumption** | 本会话 token 累计 + 按模型分布 | 按模型拆分能看出是不是切过模型 |
| 7 | **LLM Prompt** | 最近 20 条 prompt 摘要（`#序号 R轮 · 消息数 · 字符数 · 模型`）+ `View` | **点 View 看完整 system prompt**——回答「为什么这么说」的入口 |
| 8 | **LLM Response** | 最近 20 条回复摘要（字符数 + inner_voice 长度）+ `View` | 对照 prompt 看原始输出与 `<inner_voice>` |
| 9 | **Recent Session** | 当前会话最近消息 | 图片缩略图可点开灯箱 |
| 10 | **LTM Recent** | 最近 5 条长期记忆 | 提取到东西时这里最先有动静 |
| 11 | **Tools (N)** | 已加载的工具插件 | 超过 4 个可展开 |
| 12 | **Tool History** | 最近 5 次工具执行 | 空 = 这段时间没调工具 |
| 13 | **Consolidation** | `Buffer N / 6`（阈值）+ **`Last Run`** + 已提取条目 | 见下方「Consolidation 的 Last Run」 |
| 14 | **Memory Reflector** | 轮数/阈值、新记忆数/阈值、锁、上次摘要 | 定期去重 LTM |
| 15 | **Character Reflector** | 缓冲、上次反思时间、锁、耗时、成功与否、上次 prompt | L3 状态由它驱动 |
| 16 | **Character Engine** | Mood、C3 冷却、L2 Self、L2/C6 版本计数 | 版本号可点开历史并回滚 |

### LTM 双计数：`内存 N / Milvus M`

```
LTM   0 / Milvus 0
```

- 左边的 `N` 是**进程内** `_entries` 的长度（`LongTermMemory.count`）；右边的 `M` 是 **Milvus 集合真实行数**。
- **两个数不一致就是「写了但没落库」**，面板会标红。只看左边会被骗：Milvus 写入静默失败时它照样显示有条目。
- `Milvus -1` = 没取到（Milvus 不可用）。
- 出现 `⚠ N 条待同步（未落库）` = 拿到了内存条目但没拿到 Milvus durable PK，那些条目重启即丢。

### Consolidation 的 `Last Run`

```
Buffer      1 / 6
Last Run    1min 前 · 12 turns → 提取 0 条，入库 0 条
```

**这一行是为了区分四种此前完全同形的状态**：

| 显示 | 含义 |
|---|---|
| `从未运行` | 缓冲还没攒到 6 对 |
| `正在提取…` | LLM 提取进行中 |
| `X 前 · 12 turns → 提取 0 条，入库 0 条` | **跑过了，这段对话里没有值得长期记住的事实**——是正常结果，不是故障 |
| `X 前 · 失败：…` | 提取或存储失败，batch 已重新入队待重试 |

> 提取 0 条是**常见且正确**的：`ExtractMemory.j2` 明令禁止从角色的玩笑、安慰、反问、猜测、文学化表达里提取，也禁止据称呼 / 语气推断用户身份。纯陪聊（"嘿嘿" / "❤️"）提取 0 条是设计预期。
>
> 想让它提取出东西，就说**明确的事实**：「我养了一只猫叫橘子」「下周三是我的生日」「我平时喝美式不加糖」。攒满 6 对触发。

## 数据库 Tab

三段，操作的是真实数据库——破坏性操作都有确认弹窗，但**没有撤销**。

| 段 | 能做什么 |
|---|---|
| **Long-Term Memory (Milvus)** | 按类型筛选（Fact / Plan / Preference / Event / Note）+ 搜索、新增、编辑、单条删除、勾选批量删除、清空全部 |
| **Sessions (MongoDB)** | 列出全部会话（消息数 / token 累计 / 更新时间）、查看详情、删除、清空全部 |
| **Archive (MongoDB)** | 消息归档：搜索 + 按角色筛选（user / assistant / tool / system）、编辑、删除、批量删除、清空全部 |

## 任务 Tab

- 顶部**连接指示**：`已连接` / `重连中...` / `已断开`（SSE 断线按指数退避重连，1s → 30s）
- **活跃任务**：每张卡片含状态徽章、进度条、vis-network DAG（节点按状态着色）
- **已完成**：可折叠，最多保留 100 条

## 几个典型排障流程

**「角色为什么这么说？」**
概览 → `LLM Prompt` 找那一轮的 `View` → 看完整 system prompt（12 个 section + 历史 + 工具定义）。
**prompt 环形缓冲是进程内存，重启即空**——要复现就先重启再对话。

**「这条记忆进 LTM 了吗？」**
1. 概览 → `MEMORY`：`LTM 内存 N / Milvus M` 是否一致？有没有「待同步」警告？
2. 概览 → `LTM Recent`：最近 5 条里有没有？
3. 数据库 → `Long-Term Memory`：搜关键词，还能编辑 / 删除

**「为什么没提取到记忆？」**
概览 → `CONSOLIDATION` → 读 `Last Run`：没跑（缓冲没满）/ 跑了提取 0 条（内容没事实）/ 失败（看原因）。

**「后台任务卡在哪？」**
任务 Tab → 找活跃任务的 DAG → 看哪个节点停在非终态；配合概览的 `Scheduler` 与 `Tool History`。

**「面板全是 0 / 一直转圈」**
1. 是不是弹着凭据闸门？（配了 `SERVER_AUTH_TOKEN` 却没填）
2. `/debug` 是 404 还是 200？404 = 前端没构建
3. 后端日志有没有 `认证失败 code=AUTH_REQUIRED path=/debug/api`

## 已知限制

- **环形缓冲是进程内存**：`LLM Prompt` / `LLM Response`（各 20 条）、`Consolidation` 历史、`Tool History` 都在内存里，**重启即空**。这不是故障。
- **`Embedding ✗` 有两种含义**：模型还在后台加载中（冷启动约 10s），或加载失败。区分看 `log/app.log` 的 WARNING——加载失败会写明「长期记忆**不会**写入 Milvus」。
- **`Mood` 与 prompt 缓冲不一致是正常的**：Mood 走 session 持久化、重启保留；prompt 缓冲重启即空。
- **数据库 Tab 直接改真实库**，确认弹窗只防误触，没有回滚。

## 实现

Debug 页面由 Vite 多页构建生成，入口为 `frontend/debug.html` → `frontend/src/debug/main.ts`（Vue 3 + Pinia + TypeScript）。构建产物输出到 `static/debug.html` + `static/assets/`。

**所有请求必须经 `frontend/src/debug/auth.ts::debugFetch`**（它补 `Authorization` 头）。
`EventSource` 协议上无法设置请求头，故任务流的 SSE 改用 `frontend/src/debug/sse.ts` 的
`fetch` + `ReadableStream` 实现。这两条由 `make check-debug-auth` 静态守卫，漏改任何一处
调用都会红。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [core/debug/README.md](../core/debug/README.md) | 数据层：`DebugInfo` 聚合 + Prompt/Response 录制 + 全部 API 端点 |
| [docs/electron-backend-security.md](../docs/electron-backend-security.md) | 管理面鉴权语义（CR-03 / S2-F3） |
| [Module/workflow_engine/docs/api.md](../Module/workflow_engine/docs/api.md) §9 | 任务流 SSE 端点（懒启动 + keepalive） |
| [CLAUDE.md](../CLAUDE.md) | Frontend / Debug 页面章节 |
