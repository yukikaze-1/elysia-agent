# 工具插件管理面板设计

## 概述

在 Web 端新增独立的工具插件管理面板，支持查看已加载插件列表和热重载单个插件。

## 后端端点 (`core/input/server.py`)

### `GET /tools`

返回已加载插件列表，数据来源 `ToolManager.plugin_list`：

```json
{
  "plugins": [
    {
      "name": "web_search",
      "description": "...",
      "capabilities": ["web_search", "network"],
      "safety": {
        "requires_human_approval": false,
        "max_concurrency": 5,
        "network_allowed": true,
        "filesystem_allowed": false
      },
      "active": true
    }
  ]
}
```

### `POST /tools/reload`

请求体：`{"name": "web_search"}`

调用 `agent._tool_manager.reload_plugin(name)`（该方法已在 `Module/tool/manager.py` 实现）。

响应：
- 成功：`{"ok": true, "name": "web_search"}`
- 失败：`{"ok": false, "name": "web_search", "error": "…"}`

## 前端

### HTML (`static/index.html`)

在设置按钮旁新增工具按钮 `🔧`（`id="tools-btn"`），新增工具面板容器 `id="tools-panel"`。

### JS (`static/js/tools.js`, 新文件)

- `setupTools()` — 工具按钮点击切换面板开/关，点击外部关闭
- 打开面板时 `fetch('/tools')` 获取列表
- 插件卡片渲染：名称、描述、能力标签、安全配置、重载按钮
- 重载流程：`POST /tools/reload` → 显示结果（成功绿色/失败红色）→ 3秒后刷新列表

### JS (`static/js/main.js`)

导入并调用 `setupTools()`。

### CSS (`static/css/chat.css`)

新增：
- `#tools-btn` — 与 `#settings-btn` 风格一致，位置列在其下
- `#tools-panel` — 与 `#settings-panel` 风格一致（固定定位、surface背景、边框、阴影）
- `.tool-card` — 插件卡片容器
- `.reload-btn` — 重载按钮样式

## 变更文件清单

| 文件 | 改动 |
|------|------|
| `core/input/server.py` | 新增 `GET /tools`、`POST /tools/reload` 端点 |
| `static/index.html` | 新增工具按钮 + 工具面板容器 |
| `static/js/tools.js` | 新文件 — 面板逻辑 |
| `static/js/main.js` | 导入并初始化 `setupTools()` |
| `static/css/chat.css` | 新增样式 |
| `core/input/test_server.py` | 新增端点测试 |

## 验证方法

1. `python -m pytest core/input/test_server.py -v` — 确认新端点测试通过
2. 启动服务，打开浏览器
3. 点击 `🔧` 按钮 → 看到所有插件列表（名称、描述、能力、安全配置）
4. 点击某个插件的"重载" → 看到成功提示 → 列表自动刷新
5. 确认设置面板仍正常工作
