# 工具指引热插拔 — 实施计划

**日期**: 2026-06-24 | **Spec**: `docs/superpowers/specs/2026-06-24-tool-guidance-hotplug-design.md`

---

## 任务 1: ToolPlugin 新增 prompt_hint 字段

**文件**: `Module/tool/protocol.py`

在 `ToolPlugin` 类的元数据区加一行：

```python
prompt_hint: str = ""  # 注入 prompt 的用法提示（何时用/不要用于/特殊注意）
```

**验证**: import ToolPlugin 确认字段存在

---

## 任务 2: ToolManager.to_prompt_text()

**文件**: `Module/tool/manager.py`

新增方法，遍历 `self._plugins.values()`，对每个插件：
- `**name** — description`
- 若有 `prompt_hint`，追加一行缩进
- 若有 `param_model`，从 `model_fields` 提取参数名/类型/描述

```python
def to_prompt_text(self) -> str:
    if not self._plugins:
        return "（无可用工具）"
    lines = []
    for plugin in self._plugins.values():
        lines.append(f"**{plugin.name}** — {plugin.description or ''}")
        if plugin.prompt_hint:
            lines.append(f"  {plugin.prompt_hint}")
        if plugin.param_model is not None:
            params = []
            for fn, fi in plugin.param_model.model_fields.items():
                tn = fi.annotation.__name__ if hasattr(fi.annotation, '__name__') else str(fi.annotation)
                d = fi.description or ""
                params.append(f"{fn}({tn}): {d}")
            if params:
                lines.append("  参数: " + "; ".join(params))
        lines.append("")
    return "\n".join(lines).rstrip()
```

**验证**: 单元测试——mock 两个 ToolPlugin，调用 `to_prompt_text()`，检查输出格式

---

## 任务 3: PromptAssembler 注入 ToolManager

**文件**: `core/prompt_assembler.py`

1. `__init__` 中加 `self._tool_manager = None`
2. 新增 `set_tool_manager(tm)` setter
3. `build_system_prompt()` 中：
   - 调用 `self._tool_manager.to_prompt_text()` if available
   - 将结果作为 `tool_guidance` 传入 `render_macro()`

**验证**: 启动 agent，debug 页面 Prompt 查看器确认 system prompt 含工具列表

---

## 任务 4: Agent 注入

**文件**: `core/agent.py`

在 `_start_tool_context()` 或 `_start_token_and_prompt()` 后加：

```python
self._prompt.set_tool_manager(self._tool_manager)
```

**验证**: agent 启动无报错

---

## 任务 5: Brain.j2 改造

**文件**: `prompt/Brain.j2`

1. `BrainSystemPrompt` macro 签名追加 `tool_guidance=""` 参数
2. 删除 `ToolUsageGuidance` 宏（~80 行）
3. 替换 `{{ ToolUsageGuidance() }}` 调用点为：

```jinja2
# 请求分类

收到妖梦的请求时，先判断：

**A 类：复杂多步任务** — 研究报告、多文件编辑、需要规划+执行的任务。
→ 直接调用 `submit_task`。不要先搜索、先分析。

**B 类：简单工具辅助** — 快速查事实、读文件、简单搜索、查时间、单次记忆。
→ 直接调对应工具，然后回复。

# 可用工具

{{ tool_guidance }}

# 工具使用规则

- **先分类**：A 类直接 submit_task，B 类选最合适的工具。
- 果断：工具明显适用就直接调，不用问妖梦。
- 别乱调：寒暄闲聊不用工具。不确定的先搜再答。
- 了解了妖梦的重要信息后，存到记忆里。
```

**验证**: 模板渲染无 Jinja2 语法错误

---

## 最终验证

1. `python -m pytest Module/task_system/tests/unit/ -v` → 305 passed
2. 启动 agent → 发一条消息 → debug Prompt 查看器确认工具列表由 `to_prompt_text()` 生成
3. 确认加/删工具后 prompt 自动更新（通过 `plugin_list` API 验证）
