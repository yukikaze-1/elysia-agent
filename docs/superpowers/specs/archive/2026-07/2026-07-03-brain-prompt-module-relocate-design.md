# Brain Prompt 模块迁移至 Module/ 设计

**日期**: 2026-07-03
**状态**: 待实现
**范围**: 将 `core/prompt/` 迁移为 `Module/brain_prompt/`，删除原目录，更新所有引用

---

## 动机

`core/prompt/`（Brain Prompt 引擎：`PromptSection`, `SectionParser`, `ProfileConfig`, `BrainPromptRegistry`）是一个独立、通用的 prompt 管理模块，不依赖 `core/` 的其他组件。它符合 `Module/` 的定位——后端无关的接口和工具。放在 `Module/brain_prompt/` 与 `Module/prompt/`（通用 Jinja2 管理器）平级，更清晰地表达其作为独立模块的角色。

---

## 变更

### 文件搬迁

```
core/prompt/                        Module/brain_prompt/
├── __init__.py            ──►     ├── __init__.py
├── section.py             ──►     ├── section.py
├── parser.py              ──►     ├── parser.py
├── profile.py             ──►     ├── profile.py
├── registry.py            ──►     ├── registry.py
└── tests/                 ──►     └── tests/
    ├── __init__.py                    ├── __init__.py
    ├── unit/                          ├── unit/
    │   ├── __init__.py                │   ├── __init__.py
    │   ├── test_section.py            │   ├── test_section.py
    │   ├── test_parser.py             │   ├── test_parser.py
    │   ├── test_profile.py            │   ├── test_profile.py
    │   └── test_registry.py           │   └── test_registry.py
    └── integration/                   └── integration/
        ├── __init__.py                    ├── __init__.py
        ├── test_golden_diff.py            ├── test_golden_diff.py
        └── test_render_full.py            └── test_render_full.py
```

### 删除

```
core/prompt/                         整个目录删除
```

### import 修正

| 文件 | 旧 import | 新 import |
|------|----------|----------|
| `core/prompt_assembler.py:18` | `from .prompt.registry import BrainPromptRegistry` | `from Module.brain_prompt import BrainPromptRegistry` |

所有测试文件（6 个）内部的 `from core.prompt.xxx import` → `from Module.brain_prompt import`。

### 不变的部分

- `prompt/brain/` 模板目录 —— 不移动
- `Module/prompt/` —— 不动
- `core/prompt_assembler.py` 中 `from Module.prompt import PromptManager` —— 不动
- `BrainPromptRegistry("prompt/brain")` 路径 —— 不变（模板数据路径不依赖代码位置）
- 测试内容 —— 逐字不变，仅改 import

### 文档更新

| 文件 | 变更 |
|------|------|
| `CLAUDE.md` | Module index: `core.prompt` → `Module.brain_prompt` |
| `README.md` | 项目结构: `core/prompt/` → `Module/brain_prompt/` |

---

## 验证

```bash
# 新模块测试
python -m pytest Module/brain_prompt/tests/ -v

# 确认旧目录无残留
test ! -d core/prompt/

# 全量回归（含 PromptAssembler）
python -m pytest Module/task_system/tests/unit/ -v
python -m pytest Module/prompt/test_prompt.py -v
python -m pytest core/tests/test_phase3.py -v
```

预期: 45 brain_prompt + 23 Module.prompt + 305 TaskSystem + 6 PromptAssembler = 379 tests pass。
