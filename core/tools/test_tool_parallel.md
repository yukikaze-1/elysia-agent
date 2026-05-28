# 工具并行执行 — 测试用例

覆盖 `cognitive_loop.py` 中 `asyncio.gather()` 并行工具执行的所有场景。

## 实现

```python
# cognitive_loop.py — 工具执行段
results = await asyncio.gather(*[_execute_one(tc) for tc in normalized])
for tc, tool_result in results:
    messages.append({"role": "tool", "tool_call_id": tc.get("id", "unknown"),
                     "content": tool_result.llm_content})
```

每个工具的 `_execute_one` 内部有独立的 try/except，单工具失败不影响其他。`return_exceptions` 不需要（异常已在内部捕获）。

## 测试用例 (共 12 个)

### 组 1: 功能正确性

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 1 | **两个独立工具并行执行** | `search_memory("妖梦")` + `get_time()` | 两个工具结果都出现在 messages 中，`tool_call_id` 正确映射 |
| 2 | **三个独立工具并行执行** | `search_memory` + `get_time` + `web_search` | 三个工具结果都存在，总执行时间 ≈ max(各工具耗时)，非 sum |
| 3 | **单个工具执行** | 仅 `get_time()` | 降级为串行，结果正确，不抛异常 |
| 4 | **工具结果 llm_content 正确注入** | `search_memory` 返回 `{"role":"tool","content":"..."}` | 每条结果与对应 `tool_call_id` 正确关联 |

### 组 2: 错误处理

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 5 | **一个工具抛异常，另一个成功** | tool1 抛 `RuntimeError`, tool2 正常返回 | tool1: `ToolResult(success=False, llm_content="Error: ...")`<br>tool2: 正常结果<br>所有结果都在 messages 中 |
| 6 | **两个工具都失败** | 两个工具都抛异常 | 两个 `ToolResult(success=False)`，并行执行不因异常崩溃 |
| 7 | **JSON 解析失败** | `arguments` 字段非法 JSON | 降级为 `args={}`，继续执行工具（被工具侧报错或返回空） |

### 组 3: 时序与并发

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 8 | **并行执行时间 < 串行执行时间** | 两个各 sleep 0.1s 的工具 | 并行耗时 ~0.1s，串行耗时 ~0.2s |
| 9 | **长任务不阻塞短任务** | tool1 sleep 0.5s, tool2 sleep 0.05s | tool2 的结果在 tool1 之前返回，gather 等所有完成后一起处理 |
| 10 | **action 事件发送** | 执行 2 个工具 | "tool_executing" 发 2 次，"tool_done" 发 2 次，顺序不保证（并发） |

### 组 4: 边界与集成

| # | 用例 | 输入 | 预期 |
|---|------|------|------|
| 11 | **空工具列表** | `normalized = []` | `asyncio.gather()` 返回空列表，不抛异常，不进入工具执行分支 |
| 12 | **多轮工具调用** | 第 1 轮 2 工具 → 第 2 轮 1 工具 | 每轮独立并行，第 2 轮能正常使用第 1 轮的工具结果 |

## 验证方式

```bash
# 运行全部核心测试（包含工具并行相关）
python -m pytest core/ -v --asyncio-mode=auto

# 查看工具并行相关测试
python -m pytest core/test_phase1.py -v -k "tool_calling" --asyncio-mode=auto
```

## 已知限制

1. **无依赖分析**：当前所有工具无条件并行。如果 tool2 依赖 tool1 的输出，LLM 应将其拆为两轮调用（LLM 原生支持）。不需要在客户端做 DAG。
2. **ToolContext 共享**：所有工具共享同一个 `self._ctx`。工具实现应只读 ctx 或使用线程安全的追加操作。当前所有工具的 ctx 使用方式均为读取或原子写入。
3. **取消传播**：用户 INTERRUPT 时，`gather` 中的 task 会被 cancel。`_execute_one` 中的 `except Exception` 会捕获 `CancelledError` 子类？需要确认 `asyncio.CancelledError` 继承关系——Python 3.9+ `CancelledError` 继承 `BaseException` 非 `Exception`，so 不会被 `except Exception` 捕获，会正常传播。
