# 去除 system prompt 中的工具列表——改用 API tools 参数

**日期**: 2026-06-24
**原因**: 工具描述同时在 system prompt 和 API `tools` 参数中传递，冗余 ~400 tokens/次

---

## 现状

```
system prompt:  请求分类 + 工具规则 + {{ tool_guidance }}（12 工具描述，~500 tokens）
API tools 参数: ToolManager.definitions() → OpenAI 格式
```

LLM 从两个来源收到相同的工具信息，浪费 token 且 prompt 过长。

## 设计

### 改后

```
system prompt:  请求分类 + 工具规则（~100 tokens）
API tools 参数: ToolManager.definitions()（不变）
```

### 分工

| 在哪里 | 管什么 |
|--------|--------|
| **system prompt** | 什么时候用工具（分类 A/B）、怎么用（规则、约束） |
| **API `tools` 参数** | 有哪些工具、参数格式 |

### 改动

| 文件 | 动作 |
|------|------|
| `prompt/Brain.j2` | 删除 `{{ tool_guidance }}` 行；`BrainSystemPrompt` macro 删除 `tool_guidance` 参数 |
| `core/prompt_assembler.py` | `build_system_prompt()` 删除 `tool_guidance` 注入（3 行）；`set_tool_manager()` 保留 |
| `core/agent.py` | `set_tool_manager()` 调用保留（无害） |

### 保留不变

- `ToolManager.to_prompt_text()` — debug 用途
- `ToolPlugin.prompt_hint` — 字段保留（可后续用于 debug 页面工具列表渲染）
- 请求分类 + 工具使用规则 — 保留在 Brain.j2 中

### 降级

无影响——API `tools` 参数不依赖 system prompt。工具变更通过 ToolManager 热重载，下次 API 调用自动反映。

---

## 验证

1. `python -m pytest Module/task_system/tests/unit/ -v` → 305 passed
2. debug 页面 Prompt 查看器 → system prompt 不再包含工具列表
3. 发送一条需要工具的消息 → LLM 正常调用工具（确认 API tools 参数生效）
