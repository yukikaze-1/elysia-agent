# Debug 页面图片显示 设计文档

## 问题

Debug 页面 (`/debug`) 的两个位置看不到用户发送的图片：

1. **概览 → Recent Session**：消息列表只显示文本，图片信息丢失
2. **数据库 → Session 管理 → 查看详情**：同上

## 根因

两处都是 **服务端丢弃图片数据 + 前端只渲染文本** 的双重问题：

| 位置 | 服务端 | 前端 |
|------|--------|------|
| Recent Session | `debug_info.py:collect()` 从 `messages_for_client()` 取数据时只保留 `parts`，丢弃了 `content` 字段 | `debug.html` 只遍历 `parts` 渲染文本 |
| Session 详情 | `server.py:session_detail_handler()` 有独立的内联格式化逻辑，过滤掉了 image parts，且不返回 `content` | `viewSession()` 只拼接 `parts` 文本 |

`messages_for_client()` 本身设计是正确的——它同时返回 text-only `parts`（给 TTS/文本显示）和完整 `content`（给前端渲染图片）。问题在于 debug 页面的两个消费端没有正确使用。

## 设计

### 数据流

```
MongoDB raw msg (content: [{type:"image",...}, {type:"text",...}])
  → server.py session_detail_handler: 返回 {role, parts, content, content_type}
  → debug_info.py collect(): 返回 {role, parts, content, content_type}
  → debug.html JS: parts 渲染文本 + content 渲染图片缩略图
```

### 改动

#### 1. `core/debug_info.py` — 补上 `content` 字段

`collect()` 中 `session_recent` 构建时，从 `messages_for_client()` 的返回值中保留 `content` 字段：

```python
info.session_recent = [
    {
        "role": m.get("role", "?"),
        "parts": m.get("parts", [{"content": ""}]),
        "content": m.get("content", []),        # 新增
        "content_type": m.get("content_type", {}),
    }
    for m in msgs[-5:]
]
```

#### 2. `core/input/server.py` — `session_detail_handler` 对齐

内联格式化循环中，当 content 是 list 时保留完整 content：

```python
if isinstance(content, list):
    entry["parts"] = [
        {"content": p.get("content", "")}
        for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    entry["content"] = content  # 新增：保留完整多模态内容
```

#### 3. `static/debug.html` — 渲染图片缩略图

**CSS 新增：**
- `.msg-image` — max-width: 120px, max-height: 120px, 圆角, cursor: pointer, display: block
- `.msg-images` — flex 容器，gap: 4px, margin-top: 4px

**JS 新增函数：**
- `renderImages(content)` — 从 content 数组中提取 `type:"image"` 的 part，渲染 `<img class="msg-image" src="..." onclick="openDebugLightbox(...)">`
- `openDebugLightbox(url)` — 简易 lightbox（复用已有的 lightbox 结构或新开标签页）
- `closeDebugLightbox()` — 关闭 lightbox

**修改渲染逻辑：**
- "Recent Session" 卡片：每条消息在 `parts` 文本后调用 `renderImages(m.content)`
- `viewSession()`：同上

**Lightbox 处理：**
- 复用 `index.html` 的 `#lightbox` 模式，或在 `debug.html` 中新增一个独立的 lightbox div
- 因为 debug.html 是独立页面，需要在自身 HTML 中添加 lightbox 结构

### 图片 URL 处理

消息中的 `source` 字段是相对路径如 `/uploads/images/abc.png`，前端可直接用作 `<img src>`。无需额外转换。

### 验证

1. 启动服务，发送一张图片 + 文字
2. 打开 `/debug` → 概览 → Recent Session：用户消息应显示文本 + 图片缩略图
3. 点击缩略图 → lightbox 放大显示
4. 切换到数据库 tab → 刷新 → 找到当前 session → 查看详情：消息应显示文本 + 图片缩略图
5. 纯文本消息不受影响，正常显示