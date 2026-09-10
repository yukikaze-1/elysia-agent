# core ID 体系 NewType 化 — 设计文档

> **来源**：全项目数据结构排查（2026-08-08）发现 ID 类型体系不一致——`core/schema/types.py`
> 已定义 `MessageId`/`ToolCallId`（`SessionId` 由契约层 `Module/workflow_contract/schema.py` 拥有、core 再导出），
> 且 `storage/session_repo.py`、`storage/message_repo.py` 已全量使用 `session_id: SessionId` / `message_id: MessageId`，
> 但 **BE-024 generation 管线（`GenerationContext` / `generation_registry.py` / `generation_repo.py`）与
> 散落路径（`UserInputData.message_id` / `_PendingMessage` / `VersionEntry.session_id` / `CognitiveLoop` 参数）全部退回裸 `str`**——
> 同一 ID 在存储层有类型标注、在生成/编排/注册链路裸传，类型保护在跨层时断裂。
> 同时 `generation_id`（gen_）/ `segment_id`（seg_）/ `request_id` 尚无 NewType 定义。
>
> `frontend_contract/`（rest.py/ws_message.py/debug.py）大量 id 字段用裸 `str` 是**架构正确**的：
> frontend_contract 是零依赖契约层（被 `core` 反向依赖，不 import core），NewType 无运行时意义
> （pydantic 按 str 处理）、`gen_ts` 生成 TS 恒为 `string`——**本次不动 frontend_contract**。

**Goal**：core 内部 ID 类型全面对齐 NewType——现有 `MessageId`/`SessionId`/`ToolCallId` 补齐到散落裸 `str` 处，
新增 `GenerationId`/`SegmentId`/`RequestId` 并从生成函数 → 流转 → 存储/注册链路贯穿；
input 边界（wire 字符串入口）保留裸 `str`，在进入 core 的收口点显式包一层。**运行时零变化**——纯静态类型标注。

**Architecture**：单层改动。`core/schema/types.py` 新增 3 个 NewType（叶子模块，零依赖风险）；
ID 生成函数返回类型改为 NewType；`GenerationContext` / `generation_registry` / `generation_repo` /
`CognitiveLoop` 参数 / `UserInputData` / `_PendingMessage` / `VersionEntry` 签名对齐；
`Agent.process_message` 与 ws 构造 `UserInputData` 处作为收口点把 wire 裸 `str` 包成 NewType。

**Tech Stack**：Python 3.12 `typing.NewType`（纯静态，运行时即 str）。测试 pytest（marker=unit/service），
conda 环境 `Elysia`。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入：`from core.schema.types import GenerationId`。
- 代码注释/README 用中文；中英混合（中文嵌英文技术术语）可接受。
- **NewType 运行时即 str**：不做 `.value` 拆包，NewType 直接传给 frontend_contract 裸 `str` 参数
  （NewType 是 str 子类型，静态类型安全）。项目无 mypy/pyright 强制——NewType 是 IDE 级文档标注，
  本批次是类型卫生对齐，**不改变任何运行时行为与 wire 字节**。
- `MemoryType` 成员值不变：`FACT="Fact" PREFERENCE="Preference" EVENT="Event" PLAN="Plan" NOTE="Note"`。
- 不新增 `__missing__` 等魔法。NewType 是 `typing.NewType`，非类，不可加方法。

---

## 现状分析（触及点全量枚举）

### NewType 定义现状（3 处）

| 定义处 | NewType | 说明 |
|--------|---------|------|
| `core/schema/types.py:21,24` | `MessageId`、`ToolCallId` | core 自己拥有 |
| `core/schema/types.py:7` | `SessionId`（再导出） | 由 `Module/workflow_contract/schema.py:45` 拥有，core 侧重导出保持公开 API |
| `Module/workflow_contract/schema.py:26-57` | `WorkflowId`/`AgentId`/`ApprovalId`/`SessionId`/`NodeId` | 后端跨边契约层（叶子） |
| `Module/workflow_engine/schema/types.py:66` | `ArtifactId` | workflow_engine 内部 |

### 已对齐路径（零改动）

| 路径 | 位置 | 说明 |
|------|------|------|
| `SessionMemory` / `SessionManager` | `core/memory/session.py:47,79,96,109`、`session_manager.py:36,81,82,98,112,...` | `SessionId` 全程贯通 |
| storage session_repo | `session_repo.py:133,166,187,205,233` 等 | `save/create/rename/load/delete` 全 `session_id: SessionId` |
| storage message_repo | `message_repo.py:156,175,226,254,297,325,359,455` | `session_id: SessionId` / `message_id: MessageId` |
| schema 消息/工具 | `message.py:34,44,51,59,66,69`、`tool.py:36` | `message_id: MessageId` / `session_id: SessionId` / `tool_call_id: ToolCallId` |
| ID 生成 | `message_id.py:14-20` | `new_message_id() -> MessageId` |
| tool_call_parser | `adapter/tool_call_parser.py:67` | `id=ToolCallId(tc["id"])` |
| schema event | `event.py:44` | `conversation_id: SessionId | None`（**message_id 仍裸 str，见下**） |

### 需对齐路径（本次改动，核心）

| # | 来源 | 值形态 | 位置 |
|---|------|--------|------|
| 1 | `new_generation_id()` | `gen_` 前缀 str | `message_id.py:23-25` → 返回 `GenerationId` |
| 2 | `new_segment_id()` | `seg_` 前缀 str | `message_id.py:28-30` → 返回 `SegmentId` |
| 3 | `next_request_id()` | 每请求独立 ID | `agent/server.py:132` → 返回 `RequestId` |
| 4 | `GenerationContext` | 6 个 ID 字段全裸 str | `cognitive/generation.py:36-48` |
| 5 | `GenerationRegistry` | 签名全裸 str | `cognitive/generation_registry.py:58,96,129,137,152` |
| 6 | `GenerationRepository` | 签名全裸 str | `storage/generation_repo.py:160,167,186`（落后于 session_repo/message_repo 惯例） |
| 7 | `CognitiveLoop.process` 等 | 参数裸 str | `cognitive/loop.py:368-377,564-565,725,894,1063-1064` |
| 8 | `UserInputData.message_id` | `str | None` | `schema/event.py:41`（request_id 亦裸 str:36） |
| 9 | `_PendingMessage` | 字段裸 str | `agent/_types.py:20-22` |
| 10 | `VersionEntry.session_id` | 裸 str | `schema/compression.py:14` |

### 收口点（input 边界裸 str → 包一层；区分「wire 裸 str 需包」与「已是 NewType 不需包」）

| # | 位置 | 需收口的字段 | 已是 NewType 不需包的字段 |
|---|------|------------|--------------------------|
| A | `Agent.process_message`（agent.py:309-313） | `message_id`、`conversation_id`（wire 裸 str） | — |
| B | `loop.process` 内（loop.py:472,476） | `user_message_id`（`message_id or ""` 产出的空串）、`request_id`（`get_request_id()` 保持 str） | — |
| C | `ws_handler.py:735` 构造 `UserInputData` | `conversation_id`（wire 裸 str） | `message_id`（`new_message_id()`→MessageId）、`request_id`（`agent.next_request_id()`→RequestId） |
| D | `voice_sessions.py:513` `_build_user_input` | `conversation_id`（`self._conv_id` wire 裸 str） | `message_id`、`request_id`（同上） |
| E | `ws_handler.py:1169` | — | `request_id`（`next_request_id()`→RequestId） |
| F | `session_routes.py:643,730,744` REST 边界 | `registry.find_by_message`/`query_by_session`/`get` 的 id 参数（wire 裸 str） | — |
| G | `agent.py:596` 出队重建 `UserInputData` | —（`_PendingMessage` §6 对齐后自动一致） | `message_id`/`conversation_id`/`request_id` |

### 保持 str（明确不改）

- **frontend_contract 全部**（rest.py/ws_message.py/debug.py）：零依赖架构，不能引 core NewType。
- **input 层接收 wire 的裸 str 变量**（`ws_handler` 的 `data.get("conversationId")` 等）：收口点之后才 NewType。
- **wire 层 ID：`AttachmentId`（att_）/`CallId`（call_）/`StickerId`（st_）/`RecordingId`（rec_）**：
  不在 core 内部生成→存储→查询链路的核心路径，且 frontend_contract 无法互通 core NewType，留待单独决策。
- `GenerationContext.model_id`：模型名非 ID 语义，保持 str。
- cursor（base64 keyset 游标）/状态字符串：非 ID 语义。

---

## 设计

### 1. 新增 NewType（`core/schema/types.py`）

```python
GenerationId = NewType("GenerationId", str)
"""生成实例唯一标识符（gen_ 前缀）。"""

SegmentId = NewType("SegmentId", str)
"""生成分段唯一标识符（seg_ 前缀）。"""

RequestId = NewType("RequestId", str)
"""请求唯一标识符（`{_req_counter:03d}` 三位计数器格式，非 UUID——与 §2 server.py:132 实现一致）。"""
```

新增后 `core/schema/__init__.py:17-22` 导出列表补 `GenerationId`/`SegmentId`/`RequestId`。

### 2. 生成函数返回类型

```python
def new_generation_id() -> GenerationId:
    """生成一次生成实例 ID（'gen_' 前缀，与 messageId 语义区分，批次 2）。"""
    return GenerationId(_new_id("gen"))

def new_segment_id() -> SegmentId:
    """生成一个分段 ID（'seg_' 前缀，批次 2）。"""
    return SegmentId(_new_id("seg"))
```

`core/agent/server.py:132`：`def next_request_id(self) -> RequestId`（内部 `_req_counter` 自增 + `set_request_id(rid)` 逻辑不变，
return 处 `RequestId(rid)` 包一层）。注意实际 rid 是 `{_req_counter:03d}` 三位计数器格式（非 UUID）。

### 3. `GenerationContext`（`core/cognitive/generation.py`）

```python
generation_id: GenerationId
user_message_id: MessageId          # 必填，保持原语义（原 str 必填 + 构造处 `message_id or ""` 保证非空）
assistant_message_id: MessageId
conversation_id: SessionId | None = None
model_id: str = ""
request_id: RequestId | None = None
...
current_segment_id: SegmentId | None = None
```

`open_segment` 返回 `SegmentId`（`self.current_segment_id = new_segment_id()` 已是 SegmentId）。
构造处 `user_message_id` 的裸 str 空串由收口点 B 处理（loop.py:472 `MessageId(message_id or "")`）。

### 4. 存储/注册链路（对齐 session_repo/message_repo 既有惯例）

`core/cognitive/generation_registry.py`：

```python
async def register(self, generation_id: GenerationId, *, session_id: SessionId,
                   user_message_id: MessageId, message_id: MessageId, model: str,
                   request_id: RequestId | None = None) -> None:
async def mark(self, generation_id: GenerationId, *, status: str, ...) -> None:
async def get(self, generation_id: GenerationId) -> dict | None:
async def query_by_session(self, session_id: SessionId, ...) -> ...:
async def find_by_message(self, message_id: MessageId) -> dict | None:
```

`core/storage/generation_repo.py`：`find_one(generation_id: GenerationId)`、
`find_by_message(message_id: MessageId)`、`find_by_session(session_id: SessionId, ...)` 对应对齐。
`_encode_cursor(created_at: str, generation_id: str)` 保持 str（cursor 是 base64 编码，非 ID 语义）。

### 5. `CognitiveLoop`（`core/cognitive/loop.py`）

`process`/`_build_turn_prompt`/`_think_loop` 及内部 helper 的
`message_id: MessageId | None`、`conversation_id: SessionId | None` 对齐。
`process` 内 GenerationContext 构造处两处收口（非零收口，见收口点 B）：
- `rid = get_request_id()`（`core/utils/log_setup.py:29`）保持返回 str（日志工具通用性，默认 `"-"`）+ `P{_req_counter}` 兜底；
  构造 GenerationContext 处 `RequestId(rid)` 包一层（loop.py:476，`request_id=rid` 所在行）。`get_request_id`/`set_request_id` 签名不改。
- `user_message_id=message_id or ""`（loop.py:472）→ `MessageId(message_id or "")`——保持必填语义
  （原 `user_message_id: str` 必填 + `or ""` 保证非空，对齐后 MessageId 字段不持有裸 str）。
registry 调用处（loop.py:483-492）已持有 NewType，直接传入。
`loop.py:317` `return self._generation_ctx.user_message_id or None` 等返回给 wire 处保持直接返回
NewType（NewType 是 str 子类型，传给 frontend_contract 裸 `str` 参数类型安全，**不做 `.value` 拆包**——决策记录 §1）。

### 6. 散落对齐

- `schema/event.py:41`：`message_id: MessageId | None`；`:36` `request_id: RequestId`（conversation_id 已 SessionId）。
- `agent/_types.py:17-22`：`_PendingMessage.message_id: MessageId | None`、`conversation_id: SessionId | None`、
  `request_id: RequestId`（request_id 在 :17，message_id/conversation_id 在 :20-22）。
- `schema/compression.py:14`：`VersionEntry.session_id: SessionId`。

### 7. 收口点实现（完整清单——区分「wire 裸 str 需包」与「已是 NewType 不需包」）

**A. `Agent.process_message`（agent.py:309-313）**：签名保持 `message_id: str | None` / `conversation_id: str | None`（公开 wire 入口），
  函数开头统一：
  ```python
  message_id_nt = MessageId(message_id) if message_id else None
  conversation_id_nt = SessionId(conversation_id) if conversation_id else None
  ```
  后续 `_PendingMessage(...)`（agent.py:348-353）与 `UserInputData(...)`（agent.py:356-360）用 `_nt` 变量
  （`request_id=rid` 已由 §2 `next_request_id() -> RequestId` 保证）。

**B. `CognitiveLoop.process` 内（loop.py:472,476）**：
  `user_message_id=MessageId(message_id or "")`（:472）、`request_id=RequestId(rid)`（:476）——
  `get_request_id()`（log_setup.py:29）保持 str，此处收口（含 `P{_req_counter}` 兜底值）。

**C. `ws_handler.py:735` 构造 `UserInputData`**：`message_id` 来自 `new_message_id()`（MessageId）、
  `request_id` 来自 `agent.next_request_id()`（RequestId）——**已是 NewType 不需包**；仅
  `conversation_id=conversation_id`（wire 裸 str）需收口 `SessionId(conversation_id) if conversation_id else None`。

**D. `voice_sessions.py:513` `_build_user_input`**：同 C——`message_id`（`new_message_id()`）、
  `request_id`（`next_request_id()`）已是 NewType 不需包；仅 `conversation_id=self._conv_id`（wire 裸 str）
  收口 `SessionId(self._conv_id) if self._conv_id else None`。

**E. `ws_handler.py:1169`**：仅 `request_id=rid`（`next_request_id()` = RequestId）——无需收口。

**F. `session_routes.py` 三处 REST 输入边界**（wire 裸 str → 将变 NewType 的 registry 参数）：
  `:643` `registry.find_by_message(MessageId(str(message_id)))`、
  `:730` `registry.query_by_session(SessionId(sid), ...)`（sid 来自 `request.match_info["conversationId"]`）、
  `:744` `registry.get(GenerationId(gid))`（gid 来自 `request.match_info["generationId"]`）。

**G. `agent.py:596` 出队重建 `UserInputData`**：字段来自 `_PendingMessage`（§6 对齐后 message_id/
  conversation_id/request_id 已是 NewType）——无需收口，自动一致。

---

## 行为变化（须在最终报告中声明）

1. **运行时零变化**：NewType 即 str，pydantic 无校验差异、wire 字节不变、TS 生成不变。
2. **静态类型变化**：ID 生成函数返回值、`GenerationContext` 字段、registry/repo 签名、
   `CognitiveLoop` 参数、`UserInputData`/`_PendingMessage`/`VersionEntry` 字段从裸 `str` 变为 NewType。
   input 边界（`Agent.process_message`、ws 层）保持裸 `str`，收口点显式包一层。
3. **IDE 提示提升**：generation 管线的 ID 流转不再跨层降级，静态检查可区分 `message_id`/`session_id`/`generation_id`。
4. 现有测试中直接传裸 str 给 NewType 参数的调用（`test_generation_registry.py`、`test_session_resolver.py` 等）
   **运行时仍通过**（NewType 是 str），但按类型卫生适配为包一层（见测试策略）。

## 测试策略

**新增/增强（marker=unit）：**
- `test_types.py` `TestNewTypes` 补：`GenerationId`/`SegmentId`/`RequestId` 构造后是 str（isinstance str + 值相等，
  沿用既有 `test_session_id_is_str` 模式）。
- `test_generation.py` 现有 `test_new_generation_id_format`（:10-14）/`test_new_segment_id_format`（:17-22）
  补返回类型断言：`get_type_hints(new_generation_id)["return"] is GenerationId` /
  `get_type_hints(new_segment_id)["return"] is SegmentId`（**不新建 test_message_id.py**——
  review 发现现有测试已覆盖格式，仅在原文件补类型断言）。**注意：NewType 非 class，
  `isinstance(x, GenerationId)` 抛 `TypeError: isinstance() arg 2 must be a type`（Python 3.12 实测）——
  必须用 `get_type_hints` 查注解，不可 isinstance。**

**适配现有测试（核实后清单）：**
- `test_generation.py:28-30,48-50`：`GenerationContext(generation_id="gen_1", user_message_id="msg_u1",
  assistant_message_id="msg_a1")` 裸 str 构造 → `GenerationContext(generation_id=GenerationId("gen_1"),
  user_message_id=MessageId("msg_u1"), assistant_message_id=MessageId("msg_a1"))`。
- `test_generation_registry.py:35-36,50-51,69-71,81-83,110` 等：`register("gen_1", session_id="s1", ...)`
  → `register(GenerationId("gen_1"), session_id=SessionId("s1"), user_message_id=MessageId("msg_u1"), ...)`
  （类型卫生，运行时本就通过；`assert` 比对 doc["session_id"] == "s1" 不变——存库仍是 str）。
- `test_session_resolver.py:91,142,144` 等：`loop.process(conversation_id="conv_x")`
  → `conversation_id=SessionId("conv_x")`（注意 :108 是 `process("hello")` 无会话参数，无需改）。
- `test_generation_model.py` / `test_generation_wiring.py` / `test_v2_generation.py` / `test_v2_empty_response.py`：
  若直接构造 `GenerationContext(...)` 传裸 str，适配为 NewType（mock/断言不受影响——mock 断言比对参数本身，
  若断言 `assert_awaited_once_with(..., message_id=...)` 传的是 NewType 则原样）。

**回归**：`make test-normal`（`-m "not slow"`）+ 受影响模块定向跑
（core/cognitive、core/input/tests、core/agent/tests、core/schema/tests、core/storage/tests）。

## Out of Scope

- 不动 frontend_contract（零依赖架构正确）。
- 不新增 wire 层 ID NewType（AttachmentId/CallId/StickerId/RecordingId）。
- 不引入 mypy/pyright 门禁（本批次是类型卫生对齐，非工具链改造）。
- 不做 `.value` 拆包（NewType 直接透传 wire）。
- 不动 `GenerationContext.model_id` / cursor / 状态字符串。

## 决策记录（用户 /goal 要求记录，最终报告提出）

1. **wire 边界不做 `.value` 拆包**：NewType 是 str 子类型，直接传给 frontend_contract 裸 `str` 参数类型安全；
   强制 `.value` 只增加样板、无运行时收益。按推荐定案（设计草案中唯一开放取舍，用户未反驳）。
2. **新增全套 NewType（GenerationId/SegmentId/RequestId）**：用户 AskUserQuestion 拍板选"新增全套"而非
   "仅对齐现有 3 个"。范围由此扩大——`new_generation_id`/`new_segment_id`/`next_request_id` 返回类型与
   generation 管线全链对齐。wire 层 ID（att/rec/st/call）不在其列（用户选项文字聚焦核心 ID，frontend_contract 无法互通）。
3. **input 边界保留裸 str + 收口点包一层**：`Agent.process_message` 与 ws 层保持裸 str（wire 字符串入口诚实），
   收口集中在两处（process_message 开头、UserInputData 构造点），避免 input 层全量 NewType 化。
4. **storage 层对齐**：session_repo/message_repo 已有 `SessionId`/`MessageId` 先例，
   generation_repo 是 BE-024 新批次未沿用惯例的疏漏——对齐即修复疏漏，非引入新模式。
5. **`get_request_id()`/`set_request_id()` 保持 str**（review 修复，记录）：日志工具 ContextVar 默认 `"-"`、
   通用读取不属严格 ID 语义，签名不改；`RequestId` 收口集中在 loop 构造 GenerationContext 处（loop.py:476）。
6. **收口点按「来源」区分，不重复包层**（review 修复）：`ws_handler:735`/`voice_sessions:513` 的
   `message_id`（`new_message_id()`）与 `request_id`（`next_request_id()`）§2 对齐后已是 NewType，
   仅 wire 裸 str 的 `conversation_id` 需收口；`agent.py:596` 出队重建由 `_PendingMessage` 对齐自动一致，
   无需额外收口。

## Spec Review 修复记录（code-review 后并入）

| # | Review 发现 | 修复 |
|---|------------|------|
| 1 | `request_id` 经 `get_request_id()`（str）流入 GenerationContext/registry，§5"零收口"失真 | 改为两处收口（loop.py:472,476），`get_request_id` 保持 str |
| 2 | `session_routes.py:643,730,744` REST 边界把 wire 裸 str 传入将变 NewType 的 registry 参数，未列收口 | 补收口点 F |
| 3 | `test_generation.py:28-30,48-50` 裸 str 构造 GenerationContext 漏配；且其 :10-22 已有 gen/seg 格式测试，"新增 test_message_id.py" 与现有重复 | 适配清单补 test_generation.py；新增测试改为在原文件补类型断言 |
| 4 | `user_message_id=message_id or ""`（loop.py:472）产裸 str 空串进 MessageId 字段 | §3/§5 明确 `MessageId(message_id or "")` 收口 |
| 5 | 行号失准：`_PendingMessage.request_id` 实为 :17；`test_session_resolver` conversation_id 在 91/142/144（:108 无）；next_request_id 内部 `_req_counter` | 全部修正 |
| 6 | §7 B 对已 NewType 的 `message_id`/`request_id` 重复包一层；`agent.py:596` 出队重建漏列 | 收口点表按来源区分 C/D/E/G；重复包层移除 |

## Self-Review（已执行）

1. **Spec 覆盖**：新增 3 NewType、生成函数返回类型、GenerationContext、registry/repo、CognitiveLoop 参数、
   散落对齐（UserInputData/_PendingMessage/VersionEntry）、收口点、测试适配、行为声明——全部映射到 §1-7。
2. **占位符扫描**：无 TBD/TODO；每处含可执行签名与位置。
3. **类型一致性**：`GenerationId`/`SegmentId`/`RequestId` 定义在 §1，§2-6 引用一致；
   收口点 `MessageId(x) if x else None` 模式在 §7 统一。
4. **已知耦合**：§7 收口点是原子变更（`UserInputData.message_id` 改 NewType 立即破坏
   ws_handler/voice_sessions 裸 str 构造点，registry 签名改 NewType 立即破坏 session_routes 调用点——
   故收口点 A-G 与签名对齐同 spec 内完成）；frontend_contract 与 wire 层 ID 独立不动。
