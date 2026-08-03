# Fix: Agent stuck after restart — FSM + LLM client recovery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix agent becoming permanently stuck after restart — FSM not reset to IDLE, LLM HTTP client closed but not recreated, and no timeout on API calls.

**Architecture:** Three fixes: (1) reset FSM to IDLE on `start()`, (2) recreate the OpenAI HTTP client in `start()` via a `reconnect()` method, (3) add a 60s timeout to LLM API calls so unresponsive endpoints don't hang indefinitely.

**Tech Stack:** Python 3.13, asyncio, httpx, openai SDK

---

### Task 1: Reset FSM to IDLE on start()

**Files:**
- Modify: `core/agent_fsm.py` (add `reset()`)
- Modify: `core/agent.py:142` (call reset in `start()`)

- [ ] **Step 1: Add `reset()` method to AgentFSM**

In `core/agent_fsm.py`, add after `interrupt()` (line 46):

```python
def reset(self) -> None:
    """Force reset to IDLE. Used on service restart to clear any stuck state."""
    self._previous = self._state
    self._state = AgentState.IDLE
    logger.debug("FSM: reset %s → IDLE", self._previous)
```

- [ ] **Step 2: Call `reset()` in `Agent.start()`**

In `core/agent.py`, at the beginning of `start()` (after line 144 `self._state = RunState.RUNNING`):

```python
# Reset FSM — may be stuck from a previous run that was interrupted
self._fsm.reset()
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest core/tests/ -v -k fsm 2>&1 | tail -15
```

- [ ] **Step 4: Commit**

```bash
git add core/agent_fsm.py core/agent.py
git commit -m "fix: reset FSM to IDLE on agent start after restart"
```

---

### Task 2: Recreate LLM HTTP client in start()

**Files:**
- Modify: `Module/llm/openai_compat.py` (store init params, add `reconnect()`)
- Modify: `core/llm_adapter.py` (add `reconnect()`)
- Modify: `core/agent.py:142` (call `reconnect()` in `start()`)

`stop()` continues to close the LLM client (line 286). `start()` now recreates it via `reconnect()`. On fresh start (no prior `stop()`), `reconnect()` closes the old client and creates a new one — negligible overhead since it runs once.

- [ ] **Step 1: Store init params and add `reconnect()` to `OpenAICompatBackend`**

In `Module/llm/openai_compat.py`, in `__init__()`, store the params (after line 38):

```python
self._api_key = key
self._base_url = base_url
self._beta_url = beta_url
```

Then add `reconnect()` after `close()` (line 168):

```python
async def reconnect(self) -> None:
    """Recreate HTTP clients — used on service restart after stop() closed them."""
    import httpx
    # Close old client if still open
    try:
        await self._client.close()
    except Exception:
        pass
    if self._beta_client:
        try:
            await self._beta_client.close()
        except Exception:
            pass
    # Create new clients with timeout
    timeout = httpx.Timeout(60.0, connect=10.0)
    self._client = AsyncOpenAI(
        api_key=self._api_key,
        base_url=self._base_url,
        timeout=timeout,
    )
    if self._beta_url:
        self._beta_client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._beta_url,
            timeout=timeout,
        )
```

- [ ] **Step 2: Add `reconnect()` to `LLMAdapter`**

In `core/llm_adapter.py`, add after `close()` (line 133):

```python
async def reconnect(self) -> None:
    """Recreate the underlying backend's HTTP client. Used on agent restart."""
    if hasattr(self._backend, 'reconnect'):
        await self._backend.reconnect()
```

- [ ] **Step 3: Call `reconnect()` in `Agent.start()`**

In `core/agent.py`, in `start()` after the FSM reset (from Task 1):

```python
# Recreate LLM HTTP client (may have been closed by previous stop())
await self._llm.reconnect()
```

The `CognitiveLoop` and `MemoryConsolidator` hold references to the same `LLMAdapter` instance — since `reconnect()` modifies the instance in-place, no reference updates needed.

- [ ] **Step 4: Run tests**

```bash
python -m pytest core/tests/ core/input/test_server.py -v 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add core/llm_adapter.py Module/llm/openai_compat.py core/agent.py
git commit -m "fix: recreate LLM HTTP client on agent restart via reconnect()"
```

---

### Task 3: Add timeout to LLM HTTP client

**Files:**
- Modify: `Module/llm/openai_compat.py:39,42`

- [ ] **Step 1: Add timeout to AsyncOpenAI creation**

In `Module/llm/openai_compat.py`, add `import httpx` at the top, then change lines 39 and 42:

```python
# Line 39 — current:
self._client = AsyncOpenAI(api_key=key, base_url=base_url)

# New:
_timeout = httpx.Timeout(60.0, connect=10.0)
self._client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=_timeout)

# Line 42 — current:
self._beta_client = AsyncOpenAI(api_key=key, base_url=beta_url)

# New:
self._beta_client = AsyncOpenAI(api_key=key, base_url=beta_url, timeout=_timeout)
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest Module/llm/test_llm.py -v 2>&1 | tail -15
```

- [ ] **Step 3: Commit**

```bash
git add Module/llm/openai_compat.py
git commit -m "fix: add 60s timeout to LLM HTTP client to prevent indefinite hangs"
```

---

### Task 4: End-to-end verification

- [ ] **Step 1: Simulate restart scenario**

Start the server, send a message and confirm response. Then trigger restart via `/restart`. Send another message — should receive response normally (not hang, not queue).

- [ ] **Step 2: Test FSM recovery**

After restart, the `/debug` page should show FSM state as `idle` (not stuck in `thinking`).

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest core/tests/ core/input/test_server.py Module/llm/test_llm.py -v 2>&1 | tail -10
```
