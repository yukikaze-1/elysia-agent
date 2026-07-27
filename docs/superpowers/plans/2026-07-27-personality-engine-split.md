# CharacterEngine 子模块拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `core/personality/engine.py`（700 行）拆分为 4 个子模块文件 + 精简 facade `CharacterEngine`，外部调用方零改动。

**Architecture:** 从 `CharacterEngine` 大类中提取 3 个独立子组件类（`EmotionDetector`、`AwarenessJudge`、`CompressionManager`），每个 90~220 行。`CharacterEngine` 变为 facade，组装子组件并委托调用。提取 `_utils.py` 作为共享工具模块。迁移顺序：`_utils` → `emotion` → `awareness` → `compression`。

**Tech Stack:** Python 3.11+, asyncio, unittest.mock (Mock/AsyncMock), pytest

## Global Constraints

- 外部调用方（agent.py, lifecycle.py, loop.py, debug/info.py）零改动 — 所有公开方法签名不变
- `test_engine.py` 现有 37 个测试全部保持通过，不修改测试代码
- `test_reflector.py` 现有测试全部保持通过
- CharacterEngine 构造签名 `__init__()` 参数不变
- `analyze_user_stt` 保留未使用的 `emotion_tag` 参数（避免 loop.py 改动）
- `_last_awareness_time` 作为 engine property（getter/setter 代理到 AwarenessJudge）
- `_awareness_allowed()`, `_maybe_judge_awareness()`, `_judge_awareness()`, `_should_compress()` 在 engine 上保留为委托方法（测试直接调用它们）

---

### Task 1: 创建 `_utils.py` — 抽取共享工具函数

**Files:**
- Create: `core/personality/_utils.py`
- Modify: `core/personality/engine.py:112-117, 680-700`

**Interfaces:**
- Produces: `_extract_tag(text: str, tag: str) -> str`, `_schedule(coro) -> None`

`_extract_tag` 从 XML 标签提取内容，`_schedule` 在 event loop 上调度 fire-and-forget 协程。两个函数无类依赖，纯模块级。

- [ ] **Step 1: 创建 `_utils.py` 文件**

```python
"""core/personality 共享工具函数。"""
import asyncio
import logging
import re


def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。"""
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _schedule(coro) -> None:
    """Schedule a coroutine on the event loop, if one is running.

    Wrapped so exceptions in the fire-and-forget task are logged.
    """
    try:
        loop = asyncio.get_running_loop()

        async def _safe_wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger = logging.getLogger(__name__)
                _logger.warning("CharacterEngine scheduled task 异常", exc_info=True)

        loop.create_task(_safe_wrapper())
    except RuntimeError:
        pass  # no event loop running — test environment, ignore
```

- [ ] **Step 2: 从 `engine.py` 删除 `_extract_tag` 和 `_schedule`，改为从 `_utils` 导入**

从 engine.py 删除 L112-117（`_extract_tag` 函数定义）和 L680-700（`_schedule` 静态方法）。

在 engine.py 顶部 import 区域添加：
```python
from ._utils import _extract_tag, _schedule
```

`_schedule` 当前是 `CharacterEngine` 的 `@staticmethod`，调用方式是 `self._schedule(...)`。改为模块级函数后，调用方式改为 `_schedule(...)`（去掉 `self.`）。需要替换 engine.py 中所有 `self._schedule(` → `_schedule(`。

受影响行（grep 确认 engine.py 内部调用）：
- `_schedule(self._judge_awareness(...))` → `_schedule(self._judge_awareness(...))`（无变化，已经是 `self._schedule` 传 self 的 coro）
- `self._schedule(self._output.expression(...))` → `_schedule(self._output.expression(...))`
- `self._schedule(self._output.expression(...))`（另一处，idle） → `_schedule(...)`

Wait, careful: `_schedule` is currently a `@staticmethod` on CharacterEngine, called as `self._schedule(coro)`. After moving to `_utils.py` as a module-level function, calls become `_schedule(coro)`. The `coro` argument doesn't change.

- [ ] **Step 3: 运行测试验证无回归**

```bash
pytest core/personality/tests/test_engine.py -v --tb=short
```

预期：全部 37 个测试通过。

- [ ] **Step 4: Commit**

```bash
git add core/personality/_utils.py core/personality/engine.py
git commit -m "refactor: extract _utils.py — _extract_tag + _schedule from engine.py"
```

---

### Task 2: 创建 `emotion.py` — EmotionDetector

**Files:**
- Create: `core/personality/emotion.py`
- Modify: `core/personality/engine.py`（删除 L48-91, L610-700；委托给 `self._emotion`）

**Interfaces:**
- Consumes: `_schedule` from `_utils.py`, `CharacterState`, `OutputManager`
- Produces: `EmotionDetector(state, output, idle_expressions)` → `.analyze_user()`, `.analyze_assistant()`, `.analyze_user_stt()`, `.push_idle_expression()`

接管 engine.py 的情绪相关全部逻辑：`MOOD_KEYWORDS` 常量 + `analyze_user` + `analyze_assistant` + `analyze_user_stt` + `push_idle_expression` + private `_detect_mood` / `_push_expression` / `_push_idle`。

- [ ] **Step 1: 创建 `emotion.py`**

```python
"""EmotionDetector — keyword mood 检测 + STT 表情推送 + mood 衰减。

纯规则引擎，无 LLM 依赖。
"""
import logging
import random

from core.schema.character import CharacterState
from Module.output import OutputManager
from ._utils import _schedule

logger = logging.getLogger(__name__)

# ---- 情绪关键词 ----
MOOD_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "happy": (
        ["哈哈", "开心", "太好了", "喜欢", "棒", "nice", "嘿嘿", "耶", "赞", "爱你"],
        ["你喜欢", "我也开心", "太好了", "哈哈", "嘿嘿", "开心"],
    ),
    "excited": (
        ["!", "！", "天哪", "wow", "太棒", "厉害", "牛逼", "惊", "竟然"],
        ["!", "！", "兴奋", "激动", "期待"],
    ),
    "sad": (
        ["难过", "伤心", "哭", "失败", "失去", "遗憾", "倒霉", "唉", "烦", "累"],
        ["别难过", "抱抱", "辛苦了", "会好起来的"],
    ),
    "curious": (
        ["为什么", "怎么", "什么原因", "讲讲", "说说", "好奇", "告诉我"],
        ["好问题", "有趣", "想知道"],
    ),
    "neutral": (
        [],
        [],
    ),
    "warm": (
        ["谢谢", "感谢", "感恩", "感激", "温暖", "贴心", "真好", "感动"],
        ["不客气", "我的荣幸", "应该的"],
    ),
    "concerned": (
        ["担心", "焦虑", "不安", "紧张", "害怕", "怎么办", "压力"],
        ["别担心", "会好的", "我理解你的担心"],
    ),
    "calming": (
        ["气死", "生气", "愤怒", "怒火", "离谱", "太过分", "受不了", "岂有此理"],
        ["冷静", "消消气", "别急", "慢慢说"],
    ),
    "reassuring": (
        ["怕", "恐惧", "恐怖", "吓人", "不敢", "担心害怕"],
        ["别怕", "有我在", "我会保护你"],
    ),
    "understanding": (
        ["恶心", "想吐", "厌恶", "讨厌", "反感", "受不了这个"],
        ["我懂", "确实不舒服", "需要适应"],
    ),
}


class EmotionDetector:
    """角色情绪检测器 — keyword 匹配 + STT 事件 + mood 衰减。"""

    def __init__(
        self,
        *,
        state: CharacterState,
        output: OutputManager,
        idle_expressions: list[str],
    ):
        self._state = state
        self._output = output
        self._idle_expressions = idle_expressions

    # ---- 公共方法 ----

    def analyze_user(self, text: str) -> None:
        """关键词 mood 检测。匹配到非 neutral mood 时更新 state 并推送表情。"""
        detected = self._detect_mood(text)
        if detected == "neutral":
            return

        current = self._state.mood
        intensity = self._state.mood_intensity

        if detected != current:
            self._state.mood = detected
            self._state.mood_intensity = min(1.0, intensity + 0.1)
            self._push_expression(detected, intensity)
            logger.debug("心情 (关键词): %s → %s", current, detected)
        else:
            self._state.mood_intensity = min(1.0, intensity + 0.05)

    def analyze_assistant(self, _text: str) -> None:
        """每轮回复后衰减 mood intensity。mood 文本由 LLM 下轮 <mood> 更新。"""
        intensity = self._state.mood_intensity
        new_intensity = max(0.1, intensity - 0.15)
        self._state.mood_intensity = new_intensity
        if new_intensity < 0.2:
            self._push_idle()

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None:
        """处理 STT 音频事件，推送对应表情。mood 决策交给 LLM（通过 C5 Sensory）。"""
        if events:
            for evt in events:
                if evt == "Laughter":
                    _schedule(self._output.expression("laugh", intensity=0.7))
                elif evt == "Cry":
                    _schedule(self._output.expression("cry", intensity=0.8))
                elif evt == "Applause":
                    _schedule(self._output.expression("surprised", intensity=0.5))

    async def push_idle_expression(self) -> None:
        """Push a random idle expression (called periodically or on IDLE entry)."""
        self._push_idle()

    # ---- 私有方法 ----

    def _detect_mood(self, text: str) -> str:
        """Simple keyword-based mood detection. Returns the strongest match."""
        scores: dict[str, int] = {}
        for mood_key, (user_kw, _) in MOOD_KEYWORDS.items():
            if mood_key == "neutral":
                continue
            score = sum(1 for kw in user_kw if kw in text)
            if score > 0:
                scores[mood_key] = score
        if not scores:
            return "neutral"
        best_key = max(scores, key=lambda k: scores[k])
        return best_key

    def _push_expression(self, mood_text: str, intensity: float) -> None:
        """Push expression to output channels. mood_text is plain string. 保留关键词回退兼容。"""
        _schedule(self._output.expression(mood_text, intensity=intensity))

    def _push_idle(self) -> None:
        """Push one of the idle expressions."""
        if self._idle_expressions:
            expr = random.choice(self._idle_expressions)
            _schedule(self._output.expression(expr, intensity=0.3))
```

- [ ] **Step 2: 修改 `engine.py`**

在 engine.py 顶部 import：
```python
from .emotion import EmotionDetector
```

在 `__init__` 中创建子组件（添加在创建 `_reflector` 之后）：
```python
# 子组件: EmotionDetector
self._emotion = EmotionDetector(
    state=state,
    output=output,
    idle_expressions=character_config.emotion.idle_expressions,
)
```

方法委托（替换现有方法体为委托调用）：
```python
def analyze_user(self, text: str) -> None:
    self._emotion.analyze_user(text)

def analyze_assistant(self, text: str) -> None:
    self._emotion.analyze_assistant(text)

def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None:
    self._emotion.analyze_user_stt(emotion_tag, events)

async def push_idle_expression(self) -> None:
    await self._emotion.push_idle_expression()
```

从 engine.py 删除：
- `MOOD_KEYWORDS` 常量（L48-91）
- `analyze_user` 方法体（L625-640，替换为委托）
- `analyze_assistant` 方法体（L642-648，替换为委托）
- `analyze_user_stt` 方法体（L614-623，替换为委托）
- `push_idle_expression` 方法体（L650-652，替换为委托）
- `_detect_mood` 方法（L656-668）
- `_push_expression` 方法（L670-672）
- `_push_idle` 方法（L674-678）

- [ ] **Step 3: 运行测试验证**

```bash
pytest core/personality/tests/test_engine.py -v --tb=short
```

预期：全部 37 个测试通过。

```bash
pytest -m "unit or service" -q --tb=short
```

预期：无新失败。

- [ ] **Step 4: Commit**

```bash
git add core/personality/emotion.py core/personality/engine.py
git commit -m "refactor: extract EmotionDetector from engine.py"
```

---

### Task 3: 创建 `awareness.py` — AwarenessJudge

**Files:**
- Create: `core/personality/awareness.py`
- Modify: `core/personality/engine.py`（L235-332 迁移到 awareness.py；添加委托 + property）

**Interfaces:**
- Consumes: `_schedule`, `_extract_tag` from `_utils.py`; `LLMAdapter`, `CharacterState`, `PromptManager`
- Produces: `AwarenessJudge(llm, state, character_name, user_name, pm)` → `.is_allowed()`, `.maybe_judge()`, `._judge()`

接管 engine.py 的 awareness 判定全部逻辑：`is_allowed`（三重门控）+ `maybe_judge`（fire-and-forget）+ `_judge`（LLM 调用）。

**关键：** `_last_awareness_time` 迁移到 `AwarenessJudge`。`CharacterEngine` 上暴露为 property，getter/setter 代理到 `self._awareness._last_awareness_time`。这确保 `lifecycle.py:177-178` 和 `test_engine.py` 中 6 处直接赋值继续有效。

`CharacterEngine` 上保留 3 个委托方法供测试访问：
- `_awareness_allowed()` → `self._awareness.is_allowed()`
- `_maybe_judge_awareness()` → `self._awareness.maybe_judge()`
- `_judge_awareness(l3_text)` → `await self._awareness._judge(l3_text)`

- [ ] **Step 1: 创建 `awareness.py`**

```python
"""AwarenessJudge — LLM 语义判定 L2 vs L3，管理 C9 觉察文本缓存。

三重门控: 缓存存在 → 展示次数未达上限 → 冷却时间已过。
"""
import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ._utils import _extract_tag, _schedule

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.character import CharacterState
    from Module.system_prompt import PromptManager

logger = logging.getLogger(__name__)

# AwarenessJudge 配置
AWARENESS_JUDGMENT_COOLDOWN = 300  # 最小展示冷却 5 分钟
AWARENESS_MAX_SHOWS = 3            # 单条文本最大展示次数


class AwarenessJudge:
    """LLM 语义判定 — 比较 L2 Self vs L3 反思，批准或跳过 C9 觉察文本。"""

    def __init__(
        self,
        *,
        llm: "LLMAdapter",
        state: "CharacterState",
        character_name: str,
        user_name: str,
        pm: "PromptManager",
    ):
        self._llm = llm
        self._state = state
        self._character_name = character_name
        self._user_name = user_name
        self._pm = pm

        # 冷却计时: 记录上次 C9 渲染的时间戳
        self._last_awareness_time: float = state.last_awareness_time or 0.0

    # ---- 公共接口 ----

    def is_allowed(self) -> bool:
        """判断当前是否允许 C9 渲染（缓存 + 展示次数 + 冷却三重门控）。

        必须同时满足：
        1. 有 judge 批准的觉察文本
        2. 展示次数未达上限
        3. 距上次 C9 渲染超过冷却时间（首次不受冷却限制）
        """
        if not self._state.approved_awareness_text:
            return False

        if self._state.awareness_show_count >= AWARENESS_MAX_SHOWS:
            return False

        if self._last_awareness_time > 0:
            if (time.time() - self._last_awareness_time) < AWARENESS_JUDGMENT_COOLDOWN:
                return False

        return True

    def maybe_judge(self) -> None:
        """触发异步 awareness 判定（fire-and-forget）。

        捕获当前 L3 snapshot 以避免后续 L3 更新的竞态。
        L3 为空时跳过。
        """
        l3_snapshot = self._state.state_l3
        if not l3_snapshot:
            return
        _schedule(self._judge(l3_snapshot))

    # ---- 私有实现 ----

    async def _judge(self, l3_text: str) -> None:
        """执行 LLM 判定：比较 L2 vs L3，更新 awareness 缓存。

        L2 为空 → 降级模式：L3 原文直接缓存，不调 LLM。
        L2 非空 → render prompt → call LLM → parse <approved>/<skip>。
        失败时保留旧缓存并记录 warning。
        """
        if self._pm is None:
            return

        l2_self = self._state.self_l2
        if not l2_self:
            # 降级模式：无 L2 时直接缓存 L3 原文
            self._state.approved_awareness_text = l3_text
            self._state.approved_awareness_at = time.time()
            self._state.awareness_show_count = 0
            logger.debug("AwarenessJudge: L2 为空，降级使用 L3 原文 (%d chars)", len(l3_text))
            return

        last_approved = self._state.approved_awareness_text

        # 1. 渲染 prompt
        try:
            prompt = self._pm.render(
                "AwarenessJudge.j2",
                character_name=self._character_name,
                l2_self=l2_self,
                l3_reflection=l3_text,
                last_approved=last_approved,
            )
        except Exception:
            logger.warning("AwarenessJudge: 模板渲染失败", exc_info=True)
            return

        # 2. LLM 调用
        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("AwarenessJudge: LLM 调用失败 (%.0f ms)", elapsed_ms, exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 解析结果
        approved = _extract_tag(text, "approved")
        if approved:
            self._state.approved_awareness_text = approved
            self._state.approved_awareness_at = time.time()
            self._state.awareness_show_count = 0
            logger.info("AwarenessJudge: 批准觉察文本 (%d chars, %.0f ms)", len(approved), elapsed_ms)
            return

        skip_reason = _extract_tag(text, "skip")
        logger.debug("AwarenessJudge: 跳过 — %s (%.0f ms)",
                     skip_reason or "无原因", elapsed_ms)
        # 保留旧缓存不变
```

- [ ] **Step 2: 修改 `engine.py`**

在 engine.py 顶部 import：
```python
from .awareness import AwarenessJudge
```

在 `__init__` 中创建子组件（添加在 `_emotion` 之后）：
```python
# 子组件: AwarenessJudge
self._awareness = AwarenessJudge(
    llm=llm,
    state=state,
    character_name=character_name,
    user_name=user_name,
    pm=self._pm,
)
```

在 `render_c9` 中，`self._awareness_allowed()` 调用改为 `self._awareness.is_allowed()`：
```python
def render_c9(self) -> C9Data:
    if not self._awareness.is_allowed():
        return C9Data(awareness_text="")

    self._awareness._last_awareness_time = time.time()
    self.state.last_awareness_time = self._awareness._last_awareness_time
    self.state.awareness_show_count += 1
    return C9Data(awareness_text=self.state.approved_awareness_text)
```

`consume_trigger` 中 `self._maybe_judge_awareness()` 改为 `self._awareness.maybe_judge()`。

添加 `_last_awareness_time` property（getter/setter 代理）：
```python
@property
def _last_awareness_time(self) -> float:
    return self._awareness._last_awareness_time

@_last_awareness_time.setter
def _last_awareness_time(self, value: float) -> None:
    self._awareness._last_awareness_time = value
```

添加 3 个委托方法（测试访问）：
```python
def _awareness_allowed(self) -> bool:
    return self._awareness.is_allowed()

def _maybe_judge_awareness(self) -> None:
    self._awareness.maybe_judge()

async def _judge_awareness(self, l3_text: str) -> None:
    await self._awareness._judge(l3_text)
```

从 engine.py 删除：
- `_awareness_allowed` 方法定义（L237-256）
- `_maybe_judge_awareness` 方法定义（L258-267）
- `_judge_awareness` 方法定义（L269-332）
- `AWARENESS_JUDGMENT_COOLDOWN` 和 `AWARENESS_MAX_SHOWS` 常量（L40-41）
- `_last_awareness_time` 属性初始化（L150，改为 property）
- `import time` 如果不再需要（检查：压缩相关方法仍用 `time.time()` 和 `time.perf_counter()`，所以保留）

- [ ] **Step 3: 运行测试验证**

```bash
pytest core/personality/tests/test_engine.py -v --tb=short -k "AwarenessJudge or C9 or awareness or allowed"
```

预期：`TestAwarenessJudge` 全部 12 个测试 + `TestCharacterEngineC9` 全部 2 个测试通过。

```bash
pytest core/personality/tests/test_engine.py -v --tb=short
```

预期：全部 37 个测试通过。

```bash
pytest -m "unit or service" -q --tb=short
```

预期：无新失败。

- [ ] **Step 4: Commit**

```bash
git add core/personality/awareness.py core/personality/engine.py
git commit -m "refactor: extract AwarenessJudge from engine.py"
```

---

### Task 4: 创建 `compression.py` — CompressionManager

**Files:**
- Create: `core/personality/compression.py`
- Modify: `core/personality/engine.py`（L334-542 迁移到 compression.py；添加委托）

**Interfaces:**
- Consumes: `_extract_tag` from `_utils.py`; `LLMAdapter`, `CharacterState`, `PromptManager`, `CharacterVersionRepository`, `CompressionTrigger`, `now_iso`
- Produces: `CompressionManager(llm, state, character_name, l1_core, pm, version_repo, character_config)` → `.check_and_compress()`, `.flush()`, `._compress()`, `._refresh_c6()`

接管 engine.py 的压缩 + C6 刷新全部逻辑。`_should_compress` 和 `_get_new_entries` 作为私有方法跟此类走。

`CharacterEngine` 上保留 1 个委托方法供测试访问：
- `_should_compress()` → `self._compression._should_compress()`

`consume_trigger` 中 `self._maybe_compress()` 改为 `self._compression.check_and_compress()`。
`flush_compression` 委托给 `self._compression.flush()`。

- [ ] **Step 1: 创建 `compression.py`**

```python
"""CompressionManager — L3→L2 性格压缩 + C6 说话风格刷新。

触发条件:
  1. state_history 新增 >= COMPRESSION_COUNT_THRESHOLD 条
  2. 距上次压缩 > COMPRESSION_TIME_THRESHOLD 秒
"""
import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.schema.compression import CompressionTrigger
from core.schema._utils import now_iso
from ._utils import _extract_tag

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.character import CharacterState, StateHistoryEntry
    from core.storage.character_version_repo import CharacterVersionRepository
    from Module.system_prompt import PromptManager

logger = logging.getLogger(__name__)

# L3→L2 压缩配置
COMPRESSION_COUNT_THRESHOLD = 5    # state_history 新增 N 条触发压缩
COMPRESSION_TIME_THRESHOLD = 604800  # 距上次压缩 > 7 天也触发
COMPRESSION_MIN_ENTRIES = 3        # 首次压缩最少需要 state_history 条目数


class CompressionManager:
    """L3→L2 性格压缩 + C6 说话风格刷新。"""

    def __init__(
        self,
        *,
        llm: "LLMAdapter",
        state: "CharacterState",
        character_name: str,
        l1_core: str,
        pm: "PromptManager",
        version_repo: "CharacterVersionRepository | None",
        character_config,  # CharacterConfig — 拿 emotion.idle_expressions
    ):
        self._llm = llm
        self._state = state
        self._character_name = character_name
        self._l1_core = l1_core
        self._pm = pm
        self._version_repo = version_repo
        self._character = character_config
        self._compression_lock = asyncio.Lock()

    # ---- 公共接口 ----

    async def check_and_compress(self) -> None:
        """检查压缩条件，满足时触发压缩。带双重检查锁。"""
        if self._pm is None:
            return
        if not self._should_compress():
            return
        if self._compression_lock.locked():
            logger.debug("CompressionManager: 压缩已在运行中，跳过")
            return

        async with self._compression_lock:
            # 双重检查 — 上一个压缩可能刚好完成了
            if not self._should_compress():
                return
            await self._compress()

    async def flush(self) -> None:
        """Shutdown 时强制压缩（如有未处理条目）。带超时保护。"""
        if self._pm is None:
            return
        history = self._state.state_history
        last_count: int = self._state.compression_last_count
        new_count = len(history) - last_count
        if new_count < COMPRESSION_MIN_ENTRIES:
            logger.debug("CompressionManager flush: 仅 %d 新条目，跳过压缩", new_count)
            return
        if self._compression_lock.locked():
            logger.debug("CompressionManager flush: 压缩已在运行中")
            return

        async with self._compression_lock:
            try:
                await asyncio.wait_for(
                    self._compress(trigger=CompressionTrigger.FLUSH), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CompressionManager: 压缩 flush 超时")
            except Exception:
                logger.debug("CompressionManager: 压缩 flush 失败", exc_info=True)

    # ---- 内部方法（private，但 engine 委托方法暴露给测试）----

    def _should_compress(self) -> bool:
        """检查是否应触发 L3→L2 压缩。

        触发条件（任一满足）:
        1. state_history 新增 >= COMPRESSION_COUNT_THRESHOLD 条
        2. 距上次压缩 > COMPRESSION_TIME_THRESHOLD 秒

        同时要求 state_history 至少有 COMPRESSION_MIN_ENTRIES 条。
        """
        history = self._state.state_history
        total = len(history)
        if total < COMPRESSION_MIN_ENTRIES:
            return False

        last_count: int = self._state.compression_last_count
        last_at = self._state.compression_last_at or 0.0

        # 计数触发：新增 >= 阈值
        new_count = total - last_count
        if new_count >= COMPRESSION_COUNT_THRESHOLD:
            logger.debug("CompressionManager: 计数触发压缩 (%d 新条目)", new_count)
            return True

        # 时间触发：距上次 > 阈值（仅限已有压缩记录时）
        if last_at > 0 and (time.time() - last_at) >= COMPRESSION_TIME_THRESHOLD:
            logger.debug("CompressionManager: 时间触发压缩 (%.1f 天)",
                         (time.time() - last_at) / 86400)
            return True

        return False

    def _get_new_entries(self) -> "list[StateHistoryEntry]":
        """取上次压缩后新增的 state_history 条目。"""
        history = self._state.state_history
        last_count: int = self._state.compression_last_count
        return history[last_count:]

    async def _compress(self, trigger: CompressionTrigger = CompressionTrigger.COMPRESSION) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 解析 <new_self> + <changes> → 写版本历史。"""
        if self._pm is None:
            return

        current_self = self._state.self_l2
        new_entries = self._get_new_entries()
        history = self._state.state_history

        logger.info("CompressionManager: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
                    len(new_entries), len(current_self))

        try:
            system = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionSystemPrompt",
                character_name=self._character_name,
                l1_core=self._l1_core,
            )
            user_prompt = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionUserPrompt",
                character_name=self._character_name,
                current_self=current_self,
                recent_changes=new_entries,
            )
        except Exception:
            logger.warning("CompressionManager: 压缩模板渲染失败", exc_info=True)
            return

        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CompressionManager: 压缩 LLM 调用失败 — 下次重试", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        new_self = _extract_tag(text, "new_self")
        if not new_self:
            logger.warning("CompressionManager: 压缩 LLM 返回无 <new_self>")
            # 仍更新追踪，避免重复在同一批条目上重试
            self._state.compression_last_at = time.time()
            self._state.compression_last_count = len(history)
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [c.strip("- ") for c in changes_raw.strip().split("\n") if c.strip()]

        # 更新 L2
        self._state.self_l2 = new_self
        self._state.compression_last_at = time.time()
        self._state.compression_last_count = len(history)

        logger.info("CompressionManager: L3→L2 压缩完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_self), elapsed_ms, len(changes))

        # 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self._state.session_id,
                text=new_self,
                timestamp=now_iso(),
                trigger=trigger,
                change_summary=changes,
            )
        else:
            logger.info("CompressionManager: version_repo 未配置，版本历史未保存")

        # 触发 C6 刷新
        await self._refresh_c6()

    async def _refresh_c6(self) -> None:
        """L2 更新后刷新 C6 说话风格。LLM 编辑模式 + 版本历史。失败不阻塞。"""
        if self._pm is None:
            return

        # 1. 获取当前 C6 文本
        current = self._state.speaking_style_c6
        if not current:
            # 首次：渲染 L1 默认模板获取基准文本
            try:
                from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
                sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")
                sec.set_context(user_name="")
                current = await sec.render()
            except Exception:
                logger.warning("CompressionManager: C6 默认模板渲染失败，跳过刷新")
                return

        if not current.strip():
            return

        # 2. 渲染 prompt
        try:
            prompt = self._pm.render(
                "SpeakingStyleEdit.j2",
                character_name=self._character_name,
                self_l2=self._state.self_l2,
                current_speaking_style=current,
            )
        except Exception:
            logger.warning("CompressionManager: C6 prompt 渲染失败", exc_info=True)
            return

        # 3. LLM 调用
        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CompressionManager: C6 refresh LLM 调用失败", exc_info=True)
            return

        # 4. 解析 XML 标签
        new_style = _extract_tag(text, "new_speaking_style")
        if not new_style:
            logger.warning("CompressionManager: C6 refresh <new_speaking_style> 解析失败")
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [l.strip("- ").strip() for l in (changes_raw or "").splitlines() if l.strip()]

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 5. 更新 state
        self._state.speaking_style_c6 = new_style

        logger.info("CompressionManager: C6 刷新完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_style), elapsed_ms, len(changes))

        # 6. 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self._state.session_id,
                text=new_style,
                timestamp=now_iso(),
                trigger=CompressionTrigger.C6_REFRESH,
                change_summary=changes,
                content_type="c6",
            )
```

- [ ] **Step 2: 修改 `engine.py`**

在 engine.py 顶部 import：
```python
from .compression import CompressionManager
```

在 `__init__` 中创建子组件（添加在 `_awareness` 之后，替换原有的 `_compression_lock` 初始化）：
```python
# 子组件: CompressionManager
self._compression = CompressionManager(
    llm=llm,
    state=state,
    character_name=character_name,
    l1_core=base_personality,
    pm=self._pm,
    version_repo=version_repo,
    character_config=character_config,
)
```

注意：删除原有的 `self._compression_lock = asyncio.Lock()` 行（L153），这个 lock 现在由 CompressionManager 管理。

`consume_trigger` 中 `await self._maybe_compress()` 改为 `await self._compression.check_and_compress()`。

方法委托：
```python
async def flush_compression(self) -> None:
    await self._compression.flush()
```

添加 1 个委托方法（测试访问）：
```python
def _should_compress(self) -> bool:
    return self._compression._should_compress()
```

从 engine.py 删除：
- `COMPRESSION_COUNT_THRESHOLD`、`COMPRESSION_TIME_THRESHOLD`、`COMPRESSION_MIN_ENTRIES` 常量（L44-46）
- `_maybe_compress` 方法（L336-350）
- `_should_compress` 方法（L352-381，替换为委托）
- `_get_new_entries` 方法（L383-387）
- `_compress` 方法（L389-467）
- `_refresh_c6` 方法（L469-542）
- `flush_compression` 方法体（L544-565，替换为委托）
- `self._compression_lock = asyncio.Lock()` 初始化（L153，锁已在 CompressionManager 内）
- `from core.schema.compression import CompressionTrigger` — 不再被 engine.py 直接使用
- `from core.schema._utils import now_iso` — 不再被 engine.py 直接使用

engine.py 现在剩余的 import 清理：
```python
import asyncio    # 可能仍被 _schedule 相关的 import 需要… 实际上 _schedule 已移到 _utils.py
import logging
import random     # 不再需要（只有 emotion.py 用了）
import time       # 仍被 render_c9 的 time.time() 使用
```

仔细检查：engine.py render_c9 中仍用 `time.time()`，所以保留 `import time`。`import asyncio` 如果不再使用（_schedule 已移走），可以删除。`import random` 不再使用，删除。

- [ ] **Step 3: 运行测试验证**

```bash
pytest core/personality/tests/test_engine.py -v --tb=short
```

预期：全部 37 个测试通过。

```bash
pytest -m "unit or service" -q --tb=short
```

预期：无新失败。

- [ ] **Step 4: Commit**

```bash
git add core/personality/compression.py core/personality/engine.py
git commit -m "refactor: extract CompressionManager from engine.py"
```

---

### Task 5: 清理 + 最终验证

**Files:**
- Modify: `core/personality/engine.py`（清理未使用的 import + 废弃常量）
- Modify: `core/personality/__init__.py`（如需要更新 export）

**检查点：**

- [ ] **Step 1: 清理 engine.py 未使用的 import 和常量**

```python
# 删除未使用的 import:
import random     # 只在 emotion.py 中使用
import asyncio    # 检查：_schedule 已移走。如果 engine.py 中无其他 asyncio 使用则删除

# 保留的 import:
import logging    # logger 仍在使用
import time       # render_c9 使用 time.time()
from collections.abc import Callable  # 可能仍被类型标注使用
from dataclasses import dataclass     # C1Data, C9Data, C6Data
from pathlib import Path              # PromptManager 路径构造
from typing import TYPE_CHECKING      # TYPE_CHECKING 块

# 保留的废弃常量（内部无引用，但可能被外部使用）:
AWARENESS_COOLDOWN_SECONDS = 3600  # deprecated — 保留供向后兼容
AWARENESS_MAX_HISTORY = 10         # 仍被 _append_history 使用，保留
```

- [ ] **Step 2: 验证 engine.py 行数**

```bash
wc -l core/personality/engine.py
```

预期：~200~250 行（从 700 行精简）。

- [ ] **Step 3: 验证 `__init__.py` 导出**

```bash
python -c "from core.personality import CharacterEngine, CharacterReflector; print('OK')"
```

- [ ] **Step 4: 全量测试**

```bash
pytest core/personality/tests/ -v --tb=short
```

预期：全部测试通过（test_engine.py 37 tests + test_reflector.py tests）。

```bash
pytest -m "unit or service" -q --tb=short
```

预期：无新失败。

- [ ] **Step 5: Commit**

```bash
git add core/personality/engine.py core/personality/__init__.py
git commit -m "chore: clean up engine.py imports and dead code after split"
```
