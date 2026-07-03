# Prompt Module

Jinja2 模板管理与渲染模块。提供 `PromptManager` 类，加载模板文件并支持命名 macro 渲染。

## 文件结构

```
Module/prompt/
├── __init__.py          # 导出 PromptManager
├── manager.py           # PromptManager 实现
├── test_prompt.py       # 单元测试
└── README.md
```

模板文件保留在项目根目录 `prompt/*.j2` 中。

## 快速开始

```python
from Module.prompt import PromptManager

# 构造
pm = PromptManager("prompt/", auto_reload=True)

# 渲染完整模板
result = pm.render("simple.j2", name="World")

# 渲染命名 macro
system = pm.render_macro("Amygdala.j2", "AmygdalaSystemPrompt", character_name="Elysia")
```
> **注意**: 系统 prompt (Brain) 已迁移到 `core/prompt/BrainPromptRegistry`。`render_macro("Brain.j2", ...)` 仅作为 fallback 保留。推荐使用 `BrainPromptRegistry.render(profile, context)` 进行模块化渲染。
```

## API

### PromptManager(template_dir, *, auto_reload=False, logger=None)

- **template_dir** (`str`): 模板文件所在目录路径
- **auto_reload** (`bool`): 是否每次从磁盘重新加载（开发用，默认关闭）
- **logger** (`logging.Logger | None`): 可选 logger，不传则使用无 handler 的静默 logger

### render(template_name, **kwargs) -> str

加载并渲染整个模板文件，所有 `**kwargs` 作为模板变量传入。

### render_macro(template_name, macro_name, **kwargs) -> str

加载模板文件，仅渲染其中指定的命名 `{% macro %}`，所有 `**kwargs` 作为 macro 参数传入。

## 自定义 Filter

### `| datetime`

将 Unix 时间戳格式化为日期字符串，默认格式 `%Y-%m-%d %H:%M:%S`。

```jinja2
{{ msg.timestamp | datetime }}
{{ 1715845200.0 | datetime }}
{# → "2024-05-16 11:40:00" #}
```

## 测试

```bash
python -m pytest Module/prompt/test_prompt.py -v
```
