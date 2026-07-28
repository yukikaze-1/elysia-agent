from typing import Any


class ConfigError(Exception):
    """Base class for all configuration errors."""
    pass


class ConfigMissingError(ConfigError):
    """A required field has no value after loading.

    Args:
        class_name: 所属 dataclass 类名。
        field_name: 字段名。
    """

    def __init__(self, class_name: str, field_name: str):
        super().__init__(f"{class_name}.{field_name}: required field is missing")
        self.class_name = class_name
        self.field_name = field_name


class ConfigTypeError(ConfigError):
    """A field's value does not match the declared type annotation.

    Args:
        class_name: 所属 dataclass 类名。
        field_name: 字段名。
        expected: 期望的类型。
        got_value: 实际拿到的值。
    """

    def __init__(self, class_name: str, field_name: str, expected: type, got_value: Any):
        super().__init__(
            f"{class_name}.{field_name}: expected {expected.__name__}, got {type(got_value).__name__} (value={got_value!r})"
        )
        self.class_name = class_name
        self.field_name = field_name
        self.expected = expected
        self.got_value = got_value


class ConfigConstraintError(ConfigError):
    """A constraint on a field was violated.

    Args:
        class_name: 所属 dataclass 类名。
        field_name: 字段名。
        constraint: 约束描述字符串。
        value: 违反约束的值。
    """

    def __init__(self, class_name: str, field_name: str, constraint: str, value: Any):
        super().__init__(
            f"{class_name}.{field_name}: constraint '{constraint}' violated (value={value!r})"
        )
        self.class_name = class_name
        self.field_name = field_name
        self.constraint = constraint
        self.value = value


class ConfigEnvFormatError(ConfigError):
    """An environment variable string could not be coerced to the target type.

    Args:
        env_key: 环境变量名。
        value: 环境变量的原始字符串值。
        target_type: 期望的目标类型。
    """

    def __init__(self, env_key: str, value: str, target_type: type):
        super().__init__(
            f"env var {env_key}={value!r} cannot be parsed as {target_type.__name__}"
        )
        self.env_key = env_key
        self.value = value
        self.target_type = target_type


class ConfigValidationError(ConfigError):
    """Aggregate error that collects multiple ConfigError instances.

    Args:
        errors: 收集到的 ConfigError 列表。
    """

    def __init__(self, errors: list[ConfigError]):
        self.errors = errors
        lines = "\n  - ".join([""] + [str(e) for e in errors])
        super().__init__(f"{len(errors)} config error(s):{lines}")
