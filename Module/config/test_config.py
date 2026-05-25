from __future__ import annotations

import dataclasses
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from Module.config import (
    MISSING,
    Choices,
    ConfigConstraintError,
    ConfigEnvFormatError,
    ConfigField,
    ConfigLoader,
    ConfigMissingError,
    ConfigTypeError,
    ConfigValidationError,
    Range,
    Regex,
    Validator,
)
from Module.config.loader import EnvManager


# ── test dataclasses ───────────────────────────────────────────────────


@dataclasses.dataclass
class FlatConfig:
    """混合类型、带 Range 约束和 secret 字段的扁平配置。"""

    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])
    debug: bool = ConfigField(default=False)
    api_key: str = ConfigField(default="", secret=True)


@dataclasses.dataclass
class InnerConfig:
    """嵌套配置，供 OuterConfig 使用，用于测试递归合并与校验。"""

    x: int = ConfigField(default=1)
    y: str = ConfigField(default="hello")


@dataclasses.dataclass
class OuterConfig:
    """包含嵌套 dataclass 字段的父配置（通过 default_factory 创建）。"""

    name: str = ConfigField(default="app")
    inner: InnerConfig = ConfigField(default_factory=InnerConfig)


@dataclasses.dataclass
class ConstrainedConfig:
    """覆盖三种内置约束类型：Choices、Range、Regex。"""

    level: str = ConfigField(default="INFO", constraints=[Choices(["DEBUG", "INFO", "WARNING"])])
    rate: float = ConfigField(default=1.0, constraints=[Range(0.0, 100.0)])
    url: str = ConfigField(
        default="http://localhost",
        constraints=[Regex(r"^https?://", message="Must be HTTP(S) URL")],
    )


@dataclasses.dataclass
class RequiredConfig:
    """显式声明 required 和 secret 的配置（值只能来自环境变量）。"""

    name: str = ConfigField(default="default")
    token: str = ConfigField(required=True, secret=True)


# ── ConfigField ────────────────────────────────────────────────────────


class TestConfigField:
    """ConfigField() 单元测试 — 元数据、默认值、secret/required/env 的关联逻辑。"""

    def test_default_value(self):
        fld = ConfigField(default="hello")
        assert fld.default == "hello"
        assert fld.metadata["_config_secret"] is False
        assert fld.metadata["_config_required"] is False

    def test_default_is_missing_when_not_specified(self):
        fld = ConfigField()
        assert fld.default is MISSING

    def test_default_factory(self):
        fld = ConfigField(default_factory=list)
        assert fld.default_factory is list

    def test_secret_implies_required(self):
        # ConfigField 工厂在 secret=True 时强制 required=True，
        # 因为 secret 字段唯一合法的来源是环境变量。
        fld = ConfigField(secret=True)
        assert fld.metadata["_config_secret"] is True
        assert fld.metadata["_config_required"] is True

    def test_env_explicit(self):
        fld = ConfigField(env="MY_VAR")
        assert fld.metadata["_config_env"] == "MY_VAR"

    def test_env_none_by_default(self):
        fld = ConfigField()
        assert fld.metadata["_config_env"] is None

    def test_constraints_stored(self):
        r = Range(0, 10)
        fld = ConfigField(constraints=[r])
        assert fld.metadata["_config_constraints"] == [r]

    def test_required_explicit(self):
        fld = ConfigField(required=True)
        assert fld.metadata["_config_required"] is True

    def test_extra_kwargs_passthrough(self):
        fld = ConfigField(repr=False)
        assert fld.repr is False


# ── ConfigLoader: defaults only ────────────────────────────────────────


class TestLoaderDefaults:
    """无 YAML、无环境变量 — 纯默认值实例化。"""

    def test_flat_defaults(self):
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8000
        assert cfg.debug is False
        # secret+required 字段通过校验，因为 default="" 不是 MISSING 哨兵值。
        assert cfg.api_key == ""

    def test_nested_defaults(self):
        loader = ConfigLoader(OuterConfig)
        cfg = loader.load()
        assert cfg.name == "app"
        assert cfg.inner.x == 1
        assert cfg.inner.y == "hello"

    def test_default_factory_creates_distinct_instances(self):
        loader = ConfigLoader(OuterConfig)
        cfg1 = loader.load()
        cfg2 = loader.load()
        assert cfg1.inner is not cfg2.inner


# ── ConfigLoader: YAML merge ───────────────────────────────────────────


class TestLoaderYAML:
    """YAML 文件加载 — 覆盖默认值、key 作用域、嵌套合并、边界情况。"""

    def test_yaml_overrides_defaults(self):
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"host": "127.0.0.1", "port": 3000}, yaml_file)
        yaml_file.close()

        try:
            loader = ConfigLoader(FlatConfig, yaml_path=yaml_file.name)
            cfg = loader.load()
            assert cfg.host == "127.0.0.1"
            assert cfg.port == 3000
            assert cfg.debug is False  # 不在 YAML 中，保留默认值
        finally:
            os.unlink(yaml_file.name)

    def test_yaml_key_scoping(self):
        # yaml_key="MyApp" 先定位到 YAML 的子节点再合并，因此只有
        # {"name": "scoped_name"} 这个子字典会被使用。
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"MyApp": {"name": "scoped_name"}}, yaml_file)
        yaml_file.close()

        try:
            loader = ConfigLoader(OuterConfig, yaml_path=yaml_file.name, yaml_key="MyApp")
            cfg = loader.load()
            assert cfg.name == "scoped_name"
            assert cfg.inner.x == 1  # 不在 YAML 作用域内，保留默认值
        finally:
            os.unlink(yaml_file.name)

    def test_nested_yaml_merge(self):
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"inner": {"x": 42, "y": "world"}}, yaml_file)
        yaml_file.close()

        try:
            loader = ConfigLoader(OuterConfig, yaml_path=yaml_file.name)
            cfg = loader.load()
            assert cfg.inner.x == 42
            assert cfg.inner.y == "world"
        finally:
            os.unlink(yaml_file.name)

    def test_secret_field_skipped_in_yaml(self):
        # Secret 字段不参与 YAML 合并 — 其值只能来自环境变量（或声明的默认值）。
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"api_key": "should-not-be-read"}, yaml_file)
        yaml_file.close()

        try:
            loader = ConfigLoader(FlatConfig, yaml_path=yaml_file.name)
            cfg = loader.load()
            assert cfg.api_key == ""
        finally:
            os.unlink(yaml_file.name)

    def test_missing_yaml_file_raises_error(self):
        loader = ConfigLoader(FlatConfig, yaml_path="/nonexistent/path.yaml")
        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_empty_yaml_file(self):
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml_file.write("")
        yaml_file.close()

        try:
            loader = ConfigLoader(FlatConfig, yaml_path=yaml_file.name)
            cfg = loader.load()
            assert cfg.host == "0.0.0.0"  # 全部使用默认值
        finally:
            os.unlink(yaml_file.name)


# ── ConfigLoader: env var override ─────────────────────────────────────


class TestLoaderEnv:
    """环境变量覆盖 — 自动推导名称、显式 env=、类型转换。"""

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("HOST", "10.0.0.1")
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        assert cfg.host == "10.0.0.1"

    def test_env_overrides_yaml(self):
        # 优先级：环境变量 > YAML > 默认值。HOST 环境变量必须覆盖 YAML 中的 "127.0.0.1"。
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"host": "127.0.0.1"}, yaml_file)
        yaml_file.close()

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("HOST", "env-override.example.com")
                loader = ConfigLoader(FlatConfig, yaml_path=yaml_file.name)
                cfg = loader.load()
                assert cfg.host == "env-override.example.com"
        finally:
            os.unlink(yaml_file.name)

    def test_env_name_auto_derived_from_field(self, monkeypatch):
        """未指定 env= 时，环境变量名自动推导为 field_name.upper()。"""
        monkeypatch.setenv("HOST", "auto-derived.example.com")
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        assert cfg.host == "auto-derived.example.com"

    def test_explicit_env_name(self, monkeypatch):
        """显式指定 env= 的字段应使用该名称，而非自动推导的名称。"""

        @dataclasses.dataclass
        class ExplicitEnvConfig:
            key: str = ConfigField(default="default", env="EXPLICIT_VAR")

        monkeypatch.setenv("EXPLICIT_VAR", "from-explicit")
        # 不指定 env= 时，自动推导名应为 "KEY"；显式指定 env= 优先
        loader = ConfigLoader(ExplicitEnvConfig)
        cfg = loader.load()
        assert cfg.key == "from-explicit"

    def test_env_type_coercion_int(self, monkeypatch):
        monkeypatch.setenv("PORT", "9999")
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        assert cfg.port == 9999
        assert isinstance(cfg.port, int)

    def test_env_type_coercion_float(self, monkeypatch):
        @dataclasses.dataclass
        class FloatConfig:
            rate: float = ConfigField(default=1.0)

        monkeypatch.setenv("RATE", "2.5")
        loader = ConfigLoader(FloatConfig)
        cfg = loader.load()
        assert cfg.rate == 2.5
        assert isinstance(cfg.rate, float)

    def test_env_type_coercion_bool(self, monkeypatch):
        monkeypatch.setenv("DEBUG", "true")
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        assert cfg.debug is True

    def test_env_type_coercion_bool_false(self, monkeypatch):
        @dataclasses.dataclass
        class BoolConfig:
            flag: bool = ConfigField(default=True)

        monkeypatch.setenv("FLAG", "false")
        loader = ConfigLoader(BoolConfig)
        cfg = loader.load()
        assert cfg.flag is False

    def test_env_invalid_coercion_raises(self, monkeypatch):
        monkeypatch.setenv("PORT", "not-a-number")
        loader = ConfigLoader(FlatConfig)
        with pytest.raises(ConfigEnvFormatError) as exc_info:
            loader.load()
        assert "PORT" in str(exc_info.value)

    def test_nested_env_override(self, monkeypatch):
        """嵌套 dataclass 字段通过组合名（inner_x → INNER_X）覆盖。"""
        monkeypatch.setenv("INNER_X", "42")
        monkeypatch.setenv("INNER_Y", "world")
        loader = ConfigLoader(OuterConfig)
        cfg = loader.load()
        assert cfg.inner.x == 42
        assert cfg.inner.y == "world"

    def test_nested_env_partial_override_preserves_defaults(self, monkeypatch):
        """仅覆盖嵌套字段中的一个，其余保留默认值。"""
        monkeypatch.setenv("INNER_Y", "env-value")
        loader = ConfigLoader(OuterConfig)
        cfg = loader.load()
        assert cfg.inner.x == 1  # 默认值保留
        assert cfg.inner.y == "env-value"

    def test_nested_env_override_overrides_yaml(self):
        """嵌套字段的环境变量优先级高于 YAML。"""
        yaml_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        yaml.dump({"inner": {"x": 999, "y": "from-yaml"}}, yaml_file)
        yaml_file.close()

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("INNER_X", "77")
                loader = ConfigLoader(OuterConfig, yaml_path=yaml_file.name)
                cfg = loader.load()
                assert cfg.inner.x == 77  # env 覆盖 YAML
                assert cfg.inner.y == "from-yaml"  # 无 env，保留 YAML
        finally:
            os.unlink(yaml_file.name)


# ── Validation ─────────────────────────────────────────────────────────


class TestValidation:
    """加载后校验 — required、类型、约束、错误收集、Validator 独立使用。"""

    def test_required_field_missing(self):
        @dataclasses.dataclass
        class NeedsToken:
            token: str = ConfigField(required=True)

        loader = ConfigLoader(NeedsToken)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        errs = exc_info.value.errors
        assert len(errs) == 1
        assert isinstance(errs[0], ConfigMissingError)
        assert "token" in str(errs[0])

    def test_type_error(self):
        @dataclasses.dataclass
        class TypedConfig:
            count: int = ConfigField(default="not-int")  # 故意写入错误类型

        loader = ConfigLoader(TypedConfig)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        errs = exc_info.value.errors
        assert any(isinstance(e, ConfigTypeError) for e in errs)

    def test_range_constraint(self):
        @dataclasses.dataclass
        class RangeConfig:
            percent: float = ConfigField(default=150.0, constraints=[Range(0.0, 100.0)])

        loader = ConfigLoader(RangeConfig)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        assert any(isinstance(e, ConfigConstraintError) for e in exc_info.value.errors)

    def test_choices_constraint(self):
        @dataclasses.dataclass
        class ChoiceConfig:
            mode: str = ConfigField(default="INVALID", constraints=[Choices(["A", "B"])])

        loader = ConfigLoader(ChoiceConfig)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        assert any(isinstance(e, ConfigConstraintError) for e in exc_info.value.errors)

    def test_regex_constraint(self):
        @dataclasses.dataclass
        class RegexConfig:
            url: str = ConfigField(default="not-a-url", constraints=[Regex(r"^https?://")])

        loader = ConfigLoader(RegexConfig)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        assert any(isinstance(e, ConfigConstraintError) for e in exc_info.value.errors)

    def test_all_errors_collected(self):
        @dataclasses.dataclass
        class BadConfig:
            a: int = ConfigField(required=True)
            b: str = ConfigField(default="ok", constraints=[Choices(["X", "Y"])])
            c: float = ConfigField(default=999.0, constraints=[Range(0.0, 1.0)])

        loader = ConfigLoader(BadConfig)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        # a: 缺失（required 且无默认值），b: 不在 choices 中，c: 超出范围
        assert len(exc_info.value.errors) == 3

    def test_valid_config_no_error(self):
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load()
        # 不应抛出异常
        errors = Validator.validate(cfg)
        assert errors == []

    def test_validator_standalone(self):
        cfg = FlatConfig()
        errors = Validator.validate(cfg)
        assert errors == []

        cfg2 = FlatConfig()
        cfg2.port = 999999  # 违反 Range(1, 65535)
        errors2 = Validator.validate(cfg2)
        assert len(errors2) == 1
        assert isinstance(errors2[0], ConfigConstraintError)


# ── load_from_dict ─────────────────────────────────────────────────────


class TestLoadFromDict:
    """load_from_dict() — 内存字典合并，语义与 YAML 加载一致。"""

    def test_dict_overrides_defaults(self):
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load_from_dict({"host": "dict-host", "port": 1234})
        assert cfg.host == "dict-host"
        assert cfg.port == 1234
        assert cfg.debug is False  # 不在字典中，保留默认值

    def test_dict_secret_field_skipped(self):
        loader = ConfigLoader(FlatConfig)
        cfg = loader.load_from_dict({"api_key": "should-not-take-effect"})
        assert cfg.api_key == ""


# ── EnvManager ─────────────────────────────────────────────────────────


class TestEnvManager:
    """EnvManager 独立测试 — 环境变量名推导与类型转换（int、float、bool、JSON）。"""

    def test_resolve_env_name(self):
        result = EnvManager.resolve_env_name("api_key")
        assert result == "API_KEY"

    def test_coerce_int(self):
        assert EnvManager.coerce("42", int) == 42

    def test_coerce_float(self):
        assert EnvManager.coerce("3.14", float) == 3.14

    def test_coerce_bool_true(self):
        assert EnvManager.coerce("true", bool) is True
        assert EnvManager.coerce("1", bool) is True
        assert EnvManager.coerce("yes", bool) is True

    def test_coerce_bool_false(self):
        assert EnvManager.coerce("false", bool) is False
        assert EnvManager.coerce("0", bool) is False
        assert EnvManager.coerce("no", bool) is False

    def test_coerce_str(self):
        assert EnvManager.coerce("hello", str) == "hello"

    def test_coerce_list_json(self):
        result = EnvManager.coerce('[1, 2, 3]', list)
        assert result == [1, 2, 3]

    def test_coerce_dict_json(self):
        result = EnvManager.coerce('{"a": 1}', dict)
        assert result == {"a": 1}


# ── Nested dataclass validation ────────────────────────────────────────


class TestNestedValidation:
    """校验递归进入嵌套 dataclass 字段。"""

    def test_nested_type_error(self):
        @dataclasses.dataclass
        class Inner:
            x: int = ConfigField(default=1)

        @dataclasses.dataclass
        class Outer:
            # Inner.x 期望 int，但这里传入 str "bad"——这正是本测试要覆盖的运行时类型不匹配，
            # 所以用 type: ignore 抑制静态检查。
            inner: Inner = ConfigField(default_factory=lambda: Inner(x="bad"))  # type: ignore[arg-type]

        loader = ConfigLoader(Outer)
        with pytest.raises(ConfigValidationError):
            loader.load()

    def test_nested_constraint_violation(self):
        @dataclasses.dataclass
        class Inner:
            value: int = ConfigField(default=-1, constraints=[Range(0, 100)])

        @dataclasses.dataclass
        class Outer:
            inner: Inner = ConfigField(default_factory=Inner)

        loader = ConfigLoader(Outer)
        with pytest.raises(ConfigValidationError) as exc_info:
            loader.load()
        assert any(isinstance(e, ConfigConstraintError) for e in exc_info.value.errors)


# ── ConfigValidationError ──────────────────────────────────────────────


class TestConfigValidationError:
    """ConfigValidationError 字符串格式化 — 多错误汇总。"""

    def test_str_representation(self):
        err = ConfigValidationError([
            ConfigMissingError("MyConfig", "field_a"),
            ConfigMissingError("MyConfig", "field_b"),
        ])
        s = str(err)
        assert "2 config error(s)" in s
        assert "field_a" in s
        assert "field_b" in s
