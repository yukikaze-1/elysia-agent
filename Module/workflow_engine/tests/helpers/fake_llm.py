"""确定性 fake LLM——为 AgentInfra.llm 提供可控制的 chat.completions.create。

对齐 executor/planner 的访问面：
- executor._match_tool: choices[0].message.tool_calls[0].function.name/arguments
- executor._call_llm_direct / planner parser: choices[0].message.content
- token ledger: usage.prompt_tokens / usage.completion_tokens（属性访问）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, TypeVar

_T = TypeVar("_T")


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments          # JSON 字符串（executor 用 json.loads 解析）


class FakeToolCall:
    def __init__(self, name: str, arguments: dict) -> None:
        self.function = FakeFunction(name, json.dumps(arguments, ensure_ascii=False))


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list[FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls        # None → executor 走 __llm_direct__ 直出路径


class FakeUsage:
    def __init__(self, prompt_tokens: int = 1, completion_tokens: int = 1) -> None:
        self.prompt_tokens = prompt_tokens  # 属性访问（非 dict）——对齐 executor.py:310-316
        self.completion_tokens = completion_tokens


class FakeCompletion:
    def __init__(self, message: FakeMessage, usage: FakeUsage | None = None) -> None:
        self.choices = [type("Choice", (), {"message": message})()]
        self.usage = usage or FakeUsage()


class FakeLLM:
    """确定性 LLM stub。

    - handler: Callable[[list[dict]], dict]  每次 create 调用返回结构化结果：
        {"type": "tool", "name": ..., "arguments": {...}} → 工具匹配
        {"type": "text", "content": "..."}              → 直出文本（planner/汇总节点）
    - gate:    asyncio.Event | None   若设置，create 先 await gate.wait() 再返回。
        无 timeout——cancel 测试靠「挂起直到被 CancelledError 打断」语义（gate 恒不释放）。
    - calls:   调用尝试数（gate 通过后 +1，**无论 handler 是否抛异常**）——「cancel 后不再消耗 token」断言基准；prompts 记录输入。
    """

    def __init__(self) -> None:
        self.chat = self                 # AgentInfra.llm.chat.completions.create(...) 路径
        self.completions = self
        self.handler: Callable[[list[dict]], dict | str] = lambda msgs: {"type": "text", "content": "fake response"}
        self.gate: Any = None            # asyncio.Event | None
        self.calls = 0                   # 调用尝试数（gate 通过后 +1，无论 handler 是否抛）——断言基准
        self.prompts: list[list[dict]] = []

    async def create(self, *, messages: list[dict], **_: Any) -> FakeCompletion:
        if self.gate is not None:
            await self.gate.wait()       # 无 timeout：取消时 CancelledError 在此穿透
        self.calls += 1
        self.prompts.append(messages)
        result = self.handler(messages)
        if isinstance(result, dict) and result.get("type") == "tool":
            return FakeCompletion(
                FakeMessage(tool_calls=[FakeToolCall(result["name"], result.get("arguments", {}))])
            )
        content = result.get("content") if isinstance(result, dict) else str(result)
        return FakeCompletion(FakeMessage(content=content))


def with_deadline(coro: Awaitable[_T], timeout: float = 10) -> Awaitable[_T]:
    """挂死防线：为确定性测试的 await 包裹 asyncio.wait_for 总超时。

    cancel 未到达时 timeout 秒后抛 TimeoutError 使该用例失败（而非永久挂起整个套件）。
    """
    return asyncio.wait_for(coro, timeout=timeout)


class CountingLLM:
    """外部成本计数包装——包真实 AsyncOpenAI，记录 create 调用次数并透传。

    暴露 chat/completions 自引用 + async create(*, messages, **kwargs)：
    先 count += 1 再委托 real.chat.completions.create(messages=messages, **kwargs)。
    messages 是具名 keyword-only 参数、不进 **kwargs——必须显式透传，
    否则真实客户端缺 messages 必抛。
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.chat = self                 # 属性自引用——委托链 .chat.completions.create 走属性
        self.completions = self
        self.count = 0                   # create 调用次数

    async def create(self, *, messages: list[dict], **kwargs: Any) -> Any:
        self.count += 1
        return await self._real.chat.completions.create(messages=messages, **kwargs)
