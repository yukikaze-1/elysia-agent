# 插件目录化重构 — 设计

## 背景

当前所有工具插件平铺在 `core/tools/*.py`，`SafetyProfile`（沙箱配置）硬编码在各插件类的 class 属性中（如 `read_dirs=["data/sandbox"]`）。导致修改允许访问的文件目录必须改源码，不方便。

本次重构目标：
- 每个插件（或同类插件组）一个独立目录，内含代码、配置、文档
- 沙箱/安全配置从代码分离到各插件的 `config.yaml`
- 保留现有 `ToolManager.load_all()` 加载流程，只扩展配置注入能力

## 目标目录结构

```
core/tools/
├── __init__.py            # 保持 ALL_PLUGINS 列表
├── filesystem/            # 文件系统操作（读/写/列目录）
│   ├── __init__.py        # 重导出 plugin classes
│   ├── plugin.py          # ReadFilePlugin, ListDirPlugin, WriteFilePlugin
│   ├── config.yaml        # sandbox 配置
│   └── README.md
├── web/                   # 网络操作（搜索/抓取）
│   ├── __init__.py
│   ├── plugin.py          # WebSearchPlugin, WebFetchPlugin
│   ├── config.yaml        # 网络配置
│   └── README.md
├── memory/                # 记忆操作
│   ├── __init__.py
│   ├── plugin.py          # MemorySearchPlugin, MemorySavePlugin
│   ├── config.yaml        # 数据库配置
│   └── README.md
├── time/                  # 时间查询
│   ├── __init__.py
│   ├── plugin.py          # TimePlugin
│   ├── config.yaml
│   └── README.md
├── reminder/              # 提醒
│   ├── __init__.py
│   ├── plugin.py          # SetReminderPlugin
│   ├── config.yaml
│   └── README.md
├── runtime_config/        # 运行时配置查看
│   ├── __init__.py
│   ├── plugin.py          # RuntimeConfigPlugin
│   ├── config.yaml
│   └── README.md
└── self_state/            # 自身状态
    ├── __init__.py
    ├── plugin.py          # SelfStatePlugin
    ├── config.yaml
    └── README.md
```

## 各插件 config.yaml 格式

每个插件的 `config.yaml` 只定义自己关心的 SafetyProfile 字段，可选。未配置的字段沿用 SafetyProfile 类默认值。

### filesystem/config.yaml
```yaml
read_dirs:
  - /home/yomu/data/sandbox
  - /home/yomu/download
write_dir: data/sandbox/{session_id}
max_file_bytes: 10485760
allow_symlinks: false
max_dir_depth: 3
```

### web/config.yaml
```yaml
block_private_ips: true
max_redirects: 5
max_response_bytes: 5242880
request_timeout: 30.0
max_concurrency: 3
```

### memory/config.yaml
```yaml
database_allowed: true
database_readonly: true
max_result_tokens: 2000
```

### 其他插件 (time, reminder, runtime_config, self_state)
无特殊配置，`config.yaml` 为空文件或仅含注释。

## 代码改动

### 1. `ToolManager.load_all()` 扩展 — 加载插件配置

```
load_all(plugin_classes, ctx):
    for each cls in plugin_classes:
        instance = cls()
        # 新增：加载插件所在目录的 config.yaml，覆盖 SafetyProfile
        plugin_dir = path_of(cls.__module__)
        if plugin_dir/config.yaml exists:
            overrides = yaml.safe_load(plugin_dir/config.yaml)
            apply to instance.safety
        await instance.setup(ctx)
        register(instance)
```

不引入 ConfigLoader（太重），直接 `yaml.safe_load` + 简单的字段覆盖即可。只覆盖 SafetyProfile 中已存在的字段名，忽略未知字段。

### 2. `ALL_PLUGINS` 更新导入路径

```python
# __init__.py
from core.tools.filesystem.plugin import ReadFilePlugin, ListDirPlugin, WriteFilePlugin
from core.tools.web.plugin import WebSearchPlugin, WebFetchPlugin
from core.tools.memory.plugin import MemorySearchPlugin, MemorySavePlugin
from core.tools.time.plugin import TimePlugin
from core.tools.reminder.plugin import SetReminderPlugin
from core.tools.runtime_config.plugin import RuntimeConfigPlugin
from core.tools.self_state.plugin import SelfStatePlugin
```

### 3. 现有文件迁移

| 原文件 | 新位置 |
|--------|--------|
| `core/tools/filesystem.py` | `core/tools/filesystem/plugin.py` |
| `core/tools/write_file.py` | 合并到 `core/tools/filesystem/plugin.py` |
| `core/tools/web.py` | `core/tools/web/plugin.py` |
| `core/tools/memory.py` | `core/tools/memory/plugin.py` |
| `core/tools/time.py` | `core/tools/time/plugin.py` |
| `core/tools/reminder.py` | `core/tools/reminder/plugin.py` |
| `core/tools/runtime_config.py` | `core/tools/runtime_config/plugin.py` |
| `core/tools/self_state.py` | `core/tools/self_state/plugin.py` |

### 4. 测试文件同步更新

现有测试文件需要更新导入路径：
- `core/tools/test_filesystem.py` → `core/tools/filesystem/test_filesystem.py`
- `core/tools/test_write_file.py` → `core/tools/filesystem/test_write_file.py`
- 其他测试文件同理

## 不改动的部分

- `SafetyProfile` 数据类不变
- `SafetyPipeline` 不变
- `ToolPlugin` 协议不变
- `Capability` 枚举不变
- 各插件的 `execute()` 逻辑不变（只搬家，不重构内容）

## 验证

1. 现有单元测试全部通过
2. 启动服务，通过 Web 界面发消息让 Agent 列出 `/home/yomu/data/sandbox`，确认能访问
3. 修改 `filesystem/config.yaml` 中的 `read_dirs`，重启后验证新路径生效
