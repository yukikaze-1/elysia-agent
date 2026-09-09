# 阶段 H 真实联调后端整改执行记录与交付（2026-09-10）

> 文档导航：[项目文档](../../README.md) → [docs 文档索引](../README.md) → [多角色 Stage H 真实联调后端整改与复验清单](../review/2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md)

- **后端基线：** `main@65295a90` → **`main@d394f931`**（6 个实现/决策提交）
- **实现范围：** 清单 §1.2 全部 6 项（BE-STAGEH-01 ~ 06）
- **门禁：** ruff ✅ / `make check-types` ✅ 0 漂移 / `make pyright` ✅ 0 错误 / `make test-normal` ✅ **4099 passed, 2 skipped** / `make test-quick` ✅ 3906 passed
- **契约：** `schemaHash` **未变化**（本批 0 个 `frontend_contract/` 或 `frontend/src/types/` 文件改动）

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
