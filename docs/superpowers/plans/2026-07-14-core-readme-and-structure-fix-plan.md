# core/README.md + README 结构修正 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 `core/README.md`（~300 行，覆盖 core/ 全部 28 个文件），修正 README.md 项目结构树 16 处过时/错误。

**Architecture:** 两个独立文档任务。Task 1 写 core/README.md，Task 2 修 README.md。Task 1 中收集的实际文件名用于 Task 2 验证。

**Tech Stack:** Markdown

## Global Constraints

- core/README.md 必须覆盖 core/ 下所有 28 个 .py 文件（`find core/ -maxdepth 1 -name "*.py" | wc -l`）
- README.md 结构树修复项共 16 项，按 spec 修正清单逐项执行
- 每 task 完成后用验证命令确认

---

### Task 1: 新建 core/README.md

**Files:**
- Create: `core/README.md`

**Interfaces:**
- Consumes: spec §1-4（架构概览、核心组件、数据流、扩展指南）
- Produces: core/README.md 供 README.md 文档导航引用

- [ ] **Step 1: 收集 core/ 实际文件列表**

```bash
find core/ -maxdepth 1 -name "*.py" | sort
```
Expected: 28 files.

记录每个文件名，确保 README 覆盖所有。

- [ ] **Step 2: 写 §1 架构概览**

包含三层定位图（ASCII art）、核心组件关系图、启动顺序。

```markdown
# core/ — 认知中枢

## 架构概览

三层定位：

...
```

- [ ] **Step 3: 写 §2 核心组件**

对每个核心组件写 5-10 行描述：职责、关键方法、依赖关系。其余文件用简表。

核心组件详细描述（12 个）：Agent, AgentFSM, EventBus, CognitiveLoop, PersonalityEngine, EmotionEngine, MemoryConsolidator, MemoryReflector, CharacterReflector, SessionMemory, LongTermMemory, MessageArchive, PromptAssembler, IdleMonitor, TaskScheduler。

其余文件简表（13 个）：llm_adapter, character_config, app_config, sentence_buffer, tts_stream, block_filter, text_utils, message_id, debug_info, log_setup, timing, token_tracker, prompt_recorder, response_recorder, custom_providers, tool_call_parser, task, schema, \_\_init\_\_。

- [ ] **Step 4: 写 §3 数据流**

3 条 ASCII 数据流图：对话流、记忆流、性格演化流。

- [ ] **Step 5: 写 §4 扩展指南**

4 个子节：添加新工具、添加新 Prompt Section、添加新记忆类型、生命周期钩子。

- [ ] **Step 6: 验证 — 确保覆盖所有 28 个 .py 文件**

```bash
for f in $(find core/ -maxdepth 1 -name "*.py" -printf "%f\n"); do
    grep -q "$f" core/README.md || echo "MISSING: $f"
done
```
Expected: 无输出（所有文件都被引用）。

- [ ] **Step 7: 验证 — 文档内链不失效**

```bash
# 检查 core/README.md 中引用的内部路径是否存在
grep -oP '\[.*?\]\(\.\.?/[^)]+\)' core/README.md | grep -oP '(?<=\()[^)]+' | while read path; do
    test -f "$path" || echo "BROKEN: $path"
done
```

- [ ] **Step 8: Commit**

```bash
git add core/README.md
git commit -m "docs: core/README.md — 认知中枢架构 + 28 文件详解 + 数据流 + 扩展指南"
```

---

### Task 2: 修正 README.md 项目结构树

**Files:**
- Modify: `README.md:252-336`（项目结构节 + 文档导航节）
- Modify: `README.md:282`（Module/tool 测试数）
- Modify: `README.md:285`（Module/task_system 测试数）

**Interfaces:**
- Consumes: Task 1 产出的 core/README.md 路径（用于文档导航链接）
- Consumes: spec 修正清单 16 项

- [ ] **Step 1: 逐项执行修正清单**

按 spec 修正清单逐项修改 README.md：

| # | 行 | 操作 |
|---|-----|------|
| 1 | 261 | `# pytest 配置（integration 标记）` → `# pytest 配置（4 层 markers）` |
| 2 | 260 | `# 测试/检查命令` → `# 测试/检查/启动命令` |
| 3 | 282 | `(172 tests)` → `(129 tests)` |
| 4 | 285 | `(334 tests)` → `(341 tests)` |
| 5 | 288 | 在 `memory/` 前补 `personality/` 子目录行 |
| 6 | 312 | `tag_parser.py` → `tool_call_parser.py`，注释 `# 标签解析` → `# 流式 tool_call 解析` |
| 7 | 289-315 | 补 5 个缺失文件：`__init__.py`, `custom_providers.py`, `task.py`, `tts_stream.py`, `tool_call_parser.py` |
| 8 | 315 | `~367 tests` → `363 tests` |
| 9 | 317-318 | `prompt/` 展开：10 个 .j2 + `ref/` + `README.md` |
| 10 | 327-333 | `docs/` 补：testing.md, database-storage.md, sync-lessons.md, experience.md, readme-conventions.md, plans/, specs/ |
| 11 | — | 补 `character/` 目录 |
| 12 | — | 补 `data/` 关键子目录 |
| 13 | — | 补 `Module/task_system/tests/` |
| 14 | — | 补 `Module/_common/tests/` |
| 15 | — | 补 `core/tools/vision/` |
| 16 | — | `.superpowers/` 不显示 |

- [ ] **Step 2: 补充文档导航 — core/README.md 链接**

在 README.md 文档导航的 Core 层表格第一行插入：

```markdown
| Core 总览 | [core/README.md](core/README.md) | 认知中枢架构 + 28 组件详解 |
```

- [ ] **Step 3: 更新 "最近" 行日期**

README 第 11 行更新为最新完成项：

```markdown
> 最近 (2026-07-14): 测试分层管理 — 4 层 markers 覆盖 63 文件 ~1500 测试。core/README.md 新建。
```

- [ ] **Step 4: 验证 — 结构树中所有引用路径存在**

```bash
grep -oP '├── \S+|└── \S+' README.md | sed 's/[├└]── //' | while read entry; do
    # 忽略注释（# 之后的内容）和虚拟目录标记
    path=$(echo "$entry" | sed 's/#.*//' | xargs)
    test -e "$path" || test -d "$path" || echo "BROKEN: $path"
done
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: README 结构树修正 — 16 处对齐实际 + core/README.md 链接"
```
