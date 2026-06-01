# Debug 页面改进 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 debug.html 的 2 个数据缺口、1 个死代码、2 个 bug，加 5 个 UX 优化，全部在单文件内完成。

**Architecture:** 增量修补 `static/debug.html`，不拆文件。改动集中在一处，分组为 4 个 Task。

**Tech Stack:** Vanilla HTML/CSS/JS

---

### Task 1: 数据补全 — session_params、llm_mock 修复、工具参数 description

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 在 Memory 卡片添加 session_params 行**

在 Memory 卡片的 `rows` 数组中，`['Character', ...]` 行**之后**加入 `['Session Params', ...]`。

找到 Memory 卡片定义（约第 110-119 行），修改为：

```js
{ title:'Memory', wide:false, rows:[
  ['Session Messages', info.session_messages],
  ['Session Params', esc(trunc(JSON.stringify(info.session_params || {}), 80))],
  ['Long-term Entries', info.lt_count],
  ['MongoDB', ok(info.mongodb_ok)],
  ['Milvus', ok(info.milvus_ok)],
  ['Embedding', ok(info.embedding_ok)],
  ['Character', info.character_name + ' (' + (info.character_traits||[]).join(', ') + ')'],
]},
```

- [ ] **Step 2: 修复 llm_mock 检测**

找到 System 卡片中的 LLM Model 行（约第 108 行），将 `info.llm_mock` 改为检查 model 字符串：

```js
['LLM Model', esc(info.llm_model) + (
  /mock/i.test(info.llm_model || '') ? ' <span class="warn">MOCK</span>' : ''
)],
```

- [ ] **Step 3: 工具参数加 description title**

找到 Tools 卡片中参数渲染的部分（约第 178-181 行），给 `.param-tag` 加 `title` 属性：

```js
`<span class="param-tag" title="${esc(p.description || '')}">${esc(p.name)}<i>${esc(p.type)}</i>${p.required?'*':''}</span>`
```

- [ ] **Step 4: Commit**

```bash
git add static/debug.html
git commit -m "fix: add session_params display, fix llm_mock detection, add tool param description tooltip"
```

---

### Task 2: XSS 防护 — 所有动态值用 esc() 包裹

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 审查并修复所有未 escape 的动态值**

以下位置的值当前直接拼入 HTML，需加 `esc()`：

**System 卡片:**
```js
['FSM State', `<span class="${info.fsm_state==='idle'?'ok':'warn'}">${esc(info.fsm_state)}</span>`],
['Previous', esc(info.fsm_previous||'—')],
```

**执行中 banner:**
```js
`Executing: ${info.currently_executing.map(e=>esc(e)).join(', ')}`
```

**工具卡片 (name, description):**
```js
`<span class="tool-name">${esc(t.name)}</span>`
`<span class="tool-desc">${esc(t.description)}</span>`
```

**Tool History (name, args, result, error):**
```js
`<span class="tool-name">${esc(t.name)}</span> <span class="tool-args">${esc(JSON.stringify(t.args))}</span>`
// result
`→ ${esc(trunc(t.result||'',120))}`
// error
`<span class="tool-error">✗ ${esc(t.error)}</span>`
```

**Consolidation (type, content):**
```js
`<span class="type-tag">${esc(c.type||'?')}</span>${esc(trunc(c.content,100))}`
```

**Session Recent (role, content) — role 已经是预定义值，content 需要 escape:**
```js
`<span class="role-tag ${m.role}">${m.role}</span>${esc(trunc(m.content,90))}`
```

**Long-term Memory (metadata.type, text):**
```js
`<span class="type-tag">${esc(e.metadata?.type||'?')}</span>${esc(trunc(e.text,90))}`
```

逐一在文件中找到并包裹 `esc()`。

- [ ] **Step 2: Commit**

```bash
git add static/debug.html
git commit -m "fix: escape all dynamic values in debug page to prevent XSS"
```

---

### Task 3: 轮询改进 — 退避 + 暂停/恢复

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 添加轮询退避逻辑**

替换现有的全局变量和 `poll()` 函数：

```js
const POLL_MS = 2000;
const POLL_MAX_MS = 30000;
let timer = null;
let pollInterval = POLL_MS;
let paused = false;
let lastInfo = null;

async function poll() {
  if (paused) return;
  try {
    const resp = await fetch('/debug/api');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const info = await resp.json();
    lastInfo = info;
    render(info);
    pollInterval = POLL_MS;  // 成功后重置
  } catch(e) {
    showError('无法连接: ' + e.message);
    pollInterval = Math.min(pollInterval * 2, POLL_MAX_MS);
  }
  scheduleNext();
}

function scheduleNext() {
  if (timer) clearTimeout(timer);
  if (paused) return;
  timer = setTimeout(poll, pollInterval);
}
```

同时移除旧的 `setInterval` 调用，首次调用改为 `poll()`:

```js
poll();
```

- [ ] **Step 2: 添加暂停/恢复按钮**

在 HTML `<div class="header">` 内，`#updated` span 旁边加按钮：

```html
<div class="header">
  <div>
    <h1>Elysia Debug</h1>
    <div class="sub">内部状态监控 — <span id="updated">等待数据...</span></div>
  </div>
  <div style="display:flex;gap:8px;">
    <button id="pause-btn" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;">暂停</button>
    <button id="copy-btn" style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 12px;border-radius:4px;cursor:pointer;font-size:12px;">复制 JSON</button>
  </div>
</div>
```

JS 中添加暂停逻辑：

```js
document.getElementById('pause-btn').onclick = () => {
  paused = !paused;
  const btn = document.getElementById('pause-btn');
  if (paused) {
    btn.textContent = '恢复';
    if (timer) { clearTimeout(timer); timer = null; }
  } else {
    btn.textContent = '暂停';
    poll();  // 立即刷新
  }
};
```

- [ ] **Step 3: Commit**

```bash
git add static/debug.html
git commit -m "feat: add poll backoff, pause/resume button to debug page"
```

---

### Task 4: UX 优化 — 跳过无变化渲染、导出、可展开消息、响应式

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 添加跳过无变化渲染**

在 `render(info)` 函数开头加浅比较：

```js
function render(info) {
  document.getElementById('updated').textContent = new Date().toLocaleTimeString();
  document.getElementById('error-banner').style.display = 'none';

  // 关键字段未变化则跳过 DOM 重建
  if (lastInfo) {
    const same = (
      lastInfo.fsm_state === info.fsm_state &&
      lastInfo.processing === info.processing &&
      lastInfo.session_messages === info.session_messages &&
      lastInfo.lt_count === info.lt_count &&
      lastInfo.ws_clients === info.ws_clients &&
      JSON.stringify(lastInfo.currently_executing) === JSON.stringify(info.currently_executing) &&
      JSON.stringify(lastInfo.timing_stats) === JSON.stringify(info.timing_stats)
    );
    if (same) return;
  }
  lastInfo = info;

  // ... 原有的 cards 构建和 innerHTML 赋值 ...
```

- [ ] **Step 2: 添加复制 JSON 按钮**

```js
document.getElementById('copy-btn').onclick = async () => {
  if (!lastInfo) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(lastInfo, null, 2));
    const btn = document.getElementById('copy-btn');
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制 JSON'; }, 2000);
  } catch { /* clipboard denied */ }
};
```

- [ ] **Step 3: 消息可展开/收起**

给截断文本加 class 和 onclick。修改 `trunc()` 使用方式，引入新的 helper：

```js
function expandable(text, maxLen) {
  if (!text || text.length <= maxLen) return esc(text);
  const id = 'ex-' + Math.random().toString(36).slice(2, 8);
  return `<span id="${id}" class="truncated" onclick="
    const el=document.getElementById('${id}');
    if(el.classList.contains('truncated')){
      el.textContent='${esc(text).replace(/'/g, "\\'")}';
      el.classList.remove('truncated');
    }else{
      el.textContent='${esc(text.slice(0, maxLen)).replace(/'/g, "\\'")}…';
      el.classList.add('truncated');
    }
  " style="cursor:pointer;" title="点击展开/收起">${esc(text.slice(0, maxLen))}…</span>`;
}
```

在 Session Recent、Long-term Memory、Consolidation 等处使用 `expandable()` 替代 `esc(trunc(...))`。

加 CSS 样式：

```css
.truncated { color: var(--text); }
.truncated:hover { color: #fff; }
```

- [ ] **Step 4: 添加响应式 CSS**

在 `</style>` 前加：

```css
@media (max-width: 700px) {
  .grid { grid-template-columns: 1fr; }
  .card-wide { grid-column: auto; }
  body { padding: 10px; font-size: 12px; }
  .header { flex-direction: column; gap: 8px; }
}
```

- [ ] **Step 5: Commit**

```bash
git add static/debug.html
git commit -m "feat: skip unchanged render, export JSON, expandable messages, responsive debug page"
```

---

## Verification

```bash
# 启动服务
python app.py --server

# 浏览器打开 /debug
# 1. Memory 卡片 → 确认 session_params 显示
# 2. System 卡片 → 确认 llm_model 的 mock 检测生效
# 3. Tools 卡片 → hover 参数 → 确认 description tooltip
# 4. 确认所有动态值安全渲染（无 raw HTML 注入迹象）
# 5. 断开网络 → 确认退避重试 → 恢复网络 → 确认重置为 2s
# 6. 点击暂停 → 轮询停止 → 点击恢复 → 立即刷新
# 7. 点击复制 JSON → 确认剪贴板有完整数据
# 8. 点击截断消息 → 展开/收起
# 9. 缩小窗口至 <700px → 单列布局
# 10. 打开控制台 → 确认相同数据不触发 DOM 重绘（innerHTML 不闪烁）
```
