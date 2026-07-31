# inner_voice Toggle 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置面板添加"内心独白"开关，控制 inner_voice 气泡的显示/隐藏

**Architecture:** 纯前端改动，对标已有 TTS/唤醒词/Live2D toggle 模式。`state.innerVoiceEnabled` 驱动 `showInnerVoice()` guard + CSS `body.inner-voice-hidden` 隐藏已渲染气泡。localStorage 持久化，刷新不丢偏好。

**Tech Stack:** Vanilla JS (ES modules), CSS, HTML — 无框架、无构建步骤

## Global Constraints

- 后端 inner_voice 始终生成、存储、注入 LLM 历史——toggle 只影响前端渲染
- debug 页面 (`static/debug.html`) 始终显示 inner_voice——不受 toggle 影响
- 默认开启（`!== 'off'` 模式，和 TTS/Live2D 一致）
- 不引入新依赖

---

### Task 1: 存储 key + 状态初始化

**Files:**
- Modify: `static/js/config.js:30`
- Modify: `static/js/state.js:47` (after `live2dEnabled` line)

**Interfaces:**
- Produces: `SETTINGS_STORAGE_KEYS.INNER_VOICE = 'elysia_inner_voice'`
- Produces: `state.innerVoiceEnabled: bool` — 从 localStorage 恢复，默认 `true`

- [ ] **Step 1: config.js 添加 localStorage key**

`static/js/config.js` 第 30 行（`};` 之前）添加：

```javascript
  INNER_VOICE: 'elysia_inner_voice',
```

- [ ] **Step 2: state.js 添加 innerVoiceEnabled 字段**

`static/js/state.js` 在 `this.live2dEnabled = ...`（第 47 行）之后添加：

```javascript
    // 内心独白（默认开启）
    this.innerVoiceEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.INNER_VOICE) !== 'off';
```

- [ ] **Step 3: 验证**

在浏览器 console 验证：

```javascript
// 预期: true（首次加载，localStorage 无值）
state.innerVoiceEnabled
// 预期: undefined → true（`!== 'off'` 语义）
localStorage.getItem('elysia_inner_voice')
```

- [ ] **Step 4: Commit**

```bash
git add static/js/config.js static/js/state.js
git commit -m "feat: inner_voice toggle — 存储 key + 状态初始化"
```

---

### Task 2: 设置面板 toggle UI + 交互

**Files:**
- Modify: `static/index.html:48` (after Live2D toggle row)
- Modify: `static/js/settings.js:48` (after Live2D toggle handler)

**Interfaces:**
- Consumes: `SETTINGS_STORAGE_KEYS.INNER_VOICE` (Task 1)
- Consumes: `state.innerVoiceEnabled` (Task 1)
- Produces: toggle DOM `#inner-voice-toggle` — click → flip state + localStorage + body class

- [ ] **Step 1: index.html 添加 toggle 行**

`static/index.html` 第 48 行（`<div class="row"><span>Live2D</span>...` 之后）添加：

```html
          <div class="row"><span>内心独白</span><div class="toggle on" id="inner-voice-toggle"></div></div>
```

- [ ] **Step 2: settings.js 添加 toggle 初始化 + 事件**

`static/js/settings.js` 在 Live2D toggle 处理块（`document.getElementById('live2d-toggle').onclick = ...`）之后，音量之前，添加：

```javascript
  // 内心独白 toggle — 初始状态同步
  if (!state.innerVoiceEnabled) {
    document.getElementById('inner-voice-toggle').classList.remove('on');
    document.body.classList.add('inner-voice-hidden');
  }

  // 内心独白 toggle — 点击事件
  document.getElementById('inner-voice-toggle').onclick = function () {
    this.classList.toggle('on');
    state.innerVoiceEnabled = this.classList.contains('on');
    localStorage.setItem(SETTINGS_STORAGE_KEYS.INNER_VOICE, state.innerVoiceEnabled ? 'on' : 'off');
    // 切换已有气泡的可见性
    document.body.classList.toggle('inner-voice-hidden', !state.innerVoiceEnabled);
  };
```

- [ ] **Step 3: 验证**

浏览器操作：
1. 点击 ⚙ 打开设置面板 → 应看到"内心独白" toggle（默认开启/蓝色）
2. 点击关闭 → toggle 变灰，`localStorage.getItem('elysia_inner_voice')` 应为 `'off'`
3. 刷新页面 → 状态保持关闭

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/js/settings.js
git commit -m "feat: inner_voice toggle — 设置面板 UI + 交互"
```

---

### Task 3: show/hide 逻辑 — main.js guard + persistence + CSS

**Files:**
- Modify: `static/js/main.js:284-292` (`showInnerVoice` 函数)
- Modify: `static/js/persistence.js:312` (inner_voice 渲染条件)
- Modify: `static/css/chat.css:541` (inner-voice-bubble 之后)

**Interfaces:**
- Consumes: `state.innerVoiceEnabled` (Task 1)
- Produces: `showInnerVoice()` no-ops when disabled
- Produces: `body.inner-voice-hidden .inner-voice-bubble { display: none; }`

- [ ] **Step 1: main.js — showInnerVoice 加 guard**

`static/js/main.js` `showInnerVoice` 函数体开头添加 early return：

```javascript
function showInnerVoice(content) {
  if (!state.innerVoiceEnabled) return;
  if (!content) return;
  // ... 以下现有逻辑不变
```

- [ ] **Step 2: persistence.js — session restore 也检查**

`static/js/persistence.js` 第 312 行，条件从：

```javascript
      if (m.inner_voice) {
```

改为：

```javascript
      if (m.inner_voice && state.innerVoiceEnabled) {
```

- [ ] **Step 3: chat.css — 隐藏规则**

`static/css/chat.css` 在 `.inner-voice-bubble` 块（第 541 行 `}` 之后）添加：

```css

/* inner_voice 隐藏（toggle 关闭时） */
body.inner-voice-hidden .inner-voice-bubble { display: none; }
```

- [ ] **Step 4: 端到端验证**

浏览器操作：
1. 发一条消息 → inner_voice 气泡正常出现
2. 关掉"内心独白" toggle → 已有气泡消失
3. 再发一条消息 → 没有新气泡出现
4. 重新开启 → 已有气泡恢复，新消息正常渲染
5. 刷新页面 → 状态保持

- [ ] **Step 5: Commit**

```bash
git add static/js/main.js static/js/persistence.js static/css/chat.css
git commit -m "feat: inner_voice toggle — show/hide guard + CSS + session restore"
```

---

## 自检清单

- [ ] `localStorage.getItem('elysia_inner_voice')` 关闭时为 `'off'`，开启时为 `'on'`
- [ ] 刷新后状态保持
- [ ] 关闭 toggle 后新消息不产生 inner_voice 气泡
- [ ] 关闭 toggle 后已有气泡隐藏
- [ ] 重新开启后已有气泡恢复 + 新消息正常
- [ ] session restore（重连 WS）时关闭状态下不渲染气泡
- [ ] debug 页面不受影响
