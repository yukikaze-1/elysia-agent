> 文档导航：[项目文档](../../../README.md)

# 记忆工具插件

提供 Agent 长期记忆能力。

## 插件

- `search_memory` — 语义搜索长期记忆
- `save_memory` — 保存信息到长期记忆（需用户确认）

`save_memory` 接受 `type` 参数指定记忆类型，有效值：Fact（事实）、Preference（偏好）、
Event（事件）、Plan（计划）、Note（笔记）。输入大小写不敏感，统一转为 TitleCase 后存储。

## 配置 (config.yaml)

- `database_allowed` — 是否允许数据库访问
- `max_result_tokens` — 最大返回 token 数

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 完整文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| [Module/database/vectordb/README.md](../../../Module/database/vectordb/README.md) | Milvus 向量数据库 |
