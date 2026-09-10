# Electron v2 后端契约封口（Batch 9A+9B+9C）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 todo.md「Electron v2 联调：后端契约封口」的 8 项后端待办（session conversationId / 历史分页 cursor / 未知会话拒绝 / 机器契约产物 / 统一入站校验 / 严格版本协商 / 音频 descriptor / dictation STT profile），使 Electron v2 客户端可基于明确、可验证的机器契约接入。

**Architecture:** 契约层（`frontend_contract/`）先改字段并重生成 TS；`core/input`（ws_handler/session_routes/voice_sessions/server）与 `core/storage/message_repo` 消费新契约；`core/input` 处理校验与存在性；`scripts/` + `frontend_contract` 发射器产出版本化机器契约与 golden frames。设计细节、拍板决策与 code-review 修复记录见 spec：[2026-08-09-electron-v2-contract-9abc-design.md](../../../specs/archive/2026-08/2026-08-09-electron-v2-contract-9abc-design.md)。

**Tech Stack:** Python 3.12 / pydantic 2.13 / aiohttp / motor（MongoDB）/ pytest（conda env `Elysia`）/ ruff + pyright 门禁 / Vue 前端 TS 由 gen_ts 生成。

## Global Constraints

- **契约单源**：改 `frontend_contract/` 契约字段后必须 `make gen-types` 重生成 `contract.generated.ts`；每次契约相关 task 结束跑 `make check-types`（漂移门禁）。
- **禁相对导入**（ruff TID252）：一律绝对导入；`__init__.py`/`__main__.py` 豁免。
- **生产代码参数必须类型注解**（ruff ANN001/002/003）；测试文件豁免。
- **pyright 门禁**：`conda run -n Elysia pyright`（Elysia env，全仓生产代码 core + Module + frontend_contract）必须 0 errors。
- **测试 marker**：新增测试标 `unit`/`service`/`integration`；日常用 `make test-quick`（unit+service）。
- **注释/README 中文**；代码注释中文。
- **v2-only 收紧**（spec 0.2/§5）：v2 text content 恒 `str`；`before`/`after` 只认 cursor；握手头支持集合 {缺省/`"1"`→v1, `"2"`→v2}；显式未知会话拒绝。**这些行为变化有测试锚定，勿回退。**
- **产品化 defer**：鉴权 fail-closed、幂等持久化**不在本批**。

---

### Task 1: 9A-1 — session 回放携带 conversationId

**Files:**
- Modify: `frontend_contract/ws_message.py:253-258`（SessionMessage）
- Modify: `core/input/ws_handler.py:83-98`（_send_session_replay）
- Modify: `frontend/src/types/contract.generated.ts`（gen-types 重生成）
- Test: `frontend_contract/tests/test_ws_message.py`
- Test: `core/input/tests/test_server.py`
- Test: `core/input/tests/test_session_ws.py`

**Interfaces:**
- Consumes: `SessionMessage` 现仅 `type/messages/token_stats`；`_send_session_replay(ws, agent, session)` 唯一构造点；`session.session_id` 是 `SessionId`（str 子类）。
- Produces: `SessionMessage.conversationId: str`（必填）；`_send_session_replay` 填 `str(session.session_id)`。

- [ ] **Step 1: 写失败测试（契约层）**

在 `frontend_contract/tests/test_ws_message.py` 新增：
```python
def test_session_message_has_conversation_id():
    msg = SessionMessage(messages=[], token_stats=None, conversationId="conv_abc123")
    wire = msg.wire_dict()
    assert wire["conversationId"] == "conv_abc123"
    assert wire["type"] == "session"
```

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_ws_message.py -q --tb=short`
Expected: FAIL — `TypeError: SessionMessage.__init__() got an unexpected keyword argument 'conversationId'`

- [ ] **Step 3: 契约字段**

`frontend_contract/ws_message.py` SessionMessage 加字段：
```python
class SessionMessage(ContractModel):
    """会话历史回放（新连接建立时发送）。conversationId = 本回放消息所属会话。"""

    type: Literal["session"] = "session"
    conversationId: str = Field(description="本回放消息所属会话 ID")
    messages: list[SessionMessageItem] = Field(description="会话消息列表")
    token_stats: Optional[TokenStats] = Field(default=None, description="会话级 token 统计，可缺省")
```

- [ ] **Step 4: 发送点填值**

`core/input/ws_handler.py` `_send_session_replay` 构造处：
```python
await ws.send_json(SessionMessage(
    conversationId=str(session.session_id),
    messages=items, token_stats=token_stats).wire_dict())
```

- [ ] **Step 5: 重生成 TS + 更新既有断言**

Run: `make gen-types`
`frontend/src/types/contract.generated.ts` 的 `SessionMessage` 自动 + `conversationId: string;`（不要手改）。
更新 `frontend_contract/tests/test_ws_message.py:44` 既有构造 `SessionMessage(messages=[item], token_stats=...)`（Q7）：字段必填后补 `conversationId="conv_x"` 参数。更新 `core/input/tests/test_server.py` session 回放 wire 断言（约 :790，`SessionMessage` 帧 keys 断言）加 `conversationId` 键；`core/input/tests/test_session_ws.py` `test_activate_session_replays_new_active`（:108-130）断言回放 `conversationId == 被切会话 ID`（`session.session_id` 值）。

- [ ] **Step 6: 验证测试通过**

Run:
- `conda run -n Elysia python -m pytest frontend_contract/tests/test_ws_message.py core/input/tests/test_server.py core/input/tests/test_session_ws.py -q --tb=short`
- `conda run -n Elysia python -m pytest frontend_contract/tests/test_gen_ts.py -q --tb=short`
Expected: PASS

- [ ] **Step 7: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Commit: `feat(9a1): session 回放携带 conversationId（SessionMessage 必发字段）`

---

### Task 2: 9A-2 — 历史分页显式 cursor（严格区分）

**Files:**
- Modify: `frontend_contract/rest.py:325-330`（MessagesPageResponse）
- Modify: `core/input/session_routes.py:544-566`（sessions_messages_handler）
- Modify: `core/storage/message_repo.py:297-323`（query_after 锚点 message_id → _id）
- Modify: `frontend/src/types/contract.generated.ts`（gen-types 重生成）
- Test: `core/input/tests/test_session_routes.py`
- Test: `core/storage/tests/test_message_repo.py`

**Interfaces:**
- Consumes: `MessagesPageResponse(messages/has_more/total)`；`query_page(session_id, *, before, limit) -> (messages, has_more)`；`query_after(session_id, message_id, limit) -> (messages, has_more)`（锚点 `{session_id, message_id}`）；`_contract_messages(messages)` 契约转换；`Message.metadata["_mongo_id"]` 存 Mongo `_id`（`core.schema.Message`）。
- Produces: `MessagesPageResponse.cursor: Optional[str]`；handler 从 raw `messages[-1].metadata.get("_mongo_id")` 算 cursor（空页/无更多 → None）；`query_after` 签名改 `(session_id, after_oid: ObjectId, limit)`，锚点 `{"_id": {"$gt": oid}}`。

- [ ] **Step 1: 写失败测试（repo 层 query_after 锚点改 _id）**

`core/storage/tests/test_message_repo.py` 改 `test_query_after_anchor_ascending`（:201）与 `test_query_after_limit_probe_has_more`（:210）：锚点传参从 `MessageId(...)` 改为 `ObjectId(anchor_doc["_id"])`；新增正序两页无重测试：
```python
async def test_query_after_oid_anchor_pages():
    # 插入 m1..m5，after=ObjectId(m3._id) → 返回 m4,m5（正序）
    msgs, has_more = await repo.query_after(sid, ObjectId(m3["_id"]), limit=2)
    assert [m.message_id for m in msgs] == [m4, m5]
    # 无更近消息（锚点已是会话最新，或所有更近消息已删）→ ([], False)，不抛
```
（`test_query_after_missing_anchor:219` 随锚点改 ObjectId——新 `_id > oid` 过滤器**无 message_id 查找失败特判**，「无更近消息」→ 空页 + False；Q8）

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_message_repo.py -q --tb=short`
Expected: FAIL — 现有测试传 MessageId 报签名错 / 锚点查不到返回空。

- [ ] **Step 3: 实现 query_after 锚点改 _id**

`core/storage/message_repo.py`：
```python
async def query_after(self, session_id: SessionId, after_oid: ObjectId, *, limit: int = 20) -> tuple[list[Message], bool]:
    filter_expr = {"session_id": session_id, "_id": {"$gt": after_oid}}
    docs = await self._db.find(COLLECTION, filter_expr, sort=[("_id", 1)], limit=limit + 1)
    has_more = len(docs) > limit
    return [_doc_to_message(doc) for doc in docs[:limit]], has_more
```
（doc→Message 转换是**模块级 `_doc_to_message`**（message_repo.py:76），非 self 方法——现有 `query_after`（:322）即如此。Q2）

- [ ] **Step 4: 写失败测试（路由层 cursor）**

`core/input/tests/test_session_routes.py` 新增：
- `test_page_returns_cursor`：两页数据，首页 `before` 分页 → 响应含 `cursor`（= 最旧消息 `_id`）；回传 `before=<cursor>` 第二页无重/无漏。
- `test_after_cursor_pagination`：`after=<cursor>` 正序两页无重/无漏。
- `test_invalid_cursor_400`：`before=msg_xxx` 与 `after=msg_xxx`（messageId 形式）、`after=nothex` → 400 CONTRACT_VALIDATION。
- `test_deleted_anchor_empty_page`：`after=<已删消息 _id>`（锚点是会话**最新一条**）→ 200 + `messages=[]` + `cursor` 省略 + `has_more=false`，不 500。**删非最新锚点** → 返回更新的消息（新 `_id > oid` 过滤器语义，非空页；Q8）。
- `test_page_cursor_from_raw_not_contract`：页内含无法契约化的消息（构造 raw 坏消息）→ cursor 仍指向 raw 边界（下一 before 页首条 = 未显示的最旧消息，无重复）。

- [ ] **Step 5: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -q --tb=short`
Expected: FAIL — 响应无 `cursor` 键；`after=nothex` 不 400。

- [ ] **Step 6: 实现契约 + handler**

`frontend_contract/rest.py`：
```python
class MessagesPageResponse(ContractModel):
    messages: list[Message] = Field(description="消息列表")
    has_more: bool = Field(description="是否还有更多")
    total: int = Field(description="会话消息总数")
    cursor: Optional[str] = Field(default=None, description="不透明游标（_id 形式），翻同方向下一页时回传 before/after；无更多或空页省略")
```

`core/input/session_routes.py` `sessions_messages_handler`：
```python
after = request.query.get("after", "").strip() or None
if after:
    try:
        after_oid = ObjectId(after)
    except InvalidId:
        return _error_response("CONTRACT_VALIDATION", "非法 after 游标", 400)
    messages, has_more = await agent.message_repo.query_after(SessionId(sid), after_oid, limit=limit)
else:
    before = request.query.get("before", "").strip() or None
    if before is not None:
        try:
            ObjectId(before)
        except InvalidId:
            return _error_response("CONTRACT_VALIDATION", "非法 before 游标", 400)
    messages, has_more = await agent.message_repo.query_page(SessionId(sid), before=before, limit=limit)
total = await agent.message_repo.count_by_session(SessionId(sid))
cursor = None
if messages and has_more:
    cursor = messages[-1].metadata.get("_mongo_id")
resp = rest_contract.MessagesPageResponse(
    messages=_contract_messages(messages), has_more=has_more, total=total, cursor=cursor)
```
注：`before` 分支现仍接受旧 `_id` cursor（值形式相同，向后兼容）；`after` 分支原传 `MessageId(after)` 改为 `ObjectId(after)`。`cursor` 从 **raw `messages`（契约转换前）** 取，绝不可从 `_contract_messages` 输出取。

- [ ] **Step 7: 重生成 TS + 验证**

Run: `make gen-types`；`conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py core/storage/tests/test_message_repo.py frontend_contract/tests/test_rest.py -q --tb=short`
Expected: PASS

- [ ] **Step 8: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Commit: `feat(9a2): 历史分页显式 cursor（严格区分，before/after 只认 cursor，query_after 锚点改 _id）`

---

### Task 3: 9A-3 — 显式 conversationId 验证存在（禁止幽灵会话）

**Files:**
- Modify: `core/input/ws_handler.py:501-679`（_route_v2_text）、`core/input/ws_handler.py:682-754`（_handle_voice_commit）
- Modify: `core/input/voice_sessions.py:526`（_resolve_conversation，REALTIME start）
- Modify: `core/memory/session_manager.py`（新增 helper，可选位置）
- Test: `core/input/tests/test_session_ws.py`、`core/input/tests/test_attachment_message.py`、`core/input/tests/test_voice_ws.py`

**Interfaces:**
- Consumes: `agent.session_manager.has_session(sid) -> bool`（只读、不构造）；`_send_ack(ws, message_id, client_request_id, client_message_id, conversation_id, status, error)`（`ws_handler.py:427`）；`_send_voice_error`（:396）。
- Produces: helper `_session_or_none(agent, conversation_id)`（显式未知 → None，缺省 → resolve(None,2)）；三个入口未知显式 ID 拒绝路径。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_session_ws.py` 新增（FakeAgent 需接真实 SessionManager 或 stub has_session/resolve）：
- `test_text_unknown_conversation_rejected`：v2 发 `{"type":"text","clientRequestId":"r1","content":"hi","conversationId":"conv_deadbeef"}`（未知会话）→ 收到 `user_message.ack` `status=rejected` + `error.code="SESSION_NOT_FOUND"`；`messageId` 为空；**不生成 messageId、不落库**。
- `test_text_missing_conversation_uses_active`：缺省 conversationId → ack `conversationId` = 活动会话 ID（既有正例回归）。
`core/input/tests/test_attachment_message.py`：
- `test_attachment_unknown_conversation_rejected`：合法附件 + 未知会话 → rejected SESSION_NOT_FOUND；附件 refs 不增。
`core/input/tests/test_voice_ws.py`：
- `test_voice_commit_unknown_conversation_rejected`：voice.commit + 未知会话 → **`user_message.ack` rejected + `error.code="SESSION_NOT_FOUND"`**（ACK 通道，非 ErrorMessage；Q9）。
- `test_realtime_start_unknown_conversation_rejected`：voice_session.start + 未知会话 → ErrorMessage SESSION_NOT_FOUND，不建 buffer。

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_ws.py core/input/tests/test_attachment_message.py core/input/tests/test_voice_ws.py -q --tb=short`
Expected: FAIL — 未知显式 ID 当前被 resolve 懒创建，返回 accepted 或 ErrorMessage 缺 SESSION_NOT_FOUND。

- [ ] **Step 3: 实现 helper**

`core/memory/session_manager.py` 或 `core/input/ws_handler.py` 新增：
```python
async def _session_or_none(agent, conversation_id: str | None) -> SessionMemory | None:
    """v2 显式 ID → has_session 校验；未知返回 None；缺省 → 当前 active。"""
    if conversation_id:
        if not await agent.session_manager.has_session(SessionId(conversation_id)):
            return None
        return await agent.session_manager.ensure_loaded(SessionId(conversation_id))
    return await agent.session_manager.resolve(None, 2)
```

- [ ] **Step 4: 三入口接入**

`_route_v2_text`（:540-545）替换 `resolve_session` 块，**保留外层 try/except**（`ensure_loaded` 失败降级回显 conversationId，不杀连接）：
```python
try:
    target = await _session_or_none(agent, conversation_id)
    if target is None:
        await _send_ack(ws, "", client_request_id, client_message_id, conversation_id,
                        "rejected", error={"code": "SESSION_NOT_FOUND",
                                           "message": f"会话不存在: {conversation_id}",
                                           "retryable": False})
        return True
    ack_conversation_id = str(target.session_id)
except Exception:
    logger.warning("_route_v2_text: 会话解析失败，回退回显 conversationId", exc_info=True)
    ack_conversation_id = conversation_id
```
`_handle_voice_commit`（:701-707）同理：None → **ACK rejected**（spec §0.2 拍板 voice.commit 未知会话走 `user_message.ack rejected + SESSION_NOT_FOUND`，**不是 ErrorMessage**——Q9）。复用 `_send_ack(ws, "", client_request_id, client_message_id, conversation_id, "rejected", error={"code": "SESSION_NOT_FOUND", "message": f"会话不存在: {conversation_id}", "retryable": False})` 并 return（不提交、不产生 messageId）。
`voice_sessions._resolve_conversation`（:526）：**`has_session` 校验置于函数顶部、`try` 块之外**（Q4）——该函数现有 `try/except Exception: return conversation_id or "default"`（:524-530）会吞掉 raise；显式未知 → `raise VoiceSessionError("SESSION_NOT_FOUND", f"会话不存在: {conversation_id}")`（raise 在 try 外不被吞）→ **中止 start_realtime**（不建 audio buffer 不切 REALTIME 态）；`ws_handler` voice_session.start 分支既有 try/except 已把 `VoiceSessionError` 翻译为 `_send_voice_error`。

- [ ] **Step 5: 验证测试通过**

Run: 上述三个测试文件 + `core/input/tests/test_session_ws.py` 全量
Expected: PASS

- [ ] **Step 6: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright`
Commit: `feat(9a3): 显式 conversationId 验证存在，禁止懒创建幽灵会话（text/voice.commit/realtime start）`

---

### Task 4: 9B-1 — 机器契约产物 + golden frames + 漂移检测

**Files:**
- Create: `frontend_contract/version.py`（TRANSPORT_VERSION/PROTOCOL_VERSION）
- Create: `frontend_contract/gen_schema.py`（JSON Schema 发射器）
- Create: `frontend_contract/contract.schema.json`（生成产物）
- Create: `frontend_contract/contract.raw.ts`（生成产物）
- Create: `scripts/gen_contract_frames.py`（契约层 golden frames 生成）
- Modify: `frontend_contract/__main__.py`（第二产物 --check）
- Modify: `frontend_contract/gen_ts.py`（若需 raw 产物入口）
- Modify: `scripts/capture_ws_trace.py`（标记 deprecated）
- Modify: `Makefile`（check-types 覆盖新产物）
- Modify: `docs/archive/frontend-handoff-v2/traces/*`（重生成）
- Test: `frontend_contract/tests/test___main___check.py`、`frontend_contract/tests/test_gen_schema.py`

**Interfaces:**
- Consumes: `frontend_contract/__main__.py` 的 `_SUBMODULES/_collect()/main()/--check`（现仅 TS 产物）；`ServerMessage` Annotated union（ws_message.py:883-924）；`gen_ts.generate_ts(models, discriminated)`。
- Produces: `version.TRANSPORT_VERSION="2"`、`version.PROTOCOL_VERSION="2.0"`；`gen_schema.main()` 产出 `contract.schema.json`（头带 transportVersion/protocolVersion/schemaHash）；`contract.raw.ts`；`scripts/gen_contract_frames.py` 产出 `docs/archive/frontend-handoff-v2/traces/*.jsonl`；`__main__ --check` 覆盖全部产物。

- [ ] **Step 1: 写失败测试（版本常量 + __main__ 漂移）**

`frontend_contract/tests/test___main___check.py`：
```python
def test_version_constants():
    from frontend_contract import version
    assert version.TRANSPORT_VERSION == "2"
    assert version.PROTOCOL_VERSION == "2.0"

def test_check_detects_drift(tmp_path, monkeypatch):
    # monkeypatch 输出路径到 tmp_path，先 main() 生成，改动产物内容后 runpy --check → 返回非 0
    ...
```
`frontend_contract/tests/test_gen_schema.py`：
```python
def test_schema_valid_for_discriminated_union():
    doc = gen_schema.build_document()
    assert doc["transportVersion"] == "2"
    assert doc["protocolVersion"] == "2.0"
    assert doc["schemaHash"]          # 非空
    assert "ServerMessage" in doc["models"]   # 判别联合模型在
    assert "V2TextMessage" in doc["models"]   # 客户端消息也在
    assert "properties" in doc["models"]["V2TextMessage"]  # 契约模型含 properties
```

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test___main___check.py frontend_contract/tests/test_gen_schema.py -q --tb=short`
Expected: FAIL — 模块不存在。

- [ ] **Step 3: version.py + gen_schema.py**

`frontend_contract/version.py`：
```python
TRANSPORT_VERSION = "2"      # 握手头 X-Protocol-Version 可用值（Electron 照抄即合法）
PROTOCOL_VERSION = "2.0"     # 仅展示（server_info 下发），不可用于握手头
```
`frontend_contract/gen_schema.py`：
```python
"""JSON Schema 发射器：对 ServerMessage 判别联合 + 子模块模型生成 contract.schema.json。"""
import hashlib, json, logging
from pathlib import Path
from typing import Annotated, get_args, get_origin
from pydantic import BaseModel, TypeAdapter
from frontend_contract import ws_message, rest, debug
from frontend_contract.version import TRANSPORT_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)
OUTPUT = Path(__file__).parent / "contract.schema.json"

def build_models():
    """收集 (名称, 可校验对象)：BaseModel 子类 + Annotated 判别联合（Q1）。

    `ServerMessage` 是 `Annotated[Union[...], Field(discriminator)]`，**不是 type 实例**，
    `issubclass(obj, BaseModel)` 会跳过——须按 `get_origin(obj) is Annotated` 单独收集
    （复用 `__main__._collect` 的展开思路；成员类型校验 `get_args(obj)[0].__args__`）。
    """
    for mod in (ws_message, rest, debug):
        for name, obj in vars(mod).items():
            if isinstance(obj, type) and issubclass(obj, BaseModel):
                yield name, obj
        for name, obj in vars(mod).items():
            if get_origin(obj) is Annotated:
                union = get_args(obj)[0]
                if get_origin(union) is Union and all(issubclass(c, BaseModel) for c in get_args(union)):
                    yield name, obj

def build_document() -> dict:
    schemas = {}
    for name, model in build_models():
        try:
            schemas[name] = TypeAdapter(model).json_schema()
        except Exception:
            logger.warning("JSON Schema 生成跳过模型: %s", name)  # 不静默吞——告警
    body = {"transportVersion": TRANSPORT_VERSION, "protocolVersion": PROTOCOL_VERSION,
            "schemaHash": "", "models": schemas}
    _serialized = json.dumps(body, sort_keys=True, ensure_ascii=False)   # hash 与写文件同一序列化
    body["schemaHash"] = hashlib.sha256(_serialized.encode()).hexdigest()[:16]
    return body

def main() -> None:
    doc = build_document()
    OUTPUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
```
头注释注明：「`transportVersion` 是握手头可用值；`protocolVersion` 仅展示，不可用作握手头」。**`version.py` 常量被 Task 6（`resolve_protocol_version` 用 `TRANSPORT_VERSION`）与 `ServerInfoMessage.protocol_version` 默认值（`PROTOCOL_VERSION`）消费——运行时不再手写 `"2"`/`"2.0"` 字面量，实现契约单源（P7）。**

- [ ] **Step 4: raw.ts 产物入口**

`gen_ts.py` 增加 `generate_raw_ts()`（复用 `generate_ts(models, discriminated)`），`__main__.py` 写 `contract.raw.ts`（头注释 + transportVersion/protocolVersion/schemaHash）。产物 = `ServerMessage` 判别联合 + 全部客户端消息类型（raw wire 形状，无 Vue 装饰）。

- [ ] **Step 5: __main__ 第二产物 + --check 覆盖**

`__main__.py`：`_OUTPUTS = {"ts": ..., "schema": ..., "raw_ts": ...}`；`main()` 依次生成三个产物；`--check` 对全部产物 `git diff --exit-code --no-index` 比对（复用现有逻辑循环）。**frames 不并入 `__main__`**——由独立脚本（Step 6）自带 `--check`，`make check-types` 依次跑两者。

- [ ] **Step 6: frames 生成 + 停用 capture**

`scripts/gen_contract_frames.py`（无网络，构造 `ServerMessage` 契约模型 → `wire_dict()`）：
```python
# 生成 handshake(server_info)/session/chat(user_message.ack+generation.*)/tool/voice 帧
# 输出 docs/archive/frontend-handoff-v2/traces/ws-v2-{handshake,session,chat,tool,voice}.jsonl
# 每行 = 一帧 JSON；文件头注释标注「由 frontend_contract 契约源生成，非事实源，漂移门禁兜底」
# --check 模式：生成到 tempfile 后 git diff --exit-code 比对归档（防双写污染），漂移返回 1
```
`scripts/capture_ws_trace.py`：文件头 + 入口加 `DEPRECATED` 标记（不再作 traces 生成工具，避免与契约源生成双写）。

- [ ] **Step 7: 重生成产物 + 验证**

Run: `python -m frontend_contract`（生成 ts/schema/raw_ts）→ `python scripts/gen_contract_frames.py` → `python -m frontend_contract --check`（Exit 0）
`conda run -n Elysia python -m pytest frontend_contract/tests/ -q --tb=short` Expected: PASS

- [ ] **Step 8: Makefile + 门禁 + 提交**

`Makefile` check-types 注释补充「覆盖 ts + schema + raw_ts + frames 漂移」。Run: `make check-types && make lint && conda run -n Elysia pyright`
Commit: `feat(9b1): 版本化机器契约产物（JSON Schema + raw TS + golden frames）+ 漂移门禁`

---

### Task 5: 9B-2 — 统一入站校验（ClientMessage 判别联合 + 入口接入）

**Files:**
- Modify: `frontend_contract/ws_message.py`（ClientMessage 判别联合，缺失客户端消息类补齐）
- Modify: `core/input/ws_handler.py:757-1069`（handle_json_message 入口校验 + 分支改模型字段 + 修断连 bug）
- Modify: `frontend/src/types/contract.generated.ts`（gen-types 重生成）
- Test: `core/input/tests/test_v2_ws.py`、`core/input/tests/test_voice_ws.py`、`frontend_contract/tests/test_ws_message.py`

**Interfaces:**
- Consumes: 现有 23 个客户端消息类（ws_message.py:636-878）；`ServerMessage` 判别联合模式（:883-924）；`_find_channel(agent, ws)`（:374，取 `channel.protocol_version`）；`_send_ack`。
- Produces: `ClientMessage = Annotated[Union[成员], Field(discriminator="type")]`（**不含 TextMessage**）；`handle_json_message` v2 顶部 `TypeAdapter(ClientMessage).validate_python(data)`，成功分支用模型字段、失败回 `ErrorMessage` 不中断连接；`:304` 单参 ValidationError 删除。

- [ ] **Step 1: 写失败测试（判别联合可构造 + 负面校验）**

`frontend_contract/tests/test_ws_message.py`：
```python
def test_client_message_discriminated_union():
    from pydantic import TypeAdapter
    m = TypeAdapter(ClientMessage).validate_python({"type": "text", "content": "hi", "clientRequestId": "r1"})
    assert m.type == "text"
    assert isinstance(m, V2TextMessage)
```
`core/input/tests/test_v2_ws.py` 新增（表驱动负面，走 handle_json_message 或 WS 集成）：
- unknown type `{"type":"nope"}` → ErrorMessage code=CONTRACT_VALIDATION，**连接存活**（随后正常 `{"type":"interrupt"}` 仍被处理）。
- 错误枚举：`{"type":"set_service","service":"llm","enabled":true}` 非法 service → CONTRACT_VALIDATION。
- **text 缺 clientRequestId** `{"type":"text","content":"hi"}` → **ACK rejected** CONTRACT_VALIDATION（text 不走判别联合，保留既有 §4.2 ACK 通道）。
- **v2 list-content text** `{"type":"text","content":[...]}` → **ACK rejected** CONTRACT_VALIDATION（_route_v2_text 内 content 非 str 检查）。
- **非 text 缺必填**：`{"type":"dictation.start"}`（缺 clientRequestId）→ ErrorMessage CONTRACT_VALIDATION。
- 错误字段类型（非 text）：`{"type":"interrupt","extra":123}` 不影响（无必填字段）；`{"type":"voice.record.start"}` 缺 recordingId → ErrorMessage。
- 非 dict 帧（顶层数组/字符串）→ ErrorMessage 而非断连（修 :304 bug）。

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_ws_message.py core/input/tests/test_v2_ws.py -q --tb=short`
Expected: FAIL — 无 ClientMessage 符号 / 未知 type 被静默忽略 / 非 dict 断连。

- [ ] **Step 3: ClientMessage 判别联合**

`frontend_contract/ws_message.py` 在客户端消息类区（:878 后）：
```python
from typing import Annotated, Union  # 已 import
ClientMessage = Annotated[
    Union[
        V2TextMessage, InterruptMessage, SwitchModelMessage, SetServiceMessage,
        SetTtsVoiceMessage, ToolApprovalMessage, WakeToggleMessage, ActivateSessionMessage,
        VoiceRecordStartMessage, VoiceRecordStopMessage, VoiceCommitMessage, VoiceDiscardMessage,
        VoiceSessionStartMessage, VoiceSessionPauseMessage, VoiceSessionResumeMessage,
        VoiceSessionStopMessage, VoiceSessionCancelMessage, DictationStartMessage,
        DictationStopMessage, DictationCancelMessage, AudioCancelMessage, ToolStatusMessage,
    ],
    Field(discriminator="type"),
]
```
**不含 `TextMessage`**（type="text" 与 V2TextMessage 撞 discriminator）。逐类核对 `type` 字面量唯一（text/interrupt/switch_model/set_tts_voice/set_service/tool_approval/wake_toggle/activate_session/voice.record.start/voice.record.stop/voice.commit/voice.discard/voice_session.start/voice_session.pause/voice_session.resume/voice_session.stop/voice_session.cancel/dictation.start/dictation.stop/dictation.cancel/audio.cancel/tool.status）。handler 分支有而契约缺的 msg_type（对照 ws_handler.py:770-1068）补齐建模。注：判别联合供 schema/raw.ts 与类型完整性；**handle_json_message 入口对 `type="text"` 特判跳过**（Step 5），判别联合本身仍含 V2TextMessage。

- [ ] **Step 4: 修 :304 断连 bug**

`ws_handler.py` handle_ws TEXT 分支：`if not isinstance(data, dict)` 分支删除 `raise ValidationError(...)` 单参构造，改为直接 `await ws.send_json(ErrorMessage(message="WS 消息必须是 JSON 对象", code="CONTRACT_VALIDATION", requestId=_current_request_id()).wire_dict())` 后 `continue`（或并入统一入口校验）。

- [ ] **Step 5: handle_json_message 入口接入**

`handle_json_message` 顶部（v2 判定）：
```python
channel = _find_channel(agent, ws)
if channel is not None and channel.protocol_version >= 2:
    if data.get("type") == "text":
        # v2 text 不走判别联合——保留既有 ACK rejected 通道（缺 clientRequestId → ACK rejected，spec §4.2）。
        # 判别联合的 V2TextMessage.clientRequestId 必填会把缺 key 变成 ErrorMessage，改变既有 v2 错误通道。
        # content 类型收紧由 _route_v2_text 内部完成。
        msg = None
    else:
        try:
            msg = TypeAdapter(ClientMessage).validate_python(data)
        except ValidationError as e:
            await ws.send_json(ErrorMessage(
                message=f"Invalid message: {e}", code="CONTRACT_VALIDATION",
                requestId=_current_request_id()).wire_dict())
            return
else:
    msg = None  # v1 保持 data.get 老路径
```
各分支：`msg is not None` 时改用模型字段（如 `msg.service`/`msg.enabled`/`msg.recordingId`），v1 分支保持 `data.get`。**text 分支（Q6）**：`content` 非 str 检查必须放在 text 分支的 **isinstance 分发之前**——现有 text 分支（:775-800）对 list 直接 `process_message` 多模态路径、从不进 `_route_v2_text`，且 `text_content.strip()` 对 list 会 AttributeError：
```python
elif msg_type == "text":
    if channel is not None and channel.protocol_version >= 2 and not isinstance(text_content, str):
        # v2 text 收紧为 str（媒体走 attachments/stickerId）
        await _send_ack(ws, "", data.get("clientRequestId", ""), data.get("clientMessageId"),
                        data.get("conversationId"), "rejected",
                        error={"code": "CONTRACT_VALIDATION", "message": "v2 text content 必须为字符串",
                               "retryable": False})
        return
    if isinstance(text_content, str): ...  # 现有 str 分发（v2 → _route_v2_text）
    elif isinstance(text_content, list): ...  # 仅 v1 多模态（v2 已被上面拦截）
```
**逐步迁移、保持既有行为**：每分支先加 v2 模型取值，v1 分支不动。

- [ ] **Step 6: 重生成 TS + 验证通过**

Run: `make gen-types`；`conda run -n Elysia python -m pytest core/input/tests/test_v2_ws.py core/input/tests/test_voice_ws.py core/input/tests/test_queue_full.py core/input/tests/test_attachment_message.py frontend_contract/tests/test_ws_message.py -q --tb=short`
Expected: PASS（含既有 TestV2AckIdempotency/TestV2Reconnect/TestQueueFullAck 回归）

- [ ] **Step 7: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Commit: `feat(9b2): 统一入站校验——ClientMessage 判别联合入口 + 修非 dict 断连 bug`

---

### Task 6: 9B-3 — 协议版本协商显式拒绝不兼容版本

**Files:**
- Modify: `core/input/server.py:333-352`（ws_handler wrapper，版本检查提前到升级前）
- Modify: `core/input/ws_handler.py:129-131`（协商判定函数化，删除静默降级）
- Modify: `frontend_contract/ws_message.py`（ServerInfoMessage.protocol_version 默认值 → PROTOCOL_VERSION 单源）
- Modify: `core/input/capabilities.py`（build_server_info 显式传 protocol_version=PROTOCOL_VERSION）
- Test: `core/input/tests/test_version.py`、`core/input/tests/test_auth.py`

**Interfaces:**
- Consumes: `_protocol_error_response(code, message, status, retryable)`（server.py:105-119）；`ProtocolError` 契约（rest.py:155-166）；`X-Protocol-Version` 头。
- Produces: 支持集合判定 `_resolve_protocol_version(header) -> int | None`（缺省/`"1"` → 1，`"2"` → 2，其余 → None）；`None` → HTTP 400 ProtocolError（message 明示支持集合）。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_version.py` 新增：
- `test_v2dot0_handshake_rejected_400`：`X-Protocol-Version: "2.0"` → HTTP 400 + ProtocolError（非 101）。
- `test_v3_handshake_rejected_400`：`"3"` → 400。
- `test_garbage_handshake_rejected_400`：乱拼写 → 400。
- `test_explicit_v1_handshake_v1`：`"1"` → 走 v1（既有 legacy 路径）。
- `test_default_handshake_v1`：缺省头 → v1（既有回归）。

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_version.py -q --tb=short`
Expected: FAIL — `"2.0"` 当前静默 v1 可连接。

- [ ] **Step 3: 判定函数 + 提前拒绝**

`core/input/ws_handler.py` 抽判定（`handle_ws` 内 `ws.prepare` 之前可调，但放 server.py wrapper 更早）。**单源**：用 `frontend_contract.version.TRANSPORT_VERSION`（Task 4 定义），`"1"` 是历史 v1 保留字面量：
```python
from frontend_contract.version import TRANSPORT_VERSION

def resolve_protocol_version(header: str) -> int | None:
    if header in ("", "1"):
        return 1
    if header == TRANSPORT_VERSION:   # "2"
        return 2
    return None
```
**展示版本单源（P7）**：`frontend_contract/ws_message.py` `ServerInfoMessage.protocol_version` 默认值改为 `from frontend_contract.version import PROTOCOL_VERSION`（`Field(default=PROTOCOL_VERSION, ...)`）；`core/input/capabilities.py` `build_server_info` 显式传 `protocol_version=PROTOCOL_VERSION`。运行时不再手写 `"2.0"` 字面量——改协议版本只改 `version.py` + `make gen-types`。
`core/input/server.py` `ws_handler` wrapper：在调用 `handle_ws` 前读 `X-Protocol-Version`，`resolve_protocol_version` 返回 None → `_protocol_error_response("CONTRACT_VALIDATION", "不支持的协议版本 X-Protocol-Version（支持: 缺省/\"1\"→v1, \"2\"→v2）", 400, False)`。`handle_ws` 内改用 `resolve_protocol_version`（返回 None 理论上不可达，防御处理）。

- [ ] **Step 4: 验证通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_version.py core/input/tests/test_auth.py core/input/tests/test_desktop_ws.py -q --tb=short`
Expected: PASS（`"1"`/缺省/`"2"`/electron 组合全部绿）

- [ ] **Step 5: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright`
Commit: `feat(9b3): 协议版本协商显式拒绝不兼容版本（支持 {缺省/"1"→v1, "2"→v2}）`

---

### Task 7: 9C-1 — 固定并可验证客户端输入音频契约

**Files:**
- Modify: `frontend_contract/ws_message.py`（AudioInputDescriptor + 三种 start 帧 audio?）
- Modify: `core/input/voice_sessions.py`（start 帧 descriptor 校验 + feed_pcm chunk 上限）
- Modify: `core/input/ws_handler.py:321-329`（BINARY 喂入前 chunk 校验）
- Modify: `core/input/audio_buffer.py`（_process_segment 段边界整帧对齐 + RECORDING stop 截断）
- Modify: `frontend/src/types/contract.generated.ts`（gen-types 重生成）
- Create: `core/input/tests/fixtures/sample_16k_mono.pcm`（真实 PCM fixture）
- Test: `core/input/tests/test_voice_sessions.py`、`core/input/tests/test_audio_buffer.py`、`core/input/tests/test_voice_ws.py`

**Interfaces:**
- Consumes: `_FRAME_BYTES=640`/`_FRAME_MS=20`（audio_buffer.py:30-34）；`pcm_to_wav(pcm_data, sample_rate=16000, channels=1, bits=16)`（:37）；`VoiceSessionManager.feed_pcm(chunk)`（voice_sessions.py:450）；`_send_voice_error`；三种 start 帧现状无 audio 字段。
- Produces: `AudioInputDescriptor(codec/sampleRate/channels)`；start 帧 `audio: Optional[AudioInputDescriptor]`；`MAX_CHUNK_BYTES=1 MiB` 校验；段/WAV 边界 640 整帧对齐。

- [ ] **Step 1: 写失败测试**

`frontend_contract/tests/test_ws_message.py`：
```python
def test_voice_start_audio_descriptor():
    m = VoiceSessionStartMessage(sessionId="s1", audio={"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1})
    assert m.audio.sampleRate == 16000
```
`core/input/tests/test_voice_ws.py` / `test_voice_sessions.py`：
- `test_recording_start_bad_descriptor_rejected`：`voice.record.start` 带 `audio={codec:"opus",...}` → `_send_voice_error` CONTRACT_VALIDATION，不建会话。
- `test_realtime_bad_descriptor_rejected`：voice_session.start 48k/stereo → CONTRACT_VALIDATION。
- `test_chunk_too_large_rejected`：BINARY > MAX_CHUNK_BYTES → CONTRACT_VALIDATION + 会话终止。
- `test_arbitrary_length_chunk_accepted`：非 640 倍数 chunk 喂入不报错（VAD 只扫整帧）。
- `test_segment_wav_frame_aligned`：`_process_segment` 后 WAV 长度 = 整帧倍数（含尾部截断断言）。
- `test_record_stop_drops_tail`：RECORDING stop 时 `_rec_pcm` 尾部 < 640B 丢弃，WAV 长度整帧。
- `test_descriptor_omitted_default_format`：缺省 audio → 默认 16k/mono 正常（向后兼容）。

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py core/input/tests/test_audio_buffer.py frontend_contract/tests/test_ws_message.py -q --tb=short`
Expected: FAIL — 无 audio 字段/无 chunk 上限/段未对齐。

- [ ] **Step 3: 契约**

`frontend_contract/ws_message.py`：
```python
class AudioInputDescriptor(ContractModel):
    codec: str = Field(description="编码（唯一支持 pcm_s16le）")
    sampleRate: int = Field(description="采样率（唯一支持 16000）")
    channels: int = Field(description="声道数（唯一支持 1）")
```
三种 start 帧各加 `audio: Optional[AudioInputDescriptor] = Field(default=None, description="输入音频描述（缺省按 16k/mono/pcm_s16le）")`。

- [ ] **Step 4: start 帧校验**

`voice_sessions.py` 三个 start 方法增加 `audio_descriptor: AudioInputDescriptor | None = None` 参数：
```python
def _validate_audio_descriptor(self, desc) -> str | None:
    """返回错误 code 或 None。唯一支持 pcm_s16le/16000/1。"""
    if desc is None:
        return None
    if desc.codec != "pcm_s16le" or desc.sampleRate != 16000 or desc.channels != 1:
        return "CONTRACT_VALIDATION"
    return None
```
`ws_handler.py` voice.* start 分支（:945-984/1021）把 `data.get("audio")` 解析为 descriptor 传给 start，非法 → `_send_voice_error` 并 return（不建会话）。

- [ ] **Step 5: chunk 上限 + 段对齐**

`voice_sessions.py` 加 `MAX_CHUNK_BYTES = 1 * 1024 * 1024`；`feed_pcm` 入口：`len(chunk) > MAX_CHUNK_BYTES` → `_emit_error("CONTRACT_VALIDATION", ...)` + **`self._teardown_session()`**（**同步方法，`voice_sessions.py:160 def`，勿 await**——Q3；**完整拆除** REALTIME/DICTATION 的 `_buffer/_ack_task/_idle_task/_session_id`——**不能用只清 RECORDING 字段的 `_reset_recording()`**，那会让会话静默无声）+ 发 `VoiceSessionEndedMessage` 收尾。校验只在 `feed_pcm` 一处（`ws_handler` BINARY 分支不重复强制，只透传）。
`audio_buffer.py`：`_process_segment` 构造 WAV 前 `audio = audio[: len(audio) // _FRAME_BYTES * _FRAME_BYTES]`；RECORDING stop（voice_sessions.py:217 `pcm_to_wav(b"".join(self._rec_pcm))`）前同样截断 `_rec_pcm` 尾部。

- [ ] **Step 6: 真实 PCM fixture + 重生成 TS + 验证**

生成 `core/input/tests/fixtures/sample_16k_mono.pcm`（32000 bytes = 1s 16k/mono/s16le，可用 `Module/stt/tests/fixtures/sample.wav` 数据截取或程序化生成）。全链路 recording/realtime/dictation 用 fixture 喂入。
Run: `make gen-types`；上述测试文件全绿。

- [ ] **Step 7: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Commit: `feat(9c1): 输入音频契约固定格式 + descriptor 校验 + chunk 上限 + 段整帧对齐`

---

### Task 8: 9C-2 — Dictation 会话内 STT profile（不改全局活动 STT）

**Files:**
- Modify: `frontend_contract/ws_message.py`（DictationStartMessage.sttProfileId）
- Modify: `core/input/voice_sessions.py`（start_dictation profile 快照 + _transcribe_no_emotion 用快照 + 释放）
- Modify: `core/input/ws_handler.py`（dictation.start 传 sttProfileId）
- Modify: `frontend/src/types/contract.generated.ts`（gen-types 重生成）
- Test: `core/input/tests/test_voice_sessions.py`、`core/input/tests/test_voice_ws.py`

**Interfaces:**
- Consumes: `_stt_available()`（voice_sessions.py:122，依赖全局 `agent.stt`）；`_transcribe_no_emotion(wav)`（:126，读 `agent.stt`）；`_teardown_session()`（:160，同步）；`STTBackend.close()` async（Module/stt/protocol.py:62）；`STTFactory.create(model_id, **kwargs)`（Module/stt/registry.py:22，custom 无 base_url 抛 ValueError）；`resolve_model_params(svc, model_id)`（core/input/custom_providers.py:151）；`llm_adapter.py:194-198` close guard。
- Produces: `DictationStartMessage.sttProfileId: Optional[str]`；`VoiceSessionManager.start_dictation(client_request_id, stt_profile_id=None)`；`self._dictation_stt` 快照 backend；`_transcribe_no_emotion` 优先用快照；`_teardown_session` 释放（fire-and-forget + guard）。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_voice_sessions.py`：
- `test_dictation_profile_uses_snapshot_backend`：`start_dictation("r1", "sensevoice/sense-voice")` → 转写走独立 backend（fake 断言 `_dictation_stt` 被调）；**全局 `agent.stt` 未被替换**（`agent.stt` 仍为原 backend）；`service_state.stt_model` 不变。
- `test_dictation_profile_not_found`：`start_dictation("r1", "no/such-provider")` → `_emit_error` `STT_PROFILE_NOT_FOUND`，不进入 DICTATION 态。
- `test_dictation_profile_runtime_desktop_rejected`：runtime=desktop custom profile → STT_PROFILE_NOT_FOUND。
- `test_dictation_mid_switch_stt_keeps_snapshot`：dictation 进行中全局 `switch_stt` → 已开始会话继续用快照 backend。
- `test_dictation_profile_released_on_stop`：stop_dictation → 快照 backend `close()` 被调（mock 断言 fire-and-forget 任务执行）。

- [ ] **Step 2: 验证测试失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_voice_sessions.py -q --tb=short`
Expected: FAIL — start_dictation 无 stt_profile_id 参数。

- [ ] **Step 3: 契约**

`frontend_contract/ws_message.py` DictationStartMessage：
```python
class DictationStartMessage(ContractModel):
    type: Literal["dictation.start"] = "dictation.start"
    clientRequestId: str = Field(description="客户端请求 ID")
    sttProfileId: Optional[str] = Field(default=None, description="STT profile（<provider>/<model>，缺省用全局活动 STT）")
```

- [ ] **Step 4: start_dictation 快照**

`voice_sessions.py`：
```python
async def start_dictation(self, client_request_id: str, stt_profile_id: str | None = None,
                          audio_descriptor: AudioInputDescriptor | None = None) -> None:
    """签名含 audio_descriptor（Task 7 三种 start 帧共用）与 stt_profile_id（本 task）。"""
    backend = None
    if stt_profile_id:
        # resolve_model_params 是同步函数（custom_providers.py:134 def），勿 await（P1）
        params = resolve_model_params("stt", stt_profile_id)
        runtime = (params or {}).pop("runtime", "backend")
        if runtime != "backend":
            await self._emit_error("STT_PROFILE_NOT_FOUND", f"STT profile 不可由后端执行: {stt_profile_id}")
            return
        try:
            if params and params.get("base_url"):
                # SSRF 出站守卫（Q5）：validate_endpoint/build_guarded_http_client 在 core.input.ssrf
                # （ssrf.py:84/:164，非 custom_providers）。custom+backend 须注入 http_client_factory
                # **工厂**再 create——对齐 switch_stt（server.py:58/65-66 lambda 工厂），仅静态
                # validate_endpoint 不防 OpenAI 系 backend 以默认 client 出站建连。
                from core.input.ssrf import build_guarded_http_client, validate_endpoint
                validate_endpoint(params["base_url"])
                params["http_client_factory"] = lambda b=params["base_url"]: build_guarded_http_client(b)
            backend = STTFactory.create(stt_profile_id, **params)   # params 已 pop runtime，勿透传
        except Exception:
            await self._emit_error("STT_PROFILE_NOT_FOUND", f"STT profile 不存在: {stt_profile_id}")
            return
    elif not self._stt_available():
        # Q10：保留现状显式 raise（voice_sessions.py:348-349，含 retryable=True）——
        # _voice_stt_gate 只对已有活动会话生效（has_active_session 首行），新 dictation 不兜底。
        # 缺省 profile 且全局 STT 不可用 → 显式拒绝，否则静默进无后端 DICTATION 态。
        raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
    self._dictation_stt = backend   # None → 用全局 agent.stt
    self._buffer = AudioBuffer(stt_transcribe=self._make_dispatch_stt(self._transcribe_no_emotion), ...)
    self._mode = VoiceMode.DICTATION
```
`_transcribe_no_emotion`：`backend = self._dictation_stt or getattr(self._agent, "stt", None)`。

- [ ] **Step 5: ws_handler 传参**

`ws_handler.py` dictation.start 分支（:1021-1029）：`await voice_mgr.start_dictation(data.get("clientRequestId", ""), data.get("sttProfileId"))`。

- [ ] **Step 6: 释放（fire-and-forget + guard）**

`_teardown_session()`（或 `stop_dictation`/`cancel_dictation` 公共释放点）：
```python
if self._dictation_stt is not None:
    stt = self._dictation_stt
    self._dictation_stt = None
    try:
        asyncio.get_running_loop().create_task(stt.close())
    except RuntimeError:
        logger.warning("dictation profile STT close 无 running loop，跳过（泄漏告警）")
```
（照抄 `llm_adapter.py:194-198` 的 try/except RuntimeError 守卫。）

- [ ] **Step 7: 重生成 TS + 验证通过**

Run: `make gen-types`；`conda run -n Elysia python -m pytest core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py frontend_contract/tests/test_ws_message.py -q --tb=short`
Expected: PASS（既有 dictation 隔离/D6 情绪测试全绿）

- [ ] **Step 8: 门禁 + 提交**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Commit: `feat(9c2): dictation 会话内 STT profile（快照独立 backend，不改全局活动 STT）`

---

### Task 9: 集成验证 + 全量门禁

**Files:**（无代码改动，验证收口）
- Test: 全量 `make test-quick` + `make test-normal`

- [ ] **Step 1: test-quick**

Run: `make test-quick`
Expected: 全绿（8 项 feature 测试 + 既有回归）

- [ ] **Step 2: test-normal**

Run: `make test-normal`
Expected: 全绿（排除 slow；flaky 的 LLM 集成测试如偶发失败需在最终 review 中说明根因，不阻塞）

- [ ] **Step 3: 全部门禁**

Run: `make lint && conda run -n Elysia pyright && make check-types`
Expected: 0 errors

---

## Self-Review

**1. Spec 覆盖（对照 spec 8 项）：**
- 9A-1 session conversationId → Task 1 ✓
- 9A-2 历史 cursor（严格区分 + query_after 锚点 _id + cursor 空页守卫）→ Task 2 ✓
- 9A-3 未知会话拒绝（三入口含 voice_sessions._resolve_conversation）→ Task 3 ✓
- 9B-1 机器契约产物（version/gen_schema/raw_ts/frames/__main__ 漂移/停用 capture）→ Task 4 ✓
- 9B-2 ClientMessage 判别联合（不含 TextMessage、修 :304 断连 bug、v2 text str 收紧）→ Task 5 ✓
- 9B-3 版本协商（支持 {缺省/"1"/"2"}、升级前 400、echo "2.0" 缓解）→ Task 6 ✓
- 9C-1 音频 descriptor（去 chunkDurationMs、feed 不强制整帧、段边界对齐、MAX_CHUNK_BYTES、fixture）→ Task 7 ✓
- 9C-2 dictation profile（快照、不改全局、fire-and-forget+guard 释放）→ Task 8 ✓

**2. Placeholder 扫描：** 无 TBD/TODO；每个 task 有明确文件、接口、测试断言与实现要点。契约字段/签名在 Task Interfaces 块给出。

**3. 类型一致性：**
- `_send_ack` 签名（Task 3）与现有 `ws_handler.py:427` 一致。
- `query_after` 新签名 `(session_id, after_oid: ObjectId, limit)`（Task 2）与 `sessions_messages_handler` 调用一致。
- `start_dictation(client_request_id, stt_profile_id=None)`（Task 8）与 `ws_handler` dictation.start 分支一致。
- `AudioInputDescriptor` 字段（codec/sampleRate/channels）在 Task 7 契约与校验中一致；Task 4 `contract.schema.json` 自动含该类型。
- `ClientMessage` 成员清单（Task 5）与 ws_message.py 类名一致（VoiceSessionPause/Resume/Stop/Cancel 用现类名）。

**4. 与 spec 修复记录（R1-R15）一致性：** R1（判别联合不含 TextMessage）→ Task 5 Step 3；R9（query_after 锚点 _id）→ Task 2 Step 3；R10（cursor 空页守卫）→ Task 2 Step 6；R11（段对齐所有模式）→ Task 7 Step 5；R13（fire-and-forget guard）→ Task 8 Step 6；R14（"1"→v1 保留）→ Task 6 Step 3；R15（去 chunkDurationMs）→ Task 7 Step 3。全部落实。

**5. Plan review 修复记录（2026-08-09，plan 初稿 review 后）：**

| # | finding（review 原文要点） | 定案 | 落点 |
|---|---------------------------|------|------|
| P1 | Task 8 `await resolve_model_params`（sync 函数）→ TypeError 杀连接 | 去掉 await，同步调用 | Task 8 Step 4 |
| P2 | Task 8 把 `runtime` 透传 `STTFactory.create` → custom profile 全被拒 | `params.pop("runtime")` 后建后端 + SSRF `validate_endpoint`（复用 `_resolve_switch_params` 模式） | Task 8 Step 4 |
| P3 | Task 3 `_send_voice_error(code, message)` 两参 vs 签名 `(ws, VoiceSessionError)` | 改传 `VoiceSessionError("SESSION_NOT_FOUND", ...)` | Task 3 Step 4 |
| P4 | Task 5 自相矛盾：缺 clientRequestId 期望 ACK rejected 但判别联合先发 ErrorMessage（改变既有 v2 错误通道） | **text 分支跳过判别联合**（保留 ACK rejected）；content 类型收紧在 `_route_v2_text` 内；非 text 走联合 | Task 5 Step 1/5 |
| P5 | Task 3 realtime 未知会话 emit+reset 不中止 start_realtime（仍建 buffer 对 ghost _conv_id） | `_resolve_conversation` **raise `VoiceSessionError`**（ws 分支已翻译）；不 reset | Task 3 Step 4 |
| P6 | Task 4 测试引用未定义的 `build_server_schema()/header()` + `schema["properties"]` KeyError | 测试改断言 `build_document()` 实际形状 | Task 4 Step 1 |
| P7 | Task 4 version 常量未接入运行时（ws_handler/capabilities 仍手写 "2"/"2.0"），单源声明不成立 | `resolve_protocol_version` 用 `TRANSPORT_VERSION`；`ServerInfoMessage` 默认值 + `build_server_info` 用 `PROTOCOL_VERSION` | Task 4 Step 3 / Task 6 Step 3 |
| P8 | Task 4 schemaHash hash 用 ensure_ascii=True 但写文件 False（中文描述字节不一致）+ 静默吞模型错误 | hash 与写文件同一 `ensure_ascii=False` 序列化；跳过模型告警不静默 | Task 4 Step 3 |
| P9 | Task 7 超大 chunk `_reset_recording()` 只清 RECORDING，REALTIME/DICTATION 会话静默失效 + 双重强制 | `feed_pcm` 超限 → `_teardown_session()` + `VoiceSessionEndedMessage`；校验只在 feed_pcm 一处 | Task 7 Step 5 |

**6. Plan 复验修复记录（2026-08-09，plan 复验 review 后）：**

| # | finding（review 原文要点） | 定案 | 落点 |
|---|---------------------------|------|------|
| Q1 | `gen_schema.build_models` 只收 BaseModel 子类，`ServerMessage` Annotated 联合被过滤（schema 缺核心联合，测试必败） | `build_models` 额外按 `get_origin(obj) is Annotated` 收集判别联合（复用 `__main__._collect` 思路），`schemas["ServerMessage"]` 入档 | Task 4 Step 3 |
| Q2 | `query_after` snippet 调 `self._to_message`，实际是模块级 `_doc_to_message`（message_repo.py:76） | snippet 改 `_doc_to_message(doc)` | Task 2 Step 3 |
| Q3 | Task 7 对同步 `_teardown_session()`（:160 def）写 `await` → TypeError | 去掉 await，同步调用 | Task 7 Step 5 |
| Q4 | `_resolve_conversation` 的 raise 被现有 `try/except Exception: return ...`（:524-530）吞掉，幽灵会话照建 | `has_session` 校验置于函数**顶部、try 之外**，raise 不被吞 | Task 3 Step 4 |
| Q5 | dictation custom STT 只静态 `validate_endpoint`，缺 `http_client_factory` 出站守卫；模块注释错指 | 注入 `http_client_factory` 工厂 lambda（server.py:58/65-66 模式，`core.input.ssrf`）；模块注释改正 | Task 8 Step 4 |
| Q6 | v2 list-content text 走 `process_message` 不进 `_route_v2_text`，content 检查落点错 | content 非 str 检查放 text 分支 **isinstance 分发之前**（v2 且 list → ACK rejected） | Task 5 Step 5 |
| Q7 | `SessionMessage` 必填 conversationId 破 `test_ws_message.py:44` 既有构造 | Task 1 Step 5 补该构造 `conversationId` 参数 | Task 1 Step 5 |
| Q8 | 删锚点测试与 `_id > oid` 过滤器冲突（无 missing-anchor 特判，仅锚点为最新才空页） | 测试改「锚点是会话最新一条 → 空页」；删非最新锚点 → 返回更新消息 | Task 2 Step 1/4 |
| Q9 | voice.commit 未知会话用 ErrorMessage，但 spec §0.2 拍板 ACK rejected（client 等不到 ack） | 改 `_send_ack rejected + SESSION_NOT_FOUND`（ACK 通道）；REALTIME start 才 ErrorMessage | Task 3 Step 1/4 |
| Q10 | 全局 STT 不可用门控省略（`_voice_stt_gate` 只对已活动会话生效）→ 静默进无后端 DICTATION | 缺省 profile 分支保留现状 `raise VoiceSessionError(ERR_STT_UNAVAILABLE, ..., True)`（:348-349） | Task 8 Step 4 |
