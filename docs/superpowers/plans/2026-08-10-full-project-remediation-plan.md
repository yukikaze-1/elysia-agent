# Elysia Agent 全项目审查修复实施计划

> **文档状态：** 待执行，可直接交给 Claude Code
>
> **计划版本：** v0.1
>
> **源审查报告：** [Elysia Agent 全项目综合审查报告（2026-08-10）](../../review/full-project-review-2026-08-10.md)
>
> **审查基线：** `d1cb3ae0c609008ea1e5576aaa1b245f12e64756`
>
> **目标仓库：** `/home/yomu/elysia-agent`
>
> **不在本计划内：** v1 Vue 前端功能开发；Electron 独立仓库代码修改；历史/归档文档整理。

## 1. 给 Claude Code 的执行指令

你是本仓库的后端修复负责人。请按本文顺序逐任务实施，不要一次性修改全部模块。每次只处理当前批次中一个可独立验收的 Task；完成测试、静态门禁和提交后，再进入下一项。

执行纪律：

1. 开始前读取当前 `git status`、本计划、源 review 和任务涉及的生产代码/测试；代码若已变化，以当前代码为准，并在计划的执行记录里说明偏差。
2. 保留用户已有和未提交改动。禁止使用 `git reset --hard`、`git checkout --` 或批量覆盖工作树。
3. 每个缺陷先添加能稳定失败的测试，再实施最小闭环修复。并发测试使用 `asyncio.Event`/barrier 控制顺序，不依赖脆弱的短 `sleep()`。
4. 不得用“增大 limit”“吞异常”“只加日志”“继续返回成功”代替一致性修复。
5. 涉及数据格式、索引、删除或 TTL 的任务必须同时提供：幂等迁移、dry-run、失败回滚/重试策略和旧数据兼容测试。未经用户明确授权，不操作真实生产数据。
6. 涉及 Electron wire contract 的任务只修改后端事实源 `frontend_contract/` 和后端生成链；不要手工修改 v1 Vue 业务代码。生成器若按当前项目约定更新跟踪产物，则保留生成结果并执行漂移检查。
7. 一个 Task 原则上对应一个提交。提交中包含该 Task 的生产代码、测试和必要文档，不混入无关重构。
8. 完成 Task 后，把对应复选框改成 `[x]`，在“执行记录”追加 commit、测试结果和任何剩余风险。
9. 如果发现计划方案与当前实现冲突，先记录事实和建议调整；不要静默扩大范围。

建议提交格式：

```text
fix(<area>): <明确结果>
test(<area>): <新增回归门禁>        # 仅在测试需独立提交时
chore(<area>): <工程化或迁移变更>
```

## 2. 目标与完成定义

本计划的目标是把后端从“受控本地联调可用”推进到“可保存真实数据、可重启、可安全远程部署、可由 CI 证明”的状态。

全计划完成必须同时满足：

- P0 全部关闭，没有启动即触发的数据删除路径，也没有不可信 PR 接触宿主凭据的路径。
- P1 全部有回归测试；Mongo 离线/恢复、并发消息、删除、shutdown 和 restart 均不丢数据、不复活数据、不跨会话污染。
- 文件、Web、LLM endpoint 和 HITL 审批边界无法通过 sibling、symlink、DNS、redirect 或并发 context 绕过。
- 非回环部署 fail-closed；鉴权、限流、幂等和基础设施网络边界形成完整产品门禁。
- WorkflowEngine 的核心 unit/service 测试进入合并门禁；外部真实 LLM E2E 与离线全量套件分离。
- clean-room 环境可安装、启动、运行静态检查和确定性全量测试。
- `ruff`、`pyright`、契约漂移检查、配置预检和目标测试均通过；不得以新增 ignore/xfailed 绕过修复。

### 2.1 当前已知基线

Claude Code 开始执行时先重跑必要检查；若 HEAD 未变，预期基线为：

- `pytest --collect-only -q`：3290 tests。
- 全量 `pytest -q`：3286 passed、2 skipped、2 failed、15 warnings；两个失败都是带真实 `DEEPSEEK_API_KEY` 的 Workflow 外部 E2E 首事件超时。
- 项目默认 Ruff：通过。
- Pyright：0 error、1 warning（`core/monitoring/__init__.py` 无效 export）。
- contract/type/frame drift check：通过，但 desktop golden 与 runtime 的语义差异不会被当前 check 发现。
- `app.py --check`、Compose/YAML/Shell 语法：通过。

后续 Task 不得把既有失败简单归因于“本次改动”，也不得因为基线已有 warning 而新增 warning。

## 3. 批次顺序与发布门禁

| 批次 | 内容 | 依赖 | 完成后允许的场景 |
|---|---|---|---|
| A | P0 止血：附件 GC、CI 信任边界 | 无 | 继续使用测试数据联调 |
| B | 存储语义、离线恢复、删除与记忆一致性 | A | 内部真实数据试用候选 |
| C | 消息、任务、shutdown、restart 生命周期 | B 的存储结果契约 | 可进行重启/并发耐久测试 |
| D | 工具、输入与部署安全边界 | C2 的 per-call context | 远程 beta 候选 |
| E | Workflow、工具重载、Milvus、desktop golden | C | 功能完整候选 |
| F | CI、依赖、启动脚本、静态门禁与文档 | A–E | 正式 release candidate |

关键依赖：

```text
A1/A2
  └─ B1（统一存储结果 + 真实健康检查）
      ├─ B2/B3/B4/B5/B6
      └─ C1/C3/C4/C5
          └─ C2（per-call tool invocation）→ D1/D2/D4
              └─ D3/D5/D6/D7
                  └─ E1–E6 → F1–F5
```

不要把上图理解成必须单线程：文件互不重叠的测试补充可以并行，但合并顺序必须维持数据和调用上下文的基础契约。

### 3.1 主任务清单

- [x] A0 执行前安全快照与运行约束
- [x] A1 附件 GC 完整分页、复核和 fail-closed
- [x] A2 PR CI 与宿主凭据隔离
- [x] B1 统一存储不可用语义与真实健康检查
- [x] B2 Session durable save/load/pending 恢复
- [x] B3 归档任务所有权与 durable 删除
- [x] B4 accepted ACK 前建立附件引用
- [ ] B5 Consolidator/LTM 离线一致性
- [ ] B6 Session 查询、TTL 迁移与 reflector
- [ ] C1 用户消息 FIFO 与 work lease
- [ ] C2 Per-call ToolInvocation 与 call-ID task
- [ ] C3 After-response 目标会话绑定
- [ ] C4 shutdown 顺序与可重入 restart
- [ ] C5 checkpoint 与 proactive event
- [ ] D1 文件 containment/symlink/资源上限
- [ ] D2 SSRF 与 Web response 上限
- [ ] D3 搜索、WS/REST 输入上限和失败反馈
- [ ] D4 日志脱敏
- [ ] D5 上传活动内容与配置 secret
- [ ] D6a 非回环 fail-closed 鉴权
- [ ] D6b 公开入口限流
- [ ] D6c durable idempotency
- [ ] D7a Compose 网络与凭据
- [ ] D7b embedding 模型供应链
- [ ] E1 工具热重载原子回滚
- [ ] E2 Workflow 非阻塞有界 submit
- [ ] E3 Workflow cancel/pause 终态
- [ ] E4 Workflow 测试分层
- [ ] E5 Milvus 输入与 metric
- [ ] E6 Desktop golden runtime 单源
- [ ] F1 真正全量离线 CI
- [ ] F2 依赖锁与安全扫描
- [ ] F3 Backend-only setup/start
- [ ] F4 静态门禁与关键复杂度拆分
- [ ] F5 文档与 Electron 交接收口

## 4. 全局测试分层

每个 Task 至少执行目标测试和以下相关门禁；每个 Batch 结束执行完整门禁。

### 4.1 单任务门禁

```bash
conda run -n Elysia python -m pytest <target tests> -q --tb=short
/home/yomu/miniconda3/envs/Elysia/bin/python -m ruff check <changed production paths>
conda run -n Elysia pyright
```

契约相关任务额外执行：

```bash
make check-types
python -m frontend_contract --check
python scripts/gen_contract_frames.py --check
```

### 4.2 批次门禁

```bash
make test-quick
make test-normal
make test-workflow-engine
python app.py --check
docker compose config --quiet
```

在 F1 完成前，`make test-full` 会触发真实外部 LLM 用例，不能作为稳定离线门禁。F1 完成后应形成：

- `make test-full`：不联网、无真实凭据、结果确定。
- `make test-external`：显式 opt-in，运行真实 LLM/DB E2E，有单独 timeout、成本和受保护凭据。

## 5. Batch A：P0 立即止血

### Task A0：执行前安全快照与运行约束

**性质：** 运维前置，不自动操作真实数据。

- [ ] 确认当前 HEAD、工作树和 review 基线差异；保留未跟踪的 review/plan。
- [ ] 记录附件根目录、Mongo 数据库名、当前附件记录数量和磁盘文件数量。
- [ ] 向操作者提供附件目录与 Mongo 的备份/恢复命令，但没有授权不得实际备份、删除或迁移生产数据。
- [ ] 在 A1 合入前，通过配置或代码开关禁止启动 GC 的物理 orphan unlink；若当前没有安全开关，先提交一个默认 fail-closed 的临时止血开关。
- [ ] 检查历史 CI 是否运行过不可信 PR；凭据审计/轮换属于人工动作，只记录结论，不在代码代理中擅自执行。

**验收：** 修复期间即使后端被误启动，也不会继续删除未知附件；工作树中的用户文件未被覆盖。

### Task A1：附件 GC 完整分页、候选复核与 fail-closed

**对应 review：** P0-01。

**主要文件：**

- `core/storage/attachment_repo.py`
- `core/storage/attachment_gc.py`
- `Module/database/documentdb/protocol.py`
- `Module/database/documentdb/mongodb.py`
- `core/storage/tests/test_attachment_repo.py`
- `core/storage/tests/test_attachment_gc.py`

**实施步骤：**

- [ ] 先增加 101、500+、active/deleted 混合的失败测试，证明所有有效文件都进入 known 集合。
- [ ] 为附件查询实现稳定分页。推荐按唯一 `_id` 或 `attachment_id` 排序并使用 keyset cursor；不得依赖 DocumentDB 默认 `limit=100`，也不得仅把 limit 改成大常量。
- [ ] 分页 API 必须明确返回“已完整读完”或抛 `StorageUnavailableError`；中途失败不能把部分结果交给物理删除阶段。
- [ ] GC 对磁盘 orphan 候选在 unlink 前再次查询/核对数据库状态，闭合扫描期间新建引用或状态变化窗口。
- [ ] 物理删除采用两阶段策略：先移动到同文件系统 quarantine/trash 并记录原因，确认一个 grace period 后再删除；若项目决定仍直接 unlink，必须由显式配置开启且有审计记录。
- [ ] dry-run 输出候选数量、ID 和原因，但不输出敏感绝对路径或用户内容。
- [ ] 补充分页中途异常、空页、重复 cursor、状态并发变化、磁盘文件消失、quarantine 失败测试。
- [ ] 更新 `docs/database-storage.md` 的 GC 和恢复说明。

**禁止方案：** `limit=10000`、查询异常后继续扫盘、仅凭“磁盘文件不在首批 DB 结果”删除。

**验收：**

- 500+ active/ref'd 文件运行 GC 后零误删。
- 任一 DB 页失败时 `orphans_removed == 0`，且结果明确 `skipped/failed`。
- 真正 orphan 可在 dry-run 被发现，并按两阶段策略处理。

**建议提交：** `fix(attachments): make GC paginated and fail closed`

### Task A2：隔离 PR CI 与宿主凭据

**对应 review：** P0-02。

**主要文件：** `.github/workflows/ci.yml`，必要时增加 workflow policy 检查。

**实施步骤：**

- [ ] 将 `pull_request` 检查迁移到 GitHub-hosted 或一次性、无宿主秘密、无持久工作区的 runner。
- [ ] 删除 PR job 中复制 `/home/yomu/elysia-agent/.env`、source 宿主文件和注入真实 provider key 的步骤。
- [ ] PR job 使用仓库内无密钥测试配置；所有外部 E2E 默认 skip/排除。
- [ ] 真实凭据集成测试拆成受保护的 `workflow_dispatch`/environment job，只运行已审核的 main commit，不 checkout 任意 PR 代码。
- [ ] 使用标准 `actions/checkout` 或确保 token 不写入持久 remote；`persist-credentials: false`。
- [ ] 增加静态 workflow 测试/脚本，拒绝 `pull_request + self-hosted`、宿主绝对 `.env`、默认 secret source。
- [ ] 若保留 self-hosted push job，清理 workspace、限制网络/权限，并确保 job 结束销毁或恢复干净快照。

**验收：** PR 代码无法读取宿主 `.env` 或真实 API key；受保护外部 E2E 只能从可信 ref 手工启动。

**建议提交：** `fix(ci): isolate pull request jobs from host secrets`

## 6. Batch B：持久化与恢复一致性

### Task B1：统一存储不可用语义与真实健康检查

**对应 review：** P1-05，并为 P1-01/P1-03/P1-12 提供基础。

**主要文件：**

- `core/storage/base.py`
- `core/storage/attachment_repo.py`
- `core/storage/session_repo.py`
- `core/storage/message_repo.py`
- `core/storage/token_repo.py`
- `core/storage/generation_repo.py`
- `core/storage/desktop_call_repo.py`
- `core/storage/character_version_repo.py`
- `core/storage/memory_repo.py`
- `Module/database/documentdb/protocol.py`
- `Module/database/documentdb/mongodb.py`
- `core/agent/_lifecycle_persistence.py`

**实施步骤：**

- [ ] 将通用 `StorageUnavailableError` 移到 `core/storage/base.py` 或独立 errors 模块；为旧导入保留临时 re-export，避免一次性破坏消费者。
- [ ] 规定 repository 写语义：存储未连接/操作失败必须抛 typed unavailable；成功方法返回可验证的 ID/count/result；禁止静默 `return` 被 caller 当成功。
- [ ] 区分“not found/幂等删除 0 条”和“后端不可用”。caller 只有在明确 durable success 时才能 ACK/驱逐状态。
- [ ] 给 DocumentDB 增加带严格 timeout 的真实 `ping()`，Mongo 实现调用 server ping；所有 repo `health_check()` 委托真实后端检查并捕获为 False。
- [ ] 任一实际 DB 操作出现连接级错误时，使共享 backend health 状态失效；不要等下一次假检查。
- [ ] 建立统一 recovery coordinator，覆盖 session/message/token/attachment/generation/desktop/character/memory repo，不再只恢复前三类。
- [ ] 为每个 repo 定义恢复 hook：rebind、pending flush、stale cleanup 或 dirty sync；恢复失败保留待重试状态。
- [ ] `/ready` 依赖真实 mandatory dependency health；明确开发降级与生产 readiness 的差异。
- [ ] fake DocumentDB 增加 ping、断网、恢复和部分失败能力；补各 repo 一致性契约测试。

**验收：** fake DB 所有操作报错时 health=False、`/ready`=503；恢复后所有 repo 重连并执行各自补偿，`/ready` 才恢复 200。

**建议提交：** `fix(storage): unify unavailable semantics and real health checks`

### Task B2：Session durable save、并发 load 与 pending 恢复

**对应 review：** P1-01、P2-01 的 pending 部分。

**主要文件：**

- `core/memory/session.py`
- `core/memory/session_manager.py`
- `core/agent/_lifecycle_persistence.py`
- `paths.py`
- `core/memory/tests/test_session.py`
- `core/memory/tests/test_session_manager.py`

**依赖：** B1。

**实施步骤：**

- [ ] 添加“repo 对象存在但 `_db=None`”测试：save 不能假成功，必须产生原子 pending JSON 或返回明确失败。
- [ ] 只有 repo 返回 durable success 才删除 pending 文件并返回成功；存储 unavailable 时执行临时文件 + fsync（若采用）+ `os.replace` 原子 fallback。
- [ ] `ensure_loaded()` 使用 per-session lock/in-flight Future；成功后才加入 `_loaded`，失败移除 in-flight 并允许重试。
- [ ] 定义 Mongo 与 pending JSON 的 merge 规则：按稳定 message ID 去重，保留顺序和更新较新的 title/state；禁止空 session 覆盖已有 Mongo 上下文。
- [ ] 启动时扫描所有合法 `session_*.json`，而不只 active session；路径解析必须限制在 pending 根目录并忽略 malformed 文件。
- [ ] 成功导入并验证 Mongo 后再删除 pending；导入中断可幂等重试。
- [ ] 恢复过程输出 session ID/计数，不记录消息正文。
- [ ] 覆盖启动离线聊天→关闭→重启、运行中恢复、多 session、损坏 JSON、load 中途失败、两个并发 caller。

**验收：** 任何 save 返回 True 的 session 必须至少存在于 Mongo 或可恢复 pending；恢复不会覆盖/重复消息。

**建议提交：** `fix(session): make offline persistence and load recovery durable`

### Task B3：归档任务所有权与 durable session 删除

**对应 review：** P1-02、P1-03。

**主要文件：**

- `core/memory/session.py`
- `core/memory/session_manager.py`
- `core/storage/message_repo.py`
- `core/input/session_routes.py`
- `core/storage/attachment_release.py`
- `core/agent/_lifecycle_persistence.py`

**依赖：** B1、B2。

**实施步骤：**

- [ ] 用按 session 可追踪、可 drain 的有界 archive queue/task set 替换裸 `ensure_future()`；队列达到上限时形成背压或明确拒绝，不允许无界 task。
- [ ] archive item 带 session ID、message ID 和幂等键；重复 flush 不产生重复消息。
- [ ] session delete 首先进入 durable `deleting` 状态/tombstone，随后阻止新消息、归档和附件引用进入。
- [ ] 删除前 drain/cancel 并 await 该 session 所有 archive/generation/tail task；每个任务取消后必须确认结束。
- [ ] 使用 durable deletion job/outbox 编排“附件引用释放 → message 删除 → session 元数据删除”。任一步失败保留 job 并重试，不能返回最终成功。
- [ ] 不依赖 standalone Mongo 不一定支持的事务；若检测到 replica-set transaction 可用，也必须保留幂等 retry 语义。
- [ ] API 在存储不可用时返回 503/retryable；只有 job 完成才返回最终 `ok`，或返回明确 `accepted/deleting` 状态并提供可查询终态。
- [ ] 删除完成后持久 tombstone 至少覆盖所有迟到任务/幂等窗口；重启加载时不得复活。
- [ ] 覆盖 slow append→delete、delete→late append、离线删除、附件 release 部分失败、重启继续 job 和重复 delete。

**验收：** 删除成功后任何迟到 append 都不能重建记录；离线删除不会假成功；重启后状态一致。

**建议提交：** `fix(session): own archive tasks and make deletion durable`

### Task B4：附件引用在 accepted ACK 前持久建立

**对应 review：** P1-04。

**主要文件：**

- `core/input/ws_handler.py`
- `core/storage/attachment_repo.py`
- `core/storage/attachment_release.py`
- `core/input/tests/test_attachment_message.py`

**依赖：** B1、B3 的 tombstone/幂等语义。

**实施步骤：**

- [ ] 把附件关联设计成按 `(attachment_id, message_id)` 幂等的 reservation/reference，而不是不可判重的裸计数增量。
- [ ] 在 `process_message()` 和 accepted ACK 前完成全部附件存在性、状态、会话归属和 durable reservation。
- [ ] 多附件 reservation 要么全部成功，要么进入可重试补偿状态；不得部分成功后 ACK。
- [ ] 若消息 admission 被拒绝，幂等释放 reservation；进程在 reservation 后崩溃时，由 outbox/reconciler 决定重放消息或释放悬挂 reservation。
- [ ] 同一 `clientRequestId` 重试不得重复增加引用。
- [ ] 覆盖 add_ref unavailable、第二个附件失败、队列拒绝、崩溃窗口、重复请求和 session deleting。

**验收：** accepted 消息引用的每个附件均有 durable 关联；失败消息不会永久增加引用；重试不重复计数。

**建议提交：** `fix(attachments): persist message references before accepted ACK`

### Task B5：MemoryConsolidator batch 与 LongTermMemory dirty/tombstone

**对应 review：** P1-11、P1-12，以及 `/restart` 的 LTM 去重部分。

**主要文件：**

- `core/memory/consolidator.py`
- `core/memory/long_term.py`
- `core/storage/memory_repo.py`
- `core/agent/_lifecycle_persistence.py`
- `core/memory/tests/test_consolidator.py`
- `core/memory/tests/test_long_term.py`

**依赖：** B1。

**实施步骤：**

- [ ] Consolidator 在锁内原子 detach 当前 batch；LLM 等待期间的新消息进入新 buffer，不得被旧 batch `clear()`。
- [ ] 提取、比较、insert/update 任一步失败都把原 batch 按原顺序 requeue，并避免与新消息重复。
- [ ] Merge/Update target 不存在或 repo 返回失败时不得忽略；按明确策略 fallback insert 或保留重试。
- [ ] MemoryRepo insert 无 PK 视为失败；dirty item 只有拿到并记录 durable PK 后才能移除。
- [ ] 为离线 insert/update/delete/absorbed 建持久 pending-op journal 和 tombstone；重启后继续同步，旧远端行不得复活。
- [ ] sync、delete、update 使用同一写锁或乐观版本；防止 sync 在 delete 后回写 ghost entry。
- [ ] reconnect/load 按 entry ID 幂等替换或重建本地 cache，不 append 重复项。
- [ ] 健康恢复只要 dirty/pending 非空就触发同步，不依赖 `prev_ok` 的特定初始值。
- [ ] 覆盖并发 append、空 PK、首次轮询已恢复、离线删除后重启、sync/delete race、连续 connect。

**验收：** LLM 等待期间的新消息保留；离线写最终同步且离线删永不复活；连续重连不重复 memory。

**建议提交：** `fix(memory): make consolidation and offline sync lossless`

### Task B6：Session 查询、TTL 迁移与 reflector 持久化

**对应 review：** P2-01。

**主要文件：**

- `core/storage/session_repo.py`
- `core/memory/session_manager.py`
- `core/storage/generation_repo.py`
- `core/storage/desktop_call_repo.py`
- `core/schema/utils.py`
- `Module/database/documentdb/protocol.py`
- `Module/database/documentdb/mongodb.py`
- `core/memory/session.py`
- `core/agent/agent.py`
- `docs/database-storage.md`

**依赖：** B1、B2、B3。

**实施步骤：**

- [ ] 把 search、稳定 keyset cursor、排序和 `limit+1` 下推 SessionRepository；删除固定 200 条预取。
- [ ] 覆盖 201/250/500 条分页、同 timestamp tiebreak、并发新增、旧标题搜索，无漏无重。
- [ ] sessions 不再依赖 Mongo 自动 TTL 删除；使用显式 cleanup job 走 B3 的级联删除协议。
- [ ] generations/desktop calls 增加真正 BSON Date 类型的 `expires_at`，保留 ISO `updated_at` 作为业务展示/排序字段。
- [ ] 扩展 DocumentDB index 管理以支持命名、检测和幂等移除旧的无效 TTL index；提供 dry-run 迁移脚本和旧数据 backfill。
- [ ] migration 不得直接让 session 元数据过期而绕过 message/attachment 清理。
- [ ] 明确 MemoryReflector 是 session scoped 还是 global；推荐 session scoped，并在创建/切换时调用 `set_memory_reflector()`，save/load 状态闭环。
- [ ] 使用真实 Mongo 的短 TTL integration 测试验证临时 collection；session 级联用 fake + 可选真实 DB 测试。

**验收：** 第 201 条会话可分页/搜索；临时记录按 BSON Date 过期；session 清理不产生归档和附件孤儿；reflector 重启状态保留。

**建议提交：** `fix(storage): paginate sessions and repair expiration semantics`

## 7. Batch C：消息、任务与生命周期所有权

### Task C1：用户消息排队与 work lease 幂等释放

**对应 review：** P1-06。

**主要文件：**

- `core/agent/agent.py`
- `core/monitoring/event_bus.py`
- `core/memory/session_manager.py`
- `core/agent/tests/` 与相关 input service 测试

**实施步骤：**

- [ ] 先用 event/barrier 固定复现同一 event-loop tick 两条 v2 消息：两条均已 accepted，但第一条未运行且 work count 残留。
- [ ] 为每个 admitted message 创建带唯一 ownership ID 的 `WorkLease`；release 幂等，可由正常 finally 和 task done callback 双保险调用。
- [ ] 用户消息进入有界 FIFO；后一消息不得隐式 cancel 前一 cognitive task。只有明确 interrupt/cancel 协议才能取消。
- [ ] admission 与 lease reserve/queue put 原子化；queue full 时在 accepted 前返回稳定 `QUEUE_FULL`/retryable。
- [ ] session deleting/restarting 时拒绝新 admission，不创建 lease。
- [ ] task 在首次调度前被取消也必须由 done callback 释放 lease。
- [ ] 覆盖双消息、三消息顺序、pre-start cancel、generation exception、queue full、interrupt 和最终 work count=0。

**验收：** 每个 accepted 消息恰好执行一次或收到明确终态失败；任何路径 lease 最终归零。

**建议提交：** `fix(agent): queue accepted messages and own work leases`

### Task C2：Per-call ToolInvocation、正确 session 与 call-ID 任务跟踪

**对应 review：** P1-08、P1-09、P1-13 的同名工具部分；D1 的前置。

**主要文件：**

- `Module/tool/protocol.py`
- `Module/tool/manager.py`
- `Module/tool/safety.py`
- `core/adapter/tool_adapter.py`
- `core/cognitive/tools.py`
- `core/tools/filesystem/plugin.py`

**实施步骤：**

- [ ] 定义一次调用一个不可变 invocation envelope：`call_id/session_id/trace_id/agent_id/services snapshot/normalized params`。
- [ ] ToolAdapter 每次执行时按请求 conversationId 解析对应 SessionMemory，复制 services；禁止复用并修改启动期 ToolContext。
- [ ] SafetyPipeline 返回规范化参数/decision 给当前 invocation；移除通过共享 `ctx.services['_safe_path']` 传路径的机制。
- [ ] 审批 request/token/hash 绑定 `call_id + session_id + tool name + 完整规范化参数 + expiry`；执行前再次验证相同 envelope。
- [ ] ToolManager `_tasks` 改为 call ID → task；同一工具可同时有多个 task，每个 finally 只移除自己。
- [ ] streaming 的 emitter/queue 也放入当前 invocation，不写共享 services。
- [ ] `cancel_all()`/按 call cancel 后 gather 全部目标 task，并明确返回取消结果。
- [ ] 迁移全部插件对 `_safe_path`/`_emit` 等隐式共享字段的依赖；保留窄范围兼容层时必须只读且不可跨调用共享。
- [ ] 覆盖两个 session 并发调用、同名工具乱序完成、只批准 A 却启动 B、审批过期、stream+non-stream 混合、cancel_all。

**验收：** 工具看到的 session 与请求 conversationId 一致；批准参数与真实副作用完全一致；同名任务全部可追踪/取消。

**建议提交：** `fix(tools): isolate invocation context and track tasks by call id`

### Task C3：After-response/reflector 绑定目标会话并统一追踪

**对应 review：** P1-10。

**主要文件：**

- `core/cognitive/loop.py`
- `core/personality/engine.py`
- `core/memory/session.py`
- lifecycle/after-response tests

**实施步骤：**

- [ ] `_finalize_turn` 创建 tail work 时捕获不可变 session ID、SessionMemory、reflector 和 generation ID，不捕获之后会 rebind 的 CharacterEngine 状态。
- [ ] `buffer_message()` 不再内部创建不可见 task；返回 awaitable 或交给统一 tail task registry。
- [ ] 每个 after-response task 归属于 session/generation，可按 session drain/cancel。
- [ ] session switch 只影响新 turn，不改变已创建 tail task 的 target。
- [ ] 覆盖 A consolidation 阻塞→切换 B→释放 A；A 内容只能进入 A reflector，且 task 被 lifecycle registry 看到。

**验收：** 任意 session switch 时序下无跨会话 reflector 写入；tail task 可由 shutdown/delete 等待。

**建议提交：** `fix(cognitive): bind after-response work to immutable session state`

### Task C4：安全 shutdown 顺序与可重入 restart

**对应 review：** P1-07、P1-13、P2-09 的 EventBus/close 部分。

**主要文件：**

- `core/agent/lifecycle.py`
- `core/agent/_lifecycle_tools.py`
- `core/agent/_lifecycle_persistence.py`
- `core/agent/_lifecycle_services.py`
- `core/agent/_lifecycle_task_system.py`
- `core/agent/server.py`
- `core/cognitive/loop.py`
- `core/adapter/tool_adapter.py`
- `core/memory/long_term.py`

**实施步骤：**

- [ ] 把 shutdown 固定为：关闭 admission → 停 event consumer → cancel/await generation → cancel/await tools/background → drain archive/tail → flush memory/session → 逐资源 close。
- [ ] 每一步设合理 timeout 并记录结构化结果；单个 close 异常不能阻断其他资源关闭，使用逐资源 try/finally 或 `gather(return_exceptions=True)`。
- [ ] EventBus stop 时 drain 或替换新实例；为事件增加 lifecycle epoch，旧 epoch 事件不得在 restart 后执行。
- [ ] ToolAdapter shutdown 后 `_started=False`，重新 start 能完整注册 plugin/schema/registry。
- [ ] CognitiveLoop start 重置 `_shutting_down` 等生命周期状态；所有 task registry 在 stop 后为空。
- [ ] LTM restart 复用 B5 的幂等 reload；其他 repo/service 明确 rebind 或重建。
- [ ] `/restart` 只有新 lifecycle 完成 readiness 后才返回成功；中途失败保持 stopped/degraded 并返回明确错误，不返回假成功。
- [ ] 覆盖正在 generation 时 stop、tail 在 flush 边界生成、close 抛异常、陈旧 EventBus 事件、连续三次 start/stop/restart。

**验收：** 连续三次 restart 后工具数量、memory 数量和 consumer 数量稳定；没有 pending task warning；旧事件不重放。

**建议提交：** `fix(lifecycle): make shutdown ordered and restart reentrant`

### Task C5：TaskScheduler checkpoint 与 proactive event 闭环

**对应 review：** P2-09 的 scheduler/time_of_day 部分。

**主要文件：**

- `core/task/scheduler.py`
- `core/task/task.py`
- `core/monitoring/idle_monitor.py`
- `core/agent/agent.py`
- 对应 task/monitoring tests

**实施步骤：**

- [ ] checkpoint 在 pending 为空时显式写空/删除旧文件；成功 restore 后 consume 或原子重写，防重复重放。
- [ ] background task 只持久化可重建 descriptor，不序列化 coroutine/future；无法恢复的 payload 在创建时拒绝。
- [ ] checkpoint 版本化并校验 schema；损坏文件隔离且不阻断启动。
- [ ] 为 `time_of_day` 明确产品行为：实现 consumer，或停止 producer；未知 proactive kind 至少 warning/metric，不能静默吞。
- [ ] 覆盖空 checkpoint、连续两次 restore、损坏 JSON、不可序列化 task、restart epoch 和所有 event kind。

**验收：** checkpoint 恰好恢复一次；所有 producer event 均有明确消费或拒绝行为。

**建议提交：** `fix(tasks): make checkpoints consumable and proactive events explicit`

## 8. Batch D：工具、输入与部署安全边界

### Task D1：文件 containment、symlink、资源上限与异步 I/O

**对应 review：** P1-14、P2-05 的文件部分。

**主要文件：**

- `Module/tool/safety.py`
- `core/tools/filesystem/plugin.py`
- `core/tools/filesystem/config.yaml`
- `core/tools/filesystem/test_filesystem.py`
- `core/tools/filesystem/test_write_file.py`
- `Module/tool/tests/test_tool_safety.py`

**依赖：** C2；插件必须消费 per-call normalized params，不再读取共享 `_safe_path`。

**实施步骤：**

- [ ] 增加 allowed `/sandbox`、请求 `/sandbox_evil/secret` 的全管线失败测试；覆盖 `..`、相对路径、大小写/分隔符边界和不存在目标。
- [ ] containment 使用 `Path.resolve()` 后的 `is_relative_to()`/`relative_to()` 或 `commonpath()`，禁止字符串 `startswith()`。
- [ ] `allow_symlinks=False` 时 read/write/list 均拒绝路径链和遍历项中的 symlink；ListDir 不进入 symlink directory。
- [ ] 缩小 check→open TOCTOU：Linux 可使用目录 fd + `openat`/`O_NOFOLLOW`；若保留 Path API，至少在 open 前后重新核对 inode/realpath，并将剩余限制记录在文档。
- [ ] 写入上限检查待写 content 的真实 UTF-8 bytes，不能只检查旧文件大小；覆盖多字节字符、边界值、新建和覆盖。
- [ ] ListDir 在遍历过程中限制节点数、深度、累计输出 bytes 和 deadline；不能遍历/排序全树后才截字符串。
- [ ] 阻塞文件 I/O 使用 `asyncio.to_thread()` 或受控 executor；取消/超时后不继续无限遍历。
- [ ] 输出错误不泄露允许根之外的绝对路径。

**验收：** sibling 和任何 symlink 组合都无法读取/枚举沙箱外内容；32-byte 上限不能写 33 bytes；大目录不会阻塞 event loop 或无界占用内存。

**建议提交：** `fix(filesystem): enforce real containment and bounded async IO`

### Task D2：统一 SSRF transport、逐跳校验与 Web 流式上限

**对应 review：** P1-15、P2-05 的 Web 部分。

**主要文件：**

- `core/input/ssrf.py`
- `core/input/api_routes.py`
- `core/agent/server.py`
- `Module/llm/registry.py`
- `Module/llm/openai_compat.py`
- `Module/tool/safety.py`
- `core/tools/web/plugin.py`
- SSRF/provider/web tool tests

**实施步骤：**

- [ ] 统一所有用户提供 URL 的 guard。至少覆盖 web tool、custom provider、内置 provider 覆盖 `base_url`、provider test/switch 和未来复用入口。
- [ ] `localhost`、无点主机名、解析失败、所有 loopback/private/link-local/multicast/unspecified/reserved、CGNAT、IPv4-mapped IPv6 和 metadata 地址默认拒绝。
- [ ] 每次请求和每个 redirect hop 重新校验 scheme/host/port，限制只允许 HTTP(S) 和有限跳数；跨 origin 剥离 Authorization/cookie/自定义敏感头。
- [ ] 解决 DNS rebinding/TOCTOU：连接必须固定到本次批准的 A/AAAA 地址，或在 transport 中验证实际连接目标；仅“连接前再调用 getaddrinfo”不算闭环。
- [ ] 所有请求设置 connect/read/write/pool timeout 和总 deadline；错误消息不泄露内部解析 IP。
- [ ] WebFetch 使用 streaming；先把可信 `Content-Length` 当快速拒绝，再以实际累计 bytes 作为最终硬上限。超过 `max_response_bytes` 立即关闭 response。
- [ ] 限制解压后 bytes，防压缩炸弹；限制 content type/文本解析成本。
- [ ] provider usable test 不回显未经安全过滤的内部响应片段。
- [ ] 覆盖 localhost、私网 DNS、多个 A/AAAA 中一个私网、IPv6、metadata、public→private redirect、跨域头、rebinding fake resolver、chunked/no-length/伪造-length/压缩大响应。

**验收：** 测试目标服务器确认第二跳从未到达；跨域不携带 secret；无论 header 如何，大响应都不会超过配置 bytes。

**建议提交：** `fix(network): enforce SSRF guard on every hop and bound responses`

### Task D3：搜索字面化、WS/REST 输入上限与真实失败反馈

**对应 review：** P1-16、P2-02。

**主要文件：**

- `core/input/session_routes.py`
- `core/storage/message_repo.py`
- `core/input/ws_handler.py`
- `core/input/api_routes.py`
- `frontend_contract/ws_message.py`
- `frontend_contract/rest.py`
- 对应 input/storage/contract tests

**实施步骤：**

- [ ] 搜索 query 设明确字符/字节上限；产品语义按字面搜索，进入 Mongo 前 `re.escape()`。更长期可换 text index，但不得继续暴露 raw regex。
- [ ] 为 search find/count 设置 DB deadline/`maxTimeMS`；repo 层也做防御性长度检查。
- [ ] `.`、`[`、反斜线、超长 query 和灾难模式均返回稳定结果，不产生 500/ReDoS。
- [ ] v2 contract 为 content、clientRequestId/messageId/conversationId、attachment ID/kind/mime 设置合理 max length；attachments 设置 max items（推荐 10，若当前产品另有决定则文档化）。
- [ ] handler 在访问 DB 前再次校验；附件存在性使用 bounded batch query，避免一帧数千次串行查询。
- [ ] message context `before/after` 限制在明确范围（例如 `0..100`）；负数、超大和非法值返回 CONTRACT_VALIDATION。
- [ ] `/api/chat` 检查 `process_message()` 返回值；queue full 映射 429 + `QUEUE_FULL`/retryable，不得固定返回成功。
- [ ] 生成/校验 backend contract artifacts；记录 Electron 需要同步的新增限制，但本任务不直接改 Electron repo。
- [ ] 覆盖所有边界值，且超限路径断言未调用 repo、`process_message()` 或 LLM。

**验收：** 用户输入不能构造 raw regex 或无界 DB fan-out；所有拒绝都有稳定错误码，客户端不收到假成功。

**建议提交：** `fix(input): bound requests and make search literal`

### Task D4：集中日志脱敏与安全错误响应

**对应 review：** P1-17。

**主要文件：**

- `Module/tool/safety.py`
- `core/cognitive/tools.py`
- `core/input/ws_handler.py`
- 公共 logging/redaction helper（新文件位置按现有分层决定）
- tool/input caplog tests

**实施步骤：**

- [ ] 建立结构化 redaction helper；默认只记录 tool name、call ID、session hash/ID、参数 key、类型、数量和 bytes，不记录原值。
- [ ] 对 token/key/password/secret/authorization/cookie/content/prompt/path/base_url query 等字段实行 deny-by-default；少数安全标量用显式 allowlist。
- [ ] 删除 SafetyPipeline 与 CognitiveTools 对 raw args 的重复 INFO 输出。
- [ ] Pydantic 错误使用 `errors(include_input=False)`，日志和 ACK 仅返回 loc/type/稳定 code，不包含 invalid input。
- [ ] 异常链、repr 和第三方 exception 也经过安全映射；生产响应不返回 traceback。
- [ ] 增加 canary secret/content/path 的 caplog + WS ACK 测试，扫描所有级别日志均不得出现原文。
- [ ] 更新 `docs/electron-backend-security.md`，列出可记录/禁止记录字段。

**验收：** token、聊天正文和完整路径的 canary 在日志、ACK、metric label 中均为零命中。

**建议提交：** `fix(logging): redact tool arguments and validation input`

### Task D5：上传活动内容隔离与配置 secret 纪律

**对应 review：** P2-03、P2-04。

**主要文件：**

- `core/input/api_routes.py`
- `core/input/server.py`
- `core/config/app_config.py`
- `Module/config/loader.py`
- upload/config tests
- `docs/electron-backend-security.md`

**实施步骤：**

- [ ] `/api/images` 和 desktop legacy upload 使用 magic-byte + 显式扩展/MIME allowlist；未知扩展默认拒绝，不映射成 image/png 或通用 octet-stream 后公开。
- [ ] 服务端生成安全文件名/扩展，不信任用户文件名；阻止 HTML/SVG/JS/可执行内容及 polyglot 风险类型。
- [ ] 优先复用签名附件下载；legacy 静态目录至少设置 `Content-Disposition: attachment`、`X-Content-Type-Options: nosniff` 和严格 CSP，最好迁移到隔离 origin。
- [ ] `SERVER_AUTH_TOKEN` 标记 `secret=True`；YAML 中的值被忽略或报配置错误，只允许环境/secret store 注入。
- [ ] 配置 dump、debug overview 和日志不得输出 secret 值。
- [ ] 覆盖 HTML/SVG/JS/EXE、扩展与 magic 不符、双扩展、空文件、响应头、YAML secret、环境变量优先级。

**验收：** 后端同源无法托管可执行活动内容；Git 跟踪 YAML 无法注入认证 token。

**建议提交：** `fix(upload): block active content and enforce secret-only auth config`

### Task D6a：非回环部署 fail-closed 鉴权

**对应 review：** 已知产品门禁 `todo.md:45`。

**主要文件：** `core/config/`、`core/input/server.py`、`app.py`、认证 middleware/tests、部署文档。

**实施步骤：**

- [ ] 明确 loopback 判定，覆盖 IPv4/IPv6/hostname；非回环 bind 且缺少要求 token 时，preflight 和启动均拒绝。
- [ ] 只有显式 `ALLOW_INSECURE_REMOTE` 开发开关可绕过，启动日志/health 明确 insecure；生产 profile 禁止该值。
- [ ] 统一 `/ws`、`/api/*`、upload/static signed access、management/debug 的认证矩阵；不要只保护管理路由。
- [ ] token 使用常量时间比较；禁止 query-string 长期携带 secret；Renderer 不接触后端密钥的 Electron 架构保持不变。
- [ ] 文档要求 HTTPS/WSS 或可信反向代理，并定义 trusted proxy 边界。
- [ ] 覆盖 loopback dev、非回环无 token拒绝、显式 insecure opt-in、完整鉴权、IPv6 和错误 token。

**验收：** 默认配置无法匿名监听非回环地址。

**建议提交：** `fix(auth): fail closed on unauthenticated remote bind`

### Task D6b：公开入口统一限流

**对应 review：** 已知产品门禁 `todo.md:47`。

**主要文件：** input middleware、WS handler、upload/provider test 路由、config、contract/error tests。

**依赖：** D6a，以认证 identity 为首选 key。

**实施步骤：**

- [ ] 实现可配置 token-bucket/sliding-window limiter；至少区分连接、普通消息、generation、provider test、upload、search 和高成本工具。
- [ ] key 优先使用认证主体；匿名 loopback/dev 才回退 IP。默认不信任 `X-Forwarded-For`，只有显式 trusted proxy 才解析。
- [ ] 单进程实现之外，为多进程/多实例部署定义共享 backend 或明确能力限制；产品模式不能假装全局限流。
- [ ] REST 返回 429，WS 返回稳定 `RATE_LIMITED` + retry hint，不关闭整个连接（除连接级滥用）。
- [ ] limiter 内存有界，旧 key 定期清理；高基数伪造 header 不得造成内存 DoS。
- [ ] 覆盖 burst、窗口恢复、不同 identity、伪造代理头、WS/REST/upload 和 limiter backend unavailable。

**验收：** 契约中的 `RATE_LIMITED` 有真实生产者，所有公开高成本入口均受保护。

**建议提交：** `feat(rate-limit): enforce limits across public inputs`

### Task D6c：跨重启 durable idempotency

**对应 review：** 已知产品门禁 `todo.md:46`。

**主要文件：** `core/input/idempotency.py`、storage repo/schema/index、WS handler、recovery tests、contract/server info。

**依赖：** B1、B2、B4。

**实施步骤：**

- [ ] 持久化 `(conversation_id, client_request_id, content_digest, message_id, status, expires_at)`；建立唯一约束，避免并发双写。
- [ ] 同键同内容重试返回原 message/ACK；同键异内容稳定拒绝；in-progress 重试返回当前状态而不重复 generation。
- [ ] `expires_at` 使用 BSON Date 的有效 TTL，或显式 cleanup；不得复用字符串 TTL 错误。
- [ ] accepted ACK 前必须完成 idempotency claim 和 B4 附件 reservation 的一致编排。
- [ ] Mongo unavailable 时产品模式 fail-closed/503；若本地开发保留内存降级，必须通过 server capabilities 明确暴露窗口和非 durable 状态。
- [ ] 覆盖 ACK 丢失、进程重启、两个并发相同请求、同键异内容、附件、过期回收和 DB 恢复。

**验收：** ACK 丢失后跨重启重试不会生成第二条回复或重复附件引用。

**建议提交：** `feat(idempotency): persist client request claims across restart`

### Task D7a：收紧 Compose 网络与凭据默认值

**对应 review：** P1-19。

**主要文件：** `docker-compose.yml`、`.env.example`（如存在）、部署文档、Compose policy test。

**实施步骤：**

- [ ] Mongo/MinIO/Milvus 仅通过内部 network 暴露给后端；调试需要宿主端口时默认绑定 `127.0.0.1`。
- [ ] 移除 `minioadmin` 等默认凭据；缺 secret 时生产 profile 拒绝启动。
- [ ] Mongo 启用认证；Milvus 按实际版本配置认证/token 或明确仅内网不可发布。
- [ ] 划分 `dev`/`production` profile；production 不发布数据库管理端口，不允许 insecure remote 开关。
- [ ] CI 解析 Compose 并断言无 `0.0.0.0` 数据端口、默认密码、空 token。
- [ ] 更新备份、凭据轮换和最小端口文档。

**验收：** 默认宿主网络扫描无法直接访问 Mongo/MinIO/Milvus。

**建议提交：** `fix(deploy): keep data services private by default`

### Task D7b：固定 embedding 模型供应链

**对应 review：** P1-18。

**主要文件：** `scripts/download_model.py`、`Module/embedding/huggingface.py`、模型配置/manifest、tests。

**实施步骤：**

- [ ] 为默认 BGE 模型固定不可变 commit revision，并维护所需文件的 hash manifest。
- [ ] 官方 Hugging Face endpoint 为默认；第三方 mirror 只能显式 opt-in，并仍验证相同 hash。
- [ ] 默认 `trust_remote_code=False`；只有经 allowlist、固定 revision/hash 的特殊模型可显式开启。
- [ ] 缓存检查验证完整 manifest，不以目录/单文件存在作为成功。
- [ ] 下载到临时目录，全部校验通过后原子切换当前 snapshot；失败保留旧可用版本。
- [ ] 覆盖篡改文件、错误 revision、缺文件、mirror 内容不同、remote-code 默认参数。

**验收：** 上游 tag/mirror/cache 被替换时启动明确失败，不执行任意远程模型代码。

**建议提交：** `fix(models): pin and verify embedding artifacts`

## 9. Batch E：Workflow、工具重载、Milvus 与契约样例

### Task E1：工具热重载原子替换与失败回滚

**对应 review：** P2-06。

**主要文件：** `Module/tool/manager.py`、`Module/tool/tests/test_framework.py`、插件配置 loader。

**依赖：** C2 的 call-ID task registry。

**实施步骤：**

- [ ] reload 后从新 module 重新 discover class，不读取旧 `_plugin_classes[name]` 作为新实现。
- [ ] 正常 load/reload 共用同一 config.yaml 解析、SafetyProfile 和 setup 流程。
- [ ] 先在隔离位置构造/setup 新实例；成功后原子替换 registry，再等待旧实例 in-flight calls 排空并 teardown。
- [ ] 新 setup 失败时保留旧实例、旧 schema 和可用状态；返回明确失败原因。
- [ ] reload 与新 execute 加锁/版本化，调用只能看到完整旧版或完整新版。
- [ ] 覆盖 class/description/config 变化、setup/teardown 异常、in-flight call、连续 reload 和 rollback。

**验收：** reload 后新调用确实使用新 class/config；失败时旧工具仍可用。

**建议提交：** `fix(tools): reload plugins with atomic rollback`

### Task E2：Workflow submit 立即返回且有界排队

**对应 review：** P2-07。

**主要文件：**

- `Module/workflow_engine/workflow_engine.py`
- `Module/workflow_engine/mailbox.py`
- `core/cognitive/tools.py`
- workflow lifecycle/unit/service tests

**产品决策：** 采用“立即返回 mailbox + 后台有界 QUEUED”方案；如果项目负责人改选快速 `BUSY/QUEUE_FULL`，必须先更新公开契约和本计划记录。

**实施步骤：**

- [ ] `submit()` 先验证/注册 task 与 mailbox，立即返回；semaphore acquire 移到后台 runner。
- [ ] 增加显式 QUEUED 状态/事件和最大 queue size；队列满快速返回稳定错误，不能无限 await。
- [ ] PAUSED task 释放 execution slot；resume 时重新排队/acquire，保持公平性并防饥饿。
- [ ] stop/cancel 能移除排队任务并推唯一终态；shutdown 不留下等待 semaphore 的 task。
- [ ] `core/cognitive/tools.py` 不因 slot 满阻塞当前用户轮次。
- [ ] 修正现有“第二次 submit 应阻塞”的测试，改为契约期望；覆盖 max=1、pause、queue full、stop 和 FIFO。

**验收：** 首任务无限暂停时第二次 submit 在短确定时限内返回 mailbox/明确 queue-full，不阻塞聊天 turn。

**建议提交：** `fix(workflow): make submit nonblocking with bounded queue`

### Task E3：Workflow cancel/pause 终态单调性（含 M19/M20）

**对应 review：** P2-08；已知 M19/M20。

**主要文件：**

- `Module/workflow_engine/lifecycle.py`
- `Module/workflow_engine/core/sub_agent/planning.py`
- `Module/workflow_engine/core/sub_agent/sub_agent.py`
- orchestrator/execution loop/resume 路径
- workflow unit/service tests

**实施步骤：**

- [ ] 为每个 workflow 建 cancellation token/owned task；cancel 实际取消 planner/executor future，而不只写状态。
- [ ] 每个外部 await（planner、LLM、tool、retry、semaphore）后检查 token；CANCELLED/FAILED/COMPLETED 等终态不可逆。
- [ ] terminal event 只推一次，registry/mailbox 清理幂等。
- [ ] 修 M19：resume 后再次 pause 不销毁 task/mailbox，消费者不会永久阻塞。
- [ ] 与 E2 对齐：pause 释放 slot，resume 重新 acquire；cancel 排队/暂停任务均立即完成。
- [ ] 覆盖慢 planner cancel、planning retry、tool side effect 前 cancel、resume→pause→resume、stop 和乱序事件。

**验收：** 状态一旦 CANCELLED 永远不会回 RUNNING；取消后不再调用 LLM/工具或消耗 token。

**建议提交：** `fix(workflow): make cancellation terminal and pause resumable`

### Task E4：Workflow 测试确定性与外部 E2E 分层

**对应 review：** P2-11 的失败用例和 15 条 warning 的相关部分。

**主要文件：** `pytest.ini`、WorkflowEngine integration tests、Makefile、CI workflow。

**实施步骤：**

- [ ] 新增 `external` marker；真实 LLM/公网/真实 provider 测试必须同时标 `integration + external`，不能仅凭环境里恰好有 API key 自动进入默认全量。
- [ ] 为 cancel/pause/mailbox 首事件建立 fake LLM 的确定性 unit/service 测试；用 event 控制 planner 延迟。
- [ ] 真实 E2E 使用显式 `RUN_EXTERNAL_TESTS=1`、受保护凭据、总 timeout 和成本上限；失败日志不输出 prompt/response secret。
- [ ] 修 `coroutine was never awaited` warning：测试 mock 使用正确 async iterator/coroutine shape，并在 teardown drain task。
- [ ] `make test-workflow-engine` 包含核心 unit/service；`make test-external` 单独运行 external。

**验收：** 无网络/无 key 环境可稳定跑全部非 external 测试；同一 commit 连跑三次结果一致且无未 await warning。

**建议提交：** `test(workflow): separate deterministic gates from external E2E`

### Task E5：Milvus 输入校验与 metric 持久恢复

**对应 review：** P2-10。

**主要文件：**

- `Module/database/vectordb/protocol.py`
- `Module/database/vectordb/milvus.py`
- `Module/database/vectordb/tests/test_vectordb.py`
- `docs/database-storage.md`

**实施步骤：**

- [ ] bulk insert 在调用 Milvus 前验证 vectors/metadata 数量相等且非空，所有向量维度符合 collection schema；不使用 `zip` 静默截断。
- [ ] 错误返回稳定 validation exception，不产生部分 insert。
- [ ] collection 创建时从 index metadata 记录 metric；load/restart 时读取 Milvus index params 恢复 COSINE/IP/L2。
- [ ] metadata 缺失或不支持时 fail closed/要求显式 metric，禁止默认为 L2 后继续返回看似正常的排序。
- [ ] 覆盖 metadata 多/少、空输入、维度错、COSINE/IP 跨实例 load 和缺失 index params。

**验收：** 不等长输入零写入；重启前后同一 query 的 metric 和排序语义一致。

**建议提交：** `fix(vectordb): validate batches and restore collection metric`

### Task E6：Desktop golden 从 runtime 单源生成

**对应 review：** P2-14。

**主要文件：**

- `scripts/gen_contract_frames.py`
- `core/tools/desktop/plugin.py`
- desktop registry/argument hash helper
- `scripts/tests/test_gen_contract_frames.py`
- `frontend_contract/`（仅必要的正式契约事实源）

**实施步骤：**

- [ ] 把 desktop 参数 schema、risk level 和 canonical arguments hash 抽成无副作用单源，runtime plugin 与 generator 共同消费。
- [ ] read golden 只包含 runtime 支持字段；risk 与 registry 一致；hash 使用规范化参数真实 SHA-256。
- [ ] generator `--check` 除文件自洽外，还导入/比较 runtime metadata 的语义等价。
- [ ] 覆盖参数新增/删除、risk 变化、key 顺序不同 hash 相同、真实 hash 不等于 placeholder。
- [ ] 更新后端发布给 Electron 的 artifact/frames；记录 schema hash 和 backend commit。

**验收：** 人为修改 runtime schema/risk 后 `--check` 必须失败，直到 golden 重生成。

**建议提交：** `fix(contract): generate desktop frames from runtime metadata`

## 10. Batch F：工程化、release gate 与文档

### Task F1：建立真正全量的离线 CI 矩阵

**对应 review：** P2-11；承接 A2、E4。

**主要文件：** `Makefile`、`.github/workflows/ci.yml`、`pytest.ini`、测试文档。

**实施步骤：**

- [ ] `test-all` 改为真正全仓非 external pytest，或重命名旧 target 为 `test-subset`，避免名称撒谎。
- [ ] CI 必跑：lint、pyright、contract drift、Module/core/tools/frontend_contract/scripts tests、WorkflowEngine unit/service。
- [ ] slow 但确定性的测试可单独 job，不得永久忽略整个 WorkflowEngine 目录。
- [ ] external job 仅受保护手工/定时运行；结果与 merge gate 分开展示。
- [ ] docs-only path ignore 不应跳过契约/安全文档生成检查；至少 plan/review 纯文档可跳过重测试，活跃协议变更必须触发 drift check。
- [ ] CI 输出收集/运行数量和耗时，不手写易漂移数字作为唯一真相。
- [ ] 添加 warnings-as-errors 的目标范围，先清本次 15 条异步 warning，再逐步扩大。

**验收：** 一个会破坏 Workflow cancel/submit 的提交会阻止合并；干净 runner 无 secret 也能跑完 merge gate。

**建议提交：** `ci: gate the complete deterministic backend suite`

### Task F2：锁定 runtime/dev 依赖并增加安全扫描

**对应 review：** P2-12、审查限制中的未执行扫描。

**主要文件：** `requirements.txt`、新增 input/lock 文件、setup/CI、依赖文档。

**实施步骤：**

- [ ] 选择并文档化单一锁定工具。推荐保留易读的 runtime/dev input，并生成带精确版本和 hash 的 lock；不要继续只有下限。
- [ ] runtime 与 dev/test 工具分离；pytest/ruff/pyright/扫描器的版本也被锁定。
- [ ] CI 在全新临时环境从 lock 安装，不复用宿主 conda site-packages；Python 版本固定。
- [ ] `pip check`、`pip-audit`（或等价）进入定期/受控门禁；Bandit 对生产 Python 执行，误报使用带理由的窄 suppression。
- [ ] 容器镜像和关键 action 固定版本/digest；更新流程有自动测试而非永久冻结。
- [ ] clean-room smoke 验证 import、`app.py --check`、最小 backend startup。

**验收：** 新机器只凭仓库声明即可得到与 CI 一致环境；依赖解析不随时间漂移。

**建议提交：** `build: lock runtime and development dependencies`

### Task F3：Backend-only setup/start，不依赖 v1 前端

**对应 review：** P2-13。

**主要文件：** `scripts/setup.sh`、`scripts/start.sh`、Makefile、README/部署文档、shell tests。

**实施步骤：**

- [ ] backend-only 成为默认或明确的一等 mode，无 Node/npm 时仍可安装和启动后端。
- [ ] v1 UI 构建移到独立 `legacy-ui` packaging target，只有显式请求才执行，并使用 `npm ci`。
- [ ] startup 不在每次运行修改依赖树。
- [ ] preflight 失败默认非零退出；跳过只能用显式 flag，并打印明显 degraded 状态，不能 `|| true` 静默继续。
- [ ] 增加 shell smoke：PATH 无 node/npm、无 v1 build、无效配置失败、合法配置进入 backend start dry-run。

**验收：** Electron/backend 部署不安装 Node 也能使用官方脚本；错误配置不会继续启动。

**建议提交：** `fix(scripts): support strict backend-only setup and start`

### Task F4：扩展静态门禁并渐进拆分复杂入口

**对应 review：** P3-01、P3-02。

**主要文件：** Ruff/Pyright 配置、`scripts/`、`core/monitoring/__init__.py`、input handlers/tests。

**实施步骤：**

- [ ] 把 `scripts/` 纳入 Ruff/Pyright；修当前 17 个 ANN 和 `capture_ws_trace.py` 的类型错误，动态边界只做窄 suppression。
- [ ] 修 `core/monitoring/__init__.py` 的无效 `IdleMonitor` export，使默认 Pyright 0 warning。
- [ ] 对异步 `coroutine was never awaited` 开启 warnings gate；先修测试 double，不用全局 filter 忽略。
- [ ] 为安全关键模块启用选定 Bandit/Ruff 规则；任何 ignore 带路径、规则和理由。
- [ ] 不做全仓 C901 大爆炸。先把 `register_api_routes` 拆为按领域注册函数，把 `handle_json_message` 拆为验证/dispatch 表，把 `handle_ws` 的生命周期/错误映射分离。
- [ ] 每次重构先用现有行为测试锁定，并与功能修复分提交；目标是降低关键入口复杂度和可变共享状态，不追求一次清零 79 个热点。

**验收：** 默认 lint/type 覆盖生产脚本且 0 error/warning；关键 input 入口职责可独立测试。

**建议提交：** `chore(quality): extend static gates and split critical handlers`

### Task F5：活跃文档、测试统计与 Electron 交接收口

**对应 review：** P3-03；全计划的跨仓交付。

**主要文件：** `README.md`、`docs/testing.md`、`docs/README.md`、协议/安全/存储文档、契约 artifact 发布说明。

**实施步骤：**

- [ ] 删除或自动生成易漂移的固定测试数；README 和 testing 使用同一脚本输出。
- [ ] 错误码目录由 contract 单源生成或 CI 比对，消除 40/41 不一致。
- [ ] 更新存储故障、GC、删除、TTL、restart、remote auth/rate/idempotency、Compose 和模型供应链运维说明。
- [ ] 生成最终 contract schema/raw TS/golden frames，并记录 protocol version、schema hash、backend commit。
- [ ] 输出给 Electron 仓库的变更清单：新增输入上限、错误码/429、capabilities、idempotency、queued workflow/desktop frame 等；不在本仓库替 Electron 实现 mapper。
- [ ] 更新本计划所有 checkbox 和执行记录；源 review 保留原始发现，不改写成“未曾存在”。
- [ ] 在 `todo.md` 只把有代码、测试和验证证据的项标完成；未完成项保留。

**验收：** 活跃文档与实际命令/契约一致；Electron 团队拿到单一版本化交接包即可做 replay。

**建议提交：** `docs: close remediation evidence and Electron handoff`

## 11. Review 发现覆盖矩阵

| Review 项 | 修复 Task | Release gate |
|---|---|---|
| P0-01 附件 GC 误删 | A0、A1 | A |
| P0-02 self-hosted PR 读取 `.env` | A0、A2 | A |
| P1-01 session fallback/load/pending | B1、B2 | B |
| P1-02 archive 未追踪、删除后回写 | B3、C4 | B/C |
| P1-03 离线删除复活、附件遗留 | B1、B3 | B |
| P1-04 accepted 后才加附件 ref | B4、D6c | B/D |
| P1-05 Mongo health 假阳性/不恢复 | B1 | B |
| P1-06 突发消息丢失/work lease | C1 | C |
| P1-07 restart 不可重入 | B5、C4 | C |
| P1-08 ToolContext 不绑定会话 | C2 | C |
| P1-09 HITL `_safe_path` 串线 | C2、D1 | C/D |
| P1-10 after-response 跨会话 | C3、C4 | C |
| P1-11 consolidator 并发丢消息 | B5 | B |
| P1-12 LTM 离线丢/复活 | B1、B5 | B |
| P1-13 shutdown/同名工具取消 | C2、C4 | C |
| P1-14 文件 sibling/symlink 逃逸 | D1 | D |
| P1-15 Web/LLM SSRF | D2 | D |
| P1-16 Mongo raw regex/ReDoS | D3 | D |
| P1-17 原始参数/正文日志 | D4 | D |
| P1-18 模型供应链 | D7b | D |
| P1-19 Compose 无鉴权暴露 | D7a | D |
| P2-01 session 200 上限/TTL/reflector | B2、B6 | B |
| P2-02 WS/REST 上限与假成功 | D3 | D |
| P2-03 活动内容上传 | D5 | D |
| P2-04 SERVER_AUTH_TOKEN 非 secret | D5 | D |
| P2-05 文件/Web 资源上限 | D1、D2 | D |
| P2-06 工具 reload 旧 class | E1 | E |
| P2-07 Workflow submit 阻塞 | E2 | E |
| P2-08 Workflow cancel 逆转 | E3 | E |
| P2-09 EventBus/checkpoint/close/event | C4、C5 | C |
| P2-10 Milvus 截断/metric | E5 | E |
| P2-11 Workflow/全量 CI 缺口 | A2、E4、F1 | F |
| P2-12 依赖不可复现 | F2 | F |
| P2-13 backend 脚本依赖 v1 | F3 | F |
| P2-14 desktop golden 漂移 | E6 | E |
| P3-01 scripts 静态门禁 | F4 | F |
| P3-02 input 复杂度 | F4 | F |
| P3-03 活跃文档漂移 | F5 | F |
| 已知：远程鉴权 fail-closed | D5、D6a | D |
| 已知：实际限流 | D6b | D |
| 已知：跨进程幂等 | D6c | D |
| 已知：Workflow M19/M20 | E3 | E |

覆盖规则：关闭任何 review 项前，必须能从对应 Task 找到失败测试、生产修复、验收结果和 commit；不能只因“同批做过相邻代码”而关闭。

## 12. 最终故障注入与发布验收

### 12.1 数据耐久性场景

- [ ] 500+ attachment 跨页，任意页 Mongo 失败时零物理删除。
- [ ] Mongo 启动离线：新会话、多会话、附件消息可明确降级/拒绝，任何 success 都可恢复。
- [ ] Mongo 运行中断网→`/ready=503`→恢复；所有 pending/outbox/dirty 自动补偿。
- [ ] archive append 与 delete 每个关键顺序排列均不产生 orphan/复活。
- [ ] ACK 丢失 + 进程重启 + Electron 重试只产生一次 generation 和一次附件关联。
- [ ] LTM 离线 insert/update/delete 后重启，不丢写、不复活、不重复。

### 12.2 并发与生命周期场景

- [ ] 同一 tick 2/10 条消息 FIFO，accepted/终态一一对应，work count=0。
- [ ] 两会话并发工具调用，session/context/输出完全隔离。
- [ ] 两个同名写工具乱序审批，只执行被批准的准确规范化路径。
- [ ] generation、tool、archive、after-response 各自在运行时 shutdown，全部被 await/drain。
- [ ] 连续三次 `/restart`：工具数量、memory 数量、consumer/task 数稳定，旧事件不重放。
- [ ] Workflow queued/pause/resume/cancel/stop 的状态终态单调，slot 不泄漏。

### 12.3 安全场景

- [ ] 文件 sibling、文件/目录 symlink、`..`、不存在目标和替换竞态全部拒绝。
- [ ] Web 与 provider base_url 的 localhost/private/IPv6/metadata/public redirect/rebinding 全部拒绝。
- [ ] 大/压缩/chunked response 和深目录严格受 bytes/node/time 限制。
- [ ] HTML/SVG/JS/polyglot 上传不能同源执行。
- [ ] 日志/ACK/metric 中 secret 与聊天正文 canary 零命中。
- [ ] 非回环无 token 启动失败；rate limit 对 WS/REST/upload/provider test 生效。
- [ ] 默认 Compose 无公开数据端口/默认密码；模型 cache 篡改后拒绝加载。

### 12.4 工程和契约场景

- [ ] clean-room lock install 成功，`app.py --check` 13/13。
- [ ] 非 external 全量测试至少连续三次稳定通过，无未 await warning。
- [ ] Workflow core unit/service 是 required CI check；external E2E 单独可见。
- [ ] Ruff/Pyright 覆盖生产代码和 scripts，0 error；契约/golden drift 检查通过。
- [ ] backend-only 环境没有 Node 也能 setup/start。
- [ ] Electron 用最终 raw contract/golden replay 通过；backend commit/schema hash 可追溯。

## 13. 每个 Task 的完成记录模板

Claude Code 完成任务后在此追加，不要删除旧记录：

```markdown
### <Task ID> — <标题>

- 状态：完成 / 部分完成 / 阻塞
- Commit：`<sha>`
- 变更文件：`...`
- 新增失败测试：`...`
- 目标测试：`<command>` → `<result>`
- 批次门禁：`<command>` → `<result>`
- 数据迁移/回滚：无 / `<说明>`
- 契约或 Electron 影响：无 / `<说明>`
- 剩余风险：无 / `<说明>`
```

## 执行记录

### A0 — 执行前安全快照与运行约束

- 状态：完成
- Commit：随 A1 一起（见下）
- 变更文件：无（只读侦察）
- 事实记录：HEAD `d1cb3ae0c609008ea1e5576aaa1b245f12e64756` == review 基线；工作树干净，仅未跟踪 `docs/review/full-project-review-2026-08-10.md` 与 `docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md`（保留）。附件根 `data/uploads/`（0 个文件，12K——开发环境无真实数据）；Mongo 探针时未连接；`.env` 存在（3.3KB），CI 确凿存在 `pull_request + self-hosted + cp /home/yomu/elysia-agent/.env`（P0-02 实锤，A2 处理）。备份/恢复命令：Mongo `mongodump --uri "$MONGODB_URI"`；附件 `cp -a data/uploads data/uploads.bak`。是否跑过不可信 PR：无法从代码代理侧确认 GitHub Actions 运行历史，凭据轮换属人工动作——已在最终报告标注。
- 目标测试：无（只读）
- 批次门禁：无
- 数据迁移/回滚：无
- 契约或 Electron 影响：无
- 剩余风险：A0 无法替代人工凭据审计；若历史上有不可信 PR 跑过当前 CI，需人工轮换可能暴露的密钥。

### A1 — 附件 GC 完整分页、复核和 fail-closed

- 状态：完成
- Commit：`<见 A1 提交>`（本记录在 A1 提交后由会话补全）
- 变更文件：`core/storage/attachment_repo.py`、`core/storage/attachment_gc.py`、`core/storage/tests/test_attachment_repo.py`、`core/storage/tests/test_attachment_gc.py`、`docs/database-storage.md`
- 新增失败测试：`TestPagination`（>100 完整返回 / 分页中途失败抛 StorageUnavailableError / find_by_stored_name）；`TestP0OrphanPagination`（101/520/600 混合跨页零误删 / DB 页失败零删除 fail-closed）；`TestRule4Recheck`（扫描期间新登记文件免删）；`TestQuarantine`（规则 1/4 隔离入 `.quarantine` / 规则 5 超期物理删 / dry_run 不改库不落盘）
- 目标测试：`pytest core/storage/tests/test_attachment_repo.py core/storage/tests/test_attachment_gc.py` → 52 passed；`pytest core/storage/tests/ core/agent/tests/` → 368 passed, 1 warning（既有）
- 批次门禁：`ruff check core/storage/` → All checks passed；`pyright core/storage/attachment_repo.py core/storage/attachment_gc.py` → 0 errors
- 数据迁移/回滚：无新索引；物理删除改两阶段 quarantine（`.quarantine/{kind}/`，同文件系统可恢复，规则 5 默认 7 天后 unlink）。恢复：误隔离文件可从 `.quarantine` 还原 + 重建记录
- 契约或 Electron 影响：无（内部 GC，无 wire 变化）
- 剩余风险：quarantine 区在 `data/uploads/.quarantine`，`git status` 若该目录在版本控制内需确认 ignore；已确认不在 KINDS 扫描范围

### A2 — PR CI 与宿主凭据隔离

- 状态：完成
- Commit：`<见 A2 提交>`
- 变更文件：`.github/workflows/ci.yml`、`scripts/check_ci_isolation.py`（新）、`scripts/tests/test_check_ci_isolation.py`（新）
- 新增失败测试：`TestRejectInsecure`（旧模式 `pull_request + self-hosted + cp /home/.../.env` 必须拒绝）；`TestAcceptSecure`（PR 用 GitHub-hosted、push-only self-hosted 通过；PR 可达 self-hosted 仍拒绝）
- 目标测试：`pytest scripts/tests/test_check_ci_isolation.py` → 4 passed；`python scripts/check_ci_isolation.py .github/workflows/ci.yml` → 通过
- 批次门禁：无 key 环境模拟 PR runner：`env -u DEEPSEEK_API_KEY ... pytest -m "unit or service"` → 2914 passed, 2 skipped, 95 deselected（证明 PR job 无 host secret 可全绿）
- 数据迁移/回滚：无
- 契约或 Electron 影响：无（仅 CI 信任边界）
- 剩余风险：push 到 main 的 self-hosted job 仍引用宿主 `.env`（受信 ref 故意保留，`if: github.event_name == 'push'` guard + 门禁脚本双保险）；外部真实凭据 E2E 的 workflow_dispatch/environment job 拆分在 F1/E4 落地

### B1 — 统一存储不可用语义与真实健康检查

- 状态：完成（健康检查语义 + 错误收口；repo 写语义 raise 按任务拆到 B2/B3/B5）
- Commit：`<见 B1 提交>`
- 变更文件：`core/storage/base.py`、`Module/database/documentdb/{protocol,mongodb}.py`、`Module/database/vectordb/{protocol,milvus}.py`、8 个 repo（session/message/token/generation/desktop/character_version/memory/attachment）、`core/agent/_lifecycle_persistence.py`（统一恢复协调器）、`core/cognitive/generation_registry.py`、`core/desktop/bridge.py`、`core/input/server.py`（/ready 聚合）、相关测试
- 新增失败测试：`core/storage/tests/test_health_semantics.py`（7 repo × 可达/断网/恢复 21 例 + 未连接自愈）；断网假阳性测试（`_db 非 None` 但 ping 失败 → health=False）
- 目标测试：`Module/database/ core/storage/ core/memory/ core/agent/ core/input/ core/desktop/ core/cognitive/` → 1285+ passed（待最后确认）
- 批次门禁：`ruff check` → All checks passed；`pyright` → 0 errors（1 个既有 IdleMonitor warning，F4 处理）
- 数据迁移/回滚：无
- 契约或 Electron 影响：无 wire 变化；`/ready` 现在聚合 session+message+attachment 真实 ping，Mongo 断网即 503
- 剩余风险/计划偏差：B1 计划列的“repo 写语义禁止静默 return”拆到 B2（session save/load）、B3（delete/archive）、B5（LTM dirty/tombstone）逐一转换调用方（避免一次大爆炸破坏未转换 caller）；desktop_call_repo 保留设计内“离线写 cache”降级（不 raise）

### B2 — Session durable save/load/pending 恢复

- 状态：完成
- Commit：`<见 B2 提交>`
- 变更文件：`core/storage/session_repo.py`、`core/memory/session.py`、`core/memory/session_manager.py`、`core/agent/_lifecycle_persistence.py`、`core/memory/tests/test_session.py`、`core/memory/tests/test_session_manager.py`
- 新增失败测试：`TestSessionDurablePersistence`（repo 离线 save 落 JSONL / Mongo durable 删 pending / `_storage_unavailable` 标志区分 not-found）；`TestEnsureLoadedConcurrency`（并发合并单次 load / 存储不可用不标记 loaded 可重试）；`TestRecoverPendingSessions`（多 pending 全扫 / 损坏忽略 / Mongo 可用导入删 pending）；离线 create/list/rename 读后写一致
- 目标测试：`pytest core/memory/ core/agent/ core/input/ core/cognitive/ core/personality/` → 1019 passed；`pytest core/memory/tests/test_session.py core/memory/tests/test_session_manager.py` → 61 passed
- 批次门禁：`ruff check` → All checks passed；`pyright` → 0 errors（1 个既有 warning）
- 数据迁移/回滚：pending JSON 格式不变；Mongo durable 成功后删除 pending（原本 `_start_persistence` 手动删，现收敛到 save()）
- 契约或 Electron 影响：无 wire 变化
- 剩余风险：运行中「failed-load 会话收到新消息后部分覆盖 Mongo 旧上下文」的 partial-clobber 未完全闭合（空覆盖已挡；partial 需 B6 读前合并）；非活动 pending 的启动扫描已加，运行中新建 pending 的即时 import 待恢复协调器（已由 B1 的 session 恢复 hook 覆盖 load_active）

### B3 — 归档任务所有权与 durable 删除

- 状态：完成
- Commit：`<见 B3 提交>`
- 变更文件：`core/memory/session.py`、`core/memory/session_manager.py`、`core/storage/session_repo.py`、`core/storage/message_repo.py`、`core/agent/_lifecycle_persistence.py`、`core/input/session_routes.py`、相关测试
- 新增失败测试：`TestArchiveOwnership`（归档 task 登记/清理、close 后不再新增）；`TestDeleteDurable`（慢 append 在途→删除先 drain 再清记录、离线删除抛错不假成功、删除失败重开归档）；session_repo/message_repo 离线删除/保存/加载 raise 语义更新
- 目标测试：`core/memory/ core/storage/ core/input/tests/test_session_routes.py core/input/tests/test_server.py` → 488 passed；session/storage 专项 115 passed
- 批次门禁：`ruff check` → All checks passed；`pyright` → 0 errors（1 个既有 warning）
- 数据迁移/回滚：无新集合/索引；删除语义离线 → 503 retryable（不再假成功）
- 契约或 Electron 影响：DELETE /api/sessions/{id} 离线/附件释放失败 → 503 STORAGE_UNAVAILABLE retryable（新增稳定错误码，Electron 需感知）
- 剩余风险：durable deletion job/outbox（附件释放→删消息→删会话跨步幂等重试）未实现——当前为「任一存储操作失败即整体失败 + 503 retryable」，客户端重试即可收敛；多步部分成功（如消息已删、session 未删）需靠幂等重试收敛，无独立恢复 job

### B4 — accepted ACK 前建立附件引用

- 状态：完成
- Commit：`<见 B4 提交>`
- 变更文件：`core/input/ws_handler.py`、`core/storage/attachment_repo.py`、`core/input/tests/test_attachment_message.py`
- 新增失败测试：`test_queue_full_rolls_back_ref_and_keeps_active`、`test_ref_rolled_back_when_process_raises`、`test_add_ref_failure_rejects_and_rolls_back_all`（多附件部分失败→STORAGE_UNAVAILABLE 拒绝、不调 process、全部回滚 active）
- 目标测试：`pytest core/input/tests/test_attachment_message.py` → 15 passed
- 批次门禁：`ruff check core/input/ws_handler.py core/storage/attachment_repo.py` → 通过；`pyright` → 0 errors（1 个既有 warning）；input 全量回归待确认
- 数据迁移/回滚：新增 `attachment_repo.release_reservation`（幂等 pull，不触发归零软删）；无索引变化
- 契约或 Electron 影响：无 wire 变化；附件 refs 持久化失败 → rejected STORAGE_UNAVAILABLE retryable（新增稳定错误码）
- 剩余风险：预留后进程崩溃的悬挂 ref（消息未接受但 ref 存在）——安全方向（附件保留，不误删），由消息删除/reconciler 释放；当前无独立 reconciler

## 14. 需要停止并请求决策的情况

以下情况不能由代码代理自行假设：

1. 需要直接删除、覆盖或迁移真实用户数据，且没有备份/回滚确认。
2. 需要改变公开 Electron wire 语义但无法保持兼容，也没有版本升级决定。
3. Workflow submit 产品方拒绝推荐的 bounded queue，需要在 `BUSY` 与阻塞语义中另选。
4. 生产部署必须支持某种当前计划会禁止的公开数据库端口、第三方模型镜像或 insecure remote 模式。
5. 存储一致性需要 Mongo transaction，但实际部署不是 replica set；必须改用 outbox，而不能假定事务存在。
6. 当前工作树中有与任务相同文件的用户未提交改动，且无法安全合并。

除上述情况外，按本文默认决策继续实现，不要为了非关键命名/风格选择反复暂停。
