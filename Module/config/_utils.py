"""config 包内共享工具函数（避免 loader ↔ validator 循环导入）。"""
from __future__ import annotations

import dataclasses
from typing import Any


def is_dataclass_instance(obj: Any) -> bool:
    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)


def safe_get_type_hints(target_type: type) -> dict[str, type]:
    """Return resolved type hints for *target_type*, or empty dict on failure."""
    import logging
    import typing

    logger = logging.getLogger(__name__)
    try:
        return typing.get_type_hints(target_type)
    except (NameError, TypeError, AttributeError):
        # 仅限可预期的解析失败（前向引用未定义 / 注解非法）——其他异常
        # 是真实 bug，应传播而非吞掉（O4），否则错误类型静默通过加载。
        logger.warning("safe_get_type_hints 失败，类型检查跳过 — type=%s", target_type.__name__,
                       exc_info=True)
        return {}
