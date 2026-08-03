# Live2D 开关 — 设计文档

日期：2026-06-04
状态：设计完成，待实施

## 目的

新增 Live2D 开关，允许用户关闭 Live2D 动画以释放资源（iframe ~500KB JS + Cubism 运行时），切换为静态图片或 emoji 占位。关闭时移除 iframe 而非隐藏，真正释放内存/CPU。

## 配置层

### `config.yaml` 新增

```yaml
live2d:
  static_image: ""  # Live2D 关闭时显示的静态图片 URL，空字符串则 fallback 到 🌸 emoji
```

### `core/app_config.py` 新增

```python
@dataclass
class Live2DConfig:
    """Live2D 配置"""
    static_image: str = ConfigField(default="", env="LIVE2D_STATIC_IMAGE")
```

在 `AppConfig` 中新增字段：

```python
live2d: Live2DConfig = field(default_factory=Live2DConfig)
```

## 服务端

### `core/input/server.py` — capabilities 消息扩展

`_handle_ws()` 中 capabilities 消息新增 `live2d_static_image` 字段：

```python
await ws.send_json({
    "type": "capabilities",
    "stt": stt_transcribe is not None,
    "tts": agent._tts is not None,
    "llm_model": agent._llm.model,
    "live2d_static_image": agent._config.live2d.static_image,  # 新增
})
```

## 前端

### 文件变更概览

| 文件 | 变更 |
|------|------|
| `static/js/config.js` | 新增 `LIVE2D` storage key |
| `static/js/state.js` | 新增 `live2dEnabled`、`live2dStaticImage` 字段 |
| `static/js/main.js` | `capabilities` handler 新增 `live2dStaticImage` 同步 |
| `static/js/settings.js` | 新增 Live2D 开关事件 + `toggleLive2D()` 函数 |
| `static/index.html` | 设置面板新增 Live2D 开关行 |
| `static/css/chat.css` | 新增 `#live2d-static-img` 样式 |

### `static/js/config.js`

```js
LIVE2D: 'elysia_live2d',
```

### `static/js/state.js`

```js
// Live2D（默认开启）
this.live2dEnabled = localStorage.getItem(SETTINGS_STORAGE_KEYS.LIVE2D) !== 'off';
this.live2dStaticImage = '';  // 由 WS capabilities 消息更新
```

### `static/js/main.js`

`handleMessage()` 中 capabilities 分支新增：

```js
state.live2dStaticImage = msg.live2d_static_image || '';
```

### `static/js/settings.js`

**初始状态同步**（`setupSettings()` 中）：

```js
if (!state.live2dEnabled) {
  document.getElementById('live2d-toggle').classList.remove('on');
  disableLive2D();
}
```

**开关点击事件**（`setupSettings()` 中）：

```js
document.getElementById('live2d-toggle').onclick = function () {
  this.classList.toggle('on');
  state.live2dEnabled = this.classList.contains('on');
  localStorage.setItem(SETTINGS_STORAGE_KEYS.LIVE2D, state.live2dEnabled ? 'on' : 'off');
  toggleLive2D();
};
```

**`toggleLive2D()` 函数**：

```js
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

### `static/index.html`

在唤醒词开关下方新增一行：

```html
<div class="row"><span>Live2D</span><div class="toggle on" id="live2d-toggle"></div></div>
```

### `static/css/chat.css`

```css
#live2d-static-img {
  width: 100%; height: 100%; object-fit: contain;
  position: relative; z-index: 1;
}
```

## 行为规格

### 开关 ON → OFF

1. 移除 `#live2d-frame` iframe（释放 Cubism 运行时）
2. 创建 `<img id="live2d-static-img">` 插入 `#avatar-wrap`
3. 若已配置 `live2d.static_image`，img 加载该路径
4. 若未配置（空字符串），img 隐藏，CSS `#avatar-wrap::before { content: '🌸' }` 露出

### 开关 OFF → ON

1. 移除 `#live2d-static-img`
2. 重建 `<iframe id="live2d-frame">`，加载 `/static/live2d-demo/index.html`
3. 恢复 Live2D 渲染

### 持久化

- 开关状态存储在 `localStorage` key `elysia_live2d`，值为 `"on"` 或 `"off"`
- 页面刷新后恢复：`localStorage.getItem` 不为 `'off'` 时默认开启
- `live2dStaticImage` 从 WS `capabilities` 消息获取，不持久化

### 初始状态

- 默认 **ON**（Live2D 正常加载，与当前行为一致）
- 若之前关闭过，刷新后恢复关闭状态（不加载 iframe，显示静态图/emoji）

## 验证

1. 启动服务，确认 Live2D 正常加载（开关默认 ON）
2. 点击设置面板中 Live2D 开关 → OFF，确认 iframe 移除，🌸 emoji 显示
3. 刷新页面，确认开关保持 OFF 状态，Live2D 未加载
4. 点击开关 → ON，确认 Live2D 重新加载
5. 在 `config.yaml` 设置 `live2d.static_image` → 重启，关闭 Live2D 确认显示指定图片
6. 确认开关状态在 `localStorage` 中正确持久化