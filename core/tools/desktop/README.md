# Desktop 工具插件

> 文档导航：[项目文档](../../../README.md)

委托 Electron Main 在用户电脑执行的四个 P0 Desktop 工具（批次 4，BE-040）。`execute()` 经 `ctx.services["desktop_bridge"]` 委托给 `DesktopToolBridge`（duck-typed，Task 4 实现）；无桌面客户端连接时立即返回 `UNAVAILABLE`。插件不接触文件系统/屏幕——一切本地操作由 Electron 完成。

## 工具

- `desktop.file.select` — 打开系统文件选择器，返回用户所选文件路径引用。参数：无（Electron 弹选择器）。risk=safe。
- `desktop.file.read` — 读取用户所选/指定文件的文本内容。参数：`path`（必填，string）。risk=medium。
- `desktop.file.write` — 写入文本到用户指定路径。参数：`path`（必填）、`content`（必填）。risk=high。
- `desktop.screen.capture` — 截取用户屏幕，返回截图附件。参数：`region`（可选）。risk=high。`output_schema` 为 object（含 attachments）。

全部四个工具 `runtime="desktop"`、`version="1.0.0"`、`approval_authority="client"`、`safety.request_timeout=120.0`（必须 ≥ 桥超时，防 `SafetyPipeline` 外层 30s 截断桥往返）。`desktop.command.run` 不注册。

`execute()` 委托组装 call dict 时计算 `arguments_hash = sha256(canonical_json(arguments))`（键排序 + 紧凑分隔 + 保留非 ASCII），作为 BE-043 参数摘要篡改防护的权威依据。

## 配置 (config.yaml)

无需额外安全配置（request_timeout 已在代码中显式声明）。后端只负责结构化传输 + 参数校验（`param_model` Pydantic）；路径黑名单/沙盒/审批由 Electron Main 本地兜底（docs/15 §三.2）。

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架（ToolPlugin 元数据 + SafetyPipeline） |
| [批次 4 spec](../../../docs/superpowers/specs/archive/2026-08/2026-08-05-electron-backend-batch4-desktop-tools.md) | 桥/注册表/WS 协议/审批模型 |
