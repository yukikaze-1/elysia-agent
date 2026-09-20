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
- 只链接实际存在的 README（不要链接没有 README 的目录，如 `log/` 的历史示例——`log/README.md` 已有面包屑）
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

## 文档纪律（七条，2026-09-20 文档整理批固化）

这七条是那轮整理得出的结论，也是 `scripts/check_docs.py`（DG1–DG5）与
`core/tests/test_doc_counts.py` 的判据来源。**新写/修改文档时先过一遍**。

### Rule 1 — One fact → one canonical authority

一个事实只能有一个**主要**权威（[`docs/README.md`](README.md) 的 Canonical Reference Table）。
其它文档提到它时**链接**，不要复制。

> 反面教材（都在本轮修掉）：架构树在根 README / `CLAUDE.md` / `architecture.md` 各一份；
> 错误码数量在根 README 写 41、`docs/README.md` 写 54、实际 54；测试收集数同时存在过
> 七个不同版本。

### Rule 2 — Supporting docs link instead of copying large tables

`CLAUDE.md`、包 README、`README.md` 这类 supporting 文档，遇到大表（provider 列表 /
集合表 / 组件清单 / 完整目录树）一律**下放给权威 + 链接**。判据很朴素：
**这份表会不会在别人改代码时过期，而没有任何门禁告诉我？** 会 → 别抄。

### Rule 3 — Historical docs never represent current HEAD

`docs/review/**`、`docs/archive/**`、`docs/superpowers/**`、`docs/progress/**`、
`docs/completed/**`、`docs/legacy/**` 里的正文是**当时**的快照：

- **不改写结论**（包括后来被证伪的结论）、**不删失败的实验轨迹**、
  **不重写 Challenger 的反证**；
- 允许的动作只有两种：**修导航**（坏链接）与**加 banner / index 元数据**；
- 历史路径**允许保留**（当时的 `core/cognitive_loop.py` 就写它，别改成今天的路径——
  那是篡改历史上下文）；
- 每个历史目录必须有一个 README 说明"这里不代表 HEAD"。

### Rule 4 — Canonical docs avoid unguarded volatile counts

canonical 文档**不写**会静默过期的数字：当前测试数、LOC、模块/模型数、耗时、提交数。
两种例外（只此两种）：

| 例外 | 要求 | 本仓实例 |
|---|---|---|
| **A. 有门禁钉住** | 门禁会在数字漂移时变红 | `@property` 计数（`core/tests/test_doc_counts.py`）、AppConfig env 索引（`check_docs.py` DG5）、错误码表（`check_error_codes.py`） |
| **B. 明确写成 dated snapshot** | 写明"Measured on `<日期/commit>`"，且读者能看出它**不是**当前值 | `docs/testing.md` 的并行档位曲线（2026-09-14 实测） |

**没有门禁又没写日期 → 删掉**（不是"更新成对的值"——下次还会漂）。需要数字时给命令：
`python scripts/print_test_stats.py`、`git log --since=… | wc -l`。

### Rule 5 — Canonical references prefer symbols/headings over line numbers

引用代码用 **`path/to/file.py :: ClassName.method`** 或 **`path :: test_name`**，
不用 `` foo.py:123 `` / `` foo.py:321-340 ``——行号会在下一次无关重构时全部失效，
而**失效的行号不会被任何门禁发现**（它不是链接）。同理，文档内引用用**章节标题**或
可点击的相对链接，不用 `§9` 这种无锚点的裸编号（跨文档引用更是如此）。

> 历史 Batch report **允许**保留旧行号——那记录的是当时的事实（Rule 3 优先）。

### Rule 6 — New `core/` subsystem README requirement is enforced or explicitly exempted

`core/<name>/` 里只要有生产 `.py`，就**必须**有 `README.md`（模板见上文）。
确不该有的，在 `scripts/check_docs.py` 的 `_DG4_EXEMPTIONS` 登记**理由**；
**豁免陈旧也会红**（某天它有了 README 却还挂着豁免）。

"写在文档里的要求"与"有判据的要求"不是一回事——本轮补的三个丢的 README
（`core/application` / `core/desktop` / `core/status`）就是约定存在、执行缺席的结果。

### Rule 7 — Generated artifacts identify generator + check command

凡是生成产物（TS 快照、JSON Schema、golden frames、契约 raw 快照…），文档里必须写清：

1. **谁生成它**（脚本 / 命令）；
2. **怎么校验没漂**（`--check` 命令 / 哪道门禁）；
3. **不要手改**。

本仓实例见 [`docs/README.md`](README.md) 的 Generated Artifacts 表。

---

## 新增模块时的检查清单

- [ ] 创建 README.md，使用对应模板
- [ ] 顶部面包屑，相对路径正确
- [ ] 底部"相关文档"节，至少含上级模块 + 1 个兄弟模块
- [ ] 在根 README 的"文档导航"章节添加条目
- [ ] 如果新增 Module 服务，在 `Module/README.md` 对应小节末尾加 `> 📖` 链接
- [ ] 面包屑链路能一路点到根 README
