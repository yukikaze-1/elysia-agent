# Elysia Agent 交接文档

> **更新日期：** 2026-09-11
> **后端基线：** `d9ad8095`（`main`，工作树干净）
> **功能基线（Stage H 验收）：** `7d2e2b31`（运行版本 `0.1.0+7d2e2b3`）
> **前端基线（Stage H 验收）：** `d02ce15`（`elysia-frontend` 当前已推进到 `5b0263c`）
> **读者：** 接手本项目的人（或几个月后的你自己）。本文只写**当前事实**；历史过程见文末索引。

---

## 1. 项目与仓库结构

**Elysia Agent** —— Python 3.11+ AI 伴侣应用。三个顶层包：

| 包 | 职责 | 规模 |
|---|---|---|
| `Module/` | 后端无关的基础设施库（LLM/STT/TTS/embedding/向量库/文档库/输出通道/配置/日志/Prompt 引擎/工具框架） | 142 源文件 |
| `core/` | Elysia 应用本体（`Agent` / `CognitiveLoop` / `CharacterEngine` / 记忆三件套 / 会话管理 / 存储仓储 / input 服务层） | 193 源 + 177 测试 |
| `frontend_contract/` | 前后端契约**唯一类型来源**（Pydantic 模型 → TS/JSON Schema/raw TS 发射器 + 漂移门禁） | 10 源文件 |

另有**跨仓前端** `elysia-frontend`（Vue 3 + Electron），与本仓通过 `frontend_contract/` 生成的产物对接。

**运行前提**：`conda activate Elysia`；MongoDB（`:27017`，库名 `default`）与 Milvus（`:19530`）本地在跑。敏感信息一律走环境变量（`.env`）。

---

## 2. 当前阶段

**主线状态：多角色（Character）Stage A–H 已闭账，Electron v2 后端契约已封口到 Stage H。**

已完成并可视为稳定基线的能力块：

| 能力块 | 状态 | 备注 |
|---|---|---|
| 多角色 / 自定义角色 / 用户身份 | ✅ 已实施 + 真实 Windows Electron 复验通过（2026-09-10 闭账） | `core/character/`（16 文件）+ `core/input/character_routes.py`（14 路由）；契约 `character-contract-v1` |
| 记忆作用域隔离（Stage F） | ✅ 已实施 | `core/memory/scope.py` + `scoped_long_term.py`；Milvus 集合 `long_term_memory_v2` |
| Electron v2 聊天契约（批次 2–9D） | ✅ 封口 | WS v2 结构化生成 + 多会话/历史/搜索/资源 REST + Desktop 工具 + Profile/TTS + 三语音模式 + 附件媒体 |
| 工具双轨展示（阶段 6） | ✅ 后端落地 + 真实联调 | Desktop 走 `tool.request/status`；Backend v2 发 `tool.started/completed` |
| 前后端契约单源 + 漂移门禁 | ✅ | `make check-types` 覆盖 TS / JSON Schema / raw TS / golden frames / 错误码 |
| 测试分层与门禁 | ✅（本会话大幅加固） | 见 §3.3、§6 |

**Stage H 之后的运行基线**：`character_profiles=true`、`character_state_stream=true` 为**恒真硬编码**能力开关（`core/agent/agent.py`），即多角色契约对 v2 客户端**默认生效**。

---

## 3. 最近完成的工作

### 3.1 多角色 Stage A–H（2026-09-03 ~ 09-10）

后端领域模型 **Definition → Revision → Instance** 三级 + per-instance 运行时。关键设计点：engine per-instance（`CharacterRuntime` 懒建，永不换绑 session，从根上消除跨会话污染）；`CharacterInstanceRepository` 持 `state` + `stateVersion` 乐观锁；操作 lease（`_op_guard`）保证「判定 + 登记」原子；custom CRUD 走 **Revision-first 写序**（先 append revision 再 CAS 指针）。

**明确的设计边界**：单用户（`owner_user_id` 字段保留但未做多用户隔离）；旧数据不迁移、用干净基线（开发阶段决策）。

### 3.2 Electron v2 后端契约

`docs/electron-backend-protocol-v2.md` 是**当前有效**的联调契约（v0.8，54 错误码），本次已补齐整个 Stage A–H 契约族（§14：角色/身份 REST 14 端点、wire 模型、`character.state.changed`、generation 身份三元组、`live2d.directive`、会话回放绑定快照）。

### 3.3 测试体系治理（本会话，2026-09-11）

这一块改动较大，**接手时务必知道**：

1. **真实数据库防线**（`conftest.py` autouse + `core/tests/_real_db_guard.py`）
   背景：`pytest.ini` 把 `service` 定义为「不连真实 DB」，但默认配置指向**开发者本机生产库**（`mongodb://localhost:27017` / `db_name=default`），且 `agent.start()` 对连不上库**静默降级**——所以「写生产库」与「没写」两种情况下测试都绿，问题从不自我暴露。实测污染包括：`sessions` 里 50 条测试会话、`default` 会话混入 123 条测试消息、**`test_ltm_clear_all` 会 drop 整个 `long_term_memory_v2` 集合**（差一点抹掉全部长期记忆）。
   现状：防线对**所有**测试生效（仅 `external` 放开），把生产 Mongo 连接导流到内存 fake、Milvus LTM 集合重定向到 `*__test`。诊断插件实测真实 `db=default` 构造 **108 → 0**。
2. **错误码门禁修补**：`check_error_codes.py` 原先只比对「文档表 ↔ `errors.py` 列表」，**看不见路由真正发射的码**——导致 §9.1 长期自称 41 码、实际可达 54。现已补登记 13 个 Stage A–H 码，并新增**发射端扫描**（未登记即 CI 红）。
3. **修掉一处假绿**：`_run_health_rounds` 全局 patch `asyncio.sleep`，被调度器 `_prune_loop`(10s) 抢走轮次预算，导致健康循环**循环体从未执行**、测试却通过。已抽 `Agent._db_health_round(prev)` 供测试直接驱动。
4. **清理冗余**：删 29 个冗余/空转集成测试（`Module/workflow_engine/tests/integration/` 仅保留真 LLM smoke `test_e2e_mvp.py`）；slow 层中 4 个「用假 LLM 却标 slow」的测试改判。

### 3.4 文档体系（本会话）

- **归档**：146 个文档（superpowers 111 / review 23 / progress 9 / 根级 2）按月份归入 `archive/<YYYY-MM>/`；同步修正 **326 处**引用。
- **时效性同步**：`architecture.md`（补 character 域/user identity/记忆作用域三节 + 修正一批计数漂移）、`protocol-v2.md`（补 §14）、`security.md`（补 §3.8 + 披露 `/live2d/` 匿名挂载）、`database-storage.md`（补登 `long_term_memory_v2`，集合 13→14）。

---

## 4. 已知问题与技术债

### 4.1 生效中的问题单（最高优先）

**`docs/review/2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md`（状态：生效中）**

- **现象**：`character_profiles=true` 时，前端对**会话列表**与**消息历史**实行整页/整批拒绝（一条不合格即拒绝全部）——单个坏记录 → 整个多角色功能面不可用。
- **根因（仍未修）**：后端**缺少「多角色数据完整性」自检**。`character_profiles` 是全局开关，前端以此为 fail-closed 前提，而后端没有任何环节检查该前提是否成立。历史数据、测试残留、异常中断产生的记录都会直接抵达前端。
- **已做的**：现场数据修复（补角色戳 + 删无绑定会话）已闭环；**代码层的自检/兜底尚未实现**。
- **前端侧不是缺陷**：拒绝整页是防「游标推进但静默丢历史」的有意设计（见该文 §8，含跨仓同步说明）。

### 4.2 已知 Bug（摘自 `todo.md`，长期未闭合）

| 优先级 | 问题 |
|---|---|
| 中 | LLM 有时不输出 `<mood>` 标签（deepseek-v4-flash 结构化输出不稳定，回退关键词） |
| 中 | INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡（时序竞争，需单独 mini-spec） |
| 中 | TTS 密集短句导致 `wait_all()` 阻塞 → FSM 卡在 SPEAKING（已从 120s 缩到 30s 缓解，**根因「wait_all 无超时」未修**） |
| 低 | 语音识别易被环境音触发（VAD aggressiveness） |
| 低 | 工具执行 60s 超时需构造慢工具场景手工验证 |
| 低 | `flush_after_response` 内外层超时层不匹配（内 5s/任务，外 10s 整体） |
| 低 | `tokenizer.py` NORMAL 态 `_pending`/`_held` 缓冲无上界 |
| 低 | `core/tests/test_phase2.py`/`test_phase3.py` 裸 `setdefault("DEEPSEEK_API_KEY","test")`（**22 处**）——进程 env 污染隐患，建议统一改 `monkeypatch` |

### 4.3 架构债

- **反向依赖 `Module → core`（分层违规，未修）**：`Module/bench/bench.py`、`Module/database/documentdb/__main__.py` 等仍 `import core.*`。已知、已记录，未处理。
- **`character-contract-v1` 是文档级标签、非代码工件**：契约实体是 `frontend_contract/rest.py` 的 Pydantic 模型，`frontend_contract/` 内无同名符号。见 §5。

---

## 5. 设计已写、代码未实施的规划稿

**归档或引用时注意**——这些文档描述的组件**在代码里不存在**（已逐一核实命中 0）：

| 文档 | 状态 | 缺失的关键组件 |
|---|---|---|
| `docs/2026-08-29-task-system-{overall,backend}-architecture-design.md` | **未实施规划稿**（文件头已加 ⚠️ 标注） | `TaskService` / `ToolExecutionCoordinator` / `ExecutionGateway` / `TaskCenter`；无 `/api/tasks` |
| `docs/2026-08-30-provider-model-runtime-backend-architecture-design.md` | **未实施规划稿** | `ProviderConnection` / `ModelProfile` 拆分、协议 Adapter Registry、`/api/runtime/status`、在线模型发现 |

> ⚠️ **task-system 两份是跨仓镜像**——同时存放于 `elysia-frontend`，文档明写「架构结论变更时必须同一次跨仓同步」。**单仓不得移动或归档**。

另有一处**声称交付但未落地**：`docs/2026-08-30-character-prompt-customization-...md` §11.0.1 列的 4 项跨仓机器契约产物中，第 1、2 项（Pydantic 源 + 生成产物）已交付，**第 3、4 项（character golden frames、handoff manifest）未交付**——文档内已加交付状态表说明。

---

## 6. 门禁与约定（不能违反）

### 6.1 命令

```bash
conda activate Elysia

make test-quick     # unit + service（秒级~分钟）
make test-normal    # 排除 slow / external
make test-full      # 全量
make test-external  # RUN_EXTERNAL_TESTS=1，真实 LLM/DB（不进合并门禁）
make lint           # ruff，禁相对导入（TID252）
make pyright        # 类型门禁（须 Elysia env——base env 缺 aiohttp 会漏报）
make check-types    # 契约漂移 + 错误码单源 + golden frames
```

当前收集 **4285** 测试（unit 2340 / service 1918 / integration 10 / slow 39 / external 7），合并门禁 4278。

### 6.2 硬约定

- **README 用中文**；代码注释/docstring 用中文（中英混排可接受）。
- **禁止相对导入**，一律 `from core.xxx import y` / `from Module.xxx import y`。
- **生产代码函数参数必须类型注解**（`make lint` 强制；测试豁免）。
- **测试必须带 marker**（`unit`/`service`/`integration`/`slow`/`external`），否则 `test-quick` 静默跳过、门禁不设防。
- **`service` 定义为「不连真实 DB」**，这条现在由 §3.3 的防线**强制执行**。需要真库的测试改标 `integration`（或 `slow`）。
- 新增 `core/tools` 插件必须 `plugin.py` + `config.yaml` + `README.md` 三元组。
- 契约变更后必须 `make gen-types`，`make check-types` 为漂移门禁。
- **`docs/archive/frontend-handoff-v2/traces/` 禁止移动**——`elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs:19` 硬编码了该绝对路径，移动即令前端契约校验静默失效。

### 6.3 CI

`ci.yml`（PR 门禁：lint + types + contract + quick）、`contract-frames.yml`（仅 traces/契约变更时触发）、`nightly.yml`（**仅手动 dispatch**，已移除定时）。

> `ci.yml` 的 `paths-ignore` 含 `docs/**` 与 `**.md`——**没有任何门禁会检查文档引用**。归档/移动文档时务必手工修正全仓引用。

---

## 7. 快速上手

```bash
conda activate Elysia
cp .env.example .env          # 填 API key / DB 连接
make start                    # = bash scripts/start.sh：起数据库 → 预检 → app.py（:8765）
```

`make start` 默认 backend-only（v1 聊天 UI 已停用，`SERVE_CHAT=1` 恢复；`/debug` 仪表盘可用）；加 `--with-frontend` 构建 v1 UI，加 `--skip-preflight` 显式降级跳过预检。

**先读**：`CLAUDE.md`（项目约定总入口）、`docs/architecture.md`（架构详解）、`docs/testing.md`（测试分层）。

**改代码前**：`docs/code-style-guide.md`（ID NewType 体系 + 类型权威归属 + StrEnum/coerce 纪律）。

---

## 8. 关键文档索引

| 文档 | 用途 |
|---|---|
| `CLAUDE.md` | 项目约定总入口（架构、模块索引、门禁） |
| `docs/architecture.md` | 完整架构详解（数据流 / 模块 / 目录树 / 启动停止顺序） |
| `docs/testing.md` | 测试分层 + 真实库防线说明 + 清理脚本 |
| `docs/electron-backend-protocol-v2.md` | **当前有效**的前后端联调契约（v0.8，54 错误码） |
| `docs/electron-backend-security.md` | 远程安全部署指南（认证/TLS/限流/SSRF/附件签名/探针） |
| `docs/database-storage.md` | MongoDB + Milvus 集合与索引（14 集合） |
| `docs/code-style-guide.md` | 代码风格与类型约束 |
| `docs/2026-08-30-character-prompt-customization-backend-architecture-design.md` | **已实施**的多角色权威设计（含 §11.0.1 交付状态） |
| `docs/review/2026-09-10-多角色Stage-H最终真实联调验收.md` | Stage H 闭账事实入口 |
| `docs/review/2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md` | **生效中**的问题单（§4.1） |
| `todo.md` | 待办 + 决策记录 + 已知 bug 全表 |
| `docs/README.md` | docs/ 目录索引与归档约定 |

**归档约定**：spec/plan 完成后移入 `docs/superpowers/{specs,plans}/archive/<YYYY-MM>/`；review 移入 `docs/review/archive/<YYYY-MM>/`；progress 移入 `docs/progress/archive/<YYYY-MM>/`。归档**必须同批修正全仓引用**（无门禁保护）。
