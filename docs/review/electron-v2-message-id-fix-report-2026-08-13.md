# Electron v2 首次联调消息 ID 修复报告

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

**日期：** 2026-08-13

**后端仓库：** `/home/yomu/elysia-agent`

**修复基线：** `main@ac3299f`（plan 提交）→ 实现 commit `d00fbb1..740a998`

**问题清单：** [docs/review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md](2026-08-13-Windows-Electron-首次真实联调后端问题清单.md)（BE-INT-P0-01 / BE-INT-P0-02 / BE-INT-P1-01）

**前端配套：** 由 `elysia-frontend` 联调回归验证

## 1. 结论

首次真实联调阻断问题（工具结果消息空 `message_id` → Electron REST 整页校验失败）已闭环：

- **新写入**：两条生产路径（submit_task + 普通工具）构造 `ToolResultMessage` 均分配 `new_message_id()`；模型层 `message_id` 必填 + 拒绝空串；归档边界 `MessageRepository.append` 空 ID 抛 `ValueError`。
- **历史兼容**：读路径（archive / session 快照 / JSONL）统一经 `validate_message_legacy_compat` 对存量空 ID 工具消息派生稳定 ID；幂等回填脚本已实跑——archive 69 条空 ID 全部回填，剩余 0，重复运行零变化。
- **契约**：`frontend_contract.rest.Message.message_id` 加 `min_length=1`，三产物（ts/schema/raw_ts）重新生成，漂移门禁通过。
- **门禁**：`make test-quick` **3542 passed**、Ruff、Pyright 0 errors、`frontend_contract --check`、`check_error_codes` 全绿。

**BE-INT-P0-01 与 BE-INT-P0-02 一起关闭**；按问题清单 §7.2 恢复首次文本+工具联调。

## 2. 修复总表

| ID | 原问题 | 修复 | 状态 |
|---|---|---|---|
| BE-INT-P0-01 | `ToolResultMessage.message_id` 默认空串；两条写入路径不传；append 无守卫 | D3 两处补 `new_message_id()` + D2 模型必填/拒空 + D4 append 守卫 + D5 契约 `min_length=1` | 已关闭 |
| BE-INT-P0-02 | 存量 69 条空 ID 工具消息无稳定迁移 | D1 稳定派生键（读侧适配器 + 幂等回填脚本持久化） | 已关闭 |
| BE-INT-P1-01 | 模型/REST/端到端测试未守住非空不变量 | D2/D4/D5 + §6 全链测试（构造拒绝/归档守卫/REST/搜索/JSONL/回填幂等） | 已闭环 |

## 3. 设计决策记录（spec D1-D6）

| 决策 | 内容 | 关键理由 |
|---|---|---|
| **D1 派生键** | `stable_tool_message_id(tool_call_id, timestamp) = "msg_" + sha256(f"{tool_call_id}:{timestamp}")[:26]` | 跨 archive/快照/JSONL 同一稳定键（session 快照无 `_id`，`_id` 键不可跨库）；非 tool_call_id 原文（哈希摘要，语义隔离）；timestamp 参与哈希覆盖理论一对多 |
| **D2 模型不变量** | `ToolResultMessage.message_id` 必填 + 拒空；读路径适配器对存量空 ID 派生 | 范围仅 tool（实测空 ID 100% 为 tool 消息）；user/assistant 由 D4+D5 兜底 |
| **D3 写入路径** | 两处构造补 `new_message_id()` | 新消息用新鲜随机 ID，与存量派生语义分离 |
| **D4 归档守卫** | `append` 顶部空 ID 抛 ValueError（db-None 分支前） | 数据不变量与存储可用性无关；fire-and-forget 降级 warning，同步 fail-fast 靠模型校验器 |
| **D5 契约** | `Message.message_id` `min_length=1` + 三产物重生成 | 与运行时身份规则一致 |
| **D6 回填** | 幂等脚本 `--dry-run` / 实跑；archive 全量 + sessions 快照 | keyset `_id` 分页防收缩跳档；无法派生 skip + warning |

**偏离问题清单 §5.2「推荐基于 Mongo `_id`」的记录**：`_id` 只存在于 archive，session 快照消息无 `_id`、metadata 为空；跨库一致性要求统一键，故用 `(tool_call_id, timestamp)` 派生。

**决策（review 残差）**：`SearchMessageItem.messageId` / `PendingMessageItem.messageId` 未加 `min_length=1`——runtime 经 D2 读侧派生恒非空，搜索/pending 出口不会输出空 ID；契约收紧为非阻断残差，不扩范围。若后续要求搜索/pending 契约与 `Message` 同强度，可单独小批加。

## 4. 实现 commit 链

| commit | 内容 |
|---|---|
| `d00fbb1` | D1 `stable_tool_message_id` 纯函数 |
| `ddfefc3` | D2a `ToolResultMessage.message_id` 非空不变量 + 修既有构造点 |
| `6f2e17a` | D2b `validate_message_legacy_compat` 读侧派生 |
| `d3464ce` | D3 两处写入补 `message_id` |
| `fd1f25a` | D4 `append` 归档守卫 |
| `d23e052` | D5 契约 `min_length=1` + 三产物重生成 |
| `49db442` | D6 幂等回填脚本 |
| `af5e7b8` | REST/搜索/JSONL 回归测试 |
| `740a998` | 回填测试 `_run` 改 `asyncio.run`（pytest-asyncio 循环隔离） |

## 5. 门禁与真实数据验证证据

| 门禁 | 结果 |
|---|---|
| `make test-quick` | **3542 passed, 2 skipped, 183 deselected** |
| Ruff（`core/ Module/ frontend_contract/ scripts/`） | All checks passed |
| Pyright | 0 errors, 0 warnings |
| `python -m frontend_contract --check` | 通过（三产物无漂移） |
| `scripts/check_error_codes.py` | 41 码一致 |
| `git diff --check` | 干净 |

**真实 Mongo 回填**（`default` 库，2026-08-13）：

```text
[dry-run] archive: 回填 69，跳过(无法派生) 0        # 首轮 dry-run
[实跑]   archive: 回填 69，跳过(无法派生) 0          # 实跑
[dry-run] archive: 回填 0，跳过(无法派生) 0          # 幂等：重跑零变化
```

回填后终态：`message_archive` 共 715 条，`role="tool"` 73 条，**空/缺失 `message_id` = 0**；抽样 3 条派生 ID 均为 `msg_` + 26hex，且与 `(tool_call_id, timestamp)` 重派生一致（`derived_match=True`）。`sessions` 集合在回填前已为 0 空 ID（运行中后端经 D2 读侧派生 + 会话保存自动自愈，侧面验证派生生效）。

## 6. 联调恢复口径

按问题清单 §7.2：打开现有 `default` 会话不再出现「会话消息响应校验失败」；分页含历史工具结果的页 user/assistant/tool 完整显示；新会话普通工具 + `submit_task` 当轮流式正常；切走切回/刷新/重连后工具结果可读不重复；REST 历史与 WS replay 同一稳定 ID；B-01 纯文本/取消/结构化错误/重连回放无回归。

## 相关文档

| 文档 | 内容 |
|---|---|
| [问题清单](2026-08-13-Windows-Electron-首次真实联调后端问题清单.md) | BE-INT-P0-01/02 + P1-01 原始证据与验收门禁 |
| [设计 spec](../superpowers/specs/archive/2026-08/2026-08-13-electron-v2-first-integration-message-id-fix-design.md) | D1-D6 决策记录 + review 两轮修复 |
| [实现 plan](../superpowers/plans/archive/2026-08/2026-08-13-electron-v2-first-integration-message-id-fix-plan.md) | 9 任务 TDD 计划 + plan review 修复 |
