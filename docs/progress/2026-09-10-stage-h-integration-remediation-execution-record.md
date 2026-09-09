# 阶段 H 真实联调后端整改执行记录与交付（2026-09-10）

> 文档导航：[项目文档](../../README.md) → [docs 文档索引](../README.md) → [多角色 Stage H 真实联调后端整改与复验清单](../review/2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md)

- **后端基线：** `main@65295a90` → **`main@d394f931`**（第一轮 6 个实现/决策提交）→ **`main@9bca4246`**（第二轮 4 项遗留整改 + 类型/文档/测试修正，见 §5）→ **复核遗留 P0-R1/P0-R2**（`ac5ad2ea` / `0c5c45c5`，见 §6）
- **实现范围：** 清单 §1.2 全部 6 项（BE-STAGEH-01 ~ 06）+ 第二轮 §1.4 的 P0-1/P0-2/P0-3/P1 + 复核 §1.5 的 P0-R1/P0-R2
- **门禁：** ruff ✅ / `make check-types` ✅ 0 漂移 / `make pyright` ✅ 0 错误 / `make test-normal` ✅ **4099 passed, 2 skipped**（第一轮）→ 第二轮见 §5.8 / `make test-quick` ✅
- **契约：** `schemaHash` **未变化**（两轮合计 0 个 `frontend_contract/` 或 `frontend/src/types/` 文件改动）

## 1. 交付清单

| 任务 | 结论 | 提交 |
|---|---|---|
| BE-STAGEH-01 generation/reset 并发竞态 | ✅ 已修复 | `c4986eac` |
| BE-STAGEH-02 UserIdentity 持久化 | ✅ 已修复 | `92b1679f` |
| BE-STAGEH-04 保存校验错误契约 | ✅ 已修复 | `68f6f78c` |
| BE-STAGEH-03 无状态 Character Preview | ✅ 已实现 | `21c6d194` |
| BE-STAGEH-05 pinned 真实夹具 | ✅ 已实现（联调待执行 seed） | `62ad9ee2` |
| BE-STAGEH-06 单用户范围决策 | ✅ 已决策（文档） | `d394f931` |

**仍未关闭：** 清单 §11 复验矩阵的**真实 Windows Electron 复验**——本批全部证据来自后端定向测试与仓库门禁，**没有真机复验**。01/02 的「关闭」指后端缺陷已修复且被测试锁定，不等于前端复验通过。

## 2. 逐项交付内容（对应清单 §10）

### 2.1 UserIdentity：collection / index / 重启持久化

- **集合：** `default.user_identity_profiles`，**索引：** `user_id` unique。
- **写入语义：** 首次空库 `$setOnInsert` 原子 bootstrap（并发启动只有一个 winner）；更新走 `revision` CAS（`$set` 同一原子操作内递增 revision + 刷新 updated_at）。
- **重启持久化结果：** `core/agent/tests/test_user_identity_persistence.py` 用同一份假 Mongo 构造 Agent A（`PUT /api/me` 等价更新到 revision 2）→ 关闭 → Agent B 重新 start → `get()` 返回 `displayName=妖梦 / preferredAddress=主人 / pronouns=她 / initialized=true / revision=2`，**不回到 revision 1**；重复启动不覆盖已有档案。
- **fail-closed：** repo 已装配但未连接时 `update_if_revision` 抛 `StorageUnavailableError` → 路由 503 `STORAGE_UNAVAILABLE`（retryable），不写本地假象。

### 2.2 reset generation-busy 的 409 实测摘要

- **领域层：** `CharacterRuntime` 新增同步 operation lease（`begin/end_generation` + `try_begin_destructive_mutation`），busy 判定与登记同处一把同步锁内（不变量 #3）。
- **route 层：** `POST /api/character-instances/{id}/reset-evolution` 加 `_character_mutation_guard` 第一道快速拒绝。
- **测试证据：**
  - `test_reset_evolution_route_rejects_busy_turn` → **HTTP 409，`code=CHARACTER_BUSY`，`retryable=true`**；
  - `test_reset_evolution_rejects_generation_busy_runtime` → 抛 `CharacterBusyError` 且 persist 未调用、内存与 `stateVersion` 不变；
  - `test_process_holds_generation_lease_until_turn_finishes` / `..._after_turn_exception` / `..._after_cancel` → lease 覆盖整个回合，异常/取消路径由 `finally` 释放；
  - `test_concurrent_resets_same_version_only_one_wins` → 同 expectedStateVersion 两个 reset：一个成功、一个 `CHARACTER_STATE_CONFLICT`，`stateVersion` 只 +1。

### 2.3 preview：成功响应与零副作用

- **成功响应（契约冻结，`usage` 为 None——预览用量不并入任何会话统计）：**
  ```json
  {"ephemeral": true, "reply": "你好呀♪", "model": "deepseek/deepseek-chat", "usage": null}
  ```
- **零副作用计数摘要：** `test_preview_has_zero_persistence_side_effects` 断言 preview 前后 `characters` 与 `character_revisions` 集合**均为空**（不建 definition/revision；`_compile_preview_profile` 只造内存对象、不建 instance）。
- **无会话/无记忆：** `test_prompt_adapter_builds_stateless_prompt_without_tools` 断言 messages 只有 `system + user`（无历史），`kwargs == {}`（不挂工具）；`test_preview_mode_skips_session_history` 断言 `recent_messages` **未被调用**。
- **错误：** 无效 draft → `CHARACTER_VALIDATION` + `details.issues`（不调用推理口）；无口/上游失败/空回复 → `CHARACTER_PREVIEW_FAILED`（503，retryable）。

### 2.4 validation 保存错误的公开 JSON

```json
{
  "code": "CHARACTER_VALIDATION",
  "message": "角色内容校验失败",
  "retryable": false,
  "details": {
    "issues": [
      {"severity": "error", "code": "CHARACTER_RESERVED_CONTENT",
       "path": "/promptProfile/corePersonality", "message": "包含保留模板结构或标签"}
    ]
  }
}
```
顶层 code 恒为 `CHARACTER_VALIDATION`（不再按 issue code 改写）；多 issue 不改写顶层；响应字段白名单断言不含 draft 文本/绝对路径/traceback。

### 2.5 pinned fixture 执行 / 清理命令

```bash
# 创建（须显式 opt-in；Mongo 主机须为回环/内网）
STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/seed_stage_h_pinned.py

# 清理（只删固定 fixture ID 的新数据）
STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/seed_stage_h_pinned.py --cleanup
```
输出为脱敏 JSON（`characterId` / `instanceId` / `conversationId` / `revisionPolicy` / `revision`），含 pinned 与 follow_latest 各一条 instance + 各自 session binding。**本批未在真实库执行**——真机复验时执行。

### 2.6 H-83 决策

**选择「单用户 N/A — V1」**（非「另立多用户项目」）。已同步：架构稿 §13.3 + §11.7 + 状态快照、`docs/electron-backend-security.md` §1.5、复验清单 §7.2/§11。不增加第二账号测试 token 夹具，不删除 repository 已有 owner 字段与查询能力。

### 2.7 schemaHash

**未变化。** 本批未改动 `frontend_contract/` 与 `frontend/src/types/`（`git diff --name-only 65295a90..HEAD` 对这两个路径为 0 文件）；`make check-types`（含 17 个 golden frames `--check` + 41 错误码单源校验）通过。

## 3. 决策记录（本批自主裁定）

| # | 决策 | 理由 | 代价（若判断有误） |
|---|---|---|---|
| R1 | 删除旧测试 `test_reset_evolution_rejects_busy_runtime_before_mutation` | 它用手工持有 `runtime.lock` 代理 busy；lease 落地后 reset 在 `runtime.lock` 上串行化（并发 reset 由 CAS 判冲突，清单 §2.5 race 测试要求），该代理前提失效且会死锁 | 少一条「锁被持有即拒绝」的回归；已由 generation lease 测试覆盖同一不变量 |
| R2 | 预览 `usage` 返回 None | `LLMAdapter.think` 不暴露 token 用量；清单要求「用量若记录必须标识 purpose 且不并入会话统计」，不记录即满足 | 前端若强依赖 usage 字段需回退（契约声明 Optional） |
| R3 | generation 登记保持无条件（不因 reset 在途而拒绝） | 生产路径 reset 路由持 `_process_lock`，generation 无法并发进入；且 `runtime.state` 为共享对象 + persist 走 CAS，反向竞态良性 | 领域层理论上可构造 generation 与 reset 交错；已记录 |
| R4 | 新增两个 repo 删除方法（`delete_definition` / `delete`） | 清单 §6.2 要求 cleanup 只删 fixture 数据；无删除口则无法清理 | 多两个生产 API 面（已注明仅 fixture 使用） |
| R5 | `database-storage.md` 集合清单顺带补齐 characters/character_revisions/character_instances | 文档长期停留在 9 集合（实际 12），本次新增集合若不补齐会加剧漂移 | 无 |

## 4. 未完成 / 待人工验收

- **真实 Windows Electron 复验**：§11 矩阵 8 个场景（01/02/04/03/05/06 的成功与失败支路）——需真机。
- **pinned seed 的真实库执行**（§2.5 命令），随后按 §6.3 做 revision 2 对照。
- **阶段 8 发布、任务中心、Neo4j、前端 UI 改版、多用户产品化**：均按原边界排除。

---

## 5. 第二轮遗留整改（P0-1 / P0-2 / P0-3 / P1）

第一轮关闭了「reset 在 generation 主回合期间被接受」等缺口，但复验后仍有 4 项结构性遗留。本节是第二轮交付。

### 5.1 交付清单

| 任务 | 结论 | 提交 |
|---|---|---|
| P0-1 generation tail lease | ✅ 已实现 | `15a7c4ab` |
| P0-2 身份进入普通聊天 Prompt | ✅ 已实现 | `2a581267` |
| P0-3 身份 fail-closed + 恢复协调器 | ✅ 已实现 | `7128b85d` |
| P1 默认角色归档一致性 | ✅ 已实现 | `ab3b80b4` |

**仍未关闭：** §11 复验矩阵的真实 Windows Electron 复验（含新增第 9~12 行）与 pinned seed 的真实库执行。

### 5.2 P0-1：generation tail lease 并发测试摘要

**根因：** `process()` 返回即 `end_generation()`，而 `_finalize_turn` 用 `create_task` 启动的
`_after_response`（consolidator → CharacterReflector → MemoryReflector → session.save）仍在后台；
reset 可在后台 task 真正取得 `_reflect_lock` 之前插入。

**实现：**
- `CharacterRuntime.begin_generation()` → **`try_begin_generation()`**：破坏性变更在途时拒绝开始
  generation（对称互斥，不再只靠 route 的 `_process_lock`）；
- 新增 `begin_generation_tail()`：`_archive_final_turn` 在 `create_task` **之前**同步取一份 lease，
  tail task 在 `finally` 中释放；`create_task` 抛错路径同样释放；
- tail task 额外 `await` `engine.buffer_message` 返回的反思 task——闭合「task 已创建、尚未持锁」的窗口；
- `_handle_turn_exception` 新增映射：`CharacterBusyError` → `CHARACTER_BUSY`(retryable)、
  `StorageUnavailableError` → `STORAGE_UNAVAILABLE`(retryable)。

**证据（`core/cognitive/tests/test_generation_tail_lease.py`，6 条）：**

| 场景 | 断言 |
|---|---|
| consolidator 被 `asyncio.Event` 卡住 | 主回合 lease 已释放 + `generation.completed` 已发出，`try_begin_destructive_mutation()` 仍为 False（=409） |
| 放开 gate | tail 结束 → lease 释放 → reset 可进入；`session.save` 已 await |
| `_after_response` 抛异常 | lease 仍被 `finally` 释放 |
| tail task 被 cancel（shutdown/flush 超时） | lease 释放 |
| `mark_shutting_down()` 后收尾 | 立即释放，consolidator 未被调用 |
| destructive mutation 在途调 `process()` | 不进入 generation，`_handle_turn_exception` 收到 `CharacterBusyError` |

runtime 级语义见 `core/character/tests/test_runtime_lease.py`（tail lease 维持 busy、per-instance 隔离、双向互斥）。

### 5.3 P0-2：身份进入 Prompt 的证据

- `TurnCharacterContext` 新增 `userId / userDisplayName / userPreferredAddress / userPronouns /
  userLocale / userTimezone / userIdentityRevision`（**后端内部字段，wire contract 未改**）；
  `effectiveUserAddress` = `preferredAddress` 非空优先 → `displayName` → 中性「用户」。
- `_prepare_turn` 每轮从权威 store 读一次快照并冻结；有效称呼经 `runtime.apply_user_address()`
  投影到 consolidator / MemoryReflector / CharacterReflector（**不改 `runtime.user_identity` 对象**）。
- `PromptAssembler`：快照存在时称呼/代词/时区一律取自快照（调用方传参无法绕过）；C5 感官时间
  按快照时区渲染（`ZoneInfo`，未知时区/缺 tzdata 回退本地时间；`requirements.txt` 补 `tzdata`）。

**重启后 Prompt 证据（`core/agent/tests/test_user_identity_persistence.py::
test_restarted_agent_uses_persisted_identity_in_turn_prompt`）：**
Agent A 写 `displayName=妖梦 / preferredAddress=主人 / pronouns=她 / timezone=Asia/Shanghai`
（revision 2）→ 停止 → Agent B 用同一份假 Mongo 启动 → `_prepare_turn` 冻结快照
（`userIdentityRevision == 2`）→ 真实 `PromptAssembler.build_system_prompt(user_name="启动默认名")`
渲染出 **`关于 主人：`**，且 **不含** `启动默认名`。

其余锁定：`core/prompt/tests/test_identity_projection.py`（称呼优先级/空白回退/代词/时区/无上下文 override）、
`core/cognitive/tests/test_identity_snapshot.py`（完整快照、新一轮采用新 revision 而在途上下文不变、
投影到三个记忆组件、身份自由文本不进日志、无 store 回退）。

### 5.4 P0-3：Mongo 离线启动与恢复证据

- `UserIdentityStore.connect()`：权威模式装载失败 **不再降级本地** → `get()` 为 None、
  `is_available=False`；新增 `is_authoritative / placeholder() / health_check()`；
  权威模式下 `bootstrap()` 不缓存不落盘。
- `Agent`：构造期用 `placeholder()` 取固定单用户 `usr_local_default`（不伪造档案）；
  `_db_health_loop` 新增 `identity` 探针，`_recover_dependency("identity")` 在恢复边界重载权威档案
  并原子替换缓存（不覆盖 Mongo 中更高 revision）。
- `/ready`：`identity` 纳入 `chatReady` 门禁与 `dependencies`。

**证据（`core/agent/tests/test_user_identity_persistence.py`）：**

| 场景 | 断言 |
|---|---|
| Mongo 离线启动 | `store.get() is None`、`is_available is False`；`update_if_revision` 抛 `StorageUnavailableError`；`_identity_snapshot` 抛 `StorageUnavailableError`（generation fail-closed） |
| Mongo 恢复 | `_recover_dependency("identity") is True`；`get()` 返回 revision 2 / `preferredAddress=主人`；`agent._user_identity` 同步；下一轮快照用持久化值 |
| 恢复不覆盖更高 revision | 恢复装载到的 revision 等于 Mongo 中的 revision（不回到 bootstrap） |

其余锁定：`core/input/tests/test_me_identity_failclosed.py`（GET/PUT 503、本地模式仍 404、
CAS 409 保持、health_check 探针语义、preview 与会话创建 fail-closed）、
`core/input/tests/test_health.py::test_ready_identity_down_503`。

### 5.5 P1：归档一致性

`archive()` 顺序调整为**先**在有限 CAS 重试内把 `defaultCharacterId` 改到可用回退角色、**再**归档；
profile 更新失败或重试耗尽 → 整体失败，角色保持 `active`、档案未变（可恢复）。归档的不是默认角色时
不触碰 profile（`userProfile` 仍为 None）。

证据见 `core/character/tests/test_archive_consistency.py`（6 条）：顺序证据
`events == ["retarget:2", "archive"]`、存储不可用 fail-closed（角色仍 active）、
CAS 冲突重试成功、重试耗尽 409 且未归档、Mongo 权威重启后仍指向可用角色、非默认角色不受影响。

### 5.6 决策记录（第二轮）

| # | 决策 | 理由 | 代价（若判断有误） |
|---|---|---|---|
| R6 | 用 tail lease（同一计数器再取一份）而非在 `process()` 内 await 收尾 | 收尾必须保持 fire-and-forget（否则 TTS/回合边界被记忆沉淀阻塞）；tail lease 让 busy 覆盖到收尾结束而不改延迟 | 收尾较长时 reset 持续 409——语义上正确（有真实状态写入在途） |
| R7 | tail task await 反思 task | `evolution_locks_busy()` 只看锁，看不到「已创建未启动」的 task，仍留窗口 | 收尾时长增加一次 LLM 反思的时间 |
| R8 | 用户身份按回合投影到记忆组件（`set_user_name`）而非每轮重建组件 | 组件持有跨轮缓冲/上下文；重建会丢状态。回合由 `_process_lock` 全局串行，无并发写 | 若未来放开并发回合，需改为按回合传参 |
| R9 | 重试耗尽抛 `RevisionConflictError`（409 retryable） | 语义是「读到的档案 revision 已过期」；该端点无请求体，客户端直接重试即可 | 若前端把该码解释为「角色 revision 冲突」会显示不精确的提示 |
| R10 | `/ready` 把 identity 纳入 chatReady | 身份不可用时 v2 send 不应用错误身份继续（P0-3 要求 readiness 为 false） | 身份存储抖动会让 `/ready` 短暂 503（与 mongo 门禁同性质） |

### 5.7 schemaHash

**未变化。** 两轮合计 `git diff --name-only 65295a90..HEAD -- frontend_contract/ frontend/src/types/`
为 **0 文件**；`make check-types`（17 个 golden frames `--check` + 41 错误码单源）通过。

### 5.8 第二轮门禁与运行版本（2026-09-10）

**提交（`08f21a8a` → `9bca4246`）：**

| 提交 | 内容 |
|---|---|
| `15a7c4ab` | P0-1 generation tail lease |
| `2a581267` | P0-2 身份进入 Prompt |
| `7128b85d` | P0-3 身份 fail-closed + 恢复协调器 |
| `ab3b80b4` | P1 归档一致性 |
| `31f657ac` | 归档在权威身份不可用时 fail-closed |
| `dce79156` / `9a671b29` | 类型注解 / 注释修正（pyright 0 错误） |
| `2552f5be` / `8a8a9b9c` / `9df0c794` / `9bca4246` | 文档（清单、执行记录、模块 README、安全文档、索引）+ 新测试不触真实 Mongo |

**门禁（最终树 `9bca4246`，工作树干净）：**

| 门禁 | 结果 |
|---|---|
| `make lint` | ✅ All checks passed |
| `make check-types` | ✅ 0 漂移 + 41 错误码单源一致（17 golden frames `--check`） |
| `make pyright` | ✅ 0 errors, 0 warnings |
| `make test-quick` | ✅ **3948 passed, 2 skipped**（269 deselected） |
| `make test-normal` | ✅ **4141 passed, 2 skipped**（76 deselected） |
| `app.py --check` | ✅ 13/13 passed |

**运行版本（新版本已启动，旧 `65295a90` 进程此前已停止——启动前确认 8765 无监听）：**

```json
/health: {"version": "0.1.0+9bca424", "build_sha": "9bca424667b128f595735eedd08dcaa9d71fade1", "fsm": "idle"}
/ready:  {"status": "ok", "chatReady": true, "fullReady": true,
          "dependencies": {"mongo": true, "milvus": true, "idempotency": true, "identity": true}}
```

- 绑定 `SERVER_HOST=0.0.0.0:8765`；`192.168.1.18:8765/health` → 200（前端可达）；
- `/api/me`（Bearer）返回 Mongo 权威档案 `displayName=妖梦 / revision=1`；无 token → 401；
- `build_sha` 与最终 HEAD 一致。

**待真机复验（未做，不得宣称通过）：** §11 矩阵 8 个第一轮场景 + 第 9~12 行第二轮场景；
pinned seed 的真实库执行（`STAGE_H_FIXTURES=1 … seed_stage_h_pinned.py`）。

**已知观察（本轮未修，不属四项范围）：** `log/app.log` 在 01:18 的一次 shutdown 里出现过
`close_all → rt.flush → CharacterStateConflictError: chi_builtin_elysia`（DB `state_version` 与
runtime 内存 CAS token 不一致；全日志仅 1 次）。影响面是关机时最后一次 runtime flush 被跳过
（`close_all` 逐实例 flush，首个失败即抛出）。建议后续单独排查（并发写入方或重复 runtime 实例）。

---

## 6. 第二轮复核遗留修复（P0-R1 / P0-R2，2026-09-10）

第二轮交付后复核发现两个未被定向测试覆盖的 P0 缺口。两者都不改 wire contract。

### 6.1 交付清单

| 任务 | 结论 | 提交 |
|---|---|---|
| P0-R1 tail 闭包晚读 `self._engine` | ✅ 已修复 | `ac5ad2ea` |
| P0-R2 identity 首轮探测即健康时不触发恢复 | ✅ 已修复 | `0c5c45c5` |

**仍未关闭：** 真实 Windows Electron 复验（§11 第 9~13 行）与 pinned seed 的真实库执行。

### 6.2 P0-R1：tail 闭包必须使用本回合捕获的 Engine

**根因：** `_archive_final_turn` 在 `create_task` 前捕获了 `runtime`/`target`（session），
但闭包内仍写 `engine=self._engine`——该属性在 **tail task 执行时**才被读取。后台任务被
调度后，下一轮 `_prepare_turn` 会重新赋值 `self._engine`；于是 A 角色上一轮的
`buffer_message()`/CharacterReflector 会写进 B 角色的 Engine。

**实现（`core/cognitive/finalize.py`）：** `create_task` 前急切捕获 `engine = self._engine`，
闭包只使用局部 `engine`；`_after_response(engine=None)` 回退 `self._engine` 仅保留给
直接调用/测试路径（`core/tests/test_phase3.py`）。后台执行与 tail lease 语义不变——
异常/取消/shutdown/`create_task()` 失败路径仍释放 lease 与 session work reservation。

**测试证据（`core/cognitive/tests/test_generation_tail_lease.py::
test_tail_captures_turn_engine_not_next_turn_engine`）：** A 轮用 `engine_a` 调
`_archive_final_turn()` → tail task 创建后、执行前把 `loop._engine` 换成 `engine_b` →
等待 tail 完成 → `engine_a.buffer_message` 调用 **1 次**、`engine_b.buffer_message`
**从未调用**、`is_generation_busy is False`。反向验证：临时改回 `engine=self._engine`
该测试即失败（`Expected 'buffer_message' to have been called once. Called 0 times.`）。

### 6.3 P0-R2：identity 在「首轮探测即健康」时也必须补偿装载

**根因：** 恢复触发 = False→True 边界 ∪ `_dependency_needs_recovery(name)`，而后者只处理
`idempotency`。离线启动后若 Mongo 在健康循环首轮探测前已恢复：`prev={}` 不存在边界，
`identity.health_check()` 内部重连直接返回 True → `_recover_dependency("identity")`
永不执行 → 权威身份缓存恒为 `None` → 后续聊天持续 `STORAGE_UNAVAILABLE`。

**实现（`core/agent/_lifecycle_persistence.py::_dependency_needs_recovery`）：** 新增
identity 分支——权威 store 已装配（`is_authoritative`）但尚未装载成功（`not is_available`）
→ 当轮补触发重载；装载成功后 `is_available` 为 true，不再重复（避免每轮空转）。
恢复失败 → `_recover_dependency` 返回 False → 该依赖本轮记 False，后续轮次按边界继续重试，
身份保持 fail-closed；重载走 `repo.get` / `bootstrap_if_absent`，不会覆盖 Mongo 中更高
revision 的档案。`/ready` 的 identity 门禁不变。

**测试证据（`core/agent/tests/test_user_identity_persistence.py`）：**

| 测试 | 断言 |
|---|---|
| `test_identity_recovered_on_first_healthy_probe` | 离线启动（`get() is None`）→ 撤销 connect 打桩使**首轮探测即 True** → 跑 1 轮真实 `_db_health_loop`（`asyncio.sleep` 打桩技法同 `test_lifecycle_idempotency.py`）→ 装载到 **revision 2**（非本地 bootstrap 的 1）/`displayName=妖梦`/`preferredAddress=主人`，`agent._user_identity` 同步 |
| `test_identity_recovery_retries_on_later_rounds_while_failing` | connect 持续失败 → 3 轮真实循环内重试 ≥3 次，`get()` 仍为 None、`is_available` 为 False（fail-closed） |

反向验证：临时移除 identity 分支，`test_identity_recovered_on_first_healthy_probe` 即失败。

### 6.4 门禁与契约（本次复核轮）

| 门禁 | 结果 |
|---|---|
| 定向（清单 §4 的 9 个文件） | ✅ **71 passed**（新增 3 条） |
| `make lint` | ✅ All checks passed |
| `make pyright` | ✅ 0 errors, 0 warnings |
| `make check-types` | ✅ 0 漂移 + 41 错误码单源一致 |
| `core/agent/tests/` + `core/cognitive/tests/` | ✅ 426 passed |
| `make test-normal`（完整集 `not slow and not external`） | ✅ **4144 passed, 2 skipped**（76 deselected） |

**契约：无前端契约变更**——`git diff --name-only <本轮基线>..HEAD -- frontend_contract/ frontend/src/types/`
为 0 文件，`schemaHash` 未变化（`dece6f372e22f4f5`）。

**运行版本：** 本次修复**未**重启后端服务（按交接要求，由用户手动启动）；线上进程仍是
上一版 `96a60f1f`，本轮两个提交需在下次启动后生效。
