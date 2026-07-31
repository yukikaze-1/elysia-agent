# Debug 页面图片显示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Debug 页面（概览 Recent Session + 数据库 Session 详情）能展示用户消息中的图片缩略图

**Architecture:** 服务端透传 `content` 数组（含 image parts）；前端新增 `renderImages()` 函数从 content 中提取 image 渲染缩略图，点击打开简易 lightbox

**Tech Stack:** Python (aiohttp + dataclass)，Vanilla JS，CSS

---

### Task 1: `debug_info.py` — `collect()` 补上 `content` 字段

**Files:**
- Modify: `core/debug_info.py:81-88`

- [ ] **Step 1: 在 session_recent 中增加 `content` 字段**

```python
# core/debug_info.py，约第 81-88 行
# 将：
info.session_recent = [
    {
        "role": m.get("role", "?"),
        "parts": m.get("parts", [{"content": ""}]),
        "content_type": m.get("content_type", {}),
    }
    for m in msgs[-5:]
]

# 改为：
info.session_recent = [
    {
        "role": m.get("role", "?"),
        "parts": m.get("parts", [{"content": ""}]),
        "content": m.get("content", []),
        "content_type": m.get("content_type", {}),
    }
    for m in msgs[-5:]
]
```

- [ ] **Step 2: 验证语法正确**

```bash
python -c "import ast; ast.parse(open('core/debug_info.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/debug_info.py
git commit -m "fix: debug_info collect() 透传 content 字段给前端渲染图片"
```

---

### Task 2: `server.py` — `session_detail_handler` 对齐格式

**Files:**
- Modify: `core/input/server.py:283-296`

- [ ] **Step 1: 在 content 是 list 时保留完整 content 数组**

```python
# core/input/server.py，约第 283-289 行
# 将：
                if isinstance(content, list):
                    # 多模态格式 — 提取 text parts
                    entry["parts"] = [
                        {"content": p.get("content", "")}
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]

# 改为：
                if isinstance(content, list):
                    # 多模态格式 — 提取 text parts + 保留完整 content
                    entry["parts"] = [
                        {"content": p.get("content", "")}
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    entry["content"] = content
```

- [ ] **Step 2: 验证语法正确**

```bash
python -c "import ast; ast.parse(open('core/input/server.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/input/server.py
git commit -m "fix: session_detail_handler 透传 content 数组保留图片信息"
```

---

### Task 3: `debug.html` — 前端渲染图片缩略图 + lightbox

**Files:**
- Modify: `static/debug.html`

这个 task 改一个文件但涉及 3 处：CSS、渲染函数、lightbox 结构。

- [ ] **Step 1: 新增 CSS 样式 — 追加到 `</style>` 之前（约第 141 行前）**

```css
/* 图片缩略图 */
.msg-images { display: flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.msg-image {
  max-width: 120px; max-height: 120px; border-radius: 6px;
  cursor: pointer; display: block; border: 1px solid var(--border);
  object-fit: cover; transition: transform 0.15s;
}
.msg-image:hover { transform: scale(1.05); }
/* Debug lightbox */
#debug-lightbox {
  display: none; position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,0.85); justify-content: center; align-items: center;
}
#debug-lightbox.show { display: flex; }
#debug-lightbox img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 8px; }
```

- [ ] **Step 2: 新增 lightbox HTML 结构 — 追加到 `</body>` 之前（约第 707 行前）**

```html
<div id="debug-lightbox" onclick="closeDebugLightbox()">
  <img id="debug-lightbox-img" src="">
</div>
```

- [ ] **Step 3: 新增 `renderImages` 和 lightbox 辅助函数 — 追加到 `esc()` / `trunc()` 等工具函数之后（约第 218 行后）**

```javascript
function renderImages(content) {
  if (!content || !Array.isArray(content)) return '';
  const images = content.filter(p => p && p.type === 'image' && p.source);
  if (!images.length) return '';
  return `<div class="msg-images">${images.map(img =>
    `<img class="msg-image" src="${esc(img.source)}" loading="lazy"
          onclick="event.stopPropagation(); openDebugLightbox('${esc(img.source)}')"
          title="点击放大">`
  ).join('')}</div>`;
}

function openDebugLightbox(url) {
  document.getElementById('debug-lightbox-img').src = url;
  document.getElementById('debug-lightbox').classList.add('show');
}

function closeDebugLightbox() {
  document.getElementById('debug-lightbox').classList.remove('show');
  document.getElementById('debug-lightbox-img').src = '';
}
```

- [ ] **Step 4: 修改 "Recent Session" 渲染 — 在每条消息文本后追加图片（约第 317-322 行）**

```javascript
// 将：
        return roleTag + parts.map((p, i) => {
          let typeTag = '';
          if (ct.markdown && ct.markdown.includes(i)) typeTag = '<span class="type-tag">md</span>';
          if (ct.code && ct.code.includes(i)) typeTag = '<span class="type-tag">code</span>';
          return `${typeTag}${expandable(p.content, 90)}`;
        }).join('');

// 改为：
        const textHtml = parts.map((p, i) => {
          let typeTag = '';
          if (ct.markdown && ct.markdown.includes(i)) typeTag = '<span class="type-tag">md</span>';
          if (ct.code && ct.code.includes(i)) typeTag = '<span class="type-tag">code</span>';
          return `${typeTag}${expandable(p.content, 90)}`;
        }).join('');
        const imgHtml = renderImages(m.content);
        return roleTag + textHtml + imgHtml;
```

- [ ] **Step 5: 修改 `viewSession()` — 在每条消息文本后追加图片（约第 645-654 行）**

```javascript
// 将：
        ${msgs.map((m, i) => {
          const parts = m.parts || [];
          const content = parts.map(p => p.content || '').join('');
          const text = content || m.content || '';
          const ts = m.timestamp ? m.timestamp.slice(0, 19).replace('T', ' ') : '';
          return `<div class="msg">
            ${ts ? `<span style="color:var(--muted);font-size:10px;margin-right:4px;">${esc(ts)}</span>` : ''}
            <span class="role-tag ${esc(m.role||'?')}">${esc(m.role||'?')}</span>
            ${expandable(text, 120)}
          </div>`;
        }).join('')}

// 改为：
        ${msgs.map((m, i) => {
          const parts = m.parts || [];
          const text = parts.map(p => p.content || '').join('');
          const ts = m.timestamp ? m.timestamp.slice(0, 19).replace('T', ' ') : '';
          return `<div class="msg">
            ${ts ? `<span style="color:var(--muted);font-size:10px;margin-right:4px;">${esc(ts)}</span>` : ''}
            <span class="role-tag ${esc(m.role||'?')}">${esc(m.role||'?')}</span>
            ${expandable(text, 120)}
            ${renderImages(m.content)}
          </div>`;
        }).join('')}
```

- [ ] **Step 6: 验证语法**

```bash
# 检查 HTML 完整性
grep -c '<style>' static/debug.html   # 应输出: 1
grep -c '</style>' static/debug.html  # 应输出: 1
grep -c '<body>' static/debug.html    # 应输出: 1
grep -c '</body>' static/debug.html   # 应输出: 1
```

- [ ] **Step 7: Commit**

```bash
git add static/debug.html
git commit -m "feat: debug 页面渲染图片缩略图 + lightbox"
```

---

## 验证

```bash
# 启动服务
python -m core.input

# 浏览器测试:
# 1. 主页面发送图片 + 文字
# 2. 打开 /debug → 概览 → Recent Session → 确认出现图片缩略图
# 3. 点击缩略图 → lightbox 放大
# 4. 点击 lightbox 背景关闭
# 5. 切换到"数据库"tab → 刷新 → 找到当前 session → 查看详情 → 确认图片显示
# 6. 纯文本消息不出现空 img 标签
```