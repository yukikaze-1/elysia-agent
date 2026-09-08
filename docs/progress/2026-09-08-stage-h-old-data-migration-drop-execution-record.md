# 阶段 H：旧数据迁移移除执行记录（2026-09-08）

> **关联提交：** `575b430f`（chore: drop old-data migration per dev-phase decision）
> **当前基线：** backend `e7b52954`（功能基线）+ `575b430f`（本次收尾）；docs 提交不计入功能基线
> **上一批：** 阶段 G（`9d183b08`）——`character.state.changed` wire 发射
> **本批性质：** 阶段 H 代码/契约对齐已完成后的**决策落地收尾**——移除「旧数据迁移」

## 0. 背景

阶段 G（`9d183b08`）收尾后，后端推进了 24 个 commit（`9fd6464e` → `11a1e8cf`，96 文件 +12K 行）：
`character_profiles=true` 开启、Custom 角色进真实聊天、会话→canonical instance 绑定、
Revision 传播 / reset_evolution / CAS、REST 角色管理、契约 TS/schema/raw 再生。
架构稿状态快照（2026-09-08）据此标注「阶段 A–G 后端已实现，阶段 H 代码/契约对齐完成，真机联调待验收」。

经逐层核实，这批 +12K 行的后端落地在测试下**真实成立**（非空架子）：
- ruff lint ✅ / 角色域 70 测试 / check-types 0 漂移 / pyright 0 错误 / test-normal 4050 passed（本批提交前）。

## 1. 决策记录

| # | 决策 | 理由 |
|---|------|------|
| H-D1 | **不迁移旧数据，直接上新** | 产品决策：开发阶段不需要为旧库/旧会话做无损迁移；旧数据在部署时清库。架构稿 §15.3-15.8 影子迁移设计**整体不执行**。 |
| H-D2 | **删除迁移骨架 `migration.py` + `test_migration.py`** | 纯死代码：`grep` 全仓库零生产引用（未接入 lifecycle、未复制 state、未动 character_versions / LTM / pending）。删除绝对安全。 |
| H-D3 | **部署时清库，代码不加逻辑** | 旧数据清理由部署/运维侧完成，代码内不写任何重置/迁移逻辑——不引入额外入口。 |
| H-D4 | **保留 `loaded_binding or _default_binding` 运行时兜底** | 它是「无 binding → 回退内置 Elysia」的正确安全行为（新库下有效），是运行时兜底、非迁移逻辑；不属「删死代码」范围，故保留。 |

## 2. 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `core/character/migration.py` | **删除**（纯死代码：shadow migration 骨架） | -41 |
| `core/character/tests/test_migration.py` | **删除**（对应测试） | -38 |
| `core/character/README.md` | 文件布局删 migration 两行；「旧会话 shadow migration」段改写为「旧数据迁移」不迁移决策；相关文档表注 §15 不执行 | +9/-4 |
| `docs/2026-08-30-...-architecture-design.md` | 状态快照新增「旧数据迁移：已移除」决定行；注明 §15.3-15.8 设计不执行 | +2/-1 |

## 3. 门禁验证

| 门禁 | 结果 |
|------|------|
| `ruff lint`（core/Module/frontend_contract） | ✅ All checks passed |
| `make check-types`（契约漂移） | ✅ 0 漂移；41 错误码单源校验通过 |
| `make pyright`（Elysia env） | ✅ 0 errors / 0 warnings / 0 informations |
| `make test-normal`（not slow and not external） | ✅ **4050 passed, 2 skipped, 76 deselected**（217s） |
| 删除安全核查 | ✅ 全仓库 `migration.py` / `legacy_instance_id` / `migrate_legacy_sessions` 零引用 |

## 4. 保留未动（Out of scope）

- **Windows Electron 真机联调**：环境不在手里，`character_profiles=true` 的 Electron 冒烟（连接/选角/编辑/回滚/状态重置/重连/迟到帧）**未验收**。架构稿状态快照仍标记「待人工验收」，不伪称通过。
- **阶段 H 文档其余项**（database-storage / protocol / README 更新）——本次仅处理迁移相关；其余文档收尾如需可另起。

## 5. 最终状态

- 工作树干净；HEAD = `575b430f`。
- 后端主体（阶段 A–H 代码/契约）已落地并通过 full 门禁；唯一剩余验收项为真机联调（H-D1 决定下，迁移已从范围移除）。
