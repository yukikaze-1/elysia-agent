# core ID 体系 NewType 化 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 core 内部 ID 类型全面对齐 `typing.NewType`——`core/schema/types.py` 新增 `GenerationId`/`SegmentId`/`RequestId`，
现有 `MessageId`/`SessionId`/`ToolCallId` 补齐到散落裸 `str` 处（GenerationContext/registry/repo/CognitiveLoop 参数/
UserInputData/_PendingMessage/VersionEntry）；input 边界保留裸 `str`，在收口点显式包一层。**运行时零变化**——纯静态类型标注。

**Architecture:** 单层改动。schema 叶子模块新增 3 NewType + 导出；ID 生成函数返回类型改为 NewType；
GenerationContext/registry/repo/CognitiveLoop/散落 schema 签名对齐；`Agent.process_message` 与 ws 构造 `UserInputData`
处作为收口点把 wire 裸 `str` 包成 NewType。storage 层对齐是修复 BE-024 新批次未沿用 session_repo/message_repo 既有惯例的疏漏。

**Tech Stack:** Python 3.12 `typing.NewType`（纯静态，运行时即 str）。测试 pytest（marker=unit/service），
conda 环境 `Elysia`（`/home/yomu/miniconda3/envs/Elysia/bin/python`）。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入：`from core.schema.types import GenerationId`。
- 代码注释/README 用中文；中英混合（中文嵌英文技术术语）可接受。
- **NewType 运行时即 str**：不做 `.value` 拆包，NewType 直接传给 frontend_contract 裸 `str` 参数
  （NewType 是 str 子类型，静态类型安全）。项目无 mypy/pyright——NewType 是 IDE 级文档标注，**不改变任何运行时行为**。
- **`isinstance(x, NewType(...))` 在 Python 3.12 抛 `TypeError: isinstance() arg 2 must be a type`（实测）**——
  测试断言 NewType 类型必须用 `typing.get_type_hints()` 查注解（`is` 单类型 / `==` 联合类型），禁止 isinstance。
- `from __future__ import annotations` 的文件（generation.py/_types.py 等）注解是字符串，
  `get_type_hints()` 自动解析，无需改文件头。
- spec: `docs/superpowers/specs/2026-08-08-core-id-newtype-design.md`（设计依据，触及点/收口点全表）。

---

## File Structure

- **Modify:** `core/schema/types.py` — 加 `GenerationId`/`SegmentId`/`RequestId`（~10 行）
- **Modify:** `core/schema/__init__.py` — 导出补 3 个（1 行）
- **Modify:** `core/utils/message_id.py` — `new_generation_id`/`new_segment_id` 返回类型
- **Modify:** `core/agent/server.py` — `next_request_id` 返回 `RequestId`
- **Modify:** `core/cognitive/generation.py` — `GenerationContext` 6 字段类型 + `open_segment` 返回
- **Modify:** `core/cognitive/generation_registry.py` — register/mark/get/query_by_session/find_by_message 签名
- **Modify:** `core/storage/generation_repo.py` — find_one/find_by_message/find_by_session 签名
- **Modify:** `core/cognitive/loop.py` — process/_build_turn_prompt/_think_loop 参数 + 收口点 B
- **Modify:** `core/schema/event.py` — `UserInputData.message_id`/`request_id`
- **Modify:** `core/agent/_types.py` — `_PendingMessage` 3 字段
- **Modify:** `core/schema/compression.py` — `VersionEntry.session_id`
- **Modify:** `core/agent/agent.py` — process_message 收口点 A
- **Modify:** `core/input/ws_handler.py` — UserInputData 构造收口点 C/E
- **Modify:** `core/input/voice_sessions.py` — `_build_user_input` 收口点 D
- **Modify:** `core/input/session_routes.py` — registry 调用收口点 F
- **Test:** `core/schema/tests/test_types.py`、`core/cognitive/tests/test_generation.py`、
  `core/cognitive/tests/test_v2_empty_response.py`、`core/cognitive/tests/test_generation_registry.py`、
  `core/cognitive/tests/test_session_resolver.py`、`core/schema/tests/test_event.py`、
  `core/agent/tests/test_v2_message_id.py`、`core/input/tests/test_request_id.py`、`core/tests/test_phase1.py`

---

### Task 1: schema 层新增 GenerationId/SegmentId/RequestId

**Files:**
- Modify: `core/schema/types.py`
- Modify: `core/schema/__init__.py`
- Test: `core/schema/tests/test_types.py`

**Interfaces:**
- Produces: `GenerationId`/`SegmentId`/`RequestId`（NewType）。Task 2-6 依赖。

- [ ] **Step 1: 写失败测试**

在 `core/schema/tests/test_types.py` 的 `TestNewTypes` 类末尾加（顶部 import 加 3 个——`from core.schema import ... GenerationId, SegmentId, RequestId`）：

```python
    def test_generation_id_is_str(self):
        gid = GenerationId("gen_1")
        assert isinstance(gid, str)
        assert gid == "gen_1"

    def test_segment_id_is_str(self):
        sid = SegmentId("seg_1")
        assert isinstance(sid, str)
        assert sid == "seg_1"

    def test_request_id_is_str(self):
        rid = RequestId("001")
        assert isinstance(rid, str)
        assert rid == "001"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/schema/tests/test_types.py -q --tb=short`
Expected: FAIL — `ImportError: cannot import name 'GenerationId'`（core.schema 尚未导出）。

- [ ] **Step 3: 最小实现**

`core/schema/types.py` 末尾加（在 ToolCallId 定义后）：

```python
GenerationId = NewType("GenerationId", str)
"""生成实例唯一标识符（gen_ 前缀）。"""

SegmentId = NewType("SegmentId", str)
"""生成分段唯一标识符（seg_ 前缀）。"""

RequestId = NewType("RequestId", str)
"""请求唯一标识符（`{_req_counter:03d}` 三位计数器格式，非 UUID——与 server.py:132 实现一致）。"""
```

`core/schema/__init__.py:17-22` 的 `from core.schema.types import (...)` 括号内加 3 个（**严格字母序**，G<I<M<R<Se<Ses<T）：

```python
from core.schema.types import (  # noqa: F401
    GenerationId,
    IsoTimestamp,
    MessageId,
    RequestId,
    SegmentId,
    SessionId,
    ToolCallId,
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同上。Expected: PASS（含既有 TestNewTypes 三件套）。

- [ ] **Step 5: 提交**

```bash
git add core/schema/types.py core/schema/__init__.py core/schema/tests/test_types.py
git commit -m "feat(schema): 新增 GenerationId/SegmentId/RequestId NewType 并导出"
```

### Task 2: ID 生成器返回类型

**Files:**
- Modify: `core/utils/message_id.py`
- Modify: `core/agent/server.py`
- Test: `core/cognitive/tests/test_generation.py`（补返回类型断言）
- Test: `core/input/tests/test_request_id.py`（补 next_request_id 返回类型断言 + Step 3 后无适配）

**Interfaces:**
- Consumes: `GenerationId`/`SegmentId`/`RequestId`（Task 1）。
- Produces: `new_generation_id() -> GenerationId`、`new_segment_id() -> SegmentId`、`next_request_id() -> RequestId`。
  Task 3（GenerationContext 字段接收）、Task 7（ws/voice 构造点不再需包 request_id/message_id）依赖。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_generation.py` 顶部 import 加（`from typing import get_type_hints`、`from core.schema import GenerationId, SegmentId`）。
在 `test_new_generation_id_format`（:10-14）与 `test_new_segment_id_format`（:17-22）内补返回类型断言：

```python
    gid = new_generation_id()
    assert get_type_hints(new_generation_id)["return"] is GenerationId
    assert gid.startswith("gen_")
    assert len(gid) == len("gen_") + 26
```
```python
    sid = new_segment_id()
    assert get_type_hints(new_segment_id)["return"] is SegmentId
    assert sid.startswith("seg_")
    assert len(sid) == len("seg_") + 26
```

`core/input/tests/test_request_id.py` 末尾加一个 unit 测试（import 加 `from typing import get_type_hints`、`from core.schema import RequestId`）：

```python
@pytest.mark.unit
def test_next_request_id_returns_request_id():
    """next_request_id 返回 RequestId（NewType）。"""
    from core.agent.server import AgentServer
    hints = get_type_hints(AgentServer.next_request_id)
    assert hints["return"] is RequestId
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py core/input/tests/test_request_id.py -q --tb=short`
Expected: FAIL — `get_type_hints(new_generation_id)["return"]` 为裸 `str`（`is GenerationId` 失败）；`AgentServer.next_request_id` 返回注解为 `str`。

- [ ] **Step 3: 最小实现**

`core/utils/message_id.py`（import 改 `from core.schema.types import GenerationId, MessageId, SegmentId`）：

```python
def new_generation_id() -> GenerationId:
    """生成一次生成实例 ID（'gen_' 前缀，与 messageId 语义区分，批次 2）。"""
    return GenerationId(_new_id("gen"))

def new_segment_id() -> SegmentId:
    """生成一个分段 ID（'seg_' 前缀，批次 2）。"""
    return SegmentId(_new_id("seg"))
```

`core/agent/server.py`（import 加 `from core.schema.types import RequestId`；注意文件头有 `from __future__ import annotations`，`RequestId` 注解是字符串无碍）：

```python
    def next_request_id(self) -> RequestId:
        """生成并返回请求 ID，同时设置 ContextVar。供外部模块（STT 等）使用。

        Returns:
            格式为 ``{req_counter:03d}`` 的请求 ID 字符串（RequestId NewType）。
        """
        self._req_counter += 1
        rid = f"{self._req_counter:03d}"
        set_request_id(rid)
        return RequestId(rid)
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同上。Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/utils/message_id.py core/agent/server.py core/cognitive/tests/test_generation.py core/input/tests/test_request_id.py
git commit -m "feat(id): new_generation_id/new_segment_id/next_request_id 返回类型对齐 NewType"
```

### Task 3: GenerationContext 字段对齐

**Files:**
- Modify: `core/cognitive/generation.py`
- Test: `core/cognitive/tests/test_generation.py`（构造适配）
- Test: `core/cognitive/tests/test_v2_empty_response.py`（构造适配）

**Interfaces:**
- Consumes: `GenerationId`/`SegmentId`/`RequestId`（Task 1）、生成器返回类型（Task 2）。
- Produces: `GenerationContext.generation_id: GenerationId`、`user_message_id: MessageId`、
  `assistant_message_id: MessageId`、`conversation_id: SessionId | None`、`request_id: RequestId | None`、
  `current_segment_id: SegmentId | None`、`open_segment() -> SegmentId`。Task 4/5 依赖。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_generation.py` 末尾加（顶部 import 加 `from typing import get_type_hints`、
`from core.schema import GenerationId, MessageId, SegmentId, SessionId, RequestId`）：

```python
@pytest.mark.unit
def test_generation_context_id_field_types():
    """GenerationContext 的 ID 字段全部对齐 NewType（get_type_hints 查注解）。"""
    hints = get_type_hints(GenerationContext)
    assert hints["generation_id"] is GenerationId
    assert hints["user_message_id"] is MessageId
    assert hints["assistant_message_id"] is MessageId
    assert hints["conversation_id"] == SessionId | None
    assert hints["request_id"] == RequestId | None
    assert hints["current_segment_id"] == SegmentId | None
    assert hints["model_id"] is str  # 模型名非 ID 语义，保持 str
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py -q --tb=short`
Expected: FAIL — `hints["generation_id"]` 为裸 `str`（`is GenerationId` 失败）。

- [ ] **Step 3: 最小实现**

`core/cognitive/generation.py`（import 改 `from core.schema.types import GenerationId, MessageId, RequestId, SegmentId, SessionId`）：
`GenerationContext` 字段类型改：

```python
    generation_id: GenerationId
    user_message_id: MessageId
    assistant_message_id: MessageId
    conversation_id: SessionId | None = None
    model_id: str = ""
    request_id: RequestId | None = None
```
```python
    current_segment_id: SegmentId | None = None
```

`open_segment` 签名改 `def open_segment(self, channel: str, kind: str = "text") -> SegmentId:`。
（`new_segment_id()` Task 2 已返回 SegmentId，`self.current_segment_id = new_segment_id()` 自动一致。）

**同时适配既有测试构造**（同文件 :28-30,48-50 与 `test_v2_empty_response.py:18-20,67-69` 裸 str → NewType）：

```python
    ctx = GenerationContext(
        generation_id=GenerationId("gen_1"), user_message_id=MessageId("msg_u1"),
        assistant_message_id=MessageId("msg_a1"),
    )
```
（test_v2_empty_response.py 的 `conversation_id="default"` → `SessionId("default")`，import 加 `from core.schema import GenerationId, MessageId, SessionId`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py core/cognitive/tests/test_v2_empty_response.py -q --tb=short`
Expected: PASS（新增字段类型断言 + 适配后既有全绿——运行时 NewType 即 str，裸 str 传参本就通过，适配纯类型卫生）。

- [ ] **Step 5: 提交**

```bash
git add core/cognitive/generation.py core/cognitive/tests/test_generation.py core/cognitive/tests/test_v2_empty_response.py
git commit -m "refactor(cognitive): GenerationContext ID 字段对齐 NewType + open_segment 返回 SegmentId"
```

### Task 4: GenerationRegistry + GenerationRepository 签名对齐

**Files:**
- Modify: `core/cognitive/generation_registry.py`
- Modify: `core/storage/generation_repo.py`
- Test: `core/cognitive/tests/test_generation_registry.py`（构造适配）
- Test: `core/input/tests/test_request_id.py`（register 调用适配）

**Interfaces:**
- Consumes: `GenerationId`/`MessageId`/`SessionId`/`RequestId`（Task 1）。
- Produces: `register(generation_id: GenerationId, *, session_id: SessionId, user_message_id: MessageId,
  message_id: MessageId, model: str, request_id: RequestId | None)`；`mark/get(generation_id: GenerationId)`；
  `query_by_session(session_id: SessionId)`；`find_by_message(message_id: MessageId)`（registry + repo 同签名）。
  Task 5（loop 调用）、Task 7（session_routes 收口 F）依赖。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_generation_registry.py` 末尾加（顶部 import 加 `from typing import get_type_hints`、
`from core.schema import GenerationId, MessageId, SessionId, RequestId`）：

```python
@pytest.mark.unit
class TestSignatureTypes:
    """registry/repo 签名对齐 NewType（get_type_hints 查注解）。"""

    def test_register_signature(self):
        hints = get_type_hints(GenerationRegistry.register)
        assert hints["generation_id"] is GenerationId
        assert hints["session_id"] is SessionId
        assert hints["user_message_id"] is MessageId
        assert hints["message_id"] is MessageId
        assert hints["request_id"] == RequestId | None

    def test_repo_signatures(self):
        hints = get_type_hints(GenerationRepository.find_by_session)
        assert hints["session_id"] is SessionId
        hints2 = get_type_hints(GenerationRepository.find_by_message)
        assert hints2["message_id"] is MessageId
        hints3 = get_type_hints(GenerationRepository.find_one)
        assert hints3["generation_id"] is GenerationId
```

（`GenerationRepository`/`GenerationRegistry` 顶部已有 import。）

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation_registry.py -q --tb=short`
Expected: FAIL — registry/repo 方法签名仍裸 `str`。

- [ ] **Step 3: 最小实现**

`core/cognitive/generation_registry.py`（import 加 `from core.schema.types import GenerationId, MessageId, RequestId, SessionId`）：

```python
    async def register(self, generation_id: GenerationId, *, session_id: SessionId,
                       user_message_id: MessageId, message_id: MessageId, model: str,
                       request_id: RequestId | None = None) -> None:
    async def mark(self, generation_id: GenerationId, *, status: str, ...) -> None:
    async def get(self, generation_id: GenerationId) -> dict | None:
    async def query_by_session(self, session_id: SessionId, ...) -> ...:
    async def find_by_message(self, message_id: MessageId) -> dict | None:
```

`core/storage/generation_repo.py`（import 加 `from core.schema.types import GenerationId, MessageId, SessionId`）：

```python
    async def find_one(self, generation_id: GenerationId) -> dict | None:
    async def find_by_message(self, message_id: MessageId) -> dict | None:
    async def find_by_session(self, session_id: SessionId, *, status: str | None = None, ...) -> ...:
```

`_encode_cursor(created_at: str, generation_id: str)` **保持 str**（cursor 是 base64 编码，非 ID 语义——spec 明示）。

**同时适配既有测试**：`test_generation_registry.py:35-36,50-51,69-71,81-83,110` 的
`reg.register("gen_1", session_id="s1", user_message_id="msg_u1", message_id="msg_a1", model="mock")`
→ `reg.register(GenerationId("gen_1"), session_id=SessionId("s1"), user_message_id=MessageId("msg_u1"),
message_id=MessageId("msg_a1"), model="mock")`（import 加 4 个类型）；`query_by_session("s1")` → `SessionId("s1")`、
`find_by_message("msg_a2")` → `MessageId("msg_a2")`。
**mark/get 调用同文件全部适配**：`:37,52,53,61,62,72,104,112,122,123` 的 `mark("gen_1", ...)`/`get("gen_1")`
→ `mark(GenerationId("gen_1"), ...)`/`get(GenerationId("gen_1"))`——mark/get 签名已改 GenerationId，裸 str 虽运行时通过但
按类型卫生统一包层。`test_request_id.py` 若调 register 传裸 str，同样适配（`GenerationId("...")`/`SessionId("...")`/
`MessageId("...")`，import 加对应类型）。

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation_registry.py core/input/tests/test_request_id.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/cognitive/generation_registry.py core/storage/generation_repo.py core/cognitive/tests/test_generation_registry.py core/input/tests/test_request_id.py
git commit -m "refactor(generation): registry/repo 签名对齐 NewType——对齐 session_repo/message_repo 既有惯例"
```

### Task 5: CognitiveLoop 参数对齐 + 收口点 B

**Files:**
- Modify: `core/cognitive/loop.py`（process/_build_turn_prompt/_think_loop 参数 + 收口点 B）
- Test: `core/cognitive/tests/test_session_resolver.py`（适配）
- Test: `core/cognitive/tests/test_v2_generation.py`、`test_generation_wiring.py`（若直构 GenerationContext 则适配）

**Interfaces:**
- Consumes: `MessageId`/`SessionId`/`RequestId`（Task 1）、GenerationContext（Task 3）、registry 签名（Task 4）。
- Produces: `process(message_id: MessageId | None = None, conversation_id: SessionId | None = None)` 等参数对齐；
  收口点 B：`user_message_id=MessageId(message_id or "")`（:472）、`request_id=RequestId(rid)`（:476）。
  Task 7（Agent.process_message 收口 A 传 NewType）依赖。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_session_resolver.py` 末尾加（顶部 import 加 `from typing import get_type_hints`、
`from core.schema import MessageId, SessionId`）：

```python
@pytest.mark.unit
def test_process_id_param_types():
    """loop.process 的 message_id/conversation_id 对齐 NewType。"""
    hints = get_type_hints(CognitiveLoop.process)
    assert hints["message_id"] == MessageId | None
    assert hints["conversation_id"] == SessionId | None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_session_resolver.py -q --tb=short`
Expected: FAIL — 参数仍裸 `str | None`。

- [ ] **Step 3: 最小实现**

`core/cognitive/loop.py`（import 已含 `MessageId`/`SessionId`——`from core.schema.types import SessionId` 在 :26、
`from core.schema.tool import ToolCall, ToolCallId` 在 :66；`MessageId` 若未 import 则加）：

- `process`（:368-377）：`message_id: MessageId | None = None`、`conversation_id: SessionId | None = None`
- `_build_turn_prompt`（:564-565）：`user_message_id: MessageId | None = None`、`conversation_id: SessionId | None = None`
- `_think_loop` 及内部转发 `message_id`/`conversation_id` 的 helper 签名（:725,894,1063-1064）同步对齐
- **收口点 B**（GenerationContext 构造 :470-478）：
  ```python
  self._generation_ctx = GenerationContext(
      generation_id=new_generation_id(),
      user_message_id=MessageId(message_id or ""),      # :472 — 原 `message_id or ""` 产裸 str 空串
      assistant_message_id=new_message_id(),
      conversation_id=gen_conversation_id,              # gen_conversation_id = conversation_id or target.session_id，已 SessionId
      model_id=self._llm.model,
      request_id=RequestId(rid),                        # :476 — rid 来自 get_request_id()（str）或 P 兜底
      protocol_version=protocol_version,
  )
  ```
  import 加 `RequestId`（`from core.schema.types import MessageId, RequestId, SessionId`——按现有 import 位置插）。
  `rid = get_request_id()` / `P{_req_counter:03d}` 兜底逻辑（:425-430）不动，`get_request_id`/`set_request_id` 签名不改。

**同时适配既有测试**：`test_session_resolver.py:91,142,144` 的 `loop.process(conversation_id="conv_x")`
→ `conversation_id=SessionId("conv_x")`（import 加 `from core.schema import SessionId`）。
`test_v2_generation.py`/`test_generation_wiring.py` 若直构 GenerationContext 传裸 str，按 Task 3 模式适配。

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_session_resolver.py core/cognitive/tests/test_v2_generation.py core/cognitive/tests/test_generation_wiring.py core/cognitive/tests/test_v2_empty_response.py -q --tb=short`
Expected: PASS（含 `test_v2_empty_response.py`——本任务改了 `_finalize_turn`（loop.py:894）签名，该文件直接调
`_finalize_turn(conversation_id="default")`（:38,56,77），须在改签名任务内验证）。

- [ ] **Step 5: 提交**

```bash
git add core/cognitive/loop.py core/cognitive/tests/test_session_resolver.py core/cognitive/tests/test_v2_generation.py core/cognitive/tests/test_generation_wiring.py
git commit -m "refactor(cognitive): loop.process 参数对齐 NewType + GenerationContext 构造收口点 B"
```

### Task 6: schema/agent 散落对齐（UserInputData/_PendingMessage/VersionEntry）

**Files:**
- Modify: `core/schema/event.py`、`core/agent/_types.py`、`core/schema/compression.py`
- Test: `core/schema/tests/test_event.py`、`core/agent/tests/test_v2_message_id.py`、`core/tests/test_phase1.py`（构造适配）

**Interfaces:**
- Consumes: `MessageId`/`SessionId`/`RequestId`（Task 1）。
- Produces: `UserInputData.message_id: MessageId | None`、`request_id: RequestId`；
  `_PendingMessage.message_id: MessageId | None`、`conversation_id: SessionId | None`、`request_id: RequestId`；
  `VersionEntry.session_id: SessionId`。Task 7（收口点 A/C/D/E）依赖——构造点需包 NewType。

- [ ] **Step 1: 写失败测试**

`core/schema/tests/test_event.py` 末尾加（顶部 import 加 `from typing import get_type_hints`、
`from core.schema import MessageId, RequestId, SessionId`——SessionId 供 Step 3 适配 `conversation_id="sess-9"`）：

```python
@pytest.mark.unit
def test_user_input_data_id_field_types():
    """UserInputData 的 message_id/request_id 对齐 NewType（conversation_id 已 SessionId）。"""
    hints = get_type_hints(UserInputData)
    assert hints["message_id"] == MessageId | None
    assert hints["request_id"] is RequestId
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/schema/tests/test_event.py -q --tb=short`
Expected: FAIL — message_id 仍 `str | None`、request_id 仍 `str`。

- [ ] **Step 3: 最小实现**

`core/schema/event.py`（import 改 `from core.schema.types import MessageId, RequestId, SessionId`）：

```python
    request_id: RequestId
    ...
    message_id: MessageId | None = None
```

`core/agent/_types.py`（import 加 `from core.schema.types import MessageId, RequestId, SessionId`；文件头有 `from __future__ import annotations`）：

```python
    request_id: RequestId
    message_id: MessageId | None = None
    conversation_id: SessionId | None = None
```

`core/schema/compression.py`（import 加 `from core.schema.types import SessionId`）：

```python
class VersionEntry(TypedDict):
    session_id: SessionId
```

**同时适配既有测试**：`test_event.py:69,74,81,87` 的 `request_id="r1"` → `RequestId("r1")`、
`conversation_id="sess-9"`（**:87**，:86 是 `request_id="r1"`）→ `SessionId("sess-9")`（import 3 个：MessageId/RequestId/SessionId）。
`test_v2_message_id.py:14-16,25-26` 的 UserInputData/`_PendingMessage` 构造裸 str `message_id="msg_x"` → `MessageId("msg_x")`
（import 加 `from core.schema import MessageId`）；`test_phase1.py` 若构造 UserInputData 传裸 str 同理适配（`MessageId(...)`/`RequestId(...)`，
import 加对应类型）。

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/schema/tests/test_event.py core/agent/tests/test_v2_message_id.py core/tests/test_phase1.py -q --tb=short`
Expected: PASS（注意：agent.py/ws_handler/voice_sessions 的 UserInputData 构造点此时仍是裸 str 传 NewType 参数——
**运行时通过**（NewType 即 str），Task 7 统一收口）。

- [ ] **Step 5: 提交**

```bash
git add core/schema/event.py core/agent/_types.py core/schema/compression.py core/schema/tests/test_event.py core/agent/tests/test_v2_message_id.py core/tests/test_phase1.py
git commit -m "refactor(schema): UserInputData/_PendingMessage/VersionEntry ID 字段对齐 NewType"
```

### Task 7: input 收口点 A/C/D/E/F

**Files:**
- Modify: `core/agent/agent.py`（收口点 A）
- Modify: `core/input/ws_handler.py`（收口点 C/E）
- Modify: `core/input/voice_sessions.py`（收口点 D）
- Modify: `core/input/session_routes.py`（收口点 F）
- Test: 无新增——运行既有 input/agent 测试确认不回归（本任务收口是 str→NewType，运行时透明）。

**Interfaces:**
- Consumes: Task 6 的 `UserInputData`/`_PendingMessage` 字段类型、Task 4 的 registry 签名。
- Produces: 收口点 A（`Agent.process_message` 保持裸 str 签名、内部包 NewType）、C/D/E（UserInputData 构造点）、
  F（session_routes 调 registry 处）。input 边界对 wire 的签名全部保持裸 `str`。

- [ ] **Step 1: 收口点 A — `Agent.process_message`**

`core/agent/agent.py:309-313` 签名**保持** `message_id: str | None`、`conversation_id: str | None`（公开 wire 入口）。
函数开头（:332 前）加：

```python
        message_id_nt = MessageId(message_id) if message_id else None
        conversation_id_nt = SessionId(conversation_id) if conversation_id else None
```

后续 `_PendingMessage(...)`（:348-353）与 `UserInputData(...)`（:356-360）用 `message_id_nt`/`conversation_id_nt`
替代原 `message_id`/`conversation_id`（`request_id=rid` 已是 RequestId）。
import 加 `from core.schema.types import MessageId, SessionId`（agent.py 顶部已有 `from core.schema.event import ...`）。

- [ ] **Step 2: 收口点 C/E — `ws_handler`**

- `:735-738` 构造 UserInputData：`message_id=message_id`（`new_message_id()` 已 MessageId）与
  `request_id=agent.next_request_id()`（已 RequestId）**不改**；仅
  `conversation_id=conversation_id` → `conversation_id=SessionId(conversation_id) if conversation_id else None`。
- `:1169-1173`：仅 `request_id=rid`（已 RequestId）——**不改**。
- import 加 `from core.schema.types import SessionId`。

- [ ] **Step 3: 收口点 D — `voice_sessions`**

`:513-519` `_build_user_input`：`message_id=message_id`（`new_message_id()` 已 MessageId）、
`request_id=self._agent.next_request_id()`（已 RequestId）**不改**；仅
`conversation_id=self._conv_id` → `conversation_id=SessionId(self._conv_id) if self._conv_id else None`。
import 加 `from core.schema.types import SessionId`。

- [ ] **Step 4: 收口点 F — `session_routes`**

- `:643` `gen = await registry.find_by_message(MessageId(str(message_id)))`
- `:730` `docs, has_more, next_cursor = await registry.query_by_session(SessionId(sid), status=status, cursor=cursor, limit=limit)`
- `:744` `doc = await registry.get(GenerationId(gid))`
- import 加 `from core.schema.types import GenerationId, MessageId, SessionId`。

- [ ] **Step 5: 运行测试确认不回归**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/input/tests core/agent/tests core/input/tests/test_session_routes.py core/input/tests/test_v2_ws.py core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py -q --tb=short`
Expected: PASS（收口 str→NewType 运行时透明，既有断言不破）。

- [ ] **Step 6: 提交**

```bash
git add core/agent/agent.py core/input/ws_handler.py core/input/voice_sessions.py core/input/session_routes.py
git commit -m "refactor(input): wire 收口点 A/C/D/E/F——裸 str 包成 NewType，input 边界签名保持 str"
```

### Task 8: 全量回归 + 门禁

**Files:** 无新增（验证任务）。

- [ ] **Step 1: 定向回归受影响模块**

```bash
/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive core/input/tests core/agent/tests core/schema/tests core/storage/tests core/tests -m "not slow" -q --tb=short
```
Expected: PASS。

- [ ] **Step 2: 全项目门禁**

```bash
make test-normal
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests
make lint
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m ruff check core/ Module/ frontend_contract/ --exclude __pycache__
```
Expected: 全绿。

- [ ] **Step 3: 提交（若无代码改动则跳过）**

```bash
git status
# 若有遗漏修复，git add + commit；否则无提交
```

## Self-Review（已执行）

1. **Spec 覆盖**：新增 3 NewType（T1）、生成器返回类型（T2）、GenerationContext（T3）、registry/repo（T4）、
   loop 参数 + 收口 B（T5）、散落对齐（T6）、收口 A/C/D/E/F（T7）——全部映射到 spec §1-7。
2. **占位符扫描**：无 TBD/TODO；每步含可执行代码与运行命令。
3. **类型一致性**：`GenerationId`/`SegmentId`/`RequestId` 定义在 T1，T2-7 引用一致；
   `get_type_hints` 断言写法统一（`is` 单类型 / `==` 联合类型），全程避免 `isinstance(x, NewType)`（Python 3.12 实测抛 TypeError）。
4. **已知耦合**：T3/T4/T5/T6 改注解后，未收口的调用点（agent.py/ws/voice/session_routes 构造）
   **运行时仍通过**（NewType 即 str），故各 Task 可独立绿；T7 统一收口保证静态一致。T5 的收口 B 与
   T7 的收口 A 是同一链路的首尾（process_message 包 → loop.process 收），不冲突。
