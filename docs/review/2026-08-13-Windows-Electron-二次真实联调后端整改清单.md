# Electron v2 Windows 二次真实联调：后端整改清单

> **状态回填（2026-09-11 复核）：✅ 表中 2 项后端问题均已关闭。**
> 关闭证据见 [`electron-v2-int2-replay-identity-fix-report-2026-08-13.md`](electron-v2-int2-replay-identity-fix-report-2026-08-13.md)
> （BE-INT2-P0-01 已关闭、P1-01 已闭环，含 D1-D4 + 真实 sanity + 门禁证据）。
> **本文保留为问题发现时的历史快照**；§7 的未勾选复选框记录的是**当时**的后端侧动作，
> 真实 Electron 跨仓回归与前端配套清单（Renderer 水合 / replay fallback / 取消码消费）
> 由 `elysia-frontend` 侧执行，不在本清单关闭条件内。
> 当前进度以 `todo.md` 与各修复报告为准。

**日期：** 2026-08-13

**后端联调基线：** `/home/yomu/elysia-agent` `main@88ee650125db681b2289296dbaf8551a1c44fb56`

**前端联调基线：** `/home/yomu/elysia-frontend` `main@db6d127616e840b90d8ab45a76608d215aa9604f`

**联调拓扑：** Ubuntu 运行后端；Windows 运行 Electron；Windows 通过本地端口转发访问后端

**复现会话：** `conv_036c872c7c55`（`B-01 修复后回归 2026-08-13 2c7c55`）

**前端配套清单：** `/home/yomu/elysia-frontend/docs/review/2026-08-13-Windows-Electron-二次真实联调前端整改清单.md`

**交接范围：** 本文只要求修改后端仓库。Renderer 启动水合、前端 replay fallback 和取消码消费问题见前端配套清单。

## 1. 结论

此前针对“工具消息空 `message_id`”的写入修复、历史回填和 REST 读取已经通过真实回归：

- 旧 `default` 会话最近 50 条历史成功读取；
- user / assistant / tool 共 50 条消息的 `message_id` 全部非空且无重复；
- 新会话真实执行 `get_time` 成功；
- REST 返回的新工具消息有稳定 ID `msg_7c0c72b0a8ef42ad942d4f8237`。

但二次联调发现此前修复遗漏了一个独立出口：**WS `session` replay 的 tool 分支没有输出工具消息自身的 `message_id` 和 `timestamp`**。同一工具结果经 REST 和 WS replay 到达前端时身份不一致，导致重连后重复消息。

因此当前准确口径是：

> 归档写入、历史迁移和 REST ID 已修复；WS replay 尚未沿用同一个稳定 ID。后端 B-01 的“REST 历史与 WS replay 身份一致”验收项仍未通过。

## 2. 问题总表

| ID | 优先级 | 问题 | 直接影响 | 状态 |
|---|---:|---|---|---|
| BE-INT2-P0-01 | P0 | WS session replay 的工具消息缺少 `message_id` / `timestamp` | 重连后同一工具结果以不同 ID 合并，产生重复消息；多条无身份工具结果还可能互相覆盖 | 待修复，阻断 B-01 封口 |
| BE-INT2-P1-01 | P1 | replay 类型和测试继续允许 tool 消息省略身份字段 | 上述缺陷能够通过类型、契约和后端测试门禁 | 待补门禁，与 P0 同批完成 |

两项属于同一个交付批次：P0 修运行时输出，P1 防止再次回归。

## 3. 真实联调证据

### 3.1 真实工具调用

在会话 `conv_036c872c7c55` 中发送：

```text
【B-01 修复后工具回归】你必须调用 get_time 工具获取当前时间，然后只用一句中文回答当前时间。
```

后端实际执行了 `get_time`，结果为：

```text
2026-08-13 21:15:31 CST
```

工具调用 ID：

```text
call_00_8TBYMy3a8SKsXlHnVjgX8457
```

### 3.2 REST 与 WS replay 身份不一致

REST `/api/sessions/{id}/messages` 返回同一工具消息：

```text
message_id = msg_7c0c72b0a8ef42ad942d4f8237
timestamp  = 2026-08-13T13:15:31.557518+00:00
```

Electron 断开再连接后，WS `session` replay 中的同一 tool item 保留了 `tool_call_id/name/content/success`，但没有 `message_id` 和 `timestamp`。

前端因此先把 replay 项映射为：

```text
conv_036c872c7c55:replay
```

随后 REST 首屏又插入正式 ID：

```text
msg_7c0c72b0a8ef42ad942d4f8237
```

真实运行时结果：

- 后端 REST 历史共 6 条；
- 前端重连后的 message store 共 7 条；
- 两条 tool 记录具有相同 `callId/name/content/success`，只有 `messageId/createdAt` 不同；
- 这不是理论风险，重复逻辑消息已经在真实 Electron 中形成。

## 4. 根因代码链

### 4.1 tool replay 序列化遗漏身份字段

[`core/memory/session.py`](../../core/memory/session.py) 的 `messages_for_client()` 对 user / assistant 分支会输出：

```python
"message_id": msg.message_id,
"timestamp": msg.timestamp,
```

但 `ToolResultMessage` 分支只输出：

```python
role / tool_call_id / name / content / success /
error / error_type / duration_ms / truncated
```

没有输出 `msg.message_id` 和 `msg.timestamp`。

### 4.2 类型没有要求 tool 身份字段

[`core/schema/message.py`](../../core/schema/message.py) 中：

- `_ClientMessageBase` 包含 `message_id` 和 `timestamp`；
- `ClientUserMessage` / `ClientAssistantMessage` 继承该基类；
- `ClientToolMessage` 单独定义，缺少这两个字段。

所以类型检查不会指出 tool 分支的遗漏。

### 4.3 WS 契约接受缺失值

[`frontend_contract/ws_message.py`](../../frontend_contract/ws_message.py) 的 `SessionMessageItem` 将：

```python
message_id: Optional[str] = None
timestamp: Optional[str] = None
```

定义为全角色可缺省。`_build_session_items()` 因此会把无身份的 tool item 当作合法回放项。

### 4.4 现有测试固化了错误形状

[`core/input/tests/test_server.py`](../../core/input/tests/test_server.py) 的 `test_build_session_items_drops_invalid_keeps_valid` 构造了一个没有 `message_id/timestamp` 的 tool item，并断言它是合法项。

当前 `messages_for_client()` 测试只明确断言了 assistant 的 ID/时间保留，没有覆盖 `ToolResultMessage → SessionMessage.wire_dict()` 的身份字段往返。

### 4.5 为什么此前报告会误判关闭

[`electron-v2-message-id-fix-report-2026-08-13.md`](electron-v2-message-id-fix-report-2026-08-13.md) 已正确修复：

- 新工具结果创建时的 ID；
- Mongo archive 和 session snapshot 中的历史兼容；
- REST 输出的非空约束。

但“消息对象有稳定 ID”不等于“每个客户端序列化出口都发送该 ID”。本次缺陷位于 `SessionMemory.messages_for_client()` 的 tool 专用投影层，所以数据库扫描、REST 测试和回填测试均可全绿。

## 5. 必须修改

### 5.1 运行时输出

1. 为 `ClientToolMessage` 增加必填 `message_id` 和 `timestamp`。
2. `messages_for_client()` 的 `ToolResultMessage` 分支直接输出：

   ```python
   "message_id": msg.message_id,
   "timestamp": msg.timestamp,
   ```

3. 必须复用消息对象中已经持久化/稳定派生的 ID；**不得在 replay 时重新调用随机 ID 生成器**。
4. user、assistant、tool 三类 replay 项对同一条归档消息都必须与 REST 使用同一 `message_id/timestamp`。

### 5.2 WS 契约

更新 `SessionMessageItem` 的身份不变量。推荐 v2 session replay 中所有持久消息均要求：

- `message_id` 必填且 `min_length=1`；
- `timestamp` 必填且非空。

如果仍需兼容真正的历史坏数据，应在进入 `SessionMessageItem` 契约边界前通过现有 legacy compatibility adapter 稳定补齐，不能继续把缺字段作为正常 v2 wire 形状发送。

完成后重新生成并检查所有前端契约产物。

### 5.3 测试门禁

至少补齐以下测试：

1. `SessionMemory` 含真实 `ToolResultMessage` 时，`messages_for_client()` 输出原始 `message_id/timestamp`；
2. `messages_for_client() → SessionMessage → wire_dict()` 往返后，tool 身份字段不丢失；
3. `_build_session_items()` 对缺失/空 `message_id` 的持久消息按新契约拒绝，或先经明确的 legacy adapter 补齐；
4. 修改当前“无 ID tool 仍合法”的 fixture，避免继续固化旧形状；
5. 同一会话连续执行两个工具，WS replay 中两个 ID 均非空、互不相同；
6. 同一工具消息经 REST 与 WS replay 返回的 `message_id/timestamp/tool_call_id` 完全一致；
7. 对历史稳定派生 ID 的工具消息重复 replay，ID 每次相同；
8. `frontend_contract --check` 和前端字段级 `check:contract` 都通过。

## 6. 禁止的修复方式

- 不要只给 WS replay 临时生成一个新 `msg_*`；这仍会与 REST 身份分叉。
- 不要把 `tool_call_id` 直接改名为 `message_id`；二者语义不同。可以在前端 legacy 防御中用 `tool_call_id` 做诊断锚点，但后端正式输出必须使用消息自身 ID。
- 不要让前端删除 REST 正式消息来迁就无 ID replay。
- 不要放宽 REST `message_id` 非空约束。
- 不要只修改 `ClientToolMessage` 类型而漏掉运行时 dict，也不要只补运行时 dict而不收紧契约和测试。

## 7. 修复后真实 Electron 验收

沿用本次会话或新建等价会话，执行：

- [ ] 新执行一次 `get_time`，记录 `tool_call_id`、REST `message_id/timestamp`；
- [ ] 断开并重新连接 Electron，抓取 WS session replay；
- [ ] replay tool item 明确包含非空 `message_id/timestamp`；
- [ ] replay 与 REST 的三个身份字段完全一致；
- [ ] 前端 store 中该 `tool_call_id` 只对应一条 Message；
- [ ] 连续两个工具结果不会映射到相同键；
- [ ] 切换会话、Renderer reload、重启 Electron 后均不重复；
- [ ] 旧 `default` 会话继续可以读取，历史回填结果无回归；
- [ ] 文本、真实工具调用、取消和结构化错误路径保持通过。

## 8. 后端完成口径

以下条件同时满足后，本清单才可关闭：

1. BE-INT2-P0-01 与 BE-INT2-P1-01 均完成；
2. 自动化门禁全绿；
3. 用更新后的真实后端执行第 7 节，确认 REST / WS ID 一致且 Electron 无重复；
4. 前端配套清单中的 Renderer 水合和消费端防御另行完成。

在此之前可以继续开发其他后端能力，但不能把 B-01 标记为“已真实联调通过”。

## 9. 不属于后端整改的问题

- Renderer reload 后 Pinia 回到 `disconnected`：前端启动水合问题；
- 前端 fallback 使用 `${conversationId}:replay`：前端兼容防御问题；
- 前端只识别 `CANCELLED`、未识别正式码 `GENERATION_CANCELLED`：前端错误码消费问题。

后端本次真实取消输出 `GENERATION_CANCELLED` 与现有正式契约一致，不应为迁就前端 fixture 改成 `CANCELLED`。

## 相关文档

| 文档 | 内容 |
|---|---|
| [首次真实联调后端问题清单](2026-08-13-Windows-Electron-首次真实联调后端问题清单.md) | 首轮空 ID 写入、历史迁移和门禁问题 |
| [消息 ID 修复报告](electron-v2-message-id-fix-report-2026-08-13.md) | 此前已完成的写入、回填、REST 与存储边界修复 |
| [首次联调 runbook](../archive/electron-v2-first-integration-runbook.md) | 真实环境探针、认证与 WS v2 联调口径 |
