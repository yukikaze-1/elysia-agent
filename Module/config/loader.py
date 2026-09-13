from __future__ import annotations

import dataclasses
import os
from typing import Any, Generic, TypeVar, Union, get_args, get_origin

import yaml

from Module.config.errors import ConfigEnvFormatError, ConfigValidationError
from Module.config.field import MISSING, get_env_name, is_secret
from Module.config.validator import Validator
from Module.config._utils import is_dataclass_instance, safe_get_type_hints

T = TypeVar("T")


# ── env var helpers ────────────────────────────────────────────────────


class EnvManager:
    """集中式 .env 加载 —— 确保 ``load_dotenv()`` 最多执行一次。"""

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
        # S2-10：解包条件必须**区分 Union 与普通泛型**。
        # 原实现是"只要 `__origin__` 非空就走解包、取 `__args__` 的第一个"——对
        # `List[str]` 来说 `__args__[0]` 是**元素类型 str**，于是 `List[str]` /
        # `Dict[str, int]` / `list[str]` 全部被当成 `str` 处理，env 覆盖原样返回**字符串**
        # （该是 list 的地方拿到 str → 启动期类型错乱）。`Optional[List[str]]` 更绕：
        # 先取到 `List[str]` 这个泛型本身，而它又不是 `list`，同样落进 `str` 分支。
        origin = get_origin(target_type)
        if origin is Union:
            # Optional[X] / Union[X, ...] → 取第一个非 None 类型，**并继续解包它的 origin**
            non_none = [a for a in get_args(target_type) if a is not type(None)]  # noqa: E721
            inner = non_none[0] if non_none else target_type
            base_type = get_origin(inner) or inner
        elif origin is not None:
            # 普通泛型：容器类型才是目标（List[str] → list、Dict[str, int] → dict）
            base_type = origin
        else:
            base_type = target_type

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
        # list / dict —— 尝试 JSON 解析
        if base_type in (list, dict):
            import json
            return json.loads(value_str)
        return value_str


# ── loader ─────────────────────────────────────────────────────────────


class ConfigLoader(Generic[T]):
    """从默认值 < YAML < 环境变量加载 dataclass 配置，然后校验。

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
        """将 *yaml_data* 递归合并进 *instance* 的字段。

        - ``secret=True`` 的字段被跳过（永不从 YAML 读取）。
        - 嵌套的 dataclass 字段会递归处理。
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
        """遍历每个字段；若存在对应环境变量则转换并设置。

        递归进入嵌套 dataclass 字段，按字段名链拼接环境变量名
        （如 ``inner_x`` → ``INNER_X``）。
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
