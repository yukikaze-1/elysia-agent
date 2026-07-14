# README 导航网络 — 设计规格

## 问题

项目有 27 个 README 分散在 3 层目录（根、Module/、core/），彼此之间没有连接，缺少统一的文档导航入口。新开发者需要手动探索目录树才能找到所有文档。

## 目标

建立以根 README 为总枢纽、Module/README.md 为中间枢纽的文档导航网络。每个子 README 都能向上溯源、横向关联。

## 范围

- 根 `README.md`：架构节精简 + 新增"文档导航"章节
- `Module/README.md`：加面包屑 + 模块链接 + 相关文档
- 16 个 `Module/<name>/README.md`：加面包屑 + 底部"相关文档"节
- 6 个 `core/` 子 README（含 `core/personality/`）：加面包屑 + 底部"相关文档"节
- 7 个 `core/tools/<name>/README.md`：面包屑 + 相关文档，过短的补内容
- `prompt/README.md`、`character/README.md`：面包屑 + 相关文档
- **不在此范围**：`docs/architecture.md`（后续单独讨论，动之前需和用户确认）、`CLAUDE.md`（保持不变）、`static/live2d-demo/`（第三方代码）

## 设计

### 1. 根 README 改动

**1a. 架构节精简**（当前 L133-153 的 20 行 ASCII 图 + 数据流 → 替换为 ~8 行概要）

```markdown
## 架构

三层结构：L0 Module/（基础设施层）→ L1 core/（认知控制层）→ L2 core/memory/（记忆与工具层）

每层职责分明。每个 Module 服务遵循 Protocol-Registry-Backend 模式，可独立替换。

完整架构图、数据流、设计决策、并发任务全景 → [docs/architecture.md](docs/architecture.md)
```

**1b. 新增"文档导航"章节**（放在架构节之后、功能表之前）

按受众分组，表格形式列出所有 README 链接：

- 用户文档（快速开始、testing.md、companion-roadmap.md 等）
- 开发者文档 — Module 层（16 个模块 README 全列出）
- 开发者文档 — Core 层（personality、tools/、CLAUDE.md 等）
- 其他文档（architecture.md、character/、prompt/ 等）

### 2. 子 README 模板

所有子 README（除根和 Module/README.md）应用轻量模板。现有内容不动，只加两样：

**2a. 顶部面包屑**

格式：`> 文档导航：<父级链接> → <当前>`

按目录深度确定面包屑路径：
- `Module/<name>/README.md` → `[项目文档](../../README.md) → [Module 文档](../README.md)`
- `Module/database/<name>/README.md` → `[项目文档](../../../README.md) → [Module 文档](../../README.md) → [数据库文档](../README.md)`
- `core/personality/README.md` → `[项目文档](../../README.md)`
- `core/tools/<name>/README.md` → `[项目文档](../../../README.md)`
- `prompt/README.md` → `[项目文档](../README.md)`
- `character/README.md` → `[项目文档](../README.md)`

面包屑放在 `# 标题` 之后、正文第一段之前。

**2b. 底部"相关文档"节**

表格格式，列出至少上级模块 README + 1-2 个相关兄弟模块。每个链接附一句话说明（不空泛）。

原则：不为了"全"而列一堆无关链接。聚焦于"看这个模块的人，下一步最可能想找什么"。

### 3. Module/README.md 作为中间枢纽

现有内容结构已经不错，改动最小：

- 顶部加面包屑：`> 文档导航：[项目文档](../README.md)`
- 每个模块小节末尾加链接提示：`> 📖 完整 API 文档 → [Module/llm/README.md](llm/README.md)`
- 底部加"相关文档"节，指向根 README + `docs/architecture.md` + `docs/testing.md`

### 4. core/tools/ 下过短 README 补充

当前 7 个工具 README，部分（如 reminder、self_state）只有一句话。按最低标准补充：

```markdown
# <工具名>

> 面包屑...

一句话描述这个工具做什么。

## 工具

- `tool_name` — 功能说明（触发条件、参数概要）

## 配置 (config.yaml)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| ... | ... | ... |

## 相关文档
...
```

内容从工具插件的 `config.yaml` + `plugin.py` 参数定义提取，不创造新内容。

已经有足够内容的（filesystem、web、memory）只加面包屑 + 相关文档，不动正文。

## 约束

- **docs/architecture.md 不动**。最后单独讨论，动之前和用户确认
- **CLAUDE.md 不动**。确保 AI 行为不受影响
- 现有 README 内容不动，除非明显过时/错误
- 不改变任何模块的代码、API 或测试

## 涉及文件清单（33 个）

### 改动：根
1. `README.md`

### 改动：Module 层
2. `Module/README.md`
3. `Module/llm/README.md`
4. `Module/stt/README.md`
5. `Module/tts/README.md`
6. `Module/embedding/README.md`
7. `Module/output/README.md`
8. `Module/config/README.md`
9. `Module/logger/README.md`
10. `Module/system_prompt/README.md`
11. `Module/system_prompt/templates/README.md`
12. `Module/tool/README.md`
13. `Module/database/README.md`
14. `Module/database/vectordb/README.md`
15. `Module/database/documentdb/README.md`
16. `Module/input/README.md`
17. `Module/bench/README.md`

### 改动：Core 层
18. `core/personality/README.md`
19. `core/tools/filesystem/README.md`
20. `core/tools/memory/README.md`
21. `core/tools/reminder/README.md`
22. `core/tools/runtime_config/README.md`
23. `core/tools/self_state/README.md`
24. `core/tools/time/README.md`
25. `core/tools/web/README.md`

### 改动：其他
26. `prompt/README.md`
27. `character/README.md`

### 需要补充内容的 tool README（太简略）
- `core/tools/reminder/README.md`
- `core/tools/runtime_config/README.md`
- `core/tools/self_state/README.md`
- `core/tools/time/README.md`
