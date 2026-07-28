from __future__ import annotations

import dataclasses
from typing import Any

# Metadata keys stored on dataclasses.Field.metadata
_KEY_SECRET = "_config_secret"
_KEY_ENV = "_config_env"
_KEY_CONSTRAINTS = "_config_constraints"
_KEY_REQUIRED = "_config_required"

# Sentinel to distinguish "not set" from None / "" / False
MISSING: Any = object()


# Forward declaration so constraint types can reference themselves in annotations.
class _Constraint:
    """Base class for field constraints. Subclass and implement check()."""

    def check(self, value: Any) -> None:
        """检查 value 是否满足约束，不满足则抛出异常。

        Returns:
            None
        """
        raise NotImplementedError

    def describe(self) -> str:
        """返回约束的人类可读描述。

        Returns:
            str: 约束描述字符串。
        """
        return self.__class__.__name__


def ConfigField(
    *,
    default: Any = MISSING,
    default_factory: Any = MISSING,
    env: str | None = None,
    secret: bool = False,
    required: bool = False,
    constraints: list[_Constraint] | None = None,
    help: str = "",
    **field_kwargs: Any,
) -> Any:
    """Create a dataclass field with configuration semantics.

    Parameters
    ----------
    default:
        Default value for the field. If omitted, the field starts as MISSING
        and must be supplied via YAML or env var (or a required error is raised).
    default_factory:
        Zero-argument callable that produces a default. Mutually exclusive
        with *default*.
    env:
        Explicit environment variable name. When ``None`` (the default), the
        variable name is derived from the field name upper-cased, optionally
        prefixed by the loader's *env_prefix*.
    secret:
        If ``True`` the field is never read from YAML — it can only be set
        via an environment variable. Implies ``required=True``.
    required:
        If ``True`` the loader will raise ``ConfigMissingError`` when the
        value is still MISSING after YAML merge and env override.
    constraints:
        List of constraint objects (``Range``, ``Choices``, ``Regex``, etc.)
        that the value must satisfy after loading.
    help:
        Human-readable description of the field.
    **field_kwargs:
        Passed through to ``dataclasses.field()`` (e.g. ``repr``, ``compare``).
    """
    if secret and not required:
        required = True

    metadata: dict[str, Any] = {
        _KEY_SECRET: secret,
        _KEY_ENV: env,
        _KEY_CONSTRAINTS: constraints or [],
        _KEY_REQUIRED: required,
    }
    if help:
        metadata["help"] = help

    kwargs: dict[str, Any] = {"metadata": metadata}
    kwargs.update(field_kwargs)

    if default is not MISSING:
        kwargs["default"] = default
    elif default_factory is not MISSING:
        kwargs["default_factory"] = default_factory
    else:
        kwargs["default"] = MISSING

    return dataclasses.field(**kwargs)


def _get_metadata(field: dataclasses.Field[Any], key: str, default: Any = None) -> Any:
    """从字段 metadata 字典中读取一个值，不存在时返回 *default*。

    Args:
        field: dataclass 字段对象。
        key: metadata 中的键名。
        default: 键不存在时的回退值。

    Returns:
        读取到的 metadata 值，或 default。
    """
    return field.metadata.get(key, default)


def is_secret(field: dataclasses.Field[Any]) -> bool:
    """判断字段是否标记为 secret（只从环境变量读取）。

    Args:
        field: dataclass 字段对象。

    Returns:
        bool: 是否为 secret 字段。
    """
    return _get_metadata(field, _KEY_SECRET, False)


def get_env_name(field: dataclasses.Field[Any]) -> str | None:
    """获取字段显式指定的环境变量名。

    Args:
        field: dataclass 字段对象。

    Returns:
        str | None: 环境变量名，未指定时返回 None。
    """
    return _get_metadata(field, _KEY_ENV, None)


def get_constraints(field: dataclasses.Field[Any]) -> list[_Constraint]:
    """获取字段上绑定的约束列表。

    Args:
        field: dataclass 字段对象。

    Returns:
        list[_Constraint]: 约束对象列表，未设置时返回空列表。
    """
    return _get_metadata(field, _KEY_CONSTRAINTS, [])


def is_required(field: dataclasses.Field[Any]) -> bool:
    """判断字段是否为必填。

    Args:
        field: dataclass 字段对象。

    Returns:
        bool: 是否为必填字段。
    """
    return _get_metadata(field, _KEY_REQUIRED, False)
