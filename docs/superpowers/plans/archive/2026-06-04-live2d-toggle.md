# Live2D 开关 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 Live2D 开关，关闭时移除 iframe 释放资源，显示静态图片/emoji 占位，配置驱动图片路径。

**Architecture:** 纯前端 toggle 模式（遵循 TTS/唤醒词开关模式）+ 服务端 capabilities 下发静态图片路径。`config.yaml` → `app_config.py` → `capabilities` WS 消息 → `state.js` → `settings.js` toggle。

**Tech Stack:** Python dataclass (配置), aiohttp (WS capabilities), vanilla JS ES modules (前端 toggle), localStorage (持久化)

---

## 文件结构

| 文件 | 变更类型 | 职责 |
|------|----------|------|
| `config.yaml` | 修改 | 新增 `live2d.static_image` 配置项 |
| `core/app_config.py` | 修改 | 新增 `Live2DConfig` dataclass |
| `core/input/server.py` | 修改 | capabilities 消息新增 `live2d_static_image` |
| `static/js/config.js` | 修改 | 新增 `LIVE2D` storage key |
| `static/js/state.js` | 修改 | 新增 `live2dEnabled`、`live2dStaticImage` 字段 |
| `static/js/main.js` | 修改 | capabilities handler 同步 `live2dStaticImage` |
| `static/js/settings.js` | 修改 | 开关事件 + `toggleLive2D()` 函数 |
| `static/index.html` | 修改 | 设置面板新增 Live2D 开关行 |
| `static/css/chat.css` | 修改 | 新增 `#live2d-static-img` 样式 |

---

### Task 1: 配置层 — config.yaml + app_config.py

**Files:**
- Modify: `config.yaml`
- Modify: `core/app_config.py`

- [ ] **Step 1: config.yaml 新增 live2d 段**

在 `config.yaml` 末尾添加：

```yaml
live2d:
  static_image: ""
```

- [ ] **Step 2: app_config.py 新增 Live2DConfig**

在 `core/app_config.py` 的 dataclass 定义区域（在其他 Config 类之后），新增：

```python
@dataclass
class Live2DConfig:
    """Live2D 配置"""
    static_image: str = ConfigField(default="", env="LIVE2D_STATIC_IMAGE")
```

- [ ] **Step 3: AppConfig 中新增 live2d 字段**

在 `AppConfig` 类的字段中添加：

```python
live2d: Live2DConfig = field(default_factory=Live2DConfig)
```

位置：放在其他 Config 字段之后（如 `wake_word: WakeWordConfig` 之后）。

- [ ] **Step 4: 验证配置加载**

```bash
python -c "from core.app_config import AppConfig, ConfigLoader; c = ConfigLoader(AppConfig, yaml_path='config.yaml', yaml_key='live2d').load(); print(c)"
```

确认输出中 `live2d.static_image` 存在且为空字符串。

- [ ] **Step 5: Commit**

```bash
git add config.yaml core/app_config.py
git commit -m "feat: add Live2DConfig with static_image field"
```

---

### Task 2: 服务端 — capabilities 消息扩展

**Files:**
- Modify: `core/input/server.py`

- [ ] **Step 1: capabilities 消息新增 live2d_static_image**

在 `_handle_ws()` 函数中找到 capabilities 消息发送处（约第 458-466 行），新增一行：

```python
await ws.send_json({
    "type": "capabilities",
    "stt": stt_transcribe is not None,
    "tts": agent._tts is not None,
    "llm_model": agent._llm.model,
    "live2d_static_image": agent._config.live2d.static_image,
})
```

- [ ] **Step 2: Commit**

```bash
git add core/input/server.py
git commit -m "feat: add live2d_static_image to WS capabilities message"
```

---

### Task 3: 前端 — 状态层（config.js + state.js + main.js）

**Files:**
- Modify: `static/js/config.js`
- Modify: `static/js/state.js`
- Modify: `static/js/main.js`

- [ ] **Step 1: config.js 新增 LIVE2D storage key**

在 `static/js/config.js` 的 `SETTINGS_STORAGE_KEYS` 对象中新增：

```js
LIVE2D: 'elysia_live2d',
```

位置：放在 `WAKE: 'elysia_wake',` 之后。

- [ ] **Step 2: state.js 新增 Live2D 状态字段**

在 `static/js/state.js` 的 `AppState` 构造函数中新增：

```js
// Live2D（默认开启）
this.live2dEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.LIVE2D) !== 'off';
this.live2dStaticImage = '';  // 由 WS capabilities 消息更新
```

位置：放在 `this.wakeEnabled` 初始化之后（约第 42 行）。

- [ ] **Step 3: main.js capabilities handler 同步 live2dStaticImage**

在 `static/js/main.js` 的 `handleMessage()` 函数中，`case 'capabilities':` 分支内，`state.ttsAvailable` 赋值之后，新增：

```js
state.live2dStaticImage = msg.live2d_static_image || '';
```

并在 `updateCapabilityIndicators()` 调用后新增初始同步：

在 `handleMessage` 的 capabilities case 中，`updateCapabilityIndicators()` 调用后：

```js
toggleLive2D();  // 初始同步 Live2D 状态（根据 localStorage）
```

注意：需要先从 settings.js 导入 `toggleLive2D`。修改 main.js 顶部 import：

```js
import { setupSettings, updateCapabilityIndicators, setModelTag, toggleLive2D } from './settings.js';
```

- [ ] **Step 4: Commit**

```bash
git add static/js/config.js static/js/state.js static/js/main.js
git commit -m "feat: add Live2D state fields and capabilities sync"
```

---

### Task 4: 前端 — 开关 UI 与逻辑（index.html + chat.css + settings.js）

**Files:**
- Modify: `static/index.html`
- Modify: `static/css/chat.css`
- Modify: `static/js/settings.js`

- [ ] **Step 1: index.html 新增 Live2D 开关行**

在 `static/index.html` 设置面板中，唤醒词开关行之后，新增：

```html
<div class="row"><span>Live2D</span><div class="toggle on" id="live2d-toggle"></div></div>
```

位置：在 `<div class="row"><span>唤醒词</span><div class="toggle" id="wake-toggle"></div></div>` 之后。

- [ ] **Step 2: chat.css 新增静态图片样式**

在 `static/css/chat.css` 中 `#live2d-frame` 样式之后，新增：

```css
#live2d-static-img {
  width: 100%; height: 100%; object-fit: contain;
  position: relative; z-index: 1;
}
```

- [ ] **Step 3: settings.js 新增 Live2D 开关初始化和事件**

在 `static/js/settings.js` 的 `setupSettings()` 函数中，在唤醒词开关代码之后，新增：

```js
// Live2D 初始状态同步
if (!state.live2dEnabled) {
  document.getElementById('live2d-toggle').classList.remove('on');
}

// Live2D 开关
document.getElementById('live2d-toggle').onclick = function () {
  this.classList.toggle('on');
  state.live2dEnabled = this.classList.contains('on');
  localStorage.setItem(SETTINGS_STORAGE_KEYS.LIVE2D, state.live2dEnabled ? 'on' : 'off');
  toggleLive2D();
};
```

- [ ] **Step 4: settings.js 新增 toggleLive2D() 函数**

在 `static/js/settings.js` 文件末尾（`setModelTag` 函数之后），新增导出函数：

```js
/** 切换 Live2D iframe / 静态图片 */
export function toggleLive2D() {
  const wrap = document.getElementById('avatar-wrap');
  const iframe = document.getElementById('live2d-frame');
  const staticImg = document.getElementById('live2d-static-img');

  if (state.live2dEnabled) {
    // 开启 Live2D — 移除静态图，重建 iframe
    if (staticImg) staticImg.remove();
    if (!iframe) {
      const newIframe = document.createElement('iframe');
      newIframe.id = 'live2d-frame';
      newIframe.src = '/static/live2d-demo/index.html';
      newIframe.sandbox = 'allow-scripts allow-same-origin';
      wrap.appendChild(newIframe);
    }
  } else {
    // 关闭 Live2D — 移除 iframe 释放资源，显示静态图
    if (iframe) iframe.remove();
    if (!staticImg) {
      const img = document.createElement('img');
      img.id = 'live2d-static-img';
      img.src = state.live2dStaticImage || '';
      img.alt = '';
      if (!img.src) img.style.display = 'none';
      wrap.appendChild(img);
    }
  }
}
```

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/css/chat.css static/js/settings.js
git commit -m "feat: add Live2D toggle with iframe/static-image switching"
```

---

## 验证

```bash
# 1. 启动服务，确认 Live2D 正常加载（开关默认 ON）
python -m core.run

# 2. 打开浏览器 /，F12 打开 DevTools → Application → Local Storage
#    确认 elysia_live2d 为 "on" 或不存在（默认 on）

# 3. 点击设置面板 Live2D 开关 → OFF
#    确认：iframe 移除，🌸 emoji 显示
#    确认：localStorage elysia_live2d = "off"

# 4. 刷新页面
#    确认：Live2D 未加载，静态内容显示，开关处于 OFF 状态

# 5. 点击开关 → ON
#    确认：Live2D iframe 重新创建并加载

# 6. 在 config.yaml 设置 live2d.static_image: "/static/live2d-demo/Resources/Aquarius/back_class_normal.png"
#    重启服务，关闭 Live2D
#    确认：显示指定图片而非 emoji
```