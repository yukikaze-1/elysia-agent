from __future__ import annotations

import dataclasses
import os
from typing import Any, Generic, TypeVar

import yaml

from .errors import ConfigEnvFormatError, ConfigValidationError
from .field import MISSING, get_env_name, is_secret
from .validator import Validator

T = TypeVar("T")


# ── env var helpers ────────────────────────────────────────────────────


class EnvManager:
    """Centralised .env loading — ensures ``load_dotenv()`` runs at most once."""

    _loaded: bool = False

    @classmethod
    def ensure_dotenv(cls, dotenv_path: str | None = None) -> None:
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
        """Derive the environment variable name for a field."""
        return field_name.upper()

    @staticmethod
    def coerce(value_str: str, target_type: type) -> Any:
        """Coerce a string (from env var) to *target_type*.

        Supports ``Optional[X]`` by unwrapping to the inner type.
        For ``Union[X, Y, ...]`` only the **first** non-None type is used
        (e.g. ``Union[int, str]`` tries ``int``).  If you need alternate
        parsing, provide the value as a string-typed field and parse it
        yourself after loading.
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
        self.config_class = config_class
        self.env_path = env_path
        self.yaml_path = yaml_path
        self.yaml_key = yaml_key

    # ── public API ─────────────────────────────────────────────────

    def load(self) -> T:
        """Full pipeline: defaults → YAML → env → validate."""
        EnvManager.ensure_dotenv(self.env_path)

        instance = self._instantiate_defaults()
        yaml_data = self._load_yaml(self.yaml_path, self.yaml_key)
        self._merge_yaml(instance, yaml_data)
        self._override_from_env(instance)
        self._validate(instance)
        return instance

    def load_from_dict(self, data: dict[str, Any]) -> T:
        """Load from a programmatic dict (same merge/env/validate pipeline, no YAML file)."""
        EnvManager.ensure_dotenv(self.env_path)

        instance = self._instantiate_defaults()
        self._merge_yaml(instance, data)
        self._override_from_env(instance)
        self._validate(instance)
        return instance

    # ── internal steps ─────────────────────────────────────────────

    def _instantiate_defaults(self) -> T:
        return self.config_class()

    def _load_yaml(
        self, yaml_path: str | None, yaml_key: str | None
    ) -> dict[str, Any]:
        """Read and (optionally) scope a YAML file. Returns {} if no path given."""
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

    @staticmethod
    def _is_dataclass_instance(obj: Any) -> bool:
        return dataclasses.is_dataclass(obj) and not isinstance(obj, type)

    @staticmethod
    def _safe_get_type_hints(target_type: type) -> dict[str, type]:
        """Return resolved type hints for *target_type*, or empty dict on failure."""
        try:
            return __import__("typing").get_type_hints(target_type)
        except Exception:
            return {}
