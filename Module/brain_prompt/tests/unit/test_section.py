"""PromptSection 数据模型单元测试。"""

import pytest
from Module.brain_prompt import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)


class TestPromptSection:
    def test_create_minimal_section(self):
        section = PromptSection(
            name="test",
            version="1.0.0",
            description="A test section",
            params=(),
            template="Hello",
            source_file="sections/test.j2",
        )
        assert section.name == "test"
        assert section.version == "1.0.0"
        assert section.params == ()
        assert section.template == "Hello"

    def test_create_with_params(self):
        section = PromptSection(
            name="greeting",
            version="1.0.0",
            description="Greets user",
            params=("name", "mood"),
            template="Hi {{ name }}, you seem {{ mood }}",
            source_file="sections/greeting.j2",
        )
        assert section.params == ("name", "mood")

    def test_is_frozen(self):
        section = PromptSection(
            name="test", version="1.0", description="d",
            params=(), template="t", source_file="f.j2",
        )
        with pytest.raises(Exception):  # FrozenInstanceError or similar
            section.name = "other"  # type: ignore[misc]


class TestSectionParseError:
    def test_error_message(self):
        err = SectionParseError("sections/bad.j2", "section")
        assert "sections/bad.j2" in str(err)
        assert "section" in str(err)

    def test_is_value_error(self):
        err = SectionParseError("f.j2", "version")
        assert isinstance(err, ValueError)


class TestMissingContextError:
    def test_error_message(self):
        err = MissingContextError(section="identity", param="name")
        assert "identity" in str(err)
        assert "name" in str(err)

    def test_is_key_error(self):
        err = MissingContextError(section="s", param="p")
        assert isinstance(err, KeyError)


class TestSectionRenderError:
    def test_error_message(self):
        cause = RuntimeError("Jinja2 barfed")
        err = SectionRenderError(section="voice", cause=cause)
        assert "voice" in str(err)
        assert "Jinja2 barfed" in str(err)

    def test_cause_accessible(self):
        cause = ValueError("original")
        err = SectionRenderError(section="s", cause=cause)
        assert err.cause is cause


class TestProfileValidationError:
    def test_error_message(self):
        err = ProfileValidationError(profile="default", detail="section 'xxx' not found")
        assert "default" in str(err)
        assert "xxx" in str(err)
