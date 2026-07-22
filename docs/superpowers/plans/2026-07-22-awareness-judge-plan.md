# AwarenessJudge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 C9 激活判定从纯时间冷却升级为 LLM 语义判定（异步 Judge 比较 L2 vs L3）

**Architecture:** Reflector 产出 L3 后 fire-and-forget 触发 Judge → LLM 比较 L2/L3 → 缓存批准文本 → `render_c9()` 同步读缓存 + 门控（冷却 5min + 展示次数 3 次）。L2 为空时降级：L3 原文直接缓存。

**Tech Stack:** Python 3.11+, Jinja2, Pydantic, asyncio, pytest + AsyncMock

## Global Constraints

- `AWARENESS_JUDGMENT_COOLDOWN = 300` (5 min), `AWARENESS_MAX_SHOWS = 3`
- Judge 可编辑文本（`<approved>` 输出编辑后版本）
- L2 为空 → 降级，不调 LLM，L3 原文缓存
- Judge skip → 保留旧缓存不变
- 无墙钟过期；新 Judge 批准替换旧缓存
- 模板用 `_pm.render()` 单层 flat（同 SpeakingStyleEdit.j2 模式）
- LLM 调用用 `_llm.think()` + `extra_body={"thinking": {"type": "disabled"}}`（同 `_refresh_c6`）
- Fire-and-forget 用 `self._schedule()`（同 expression push 模式）
- 所有新增测试加 `@pytest.mark.unit`

---

### Task 1: Prompt 模板

**Files:**
- Create: `prompt/AwarenessJudge.j2`

**Interfaces:**
- Produces: `AwarenessJudge.j2` — 接收 `character_name`, `l2_self`, `l3_reflection`, `last_approved`，输出 `<approved>` 或 `<skip>`

- [ ] **Step 1: 创建模板文件**

```jinja2
{# AwarenessJudge — L2 vs L3 语义偏离判定，控制 C9 是否激活 #}

你是 {{ character_name }}。你正在审视自己最近的变化感受，判断是否需要表达出来。

以下是你长期以来如何认识自己的：

<long_term_self>
{{ l2_self }}
</long_term_self>

以下是你最近对自己的新感受（来自反思）：

<recent_reflection>
{{ l3_reflection }}
</recent_reflection>

{% if last_approved %}
以下是你最近已经表达过的觉察（避免重复）：

<previous_awareness>
{{ last_approved }}
</previous_awareness>
{% endif %}

## 判断任务

判断"最近的新感受"是否包含了对"长期自我认知"的**有意义的补充或偏移**，值得你在对话中自然表达出来。

必须跳过的情形（即使只有一部分符合也要跳过）：
1. 新感受说"没什么变化"、"没有明显变化"、"还是老样子"等
2. 新感受的内容已经被长期自我认知覆盖（意思相同或高度重叠）
3. 新感受只是对当前情境的暂时反应，不是性格层面的偏移
4. 新感受与"最近已经表达过的觉察"高度相似

只有在新感受代表了**真实的、新的自我洞察**时才批准。

{% if last_approved %}
注意：新感受可能与上次已表达的内容类似。这种情况下必须跳过，保证觉察总是新鲜的。
{% endif %}

## 编辑要求（仅当批准时）

- 用第一人称，1-2 句中文
- 保持自然、亲密的语气，像是在心里对自己说的话
- 不要把暂时情绪包装成性格变化
- 可以用新感受中的原句，也可以润色使之更自然

## 输出格式（必须二选一）

批准展示：
<approved>编辑后的觉察文本（1-2 句中文，第一人称）</approved>

跳过：
<skip>跳过的简短原因（不必说明所有原因，一句即可）</skip>
```

- [ ] **Step 2: 验证模板可被 PromptManager 加载**

Run: `cd /home/yomu/Agent && python -c "from Module.system_prompt import PromptManager; pm = PromptManager('prompt'); result = pm.render('AwarenessJudge.j2', character_name='Elysia', l2_self='测试L2', l3_reflection='测试L3', last_approved=''); print('OK:', len(result), 'chars')"`
Expected: `OK: N chars`（无异常）

- [ ] **Step 3: 提交**

```bash
git add -f prompt/AwarenessJudge.j2
git commit -m "feat: 新增 AwarenessJudge.j2 — C9 激活 LLM 判定模板"
```

---

### Task 2: CharacterState 新增缓存字段

**Files:**
- Modify: `core/schema/character.py:22-24`

**Interfaces:**
- Produces: `CharacterState.approved_awareness_text: str`, `approved_awareness_at: float | None`, `awareness_show_count: int`

- [ ] **Step 1: 添加字段**

在 `last_awareness_time` 之后、`compression_last_at` 之前插入：

```python
approved_awareness_text: str = ""
"""Judge 批准或降级的觉察文本。空串 = 无可展示内容。"""
approved_awareness_at: float | None = None
"""批准时间戳（epoch float）。None = 未批准。"""
awareness_show_count: int = 0
"""当前批准文本已展示次数。新批准时重置为 0。"""
```

修改后的 `character.py` 相关部分：

```python
class CharacterState(BaseModel):
    """CharacterEngine 完整状态。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None       # epoch float，非 IsoTimestamp
    approved_awareness_text: str = ""               # AwarenessJudge 批准的觉察文本
    approved_awareness_at: float | None = None       # 批准时间戳（epoch）
    awareness_show_count: int = 0                    # 已展示次数
    compression_last_at: float | None = None         # epoch float，非 IsoTimestamp
    compression_last_count: int = 0
    mood: str = "平静"
    mood_intensity: float = 0.5
    speaking_style_c6: str = ""  # C6 动态文本。空串 = 使用 L1 默认模板
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

- [ ] **Step 2: 运行现有测试验证向后兼容**

Run: `cd /home/yomu/Agent && python -m pytest core/personality/tests/test_engine.py -v -q`
Expected: 所有现有测试 PASS（新字段有默认值，不影响反序列化）

- [ ] **Step 3: 运行 schema 测试**

Run: `cd /home/yomu/Agent && python -m pytest core/schema/tests/ -v -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add core/schema/character.py
git commit -m "feat: CharacterState 新增 AwarenessJudge 缓存字段

- approved_awareness_text: Judge 批准/降级的觉察文本
- approved_awareness_at: 批准时间戳
- awareness_show_count: 已展示次数"
```

---

### Task 3: CharacterEngine 核心变更

**Files:**
- Modify: `core/personality/engine.py`

**Interfaces:**
- Consumes: `CharacterState.approved_awareness_text`, `approved_awareness_at`, `awareness_show_count`
- Produces: `_judge_awareness(l3_text: str) -> None` (async), `_maybe_judge_awareness() -> None` (sync)
- Changes: `render_c9()`, `_awareness_allowed()`, `consume_trigger()`

- [ ] **Step 1: 更新常量**

在 `AWARENESS_MAX_HISTORY = 10` 之后（第 36 行后）新增：

```python
# AwarenessJudge 配置
AWARENESS_JUDGMENT_COOLDOWN = 300  # 最小展示冷却 5 分钟
AWARENESS_MAX_SHOWS = 3            # 单条文本最大展示次数
```

- [ ] **Step 2: 替换 `_awareness_allowed()`**

将第 228-235 行替换为：

```python
def _awareness_allowed(self) -> bool:
    """判断当前是否允许 C9 渲染（缓存 + 展示次数 + 冷却三重门控）。

    Phase 2: LLM-judge 缓存驱动。
    必须同时满足：
    1. 有 judge 批准的觉察文本
    2. 展示次数未达上限
    3. 距上次 C9 渲染超过冷却时间（首次不受冷却限制）
    """
    if not self.state.approved_awareness_text:
        return False

    if self.state.awareness_show_count >= AWARENESS_MAX_SHOWS:
        return False

    if self._last_awareness_time > 0:
        if (time.time() - self._last_awareness_time) < AWARENESS_JUDGMENT_COOLDOWN:
            return False

    return True
```

- [ ] **Step 3: 替换 `render_c9()`**

将第 178-190 行替换为：

```python
def render_c9(self) -> C9Data:
    """产出 C9 SelfPerception section 的数据。

    Phase 2: 从 judge 缓存读取，不再直接使用 raw L3。
    加冷却 + 展示次数门控。
    """
    if not self._awareness_allowed():
        return C9Data(awareness_text="")

    self._last_awareness_time = time.time()
    self.state.last_awareness_time = self._last_awareness_time
    self.state.awareness_show_count += 1
    return C9Data(awareness_text=self.state.approved_awareness_text)
```

- [ ] **Step 4: 新增 `_maybe_judge_awareness()`**

在 `_awareness_allowed()` 方法之后插入：

```python
def _maybe_judge_awareness(self) -> None:
    """触发异步 awareness 判定（fire-and-forget）。

    捕获当前 L3 snapshot 以避免后续 L3 更新的竞态。
    L3 为空时跳过。
    """
    l3_snapshot = self.state.state_l3
    if not l3_snapshot:
        return
    self._schedule(self._judge_awareness(l3_snapshot))
```

- [ ] **Step 5: 新增 `_judge_awareness(l3_text)`**

在 `_maybe_judge_awareness()` 之后插入：

```python
async def _judge_awareness(self, l3_text: str) -> None:
    """执行 LLM 判定：比较 L2 vs L3，更新 awareness 缓存。

    L2 为空 → 降级模式：L3 原文直接缓存，不调 LLM。
    L2 非空 → render prompt → call LLM → parse <approved>/<skip>。
    失败时保留旧缓存并记录 warning。
    """
    if self._pm is None:
        return

    l2_self = self.state.self_l2
    if not l2_self:
        # 降级模式：无 L2 时直接缓存 L3 原文
        self.state.approved_awareness_text = l3_text
        self.state.approved_awareness_at = time.time()
        self.state.awareness_show_count = 0
        logger.debug("AwarenessJudge: L2 为空，降级使用 L3 原文 (%d chars)", len(l3_text))
        return

    last_approved = self.state.approved_awareness_text

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
        self.state.approved_awareness_text = approved
        self.state.approved_awareness_at = time.time()
        self.state.awareness_show_count = 0
        logger.info("AwarenessJudge: 批准觉察文本 (%d chars, %.0f ms)", len(approved), elapsed_ms)
        return

    skip_reason = _extract_tag(text, "skip")
    logger.debug("AwarenessJudge: 跳过 — %s (%.0f ms)",
                 skip_reason or "无原因", elapsed_ms)
    # 保留旧缓存不变
```

- [ ] **Step 6: 更新 `consume_trigger()`**

在 `consume_trigger()` 中 `source == "reflector"` 分支的 `_set_state_l3(content)` 之后加入 `_maybe_judge_awareness()` 调用。

将第 211-212 行：
```python
        if source == "reflector":
            # Reflector 产出 → 直接设为 L3 State
            self._set_state_l3(content)
```

改为：
```python
        if source == "reflector":
            # Reflector 产出 → 直接设为 L3 State
            self._set_state_l3(content)
            # 触发异步 awareness 判定
            self._maybe_judge_awareness()
```

- [ ] **Step 7: 运行现有测试验证无回归**

Run: `cd /home/yomu/Agent && python -m pytest core/personality/tests/test_engine.py -v -q`
Expected: 所有现有测试 PASS。（`render_c9()` 的现有测试预期可能需要更新，因为 `approved_awareness_text` 默认为空，`render_c9()` 会返回空。检查 `test_c9_returns_state_when_triggered`）

- [ ] **Step 8: 修复受影响的现有测试**

`test_c9_returns_state_when_triggered` 测试设置了 `state_l3` 并通过 `consume_trigger` 触发，但现在 `render_c9()` 不再直接读 `state_l3`。需要改为设置 `approved_awareness_text`：

```python
def test_c9_returns_state_when_triggered(self):
    """approved_awareness_text 存在且触发条件满足时 C9 返回内容。"""
    s = CharacterState(session_id=SessionId("test-sid"))
    s.approved_awareness_text = "最近比较敷衍"
    engine = _make_engine(state=s)
    result = engine.render_c9()
    assert result.awareness_text == "最近比较敷衍"
```

- [ ] **Step 9: 提交**

```bash
git add core/personality/engine.py core/personality/tests/test_engine.py
git commit -m "feat: AwarenessJudge — LLM 语义判定替换纯时间冷却

- _awareness_allowed(): 缓存 + 展示次数 + 冷却三重门控
- render_c9(): 从 judge 缓存读取
- _maybe_judge_awareness(): fire-and-forget 触发判定
- _judge_awareness(): LLM 比较 L2 vs L3（降级/批准/跳过）
- consume_trigger(): reflector 源追加判定触发
- 修复 test_c9_returns_state_when_triggered 适配新接口"
```

---

### Task 4: 测试

**Files:**
- Modify: `core/personality/tests/test_engine.py`

**Interfaces:**
- Consumes: `CharacterEngine._judge_awareness()`, `_maybe_judge_awareness()`, `_awareness_allowed()`, `render_c9()`

- [ ] **Step 1: 添加 TestAwarenessJudge 类**

在文件末尾追加测试类：

```python
@pytest.mark.unit
class TestAwarenessJudge:
    """AwarenessJudge — LLM 语义判定测试。"""

    @staticmethod
    def _make_state(**kwargs):
        """创建测试用 CharacterState。"""
        s = CharacterState(session_id=SessionId("test-sid"))
        for k, v in kwargs.items():
            setattr(s, k, v)
        return s

    # ---- _judge_awareness ----

    def test_judge_approves_meaningful_deviation(self):
        """LLM 返回 <approved> 时更新缓存。"""
        s = self._make_state(
            self_l2="我比较直接，不太会撒娇。",
            state_l3="最近我发现自己更愿意示弱了。",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<approved>我变得更愿意在妖梦面前示弱了。</approved>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "我变得更愿意在妖梦面前示弱了。"
        assert s.approved_awareness_at is not None
        assert s.awareness_show_count == 0

    def test_judge_skip_preserves_cache(self):
        """LLM 返回 <skip> 时保留旧缓存。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="没什么变化。",
            approved_awareness_text="旧的觉察文本",
            approved_awareness_at=time.time(),
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<skip>没有新的变化</skip>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "旧的觉察文本"

    def test_judge_no_l2_self_fallback(self):
        """L2 为空时降级：L3 原文直接缓存，不调 LLM。"""
        s = self._make_state(
            self_l2="",
            state_l3="最近感觉自己变得更温柔了。",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "最近感觉自己变得更温柔了。"
        assert s.approved_awareness_at is not None
        engine._llm.think.assert_not_called()

    def test_judge_llm_failure_preserves_cache(self):
        """LLM 调用异常时保留旧缓存。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
            approved_awareness_text="旧文本",
            approved_awareness_at=time.time(),
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(side_effect=RuntimeError("API down"))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "旧文本"

    def test_judge_template_render_failure_no_crash(self):
        """模板渲染失败时不抛异常。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.side_effect = RuntimeError("模板错误")

        # 不应抛异常
        asyncio.run(engine._judge_awareness(s.state_l3))

    def test_judge_no_pm_returns_early(self):
        """_pm 为 None 时提前返回。"""
        s = self._make_state(
            self_l2="我比较直接。",
            state_l3="新的反思",
        )
        engine = _make_engine(state=s)
        engine._pm = None
        engine._llm.think = AsyncMock()

        asyncio.run(engine._judge_awareness(s.state_l3))

        engine._llm.think.assert_not_called()

    def test_judge_overwrites_old_cache_on_approve(self):
        """新 <approved> 替换旧缓存。"""
        s = self._make_state(
            self_l2="测试。",
            state_l3="新反思。",
            approved_awareness_text="旧觉察",
            approved_awareness_at=time.time() - 1000,
            awareness_show_count=2,
        )
        engine = _make_engine(state=s)
        engine._pm = MagicMock()
        engine._pm.render.return_value = "rendered prompt"
        engine._llm.think = AsyncMock(return_value=ThoughtResult(
            text="<approved>新觉察文本</approved>"
        ))

        asyncio.run(engine._judge_awareness(s.state_l3))

        assert s.approved_awareness_text == "新觉察文本"
        assert s.awareness_show_count == 0

    # ---- _maybe_judge_awareness ----

    def test_maybe_judge_empty_l3_skips(self):
        """L3 snapshot 为空时跳过。"""
        s = self._make_state(state_l3="")
        engine = _make_engine(state=s)
        # _schedule 不应该被调用
        engine._schedule = MagicMock()

        engine._maybe_judge_awareness()

        engine._schedule.assert_not_called()

    # ---- _awareness_allowed ----

    def test_allowed_empty_cache(self):
        """缓存为空时不允许。"""
        s = self._make_state(approved_awareness_text="")
        engine = _make_engine(state=s)
        assert not engine._awareness_allowed()

    def test_allowed_show_count_exceeded(self):
        """展示次数达到上限时不允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=3,
        )
        engine = _make_engine(state=s)
        assert not engine._awareness_allowed()

    def test_allowed_cooldown_not_met(self):
        """冷却未到时不允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine._last_awareness_time = time.time()  # 刚刚展示过
        assert not engine._awareness_allowed()

    def test_allowed_first_call_no_cooldown(self):
        """首次调用不受冷却限制。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        # _last_awareness_time == 0.0（默认），首次调用
        engine._last_awareness_time = 0.0
        assert engine._awareness_allowed()

    def test_allowed_cooldown_passed(self):
        """冷却过后允许。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine._last_awareness_time = time.time() - 301  # 5 分钟前
        assert engine._awareness_allowed()

    # ---- render_c9 ----

    def test_render_c9_returns_approved_text(self):
        """缓存有效时返回批准文本。"""
        s = self._make_state(
            approved_awareness_text="我变得更温柔了",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine._last_awareness_time = time.time() - 301

        result = engine.render_c9()

        assert result.awareness_text == "我变得更温柔了"
        assert s.awareness_show_count == 1
        assert s.last_awareness_time is not None

    def test_render_c9_empty_when_no_cache(self):
        """无缓存时返回空。"""
        s = self._make_state(approved_awareness_text="")
        engine = _make_engine(state=s)
        result = engine.render_c9()
        assert result.awareness_text == ""

    def test_render_c9_show_count_stops(self):
        """展示满 3 次后返回空。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=3,
        )
        engine = _make_engine(state=s)
        result = engine.render_c9()
        assert result.awareness_text == ""

    def test_render_c9_cooldown_blocks(self):
        """冷却期内返回空。"""
        s = self._make_state(
            approved_awareness_text="有内容",
            awareness_show_count=0,
        )
        engine = _make_engine(state=s)
        engine._last_awareness_time = time.time()  # 刚刚展示
        result = engine.render_c9()
        assert result.awareness_text == ""
```

- [ ] **Step 2: 运行新测试验证通过**

Run: `cd /home/yomu/Agent && python -m pytest core/personality/tests/test_engine.py -v -k "AwarenessJudge"`
Expected: 所有新测试 PASS (~17 tests)

- [ ] **Step 3: 运行全量 personality 测试**

Run: `cd /home/yomu/Agent && python -m pytest core/personality/tests/ -v`
Expected: 全部 PASS

- [ ] **Step 4: 提交**

```bash
git add core/personality/tests/test_engine.py
git commit -m "test: 新增 TestAwarenessJudge — 17 个单元测试覆盖 Judge/门控/降级"
```

---

### Task 5: 文档更新

**Files:**
- Modify: `core/personality/README.md:112-119`
- Modify: `docs/completed.md:5-13`
- Modify: `todo.md:42`

- [ ] **Step 1: 更新 `core/personality/README.md`**

将第 112-119 行的 AwarenessTrigger 章节替换为：

```markdown
### AwarenessTrigger（C9 激活判定）

Phase 2: LLM-judge 语义判定。Reflector 产出 L3 后，异步 judge 比较 L2 vs L3：

1. **降级模式**：L2 为空时，L3 原文直接缓存（不调 LLM）
2. **Judge 判定**：L2 非空时，渲染 `AwarenessJudge.j2` → LLM 判断是否批准
   - `<approved>`：编辑后文本写入缓存，替换旧值
   - `<skip>`：保留旧缓存不变
3. **`render_c9()` 三重门控**：
   - 缓存非空
   - 展示次数 < 3
   - 距上次展示 >= 5 分钟
```

- [ ] **Step 2: 更新 `docs/completed.md`**

在 2026-07-22 条目的"其他"行追加：

```
AwarenessJudge: C9 LLM 语义判定（异步 Judge 比较 L2 vs L3，缓存 + 冷却 + 展示次数门控）
```

- [ ] **Step 3: 更新 `todo.md`**

将第 44 行：
```
  - AwarenessTrigger: L2/L3 语义偏差检测（向量相似度替代轮次冷却）
```

改为：
```
  - ✅ ~~AwarenessTrigger: L2/L3 语义偏差检测~~ — LLM-judge 驱动（`_judge_awareness` + `AwarenessJudge.j2`）
```

- [ ] **Step 4: 运行全量 system prompt 测试确认 C9 接口不变**

Run: `cd /home/yomu/Agent && python -m pytest Module/system_prompt/tests/ -v -q`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add core/personality/README.md docs/completed.md todo.md
git commit -m "docs: AwarenessJudge 文档更新（README + completed + todo）"
```

---

## Verification

```bash
# 全量 personality 测试
pytest core/personality/tests/ -v

# 全量 system prompt 测试（C9 接口不变）
pytest Module/system_prompt/tests/ -v

# Schema 测试
pytest core/schema/tests/ -v
```
