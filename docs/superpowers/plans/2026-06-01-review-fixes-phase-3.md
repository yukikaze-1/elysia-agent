# Review Fixes — Phase 3: Resilience & Observability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Master plan:** `docs/superpowers/plans/2026-06-01-review-master-plan.md`
> **Review report:** `docs/review-2026-06-01.md`
> **Prerequisite:** Phase 2 (`docs/superpowers/plans/2026-06-01-review-fixes-phase-2.md`)

**Goal:** Improve system resilience against external failures, add memory lifecycle management, and fix the TTS/INTERRUPT race condition.

**Architecture:** 7 tasks covering LLM reliability, memory lifecycle, health monitoring, TTS timing fix, and frontend polish. Tasks 3.1, 3.3, 3.4, and 3.6 are backend-only; Tasks 3.5 and 3.7 touch both backend and frontend; Task 3.8 is frontend-only.

**Note:** 原 Task 3.2 (会话管理) 已移至 todo.md。Task 3.5 (TTS 竞争) 需要仔细分析当前时序，建议在执行前先阅读 `docs/sync-lessons.md`。

**Tech Stack:** Python 3.13, asyncio, httpx, openai, aiohttp, pymilvus

---

### Task 3.1: LLM retry on 429 and 5xx errors

**Files:**
- Modify: `core/llm_adapter.py`
- Modify: `core/tests/test_phase3.py`

- [ ] **Step 1: Write failing test for retry behavior**

Add to `core/tests/test_phase3.py`:

```python
import asyncio
from unittest.mock import AsyncMock, patch

async def test_llm_adapter_retries_on_429():
    """LLMAdapter.stream_think() 应在收到 429 时自动重试。"""
    from core.llm_adapter import LLMAdapter

    adapter = LLMAdapter(model="test-model")

    call_count = 0

    async def mock_stream(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            import httpx
            raise httpx.HTTPStatusError(
                "rate limited",
                request=AsyncMock(),
                response=AsyncMock(status_code=429),
            )
        # 第三次成功
        yield "成功"

    adapter._backend = AsyncMock()
    adapter._backend.stream_think = mock_stream

    chunks = []
    async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    assert call_count == 3, f"应重试 3 次，实际调用了 {call_count} 次"
    assert chunks == ["成功"]


async def test_llm_adapter_retries_on_503():
    """LLMAdapter.stream_think() 应在收到 503 时自动重试。"""
    from core.llm_adapter import LLMAdapter

    adapter = LLMAdapter(model="test-model")

    call_count = 0

    async def mock_stream(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            import httpx
            raise httpx.HTTPStatusError(
                "service unavailable",
                request=AsyncMock(),
                response=AsyncMock(status_code=503),
            )
        yield "恢复"

    adapter._backend = AsyncMock()
    adapter._backend.stream_think = mock_stream

    chunks = []
    async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    assert call_count == 2
    assert chunks == ["恢复"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest core/tests/test_phase3.py::test_llm_adapter_retries_on_429 -v
python -m pytest core/tests/test_phase3.py::test_llm_adapter_retries_on_503 -v
```

Expected: FAIL — 无重试逻辑，429 异常直接抛出。

- [ ] **Step 3: Add retry logic to LLMAdapter**

In `core/llm_adapter.py`, add retry configuration and modify `stream_think()`:

```python
import asyncio
import logging

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0


async def stream_think(self, messages, tools=None, **kwargs):
    """流式 LLM 调用，对可重试错误自动退避重试。

    仅在流未开始产出 token 时重试（请求阶段失败）。
    一旦开始流式产出，不再重试（避免重复内容）。
    """
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            started_streaming = False
            async for chunk in self._backend.stream_think(messages, tools=tools, **kwargs):
                started_streaming = True
                yield chunk
            return  # success
        except Exception as e:
            last_exc = e
            # 如果已经开始流式产出，不再重试
            if started_streaming:
                raise
            # 检查是否是可重试错误
            status = None
            if hasattr(e, 'status_code'):
                status = e.status_code
            elif hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                status = e.response.status_code
            # httpx.HTTPStatusError
            if status is None:
                try:
                    import httpx
                    if isinstance(e, httpx.HTTPStatusError):
                        status = e.response.status_code
                except ImportError:
                    pass

            if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
                raise
            backoff = _RETRY_BACKOFF_BASE * (2 ** attempt)
            logger.warning("LLM 调用失败 (status=%s), %d/%d 重试 (%.0fs)",
                          status, attempt + 1, _MAX_RETRIES, backoff)
            await asyncio.sleep(backoff)
    raise last_exc
```

**注意:** 当前 LLMAdapter 的实现可能和上面不完全一致。需要先读取 `core/llm_adapter.py` 了解实际的 `stream_think()` 签名和 delegate 方式，然后据此调整重试逻辑。

- [ ] **Step 4: Run tests to verify fix**

```bash
python -m pytest core/tests/test_phase3.py -v -k "llm"
```

Expected: All LLM adapter tests pass, including the new retry tests.

- [ ] **Step 5: Commit**

```bash
git add core/llm_adapter.py core/tests/test_phase3.py
git commit -m "feat: add exponential backoff retry for LLM API errors (429, 5xx)"
```

---

### Task 3.3: LongTermMemory TTL and eviction

**Files:**
- Modify: `core/memory/long_term.py`
- Modify: `core/tests/test_phase2.py`
- Modify: `config.yaml`

- [ ] **Step 1: Write failing test for TTL eviction**

Add to `core/tests/test_phase2.py`:

```python
import time

def test_long_term_memory_evicts_old_entries():
    """验证过期条目被驱逐。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    now = time.time()
    old_cutoff = now - 100 * 86400  # 100 天前

    # 添加过期条目
    ltm._entries = [
        MemoryEntry(text="过期数据", metadata={}, timestamp=old_cutoff),
        MemoryEntry(text="新数据", metadata={}, timestamp=now),
    ]
    ltm._MAX_AGE_DAYS = 90

    removed = ltm._evict_old_entries()
    assert removed >= 1
    assert len(ltm._entries) <= 1
    # 新数据应保留
    remaining_texts = [e.text for e in ltm._entries]
    assert "新数据" in remaining_texts
    assert "过期数据" not in remaining_texts


def test_long_term_memory_evicts_overflow():
    """验证超过上限的条目被驱逐（保留最新的）。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    now = time.time()
    ltm._MAX_ENTRIES = 5

    ltm._entries = [
        MemoryEntry(text=f"entry-{i}", metadata={}, timestamp=now - 100 + i)
        for i in range(10)
    ]

    removed = ltm._evict_old_entries()
    assert removed >= 5
    assert len(ltm._entries) <= 5
    # 保留的应是最新的 5 条
    for e in ltm._entries:
        assert int(e.text.split("-")[1]) >= 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest core/tests/test_phase2.py::test_long_term_memory_evicts_old_entries -v
python -m pytest core/tests/test_phase2.py::test_long_term_memory_evicts_overflow -v
```

Expected: FAIL — `_evict_old_entries()` 方法不存在。

- [ ] **Step 3: Add TTL configuration constants and eviction method**

In `core/memory/long_term.py`, add class-level constants and eviction method:

```python
class LongTermMemory:
    # 驱逐策略
    _MAX_ENTRIES = 10000   # 内存列表最大条目数
    _MAX_AGE_DAYS = 90     # 最大保留天数

    # ... existing code ...

    def _evict_old_entries(self) -> int:
        """驱逐过期和超过上限的条目。返回驱逐数量。

        在每次 insert() 后调用，保持内存和 Milvus 健康。
        """
        now = time.time()
        cutoff = now - (self._MAX_AGE_DAYS * 86400)
        removed = 0

        # 移除过期条目
        new_entries = []
        for e in self._entries:
            if e.timestamp >= cutoff:
                new_entries.append(e)
            else:
                removed += 1
        self._entries = new_entries

        # 移除超过上限的条目（保留最新的，按 timestamp 降序）
        if len(self._entries) > self._MAX_ENTRIES:
            self._entries.sort(key=lambda e: e.timestamp, reverse=True)
            overflow = len(self._entries) - self._MAX_ENTRIES
            self._entries = self._entries[:_MAX_ENTRIES]
            removed += overflow

        if removed:
            logger.info("Long-term memory: 驱逐 %d 条过期/超限条目 (剩余 %d)",
                       removed, len(self._entries))
        return removed
```

- [ ] **Step 4: Integrate eviction into insert()**

In `core/memory/long_term.py`, find the `insert()` method and add eviction call at the end:

```python
def insert(self, entry: MemoryEntry) -> str:
    # ... existing insert logic ...
    self._entries.append(entry)
    # 触发驱逐检查
    self._evict_old_entries()
    return entry.id
```

- [ ] **Step 5: Run tests to verify fix**

```bash
python -m pytest core/tests/test_phase2.py -v -k "long_term"
```

Expected: All LongTermMemory tests pass, including the new eviction tests.

- [ ] **Step 6: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "feat: add TTL eviction to LongTermMemory (90 day age cap, 10k entry limit)"
```

---

### Task 3.4: Health check endpoint

**Files:**
- Modify: `Module/input/server.py`

- [ ] **Step 1: Write failing test for /health endpoint**

Create or add to an existing test file. Since the server is an integration component, write a simple unit test for the handler function:

```python
async def test_health_endpoint_returns_json(aiohttp_client):
    """GET /health 应返回 JSON 状态信息。"""
    from Module.input.server import create_app
    import json

    # 需要 mock Agent — 使用简化方式
    class MockAgent:
        class _fsm:
            state = "idle"
        class _llm:
            model = "test-model"
        class _session:
            _messages = []
        class _long_term:
            count = 0
        class _tools:
            tool_names = []
        class _output:
            @staticmethod
            def _ws_count():
                return 0

    app = create_app(MockAgent(), serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert "status" in data
    assert data["status"] in ("ok", "degraded")
    assert "fsm" in data
    assert "llm" in data
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest core/tests/ -k "health" -v
```

Expected: FAIL — `/health` 路由不存在 (404)。

- [ ] **Step 3: Add health check handler**

In `Module/input/server.py`, find the `create_app()` function and add:

```python
from aiohttp import web

# 在 create_app() 内部：
async def health_handler(request):
    """健康检查端点 — Kubernetes / Docker 探针。"""
    agent = request.app["agent"]
    try:
        fsm_state = agent._fsm.state
    except Exception:
        fsm_state = "unknown"

    status_code = 200

    checks = {
        "status": "ok",
        "fsm": fsm_state,
        "llm": getattr(agent._llm, 'model', 'unknown'),
        "memory": {
            "session_count": len(getattr(agent._session, '_messages', [])),
            "long_term_count": getattr(agent._long_term, 'count', 0),
        },
        "tools": getattr(agent._tools, 'tool_names', []),
    }
    return web.json_response(checks, status=status_code)

app.router.add_get("/health", health_handler)
app.router.add_get("/ready", health_handler)
```

- [ ] **Step 4: Run test to verify**

```bash
python -m pytest core/tests/ -k "health" -v
```

Expected: Health endpoint test passes (200 JSON response).

- [ ] **Step 5: Commit**

```bash
git add Module/input/server.py
git commit -m "feat: add /health and /ready endpoints"
```

---

### Task 3.5: TTS + INTERRUPT race condition fix

**Files:**
- Modify: `core/cognitive_loop.py`
- Modify: `static/index.html`
- Reference: `docs/sync-lessons.md`

- [ ] **Step 1: Read sync-lessons.md and current code**

```bash
cat docs/sync-lessons.md
```

Read `core/cognitive_loop.py` — focus on:
- `_handle_interrupt()` or the INTERRUPT handling path
- `_start_tts_stream()` — how TTS tasks are created and tracked
- `_transition_idle()` — how TTS tasks are awaited

Read `static/index.html` — focus on:
- `finalizeDelta()` function
- WebSocket message handler for `delta`, `audio_end`, and `interrupt` types
- How `stream_id` is tracked

- [ ] **Step 2: Write test for the race condition**

```python
async def test_interrupt_during_tts_flushes_bubbles():
    """INTERRUPT 时应发送 flush 信号确保前端完成当前气泡。"""
    from core.cognitive_loop import CognitiveLoop
    # 使用 FakeLLMBackend 模拟正在流式输出 + TTS 中的状态
    # 注入 INTERRUPT → 确认 flush 消息被发送到 output channel
    # ...

    # 具体实现需要根据当前 CognitiveLoop 的测试模式编写。
    # 已有测试可参考:
    #   core/tests/test_phase3.py 中的 TTS 相关测试
    #   core/tests/test_phase1.py 中的 INTERRUPT 相关测试
```

- [ ] **Step 3: Add explicit flush signal on INTERRUPT**

In `core/cognitive_loop.py`, in the INTERRUPT handling:

```python
async def _handle_interrupt(self) -> None:
    """处理 INTERRUPT 信号。"""
    # 1. 发送 flush 信号 — 强制前端完成当前气泡
    if self._tts_total > 0:
        try:
            await self._output.command("flush", {
                "stream_id": f"tts-{self._tts_total}"
            })
        except Exception:
            logger.debug("Flush command failed", exc_info=True)

    # 2. Cancel TTS tasks
    for t in list(self._tts_tasks):
        if not t.done():
            t.cancel()
    self._tts_tasks.clear()

    # 3. Reset buffers
    self._sentence_buffer.reset()
    # ... existing interrupt logic ...
```

- [ ] **Step 4: Add frontend flush handler**

In `static/index.html`, add handler for the `flush` message type:

```javascript
// 在 WebSocket message handler 中添加:
case "flush":
    const flushStreamId = data.stream_id;
    // 立即完成当前 stream_id 的气泡
    finalizeDelta();
    // 清空 audio queue
    audioQueue = [];
    currentAudio = null;
    break;
```

- [ ] **Step 5: Manual verification**

```bash
python app.py --server
```

然后在浏览器中:
1. 发送一条长消息触发 TTS
2. 在 TTS 播放期间点击打断
3. 确认 emoji 不会合并到上一气泡
4. 确认新消息的气泡正常创建

- [ ] **Step 6: Commit**

```bash
git add core/cognitive_loop.py static/index.html core/tests/test_phase3.py
git commit -m "fix: resolve TTS/INTERRUPT emoji race condition with explicit flush signal"
```

---

### Task 3.6: LongTermMemory write-back sync

**Files:**
- Modify: `core/memory/long_term.py`
- Modify: `core/tests/test_phase2.py`

- [ ] **Step 1: Write test for dirty entry tracking**

Add to `core/tests/test_phase2.py`:

```python
def test_long_term_memory_tracks_dirty_entries():
    """Milvus 不可用时插入的条目应标记为 dirty，重连后可同步。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    ltm._milvus_available = False  # 模拟 Milvus 不可用

    entry = MemoryEntry(text="离线数据", metadata={"type": "fact"})
    eid = ltm.insert(entry)

    assert eid in ltm._dirty_ids, "离线条目应标记为 dirty"

    # 模拟 Milvus 恢复
    ltm._milvus_available = True
    # 模拟 embedding 已生成
    entry.embedding = [0.1] * 1024
    synced = ltm._sync_dirty_entries()
    assert synced >= 1
    assert eid not in ltm._dirty_ids

def test_long_term_memory_sync_handles_milvus_still_down():
    """Milvus 仍然不可用时 sync 应安全返回 0。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    ltm._milvus_available = False

    entry = MemoryEntry(text="离线数据", metadata={})
    ltm.insert(entry)

    synced = ltm._sync_dirty_entries()
    assert synced == 0, "Milvus 不可用时应返回 0 而非崩溃"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest core/tests/test_phase2.py -k "dirty" -v
```

Expected: FAIL — `_dirty_ids` 和 `_sync_dirty_entries()` 不存在。

- [ ] **Step 3: Add dirty tracking and sync**

In `core/memory/long_term.py`, add to `__init__`:

```python
self._dirty_ids: set[str] = set()  # 未同步到 Milvus 的条目 ID
```

Update `insert()` to track dirty entries:

```python
def insert(self, entry: MemoryEntry) -> str:
    """插入条目。如果 Milvus 不可用，标记为 dirty 等重连后同步。"""
    self._entries.append(entry)
    self._evict_old_entries()

    if self._milvus_available and entry.embedding:
        try:
            self._collection.insert([entry.to_milvus_dict()])
        except Exception:
            self._dirty_ids.add(entry.id)
    elif not self._milvus_available:
        self._dirty_ids.add(entry.id)

    return entry.id
```

Add sync method:

```python
async def _sync_dirty_entries(self) -> int:
    """尝试将脏条目同步到 Milvus。返回成功同步数。"""
    if not self._milvus_available or not self._dirty_ids:
        return 0
    synced = 0
    for eid in list(self._dirty_ids):
        entry = self._get_by_id(eid)
        if entry is None or not entry.embedding:
            continue
        try:
            self._collection.insert([entry.to_milvus_dict()])
            self._dirty_ids.discard(eid)
            synced += 1
        except Exception:
            break  # Milvus 仍然不可用
    if synced:
        logger.info("Synced %d dirty entries to Milvus", synced)
    return synced
```

Add helper `_get_by_id` if not already present:

```python
def _get_by_id(self, entry_id: str):
    """通过 ID 查找条目。"""
    for e in self._entries:
        if e.id == entry_id:
            return e
    return None
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest core/tests/test_phase2.py -v -k "dirty or long_term"
```

Expected: All related tests pass.

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "feat: track and sync dirty entries when Milvus reconnects"
```

---

### Task 3.7: MemoryConsolidator throttling

**Files:**
- Modify: `core/consolidator.py`
- Modify: `core/tests/test_phase3.py`

- [ ] **Step 1: Write test for concurrent consolidation prevention**

Add to `core/tests/test_phase3.py`:

```python
async def test_consolidator_prevents_concurrent_runs():
    """快速连续触发 consolidate 时只应有一个 LLM 调用。"""
    from core.consolidator import MemoryConsolidator
    from core.memory.long_term import LongTermMemory

    call_count = 0

    class CountingLLM:
        async def think(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)
            return "[]"

    ltm = LongTermMemory()
    consolidator = MemoryConsolidator(
        llm=CountingLLM(),
        memory=ltm,
        character_name="Test",
    )
    # 手动设置 buffer 满
    consolidator._buffer = [
        ("msg1", "resp1"), ("msg2", "resp2"), ("msg3", "resp3"),
        ("msg4", "resp4"), ("msg5", "resp5"), ("msg6", "resp6"),
    ]

    # 并发触发 3 次 consolidate
    tasks = [
        asyncio.create_task(consolidator.consolidate("u", "a"))
        for _ in range(3)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    assert call_count == 1, f"应只有 1 次 LLM 调用，实际 {call_count} 次"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest core/tests/test_phase3.py::test_consolidator_prevents_concurrent_runs -v
```

Expected: FAIL — 多次并发触发导致多次 LLM 调用。

- [ ] **Step 3: Add consolidation lock**

In `core/consolidator.py`, add `asyncio.Lock`:

```python
import asyncio

class MemoryConsolidator:
    def __init__(self, llm, memory, character_name):
        # ... existing init ...
        self._consolidate_lock = asyncio.Lock()

    async def consolidate(self, user_msg, assistant_msg) -> bool:
        """触发记忆沉淀。如果已有任务在运行则跳过本轮。"""
        self._buffer.append((user_msg, assistant_msg))
        if len(self._buffer) < self._threshold:
            return False

        if self._consolidate_lock.locked():
            logger.debug("Consolidation skipped — previous run still active")
            return False

        async with self._consolidate_lock:
            return await self._do_consolidate()
```

**注意:** 需要把现有的 consolidate 逻辑重命名为 `_do_consolidate()` 或直接在 `async with` 块内执行。

- [ ] **Step 4: Run tests**

```bash
python -m pytest core/tests/test_phase3.py -v -k "consolid"
```

Expected: All consolidator tests pass, including the new concurrency test.

- [ ] **Step 5: Commit**

```bash
git add core/consolidator.py core/tests/test_phase3.py
git commit -m "fix: add consolidation lock to prevent concurrent LLM calls"
```

---

### Task 3.8: Frontend UX improvements

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add hover copy button to message bubbles**

In `static/index.html`, add CSS and a copy button to each message bubble:

CSS (添加到 `<style>` 块):

```css
.message-bubble {
    position: relative;
}
.copy-btn {
    display: none;
    position: absolute;
    top: 6px;
    right: 6px;
    background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.2);
    border-radius: 6px;
    color: #aaa;
    padding: 2px 8px;
    font-size: 0.75rem;
    cursor: pointer;
    backdrop-filter: blur(4px);
    transition: all 0.15s;
}
.copy-btn:hover {
    background: rgba(255,255,255,0.25);
    color: #fff;
}
.copy-btn.copied {
    color: #4ade80;
    border-color: #4ade80;
}
.message-bubble:hover .copy-btn {
    display: block;
}
```

JS: Add click handler — append copy button at message creation time, or use event delegation:

```javascript
// 在 message bubble 创建时添加:
const copyBtn = document.createElement('span');
copyBtn.className = 'copy-btn';
copyBtn.textContent = '复制';
copyBtn.onclick = async (e) => {
    e.stopPropagation();
    const text = bubble.textContent.replace('复制', '').trim();
    await navigator.clipboard.writeText(text);
    copyBtn.textContent = '已复制';
    copyBtn.classList.add('copied');
    setTimeout(() => {
        copyBtn.textContent = '复制';
        copyBtn.classList.remove('copied');
    }, 2000);
};
bubble.appendChild(copyBtn);
```

- [ ] **Step 2: Add send status animation**

CSS (添加到 `<style>` 块):

```css
.send-btn {
    transition: all 0.2s;
}
.send-btn.sent {
    animation: send-flash 0.4s ease;
}
@keyframes send-flash {
    0% { transform: scale(1); }
    30% { transform: scale(0.85); }
    60% { transform: scale(1.08); }
    100% { transform: scale(1); }
}
.send-btn.sent::after {
    content: '✓';
    position: absolute;
    top: -6px;
    right: -6px;
    background: #4ade80;
    color: #000;
    border-radius: 50%;
    width: 16px;
    height: 16px;
    font-size: 0.6rem;
    display: flex;
    align-items: center;
    justify-content: center;
    animation: check-pop 0.3s ease 0.1s both;
}
@keyframes check-pop {
    0% { transform: scale(0); }
    100% { transform: scale(1); }
}
```

JS: Add class on send, remove after animation:

```javascript
sendBtn.classList.add('sent');
sendBtn.disabled = true;
setTimeout(() => {
    sendBtn.classList.remove('sent');
    sendBtn.disabled = false;
}, 800);
```

- [ ] **Step 3: Manual verification**

```bash
python app.py --server
```

在浏览器中:
1. 鼠标悬停消息气泡 → 确认复制按钮出现
2. 点击复制 → 确认文本被复制，按钮文字变为"已复制"
3. 发送消息 → 确认发送按钮有动画反馈

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat: add hover copy button and send status animation"
```

---

## Phase 3 Verification

```bash
# 全量测试
python -m pytest Module/ core/ -v

# 集成测试
python app.py --server
# 浏览器验证:
# - curl http://localhost:8765/health
# - 快速连续对话 → 确认 LLM 重试有效
# - 打断 TTS → 确认气泡不混乱
# - hover 消息 → 确认复制按钮
# - 发送 → 确认动画
```
