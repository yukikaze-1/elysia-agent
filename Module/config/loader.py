from __future__ import annotations

import dataclasses
import os
from typing import Any, Generic, TypeVar

import yaml

from .errors import ConfigEnvFormatError, ConfigValidationError
from .field import MISSING, get_env_name, is_secret
from .validator import Validator
from ._utils import is_dataclass_instance, safe_get_type_hints

T = TypeVar("T")


# ── env var helpers ────────────────────────────────────────────────────


class EnvManager:
    """Centralised .env loading — ensures ``load_dotenv()`` runs at most once."""

    _loaded: bool = False

    @classmethod
    def ensure_dotenv(cls, dotenv_path: str | None = None) -> None:
        """加载 .env 文件，确保全局只执行一次。

        Args:
            dotenv_path: .env 文件的显式路径，为 None 时自动查找。
        """
        if cls._loaded:
            return
        from dotenv import load_dotenv

        if dotenv_path:
            load_dotenv(dotenv_path=dotenv_path)
        else:
            load_dotenv()
        cls._loaded = True

    @staticmethod
    def resolve_env_name(field_name: str) -> str:
        """从字段名推导环境变量名（大写转换）。

        Args:
            field_name: 字段名（snake_case）。

        Returns:
            str: 大写的环境变量名。
        """
        return field_name.upper()

    @staticmethod
    def coerce(value_str: str, target_type: type) -> Any:
        """将环境变量字符串强制转换为 *target_type*。

        支持 ``Optional[X]``（解包到内层类型）。对于 ``Union[X, Y, ...]`` 只使用
        **第一个**非 None 类型（如 ``Union[int, str]`` 尝试 ``int``）。
        如需其他解析方式，可将字段声明为 str 类型并在加载后自行解析。

        Args:
            value_str: 环境变量的原始字符串值。
            target_type: 目标类型。

        Returns:
            转换后的值。
        """
        origin = getattr(target_type, "__origin__", None)
        base_type = origin if origin is not None else target_type
        # unwrap Optional[X] / Union[X, ...]  →  first non-None type
        if origin is not None:
            args = getattr(target_type, "__args__", ())
            non_none = [a for a in args if a is not type(None)]  # noqa: E721
            if non_none:
                base_type = non_none[0]

        if base_type is bool:
            lowered = value_str.lower()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no"):
                return False
            raise ValueError(f"cannot parse {value_str!r} as bool")
        if base_type is int:
            return int(value_str)
        if base_type is float:
            return float(value_str)
        if base_type is str:
            return value_str
        # list / dict — try JSON
        if base_type in (list, dict):
            import json
            return json.loads(value_str)
        return value_str


# ── loader ─────────────────────────────────────────────────────────────


class ConfigLoader(Generic[T]):
    """Load a dataclass config from defaults < YAML < env vars, then validate.

    Usage::

        @dataclass
        class MyConfig:
            host: str = ConfigField(default="0.0.0.0")
            port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])

        loader = ConfigLoader(MyConfig, yaml_path="config.yaml", yaml_key="Server")
        cfg = loader.load()
    """

    def __init__(
        self,
        config_class: type[T],
        *,
        env_path: str | None = None,
        yaml_path: str | None = None,
        yaml_key: str | None = None,
    ):
        """初始化配置加载器。

        Args:
            config_class: 目标 dataclass 类型。
            env_path: .env 文件的显式路径，为 None 时自动查找。
            yaml_path: YAML 配置文件的路径，为 None 时跳过 YAML 加载。
            yaml_key: YAML 中的顶级键（如 "Server"），支持点号分隔的子键。
        """
        self.config_class = config_class
        self.env_path = env_path
        self.yaml_path = yaml_path
        self.yaml_key = yaml_key

    # ── public API ─────────────────────────────────────────────────

    def load(self) -> T:
        """完整管线：默认值 → YAML → 环境变量 → 校验。

        Returns:
            T: 加载并校验完成的配置实例。
        """
        EnvManager.ensure_dotenv(self.env_path)

        instance = self._instantiate_defaults()
        yaml_data = self._load_yaml(self.yaml_path, self.yaml_key)
        self._merge_yaml(instance, yaml_data)
        self._override_from_env(instance)
        self._validate(instance)
        return instance

    def load_from_dict(self, data: dict[str, Any]) -> T:
        """从程序化字典加载（同样的 merge/env/validate 管线，无需 YAML 文件）。

        Args:
            data: 配置字典，key 对应字段名。

        Returns:
            T: 加载并校验完成的配置实例。
        """
        EnvManager.ensure_dotenv(self.env_path)

        instance = self._instantiate_defaults()
        self._merge_yaml(instance, data)
        self._override_from_env(instance)
        self._validate(instance)
        return instance

    # ── internal steps ─────────────────────────────────────────────

    def _instantiate_defaults(self) -> T:
        """使用 dataclass 默认值实例化配置对象。

        Returns:
            T: 只含默认值的配置实例。
        """
        return self.config_class()

    def _load_yaml(
        self, yaml_path: str | None, yaml_key: str | None
    ) -> dict[str, Any]:
        """读取并（可选）限定 YAML 文件的作用域。

        Args:
            yaml_path: YAML 文件路径，为 None 时返回空字典。
            yaml_key: YAML 顶级键（支持点号分隔），为 None 时返回全部内容。

        Returns:
            dict[str, Any]: 解析后的 YAML 数据字典。
        """
        if not yaml_path:
            return {}
        with open(yaml_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        if yaml_key:
            for part in yaml_key.split("."):
                if isinstance(raw, dict):
                    raw = raw.get(part, {})
                else:
                    return {}
        return raw if isinstance(raw, dict) else {}

    def _merge_yaml(self, instance: Any, yaml_data: dict[str, Any]) -> None:
        """Recursively merge *yaml_data* into *instance* fields.

        - ``secret=True`` fields are skipped (never read from YAML).
        - Nested dataclass fields are recursed into.
        """
        if not yaml_data:
            return

        cls = type(instance)
        for fld in dataclasses.fields(cls):
            field_name = fld.name
            if field_name not in yaml_data:
                continue
            if is_secret(fld):
                continue

            yaml_value = yaml_data[field_name]
            if yaml_value is None:
                continue  # YAML 空值视为"未设置"，不覆盖默认值
            current = getattr(instance, field_name)

            if self._is_dataclass_instance(current) and isinstance(yaml_value, dict):
                self._merge_yaml(current, yaml_value)
            else:
                setattr(instance, field_name, yaml_value)

    def _override_from_env(self, instance: Any, prefix: str = "") -> None:
        """Walk every field; if an env var exists, coerce and set it.

        Recurses into nested dataclass fields, building the env var name
        from the chain of field names (e.g. ``inner_x`` → ``INNER_X``).
        """
        cls = type(instance)
        hints = self._safe_get_type_hints(cls)
        for fld in dataclasses.fields(cls):
            field_name = fld.name
            current_value = getattr(instance, field_name)

            if self._is_dataclass_instance(current_value):
                self._override_from_env(current_value, prefix=f"{field_name}_")
                continue

            env_name = get_env_name(fld) or EnvManager.resolve_env_name(prefix + field_name)
            env_val = os.getenv(env_name)
            if env_val is not None:
                target_type = hints.get(field_name, str)
                try:
                    coerced = EnvManager.coerce(env_val, target_type)
                except (ValueError, TypeError) as exc:
                    raise ConfigEnvFormatError(env_name, env_val, target_type) from exc
                setattr(instance, field_name, coerced)

    def _validate(self, instance: T) -> None:
        errors = Validator.validate(instance)
        if errors:
            raise ConfigValidationError(errors)

    # ── helpers ────────────────────────────────────────────────────

    _is_dataclass_instance = staticmethod(is_dataclass_instance)
    _safe_get_type_hints = staticmethod(safe_get_type_hints)
