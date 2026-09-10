# Elysia Agent 全项目综合审查报告（2026-08-10）

> 审查基线：`d1cb3ae0c609008ea1e5576aaa1b245f12e64756`
>
> 审查性质：只读代码审查、静态检查、测试与定向最小复现；本报告不包含修复。

## 1. 结论先行

当前后端可以继续用于**受控的 Electron 联调开发**，但尚不满足携带真实用户数据的发布条件，也不应直接暴露到不可信网络。

主要原因不是 Electron v2 字段契约整体失配：契约生成、自检和大部分自动化门禁均能通过。真正的发布阻断集中在以下四条链路：

1. **数据安全**：启动附件 GC 会把第 101 条以后仍有效、仍被引用的附件误判为孤儿并删除；Mongo 离线时多个“成功”结果并不代表数据已经持久化。
2. **并发与生命周期**：突发消息、归档、会话删除、记忆整合、工具取消和 `/restart` 都存在可稳定复现的竞态或不可重入状态。
3. **工具安全边界**：文件沙箱、HITL 审批路径、`web_fetch`/LLM 自定义地址的 SSRF 防护以及日志脱敏均未闭合。
4. **发布工程**：不可信 PR 可在持久化 self-hosted runner 上接触宿主 `.env`；依赖和 CI 环境不可复现，WorkflowEngine 测试没有进入常规合并门禁。

发布判断：

| 场景 | 结论 | 前置条件 |
|---|---|---|
| 本机、测试数据、受控 Electron 联调 | **有条件可继续** | 备份数据；不要把当前结果视为持久化/重启可靠性证明；优先停用或修复危险附件 GC |
| 内部多人试用、保留真实会话和附件 | **不建议** | 至少修完 P0 和数据一致性类 P1，并完成故障注入测试 |
| 非回环绑定或远程访问 | **不允许作为生产方案** | 修复 SSRF/沙箱/日志问题，并完成 `todo.md` 中 fail-closed 鉴权和限流门禁 |
| 正式发布 | **不通过** | P0 清零；P1 有代码修复、回归测试和全生命周期复验；CI/依赖可复现 |

## 2. 范围、排除项与方法

### 2.1 纳入范围

- 后端运行时：`core/`、`Module/`、`app.py`、`paths.py`。
- Electron 后端契约事实源：`frontend_contract/`。
- 配置、部署与工程：`config.yaml`、`docker-compose.yml`、`requirements.txt`、`Makefile`、`.github/workflows/`、`scripts/`。
- 当前活跃 README、测试指南、协议和安全文档，以及 `todo.md`。
- 按排除规则统计，共 683 个 Git 跟踪文件，其中 535 个 Python 文件。

### 2.2 明确排除

- 项目自带的 v1 Vue 前端：`frontend/` 整体。
- `docs/archive/`、`docs/legacy/`、`docs/review/`、`docs/superpowers/`、`docs/completed/`。

因此，本报告不拿历史 review 结论充当当前证据，也不保证与被排除的历史报告完全去重；所有结论都以当前基线源码、活跃文档和本次复现为准。

### 2.3 审查方法

- 分区阅读运行时、输入/存储/安全、模块/契约/工程化代码。
- 执行项目已有 lint、类型、契约、配置和测试门禁。
- 对高风险结论编写临时最小复现；临时数据不写入仓库。
- 检查失败语义、并发时序、离线恢复、重启、删除、权限边界和资源上限，而不只检查 happy path。

严重度定义：

- **P0**：当前即可造成不可恢复数据破坏，或在常见部署条件下直接暴露宿主凭据；应立即止血。
- **P1**：会丢消息/跨会话污染/绕过安全边界/破坏核心生命周期；进入真实数据试用前必须修复。
- **P2**：显著的可靠性、契约、资源或工程化缺口；应在 beta/产品化前处理。
- **P3**：维护性、文档和门禁覆盖问题；不单独阻断联调，但会放大回归概率。

## 3. 自动化检查与复现摘要

| 检查 | 结果 | 说明 |
|---|---|---|
| `ruff check core/ Module/ frontend_contract/` | 通过 | 项目当前配置主要检查 `TID252/ANN`；不是完整安全或复杂度检查 |
| `make pyright` | 0 error，1 warning | `core/monitoring/__init__.py` 的 `__all__` 声明了未导出的 `IdleMonitor` |
| `make check-types` | 通过 | 当前生成契约无漂移 |
| `python -m frontend_contract --check` | 通过 | 证明生成物与当前契约生成器一致 |
| `python scripts/gen_contract_frames.py --check` | 通过 | 只证明 frame 与生成器自洽；未证明与真实 desktop tool schema 语义一致 |
| `python app.py --check` | 13/13 通过 | 启动前配置检查通过 |
| `docker compose config --quiet` | 通过 | Compose 语法通过，不代表网络暴露安全 |
| `bash -n scripts/*.sh` | 通过 | Shell 语法通过 |
| 当前 YAML 解析 | 14/14 通过 | 仅语法层检查 |
| `pip check` | 通过 | 当前环境无已安装依赖冲突 |
| `pytest --collect-only -q` | 收集 3290 个测试 | 活跃文档仍写 3186 |
| 核心运行时定向测试 | 519 passed，1 warning | 另有并发/故障注入最小复现 |
| 全量 `pytest -q` | **3286 passed，2 skipped，2 failed，15 warnings** | 26m17s；两个失败均为 WorkflowEngine 真实 LLM 集成测试等待首个 mailbox 事件超时 |
| Bandit / `pip-audit` | 未执行 | 当前环境未安装，不能据此声称安全扫描通过 |

本次已稳定复现的关键行为包括：

- 101 个有效附件文件经过 GC 后只剩 100 个，第 101 个被计为 orphan 并删除。
- Mongo repo 对象存在但未连接时，session save 返回 `True`，却没有 Mongo 记录和 JSON fallback 文件。
- 同批两条已接受消息只有第二条执行，且 session `work_count` 最终残留 1。
- 删除 session 后，先前未追踪的延迟 archive append 能重新写入孤儿消息。
- 文件工具能读取允许目录的同名前缀 sibling；目录遍历还能跟随 symlink 泄漏外部条目。
- 两个并发写工具只批准 A，A 的内容最终可写进 B 的未批准路径。
- `http://localhost:...` 可通过当前 URL 校验；HTTP redirect 没有逐跳重新校验。
- 搜索 `.` 会广泛匹配，搜索 `[` 会触发正则错误；工具日志可直接包含 API token 和聊天正文。

全量测试的两个失败分别是：

- `Module/workflow_engine/tests/integration/test_e2e_mvp.py::test_e2e_cancel`：`mailbox.get()` 等待首事件 60 秒超时。
- `Module/workflow_engine/tests/integration/test_workflow_engine_lifecycle.py::test_pause_sets_flag_and_cleanly_stops`：等待首事件 30 秒超时。

两者都因为当前环境存在 `DEEPSEEK_API_KEY` 而执行真实外部 LLM 流程；失败表现同时受外部延迟和 Workflow 首个 progress 事件产生时机影响。本次不把它们直接等同于某个已定位的纯代码根因，但它们明确说明：全量套件当前不是稳定、隔离、可复现的 release gate。15 条 warning 中还包含多处 `coroutine was never awaited`，测试 mock/异步清理也需要单独收敛。

## 4. P0：立即止血

### P0-01 附件 GC 会删除第 101 条以后仍有效的文件

**状态：新发现；已独立复现。**

证据链：

- [`core/storage/attachment_repo.py`](../../../../core/storage/attachment_repo.py#L259) 的 `find_by_status()` 与 `find_unreferenced_older_than()` 没有传分页 limit。
- [`Module/database/documentdb/mongodb.py`](../../../../Module/database/documentdb/mongodb.py#L77) 的查询默认只返回 100 条。
- [`core/storage/attachment_gc.py`](../../../../core/storage/attachment_gc.py#L105) 用截断结果构造 `known`，随后在 [`attachment_gc.py`](../../../../core/storage/attachment_gc.py#L170) 把不在集合内的磁盘文件直接 `unlink`。
- [`core/agent/_lifecycle_persistence.py`](../../../../core/agent/_lifecycle_persistence.py#L140) 在启动阶段调度 GC。

影响：附件超过 100 条后，仍处于 active、仍有消息引用的文件也可能在每次后端启动时被永久删除。数据库元数据还在，用户看到的将是不可恢复的破图/失效附件。

修复要求：

1. 使用稳定 cursor 完整分页，或针对每个磁盘候选逐条确认数据库存在性。
2. 任一页查询失败、返回不完整或存储健康未知时，孤儿删除阶段必须 fail-closed。
3. 不要只把 `limit` 改成更大的常量，那只会把破坏阈值后移。
4. 验收覆盖 101、500+、active/deleted 混合、跨页状态变化及分页中途 DB 失败；失败场景必须零删除。

临时发布约束：修复前不应让带真实附件数据的实例执行该 GC。

### P0-02 不可信 PR 可在持久化 self-hosted runner 上读取宿主 `.env`（条件性 P0）

**状态：新发现；若仓库只允许完全可信协作者，按 P1；若接受 fork/外部或半可信 PR，按 P0。**

[`ci.yml`](../../../../.github/workflows/ci.yml#L10) 在 `pull_request` 上运行，job 使用 `self-hosted`，并在 [`ci.yml`](../../../../.github/workflows/ci.yml#L53) 一带从固定宿主路径复制/source `/home/yomu/elysia-agent/.env`。随后会运行来自 PR merge commit 的 Makefile、pytest 和 Python import。恶意 PR 无需修改 workflow 本身，只需修改测试或被导入代码即可读取宿主凭据、访问 runner 网络或污染持久工作区。

修复要求：

- PR 检查使用 GitHub-hosted 或真正一次性、无宿主秘密的隔离 runner。
- PR 测试使用假配置；需要真实凭据的集成测试移到审核后的受保护环境或手工工作流。
- 增加 workflow policy，禁止 `pull_request + self-hosted + 宿主绝对路径密钥` 组合。
- 若历史上有不可信 PR 跑过当前流程，应审计日志和 runner，并轮换可能暴露的凭据。

## 5. P1：真实数据试用或远程部署前必须修复

### P1-01 Mongo 离线时 session fallback 假成功，恢复后还可能覆盖旧上下文

[`core/storage/session_repo.py`](../../../../core/storage/session_repo.py#L133) 在 `_db is None` 时静默返回；[`core/memory/session.py`](../../../../core/memory/session.py#L240) 把“无异常”误认为 durable save 成功，因而不写 JSON fallback。最小复现得到 `save_return=True`，但 fallback 文件不存在。

此外，[`core/memory/session_manager.py`](../../../../core/memory/session_manager.py#L155) 在 `await session.load()` 前就标记 `_loaded`，并忽略失败结果；第二个并发 caller 可以看到未加载的空 session，失败后也不会自然重试。数据库稍后恢复时，空/不完整状态还可能覆盖原记录。非活动 session 的独立 pending JSON 也没有启动扫描入口。

修复与验收：repo 写入返回明确的 durable result 或抛 typed unavailable；只有数据库确认写入后才能返回成功，否则原子写 JSON。加载成功后再标记 loaded，并用 per-session in-flight future 合并并发加载；启动/恢复扫描所有 pending session。覆盖“启动离线聊天→关闭→重启”“运行中恢复”“load 失败重试”“多个离线会话”。

### P1-02 消息归档未追踪，shutdown 丢记录、删除后能回写孤儿消息

[`core/memory/session.py`](../../../../core/memory/session.py#L43) 通过 `ensure_future()` 发起 archive，只挂日志 callback，没有形成可 drain 的所有权；[`core/storage/message_repo.py`](../../../../core/storage/message_repo.py#L156) 离线时也只 warning 后返回。shutdown 会在这些任务完成前关闭 repo，而 [`core/memory/session_manager.py`](../../../../core/memory/session_manager.py#L418) 的删除无法阻止已进入的迟到 append。

已复现：append 被阻塞时先完成 `delete_by_session()`，随后释放 append，数据库重新出现 `('doomed', 'm1')` 孤儿记录。

修复与验收：按 session 登记 archive task 或使用真正有界队列；删除前停止 admission 并 drain/cancel 该 session 的归档，再执行持久删除；shutdown 在关闭 repo 前全局 drain。覆盖 slow append + shutdown、append/delete race、积压上限和 repo 离线。

### P1-03 离线删除会返回成功并在重启后复活，附件引用也可能遗留

[`core/memory/session_manager.py`](../../../../core/memory/session_manager.py#L445) 忽略 session/message 删除数量并无条件驱逐缓存；[`core/storage/session_repo.py`](../../../../core/storage/session_repo.py#L233) 与 [`core/storage/message_repo.py`](../../../../core/storage/message_repo.py#L476) 离线时返回 0；最终 [`core/input/session_routes.py`](../../../../core/input/session_routes.py#L527) 仍返回 `ok: true`。附件 release 的异常也会被吞掉。

影响：当前进程里会话看似消失，重启后却从数据库复活；附件引用未释放时，隐私数据和文件会永久遗留。

修复：使用持久 tombstone/outbox 或事务式删除；没有确认 durable delete 前不要报告成功或驱逐唯一可恢复状态。存储不可用返回 503/retryable，附件释放失败进入持久重试。覆盖部分删除、离线删除、重启和幂等重试。

### P1-04 附件消息先 ACK/入队，之后才写引用；写引用失败仍成功

[`core/input/ws_handler.py`](../../../../core/input/ws_handler.py#L716) 先 `process_message()`，在约 740 行发送 accepted ACK，随后才调用 `add_ref()`，且吞掉异常。

影响：消息已被接受和归档，但附件仍为 `ref_count=0`，稍后会被未引用清理流程删除，历史消息中的媒体永久失效。

修复：在 dispatch/ACK 前建立持久消息—附件关联，使用事务或 outbox；若采用预留引用，必须定义队列拒绝、重复请求和崩溃窗口的回滚/重放语义。多附件部分失败也必须原子或可补偿。

### P1-05 Mongo health check 假阳性，启动离线的部分服务不会恢复

session/message/token/attachment/generation 等 repo 的 `health_check()` 多数只判断 `_db is not None`，例如 [`session_repo.py`](../../../../core/storage/session_repo.py#L108) 和 [`attachment_repo.py`](../../../../core/storage/attachment_repo.py#L122)。Motor client 在实际断网后仍非 `None`。注入所有操作均报错的 fake DB 时，health check 仍返回 `True`；`/ready` 因而可能在存储已失效时继续 200。

健康循环只覆盖部分 repo；启动离线时 `TokenTracker` 还会永久绑定 `repo=None`，恢复后没有 rebind/flush。

修复：提供带短超时的真实 ping；操作失败后使 backend 状态失效并触发统一重连协调器，覆盖全部 repo 和恢复 hook。验收必须包含运行中断网 `/ready: 503 → 恢复 200`，以及启动离线后 session/token/generation/attachment/desktop 状态恢复。

### P1-06 同批突发消息会静默丢第一条，并永久泄漏 work lease

[`core/agent/agent.py`](../../../../core/agent/agent.py#L349) 在 FSM 仍为 IDLE 时允许同一 event-loop 周期的两条 v2 消息都 reserve work；第二个 handler 随后取消第一个尚未真正启动的 cognitive task。pre-start cancellation 不会进入 coroutine `finally`，所以第一条已 ACK 消息不执行，lease 也不释放。

稳定复现：只有 `m2` 执行，最终 `remaining_work_count == 1`，该 session 后续删除会持续 busy/409。

修复：把 admission、排队和 processing ownership 原子化；用户消息应排队，不应由后一 handler 取消前一 handler；task done callback 必须作为 lease 释放兜底。增加同一 tick 双消息与 pre-start cancel 测试。

### P1-07 `/restart` 对同一 Agent 实例不可重入

三个独立状态没有正确复位：

- [`core/cognitive/loop.py`](../../../../core/cognitive/loop.py#L268) 的 `_shutting_down` 在 stop 后保持 `True`，后续 `_after_response` 永久跳过。
- [`core/adapter/tool_adapter.py`](../../../../core/adapter/tool_adapter.py#L85) shutdown 清空插件，却没有把 `_started` 恢复为 `False`；再次 start 提前返回。复现插件数 `1 → 0`，但 `_started=True`。
- [`core/memory/long_term.py`](../../../../core/memory/long_term.py#L80) reconnect 时追加远端记录，不清空/去重；连续 connect 会复制同一 memory entry。

而 [`core/agent/server.py`](../../../../core/agent/server.py#L469) 的 `/restart` 正是复用同一个 Agent。结果是接口可能报告重启成功，但工具为空、反思/整合停止、长期记忆倍增。

修复：定义完整 lifecycle reset/rebuild；对同一实例做连续多次 `start → stop → start` 集成测试，断言工具、after-response、memory、event consumer 和 repository 全部恢复且不重复。

### P1-08 ToolContext 未绑定请求 conversationId，且共享可变 context 破坏隔离

[`core/agent/agent.py`](../../../../core/agent/agent.py#L173) 构造 ToolContext 时 `session_id` 是空串；[`core/agent/_lifecycle_tools.py`](../../../../core/agent/_lifecycle_tools.py#L67) 注入的是启动时 session。每轮 cognitive session 切换只更新 engine/trace，没有创建与请求 conversationId 对应的 context。

影响：非默认会话的文件、runtime config、安全路径解析可能落到 default 或旧 session，造成跨会话状态污染。并发调用还共享 `services` 字典，是下一项审批路径串线的根因之一。

修复：每次 tool invocation 构造不可变 context snapshot，显式携带目标 session ID、对应 SessionMemory、call ID 和 trace；禁止调用间修改全局 context。验收两个会话并发调用、session 切换和 restart。

### P1-09 HITL 文件审批与真实写入路径可并发串线

安全层在等待审批前把规范化路径写入共享 `ToolContext.services['_safe_path']`（[`Module/tool/safety.py`](../../../../Module/tool/safety.py#L400)）；真正写文件时 [`core/tools/filesystem/plugin.py`](../../../../core/tools/filesystem/plugin.py#L231) 再从共享位置取值。另一个并发调用可以在 A 等待审批时覆盖该值。

稳定复现：A 请求写 `a.txt`，B 请求写 `b.txt`，只批准 A；最终 A 的内容写进未经批准的 `b.txt`，`a.txt` 不存在。

修复：规范化参数必须是每次调用的不可变结果，不得经共享 service 传递；审批 token/hash 应绑定完整规范化参数、目标 session 和 call ID。增加乱序审批、只批准一个、审批过期和并发写测试。

### P1-10 after-response 捕获可变 CharacterEngine，能把 A 会话写进 B reflector

[`core/cognitive/loop.py`](../../../../core/cognitive/loop.py#L998) 创建延迟 `_after_response` 时捕获的是可变 CharacterEngine 单例。等待 consolidation 期间切换 session 会重新 `attach_session()`；旧 A 任务恢复后，`buffer_message()` 使用的是 B 的 reflector，并再次创建未追踪 task。

修复：after-response 必须显式持有目标 session/reflector 的不可变引用，并纳入统一 task ownership；测试在 A tail task 阻塞时切换 B，最终数据只能落 A。

### P1-11 MemoryConsolidator 并发 clear 会丢掉未参与本轮提取的新消息

[`core/memory/consolidator.py`](../../../../core/memory/consolidator.py#L196) 在锁外 append。第一轮等待 LLM 时，第二轮追加后因锁已占用直接返回；第一轮恢复后对 live buffer 整体 `clear()`，把第二轮新消息一并删除。存储失败发生在 clear 之后时也不会恢复 batch，Merge/Update 指向未知 ID 的失败结果还被忽略。

修复：锁内原子 detach 当前 batch，让新消息进入新 buffer；LLM/存储失败必须 requeue；校验 target ID 和 update result。覆盖并发 append、远端写失败及未知 target ID。

### P1-12 长期记忆离线写、同步和删除会丢数据或复活旧数据

[`core/storage/memory_repo.py`](../../../../core/storage/memory_repo.py#L82) 离线 insert 返回空 PK；[`core/memory/long_term.py`](../../../../core/memory/long_term.py#L155) 因 repo 对象存在而不标 dirty。`sync_dirty_entries()` 对空 PK 仍移除 dirty 并计成功。离线 delete/update 又缺少持久 tombstone，重连/重启会复活旧行，sync 还未统一使用 `_write_lock`。

修复：无 PK 永远不能视为成功；dirty queue/tombstone 必须持久化；健康时只要有 dirty 就重试；所有同步写用版本/同一锁防止 ghost write。覆盖首次健康轮询恢复、离线删除后重启、空 PK 和 sync/delete race。

### P1-13 shutdown 与工具任务取消不完整，副作用可在停止后继续

[`core/agent/lifecycle.py`](../../../../core/agent/lifecycle.py#L51) 先 flush after-response/consolidator/reflector，之后才停止 event consumer 和 cognitive task；当前 turn 可在 flush 以后又创建 tail task。cognitive task 只 cancel、不 await。

同时，[`Module/tool/manager.py`](../../../../Module/tool/manager.py#L159) 以工具名作为 `_tasks` 唯一 key；两个同名调用会互相覆盖，`cancel_all()` 只能取消其中一个，较早任务完成时还可能误删较新任务的 tracking。

修复顺序：先关闭 admission/consumer，再 cancel 并 await generation 和所有 call-ID 级工具任务，再 drain tail/archive task，最后关闭持久层。覆盖同名工具乱序完成、shutdown 中网络/文件工具、正在生成时停止。

### P1-14 文件工具沙箱可通过同名前缀目录和 symlink 逃逸

[`Module/tool/safety.py`](../../../../Module/tool/safety.py#L243) 使用字符串 `startswith()` 判断目录归属；允许 `/sandbox` 时，`/sandbox_evil/secret` 同样通过。真实 ReadFile pipeline 已复现读取 sibling secret。另据 [`core/tools/filesystem/plugin.py`](../../../../core/tools/filesystem/plugin.py#L141)，ListDir 即使配置 `allow_symlinks=False`，递归遍历仍会跟随 symlink directory 并列出外部内容。

修复：使用解析后 `Path.is_relative_to()`/`relative_to()` 或 `commonpath()`；从校验到 open 应尽量基于安全目录 fd，防 symlink TOCTOU；`allow_symlinks=False` 时跳过所有 symlink entry。覆盖 sibling、`..`、不存在目标、文件/目录 symlink 和替换竞态。

### P1-15 两条 SSRF 防护链都能经 hostname/redirect 绕过

**Web 工具：** [`Module/tool/safety.py`](../../../../Module/tool/safety.py#L169) 只可靠拒绝 URL 字面量里的私网 IP；`localhost` 与普通 DNS hostname 可通过。随后 [`core/tools/web/plugin.py`](../../../../core/tools/web/plugin.py#L230) 使用 `follow_redirects=True`，没有逐跳校验，也没有连接层 IP 固定。

**LLM provider test：** [`core/input/api_routes.py`](../../core/input/api_routes.py#L406) 只校验初始 `base_url`，且 guarded client 只在 `provider == 'custom'` 时注入；内置 provider 同样允许请求覆盖 base URL，默认 OpenAI HTTP client 会自动跟随 redirect。公开 URL 302 到 loopback/metadata 后，第二跳绕过 transport guard；usable 模式还会回显响应前 20 字符。

修复：所有用户提供的 URL 都必须使用统一 guarded transport；解析并检查全部 A/AAAA，逐连接和逐跳重定向校验，处理 IPv4-mapped IPv6、metadata、DNS rebinding，并禁止跨域携带认证头。Web 工具还应设置安全的 redirect 数量和协议集合。

### P1-16 搜索词直接进入 Mongo `$regex`，可 500、语义注入和 ReDoS

[`core/input/session_routes.py`](../../../../core/input/session_routes.py#L631) 没有限制 query 长度或语法；[`core/storage/message_repo.py`](../../../../core/storage/message_repo.py#L393) 直接把它作为 `$regex`，debug offset 路径也相同，且无 `maxTimeMS`。find 与 count 会分别执行一次恶意表达式。

实测 `.` 匹配普通文本，`[` 抛未闭合 character set；灾难性表达式可在大 collection 上消耗 Mongo CPU。

修复：若产品语义是字面搜索，至少 `re.escape()` 并限制长度；更适合使用 text/search index，同时给查询设执行超时。非法/超长输入应稳定 400，不能成为 500。

### P1-17 日志会记录工具参数、凭据和聊天正文

[`Module/tool/safety.py`](../../../../Module/tool/safety.py#L111) 直接 stringify/truncate 原始参数，并在约 365 行 INFO 记录；[`core/cognitive/tools.py`](../../../../core/cognitive/tools.py#L63) 又记录一遍。最小复现的日志包含 API token 和私聊正文。

此外，[`core/input/ws_handler.py`](../../../../core/input/ws_handler.py#L352) 格式化完整 Pydantic `ValidationError`；错误对象默认包含 invalid input，畸形 content 会进入日志，部分还会进入 ACK。该行为与活跃安全文档“日志不记录正文/凭据”的约束冲突。

修复：日志只记录 tool name、call ID、参数 key、类型/尺寸和经过 allowlist 的非敏感元数据；ValidationError 使用 `errors(include_input=False)`。对 token/password/authorization/content/path 等字段结构化脱敏，并以 caplog 验证原文绝不出现。

### P1-18 模型供应链未固定，加载时启用 `trust_remote_code=True`

[`scripts/download_model.py`](../../../../scripts/download_model.py#L48) 默认使用第三方 HF mirror，没有不可变 revision/hash，缓存存在性检查也不足；[`Module/embedding/huggingface.py`](../../../../Module/embedding/huggingface.py#L118) 从该缓存加载并启用 `trust_remote_code=True`。

影响：镜像、上游 tag 或本地 cache 被替换后，后端初始化阶段可执行非预期 Python 代码。

修复：BGE 这类已知模型默认关闭 remote code；固定 commit revision，维护 artifact manifest/hash；第三方 mirror 和自定义 remote code 只能显式 opt-in。篡改 snapshot/hash 时必须拒绝启动。

### P1-19 默认 Compose 暴露无鉴权基础设施

[`docker-compose.yml`](../../../../docker-compose.yml#L18) 把 Mongo 27017 发布到所有接口且没有认证；MinIO 使用默认 `minioadmin` 并发布 9000/9001；Milvus 发布 19530/9091，项目配置未建立有效 token 边界。

影响：在非单机防火墙环境中，攻击者可直接访问持久数据、对象存储或向量服务。Compose 语法检查通过并不能发现这一点。

修复：默认只绑定 loopback 或内部 Docker network；启用强随机凭据并通过 secret 注入；仅后端需要访问的端口不发布到宿主。文档明确开发与生产 profile，CI 校验生产 profile 不含公开数据库端口/默认密码。

## 6. P2：beta/产品化前应处理

### P2-01 Session 列表、TTL 与离线发现协议不完整

- [`core/storage/session_repo.py`](../../../../core/storage/session_repo.py#L248) 固定只取最新 200 条，manager 拿到后才做 search/cursor；第 201 条以后无法分页或搜索。
- sessions/generations/desktop calls 给 `updated_at` 建 TTL，但 [`core/schema/utils.py`](../../../../core/schema/utils.py#L28) 写的是 ISO 字符串，Mongo TTL 不会生效。
- 直接把 session 时间改成 BSON Date 又会让 Mongo 自动 TTL 绕过消息/附件级联，制造孤儿数据。
- 非活动 pending JSON 没有启动扫描；MemoryReflector 也没有生产调用把它挂到 SessionMemory，反思状态无法持久化。

修复：把 search、keyset cursor 和 `limit+1` 下推 repo；为真正临时数据增加 BSON `expires_at`；session 过期走显式级联 worker/outbox；启动扫描 pending session，并明确 reflector 的 session ownership。

### P2-02 输入大小/数量与失败反馈缺少统一上限

- v2 WS 虽有 16 MiB frame 上限，但 [`frontend_contract/ws_message.py`](../../../../frontend_contract/ws_message.py#L645) 的 content、ID 和 attachments 没有业务长度/数量限制；每个 attachment 会触发串行 DB 查询。
- session message context 的 `before/after` 只做 `int()`，允许负数和任意大数，最终进入 Mongo limit。
- [`core/input/api_routes.py`](../../core/input/api_routes.py#L1085) 的 `/api/chat` 忽略 `process_message()` 的 `False`，队列满仍返回成功。

修复：契约层和 handler 双层限制 content/ID/attachments（例如附件最多 10 个），context window 限定合理区间；队列满映射稳定 429/`QUEUE_FULL`。超限输入不能访问 DB 或进入 generation。

### P2-03 legacy/static 上传可托管同源 HTML/SVG/JS 活动内容

[`core/input/api_routes.py`](../../core/input/api_routes.py#L587) 的 `/api/images` 对未知扩展默认标为 `image/png` 却保留原扩展；desktop 上传又把未知扩展映射到允许的 `application/octet-stream`。这些目录由 [`core/input/server.py`](../../../../core/input/server.py#L521) 静态公开，FileResponse 会按实际扩展返回 MIME。

修复：迁移到签名附件系统；magic-byte 与显式扩展白名单双检，服务端生成安全扩展；下载响应设置 `Content-Disposition: attachment`、`nosniff`，最好使用隔离 origin/CSP。拒绝 HTML/SVG/JS/可执行文件和扩展—内容不一致。

### P2-04 `SERVER_AUTH_TOKEN` 未标为 secret，可被 YAML 配置接收

[`core/config/app_config.py`](../../../../core/config/app_config.py#L35) 的 `auth_token` 没有 `secret=True`，而 config loader 只跳过被标记的 secret 字段。根目录 `config.yaml` 又被 Git 跟踪，违背活跃安全文档要求 token 只经环境变量注入的规则。

修复：标记 secret；测试 YAML 值被忽略、环境变量有效，且任何配置 dump/日志不包含该值。

### P2-05 声明的文件和 Web 资源上限没有真正执行

- 写文件的 `max_file_bytes` 只检查目标文件**当前**大小，不检查即将写入的 UTF-8 字节数；32-byte 上限可成功创建 1000-byte 新文件。
- Web `max_response_bytes` 没有被使用；`client.get()` 会先缓冲完整 body 再读 `resp.text`，大或无限 chunked 响应可造成内存/CPU DoS。
- 文件 read/write/list 在 async tool 中直接做同步 I/O；ListDir 会先遍历、收集、排序整棵树，最后才按输出字符截断。

修复：审批和落盘前按编码后字节校验；HTTP 使用 streaming 并累计硬上限；文件操作放到受控线程/异步接口，遍历过程本身限制节点数、深度和耗时。

### P2-06 工具热重载仍实例化旧 class，忽略新配置且无法回滚

[`Module/tool/manager.py`](../../../../Module/tool/manager.py#L483) `importlib.reload()` 后优先读取旧 `_plugin_classes[name]`；即使模块暴露 New class，结果仍是 Old class。reload 路径不复用正常 config 流程，还先 teardown/remove 旧实例；新 setup 失败后工具永久消失。

修复：从 reload 后 module 重新 discover class，复用统一配置加载；先构造/setup 新实例，成功后原子替换，并等待旧实例 in-flight 调用排空。覆盖实现/版本/config 变化及 setup 失败回滚。

### P2-07 Workflow `submit()` 会等待 semaphore，违反 fire-and-forget 契约

[`Module/workflow_engine/workflow_engine.py`](../../../../Module/workflow_engine/workflow_engine.py#L173) 在注册 mailbox 和创建后台 task 前先等待 semaphore；PAUSED 任务继续占 slot。`max_concurrent_tasks=1` 时，首任务暂停/阻塞会让第二次 submit 无期限等待。生产调用 [`core/cognitive/tools.py`](../../../../core/cognitive/tools.py#L275) 又同步 await 它，所以整个当前聊天轮次会卡住。

修复：把 acquire 移到后台 queue runner，submit 立即返回 mailbox；或者快速返回稳定 `BUSY/QUEUE_FULL`。现有集成测试固化了阻塞行为，需要先确认产品契约再修改。

### P2-08 Workflow planning 期间 cancel 会被后续代码改回 RUNNING

[`Module/workflow_engine/lifecycle.py`](../../../../Module/workflow_engine/lifecycle.py#L157) 只写 CANCELLED，没有取消 planner future；[`planning.py`](../../../../Module/workflow_engine/core/sub_agent/planning.py#L111) 返回后无条件写 RUNNING 并继续执行。慢 planner 复现中，状态从 cancelled 恢复为 running。

这是 [`todo.md`](../../../../todo.md#L103) 已知 M20 的增强确认。修复应使用真实 task cancellation 或 cancellation token，在每个 await 后检查，且终态不可逆。测试必须断言取消后不再消耗 token/执行有副作用工具。

### P2-09 其它 lifecycle/checkpoint 恢复语义不完整

- 正常 stop 不 drain EventBus；restart 可能重放旧 UserInput/SystemTick，而对应 lease 已被清除。
- TaskScheduler 在 pending 为空时不覆盖旧 checkpoint，restore 后也不消费；background payload 包含 coroutine，无法 JSON 序列化。
- 单个 repository/service `close()` 抛普通异常会中断同一步后续资源清理。
- IdleMonitor 产生 `time_of_day` 事件，Agent 只处理 `silence_timeout`，其余静默丢弃。

修复方向：为生命周期引入 epoch 和明确 task/resource ownership；checkpoint 只保存可重建描述并在成功 restore 后 consume；逐资源独立 close 或 `gather(return_exceptions=True)`；未知 proactive kind 至少告警。

### P2-10 Milvus bulk insert 和 metric 恢复会静默改变数据语义

[`Module/database/vectordb/milvus.py`](../../../../Module/database/vectordb/milvus.py#L123) 用 `zip(vectors, metadata)`，数量不等时静默截断较长一侧。metric 又只保存在进程内 `_metric_types`；新进程 `load_collection()` 后查不到就默认 L2，原 COSINE/IP collection 的排序语义被改变。

修复：insert 前验证非空、数量相等、维度一致；从 collection/index metadata 恢复 metric，或把它作为必须持久化/显式传入的 schema。增加跨实例重载 COSINE 的真实测试。

### P2-11 CI 没有执行 WorkflowEngine 测试，`test-all` 也不是全量

[`Makefile`](../../../../Makefile#L21) 让 quick/normal 永久 `--ignore=Module/workflow_engine/tests`；CI 只运行这两个 target。本次收集到约 299 个 WorkflowEngine/contract 测试，没有进入合并门禁。`make test-all` 也只列出 `Module/ core/tests/ core/tools/`，遗漏若干 `core/*/tests`、`frontend_contract/tests` 和 `scripts/tests`。

修复：增加必需的 workflow unit/service job；耗时 integration 可分层，但核心取消、暂停、submit 生命周期必须 merge-gating。把 target 改成准确名称，或由统一排除清单生成真正全量命令。

本次真正的全量运行得到 `3286 passed, 2 skipped, 2 failed, 15 warnings`。两个失败用例因宿主存在真实 API key 而进入外部 LLM 路径，并在首个 mailbox 事件处超时。这类测试应明确标为 opt-in external E2E、使用受控凭据和总成本/时限；merge gate 另用 fake LLM 验证完全确定的 cancel/pause 事件语义。未 await coroutine warning 也应升级为测试失败或至少独立清零门禁。

### P2-12 依赖和 CI 工具链不可复现

[`requirements.txt`](../../../../requirements.txt) 主要只有下限，无 lock/hash，也未完整声明 pytest/ruff/pyright 等开发工具；CI 直接复用宿主已有 conda `Elysia` 环境而不从干净状态安装。

影响：持久 runner 可通过而新机器安装失败；未来 major 依赖升级可无预警改变行为。

修复：用 uv/pip-tools/conda-lock 固定 runtime/dev，CI 在一次性环境 clean install；基础镜像固定版本或 digest，并增加 clean-room build/smoke test。

### P2-13 官方 backend setup/start 强制构建被排除的 v1 前端

[`scripts/setup.sh`](../../../../scripts/setup.sh#L77) 与 [`scripts/start.sh`](../../../../scripts/start.sh#L56) 要求 Node/npm 并执行旧前端 install/build；无 npm 就退出。preflight 还被 `|| true` 和 stderr 重定向吞掉。

影响：Electron/backend-only 部署无法使用官方脚本，启动过程会修改旧 UI 依赖状态，错误配置也可能继续运行。

修复：backend-only 成为默认或明确 flag；legacy UI 只在独立 packaging target 中用 `npm ci` 构建；preflight 默认失败退出，跳过必须显式 opt-in。增加无 Node 的 backend smoke test。

### P2-14 Desktop golden frame 与真实 runtime/protocol 不一致

[`scripts/gen_contract_frames.py`](../../../../scripts/gen_contract_frames.py#L224) 生成的 desktop read 参数含 `offset/limit`，真实 [`core/tools/desktop/plugin.py`](../../../../core/tools/desktop/plugin.py#L29) 只支持 `path`；golden 风险为 high，而 runtime/协议为 medium；示例还使用假的 `sha256_frame_demo`，不是规范化参数真实 SHA-256。

`--check` 仍通过，因为它只验证生成文件与生成器一致。修复应直接从 runtime 参数模型、registry metadata 和真实 hash 算法生成，并加入 golden 与 runtime schema 的语义等价测试。

## 7. P3：维护性与文档门禁

### P3-01 默认静态门禁覆盖窄，scripts 已有未纳入错误

默认 Ruff 规则范围较窄；把 `scripts/` 纳入同一规则后出现 17 个 ANN 问题。默认 Pyright 对生产目标是 0 error/1 warning，但扩到 scripts 后，`scripts/capture_ws_trace.py` 出现 5 个 error 和若干 missing-source warning。

建议把 scripts 纳入 lint/type，动态脚本仅做窄范围 suppression；同时修正 `core/monitoring/__init__.py` 的无效 `__all__` export。

### P3-02 高复杂度入口缺少结构性保护

扩展 Ruff `C901` 共发现 79 个高复杂度函数（含测试）。生产热点包括：

- `core/input/api_routes.py::register_api_routes`：复杂度约 124。
- `core/input/ws_handler.py::handle_json_message`：约 92。
- `core/input/ws_handler.py::handle_ws`：约 43。

这些入口同时承担鉴权、契约解析、幂等、附件、路由和错误映射，正是本次多项边界问题聚集处。建议按协议动作拆 handler/service，并对安全关键纯函数建立表驱动测试；无需一次性追求全仓复杂度清零。

### P3-03 活跃测试文档和 README 已漂移

当前实际收集 3290 tests，而 [`docs/testing.md`](../../../testing.md#L5) 与 README 后段仍写 3186；README 顶部已有 3290。协议/文档中的错误目录数也存在 40/41 的不一致。

建议去掉易漂移的手写统计，或由收集脚本/契约源自动生成并在 CI 校验。

## 8. 已知产品化门禁（本次确认仍未关闭）

以下不是本次首次发现，已经记录在 [`todo.md`](../../../../todo.md#L45)，但它们会改变“是否可远程对接”的结论，不能因已有记录而从发布判断中删除：

1. 非回环绑定且无 token 时只 warning，未 fail-closed。
2. `clientRequestId` 幂等状态仅在当前进程内，重启后的 ACK 丢失重试可能重复生成。
3. WS/REST/upload 没有实际应用层 rate limiter，尽管契约登记了 `RATE_LIMITED`。
4. Workflow M19/M20 尚未全部闭合；本次已再次复现 planning cancel 终态逆转。

这些项目与本次新发现的 SSRF、Compose 暴露和输入资源上限组合后，意味着当前版本只能按本机/受控开发服务看待。

## 9. 建议修复顺序

### Batch A：立即止血

1. 修复或临时禁止附件 GC 的 orphan unlink；先为现有附件目录做可恢复备份。
2. 若 CI 接受任何不完全可信 PR，立即停止 self-hosted PR job，审计/轮换凭据。
3. 为上述两项增加独立 merge-gating 测试/策略，避免修复只存在于运行约定。

### Batch B：持久化一致性

1. 统一 repository 的成功/离线/部分失败返回协议，禁止 silent return 被解释为成功。
2. 修 session fallback/load/pending import、archive task ownership、删除 tombstone/outbox、附件引用顺序和真实 health/recovery。
3. 修 consolidator/LTM 的 batch、dirty、tombstone 与并发锁。
4. 做完整 fault-injection：启动离线、运行中断网、恢复、shutdown、restart、delete/append race。

### Batch C：工具与安全边界

1. 改成 per-call immutable ToolContext，先同时解决 conversation 隔离和 `_safe_path` 串线。
2. 修文件 containment/symlink、两个 SSRF 入口、原始参数日志和资源 byte/node/time 上限。
3. 修同名任务跟踪和 shutdown drain。
4. 收紧 Compose 网络与 secret；固定 embedding artifact 并关闭默认 remote code。

### Batch D：生命周期与工程化

1. 完成同一 Agent 多轮 restart 测试；重排 shutdown 顺序。
2. 修 Workflow submit/cancel、EventBus/checkpoint 和 vector metric 恢复。
3. 把 WorkflowEngine、scripts 和真正全量测试加入 CI。
4. 锁定 runtime/dev 依赖，提供 backend-only setup/start。

## 10. 建议的发布验收清单

修复完成后，不应只回跑现有 happy-path 测试。至少要求：

- 500+ 附件跨页 GC；DB 任意页失败时零物理删除。
- Mongo 启动离线、运行中断网、自动恢复、进程重启，session/message/token/attachment/generation 均不丢、不复活。
- 同一 tick 双消息、消息归档与删除竞态、shutdown 正在生成、连续三次 `/restart`。
- A/B 两会话并发工具调用；A/B 两个同名写调用乱序审批/取消；副作用与 call/session 精确绑定。
- 文件 sibling/symlink/TOCTOU；Web/LLM localhost、IPv4/IPv6、metadata、redirect、DNS rebinding。
- 日志与错误帧中不出现聊天正文、token、authorization、完整路径或原始非法输入。
- Workflow 暂停占满 slot 时第二次 submit 快速返回；planning cancel 后终态永不逆转。
- clean-room 安装、无 Node backend 启动、PR runner 无宿主 secret、CI 执行 workflow 核心测试。
- Electron 使用发布的 raw contract/golden 做跨仓 replay；desktop golden 与 runtime schema/hash 做语义校验。

## 11. 审查限制

- 按用户要求未审查 v1 Vue 前端及列出的历史/归档文档目录。
- 未连接真实生产 Mongo/Milvus/MinIO，也未对外部 provider 发起真实攻击流量；数据库/网络结论来自源码、mock/fake 和本机最小复现。
- 当前环境没有 Bandit 和 `pip-audit`，所以本报告不是第三方依赖 CVE 清单，也不代表供应链扫描通过。
- 复杂度数字来自扩展静态检查，仅用于定位重构热点，不等同于功能缺陷数量。

总体建议：先把本报告视为后端修复 backlog 的事实清单。P0 修复完成前避免让现有 GC 处理真实附件；P1 数据一致性与工具边界完成前，不要把“契约检查/现有测试全绿”解释为生产接入充分。
