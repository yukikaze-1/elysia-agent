"""SectionParser 单元测试。"""

import tempfile
from pathlib import Path

import pytest
from core.prompt.parser import SectionParser
from core.prompt.section import PromptSection, SectionParseError


class TestParse:
    def test_parse_valid_section(self):
        """解析带有完整头部标记的 section 文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "identity.j2"
            filepath.write_text(
                "{#\n"
                "@section identity\n"
                "@version 1.2.0\n"
                "@description 角色身份、性格描述\n"
                "@params name, user_name\n"
                "#}\n"
                "你是 {{ name }}。\n"
                "关于 {{ user_name }}：你们很熟了。\n"
            )
            parser = SectionParser()
            section = parser.parse(str(filepath))
            assert section.name == "identity"
            assert section.version == "1.2.0"
            assert section.description == "角色身份、性格描述"
            assert section.params == ("name", "user_name")
            assert "你是 {{ name }}" in section.template
            assert "关于 {{ user_name }}" in section.template
            # 头部标记不应出现在 template 中
            assert "@section" not in section.template
            assert "@version" not in section.template

    def test_parse_section_with_no_params(self):
        """@params 可以为空。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "output.j2"
            filepath.write_text(
                "{#\n"
                "@section output_format\n"
                "@version 1.0.0\n"
                "@description 输出格式协议\n"
                "@params\n"
                "#}\n"
                "每次回复都必须包含 <inner_voice>\n"
            )
            parser = SectionParser()
            section = parser.parse(str(filepath))
            assert section.params == ()

    def test_parse_minimal_section(self):
        """只有必需字段的最简 section。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "minimal.j2"
            filepath.write_text(
                "{#\n"
                "@section minimal\n"
                "@version 0.1.0\n"
                "@description 最简测试\n"
                "@params\n"
                "#}\n"
                "content here\n"
            )
            parser = SectionParser()
            section = parser.parse(str(filepath))
            assert section.name == "minimal"
            assert section.version == "0.1.0"
            # 不再 rstrip("\n")，保留模板体尾随换行用于节间间距控制
            assert section.template == "content here\n"

    def test_parse_params_with_spaces(self):
        """@params 值逗号分隔，两侧空格被 strip。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "spaces.j2"
            filepath.write_text(
                "{#\n"
                "@section test\n"
                "@version 1.0.0\n"
                "@description test\n"
                "@params  name ,  user_name , mood \n"
                "#}\n"
                "body\n"
            )
            parser = SectionParser()
            section = parser.parse(str(filepath))
            assert section.params == ("name", "user_name", "mood")

    def test_parse_source_file_tracks_origin(self):
        """source_file 字段记录原始文件路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "identity.j2"
            filepath.write_text(
                "{#\n"
                "@section identity\n"
                "@version 1.0.0\n"
                "@description test\n"
                "@params\n"
                "#}\n"
                "body\n"
            )
            parser = SectionParser()
            section = parser.parse(str(filepath))
            assert "identity.j2" in section.source_file


class TestParseErrors:
    def test_missing_section_tag(self):
        """缺少 @section 标记。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "bad.j2"
            filepath.write_text(
                "{#\n"
                "@version 1.0.0\n"
                "@description test\n"
                "@params\n"
                "#}\nbody\n"
            )
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="section"):
                parser.parse(str(filepath))

    def test_missing_version_tag(self):
        """缺少 @version 标记。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "bad.j2"
            filepath.write_text(
                "{#\n"
                "@section test\n"
                "@description test\n"
                "@params\n"
                "#}\nbody\n"
            )
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="version"):
                parser.parse(str(filepath))

    def test_missing_description_tag(self):
        """缺少 @description 标记。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "bad.j2"
            filepath.write_text(
                "{#\n"
                "@section test\n"
                "@version 1.0.0\n"
                "@params\n"
                "#}\nbody\n"
            )
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="description"):
                parser.parse(str(filepath))

    def test_missing_params_tag(self):
        """缺少 @params 标记。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "bad.j2"
            filepath.write_text(
                "{#\n"
                "@section test\n"
                "@version 1.0.0\n"
                "@description test\n"
                "#}\nbody\n"
            )
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="params"):
                parser.parse(str(filepath))

    def test_no_header_block(self):
        """文件完全没有头部标记块。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "bad.j2"
            filepath.write_text("just body, no header")
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="section"):
                parser.parse(str(filepath))

    def test_empty_file(self):
        """空文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "empty.j2"
            filepath.write_text("")
            parser = SectionParser()
            with pytest.raises(SectionParseError, match="section"):
                parser.parse(str(filepath))


class TestScanAll:
    def test_scan_all_discovers_all_j2_files(self):
        """扫描目录下所有 .j2 文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sections_dir = Path(tmpdir) / "sections"
            sections_dir.mkdir()
            for name in ["identity", "voice", "output_format"]:
                (sections_dir / f"{name}.j2").write_text(
                    "{#\n"
                    f"@section {name}\n"
                    "@version 1.0.0\n"
                    f"@description {name} section\n"
                    "@params\n"
                    "#}\n"
                    f"{name} body\n"
                )
            parser = SectionParser()
            result = parser.scan_all(str(sections_dir))
            assert set(result.keys()) == {"identity", "voice", "output_format"}
            # 每个 section 的默认变体用空字符串 key
            for name in ["identity", "voice", "output_format"]:
                assert "" in result[name]
                assert result[name][""].name == name

    def test_scan_all_handles_variants(self):
        """扫描识别 @ 后缀的变体文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sections_dir = Path(tmpdir) / "sections"
            sections_dir.mkdir()
            (sections_dir / "identity.j2").write_text(
                "{#\n@section identity\n@version 1.0.0\n@description base\n@params\n#}\nbase body\n"
            )
            (sections_dir / "identity@soft.j2").write_text(
                "{#\n@section identity\n@version 1.1.0\n@description soft variant\n@params\n#}\nsoft body\n"
            )
            parser = SectionParser()
            result = parser.scan_all(str(sections_dir))
            assert "identity" in result
            assert "" in result["identity"]          # default variant
            assert "soft" in result["identity"]      # variant
            assert result["identity"][""].template == "base body\n"
            assert result["identity"]["soft"].template == "soft body\n"

    def test_scan_all_skips_non_j2_files(self):
        """忽略非 .j2 文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sections_dir = Path(tmpdir) / "sections"
            sections_dir.mkdir()
            (sections_dir / "identity.j2").write_text(
                "{#\n@section identity\n@version 1.0.0\n@description test\n@params\n#}\nbody\n"
            )
            (sections_dir / "README.md").write_text("docs")
            (sections_dir / "notes.txt").write_text("notes")
            parser = SectionParser()
            result = parser.scan_all(str(sections_dir))
            assert set(result.keys()) == {"identity"}

    def test_scan_all_empty_directory(self):
        """空目录返回空 dict。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            sections_dir = Path(tmpdir) / "sections"
            sections_dir.mkdir()
            parser = SectionParser()
            result = parser.scan_all(str(sections_dir))
            assert result == {}
