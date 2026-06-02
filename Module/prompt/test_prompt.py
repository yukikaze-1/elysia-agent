"""Module/prompt/manager.py 单元测试。"""

import tempfile
from pathlib import Path

import pytest

from Module.prompt import PromptManager


class TestConstructor:
    def test_valid_template_dir(self):
        """构造时模板目录存在，不报错。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(tmpdir)
            assert pm.template_dir == tmpdir

    def test_missing_template_dir(self):
        """模板目录不存在时抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="Template directory not found"):
            PromptManager("/nonexistent/path/xyz")

    def test_auto_reload_default_false(self):
        """默认不开启 auto_reload。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(tmpdir)
            assert pm.env.auto_reload is False

    def test_auto_reload_enabled(self):
        """传入 auto_reload=True 时生效。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(tmpdir, auto_reload=True)
            assert pm.env.auto_reload is True


class TestRender:
    def test_render_simple_template(self):
        """渲染完整模板文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "hello.j2").write_text("Hello, {{ name }}!")
            pm = PromptManager(tmpdir)
            result = pm.render("hello.j2", name="World")
            assert result == "Hello, World!"

    def test_render_missing_template(self):
        """模板文件不存在时抛出 FileNotFoundError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(tmpdir)
            with pytest.raises(FileNotFoundError, match="Template 'missing.j2' not found"):
                pm.render("missing.j2")

    def test_render_with_multiline_template(self):
        """多行模板正确渲染。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "multi.j2").write_text("line1\nline2\n{{ var }}\nline4")
            pm = PromptManager(tmpdir)
            result = pm.render("multi.j2", var="val")
            assert result == "line1\nline2\nval\nline4"


class TestRenderMacro:
    def test_render_macro_basic(self):
        """渲染模板中的命名 macro。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "macros.j2").write_text(
                "{% macro greet(name) %}Hi, {{ name }}!{% endmacro %}"
            )
            pm = PromptManager(tmpdir)
            result = pm.render_macro("macros.j2", "greet", name="Elysia")
            assert result == "Hi, Elysia!"

    def test_render_macro_missing_macro(self):
        """macro 不存在时抛出 ValueError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "macros.j2").write_text(
                "{% macro greet(name) %}Hi, {{ name }}!{% endmacro %}"
            )
            pm = PromptManager(tmpdir)
            with pytest.raises(ValueError, match="Macro 'nonexist' not found"):
                pm.render_macro("macros.j2", "nonexist")

    def test_render_macro_missing_template(self):
        """模板文件不存在时抛出 FileNotFoundError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(tmpdir)
            with pytest.raises(FileNotFoundError, match="Template 'missing.j2' not found"):
                pm.render_macro("missing.j2", "any_macro")

    def test_render_macro_multiple_macros(self):
        """文件中多个 macro，只渲染指定的那个。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "macros.j2").write_text(
                "{% macro a() %}A{% endmacro %}"
                "{% macro b() %}B{% endmacro %}"
            )
            pm = PromptManager(tmpdir)
            assert pm.render_macro("macros.j2", "a") == "A"
            assert pm.render_macro("macros.j2", "b") == "B"


class TestDatetimeFilter:
    def test_datetime_filter(self):
        """datetime filter 正确格式化时间戳。"""
        from datetime import datetime as dt

        ts = 1715845200.0
        expected = dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "time.j2").write_text("{{ ts | datetime }}")
            pm = PromptManager(tmpdir)
            result = pm.render("time.j2", ts=ts)
            assert result == expected

    def test_datetime_filter_none(self):
        """None 值返回空字符串。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "time.j2").write_text("[{{ ts | datetime }}]")
            pm = PromptManager(tmpdir)
            result = pm.render("time.j2", ts=None)
            assert result == "[]"


_PROMPT_DIR = str(Path(__file__).resolve().parent.parent.parent / "prompt")


class TestIntegrationWithRealTemplates:
    """使用项目真实模板进行烟雾测试。"""

    def test_render_brain_system_prompt(self):
        """Brain.j2 → BrainSystemPrompt 能正常渲染。"""
        PROMPT_DIR = _PROMPT_DIR

        pm = PromptManager(str(PROMPT_DIR))
        result = pm.render_macro(
            "Brain.j2", "BrainSystemPrompt",
            l3_personality_block="[persona]",
            sensory={
                "current_time": "2024-01-01 12:00:00",
                "time_of_day": "afternoon",
                "day_of_week": "Monday",
                "season": "winter",
                "latency": 0.5,
                "perception": "user looks happy",
            },
            memory={"micro": [], "macro": []},
            state={"mood": "neutral"},
        )
        assert "[persona]" in result
        assert "2024-01-01 12:00:00" in result
        assert "neutral" in result

    def test_render_amygdala_prompts(self):
        """Amygdala.j2 的 system + user prompt 能正常渲染。"""
        PROMPT_DIR = _PROMPT_DIR

        pm = PromptManager(str(PROMPT_DIR))
        system = pm.render_macro(
            "Amygdala.j2", "AmygdalaSystemPrompt",
            character_name="Elysia",
            l3_persona_block="[persona]",
        )
        assert "Elysia" in system

        user = pm.render_macro(
            "Amygdala.j2", "AmygdalaUserPrompt",
            current_time="2024-01-01 12:00:00",
            time_of_day="afternoon",
            day_of_week="Monday",
            season="winter",
            user_reaction_latency=1200,
            user_message="Hello",
        )
        assert "Hello" in user
        assert "1200" in user

    def test_render_active_speak_prompt(self):
        """ActiveSpeak.j2 能正常渲染。"""
        PROMPT_DIR = _PROMPT_DIR

        pm = PromptManager(str(PROMPT_DIR))
        result = pm.render_macro(
            "ActiveSpeak.j2", "L1DecideToActiveSpeakSystemPrompt",
            user_name="妖梦",
            last_speaker="user",
            silence_duration="30s",
            current_mood="happy",
            current_psyche_state="energy: 80%",
            current_time_envs={"time_of_day": "morning"},
            recent_conversations=[],
        )
        assert "妖梦" in result
        assert "30s" in result
        assert "happy" in result

    def test_render_reflector_prompts(self):
        """MicroReflector / MacroReflector 的 prompt 能正常渲染。"""
        PROMPT_DIR = _PROMPT_DIR

        pm = PromptManager(str(PROMPT_DIR))

        micro_sys = pm.render_macro(
            "MicroReflector.j2", "MicroReflectorSystemPrompt",
            character_name="Elysia", user_name="妖梦",
        )
        assert "Elysia" in micro_sys

        macro_sys = pm.render_macro(
            "MacroReflector.j2", "MacroReflectorSystemPrompt",
            character_name="Elysia",
        )
        assert "Elysia" in macro_sys


# ============================================================
# datetime filter custom formats
# ============================================================

class TestDatetimeFilterExtended:
    @pytest.fixture
    def pm(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        tpl_dir.mkdir()
        return PromptManager(str(tpl_dir))

    def test_custom_format_date_only(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("{{ 1717334400.0 | datetime('%Y-%m-%d') }}")
        result = pm.render("t.j2")
        assert result == "2024-06-02"

    def test_custom_format_time_only(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("{{ 1717334400.0 | datetime('%H:%M:%S') }}")
        result = pm.render("t.j2")
        assert ":" in result

    def test_none_value_returns_empty(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("{{ None | datetime }}")
        result = pm.render("t.j2")
        assert result == ""


# ============================================================
# Missing variable / None context
# ============================================================

class TestRenderErrors:
    @pytest.fixture
    def pm(self, tmp_path):
        tpl_dir = tmp_path / "templates"
        tpl_dir.mkdir()
        return PromptManager(str(tpl_dir))

    def test_missing_variable_renders_empty(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("Hello {{ name }}!")
        result = pm.render("t.j2")
        assert "Hello" in result

    def test_none_value_renders_as_string(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("{{ name }}")
        result = pm.render("t.j2", name=None)
        assert result == "None"

    def test_empty_template(self, pm):
        from pathlib import Path
        tpl_path = Path(pm.template_dir) / "t.j2"
        tpl_path.write_text("")
        result = pm.render("t.j2")
        assert result == ""
