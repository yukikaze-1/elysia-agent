from __future__ import annotations

import dataclasses
import re
import types
from typing import Any, Union, get_origin, get_args

from .errors import (
    ConfigConstraintError,
    ConfigError,
    ConfigMissingError,
    ConfigTypeError,
)
from .field import (
    _Constraint,
    MISSING,
    get_constraints,
    is_required,
)


# ── built-in constraints ──────────────────────────────────────────────


class Range(_Constraint):
    """Value must be within [*min*, *max*] (inclusive by default)."""

    def __init__(self, min: float | int | None = None, max: float | int | None = None):
        self.min = min
        self.max = max

    def check(self, value: Any) -> None:
        if self.min is not None and value < self.min:
            raise ValueError(f"value {value!r} < min {self.min!r}")
        if self.max is not None and value > self.max:
            raise ValueError(f"value {value!r} > max {self.max!r}")

    def describe(self) -> str:
        lo = f"{self.min}" if self.min is not None else "-inf"
        hi = f"{self.max}" if self.max is not None else "+inf"
        return f"Range({lo}, {hi})"


class Choices(_Constraint):
    """Value must be one of the *allowed* values."""

    def __init__(self, allowed: list[Any]):
        if not allowed:
            raise ValueError("Choices must have at least one allowed value")
        self.allowed = allowed

    def check(self, value: Any) -> None:
        if value not in self.allowed:
            raise ValueError(f"value {value!r} not in {self.allowed!r}")

    def describe(self) -> str:
        return f"Choices({self.allowed!r})"


class Regex(_Constraint):
    """Value (str) must match *pattern* (uses ``re.search`` — matches anywhere)."""

    def __init__(self, pattern: str, *, message: str | None = None):
        self.pattern = re.compile(pattern)
        self._message = message or f"must match pattern {pattern!r}"

    def check(self, value: Any) -> None:
        if not isinstance(value, str) or not self.pattern.search(value):
            raise ValueError(self._message)

    def describe(self) -> str:
        return f"Regex({self.pattern.pattern!r})"


# ── validator ──────────────────────────────────────────────────────────


class Validator:
    """Post-load validation for a dataclass config instance.

    Runs three checks in order:
    1. Required fields — must not be MISSING.
    2. Type check — value must be an instance of the annotated type.
    3. Constraints — each constraint.check(value) must pass.

    All errors are collected before raising.
    """

    @staticmethod
    def validate(instance: Any) -> list[ConfigError]:
        """Validate *instance*. Returns a list of errors (empty = valid).

        Does NOT raise — callers decide how to handle the list.
        """
        errors: list[ConfigError] = []
        Validator._validate_one(instance, errors)
        return errors

    @staticmethod
    def _validate_one(instance: Any, errors: list[ConfigError]) -> None:
        cls = type(instance)
        class_name = cls.__name__
        hints = Validator._safe_get_type_hints(cls)

        for fld in dataclasses.fields(cls):
            field_name = fld.name
            value = getattr(instance, field_name)

            # 1. required check
            if is_required(fld) and value is MISSING:
                errors.append(ConfigMissingError(class_name, field_name))
                continue

            if value is MISSING:
                continue

            # 2. type check
            expected_type = hints.get(field_name)
            if expected_type is not None and value is not None:
                # handle Optional[X] (Union[X, None]) — cannot isinstance against raw Union
                origin = get_origin(expected_type)
                if origin is not None and origin in (Union, types.UnionType):
                    # Optional[X] = Union[X, None]; X | None = UnionType
                    check_types = [t for t in get_args(expected_type) if t is not type(None)]
                    if not any(isinstance(value, t) for t in check_types):
                        errors.append(ConfigTypeError(class_name, field_name, expected_type, value))
                        continue
                else:
                    check_type = origin or expected_type
                    if not isinstance(value, check_type):
                        errors.append(ConfigTypeError(class_name, field_name, expected_type, value))
                        continue

            # 3. constraints
            for constraint in get_constraints(fld):
                try:
                    constraint.check(value)
                except ValueError as exc:
                    errors.append(
                        ConfigConstraintError(class_name, field_name, constraint.describe(), value)
                    )

            # 4. recurse into nested dataclass
            if Validator._is_dataclass_instance(value):
                Validator._validate_one(value, errors)

    @staticmethod
    def _is_dataclass_instance(obj: Any) -> bool:
        return dataclasses.is_dataclass(obj) and not isinstance(obj, type)

    @staticmethod
    def _safe_get_type_hints(target_type: type) -> dict[str, type]:
        """Return resolved type hints for *target_type*, or empty dict on failure."""
        import typing
        try:
            return typing.get_type_hints(target_type)
        except Exception:
            return {}
