# Electron v2 二次联调 WS replay 身份修复设计

**文档版本：** v1.0
**文档状态：** 待评审
**日期：** 2026-08-13
**后端仓库：** `/home/yomu/elysia-agent`
**整改清单：** [docs/review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md](../../review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md)（BE-INT2-P0-01 / BE-INT2-P1-01）

---

## 1. 背景

二次真实联调确认首轮消息 ID 修复（新写入 + 历史回填 + REST）已通过，但**遗漏独立出口**：
WS `session` replay 的 tool 分支未输出工具消息自身的 `message_id`/`timestamp`。同一工具结果
经 REST（带 `msg_7c0c72b0a8ef42ad942d4f8237`）与 WS replay（无身份）到达前端 → 前端先映射为
`{conversationId}:replay`、REST 首屏又插入正式 ID → **重连后真实形成重复逻辑消息**（复现会话
`conv_036c872c7c55`：REST 6 条、前端 store 7 条，两条 tool 记录仅 `messageId/createdAt` 不同）。

**实测证据（复现会话）：**
- 工具调用 `call_00_8TBYMy3a8SKsXlHnVjgX8457`（get_time）；
- REST：`message_id = msg_7c0c72b0a8ef42ad942d4f8237`、`timestamp = 2026-08-13T13:15:31.557518+00:00`；
- WS replay：同 tool item 有 `tool_call_id/name/content/success`，**无** `message_id/timestamp`。

## 2. 根因链

```text
messages_for_client() 的 ToolResultMessage 分支只输出 role/tool_call_id/name/content/success/
error/error_type/duration_ms/truncated，漏 message_id/timestamp    [core/memory/session.py:211-221]
  → ClientToolMessage 单独 TypedDict，不继承 _ClientMessageBase，类型不要求身份字段
                                                                    [core/schema/message.py:112]
  → SessionMessageItem.message_id/timestamp 为 Optional（全角色可缺省），缺身份 tool 项被当合法
                                                                    [frontend_contract/ws_message.py:202-203]
  → _build_session_items() 放行无身份 tool 项 → WS replay 与 REST 身份分叉 → 前端重复消息
                                                                    [core/input/ws_handler.py:122-156]
```

首轮修复保证「消息对象有稳定 ID」，但「每个客户端序列化出口都发送该 ID」未被覆盖——tool 专用投影
层漏了身份字段，数据库扫描 / REST 测试 / 回填测试均全绿。

## 3. 设计决策记录

### D1：`messages_for_client()` tool 分支输出消息自身身份

**决策：** `ToolResultMessage` 分支追加 `"message_id": msg.message_id, "timestamp": msg.timestamp`。
**复用已持久化/稳定派生的 ID——绝不重新调用随机 ID 生成器**（整改清单 §5.1.3 禁止）。

**前提成立性：** 首轮批次后，内存中所有 tool 消息 `message_id` 恒非空——新写入 `new_message_id()`
（D3 首轮）、存量经 `validate_message_legacy_compat` 适配器派生稳定 ID（D2 首轮）。`timestamp`
为 `default_factory=now_iso` 恒有值。故 tool 分支输出非空身份是安全的。

**legacy 边界（review 4）：** 适配器派生要求 `tool_call_id` **且** `timestamp` 均在；缺 `timestamp`
的存量 tool 文档跳过派生 → `message_id` 必填校验失败 → **该会话 load 失败**（视为坏数据 fail，
非补发）。这是首轮 D2 既有边界，非本批引入，D1 只是依赖它；真实 69 条空 ID 工具消息均带
tool_call_id+timestamp，不命中。

**v1 影响（review 3）：** `messages_for_client`/`_build_session_items` 为 v1/v2 共用。D1 给 v1
tool item 新增 `message_id`/`timestamp`（**附加字段，v1 前端安全**）；D3 使 v1 replay 同样要求
身份——v1 消息均有非空 ID（同 D1 前提），无回归。

### D2：`ClientToolMessage` 加必填身份字段

**决策：** `ClientToolMessage` 增加 `message_id: str`、`timestamp: str`（必填）。
**不继承 `_ClientMessageBase`**：基类含 `parts: list[dict[str, str]]` 与 `content: list[dict]`，
与 tool 的 `content: str` 形状冲突（记录理由，避免 reviewer 误判为「应继承」）。

### D3：`SessionMessageItem` 收紧身份不变量

**决策：** `message_id: str = Field(min_length=1, ...)`、`timestamp: str = Field(min_length=1, ...)`（均必填且非空）。
v2 session replay 中所有持久消息（user/assistant/tool）均要求身份字段。

**legacy 兼容：** 进入 `SessionMessageItem` 契约边界前由既有 `validate_message_legacy_compat`
稳定补齐（首轮已为 tool 派生；user/assistant 的 `message_id` 为模型必填，存量实测无空 ID）。
**残余**：若未来出现空 ID 的 user/assistant 消息（实测不存在），`_build_session_items` 按
fail-closed 丢弃该条（`model_validate` 失败 → drop + warning）——与整改清单 §5.2「不能继续把
缺字段作为正常 v2 wire 形状发送」一致。

**user/assistant 校验器决策（review 1，复核后不采纳构造校验器）：** 不改
`UserMessage`/`AssistantMessage` 模型加非空 validator（会牵动首轮 D4 append 守卫测试的重构）。
**关键复核**：空 ID user/assistant 消息若未来出现，REST 侧 `Message.message_id` 已有
`min_length=1`（首轮 D5）同样契约拒绝丢弃 → **REST 与 WS 双侧一致 fail-closed，不会形成本批要
消除的分叉**。构造校验器属纯防御性加固，记为后续项（若未来有写入路径可能产出空 ID user/
assistant 再补）。

### D4：契约产物重生成

**决策：** `SessionMessageItem` 变更 → 重新生成 `contract.generated.ts` / `contract.schema.json` /
`contract.raw.ts`（受管路径 `frontend/src/types/` 与 `frontend_contract/`），漂移门禁通过。

## 4. 组件改动清单

| 文件 | 改动 |
|---|---|
| `core/memory/session.py` | `messages_for_client()` tool 分支输出 `message_id`/`timestamp`（D1） |
| `core/schema/message.py` | `ClientToolMessage` 加必填 `message_id`/`timestamp`（D2） |
| `frontend_contract/ws_message.py` | `SessionMessageItem.message_id` min_length=1 + `timestamp` 必填（D3） |
| `core/input/tests/test_server.py` | `test_build_session_items_drops_invalid_keeps_valid` fixture 的 tool 项补身份（§5.3.4）；新增 tool replay 身份断言 |
| `frontend/src/types/contract.generated.ts` + `frontend_contract/contract.schema.json` + `frontend_contract/contract.raw.ts` | 重新生成（D4） |
| `core/schema/tests/` 或 `core/memory/tests/` | `messages_for_client` tool 身份往返测试 |

## 5. 测试要求（整改清单 §5.3 逐条落实）

1. `SessionMemory` 含真实 `ToolResultMessage` 时，`messages_for_client()` 输出原始
   `message_id`/`timestamp`（断言与消息对象值一致，非新生成）；
2. `messages_for_client() → SessionMessage → wire_dict()` 往返后 tool 身份字段不丢失；
3. `_build_session_items()` 对缺失/空 `message_id` 的持久消息按新契约拒绝（fail-closed drop）；
4. 修改「无 ID tool 仍合法」fixture——tool 项补 `message_id`/`timestamp` 保持合法；
5. 同一会话连续两个工具，WS replay 两个 ID 均非空、互不相同；
6. 同一工具消息经 REST（`_archive_msg_to_contract`）与 WS replay（`messages_for_client`）
   返回的 `message_id`/`timestamp`/`tool_call_id` 完全一致；
7. 对历史稳定派生 ID 的工具消息重复 replay，ID 每次相同；
8. `frontend_contract --check`（含三产物漂移）通过——对应整改清单 §5.3.8；其「前端字段级
   `check:contract`」部分由 `elysia-frontend` 侧执行（spec §6 验收配合项，非后端门禁）。

## 6. 验收门禁

- [ ] D1/D2/D3 实现 + §5 测试全绿；
- [ ] `make test-quick` / Ruff / Pyright / `frontend_contract --check` /
      `scripts/check_error_codes.py` 全绿；
- [ ] 契约三产物重生成且漂移门禁通过；
- [ ] 前端仓库 `check:contract` 通过（配合项）。

### 真实 Electron 回归（整改清单 §7）

- [ ] 新执行 `get_time`，断开重连后 WS replay tool item 含非空 `message_id`/`timestamp`；
- [ ] replay 与 REST 三身份字段完全一致；前端 store 该 `tool_call_id` 只对应一条 Message；
- [ ] 连续两个工具结果不映射到相同键；切换会话/刷新/重启均不重复；
- [ ] 旧 `default` 会话可读，首轮回填无回归；文本/取消/结构化错误路径保持通过。

## 7. 明确不做（防范围蔓延）

- 不在 replay 时生成新随机 ID（整改清单 §6 禁止）；
- 不把 `tool_call_id` 改名为 `message_id`（整改清单 §6 禁止）；
- 不让前端删除 REST 消息迁就无 ID replay（整改清单 §6 禁止）；
- 不放宽 REST `message_id` 非空约束（整改清单 §6 禁止）；
- 不只改 `ClientToolMessage` 类型而漏运行时 dict、也不只补运行时 dict 而不收紧契约和测试
  （整改清单 §6 第 5 条——本设计 D1/D2/D3 + §5 测试三层齐改）；
- 不修前端 Renderer 水合 / replay fallback / 取消码消费（整改清单 §9 明确属前端配套）。
