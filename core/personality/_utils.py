"""core/personality 共享工具函数。"""
from __future__ import annotations

import re
from collections.abc import Coroutine
from typing import Any

from Module._common.task_scope import AsyncTaskScope

_SCHEDULED = AsyncTaskScope("personality")
"""personality 域 fire-and-forget 任务的进程级作用域（ARCH-INC-P2-03）。

见 :func:`_schedule` 的 docstring——这里刻意用进程级而非 per-component，
因为调用者不持有生命周期。
"""


def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。

    Args:
        text: 包含 XML 标签的原始文本。
        tag: 标签名（不含尖括号）。

    Returns:
        标签内的文本内容，未找到时返回空字符串。
    """
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _schedule(coro: Coroutine[Any, Any, Any]) -> None:
    """在事件循环上调度一个协程（fire-and-forget）。

    ARCH-INC-P2-03：经模块级 :class:`AsyncTaskScope` 调度——此前 ``loop.create_task``
    的结果被直接丢弃，任务既无强引用、也无人观察、关机时也无处 drain。异常仍由
    scope 统一记 ERROR（原来由本函数的包装器记录，行为不变）；无事件循环时记
    warning 而非静默 pass。

    ``_SCHEDULED`` 是**进程级**作用域（owner ``"personality"``）：这里的调用者
    （``AwarenessTrigger``、``EmotionEngine``）是轻量协作对象，不持有生命周期，
    强行给每个都塞一个 scope 只会制造伪所有权。关机排水由
    :func:`drain_scheduled` 负责。

    Args:
        coro: 要调度的协程对象。无事件循环时协程被显式关闭（不留 never-awaited 告警）。
    """
    async def _run() -> None:
        # 包一层而非直接 spawn(coro)：保持 fire-and-forget 的原有容忍度——
        # 调用方传入的对象若不可 await（例如 output 层被替换成同步实现），
        # 错误会落在任务内部并被 scope 记 ERROR，而不是从 _schedule 抛回
        # 认知主循环把回合打断。
        await coro

    _SCHEDULED.try_spawn(_run(), name="personality-scheduled")


async def drain_scheduled(*, timeout: float = 3.0) -> int:
    """等待 personality 域的 fire-and-forget 任务落定（ARCH-INC-P2-03）。

    由 Agent 关机流程调用；这些任务里有 awareness LLM 判定与表情帧推送，
    都可能触碰输出通道/LLM 客户端——应在它们被关闭**之前**收尾。

    Args:
        timeout: 总预算（秒）。

    Returns:
        超时前未落定的任务数（0 = 全部完成）。
    """
    return await _SCHEDULED.drain(timeout=timeout)
