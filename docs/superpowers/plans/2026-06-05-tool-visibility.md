# 工具调用详情可见性 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 工具调用在日志和前端都展示完整详情：调用参数、返回结果、错误信息、耗时

**Architecture:** SafetyPipeline 统一日志（覆盖所有执行路径）；CognitiveLoop `tool_done` 消息扩展为 7 字段；前端工具卡片支持展开查看完整参数+结果+错误

**Tech Stack:** Python (asyncio, logging), Vanilla JS, CSS

---

### Task 1: SafetyPipeline 统一日志

**Files:**
- Modify: `Module/tool/safety.py`

在 `execute()` 中添加统一的开始/完成/失败日志。所有工具无论走 TaskScheduler 还是 legacy 路径都会经过这个管道。

- [ ] **Step 1: 添加 `_format_args` 辅助函数**

在文件顶部 `SafetyProfile` 类之前插入：

```python
def _format_args(raw_params: dict, max_len: int = 60) -> str:
    """格式化工具参数用于日志，截断长值。"""
    if not raw_params:
        return ""
    parts = []
    for k, v in raw_params.items():
        s = str(v)
        if len(s) > max_len:
            s = s[:max_len] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)
```

- [ ] **Step 2: 在 `execute()` 中添加 `_finish` 内部函数和日志**

`execute()` 有 6 个提前 return 的失败路径 + 1 个成功路径（`_wrap_result`）。最干净的方式：在 `execute()` 方法内定义 `_finish(result)` 闭包统一日志，替换所有 return 点。

在 `start = asyncio.get_event_loop().time()` 之后、`self._stats["total"] += 1` 之后（约第 227 行）加入：

```python
        start = asyncio.get_event_loop().time()
        self._stats["total"] += 1
        logger.info("工具开始: %s(%s)", name, _format_args(raw_params))

        # 统一日志 + 返回的闭包
        def _finish(result):
            elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
            if result.success:
                logger.info("工具完成: %s — OK — %dms", name, elapsed)
            else:
                et = result.error_type.value if result.error_type else "UNKNOWN"
                logger.warning("工具失败: %s — %s — %s", name, et, result.error or "unknown")
            return result
```

然后将所有 `return ToolResult.fail(...)` 改为 `return _finish(ToolResult.fail(...))`，共 6 处：
- Line ~234: validation error → `return _finish(ToolResult.fail(...))`
- Line ~246: safety check → `return _finish(ToolResult.fail(...))`
- Line ~255: HITL denied → `return _finish(ToolResult.fail(...))`
- Line ~267: semaphore timeout → `return _finish(ToolResult.fail(...))`
- Line ~282: execution timeout → `return _finish(ToolResult.fail(...))`
- Line ~287: cancelled → `return _finish(ToolResult.fail(...))`
- Line ~293: general exception → `return _finish(ToolResult.fail(...))`

最后将 `return self._wrap_result(...)`（约第 301 行）改为：

```python
        return _finish(self._wrap_result(
            raw, name, asyncio.get_event_loop().time() - start,
            plugin.safety,
        ))
```

- [ ] **Step 3: 验证语法**

```bash
python -c "import ast; ast.parse(open('Module/tool/safety.py').read()); print('OK')"
```

- [ ] **Step 4: 运行 Module/tool 测试确认无回归**

```bash
python -m pytest Module/tool/ -v
```

- [ ] **Step 6: Commit**

```bash
git add Module/tool/safety.py
git commit -m "feat: SafetyPipeline 统一工具执行日志（开始/完成/失败）"
```

---

### Task 2: `tool_executing` / `tool_done` 消息补全

**Files:**
- Modify: `core/cognitive_loop.py:443-477`

- [ ] **Step 1: `tool_executing` args 不截断**

```python
# core/cognitive_loop.py，约第 451-455 行
# 将：
                        await self._output.action("tool_executing", params={
                            "tool": func_name,
                            "call_id": call_id,
                            "args": {k: str(v)[:80] for k, v in args.items()},
                        })

# 改为：
                        await self._output.action("tool_executing", params={
                            "tool": func_name,
                            "call_id": call_id,
                            "args": {k: str(v) for k, v in args.items()},
                        })
```

- [ ] **Step 2: `tool_done` 扩展为 7 字段**

```python
# core/cognitive_loop.py，约第 473-477 行
# 将：
                        await self._output.action("tool_done", params={
                            "tool": tc["function"]["name"],
                            "call_id": tc.get("id", "unknown"),
                            "success": tool_result.success,
                        })

# 改为：
                        error_type_str = tool_result.error_type.value if tool_result.error_type else None
                        await self._output.action("tool_done", params={
                            "tool": tc["function"]["name"],
                            "call_id": tc.get("id", "unknown"),
                            "success": tool_result.success,
                            "content": tool_result.content[:500] if tool_result.content else "",
                            "error": tool_result.error,
                            "error_type": error_type_str,
                            "duration_ms": tool_result.duration_ms,
                            "truncated": tool_result.truncated,
                        })
```

- [ ] **Step 3: 验证语法**

```bash
python -c "import ast; ast.parse(open('core/cognitive_loop.py').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "feat: tool_executing args 不截断 + tool_done 扩展为 7 字段含结果/错误/耗时"
```

---

### Task 3: 前端工具卡片可展开详情

**Files:**
- Modify: `static/css/chat.css`
- Modify: `static/js/chat.js`

- [ ] **Step 1: CSS — 补全已有样式中缺少的 `.tool-card-error`**

在 `static/css/chat.css` 的 `.tool-card-result.fail` 之后追加：

```css
.tool-card-error {
  font-size: 12px; padding: 6px 8px; border-radius: 4px;
  background: rgba(233,69,96,.1); color: var(--accent);
  white-space: pre-wrap; word-break: break-all; margin-bottom: 6px;
}
.tool-card-section { margin-bottom: 6px; }
.tool-card-section .label { font-size: 11px; color: var(--muted); margin-bottom: 2px; text-transform: uppercase; }
.tool-card-section .value {
  font-size: 12px; color: var(--text); white-space: pre-wrap; word-break: break-all;
  max-height: 200px; overflow-y: auto;
}
.tool-card-section .value.mono { font-family: monospace; font-size: 11px; }
```

- [ ] **Step 2: JS — 重写 `addToolCard()` 支持详细参数和默认展开**

```javascript
// static/js/chat.js，替换现有的 addToolCard 函数（约第 240-261 行）
export function addToolCard(name, callId, args) {
  finalizeDelta();
  const id = callId || `${name}-${Date.now()}`;
  const argsStr = args ? Object.entries(args).map(([k, v]) => `${k}: ${v}`).join(', ') : '';
  const argsJson = args ? JSON.stringify(args, null, 2) : '{}';
  const card = document.createElement('div');
  card.className = 'tool-card executing';
  card.id = `tool-${id}`;
  card.innerHTML = `
    <div class="tool-card-header" onclick="this.parentElement.classList.toggle('expanded')">
      <span class="tool-icon">🔧</span>
      <span class="tool-name">${esc(name)}</span>
      <span class="tool-status"><span class="tool-spinner"></span> 执行中</span>
      <span class="tool-card-chevron">▾</span>
    </div>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="label">参数</div>
        <div class="value mono">${esc(argsJson)}</div>
      </div>
      <div class="tool-card-section tool-result-section" style="display:none">
        <div class="label">结果</div>
        <div class="value tool-result-content"></div>
      </div>
      <div class="tool-card-error" style="display:none"></div>
    </div>`;
  document.getElementById('chat').appendChild(card);
  state.toolCards[id] = card;
  scrollDown();
  return id;
}
```

- [ ] **Step 3: JS — 重写 `updateToolCard()` 填充详情**

```javascript
// static/js/chat.js，替换现有的 updateToolCard 函数（约第 263-277 行）
export function updateToolCard(id, data) {
  // data: { success, content, error, error_type, duration_ms, truncated }
  const card = state.toolCards[id];
  if (!card) return;
  card.classList.remove('executing');
  const success = data.success !== false;
  card.classList.add(success ? 'done' : 'fail');
  
  const status = card.querySelector('.tool-status');
  const icon = card.querySelector('.tool-icon');
  const durationStr = data.duration_ms ? ` ${(data.duration_ms / 1000).toFixed(1)}s` : '';
  
  if (success) {
    status.innerHTML = `<span style="color:var(--idle)">✓</span> 完成${durationStr}`;
    icon.textContent = '🔧';
    // 显示结果
    if (data.content) {
      const resultSection = card.querySelector('.tool-result-section');
      const resultContent = card.querySelector('.tool-result-content');
      if (resultSection && resultContent) {
        resultSection.style.display = '';
        const truncated = data.truncated ? ' (已截断)' : '';
        resultContent.textContent = data.content + truncated;
      }
    }
  } else {
    status.innerHTML = `<span style="color:var(--accent)">✗</span> 失败${durationStr}`;
    icon.textContent = '⚠️';
    // 显示错误
    const errorEl = card.querySelector('.tool-card-error');
    if (errorEl && data.error) {
      errorEl.style.display = '';
      const et = data.error_type ? `[${data.error_type}] ` : '';
      errorEl.textContent = `❌ ${et}${data.error}`;
    }
    // 失败默认展开
    card.classList.add('expanded');
  }
}
```

注意：`updateToolCard` 的签名变了——第二个参数从 `success: bool` 变成 `data: object`。需要同步修改调用方。

- [ ] **Step 4: JS — 修改 `main.js` 中 `updateToolCard` 的调用方**

在 `static/js/main.js` 中找到 `updateToolCard` 的调用（应该在 WS 消息路由中 `tool_done` case），把 `updateToolCard(callId, msg.success)` 改为 `updateToolCard(callId, msg)`：

```javascript
// 将：
updateToolCard(callId, msg.success);

// 改为：
updateToolCard(callId, msg);
```

（因为新签名直接取整个 msg 对象的字段，且 `msg.success` 字段名不变）

- [ ] **Step 5: 验证 HTML/CSS/JS 一致性**

```bash
# 确认 tool-card 相关 class 和 JS 函数都存在
grep -c 'tool-card-error' static/css/chat.css
grep -c 'tool-result-section' static/js/chat.js
grep -c 'updateToolCard' static/js/main.js
```

- [ ] **Step 6: Commit**

```bash
git add static/css/chat.css static/js/chat.js static/js/main.js
git commit -m "feat: 工具卡片可展开详情 — 完整参数+结果+错误+耗时"
```

---

## 验证

```bash
# 启动服务
python -m Module.input

# 浏览器测试:
# 1. 发送图片触发 describe_image
#    预期日志: "工具开始: describe_image(image_index=0, prompt=请详细描述...)"
#    预期日志: "工具完成: describe_image — OK — 2340ms"
#    前端卡片: describe_image ⏳ → 展开 → 看到参数 JSON + 结果内容
# 2. 用 web_search 搜索
#    预期同上：日志有开始/完成/耗时
# 3. 模拟工具失败（设置错误 API key 或临时断网）
#    预期日志: "工具失败: describe_image — NETWORK_ERROR — Connection timeout"
#    前端卡片: 红色边框 + 默认展开 + 显示错误详情
# 4. 纯文本对话 — 工具卡片不影响正常聊天

# 单元测试
python -m pytest Module/tool/ -v
```
