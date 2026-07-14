# 提醒工具插件

> 文档导航：[项目文档](../../../README.md)

设置定时提醒，在指定秒数后通知用户。

## 工具

- `set_reminder` — 设置定时提醒。参数：`message`（提醒内容）、`delay_seconds`（延迟秒数，1-86400）。到期后通过 asyncio 回调通知用户。

## 配置 (config.yaml)

无需特殊安全配置，使用 SafetyProfile 默认值。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/time/README.md](../time/README.md) | 时间工具（获取当前时间） |
