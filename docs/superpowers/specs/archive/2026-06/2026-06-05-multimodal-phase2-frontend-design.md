# 多模态 Phase 2 — 前端图片交互

用户发送图片给 AI 的前端实现：上传、预览、发送、气泡渲染、lightbox。

## 范围

- 图片上传 UI（粘贴/拖拽/文件选择）
- 附件预览（输入框上方缩略图 + 删除）
- 图片+文字一起发送
- 用户气泡中图片缩略图渲染 + lightbox 点击放大
- session 历史恢复时图片渲染
- localStorage 持久化支持图片

### 不做

- AI → 用户图片输出（工具返回图片、LLM 生成图片）
- 文件/音频/视频 Part
- assistant 消息中渲染图片

## 数据流

```
粘贴/拖拽/选文件
  → POST /api/images (multipart)
  → 服务端保存到 data/uploads/images/，返回 {url, mime}
  → 输入框上方缩略图预览
  → 用户输入文字 + 点发送
  → WS send({type:"text", content:[
      {type:"image", source:"/uploads/images/abc.png", mime:"image/png"},
      {type:"text", content:"图片里有什么？"}
    ]})
  → 服务端 → LLM (Phase 1 管线，不动)
  → 前端立即渲染用户气泡（含缩略图）
  → session 恢复: messages_for_client() content 数组 → <img>
```

## 后端改动

| 文件 | 改动 |
|------|------|
| `core/input/server.py` | `POST /api/images` 保存路径 `data/images/` → `data/uploads/images/`；新增独立 static route `/uploads/` → `data/uploads/`，不与 `/static/` 混用 |
| `Module/schema/message_format.py` | `_IMAGES_DIR` → `data/uploads/images/` |
| `.gitignore` | `data/images/` → `data/uploads/` |

## 前端改动

### HTML（`static/index.html`）

输入栏新增：

```html
<!-- #input-bar 内 -->
<button id="attach-btn" title="添加图片">📷</button>
<input type="file" id="file-input" hidden
       accept="image/png,image/jpeg,image/gif,image/webp">

<!-- #input-bar 上方 -->
<div id="preview-bar" style="display:none"></div>

<!-- body 末尾 -->
<div id="lightbox" class="lightbox-hidden">
  <img id="lightbox-img" src="">
</div>
```

### CSS（`static/css/chat.css`）

新增约 60-80 行：

- `#attach-btn` — 圆形按钮，风格同 `#send-btn` / `#mic-btn`
- `#preview-bar` — flex 水平排列，图片 48px 高 + 右上角 × 删除
- `.msg-image` — 气泡内缩略图，max-width: 200px，圆角，pointer
- `#lightbox` — 全屏半透明遮罩，居中大图，点击关闭
- 拖拽高亮 `#chat-wrap.drag-over` — 蓝色虚线边框

### JS（`static/js/main.js`）

- **附件按钮** → 触发 `#file-input` 点击
- **文件选择**（input change）→ `uploadImage(file)` → 预览
- **粘贴事件**（document paste）→ 剪贴板有图片时 `uploadImage(file)` → 预览
- **拖拽事件**（#chat-wrap dragover/drop）→ drop 取图片文件 → upload → 预览
- **`uploadImage(file)`** — POST /api/images，multipart form，返回 `{url, mime}` → 调用 `addImageToPreview()`
- **`sendText()` 改造** — 构建 content 数组：预览区图片 `{type:"image", source, mime}` + 文本 `{type:"text", content}`；无图片时保持现有纯文本行为
- **lightbox** — 点击缩略图 → `openLightbox(url)`，点击遮罩 → `closeLightbox()`

### JS（`static/js/chat.js`）

- **`addImageToPreview(url, mime)`** — 创建缩略图 DOM，绑定 × 删除按钮，显示 `#preview-bar`
- **`clearPreview()`** — 发送后清空预览区
- 用户气泡渲染 — 有图片时文本+图片混排（先图后文或与后端 content 顺序一致）
- **`openLightbox(url)`** / **`closeLightbox()`** — lightbox 开关

### JS（`static/js/persistence.js`）

- **`saveChat()`** — 用户消息从 DOM 提取文本+图片 URL，存为 `{role, content: [...]}`
- **`restoreChat()`** / **`restoreFromServer()`** — `content` 数组中 `type:"image"` 时渲染 `<img>` 缩略图

### JS（`static/js/state.js`）

- 新增 `previewImages: [{url, mime}]` 数组，跟踪当前预览区图片

## 不改（已有后端能力复用）

- WebSocket 消息格式 — `{type:"text", content:[...]}` 已在 server.py 支持
- `messages_for_client()` — content 数组已包含图片元数据
- `POST /api/images` — 已实现，仅改保存路径
- ws.js / config.js / audio.js / mic.js / settings.js — 不涉及

## 验证

```bash
# 1. 启动服务
python -m core.input

# 2. 浏览器打开 http://localhost:8765
# 3. 测试:
#    - 粘贴图片 (Ctrl+V) → 预览出现
#    - 点击 📷 按钮选文件 → 预览出现
#    - 拖拽图片到聊天区 → 预览出现
#    - 输入文字 + 点发送 → 气泡含缩略图
#    - 点击缩略图 → lightbox 放大
#    - 刷新页面 → session 恢复显示图片
#    - 切换 MIMO 模型 → AI 正确描述图片

# 4. 单元测试
python -m pytest core/input/ -v --asyncio-mode=auto
python -m pytest core/tests/ -v --asyncio-mode=auto
```