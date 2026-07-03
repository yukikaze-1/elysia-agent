"""BrainPromptRegistry 单元测试。"""

import tempfile
from pathlib import Path

import pytest
import yaml
from Module.brain_prompt import BrainPromptRegistry
from Module.brain_prompt import MissingContextError, ProfileValidationError


def _make_sections_dir(base: Path) -> Path:
    """在 base 下创建 sections/ 目录，写入 3 个 section 文件。"""
    sections = base / "sections"
    sections.mkdir()
    (sections / "identity.j2").write_text(
        "{#\n"
        "@section identity\n"
        "@version 1.0.0\n"
        "@description 角色身份\n"
        "@params name, user_name\n"
        "#}\n"
        "你是 {{ name }}。关于 {{ user_name }}：你们很熟了。\n"
    )
    (sections / "sensory.j2").write_text(
        "{#\n"
        "@section sensory\n"
        "@version 1.0.0\n"
        "@description 感官输入\n"
        "@params sensory_ctx\n"
        "#}\n"
        "<sensory>\n"
        "当前时间: {{ sensory_ctx.current_time }}\n"
        "</sensory>\n"
    )
    (sections / "output.j2").write_text(
        "{#\n"
        "@section output_format\n"
        "@version 1.0.0\n"
        "@description 输出格式\n"
        "@params\n"
        "#}\n"
        "<inner_voice>必选</inner_voice>\n"
    )
    return sections


def _make_profiles_dir(base: Path) -> None:
    """在 base 下创建 profiles/ 目录，写入 default + minimal。"""
    profiles = base / "profiles"
    profiles.mkdir()
    (profiles / "default.yaml").write_text(
        "name: default\n"
        "description: 完整\n"
        "enabled:\n"
        "  - identity\n"
        "  - sensory\n"
        "  - output_format\n"
    )
    (profiles / "minimal.yaml").write_text(
        "name: minimal\n"
        "description: 极简\n"
        "enabled:\n"
        "  - sensory\n"
        "  - output_format\n"
    )


def _make_metadata(base: Path) -> None:
    """写入 metadata.yaml。"""
    (base / "metadata.yaml").write_text(yaml.dump({
        "version": "1.0.0",
        "updated": "2026-07-03",
        "order": ["identity", "sensory", "output_format"],
        "dependencies": {"identity": [], "sensory": [], "output_format": []},
    }))


class TestInit:
    def test_init_loads_all_sections_and_profiles(self):
        """构造时加载所有 section、profile、metadata。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            versions = registry.get_versions()
            assert versions["identity"] == "1.0.0"
            assert versions["sensory"] == "1.0.0"
            assert versions["output_format"] == "1.0.0"

    def test_init_missing_metadata_raises(self):
        """metadata.yaml 缺失时启动即报错。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            with pytest.raises(FileNotFoundError, match="metadata"):
                BrainPromptRegistry(tmpdir)

    def test_init_profile_refs_unknown_section_raises(self):
        """profile 引用了不存在的 section → ProfileValidationError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            profiles = Path(tmpdir) / "profiles"
            profiles.mkdir()
            (profiles / "bad.yaml").write_text(
                "name: bad\n"
                "description: bad\n"
                "enabled:\n"
                "  - nonexistent\n"
            )
            _make_metadata(Path(tmpdir))
            with pytest.raises(ProfileValidationError, match="nonexistent"):
                BrainPromptRegistry(tmpdir)


class TestRender:
    def test_render_default_profile(self):
        """默认 profile 渲染所有 section。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            result = registry.render(
                profile="default",
                context={
                    "name": "Elysia",
                    "user_name": "妖梦",
                    "sensory_ctx": {"current_time": "2024-01-01 12:00:00"},
                },
            )
            assert "Elysia" in result
            assert "妖梦" in result
            assert "2024-01-01 12:00:00" in result
            assert "<inner_voice>必选</inner_voice>" in result

    def test_render_minimal_profile_skips_identity(self):
        """minimal profile 跳过 identity section。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            result = registry.render(
                profile="minimal",
                context={
                    "name": "Elysia",
                    "user_name": "妖梦",
                    "sensory_ctx": {"current_time": "now"},
                },
            )
            assert "Elysia" not in result  # identity skipped
            assert "now" in result
            assert "<inner_voice>" in result

    def test_render_missing_context_param_raises(self):
        """context 缺少 section 声明的参数 → MissingContextError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            with pytest.raises(MissingContextError, match="name"):
                registry.render(profile="default", context={})

    def test_render_sections_joined_by_double_newline(self):
        """Section 之间用 \\n\\n 拼接。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sections = Path(tmpdir) / "sections"
            sections.mkdir()
            (sections / "a.j2").write_text(
                "{#\n@section a\n@version 1.0.0\n@description a\n@params\n#}\nAAA\n"
            )
            (sections / "b.j2").write_text(
                "{#\n@section b\n@version 1.0.0\n@description b\n@params\n#}\nBBB\n"
            )
            profiles = Path(tmpdir) / "profiles"
            profiles.mkdir()
            (profiles / "default.yaml").write_text(
                "name: default\n"
                "description: test\n"
                "enabled:\n"
                "  - a\n"
                "  - b\n"
            )
            (Path(tmpdir) / "metadata.yaml").write_text(yaml.dump({
                "version": "1.0.0", "updated": "",
                "order": ["a", "b"],
                "dependencies": {},
            }))
            registry = BrainPromptRegistry(tmpdir)
            result = registry.render(profile="default", context={})
            assert result == "AAABBB"


class TestGetProfileForSession:
    def test_deterministic_same_session(self):
        """相同 session_id 返回相同 profile。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            p1 = registry.get_profile_for_session("session-123")
            p2 = registry.get_profile_for_session("session-123")
            assert p1 == p2

    def test_no_experiments_returns_default(self):
        """无 AB 实验时返回 'default'。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            result = registry.get_profile_for_session("any-session")
            assert result == "default"


class TestReload:
    def test_reload_picks_up_new_section(self):
        """reload() 后能发现新增的 section 文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_sections_dir(Path(tmpdir))
            _make_profiles_dir(Path(tmpdir))
            _make_metadata(Path(tmpdir))
            registry = BrainPromptRegistry(tmpdir)
            assert "new_section" not in registry.get_versions()

            # 新增 section 文件 + 更新 profile + metadata order
            (Path(tmpdir) / "sections" / "new_section.j2").write_text(
                "{#\n@section new_section\n@version 1.0.0\n@description new\n@params\n#}\nNEW\n"
            )
            # 更新 profile 启用新 section
            (Path(tmpdir) / "profiles" / "default.yaml").write_text(
                "name: default\n"
                "description: 完整\n"
                "enabled:\n"
                "  - identity\n"
                "  - sensory\n"
                "  - output_format\n"
                "  - new_section\n"
            )
            # 更新 metadata order
            (Path(tmpdir) / "metadata.yaml").write_text(yaml.dump({
                "version": "1.0.0", "updated": "",
                "order": ["identity", "sensory", "output_format", "new_section"],
                "dependencies": {},
            }))

            registry.reload()
            assert "new_section" in registry.get_versions()
            result = registry.render(
                profile="default",
                context={
                    "name": "E", "user_name": "u",
                    "sensory_ctx": {"current_time": "t"},
                },
            )
            assert "NEW" in result
