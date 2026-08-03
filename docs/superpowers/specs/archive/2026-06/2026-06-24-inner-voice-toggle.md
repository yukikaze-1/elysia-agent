# inner_voice 前端显示/隐藏 Toggle

**日期**: 2026-06-24
**步骤**: 伴侣化 Step 2 收尾

---

## 目标

在设置面板添加"内心独白"开关，用户可以控制是否看到 Elysia 的 inner_voice 气泡。

---

## 改动

### 1. `static/js/config.js` — 加 localStorage key

```javascript
INNER_VOICE: 'elysia_inner_voice',
```

### 2. `static/js/state.js` — 加状态

```javascript
this.innerVoiceEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.INNER_VOICE) !== 'off';
```

>`!== 'off'` 和 TTS/Live2D 一致——默认 on，只有显式存过 `'off'` 才关。

### 3. `static/index.html` — 设置面板加 toggle 行

在 TTS/唤醒词/Live2D 组之后、音量之前加：

```html
<div class="row"><span>内心独白</span><div class="toggle on" id="inner-voice-toggle"></div></div>
```

### 4. `static/js/settings.js` — toggle 逻辑

参照已有 TTS/唤醒词/Live2D toggle 模式：

```javascript
// 初始状态同步
if (!state.innerVoiceEnabled) {
  document.getElementById('inner-voice-toggle').classList.remove('on');
}

// 点击事件
document.getElementById('inner-voice-toggle').onclick = function () {
  this.classList.toggle('on');
  state.innerVoiceEnabled = this.classList.contains('on');
  localStorage.setItem(SETTINGS_STORAGE_KEYS.INNER_VOICE, state.innerVoiceEnabled ? 'on' : 'off');
  // 切换已有气泡的可见性
  document.body.classList.toggle('inner-voice-hidden', !state.innerVoiceEnabled);
};
```

### 5. `static/js/main.js` — `showInnerVoice()` 加 guard

```javascript
function showInnerVoice(content) {
  if (!state.innerVoiceEnabled) return;
  // ... 现有逻辑不变
}
```

### 6. `static/js/persistence.js` — session restore 也检查

渲染 inner_voice 气泡之前检查：

```javascript
if (m.inner_voice && state.innerVoiceEnabled) {
```

### 7. `static/css/chat.css` — 隐藏规则

```css
body.inner-voice-hidden .inner-voice-bubble { display: none; }
```

---

## 行为

| 操作 | 结果 |
|------|------|
| 默认 | 显示 inner_voice 气泡 |
| 关闭 toggle | 新消息不再渲染气泡；已有气泡隐藏 |
| 重新开启 | 已有气泡恢复可见；新消息正常渲染 |
| 刷新页面 | localStorage 持久化，状态保持 |
| session restore | 关闭时不渲染历史 inner_voice 气泡 |

---

## 不改

- 后端：inner_voice 始终生成、存储、注入 LLM 历史。toggle 只控制前端可见性。
- debug 页面：始终显示 inner_voice（调试用途）。
