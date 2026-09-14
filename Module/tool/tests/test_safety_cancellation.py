# 测试文件：Module/tool/tests/test_safety_cancellation.py
"""SafetyPipeline 的取消语义守卫（#33b / S2-R2-1，marker=unit）。

**为什么单独立文件**：这条缺陷不体现在返回值里，而体现在 `wait_for` 的语义上——
修前 `execute()` 在 `except asyncio.CancelledError` 里
`return ToolResult.fail("用户取消", ErrorType.CANCELLED)`。调度器 60s 超时 cancel
它时，被取消方**正常返回** → `wait_for` 不抛 `TimeoutError`（Python 3.11+ 的
`Timeout.__aexit__` 只在"异常仍在传播"时才改抛）→ 调度器的
`except asyncio.TimeoutError` 永不执行（`CANCELLED.is_retryable is False`，
重试分支被跳过），worker 随后把任务标成成功。用户看到"用户取消"，而用户从未取消。

两条用例**必须成对**：只测"外部取消要上抛"会退化成"任何 `CancelledError` 都上抛"，
把插件内部自取消（真正的业务结果）也变成异常。
"""

import asyncio

import pytest

from Module.tool.protocol import ErrorType, ToolResult
from Module.tool.safety import SafetyPipeline, SafetyProfile

pytestmark = pytest.mark.unit


class _BlockingPlugin:
    """execute 永不返回——由外层 `wait_for` 制造**真实**的超时取消。"""

    safety = SafetyProfile(request_timeout=30.0)
    param_model = None

    async def execute(self, *, ctx, params):
        await asyncio.sleep(30)
        return ToolResult(success=True, content="never", tool_name="blocking")


class _SelfCancelPlugin:
    """**插件自己**抛 `CancelledError`（任务并未被取消）——是业务结果，不是外部取消。"""

    safety = SafetyProfile(request_timeout=30.0)
    param_model = None

    async def execute(self, *, ctx, params):
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_outer_timeout_is_not_rewritten_into_user_cancel():
    """外层超时必须照旧抛 `TimeoutError`——不得被伪造成"用户取消"业务结果。"""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            SafetyPipeline().execute(
                _BlockingPlugin(), "blocking", None, {}, semaphore=None),
            timeout=0.05,
        )


@pytest.mark.asyncio
async def test_plugin_self_cancel_still_reports_cancelled_business_result():
    """对照：非外部取消（插件内部自取消）仍归一为 `CANCELLED` 业务结果。"""
    result = await SafetyPipeline().execute(
        _SelfCancelPlugin(), "self_cancel", None, {}, semaphore=None)
    assert result.success is False
    assert result.error_type is ErrorType.CANCELLED
