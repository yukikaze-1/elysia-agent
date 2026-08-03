# Think Loop 拆分 — 设计文档

> 2026-07-27

## 目标

将 `CognitiveLoop._think_loop()` 从 378 行提取为 4 个独立方法 + ~30 行主循环。不改变行为，纯提取重构。

## 动机

当前 `_think_loop()` 一个方法承担了 5 个职责：构建 prompt、流式消费 LLM、判定 tool_calls、执行工具、收尾归档。方法过长，难以理解 ReAct 循环的骨架。提取后主循环 ~30 行，一眼看清 `for round → stream → decide → (execute | finalize)` 结构。

## 设计

### 提取的 4 个方法

**`_build_turn_prompt()`**（~40 行）

```python
async def _build_turn_prompt(
    self, user_message: str, user_name: str,
    content: list[dict] | None,
    stt_emotion: str, stt_events: list[str] | None,
    is_task_result: bool,
) -> tuple[list[dict], list[dict] | None]:
    """构建本轮 LLM 调用的 messages + tool definitions。
    Returns: (messages, tool_defs)
    """
```

- 输入：所有用户消息参数
- 输出：`(messages, tool_defs)` — 不可变返回，调用方持有引用
- 包含：prompt 构建、session 写 UserMessage、tool_defs 构建（submit_task 附加逻辑）

**`_stream_llm_response()`**（~45 行）

```python
async def _stream_llm_response(
    self, messages: list[dict], tool_defs: list[dict] | None, round_num: int,
) -> tuple[str, list[dict], bool, list[str], str]:
    """流式调用 LLM，通过 StreamProcessor 分发事件。
    Returns: (acc_text, raw_tool_calls, speaking_started, all_sentences, inner_voice)
    """
```

- 输入：messages + tool_defs + round_num
- 输出：5-tuple 包含流式处理的所有产��
- 包含：StreamProcessor 创建/feed/flush、token 记录、prompt 记录

**`_execute_tool_round()`**（~150 行）

```python
async def _execute_tool_round(
    self, messages: list[dict], acc_text: str,
    raw_tool_calls: list[dict], _on_tool_calls,
) -> list:
    """执行工具调用轮次。
    
    - submit_task 拦截（handle_submit_task）
    - 普通工具 dispatch（_on_tool_calls 或 _execute_tools_directly）
    - 结果注入 messages（in-place 修改）
    - session 写 AssistantMessage + ToolResultMessage
    - output 推 tool_executing / tool_done 事件
    
    Returns: 本次 dispatch 的 tc 列表
    """
```

- 副作用为主：修改 messages、写 session、推 output
- 返回值：dispatch 的工具调用列表（供主循环清理 `_executing_tools`）

**`_finalize_turn()`**（~80 行）

```python
async def _finalize_turn(
    self, acc_text: str, all_sentences: list[str],
    iv_think_buf: str, speaking_started: bool,
    user_message: str,
) -> None:
    """收尾处理：标签剥离 → 归档 session → 推送结构化块 → 触发 after_response。"""
```

- 纯副作用方法，无返回值
- 包含：<mood>/<inner_voice> 标签剥离、session 写 AssistantMessage、结构化块推送、after_response 触发

### 提取后的 `_think_loop()`

```python
async def _think_loop(self, user_message, user_name, ...) -> None:
    # 前置：STT + 唤醒消息
    ...
    
    # 1. 构建 prompt
    messages, tool_defs = await self._build_turn_prompt(...)
    
    # 记录 prompt + token turn start
    ...
    await self._output.typing(True)
    
    # 2. ReAct 循环
    for round_num in range(self.MAX_TOOL_ROUNDS + 1):
        # 流式调用
        acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think = \
            await self._stream_llm_response(messages, tool_defs, round_num)
        
        # 判定：工具调用？
        if raw_tool_calls and round_num < self.MAX_TOOL_ROUNDS:
            await self._execute_tool_round(messages, acc_text, raw_tool_calls, _on_tool_calls)
            continue
        
        # 终点
        await self._finalize_turn(acc_text, all_sentences, iv_think, speaking_started, user_message)
        return
```

## 不在范围内的改动

- 不加状态机
- 不改 `_dispatch_stream_event()`
- 不改任何测试（全部覆盖公开接口，无需变动）
- 不改 `process()` 签名

## 文件变更

| 文件 | 变更 |
|------|------|
| `core/cognitive/loop.py` | 提取 4 个 `_xxx()` 方法，`_think_loop()` 缩减到 ~30 行 |

## 验证

```bash
pytest core/tests/test_phase3.py -v -k "CognitiveLoop"
pytest -m "unit or service" -q  # 全量快速测试
```
