> 文档导航：[项目文档](../../../README.md)

# 文件系统工具插件

提供 Agent 访问本地文件系统的能力。

## 插件

- `read_file` — 读取文本文件内容（仅 UTF-8 文本）
- `list_directory` — 列出目录内容（最多 3 层深度）
- `write_file` — 在 sandbox 目录下写入文件（需用户确认）

## 配置 (config.yaml)

- `read_dirs` — 允许读取的目录白名单（绝对路径）
- `write_dir` — 写入目录模板，`{session_id}` 会替换为当前会话 ID
- `max_file_bytes` — 最大允许读取/写入的文件大小（默认 10MB）
- `allow_symlinks` — 是否允许跟随符号链接
- `max_dir_depth` — 最大目录遍历深度

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [core/tools/web/README.md](../web/README.md) | 网络工具（同样涉及安全约束） |
