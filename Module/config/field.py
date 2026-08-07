from __future__ import annotations

import dataclasses
from typing import Any

# 存储在 dataclasses.Field.metadata 上的元数据键
_KEY_SECRET = "_config_secret"
_KEY_ENV = "_config_env"
_KEY_CONSTRAINTS = "_config_constraints"
_KEY_REQUIRED = "_config_required"

# 哨兵值：用于区分「未设置」与 None / "" / False
MISSING: Any = object()


# 前向声明：让约束类型可以在注解中引用自身。
class _Constraint:
    """字段约束基类。子类需实现 check()。"""

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
    """创建带配置语义的 dataclass 字段。

    Parameters
    ----------
    default:
        字段默认值。省略时字段以 MISSING 开始，必须通过 YAML 或环境变量提供
        （否则触发 required 错误）。
    default_factory:
        生成默认值的零参可调用对象。与 *default* 互斥。
    env:
        显式环境变量名。为 ``None``（默认）时，变量名由字段名大写推导，
        可选地加上 loader 的 *env_prefix* 前缀。
    secret:
        为 ``True`` 时字段永不从 YAML 读取 —— 只能通过环境变量设置。
        隐含 ``required=True``。
    required:
        为 ``True`` 时，YAML 合并与环境变量覆盖后值仍为 MISSING 则抛出
        ``ConfigMissingError``。
    constraints:
        加载后值必须满足的约束对象列表（``Range``、``Choices``、``Regex`` 等）。
    help:
        字段的人类可读描述。
    **field_kwargs:
        透传给 ``dataclasses.field()``（如 ``repr``、``compare``）。
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
