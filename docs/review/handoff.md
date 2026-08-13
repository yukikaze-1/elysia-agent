# Elysia Agent / Cyrene 开发环境迁移与真实联调上下文交接

**交接日期：** 2026-08-14（Asia/Shanghai）

**旧开发环境：** Ubuntu 22.04，仓库位于 `/home/yomu/elysia-agent` 和 `/home/yomu/elysia-frontend`

**新开发主机：** Ubuntu 22.04，IP `192.168.1.18`，CPU Xeon E5-2690 v3，内存 32 GiB，已安装 Codex CLI

**本文用途：** 在新机器上不依赖旧对话记录，恢复项目上下文、代码基线、环境、数据与联调进度，并记录 B-01 真实 Electron 回归的最终结果。

> 本文不包含 `.env`、API key、`AGENT_ACCESS_TOKEN` 或数据库密码的明文。凭据必须通过 SSH/可信秘密管理通道单独迁移，不得写入 Git 或联调报告。

---

## 0. 一页结论

1. 新 Electron 前端与后端的文本、会话、流式生成、真实工具调用、取消与重连主链已完成三轮 Windows Electron + Ubuntu 后端真实联调。
2. 第二轮发现的 Renderer 水合/replay 身份/取消码问题和第三轮发现的 WS 压缩空闲断连已全部修复并复验。
3. **B-01 十项已于 2026-08-14 在后端 `73ac7a3` + 前端 `6b27e2e` 的最终验收基线上全部通过。** 压缩修复后在新 SHA 上重跑了受影响的文本/reload/取消/诊断项，未受影响的工具/replay/历史项沿用同一轮父提交原始证据。当前状态是“已真实联调”，阶段 3 可转为主线。
4. 第三轮已留下原始帧、REST/replay/Store 身份、Renderer reload、取消、两轮 `76s` 空闲与全轮诊断证据；详见同日联调结果报告。
   - 后端完整报告：[2026-08-14 Windows Electron 第三次真实联调结果](2026-08-14-Windows-Electron-第三次真实联调结果.md)
   - 前端闭环报告：`/home/yomu/elysia-frontend/docs/review/2026-08-14-Windows-Electron-第三次真实联调前端阻断.md`
5. 最稳妥的 Windows 联调拓扑仍是：新 Ubuntu 主机仅在回环 `127.0.0.1:8765` 运行后端，Windows 用 SSH 本地端口转发访问，Electron 仍填 `http://127.0.0.1:18765`。新 IP 只出现在 SSH 目标 `192.168.1.18`，不直接暴露 8765。
6. Git 传递状态：

   - 前端 `main` 与 `origin/main` 已同步到 `6b27e2e`，包含压缩修复、回归和前端报告。
   - 后端本交接文档与第三次联调结果报告仍需 commit/push 或单独传递。

---

## 1. 当前代码冻结快照

### 1.1 仓库状态

| 项目 | 旧机路径 | 分支 | 当前 HEAD | 远端状态 | 交接结论 |
|---|---|---|---|---|---|
| 后端 | `/home/yomu/elysia-agent` | `main` | `73ac7a32415aa96bbec7687ae0c9e674ab675438` | 创建本文前与 `origin/main` 同步 | 二次联调 replay 身份修复与验收测试已在此 SHA |
| Electron 前端 | `/home/yomu/elysia-frontend` | `main` | `6b27e2e1ebf4e6ba3e335352379892e7c2679da2` | 与 `origin/main` 同步 | 第三次联调压缩修复、握手回归与前端报告已在此 SHA |

远端：

- 后端：`https://github.com/yukikaze-1/elysia-agent.git`
- 前端：`https://github.com/yukikaze-1/elysia-frontend.git`

新机 clone/复制后先执行：

```bash
cd /home/yomu/elysia-agent
git status -sb
git rev-parse HEAD

cd /home/yomu/elysia-frontend
git status -sb
git rev-parse HEAD
```

期望两个 SHA 与上表一致，工作树无未知改动。如果后续对本文做了 commit，后端 HEAD 自然会比上表更新；应确认其父 commit 包含 `73ac7a3`。

### 1.2 当前后端机器契约

| 项 | 当前值 |
|---|---|
| transport version | `2` |
| protocol version | `2.0` |
| `frontend_contract/contract.schema.json` schemaHash | `e5d4dd2b9296b727` |
| 前端 raw frame 覆盖 | WS v2 `13/13` |
| 后端最后记录的快速门禁 | `3548 passed, 2 skipped, 183 deselected` |
| 前端最后记录的门禁 | 526 单测（47 files）+ 23 集成（6 files）+ typecheck/lint/build + contract 13/13（63 帧）+ package/smoke |

> `docs/electron-v2-first-integration-runbook.md` 的冻结表仍有旧 schemaHash 和“待联调回填”占位值。B-01 复跑时应将真实 SHA/schemaHash 回填；本文中的当前值优先于该旧占位值。

---

## 2. 项目背景和开发分工

### 2.1 两个前端不要混淆

- `elysia-agent/frontend` 是旧的 v1 Vue 网页前端，默认 `SERVE_CHAT=0`，访问后端 `/` 返回 404 是正常现象。
- `/home/yomu/elysia-frontend` 是正在开发的新 Electron + Vue 3 + TypeScript 桌面客户端，产品联调对象是这个仓库。
- Electron 前端的 Renderer 不直接保管 token，连接、凭据、WS/REST 在 Electron Main/Preload 受信边界内完成。

### 2.2 本轮协作过程

1. 先审阅后端是否足够支持新 Electron v2 协议，把问题写入后端 `docs/review/` 和 `todo.md`。
2. 对前端阶段 0/1/2 分批 review，问题分别记录到前端 `docs/review/`；前端主要由 Claude Code 实施，后端由本任务重点 review/修复。
3. 前后端按 review 清单多轮修复，先通过单仓自动化门禁，再进入 Windows Electron + Ubuntu 后端的真实联调。
4. 首轮真实联调暴露后端历史/新写入 tool `message_id` 空值，以及前端对 `state/typing` 合法帧误报协议错误。双方修复后做了第二轮联调。
5. 第二轮真实联调确认文本、工具、取消主链可用，同时发现 Renderer 水合、WS replay 身份和取消码消费问题。
6. 目前两仓已完成第二轮问题修复和自动化验收，但尚未用更新后的双方 SHA 做第三次真实回归。

---

## 3. 已做过的真实联调和证据

### 3.1 当时的拓扑和运行时

- Ubuntu 主机运行后端，默认回环 `127.0.0.1:8765`。
- Windows 运行 Electron 开发版，Renderer 是 `http://localhost:5173/`。
- Windows 通过 SSH 本地端口转发访问 Ubuntu 后端，Electron Endpoint 填 `http://127.0.0.1:18765`。
- Windows 真实运行时为 Electron `43.3.0` / Chrome `150.0.7871.212`，Node `22.23.0`，pnpm `11.20.0`。
- 为了自动化观察 Electron，当时临时在 Windows 回环开启过 CDP `127.0.0.1:9223`，并反向转发到 Ubuntu `127.0.0.1:19223`。这仅是开发/测试通道，不是产品配置，打包产物会拒绝固定远程调试端口。

第二轮真实联调基线：

- 后端：`88ee650125db681b2289296dbaf8551a1c44fb56`
- 前端：`db6d127616e840b90d8ab45a76608d215aa9604f`

因此，下面的“真实通过”证据只证明该基线的 happy path；当前 `73ac7a3 + 05f5af0` 的修复组合需重新验证。

### 3.2 已通过的真实主链

- 认证、v2 握手、`REAL` 运行模式和连接状态。
- `/health` 正常，`/ready` 显示 `chatReady=true` / `fullReady=true`，Mongo、Milvus 和幂等依赖 ready。
- 列会话、创建/激活会话、历史分页、旧 `default` 会话读取。
- 乐观 user 消息到 accepted ACK 的 ID 迁移。
- `inner_voice` / `public_reply` 双通道流式分段和 completed 终态。
- 真实 `get_time` 工具执行和最终公开回复。
- 用户主动取消长生成，后端返回 `GENERATION_CANCELLED`，当次 UI 最终显示 `interrupted`，连接保持。
- 手动断开、重连、session replay 和活动会话恢复。
- `SESSION_PROTECTED` 等结构化错误的可见显示。

### 3.3 保留的联调会话和关键 ID

**测试会话：**

```text
conversationId = conv_036c872c7c55
title          = B-01 修复后回归 2026-08-13 2c7c55
```

该会话当时被刻意保留，但它位于旧机 Mongo 数据中；如果新机使用空数据库，该会话不会随 Git 出现。

**纯文本：**

```text
prompt        = 【B-01 修复后文本回归】请不要调用任何工具，只回复：B-01 修复后文本联调成功
reply         = B-01 修复后文本联调成功
generationId  = gen_21769f343a884a6aaa9f7eb2f0
assistant ID  = msg_22f71...   # 报告中仅保留了该前缀
```

**真实工具：**

```text
tool          = get_time
tool_call_id  = call_00_8TBYMy3a8SKsXlHnVjgX8457
tool message  = msg_7c0c72b0a8ef42ad942d4f8237
tool content  = 2026-08-13 21:15:31 CST
public reply  = 现在是 2026 年 8 月 13 日晚上九点一刻左右。
```

**取消：**

```text
generationId = gen_c3b39f0752c64bafbfbce2b8fe
backend code = GENERATION_CANCELLED
```

**旧 `default` 会话回归：**

- 读取最近 50 条：user 19 / assistant 25 / tool 6。
- 当时已确认 50 条 `message_id` 全部非空、无重复，`hasMore/cursor` 正常。

---

## 4. 已修复并完成真实复跑的问题

> 2026-08-14 封口更新：本节四项已全部通过第三次 Windows Electron 复跑。下方每项末尾的“待复跑”是迁移时的原始状态记录，不再是当前结论。

### 4.1 后端：WS replay 工具消息身份

第二轮联调中，同一条 tool 结果经 REST 返回稳定 `message_id/timestamp`，但 WS `session` replay 遗漏这两个字段。前端因而产生 `<conversationId>:replay` fallback，随后又插入 REST 正式 ID，导致同一 tool 消息在 store 中出现两次（REST 6 条，store 7 条）。

当前后端已修复：

- `messages_for_client()` 的 tool 分支输出消息自身 `message_id/timestamp`，复用持久/稳定派生的 ID，不在 replay 时生成新 ID。
- `ClientToolMessage` 要求身份字段。
- `SessionMessageItem.message_id/timestamp` 收紧为必填非空，契约产物已重生成。
- 真实存量数据 sanity 显示 `msg_7c0c...` 在持久消息与 `messages_for_client()` 中相同。

实现 commit：

- `2df115c` — tool replay 输出身份。
- `fb9f8f1` — 契约身份必填非空与产物重生成。
- `2c26078` — 补齐 REST/WS/重复 replay/空身份验收测试。
- `73ac7a3` — 报告、进度、todo 和测试统计同步。

状态：**代码与自动化已关闭 BE-INT2-P0-01 / BE-INT2-P1-01；真实 Electron 跨出口一致性待复跑。**

### 4.2 前端：Renderer 启动水合

原问题：只 reload Renderer 而 Main 仍保持连接时，新 Pinia 回到 `disconnected`，UI 显示“无会话/未连接”；必须手动 disconnect/connect 才恢复。

当前前端已修复：

- 新增 `renderer/src/bootstrap.ts`，push 和启动 snapshot 共用 `applyChatState`。
- 先注册订阅，再读 `getConnectionState()`。
- 使用启动 epoch 防止迟到 snapshot 覆盖更新 push。
- connected snapshot 会继续恢复会话列表、活动会话与历史。

实现 commit：`a9a9b0d`。

状态：**单测/集成门禁已通过；真实 Renderer reload 待复跑。**

### 4.3 前端：replay 身份防御

当前前端已修复：

- replay `message_id/timestamp` 收紧为必填非空。
- 删除共享 `${conversationId}:replay` fallback。
- 无身份 v2 session 帧 fail-closed 为协议错误，不进 store，REST 首屏作为恢复路径。
- 增加 user/assistant/tool replay fixture，覆盖两种到达顺序的 ID 去重。

实现 commit：`cbd0b82` + `eb95afb`。

状态：**自动化已通过；真实 WS replay ID = REST ID、store 无重复待复跑。**

### 4.4 前端：取消码和取消终态

原问题：前端只识别 fixture 中的 `CANCELLED`，而后端正式契约与真实输出是 `GENERATION_CANCELLED`。当次联调因乐观取消的时序恰好使 UI 正确，但其他时序可能误标 `failed` 并留下失败横幅。

当前前端已修复：

- 正式识别 `GENERATION_CANCELLED`，保留 legacy `CANCELLED` 兼容 helper。
- 取消终态统一为 `interrupted`，保留 partial segments，不设普通 `lastError`。
- 迟到 delta/completed/failed 不得复活已中断消息。
- fixture 已改成真实 `GENERATION_CANCELLED`。

实现 commit：`a68891a`。

状态：**自动化已通过；真实取消时序待复跑。**

---

## 5. 仍开放的风险、限制和文档债务

### 5.1 不阻断本次 B-01 happy path 的事项

| 项 | 当前口径 | 何时处理 |
|---|---|---|
| `BE-RISK-01` accepted 提交窗口 | accepted ACK 证明消息已进 EventBus，但在 loop `register()` 前强杀仍缺 outbox/启动重入队的完整恢复证据 | 不阻断单机 happy-path 联调；生产可靠性封口时独立设计 |
| 单活动桌面客户端/单写者 | 同一会话多客户端同时写入不在当前产品范围，不保证跨客户端因果顺序 | 需要多客户端时，后端应增加 user committed/引用/sequence 契约 |
| 工具结果正式 UI | 当前 tool role 可能显示成空 assistant 风格行，工具卡片留到阶段 3 | 阶段 3 |
| raw schema 指纹不下探数组元素 | 嵌套 item requiredness 变更可能不改 manifest schemaHash；字段级 63 帧深比较提供另一层门禁 | 后续改造指纹机制时 |
| `state/typing` | 已建模为“已知但当前有意忽略”的兼容帧，未知帧仍 fail-closed | 产品需要状态 UI 时再映射 |

### 5.2 迁移时必须知道的跨仓脚本问题

前端文件：

```text
apps/desktop/scripts/check-backend-contract.mjs
```

当前硬编码了：

```text
/home/yomu/elysia-agent/frontend_contract
/home/yomu/elysia-agent/docs/archive/frontend-handoff-v2/traces
```

如果新机用户名不是 `yomu` 或仓库不在此路径，脚本在找不到后端时会输出：

```text
[check-contract] 后端仓库未挂载,跳过
```

并以 exit 0 结束，因此容易形成**假绿**。

迁移立即规则：

- 如果新机仍是 `/home/yomu/...`，保持两仓路径不变，并确认契约检查实际输出 `13/13` 和字段级帧数，不能只看 exit code。
- 如果用户名/路径改变，应优先修复脚本：支持如 `CYRENE_BACKEND_REPO` 的显式路径覆盖，并以前端仓库的 sibling 路径作为 fallback。
- 未修复前，不得把“后端仓库未挂载,跳过”当成契约门禁通过。

### 5.3 已知文档漂移

- 前端 README 仍有“后端配套清单落地前”的过渡期文案；实际后端修复已在 `73ac7a3`落地，现在只差真实复跑。
- 后端首次联调 runbook 的 SHA/schemaHash 冻结表未回填当前值。
- 这些是文档口径问题，不代表代码修复未落地；B-01 通过后应统一更新。

---

## 6. 迁移前的代码保全

### 6.1 推荐方式：提交并推送后再 clone

旧机先确认差异：

```bash
cd /home/yomu/elysia-agent
git status -sb

cd /home/yomu/elysia-frontend
git status -sb
git log --oneline origin/main..HEAD
```

然后由开发者完成：

1. 把本交接文档 commit 到后端 `main`并 push。
2. 把前端本地 commit `05f5af0` push 到 `origin/main`。
3. 再在新机 clone 两仓，核对 SHA。

> 本文的创建动作本身不会自动 commit/push。

### 6.2 不想 push 时：Git bundle 或带 `.git` 的 rsync

可以在旧机创建 bundle：

```bash
cd /home/yomu/elysia-agent
git bundle create /tmp/elysia-agent-main.bundle main

cd /home/yomu/elysia-frontend
git bundle create /tmp/elysia-frontend-main.bundle main
```

然后将 bundle 和**尚未提交的本交接文档**一起通过 SSH 传输。bundle 只包含 Git commit，不包含 untracked 文件、`.env`、模型、Docker volume 或 Electron safeStorage。

另一种方式是直接 rsync 整个仓库并保留 `.git`，但应排除 `node_modules/out/dist/log` 等可重建产物，`.env` 和 `.model_cache` 按第 7 节单独处理。

---

## 7. 非 Git 资产：凭据、模型、数据和 Electron 本机状态

### 7.1 `.env` 和凭据

旧机后端 `.env` 当前已设置的键名如下（本文从未读取/记录值）：

```text
AGENT_ACCESS_TOKEN
ALLOW_INSECURE_REMOTE
DEEPSEEK_API_KEY
DEEPSEEK_URL
GPT_SOVITS_URL
MILVUS_URI
MINIMAX_API_KEY
MINIMAX_TTS_URL
MONGODB_URI
```

当前文件未包含新 compose 必需的服务端凭据：

```text
MONGO_INITDB_ROOT_USERNAME
MONGO_INITDB_ROOT_PASSWORD
MINIO_ROOT_USER
MINIO_ROOT_PASSWORD
MILVUS_ROOT_PASSWORD
MILVUS_TOKEN
```

新机推荐不盲目覆盖整个 `.env`，而是：

1. 在新 clone 中运行 `bash scripts/setup.sh`，让脚本从 `.env.example` 创建新文件并生成 Mongo/MinIO/Milvus 随机凭据及对应 URI/token。
2. 再通过 SSH/可信方式把 API key、提供商 URL 和 `AGENT_ACCESS_TOKEN` 的值填入新 `.env`。
3. 使用 `chmod 600 .env`。
4. 逐项检查 `MONGODB_URI` 与新 Mongo 凭据一致，`MILVUS_TOKEN=root:<MILVUS_ROOT_PASSWORD>` 与新 Milvus 一致。复制旧 URI 可能会让新认证容器启动正常但应用连接失败。
5. 采用 SSH 隧道时保持后端默认 `SERVER_HOST=127.0.0.1`；不需要 `SERVER_HOST=0.0.0.0`，也不需要依赖 `ALLOW_INSECURE_REMOTE=1`。建议从新环境删除/禁用该不安全开关，仅在明确的受控开发场景再开。

### 7.2 嵌入模型缓存

旧机实测：

```text
.model_cache  2.5G
data          16K
log           424K
static        6.7M
```

可选择：

- 通过 `rsync` 复制 `.model_cache/`，节省重新下载时间；到新机后仍运行 `python scripts/download_model.py`，让 manifest/revision/hash 校验确认缓存完整。
- 不复制，让 `scripts/setup.sh` 从固定 revision 重新下载和验证。

`log/`不是启动必需项；如果需要保留联调证据，只复制相关时间段的日志并注意脱敏。

### 7.3 旧机 Docker 数据现状与版本差异

旧机目前运行的是较旧容器组合：

| 容器 | 旧机实际 image | 旧机实际端口 |
|---|---|---|
| `elysia-mongo` | `mongo:4.4` | `0.0.0.0:27017` |
| `elysia-milvus` | `milvusdb/milvus:v2.4.0` | `0.0.0.0:19530/9091` |
| `elysia-etcd` | `quay.io/coreos/etcd:v3.5.5` | 容器内 |
| `elysia-minio` | `minio/minio:RELEASE.2023-03-20T20-16-18Z` | `0.0.0.0:9000/9001` |

旧机具名 volume 大小（仅用于备份核对）：

```text
elysia-agent-main_mongo_data   约 383.5 MB
elysia-agent-main_milvus_data  约 261.8 MB
elysia-agent-main_etcd_data    约 128.4 MB
elysia-agent-main_minio_data   约 44 KB
```

当前仓库 `docker-compose.yml` 已改为：

- MongoDB 8；
- Milvus 2.6.22；
- etcd 3.5.14；
- 数据服务端口只绑定 `127.0.0.1`；
- Mongo/Milvus/MinIO 强制随机凭据与认证。

**禁止把 Mongo 4.4 / Milvus 2.4 的原始 volume 直接挂载给 Mongo 8 / Milvus 2.6。** 原始数据目录跨主版本复制可能无法启动或静默损坏；应选择空库或逻辑导出/导入。

### 7.4 数据迁移决策

#### 方案 A：新建空数据库（最快）

适合只需继续功能开发和新会话联调。上次真实联调观察到 `long_term_count=0`，因此 B-01 文本/工具/取消主链不依赖旧 Milvus 业务数据。

代价：

- 旧 `default` 会话和 `conv_036c872c7c55` 不存在。
- B-01 中“旧 `default` 可打开”只能改成新空库 bootstrap 验证，不能等价宣称历史兼容真实回归通过。

#### 方案 B：保留 Mongo 逻辑数据（推荐用于完整 B-01）

1. 在旧机使用 `mongodump --archive --gzip` 对当前 Mongo 做**逻辑备份**。根据旧容器的实际认证状态选择匿名或认证连接，不要把 URI/密码直接留在 shell history 或文档中。
2. 记录 archive 文件大小与 SHA-256，通过 SSH 传输到新机。
3. 在新 Mongo 8 空库启动且新凭据已确认后，使用 `mongorestore --archive --gzip` 逻辑恢复。
4. 恢复后至少核对：会话数、`default` 最近 50 条、`conv_036c872c7c55`、tool `msg_7c0c...`、空/重复 `message_id` 数。

Milvus 如确实需保留，应用与 2.4 兼容的官方 Milvus Backup/导出路径，再按支持的升级流程导入；不要 tar 原始 volume 后直接挂载。当前 B-01 推荐 Mongo 逻辑迁移 + Milvus/etcd/MinIO 新建。

### 7.5 Electron safeStorage 和本机状态

- Electron 的 Endpoint/token 保存在操作系统本机状态中，token 由 Electron `safeStorage` 加密，不在 Git。
- 不要把 `~/.config/Cyrene/config/credentials.json` 从一台机器/操作系统盲目复制到另一台；新机很可能无法解密。
- 如果 Electron 仍运行在原 Windows 机，Windows 的 safeStorage 不因 Ubuntu 后端迁移而消失；但 Endpoint origin 改变会按设计清理旧 token 绑定，应在 UI 重新确认/设置。
- 如果在新 Ubuntu 上运行 Electron，必须在新系统上重新输入 Endpoint 和 token。

---

## 8. 新 Ubuntu 22.04 主机环境恢复

### 8.1 基础检查

```bash
uname -a
lscpu | rg -i 'model name|avx|avx2'
free -h
docker --version
docker compose version
conda --version
codex --version
```

E5-2690 v3 预期具备运行 MongoDB 8 需要的 AVX，但仍应以 `lscpu` 实际输出为准。如果没有 `rg`，先安装 ripgrep 或用其他只读查询命令。

必需基础软件：

- Git；
- Docker Engine + Docker Compose plugin；
- Miniconda/Conda（后端推荐）；
- Node.js 22.23.0 + pnpm 11.20.0（Electron 前端）；
- Ubuntu 桌面或 Xvfb（Electron 开发运行/集成与产物 smoke）。

### 8.2 建议目录布局

如果新机用户仍是 `yomu`，保持：

```text
/home/yomu/elysia-agent
/home/yomu/elysia-frontend
```

这样现有跨仓契约脚本不会因硬编码路径跳过。如果用户名不同，使用两个 sibling 仓库，并先按第 5.2 节修正契约脚本。

### 8.3 后端安装

```bash
cd /home/yomu/elysia-agent
bash scripts/setup.sh
```

`setup.sh` 默认为 backend-only，不构建旧 v1 Vue 界面。检测到 conda 时会创建 `Elysia` Python 3.12 环境，安装 runtime/dev 依赖，生成数据库凭据，校验/下载嵌入模型并跑预检。

首次运行时，如 API key 尚未填入或数据库尚未启动，脚本末尾的 `app.py --check` 可能以非零结束；先确认前面的环境、`.env` 与模型步骤已完成，填好凭据并启动 compose 后再跑 `bash scripts/check.sh --full`。不要用 `--skip-preflight` 掩盖真实配置错误。

如果要严格复现仓库锁定环境，也可以先按 README 使用 `environment.lock.yml`创建 conda 环境；但不要同时在未知状态的系统 Python 中混装依赖。

设置/合并 `.env` 后：

```bash
cd /home/yomu/elysia-agent
docker compose --profile dev config >/dev/null
docker compose --profile dev up -d
docker compose --profile dev ps
bash scripts/check.sh --full
```

### 8.4 Electron 前端安装

版本必须精确对齐：

```text
Node.js 22.23.0
pnpm 11.20.0
```

使用 fnm 的示例：

```bash
cd /home/yomu/elysia-frontend
fnm install 22.23.0
fnm use 22.23.0
corepack enable
corepack prepare pnpm@11.20.0 --activate
node --version
pnpm --version
pnpm install --frozen-lockfile
```

如果 Electron 包已安装但二进制未下载，会出现 `Electron uninstall` / `Electron failed to install correctly` / `fetch failed`。这是 Electron 二进制下载网络/代理问题，不是业务源码故障；配好可用网络/镜像后重跑：

```bash
pnpm --filter @cyrene/desktop ensure:electron
```

然后运行：

```bash
pnpm typecheck
pnpm lint
pnpm test
```

Linux Electron 缺系统库时，根据启动报错安装 Chromium/Electron 必需库；前端 README 记录了 `libnss3` / `libatk` / `libgtk-3` / `libasound` 等依赖。无图形会话的自动化用 Xvfb，但真实 UI 联调仍应在真实桌面或 Windows Electron 上完成。

---

## 9. 新 IP 下的推荐联调拓扑

### 9.1 方案 A：后端和 Electron 都在新 Ubuntu（快速调试）

- 后端绑定：`127.0.0.1:8765`
- Electron Endpoint：`http://127.0.0.1:8765`
- 如后端 `.env` 配了 `AGENT_ACCESS_TOKEN`，在 Electron 连接面板输入相同 token。

优点是无网络转发变量、排障最快；缺点是不能替代真 Windows 文件、safeStorage 与桌面运行时验收。

### 9.2 方案 B：新 Ubuntu 后端 + Windows Electron（B-01 推荐）

新 Ubuntu 上保持后端回环绑定。Windows PowerShell 开一个持续 SSH 窗口：

```powershell
ssh -N -L 18765:127.0.0.1:8765 <ubuntu-user>@192.168.1.18
```

Windows Electron 连接面板：

```text
Endpoint = http://127.0.0.1:18765
Token    = <新后端 AGENT_ACCESS_TOKEN>
```

这个方案的好处：

- Electron 仍看到回环 HTTP，符合前端 Endpoint 安全策略。
- 后端 8765 不暴露给局域网。
- 无需将 `SERVER_HOST` 改为 `0.0.0.0`。
- 新 IP 变化只修改 SSH 目标，Electron Endpoint 仍可保持 `127.0.0.1:18765`。

前置：新 Ubuntu 已启用 SSH server，局域网防火墙只需允许 Windows 访问 22 端口，无需开放 8765/27017/19530。

### 9.3 不推荐：Windows 直连 `http://192.168.1.18:8765`

前端只允许：

- 任意主机的 `https:`；或
- 回环主机的 `http:`。

因此非回环明文 `http://192.168.1.18:8765` 会被 Electron Main 拒绝。`ALLOW_INSECURE_REMOTE=1` 只影响后端的非回环启动守卫，**不会绕过前端 Endpoint 策略**，也不应当作产品安全方案。

如必须直连，需要正式 HTTPS/WSS 反向代理、可信证书、`AGENT_ACCESS_TOKEN` 和后端受控绑定；这不是首次联调的最短路径。

### 9.4 可选 CDP 反向隧道（仅临时自动化）

如果 Windows 开发 Electron 已经在本机回环 `9223` 启用 CDP，可将前述 SSH 命令扩展为：

```powershell
ssh -N -L 18765:127.0.0.1:8765 -R 19223:127.0.0.1:9223 <ubuntu-user>@192.168.1.18
```

新 Ubuntu/Codex 上检查：

```bash
curl http://127.0.0.1:19223/json/version
```

必须满足：

- Windows CDP 只绑定 `127.0.0.1`。
- 仅在受控联调时开启，用完关闭。
- 不将固定远程调试端口写入打包/产品启动配置。
- 现有 `pnpm dev -- ...` 的多层参数转发曾丢失 `--remote-debugging-port`；不要把端口未监听误判为业务故障。

---

## 10. 启动顺序与最小健康检查

### 10.1 启动后端

```bash
cd /home/yomu/elysia-agent
docker compose --profile dev up -d
bash scripts/start.sh
```

`scripts/start.sh` 会优先使用 conda `Elysia`，检查/启动数据库，跑预检后启动 `127.0.0.1:8765`。不应把 `--skip-preflight` 作为正常启动方式。

另开终端检查：

```bash
curl -sS http://127.0.0.1:8765/health
curl -sS http://127.0.0.1:8765/ready
```

预期：

- `/health` HTTP 200。
- `/ready` HTTP 200，payload 中 `chatReady=true`；要做完整文本/记忆联调时同时确认 `fullReady=true`。
- `/` 可能是 404，因为 v1 聊天页默认关闭，这不代表 Electron API/WS 不可用。

### 10.2 启动 Electron

真实模式默认启用：

```bash
cd /home/yomu/elysia-frontend
fnm use 22.23.0
pnpm dev
```

不要在真实联调时设置 `CYRENE_BACKEND=mock`。如 shell 残留过该变量，先清除或显式使用 real。

启动后在连接面板：

1. 设置 Endpoint。
2. 设置 Token（先有 Endpoint 才能设 token）。
3. 点击连接。
4. 确认 UI 显示 `REAL` / protocol `2.0` / 当前后端 build SHA。

> 只在普通 Chrome/浏览器打开 Renderer URL 不等价于 Electron 联调：普通浏览器没有 Preload `desktopApi`、Main 进程、safeStorage 与真实 BackendAdapter。B-01 必须在 Electron 中执行。

---

## 11. 迁移后的自动化基线

在做真实 UI 操作前，先确认新机没有安装/路径/依赖偏差。

### 11.1 后端

```bash
cd /home/yomu/elysia-agent
conda activate Elysia
make test-quick
make lint
make pyright
make check-types
make check-compose
```

关键期望：

- `test-quick` 不少于交接时记录的 3548 passed（如新 commit 增加测试，数量可增加）。
- Ruff 全绿。
- Pyright 0 errors / 0 warnings。
- `frontend_contract --check`、golden frames 和 41 个错误码一致。
- compose 仅 loopback 端口，无默认/空凭据。

### 11.2 前端

```bash
cd /home/yomu/elysia-frontend
fnm use 22.23.0
pnpm install --frozen-lockfile
pnpm typecheck
pnpm lint
pnpm test
pnpm test:integration
pnpm build
pnpm --filter @cyrene/desktop check:contract
pnpm package
pnpm verify:artifact -- --smoke
```

无桌面会话时，对需要 Electron 的命令用 `xvfb-run -a` 包装。契约检查必须看到类似：

```text
[check-contract] WS v2 type 覆盖: 13/13 通过;字段级帧 63 条完整保留;...
```

如果只看到“后端仓库未挂载,跳过”，该门禁不算通过。

---

## 12. 迁移后的第一目标：B-01 十项真实复跑

复跑时必须记录实际 SHA，不要仅写 `main`。如果中途修了代码，从修复后新 SHA 重新开始受影响的项。

| # | 验收项 | 必留证据 | 当前状态 |
|---:|---|---|---|
| 1 | 冻结更新后的前后端 SHA | `git rev-parse HEAD`、后端 `server_info/build_sha`、schemaHash | **通过**：BE `73ac7a3`、FE `6b27e2e`、schema `e5d4dd2b9296b727` |
| 2 | 启动 Electron 并连接 | UI/RuntimeInfo 显示 `REAL` / protocol 2.0 / 正确后端版本 | **通过**：Windows Electron 43.3.0，`REAL/2.0/0.1.0+73ac7a3` |
| 3 | 创建会话并完成一条纯文本 | conversationId、clientRequestId、accepted messageId、generationId、completed | **通过**：ACK/流式/completed/UI/REST 身份一致 |
| 4 | 连续执行两次真实工具 | 两个不同 callId、两个非空不同 messageId、REST timestamp | **通过**：两组 call/message/timestamp 非空且不同 |
| 5 | 只 reload Renderer | 无手工 disconnect/connect 即自动恢复 connected、活动会话和历史；Main connectionId 不变 | **通过**：静默 76s reload 后 `conn_240807de609a` 不变，REST 13=DOM 13 |
| 6 | 断开再重连 | 抓 WS session replay，对每个 tool 比较 replay/REST `message_id/timestamp/tool_call_id` 全部一致 | **通过**：两条 tool 三字段全部一致 |
| 7 | 检查 Pinia/store/DOM 去重 | 每个 callId 只对应一条 Message，没有 `<conversationId>:replay`，没有额外空工具行 | **通过**：无 fallback/重复，最终 Pinia 19=DOM 19，`droppedEvents=0` |
| 8 | 取消一次真实长生成 | 原始 `GENERATION_CANCELLED`、UI `interrupted`、partial 保留、无失败横幅、迟到 completed 不复活 | **通过**：修复后保留 62 字 partial，无迟到 completed |
| 9 | 打开旧 `default` 会话 | 若迁移 Mongo：50 条历史可读、ID 非空无重复；若空库：明确标“未执行存量数据回归” | **通过**：存量 50 条可读，ID/时间完整无重复，`hasMore=true` |
| 10 | 检查诊断 | 本轮没有 schema/protocol/RAW_FRAME_INVALID/未处理 Promise 新错误；`state/typing` 不误报 | **通过**：修复后 0 warning/error、0 断线、0 RSV，console/page error=0 |

### 12.1 通过口径

本轮十项已全部取得真实证据，因此现在可以：

- 将 B-01 从“已开发/已验证、未联调”改为“已真实联调”。
- 回填前后端报告、README 和 runbook 的实际 SHA/schemaHash/环境。
- 开始把阶段 3 作为主线（完整聊天/历史/工具卡片/多媒体等）。

某一项失败时，应分别在两仓 `docs/review/` 写简洁的真实证据，按所有权分到前端、后端或跨层问题；不要为了让 UI 继续运行而放宽消息 ID/协议 schema。

---

## 13. 新 Codex CLI 会话的建议首条提示

> **2026-08-14 更新：** 本节原提示所述的 B-01 任务已完成，仅作历史交接记录，不应再用它启动一次重复复跑。新任务应从阶段 3 计划与当日第三次联调结果报告开始。

在新机的后端仓库（或能同时读取两个 sibling 仓库的工作区）启动 Codex CLI，然后粘贴：

```text
你现在接手 Elysia Agent 后端和 Cyrene Electron 前端的真实联调。

后端仓库：/home/yomu/elysia-agent
前端仓库：/home/yomu/elysia-frontend
新 Ubuntu 主机 IP：192.168.1.18

请先完整阅读：
1. /home/yomu/elysia-agent/docs/review/2026-08-14-开发环境迁移与真实联调上下文交接.md
2. /home/yomu/elysia-agent/docs/review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md
3. /home/yomu/elysia-frontend/docs/superpowers/2026-08-13-realint2-fix-final-report.md
4. 两仓 README 和后端 docs/electron-v2-first-integration-runbook.md

先核对两仓 git status、HEAD、origin 差异和 schemaHash，确认迁移没有丢 commit/文档。不要输出 .env、token 或 API key 的值。

当前准确状态是：第二轮联调发现的问题已在代码中修复，自动化门禁已绿，但更新后的前后端尚未做 B-01 十项真实 Electron 复跑。不要宣称 B-01 已通过。

你的第一目标是：按交接文档第 11 节先恢复自动化基线，再按第 12 节执行 B-01 十项真实复跑，记录双方实际 SHA、REST/WS ID、callId、generationId、Renderer reload、取消码和诊断证据。如果失败，先证据化定位属于前端、后端或跨层，再决定修复。
```

如果新机用户名不是 `yomu`，先替换提示中的路径，并处理第 5.2 节的契约脚本硬编码。

---

## 14. 推荐阅读顺序

### 14.1 后端

1. [`README.md`](../../README.md)
2. 本交接文档
3. [`electron-v2-int2-replay-identity-fix-report-2026-08-13.md`](electron-v2-int2-replay-identity-fix-report-2026-08-13.md)
4. [`2026-08-13-Windows-Electron-二次真实联调后端整改清单.md`](2026-08-13-Windows-Electron-二次真实联调后端整改清单.md)
5. [`electron-v2-message-id-fix-report-2026-08-13.md`](electron-v2-message-id-fix-report-2026-08-13.md)
6. [`2026-08-13-Windows-Electron-首次真实联调后端问题清单.md`](2026-08-13-Windows-Electron-首次真实联调后端问题清单.md)
7. [`../electron-v2-first-integration-runbook.md`](../electron-v2-first-integration-runbook.md)
8. [`../electron-backend-protocol-v2.md`](../electron-backend-protocol-v2.md)
9. `todo.md` 中 Electron v2 联调段落

### 14.2 前端

1. `/home/yomu/elysia-frontend/README.md`
2. `/home/yomu/elysia-frontend/docs/00-Cyrene-前端文档索引与阅读顺序.md`
3. `/home/yomu/elysia-frontend/docs/superpowers/2026-08-13-realint2-fix-final-report.md`
4. `/home/yomu/elysia-frontend/docs/review/2026-08-13-Windows-Electron-二次真实联调前端整改清单.md`
5. `/home/yomu/elysia-frontend/docs/review/2026-08-13-Windows-Electron-首次真实联调前端问题清单.md`
6. `/home/yomu/elysia-frontend/docs/02-Cyrene-前后端通信协议与前端数据结构-v0.2.md`
7. `/home/yomu/elysia-frontend/docs/16-Cyrene-前端实施任务清单与进度跟踪.md`
8. `/home/yomu/elysia-frontend/docs/17-Cyrene-前端开发环境与依赖安装记录.md`

阅读旧“问题清单”时，必须以同仓的最终修复报告和当前源码为准：问题清单保留的是当时证据，其“待修复”状态已被后续 commit 取代。

---

## 15. 迁移完成清单

### 代码

- [ ] 后端包含 `73ac7a3` 及本交接文档。
- [ ] 前端包含 `05f5af0`，不是只停在 `origin/main@e5142bf`。
- [ ] 两仓 `git status -sb` 没有不明的未提交改动。
- [ ] 跨仓 contract 检查不是“跳过”假绿。

### 凭据与环境

- [ ] 新 `.env` 权限 0600，没有进 Git。
- [ ] 新 Mongo/Milvus/MinIO 凭据由新环境生成，URI/token 一致。
- [ ] `AGENT_ACCESS_TOKEN` 已设置，但未出现在命令输出/文档中。
- [ ] 新机后端保持 loopback 绑定，Windows 通过 SSH 隧道联调。
- [ ] Node `22.23.0`、pnpm `11.20.0`、Electron 二进制可用。
- [ ] 嵌入模型通过 manifest/hash 校验。

### 数据

- [ ] 明确选择“空数据库”或“Mongo 逻辑迁移”，并记录该选择对 B-01 第 9 项的影响。
- [ ] 未将 Mongo 4.4/Milvus 2.4 原始 volume 直接挂载到新版。
- [ ] 如迁移 Mongo，已核对 `default`、`conv_036c872c7c55` 和 tool message ID。
- [ ] 旧机数据在新机验证完成前未删除，未执行 `docker compose down -v`。

### 验证

- [ ] 后端自动化基线已在新机复现。
- [ ] 前端自动化基线已在新机复现。
- [x] `/health` / `/ready` 口径正确，没有把 `/` 404 误判为后端故障。
- [x] B-01 十项真实复跑已按当前 SHA 执行并形成报告。

---

## 16. 最终交接口径

> **2026-08-14 最新结论：** 前后端的真实文本、工具、取消、Renderer reload 和重连/replay 主链已在 Windows Electron + Ubuntu 后端中按当前双仓 SHA 通过 B-01 十项复跑；空闲压缩断连也已修复并完成两轮 `76s` 复验。阶段 2 主链已真实联调，阶段 3 可转为主线；多客户端、故障恢复、签名发布等独立门禁仍按原计划保持开放。

> 下方“迁移前”段落是原始交接口径，已被上述最新结论取代，仅保留作为历史记录。

迁移前的项目不是“尚未能联调”，也不是“已经完全联调通过”。最准确的表述是：

> **前后端的真实文本、工具、取消和重连主链已在 Windows Electron + Ubuntu 后端中跑通；真实联调发现的 Renderer 水合、replay 身份与取消码缺陷已在当前代码中修复，自动化门禁全绿；但更新后的双方 SHA 还需在新环境完成 B-01 十项真实复跑，之后才能封口并转入阶段 3。**
