# Elysia Agent Review 修复总计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase's detailed plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 系统性修复代码审查中发现的所有问题，分 4 个阶段从基础清理到功能完善逐步推进。

**Architecture:** 每个阶段产出独立可测试的工作软件。Phase 1-2 以修复和加固为主，Phase 3-4 以功能增强为主。阶段间有依赖关系（后一阶段建立在上一阶段的基础上），但阶段内任务尽量独立。

**Tech Stack:** Python 3.13, asyncio, aiohttp, httpx, openai, pymilvus, motor, Jinja2, PyYAML

**关联文档:**
- 审查报告: `docs/review-2026-06-01.md`
- Phase 1 详细计划: `docs/superpowers/plans/2026-06-01-review-fixes-phase-1.md`
- Phase 2 详细计划: `docs/superpowers/plans/2026-06-01-review-fixes-phase-2.md`
- Phase 3 详细计划: `docs/superpowers/plans/2026-06-01-review-fixes-phase-3.md`
- Phase 4 详细计划: `docs/superpowers/plans/2026-06-01-review-fixes-phase-4.md`

---

## 阶段总览

```
Phase 1 (本周)          Phase 2 (1-2周)         Phase 3 (1个月)          Phase 4 (长期)
基础清理                  代码质量 & 解耦          韧性 & 可观测性            功能完善
├─ requirements.txt      ├─ 组件公开 API          ├─ LLM 重试/Fallback     ├─ MacroMemory
├─ demo data 标注        ├─ EmotionEngine 异常    ├─ 会话管理               ├─ EmotionEngine 升级
├─ dead code 清理        ├─ prune 并发安全        ├─ 记忆 TTL/驱逐         ├─ 多角色支持
├─ can_retry 修复        ├─ MongoDB upsert 原子化 ├─ /health 端点           ├─ 唤醒词检测
├─ 消息队列上限          ├─ 缺失测试补充           ├─ TTS/INTERRUPT 竞争     ├─ hot reload
├─ 文件系统安全加固      ├─ 废弃代码评估           ├─ LongTermMemory 回写    ├─ 可观测性
                                                      ├─ Consolidator 节流      ├─ Docker 化
                                                      ├─ 消息队列剩余问题       ├─ Prompt 管理
                                                      └─ 前端体验优化           └─ 对话导出
```

---

# Phase 1: 基础清理（本周）

> 详细 TDD 步骤见: `docs/superpowers/plans/2026-06-01-review-fixes-phase-1.md`

**目标:** 修复最高优先级的代码质量和安全问题。每个任务独立，预计总时间 ~2 小时。

## Task 1.1: Create requirements.txt

**问题:** 项目无依赖声明文件，新人无法复现环境（遗漏 6）。

**Files:**
- Create: `requirements.txt`

**实现概要:**

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

**验证:** `pip install -r requirements.txt --dry-run` 无错误。

**Commit:** `chore: add requirements.txt with dependency declarations`

---

## Task 1.2: Label _seed_memories() as demo scaffolding

**问题:** 硬编码中文演示数据绕过正常 insert 流程，无文档说明（坑 4）。

**Files:**
- Modify: `core/agent.py`

**实现概要:**

在 `_seed_memories()` 方法上添加 docstring:

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

**验证:** `python -c "import core.agent; help(core.agent.Agent._seed_memories)"` 显示 docstring。

**Commit:** `docs: label _seed_memories() as demo scaffolding`

---

## Task 1.3: Fix can_retry() status check

**问题:** `can_retry()` 对已完成任务返回 True（问题 9）。`depends_on` 和 `run_in_background` 保留不动——它们是 todo.md 中"后台任务系统 — 后续迭代"的预留字段，等 BACKGROUND 任务和 DAG 依赖解析实现时会用到。

**Files:**
- Modify: `core/task.py`
- Modify: `core/tests/test_task_model.py`

**实现概要:**

1. 修复 `can_retry()` 添加状态检查

```python
def can_retry(self) -> bool:
    """只有非终态或 FAILED 状态且未达重试上限才能重试。"""
    if self.status == TaskStatus.CANCELLED:
        return False
    if self.status == TaskStatus.COMPLETED:
        return False
    return self.retry_count < self.max_retries
```

2. 新增测试 `test_can_retry_requires_non_terminal_status`:

```python
def test_can_retry_requires_non_terminal_status():
    from core.task import Task, TaskType, TaskStatus

    t = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t.status = TaskStatus.COMPLETED
    assert not t.can_retry()

    t2 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t2.status = TaskStatus.FAILED
    assert t2.can_retry()

    t3 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t3.status = TaskStatus.CANCELLED
    assert not t3.can_retry()
```

**验证:** `python -m pytest core/tests/test_task_model.py -v` 全部通过。

**Commit:** `fix: can_retry() returns False for COMPLETED and CANCELLED tasks`

---

## Task 1.4: Add capacity limit to _pending_messages queue

**问题:** `_pending_messages` 列表无上限，消息风暴可导致内存无限增长（坑 3）。

**Files:**
- Modify: `core/agent.py`
- Modify: `core/app_config.py`
- Modify: `config.yaml`

**实现概要:**

1. 在 `app_config.py` 的 `AgentConfig` 添加配置项:

```python
max_pending_messages: int = ConfigField(default=32)
```

2. 在 `config.yaml` 添加:

```yaml
agent:
  max_pending_messages: 32
```

3. 在 `agent.py` 的 `process_message()` 添加上限检查:

```python
if self._fsm.state != AgentState.IDLE:
    max_queue = self._config.agent.max_pending_messages
    if len(self._pending_messages) >= max_queue:
        logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
        return
    # ... existing queue logic ...
```

**验证:** 配置加载正确，应用正常启动。

**Commit:** `feat: add capacity limit to pending message queue (default 32)`

---

## Task 1.5: Expand filesystem tool security denylist

**问题:** 文件系统工具的 denylist 太小（安全-中）。

**Files:**
- Modify: `core/tools/filesystem.py`
- Modify: `core/tests/test_phase3.py`

**实现概要:**

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

**验证:** `python -m pytest core/tests/ -k "tool" -v` 通过。

**Commit:** `security: expand filesystem tool path/name denylist`

---

## Task 1.6: API key rotation (manual + documentation)

**问题:** `.env` 文件包含真实 API key 且已在 git history 中暴露（安全-严重）。

**这个任务需要人工操作，不可自动化。**

**Files:**
- Create: `.env.example`

**步骤:**

1. 在 DeepSeek 后台 (platform.deepseek.com) 轮换 API key
2. 在 Milvus 后台更换密码
3. 确认 `.env` 在 `.gitignore` 中（已包含，但文件曾被提交过）
4. 创建 `.env.example` 模板:

```bash
# DeepSeek API
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_API_BETA=https://api.deepseek.com/beta

# Milvus (optional)
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=root:your-password
```

5. 更新 `.env` 为新 key
6. 使用 `git filter-branch` 或 `BFG Repo-Cleaner` 清除 git history 中的敏感信息
7. 强制推送（如果有远程仓库）

**Commit:** `security: add .env.example template; rotate exposed credentials`

---

### Phase 1 Verification

```bash
# 全量测试
python -m pytest Module/ core/ -v

# 确认所有 708+ 测试通过
# 行为变更: can_retry() 对 COMPLETED/CANCELLED 任务现在返回 False
```

---

# Phase 2: 代码质量 & 解耦（1-2 周）

**目标:** 减少组件耦合，修复并发和异常处理问题，补充测试覆盖。Phase 2 任务之间相互独立。

---

## Task 2.1: Give CognitiveLoop and TaskScheduler public APIs

**问题:** `agent.py` 大量使用私有属性访问 `self._cognitive._current_task`、`self._scheduler._tool_ctx`（问题 1）。

**Files:**
- Modify: `core/cognitive_loop.py`
- Modify: `core/task_scheduler.py`
- Modify: `core/agent.py`

**实现概要:**

为 CognitiveLoop 添加公开方法:

```python
# core/cognitive_loop.py

def cancel_current(self) -> bool:
    """取消当前认知任务（如有）。返回 True 表示确实取消了任务。"""
    if self._current_task is not None and not self._current_task.done():
        self._current_task.cancel()
        return True
    return False

def set_tool_context(self, ctx) -> None:
    """设置当前 turn 的工具上下文。"""
    self._ctx = ctx
```

为 TaskScheduler 的 `_tool_ctx` 添加 setter:

```python
# core/task_scheduler.py

def set_tool_context(self, ctx) -> None:
    """更新工具上下文（用于 Agent.start() 中延迟注入）。"""
    self._tool_ctx = ctx
```

更新 `agent.py` 中的调用点:

```python
# 替换:
# self._cognitive._current_task.cancel()
# 为:
self._cognitive.cancel_current()

# 替换:
# self._cognitive._ctx = self._tool_ctx
# 为:
self._cognitive.set_tool_context(self._tool_ctx)

# 替换:
# self._scheduler._tool_ctx = self._tool_ctx
# 为:
self._scheduler.set_tool_context(self._tool_ctx)
```

**验证:** `python -m pytest core/tests/ -v` 全部通过。

**Commit:** `refactor: add public API to CognitiveLoop and TaskScheduler for encapsulation`

---

## Task 2.2: Fix EmotionEngine fire-and-forget exception swallowing

**问题:** `EmotionEngine._schedule` 的 fire-and-forget task 异常静默丢失（坑 7）。

**Files:**
- Modify: `core/emotion_engine.py`
- Modify: `core/tests/test_phase1.py`

**实现概要:**

在 `core/emotion_engine.py` 添加一个安全的 task 包装器:

```python
async def _safe_fire_and_forget(self, coro, name: str = "") -> None:
    """执行 fire-and-forget 协程，捕获并记录异常。"""
    try:
        await coro
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Fire-and-forget task '%s' 异常", name, exc_info=True)
```

将所有 `asyncio.create_task(self._schedule(...))` 调用改为 `asyncio.create_task(self._safe_fire_and_forget(self._schedule(...), name="expression"))`。

或更直接的方式 — 在 `_schedule` 方法内部添加 try/except:

```python
async def _schedule(self, coro) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("EmotionEngine scheduled task 异常", exc_info=True)
```

添加测试:

```python
def test_emotion_engine_task_exception_does_not_crash():
    """EmotionEngine 的 fire-and-forget task 异常应被捕获并记录。"""
    # ... 使用 caplog 验证异常被记录而非静默 ...
```

**验证:** `python -m pytest core/tests/ -k "emotion" -v` 通过。

**Commit:** `fix: add exception boundary to EmotionEngine fire-and-forget tasks`

---

## Task 2.3: Fix _prune_tasks concurrency safety

**问题:** `_completed_count` 在多个 Worker 之间共享无锁保护，modulo 检查和 prune 之间可能有竞态（问题 8）。

**Files:**
- Modify: `core/task_scheduler.py`

**实现概要:**

将 prune 逻辑从 worker loop 中移出，改为独立的周期性任务:

```python
# core/task_scheduler.py

async def start(self) -> None:
    self._running = True
    if self._checkpoint_path:
        Path(self._checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    self._restore_checkpoint()
    for i in range(self._num_workers):
        w = asyncio.create_task(self._worker_loop(f"w-{i}"), name=f"scheduler-w-{i}")
        self._workers.append(w)
    # 新增: 周期性清理任务
    self._prune_task = asyncio.create_task(self._prune_loop(), name="scheduler-prune")
    logger.info("Scheduler Worker Pool 启动 (%d workers)", self._num_workers)

async def stop(self) -> None:
    self._running = False
    self._save_checkpoint()
    # Cancel prune task
    if self._prune_task and not self._prune_task.done():
        self._prune_task.cancel()
    # ... rest of stop() ...

async def _prune_loop(self, interval: float = 10.0) -> None:
    """周期性清理终态任务和保存 checkpoint。"""
    while self._running:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        self._save_checkpoint()
        self._prune_tasks()
```

从 `_worker_loop` 的 finally 块中移除 `_save_checkpoint()` 和 `_prune_tasks()` 调用，移除 `_completed_count` 字段。

**验证:** `python -m pytest core/tests/test_task_scheduler.py -v` 全部通过。

**Commit:** `fix: move prune/checkpoint to dedicated periodic task for concurrency safety`

---

## Task 2.4: Fix SessionMemory.save() MongoDB race condition

**问题:** `SessionMemory.save()` 先 find 再 update_one/insert_one，不是原子操作（坑 5）。

**Files:**
- Modify: `core/memory/session.py`

**实现概要:**

将 find + update/insert 替换为 `update_one(upsert=True)`:

```python
# core/memory/session.py — save() method

async def save(self) -> bool:
    """保存 session 到 MongoDB（使用 upsert 保证原子性）。"""
    if self._db is None:
        return False
    try:
        doc = {
            "session_id": self._session_id,
            "messages": [m.to_dict() for m in self._messages],
            "params": dict(self._params),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        result = await self._collection.update_one(
            {"session_id": self._session_id},
            {"$set": doc},
            upsert=True,
        )
        return result.acknowledged
    except Exception:
        logger.debug("Session save failed", exc_info=True)
        return False
```

同时将 `datetime` 改为模块级 import:

```python
import datetime
```

**验证:** `python -m pytest core/tests/test_phase2.py -v -k "session"` 通过。

**Commit:** `fix: use atomic upsert for SessionMemory MongoDB save`

---

## Task 2.5: Supplement missing tests (~15 tests)

**问题:** todo.md 列出了 ~15 个缺失测试，覆盖 `log_setup.py`、`long_term.py`、`cognitive_loop.py`、`audio_buffer.py`、`server.py`、`console.py` 等。

**Files:**
- Create/Modify: `core/tests/test_log_setup.py`
- Modify: `core/tests/test_phase2.py` (long_term tests)
- Modify: `core/tests/test_phase1.py` (cognitive_loop tests)

**测试概要（关键新增）:**

```python
# core/tests/test_log_setup.py

def test_set_request_id_contextvar():
    """测试 request_id 的 ContextVar 跨协程传播。"""
    from core.log_setup import set_request_id, get_request_id
    set_request_id("001")
    assert get_request_id() == "001"

def test_request_id_isolation():
    """不同 task 应有独立的 request_id。"""
    import asyncio
    from core.log_setup import set_request_id, get_request_id

    async def task_a():
        set_request_id("A")
        await asyncio.sleep(0.01)
        return get_request_id()

    async def task_b():
        set_request_id("B")
        await asyncio.sleep(0.01)
        return get_request_id()

    results = asyncio.run(asyncio.gather(task_a(), task_b()))
    assert results == ["A", "B"]
```

```python
# 添加至 core/tests/test_phase2.py

def test_long_term_memory_in_memory_fallback_search():
    """Milvus 不可用时回退到字符重叠搜索。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry
    ltm = LongTermMemory()
    ltm._entries = [
        MemoryEntry(text="妖梦喜欢爬山", metadata={"type": "preference"}),
        MemoryEntry(text="昨天去了香山", metadata={"type": "event"}),
    ]
    results = ltm.search("爬山", limit=5)
    assert len(results) > 0
    assert any("爬山" in r.text for r in results)

def test_long_term_memory_rerank_weights():
    """验证 rerank 使用正确的权重公式。"""
    # ... test 0.5*sim + 0.3*poignancy + 0.2*recency ...
```

```python
# 添加至 core/tests/test_phase1.py

def test_cognitive_loop_interrupt_during_tool_execution():
    """INTERRUPT 应在工具执行期间正确取消。"""
    # ... 使用 FakeLLMBackend(returns_tool_calls=True) + cancel ...

def test_cognitive_loop_max_tool_rounds():
    """验证最多 3 轮工具调用。"""
    # ... FakeLLMBackend 始终返回 tool_calls ...
```

**验证:** `python -m pytest core/tests/ -v --cov=core --cov-report=term-missing` 查看覆盖率提升。

**Commit:** `test: add ~15 missing tests for log_setup, long_term, cognitive_loop, etc.`

---

## Task 2.6: Evaluate and handle Module/schema/ dead code

**问题:** `Module/schema/` 有 479 行预留架构代码与 `core/` 的数据结构并存，新人容易混淆（坑 8）。

**Files:**
- 评估: `Module/schema/`
- 可能修改: `Module/schema/__init__.py`（添加 deprecation 注释）

**实现概要:**

在 `Module/schema/__init__.py` 添加模块级注释:

```python
"""
DEPRECATED: 本模块为旧版预留架构，当前未被 core/ 使用。
core/ 使用自己的内部数据结构（core/schema.py 等）。
保留本模块仅作为未来架构演进参考。

如需使用数据模型，请参考:
- core/schema.py — AgentState, EventType 等枚举
- core/memory/session.py — SessionMemory
- core/memory/long_term.py — MemoryEntry
"""
```

不需要删除代码（todo.md 标记为"保留参考"），只需加注释说明现状。

**Commit:** `docs: add deprecation notice to Module/schema/`

---

### Phase 2 Verification

```bash
python -m pytest Module/ core/ -v
# 确认所有测试通过，新增测试被计入
```

---

# Phase 3: 韧性 & 可观测性（1 个月）

**目标:** 提升系统应对异常情况的能力，增加可观测性，改善用户体验的连续性。

---

## Task 3.1: LLM retry on 429 and 5xx errors

**问题:** LLM API 调用失败直接抛异常，无重试（遗漏 3）。

**Files:**
- Modify: `core/llm_adapter.py`
- Modify: `core/tests/test_phase3.py`

**实现概要:**

在 `LLMAdapter.stream_think()` 中添加指数退避重试:

```python
import asyncio
import logging

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds

async def stream_think(self, messages, tools=None, **kwargs):
    """流式 LLM 调用，含自动重试。"""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for chunk in self._backend.stream_think(messages, tools=tools, **kwargs):
                yield chunk
            return  # success
        except Exception as e:
            last_exc = e
            status = getattr(e, 'status_code', None)
            if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
                raise
            backoff = _RETRY_BACKOFF_BASE * (2 ** attempt)
            logger.warning("LLM 调用失败 (status=%s), %d/%d 重试 (%.0fs)",
                          status, attempt + 1, _MAX_RETRIES, backoff)
            await asyncio.sleep(backoff)
    raise last_exc
```

**注意:** 流式 LLM 调用的重试需要小心 — 部分 token 已发送给用户，重试会从头开始。需要考虑:
- 仅在没有 token 产出时重试（请求阶段失败）
- 或者使用新的 request_id 重试并告知前端

**简化方案（推荐）:** 只在请求级别（非流式中途）重试。如果流已经开始产出 token 后出错，不重试流，而是抛出异常让上层处理。

**验证:** 模拟 429/503 响应，确认重试逻辑触发并最终成功。

**Commit:** `feat: add exponential backoff retry for LLM API errors (429, 5xx)`

---

## Task 3.2: Session management → 移至 todo.md

> **决定：暂不做。** 等会话持久化和多用户需求更明确后再设计。已写入 todo.md。

**问题:** `session_id = "default"` 硬编码，无法新建/切换对话（遗漏 1）。

**Files:**
- Modify: `core/agent.py`
- Modify: `core/memory/session.py`
- Modify: `core/app_config.py`
- Modify: `Module/input/server.py` (WebSocket handler)
- Modify: `static/index.html` (前端)

**实现概要:**

本任务涉及前后端联动，是 Phase 3 中工作量最大的任务。

**后端变更:**

1. `SessionMemory` 支持传入 `session_id`:

```python
class SessionMemory:
    def __init__(self, max_messages=20, session_id="default"):
        self._session_id = session_id
        # ... rest unchanged ...
```

2. 新增 WebSocket 消息类型 `{"type": "switch_session", "session_id": "xxx"}` 和 `{"type": "list_sessions"}`

3. Agent 支持 `new_session()` 和 `switch_session(session_id)`:

```python
async def new_session(self) -> str:
    """创建新会话，返回 session_id。"""
    sid = uuid.uuid4().hex[:12]
    # 保存当前 session
    await self._session.save()
    # 创建新 session
    self._session = SessionMemory(
        max_messages=self._config.memory.session_max_messages,
        session_id=sid,
    )
    await self._session.connect_db()
    logger.info("New session: %s", sid)
    return sid

async def list_sessions(self) -> list[dict]:
    """列出所有 session（从 MongoDB 查询）。"""
    # ... MongoDB find distinct session_ids ...
```

4. 前端添加会话列表侧边栏 + 新建会话按钮

**验证:** 创建新会话 → 切换 → 确认历史隔离。

**Commit:** 分多个 commit（后端模型 → API → 前端）。

---

## Task 3.3: LongTermMemory TTL and eviction

**问题:** `_entries` 列表无界增长，Milvus 无 TTL 配置（坑 6）。

**Files:**
- Modify: `core/memory/long_term.py`
- Modify: `core/app_config.py`
- Modify: `config.yaml`

**实现概要:**

1. 在 `LongTermMemory` 中添加 TTL 驱逐:

```python
# core/memory/long_term.py

_MAX_ENTRIES = 10000  # 内存列表最大条目数
_MAX_AGE_DAYS = 90     # 最大保留天数

def _evict_old_entries(self) -> int:
    """驱逐过期和超过上限的条目。返回驱逐数量。"""
    now = time.time()
    cutoff = now - (_MAX_AGE_DAYS * 86400)
    removed = 0

    # 移除过期条目
    new_entries = []
    for e in self._entries:
        age = now - e.timestamp
        if age < _MAX_AGE_DAYS * 86400:
            new_entries.append(e)
        else:
            removed += 1
    self._entries = new_entries

    # 移除超过上限的条目（保留最新的）
    if len(self._entries) > _MAX_ENTRIES:
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        overflow = len(self._entries) - _MAX_ENTRIES
        self._entries = self._entries[:_MAX_ENTRIES]
        removed += overflow

    if removed:
        logger.info("Long-term memory: 驱逐 %d 条过期/超限条目", removed)
    return removed
```

2. 在 `insert()` 后调用 `_evict_old_entries()`
3. 在 Milvus 中配置 TTL（如果 Milvus >= 2.3）:

```python
# 创建 collection 时添加 TTL
# Milvus collection schema 中设置 ttl_seconds
```

4. 参数可配置化:

```python
# config.yaml
memory:
  long_term_max_entries: 10000
  long_term_max_age_days: 90
```

**验证:** 插入 > MAX_ENTRIES 条数据后确认驱逐生效。

**Commit:** `feat: add TTL eviction to LongTermMemory`

---

## Task 3.4: Health check endpoint

**问题:** 无 `/health` 或 `/ready` 端点（遗漏 5）。

**Files:**
- Modify: `Module/input/server.py`

**实现概要:**

在 aiohttp app 中添加健康检查路由:

```python
# Module/input/server.py — create_app()

async def health_handler(request):
    """健康检查端点。"""
    agent = request.app["agent"]
    status = 200
    checks = {
        "status": "ok",
        "fsm": agent._fsm.state,
        "llm": agent._llm.model,
        "memory": {
            "session_count": len(agent._session._messages),
            "long_term_count": agent._long_term.count,
        },
        "tools": agent._tools.tool_names,
        "ws_clients": getattr(agent._output, '_ws_count', lambda: 0)(),
    }
    # 如果 FSM 卡在非 IDLE 状态超过 5 分钟，报告 degraded
    return web.json_response(checks, status=status)

app.router.add_get("/health", health_handler)
app.router.add_get("/ready", health_handler)  # Kubernetes readiness probe
```

**验证:** `curl http://localhost:8765/health` 返回 JSON。

**Commit:** `feat: add /health and /ready endpoints`

---

## Task 3.5: TTS + INTERRUPT race condition fix

**问题:** INTERRUPT 时 emoji 可能合并到上一个气泡（坑 1，已知）。

**Files:**
- Modify: `core/cognitive_loop.py`
- Modify: `static/index.html` (前端配合)

**实现概要:**

这是已知的时序竞争问题。根因在 `finalizeDelta`（前端 JS）和下一句 delta 在不同的 asyncio.Task 中执行。

**后端方案:** 在 INTERRUPT 时发送一个显式的 `{"type": "flush", "stream_id": old_id}` 消息，强制前端完成当前气泡的 finalize。

**前端方案:** 在 WebSocket message handler 中，收到 `flush` 消息后立即 `finalizeDelta()` 并拒绝后续 append 到同一个 stream_id。

具体实现需要仔细分析当前的消息流时序，建议单独写一个 mini-spec。

**Commit:** `fix: resolve TTS/INTERRUPT emoji race condition with explicit flush signal`

---

## Task 3.6: LongTermMemory write-back sync

**问题:** Milvus 断连期间添加的内存条目永远不会同步到 Milvus（审查发现的设计问题）。

**Files:**
- Modify: `core/memory/long_term.py`

**实现概要:**

添加脏数据追踪和重连同步:

```python
class LongTermMemory:
    def __init__(self):
        # ...
        self._dirty_ids: set[str] = set()  # 未同步到 Milvus 的条目 ID

    def insert(self, entry: MemoryEntry) -> str:
        # ... existing insert logic ...
        if not self._milvus_available:
            self._dirty_ids.add(entry.id)  # 标记为脏
        return entry.id

    async def _sync_dirty_entries(self) -> int:
        """尝试将脏条目同步到 Milvus。"""
        if not self._milvus_available:
            return 0
        synced = 0
        for eid in list(self._dirty_ids):
            entry = self._get_by_id(eid)
            if entry and entry.embedding:
                # 尝试插入 Milvus
                try:
                    self._collection.insert([entry.to_milvus_dict()])
                    self._dirty_ids.discard(eid)
                    synced += 1
                except Exception:
                    break  # Milvus still unavailable
        if synced:
            logger.info("Synced %d dirty entries to Milvus", synced)
        return synced
```

在 `_evict_old_entries()` 中调用 `_sync_dirty_entries()`。

**验证:** 模拟 Milvus 断连 → 插入 → 重连 → 确认同步。

**Commit:** `feat: track and sync dirty entries when Milvus reconnects`

---

## Task 3.7: MemoryConsolidator throttling

**问题:** 每 6 轮调用一次 LLM，无节流，多轮快速对话可能导致多个 consolidation 同时运行（坑 2）。

**Files:**
- Modify: `core/consolidator.py`

**实现概要:**

添加运行锁，确保同一时间只有一个 consolidation 任务:

```python
class MemoryConsolidator:
    def __init__(self, ...):
        # ...
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

**验证:** 快速连续发送 20 条消息，确认不会启动多个并发 consolidation。

**Commit:** `fix: add consolidation lock to prevent concurrent LLM calls`

---

## Task 3.8: Frontend UX improvements (from todo.md)

**问题:** todo.md 记录了两个前端体验改进: 消息悬停复制 + 发送状态反馈。

**Files:**
- Modify: `static/index.html`

**实现概要:**

1. 消息悬停复制按钮 — 在气泡 CSS 中添加:

```css
.message-bubble {
    position: relative;
}
.message-bubble:hover .copy-btn {
    display: block;
}
.copy-btn {
    display: none;
    position: absolute;
    top: 4px;
    right: 4px;
    /* ... styling ... */
}
```

2. 发送状态反馈 — 发送按钮点击后显示勾动画:

```css
.send-btn.sent {
    animation: send-check 0.5s ease;
}
@keyframes send-check {
    /* checkmark animation */
}
```

**验证:** 浏览器中测试 hover 复制 + 发送动画。

**Commit:** `feat: add hover copy button and send status animation`

---

### Phase 3 Verification

```bash
# 全量测试
python -m pytest Module/ core/ -v

# 集成测试: 启动服务器
python app.py --server

# 手动验证:
# - 断网 → 发消息 → 恢复网络 → 确认 LLM 重试
# - 新建会话 → 切换 → 确认隔离
# - curl /health → 确认端点响应
# - 快速连续发消息 → 确认 consolidation 不并发
```

---

# Phase 4: 功能完善（长期）

**目标:** 实现 review 中发现的遗漏需求和 todo.md 中的功能改进。这些任务粒度更大，每个可能需要独立的设计文档。

---

## Task 4.1: MacroMemory + MacroReflector → 移至 todo.md

> **决定：后续考虑。** 当前微记忆系统够用，MacroMemory 作为未来增强方向。已写入 todo.md。

**问题:** 当前只有微记忆（每 6 轮即时提取），缺少定期的反思（reflection）来生成更高层次的洞察（遗漏-已记录）。

**Files (规划):**
- Create: `core/macro_reflector.py`
- Create: `prompt/MacroReflector.j2`
- Modify: `core/agent.py`
- Modify: `core/memory/long_term.py`

**设计概要:**

- `MacroReflector` 是一个定时后台任务（如每天一次或每 50 轮对话一次）
- 输入: 最近的 MicroMemory 条目（从 LongTermMemory 查询）
- 输出: 高层次洞察（"用户最近对 Rust 的热情下降了"、"用户提到下周有考试，可能需要鼓励"）
- 存储到独立的 Milvus collection 或 LongTermMemory 的特殊分区
- Prompt 模板: `prompt/MacroReflector.j2`（当前是 stub）

**依赖:** 需要 Phase 3 先完成 TTL/驱逐，否则内存条目过多影响反思质量。

**Commit:** 多个 commit（model → prompt → reflector → scheduling → test）

---

## Task 4.2: EmotionEngine upgrade

**问题:** 当前只有关键词匹配 + STT 标签映射，缺少真正的情感语义理解和时间衰减（遗漏-已记录）。

**Files (规划):**
- Modify: `core/emotion_engine.py`
- Create: `prompt/Amygdala.j2`（当前是 stub）
- Modify: `core/agent.py`

**设计概要:**

- 添加生理状态模型（valence/arousal/dominance 三维）
- 时间衰减曲线: `intensity *= exp(-lambda * dt)` 替代线性衰减
- 混合情绪: 同时存在 warm + concerned，而不是互斥
- Amygdala LLM 补充: 对复杂情绪调用轻量 LLM 分析

**依赖:** Phase 2 的异常边界修复（Task 2.2）。

**Commit:** 多个 commit

---

## Task 4.3: Multi-character support

**问题:** 当前 prompt 模板路径硬编码为 `prompt/`（遗漏-已记录）。

**Files (规划):**
- Modify: `core/prompt_assembler.py`
- Modify: `core/character_config.py`
- Modify: `core/app_config.py`
- Create: `character/<name>/` 目录（prompt + config per character）

**设计概要:**

- `CharacterConfig` 增加 `prompt_dir` 字段
- `PromptAssembler` 接受可选的 prompt 目录参数（fallback 到 `prompt/`）
- 切换角色 = 切换 CharacterConfig + 对应的 prompt 模板 + SessionMemory

**Commit:** 多个 commit

---

## Task 4.4: Wake word detection (openWakeWord)

**问题:** todo.md 记录了唤醒词检测需求。

**Files (规划):**
- Create: `Module/stt/wake_word.py`
- Modify: `Module/input/server.py`
- Modify: `config.yaml`

**设计概要:**

- 集成 openWakeWord 库（Python 绑定）
- 唤醒词检测运行在独立线程（不阻塞 asyncio 事件循环）
- 检测到唤醒词后通过 EventBus 触发（类似 SYSTEM_TICK 但更高优先级）
- 支持自定义唤醒词（通过 character YAML 配置）

**Commit:** 多个 commit

---

## Task 4.5: Observability (metrics + alerting)

**问题:** 无 metrics/历史趋势/告警（遗漏 8）。

**Files (规划):**
- Create: `core/metrics.py`
- Modify: `core/agent.py`
- Modify: `core/llm_adapter.py`
- Modify: `Module/input/server.py`

**设计概要:**

- 使用 `prometheus_client` 库暴露 metrics
- 关键 metrics:
  - `elysia_llm_requests_total{model, status}` — LLM 调用计数
  - `elysia_llm_duration_seconds{model}` — LLM 调用延迟直方图
  - `elysia_tts_duration_seconds` — TTS 合成延迟
  - `elysia_tool_calls_total{tool_name, status}` — 工具调用计数
  - `elysia_memory_entries_total` — 长期记忆条目数
  - `elysia_fsm_state` — 当前 FSM 状态
- `/metrics` 端点（Prometheus scrape target）
- `timing.py` 的 span 数据输出到 metrics

**Commit:** 多个 commit

---

## Task 4.6: Configuration hot-reload

**问题:** 修改 `config.yaml` 需要重启（遗漏 4）。

**Files (规划):**
- Modify: `Module/config/loader.py`
- Modify: `core/agent.py`

**设计概要:**

- 使用 `watchdog` 库监控 `config.yaml` 文件变化
- 变化时重新加载配置并更新 Agent 组件
- 仅支持热加载安全的配置项（LLM model、TTS enabled、memory 参数等）
- 不支持热加载: server host/port、character path

**验证:** 修改 `config.yaml` → 确认 Agent 行为变化无需重启。

**Commit:** `feat: add config file hot-reload via watchdog`

---

## Task 4.7: Docker support → 移至 todo.md

> **决定：后续考虑。** 当前项目处于快速迭代期，Docker 化会减慢开发速度。等架构稳定后再做。已写入 todo.md。

**问题:** 无 Dockerfile（遗漏-未记录在 review 中，但是生产部署必要步骤）。

**Files (规划):**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`

**设计概要:**

- 多阶段构建: builder 安装依赖 → runtime 启动
- docker-compose 编排: agent + MongoDB + Milvus + SenseVoice + GPT-SoVits
- 健康检查使用 `/health` 端点
- 环境变量通过 `.env` 挂载

**Commit:** `feat: add Docker support`

---

## Task 4.8: Prompt versioning and A/B testing

**问题:** 无 prompt 版本管理、测试、评估（遗漏 7）。

**Files (规划):**
- Create: `prompt/versions/`
- Modify: `core/prompt_assembler.py`
- Create: `core/prompt_evaluator.py`

**设计概要:**

- Prompt 文件命名: `Brain-v1.2.j2`
- `character.yaml` 中指定 prompt 版本
- `PromptEvaluator`: 使用同一段对话历史 + 不同 prompt 版本 → 比较 LLM 输出质量
- 评估标准: 响应长度、情绪匹配度、工具调用准确率

**Commit:** 多个 commit

---

## Task 4.9: Conversation export/import

**问题:** 无法导出对话记录（遗漏 2）。

**Files (规划):**
- Modify: `Module/input/server.py`
- Modify: `core/memory/session.py`
- Modify: `static/index.html`

**设计概要:**

- 后端: `GET /api/export?session_id=xxx` 返回 JSON/Markdown 格式的完整对话
- 前端: 设置面板添加"导出对话"按钮
- 格式: Markdown（人类可读）+ JSON（可重新导入）

**Commit:** `feat: add conversation export to Markdown and JSON`

---

# 附录: 完整问题-任务映射表

| Review 问题 | 阶段 | 任务 |
|------------|------|------|
| 安全-严重: .env 暴露 | Phase 1 | Task 1.6 |
| 遗漏 6: 无 requirements.txt | Phase 1 | Task 1.1 |
| 坑 4: _seed_memories() demo 数据 | Phase 1 | Task 1.2 |
| 问题 6: depends_on/run_in_background 预留字段 | — | 保留（todo.md 后台任务系统）|
| 问题 9: can_retry() 状态检查 | Phase 1 | Task 1.3 |
| 坑 3: _pending_messages 无上限 | Phase 1 | Task 1.4 |
| 安全-中: 文件系统 denylist 太小 | Phase 1 | Task 1.5 |
| 问题 1: 组件私有属性访问 | Phase 2 | Task 2.1 |
| 坑 7: EmotionEngine 异常静默丢失 | Phase 2 | Task 2.2 |
| 问题 8: _prune_tasks 并发安全 | Phase 2 | Task 2.3 |
| 坑 5: MongoDB upsert 竞态 | Phase 2 | Task 2.4 |
| 测试补充 (~15) | Phase 2 | Task 2.5 |
| 坑 8: Module/schema/ 死代码 | Phase 2 | Task 2.6 |
| 遗漏 3: LLM 重试/Fallback | Phase 3 | Task 3.1 |
| 遗漏 1: 会话管理 | — | → todo.md |
| 坑 6: LongTermMemory 无 TTL | Phase 3 | Task 3.3 |
| 遗漏 5: 健康检查端点 | Phase 3 | Task 3.4 |
| 坑 1: TTS/INTERRUPT 竞争条件 | Phase 3 | Task 3.5 |
| LongTermMemory write-back 同步 | Phase 3 | Task 3.6 |
| 坑 2: Consolidator 无节流 | Phase 3 | Task 3.7 |
| 前端体验优化 | Phase 3 | Task 3.8 |
| MacroMemory + MacroReflector | — | → todo.md（后续考虑）|
| EmotionEngine 升级 | Phase 4 | Task 4.2 |
| 多角色支持 | Phase 4 | Task 4.3 |
| 唤醒词检测 | Phase 4 | Task 4.4 |
| 遗漏 8: 可观测性 | Phase 4 | Task 4.5 |
| 遗漏 4: 配置热加载 | Phase 4 | Task 4.6 |
| Docker 化 | — | → todo.md（后续考虑）|
| 遗漏 7: Prompt 管理 | Phase 4 | Task 4.8 |
| 遗漏 2: 对话导出 | Phase 4 | Task 4.9 |
| 问题 2: 无依赖注入 | — | 暂缓（当前规模不需要） |
| 问题 3: FSM 观察型 → 控制型 | — | 暂缓（需要更大重构） |
| 问题 4: SPEAKING 状态语义 | — | 暂缓（需审查实际使用） |
| 问题 5: dispatch_tool_calls 阻塞 | — | 短期不需要（60s 超时够用） |
| 问题 7: Checkpoint 仅 BACKGROUND | — | 暂缓（等 BACKGROUND 任务实现后） |
| 安全-低: 无速率限制 | — | 暂缓（等实际遇到问题再加） |
