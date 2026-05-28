"""安全机制 — 超时守卫、结果截断、参数校验。

被 ToolRegistry.execute() 调用，不暴露给用户。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .protocol import ToolParameter

logger = logging.getLogger(__name__)


async def execute_with_timeout(func, kwargs: dict, timeout: float) -> Any:
    """asyncio.wait_for 包裹执行。TimeoutError → 抛 TimeoutError（由 caller 转为 ToolResult）。

    Raises:
        asyncio.TimeoutError: 执行超时
        Exception: 工具内部的任何异常（传播给 caller）
    """
    return await asyncio.wait_for(func(**kwargs), timeout=timeout)


def truncate_result(result: Any, max_len: int) -> tuple[str, bool]:
    """str() 转换后截断到 max_len 字符。

    Returns:
        (content, truncated) — truncated=True 表示内容被截断
    """
    text = str(result)
    if len(text) <= max_len:
        return text, False
    return text[:max_len] + "…", True


def validate_args(params: dict[str, ToolParameter], raw_args: dict) -> tuple[dict, list[str]]:
    """类型转换 + 必填检查。

    Returns:
        (coerced_args, errors) — errors 为空表示校验通过
    """
    coerced: dict[str, Any] = {}
    errors: list[str] = []

    for name, param in params.items():
        # 获取原始值
        if name not in raw_args:
            if param.required and not param.default_set:
                errors.append(f"缺少必填参数: {name}")
            elif param.default_set:
                coerced[name] = param.default
            continue

        value = raw_args[name]
        coerced_value, err = _coerce_value(name, value, param.type)
        if err:
            errors.append(err)
        else:
            coerced[name] = coerced_value

    # 检查未知参数（不在定义中的, 只记录不报错）
    for name in raw_args:
        if name not in params:
            coerced[name] = raw_args[name]

    return coerced, errors


def _coerce_value(name: str, value: Any, target_type: str) -> tuple[Any, str | None]:
    """将 value 转换为 target_type。返回 (coerced, error)。"""
    if target_type == "integer":
        if isinstance(value, bool):
            return value, None  # bool is subclass of int, don't convert
        if isinstance(value, int):
            return value, None
        try:
            return int(value), None
        except (ValueError, TypeError):
            return None, f"参数 {name} 需要整数类型，收到: {value!r}"

    if target_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), None
        try:
            return float(value), None
        except (ValueError, TypeError):
            return None, f"参数 {name} 需要数字类型，收到: {value!r}"

    if target_type == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes"):
                return True, None
            if v in ("false", "0", "no"):
                return False, None
        return None, f"参数 {name} 需要布尔类型，收到: {value!r}"

    # string / object / array — pass through
    return value, None
