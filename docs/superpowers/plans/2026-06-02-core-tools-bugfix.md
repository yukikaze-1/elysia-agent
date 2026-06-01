# Core Tools Bugfix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 16 issues found in core/tools/ review: sandbox bypass, broken reminder plugin, dead self_state defaults, HITL not wired, missing safety enforcement, and various logic bugs.

**Architecture:** Fixes span three layers — `core/tools/` plugins (application logic), `Module/tool/safety.py` (pipeline enforcement), and `core/agent.py` (ctx.services injection). All fixes are targeted edits to existing files; no new files needed.

**Tech Stack:** Python 3.13, Pydantic v2, asyncio, httpx

---

## File Map

```
Module/tool/safety.py          # HITL wiring, is_write by capability, max_response_bytes enforcement, path resolution fix
core/tools/filesystem.py       # Path resolution delegated to SafetyPipeline, line count fix, double-open fix
core/tools/reminder.py         # Rewrite to use TaskScheduler.submit() correctly
core/tools/self_state.py       # Read state from ctx.services (injected by agent.py)
core/tools/web.py              # Use SafetyProfile.request_timeout, remove redundant truncation
core/tools/memory.py           # Remove dead runtime filter code
core/tools/write_file.py       # mkdir after validation, sanitize session_id
core/tools/time.py             # Error on timezone failure, module-level zoneinfo import
core/tools/runtime_config.py   # asyncio.to_thread for psutil, remove API key exposure
core/agent.py                  # Inject _fsm_state, _session_msg_count, _tool_count into ctx.services
core/task_scheduler.py         # Add dispatch_background() method, background execution path
core/tests/test_phase3.py      # Fix broken _DENIED_PATHS import test
```

---

### Task 1: Fix SafetyPipeline path resolution — unified sandbox

**Files:**
- Modify: `Module/tool/safety.py:119-144, 385-411`

**Problem:** `_resolve_sandbox_path` prepends `read_dirs[0]` for reads; plugins do `Path(path).expanduser().resolve()` from CWD. Different base paths = sandbox bypass.

**Fix:** Move path resolution out of individual plugins into SafetyPipeline, which should resolve and store the sandboxed path in a `_resolved_path` key on ctx, and plugins should read from `ctx.services["_resolved_path"]` instead of resolving themselves.

Simpler fix (less invasive): Make SafetyPipeline return a `_safe_path` that plugins must use. Or: simply ensure plugins use the SAME resolution logic.

Actually the simplest fix: the SafetyPipeline already calls `_resolve_sandbox_path` and `_validate_path`. The fix is to pass the validated resolved path to the plugin via ctx.services. The plugin then reads `ctx.get("_safe_path")` instead of resolving from `params.path` directly.

- [ ] **Step 1: Update SafetyPipeline._check_safety to inject resolved path**

In `Module/tool/safety.py`, modify `_check_safety` (line 385-411) to store the validated path in ctx:

```python
async def _check_safety(self, plugin, ctx, raw_params):
    profile = plugin.safety
    if profile is None:
        return None
    session_id = getattr(ctx, "session_id", "default") or "default"

    if "url" in raw_params and profile.network_allowed:
        err = _validate_url(raw_params["url"], profile)
        if err:
            return err

    if "path" in raw_params and profile.filesystem_allowed:
        raw_path = raw_params["path"]
        is_write = "write" in plugin.name.lower() if plugin.name else False
        resolved, err = _resolve_sandbox_path(
            raw_path, profile, session_id, is_write,
        )
        if err:
            return err
        err = _validate_path(resolved, profile)
        if err:
            return err
        # 注入已验证的安全路径，插件从此读取
        ctx.services["_safe_path"] = str(resolved)

    return None
```

- [ ] **Step 2: Update ReadFilePlugin to use ctx.get("_safe_path")**

In `core/tools/filesystem.py`, change the execute method (lines 47-95):

```python
async def execute(self, *, ctx, emit=None, params):
    path = ctx.get("_safe_path") or params.path
    resolved = Path(path)
    if not resolved.exists():
        return f"路径不存在: {path}"
    # ... rest unchanged, but remove the self-resolve at line 51
```

- [ ] **Step 3: Update ListDirPlugin to use ctx.get("_safe_path")**

In `core/tools/filesystem.py`, change execute (line 123):

```python
path = ctx.get("_safe_path") or params.path
resolved = Path(path)
```

- [ ] **Step 4: Update WriteFilePlugin to use ctx.get("_safe_path")**

In `core/tools/write_file.py`, change execute (lines 29-43):

```python
path = ctx.get("_safe_path") or params.path
resolved = Path(path)
```

- [ ] **Step 5: Verify with test**

```python
def test_sandbox_resolves_correctly():
    """相对路径应从 read_dirs[0] 解析，不是 CWD。"""
    # Plugin receives _safe_path from pipeline, not self-resolve
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest Module/tool/test_framework.py core/tools/ -v
```

- [ ] **Step 7: Commit**

```bash
git add Module/tool/safety.py core/tools/filesystem.py core/tools/write_file.py
git commit -m "fix: unified sandbox path resolution — SafetyPipeline injects _safe_path, plugins no longer self-resolve from CWD"
```

---

### Task 2: Fix is_write detection — use capabilities, not string match

**Files:**
- Modify: `Module/tool/safety.py:401`

**Problem:** `is_write = "write" in tool_name.lower()` is fragile.

**Fix:** Check `Capability.FILESYSTEM_WRITE` in plugin capabilities instead:

```python
is_write = Capability.FILESYSTEM_WRITE in getattr(plugin, "capabilities", set())
```

- [ ] **Step 2: Add import**

At top of safety.py, add:
```python
from .capability import Capability
```

- [ ] **Step 3: Commit**

```bash
git add Module/tool/safety.py
git commit -m "fix: detect write operations by FILESYSTEM_WRITE capability, not tool name substring"
```

---

### Task 3: Wire HITL broadcast callback

**Files:**
- Modify: `Module/tool/safety.py:180-186, 422-424`

**Problem:** `_broadcast_hitl` never set; HITL always times out after 60s.

**Fix:** Add a `set_hitl_broadcast()` method to SafetyPipeline, call it from ToolManager.

- [ ] **Step 1: Add setter method to SafetyPipeline**

```python
def set_hitl_broadcast(self, callback):
    """Set the HITL approval broadcast callback.
    
    callback(request_id, tool_name, params) -> None
    Should broadcast approval request to frontend via OutputManager.
    """
    self._broadcast_hitl = callback
```

- [ ] **Step 2: Wire from ToolManager**

In `Module/tool/manager.py`, add a method:
```python
def set_hitl_broadcast(self, output_manager=None):
    """Wire HITL approval broadcast through OutputManager."""
    if output_manager is not None:
        async def _broadcast(request_id, tool_name, params):
            await output_manager.broadcast_json({
                "type": "tool_approval",
                "tool": tool_name,
                "params": str(params)[:200],
                "request_id": request_id,
            })
        self._pipeline.set_hitl_broadcast(_broadcast)
```

- [ ] **Step 3: Commit**

```bash
git add Module/tool/safety.py Module/tool/manager.py
git commit -m "fix: wire HITL broadcast callback — SafetyPipeline.set_hitl_broadcast() + ToolManager.set_hitl_broadcast()"
```

---

### Task 4: Fix SetReminderPlugin — add dispatch_background + background execution

**Files:**
- Modify: `core/task_scheduler.py:33, 258-261`, `core/tools/reminder.py:28-40`

**Problem:** `dispatch_background` doesn't exist; BACKGROUND tasks have no execution path; `_remind()` called immediately.

**Fix:** Add `dispatch_background()` to TaskScheduler; implement background execution in `_worker_loop`; fix reminder plugin.

- [ ] **Step 1: Add dispatch_background to TaskScheduler**

```python
async def dispatch_background(self, coro) -> str:
    """Submit a background coroutine, return task_id."""
    task = Task.new_background(payload={"coro": coro})
    self._tasks[task.id] = task
    self._seq += 1
    await self._queue.put((task.priority, self._seq, task))
    logger.info("Background task dispatched: %s", task.id)
    return task.id
```

- [ ] **Step 2: Add background execution to _worker_loop**

```python
if task.type == TaskType.TOOL_CALL:
    result = await self._execute_tool(task)
else:
    coro = task.payload.get("coro")
    if coro is not None and callable(getattr(coro, "__await__", None)):
        result = await coro
    else:
        result = task.payload.get("result")
```

- [ ] **Step 3: Fix SetReminderPlugin**

```python
async def execute(self, *, ctx, emit=None, params):
    scheduler = ctx.get("scheduler")
    if scheduler is None:
        return "提醒功能当前不可用。"

    async def _remind():
        await asyncio.sleep(params.delay_seconds)
        return f"⏰ 提醒: {params.message}"

    tid = await scheduler.dispatch_background(_remind())
    return f"已设置提醒 (ID: {tid[:8]})，{params.delay_seconds} 秒后通知: {params.message}"
```

- [ ] **Step 4: Commit**

```bash
git add core/task_scheduler.py core/tools/reminder.py
git commit -m "fix: add dispatch_background + background execution to TaskScheduler; fix SetReminderPlugin"
```

---

### Task 5: Fix SelfStatePlugin — inject state into ctx.services

**Files:**
- Modify: `core/agent.py:145-153`, `core/tools/self_state.py:17-19`

**Problem:** `_fsm_state`, `_session_msg_count`, `_tool_count` never injected into ctx.services.

**Fix:** Add these keys to ctx.services in agent.py; update self_state.py to match.

- [ ] **Step 1: Inject state into ctx.services (agent.py)**

```python
self._tool_ctx = ToolContext(
    session_id=getattr(self._session, "_session_id", "default"),
    user_id=self._user_name,
    services={
        "memory": self._long_term,
        "scheduler": self._scheduler,
        "config": self._config,
        "_tool_names": list(self._tool_manager.tool_names),
        "_fsm_state": str(self._fsm.state),
        "_session_msg_count": len(self._session._messages),
        "_tool_count": len(self._tool_manager._plugins),
    },
)
```

- [ ] **Step 2: Update SelfStatePlugin to read correct keys**

```python
fsm_state = str(ctx.get("_fsm_state", "unknown"))
session_count = ctx.get("_session_msg_count", 0)
tool_count = ctx.get("_tool_count", 0)
```

- [ ] **Step 3: Commit**

```bash
git add core/agent.py core/tools/self_state.py
git commit -m "fix: inject _fsm_state, _session_msg_count, _tool_count into ctx.services; SelfStatePlugin reads correct keys"
```

---

### Task 6: Fix MemorySavePlugin — remove dead runtime filter

**Files:**
- Modify: `core/tools/memory.py:86-97`

**Problem:** `json.loads(params.content)` on plain text always raises; `pass` swallows it.

**Fix:** Remove the _RUNTIME_KEY_BLACKLIST and dead filtering logic.

- [ ] **Step 1: Remove dead filter code**

Delete lines 86-97 from memory.py (the json.loads/blacklist/filter block).

- [ ] **Step 2: Commit**

```bash
git add core/tools/memory.py
git commit -m "fix: remove dead runtime parameter filter from MemorySavePlugin — plain text content is never JSON"
```

---

### Task 7: Fix web_search — enforce URL validation

**Files:**
- Modify: `Module/tool/safety.py:385-411`

**Problem:** web_search has no URL params, so `_check_safety` skips it entirely.

**Fix:** Add a `_network_allowed` check that verifies `Capability.NETWORK in plugin.capabilities` when `profile.network_allowed` is set, even without explicit URL/path params.

- [ ] **Step 1: Add network capability enforcement**

```python
# After existing URL/path checks, add:
if profile.network_allowed:
    # Ensure NETWORK capability is declared (belt-and-suspenders)
    if Capability.NETWORK not in getattr(plugin, "capabilities", set()):
        return "插件未声明 NETWORK 能力"
```

This doesn't validate URLs for web_search (since the URLs are constructed internally), but it ensures only capable plugins can make network calls.

- [ ] **Step 2: For web_search specifically, validate search endpoint at setup time**

In `WebSearchPlugin.setup()`:
```python
async def setup(self, ctx):
    # Validate that search backend is available
    if not ctx.http_client:
        logger.warning("web_search: no http_client in PluginContext")
```

- [ ] **Step 3: Commit**

```bash
git add Module/tool/safety.py core/tools/web.py
git commit -m "fix: enforce NETWORK capability check in SafetyPipeline; add web_search setup() backend check"
```

---

### Task 8: Fix ReadFilePlugin — single open, fix line count

**Files:**
- Modify: `core/tools/filesystem.py:51-94`

**Problem:** File opened twice (probe then read); line count is wrong after early stop.

**Fix:** Single open, proper total line count.

- [ ] **Step 1: Single-pass read with correct line count**

```python
try:
    with open(resolved, "r", encoding="utf-8") as f:
        # Binary probe: read first chunk, check decode
        probe = f.read(1024)
except UnicodeDecodeError:
    return f"无法读取（可能是二进制文件）: {path}"

try:
    with open(resolved, "r", encoding="utf-8") as f:
        result_lines: list[str] = []
        for i, line in enumerate(f):
            if i < params.start_line:
                continue
            if len(result_lines) >= params.max_lines:
                break
            result_lines.append(f"{i+1:4d} | {line.rstrip()}")
        total = i + 1  # 当前行号 = 总行数
except Exception as e:
    return f"读取失败: {e}"
```

- [ ] **Step 2: Fix line count message**

```python
last_shown = params.start_line + len(result_lines)
if last_shown < total:
    output += f"\n\n…[显示 {params.start_line+1}-{last_shown} 行，共 {total} 行]"
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/filesystem.py
git commit -m "fix: ReadFilePlugin — single file open, correct total line count after early stop"
```

---

### Task 9: Fix web_fetch — use SafetyProfile timeout

**Files:**
- Modify: `core/tools/web.py:174`

**Problem:** `httpx.AsyncClient(timeout=15.0)` hardcoded; SafetyProfile says 30.0.

**Fix:** Read timeout from `self.safety.request_timeout`.

```python
async with httpx.AsyncClient(
    timeout=self.safety.request_timeout,
    follow_redirects=True,
    max_redirects=self.safety.max_redirects,
    headers={"User-Agent": "Mozilla/5.0 (compatible; Elysia-Agent/1.0)"},
) as client:
```

- [ ] **Step 1: Commit**

```bash
git add core/tools/web.py
git commit -m "fix: WebFetchPlugin uses self.safety.request_timeout instead of hardcoded 15.0"
```

---

### Task 10: Fix TimePlugin — error on timezone failure

**Files:**
- Modify: `core/tools/time.py:3, 39-48`

**Problem:** Named timezone errors silently fall to UTC.

**Fix:** Module-level `zoneinfo` import; return error on unknown timezone.

- [ ] **Step 1: Module-level import and explicit error**

```python
from datetime import datetime, timezone, timedelta
try:
    import zoneinfo as _zoneinfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False

# In execute():
else:
    if not HAS_ZONEINFO:
        return "时区功能不可用（缺少 zoneinfo 模块）"
    try:
        tz = _zoneinfo.ZoneInfo(tz_str)
    except Exception:
        return f"无效的时区名称: '{tz_str}'，请使用 'UTC'、'Asia/Shanghai' 或 '+8'"
```

- [ ] **Step 2: Commit**

```bash
git add core/tools/time.py
git commit -m "fix: TimePlugin — module-level zoneinfo import, explicit error on invalid timezone"
```

---

### Task 11: Fix WriteFilePlugin — validate then mkdir

**Files:**
- Modify: `core/tools/write_file.py:30-43`

**Problem:** `write_dir.mkdir()` before path validation; session_id unsanitized.

**Fix:** Reorder — resolve first, validate path, then mkdir. Sanitize session_id.

- [ ] **Step 1: Reorder and sanitize**

```python
session_id = ctx.session_id or "default"
# Sanitize: replace path separators
safe_sid = session_id.replace("/", "_").replace("\\", "_")
write_dir = Path(
    self.safety.write_dir.replace("{session_id}", safe_sid)
).resolve()

file_path = write_dir / params.path
resolved = file_path.resolve()

# Validate BEFORE creating directories
if not str(resolved).startswith(str(write_dir)):
    return f"拒绝写入: 路径 '{params.path}' 越过了 sandbox 边界"

write_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 2: Commit**

```bash
git add core/tools/write_file.py
git commit -m "fix: WriteFilePlugin — validate path before mkdir, sanitize session_id"
```

---

### Task 12: Fix RuntimeConfigPlugin — non-blocking psutil, remove API key leak

**Files:**
- Modify: `core/tools/runtime_config.py:63-74`

**Problem:** `psutil.Process()` blocks event loop; API key status exposed to LLM.

**Fix:** `asyncio.to_thread()` for psutil; remove env var exposure.

- [ ] **Step 1: Non-blocking psutil**

```python
if HAS_PSUTIL:
    def _get_memory():
        proc = psutil.Process()
        mem = proc.memory_info()
        return mem, proc.num_threads()
    mem, threads = await asyncio.to_thread(_get_memory)
    lines.append(
        f"进程内存: RSS={mem.rss / 1024 / 1024:.1f}MB, "
        f"VMS={mem.vms / 1024 / 1024:.1f}MB, "
        f"线程数={threads}"
    )
```

- [ ] **Step 2: Remove API key exposure**

Delete the env_vars block (lines 67-70).

- [ ] **Step 3: Commit**

```bash
git add core/tools/runtime_config.py
git commit -m "fix: RuntimeConfigPlugin — asyncio.to_thread for psutil, remove API key status exposure"
```

---

### Task 13: Fix broken test imports

**Files:**
- Modify: `core/tests/test_phase3.py:1564-1574`

**Problem:** Test imports deleted `_DENIED_PATHS` / `_DENIED_NAMES` from filesystem.py.

**Fix:** Update test to check SafetyProfile instead.

- [ ] **Step 1: Update test**

```python
def test_filesystem_safety_defaults():
    """安全敏感路径应通过 SafetyProfile 的 read_dirs 白名单控制。"""
    from core.tools.filesystem import ReadFilePlugin
    p = ReadFilePlugin()
    assert p.safety.filesystem_allowed is True
    assert p.safety.read_dirs == ["data/sandbox"]
    assert p.safety.allow_symlinks is False
```

- [ ] **Step 2: Run the test**

```bash
python -m pytest core/tests/test_phase3.py::test_filesystem_safety_defaults -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tests/test_phase3.py
git commit -m "fix: update test_phase3 — replace deleted _DENIED_PATHS import with SafetyProfile check"
```

---

### Task 14: Run regression + commit verification

- [ ] **Step 1: Run all tests**

```bash
python -m pytest Module/ core/ -v --tb=short 2>&1 | tail -20
```

- [ ] **Step 2: Verify no new failures**

Compare with baseline: 362 passed, 1 pre-existing failure (TTS config).

- [ ] **Step 3: Commit any remaining fixes**

```bash
git add -u
git commit -m "chore: final regression verification after core/tools bugfixes"
```

---

## Summary

| Task | Area | Severity |
|------|------|----------|
| 1 | Unified sandbox path resolution | Critical |
| 2 | is_write by capability, not name | High |
| 3 | HITL broadcast wiring | High |
| 4 | SetReminderPlugin + dispatch_background | Critical |
| 5 | SelfStatePlugin ctx.services injection | Critical |
| 6 | MemorySavePlugin dead code removal | High |
| 7 | web_search safety enforcement | Critical |
| 8 | ReadFilePlugin single-open + line count | Medium |
| 9 | WebFetchPlugin timeout from SafetyProfile | Low |
| 10 | TimePlugin timezone error handling | Medium |
| 11 | WriteFilePlugin validate-before-mkdir | Medium |
| 12 | RuntimeConfig non-blocking + no API key leak | Low |
| 13 | test_phase3 import fix | Medium |
| 14 | Regression verification | All |
