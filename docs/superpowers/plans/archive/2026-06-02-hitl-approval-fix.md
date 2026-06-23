# HITL 审批回路修复 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补全 HITL 审批的入站通路（前端 → 服务器 → SafetyPipeline），使 `write_file` 等 `requires_human_approval=True` 的工具能正常等待并接收用户审批结果。

**Architecture:** 三层改动：服务器 `_handle_json()` 新增 `tool_approval` 路由 → 前端 `handleMessage()` 新增 `command` 分支，弹出原生确认框 → `ToolManager` 新增 `submit_approval()` 桥接方法，服务器通过 agent 调用。

**Tech Stack:** Python 3.13 (aiohttp), vanilla JS, CSS

---

## 涉改文件

| 文件 | 改动 |
|------|------|
| `Module/tool/manager.py` | 新增 `submit_approval()` 方法，桥接到 SafetyPipeline |
| `Module/input/server.py` | `_handle_json()` 新增 `tool_approval` case |
| `static/js/main.js` | `handleMessage()` 新增 `command` case + 审批对话框函数 |
| `static/css/chat.css` | 新增审批对话框样式 |

---

### Task 1: ToolManager 新增 `submit_approval()` 桥接方法

**Files:**
- Modify: `Module/tool/manager.py`

在 `ToolManager` 类中新增一个公共方法，桥接到 SafetyPipeline 的 `submit_approval()`，让 server.py 不需要直访 `_pipeline` 内部。

- [ ] **Step 1: 在 `cancel_all()` 方法之后添加新方法**

Edit `/home/yomu/Agent/Module/tool/manager.py`，在 `cancel_all()` 方法（约第 208 行）之后插入:

```python
    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """提交 HITL 审批结果。由 server 层在收到前端审批响应后调用。"""
        return self._pipeline.submit_approval(request_id, approved)
```

- [ ] **Step 2: 验证方法存在**

```bash
python -c "
from Module.tool.manager import ToolManager
mgr = ToolManager()
print(hasattr(mgr, 'submit_approval'))
print(hasattr(mgr._pipeline, 'submit_approval'))
"
```
Expected: `True` `True`

- [ ] **Step 3: 运行现有测试确认无回归**

```bash
python -m pytest Module/tool/test_framework.py -v -k "hitl" 2>&1 | tail -10
```
Expected: 相关 HITL 测试通过。

---

### Task 2: 服务器 `_handle_json()` 新增 `tool_approval` 路由

**Files:**
- Modify: `Module/input/server.py`

- [ ] **Step 1: 在 `_handle_json` 中添加 `tool_approval` 分支**

Edit `/home/yomu/Agent/Module/input/server.py`，在 `elif msg_type == "interrupt":` 分支之后（约第 189 行），`else:` 分支之前插入:

```python
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent._tool_manager.submit_approval(request_id, approved)
            if ok:
                logger.info(
                    "WS 审批结果: request=%s approved=%s", request_id, approved,
                )
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
```

修改后的 `_handle_json` 完整代码:

```python
async def _handle_json(data: dict, agent) -> None:
    msg_type = data.get("type", "")
    if msg_type == "text":
        content = data.get("content", "").strip()
        if content:
            await agent.process_message(content)
            logger.info("WS 收到文本: %s", content[:100])
    elif msg_type == "interrupt":
        logger.info("WS 收到打断指令")
        await _do_interrupt(agent)
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent._tool_manager.submit_approval(request_id, approved)
            if ok:
                logger.info(
                    "WS 审批结果: request=%s approved=%s", request_id, approved,
                )
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)
```

- [ ] **Step 2: 运行服务器相关测试**

```bash
python -m pytest Module/input/ -v 2>&1 | tail -10
```
Expected: 全部通过。

---

### Task 3: 前端审批对话框 — JS 逻辑

**Files:**
- Modify: `static/js/main.js`

- [ ] **Step 1: 在 `handleMessage()` 的 switch 中添加 `command` 分支**

Edit `/home/yomu/Agent/static/js/main.js`，在 `case 'error':` 之前插入:

```javascript
    case 'command':
      if (msg.command === 'tool_approval') {
        showApprovalDialog(msg.params);
      }
      break;
```

- [ ] **Step 2: 在文件末尾（`init()` 调用之前）添加 `showApprovalDialog` 函数**

```javascript
/* ---- HITL 审批对话框 ---- */
function showApprovalDialog(params) {
  const { tool, params: toolParams, request_id } = params;

  // 创建遮罩层
  const overlay = document.createElement('div');
  overlay.className = 'approval-overlay';
  overlay.id = 'approval-overlay';

  // 创建对话框
  const dialog = document.createElement('div');
  dialog.className = 'approval-dialog';
  dialog.innerHTML =
    '<div class="approval-icon">&#9888;</div>' +
    '<div class="approval-title">工具操作确认</div>' +
    '<div class="approval-tool">工具: <strong>' + esc(tool) + '</strong></div>' +
    '<div class="approval-detail">参数: ' + esc(toolParams || '(无)') + '</div>' +
    '<div class="approval-buttons">' +
      '<button class="approval-approve" id="approval-approve">允许</button>' +
      '<button class="approval-deny" id="approval-deny">拒绝</button>' +
    '</div>';

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  // 按钮事件
  function respond(approved) {
    send({ type: 'tool_approval', request_id: request_id, approved: approved });
    document.body.removeChild(overlay);
  }

  document.getElementById('approval-approve').onclick = function() { respond(true); };
  document.getElementById('approval-deny').onclick = function() { respond(false); };

  // 点击遮罩外部区域不关闭（必须明确选择），但按 Escape 拒绝
  function onKey(e) {
    if (e.key === 'Escape') {
      respond(false);
      document.removeEventListener('keydown', onKey);
    }
  }
  document.addEventListener('keydown', onKey);
}
```

注意: 使用 `esc()` 函数防 XSS（`chat.js` 中已导出）。

- [ ] **Step 3: 在 `import` 区域添加 `send` 引用检查**

`main.js` 第 2 行已经 `import { connect, send } from './ws.js';` — `send` 已导入，无需改动。

---

### Task 4: 前端审批对话框 — CSS 样式

**Files:**
- Modify: `static/css/chat.css`

- [ ] **Step 1: 在 chat.css 末尾追加审批对话框样式**

```css
/* ---- HITL 审批对话框 ---- */
.approval-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
  animation: fadeIn 0.15s ease;
}
.approval-dialog {
  background: #1e1e2e;
  border: 1px solid #45475a;
  border-radius: 12px;
  padding: 28px 32px 24px;
  max-width: 380px; width: 90%;
  text-align: center;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  animation: slideUp 0.2s ease;
}
.approval-icon {
  font-size: 36px; margin-bottom: 8px;
}
.approval-title {
  font-size: 16px; font-weight: 600;
  color: #cdd6f4; margin-bottom: 12px;
}
.approval-tool {
  font-size: 14px; color: #a6adc8; margin-bottom: 6px;
}
.approval-detail {
  font-size: 12px; color: #6c7086;
  word-break: break-all; margin-bottom: 20px;
  max-height: 80px; overflow-y: auto;
}
.approval-buttons {
  display: flex; gap: 12px; justify-content: center;
}
.approval-buttons button {
  border: none; border-radius: 8px;
  padding: 10px 28px; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: opacity 0.15s;
}
.approval-buttons button:hover { opacity: 0.85; }
.approval-approve {
  background: #a6e3a1; color: #1e1e2e;
}
.approval-deny {
  background: #f38ba8; color: #1e1e2e;
}
@keyframes fadeIn {
  from { opacity: 0; } to { opacity: 1; }
}
@keyframes slideUp {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}
```

---

### Task 5: 端到端验证

- [ ] **Step 1: 启动服务**

```bash
python app.py
```

- [ ] **Step 2: 浏览器打开 `http://0.0.0.0:8765/`**

- [ ] **Step 3: 发送消息触发 write_file**

输入: "帮我在 sandbox 里写一个 test.txt，内容是 hello"

Expected:
1. 前端弹出审批对话框，显示工具名 `write_file`
2. 点击"允许"后，send 发送 `{type: "tool_approval", request_id: "...", approved: true}`
3. 服务器收到并调用 `submit_approval()`
4. write_file 正常执行，返回写入成功
5. 文件实际创建在 sandbox 目录下

- [ ] **Step 4: 测试拒绝流程**

再次触发 write_file，在弹出的对话框中点击"拒绝"或按 Escape。

Expected:
1. 审批被拒绝
2. Agent 收到 "操作被拒绝" 错误
3. 不会创建文件

- [ ] **Step 5: 运行全部测试确认无回归**

```bash
python -m pytest Module/ core/ -v 2>&1 | tail -5
```
Expected: 全部通过。

---

### Task 6: 提交

- [ ] **Step 1: 提交**

```bash
git add Module/tool/manager.py Module/input/server.py static/js/main.js static/css/chat.css
git commit -m "feat: complete HITL approval round-trip (server route + frontend dialog)"
```
