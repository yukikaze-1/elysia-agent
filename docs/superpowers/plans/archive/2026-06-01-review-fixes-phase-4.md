# Review Fixes — Phase 4: Feature Completion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Master plan:** `docs/superpowers/plans/2026-06-01-review-master-plan.md`
> **Review report:** `docs/review-2026-06-01.md`
> **Prerequisite:** Phase 3 (`docs/superpowers/plans/2026-06-01-review-fixes-phase-3.md`)

**Goal:** Complete the feature gaps identified in the review — emotion intelligence, multi-character support, wake words, observability, hot reload, prompt management, and conversation export.

**Architecture:** 7 long-term features, each large enough to warrant its own mini-spec when implementation begins. This plan provides the design outline, key file paths, and concrete starting points. Each task should be further refined with a brainstorming session before coding begins.

**Note:** 原 Task 4.1 (MacroMemory) 和 Task 4.7 (Docker) 已移至 todo.md 作为后续考虑。

**Tech Stack:** Python 3.13, asyncio, aiohttp, prometheus_client, openWakeWord (TBD), watchdog

---

### Task 4.2: EmotionEngine upgrade

**Files (规划):**
- Modify: `core/emotion_engine.py`
- Create: `prompt/Amygdala.j2`（基于现有 stub）
- Modify: `core/cognitive_loop.py`
- Modify: `core/tests/test_phase1.py`

**设计概要:**

当前 EmotionEngine 只有关键词匹配 + STT 标签映射。升级方向：
1. **三维情绪模型** (valence/arousal/dominance，简称 VAD)
2. **时间指数衰减** `intensity *= exp(-lambda * dt)` 替代线性衰减
3. **混合情绪**：支持同时存在多种情绪（如 70% warm + 30% curious）
4. **Amygdala LLM 补充**：对复杂情绪调用轻量 LLM 分析

```
当前模型:  关键词 → 单 mood + intensity
目标模型:  STT / 关键词 / LLM → VAD 三维 → mood 映射 + 混合 + 时间衰减
```

**关键数据结构变更:**

```python
@dataclass
class EmotionState:
    valence: float = 0.0      # -1 (负面) ~ +1 (正面)
    arousal: float = 0.0      # 0 (平静) ~ 1 (兴奋)
    dominance: float = 0.0    # -1 (顺从) ~ +1 (主导)
    timestamp: float = 0.0    # 最后更新时间

    def to_mood(self) -> dict:
        """VAD → mood 映射表"""
        # warm: high valence + moderate arousal
        # excited: high valence + high arousal
        # concerned: low valence + high arousal
        # calm: mid valence + low arousal
        ...

class EmotionEngine:
    def __init__(self, ...):
        self._state = EmotionState()
        self._decay_lambda = 0.001  # 时间衰减系数
        self._mixed_emotions: dict[str, float] = {}  # mood → intensity
```

**实现步骤:**

- [ ] **Step 1: Define EmotionState dataclass and VAD → mood mapping**
- [ ] **Step 2: Replace linear decay with exponential time decay**
- [ ] **Step 3: Support mixed emotions (dict instead of single mood)**
- [ ] **Step 4: Add Amygdala LLM fallback for complex text analysis**
- [ ] **Step 5: Update tests**
- [ ] **Step 6: Commit**

**验证:** 发送多种情绪文本，确认 mood 变化平滑且有混合。

**Commit 策略:** 分 3-4 个 commit（model → decay → mixed → amygdala）

---

### Task 4.3: Multi-character support

**Files (规划):**
- Modify: `core/prompt_assembler.py`
- Modify: `core/character_config.py`
- Modify: `core/app_config.py`
- Modify: `config.yaml`
- Create: `character/<name>/` 目录结构（per-character prompt templates）

**设计概要:**

当前 `PromptAssembler._PROMPT_DIR` 硬编码为 `prompt/`。多角色需要：

```
character/
├── elysia/
│   ├── config.yaml      # CharacterConfig
│   └── Brain.j2         # 该角色的系统提示词
├── another_char/
│   ├── config.yaml
│   └── Brain.j2
└── ...
```

**关键代码变更:**

```python
# core/prompt_assembler.py

class PromptAssembler:
    def __init__(self, character, session, long_term, prompt_dir=None):
        self._prompt_dir = prompt_dir or Path(__file__).parent.parent / "prompt"
        # If character has its own prompt dir
        char_dir = Path(character.config_path).parent
        char_prompt = char_dir / "Brain.j2"
        if char_prompt.exists():
            self._prompt_dir = char_dir
        ...

# core/character_config.py — 添加 prompt_dir
@dataclass
class CharacterConfig:
    name: str
    # ...
    prompt_dir: str | None = None  # 可选的自定义 prompt 目录
```

**实现步骤:**

- [ ] **Step 1: Update CharacterConfig to support custom prompt_dir**
- [ ] **Step 2: Update PromptAssembler to resolve prompt_dir from character config**
- [ ] **Step 3: Create example multi-character directory structure**
- [ ] **Step 4: Add WebSocket message type `switch_character` for runtime switching**
- [ ] **Step 5: Update tests**
- [ ] **Step 6: Commit**

**验证:** 在 `config.yaml` 中切换 character → 确认使用不同的 prompt 模板。

**Commit 策略:** 分 3 个 commit（config → assembler → runtime switch）

---

### Task 4.4: Wake word detection (openWakeWord)

**Files (规划):**
- Create: `Module/stt/wake_word.py`
- Modify: `Module/input/server.py`
- Modify: `core/event_bus.py`（可能需要新事件类型）
- Modify: `core/character_config.py`
- Modify: `config.yaml`

**设计概要:**

```
音频输入 → WakeWordDetector (独立线程) → 检测到唤醒词 → EventBus.post(WAKEWORD)
                                                                    ↓
                                                          Agent._process_events()
                                                                    ↓
                                                          触发主动问候
```

**WakeWordDetector 接口:**

```python
# Module/stt/wake_word.py

import queue
import threading

class WakeWordDetector:
    """开放唤醒词检测器。

    在独立线程中运行 openWakeWord 模型，通过回调通知。
    """

    def __init__(
        self,
        wake_words: list[str],        # 如 ["elysia", "伊莉西亚"]
        model_path: str | None = None,
        threshold: float = 0.5,
    ):
        self._wake_words = wake_words
        self._threshold = threshold
        self._audio_queue = queue.Queue()
        self._running = False

    def start(self, on_detected: callable):
        """启动检测线程。on_detected(word, confidence) 在检测到唤醒词时调用。"""
        self._running = True
        self._thread = threading.Thread(target=self._detect_loop, args=(on_detected,))
        self._thread.start()

    def feed(self, pcm_bytes: bytes) -> None:
        """喂入 16kHz 16bit PCM 音频数据。"""
        if self._running:
            self._audio_queue.put(pcm_bytes)

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=3.0)
```

**与现有 VAD 的关系:**
- 当前 WebRTC VAD 用于语音活动检测（静音→停止录音→发 STT）
- 唤醒词检测是前置步骤（静音时监听唤醒词 → 检测到 → 开始对话）

**实现步骤:**

- [ ] **Step 1: 安装 openWakeWord Python 绑定**
- [ ] **Step 2: 实现 WakeWordDetector 类**
- [ ] **Step 3: 集成到 WebSocket server（音频同时喂 VAD + 唤醒词检测）**
- [ ] **Step 4: 添加 WAKEWORD 事件类型到 EventBus**
- [ ] **Step 5: 在 CharacterConfig 中添加 wake_words 字段**
- [ ] **Step 6: 添加测试**
- [ ] **Step 7: Commit**

**Commit 策略:** 分 3 个 commit（detector → integration → tests）

---

### Task 4.5: Observability (metrics + alerting)

**Files (规划):**
- Create: `core/metrics.py`
- Modify: `core/agent.py`
- Modify: `core/llm_adapter.py`
- Modify: `Module/input/server.py`

**设计概要:**

使用 `prometheus_client` 库暴露 Prometheus 格式 metrics。

```python
# core/metrics.py

from prometheus_client import Counter, Histogram, Gauge, generate_latest

LLM_REQUESTS = Counter(
    "elysia_llm_requests_total",
    "Total LLM API calls",
    ["model", "status"],
)

LLM_DURATION = Histogram(
    "elysia_llm_duration_seconds",
    "LLM response latency",
    ["model"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

TTS_DURATION = Histogram(
    "elysia_tts_duration_seconds",
    "TTS synthesis latency",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0],
)

TOOL_CALLS = Counter(
    "elysia_tool_calls_total",
    "Total tool calls",
    ["tool_name", "status"],
)

MEMORY_ENTRIES = Gauge(
    "elysia_memory_entries_total",
    "Current long-term memory entries",
)

FSM_STATE = Gauge(
    "elysia_fsm_state",
    "Current FSM state",
)
```

**集成点:**

```python
# core/llm_adapter.py — stream_think() 中:
start = time.time()
status = "success"
try:
    async for chunk in ...:
        yield chunk
except Exception:
    status = "error"
    raise
finally:
    LLM_DURATION.labels(model=self.model).observe(time.time() - start)
    LLM_REQUESTS.labels(model=self.model, status=status).inc()
```

```python
# Module/input/server.py — 添加 /metrics 端点:
from core.metrics import generate_latest

async def metrics_handler(request):
    return web.Response(body=generate_latest(), content_type="text/plain")

app.router.add_get("/metrics", metrics_handler)
```

**实现步骤:**

- [ ] **Step 1: 安装 prometheus_client**
- [ ] **Step 2: 创建 core/metrics.py 定义所有 metrics**
- [ ] **Step 3: 在关键路径添加 metrics 埋点（LLM/TTS/tools/FSM）**
- [ ] **Step 4: 将 timing.py 的 span 数据输出到 Histogram**
- [ ] **Step 5: 添加 /metrics 端点**
- [ ] **Step 6: 添加测试验证 metrics 正确计数**
- [ ] **Step 7: Commit**

**Commit 策略:** 分 4 个 commit（definitions → LLM/TTS → tools/FSM → endpoint）

---

### Task 4.6: Configuration hot-reload

**Files (规划):**
- Modify: `Module/config/loader.py`
- Modify: `core/agent.py`
- Modify: `config.yaml`

**设计概要:**

使用 `watchdog` 库监控 `config.yaml` 变化，自动重载并更新 Agent 组件。

```python
# Module/config/loader.py 或独立文件 core/config_watcher.py

import asyncio
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class ConfigWatcher:
    """监控 config.yaml 变化并回调。"""

    def __init__(self, config_path: str, on_change: callable):
        self._path = config_path
        self._on_change = on_change
        self._observer = None

    def start(self) -> None:
        handler = _ConfigHandler(self._on_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(Path(self._path).parent))
        self._observer.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3.0)
```

**可热加载的配置项:**
- `llm.model` — 切换模型
- `tts.enabled` / `stt.enabled` — 开关服务
- `memory.session_max_messages` — 窗口大小
- `idle.silence_timeout` — 主动搭话间隔

**不可热加载的配置项:**
- `server.host` / `server.port` — 需要重启 HTTP server
- `character` — 需要切换大量组件

**实现步骤:**

- [ ] **Step 1: 安装 watchdog**
- [ ] **Step 2: 实现 ConfigWatcher 类**
- [ ] **Step 3: 在 Agent.start() 中启动 watcher，实现 on_change 回调**
- [ ] **Step 4: 添加测试（修改 yaml → 确认 Agent 行为变化）**
- [ ] **Step 5: Commit**

**Commit 策略:** 2 个 commit（watcher → integration）

---

### Task 4.8: Prompt versioning and A/B testing

**Files (规划):**
- Create: `prompt/versions/`
- Modify: `core/prompt_assembler.py`
- Create: `core/prompt_evaluator.py`
- Modify: `character/elysia.yaml`

**设计概要:**

```
prompt/
├── Brain.j2              # 当前活跃版本（或 symlink 到 version）
├── versions/
│   ├── Brain-v1.0.j2     # 各版本存档
│   ├── Brain-v1.1.j2
│   └── Brain-v1.2.j2
├── Amygdala.j2           # (stub)
└── ...
```

**版本选择:**

```yaml
# character/elysia.yaml
prompt:
  template: "Brain.j2"
  version: "v1.2"
  # 或直接指定:
  # template: "versions/Brain-v1.2.j2"
```

**PromptEvaluator 接口:**

```python
# core/prompt_evaluator.py

class PromptEvaluator:
    """评估不同 prompt 版本对同一对话的输出质量。"""

    def __init__(self, llm):
        self._llm = llm

    async def compare(
        self,
        prompt_a: str,     # 路径或模板内容
        prompt_b: str,
        test_conversations: list[list[dict]],  # 多组对话历史
    ) -> dict:
        """返回两个 prompt 的比较结果:
        {
            "a_better": 3,
            "b_better": 2,
            "tie": 5,
            "details": [...],
        }
        """
```

**实现步骤:**

- [ ] **Step 1: 创建 prompt/versions/ 目录，迁移当前 Brain.j2 为 v1.0**
- [ ] **Step 2: 在 CharacterConfig 中支持 prompt.version 字段**
- [ ] **Step 3: PromptAssembler 支持按版本加载**
- [ ] **Step 4: 实现 PromptEvaluator.compare()**
- [ ] **Step 5: 添加测试（两个已知质量的 prompt 对比）**
- [ ] **Step 6: Commit**

**Commit 策略:** 分 3 个 commit（version dir → assembler → evaluator）

---

### Task 4.9: Conversation export/import

**Files (规划):**
- Modify: `Module/input/server.py`
- Modify: `core/memory/session.py`
- Modify: `static/index.html`

**设计概要:**

**后端导出 API:**

```python
# Module/input/server.py

async def export_handler(request):
    """GET /api/export?format=markdown|json&session_id=xxx"""
    agent = request.app["agent"]
    fmt = request.query.get("format", "markdown")
    sid = request.query.get("session_id", "default")

    session = agent._session  # or load specific session from MongoDB
    messages = list(session._messages)

    if fmt == "json":
        data = {
            "session_id": sid,
            "exported_at": datetime.datetime.now().isoformat(),
            "messages": [m if isinstance(m, dict) else m.__dict__ for m in messages],
        }
        return web.json_response(data)
    else:
        md = f"# Elysia 对话记录 — {sid}\n\n"
        md += f"导出时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
        for m in messages:
            msg = m if isinstance(m, dict) else m.__dict__
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            name = "Elysia" if role == "assistant" else "你"
            md += f"**{name}**: {content}\n\n"
        return web.Response(body=md, content_type="text/markdown")

app.router.add_get("/api/export", export_handler)
```

**前端导出按钮:**

设置面板中添加"导出对话"按钮，调用 `/api/export` 下载文件。

**实现步骤:**

- [ ] **Step 1: 实现 GET /api/export 端点（Markdown + JSON）**
- [ ] **Step 2: 添加 tests（验证导出格式正确）**
- [ ] **Step 3: 前端添加导出按钮 + 下载逻辑**
- [ ] **Step 4: 手动验证导出文件可读性**
- [ ] **Step 5: Commit**

**Commit 策略:** 3 个 commit（backend → tests → frontend）

---

## Phase 4 Verification

Phase 4 的每个任务都较大，应单独验证：

| Task | 验证方式 |
|------|---------|
| 4.2 | 发送多种情绪文本，观察 mood 变化平滑性 |
| 4.3 | 切换 character → 确认不同 prompt 模板生效 |
| 4.4 | 在安静环境说唤醒词 → 确认触发 |
| 4.5 | `curl localhost:8765/metrics` → 确认 metrics 暴露 |
| 4.6 | 修改 config.yaml → 5 秒内确认变化生效 |
| 4.8 | 用相同对话测试两个 prompt 版本 → 比较输出 |
| 4.9 | 浏览器点击导出 → 下载 Markdown 文件 → 确认可读 |

```bash
# 回归测试
python -m pytest Module/ core/ -v
```
