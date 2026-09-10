# Electron v2 二次联调 WS replay 身份修复实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 BE-INT2-P0-01 + P1-01——WS `session` replay 的 tool 项输出 `message_id`/`timestamp`，与 REST 身份一致；契约与测试守住 tool 身份不变量。

**Architecture:** `messages_for_client()` tool 分支复用消息对象已持久/派生的 ID（D1）→ `ClientToolMessage` 类型加必填身份（D2）→ `SessionMessageItem` 契约收紧（D3）→ 三产物重生成（D4）→ 测试补全。

**Tech Stack:** Python 3.12 / Pydantic / aiohttp / pytest（marker: unit + service）。

## Global Constraints

- 复用消息对象已持久化/稳定派生的 `message_id`/`timestamp`，**绝不**在 replay 时调用随机 ID 生成器。
- 不把 `tool_call_id` 改名/冒充 `message_id`；不放宽 REST `message_id` 非空约束。
- 三类 replay 项（user/assistant/tool）对同一条归档消息与 REST 使用同一 `message_id`/`timestamp`。
- v1 影响：tool 项新增身份字段（附加，v1 前端安全）；v1 replay 同样要求身份（均有，无回归）。
- 代码注释中文；参数类型注解（ruff ANN 门禁）；禁相对导入；测试 marker 按 `docs/testing.md`。
- conda env：`Elysia`。

---

### Task 1: D1+D2 — tool replay 输出身份 + `ClientToolMessage` 类型

**Files:**
- Modify: `core/memory/session.py`（`messages_for_client` ToolResultMessage 分支）
- Modify: `core/schema/message.py`（`ClientToolMessage` 加必填字段）
- Test: `core/input/tests/test_server.py`（加 tool replay 身份断言）

**Interfaces:**
- Consumes: 首轮批次已保证内存 tool 消息 `message_id` 恒非空（新写入 `new_message_id()` + 适配器派生）。
- Produces: `messages_for_client()` 的 tool item 含 `message_id`/`timestamp`——Task 2 契约收紧依赖。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_server.py` 的 session replay 测试区（`I1'` 用例后）加：

```python
    def test_tool_replay_keeps_message_id_and_timestamp(self):
        """tool replay 项输出消息自身 message_id/timestamp（spec §5.3.1/5.3.2）。"""
        from core.schema.message import ToolResultMessage
        from core.schema.part import TextPart
        from core.schema.types import ToolCallId, MessageId

        s = SessionMemory()
        msg = ToolResultMessage(
            content=[TextPart(content="tool result")],
            tool_call_id=ToolCallId("call_tool_1"),
            tool_name="get_time",
            success=True,
            duration_ms=5.0,
            message_id=MessageId("msg_0123456789abcdef0123456789"),
        )
        s.add_message(msg)
        client_msgs = s.messages_for_client()
        tool_item = next(m for m in client_msgs if m["role"] == "tool")
        assert tool_item["message_id"] == "msg_0123456789abcdef0123456789"
        # 断言与消息对象值一致（非新生成随机 ID/时间，spec §5.1 要求）
        assert tool_item["timestamp"] == msg.timestamp
        # 经 SessionMessage.wire_dict() 往返身份不丢（spec §5.3.2）
        frame = SessionMessage(messages=client_msgs, token_stats=None,
                               conversationId="conv_tool").wire_dict()
        tool_wire = next(m for m in frame["messages"] if m["role"] == "tool")
        assert tool_wire["message_id"] == "msg_0123456789abcdef0123456789"
        assert tool_wire["timestamp"] == msg.timestamp
```

（复用测试类内既有 import：`SessionMemory`/`SessionMessage`/`TextPart` 等已存在；补 `ToolResultMessage`/`ToolCallId`/`MessageId` 按既有风格。）

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q --tb=short`
Expected: 新用例 FAIL（tool item 无 `message_id` 键 → KeyError）。

- [ ] **Step 3: 实现**

`core/memory/session.py` `messages_for_client` 的 `ToolResultMessage` 分支（tool_entry dict）追加：

```python
                tool_entry = cast(ClientToolMessage, {
                    "role": "tool",
                    "message_id": msg.message_id,
                    "timestamp": msg.timestamp,
                    "tool_call_id": msg.tool_call_id,
                    ...
                })
```

`core/schema/message.py` `ClientToolMessage` 加必填字段（**不继承 `_ClientMessageBase`**——基类 `parts: list[dict[str, str]]` 与 `content: list[dict]` 与 tool 的 `content: str` 形状冲突）：

```python
class ClientToolMessage(TypedDict):
    """工具执行结果客户端格式。"""
    role: Literal[MessageRole.TOOL]
    message_id: str
    timestamp: str
    tool_call_id: str
    name: str
    content: str
    success: bool
    error: str | None
    error_type: str | None
    duration_ms: float
    truncated: bool
```

（注：`session.py` tool 分支用 `cast(ClientToolMessage, {...})` 构造——`cast` 对 dict 字面量不做结构校验，pyright 不会因必填字段而强制构造点带两键。必填字段的价值是让 `ClientToolMessage` 类型的**下游消费方**（及任何未来非 cast 构造）受类型约束；运行时强制由 Task 2 的 `SessionMessageItem` 契约收紧兜底。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q --tb=short`
Expected: PASS（新用例 + 既有全绿）。若 `test_phase2.py`/`test_auth.py` 的 `messages_for_client` 断言因 tool item 新增键受影响，一并核查（新增键不破坏既有 `==` 全量断言的话无需改）。

- [ ] **Step 5: 提交**

```bash
git add core/memory/session.py core/schema/message.py core/input/tests/test_server.py
git commit -m "fix(session): WS tool replay 输出消息身份 message_id/timestamp（spec D1/D2）"
```

---

### Task 2: D3+D4 — `SessionMessageItem` 契约收紧 + 重生成

**Files:**
- Modify: `frontend_contract/ws_message.py`（`SessionMessageItem.message_id`/`timestamp`）
- Modify: `core/input/tests/test_server.py`（`test_build_session_items_drops_invalid_keeps_valid` fixture 补身份）
- Modify: `frontend_contract/tests/test_ws_message.py`（`test_wire_dict_list_recursive` 构造补身份——review 发现）
- Regenerate: `frontend/src/types/contract.generated.ts`、`frontend_contract/contract.schema.json`、`frontend_contract/contract.raw.ts`

**Interfaces:**
- Consumes: Task 1（tool 项现带身份）。`_build_session_items` 输入恒来自 `messages_for_client()`。
- Produces: `SessionMessageItem` 对缺身份项 fail-closed 拒绝。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_server.py` 的 `test_build_session_items_drops_invalid_keeps_valid`：给 tool item fixture 补身份（否则新契约后它会被拒，破坏「keeps valid」断言）：

```python
            {"role": "tool", "tool_call_id": "c1", "name": "web", "content": "ok",
             "message_id": "t1", "timestamp": "t3",
             "success": True, "duration_ms": 1.0, "truncated": False},
```

加新用例：

```python
    def test_build_session_items_rejects_tool_without_identity(self):
        """缺 message_id/timestamp 的持久消息按新契约 fail-closed 拒绝（spec §5.3.3）。"""
        from core.input.ws_handler import _build_session_items

        msgs = [
            {"role": "tool", "tool_call_id": "c1", "name": "web", "content": "ok",
             "success": True, "duration_ms": 1.0, "truncated": False},
        ]
        items = _build_session_items(msgs)
        assert len(items) == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py -q --tb=short`
Expected: 新用例 FAIL（当前 Optional 放行缺身份 tool 项）；fixture 用例在补身份前 tool 项被拒 → 先补 fixture 再确认。

- [ ] **Step 3: 实现 + 修既有构造点 + 重新生成契约**

`frontend_contract/ws_message.py` `SessionMessageItem`：

```python
    message_id: str = Field(min_length=1, description="消息 ID（前端去重/归档去重，必填非空）")
    timestamp: str = Field(min_length=1, description="ISO 时间戳，必填非空")
```

`frontend_contract/tests/test_ws_message.py` `test_wire_dict_list_recursive`（约 52 行）构造
`SessionMessageItem(role="assistant", content="hi")` 补身份——`message_id="m1", timestamp="t1"`。

运行：
```bash
conda run -n Elysia python -m frontend_contract   # 生成 ts/schema/raw_ts
conda run -n Elysia python -m frontend_contract --check
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py frontend_contract/tests/ -q --tb=short` 与 `conda run -n Elysia python -m frontend_contract --check`
Expected: PASS + 漂移门禁通过。

- [ ] **Step 5: 提交**

```bash
git add frontend_contract/ws_message.py frontend_contract/tests/test_ws_message.py frontend/src/types/contract.generated.ts frontend_contract/contract.schema.json frontend_contract/contract.raw.ts core/input/tests/test_server.py
git commit -m "feat(contract): SessionMessageItem 身份必填非空 + 三产物重生成（spec D3/D4）"
```

---

### Task 3: 门禁全绿 + 真实数据 sanity + 文档收口

**Files:**
- 门禁：`make test-quick`、`ruff`、`pyright`、`frontend_contract --check`、`scripts/check_error_codes.py`、`git diff --check`
- 文档：`docs/review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md`、`docs/progress/archive/2026-08/2026-08-13-int2-replay-identity-fix-progress.md`、`todo.md` 状态、`docs/README.md` 导航

- [ ] **Step 1: 全量门禁**

```bash
conda run -n Elysia make test-quick
conda run -n Elysia ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__
conda run -n Elysia pyright
conda run -n Elysia python -m frontend_contract --check
conda run -n Elysia python scripts/check_error_codes.py
git diff --check
```
Expected: 全绿。

- [ ] **Step 2: 真实数据 sanity**

复现会话 `conv_036c872c7c55` 的 tool 消息：确认 REST（`_archive_msg_to_contract`）与 WS（`messages_for_client`）读取的 `message_id`/`timestamp`/`tool_call_id` 一致（脚本或单测断言）。

- [ ] **Step 3: 文档收口**

写 `docs/review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md`（D1-D4 决策 + 证据 + 门禁）；`docs/progress/` 快照；`todo.md` 批次 note；`docs/README.md` 注册新文档。

- [ ] **Step 4: 提交**

```bash
git add docs/ todo.md
git commit -m "docs: 二次联调 WS replay 身份修复批次状态 + 报告 + todo（BE-INT2-P0-01/P1-01）"
```

---

## 执行交接

Plan 已保存。两种执行方式：
1. **Subagent-Driven（推荐）**：每任务派发 implementer + reviewer。
2. **Inline Execution**：本会话批量执行 + checkpoint。

请选择。
