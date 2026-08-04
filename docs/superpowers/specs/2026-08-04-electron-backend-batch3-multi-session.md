# Electron 后端改造 — 批次 3：产品级多会话、历史、搜索与资源索引（Spec）

**Spec 版本：** v0.2（修订：吸收批次 3 四角度 review 全部 findings——契约字段真实性、命名收口、loop/PromptAssembler/attach_session 换绑、启动时序、分页游标、register/mark 竞态）
**文档日期：** 2026-08-04
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-030/031/032 + BE-024 剩余项）与 `docs/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md`
**前置：** 批次 0 基线 + 批次 1（认证/版本协商/错误信封）+ 批次 2（结构化聊天 v2）
**目标 HEAD：** `d6e4c91`

---

## 1. 目的

把后端从"单活动会话"升级为产品级多会话，交付三块能力：**产品级多会话 API（BE-030）**、**会话内历史/上下文/搜索（BE-031）**、**会话资源索引（BE-032）**；同时补齐 **BE-024 剩余项**（事件游标 / generation 状态查询 / pending 用户消息与工具调用查询 / 终态安全重放），使重连恢复语义完整化。

**兼容硬约束**：v1 客户端（未带 `X-Protocol-Version: 2` 握手头）收到的 wire 与现状**逐字节一致**；现有 1951 测试全绿；`/api/messages` 全库 `_id` 游标**零改动保留**为兼容接口（BE-031 明确允许），不作为产品级会话历史的唯一接口。

## 2. 已定决策

| 决策 | 结论 |
|---|---|
| D-a | **交互模型：两者结合**。服务端维护一个"当前活动会话"（active），WS 聊天消息默认进入它；v2 `text` 消息显式携带 `conversationId` 时路由到该会话、覆盖 active。会话切换 = 显式操作 + 重新下发该会话回放。 |
| D-b | **隔离范围：最小隔离**。会话隔离限定为：上下文（messages）+ character_state 每会话独立；Token 每会话累计统计 + 全局总账；模型 / STT·TTS 开关 / 工具保持全局共享（切会话不改服务配置）。 |
| D-c | **会话上下文模型：SessionManager 缓存池 + 活动指针**（方案 1）。`agent.session` 动态返回活动会话；切换 = 换指针（无 I/O、无竞态）。 |
| D-d | **BE-024 剩余项并入批次 3**：generation 状态查询 / pending 查询 / 增量拉取 / 终态安全重放。 |
| D-e | **资源索引模型：按需扫描**。无独立 resources 集合；每次查询按该会话归档游标扫描并提取资源。 |
| D-f | **GenerationRegistry：Mongo 持久化**。新增 `generations` 集合，重启后 generation 状态可查询。 |
| D-g | **默认活动会话沿用 `"default"`**（兼容既有 Mongo `sessions` doc 与 v1 行为）；新建会话 ID = `conv_<uuid4 hex[:12]>`。 |
| D-h | **自动标题**：创建后可无标题；首条用户消息写入时取前 20 字符作标题，可被前端重命名覆盖。标题持久化到 `sessions` doc 与 JSONL fallback。 |
| D-i | **v2 生成事件补 `conversationId` 实际值**：`generation.started`/`segment.started`/`delta`/`segment.completed`/`completed`/`failed` 六类全部新增 `conversationId: Optional[str]` 契约字段（ACK 已有）——这是 **v2 类型增量**（gen_ts 对这 6 个类型新增字段），非"无类型变更"。 |
| D-j | 时间戳一律 `now_iso()`（UTC ISO 8601，`+00:00`），与批次 0 约定一致。 |
| D-k | **wire 命名收口**：新 REST 接口与 WS 新消息**统一用 `conversationId`**（与 v2 协议、docs/14 一致）；内部 Python（repo/schema/Mongo 键 `session_id`）与既有 wire（`SessionMessageItem.session_id`、debug `sessionId`）**保持不动**。`/api/sessions` 路径的参数即 conversationId 值。 |
| D-l | **启动时序**：`Agent.__init__` eager 构造 default 空 `SessionMemory` 实例并注册到 SessionManager（**不 load**）；CharacterEngine 构造绑定该实例的 `character_state`；`_start_persistence` `connect_db()` 后 `await session_manager.load_active()` 从 DB/JSONL 恢复（SessionMemory.load 是**原地改字段**，同一实例生效）。运行时首次访问新会话 → 构造 + 异步 load。 |
| D-m | **CognitiveLoop 注入 session resolver**：loop 构造移除 `session` 参数，改为 `session_resolver: Callable[[SessionId|None, int], Awaitable[SessionMemory]]`（agent 传入 `resolve_session` 闭包）。 |
| D-n | **PromptAssembler 会话换绑**：`PromptAssembler.set_session(session)` 重绑 `_session` + `EmotionSection`/`OwnLifeSection`/`MemorySection` 的 session 引用。 |
| D-o | **attach_session 语义**：切换前对 outgoing 会话调用 `_pre_save_hook()`（sync reflector buffer 到其 character_state，内存操作）；attach 时**只重绑引用**（`engine.state` + `_awareness`/`_compression`/`_emotion` 三个子组件的 `_state` + `_pre_save_hook` + `_last_awareness_time`），**不调 `_restore_from_state`**（reflector buffer 随各 SessionMemory 实例内存独立，load 时已恢复；避免覆盖在途 buffer）。 |
| D-p | **TokenTracker 会话感知兼容**：保留 `session_total` 属性（返回最近记录会话累计，兼容 `debug/info.py` 与 `runtime_config/plugin.py` 既有读取）；新增 `_session_totals: dict[SessionId, int]`；`record()` 增加 `session_id` 参数（loop 传目标会话）。 |
| D-q | **搜索返回 ID 语义**：搜索项返回 `{conversationId, messageId, responseId|null, segmentId|null, ...}`；`responseId` = GenerationRegistry 按 `message_id` 反查最近一条 generation（best-effort）；`segmentId` 消息级搜索不定位到段 → 恒 null（跳回消息后用 context 窗口定位），显式声明该取舍。 |

## 3. 会话上下文模型

### 3.1 SessionManager（新组件 `core/memory/session_manager.py`）

```python
class SessionManager:
    def __init__(self, repo: SessionRepository, msg_repo: MessageRepository,
                 max_sessions: int = 20, max_messages: int = 20):
        self._sessions: dict[SessionId, SessionMemory] = {}
        self._active_id: SessionId = SessionId("default")
        self._lru: deque[SessionId] = deque()          # 尾 = 最近使用
        self._character_engine: "CharacterEngine | None" = None
        ...

    @property
    def active_id(self) -> SessionId
    @property
    def active(self) -> SessionMemory
    def get(self, sid: SessionId) -> SessionMemory     # 内存命中返回；未命中构造空实例（不 load）+ touch LRU
    async def ensure_loaded(self, sid: SessionId) -> SessionMemory  # 构造后首次：await load(sid)
    async def resolve(self, conversation_id: SessionId | None,
                      protocol_version: int) -> SessionMemory
        # 规则：protocol_version<2 → 恒 "default"（v1 强制默认会话）；>=2 且 conversation_id 非空 → 显式；
        #      >=2 且 None → active。返回前 ensure_loaded。
    async def switch(self, sid: SessionId) -> SessionMemory  # 目标 resolve → 换 active_id → bind_character_engine
    async def create(self, title: str = "") -> SessionSummary   # 生成 conv_<hex> → doc 落库 → 注册空实例
    async def list(self, *, cursor: str | None = None, limit: int = 50,
                   search: str = "") -> tuple[list[SessionSummary], bool, str | None]
    async def rename(self, sid: SessionId, title: str) -> bool
    async def delete(self, sid: SessionId) -> int        # 联动 msg_repo.delete_by_session + 内存移除 + active 回落
    async def save_all(self) -> None                     # 全部内存会话 save（shutdown 用）
    async def connect_db(self) -> bool
    async def health_check(self) -> bool
    async def load_active(self) -> None                  # connect 后 active 从 DB/JSONL 恢复
    def bind_character_engine(self, engine: "CharacterEngine") -> None  # __init__ 后设置；switch 时 attach
    def evict_if_needed(self) -> None                    # LRU 超限：最久未用非活动 save + drop
```

**语义：**
- **eager default**：构造时 `get("default")` 创建空 `SessionMemory("default")` 实例（不 load），供 CharacterEngine 绑定；`load_active()` 在 DB 连接后原地恢复。
- **懒加载**：`get(sid)` 首次构造新 `SessionMemory(session_id=sid, character_state=CharacterState(session_id=sid), repo, msg_repo)`，不 load；`ensure_loaded` 首次 `await load(sid)`（Mongo 或 JSONL fallback）。`pending_file` 每会话独立（`ProjectPath.SESSION_PENDING.resolve(session_id=sid)` 已参数化）。
- **切换零 I/O**：`switch()` 换 `_active_id` 指针 + `bind_character_engine`（内含 attach）；不 save 当前、不 load 目标（缓存命中直接换，未缓存先构造）。
- **resolve 规则（D-l/D-m 派生）**：v1 连接恒 `"default"`，active 切换**不影响 v1**（v1 行为与批次 0-2 逐字节一致）；v2 无 conversationId → active；v2 显式 → 该会话。resolve 返回前确保 loaded。
- **并发生成正确性**：loop 每轮开头 resolve target 并赋给 `self._session`（§3.4）；生成是串行处理的（`_pending_messages` 逐个出队），单在途生成下重绑安全；异步延续用捕获的局部 target，不读 `self._session`。
- **LRU evict**：超出 `max_sessions` 时从头部逐出**非活动**会话：`await save()` 后 drop。Demo 会话数少，此为安全网。

### 3.2 Agent 接口改造

```python
# core/agent/agent.py
@property
def session(self) -> SessionMemory              # → session_manager.active（动态）
@property
def active_session_id(self) -> SessionId
@property
def pending_messages(self) -> list[_PendingMessage]   # 新增只读访问器（BE-024 pending 查询用）
async def resolve_session(self, conversation_id: SessionId | None,
                          protocol_version: int) -> SessionMemory   # → session_manager.resolve
async def switch_session(self, sid: SessionId) -> SessionMemory    # → session_manager.switch
```

- **构造顺序（D-l）**：`Agent.__init__` ① 创建 `SessionManager(repo, msg_repo)`；② eager `get("default")` 得 default 空实例；③ `CharacterEngine(state=default.character_state, ...)`；④ `session_manager.bind_character_engine(engine)`；⑤ `engine._pre_save_hook = engine.sync_reflector_to_state`（随 attach 维护，见 §3.3）；⑥ PromptAssembler/MemoryReflector/CognitiveLoop 构造经 `resolve_session` 绑定。**同一 default 实例**贯穿 engine 与 SessionManager（SessionMemory.load 原地改字段，仅同一实例生效）。
- `agent.purge_message_from_session(msg_id)` 作用域限定为 active 会话（现语义不变）。
- `process_message` 增加 `conversation_id: SessionId | None = None` 参数（v1 调用不传 → `protocol_version=1` → resolve 恒 default）。
- 生命周期 `_lifecycle_persistence.py`：`_start_persistence` 中 `connect_db()` 后 `await self._session_manager.load_active()`；`_stop_persistence` 中 `await self._session_manager.save_all()`；`_db_health_loop` 中 `health_check()` 改走 manager。

### 3.3 CharacterEngine 换绑（D-o）

```python
# core/personality/engine.py 新增
class CharacterEngine:
    def attach_session(self, session: SessionMemory) -> None:
        """换绑当前会话状态：先 sync outgoing，再重绑引用（不 _restore_from_state）。"""
        self.state = session.character_state
        # 三个子组件各自持有 _state 引用——必须同步重绑（awareness/compression/emotion）
        self._awareness._state = session.character_state
        self._compression._state = session.character_state
        self._emotion._state = session.character_state
        session._pre_save_hook = self.sync_reflector_to_state
        self._last_awareness_time = session.character_state.last_awareness_time
```

- **调用点**：`SessionManager.switch()` 换 active 时；loop 每轮 resolve 后（`bind_character_engine`/`attach` 幂等，重复绑定同一会话为无害）。
- **outgoing 同步**：切换前由调用方（switch/轮次 resolve）先对 outgoing 会话调用 `_pre_save_hook()`（sync reflector buffer → 其 character_state，内存操作，非落盘）。
- **不 `_restore_from_state`**：reflector buffer 随各 SessionMemory 实例内存独立，load 时已恢复；attach 时调用会覆盖在途 buffer，禁止。
- 启动恢复：`_start_persistence` 中 default 会话 `load_active()` 后 C9 `_last_awareness_time` 与 reflector 恢复逻辑沿用现有（`_lifecycle_persistence` 现逻辑，仅换 manager 路径）。

### 3.4 CognitiveLoop 轮次目标会话（D-m）

- 构造参数 `session: SessionMemory` 移除，新增 `session_resolver`（见 D-m）；loop 删除 `self._session` 初始化。
- `process()` 每轮开头：`target = await self._session_resolver(conversation_id, protocol_version)`；`self.agent._character_engine.attach_session(target)`（经 agent 暴露的 attach 入口或 bind）；随后 `self._session = target`（动态属性，供 `CognitiveTools` mixin 的 `self._session.recent_messages()`/`self._session._session_id` 自动跟随）。
- 本轮所有 `add_message`（User/Assistant/ToolResult，loop 共 5 处：445/624/650/703/769）、`recent_messages`、`save()`（997）经 `self._session`。
- **异步延续竞态防护**：`_after_response`（`asyncio.create_task` 触发）与 `_finalize_turn` 的错误/收尾路径，把 `target` 作为**捕获的局部变量**传入（`_after_response(session=target)`），不读 `self._session`——active 切换后 in-flight 异步延续仍写发起会话。
- `process()` / `_think_loop` / `_build_turn_prompt` / `_execute_tool_round` 增加 `conversation_id` 参数贯通；`UserInputData`（`core/schema/event.py`）与 `_PendingMessage`（`core/agent/_types.py`）增加 `conversation_id: SessionId | None` 字段。
- **PromptAssembler（D-n）**：resolve target 后 `prompt_assembler.set_session(target)`，使 `recent_messages(8)`、`EmotionSection.mood`、`OwnLifeSection.own_life`、`MemorySection` 均取 target 会话。
- **v1 路径**：`agent.process_message(text=...)` 无 conversation_id、`protocol_version=1` → resolve 恒 `"default"`，行为与批次 0-2 一致。

### 3.5 Message.session_id 贯通

`AssistantMessage` / `ToolResultMessage` 补 `session_id: SessionId = SessionId("")`（对齐 `UserMessage`）。`SessionMemory.add_message` 写入时已由实例 `_session_id` 注入归档；模型字段补齐后，回放/搜索结果/资源索引可直接取消息自带会话归属。旧数据无字段 → 默认 `""`，查询仍走 repo 层 `session_id` 过滤，兼容无损。

### 3.6 TokenTracker 会话感知（D-p）

- 内存 `session_total: int` **保留**（返回最近记录会话的累计，兼容 `debug/info.py:339` 与 `runtime_config/plugin.py:322` 读取）；新增 `_session_totals: dict[SessionId, int]`。
- `record(usage, model, round_num, session_id: SessionId)`：`session_total = _session_totals[session_id] += ...`（同步兼容属性）；`current_turn_usages` 归属轮次目标会话（loop 调用点传 target session）。
- `get_session_stats(session_id)` 按会话返回；v1 `token_usage` 广播的 `session.total` 取目标会话累计（token_tracker 内部记当前 session_id）。
- **generation.completed 会话累计（D-i 扩展）**：`GenerationCompletedMessage` 新增可选顶层字段 `conversationUsage: ConversationUsage | None`（新模型 `{total, byModel}`），loop 从 `get_session_stats(target)` 填充——**不要**写入 `usage`（契约 `TokenUsage` 只有 `rounds`+`turn_total`，不加字段）。
- Mongo `token_usage` 记录已带 `session_id`；全局总账 = repo 对全库求和，不变。

## 4. 多会话 API（BE-030）

新文件 `core/input/session_routes.py`，`server.py` 一并注册；全部走批次 1 产品认证（`AGENT_ACCESS_TOKEN`）。**wire 字段统一 `conversationId`（D-k）**；`/api/sessions` 路径参数即 conversationId 值。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/sessions` | `cursor/limit/search` | `SessionListResult{ sessions[], has_more, cursor }` |
| POST | `/api/sessions` | `{ title? }` | `SessionSummary` |
| GET | `/api/sessions/{conversationId}` | — | `SessionDetail{ session, messages(第一页), active }` |
| PATCH | `/api/sessions/{conversationId}` | `{ title }` | `SessionSummary` |
| DELETE | `/api/sessions/{conversationId}` | — | `{ ok: bool, deleted: int }` |
| POST | `/api/sessions/{conversationId}/activate` | — | `SessionActivateResult{ activeConversationId }` |

**契约字段（SessionSummary）：**
```json
{
  "conversationId": "conv_...",
  "title": "…（可空）",
  "messageCount": 12,
  "updatedAt": "2026-08-04T00:00:00+00:00",
  "active": false
}
```

- **列表游标（keyset）**：`cursor = base64("{{updated_at}}|{{session_id}}")`；排序 `updated_at DESC, session_id ASC`；下一页过滤谓词 **`(updated_at < U) OR (updated_at = U AND session_id > S)`**（DESC 主键 + ASC 次键）；空/缺省 cursor = 第一页；`has_more` 用 limit+1 探测；`list()` 返回 `(items, has_more, next_cursor)`。
- **列表搜索**：`search` 对 title `$regex` 大小写不敏感（消息文本搜索走 §5.3，不混入）。
- **创建**：`conversationId = conv_<uuid4 hex[:12]>`；`title` 可空（自动标题 D-h）；空会话落库（messages=[]）。
- **重命名**：title 非空才写；空字符串清除；更新 `sessions` doc 的 `title`。
- **删除**：联动 `msg_repo.delete_by_session(sid)`；内存会话 drop；若删除的是 active，active 回落 `"default"`。**资源/附件文件清理延后 BE-071（批次 7）**；**LTM 跨会话，不联动删除**（删除会话不影响长期记忆）。
- **激活（REST vs WS 分工）**：REST `/activate` 仅切 active 并返回 `{ activeConversationId }`，**不主动向 WS 客户端推送回放**（前端切会话时用 WS `activate_session` 触发回放，见 §6.1）；WS 客户端已连接时也可直接走 WS 消息。二者都切同一 active 指针。

**错误**：未知 `conversationId` → 404 `SESSION_NOT_FOUND`（错误目录新增码，`retryable=false`）。

## 5. 历史、上下文与搜索（BE-031）

### 5.1 会话内历史

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/sessions/{conversationId}/messages` | `before/limit`（_id 游标，倒序） | `MessagesPageResponse{ messages[], has_more, total }` |
| GET | `/api/sessions/{conversationId}/messages?after=` | `after=<message_id>`（增量） | 同 `MessagesPageResponse`（该消息之后新增，正序） |

- `before/limit`：**新增 repo 方法 `query_page(session_id, before: ObjectId|None, limit) -> (messages, has_more)`**（limit+1 探测，复用现有 `query_by_session` 的 filter/sort 逻辑）；`query_by_session` 保留（debug 面板用）。`total` = `count_by_session`。
- **增量 `after=<message_id>`**：新增 repo 方法 `query_after(session_id, message_id, limit)`——**按 `{session_id, message_id}` 字段过滤定位该消息的 `_id` 锚点**（archive doc 含 `message_id` 字段，非 ObjectId；messageId→_id 单次查询映射），再返回锚点之后正序消息。`message_id` 无索引，Demo 规模可接受（connect 现只建 `session_id`/`session_id+_id` 索引）。
- 会话不存在 → 404 `SESSION_NOT_FOUND`；空会话 → `{ messages: [], has_more: false, total: 0 }`。

### 5.2 消息上下文窗口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/messages/{messageId}/context` | `conversationId&before=5&after=5`（默认各 5） | `MessageContextResponse{ anchor, before[], after[], conversationId }` |

- **`conversationId` 查询参数必填**（messageId 非 ObjectId，无法推出会话；由搜索结果回填或前端持有）。
- 定位：新增 repo 方法 `find_context(session_id, message_id, before, after)`——会话内按 `{session_id, message_id}` 定位锚点 `_id`，返回其前后窗口（同会话、按时间序）。

### 5.3 搜索

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/search/messages` | `query&conversationId?&date_from&date_to&timezone&category&limit&cursor` | `SearchMessagesResponse{ items[], has_more, cursor }` |

**组合过滤（全部可选，AND 语义）：**
- `query`：内容 `$regex` 大小写不敏感（复用 `query_offset` 的 `content.$elemMatch` 模式）；
- `conversationId`：限定会话（缺省 = 全部会话）；
- **自然日**：`date_from`/`date_to` 为 `YYYY-MM-DD`（本地自然日），`timezone` 为 IANA 名（如 `Asia/Shanghai`）。**区间合成**：两日都传 → 半开区间 `[dayStart(date_from), nextDayStart(date_to))`（date_to 含整天）；只传 `date_from` → `[dayStart(date_from), +∞)`；只传 `date_to` → `(−∞, nextDayStart(date_to))`。服务端用 `zoneinfo` 把边界转 UTC 过滤 `timestamp`。`timezone` **缺省默认 `"UTC"`**（显式声明，不探测服务端本地）。两日都不传 → 不按日期过滤；
- `category`：`text`（含文本匹配）\| `image` \| `link` \| `video` \| `audio` \| `file` \| `tool`；缺省全部；
- 分页：不透明 `cursor = base64("{{timestamp}}|{{_id}}")`；排序 **`timestamp DESC, _id DESC`**（`_id` ObjectId 插入单调，作稳定 tiebreak）；下一页谓词 `(timestamp < T) OR (timestamp = T AND _id < ID)`；`limit` 默认 20、上限 100；`has_more` 用 limit+1 探测。

**返回项（D-q）**：
```json
{
  "conversationId": "conv_...",
  "messageId": "msg_...",
  "responseId": "gen_... | null",
  "segmentId": null,
  "role": "user | assistant",
  "timestamp": "…",
  "snippet": "前 100 字"
}
```
- `responseId` = GenerationRegistry 按 `message_id` 反查最近一条 `generation_id`（best-effort，无则 null）；
- `segmentId` **恒 null**（消息级搜索不定位到段，跳回消息后用 `find_context` 定位）；
- 点击 `messageId` + `conversationId` 回填 `/api/messages/{messageId}/context` 跳回来源。

**实现**：`MessageRepository.query_search(...)` 组合 Mongo filter；第一版不做全文索引（`$regex` 够 Demo 规模）；`zoneinfo` 未知时区 → 400 `INVALID_TIMEZONE`。

## 6. WS 协议扩展

### 6.1 客户端→服务端新增

```json
{ "type": "activate_session", "conversationId": "conv_..." }
```

- 服务端 `await agent.switch_session(conversationId)` → **重发 `SessionMessage`**（新活动会话回放，复用建连回放路径 `_build_session_items`）+ 后续 `state` 帧照常。前端切会话用它触发回放。
- 未知 conversationId → 不发回放，发 `ErrorMessage{ code: SESSION_NOT_FOUND }`（复用批次 1 错误信封）。

### 6.2 text 消息 conversationId 路由

`_route_v2_text` 解析 `conversationId` 并传给 `agent.process_message(text=..., conversation_id=..., protocol_version=2)`；`resolve` 规则见 §3.1（v2 显式覆盖、v2 缺省 active）。**幂等键不变**（`clientRequestId` 全局唯一，与会话无关）。

### 6.3 生成事件补 conversationId（D-i）

`generation.started` / `generation.segment.started` / `generation.delta` / `generation.segment.completed` / `generation.completed` / `generation.failed` 六类 `conversationId: Optional[str]`（= 目标会话 ID，由 loop 经 GenerationContext 携带 → OutputManager → WebSocketChannel wire）。**契约新增字段 + 方法签名加参**（§9）。`user_message.ack` 已带，填实际值。`generation.completed` 另增可选 `conversationUsage`（§3.6）。

## 7. BE-024 落地

### 7.1 GenerationRegistry（Mongo 持久化）

新文件 `core/cognitive/generation_registry.py`（注册表）+ `core/storage/generation_repo.py`（Mongo）。

**`generations` 集合文档：**
```json
{
  "generation_id": "gen_...",
  "session_id": "conv_...",
  "user_message_id": "msg_...",
  "message_id": "msg_...",           // assistant 父消息 ID
  "status": "running|completed|failed|cancelled|interrupted",
  "model": "…",
  "finish_reason": null | "stop" | "tool_calls",
  "error": null | {"code": "...", "message": "...", "retryable": false},
  "created_at": "…",
  "updated_at": "…",
  "finished_at": null | "…"
}
```

- 索引：`generation_id` unique；`session_id + updated_at`；TTL 30 天（沿用 session TTL 模式）。
- **生命周期挂点**（`core/cognitive/loop.py`）：generation 开始 **同步 `register(...)`**（insert，`status=running`；Mongo 离线时 try/except 仅 log，不阻塞生成）；`_finalize_turn` 成功 `mark(completed, finish_reason)`；错误路径 `mark(failed, error)`；`_handle_interrupt` `mark(cancelled)`；v2 断线取消 `mark(interrupted)`。
- **中断来源贯通（cancelled vs interrupted）**：`InterruptData.source`（`"websocket"` 用户打断 / `"disconnect"` v2 断线，ws_handler 已带）贯通到终态标记——`agent._handle_interrupt` 把 `data.source` 传给 `CognitiveLoop.cancel_current(source)` → `_handle_interrupt(source)` → `mark(cancelled if source != "disconnect" else interrupted)`。`cancel_current()` 增加 `source: str | None = None` 参数（默认 None → cancelled）。
- **register/mark 竞态防护**：register 同步先落（insert 完成后才进入流式）；mark 用 **upsert**（`update_one({generation_id}, {$set}, upsert=True)`）——即使 mark 先到也建档，不丢 generation。
- **崩溃遗留 stale running**：启动时 `mark_stale_interrupted(cutoff)`——把 `status=running` 且 `updated_at < 启动时刻-5min` 的 generation 批量标为 `interrupted`（客户端重连不再误判在途）。

### 7.2 状态查询 API

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/sessions/{conversationId}/generations` | `status?&cursor&limit=20`（上限 100） | `GenerationListResponse{ generations[], has_more, cursor }` |
| GET | `/api/generations/{genId}` | — | `GenerationStatus` |

**`GenerationStatus`**（含 `userMessageId`，与存储对称）：
```json
{
  "generationId": "gen_...",
  "conversationId": "conv_...",
  "messageId": "msg_...",
  "userMessageId": "msg_...",
  "status": "running|completed|failed|cancelled|interrupted",
  "model": "…",
  "finishReason": null | "stop" | "tool_calls",
  "error": null | {...},
  "createdAt": "…",
  "finishedAt": null | "…"
}
```
- 列表游标：`cursor = base64("{{created_at}}|{{generation_id}}")`，排序 `created_at DESC, generation_id DESC`，谓词同 §5.3 模式。
- 未知 genId → 404 `GENERATION_NOT_FOUND`。

### 7.3 pending 查询

| 方法 | 路径 | 响应 |
|---|---|---|
| GET | `/api/sessions/{conversationId}/pending` | `PendingQueryResponse{ pendingMessages[], pendingApprovals[] }` |

- `pendingMessages`：`agent.pending_messages`（新增只读访问器）中属于该会话、尚未出队 → `{ messageId, snippet(前 50 字), createdAt }`；
- `pendingApprovals`：`tool_adapter._manager.get_pending_approvals()` **全局返回、不按会话过滤**（D-b：工具审批全局共享，会话隔离不适用）→ `{ requestId, tool, params }`（复用 ws_handler 重连广播同源数据）。spec 明确该取舍——审批无会话归属标识（`get_pending_approvals` 元组为 `(requestId, tool, params)`），且全局工具语义下无需过滤。
- 语义：客户端重连后据此判断"我发过但未处理的请求"，决定是否重发（配合幂等）。

### 7.4 重连语义闭环

客户端重连后对齐顺序（协议 v2 文档 §6 升级）：
1. `session` 回放（消息级稳定 messageId，已有）；
2. `GET /api/sessions/{conversationId}/pending` → 补 pending 消息/审批；
3. `GET /api/sessions/{conversationId}/generations?status=running|interrupted` + `GET /api/messages/{messageId}/context` → 判断在途/中断回复；
4. 需续续 → 用**新** `clientRequestId` + 原文本重新发送 → 创建**新用户消息 + 新 generation**（幂等表不拦新 ID，BE-021 重放语义不适用）。**工具副作用重放防护属 BE-043（批次 4）**——本批次声明该跨批次依赖，工具级 `callId` 幂等由批次 4 兜底。

**终态安全重放**：`generation.completed`/`generation.failed` 均携带稳定 `generationId`/`messageId`，客户端按 `generationId` 去重可安全重放；服务端无重复投递副作用。

## 8. 资源索引（BE-032）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/sessions/{conversationId}/resources` | `category=image\|link\|video\|audio\|file\|sticker&before&limit` | `ResourceListResponse{ items[], has_more, cursor }` |

**按需扫描（D-e）**：从该会话归档按 `_id` 游标倒序扫描每条消息，按类别提取：
- `image` → `ImagePart.src`；
- `link` → `TextPart.content` 中 URL 提取（正则，统一提取 helper）；
- `video` / `audio` / `file` → `VideoPart` / `AudioPart` / `FilePart` `.src`（批次 7 前无真实数据，结构就绪返回空）；
- `sticker` → 第一版恒空（无 part 类型，批次 7 定）；
- `text` → 非资源类，`category` 不支持（走搜索）。

**每项**：`{ messageId, timestamp, kind, url }`——`url` 即下载引用（图片场景可作缩略图；**独立缩略图生成延后 BE-071**），前端可跳回来源消息。

**分页边界**：
- `before`（请求）= 上页 `cursor`（base64(`_id`)，或直接 `_id` 字符串）；`cursor` 指向**最后一次扫描到的消息 `_id`**（无论是否命中）；
- 扫描：从 `before` 继续倒序，跳过不含目标类别 part 的消息，收集命中项直到**凑够 `limit`**，再 **peek 一条**命中项判定 `has_more`（peek 不到 = 无更多）；
- 响应 `cursor` = 本次扫描停止处的 `_id`（下页回传为 `before`），避免重复扫描被跳过区段。

## 9. 契约层（frontend_contract）

`frontend_contract/rest.py` 新增（**命名避开 debug 既有同名类**；gen_ts 对既有 v1 类型字节不变，v2/新类型增量）：
- `SessionSummary`（conversationId/title/messageCount/updatedAt/active）、`SessionListResult`（**不得命名 `SessionListResponse`——与 `frontend_contract/debug.py:402` 同名，gen_ts 同名安全网抛 ValueError**）、`SessionDetail`（session + messages + active: bool）
- `SessionActivateResult`（activeConversationId）
- `MessagesPageResponse`、`MessageContextResponse`（含 conversationId）、`SearchMessagesResponse` + `SearchMessageItem`（D-q）
- `ResourceItem`、`ResourceListResponse`
- `GenerationStatus`、`GenerationListResponse`、`PendingQueryResponse`
- `ConversationUsage`（total/byModel，generation.completed 可选字段）

`frontend_contract/ws_message.py`（v2 类型增量）：
- 6 个 `generation.*` 模型补 `conversationId: Optional[str]`（`GenerationStartedMessage` 已有，其余 5 个新增）；
- `GenerationCompletedMessage` 增 `conversationUsage: Optional[ConversationUsage]`；
- `ActivateSessionMessage`（Client→Server，type=`activate_session`，conversationId 必填）加入 Client→Server 契约。

错误目录新增码：`SESSION_NOT_FOUND`（404）、`GENERATION_NOT_FOUND`（404）、`INVALID_TIMEZONE`（400）、`SESSION_PROTECTED`（409，默认会话删除保护）、`RESOURCE_NOT_FOUND`（404，消息/资源不存在，错误目录批次 1 已有码；message context 端点消息不存在时使用）。

## 10. 兼容性硬约束

1. **v1 wire 逐字节不变**：v1 连接 `resolve` 恒 `"default"`（§3.1），active 切换**不影响 v1**——v1 回放/写入/统计与批次 0-2 完全一致；v1 通道所有事件帧不变。
2. **v2 wire 增量**：`generation.*` 补 `conversationId`（6 类）、`generation.completed` 补 `conversationUsage`、新增 `activate_session` 消息；既有 v2 字段不删除不重命名。
3. **`/api/messages` 全库游标零改动保留**为兼容接口（handler 与 `rest.MessagesResponse` **不改**）；`/api/sessions/{conversationId}/messages` 是产品级会话历史接口。新增回归测试锁定 `/api/messages` wire。
4. **默认会话 `"default"` 无缝衔接**：eager 实例 + `load_active()` 恢复既有 Mongo `sessions` doc，行为不回归。
5. `contract.generated.ts`：v1 类型字节不变；v2/新类型增量（gen_ts 全量重生成，diff 仅含新增）。
6. 现有 1951 测试全绿；`make check-types` 通过。

## 11. 测试策略

| 层 | 覆盖 |
|---|---|
| 单元 | SessionManager 懒加载/ensure_loaded/switch/evict/resolve 规则（v1 恒 default / v2 active / v2 显式覆盖）；复合游标编解码 + keyset 谓词重建；自然日区间合成（单边/双边）+ timezone 转 UTC（含 `INVALID_TIMEZONE`）；资源提取（image/link 用 `.src`，video/audio/file）；自动标题（首消息前 20 字）；`ConversationUsage` 契约 round-trip |
| service | **REST 会话 CRUD**：创建/列表/重命名/删除/激活 + 404 `SESSION_NOT_FOUND`；**会话隔离**：两个会话上下文/character_state/mood 互不串（含子组件 awareness/compression/emotion 归属）；**conversationId 显式覆盖路由**（v2 显式 vs active）；**v1 恒 default 回归**（active 切走后 v1 回放/写入仍 default）；**历史分页**：`query_page` before/limit has_more 不重不漏 + `query_after` 增量；**context 窗口** `find_context` 前后 N 条；**搜索端点**：query/conversationId/自然日/category 组合 + cursor 分页 + `INVALID_TIMEZONE`；**generation 状态**：register→mark 终态生命周期（completed/failed/cancelled/interrupted）+ stale running 启动清理 + 404 `GENERATION_NOT_FOUND`；**pending 查询**（pendingMessages 按会话 + pendingApprovals 全局）；**resources 端点**：category 过滤 + cursor/before 分页 + has_more peek；**TokenTracker**：`record(session_id)` 会话累计 + `get_session_stats(sid)` + `session_total` 兼容属性不破 debug/runtime_config；`/api/messages` 兼容接口回归；Mongo 离线降级（JSONL fallback） |
| 回归 | 批次 2 v2 完整生成事件**补 conversationId** 后 wire 断言更新；取消/断线 mark 对应终态；v1 单会话聊天 wire 不变（现有测试全绿） |

**Fake 约定**：多会话测试注入 FakeLLM（复用 `_make_agent_with` 模式），Mongo 不可用走 JSONL fallback；`aiohttp_client` 建 app 复用现有 test fixture。新测试 marker 沿用 unit/service。

## 12. 验收（docs/14 门槛 + 场景）

**进入批次 4 门槛**：两个会话内容和上下文互相隔离；历史向上分页不重不漏；按日期/关键词/资源类别查询后能跳回来源消息；重连后客户端能确定回复完成/中断/续续所需状态。

**docs/14 场景 C（多会话历史）**：
1. 创建两个会话 A/B，各自聊天；
2. 切到 A 看到 A 的历史，上下文来自 A（助手回话不带 B 的内容）；
3. 历史向上分页不重不漏（`before/limit` + hasMore）；
4. 按关键词、本地自然日、资源类别搜索，结果能跳回来源消息；
5. 删除会话 B，B 的消息从归档消失，A 不受影响。

**BE-024 闭环**：发送后断线 → 重连回放 + pending 查询 + generation 状态查询 → 客户端可确定回复状态并用新 clientRequestId 续续；终态事件按 generationId 重放不产生重复副作用。

## 13. 明确不做（YAGNI）

- **不做会话级服务配置**（D-b）：模型/STT/TTS 开关全局共享；
- **不做会话级 LTM**：长期记忆跨会话；删除会话不联动删除 LTM（§4 显式）；
- **不做断点续流**：重连后中断回复用新 clientRequestId 重新生成，不做 token 级续传（BE-024 边界，批次 2 已声明）；
- **不做全文搜索引擎**：`$regex` 足够 Demo 规模；
- **不做资源持久化索引**：按需扫描（D-e）；
- **不做独立缩略图生成**：url 即下载引用/缩略图，缩略图/资源清理延后 BE-071；
- **不做 sticker 真实数据**：批次 7 定 part 类型后填实；
- **不做消息级 segmentId 定位**：搜索结果 segmentId 恒 null（D-q），跳回消息用 context 窗口；
- **不做工具审批会话归属**：pendingApprovals 全局返回（D-b 全局工具）；
- **不做多租户**：单用户 Demo（批次 1 已声明）。
