# Action Gate — LLM 工具调用决策校验

> 状态: 待实现 | 日期: 2026-07-28

## 动机

当前 LLM 产出的 `tool_calls` 在 `normalize_tool_calls()` 之后直接进入 dispatch 流程——session 写入、UI 事件广播都在任何校验之前发生。工具名是否存在于注册表，要到 `ToolManager.execute()`（第 5 层）才被拦截。此时 session 和 UI 状态已被污染。

在 `normalize_tool_calls()` 之后、dispatch 之前加一层纯代码校验（Action Gate），提前拦截无效 tool_call，整轮 abort 让 LLM 自行修正。

**参数校验不纳入 gate**——`SafetyPipeline` 已有 Pydantic 校验，且 gate 通过 `ToolExecutor` 协议无法访问 `plugin.param_model`。参数错误仍由 `SafetyPipeline` 拦截，只是比 gate 晚（session 已提交）。幻觉工具名是更需要提前拦截的场景——它意味着 LLM 产出本身有问题，应在任何副作用之前 abort。

## 设计

### 插入点与控制流

`core/cognitive/loop.py` 的 `_execute_tool_round()`，第 395 行之后：

```python
# 395: normalized = normalize_tool_calls(raw_tool_calls)
# 397-403: 拆分 submit/other（abort 场景下无害，submit_tcs 为空）
# 406-409: assistant_entry 写入 messages（始终执行——OpenAI 协议要求
#          assistant 消息在所有 tool result 之前）
# NEW:   if not self._gate_tool_calls(normalized, messages):
#            return  # gate 已注入 failure tool messages，跳过后续 dispatch
# 411+:  submit_task 处理 + session 写入 + UI 事件 + dispatch
```

`_gate_tool_calls()` 签名与返回值：

```python
def _gate_tool_calls(
    self,
    tool_calls: list[ToolCall],
    messages: list[dict],
) -> bool:
    """校验 LLM 产出的 tool_calls。全部通过返回 True。
    任一被拒 → 注入 GATE_REJECTED tool messages 到 messages，返回 False。
    """
```

**关键设计决策：`assistant_entry` 在 gate 之前写入。** OpenAI 协议要求 assistant 消息（含 tool_calls）出现在 tool result 消息之前。如果 gate 先于 assistant_entry 执行并注入 failure tool messages，消息序列会断——assistant_entry 出现在 tool results 之后，违反协议。因此 gate 放在 assistant_entry（line 409）之后，合法路径继续 dispatch，abort 路径直接 return（跳过 submit_task 处理、session 写入、UI 事件、dispatch）。

### abort 路径

任一 tool_call 被 gate 拒绝 → **整轮 abort**：
- 所有 tool_call（包括合法的）都不执行
- 每个被拒的 tool_call 生成 `ToolResult.fail(error_type=GATE_REJECTED, message=reason)`
- 合法的 tool_call 生成 `ToolResult.fail(error_type=GATE_REJECTED, message="本轮其他工具校验失败，整轮取消")`
- 结果作为 tool messages 追加到 `messages`
- LLM 在下一轮 ReAct 看到错误后自行修正

### 校验项

1. **工具存在性** — 交叉比对 `self._tool_executor.definitions()` 的 name 集合
2. **（预留）黑名单** — `blocked_tools: set[str] | None = None`，本次不实现

### submit_task 特殊处理

`submit_task` 是合成工具，不在 plugin registry 里。gate 在比对 registry 之前先把它从待校验列表拆出来，单独放行——它有自己的 `_handle_submit_task` 校验逻辑。

### 新增 ErrorType

```python
GATE_REJECTED = "gate_rejected"  # 调度前被 gate 拦截，不可重试
```

不可重试——gate 拒绝意味着 LLM 产出了不存在的工具名，重试同样的 tool_call 不会变正确。LLM 需要在下一轮 ReAct 中调整自己的输出。

**与 `UNKNOWN_TOOL` 的关系：** `UNKNOWN_TOOL` 保留给深层 pipeline（`ToolManager.execute()` 发现工具未注册时返回）。gate 统一用 `GATE_REJECTED`，覆盖两种子场景：幻觉工具名（gate 直接拦截）+ 整轮连坐（合法工具因同轮其他工具被拒而取消）。两者分工明确——`UNKNOWN_TOOL` = 执行层发现，`GATE_REJECTED` = 调度层拦截。

## 修改范围

### 代码

| 文件 | 改动 |
|------|------|
| `core/schema/error_type.py` | 新增 `GATE_REJECTED` |
| `core/cognitive/tools.py` | `CognitiveTools` 新增 `_gate_tool_calls()` 方法 |
| `core/cognitive/loop.py` | `_execute_tool_round()` 在 `normalize_tool_calls()` 之后调用 gate |

### 测试

| 文件 | 内容 |
|------|------|
| `core/cognitive/tests/test_tool_gate.py` | 新文件：gate 单元测试 |

### 不改动

- 不新建模块文件
- `tool_call_parser.py` 不变
- `ToolAdapter` / `ToolManager` / `SafetyPipeline` 不变
- 现有测试不需要修改（gate 是新增校验层，不改变现有合法路径的行为）

## 兼容性

- 合法 tool_call 的路径完全不变——gate 全通过时行为与现在一致
- 新 `GATE_REJECTED` error type 不影响现有 error handling
- `submit_task` 的校验逻辑独立，不受 gate 影响

## 验证

```bash
# 新 gate 测试
pytest core/cognitive/tests/test_tool_gate.py -v

# 防回归 — cognitive 测试
pytest core/cognitive/tests/ -v

# 全量 tool 测试
pytest Module/tool/tests/ -v
```
