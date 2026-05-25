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
        """Raise an error if the value violates this constraint."""
        raise NotImplementedError

    def describe(self) -> str:
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
    """Read a value from a field's metadata dict, falling back to *default*."""
    return field.metadata.get(key, default)


def is_secret(field: dataclasses.Field[Any]) -> bool:
    return _get_metadata(field, _KEY_SECRET, False)


def get_env_name(field: dataclasses.Field[Any]) -> str | None:
    return _get_metadata(field, _KEY_ENV, None)


def get_constraints(field: dataclasses.Field[Any]) -> list[_Constraint]:
    return _get_metadata(field, _KEY_CONSTRAINTS, [])


def is_required(field: dataclasses.Field[Any]) -> bool:
    return _get_metadata(field, _KEY_REQUIRED, False)
