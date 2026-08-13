# Electron v2 首次联调消息 ID 修复设计

**文档版本：** v1.0
**文档状态：** 待评审
**日期：** 2026-08-13
**后端仓库：** `/home/yomu/elysia-agent`
**问题清单：** [docs/review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md](../../review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md)（BE-INT-P0-01 / BE-INT-P0-02 / BE-INT-P1-01）

---

## 1. 背景

首次真实联调（Ubuntu 后端 + Windows Electron）暴露：打开旧 `default` 会话时，Electron 主进程
`getMessages({ conversationId: "default", limit: 50 })` 返回 `[SCHEMA_VALIDATION_FAILED]`——
后端 REST 响应中 `role="tool"` 的消息带 `message_id: ""`，前端 `RestMessageSchema` 的
`z.string().min(1)` 校验整页失败。

**实测数据**（`default` 库，2026-08-13）：
- `message_archive` 共 679 条，其中 **69 条 `role="tool"` 消息 `message_id` 为空/缺失**；
- `default` 会话最近 50 条工具消息**全部**空 ID；
- `sessions` 集合快照同样存有空 ID 工具消息（default 会话 3/3），且快照内消息**无 `_id`、
  metadata 为空**——无法与 archive 用 Mongo `_id` 键关联。

## 2. 根因链

```text
ToolResultMessage.message_id 默认 MessageId("")            [core/schema/message.py:66]
  → CognitiveLoop 两条生产写入路径不传 message_id          [loop.py:828 submit_task / loop.py:907 普通工具]
  → MessageRepository.append() 无守卫直接 model_dump 落库  [message_repo.py:170]
  → frontend_contract.rest.Message.message_id 无 min_length [rest.py:257]，空串被当合法 wire 放行
  → Electron z.string().min(1) 校验失败 → 整页拒绝
```

`MessageId` 是裸 `NewType(str)`，Pydantic 不拒空；`UserMessage/AssistantMessage.message_id`
为必填（无默认），但显式传 `MessageId("")` 同样能构造。只有 `ToolResultMessage` 带空默认值，
因此存量空 ID 全部是工具消息。

## 3. 设计总览

三条线同时闭环，缺一不可：

| 线 | 目标 | 手段 |
|---|---|---|
| **P0-01 新写入** | 新建工具消息必须非空 ID | 两处写入补 `new_message_id()`；模型加非空不变量；归档边界防御守卫 |
| **P0-02 历史兼容** | 存量空 ID 稳定可读、最终持久回填 | 读路径统一派生稳定 ID（适配器）；幂等回填脚本持久化 |
| **P1-01 门禁** | 回归不再穿过门禁 | 模型/归档/REST/迁移全链测试 + 契约 `min_length=1` |

核心不变量：**任何经 archive / session 快照 / JSONL 读出的工具消息，`message_id` 恒非空、
稳定、唯一，且同一消息跨 archive 与 session 快照 ID 一致。**

## 4. 设计决策记录

### D1：历史工具消息的稳定 ID 派生键

**决策：** `stable_tool_message_id(tool_call_id, timestamp) = "msg_" + sha256(f"{tool_call_id}:{timestamp}")[:26]`。

**偏离问题清单 §5.2「推荐基于 Mongo `_id`」的原因（记录）：** `_id` 只存在于
`message_archive`；`sessions` 快照消息无 `_id`、metadata 为空，无法用 `_id` 跨两库关联。
联调验收要求（问题清单 6.6）「同一工具消息经 WS replay 和 REST 读取时 ID 一致」要求两库
使用**同一派生键**。`tool_call_id`（LLM 生成的全局唯一调用 ID）与 `timestamp`（消息对象
自带字段）在两库中逐字一致，是唯一可用的跨库稳定键。

**对问题清单 4.2.5「不要用 tool_call_id 直接代替消息 ID」的满足方式：** 派生值是
`msg_` 前缀 + sha256 摘要的**不透明值**，不是 `call_*` 原文；`timestamp` 参与哈希，
覆盖理论上的「同一 tool_call 未来一对多/跨轮」场景（同一 tool_call_id + 不同 timestamp →
不同消息 ID）。与 `new_message_id()` 同为 `msg_` + 26hex，值空间一致、不冲突
（随机 26hex 与 sha256 摘要撞车概率可忽略）。

**幂等性：** 同一 `(tool_call_id, timestamp)` 恒产生同一 ID；不同消息因 tool_call_id 唯一
而 ID 唯一；回填脚本重复运行零变化。

### D2：模型级非空不变量

**决策：** `ToolResultMessage.message_id` 改为必填 + `field_validator` 拒绝空串。

**范围仅限 `ToolResultMessage`：** 实测存量空 ID 100% 是工具消息（`message_archive` 679 条
中 69 条空 ID 全为 `role="tool"`）；`UserMessage/AssistantMessage` 生产构造路径恒带非空 ID
（`loop.py:629 user_message_id or new_message_id()` 兜底）。user/assistant 的空 ID 防泄漏由
D4 归档边界守卫 + D5 契约 `min_length` 共同兜底——不给「实测不存在」的 legacy 空 user/
assistant 文档引入加载风险（这类文档无 `tool_call_id`，无法用 D1 派生，严格校验会导致
会话加载失败而非稳定读回）。

**对存量数据的兼容：** 所有生产读路径（`message_repo._doc_to_message`、`session_repo.load`、
`session._load_from_jsonl`）均经 `validate_message_legacy_compat`。在该适配器入口对
`role="tool"` 且 `message_id` 空/缺失的 dict **先派生 D1 ID 再校验**——存量空 ID 文档
加载即得稳定 ID，且与回填结果一致。依赖：工具消息 `tool_call_id` 为必填字段
（`ToolResultMessage.tool_call_id` 无默认），合法工具消息恒可派生。

**缺字段边界的显式定义（review B2）：** 派生**只使用文档自带的** `tool_call_id` + `timestamp`；
`tool_call_id` 或 `timestamp` 任一为空/缺失 → **不派生**，留空交 strict 校验失败并
`logger.warning` 定位（按畸形文档处理）。这是**相对现状的行为变更**（现状缺 timestamp 的
tool 文档会被 `default_factory=now_iso` 补上而「能读」，D2 后加载该消息会失败）——
实测 69 条空 ID 工具消息**全部**带 tool_call_id + timestamp，真实数据不命中；此边角仅
防御未来源头损坏的文档，绝不 fallback `now_iso()` 派生（会导致跨加载 ID 不稳定且与回填
「跳过缺字段」结果不一致）。

### D3：写入路径

**决策：** `loop.py:828`（submit_task）与 `loop.py:907`（普通工具）两处
`ToolResultMessage(...)` 显式传 `message_id=new_message_id()`。新消息用新鲜随机 ID
（非 D1 派生）——派生只用于**存量**消息；新消息从创建起就拥有独立身份。

### D4：归档边界防御守卫

**决策：** `MessageRepository.append()` 对 `not msg.message_id` 抛 `ValueError`
（fail-fast，不静默丢弃）。D2 使构造即失败，此守卫是**统一归档边界的第二道防线**，
覆盖全部角色（防显式 `MessageId("")` 的 user/assistant 消息漏网）。

**位置（review C）：** 守卫置于 `append()` **顶部、`self._db is None` 分支之前**——
数据不变量与存储可用性无关，放后面会让「Mongo 离线时空 ID」走「DB 不可用」warning 而掩盖
不变量违规。**异常语义：** `append` 是 fire-and-forget 归档（`session.py` `_archive_done`
用 `task.result()` 捕获），守卫抛的 ValueError 会被降级为 warning、不崩主流程；真正同步
fail-fast 靠 D2 模型校验器（构造即抛），D4 是归档边界的兜底防线。

### D5：契约

**决策：** `frontend_contract.rest.Message.message_id` 加 `min_length=1`
（描述注明「归档消息 ID 非空不变量」）。收紧后 D2+D6 保证所有读出的消息 ID 非空，
校验不会误伤。重新生成 `contract.generated.ts` / `contract.schema.json` / `contract.raw.ts`。

**残余 drop 范围声明（review D）：** `_archive_msg_to_contract`（`api_routes.py:131-146`）
对校验失败返回 `None` + warning、该条从页内丢弃——D2 使正常 tool/存量消息恒非空，
此 drop 分支**只可能命中「无法派生」的畸形残余文档**（如缺 timestamp 的 tool、显式空 ID 的
user/assistant）。实测数据不命中（空 ID 100% 为带 tool_call_id+timestamp 的 tool 消息）；
对不存在的畸形残余，drop + warning 是现有兜底策略的延续，与问题清单 §5.3「不丢弃坏工具
消息」在**实际数据面**不冲突（丢弃的是无法修复的畸形文档，非真实工具历史）。

### D6：回填脚本（持久化迁移，推荐路径）

**决策：** 新增 `scripts/backfill_message_ids.py`，扫描并持久化：

1. **`message_archive`**：`role="tool"` 且 `message_id ∈ {"", None, 缺失}` → `$set` D1 派生 ID；
2. **`sessions.messages[]`**：同条件 → 更新快照内对应消息的 `message_id`。

**`sessions.messages[]` 更新机制（review E2）：** **禁止** positional `$`
（`messages.$.message_id` 只更新每条查询的第一个匹配元素，快照内多条空 ID 会漏改）；
**禁止** `arrayFilters $[elem]`（所有匹配元素赋同一个值，而每条派生 ID 各不相同）。
采用其一：① 逐元素按数组下标 `$set: {"messages.<idx>.message_id": derived}`；② Python 侧
取回快照 → 修改 `messages` 数组 → 一次整写 `$set: {messages: new_array}`（与
`session_repo.save` 的整写方式一致）。选择②时注意：整写只更新本次匹配到的字段，其它字段
原样保留（先读后改再写，避免并发覆盖）。

**CLI（review E）：** 提供 `--dry-run`（只扫描报告将改动的文档数，不写库）与默认实跑模式；
验收门禁 §7 引用 `--dry-run` 变更数为 0 作为幂等证明。

**幂等语义：** 只处理空/缺失 ID 的文档；已回填的文档不再匹配过滤器 → 重复运行零变化。
**无法派生**（缺 `tool_call_id`/`timestamp`，畸形文档）→ 跳过并 `logger.warning` 定位，
不静默丢弃、不清库。**不触碰**已有非空 ID 的文档（新旧消息身份永不被覆盖）。

**顺序要求（问题清单 §4.3）：** 回填先于（或并行于）契约 `min_length` 生效——D2 的读路径
派生已保证迁移前读不出空值，契约收紧不会造成 500 或丢页。

## 5. 组件改动清单

| 文件 | 改动 |
|---|---|
| `core/schema/message.py` | `ToolResultMessage.message_id` 必填 + 非空 `field_validator`（D2） |
| `core/schema/adapters/message_adapter.py` | `validate_message_legacy_compat` 入口对 tool 空 ID 派生 D1 |
| `core/utils/message_id.py` | 新增 `stable_tool_message_id(tool_call_id, timestamp)` 纯函数（D1 单源） |
| `core/cognitive/loop.py` | 两处 `ToolResultMessage(...)` 补 `message_id=new_message_id()` |
| `core/storage/message_repo.py` | `append()` 空 ID 抛 `ValueError`（D4） |
| `frontend_contract/rest.py` | `Message.message_id` 加 `min_length=1`（D5） |
| `scripts/backfill_message_ids.py` | 新增幂等回填脚本（D6） |
| `frontend/src/types/contract.generated.ts` + `contract.schema.json` + `contract.raw.ts` | 重新生成（D5） |

## 6. 测试要求（问题清单 §6 逐条落实）

1. `ToolResultMessage` 构造时传空/缺省 `message_id` → 校验拒绝；`MessageRepository.append`
   对空 ID（含 user/assistant 显式空串）抛 `ValueError`（D2/D4）；
2. `submit_task` 与普通工具执行后，session 与 `message_archive` 中的工具消息 ID 均非空
   （service 测试走真实 loop → session → archive；**先 drain fire-and-forget 归档任务
   （`session.close_archive`/`_archive_tasks`）再断言 archive 非空，防 flaky**）；
3. `/api/sessions/{id}` 与 `/api/sessions/{id}/messages` 返回的 user/assistant/tool 每条
   `message_id` 均非空（REST 契约测试）；
4. `validate_message_legacy_compat` 对缺字段（无 message_id / 空串）派生稳定 ID；派生往返
   跨 archive 与 session 快照一致；**补 JSONL fallback（`_load_from_jsonl`）往返用例——
   同一工具消息经三源（archive / 快照 / JSONL）读出同一稳定 ID（对齐 §4 核心不变量）**；
5. 回填脚本：缺字段/空串/多会话/多工具消息/重复运行零变化；迁移前后 `_id` 分页 cursor、
   消息数量与时间顺序不变，无重复、无漏项；
6. 同一条工具消息经 `session.to_client()`（WS replay）与 `_archive_to_client`（REST）读取
   ID 一致（或 WS 侧工具卡片不含 message_id 时，archive 侧读出的 ID 稳定且可重复）；
7. `python -m frontend_contract --check`（含 schema/raw_ts/golden frames 漂移）重新通过；
8. **搜索与上下文窗口出口（问题清单 4.2.4 补齐核查）**：`/api/search/messages` 结果中
   工具消息 `message_id` 非空且与 archive 一致（搜索经 `_doc_to_message` → 适配器派生，
   结构性已覆盖，补显式断言）；上下文窗口（LLM 历史构建）不暴露工具消息 ID
   （`_format_tool_summary` 用 tool_call_id+args 摘要、无 message_id 泄漏），断言历史构建
   不产出空/不一致的工具消息 ID。

## 7. 验收门禁

### 静态与自动化

- [ ] 模型非空不变量 + 两处写入路径 + 归档守卫（D2/D3/D4）测试通过；
- [ ] 回填脚本在真实 Mongo 数据上实跑：剩余空/缺失 ID 工具消息数 = 0；重复运行 `--dry-run`
      变更数为 0；
- [ ] `make test-quick` / Ruff / Pyright / `python -m frontend_contract --check` /
      `scripts/check_error_codes.py` 全绿；
- [ ] 契约三产物已重新生成且漂移门禁通过；
- [ ] 前端仓库运行 `check:contract` 重新通过（前端侧脚本由 `elysia-frontend` 执行；后端保证
      契约产物无漂移，前端脚本预期通过——作为联调恢复口径的配合项，非后端门禁）。

### 真实 Electron 回归（联调恢复口径，问题清单 §7.2）

- [ ] 打开现有 `default` 会话不再出现「会话消息响应校验失败」；
- [ ] 分页覆盖含历史工具结果的页，user/assistant/tool 均完整显示；
- [ ] 新会话普通工具 + `submit_task` 当轮流式正常；切走切回/刷新/重连后工具结果可读且不重复。

## 8. 明确不做（防范围蔓延）

- 不改前端契约让空 `message_id` 合法（问题清单 §5.3 禁止）；
- 不在每次 REST 请求时随机生成 ID（问题清单 §5.3 禁止）；
- 不从响应中丢弃坏工具消息（问题清单 §5.3 禁止）；
- 不清空 `default` 或整库（问题清单 §5.3 禁止）；
- 不把 Mongo `_id` 游标当消息 ID 冒充（问题清单 §5.3 禁止；本设计派生 `msg_` 值，非 `_id`）；
- 不实现 outbox/启动重入队（BE-RISK-01 依旧开放，与本批无关）。
