# get_runtime_config 增强设计

> 日期: 2026-06-22
> 状态: 设计

## 问题

`get_runtime_config` 输出太简略，LLM 拿不到有用的运行时上下文：

1. **Bug**: `_tool_names` 在 `load_all()` 之前快照，已加载工具始终为 0
2. **`query` 参数是死代码** — schema 定义了但 execute 忽略
3. **有数据不展示** — `_fsm_state`、`_session_msg_count` 在 services 里但不输出
4. **缺少关键能力信息** — 文件读写目录、网络权限、审批要求，LLM 完全看不到
5. **没有任务/Tokens 信息** — 活跃后台任务数、token 消耗量均不可见
6. **ToolManager 丰富接口未暴露** — `plugin_list`、`pipeline_stats`、`capability_status` 等 8 个属性全部不可达

## 职责划分

| 工具 | 职责 | 回答的问题 |
|------|------|-----------|
| `get_runtime_config` | 环境 / 能力 / 权限 | "我能做什么？有哪些限制？" |
| `get_self_state` | 状态 / 会话 / 进度 | "我现在在做什么？当前状态如何？" |

`get_self_state` 本次不修改。

---

## 1. 输出设计

### 默认输出（无 query 参数，~12 行）

```
模型: deepseek/deepseek-v4-flash
TTS: 启用 (gpt-sovits)  |  STT: 启用 (sensevoice)
服务地址: 0.0.0.0:8765

📁 文件系统:
  读取: /home/yomu/data/sandbox (max_depth=3, 禁止symlink, max 10MB)
  写入: data/sandbox/{session_id} (需审批, max 10MB)

🌐 网络: 允许
  出站: web_search, web_fetch
  限制: 禁止私有IP, 最大重定向5次, 超时30s

📦 已加载工具 (12): web_search, read_file, write_file, submit_task, memory_search, list_directory, web_fetch, save_memory, set_reminder, get_runtime_config, get_self_state, describe_image

📊 进程: RSS=2012.9MB, 线程=222, 运行 3h22m
📋 活跃后台任务: 1 个

使用 query 参数查看详情: "tools" | "filesystem" | "network" | "tasks" | "all"
```

### `query="tools"` — 工具详情

每个工具一行：`名称 — 描述 — 能力标签 — 安全标记`。

```
📦 已加载工具 (12):

  web_search        — 搜索网页                     网络,搜索  并发:3
  web_fetch          — 获取网页内容                  网络       超时:30s
  read_file          — 读取文件内容                  文件读     目录:/home/yomu/data/sandbox
  list_directory     — 列出目录内容                  文件读     目录:/home/yomu/data/sandbox, 深度:3
  write_file         — 写入/创建文件                 文件写 🔒  目录:data/sandbox/{id}, max 10MB
  search_memory      — 搜索长期记忆                 数据库读
  save_memory        — 保存到长期记忆                数据库写 🔒
  submit_task        — 委派复杂任务到后台执行         
  set_reminder       — 设置定时提醒                  
  get_runtime_config — 查看运行时配置和权限           
  get_self_state     — 查看自身运行状态              
  describe_image     — 识别图片内容 (vision)         
```

### `query="filesystem"` — 文件系统权限详情

```
📁 文件系统权限:

  读取目录:
    /home/yomu/data/sandbox
      限制: max_depth=3, 禁止符号链接, max 10MB/文件

  写入目录:
    data/sandbox/{session_id}
      限制: 需人工审批 🔒, max 10MB/文件

  可用操作:
    read_file       — 读取文本文件内容
    list_directory  — 列出目录中的文件和子目录
    write_file 🔒   — 写入/创建文件（需用户确认）
```

### `query="network"` — 网络权限详情

```
🌐 网络权限:

  允许出站请求: 是
  私有IP过滤: 启用 (禁止 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
  最大重定向: 5 次
  最大响应体: 5 MB
  请求超时: 30 秒

  可用操作:
    web_search  — 搜索网页 (Tavily/Brave), 最多3并发
    web_fetch    — 获取并解析网页文本
```

### `query="tasks"` — 活跃后台任务

```
📋 活跃后台任务 (2):

  task-abc123 — RUNNING  — "帮我分析 agents.md 的设计问题"  — 已运行 5m23s
  task-def456 — PAUSED   — "整理 data/sandbox 目录结构"     — 已运行 2m10s

汇总: 2 个活跃, 0 个完成待清理
```

### `query="all"` — 全量输出

返回上述所有 section 的完整内容。

---

## 2. 数据来源与注入

### 新增注入到 `ToolContext.services`

| Key | 来源 | 说明 |
|-----|------|------|
| `_tool_manager` | `self._tool_manager` | ToolManager 实例（暴露 plugin_list / pipeline_stats / capability_status） |
| `_task_system` | `self._task_system` | TaskSystem 实例（暴露 list_active_tasks / get_token_stats） |
| `_started_at` | `time.monotonic()` | Agent 启动时刻，用于计算运行时长 |
| `_token_tracker` | `self._token_tracker` | TokenTracker 实例（可选，暴露当前 token 消耗） |

### `write_dir` 模板变量解析

`SafetyProfile.write_dir` 是模板字符串 `"data/sandbox/{session_id}"`（safety.py:39）。展示前必须解析 `{session_id}` → 实际值。解析在插件侧执行：

```python
session_id = ctx.session_id or "default"
write_dir = tool_safety.write_dir.replace("{session_id}", session_id)
```

### 修改 `agent.py`

三处改动：

**`start()` 开头** — 启动时刻打点（在 `_start_tool_context()` 之前，避免偏晚数秒）：
```python
self._state = RunState.RUNNING
self._started_at = time.monotonic()           # 新增：尽早打点
```

文件顶部需新增 `import time`。

**`_start_tool_context()`** — ToolManager 注入，同时把 `_started_at` 注入 services：
```python
services={
    ...
    "_tool_manager": self._tool_manager,      # 新增：ToolManager 实例
    "_started_at": self._started_at,           # 新增：启动时刻（来自 start() 开头打点）
    ...
}
```

**`_start_task_system()`** — TaskSystem 延迟注入：
```python
self._task_system = TaskSystem(infra=_ts_infra, hitl_notifier=_hitl)
await self._task_system.start()
self._tool_ctx.services["_task_system"] = self._task_system   # 新增
```

### 修复 `_tool_names` 时机

在 `_start_tool_context()` 中 `load_all()` 之后更新：
```python
await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
# 修复：load_all 后更新工具名列表
self._tool_ctx.services["_tool_names"] = list(self._tool_manager.tool_names)
self._tool_ctx.services["_tool_count"] = len(self._tool_manager._plugins)
```

---

## 3. `get_runtime_config` 插件改动

### 结构

`execute()` 拆分为 section 构建函数：

```
execute()
├── _section_model()         — 模型/TTS/STT/服务
├── _section_filesystem()    — 聚合所有工具的读/写权限
├── _section_network()       — 聚合所有工具的网络权限
├── _section_tools_summary() — 工具名列表（默认）或详情（query="tools"）
├── _section_process()       — 内存/线程/运行时长
├── _section_tasks()         — 活跃任务（默认仅数量，query="tasks"展开）
└── _section_token_usage()   — Token 消耗（如果有 tracker）
```

### query 参数路由

```python
async def execute(self, *, ctx, emit=None, params):
    query = (params.query or "").strip().lower()
    
    if query == "tools":
        return self._section_tools_detail(ctx)
    elif query == "filesystem":
        return self._section_filesystem(ctx)
    elif query == "network":
        return self._section_network(ctx)
    elif query == "tasks":
        return self._section_tasks_detail(ctx)
    elif query == "all":
        return self._render_all(ctx)
    else:
        return self._render_summary(ctx)  # 默认摘要
```

### 工具信息获取

从 `ctx.get("_tool_manager")` 获取 ToolManager，调用 `plugin_list` 属性获取每个工具的 name / description / capabilities / safety。

聚合逻辑：
- **文件系统**：遍历所有 tool，`filesystem_allowed=True` 的收集 read_dirs / write_dir，去重合并
- **网络**：遍历所有 tool，`network_allowed=True` 的收集限制参数，取并集
- **工具列表**：直接从 `plugin_list` 读取，不需要 `_tool_names` 字符串列表

---

## 4. 测试

在 `core/tools/runtime_config/test_runtime_config.py` 新增：

| 测试 | 验证点 |
|------|--------|
| `test_query_tools_shows_details` | `query="tools"` 返回每个工具的 name + description + 能力标签 |
| `test_query_filesystem_shows_dirs` | `query="filesystem"` 聚合展示 read_dirs + write_dir |
| `test_query_network_shows_restrictions` | `query="network"` 展示私有IP过滤/超时/并发限制 |
| `test_query_all_returns_all_sections` | `query="all"` 包含所有 section |
| `test_default_summary` | 无 query 返回摘要（模型+工具数+进程+权限概要） |
| `test_tool_list_after_load_all` | 工具列表不再为空（修复 _tool_names 时机 bug） |
| `test_no_tool_manager_graceful` | ctx 无 `_tool_manager` 时优雅降级，不崩溃 |

保持现有 5 个测试通过。

---

## 5. 不变原则

- 不改变 `RuntimeConfigPlugin` 的公开属性（name / description / param_model / capabilities / safety）
- 不改变 `get_self_state` 的任何行为
- 不改变 SafetyProfile / SafetyPipeline 的任何逻辑
- `get_runtime_config` 只读，不产生副作用
- 所有 ToolContext 改动向后兼容——缺失新增 key 时优雅降级，不崩溃

---

## 6. 涉及文件

| 操作 | 文件 |
|------|------|
| Modify | `core/agent.py` — `_start_tool_context()` + `_start_task_system()` |
| Modify | `core/tools/runtime_config/plugin.py` — 重写 execute() |
| Modify | `Module/tool/manager.py` — `plugin_list` 属性 safety 子字典扩展字段 |
| Modify | `core/tools/runtime_config/test_runtime_config.py` — 新增 7 个测试 |

### `plugin_list` safety 子字典扩展

当前 `plugin_list` 的 safety 只有 4 字段：
```
requires_human_approval, max_concurrency, network_allowed, filesystem_allowed
```

filesystem/network 详情需要额外字段。扩展后：
```python
"safety": {
    "requires_human_approval": ...,
    "max_concurrency": ...,
    "network_allowed": ...,
    "filesystem_allowed": ...,
    # 新增 — 文件系统
    "read_dirs": p.safety.read_dirs if p.safety else None,
    "write_dir": p.safety.write_dir if p.safety else "",
    "max_file_bytes": p.safety.max_file_bytes if p.safety else 0,
    "allow_symlinks": p.safety.allow_symlinks if p.safety else False,
    "max_dir_depth": p.safety.max_dir_depth if p.safety else 0,
    # 新增 — 网络
    "block_private_ips": p.safety.block_private_ips if p.safety else True,
    "max_redirects": p.safety.max_redirects if p.safety else 5,
    "max_response_bytes": p.safety.max_response_bytes if p.safety else 0,
    "request_timeout": p.safety.request_timeout if p.safety else 30.0,
}
```

向后兼容：仅新增字段，不删除/重命名现有字段。

---

## 7. 验证

```bash
conda activate Elysia

# 1. runtime_config 测试
python -m pytest core/tools/runtime_config/test_runtime_config.py -v

# 2. 确认 agent import 正常
python -c "from core.agent import Agent; print('import OK')"

# 3. 确认 self_state 不受影响
python -c "from core.tools.self_state.plugin import SelfStatePlugin; print('OK')"
```
