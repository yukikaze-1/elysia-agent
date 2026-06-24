# 工具指引热插拔 — 设计文档

**日期**: 2026-06-24
**目标**: ToolUsageGuidance 从硬编码变为动态生成，加/删/重载工具后 prompt 自动反映

---

## 现状

Brain.j2 中有 80 行的 `ToolUsageGuidance` 宏，手写了 12 个工具的名称、描述、参数和用法提示。加一个新工具必须手动改 prompt，删工具后 prompt 过时。

## 设计

### 数据流

```
ToolPlugin.name + .description + .param_model + .prompt_hint
        │
        ▼
ToolManager.to_prompt_text()  ← 遍历 self._plugins 自动拼接
        │
        ▼
PromptAssembler.build_system_prompt()  ← 注入为 tool_guidance 变量
        │
        ▼
Brain.j2: {{ tool_guidance }}  ← 替换硬编码宏
```

### 改动

| 文件 | 改动 |
|------|------|
| `Module/tool/protocol.py` | ToolPlugin 新增 `prompt_hint: str = ""` — 可选，用于注入"何时用/不要用于"等提示 |
| `Module/tool/manager.py` | 新增 `to_prompt_text()` — 遍历 `_plugins`，拼成 `**name** — description\n  参数: ...\n  prompt_hint` 格式 |
| `core/prompt_assembler.py` | 新增 `set_tool_manager()` + `build_system_prompt()` 中调用 `to_prompt_text()` 传入 Brain.j2 |
| `core/agent.py` | `start()` 中 `prompt.set_tool_manager(tool_manager)` |
| `prompt/Brain.j2` | 删除 `ToolUsageGuidance` 宏 → 保留分类规则 + `{{ tool_guidance }}` 变量 |

### 生成格式示例

```
**search_memory** — 搜索长期记忆，查找过去的对话和偏好
  何时用: 妖梦问"你还记得..."、提到过去的事。不要用于通用知识
  参数: query(str): 搜索关键词; limit(int): 结果数量

**write_file** — 在沙箱目录里写文件（需要妖梦确认）
  参数: path(str): 文件路径; content(str): 文件内容
```

### 保留手写的部分

请求分类（A/B 类）和使用规则保留在 Brain.j2 中——这些是逻辑性内容，不随工具变化。

### 降级

ToolManager 未注入时 `tool_guidance` 为空字符串，Brain.j2 只渲染分类规则，不崩溃。

---

## 验证

1. 启动 agent → debug 页面 Prompt 查看器 → 确认 system prompt 中包含自动生成的工具列表
2. 热重载一个工具（`/tools/reload`）→ 下次对话 prompt 工具列表自动更新
3. 305 tests passed
