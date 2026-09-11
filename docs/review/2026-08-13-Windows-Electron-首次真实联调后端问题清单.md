# Electron v2 Windows 首次真实联调：后端问题清单

**日期：** 2026-08-13

**后端审查基线：** `/home/yomu/elysia-agent` `main@bd03c4178b61ced2470ca82f00f637af94212e97`

**前端联调基线：** `/home/yomu/elysia-frontend` `main@0236a97b783c66b55c6a20aeb0511136f5d774d9`

**联调拓扑：** Ubuntu 运行后端；Windows 运行 Electron；Windows 通过本地转发地址连接 Ubuntu 后端

**前端配套报告：** `/home/yomu/elysia-frontend/docs/review/2026-08-13-Windows-Electron-首次真实联调前端问题清单.md`

**性质：** 本文记录真实联调后仍需整改的后端问题、修复边界和验收门禁；本文不包含业务源码修复。

## 1. 结论

当前后端不是“整体不可联调”。在全新、无工具调用的会话中，B-01 文本主链已经真实通过：

- Electron 认证连接、v2 握手和服务信息展示；
- 用户消息 accepted ACK；
- `inner_voice` / `public_reply` 分段流式输出；
- `generation.completed`、模型、Token 和情绪信息落位；
- 新会话 REST 历史、自动标题、断线重连和 session replay；
- 主动取消后收到 `generation.failed / GENERATION_CANCELLED`，且没有迟到事件覆盖中断状态；
- 默认会话删除返回结构化 `SESSION_PROTECTED` 错误并由前端展示。

但本次联调确认了一组同源的 **P0 阻断问题**：工具结果消息可以以空 `message_id` 写入归档，现有数据库中也已经存在这类记录。只要历史分页中包含一条空 ID 工具消息，Electron 就会拒绝整页响应，旧会话无法打开。仅修新消息生成逻辑仍不够，历史数据必须使用稳定 ID 回填或做等价的稳定兼容转换。

因此当前准确口径是：

> 新会话、纯文本 happy path 已通过；包含历史工具消息的会话和“工具调用后重新打开/重连”尚未通过。BE-INT-P0-01 与 BE-INT-P0-02 必须一起关闭，之后才能把首次文本+工具联调视为完整通过。

## 2. 问题总表

| ID | 优先级 | 问题 | 直接影响 | 状态 |
|---|---:|---|---|---|
| BE-INT-P0-01 | P0 | 新建 `ToolResultMessage` 默认写入空 `message_id` | 每次真实工具调用都可能为会话写入一条后续无法被 Electron REST 历史读取的记录 | 待修复 |
| BE-INT-P0-02 | P0 | 已归档的空 ID 工具消息没有稳定迁移/兼容策略 | 即使修复新写入，`default` 等旧会话仍然打不开 | 待修复 |
| BE-INT-P1-01 | P1 | 后端模型、REST 契约和端到端测试未共同守住“归档消息 ID 非空”不变量 | 同类回归可再次穿过后端门禁，直到真实前端联调才暴露 | 待补门禁 |

BE-INT-P0-01 与 BE-INT-P0-02 是同一运行时故障的两个修复面，不能只选其一。

## 3. 真实联调证据与复现

### 3.1 通过场景

在新会话 `conv_e7f15e98fa2e` 中发送不调用工具的文本请求，Electron 成功展示公开回复，REST 历史返回 user + assistant，顺序和字段均通过前端校验。取消和重连回放也成功。

这说明认证、连接、纯文本 generation 和新会话 REST 主体没有被本问题破坏。

### 3.2 失败场景

打开旧 `default` 会话时，Electron 主进程调用：

```text
window.desktopApi.getMessages({ conversationId: "default", limit: 50 })
```

返回前端错误：

```text
[SCHEMA_VALIDATION_FAILED] 会话消息响应校验失败
```

检查后端实际 REST 响应，最近 50 条记录中有 **6 条** `role="tool"` 的消息带：

```json
{
  "role": "tool",
  "message_id": ""
}
```

前端 REST 契约要求所有归档消息的 `message_id` 至少包含一个字符。数组中任意一条消息失败会使整页 `MessagesPageResponse` 校验失败，因此不是“只少显示一条工具结果”，而是整个会话页无法加载。

### 3.3 最小代码链

1. [`ToolResultMessage`](../../core/schema/message.py) 的 `message_id` 默认值为空字符串：

   ```python
   message_id: MessageId = Field(default=MessageId(""))
   ```

2. [`CognitiveLoop`](../../core/cognitive/loop.py) 中两条生产写入路径都没有显式分配 ID：

   - `submit_task` 工具结果写入；
   - 普通工具分发结果写入。

3. [`MessageRepository.append()`](../../core/storage/message_repo.py) 直接 `model_dump()` 后写入 `message_archive`，没有在存储边界拒绝空 ID。

4. [`frontend_contract.rest.Message`](../../frontend_contract/rest.py) 当前只声明 `str`，没有 `min_length=1`，所以 [`_archive_msg_to_contract()`](../../core/input/_common.py) 会把空字符串当作合法 wire 值继续返回。

5. Electron 的 `RestMessageSchema` 使用 `z.string().min(1)`。这是正确的身份不变量；问题归属后端写入、后端契约与历史兼容层。

## 4. BE-INT-P0-01：新工具结果写入空 `message_id`

### 4.1 影响

- 普通工具和 `submit_task` 都会命中；
- 工具执行当轮可能仍能通过流式 UI，看起来“调用成功”；
- 一旦刷新、切换会话、重连或分页读取历史，含该记录的页面就会失败；
- 多条空 ID 若被前端容忍，还会在 `byId[messageId]`、去重、排序、上下文锚点和附件引用中互相覆盖，不能把空字符串视作可接受的兼容值。

### 4.2 必须修改

1. 两条生产构造路径都为 `ToolResultMessage` 分配真实 `new_message_id()`；不要只修普通工具而漏掉 `submit_task`。
2. 在后端模型或统一归档边界建立非空约束，使未来任何调用点遗漏 ID 时立即在测试/开发阶段失败，而不是把坏记录写入 Mongo。
3. `frontend_contract.rest.Message.message_id` 同步增加非空约束，保证 REST 契约与运行时身份规则一致。
4. 检查 WS session replay、REST 会话详情、历史分页、搜索和上下文窗口，确保同一条工具消息在所有出口使用同一个 ID。
5. 不要用工具调用 ID `tool_call_id` 直接代替消息 ID；两者语义不同，且后续可能有一对多或跨轮引用。

### 4.3 建议实现原则

- ID 在创建工具结果消息时生成一次，随后原样持久化和回放；
- 新写入继续使用项目统一的 `msg_*` 生成器；
- 存储边界可增加防御性断言/校验和可定位日志，但不能静默丢弃消息；
- schema 收紧与历史迁移需要安排顺序，避免旧空值在迁移前因模型校验失败而让整个查询直接 500。

## 5. BE-INT-P0-02：历史空 ID 缺少稳定回填

### 5.1 为什么只改默认值不够

Mongo `message_archive` 中的旧文档不会因为 Python 默认值变化而自动更新。只修 BE-INT-P0-01 后：

- 新工具消息正常；
- 旧 `default` 和其他含空 ID 工具消息的会话仍返回空 ID；
- 当前联调阻断仍可稳定复现。

因此历史兼容是独立的 P0 交付物。

### 5.2 必须修改

采用以下两类方案之一，推荐持久化迁移：

1. **推荐：幂等回填脚本/启动迁移。** 扫描 `role="tool"` 且 `message_id` 缺失或为空的归档文档，根据 Mongo `_id` 生成稳定、唯一且合法的消息 ID，并把结果持久化。脚本重复运行必须保持同一结果、不得再次改 ID。
2. **兼容出口映射。** 若暂时不能修改历史库，可基于不可变 Mongo `_id` 计算稳定 ID，并保证 REST、WS replay、搜索和上下文等所有出口都使用相同映射。不得在每次请求时调用随机 ID 生成器。

迁移/兼容还需要检查实际可被会话回放读取的其他持久化副本；如果 JSONL 或 session snapshot 中也保存了空 ID，应纳入同一策略。

### 5.3 禁止的“修复”

- 不要让前端接受空 `message_id`；
- 不要在每次 REST 请求时随机生成一个新 ID；
- 不要仅从响应中丢弃坏工具消息；这会造成 `total/messageCount` 与实际条目不一致，也会永久隐藏工具执行历史；
- 不要清空 `default` 或整库来通过联调；这只能掩盖兼容问题，并会丢失用户数据；
- 不要以 Mongo `_id` 游标本身临时冒充不持久的消息 ID，而让其他 API 仍返回空值。

## 6. BE-INT-P1-01：缺少不变量和回归门禁

当前后端自己的 Pydantic REST 模型接受空字符串，现有测试也没有覆盖“真实工具调用 → 归档 → REST 读取 → Electron 契约校验”全链。因此后端测试全绿仍可能输出前端拒绝的响应。

至少补齐以下测试：

1. `ToolResultMessage` 新建时不能得到空 ID；显式传空字符串也应按最终设计拒绝或被统一修正；
2. `submit_task` 和普通工具执行后，session 与 `message_archive` 中的工具消息 ID 均非空；
3. `/api/sessions/{id}` 与 `/api/sessions/{id}/messages` 返回的 user/assistant/tool 每条消息 ID 均非空；
4. 历史迁移包含缺字段、空字符串、多会话、多工具消息和重复运行场景；
5. 迁移前后的 `_id` 分页 cursor、消息数量和时间顺序不变，无重复、无漏项；
6. 同一工具消息经 WS replay 和 REST 读取时 ID 一致；
7. 生成的契约产物和前端 `check:contract` 重新通过。

## 7. 修复后验收门禁

### 7.1 静态与自动化门禁

- [ ] 两个生产 `ToolResultMessage` 构造点都有统一非空 ID；
- [ ] 模型、REST 契约和存储边界共同守住非空不变量；
- [ ] 历史回填幂等，重复运行零变化；
- [ ] 扫描 `message_archive` 后，不再存在 `role="tool"` 且 ID 缺失/为空的记录；
- [ ] 相关 schema、cognitive loop、message repo、session routes 和迁移测试通过；
- [ ] `python -m frontend_contract --check` 及契约漂移门禁通过；
- [ ] 后端原有快速测试、Ruff、Pyright 通过。

### 7.2 真实 Electron 回归

- [ ] 打开现有 `default` 会话，不再出现“会话消息响应校验失败”；
- [ ] 分页覆盖至少一页含历史工具结果的记录，user/assistant/tool 均完整显示；
- [ ] 在新会话执行一次普通工具和一次 `submit_task`，当轮流式结果正常；
- [ ] 切走再切回、刷新/重启 Electron、断线重连后，两类工具结果仍可读取且不重复；
- [ ] REST 历史与 WS replay 对同一消息使用同一个稳定 ID；
- [ ] B-01 纯文本、取消、结构化错误和重连回放原有通过项无回归。

## 8. 联调恢复口径

两个 P0 完成且 7.2 的真实 Electron 回归通过后，可恢复以下联调：

- 旧会话历史兼容；
- 新会话工具调用；
- 工具结果分页、切换会话和断线重放；
- 在此基础上继续附件、工具审批和更复杂场景。

在此之前可以继续测试全新纯文本会话，但不能据此宣称后端已满足完整前端对接。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [首次联调封口问题修复报告](archive/2026-08/electron-v2-integration-gap-fix-report-2026-08-13.md) | 本次运行联调前最后一轮后端修复和门禁结果 |
| [首次联调 runbook](../archive/electron-v2-first-integration-runbook.md) | 双方 SHA、认证、探针、WS v2 和负面用例执行口径 |
| [Electron 后端协议 v2](../electron-backend-protocol-v2.md) | REST、WS、会话与工具契约事实源 |
