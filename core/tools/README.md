# core/tools/ — 工具插件

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

Agent 工具插件集合。每个插件实现 `Module.tool.protocol.ToolPlugin`，提供 `execute()` + `SafetyProfile` + `config.yaml`。由 `ToolAdapter` 加载注册，`CognitiveLoop` 通过 `ToolExecutor` Protocol 调用。

## 文件布局

```
core/tools/
├── __init__.py         # ALL_PLUGINS 注册表
├── filesystem/         # 文件读写 (ReadFile, ListDir, WriteFile)
├── web/                # 网页搜索与抓取 (WebSearch, WebFetch)
├── memory/             # 记忆搜索与保存 (MemorySearch, MemorySave)
├── time/               # 时间查询 (GetTime)
├── reminder/           # 提醒设置 (SetReminder)
├── runtime_config/     # 运行时配置查询 (GetRuntimeConfig)
├── self_state/         # 自身状态查询 (GetSelfState)
├── vision/             # 图片识别 (DescribeImage)
├── desktop/            # Desktop 工具 (FileSelect/FileRead/FileWrite/ScreenCapture，委托 Electron)
└── tests/              # 全部插件测试 + MockWebSearchPlugin（CORE-MOD-P3-01 从插件目录迁入）
```

## 插件列表

| 插件 | 目录 | 说明 |
|------|------|------|
| `ReadFilePlugin` | `filesystem/` | 读取文件内容 |
| `ListDirPlugin` | `filesystem/` | 列出目录 |
| `WriteFilePlugin` | `filesystem/` | 写入文件 |
| `WebSearchPlugin` | `web/` | 网页搜索（Tavily API） |
| `WebFetchPlugin` | `web/` | 网页内容抓取 |
| `MemorySearchPlugin` | `memory/` | 语义搜索 LTM |
| `MemorySavePlugin` | `memory/` | 保存记忆到 LTM |
| `TimePlugin` | `time/` | 获取当前时间 |
| `SetReminderPlugin` | `reminder/` | 设置提醒 |
| `RuntimeConfigPlugin` | `runtime_config/` | 查询运行配置 |
| `SelfStatePlugin` | `self_state/` | 查询自身状态 |
| `DescribeImagePlugin` | `vision/` | 图片内容识别 |
| `DesktopFileSelectPlugin` | `desktop/` | 系统文件选择器（runtime=desktop） |
| `DesktopFileReadPlugin` | `desktop/` | 读取用户电脑文件（runtime=desktop） |
| `DesktopFileWritePlugin` | `desktop/` | 写入用户电脑文件（runtime=desktop） |
| `DesktopScreenCapturePlugin` | `desktop/` | 截取用户屏幕（runtime=desktop） |

Desktop 插件（批次 4，BE-040）`runtime="desktop"`，`execute()` 经 `ctx.services["desktop_bridge"]` 委托 Electron Main；无桌面客户端连接时返回 `UNAVAILABLE`。详见 [desktop/](desktop/README.md)。

## 添加新工具

在 `core/tools/` 下创建目录，包含三元组：

```python
# core/tools/<name>/plugin.py
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.safety import SafetyProfile

class MyPlugin(ToolPlugin):
    name = "my_tool"
    description = "What this tool does"
    safety = SafetyProfile()
    
    async def execute(self, *, ctx: ToolContext, params=None):
        return ToolResult.ok(self.name, "result", duration_ms=0)
```

然后在 `core/tools/__init__.py` 的 `ALL_PLUGINS` 列表中注册。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [Module/tool/README.md](../../Module/tool/README.md) | 工具执行框架 |
| [core/adapter/README.md](../adapter/README.md) | ToolAdapter 生命周期封装 |
