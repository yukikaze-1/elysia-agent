# Review Fixes — Phase 1: Foundation Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Master plan:** `docs/superpowers/plans/2026-06-01-review-master-plan.md`
> **Review report:** `docs/review-2026-06-01.md`

**Goal:** Fix the highest-priority code quality and safety issues found in the comprehensive project review.

**Architecture:** This plan covers 6 independent areas: dependency management, demo data labeling, Task model dead code removal + retry fix, message queue bounding, filesystem sandbox hardening, and API key documentation. Each task is self-contained and can be committed independently.

**Tech Stack:** Python 3.13, asyncio, dataclasses, pip

---

### Task 1: Create requirements.txt

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Write requirements.txt**

Extract dependencies from the codebase's import statements. The project uses:
- `aiohttp` — WebSocket server + HTTP client
- `httpx` — HTTP client (LLM backends)
- `openai` — OpenAI-compatible API client
- `python-dotenv` — .env loading
- `pymilvus` — Milvus vector DB client (optional, gracefully degraded)
- `motor` — MongoDB async client (optional, gracefully degraded)
- `jinja2` or `Jinja2` — prompt template rendering
- `pyyaml` — YAML config parsing
- `ddgs` — DuckDuckGo search (optional, for web_search tool)
- `numpy` — embedding math
- `sentence-transformers` or `huggingface-hub` — local embedding (optional)
- `webrtcvad` or `webrtc-vad` — VAD for STT

```text
# Core (required)
aiohttp>=3.9
httpx>=0.27
openai>=1.0
python-dotenv>=1.0
Jinja2>=3.1
PyYAML>=6.0

# Database (optional — app gracefully degrades without them)
pymilvus>=2.4
motor>=3.5

# Tools (optional)
ddgs>=6.0

# Embedding (optional — HuggingFace local embedding)
sentence-transformers>=2.7

# STT/VAD (optional)
webrtc-vad>=2.0
```

- [ ] **Step 2: Verify file is parseable**

```bash
pip install -r requirements.txt --dry-run 2>&1 | head -5
```

Expected: pip checks for conflicts without errors.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add requirements.txt with dependency declarations"
```

---

### Task 2: Label _seed_memories() as demo scaffolding

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: Add docstring note to _seed_memories()**

In `core/agent.py`, find the `_seed_memories()` method (around line 455) and add a docstring:

```python
async def _seed_memories(self) -> None:
    """Seed long-term memory with demo entries.

    NOTE: These are demo scaffolding entries that bypass the normal
    insert() path (no embedding). Replace with real seed data or remove
    for production deployment.
    """
    entries = [
        # ... existing entries unchanged ...
    ]
```

- [ ] **Step 2: Verify the change is correct**

```bash
python -c "import core.agent; help(core.agent.Agent._seed_memories)" 2>&1 | head -10
```

Expected: Shows the docstring.

- [ ] **Step 3: Commit**

```bash
git add core/agent.py
git commit -m "docs: label _seed_memories() as demo scaffolding"
```

---

### Task 3: Fix can_retry() status check

**Files:**
- Modify: `core/task.py`
- Modify: `core/tests/test_task_model.py`

- [ ] **Step 1: Write failing test for can_retry() status check**

Add to `core/tests/test_task_model.py`:

```python
def test_can_retry_requires_non_terminal_status():
    """can_retry() 应对终态任务返回 False。"""
    from core.task import Task, TaskType, TaskStatus

    # COMPLETED with retries remaining — should still return False
    t = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t.status = TaskStatus.COMPLETED
    assert not t.can_retry(), "COMPLETED task should not be retryable"

    # FAILED with retries remaining — should return True
    t2 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t2.status = TaskStatus.FAILED
    assert t2.can_retry(), "FAILED task with retries remaining should be retryable"

    # CANCELLED — should return False
    t3 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t3.status = TaskStatus.CANCELLED
    assert not t3.can_retry(), "CANCELLED task should not be retryable"

    # PENDING — should return True if under max_retries
    t4 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    assert t4.can_retry(), "PENDING task should be retryable"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest core/tests/test_task_model.py::test_can_retry_requires_non_terminal_status -v
```

Expected: FAIL — `can_retry()` returns True for COMPLETED tasks.

- [ ] **Step 3: Fix can_retry()**

In `core/task.py`, fix `can_retry()`:

```python
def can_retry(self) -> bool:
    """只有非终态或 FAILED 状态且未达重试上限才能重试。"""
    if self.status == TaskStatus.CANCELLED:
        return False
    if self.status == TaskStatus.COMPLETED:
        return False
    return self.retry_count < self.max_retries
```

Note: `depends_on` 和 `run_in_background` 字段保留不动 — 它们是预留字段，标注在 todo.md 的"后台任务系统 — 后续迭代"中，等 BACKGROUND 任务和 DAG 依赖解析实现时会用到。

- [ ] **Step 4: Run tests to verify fix**

```bash
python -m pytest core/tests/test_task_model.py -v
```

Expected: All Task model tests pass. The new `test_can_retry_requires_non_terminal_status` passes.

- [ ] **Step 5: Commit**

```bash
git add core/task.py core/tests/test_task_model.py
git commit -m "fix: can_retry() returns False for COMPLETED and CANCELLED tasks"
```

---

### Task 4: Add capacity limit to _pending_messages queue

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: Add max_pending_messages to AppConfig**

In `core/app_config.py`, add `max_pending_messages` to `AgentConfig` (dataclass):

```python
@dataclass
class AgentConfig:
    character: str = ConfigField(default="character/elysia.yaml")
    user_name: str = ConfigField(default="妖梦")
    session_id: str = ConfigField(default="default")
    max_pending_messages: int = ConfigField(default=32)
```

In `config.yaml`, add the field:

```yaml
agent:
  max_pending_messages: 32
```

- [ ] **Step 2: Add queue bounds check in process_message()**

In `core/agent.py`, modify the busy-state queue branch in `process_message()`:

```python
if self._fsm.state != AgentState.IDLE:
    # Queue message instead of interrupting
    max_queue = self._config.agent.max_pending_messages
    if len(self._pending_messages) >= max_queue:
        logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
        return
    logger.info("Agent busy — 消息排队 (state=%s, queue=%d)",
               self._fsm.state, len(self._pending_messages) + 1)
    self._pending_messages.append({
        "text": text, "user_name": user_name, "request_id": rid,
    })
    return
```

- [ ] **Step 3: Verify**

No tests directly test pending_messages cap, but verify the app still starts:

```bash
python -c "from core.app_config import AppConfig, ConfigLoader; c = ConfigLoader(AppConfig, yaml_path='config.yaml', yaml_key='Agent').load(); print(f'max_pending_messages={c.agent.max_pending_messages}')"
```

Expected: prints `max_pending_messages=32`

- [ ] **Step 4: Commit**

```bash
git add core/agent.py core/app_config.py config.yaml
git commit -m "feat: add capacity limit to pending message queue (default 32)"
```

---

### Task 5: Expand filesystem tool security denylist

**Files:**
- Modify: `core/tools/filesystem.py`

- [ ] **Step 1: Expand denylist**

In `core/tools/filesystem.py`, expand `_DENIED_PATHS` and `_DENIED_NAMES`:

```python
_DENIED_PATHS = [
    "/etc", "/proc", "/sys", "/dev",
    "/root", "/var/log", "/var/run",
    "/boot", "/lib", "/lib64",
    "/usr/lib", "/usr/lib64", "/usr/bin", "/usr/sbin",
    "/snap",
]

_DENIED_NAMES = [
    ".env", ".gitconfig", ".ssh", ".gnupg",
    ".aws", ".config", ".kube", ".kaggle",
    ".bash_history", ".zsh_history", ".mysql_history",
    ".pgpass", ".netrc", ".docker",
    "id_rsa", "id_ed25519", "id_ecdsa",
]
```

- [ ] **Step 2: Add test for new blocked paths**

Add to `core/tests/test_phase3.py` (or the test file that covers tools):

```python
def test_filesystem_tool_blocks_sensitive_paths():
    """文件系统工具应拒绝访问敏感路径。"""
    from core.tools.filesystem import _DENIED_PATHS, _DENIED_NAMES

    assert ".aws" in _DENIED_NAMES
    assert ".kube" in _DENIED_NAMES
    assert ".bash_history" in _DENIED_NAMES
    assert "/root" in _DENIED_PATHS
    assert "/var/log" in _DENIED_PATHS
```

- [ ] **Step 3: Run tests**

```bash
python -m pytest core/tests/ -k "filesystem" -v
```

If no filesystem-specific tests exist, run the full suite:

```bash
python -m pytest core/tests/test_phase3.py -v -k "tool"
```

Expected: All tool-related tests pass.

- [ ] **Step 4: Commit**

```bash
git add core/tools/filesystem.py core/tests/test_phase3.py
git commit -m "security: expand filesystem tool path/name denylist"
```

---

## Verification

After all tasks, run the full test suite:

```bash
python -m pytest Module/ core/ -v
```

Expected: All 708+ tests continue to pass. The only behavioral change is `can_retry()` now correctly returns False for COMPLETED/CANCELLED tasks.
