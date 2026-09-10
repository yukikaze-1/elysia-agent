# Electron v2 首次联调消息 ID 修复实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 BE-INT-P0-01/02 + P1-01——工具结果消息 `message_id` 恒非空、历史空 ID 稳定兼容并幂等回填、契约与门禁守住不变量。

**Architecture:** 读路径（archive/快照/JSONL）统一经 `validate_message_legacy_compat` 派生稳定 ID（D1）；模型级非空不变量（D2）；两处写入补 `new_message_id()`（D3）；归档边界守卫（D4）；契约 `min_length=1`（D5）；幂等回填脚本持久化（D6）。

**Tech Stack:** Python 3.12 / Pydantic / motor / aiohttp / pytest（marker: unit + service）。

## Global Constraints

- 契约名不变：`desktop.file.read` 等 wire 名、`message_id` 的 `msg_` 值空间永不改动。
- 核心不变量：任何经 archive / session 快照 / JSONL 读出的工具消息，`message_id` 恒非空、稳定、唯一，同一消息跨三源 ID 一致。
- D1 派生只使用文档自带 `tool_call_id` + `timestamp`；缺任一 → 不派生、按畸形文档处理（strict 校验失败 + warning），**绝不** `now_iso()` 兜底。
- 回填幂等：只处理空/缺失 ID；重复运行零变化；不清库、不丢消息、不覆盖已有非空 ID。
- 所有代码注释中文（docstring 同理）；生产代码参数类型注解（ruff ANN001/002/003 门禁）；禁相对导入。
- 测试 marker：单元/服务按 `docs/testing.md`；日常门禁 `make test-quick` / Ruff / Pyright / `python -m frontend_contract --check` / `scripts/check_error_codes.py`。
- conda env：`Elysia`。

---

### Task 1: D1 — `stable_tool_message_id` 纯函数

**Files:**
- Modify: `core/utils/message_id.py`（加 `import hashlib` + 新函数）
- Test: `core/utils/tests/test_message_id.py`（新建目录 + `__init__.py`）
- Create: `core/utils/tests/__init__.py`

**Interfaces:**
- Produces: `stable_tool_message_id(tool_call_id: str, timestamp: str) -> MessageId`——`"msg_" + sha256(f"{tool_call_id}:{timestamp}")[:26]`。Task 3（适配器）与 Task 7（回填脚本）消费。

- [ ] **Step 1: 写失败测试**

创建 `core/utils/tests/__init__.py`（空）与 `core/utils/tests/test_message_id.py`：

```python
"""stable_tool_message_id 派生测试（D1，marker=unit）。"""
import pytest

from core.schema.types import MessageId
from core.utils.message_id import new_message_id, stable_tool_message_id


pytestmark = pytest.mark.unit


def test_deterministic_same_input_same_id():
    assert stable_tool_message_id("call_x", "2026-08-13T00:00:00Z") \
        == stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")


def test_different_input_different_id():
    a = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    b = stable_tool_message_id("call_x", "2026-08-13T00:00:01Z")
    c = stable_tool_message_id("call_y", "2026-08-13T00:00:00Z")
    assert len({a, b, c}) == 3


def test_format_matches_msg_namespace():
    mid = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    assert str(mid).startswith("msg_")
    # msg_ + 26 位小写 hex（与 new_message_id 值空间一致）
    assert len(str(mid)) == 30
    assert str(mid)[4:].isalnum() and str(mid)[4:].islower()


def test_new_message_id_never_collides_with_derived():
    # 随机 26hex 与 sha256 摘要撞车概率可忽略；此测试为格式回归护栏
    mid = stable_tool_message_id("call_x", "2026-08-13T00:00:00Z")
    assert mid != new_message_id()
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/utils/tests/test_message_id.py -q --tb=short`
Expected: FAIL with `ImportError: cannot import name 'stable_tool_message_id'`.

- [ ] **Step 3: 最小实现**

在 `core/utils/message_id.py` 顶部加 `import hashlib`，文件末尾加：

```python
def stable_tool_message_id(tool_call_id: str, timestamp: str) -> MessageId:
    """历史工具消息的稳定 ID（spec D1）：msg_ + sha256(tool_call_id:timestamp)[:26]。

    存量空 message_id 工具消息的读侧派生与回填共用本函数：同一
    (tool_call_id, timestamp) 恒同一 ID，跨 archive / session 快照 / JSONL 一致；
    与 new_message_id() 同为 msg_ + 26hex 值空间，互不冲突。
    timestamp 参与哈希覆盖「同一 tool_call 未来一对多」场景。
    """
    digest = hashlib.sha256(f"{tool_call_id}:{timestamp}".encode("utf-8")).hexdigest()
    return MessageId("msg_" + digest[:26])
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/utils/tests/test_message_id.py -q --tb=short`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add core/utils/message_id.py core/utils/tests/
git commit -m "feat(message_id): stable_tool_message_id 派生函数（spec D1）"
```

---

### Task 2: D2a — `ToolResultMessage.message_id` 非空不变量 + 修既有构造点

**Files:**
- Modify: `core/schema/message.py`（import `field_validator`；`ToolResultMessage.message_id` 必填 + validator）
- Modify: 既有测试构造点——`core/schema/tests/test_message.py`（4 处缺 `message_id`，补 `message_id=MessageId("msg_test_...")`）、`core/adapter/tests/test_message_builder.py`（3 处显式 `MessageId("")`，改为非空 `MessageId("msg_test_...")`）
- Test: `core/schema/tests/test_message.py` 加拒绝用例
- Note: `core/schema/tests/test_message_degradation.py` 无 `ToolResultMessage(` 构造点（review 核实）；`core/input/tests/test_session_routes.py:1124` 已带 `message_id=MessageId("m_tool1")`；`core/cognitive/tests/test_tool_image_parts.py:91` 是 f-string 非构造——均无需改。

**Interfaces:**
- Consumes: Task 1 `new_message_id`（既有）。`ToolResultMessage` 构造签名变为 `message_id` 必填。
- Produces: 空 `message_id` 构造抛 `ValidationError`——Task 3 适配器依赖此 strict 兜底畸形文档；Task 4 写入路径依赖必填签名。

- [ ] **Step 1: 写失败测试**

在 `core/schema/tests/test_message.py` 加：

```python
from pydantic import ValidationError

def test_tool_result_message_rejects_empty_message_id():
    with pytest.raises(ValidationError):
        ToolResultMessage(
            content=[TextPart(content="ok")],
            tool_call_id=ToolCallId("call_1"),
            tool_name="get_time",
            message_id=MessageId(""),
        )

def test_tool_result_message_requires_message_id():
    with pytest.raises(ValidationError):
        ToolResultMessage(
            content=[TextPart(content="ok")],
            tool_call_id=ToolCallId("call_1"),
            tool_name="get_time",
        )

def test_tool_result_message_accepts_real_message_id():
    msg = ToolResultMessage(
        content=[TextPart(content="ok")],
        tool_call_id=ToolCallId("call_1"),
        tool_name="get_time",
        message_id=MessageId("msg_0123456789abcdef0123456789"),
    )
    assert str(msg.message_id).startswith("msg_")
```

（确认 `test_message.py` 顶部 import 有 `ValidationError` 需要的 `pytest`；没有则补。`TextPart`/`ToolCallId`/`MessageId` 按该文件现有 import 风格补充。）

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_message.py -q --tb=short`
Expected: 新用例 FAIL（当前空串合法通过）；且既有无 `message_id=` 的 `ToolResultMessage(...)` 用例开始 FAIL。

- [ ] **Step 3: 实现 strict + 修全部既有构造点**

`core/schema/message.py`：
- `from pydantic import BaseModel, Field` → `from pydantic import BaseModel, Field, field_validator`。
- `ToolResultMessage.message_id: MessageId = Field(default=MessageId(""))` → `message_id: MessageId`（必填，去默认）。
- 在 `ToolResultMessage` 类内加：

```python
    @field_validator("message_id")
    @classmethod
    def _message_id_non_empty(cls, v: MessageId) -> MessageId:
        """归档消息 ID 非空不变量（spec D2）：空串拒绝，防坏记录写入 Mongo。"""
        if v is None or not str(v).strip():
            raise ValueError("ToolResultMessage.message_id 不能为空")
        return v
```

用 `grep -rln "ToolResultMessage(" core/ Module/ --include="*.py" | grep -i test` 复核全部测试构造点，逐一补 `message_id=MessageId("msg_test_...")`（`test_message.py` 4 处缺参；`test_message_builder.py` 3 处把显式 `MessageId("")` 改为非空）。生产构造点（`loop.py`）留到 Task 4 统一改。

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/schema/tests/ core/adapter/tests/test_message_builder.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/schema/message.py core/schema/tests/test_message.py core/adapter/tests/test_message_builder.py
git commit -m "feat(schema): ToolResultMessage.message_id 非空不变量（spec D2）+ 修既有构造点"
```

---

### Task 3: D2b — `validate_message_legacy_compat` 读侧派生

**Files:**
- Modify: `core/schema/adapters/message_adapter.py`（import `stable_tool_message_id`；`validate_message_legacy_compat` 入口派生）
- Test: `core/schema/tests/test_adapters.py` 加派生用例；`core/schema/tests/test_message_degradation.py` 加畸形文档边界

**Interfaces:**
- Consumes: Task 1 `stable_tool_message_id`。
- Produces: `validate_message_legacy_compat(dict)` 对 `role="tool"` 且 `message_id` 空/缺失且 `tool_call_id`+`timestamp` 齐全的 dict 返回带稳定 `message_id` 的 `ToolResultMessage`——Task 8 的 REST/搜索/JSONL 测试依赖。

- [ ] **Step 1: 写失败测试**

`core/schema/tests/test_adapters.py`（顶部已有 `from core.schema.adapters.message_adapter import ...`）追加：

```python
from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.message import ToolResultMessage
from core.utils.message_id import stable_tool_message_id


class TestLegacyToolMessageIdDerivation:
    def test_empty_message_id_derives_stable_id(self):
        doc = {
            "role": "tool", "tool_call_id": "call_1",
            "timestamp": "2026-08-13T00:00:00Z", "message_id": "",
            "content": [{"type": "text", "content": "ok"}],
            "tool_name": "get_time",
        }
        msg = validate_message_legacy_compat(doc)
        assert isinstance(msg, ToolResultMessage)
        assert str(msg.message_id) == str(
            stable_tool_message_id("call_1", "2026-08-13T00:00:00Z"))

    def test_missing_message_id_derives_too(self):
        doc = {
            "role": "tool", "tool_call_id": "call_2",
            "timestamp": "2026-08-13T00:00:01Z",
            "content": [{"type": "text", "content": "ok"}],
            "tool_name": "web_search",
        }
        msg = validate_message_legacy_compat(doc)
        assert str(msg.message_id) == str(
            stable_tool_message_id("call_2", "2026-08-13T00:00:01Z"))

    def test_missing_timestamp_not_derived_raises(self):
        """缺 timestamp → 不派生、strict 拒绝（畸形文档，spec D2 边界）。"""
        doc = {
            "role": "tool", "tool_call_id": "call_3", "message_id": "",
            "content": [{"type": "text", "content": "ok"}],
            "tool_name": "get_time",
        }
        with pytest.raises(ValidationError):
            validate_message_legacy_compat(doc)

    def test_existing_non_empty_id_untouched(self):
        doc = {
            "role": "tool", "tool_call_id": "call_4",
            "timestamp": "2026-08-13T00:00:02Z", "message_id": "msg_0123456789abcdef0123456789",
            "content": [{"type": "text", "content": "ok"}],
            "tool_name": "get_time",
        }
        msg = validate_message_legacy_compat(doc)
        assert str(msg.message_id) == "msg_0123456789abcdef0123456789"
```

（`ValidationError` 从 `pydantic` import；`pytest` 已存在。）

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_adapters.py -q --tb=short`
Expected: 新用例 FAIL（`message_id` 空串通过 strict 前的校验 → 被 Task 2 validator 拒绝 → 抛 ValidationError；`test_empty_message_id_derives_stable_id` 断言失败）。

- [ ] **Step 3: 实现派生**

`core/schema/adapters/message_adapter.py`：
- 顶部 import 追加 `from core.utils.message_id import stable_tool_message_id`。
- `validate_message_legacy_compat` 开头加：

```python
    # D1/D2（spec）：存量工具消息 message_id 空/缺失 → 用文档自带 tool_call_id+timestamp
    # 派生稳定 ID（读侧兼容，与回填一致）。tool_call_id/timestamp 缺任一 → 不派生，
    # 留空交 strict 校验失败（畸形文档，绝不 now_iso 兜底）。
    if (isinstance(data, dict) and data.get("role") == "tool"
            and not data.get("message_id")
            and bool(data.get("tool_call_id")) and bool(data.get("timestamp"))):
        data = {**data, "message_id": stable_tool_message_id(
            data["tool_call_id"], data["timestamp"])}
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_adapters.py core/schema/tests/test_message_degradation.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/schema/adapters/message_adapter.py core/schema/tests/test_adapters.py
git commit -m "feat(message_adapter): 存量工具消息空 ID 读侧派生稳定 ID（spec D2）"
```

---

### Task 4: D3 — 两条生产写入路径补 `message_id`

**Files:**
- Modify: `core/cognitive/loop.py`（828 行 submit_task、907 行普通工具两处 `ToolResultMessage(...)`）
- Test: `core/cognitive/tests/test_tool_image_parts.py` 加写入非空断言

**Interfaces:**
- Consumes: Task 2 strict 模型（构造必填）。`new_message_id` 已 import（loop.py:25）。
- Produces: 新工具消息从创建起拥有独立 `msg_` ID，与 D1 派生（仅存量）语义分离。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_tool_image_parts.py`（`_make_loop`/`_raw_tool_call` 基建已存在）加：

```python
class TestToolResultMessageHasId:
    @pytest.mark.asyncio
    async def test_normal_tool_dispatch_gets_message_id(self):
        """普通工具分发（loop.py:907）→ ToolResultMessage.message_id 非空。"""
        adapter = _FakeAdapter(["desktop.screen.capture"])
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name=tc.function.name, content="ok", duration_ms=0)) for tc in tcs]

        await loop._execute_tool_round(
            [], "", _raw_tool_call(call_id="c_1", name="desktop_screen_capture"),
            fake_execute)

        tool_msgs = [m for m in loop._session.messages
                     if isinstance(m, ToolResultMessage)]
        assert len(tool_msgs) == 1
        assert str(tool_msgs[0].message_id).startswith("msg_")
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_submit_task_interception_gets_message_id(self):
        """submit_task 拦截（loop.py:828）→ ToolResultMessage.message_id 非空。"""
        adapter = _FakeAdapter([])
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        # 注入 delegation_preparer 触发 submit_task 拦截分支；mock _handle_submit_task
        loop._delegation_preparer = MagicMock()
        loop._handle_submit_task = AsyncMock(return_value=ToolResult.ok(
            name="submit_task", content="ok", duration_ms=0.0))
        await loop.phases.enter_thinking()

        await loop._execute_tool_round(
            [], "", _raw_tool_call(call_id="c_2", name="submit_task"), None)

        tool_msgs = [m for m in loop._session.messages
                     if isinstance(m, ToolResultMessage) and m.tool_name == "submit_task"]
        assert len(tool_msgs) == 1
        assert str(tool_msgs[0].message_id).startswith("msg_")
        await loop._output.close()
```

> 两条路径覆盖说明：① 普通工具走 `_execute_tool_round` 分发 → `loop.py:907`；② submit_task 走
> `_delegation_preparer` 非 None 的拦截分支 → `loop.py:828`（mock `_handle_submit_task` 隔离
> 后台任务系统）。gate 对 `submit_task` 跳过校验（`tools.py:118` 显式排除）→ 放行；
> `submit_task` 不在 registry，`_execute_tools_directly` 不参与本分支。

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_tool_image_parts.py -q --tb=short`
Expected: 新用例 FAIL（当前两条路径产空 message_id，且 Task 2 strict 后 `_execute_tool_round` 会因缺 message_id 抛 ValidationError——Task 4 未实现前为失败态）。

- [ ] **Step 3: 实现**

`core/cognitive/loop.py`：
- 828 行 submit_task 的 `ToolResultMessage(` 首参前加 `message_id=new_message_id(),`。
- 907 行普通工具分发的 `ToolResultMessage(` 加 `message_id=new_message_id(),`。

（`new_message_id` 已在 loop.py:25 import。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_tool_image_parts.py core/cognitive/tests/test_v2_generation.py -q --tb=short`
Expected: PASS（两条路径各覆盖）。

- [ ] **Step 5: 提交**

```bash
git add core/cognitive/loop.py core/cognitive/tests/test_tool_image_parts.py
git commit -m "fix(loop): 工具结果消息两处写入补 message_id（spec D3）"
```

---

### Task 5: D4 — `MessageRepository.append` 归档守卫

**Files:**
- Modify: `core/storage/message_repo.py`（append 顶部守卫）
- Test: `core/storage/tests/test_message_repo.py`

**Interfaces:**
- Consumes: Task 2 strict 模型（主要防线）。`append` 是 fire-and-forget 归档（session `_archive_done` 捕获异常降级 warning）。
- Produces: 归档边界对空 ID 抛 `ValueError`——守卫置于 `self._db is None` 分支**之前**。

- [ ] **Step 1: 写失败测试**

`core/storage/tests/test_message_repo.py`（`TestMessageRepository` 类内加；复用既有 `asyncio.run` / `_make_user_msg` / MagicMock 模式，`MessageId`/`SessionId`/`TextPart` 已 import）：

```python
def test_append_rejects_empty_message_id(self):
    """空 message_id → ValueError（守卫在 db-None 分支之前，与存储可用性无关）。

    D4 守卫的真实对象是 UserMessage/AssistantMessage 显式 MessageId("")——
    ToolResultMessage 已被 Task 2 模型校验器拒绝，构造即抛，到不了 append。
    """
    repo = MessageRepository(db=MagicMock())
    msg = UserMessage(
        content=[TextPart(content="hi")],
        message_id=MessageId(""),
        session_id=SessionId("sid"),
    )
    with pytest.raises(ValueError):
        asyncio.run(repo.append(SessionId("sid"), msg))

def test_append_guard_before_db_none_branch(self):
    """Mongo 离线（db=None）且空 ID → 仍抛 ValueError，不被「DB 不可用」warning 掩盖。"""
    repo = MessageRepository(db=None)
    msg = UserMessage(
        content=[TextPart(content="hi")],
        message_id=MessageId(""),
        session_id=SessionId("sid"),
    )
    with pytest.raises(ValueError):
        asyncio.run(repo.append(SessionId("sid"), msg))
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_message_repo.py -q --tb=short`
Expected: 新用例 FAIL（当前 append 放行空 ID）。

- [ ] **Step 3: 实现守卫**

`core/storage/message_repo.py` 的 `append` 顶部（`if self._db is None:` 之前）加：

```python
        # 归档 ID 非空不变量（spec D4）：数据不变量与存储可用性无关，置于 db-None
        # 分支之前——否则 Mongo 离线时空 ID 被「DB 不可用」warning 掩盖违规。
        # append 是 fire-and-forget 归档，此 ValueError 会被 session._archive_done
        # 降级为 warning；同步 fail-fast 靠模型校验器（Task 2）。
        if not msg.message_id:
            raise ValueError(
                f"消息归档拒绝空 message_id（role={msg.role}，session={session_id}）"
                "——生产写入路径必须分配非空 ID")
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_message_repo.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/storage/message_repo.py core/storage/tests/test_message_repo.py
git commit -m "feat(message_repo): append 归档空 ID 守卫（spec D4）"
```

---

### Task 6: D5 — 契约 `min_length=1` + 重新生成

**Files:**
- Modify: `frontend_contract/rest.py`（`Message.message_id`）
- Test: `frontend_contract/tests/test_rest.py`
- Regenerate: `frontend/src/types/contract.generated.ts`、`frontend_contract/contract.schema.json`、`frontend_contract/contract.raw.ts`（后两者受管路径在 `frontend_contract/` 下）

**Interfaces:**
- Consumes: Task 3 派生（读侧恒非空）→ 契约收紧不误伤。
- Produces: `Message.message_id` 空串校验失败；三产物漂移门禁通过。

- [ ] **Step 1: 写失败测试**

`frontend_contract/tests/test_rest.py` 加（该文件逐测试用 `@pytest.mark.unit`，新用例同样必须带，否则 `make test-quick` 的 `-m "unit or service"` 会静默跳过）：

```python
@pytest.mark.unit
def test_message_requires_non_empty_message_id():
    from frontend_contract.rest import Message
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        Message.model_validate({
            "role": "tool", "message_id": "", "timestamp": "2026-08-13T00:00:00Z",
            "content": [], "parts": [],
        })

@pytest.mark.unit
def test_message_accepts_real_message_id():
    from frontend_contract.rest import Message

    m = Message.model_validate({
        "role": "tool", "message_id": "msg_0123456789abcdef0123456789",
        "timestamp": "2026-08-13T00:00:00Z", "content": [], "parts": [],
    })
    assert m.message_id.startswith("msg_")
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_rest.py -q --tb=short`
Expected: 新用例 FAIL（当前空串合法）。

- [ ] **Step 3: 实现 + 重新生成契约**

`frontend_contract/rest.py` `Message.message_id`：

```python
    message_id: str = Field(min_length=1, description="消息 ID（归档消息 ID 非空不变量，spec D5）")
```

运行：
```bash
conda run -n Elysia python -m frontend_contract   # 生成 ts/schema/raw_ts 三产物
conda run -n Elysia python -m frontend_contract --check
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_rest.py -q --tb=short` 与 `conda run -n Elysia python -m frontend_contract --check`
Expected: PASS + 漂移门禁通过（产物已重新生成）。

- [ ] **Step 5: 提交**

```bash
git add frontend_contract/rest.py frontend_contract/tests/test_rest.py frontend/src/types/contract.generated.ts frontend_contract/contract.schema.json frontend_contract/contract.raw.ts
git commit -m "feat(contract): Message.message_id min_length=1 + 三产物重生成（spec D5）"
```

---

### Task 7: D6 — 幂等回填脚本

**Files:**
- Create: `scripts/backfill_message_ids.py`
- Test: `scripts/tests/test_backfill_message_ids.py`

**Interfaces:**
- Consumes: Task 1 `stable_tool_message_id`；`Module.database.documentdb` backend；`core.storage.message_repo.COLLECTION`、`core.storage.session_repo.COLLECTION`。
- Produces: `python scripts/backfill_message_ids.py [--dry-run]`——archive + sessions 空/缺失 ID 工具消息回填派生 ID；重复运行零变化；无法派生跳过 + warning。

- [ ] **Step 1: 写失败测试**

`scripts/tests/test_backfill_message_ids.py`（marker=unit；用内存 fake 镜像真实 backend 的
`find`（`_id $gt` keyset + `$in` + sort）+ `update_one` 语义）：

```python
"""回填脚本幂等性测试（spec D6，marker=unit）。"""
import asyncio

import pytest

from core.storage.message_repo import COLLECTION as MSG_COLLECTION
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION
from scripts.backfill_message_ids import backfill_archive, backfill_sessions


pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeDocDB:
    """内存文档 DB：支持 find($in/$gt keyset/sort) + update_one($set)。

    镜像真实 backend（find 返回 str _id、update 按 _id ObjectId 过滤）的关键语义：
    更新后文档离开过滤集（message_id 非空）→ keyset 分页不跳档。
    """

    def __init__(self, docs: dict[str, list[dict]]):
        self._docs = {k: list(v) for k, v in docs.items()}

    def _match(self, d: dict, filter_expr: dict) -> bool:
        for k, v in filter_expr.items():
            if isinstance(v, dict):
                # 支持 $in（含缺失→None）与 $gt（keyset _id）
                if "$in" in v and d.get(k) not in v["$in"]:
                    return False
                if "$gt" in v and not (str(d.get(k)) > str(v["$gt"])):
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find(self, collection, filter_expr, *, sort=None, limit=100, skip=0):
        hits = [d for d in self._docs.get(collection, [])
                if self._match(d, filter_expr)]
        if sort:
            for key, direction in sort:
                hits.sort(key=lambda d: str(d.get(key, "")), reverse=(direction == -1))
        return hits[skip:skip + limit]

    async def update_one(self, collection, filter_expr, update, upsert=False):
        for d in self._docs.get(collection, []):
            if self._match(d, filter_expr):
                d.update(update.get("$set", {}))
                return 1
        return 0

    async def count(self, collection, filter_expr):
        return len(self._docs.get(collection, []))


def _tool_doc(mid="", tool_call_id="call_1", timestamp="2026-08-13T00:00:00Z",
              _id="0123456789abcdef01234567"):
    return {"_id": _id, "role": "tool", "message_id": mid,
            "tool_call_id": tool_call_id, "timestamp": timestamp,
            "tool_name": "get_time", "content": [{"type": "text", "content": "ok"}]}


def test_archive_empty_ids_filled_deterministically():
    docs = [_tool_doc(mid=""), _tool_doc(mid="", tool_call_id="call_2",
                                          timestamp="2026-08-13T00:00:01Z")]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 2 and skipped == 0
    for d in db._docs[MSG_COLLECTION]:
        assert str(d["message_id"]).startswith("msg_")


def test_archive_idempotent_rerun_zero_changes():
    docs = [_tool_doc(mid="")]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    _run(backfill_archive(db))
    before = [str(d["message_id"]) for d in db._docs[MSG_COLLECTION]]
    changed, _ = _run(backfill_archive(db))
    assert changed == 0
    after = [str(d["message_id"]) for d in db._docs[MSG_COLLECTION]]
    assert before == after


def test_archive_keyset_pagination_no_skips():
    """>batch（batch=100）空 ID 文档 keyset 分页单轮收敛，无跳档（review 修复）。"""
    docs = [_tool_doc(mid="", tool_call_id=f"call_{i}",
                      timestamp=f"2026-08-13T00:00:0{i % 10}Z",
                      _id=f"{i:024d}") for i in range(150)]
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 150 and skipped == 0
    assert all(str(d["message_id"]).startswith("msg_")
               for d in db._docs[MSG_COLLECTION])


def test_archive_unable_to_derive_skipped_not_crashed():
    docs = [_tool_doc(mid="", tool_call_id="")]  # 缺 tool_call_id → 无法派生
    db = _FakeDocDB({MSG_COLLECTION: docs})
    changed, skipped = _run(backfill_archive(db))
    assert changed == 0 and skipped == 1
    assert db._docs[MSG_COLLECTION][0]["message_id"] == ""


def test_sessions_array_elements_filled_by_index():
    sess = {"_id": "0123456789abcdef01234567", "session_id": "s1",
            "messages": [_tool_doc(mid=""), _tool_doc(mid="", tool_call_id="call_2")]}
    db = _FakeDocDB({SESSION_COLLECTION: [sess]})
    changed, skipped = _run(backfill_sessions(db))
    assert changed == 2 and skipped == 0
    for m in db._docs[SESSION_COLLECTION][0]["messages"]:
        assert str(m["message_id"]).startswith("msg_")
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest scripts/tests/test_backfill_message_ids.py -q --tb=short`
Expected: FAIL with `ImportError`（脚本不存在）。

- [ ] **Step 3: 实现脚本**

`scripts/backfill_message_ids.py`（参照 `scripts/clean_db.py` 的连接样板）：

```python
#!/usr/bin/env python
"""幂等回填历史工具消息的空/缺失 message_id（spec D6）。

用法:
    python scripts/backfill_message_ids.py --dry-run   # 只报告将改动数，不写库
    python scripts/backfill_message_ids.py             # 实跑回填
"""
import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
load_dotenv(_SCRIPT_DIR / ".env")

from bson import ObjectId  # noqa: E402

from Module.database.documentdb import DocumentDBFactory  # noqa: E402
from core.storage.message_repo import COLLECTION as MSG_COLLECTION  # noqa: E402
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION  # noqa: E402
from core.utils.message_id import stable_tool_message_id  # noqa: E402

_EMPTY_ID_FILTER = {"message_id": {"$in": ["", None]}}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填历史工具消息空 message_id")
    parser.add_argument("--dry-run", action="store_true",
                        help="只扫描报告将改动的文档数，不写库")
    parser.add_argument("--limit", type=int, default=0,
                        help="每批处理上限（0=全量）")
    return parser.parse_args(argv)


async def backfill_archive(db, *, dry_run: bool = False,
                           limit: int = 0) -> tuple[int, int]:
    """message_archive：role=tool 且空/缺失 message_id → 派生稳定 ID。返回 (changed, skipped)。

    keyset 分页（_id $gt）：更新后文档离开过滤集（message_id 非空），skip-offset
    会越过收缩后的剩余文档（review 修复——>batch 空 ID 单轮收敛不跳档）。
    """
    changed = skipped = 0
    batch = 100
    last_id = None
    while True:
        filt = {"role": "tool", "message_id": {"$in": ["", None]}}
        if last_id is not None:
            filt["_id"] = {"$gt": ObjectId(last_id)}
        docs = await db.find(MSG_COLLECTION, filt,
                             sort=[("_id", 1)], limit=batch)
        if not docs:
            break
        last_id = docs[-1]["_id"]
        for doc in docs:
            tc, ts = doc.get("tool_call_id"), doc.get("timestamp")
            if not tc or not ts:
                skipped += 1
                continue
            mid = stable_tool_message_id(tc, ts)
            if not dry_run:
                await db.update_one(
                    MSG_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": {"message_id": str(mid)}})
            changed += 1
        if limit and changed + skipped >= limit:
            break
    return changed, skipped


async def backfill_sessions(db, *, dry_run: bool = False,
                            limit: int = 0) -> tuple[int, int]:
    """sessions.messages[]：role=tool 且空/缺失 message_id → 按数组下标 $set。返回 (changed, skipped)。"""
    changed = skipped = 0
    batch = 100
    skip = 0
    while True:
        docs = await db.find(SESSION_COLLECTION, {}, limit=batch, skip=skip)
        if not docs:
            break
        skip += len(docs)
        for doc in docs:
            messages = doc.get("messages") or []
            updates: dict[str, object] = {}
            for idx, m in enumerate(messages):
                if m.get("role") != "tool" or m.get("message_id"):
                    continue
                tc, ts = m.get("tool_call_id"), m.get("timestamp")
                if not tc or not ts:
                    skipped += 1
                    continue
                updates[f"messages.{idx}.message_id"] = str(
                    stable_tool_message_id(tc, ts))
            if updates and not dry_run:
                await db.update_one(
                    SESSION_COLLECTION, {"_id": ObjectId(doc["_id"])},
                    {"$set": updates})
            changed += len(updates)
        if limit and skip >= limit:
            break
    return changed, skipped


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = DocumentDBFactory.create("mongodb/default")
    mode = "dry-run" if args.dry_run else "实跑"
    a_changed, a_skipped = await backfill_archive(db, dry_run=args.dry_run,
                                                  limit=args.limit)
    s_changed, s_skipped = await backfill_sessions(db, dry_run=args.dry_run,
                                                   limit=args.limit)
    print(f"[{mode}] archive: 回填 {a_changed}，跳过(无法派生) {a_skipped}")
    print(f"[{mode}] sessions: 回填 {s_changed}，跳过(无法派生) {s_skipped}")
    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

（实现时若 `DocumentDBFactory` 的 `find` 不支持空 filter，`backfill_sessions` 用 `{"messages": {"$exists": True}}`；`$in` 空值过滤对 fake 测试提供兼容。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest scripts/tests/test_backfill_message_ids.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/backfill_message_ids.py scripts/tests/test_backfill_message_ids.py
git commit -m "feat(scripts): 幂等回填历史工具消息空 message_id（spec D6，--dry-run）"
```

---

### Task 8: REST / 搜索 / JSONL 回归测试

**Files:**
- Test: `core/input/tests/test_session_routes.py`（REST 全角色 ID 非空 + 搜索出口）；`core/schema/tests/test_adapters.py` 或新文件（JSONL 三源一致）

**Interfaces:**
- Consumes: Task 3 派生 + Task 6 契约。断言：REST 响应每条 `message_id` 非空；搜索工具消息 ID 稳定；JSONL 三源一致。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_session_routes.py`（或 `core/schema/tests/test_adapters.py` 就近放置）加：

```python
@pytest.mark.unit
class TestArchiveExportNeverEmitsEmptyMessageId:
    """存量空 ID 工具文档经 archive→wire→contract 链路产出非空稳定 ID（spec 6.3/6.8）。

    这正是 Electron 阻断链：_doc_to_message（读侧派生）→ _archive_to_client →
    _archive_msg_to_contract（min_length=1）→ 前端 z.string().min(1)。
    """

    def test_legacy_empty_id_derived_through_export_chain(self):
        from core.input.api_routes import _archive_msg_to_contract, _archive_to_client
        from core.schema.adapters.message_adapter import validate_message_legacy_compat

        doc = {
            "role": "tool", "message_id": "", "tool_call_id": "call_legacy_1",
            "timestamp": "2026-08-13T00:00:00Z", "tool_name": "get_time",
            "content": [{"type": "text", "content": "legacy"}],
        }
        msg = validate_message_legacy_compat(doc)          # 读侧派生（Task 3）
        wire = _archive_to_client([msg])[0]
        contract = _archive_msg_to_contract(wire)          # min_length=1（Task 6）
        assert contract is not None
        assert contract.message_id.startswith("msg_")

    def test_search_export_uses_same_derivation(self):
        """搜索出口经 _doc_to_message → 适配器派生，与历史同一派生键。"""
        from core.storage.message_repo import _doc_to_message

        doc = {
            "_id": "0123456789abcdef01234567", "session_id": "default",
            "role": "tool", "message_id": "", "tool_call_id": "call_legacy_2",
            "timestamp": "2026-08-13T00:00:01Z", "tool_name": "web_search",
            "content": [{"type": "text", "content": "hit"}],
        }
        msg = _doc_to_message(doc)
        assert str(msg.message_id).startswith("msg_")
        assert msg.metadata.get("_mongo_id") == "0123456789abcdef01234567"
```

（`pytestmark` 按测试文件既有 marker；`_doc_to_message` 是 module 私有函数，测试内 `# noqa: SLF001` 不适用——直接 import 即可，与 `_archive_msg_to_contract` 同样属共享 helper 测试。）

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -q --tb=short`
Expected: 新用例 FAIL——`test_legacy_empty_id_derived_through_export_chain` 当前链路的
`_archive_msg_to_contract` 对空 ID 放行但 `msg.message_id` 为空（Task 3 未实现前）；
`test_search_export_uses_same_derivation` 当前 `_doc_to_message` 不派生 → 断言空。

- [ ] **Step 3: 实现（仅测试；生产由 Task 3/6 覆盖）——补 JSONL 三源一致用例**

在 `core/schema/tests/test_message_degradation.py`（或同文件追加）加 JSONL 往返用例：

```python
@pytest.mark.unit
class TestJsonlThreeSourceConsistency:
    def test_jsonl_legacy_empty_id_derives_same_as_archive(self):
        """同一工具消息经 archive 读侧派生与 JSONL 读侧派生产出同一稳定 ID（spec §4）。"""
        from core.schema.adapters.message_adapter import validate_message_legacy_compat

        doc = {
            "role": "tool", "message_id": "", "tool_call_id": "call_3src",
            "timestamp": "2026-08-13T00:00:00Z", "tool_name": "get_time",
            "content": [{"type": "text", "content": "ok"}],
        }
        archive_msg = validate_message_legacy_compat(doc)
        jsonl_msg = validate_message_legacy_compat({**doc})  # 同一 dict 经两读路径
        assert str(archive_msg.message_id) == str(jsonl_msg.message_id)
        assert str(jsonl_msg.message_id).startswith("msg_")
```

（archive 读路径 `_doc_to_message` 与 JSONL 读路径 `session._load_from_jsonl` 都调
`validate_message_legacy_compat`——同一派生键即保证三源一致；本用例锁定该共享路径。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py core/schema/tests/test_message_degradation.py -q --tb=short`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add core/input/tests/test_session_routes.py core/schema/tests/test_message_degradation.py
git commit -m "test(routes): REST/搜索出口 message_id 非空 + JSONL 三源一致回归（spec 6.3/6.8）"
```

---

### Task 9: 门禁全绿 + 真实 Mongo 回填验证 + 文档收口

**Files:**
- 门禁：`make test-quick`、`ruff`、`pyright`、`python -m frontend_contract --check`、`scripts/check_error_codes.py`、`git diff --check`
- 实跑：`python scripts/backfill_message_ids.py --dry-run`（先）→ `python scripts/backfill_message_ids.py`（后）
- 文档：`docs/progress/2026-08-13-...-message-id-fix.md`、`docs/review/` 补修复报告、`todo.md` 状态

- [ ] **Step 1: 全量门禁**

```bash
conda run -n Elysia make test-quick
conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__
conda run -n Elysia pyright
conda run -n Elysia python -m frontend_contract --check
conda run -n Elysia python scripts/check_error_codes.py
git diff --check
```
Expected: 全绿。

- [ ] **Step 2: 真实 Mongo 回填 + 幂等验证**

```bash
conda run -n Elysia python scripts/backfill_message_ids.py --dry-run   # 记录改动数
conda run -n Elysia python scripts/backfill_message_ids.py             # 实跑
conda run -n Elysia python scripts/backfill_message_ids.py --dry-run   # 应为 0 改动
```
Expected: 首次实跑后 `--dry-run` 变更数 = 0；archive 中 `role=tool 且空/缺失 message_id` 计数 = 0。

- [ ] **Step 3: 文档收口**

写 `docs/progress/2026-08-13-...-message-id-fix.md`（批次状态 + 门禁证据 + 实跑结果）；更新 `todo.md`（本批闭环，BE-INT-P0-01/02/P1-01 状态）；`docs/review/` 补修复报告（D1-D6 决策 + 实测数据 + 验收对照）。

- [ ] **Step 4: 提交**

```bash
git add docs/ todo.md
git commit -m "docs(progress): 消息 ID 修复批次状态 + review 报告 + todo（BE-INT-P0-01/02/P1-01）"
```

---

## 执行交接

Plan 已保存至 `docs/superpowers/plans/2026-08-13-...-plan.md`。两种执行方式：

1. **Subagent-Driven（推荐）**：每个任务派发新 implementer subagent，任务间 review，快速迭代。
2. **Inline Execution**：本会话按 executing-plans 批量执行 + checkpoint 审查。

请选择。
