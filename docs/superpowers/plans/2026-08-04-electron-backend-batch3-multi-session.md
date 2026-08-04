# Electron 后端改造 — 批次 3：产品级多会话、历史、搜索与资源索引（Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 版本：** v0.1
**依据 Spec：** `docs/superpowers/specs/2026-08-04-electron-backend-batch3-multi-session.md`（v0.2）
**前置：** 批次 0 基线 + 批次 1 + 批次 2（`user_message.ack`/`generation.*`/幂等/取消，HEAD `d6e4c91`）
**执行环境：** Elysia conda（`conda activate Elysia`）

---

## 概览

批次 3 把后端从单活动会话升级为产品级多会话（BE-030）+ 历史/上下文/搜索（BE-031）+ 资源索引（BE-032）+ BE-024 剩余（generation 状态/pending/增量拉取/终态重放）。核心是 **SessionManager 缓存池 + 活动指针**（D-c）：`agent.session` 动态返回 active；v1 连接恒绑定 `"default"`、v2 缺省 active、v2 显式 `conversationId` 覆盖（D-a/D-l）。

**硬约束**：v1 wire 逐字节不变；`/api/messages` 零改动；`contract.generated.ts` v1 类型字节不变（仅 v2/新类型增量）；现有 1951 测试全绿。**TDD：每个任务先写失败测试。**

## Global Constraints

- 禁相对导入（绝对导入，`make lint` 强制）；`__init__.py`/`__main__.py` 豁免。
- 时间戳一律 `now_iso()`（UTC ISO 8601 `+00:00`）。
- 新测试必须带 marker（unit/service），日常 `make test-quick`。
- REST wire 字段统一 `conversationId`（D-k）；内部 Python/Mongo 保持 `session_id`。
- 新增错误码入目录：`SESSION_NOT_FOUND`(404)/`GENERATION_NOT_FOUND`(404)/`INVALID_TIMEZONE`(400)。
- **YAGNI 边界（spec §13，不得越界实现）**：不做会话级服务配置/会话级 LTM（模型·STT·TTS·工具全局共享）；不做断点续流（重连用新 clientRequestId 重发）；不做全文搜索引擎（`$regex`）；不做资源持久化索引（按需扫描）；不做独立缩略图生成（url 即下载引用，延后 BE-071）；不做 sticker 真实数据（恒空，批次 7）；不做消息级 segmentId 定位（搜索结果 segmentId 恒 null）；不做工具审批会话归属（pendingApprovals 全局返回）；工具副作用重放防护属 BE-043（批次 4）。

---

### Task 1: v2/新契约模型（frontend_contract）

**Files:**
- Modify: `frontend_contract/rest.py`
- Modify: `frontend_contract/ws_message.py`
- Generate: `frontend/src/types/contract.generated.ts`
- Test: `frontend_contract/tests/`

**Interfaces:**
- Produces（后续 Task 用）：`rest_contract.SessionSummary`（conversationId/title/messageCount/updatedAt/active）、`SessionListResult`、`SessionDetail`、`SessionActivateResult`、`MessagesPageResponse`、`MessageContextResponse`、`SearchMessagesResponse`+`SearchMessageItem`、`ResourceItem`、`ResourceListResponse`、`GenerationStatus`、`GenerationListResponse`、`PendingQueryResponse`、`ConversationUsage`；`ws_message.GenerationStartedMessage`（已有 conversationId）其余 5 个 generation.* 加 `conversationId`、`GenerationCompletedMessage` 加 `conversationUsage`、`ActivateSessionMessage`。

- [ ] **Step 1: RED（unit）** — `frontend_contract/tests/` 新增测试：
  - 新 REST 模型 round-trip：`SessionSummary`（含 conversationId/title）、`MessagesPageResponse`、`SearchMessageItem`（含 responseId/segmentId 可空省略）、`GenerationStatus`、`ConversationUsage`。
  - WS 模型：6 个 generation.* 均带 `conversationId`；`GenerationCompletedMessage` 带 `conversationUsage`；`ActivateSessionMessage`（type=`activate_session`，conversationId 必填）。
- [ ] **Step 2: 验证 RED** — `pytest frontend_contract/tests/ -v` FAIL（模型不存在/缺字段）。
- [ ] **Step 3: 实现** — `rest.py` 加上述模型（命名**避开 `debug.py` 同名 `SessionListResponse`**）；`ws_message.py` 给 5 个 generation.* 加 `conversationId: Optional[str] = None`、`GenerationCompletedMessage` 加 `conversationUsage: Optional[ConversationUsage] = None`、`ActivateSessionMessage` 加 Client→Server 判别。
- [ ] **Step 4: 验证 GREEN** — `pytest frontend_contract/tests/ -v` PASS。
- [ ] **Step 5: gen_ts** — `make gen-types` 重生成；`git diff frontend/src/types/contract.generated.ts` **仅新增 v2/新类型，v1 类型字节不变**（`python -m frontend_contract --check` 通过）。
- [ ] **Step 6: Commit** — `feat(contract): 批次 3 契约——多会话/历史/搜索/资源/状态模型 + generation conversationId`

### Task 2: schema 层（session_id/conversation_id 贯通）

**Files:**
- Modify: `core/schema/message.py`（AssistantMessage/ToolResultMessage 补 session_id）
- Modify: `core/schema/event.py`（UserInputData 补 conversation_id）
- Modify: `core/agent/_types.py`（_PendingMessage 补 conversation_id）
- Test: `core/schema/tests/`、`core/agent/tests/`

**Interfaces:**
- Produces：`AssistantMessage.session_id: SessionId = SessionId("")`、`ToolResultMessage.session_id` 同；`UserInputData.conversation_id: SessionId | None = None`；`_PendingMessage.conversation_id`。
- Consumes：Task 8（loop 读取）。

- [ ] **Step 1: RED（unit）** — `AssistantMessage()` 默认 `session_id=""`；`UserInputData()` 默认 `conversation_id=None` 且可填值 round-trip；`_PendingMessage` 带 conversation_id 不破坏现有构造。
- [ ] **Step 2: 验证 RED** — FAIL（字段不存在）。
- [ ] **Step 3: 实现** — 三处加可选字段（`session_id` 默认 `SessionId("")` 对齐 UserMessage；`conversation_id` 默认 None）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(schema): 批次 3——Message.session_id 补齐 + UserInputData/_PendingMessage.conversation_id`

### Task 3: 仓库层扩展（session_repo + message_repo）

**Files:**
- Modify: `core/storage/session_repo.py`（SessionSummary.title + create/rename + save 写 title）
- Modify: `core/storage/message_repo.py`（query_page/query_after/find_context/query_search）
- Test: `core/storage/tests/`

**Interfaces:**
- Produces：
  - `SessionRepository.create(session_id, title="")`、`rename(session_id, title)->bool`、`SessionSummary.title: str`、`save(...)` 带 title（preserve 参数语义：create 后 title 保留，save 不覆盖非空 title 除非显式）。
  - **自动标题（D-h）**：`save()` 时若 doc 无 `title` 且 messages 非空 → `title = 首条 UserMessage 文本前 20 字符`（幂等派生，无额外调用点；rename 显式写后保留）。
  - `MessageRepository.query_page(session_id, *, before=None, limit=20) -> tuple[list[Message], bool]`（limit+1 探测 has_more）；
  - `query_after(session_id, message_id, *, limit=20) -> tuple[list[Message], bool]`（按 `{session_id,message_id}` 定位锚点 _id → 之后正序）；
  - `find_context(session_id, message_id, *, before=5, after=5) -> tuple[Message|None, list[Message], list[Message]]`；
  - `query_search(*, session_id=None, query="", date_from=None, date_to=None, timezone="UTC", category=None, cursor=None, limit=20) -> tuple[list[Message], bool, str|None]`（组合 filter；cursor=base64(`timestamp|_id`)；排序 `timestamp DESC,_id DESC`；谓词 `(timestamp<T) OR (timestamp=T AND _id<ID)`；`INVALID_TIMEZONE` 由调用方抛）。

- [ ] **Step 1: RED（unit）** — session_repo：`create`/`rename` 写/读 title；`list_all` 摘要含 title。message_repo（注入 fake db）：`query_page` has_more 边界（limit 恰满 vs 多一条）；`query_after` 锚点后正序；`find_context` 前后窗口；`query_search` 组合（query/conversationId/自然日时区/category/cursor 分页）。
- [ ] **Step 2: 验证 RED** — FAIL（方法不存在/字段缺）。
- [ ] **Step 3: 实现** — 按 Interfaces 签名实现；`query_search` 的 Mongo filter 组合（`content.$elemMatch` 复用 query_offset 模式；`timestamp` 区间；`category` 按 part 类型/role；keyset 谓词）；时区转换用 `zoneinfo.ZoneInfo`，未知时区由路由层抛 400。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(storage): 批次 3——session title/create/rename + message query_page/query_after/find_context/query_search`

### Task 4: SessionManager（核心组件）

**Files:**
- Create: `core/memory/session_manager.py`
- Test: `core/memory/tests/test_session_manager.py`

**Interfaces:**
- Consumes：Task 3 的 repo 方法、`SessionMemory`（`core/memory/session.py`，构造支持 `character_state/repo/msg_repo`）。
- Produces（后续 Task 用）：
```python
class SessionManager:
    def __init__(self, repo, msg_repo, max_sessions=20, max_messages=20): ...
    @property def active_id(self) -> SessionId
    @property def active(self) -> SessionMemory
    def get(self, sid) -> SessionMemory                       # 内存命中/构造空实例
    async def ensure_loaded(self, sid) -> SessionMemory       # 首次 await load
    async def resolve(self, conversation_id, protocol_version) -> SessionMemory
    async def switch(self, sid) -> SessionMemory
    async def create(self, title="") -> SessionSummary
    async def list(self, *, cursor=None, limit=50, search="") -> tuple[list[SessionSummary], bool, str | None]
    async def rename(self, sid, title) -> bool
    async def delete(self, sid) -> int
    async def save_all(self) -> None
    async def connect_db(self) -> bool
    async def health_check(self) -> bool
    async def load_active(self) -> None
    def bind_character_engine(self, engine) -> None
    def evict_if_needed(self) -> None
```

- [ ] **Step 1: RED（unit）** — `test_session_manager.py`（**fake repo 需实现方法集**：`repo.save(session_id, messages, *, character_state, reflector_state, service_state)`、`repo.load(sid) -> 4 元组 (messages, cs, rs, ss)`、`repo.connect()/health_check()/create(sid,title)/rename(sid,title)/delete(sid)/list_all() -> list[SessionSummary]`；`msg_repo.append(session_id, msg)/delete_by_session(sid)`；Task 3 提供真实版，Task 4 用 fake 验证 manager 逻辑）：
  - eager `"default"`：构造后 `get("default")` 命中同一实例（`is`），`active_id=="default"`；
  - `ensure_loaded`：首次构造 + `load()`（fake repo 返回消息）；重复调用不重复 load；
  - `resolve` 规则：`protocol_version=1` 恒 default；v2 None → active；v2 显式 conv → 该会话；
  - `switch`：换 active_id + `bind_character_engine` 被调（fake engine 记录 attach 次数）+ 目标未加载先 ensure_loaded；
  - `create`：生成 `conv_` 前缀 ID、title 可空、空会话落库（fake repo 断言）；`rename`/`delete`（delete 联动 msg_repo.delete_by_session + active 回落 default）；
  - `list`：返回 `(items, has_more, next_cursor)`——实现用 `repo.list_all()`（≤200，`updated_at DESC, session_id ASC` tiebreak）+ search 过滤（**仅 title `$regex` 大小写不敏感**，不混消息文本）+ **内存 keyset 游标**（demo 规模足够，无需 repo 新分页方法）；items 填 `active`（按 active_id 比较）；
  - `evict_if_needed`：超 max_sessions 逐出非活动并 save。
- [ ] **Step 2: 验证 RED** — FAIL（SessionManager 不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces 实现；eager default 空实例；`ensure_loaded` 内部 try Mongo load → JSONL fallback（复用 `SessionMemory.load`）；`resolve` 规则；`switch` 先 outgoing `_pre_save_hook()` 再换指针 + `bind_character_engine`（**调用 Task 5 已实现的 `engine.attach_session(target)`**——Task 4 依赖 Task 5，执行顺序 5 先于 4）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(session): 批次 3——SessionManager 缓存池 + 活动指针 + resolve/switch/CRUD`

### Task 5: CharacterEngine.attach_session + PromptAssembler.set_session

**Files:**
- Modify: `core/personality/engine.py`
- Modify: `core/adapter/prompt_assembler.py`
- Test: `core/personality/tests/`、`core/adapter/tests/`（或就近）

**Interfaces:**
- Produces：`CharacterEngine.attach_session(session)`（D-o：重绑 `state` + `_awareness._state`/`_compression._state`/`_emotion._state` + `_pre_save_hook` + `_last_awareness_time`；**不调 `_restore_from_state`**）；`PromptAssembler.set_session(session)`（重绑 `_session` + `EmotionSection`/`OwnLifeSection`/`MemorySection` 的 session）。

- [ ] **Step 1: RED（unit）** — `attach_session`：构造 fake engine（含三个子组件），attach 到 session B 后 `engine.state is B.character_state` 且 `engine._awareness._state is B.character_state`（三个子组件逐一断言）；`session._pre_save_hook` 已设置；`_last_awareness_time` 同步。`set_session`：切换后 `_session`/三个 section 的 session 指向新对象。
- [ ] **Step 2: 验证 RED** — FAIL（方法不存在）。
- [ ] **Step 3: 实现** — 按 Interfaces；注意 `_last_awareness_time` 是 engine 上**代理到 `_awareness._last_awareness_time` 的 property setter**（engine.py:228-234），重绑后需同步 `state.last_awareness_time`（新会话的 awareness 状态）；**不 `_restore_from_state` 的后果**：reflector live buffer 不恢复——切换时 outgoing 已 `_pre_save_hook()` 落盘、incoming 的 buffer 随其 SessionMemory 实例内存独立（load 时已恢复），属 spec D-o 有意取舍，勿加回恢复调用。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(personality,adapter): 批次 3——CharacterEngine.attach_session 换绑 + PromptAssembler.set_session`

### Task 6: TokenTracker 会话感知

**Files:**
- Modify: `core/monitoring/token_tracker.py`
- Test: `core/monitoring/tests/`

**Interfaces:**
- Produces：`record(usage, model=None, round_num=None, session_id=None)`（`session_id` 缺省 → 沿用实例 `self.session_id`；`_session_totals[session_id]` 累计）；`session_total` **保持普通 int 实例属性**（`record` 时同步为最近记录会话累计，兼容 debug/runtime_config 读取——**不改 property**，避免破坏内部 3 处 `self.session_total +=`/赋值写点 line 81/124/130）；`get_session_stats(session_id=None)` **带默认值**（现有 `ws_handler.py:98` 零参调用 `get_session_stats()` 必须不破）按会话返回 `{rounds, turn_total, total, by_model}`。

- [ ] **Step 1: RED（unit）** — `record(usage, session_id="a")` 两次 + `session_id="b"` 一次 → `get_session_stats("a")` total 正确、`get_session_stats("b")` 独立；`session_total` 属性不抛且返回最近会话累计（`debug/info.py`/`runtime_config` 既有读取方式不破）；`get_session_stats(None)` 兼容旧调用。
- [ ] **Step 2: 验证 RED** — FAIL（签名/行为不符）。
- [ ] **Step 3: 实现** — `_session_totals: dict[SessionId,int]`；`record` 加 `session_id` 参数（默认 None → 沿用 `self.session_id`），累计 `_session_totals` 并同步 `self.session_total = _session_totals[最近记录会话]`（**保留普通属性，内部 `+=` 写点不动**）；`get_session_stats(session_id=None)` 按会话取（None → 最近记录会话，兼容零参）。
- [ ] **Step 4: 验证 GREEN** — PASS（含既有 token_tracker 测试不破）。
- [ ] **Step 5: Commit** — `feat(token): 批次 3——TokenTracker 会话感知（_session_totals + session_total 兼容）`

### Task 7: Agent 集成

**Files:**
- Modify: `core/agent/agent.py`（SessionManager 替换 `self._session`、构造顺序、resolve/switch/pending_messages、process_message conversation_id、_handle_interrupt source 贯通）
- Modify: `core/agent/_lifecycle_persistence.py`（connect_db→load_active、save→save_all、health_check 走 manager）
- Test: `core/agent/tests/`

**Interfaces:**
- Consumes：Task 4/5/6。
- Produces：`agent.session -> SessionMemory`（active）、`active_session_id`、`pending_messages`、`resolve_session(conversation_id, protocol_version)`、`switch_session(sid)`；`process_message(..., conversation_id=None, protocol_version=1)`；`cancel_current(source)` 贯通（loop 侧 Task 8）。

- [ ] **Step 1: RED（service）** — `agent.session` 返回 active 且 `session_manager.active` 同实例；`switch_session("conv_x")` 后 `active_session_id` 更新、`character_engine` 已 attach（mock engine 断言）；`resolve_session(None, 1)` 恒 default；`process_message(conversation_id=...)` 参数贯通到 `_PendingMessage`；`pending_messages` 返回未出队列表；lifecycle `_start_persistence` 走 `load_active`（Mongo 不可用降级 JSONL）。
- [ ] **Step 2: 验证 RED** — FAIL（构造/方法缺）。
- [ ] **Step 3: 实现** — 构造顺序（D-l）：SessionManager → eager default → CharacterEngine(state=default.character_state) → bind_character_engine → PromptAssembler/MemoryReflector/CognitiveLoop（resolver）；`self._session` 删除改 `session` property；lifecycle 改造；`_handle_interrupt` 传 `data.source`。
- [ ] **Step 4: 验证 GREEN** — PASS + 既有 agent lifecycle 测试全绿。
- [ ] **Step 5: Commit** — `feat(agent): 批次 3——SessionManager 集成 + resolve/switch/pending + lifecycle + interrupt source`

### Task 8: CognitiveLoop 改造

**Files:**
- Modify: `core/cognitive/loop.py`（session_resolver、self._session 动态、conversation_id 贯通、_after_response 局部捕获、_handle_interrupt(source)）
- Modify: `core/cognitive/generation.py`（GenerationContext 加 conversation_id）
- Modify: `core/cognitive/tools.py`（self._session 自动跟随——核实现状，随 loop 改造）
- Test: `core/cognitive/tests/`

**Interfaces:**
- Consumes：Task 2（conversation_id 字段）、Task 4（resolve）、Task 5（attach_session）、Task 6（token_tracker 会话感知）。
- Produces：`CognitiveLoop.__init__(..., session_resolver: Callable[[SessionId|None,int], Awaitable[SessionMemory]], ...)`（`session` 参数移除）；`process(data, *, conversation_id=None, protocol_version=1)`；`cancel_current(source=None)`。

- [ ] **Step 1: RED（service）** — 注入 fake resolver 返回固定 session；`process(conversation_id="conv_x")` 后 `add_message` 落在 `conv_x` 实例（fake resolver 目标 session 断言消息数）；无 conversation_id + protocol_version=1 → default；`_after_response` 用捕获 target（resolver 切走后异步延续仍写原会话）；`cancel_current("disconnect")` 贯通 source。
- [ ] **Step 2: 验证 RED** — FAIL（loop 构造缺 resolver/行为不符）。
- [ ] **Step 3: 实现** — `session_resolver` 构造参数；`process` 开头 `target = await self._session_resolver(conversation_id, protocol_version)` → **`self._character_engine.attach_session(target)`（D-o 换绑，v2 显式 conversationId 覆盖 active 时 engine 才不串会话）** → `self._session = target`（动态属性，tools mixin 自动跟随）→ `prompt_assembler.set_session(target)`（D-n）；`_after_response(session=target, engine=self._character_engine)` **局部捕获**（除 save 外，`consolidator.consolidate`/`character_engine.buffer_message` 也传 target 归属，防切会话后落错会话）；`_build_turn_prompt`/`_think_loop`/`_execute_tool_round` 传 conversation_id；**`token_tracker.record(..., session_id=target)`（loop.py:533 调用点，conversationUsage 数据来源）**；**turn_completed 调用点（loop.py:828）先 `await token_tracker.get_session_stats(target)` 再作为 conversation_usage 传入 output**；`GenerationContext` 加 `conversation_id`。
  - **同步更新既有 CognitiveLoop 构造点（移除 session 参数后全部 `session=` 改 `session_resolver=`）**：`core/agent/agent.py:220-232`（Task 7 已重写）+ 测试 `core/task/tests/test_task_scheduler.py:761,817`、`core/cognitive/tests/test_flush.py:21`、`core/cognitive/tests/test_empty_response_guard.py:27`、`core/tests/test_phase3.py:162,325,423,1718,1815`。
- [ ] **Step 4: 验证 GREEN** — PASS + 既有 loop 测试（批次 2 v2 generation）全绿。
- [ ] **Step 5: Commit** — `feat(loop): 批次 3——session_resolver + target 归属 + conversation_id 贯通`

### Task 9: GenerationRegistry + GenerationRepo（Mongo）

**Files:**
- Create: `core/storage/generation_repo.py`
- Create: `core/cognitive/generation_registry.py`
- Modify: `core/cognitive/loop.py`（register/mark 挂点 + 中断来源分派）
- Modify: `core/agent/agent.py`（cancel_current(source) 贯通）
- Test: `core/cognitive/tests/`、`core/storage/tests/`

**Interfaces:**
- Consumes：Task 8（loop 挂点可达）。
- Produces：`GenerationRepository`（connect/close/health_check/upsert/find_one/**find_by_message(message_id) 按 assistant 父消息 ID 反查最近一条 generation（D-q 搜索用）**/find_by_session(session_id, *, status, cursor, limit)->(docs, has_more, next_cursor)/mark_stale）；`GenerationRegistry`（register/mark/get/query_by_session/**find_by_message**/mark_stale_interrupted）。
- 集合 `generations`：`generation_id` unique + `session_id+updated_at` + TTL 30d；`message_id` 辅助索引（find_by_message 用）。

- [ ] **Step 1: RED（service）** — register 后 `get(genId)` status=running；mark(completed, finish_reason="stop") 后终态；`mark` upsert 语义（先 mark 后 register 也建档）；`query_by_session(session_id, status=running)` 过滤；`mark_stale_interrupted(cutoff)` 把旧 running 标 interrupted；Mongo 离线 register 不抛（try/except）。
- [ ] **Step 2: 验证 RED** — FAIL（组件不存在）。
- [ ] **Step 3: 实现** — GenerationRepo（Mongo upsert/find/index/TTL）；GenerationRegistry 薄封装；**register 挂点：`process()` 创建 `GenerationContext` 后 / `_think_loop` 开头 `turn_started` 发射处调用一次**（`_dispatch_stream_event` 是逐帧分发、每 delta 都会进——**严禁**放那里，会重复 upsert）；register 同步 await + try/except；`_finalize_turn`/错误路径/`_handle_interrupt` `mark`（upsert）；`cancel_current(source)` 分派 cancelled/interrupted；`find_by_session` 用 `created_at DESC, generation_id DESC` keyset（cursor=base64(`created_at|generation_id`)，谓词同 §5.3 模式）。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(generation): 批次 3——GenerationRegistry Mongo 持久化 + register/mark/中断分派 + stale 清理`

### Task 10: Output 层 conversation_id/conversationUsage 贯通

**Files:**
- Modify: `Module/output/protocol.py`（on_* 加 conversation_id 参数）
- Modify: `Module/output/websocket.py`（wire 填 conversationId）
- Modify: `Module/output/manager.py`（统一回合事件透传 conversation_id + conversation_usage）
- Test: `Module/output/tests/`

**Interfaces:**
- Consumes：Task 1（契约字段）、Task 8（GenerationContext.conversation_id）。
- Produces：`turn_started/segment_started/text_delta/segment_completed/inner_voice/turn_completed/turn_failed/turn_cancelled` **8 个统一方法全部加 `conversation_id` 透传**（`turn_cancelled` 走 v2 `on_generation_failed{CANCELLED}`，也属 6 类 generation.*，必须带 conversationId）；`turn_completed` 加 `conversation_usage`（v2 填 `conversationUsage`，v1 NO-OP 不变）。

- [ ] **Step 1: RED（unit）** — v2 channel：`on_generation_segment_started(conversation_id="conv_x")` 产出 wire 含 `conversationId:"conv_x"`（6 类逐一）；`on_generation_completed(conversation_usage={...})` 产出 `conversationUsage`；v1 channel 不因新参数改变 wire。
- [ ] **Step 2: 验证 RED** — FAIL（签名/字段缺）。
- [ ] **Step 3: 实现** — protocol 抽象方法加参（no-op 默认）；websocket 填 wire；manager 透传（从 ctx 取 conversation_id）。`_split_versions` 或等价分发保持 v1 不变。
- [ ] **Step 4: 验证 GREEN** — PASS + 既有 `test_manager_v2.py` 全绿。
- [ ] **Step 5: Commit** — `feat(output): 批次 3——generation.* conversationId + conversationUsage 贯通`

### Task 11: WS 协议（activate_session + conversationId 路由）

**Files:**
- Modify: `core/input/ws_handler.py`
- Test: `core/input/tests/test_v2_ws.py`（更新）、`core/input/tests/test_session_ws.py`（新增）

**Interfaces:**
- Consumes：Task 7（switch_session）、Task 10。
- Produces：`activate_session` 消息处理（切 active + 重发 SessionMessage 回放 + 404）；`_route_v2_text` 传 `conversation_id`；建连回放 v1→default / v2→active。

- [ ] **Step 1: RED（service）** — v2 连接发 `{"type":"activate_session","conversationId":"conv_x"}` → 收到新会话 SessionMessage 回放（fake 会话预置消息）；未知 conversationId → `ErrorMessage{code:SESSION_NOT_FOUND}`；v2 发 `text` 带 conversationId → 消息落该会话（generation 事件 conversationId 字段非空）；**v1 连接发 text → 恒 default**（active 切到 conv_x 后 v1 回放/写入仍 default）。
- [ ] **Step 2: 验证 RED** — FAIL（activate_session 未处理/conversationId 未路由）。
- [ ] **Step 3: 实现** — `handle_json_message` 分支 `activate_session`（切 active + 重发 SessionMessage + 404 `SESSION_NOT_FOUND`）；`_route_v2_text` 解析 conversation_id 传 `agent.process_message(..., conversation_id=..., protocol_version=2)`；**`user_message.ack` 的 `conversationId` 填实际目标会话**（v2 缺省 → active，显式 → 该会话，非仅回显原始字段）；建连回放按 protocol_version resolve（v1 用 `agent.resolve_session(None, 1)`、v2 用 active）。
- [ ] **Step 4: 验证 GREEN** — PASS + 批次 2 v2 测试更新后全绿。
- [ ] **Step 5: Commit** — `feat(ws): 批次 3——activate_session + conversationId 路由 + 建连 v1/v2 resolve`

### Task 12: REST 会话路由（session_routes.py）

**Files:**
- Create: `core/input/session_routes.py`
- Modify: `core/input/server.py`（注册）
- Test: `core/input/tests/test_session_routes.py`

**Interfaces:**
- Consumes：Task 3/4/7/9（repo + manager + agent + registry）。
- Produces：全部端点（§4-§8 spec）：会话 CRUD、历史/增量、context、搜索、资源、generation 状态、pending。

- [ ] **Step 1: RED（service）** — 逐端点（用 fake/mock 或真实 app + fake agent）：
  - `GET /api/sessions`（cursor/limit/search——search 仅 title 大小写不敏感；items 的 `active` 按 active_id 填充）、`POST`、`GET/{id}`、`PATCH`（title）、`DELETE`（联动 + active 回落）、`POST /{id}/activate`；
  - `GET /api/sessions/{id}/messages`（before/limit has_more）、`?after=` 增量；
  - `GET /api/messages/{mid}/context?conversationId=...`（缺 conversationId → 400）；
  - `GET /api/search/messages`（query/日期/时区/category/cursor/limit 上限 100 + `INVALID_TIMEZONE` 400；返回项含 responseId 反查/segmentId 恒 null/snippet 前 100 字）；
  - `GET /api/sessions/{id}/resources`（category 过滤：image/link 命中、sticker 恒空、text 不支持 400；cursor=扫描停止处 _id；peek 判 has_more）；
  - `GET /api/sessions/{id}/generations`（status/cursor/limit 上限 100 keyset 分页）、`GET /api/generations/{gid}`（404 `GENERATION_NOT_FOUND`）；
  - `GET /api/sessions/{id}/pending`（pendingMessages `{messageId, snippet(前50字), createdAt}` 按会话过滤 + pendingApprovals **全局返回不按会话过滤**）；
  - 全部匿名 401（批次 1 认证接入）、未知 id 404 `SESSION_NOT_FOUND`。
- [ ] **Step 2: 验证 RED** — FAIL（路由未注册）。
- [ ] **Step 3: 实现** — `session_routes.py` 各 handler（构造契约模型 + wire_dict，复用 `_archive_to_client`/错误信封模式）；**资源提取 helper** `_extract_resources(messages, category)`（`ImagePart.src` / `TextPart.content` URL 正则 / `VideoPart`/`AudioPart`/`FilePart` `.src`；url 即下载引用，缩略图延后 BE-071）与**链接 URL 提取**正则统一 helper（`core/input/session_routes.py` 内私有函数，spec §8）；server.py `app.router` 注册 + 走认证中间件。
- [ ] **Step 4: 验证 GREEN** — PASS。
- [ ] **Step 5: Commit** — `feat(rest): 批次 3——session_routes 全端点（CRUD/历史/搜索/资源/状态/pending）`

### Task 13: 回归、测试收敛与文档

**Files:**
- Modify: 批次 2 测试（`test_v2_generation.py` 等，generation 事件补 conversationId 断言）
- Modify: `docs/electron-backend-progress.md`、`docs/electron-backend-protocol-v2.md`（会话/activate/增量/状态）、`docs/electron-backend-baseline.md`（如需）、`CLAUDE.md`（core/ 结构加 session_manager/generation_registry/session_routes）、`core/README.md`、`docs/architecture.md`（如需）
- Verify: CI 等价（lint + check-types + test-quick + test-normal）

- [ ] **Step 1: 更新批次 2 v2 测试** — generation.* wire 断言补 `conversationId`（非空）；取消/断线断言 mark 终态；**新增 `/api/messages` 兼容接口 wire 回归锁定测试**（全库 `_id` 游标响应形状不变，spec §10.3）。
- [ ] **Step 2: 回归** — `make test-quick`（Elysia conda）全绿；`make lint`；`make check-types`；`make test-normal`。
- [ ] **Step 3: 真实轨迹** — `scripts/capture_ws_trace.py` 新增/更新 v2 轨迹（chat 带 conversationId + activate_session + 增量），提交 `frontend-handoff-v2/traces/`。
- [ ] **Step 4: 文档更新** — progress.md 记批次 3 完成（含提交）；protocol-v2.md 补会话/activate/增量/状态查询；baseline 现状描述更新（多会话）；CLAUDE.md/README/architecture 结构更新。
- [ ] **Step 5: Commit** — `docs+batch3: 回归收敛 + v2 轨迹 + 文档更新`

---

## 执行顺序与依赖

```
Task 1 (契约) → Task 2 (schema) → Task 3 (repo) → Task 5 (attach/set_session) → Task 4 (SessionManager，依赖 5 的 attach_session)
Task 6 (token) 可并行于 Task 3/5
Task 7 (agent 集成) 依赖 4/5/6
Task 8 (loop) 依赖 2/4/5/6/7（resolve + attach + token + conversation_id）
Task 9 (generation registry) 依赖 8（loop 挂点）
Task 10 (output) 依赖 1/8
Task 11 (ws) 依赖 7/10
Task 12 (rest) 依赖 3/4/7/9
Task 13 (回归/文档) 依赖全部
```

每个 Task 独立提交，全部完成后 Task 13 做总回归。
