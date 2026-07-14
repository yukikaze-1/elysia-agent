# 运行时配置工具插件

> 文档导航：[项目文档](../../../README.md)

查看 Agent 运行时配置和状态：模型、TTS/STT、文件/网络权限、工具列表、后台任务、内存使用。

## 工具

- `get_runtime_config` — 查看运行时配置。参数：`query`（可选，`"tools"` / `"filesystem"` / `"network"` / `"tasks"` / `"all"`，留空返回摘要）。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/self_state/README.md](../self_state/README.md) | 自身状态工具（FSM 状态） |
