# 服务重启功能设计

## 概述

为 Web 端新增重启服务功能。采用应用级软重启（不退出进程），重启过程中重新加载 YAML 配置文件，前端显示状态并自动刷新页面。

## 核心变更

### 1. Agent 生命周期状态 — `RunState` 枚举 (`core/agent.py`)

用 `RunState` 枚举替代 `self._running: bool`：

```python
class RunState(Enum):
    STOPPED = auto()     # 初始/已停止
    RUNNING = auto()     # 运行中
    STOPPING = auto()    # 正常关机中
    RESTARTING = auto()  # 重启中
```

命名为 `RunState` 而非 `AgentState`，避免与 `agent_fsm.AgentState` 冲突。

`Agent.__init__`: 新增 `config_path: str | None` 参数，保存配置路径以供重启时重载。

### 2. `/restart` 端点 (`Module/input/server.py`)

新增路由处理重启请求：

- `POST /restart` — 设置 `agent._state = RunState.RESTARTING`
- `POST /shutdown` — 改为设置 `agent._state = RunState.STOPPING`

### 3. `run_server()` 主循环改造 (`core/agent.py`)

把 `start()` → 服务器启动 → 轮询循环 → `stop()` → cleanup 包在外层 while 中。当 `stop()` 后发现状态是 `RESTARTING`，重新加载配置、调 `start()`、重建 aiohttp runner/site，重新进入轮询；否则退出。

配置重载：`ConfigLoader(AppConfig, yaml_path=self._config_path).load()` 只读取文件，不重新解析 CLI 参数（重启保持原 CLI 覆盖值不生效，以文件为准）。

### 4. 前端 (`static/index.html`, `static/js/settings.js`, `static/css/chat.css`)

- HTML: 在关闭按钮旁新增 `<button id="restart-btn">重启服务</button>`
- JS: 点击后确认 → 调 `/restart` → 显示"服务重启中…" → 轮询 `/health`（每秒，最多 60 次） → 恢复后 `location.reload()`
- CSS: 新增 `#restart-btn:hover` 蓝色 hover 样式

## 变更文件清单

| 文件 | 改动要点 |
|------|----------|
| `core/agent.py` | 新增 `RunState` 枚举；`__init__` 新增 `config_path` 参数；`self._running` → `self._state`；`start()` 适配；`run_server()` 外循环 |
| `Module/input/server.py` | 新增 `/restart` 路由；`shutdown_handler` 改为设置 `STOPPING` |
| `app.py` | `Agent(...)` 传入 `config_path=args.config` |
| `static/index.html` | 新增重启按钮 |
| `static/js/settings.js` | 新增按钮点击处理 + 轮询恢复 + 自动刷新 |
| `static/css/chat.css` | 新增 `#restart-btn:hover` 样式 |
| `Module/input/test_server.py` | 新增 `/restart` 端点测试 |

## 验证方法

1. `python -m pytest Module/input/test_server.py -v` — 确认 `/restart` 端点测试通过
2. 启动服务 `python app.py -s`，浏览器打开前端
3. 点击"重启服务"，确认弹窗 → 确认后显示"服务重启中…" → 页面自动刷新 → 服务可用
4. 修改 `config.yaml` 中的某个参数（如端口），重启后确认配置生效
5. 确认"关闭服务"按钮仍然正常工作
