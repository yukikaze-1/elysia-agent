# Literal → StrEnum 统一重构设计

> **触发**：用户排查项目 `Literal` 使用（~85 处），判断部分应使用 `StrEnum`。决策：**全换**（非判别字段全部换 StrEnum）+ **枚举统一**（判别字段统一引用现有枚举成员做单一来源）+ 前端兼容性影响记录。
>
> **关联**：`docs/electron-backend-baseline.md`、`frontend_contract/`（契约单源）、`core/schema/`（共享类型）、`Module/tool/`、`Module/workflow_engine/`。
>
> **约束**：Pydantic discriminated union 的 discriminator 字段**必须是 `Literal`**（实测 `PydanticUserError: needs field to be of type Literal`）——判别字段不能改为 StrEnum 类型注解，只能统一 `Literal[Enum.MEMBER]` 引用形式。

## 1. 背景

项目 `Literal` 使用约 85 处，横跨 6 个文件族。其中：

- **判别字段**（discriminator）：`core/schema/`（`event.py` event_type、`memory.py` action、`part.py` type）+ `frontend_contract/ws_message.py`（`type`，~40 处）+ `rest.py` + `Module/workflow_engine/`。这些是 Pydantic `Field(discriminator=...)` 联合的判别键，**类型必须 Literal**。
- **非判别受限字段**：`channel`/`kind`/`status`/`service`/`finishReason`/`format`/`role`/`reason`/`action`(command) 等 **27 处**（实测 grep 统计），其中 25 处「全换」、2 处 `| str` 开放字段（D5）保持。可用 StrEnum 替代。

现状问题：
1. **枚举未引用**：`core/schema/agent.py` 已有 `EventType(StrEnum)`，但 `event.py` 用裸字符串 `Literal["interrupt"]`——同一语义两处定义，漂移风险；
2. **魔数散落**：`channel: Literal["inner_voice","public_reply","status"]` 在两处重复、`kind: Literal[8值]` 在两处重复、`finishReason: Literal["stop","tool_calls"]` 在两处重复——无单一来源；
3. **类型别名**：`Module/tool/protocol.py:18` `ResultType = Literal[5值]` 是命名别名，本可自然升级为 StrEnum。

已有先例（正确方向）：`core/schema/part.py` 判别字段用 `Literal[PartType.TEXT] = PartType.TEXT`（引用枚举成员）；`frontend_contract/ws_message.py` 已有 `ToolStatusEnum(str, Enum)` 供 `ToolStatusMessage.status` 字段引用，gen_ts 发射 `export type ToolStatusEnum = 'a'|'b'`。

## 2. 决策点（按用户指示执行）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 非判别字段换 StrEnum 的范围 | **全换** | 用户明确「全换」；StrEnum 提供类型安全、IDE 补全、防漂移 |
| D2 | 判别字段处理 | **枚举统一**（`Literal[Enum.MEMBER]`） | Pydantic 强制 discriminator 类型为 `Literal`，不能换 StrEnum 注解；但判别值应引用现有枚举成员（如 `EventType`）做单一来源，消除裸字符串 |
| D3 | 前端 gen-ts 兼容性 | **记录，不阻塞** | StrEnum 字段经 gen_ts 发射为命名类型别名（`export type X = 'a'|'b'`），字段类型从内联字面量 `'a'|'b'` 变引用 `X`——TS 形状变化需前端同步，但属低风险（gen_ts `_referenced_enums` 已支持，`ToolStatusEnum` 已验证）。记录到文档，不在此批次改前端 |
| D4 | bool 判别（`ok: Literal[True]/[False]`） | **保持** | StrEnum 不适用 bool；`_literal` 已处理 bool 小写 |
| D5 | `Literal[...] | str` 开放扩展字段 | **保持** | `ActionMessage.action`/`CommandMessage.command` 是「白名单 + 开放」语义，StrEnum 无法表达 `| str` 开放端；换枚举会破坏未知值透传 |

## 3. 枚举定义（新建）

跨包共享枚举放置策略：`frontend_contract` 是契约单源（不能 import core/Module）；`core/schema` 是 core 层共享类型。为避免跨层反向依赖，枚举按**消费方归属**定义：

| 枚举 | 值 | 归属 | 消费方 |
|---|---|---|---|
| `MessageChannel` | inner_voice / public_reply / status | `frontend_contract/ws_message.py` | GenerationChannel（`channel` 字段）+ GenerationCompletedMessage.channel |
| `SegmentKind` | text / markdown / image / video / audio / file / sticker / link | `frontend_contract/ws_message.py` | GenerationSegment（`kind` 字段） |
| `FinishReason` | stop / tool_calls | `frontend_contract/rest.py` | GenerationStatus.finishReason（rest.py:345）+ GenerationCompletedMessage.finishReason（ws_message.py:321，ws_message 已 import rest） |
| `AudioFormat` | wav / mp3 | `frontend_contract/ws_message.py` | AudioMessage/AudioStartMessage（`format`） |
| `ServiceName` | stt / tts / llm | `frontend_contract/ws_message.py` | ServiceModeResultMessage/SetServiceMessage（`service`） |
| `VoiceEndReason` | stopped / cancelled / timeout / stt_disabled | `frontend_contract/ws_message.py` | VoiceSessionEndedMessage（`reason`） |
| `AckStatus` | accepted / rejected | `frontend_contract/ws_message.py` | UserAckMessage（`status`） |
| `Role` | user / assistant / tool | `frontend_contract/rest.py`（**契约层自建**，值对齐 `core.schema.message.MessageRole`，不 import core） | GenerationStatus.role / rest.py role 字段 |
| `ToolResultType` | text / image / audio / file / mixed | `Module/tool/protocol.py` | ToolResult.result_type（原 `ResultType` 别名） |
| `ToolStatus` | pending / processing / done / failed | `Module/tool/protocol.py` | ToolRequest.status |
| `ExperienceStatus` | unverified / verified / deprecated | `Module/workflow_engine/schema/internal.py` | ExperienceRecord.status（internal.py:540）。**不能用 `WorkflowStatus`**——`Module/workflow_contract/schema.py:64` 已有 11 态 WorkflowStatus（PENDING…COMPLETED）且 internal.py:18 已 import |
| `RepoCallStatus` | pending/accepted/running/succeeded/failed/rejected/cancelled/expired/offline | `frontend_contract/rest.py` | DesktopCallStatus.status |
| `GenerationStatusEnum` | running / completed / failed / cancelled / interrupted | `frontend_contract/rest.py` | GenerationStatus.status |
| `VoiceReason` | silence_timeout / time_of_day | `core/schema/event.py` | SystemTickData.reason |

> **判别值引用枚举（D2）**：`event.py` 判别字段改为 `event_type: Literal[EventType.INTERRUPT] = EventType.INTERRUPT`（复用 `core/schema/agent.py` 已有 `EventType`）；`memory.py` 的 `action` 判别字段引用新建 `CompareActionType` 枚举（**不能用 `CompareAction`**——该名已被 `memory.py:59` 的判别联合别名占用）。`part.py` 已是最佳形式，不动。

## 4. 改动文件

### 4.1 `frontend_contract/ws_message.py`
- 新建枚举：`MessageChannel`、`SegmentKind`、`AudioFormat`、`ServiceName`、`VoiceEndReason`、`AckStatus`；
- `channel: Literal[...]` ×2 → `MessageChannel`；`kind: Literal[...]` ×2 → `SegmentKind`；`format: Literal["wav","mp3"]` ×2 → `AudioFormat`；`service: Literal[...]` ×2 → `ServiceName`；`reason: Literal[4值]` → `VoiceEndReason`；`UserAckMessage.status: Literal["accepted","rejected"]` → `AckStatus`；`GenerationCompletedMessage.finishReason: Literal["stop","tool_calls"]` → `FinishReason`（from rest import）；
- **恒值字段统一**：`GenerationCompletedMessage.channel: Literal[MessageChannel.PUBLIC_REPLY] = MessageChannel.PUBLIC_REPLY`（**必须 `Literal[...]` 包裹**——裸枚举成员注解 `channel: MessageChannel.PUBLIC_REPLY` 会让 gen_ts `_ts_type` 落到 `__name__` 抛 AttributeError，review 实证）；
- **严格性保持**：`SetServiceMessage.service: Literal["stt","tts"]` → `Literal[ServiceName.STT, ServiceName.TTS]`（**不直接用 `ServiceName`**——那会加宽契约接受 "llm"；原契约仅 STT/TTS）。`ServiceModeResultMessage.service: Literal["stt","tts","llm"]` → `ServiceName`；
- 判别字段 `type: Literal["..."]` 保持（Pydantic 约束）；
- `ActionMessage.action` / `CommandMessage.command` 保持（D5）。

### 4.2 `frontend_contract/rest.py`
- 新建枚举：`FinishReason`、`RepoCallStatus`、`GenerationStatusEnum`、`Role`（契约层自建，值 = user/assistant/tool）；
- `finishReason: Optional[Literal["stop","tool_calls"]]` ×2 → `Optional[FinishReason]`；
- `status: Literal[9值]`（DesktopCallStatus）→ `RepoCallStatus`；
- `status: Literal["running","completed","failed","cancelled","interrupted"]`（GenerationStatus）→ `GenerationStatusEnum`；
- `role: Literal["user","assistant","tool"]` → 契约层自建 `Role`（**不能 import core/**，§5.1）；
- `ok: Literal[True]/[False]` 保持（D4）。

### 4.3 `core/schema/event.py`
- 判别字段 `event_type: Literal[EventType.X] = EventType.X`（引用 `agent.py` 已有 `EventType`）；
- `reason: Literal["silence_timeout","time_of_day"]` → 新建 `VoiceReason` 枚举。

### 4.4 `core/schema/memory.py`
- 判别字段 `action: Literal["skip"]` 等 ×4 → 引用新建 `CompareActionType` 枚举（`Literal[CompareActionType.SKIP]`）。**命名注意**：不能用 `CompareAction`——该名已被判别联合别名占用（`memory.py:59` `CompareAction = Annotated[...]`）。

### 4.5 `core/schema/message.py`
- `ClientToolMessage.role: Literal["tool"]`（TypedDict）→ `MessageRole.TOOL`（已有枚举）。

### 4.6 `Module/tool/protocol.py`
- `ResultType` 别名 → `ToolResultType(StrEnum)`；`status: Literal[4值]` → `ToolStatus(StrEnum)`。

### 4.7 `Module/workflow_engine/schema/internal.py`
- `ExperienceRecord.status: Literal[3值]` → `ExperienceStatus(StrEnum)`（**不能用 `WorkflowStatus` 命名**——internal.py 已 import `Module/workflow_contract` 的 11 态 `WorkflowStatus`）。

### 4.8 `frontend_contract/tests/test_gen_ts.py` + `test_gen_ts` 扩展
- 新增 StrEnum → TS 别名 + 字段引用测试（`ToolStatusEnum` 先例已覆盖，补非判别枚举）。

### 4.9 恒值字段 carve-out（明确保持 Literal，非遗漏）
按 D1「全换」仅覆盖**多值**受限字段；以下**恒值**字段保持 `Literal`（单值无枚举收益，gen_ts 会发射单成员别名噪音；`type: "function"` 另对齐 OpenAI wire 格式）：
- `core/schema/tool.py:37` `ToolCall.type: Literal["function"]`；
- `frontend_contract/ws_message.py:100` `ToolCallData.type: Optional[Literal["function"]]`；
- `ws_message.py:325` `GenerationCompletedMessage.completeness: Literal["complete"]`；
- `ws_message.py:320` `channel` 属例外——MessageChannel 已含 `public_reply`，引用统一（见 4.1）。

## 5. 依赖约束与风险

1. **frontend_contract 零依赖**：契约层不能 import `core/`/`Module/`。`rest.py` 的 `role` 需要 `MessageRole`——**必须**在 `frontend_contract/rest.py`（或 ws_message.py）内定义契约层自己的 `Role(StrEnum)`，不能 import `core.schema.message.MessageRole`。core 层字段若类型是 `core.schema.message.MessageRole` 而契约层是 `frontend_contract.Role`——两者值相同（都是 "user"/"assistant"/"tool"），互转靠字符串值，无强类型耦合。需确认 `_make_agent`/序列化路径不依赖同类型。
2. **gen_ts 三重门禁**：改 `frontend_contract/` → `make gen-types` + `make check-types`；前端 `contract.generated.ts` 消费点（`status`/`channel`/`kind`/`finishReason`/`service`/`format`）TS 形状从内联字面量变命名别名——需跑 `cd frontend && npx vue-tsc --noEmit` 确认。
3. **判别联合行为不变**：判别字段保持 `Literal[Enum.MEMBER]`，wire 值不变（StrEnum 序列化为字符串值），pydantic 校验不变。既有测试应全绿。
4. **`_referenced_enums` 与判别联合字段共存**：`type: Literal[PartType.TEXT]` 中枚举成员引用 vs 字段类型引用——gen_ts 的 `_referenced_enums` 只收集「字段类型注解是 Enum 子类」的；`Literal[Enum.MEMBER]` 的 get_args 是 enum 成员而非 Enum 类，不会被收集为命名别名（保持内联字面量）——需实测确认不产生幽灵枚举。
5. **StrEnum vs `(str, Enum)`**：项目两种写法并存（`core/schema` 用 `StrEnum`，`frontend_contract` 用 `(str, Enum)`）。统一用 `StrEnum`（Python 3.11+，项目要求 3.11+）。

## 6. 测试策略

- 契约层：`make gen-types` + `make check-types`（gen_ts 输出漂移门禁）；
- 前端：`cd frontend && npx vue-tsc --noEmit`（消费点 TS 形状变化验证）；
- 后端：`pytest -m "unit or service"`（排除 workflow_engine）全绿——重点 `core/schema/tests/`、`frontend_contract/tests/`、`Module/tool/tests/`；
- 新增：gen_ts 对非判别 StrEnum 的发射测试（若 `ToolStatusEnum` 测试已覆盖则复用）。

## 7. 已知限制（记录，不阻塞）

- **前端 TS 形状变化**：非判别字段从内联 `'a'|'b'` 变命名别名 `export type X`——前端代码若直接写字符串字面量比较（如 `msg.channel === 'inner_voice'`）仍兼容（值不变）；仅类型引用需感知新别名。记录，不在此批次改前端。
- **契约层 role 枚举双定义**：frontend_contract 需自建 `Role`，与 `core.schema.message.MessageRole` 值等价但类型不同——无法强类型统一（契约层零依赖约束），靠字符串值互转。
- `Literal[...] | str` 开放字段保持（D5）：`ActionMessage.action`/`CommandMessage.command` 换枚举会破坏未知值透传。
- **恒值字段保持 Literal（carve-out）**：`ToolCall.type: "function"`、`ToolCallData.type: "function"`、`completeness: "complete"` 为单值恒键，换枚举无收益且 gen_ts 发射单成员别名噪音（§4.9）。
- **str Enum 命名风格**：本项目 `core/schema` 用 `StrEnum`、`frontend_contract` 现有 `ToolStatusEnum` 用 `(str, Enum)`——统一新建枚举用 `StrEnum`（§5.5）。
