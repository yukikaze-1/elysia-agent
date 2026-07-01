# Debug 页面 — CharacterReflector 展示

**日期**: 2026-07-02
**来源**: 性格演化系统完成后 debug 页面缺少反射状态展示

---

## 目标

在 debug 页面新增 CharacterReflector 卡片，展示：
1. 反射运行状态（buffer 轮数、上次反思时间、是否正在反思）
2. Self-Perception 历史（最近 5 条，可展开）
3. 最近一次反思的 prompt + LLM 调用结果

---

## 一、后端：CharacterReflector 新增只读属性

`core/character_reflector.py` 新增：

```python
# 在 __init__ 中初始化:
self._last_reflection_at: float | None = None
self._last_success: bool = False
self._last_duration_ms: float = 0.0
self._last_text_len: int = 0
self._last_prompt_system: str = ""
self._last_prompt_user: str = ""

# 只读 property:
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

**`_reflect()` 中更新**：
- 成功时：记录 `_last_reflection_at`、`_last_success=True`、`_last_duration_ms`、`_last_text_len`、`_last_prompt_system`、`_last_prompt_user`
- 失败时：记录 `_last_success=False`、`_last_duration_ms`（不更新 prompt 字段）
- prompt 截断到 1000 字符（避免 debug 响应过大）

---

## 二、后端：DebugInfo 新增字段

`core/debug_info.py` 的 `DebugInfo` dataclass 新增：

```python
reflector_buffer_size: int = 0
reflector_last_reflection_at: float | None = None
reflector_locked: bool = False
self_perception_history: list[dict] = field(default_factory=list)   # 最近 5 条
reflector_last_prompt_system: str = ""
reflector_last_prompt_user: str = ""
reflector_last_success: bool = False
reflector_last_duration_ms: float = 0.0
reflector_last_text_len: int = 0
```

**`collect()` 中读取**：

```python
if agent._character_reflector:
    ref = agent._character_reflector
    info.reflector_buffer_size = ref.buffer_size
    info.reflector_last_reflection_at = ref.last_reflection_at
    info.reflector_locked = ref.locked
    info.reflector_last_prompt_system = ref.last_prompt_system[:1000]
    info.reflector_last_prompt_user = ref.last_prompt_user[:1000]
    info.reflector_last_success = ref.last_success
    info.reflector_last_duration_ms = ref.last_duration_ms
    info.reflector_last_text_len = ref.last_text_len

history = agent._session.get_param("self_perception_history", [])
info.self_perception_history = history[-5:]  # 最近 5 条
```

`asdict()` 自动包含新字段。

---

## 三、前端：debug.html 新增卡片

在 Overview 标签页，Consolidation 卡片之后新增 Character Reflector 卡片。

### 3.1 HTML 结构

```html
<div class="card card-wide" id="card-reflector">
  <h2>Character Reflector</h2>
  <div class="row"><span class="label">Buffer</span><span class="value" id="ref-buffer">-/50</span></div>
  <div class="row"><span class="label">上次反思</span><span class="value" id="ref-last">-</span></div>
  <div class="row"><span class="label">状态</span><span class="value" id="ref-status">-</span></div>

  <details style="margin-top:8px">
    <summary>Self-Perception 历史 (<span id="ref-hist-count">0</span>)</summary>
    <div id="ref-history" style="max-height:300px;overflow-y:auto"></div>
  </details>

  <details style="margin-top:4px">
    <summary>最近反思 Prompt</summary>
    <div id="ref-prompt-system" class="list-item" style="white-space:pre-wrap;max-height:200px;overflow-y:auto"></div>
    <div id="ref-prompt-user" class="list-item" style="white-space:pre-wrap;max-height:200px;overflow-y:auto;margin-top:4px"></div>
    <div class="row" style="margin-top:4px">
      <span class="label">调用结果</span>
      <span class="value" id="ref-call-result">-</span>
    </div>
  </details>
</div>
```

### 3.2 JS 更新函数

在 poll 回调中更新：

```javascript
// Character Reflector
const ref = info.reflector_buffer_size !== undefined ? info : null;

// 状态行
$('ref-buffer').textContent = ref ? `${ref.reflector_buffer_size}/50` : 'N/A';
$('ref-last').textContent = ref && ref.reflector_last_reflection_at
  ? new Date(ref.reflector_last_reflection_at * 1000).toLocaleString()
  : '从未';
const locked = ref && ref.reflector_locked;
$('ref-status').textContent = locked ? '⏳ 反思中...' : 'idle';
$('ref-status').className = 'value ' + (locked ? 'warn' : 'ok');

// Self-Perception 历史
const hist = (ref && ref.self_perception_history) || [];
$('ref-hist-count').textContent = hist.length;
const histDiv = $('ref-history');
histDiv.innerHTML = hist.length === 0
  ? '<div class="list-item" style="color:var(--muted)">暂无</div>'
  : hist.map((h, i) => {
      const ts = new Date(h.timestamp * 1000).toLocaleString();
      const isLatest = i === hist.length - 1;
      const bg = isLatest ? 'background:rgba(233,69,96,0.08)' : '';
      return `<div class="list-item" style="${bg}">
        <span style="color:var(--muted)">${ts}</span>
        <details><summary style="cursor:pointer">${esc(trunc(h.content, 80))}</summary>
        <div style="white-space:pre-wrap;margin-top:4px;padding-left:8px;border-left:2px solid var(--border)">${esc(h.content)}</div>
        </details>
      </div>`;
    }).join('');

// 最近反思 Prompt
$('ref-prompt-system').textContent = ref ? trunc(ref.reflector_last_prompt_system || '(无)', 500) : '';
$('ref-prompt-user').textContent = ref ? trunc(ref.reflector_last_prompt_user || '(无)', 500) : '';
if (ref && ref.reflector_last_success !== undefined && ref.reflector_last_text_len !== undefined) {
  const ok = ref.reflector_last_success;
  $('ref-call-result').innerHTML = ok
    ? `<span class="ok">✅ 成功</span> | ${ref.reflector_last_duration_ms.toFixed(0)}ms | ${ref.reflector_last_text_len} chars`
    : `<span class="fail">❌ 失败</span> | ${ref.reflector_last_duration_ms.toFixed(0)}ms`;
} else {
  $('ref-call-result').textContent = '-';
}
```

旧字段兼容：`info.reflector_buffer_size === undefined` 时所有字段显示 N/A 或 `-`。

---

## 四、不变

- 已有卡片布局不动
- `/debug/api` 的已有字段不变
- CharacterReflector 的核心逻辑不动（只加只读属性）
- `$()` = `document.getElementById()`（debug 页面已有此 helper）
- `esc()` / `trunc()` 复用 debug 页面已有函数

---

## 五、测试

1. CharacterReflector 新属性单元测试：
   - `buffer_size` 随 buffer() 递增
   - `locked` 在反思中为 True
   - `last_reflection_at` / `last_success` / `last_duration_ms` / `last_text_len` 在反思后正确
   - `last_prompt_system` / `last_prompt_user` 在反思后记录（截断至 1000）

2. debug_info 测试：
   - `collect()` 正确填入新字段
   - `self_perception_history` 只取最近 5 条
   - reflector 为 None 时所有字段 fallback 到默认值

3. 已有测试回归：
   - `core/tests/test_character_reflector.py` — 8 passed
   - `core/tests/test_phase3.py` — 109 passed
