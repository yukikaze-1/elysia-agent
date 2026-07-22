# Prompt 模板

> 文档导航：[项目文档](../../README.md)

Jinja2 `.j2` 模板目录，PascalCase 命名。运行时通过 Python `str.replace()` 注入变量（非 Jinja2 `{{ }}` 语法——JSON 示例中的大括号与 `.format()` 冲突，统一用 `{variable_name}` 占位符 + `.replace()` 替换）。

## 活跃模板

| 文件 | 加载方 | 用途 | 变量 |
|------|--------|------|------|
| `ExtractMemory.j2` | `core/memory/consolidator.py` | 从对话提取长期记忆（反幻觉） | `{character_name}`, `{user_name}`, `{conversation}` |
| `ConsolidationCompare.j2` | `core/memory/consolidator.py` | 新记忆 vs 已有记忆比对（insert/update/merge/skip） | `{new_items}`, `{existing_items}` |
| `MemoryReflector.j2` | `core/memory/memory_reflector.py` | 定期深度去重（合并相似、修正过时、删除垃圾） | `{memories}`, `{last_summary}` |
| `CharacterReflector.j2` | `core/personality/reflector.py` | 性格反思 — L3 State 更新（情境反应 vs 人格偏移） | 10 变量: character_name, user_name, l1_core, l2_self, l3_history, last_self_perception, mood_snapshot, time_span, buffer_size, recent_conversations |
| `PersonalityCompression.j2` | `core/personality/engine.py` | L3→L2 性格压缩（长期自我沉淀） | `{l2_self}`, `{l3_history}` |

## 参考模板（不活跃）

这些来自旧项目，保留作为风格参考：

| 文件 | 原始用途 |
|------|----------|
| `MicroReflector.j2` | 逐对话提取记忆（当前由 ExtractMemory.j2 替代） |
| `MacroReflector.j2` | 每日日记合成 + 情感分析（MemoryReflector 的灵感来源） |
| `ActiveSpeak.j2` | 主动搭话 prompt |
| `Amygdala.j2` | 情感分析 prompt |
| `Utils.j2` | 共享 Jinja2 工具宏 |

## ref/ 子目录

GPT 生成的原始版本归档，snake_case 命名：

- `prompt_gpt_5.6_sol_pro.md` — ConsolidationCompare + MemoryReflector（sol_pro 版本，已采用）
- `prompt_gpt_5.6_terra_pro.md` — 同上（terra_pro 版本，未采用）
- `optimized_prompt_gpt_5.6_sol_pro.md` — 优化版（含 `{character_name}` 变量，未采用——与 `.replace()` 冲突）
- `extract_memory_gpt_5.6_sol_pro.md` — ExtractMemory（sol_pro 版本，已采用）
- `优化后的prompt-requirements.md` — LTM 系统 prompt 需求文档
- `character-reflector-prompt-requirements.md` — CharacterReflector 升级需求文档（2026-07-14）

## 命名规范

- 活跃模板：**PascalCase `.j2`**（如 `ExtractMemory.j2`）
- 参考/归档：**snake_case `.md`**（如 `prompt_gpt_5.6_sol_pro.md`）
- 新 prompt 需求文档放在根目录，生成后移入 `ref/`

## 渲染方式

所有活跃模板使用 `str.replace()` 而非 `.format()` 或 Jinja2 渲染：

```python
prompt = path.read_text(encoding="utf-8")
rendered = prompt.replace("{variable_name}", value)
```

原因：prompt 内容包含大量 JSON 示例 `{ "key": "value" }`，`.format()` 会把它们当作占位符导致 `KeyError`。`str.replace()` 只替换精确匹配的字符串，JSON 大括号不受影响。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [Module/system_prompt/README.md](../../Module/system_prompt/README.md) | System Prompt 引擎 |
| [Module/system_prompt/templates/README.md](../../Module/system_prompt/templates/README.md) | 模板格式约定 |
