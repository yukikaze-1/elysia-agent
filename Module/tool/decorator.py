"""@tool 装饰器 — 面向用户的工具定义语法糖。

用法:
    @tool(description="获取当前时间", timeout=5.0)
    async def get_time() -> str:
        ...

    @tool  # name 自动从函数名取
    async def search(query: str, limit: int = 5) -> str:
        ...

    @tool(parameters={"query": ToolParameter(...)})  # 手动覆盖 schema
    async def complex_tool(...) -> str:
        ...
"""

from __future__ import annotations

import inspect
import typing
from functools import wraps
from typing import Any, Callable

from .protocol import ResultType, ToolContext, ToolDefinition, ToolEmitter, ToolParameter
from .schema import type_hints_to_params


def tool(
    func_or_name=None,
    *,
    name: str | None = None,
    description: str = "",
    timeout: float = 30.0,
    max_result_len: int = 4000,
    parameters: dict[str, ToolParameter] | None = None,
    result_type: ResultType = "text",
    requires: list[str] | None = None,
    side_effects: bool = False,
    progress_timeout: float = 0.0,
) -> Callable:
    """标记一个 async function 为工具。

    支持两种用法:
        @tool                       # bare, name 自动从函数名取
        @tool(timeout=5.0)          # 带参数
        @tool(requires=["web_search"], result_type="mixed", side_effects=True)

    自动从 type hints + docstring 推导参数 schema，除非显式传入 parameters。

    Args:
        func_or_name:   裸调用时传入被装饰函数
        name:           工具名（默认用函数名）
        description:    工具描述（默认用 docstring 第一行）
        timeout:        执行超时（秒）
        max_result_len: 结果最大字符数（截断）
        parameters:     手动覆盖参数 schema（不自动推导）
        result_type:    工具产出类型 ("text"|"image"|"audio"|"file"|"mixed")
        requires:       声明所需资源/能力，如 ["web_search", "database:read"]
        side_effects:   是否会修改外部状态
    """

    # 检测是否是裸调用 `@tool`（第一个参数是函数）
    if callable(func_or_name):
        # `@tool` without parens
        return _decorate_tool(
            func_or_name, name=func_or_name.__name__, description=description,
            timeout=timeout, max_result_len=max_result_len, parameters=parameters,
            result_type=result_type, requires=requires, side_effects=side_effects,
            progress_timeout=progress_timeout,
        )

    # `@tool(...)` with arguments — return decorator
    def decorator(func: Callable) -> Callable:
        return _decorate_tool(
            func, name=name or func.__name__, description=description,
            timeout=timeout, max_result_len=max_result_len, parameters=parameters,
            result_type=result_type, requires=requires, side_effects=side_effects,
            progress_timeout=progress_timeout,
        )
    return decorator


def _decorate_tool(
    func: Callable,
    name: str = "",
    description: str = "",
    timeout: float = 30.0,
    max_result_len: int = 4000,
    parameters: dict[str, ToolParameter] | None = None,
    result_type: ResultType = "text",
    requires: list[str] | None = None,
    side_effects: bool = False,
    progress_timeout: float = 0.0,
) -> Callable:
    # 解析描述
    tool_desc = description
    if not tool_desc and func.__doc__:
        first_line = func.__doc__.strip().split("\n")[0].strip()
        if first_line:
            tool_desc = first_line

    # 解析参数
    if parameters is not None:
        param_map = dict(parameters)
    else:
        param_map, _ = type_hints_to_params(func)

    # 检测 ctx 参数 → 标记为对 LLM 隐藏 + 是否必需
    hidden_params: set[str] = set()
    ctx_required: bool = False
    try:
        hints = typing.get_type_hints(func, include_extras=False)
    except Exception:
        hints = {}
    sig_params = dict(inspect.signature(func).parameters)
    for pname, ptype in hints.items():
        if pname == "return":
            continue
        origin = typing.get_origin(ptype)
        args = typing.get_args(ptype)
        # ToolContext or ToolEmitter or Optional variants → hide from LLM
        if ptype is ToolContext or ptype is ToolEmitter:
            hidden_params.add(pname)
            if ptype is ToolContext:
                # ctx 没有默认值 → 标记为必需（Fail Fast）
                ctx_param = sig_params.get(pname)
                if ctx_param is not None and ctx_param.default is inspect.Parameter.empty:
                    ctx_required = True
        elif origin is not None and (ToolContext in args or ToolEmitter in args):
            hidden_params.add(pname)

    # 构造 ToolDefinition
    definition = ToolDefinition(
        name=name,
        description=tool_desc,
        parameters=param_map,
        required=None,  # auto-derive from parameters
        func=func,
        timeout=timeout,
        max_result_len=max_result_len,
        result_type=result_type,
        requires=requires or [],
        side_effects=side_effects,
        progress_timeout=progress_timeout,
        _hidden_params=hidden_params,
        _ctx_required=ctx_required,
    )

    func._tool_definition = definition  # type: ignore[attr-defined]

    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await func(*args, **kwargs)

    wrapper._tool_definition = definition  # type: ignore[attr-defined]
    return wrapper


def get_tool_definition(func: Callable) -> ToolDefinition | None:
    """从 @tool 装饰过的函数上提取 ToolDefinition。"""
    return getattr(func, "_tool_definition", None)
