# Debug 页面 — CharacterReflector 展示 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 debug 页面新增 CharacterReflector 卡片，展示反射状态、self-perception 历史、最近反思 prompt/LLM 调用结果

**Architecture:** 三层变更：CharacterReflector 新增 8 个只读 property + `_reflect()` 记录元数据 → DebugInfo 新增 10 个字段 + `collect()` 读取 → debug.html 新增全宽卡片

**Tech Stack:** Python 3.13, asyncio, pytest, vanilla JS, conda (Elysia)

## Global Constraints

- `conda` 环境: `source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia`
- 用中文写 commit message
- `reflector_exists` 字段用于前端兼容性检测——dataclass 默认值不可靠
- `document.getElementById()` 直接使用，不引入 `$()` helper
- prompt 截断到 1000 字符（debug 响应大小控制）
- CharacterReflector 核心逻辑不动（只加只读属性和元数据记录）

---

### Task 1: CharacterReflector 新增只读属性 + 元数据记录

**Files:**
- Modify: `core/character_reflector.py`
- Modify: `core/tests/test_character_reflector.py`

**Interfaces:**
- Produces: 8 个只读 property — `buffer_size`, `locked`, `last_reflection_at`, `last_success`, `last_duration_ms`, `last_text_len`, `last_prompt_system`, `last_prompt_user`
- Consumes: 无新增依赖

- [ ] **Step 1: 写失败测试**

在 `core/tests/test_character_reflector.py` 的 `TestCharacterReflector` 类中新增测试方法：

```python
    # ---- debug 属性 ----

    def test_buffer_size_property(self, reflector):
        """buffer_size 返回当前缓冲轮数。"""
        assert reflector.buffer_size == 0
        reflector._buffer.append(("hi", "hello"))
        assert reflector.buffer_size == 1

    def test_locked_property(self, reflector):
        """locked 在无锁时为 False。"""
        assert reflector.locked is False

    @pytest.mark.asyncio
    async def test_last_reflection_at_updated_after_reflect(self, reflector, llm):
        """last_reflection_at 在反思后更新。"""
        assert reflector.last_reflection_at is None
        llm.think.return_value = MagicMock(text="新的感受")
        for i in range(50):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert reflector.last_reflection_at is not None
        assert reflector.last_success is True
        assert reflector.last_text_len > 0

    @pytest.mark.asyncio
    async def test_last_success_false_on_llm_failure(self, reflector, llm):
        """LLM 调用失败时 last_success 为 False。"""
        llm.think.side_effect = Exception("模拟失败")
        reflector._session._params["self_perception_history"] = [
            {"timestamp": time.time() - 25*3600, "content": "旧"}
        ]
        for i in range(10):
            await reflector.buffer(f"msg{i}", f"reply{i}")
        await asyncio.sleep(0.1)
        assert reflector.last_success is False
        assert reflector.last_duration_ms > 0

    def test_last_prompt_truncated(self, reflector, llm):
        """prompt 字段在反思后记录并截断到 1000 字符。"""
        pass  # 验证通过 integration 测试
```

- [ ] **Step 2: 运行测试确认失败**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/test_character_reflector.py::TestCharacterReflector -v -k "buffer_size or locked or last_reflection" 2>&1
```

预期: FAIL — 属性不存在

- [ ] **Step 3: 实现 CharacterReflector 属性**

在 `core/character_reflector.py` 的 `__init__` 末尾添加：

```python
        # ---- debug 状态 ----
        self._last_reflection_at: float | None = None
        self._last_success: bool = False
        self._last_duration_ms: float = 0.0
        self._last_text_len: int = 0
        self._last_prompt_system: str = ""
        self._last_prompt_user: str = ""
```

在类体中添加 8 个只读 property（放在 `flush()` 之后、`_should_trigger()` 之前）：

```python
    # ---- debug 属性 ----

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def locked(self) -> bool:
        return self._reflect_lock.locked()

    @property
    def last_reflection_at(self) -> float | None:
        return self._last_reflection_at

    @property
    def last_success(self) -> bool:
        return self._last_success

    @property
    def last_duration_ms(self) -> float:
        return self._last_duration_ms

    @property
    def last_text_len(self) -> int:
        return self._last_text_len

    @property
    def last_prompt_system(self) -> str:
        return self._last_prompt_system

    @property
    def last_prompt_user(self) -> str:
        return self._last_prompt_user
```

- [ ] **Step 4: 在 `_reflect()` 中记录元数据**

修改 `core/character_reflector.py` 中的 `_reflect()` 方法。在 LLM 调用前后添加计时和记录：

```python
    async def _reflect(self) -> None:
        """执行 LLM 反思并存储结果。"""
        if self._pm is None:
            return

        pm = self._pm

        history = self._session.get_param("self_perception_history", [])
        last_perception = history[-1]["content"] if history else ""

        system = pm.render_macro(
            "CharacterReflector.j2", "CharacterReflectorSystemPrompt",
            character_name=self._character_name,
            user_name=self._user_name,
        )
        user_prompt = pm.render_macro(
            "CharacterReflector.j2", "CharacterReflectorUserPrompt",
            character_name=self._character_name,
            user_name=self._user_name,
            last_self_perception=last_perception,
            recent_conversations=self._context,
        )

        # 记录 prompt（截断到 1000 字符）
        self._last_prompt_system = system[:1000]
        self._last_prompt_user = user_prompt[:1000]

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
        except Exception:
            self._last_duration_ms = (time.perf_counter() - t0) * 1000
            self._last_success = False
            self._last_reflection_at = time.time()
            logger.warning("CharacterReflector: LLM 调用失败", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # LLM 调用成功后才清空 buffer
        self._buffer.clear()

        if not text:
            self._last_duration_ms = elapsed_ms
            self._last_success = True
            self._last_text_len = 0
            self._last_reflection_at = time.time()
            logger.debug("CharacterReflector: 反思结果为空，跳过")
            return

        # 追加到历史
        entry = {"timestamp": time.time(), "content": text}
        current_history = self._session.get_param("self_perception_history", [])
        current_history.append(entry)
        if len(current_history) > self.MAX_HISTORY:
            current_history = current_history[-self.MAX_HISTORY:]
        self._session.set_param("self_perception_history", current_history)

        # 记录 debug 元数据
        self._last_success = True
        self._last_duration_ms = elapsed_ms
        self._last_text_len = len(text)
        self._last_reflection_at = time.time()

        logger.info("CharacterReflector: 性格反思完成 (%d chars, %.0f ms)",
                    len(text), elapsed_ms)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/test_character_reflector.py -v 2>&1
```

预期: 12 passed（原有 8 + 新增 4）

- [ ] **Step 6: Commit**

```bash
git add core/character_reflector.py core/tests/test_character_reflector.py
git commit -m "feat: CharacterReflector 新增 debug 属性 — 8 个只读 property + 元数据记录"
```

---

### Task 2: DebugInfo 新增字段 + collect() 适配

**Files:**
- Modify: `core/debug_info.py`

**Interfaces:**
- Consumes: `CharacterReflector.buffer_size`, `.locked`, `.last_reflection_at`, `.last_success`, `.last_duration_ms`, `.last_text_len`, `.last_prompt_system`, `.last_prompt_user`
- Consumes: `session.get_param("self_perception_history")`
- Produces: `DebugInfo` 新字段通过 `/debug/api` JSON 返回

- [ ] **Step 1: 添加 DebugInfo dataclass 字段**

在 `core/debug_info.py` 的 `DebugInfo` dataclass 中，Consolidation 字段之后添加：

```python
    # Character Reflector
    reflector_exists: bool = False
    reflector_buffer_size: int = 0
    reflector_last_reflection_at: float | None = None
    reflector_locked: bool = False
    self_perception_history: list[dict] = field(default_factory=list)
    reflector_last_prompt_system: str = ""
    reflector_last_prompt_user: str = ""
    reflector_last_success: bool = False
    reflector_last_duration_ms: float = 0.0
    reflector_last_text_len: int = 0
```

- [ ] **Step 2: 在 `collect()` 中填充新字段**

在 `core/debug_info.py` 的 `collect()` 函数中，Character 字段之后（大致 line 127 之后）添加：

```python
    # ---- Character Reflector ----
    ref = getattr(agent, "_character_reflector", None)
    if ref is not None:
        info.reflector_exists = True
        info.reflector_buffer_size = ref.buffer_size
        info.reflector_last_reflection_at = ref.last_reflection_at
        info.reflector_locked = ref.locked
        info.reflector_last_prompt_system = ref.last_prompt_system[:1000]
        info.reflector_last_prompt_user = ref.last_prompt_user[:1000]
        info.reflector_last_success = ref.last_success
        info.reflector_last_duration_ms = ref.last_duration_ms
        info.reflector_last_text_len = ref.last_text_len

    history = agent._session.get_param("self_perception_history", [])
    info.self_perception_history = list(history[-5:])
```

- [ ] **Step 3: 验证 — 运行 debug_info 相关测试**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/ -v -k "debug" 2>&1
```

（如果没有 debug 相关的独立测试，手动验证 import 和 dataclass 实例化无报错）

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -c "
from core.debug_info import DebugInfo, collect, asdict
# 验证新字段存在且 asdict 正常
d = DebugInfo()
d.reflector_exists = True
d.self_perception_history = [{'timestamp': 1.0, 'content': 'test'}]
j = asdict(d)
assert j['reflector_exists'] == True
assert len(j['self_perception_history']) == 1
print('OK: DebugInfo 新字段正常')
"
```

- [ ] **Step 4: Commit**

```bash
git add core/debug_info.py
git commit -m "feat: DebugInfo 新增 CharacterReflector 字段 + collect() 适配"
```

---

### Task 3: debug.html 新增 Character Reflector 卡片

**Files:**
- Modify: `static/debug.html`

**Interfaces:**
- Consumes: `/debug/api` JSON 响应中的 `reflector_exists`, `reflector_buffer_size`, `reflector_locked`, `reflector_last_reflection_at`, `self_perception_history`, `reflector_last_prompt_system`, `reflector_last_prompt_user`, `reflector_last_success`, `reflector_last_duration_ms`, `reflector_last_text_len`

- [ ] **Step 1: 在 `render()` 函数中添加 Character Reflector 卡片**

在 `core/static/debug.html` 的 `render()` 函数中，Row 4 之后（`];` 结束 Row 4 的那一行之后）插入新 Row：

```javascript
    // Row 5: Character Reflector
    [
      (() => {
        const ref = info.reflector_exists ? info : null;
        const items = [];

        // 状态行
        items.push(
          `<div class="row"><span class="label">Buffer</span><span class="value">${ref ? ref.reflector_buffer_size + '/50' : 'N/A'}</span></div>`,
          `<div class="row"><span class="label">上次反思</span><span class="value">${ref && ref.reflector_last_reflection_at ? new Date(ref.reflector_last_reflection_at * 1000).toLocaleTimeString() : '从未'}</span></div>`,
        );
        const locked = ref && ref.reflector_locked;
        items.push(
          `<div class="row"><span class="label">状态</span><span class="value ${locked ? 'warn' : 'ok'}">${locked ? '反思中...' : 'idle'}</span></div>`,
        );

        // Self-Perception 历史
        const hist = (ref && ref.self_perception_history) || [];
        if (hist.length === 0) {
          items.push('<div class="empty" style="margin-top:4px">Self-Perception: 暂无</div>');
        } else {
          let histHtml = '<details style="margin-top:4px"><summary>Self-Perception 历史 (' + hist.length + ')</summary>';
          histHtml += '<div style="max-height:200px;overflow-y:auto">';
          hist.reverse().forEach((h, i) => {
            const ts = new Date(h.timestamp * 1000).toLocaleString();
            const isLatest = i === 0;
            const bg = isLatest ? 'background:rgba(233,69,96,0.08);border-left:2px solid var(--accent);padding-left:6px' : '';
            histHtml += '<div class="list-item" style="' + bg + '">';
            histHtml += '<span style="color:var(--muted);font-size:11px">' + esc(ts) + '</span><br>';
            histHtml += '<details><summary style="cursor:pointer">' + esc(trunc(h.content, 60)) + '</summary>';
            histHtml += '<div style="white-space:pre-wrap;margin-top:3px">' + esc(h.content) + '</div>';
            histHtml += '</details></div>';
          });
          histHtml += '</div></details>';
          items.push(histHtml);
        }

        // 最近反思 Prompt
        if (ref && (ref.reflector_last_prompt_system || ref.reflector_last_prompt_user)) {
          let promptHtml = '<details style="margin-top:4px"><summary>最近反思 Prompt</summary>';
          if (ref.reflector_last_prompt_system) {
            promptHtml += '<div style="font-size:11px;color:var(--muted);margin-top:4px">System:</div>';
            promptHtml += '<div class="list-item" style="white-space:pre-wrap;max-height:120px;overflow-y:auto;font-size:11px">' + esc(trunc(ref.reflector_last_prompt_system, 400)) + '</div>';
          }
          if (ref.reflector_last_prompt_user) {
            promptHtml += '<div style="font-size:11px;color:var(--muted);margin-top:4px">User:</div>';
            promptHtml += '<div class="list-item" style="white-space:pre-wrap;max-height:120px;overflow-y:auto;font-size:11px">' + esc(trunc(ref.reflector_last_prompt_user, 400)) + '</div>';
          }
          // 调用结果
          if (ref.reflector_last_text_len !== undefined) {
            const ok = ref.reflector_last_success;
            promptHtml += '<div class="row" style="margin-top:4px"><span class="label">调用</span>';
            promptHtml += '<span class="value ' + (ok ? 'ok' : 'fail') + '">';
            promptHtml += (ok ? '成功' : '失败') + ' | ' + ref.reflector_last_duration_ms.toFixed(0) + 'ms | ' + ref.reflector_last_text_len + ' chars';
            promptHtml += '</span></div>';
          }
          promptHtml += '</details>';
          items.push(promptHtml);
        }

        return { title:'Character Reflector', wide:true, items: items };
      })()
    ],
```

注意：Row 4 结束行是 `],`，在其后插入上面的代码，然后在 `];` 结束 cards 数组之前。

- [ ] **Step 2: 验证 debug.html 语法**

```bash
# 无 JS 语法检查工具，确认 HTML 在浏览器中无报错（手动验证）
```

- [ ] **Step 3: Commit**

```bash
git add static/debug.html
git commit -m "feat: debug 页面新增 Character Reflector 卡片"
```

---

## 验证

完成后运行完整测试套件：

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/test_character_reflector.py Module/prompt/test_prompt.py -v 2>&1
```

预期: 20 passed（12 reflector + 8 prompt）
