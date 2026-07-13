"""PromptSection ABC, StaticSection, Jinja2Section 单元测试。"""
import tempfile
from pathlib import Path

import pytest
from Module.system_prompt.protocol import PromptSection, StaticSection, Jinja2Section

pytestmark = pytest.mark.unit


class TestPromptSectionABC:
    def test_cannot_instantiate_directly(self):
        """ABC 不能直接实例化。"""
        with pytest.raises(TypeError):
            PromptSection()  # type: ignore[abstract]

    def test_default_methods(self):
        """默认方法返回合理值。"""

        class TestSection(PromptSection):
            name = "test"
            version = "1.0"
            description = "desc"

            async def render(self) -> str:
                return "rendered"

        s = TestSection()
        assert s.name == "test"
        assert s.version == "1.0"
        assert s.dependencies() == []
        assert s.validate() is True
        s.set_context(key="value")  # 不报错
        s.reload()  # 不报错


class TestStaticSection:
    def test_render_returns_text(self):
        s = StaticSection("test", "1.0", "desc", "Hello world")
        assert s.name == "test"
        assert s.version == "1.0"
        assert s.description == "desc"
        result = s.render_sync()
        assert result == "Hello world"

    def test_async_render(self):
        import asyncio
        s = StaticSection("test", "1.0", "desc", "async text")
        result = asyncio.run(s.render())
        assert result == "async text"


class TestJinja2Section:
    def test_render_with_variables(self):
        """Jinja2Section 渲染带变量的模板。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "test.j2"
            tpl.write_text("Hello {{ name }}!")

            class MySection(Jinja2Section):
                name = "my"
                version = "1.0"
                description = "test"

                async def render(self) -> str:
                    return self._render_template(name="World")

            s = MySection(str(tpl))
            import asyncio
            result = asyncio.run(s.render())
            assert result == "Hello World!"

    def test_validate_parses_template(self):
        """validate 检查模板可解析。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "good.j2"
            tpl.write_text("{{ x }}")
            s = Jinja2Section(str(tpl))
            assert s.validate() is True

    def test_validate_fails_on_bad_template(self):
        """validate 对坏模板返回 False。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "bad.j2"
            tpl.write_text("{{ {% broken")
            s = Jinja2Section(str(tpl))
            assert s.validate() is False

    def test_validate_file_not_found(self):
        """模板文件不存在时 validate 返回 False（不抛异常）。"""
        s = Jinja2Section("/nonexistent/template.j2")
        assert s.validate() is False

    def test_reload_rereads_template(self):
        """reload 后渲染内容更新。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "reload.j2"
            tpl.write_text("v1")
            s = Jinja2Section(str(tpl))
            assert s.render_sync() == "v1"
            tpl.write_text("v2")
            s.reload()
            assert s.render_sync() == "v2"
