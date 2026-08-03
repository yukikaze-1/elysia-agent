# Token 消耗追踪 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为项目对话系统增加 token 消耗追踪 — 记录每次 LLM API 调用的 token 数、按模型分类、前端展示、MongoDB 持久化（降级到本地 JSONL）。

**Architecture:** 新建 `TokenTracker` 组件统一管理 token 统计/广播/持久化。Backend 层从 OpenAI API 响应捕获 usage 并通过 `stream_think()` 透传。`CognitiveLoop` 检测 usage chunk 后调用 TokenTracker。Output 层新增 `token_usage` 方法走 WebSocket 推送到前端。

**Tech Stack:** Python 3.13, asyncio, openai SDK, MongoDB (documentdb), vanilla JS/CSS

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `core/token_tracker.py` | **新建** — 核心组件：累计、广播、持久化 |
| `core/test_token_tracker.py` | **新建** — TokenTracker 单元测试 |
| `Module/llm/openai_compat.py` | 捕获 API usage，emit usage chunk |
| `Module/llm/protocol.py` | 更新 stream_think docstring |
| `Module/llm/test_llm.py` | 新增 usage chunk 测试 |
| `Module/output/protocol.py` | 新增 on_token_usage 默认 no-op |
| `Module/output/websocket.py` | 新增 on_token_usage → WS JSON |
| `Module/output/manager.py` | 新增 token_usage() 便捷方法 |
| `core/cognitive_loop.py` | 检测 usage chunk → TokenTracker |
| `core/agent.py` | 实例化 TokenTracker，注入 |
| `core/debug_info.py` | 新增 token_stats 字段 |
| `static/index.html` | 顶栏加 `#session-token-bar` |
| `static/js/state.js` | 加 token 状态字段 |
| `static/js/main.js` | 处理 `token_usage` 消息 |
| `static/js/chat.js` | 渲染 token 标签 + 更新顶栏 |
| `static/css/chat.css` | token 标签样式 |

---

### Task 1: Output 层 — 新增 token_usage 通道

**Files:**
- Modify: `Module/output/protocol.py:28`（OutputChannel 类，在 on_state 和 on_message 之间）
- Modify: `Module/output/websocket.py:90`（在 on_state 之后）
- Modify: `Module/output/manager.py:155`（在 command 方法之后）

- [ ] **Step 1: OutputChannel 新增 on_token_usage 默认 no-op**

```python
# 在 Module/output/protocol.py 的 OutputChannel 类中，on_state 方法之后添加：

    async def on_token_usage(self, data: dict) -> None:
        """Token 消耗统计 — 前端展示用量条形图/标签"""
```

- [ ] **Step 2: WebSocketChannel 实现 on_token_usage**

```python
# 在 Module/output/websocket.py 的 on_state 方法之后添加：

    async def on_token_usage(self, data: dict) -> None:
        data["type"] = "token_usage"
        await self._send_json(data)
```

- [ ] **Step 3: OutputManager 新增 token_usage 便捷方法**

```python
# 在 Module/output/manager.py 的 command 方法之后添加：

    async def token_usage(
        self,
        data: dict,
        *,
        channels: list[OutputChannel] | None = None,
    ) -> None:
        """广播 token 消耗统计到所有通道（前端实时更新用量）。"""
        await self._broadcast(
            "on_token_usage", data,
            channels=channels,
        )
```

- [ ] **Step 4: 运行现有测试确认无回归**

```bash
python -m pytest Module/output/test_output.py -v
```

- [ ] **Step 5: Commit**

```bash
git add Module/output/protocol.py Module/output/websocket.py Module/output/manager.py
git commit -m "feat(output): 新增 on_token_usage 通道，支持 token 消耗统计广播"
```

---

### Task 2: Backend 层 — 捕获 API usage 并通过 stream 发出

**Files:**
- Modify: `Module/llm/openai_compat.py:90`（`_stream_structured` 方法）
- Modify: `Module/llm/openai_compat.py:147-169`（`_call_api` 非流式路径）
- Modify: `Module/llm/openai_compat.py:28-46`（`__init__`，新增 last_usage 属性）
- Modify: `Module/llm/protocol.py:25-36`（`stream_think` docstring）
- Modify: `Module/llm/test_llm.py`（新增 usage 测试）

- [ ] **Step 1: OpenAICompatBackend.__init__ 新增 last_usage 属性**

在 `Module/llm/openai_compat.py` 的 `__init__` 方法末尾（`self._create_clients()` 之前）添加：

```python
        self.last_usage: dict | None = None  # 最近一次 API 调用的 token 消耗
```

- [ ] **Step 2: _stream_structured 添加 stream_options 并在循环末尾 emit usage chunk**

修改 `_stream_structured` 方法中的 `client.chat.completions.create(...)` 调用，添加 `stream_options`：

```python
        response = await client.chat.completions.create(
            model=self._model,
            messages=cleaned,
            stream=True,
            extra_body=extra_body_final,
            stream_options={"include_usage": True},
            **kwargs,
        )
```

在 `async for chunk in response:` 循环末尾（`yield {"content": content, "tool_calls": tool_calls}` 之后），检测 usage：

```python
            yield {"content": content, "tool_calls": tool_calls}
            # 流式最后一个 chunk 可能携带 usage 统计
            if getattr(chunk, "usage", None):
                u = chunk.usage
                usage = {
                    "prompt_tokens": u.prompt_tokens or 0,
                    "completion_tokens": u.completion_tokens or 0,
                    "total_tokens": u.total_tokens or 0,
                }
                self.last_usage = usage
                yield {"content": None, "tool_calls": None, "usage": usage}
```

- [ ] **Step 3: _call_api 非流式路径捕获 usage**

在 `_call_api` 非流式路径（约第 167 行）的 `content = response.choices[0].message.content` 之后添加：

```python
            content = response.choices[0].message.content
            # 捕获 usage 统计
            if getattr(response, "usage", None):
                self.last_usage = {
                    "prompt_tokens": response.usage.prompt_tokens or 0,
                    "completion_tokens": response.usage.completion_tokens or 0,
                    "total_tokens": response.usage.total_tokens or 0,
                }
            if content:
                yield content
```

- [ ] **Step 4: protocol.py 更新 stream_think docstring**

```python
    async def stream_think(
        self, messages: list[dict], **kwargs
    ) -> AsyncGenerator[dict, None]:
        """流式思考 — 逐 chunk 产出结构化 dict，支持 tool_calls 检测和 usage 统计。

        每个 chunk: {"content": str | None, "tool_calls": list | None}
        最后一个 chunk 可能包含可选的 "usage" 键:
          {"content": None, "tool_calls": None, "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}
        默认实现回退到 chat()，逐字符 yield 纯文本（无 tool_calls）。
        支持 tool calling 的 backend 应覆盖此方法。
        """
```

- [ ] **Step 5: 编写 backend usage 单元测试**

在 `Module/llm/test_llm.py` 中添加测试类：

```python
class TestUsageTracking:
    """测试 API usage 捕获。"""

    @pytest.mark.asyncio
    async def test_stream_structured_yields_usage_chunk(self, llm_backend):
        """流式 streaming 中检测到 usage 时产出 usage chunk。"""
        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 100
        usage_obj.completion_tokens = 50
        usage_obj.total_tokens = 150

        delta = MagicMock()
        delta.content = "hello"
        delta.tool_calls = None

        chunk_no_usage = MagicMock()
        chunk_no_usage.choices = [MagicMock(delta=delta)]
        chunk_no_usage.usage = None

        chunk_with_usage = MagicMock()
        chunk_with_usage.choices = [MagicMock(delta=MagicMock(content=None, tool_calls=None))]
        chunk_with_usage.usage = usage_obj

        llm_backend._client.chat.completions.create = AsyncMock()
        llm_backend._client.chat.completions.create.return_value = [chunk_no_usage, chunk_with_usage]

        chunks = []
        async for chunk in llm_backend._stream_structured([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0] == {"content": "hello", "tool_calls": None}
        assert chunks[1] == {
            "content": None,
            "tool_calls": None,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        assert llm_backend.last_usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_stream_structured_no_usage_when_none(self, llm_backend):
        """无 usage 时不产出额外 chunk。"""
        delta = MagicMock()
        delta.content = "ok"
        delta.tool_calls = None

        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=delta)]
        chunk.usage = None

        llm_backend._client.chat.completions.create = AsyncMock()
        llm_backend._client.chat.completions.create.return_value = [chunk]

        chunks = []
        async for chunk in llm_backend._stream_structured([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert "usage" not in chunks[0]

    @pytest.mark.asyncio
    async def test_call_api_nonstreaming_captures_usage(self, llm_backend):
        """非流式路径捕获 usage。"""
        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 10
        usage_obj.completion_tokens = 5
        usage_obj.total_tokens = 15

        choice = MagicMock()
        choice.message.content = "resp"

        mock_response = MagicMock()
        mock_response.choices = [choice]
        mock_response.usage = usage_obj

        llm_backend._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await llm_backend.chat([{"role": "user", "content": "hi"}])
        assert result == "resp"
        assert llm_backend.last_usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
```

- [ ] **Step 6: 运行 LLM 测试**

```bash
python -m pytest Module/llm/test_llm.py -v
```

- [ ] **Step 7: Commit**

```bash
git add Module/llm/openai_compat.py Module/llm/protocol.py Module/llm/test_llm.py
git commit -m "feat(llm): 捕获 OpenAI API usage 并通过 stream_think 透传"
```

---

### Task 3: TokenTracker 核心组件 + 测试

**Files:**
- Create: `core/token_tracker.py`
- Create: `core/test_token_tracker.py`

- [ ] **Step 1: 编写 TokenTracker 单元测试（先写测试）**

```python
# core/test_token_tracker.py
"""TokenTracker 单元测试。"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from core.token_tracker import TokenTracker, TokenUsage


class TestTokenUsage:
    def test_dataclass_fields(self):
        u = TokenUsage(
            model="deepseek/deepseek-chat",
            prompt_tokens=800,
            completion_tokens=200,
            total_tokens=1000,
            round_num=0,
            timestamp=1717718400.0,
        )
        assert u.model == "deepseek/deepseek-chat"
        assert u.total_tokens == 1000
        assert u.round_num == 0


class TestTokenTracker:
    @pytest.fixture
    def output_mock(self):
        output = MagicMock()
        output.broadcast = AsyncMock()
        return output

    @pytest.fixture
    def tracker(self, output_mock):
        return TokenTracker(session_id="test-session", output_manager=output_mock, document_db=None)

    @pytest.mark.asyncio
    async def test_record_accumulates_session_total(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat",
            round_num=0,
        )
        assert tracker.session_total == 1000
        assert tracker.per_model == {"deepseek/deepseek-chat": 1000}

    @pytest.mark.asyncio
    async def test_record_per_model_separate(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0,
        )
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
            model="openai/gpt-4o", round_num=0,
        )
        assert tracker.session_total == 1200
        assert tracker.per_model == {"deepseek/deepseek-chat": 1000, "openai/gpt-4o": 200}

    @pytest.mark.asyncio
    async def test_record_broadcasts_token_usage(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
            model="deepseek/deepseek-chat", round_num=0,
        )
        tracker._output.token_usage.assert_awaited_once()
        call_data = tracker._output.token_usage.call_args[0][0]
        assert call_data["turn"]["turn_total"] == 1000
        assert call_data["session"]["total"] == 1000
        assert call_data["session"]["by_model"]["deepseek/deepseek-chat"] == 1000

    @pytest.mark.asyncio
    async def test_start_turn_clears_current_usages(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        assert len(tracker.current_turn_usages) == 1
        tracker.start_turn()
        assert len(tracker.current_turn_usages) == 0
        # session_total 不受影响
        assert tracker.session_total == 150

    @pytest.mark.asyncio
    async def test_persist_writes_jsonl_when_no_db(self, tracker):
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            tracker._pending_file = f.name

        try:
            await tracker.record(
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model="test/model", round_num=0,
            )
            with open(tracker._pending_file) as f:
                lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["model"] == "test/model"
            assert record["total_tokens"] == 15
        finally:
            os.unlink(tracker._pending_file)

    @pytest.mark.asyncio
    async def test_persist_to_mongodb_when_available(self, output_mock):
        db_mock = MagicMock()
        db_mock.insert_one = AsyncMock(return_value=MagicMock(inserted_id="abc"))
        tracker = TokenTracker(session_id="s1", output_manager=output_mock, document_db=db_mock)

        await tracker.record(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="test/model", round_num=0,
        )
        db_mock.insert_one.assert_awaited_once()
        # 验证写入的文档
        call_args = db_mock.insert_one.call_args[0]
        assert call_args[0] == "token_usage"
        doc = call_args[1]
        assert doc["session_id"] == "s1"
        assert doc["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_flush_pending_imports_and_deletes_file(self, output_mock):
        db_mock = MagicMock()
        db_mock.insert_many = AsyncMock()
        tracker = TokenTracker(session_id="s1", output_manager=output_mock, document_db=db_mock)

        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".jsonl") as f:
            f.write(json.dumps({"session_id": "s1", "model": "m", "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "round_num": 0, "timestamp": 1.0}) + "\n")
            tracker._pending_file = f.name

        try:
            await tracker.flush_pending()
            db_mock.insert_many.assert_awaited_once()
            assert not os.path.exists(tracker._pending_file)
        finally:
            if os.path.exists(tracker._pending_file):
                os.unlink(tracker._pending_file)

    @pytest.mark.asyncio
    async def test_flush_pending_no_file_is_noop(self, output_mock):
        db_mock = MagicMock()
        tracker = TokenTracker(session_id="s1", output_manager=output_mock, document_db=db_mock)
        tracker._pending_file = "/tmp/nonexistent_token_test.jsonl"
        await tracker.flush_pending()
        db_mock.insert_many.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_session_stats_from_memory(self, tracker):
        await tracker.record(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model="deepseek/deepseek-chat", round_num=0,
        )
        stats = await tracker.get_session_stats()
        assert stats["total"] == 150
        assert stats["by_model"] == {"deepseek/deepseek-chat": 150}
```

- [ ] **Step 2: 运行测试验证失败**

```bash
python -m pytest core/test_token_tracker.py -v
```
预期：全部失败（ModuleNotFoundError: No module named 'core.token_tracker'）

- [ ] **Step 3: 实现 TokenTracker**

```python
# core/token_tracker.py
"""TokenTracker — 统一管理 LLM token 消耗统计、广播和持久化。

Usage:
    tracker = TokenTracker(session_id="abc", output_manager=output, document_db=db)
    await tracker.record(usage, model="deepseek/deepseek-chat", round_num=0)
    stats = await tracker.get_session_stats()
    await tracker.flush_pending()
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.output import OutputManager


@dataclass
class TokenUsage:
    """单次 API 调用的 token 消耗记录。"""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: float


class TokenTracker:
    """Token 消耗统计器。

    负责：
    - 内存累计（session 内按模型分类）
    - WebSocket 广播（前端实时显示）
    - MongoDB 持久化（不可用时降级到本地 JSONL 文件）
    """

    _pending_file = "data/token_usage_pending.jsonl"

    def __init__(
        self,
        session_id: str,
        output_manager: "OutputManager",
        document_db=None,
    ):
        self.session_id = session_id
        self._output = output_manager
        self._db = document_db

        # Session 内存累计
        self.session_total: int = 0
        self.per_model: dict[str, int] = {}
        self.current_turn_usages: list[TokenUsage] = []

    def start_turn(self) -> None:
        """新一轮用户对话开始前清空 turn 缓存。"""
        self.current_turn_usages.clear()

    async def record(self, usage: dict, model: str, round_num: int) -> None:
        """记录一次 API 调用的 token 消耗。

        每轮 LLM 调用完成后由 CognitiveLoop 调用一次。
        自动完成：内存累计、WS 广播、持久化。
        """
        u = TokenUsage(
            model=model,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            round_num=round_num,
            timestamp=time.time(),
        )

        # 1. 内存累计
        self.session_total += u.total_tokens
        self.per_model[model] = self.per_model.get(model, 0) + u.total_tokens
        self.current_turn_usages.append(u)

        # 2. WS 广播（前端实时更新）
        await self._output.token_usage({
            "turn": {
                "rounds": [asdict(r) for r in self.current_turn_usages],
                "turn_total": sum(r.total_tokens for r in self.current_turn_usages),
            },
            "session": {
                "total": self.session_total,
                "by_model": self.per_model,
            },
        })

        # 3. 持久化
        await self._persist(u)

    async def get_session_stats(self) -> dict:
        """获取当前 session 的 token 统计（用于前端重连恢复）。

        优先从内存，如果 MongoDB 可用则从 DB 聚合补充。
        """
        if self._db is not None:
            try:
                pipeline = [
                    {"$match": {"session_id": self.session_id}},
                    {"$group": {
                        "_id": "$model",
                        "total": {"$sum": "$total_tokens"},
                        "prompt": {"$sum": "$prompt_tokens"},
                        "completion": {"$sum": "$completion_tokens"},
                    }},
                ]
                result = await self._db.aggregate("token_usage", pipeline)
                per_model: dict[str, int] = {}
                session_total = 0
                for doc in result:
                    model = doc["_id"]
                    total = doc["total"]
                    per_model[model] = total
                    session_total += total
                # 补充 pending 文件中当前 session 的数据
                if os.path.exists(self._pending_file):
                    with open(self._pending_file) as f:
                        for line in f:
                            if not line.strip():
                                continue
                            r = json.loads(line)
                            if r.get("session_id") == self.session_id:
                                m = r["model"]
                                per_model[m] = per_model.get(m, 0) + r["total_tokens"]
                                session_total += r["total_tokens"]
                if session_total > self.session_total:
                    self.session_total = session_total
                    self.per_model = per_model
                return {"total": session_total, "by_model": per_model}
            except Exception:
                pass
        return {"total": self.session_total, "by_model": dict(self.per_model)}

    async def flush_pending(self) -> None:
        """将本地 pending 文件中的数据批量导入 MongoDB，成功后删除文件。"""
        if self._db is None:
            return
        if not os.path.exists(self._pending_file):
            return
        try:
            with open(self._pending_file) as f:
                records = [json.loads(line) for line in f if line.strip()]
            if records:
                await self._db.insert_many("token_usage", records)
            os.remove(self._pending_file)
        except Exception:
            pass  # 下次再试

    async def _persist(self, usage: TokenUsage) -> None:
        """持久化单条记录：MongoDB 优先，失败则写本地 JSONL。"""
        if self._db is not None:
            try:
                await self._db.insert_one("token_usage", asdict(usage))
                return
            except Exception:
                pass  # 降级到文件
        # 追加到本地 JSONL
        os.makedirs(os.path.dirname(self._pending_file), exist_ok=True)
        with open(self._pending_file, "a") as f:
            f.write(json.dumps(asdict(usage), ensure_ascii=False) + "\n")
```

- [ ] **Step 4: 运行 TokenTracker 测试验证通过**

```bash
python -m pytest core/test_token_tracker.py -v
```
预期：全部 PASS

- [ ] **Step 5: Commit**

```bash
git add core/token_tracker.py core/test_token_tracker.py
git commit -m "feat: 新增 TokenTracker 组件 — token 统计/广播/持久化"
```

---

### Task 4: Agent 层 — 集成 TokenTracker

**Files:**
- Modify: `core/agent.py:49`（__init__ 开头区域）
- Modify: `core/agent.py:98`（CognitiveLoop 构造处）
- Modify: `core/agent.py:133`（start 方法）
- Modify: `core/cognitive_loop.py:178`（__init__ 签名 + 赋值）
- Modify: `core/cognitive_loop.py:306`（_think_loop 方法）

- [ ] **Step 1: CognitiveLoop 接受 token_tracker 参数**

在 `cognitive_loop.py` 的 `__init__` 签名中新增参数（在 `ctx: ToolContext | None = None` 之后）：

```python
    def __init__(
        self,
        llm: LLMAdapter,
        fsm: AgentFSM,
        event_bus: EventBus,
        output: OutputManager,
        prompt: PromptAssembler,
        session: SessionMemory,
        tool_executor=None,
        emotion: EmotionEngine | None = None,
        consolidator: MemoryConsolidator | None = None,
        tts: TTSBackend | None = None,
        ctx: ToolContext | None = None,
        token_tracker=None,  # TokenTracker | None
    ):
        # ... 现有赋值不变 ...
        self._token_tracker = token_tracker
```

- [ ] **Step 2: CognitiveLoop._think_loop 检测 usage chunk**

在 `_think_loop` 的 `for round_num in range(self.MAX_TOOL_ROUNDS + 1):` 循环开头，`acc_text = ""` 行之后，`raw_tool_calls: list[dict] = []` 行之前，添加 `start_turn()` 调用：

加上在 `async for chunk in stream:` 循环中检测 usage（在现有 `if tc_deltas:` 判断之后）：

```python
            for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                acc_text = ""
                raw_tool_calls: list[dict] = []
                # 新 round 开始 — 下一轮的 usage 会在 stream 结束时 record
                ...
                
                stream = self._llm.stream_think(messages, tools=tool_defs,
                    extra_body={"thinking": {"type": "disabled"}})
                try:
                    async for chunk in stream:
                        content = chunk.get("content")
                        tc_deltas = chunk.get("tool_calls")

                        if content:
                            # ... 现有内容处理不变 ...

                        if tc_deltas:
                            self._merge_stream_tool_calls(raw_tool_calls, tc_deltas)

                        # Token 消耗: stream 最后可能带 usage
                        if "usage" in chunk:
                            if self._token_tracker is not None:
                                await self._token_tracker.record(
                                    usage=chunk["usage"],
                                    model=self._llm.model,
                                    round_num=round_num,
                                )
                except asyncio.CancelledError:
                    raise
```

并在每次新的用户 turn 开始前调 `start_turn()`。在 `_think_loop` 中，`tool_defs` 之后，`await self._output.typing(True)` 之前：

```python
        # 新一轮对话开始，清空 turn 缓存
        if self._token_tracker is not None:
            self._token_tracker.start_turn()

        await self._output.typing(True)
```

- [ ] **Step 3: Agent.__init__ 创建并注入 TokenTracker**

在 `agent.py` 的 `__init__` 中，`self._cognitive = CognitiveLoop(...)` 行之前：

```python
        # ---- Token 统计 ----
        self._token_tracker = None  # 在 start() 中创建（需要 session_id）

        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session=self._session,
            tool_executor=self._tool_manager,
            emotion=self._emotion,
            consolidator=self._consolidator,
            tts=self._tts,
            token_tracker=self._token_tracker,
        )
```

- [ ] **Step 4: Agent.start() 中初始化 TokenTracker**

在 `start()` 方法内，`mongo_ok` 确定之后，`# 加载工具插件` 之前：

```python
        # ---- Token 统计 ----
        from core.token_tracker import TokenTracker
        self._token_tracker = TokenTracker(
            session_id=getattr(self._session, "_session_id", "default"),
            output_manager=self._output,
            document_db=self._session._db if mongo_ok else None,
        )
        self._cognitive._token_tracker = self._token_tracker
        # MongoDB 可用时 flush 积压的 pending 文件
        if mongo_ok:
            await self._token_tracker.flush_pending()
```

- [ ] **Step 5: 运行现有测试确认无回归**

```bash
python -m pytest core/ -v --ignore=core/test_token_tracker.py
```

- [ ] **Step 6: Commit**

```bash
git add core/cognitive_loop.py core/agent.py
git commit -m "feat: CognitiveLoop + Agent 集成 TokenTracker，检测 usage chunk 并记录"
```

---

### Task 5: Debug 信息 — token_stats

**Files:**
- Modify: `core/debug_info.py:48`（DebugInfo dataclass + collect 函数）

- [ ] **Step 1: DebugInfo 新增 token_stats 字段**

```python
# 在 core/debug_info.py 的 DebugInfo dataclass 中，llm_model 字段之后添加：

    # Token 统计
    token_stats: dict = field(default_factory=dict)
```

- [ ] **Step 2: collect() 函数填充 token_stats**

在 `collect()` 函数末尾，`return info` 之前添加：

```python
    # ---- Token 统计 ----
    tracker = getattr(agent, "_token_tracker", None)
    if tracker is not None:
        info.token_stats = {
            "session_total": tracker.session_total,
            "per_model": dict(tracker.per_model),
            "current_turn": [
                {
                    "model": u.model,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "total_tokens": u.total_tokens,
                    "round_num": u.round_num,
                }
                for u in tracker.current_turn_usages[-10:]
            ],
        }
```

- [ ] **Step 3: 验证 debug_info 测试**

```bash
python -m pytest core/test_debug_info.py -v 2>/dev/null || echo "no test file, skipping"
```

- [ ] **Step 4: Commit**

```bash
git add core/debug_info.py
git commit -m "feat(debug): DebugInfo 新增 token_stats 字段"
```

---

### Task 6: 前端 — HTML + State

**Files:**
- Modify: `static/index.html:25`（顶栏区域）
- Modify: `static/js/state.js:53`（AppState 类）

- [ ] **Step 1: index.html 顶栏加 session token bar**

在 `#llm-model-tag` 之后、`#char-state` 之前：

```html
  <div id="llm-model-tag"></div>
  <div id="session-token-bar" title="Session token 消耗"></div>
  <div id="char-state">idle</div>
```

- [ ] **Step 2: state.js 新增 token 状态字段**

在 `AppState` 构造函数中，`this.llmModel = '';` 之后添加：

```javascript
    // Token 统计
    this.sessionTokens = { total: 0, byModel: {} };
    this.currentTurnTokens = [];        // 当前轮各 round 的 usage
    this.activeTurnTotal = 0;          // 当前轮总 token
```

在 `resetConnection()` 方法中添加：

```javascript
    this.sessionTokens = { total: 0, byModel: {} };
    this.currentTurnTokens = [];
    this.activeTurnTotal = 0;
```

- [ ] **Step 3: Commit**

```bash
git add static/index.html static/js/state.js
git commit -m "feat(frontend): HTML 加 token bar 容器 + state 加 token 字段"
```

---

### Task 7: 前端 — chat.js 渲染 + main.js 消息处理

**Files:**
- Modify: `static/js/main.js:130`（handleMessage switch）
- Modify: `static/js/chat.js:436`（末尾导出附近）

- [ ] **Step 1: main.js 处理 token_usage 消息**

在 `handleMessage` 函数的 switch 中，`case 'expression':` 之前添加：

```javascript
    case 'token_usage':
      state.currentTurnTokens = msg.turn ? msg.turn.rounds || [] : [];
      state.activeTurnTotal = msg.turn ? msg.turn.turn_total || 0 : 0;
      if (msg.session) {
        state.sessionTokens = msg.session;
      }
      // 附加到当前 AI 气泡（如有）
      attachTokenLabel();
      // 更新顶栏
      updateSessionTokenDisplay();
      break;
```

- [ ] **Step 2: chat.js 渲染函数**

在 `chat.js` 末尾（`import { debounceSave, saveChat } from './persistence.js';` 之前）添加：

```javascript
/* ---- Token 标签 ---- */

/**
 * 将 token_usage 数据附加到最近的 AI 气泡。
 * 每次收到 token_usage 消息时调用，会替换之前的标签。
 */
export function attachTokenLabel() {
  const rows = document.querySelectorAll('.msg-row.assistant');
  if (!rows.length) return;
  const lastRow = rows[rows.length - 1];
  const bubble = lastRow.querySelector('.msg-bubble');
  if (!bubble) return;

  // 移除旧标签
  const existing = bubble.querySelector('.token-label');
  if (existing) existing.remove();

  const rounds = state.currentTurnTokens;
  if (!rounds || !rounds.length) return;

  const turnTotal = state.activeTurnTotal;
  const totalStr = turnTotal >= 1000 ? (turnTotal / 1000).toFixed(1) + 'k' : String(turnTotal);

  const label = document.createElement('div');
  label.className = 'token-label';

  // 紧凑摘要
  const summary = document.createElement('span');
  summary.className = 'token-summary';
  summary.textContent = '\u{1FA99} ' + totalStr + ' tokens ▼';
  label.appendChild(summary);

  // 展开明细
  const detail = document.createElement('div');
  detail.className = 'token-detail';
  const lines = rounds.map(r =>
    `<div class="token-round">
      <span class="token-round-num">第${r.round_num + 1}轮</span>
      <span class="token-round-item">prompt: ${formatTokens(r.prompt_tokens)}</span>
      <span class="token-round-item">completion: ${formatTokens(r.completion_tokens)}</span>
      <span class="token-round-total">= ${formatTokens(r.total_tokens)}</span>
    </div>`
  ).join('');
  detail.innerHTML = lines;
  label.appendChild(detail);

  // 点击展开/收起
  label.onclick = (e) => {
    e.stopPropagation();
    label.classList.toggle('expanded');
  };

  bubble.appendChild(label);
}

function formatTokens(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

/**
 * 更新顶栏 session 累计 token 显示。
 */
export function updateSessionTokenDisplay() {
  const bar = document.getElementById('session-token-bar');
  if (!bar) return;
  const st = state.sessionTokens;
  if (!st || !st.total) {
    bar.textContent = '';
    bar.style.display = 'none';
    return;
  }
  const models = Object.entries(st.byModel || {});
  const parts = models.map(([m, t]) => {
    const short = m.split('/').pop();
    return short + ': ' + formatTokens(t);
  });
  bar.textContent = '\u{1FA99} 总计 ' + formatTokens(st.total) + (parts.length ? ' (' + parts.join(', ') + ')' : '');
  bar.style.display = '';
}
```

- [ ] **Step 3: 更新 chat.js 中的 export 列表**

确保 `attachTokenLabel` 和 `updateSessionTokenDisplay` 被导出给 main.js 使用。在 `chat.js` 末尾的 export 无需改动 — 因为 `main.js` 已经通过 import 引用。

在 `main.js` 的 import 中添加新函数：

```javascript
import {
  appendDelta, finalizeDelta, addUserMsg, addStatusMsg,
  addToolCard, updateToolCard, showTyping, hideTyping,
  setMood, setState, esc, createAssistantRow, setupInput,
  resetInput, scrollDown, hideNewMsgIndicator,
  addImageToPreview, clearPreview, renderUserMsg, openLightbox, closeLightbox,
  attachTokenLabel, updateSessionTokenDisplay,
} from './chat.js';
```

- [ ] **Step 4: Commit**

```bash
git add static/js/main.js static/js/chat.js
git commit -m "feat(frontend): token_usage 消息处理 + token 标签渲染"
```

---

### Task 8: 前端 — CSS 样式

**Files:**
- Modify: `static/css/chat.css`

- [ ] **Step 1: 添加 token 标签和 session bar 样式**

在 `chat.css` 末尾添加：

```css
/* ---- Session Token Bar ---- */
#session-token-bar {
  font-size: 11px;
  color: var(--muted);
  text-align: center;
  padding: 2px 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- Token Label (AI 气泡底部) ---- */
.token-label {
  margin-top: 6px;
  padding-top: 4px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}

.token-summary {
  display: inline-block;
}

.token-summary:hover {
  color: var(--text);
}

.token-detail {
  display: none;
  margin-top: 4px;
  padding: 6px 8px;
  background: var(--bg);
  border-radius: 4px;
  font-size: 10px;
  line-height: 1.6;
}

.token-label.expanded .token-detail {
  display: block;
}

.token-label.expanded .token-summary {
  color: var(--accent);
}

.token-round {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.token-round-num {
  font-weight: 600;
  min-width: 32px;
}

.token-round-item {
  color: var(--muted);
}

.token-round-total {
  font-weight: 600;
  margin-left: auto;
}
```

- [ ] **Step 2: Commit**

```bash
git add static/css/chat.css
git commit -m "feat(frontend): token 标签 + session bar CSS 样式"
```

---

### Task 9: session 重连恢复 token 数据

**Files:**
- Modify: `core/input/server.py`（WebSocket connect handler — 发送 session 消息时附带 token_stats）

- [ ] **Step 1: 找到 server.py 中发送 session 消息的位置**

```bash
grep -n "type.*session\|'session'" core/input/server.py
```

- [ ] **Step 2: 在 session 消息中附带 token_stats**

找到发送 `{"type": "session", "messages": ...}` 的位置，在消息体中添加 `token_stats` 字段。调用 `agent._token_tracker.get_session_stats()` 获取数据。

具体改动（根据 server.py 实际代码调整）:

```python
# 在 ws handler 连接时发送 session restore 消息处
token_stats = {}
if hasattr(agent, '_token_tracker') and agent._token_tracker is not None:
    token_stats = await agent._token_tracker.get_session_stats()

await ws.send_json({
    "type": "session",
    "messages": msgs,
    "token_stats": token_stats,
    ...
})
```

- [ ] **Step 3: 前端恢复 token 状态**

在 `main.js` 的 `handleMessage` 中，`case 'session':` 分支添加 token 恢复：

```javascript
    case 'session':
      restoreFromServer(msg.messages);
      if (msg.token_stats) {
        state.sessionTokens = msg.token_stats;
        updateSessionTokenDisplay();
      }
      break;
```

- [ ] **Step 4: Commit**

```bash
git add core/input/server.py static/js/main.js
git commit -m "feat: session 重连恢复 token 统计数据"
```

---

### Task 10: LLMAdapter docstring 更新

**Files:**
- Modify: `core/llm_adapter.py:71-77`（stream_think docstring）

- [ ] **Step 1: 更新 stream_think docstring**

```python
    async def stream_think(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> AsyncIterator[dict]:
        """Stream structured chunks with tool_call detection + retry on 429/5xx.

        Yields dicts: {"content": str | None, "tool_calls": list | None}
        流最后一个 chunk 可能包含可选的 "usage" 键:
          {"content": None, "tool_calls": None, "usage": {prompt_tokens, completion_tokens, total_tokens}}
        Retries on transient server errors (429, 500-504) with exponential backoff.
        Only retries if streaming hasn't started yet (no duplicate content).
        """
```

- [ ] **Step 2: Commit**

```bash
git add core/llm_adapter.py
git commit -m "docs(llm_adapter): stream_think docstring 说明 usage chunk"
```

---

### Task 11: 集成验证

- [ ] **Step 1: 运行全部后端测试**

```bash
python -m pytest Module/llm/test_llm.py Module/output/test_output.py core/test_token_tracker.py -v
```
预期：全部 PASS

- [ ] **Step 2: 启动 agent 验证端到端**

```bash
python -m core.agent  # 或 python main.py
```

在浏览器中打开 `http://localhost:8000/`，发送一条消息，验证：
1. AI 气泡底部出现 `🪙 X.Xk tokens ▼`
2. 点击 ▼ 展开后显示每轮明细
3. 顶栏 `#session-token-bar` 显示累计总量
4. 发送多条消息，累计总量递增
5. 访问 `/debug` 页面，token_stats 字段有数据

- [ ] **Step 3: 验证 MongoDB 持久化**

```bash
# 如果 MongoDB 可用
python -c "
import asyncio
from pymongo import MongoClient
c = MongoClient('mongodb://localhost:27017')
print(list(c.elysia.token_usage.find().limit(5)))
"
```
预期：有 `token_usage` 集合，包含刚产生的记录

- [ ] **Step 4: 验证本地文件降级**

停止 MongoDB 服务，发送消息，检查 `data/token_usage_pending.jsonl` 存在且内容正确。

- [ ] **Step 5: 验证 flush_pending**

重启 MongoDB，重启 agent，检查：
1. `data/token_usage_pending.jsonl` 被删除
2. MongoDB `token_usage` 集合包含原 pending 文件中的记录

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: 集成验证通过 — token 消耗追踪完整功能"
```

---

## 验证清单

| 场景 | 预期行为 |
|------|---------|
| 正常对话 | AI 气泡底部显示 token 标签，点击展开明细 |
| 工具调用（多轮） | token 标签显示所有 round 合计，展开可看每轮 |
| 模型切换 | 按模型分别累计，顶栏和 debug 均可见 |
| Ollama 不返回 usage | 无 token 标签，无报错，正常对话 |
| MongoDB 不可用 | 降级到 `data/token_usage_pending.jsonl` |
| MongoDB 恢复 | `flush_pending()` 自动导入后删除文件 |
| 页面刷新重连 | session 恢复时附带 token_stats |
| /debug 页面 | token_stats 字段显示 session_total + per_model |
