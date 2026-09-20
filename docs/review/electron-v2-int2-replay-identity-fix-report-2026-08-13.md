# Electron v2 二次联调 WS replay 身份修复报告

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

**日期：** 2026-08-13

**后端仓库：** `/home/yomu/elysia-agent`

**修复基线：** `main@b1f372d`（spec 提交）→ 实现 commit `2df115c..fb9f8f1`

**整改清单：** [docs/review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md](2026-08-13-Windows-Electron-二次真实联调后端整改清单.md)（BE-INT2-P0-01 / BE-INT2-P1-01）

**复现会话：** `conv_036c872c7c55`

## 1. 结论

二次联调阻断问题（WS `session` replay 的 tool 项缺 `message_id`/`timestamp` → 与 REST 身份分叉 → 前端重复消息）已闭环：

- **运行时输出（P0-01）**：`messages_for_client()` tool 分支输出消息自身 `message_id`/`timestamp`（复用已持久/派生的 ID，绝不新生成）；`ClientToolMessage` 类型加必填身份字段。
- **契约（P1-01）**：`SessionMessageItem.message_id`/`timestamp` 必填非空（`min_length=1`），缺身份项 fail-closed 拒绝；三产物（ts/schema/raw_ts）重生成。
- **门禁**：`make test-quick` **3548 passed**、Ruff、Pyright 0 errors、`frontend_contract --check`、`check_error_codes` 全绿。
- **真实数据 sanity**：复现会话 tool 消息持久 `message_id=msg_7c0c72b0a8ef42ad942d4f8237`，REST 与 WS（`messages_for_client`）现读取同一身份（`inmem_same=True`）。

按整改清单 §8，BE-INT2-P0-01 与 BE-INT2-P1-01 一起关闭；真实 Electron 回归（§7）与前端配套清单（Renderer 水合 / replay fallback / 取消码消费）由 `elysia-frontend` 侧验证。

## 2. 修复总表

| ID | 原问题 | 修复 | 状态 |
|---|---|---|---|
| BE-INT2-P0-01 | WS replay tool 项漏 message_id/timestamp，与 REST 身份分叉 | `messages_for_client()` tool 分支输出身份（复用持久 ID）+ `ClientToolMessage` 必填 | 已关闭 |
| BE-INT2-P1-01 | 类型/契约/测试允许 tool 省略身份 | `SessionMessageItem` 身份必填非空 + 缺身份 fail-closed + 测试门禁 | 已闭环 |

## 3. 设计决策记录（spec D1-D4）

| 决策 | 内容 | 关键理由 |
|---|---|---|
| **D1** | `messages_for_client()` tool 分支输出 `msg.message_id`/`msg.timestamp` | 复用已持久/派生 ID，绝不在 replay 时重新随机（整改清单 §5.1.3 禁止） |
| **D2** | `ClientToolMessage` 加必填身份 | 不继承 `_ClientMessageBase`（content 类型冲突，PEP 589 禁止不兼容覆盖）；类型下游受约束，运行时强制由 D3 兜底 |
| **D3** | `SessionMessageItem.message_id`/`timestamp` 必填非空 | 缺身份项 fail-closed；legacy 经首轮适配器稳定补齐；user/assistant 构造校验器复核后记为后续项（REST 侧 min_length 已使空 ID 双侧一致 fail-closed，不分叉） |
| **D4** | 三产物重生成 | ts/schema/raw_ts 与源模型一致，漂移门禁通过 |

**v1 影响（记录）**：`messages_for_client`/`_build_session_items` 为 v1/v2 共用——D1 给 v1 tool 项新增身份字段（附加，前端安全）；D3 使 v1 replay 同样要求身份（v1 消息均有，无回归）。

## 4. 实现 commit 链

| commit | 内容 |
|---|---|
| `2df115c` | D1+D2 `messages_for_client` tool 分支身份 + `ClientToolMessage` 必填 |
| `fb9f8f1` | D3+D4 `SessionMessageItem` 身份必填非空 + 三产物重生成 + 测试 fixture 修补 |
| `2c26078` | 补 §5.3.5/5.3.6/5.3.7 + 空身份验收测试（整改清单 §5.3 逐条落实，最终 review Finding 1/2） |

## 5. 门禁与真实数据验证证据

| 门禁 | 结果 |
|---|---|
| `make test-quick` | **3548 passed, 2 skipped, 183 deselected** |
| Ruff（`core/ Module/ frontend_contract/ scripts/`） | All checks passed |
| Pyright | 0 errors, 0 warnings |
| `python -m frontend_contract --check` | 通过（三产物无漂移，schemaHash `45e5390c…` → `e5d4dd2b…`） |
| `scripts/check_error_codes.py` | 41 码一致 |
| `git diff --check` | 干净 |

**真实数据 sanity**（`conv_036c872c7c55`）：

```text
tool_call_id=call_00_8TBYMy3a8SKsXlHnVjgX8457
persisted message_id=msg_7c0c72b0a8ef42ad942d4f8237（与整改清单 §3.2 REST 值一致，非空）
in-memory 消息 message_id 相同（inmem_same=True）→ WS messages_for_client 输出同一身份
```

## 6. 后端完成口径

整改清单 §8 第 1-2 项（P0+P1 完成、自动化门禁全绿）已满足；第 3 项真实 Electron 回归（§7）与第 4 项前端配套清单由 `elysia-frontend` 侧执行验证。

## 相关文档

| 文档 | 内容 |
|---|---|
| [整改清单](2026-08-13-Windows-Electron-二次真实联调后端整改清单.md) | BE-INT2-P0-01/P1-01 原始证据与验收门禁 |
| [设计 spec](../superpowers/specs/archive/2026-08/2026-08-13-electron-v2-int2-replay-identity-fix-design.md) | D1-D4 决策 + review 修复 |
| [实现 plan](../superpowers/plans/archive/2026-08/2026-08-13-electron-v2-int2-replay-identity-fix-plan.md) | 3 任务 TDD 计划 + plan review 修复 |
