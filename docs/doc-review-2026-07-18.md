# 文档审查报告

> 审查日期: 2026-07-18 | 范围: 项目全部 60+ 个 md 文件 | 方法: 逐条与实际代码交叉验证

---

## 严重问题（事实性错误，会误导读者）

### 1. [README.md:382] 测试总数错误：1558 → 实际 1384

`pytest --collect-only -q` 收集到 1384 个测试（含 14 个 collection error 为 1398）。README 多报了 ~170 个。

### 2. [README.md:500-525] `personality.txt` 扩展名错误（3 处）

README 示例 config 和说明中多处写 `personality.txt`，但实际文件是 `character/elysia/personality.md`：
- L502: `base_personality_file: personality.txt`
- L523: `character/<name>/personality.txt`

### 3. [README.md:155-156] 已废弃文档仍作为"用户文档"首条链接

`docs/companion-roadmap.md` 和 `docs/companion-prompt-guide.md` 自带废弃声明（"已于 2026-07-12 被替换"），但 README 仍将它们放在"用户文档"导航第一条，且未标注废弃。这两个文件还包含 3 个破损内部链接（引用不存在的 `Brain.j2`、`identity.j2`、`2026-07-04-prompt-section-provider-design.md`）。

### 4. [README.md:296] + [docs/architecture.md:482] PromptSection 数量错误：11 → 实际 12

多处说 "11 个 PromptSection"，但 `Module/system_prompt/sections/` 下有 12 个 section 文件（含 `tts_markup.py`）。同样有 12 个 `.j2` 模板文件。architecture.md 中部分地方正确写 12，部分写 11——内部矛盾。

### 5. [CLAUDE.md:38] 引用不存在的 `core/memory/message_archive.py`

CLAUDE.md 写道 `core/memory/message_archive.py`，但该文件不存在（仅有 `.pyc` 缓存残留）。

### 6. [README.md:12] + [CLAUDE.md:7] Python 版本矛盾：3.11+ vs 3.13

README badge 标 3.11+，CLAUDE.md L7 标 3.13。`setup.sh` 实际安装 python=3.13 但检查条件为 >= 3.11。无 `pyproject.toml` 或 `setup.cfg` 锁定版本。

### 7. [README.md:67-68] 引用不存在的 `static/live2d-demo/index.html`

该目录只有 Cubism SDK 源码（Core/、Framework/、Samples/），不含 `index.html`。

### 8. [core/storage/README.md:17] 引用不存在的 `character_state_repo.py`

README 和 `core/storage/__init__.py:9` 都引用了该文件，但从未被创建。

### 9. [core/schema/README.md:19-20, 45-46] 引用不存在的 `emotion.py` 和 `reflector.py`

README 列出 `emotion.py` (EmotionState) 和 `reflector.py` (ReflectorState)，但实际文件是 `mood.py`（含 `Mood` 枚举）和 `memory_reflector.py`（含 `MemoryReflectorState`）。文件名和类名均不匹配。

### 10. [core/README.md:72, 151, 276] `schema.py` 错误列为单文件

说 `schema.py` 是 52 行的单文件，但实际 `core/schema/` 是含 11 个 `.py` 文件的目录包。文件数也说 28，实际 26。

### 11. [core/README.md:248-277] 文件行数表严重过时

| 文件 | 声称行数 | 实际行数 | 偏差 |
|------|---------|---------|------|
| `task_scheduler.py` | ~250 | 433 | +73% |
| `sentence_buffer.py` | ~120 | 274 | +128% |
| `block_filter.py` | ~120 | 306 | +155% |
| `debug_info.py` | ~130 | 296 | +128% |
| `response_recorder.py` | ~40 | 80 | +100% |

### 12. [docs/testing.md:5, 12-14] marker 测试计数不准确

| marker | 声称数 | 实际收集数 | 偏差 |
|--------|--------|-----------|------|
| 总数 | 1558 | 1384 | -11% |
| `service` | ~680 | 518 | -24% |
| `integration` | ~19 | 34 | +79% |

---

## 中等问题（过时信息或内部矛盾）

### 13. [README.md:237] vs [README.md:301] TaskSystem 测试数内部矛盾：334 vs 341

同一 README 内两个数字矛盾。实际收集到 339 个 TaskSystem 测试。两个数字都不对。

### 14. [README.md:283] `character/cyrene/` 列为角色但不可用

cyrene 目录只有 `reference/`（GPT 参考材料），无 `config.yaml` 和 `personality.md`。`character/README.md` 也未提及 cyrene。

### 15. [CLAUDE.md:213-221] `static/js/` 遗漏 `tools.js` 和 `esc.js`

CLAUDE.md 列出 9 个 JS 文件，但 `static/js/` 实际有 11 个。

### 16. [Module/system_prompt/README.md:22-44] Section 和模板计数错误 + 引用不存在的 `_reference_behavior.j2`

说 11 sections，实际 12（遗漏 `tts_markup.py`）。模板列表不含 `f1_conflict_rules.j2` 和 `f3_tts_markup.j2`，却列有不存在的 `_reference_behavior.j2`。

### 17. [Module/stt/README.md:52-57] OpenAI STT 标记为"占位"但实际已实现

`openai_stt.py` 有完整实现，README 却说 "占位，未实现"。文件树图中也未列出 `openai_stt.py`。

### 18. [Module/database/README.md:11 vs 22] documentdb 矛盾：树图写"(预留)"，状态表写"可用"

MongoDB backend 已完整实现（"可用"正确），但树图仍标记为预留。

### 19. [docs/architecture.md:445] Agent.stop() 步骤数错误：14 → 实际 16

`core/agent.py` 的 `stop()` 方法有 16 个编号注释步骤。

### 20. [prompt/README.md] 遗漏活跃模板 `PersonalityCompression.j2`

该模板由 `core/personality/engine.py` 加载使用，但 prompt/README.md 的活跃模板表中未列出。

### 21. [core/README.md:158] 引用不存在的 `core/memory/README.md`

说 "详细文档: `memory/`" 但 `core/memory/` 目录没有 README.md。

### 22. [config.yaml] 缺少数据库配置节

`docs/database-storage.md` 详细记录了 MongoDB/Milvus 配置，但 `config.yaml` 中完全没有数据库配置段（仅通过环境变量驱动）。

### 23. [Module/config/README.md] 缺少 `providers.py/providers.yaml` 和 `preflight.py` 文档

provider 系统和 preflight 检查是两个重要公开功能，README 未提及。且 README 无文件结构图。

### 24. [Module/README.md] 树图遗漏 `input/` 和 `task_system/` 目录

两个模块完全未出现在 Module 总览的目录树和各模块概览中。

---

## 轻微问题（遗漏或过期注释）

### 25. [character/elysia/config.yaml:5] 过期注释

注释写 "性格描述已拆分至 elysia_personality.txt"，但实际文件是 `personality.md`。

### 26. [core/character_config.py:80-91] 死代码：fallback 仍搜索 `{name}_personality.txt`

3 处代码仍试图加载 `.txt` 扩展名（含注释和 `candidates.append`），但文件早已改为 `.md`。

### 27. [Module/tts/README.md] 树图引用不存在的 `tmp/` 目录；遗漏 `minimax_tts_api.md`

### 28. [Module/input/README.md] 文件树图遗漏 `test_server.py` 和 `test_wakeword.py`

### 29. [Module/logger/README.md] 文件树图遗漏 `__main__.py`

### 30. [docs/data-structure-survey.md] 孤立文档

505 行、2026-07-15 创建，未被任何导航索引引用（README、CLAUDE.md、architecture.md 均未链接）。仅从 `docs/plans/` 和 `docs/specs/` 深层文件引用。

### 31. [Module/task_system/] 无模块级 README

有 `docs/` 子目录（api.md、architecture.md、implementation.md、migration.md、PROGRESS.md），但无模块入口 README，且这些 docs 文件未被任何活跃文档引用。

### 32. [README.md:225] Live2D 标记 "done" 但实际不完整

`todo.md` 正确说明 Live2D "当前仅 SDK 文件到位、默认关闭"，但 README 功能表中标记为 "done"。

---

## 修复优先级

| 优先级 | 涉及文档 | 问题数 |
|--------|----------|--------|
| P0（立即修复） | README.md | 7 |
| P1（近期修复） | docs/architecture.md, CLAUDE.md, core/README.md, core/schema/README.md, core/storage/README.md, docs/testing.md | 8 |
| P2（计划修复） | Module/system_prompt/README.md, Module/stt/README.md, Module/database/README.md, Module/README.md, prompt/README.md | 6 |
| P3（后续清理） | 各模块 README 行数/遗漏, 孤立文档, 过期注释, 死代码 | 11 |
