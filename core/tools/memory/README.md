> 文档导航：[项目文档](../../../README.md)

# 记忆工具插件

提供 Agent 长期记忆能力。

## 插件

- `search_memory` — 语义搜索长期记忆
- `save_memory` — 保存信息到长期记忆（需用户确认）

## 配置 (config.yaml)

- `database_allowed` — 是否允许数据库访问
- `database_readonly` — 数据库操作是否只读
- `max_result_tokens` — 最大返回 token 数

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [Module/database/vectordb/README.md](../../../Module/database/vectordb/README.md) | Milvus 向量数据库 |
