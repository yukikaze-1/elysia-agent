# 自身状态工具插件

> 文档导航：[项目文档](../../../README.md)

查看 Agent 当前运行状态：FSM 状态、会话消息数、已加载工具数。

## 工具

- `get_self_state` — 查看 Agent 运行状态。无参数，返回 FSM 状态、会话消息数、已加载工具数、trace_id。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/runtime_config/README.md](../runtime_config/README.md) | 运行时配置工具 |
