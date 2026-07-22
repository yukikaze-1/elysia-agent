# README 编写规范

> 文档导航：[项目文档](../README.md)

项目 README 需要彼此连接。新模块、新插件必须按此规范编写或更新 README。

## 何时需要 README

| 场景 | 要求 |
|------|------|
| `Module/<name>/` 新服务模块 | **必须**。遵循 Protocol-Registry-Backend 模式 |
| `core/tools/<name>/` 新工具插件 | **必须**。CLAUDE.md 要求 plugin.py + config.yaml + README.md 三元组 |
| `core/` 下新子系统 | **必须**。如 personality/、memory/ 等 |
| `character/<name>/` 新角色 | 建议，非强制（config.yaml 已有自描述能力） |

## 结构模板

### Module 服务模块

```markdown
# <模块名>

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

一句话概述这个模块做什么。

## 文件结构
（目录树，标注每个文件职责）

## 快速开始
（最小可用代码示例）

## 接口
（关键类/方法签名 + 参数说明）

## 支持的后端 / 配置
（表格：provider、env var、默认值）

## 测试
（pytest 命令）

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 |
| （1-2 个相关兄弟模块的 README） | （说明关联点） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
```

### 工具插件

```markdown
# <工具名>工具插件

> 文档导航：[项目文档](../../../README.md)

一句话描述这个工具做什么。

## 工具

- `tool_name` — 功能说明。参数：`param1`（类型，约束）、`param2`...

## 配置 (config.yaml)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| ... | ... | ... |

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../../README.md) | 快速开始 + 文档导航 |
| [Module/tool/README.md](../../../Module/tool/README.md) | 工具执行框架 |
| （1-2 个相关兄弟工具） | （说明关联点） |
```

### Core 子系统

```markdown
# <子系统名>

> 文档导航：[项目文档](../../README.md)

一句话概述。

## 架构 / 数据流
（简明图或文字描述）

## 关键机制
（核心设计决策、阈值常量等）

## 文件布局
（目录树 + 关联文件）

## 调试
（debug 面板入口 / API 端点）

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| （1-2 个相关子系统） | （说明关联点） |
```

## 面包屑规则

每个 README 顶部必须有面包屑，放在 `# 标题` 之后、正文第一段之前。格式：

```
> 文档导航：<父级链接> → <父级链接> → ...
```

### 按目录深度

| 文件位置 | 面包屑 |
|----------|--------|
| `Module/<name>/README.md` | `[项目文档](../../README.md) → [Module 文档](../README.md)` |
| `Module/<name>/<sub>/README.md` | `[项目文档](../../../README.md) → [Module 文档](../../README.md) → [<父级>文档](../README.md)` |
| `core/personality/README.md` | `[项目文档](../../README.md)` |
| `core/tools/<name>/README.md` | `[项目文档](../../../README.md)` |
| `core/prompts/README.md` | `[项目文档](../../README.md)` |
| `character/README.md` | `[项目文档](../README.md)` |
| `docs/<name>.md` | `[项目文档](../README.md)` |

### 原则

- 相对路径，不要用绝对路径
- 只链接实际存在的 README（不要链接没有 README 的目录，如 `Module/schema/`）
- 链路终点始终是根 `README.md` 的"文档导航"章节

## "相关文档"节规则

放在文件末尾，`---` 分隔线之后。表格格式：

```markdown
---

## 相关文档

| 文档 | 内容 |
|------|------|
| <链接> | <一句话说明这个文档和当前模块的关系> |
```

### 原则

- 至少包含上级模块 README + 1-2 个最相关的兄弟模块
- 每个链接附一句话，说明关联点（不空泛）
- 聚焦"读者看完当前文档后，下一步最可能想找什么"
- 不为了"全"列一堆无关链接

## 内容原则

- **代码示例要能跑** —— 复制粘贴即可执行，不省略 import、不省略 await
- **配置表要完整** —— 列出所有配置项、默认值、环境变量绑定
- **测试命令要可执行** —— 给完整的 pytest 命令，不要伪代码
- **不重复 architecture.md** —— 详细架构图/数据流指向 `docs/architecture.md`，README 只给概要
- **不重复其他 README** —— 链接过去，不要复制内容

## 新增模块时的检查清单

- [ ] 创建 README.md，使用对应模板
- [ ] 顶部面包屑，相对路径正确
- [ ] 底部"相关文档"节，至少含上级模块 + 1 个兄弟模块
- [ ] 在根 README 的"文档导航"章节添加条目
- [ ] 如果新增 Module 服务，在 `Module/README.md` 对应小节末尾加 `> 📖` 链接
- [ ] 面包屑链路能一路点到根 README
