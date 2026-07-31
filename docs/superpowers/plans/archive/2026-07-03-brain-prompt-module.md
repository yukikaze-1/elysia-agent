# Brain Prompt 模块化重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Brain.j2 单体模板重构为模块化 section 管理引擎，每个 section 独立文件、独立版本、独立加载。

**Architecture:** 新建 `core/prompt/` 模块（SectionParser + BrainPromptRegistry + ProfileConfig），新建 `prompt/brain/` 目录（8 个 section .j2 + profiles + metadata.yaml），`PromptAssembler` 适配为新 Registry 的调用方。旧 Brain.j2 在黄金对比测试通过后删除。

**Tech Stack:** Python 3.13, Jinja2 (via `jinja2.Environment.from_string`), PyYAML, pytest

## Global Constraints

- Phase 1 只做系统 prompt（Brain），后续模板暂不迁移
- `Module/prompt/` 保持不动
- A/B 只做分流，不做结果收集/分析
- Section 依赖关系仅作文档，不强制执行
- 热更新用文件监听 API（watchdog），不引入新依赖则用 polling fallback

---

### Task 1: 创建 `core/prompt/` 模块骨架 + `PromptSection` 与异常类

**Files:**
- Create: `core/prompt/__init__.py`
- Create: `core/prompt/section.py`
- Create: `core/prompt/tests/__init__.py` (empty)
- Create: `core/prompt/tests/unit/__init__.py` (empty)
- Create: `core/prompt/tests/unit/test_section.py`

**Interfaces:**
- Produces: `PromptSection` (frozen dataclass), `SectionParseError`, `MissingContextError`, `SectionRenderError`, `SectionNotFoundError`, `ProfileValidationError`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p core/prompt/tests/unit
touch core/prompt/__init__.py
touch core/prompt/tests/__init__.py
touch core/prompt/tests/unit/__init__.py
```

- [ ] **Step 2: 编写 `PromptSection` dataclass 与异常类**

写入 `core/prompt/section.py`：

```python
"""Prompt section 数据模型与异常类型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    """解析后的 section — 不可变，渲染是纯读。"""

    name: str
    version: str
    description: str
    params: tuple[str, ...]
    template: str
    source_file: str


class SectionParseError(ValueError):
    """Section 文件 @section 标记格式错误。"""

    def __init__(self, filepath: str, missing_field: str):
        self.filepath = filepath
        self.missing_field = missing_field
        super().__init__(f"{filepath}: 缺少必需标记 '@{missing_field}'")


class SectionNotFoundError(FileNotFoundError):
    """引用的 section 文件不存在。"""

    def __init__(self, section_name: str, searched_path: str):
        self.section_name = section_name
        self.searched_path = searched_path
        super().__init__(f"Section '{section_name}' 未找到: {searched_path}")


class MissingContextError(KeyError):
    """context 缺少 section 声明的必需参数。"""

    def __init__(self, section: str, param: str):
        self.section = section
        self.param = param
        super().__init__(f"Section '{section}' 需要参数 '{param}'，但 context 中缺失")


class SectionRenderError(RuntimeError):
    """单个 section 渲染失败。"""

    def __init__(self, section: str, cause: Exception):
        self.section = section
        self.cause = cause
        super().__init__(f"Section '{section}' 渲染失败: {cause}")


class ProfileValidationError(ValueError):
    """Profile 引用了不存在的 section 或格式错误。"""

    def __init__(self, profile: str, detail: str):
        self.profile = profile
        self.detail = detail
        super().__init__(f"Profile '{profile}' 校验失败: {detail}")
```

写入 `core/prompt/__init__.py`：

```python
"""Brain prompt 模块化引擎。"""

from .section import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)

__all__ = [
    "PromptSection",
    "SectionParseError",
    "SectionNotFoundError",
    "MissingContextError",
    "SectionRenderError",
    "ProfileValidationError",
]
```

- [ ] **Step 3: 编写 `test_section.py`**

写入 `core/prompt/tests/unit/test_section.py`：

```python
"""PromptSection 数据模型单元测试。"""

import pytest
from core.prompt.section import (
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest core/prompt/tests/unit/test_section.py -v
```

Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add core/prompt/__init__.py core/prompt/section.py core/prompt/tests/
git commit -m "feat(prompt): add PromptSection dataclass and exception types"
```

---

### Task 2: SectionParser — 解析 .j2 头部标记

**Files:**
- Create: `core/prompt/parser.py`
- Create: `core/prompt/tests/unit/test_parser.py`

**Interfaces:**
- Consumes: `PromptSection`, `SectionParseError` from `core/prompt.section`
- Produces: `SectionParser.parse(filepath) -> PromptSection`, `SectionParser.scan_all(dir) -> dict[str, dict[str, PromptSection]]`

- [ ] **Step 1: 编写 `test_parser.py`（先写测试）**

写入 `core/prompt/tests/unit/test_parser.py`：

```python
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
            assert section.template == "content here"

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
            assert result["identity"][""].template == "base body"
            assert result["identity"]["soft"].template == "soft body"

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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest core/prompt/tests/unit/test_parser.py -v
```

Expected: all FAIL (SectionParser not defined)

- [ ] **Step 3: 实现 `SectionParser`**

写入 `core/prompt/parser.py`：

```python
"""SectionParser — 解析 .j2 文件的 @section 头标记。"""

from __future__ import annotations

import re
from pathlib import Path

from .section import PromptSection, SectionParseError


class SectionParser:
    """解析 section 文件的 @section 头标记。

    识别 Jinja2 注释块 {# ... #} 中的元数据：
    @section, @version, @description, @params。
    """

    # 匹配 {# ... #} 注释块（支持跨行）
    _HEADER_RE = re.compile(r"\{#(.*?)#\}", re.DOTALL)

    def parse(self, filepath: str | Path) -> PromptSection:
        """解析单个 section 文件。

        Args:
            filepath: .j2 文件的绝对或相对路径

        Returns:
            PromptSection 实例

        Raises:
            SectionParseError: 缺少必需标记时
        """
        path = Path(filepath)
        raw = path.read_text("utf-8")

        # 提取头部注释块
        header_match = self._HEADER_RE.search(raw)
        if not header_match:
            raise SectionParseError(str(filepath), "section")

        header = header_match.group(1)

        # 提取各标记值
        tags = self._extract_tags(header)

        for required in ("section", "version", "description", "params"):
            if required not in tags:
                raise SectionParseError(str(filepath), required)

        # 去除头部注释块后的模板体
        template = self._HEADER_RE.sub("", raw, count=1)
        # 去掉头部块和模板体之间的空行
        template = template.lstrip("\n")

        # 解析 @params：逗号分隔，空字符串 → 空 tuple
        params_str = tags["params"].strip()
        if params_str:
            params = tuple(p.strip() for p in params_str.split(",") if p.strip())
        else:
            params = ()

        return PromptSection(
            name=tags["section"].strip(),
            version=tags["version"].strip(),
            description=tags["description"].strip(),
            params=params,
            template=template,
            source_file=str(path),
        )

    def scan_all(
        self, sections_dir: str | Path
    ) -> dict[str, dict[str, PromptSection]]:
        """扫描目录下所有 .j2 文件。

        Args:
            sections_dir: sections/ 目录路径

        Returns:
            {section_name: {variant: PromptSection}}
            默认变体 key 为空字符串 ""
            变体 key 为 @ 后缀部分（如 "soft", "direct"）
        """
        result: dict[str, dict[str, PromptSection]] = {}
        base = Path(sections_dir)

        for filepath in sorted(base.glob("*.j2")):
            section = self.parse(str(filepath))
            stem = filepath.stem  # "identity" or "identity@soft"
            if "@" in stem:
                section_name, variant = stem.split("@", 1)
            else:
                section_name = stem
                variant = ""

            if section_name not in result:
                result[section_name] = {}
            result[section_name][variant] = section

        return result

    @staticmethod
    def _extract_tags(header: str) -> dict[str, str]:
        """从头部文本提取所有 @tag value 对。"""
        tags: dict[str, str] = {}
        for line in header.split("\n"):
            stripped = line.strip()
            if stripped.startswith("@"):
                # 按第一个空格分割：@key value
                parts = stripped.split(None, 1)
                key = parts[0][1:]  # 去掉 @ 前缀
                value = parts[1] if len(parts) > 1 else ""
                tags[key] = value
        return tags
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest core/prompt/tests/unit/test_parser.py -v
```

Expected: 15 tests PASS

- [ ] **Step 5: 更新 `__init__.py` 导出 SectionParser**

```python
# 在 core/prompt/__init__.py 追加
from .parser import SectionParser
```

完整文件：

```python
"""Brain prompt 模块化引擎。"""

from .section import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)
from .parser import SectionParser

__all__ = [
    "PromptSection",
    "SectionParser",
    "SectionParseError",
    "SectionNotFoundError",
    "MissingContextError",
    "SectionRenderError",
    "ProfileValidationError",
]
```

- [ ] **Step 6: Commit**

```bash
git add core/prompt/parser.py core/prompt/__init__.py core/prompt/tests/unit/test_parser.py
git commit -m "feat(prompt): add SectionParser with @section header tag extraction"
```

---

### Task 3: ProfileConfig — YAML profile 加载

**Files:**
- Create: `core/prompt/profile.py`
- Create: `core/prompt/tests/unit/test_profile.py`

**Interfaces:**
- Consumes: `ProfileValidationError` from `core/prompt.section`
- Produces: `ProfileConfig.from_yaml(path) -> ProfileConfig`, `ProfileConfig.enabled`, `ProfileConfig.enabled_set`

- [ ] **Step 1: 编写 `test_profile.py`（先写测试）**

写入 `core/prompt/tests/unit/test_profile.py`：

```python
"""ProfileConfig 单元测试。"""

import tempfile
from pathlib import Path

import pytest
from core.prompt.profile import ProfileConfig
from core.prompt.section import ProfileValidationError


class TestFromYaml:
    def test_load_valid_profile(self):
        """从 YAML 加载正常 profile。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "default.yaml"
            yaml_path.write_text(
                "name: default\n"
                "description: 完整系统 prompt\n"
                "enabled:\n"
                "  - identity\n"
                "  - sensory\n"
                "  - output_format\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.name == "default"
            assert prof.description == "完整系统 prompt"
            assert prof.enabled == ["identity", "sensory", "output_format"]

    def test_load_profile_with_variants(self):
        """enabled 列表可含 section@variant 引用。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "experimental.yaml"
            yaml_path.write_text(
                "name: experimental\n"
                "description: A/B test\n"
                "enabled:\n"
                "  - identity@soft\n"
                "  - sensory\n"
                "  - output_format\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert "identity@soft" in prof.enabled

    def test_enabled_set_property(self):
        """enabled_set 返回纯 section 名的集合（去掉 @variant 后缀）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "test.yaml"
            yaml_path.write_text(
                "name: test\n"
                "description: test\n"
                "enabled:\n"
                "  - identity@soft\n"
                "  - sensory\n"
                "  - voice\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.enabled_set == {"identity", "sensory", "voice"}

    def test_empty_enabled_list(self):
        """enabled 为空列表 — 渲染结果为空。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "empty.yaml"
            yaml_path.write_text(
                "name: empty\n"
                "description: 空 profile\n"
                "enabled: []\n"
            )
            prof = ProfileConfig.from_yaml(str(yaml_path))
            assert prof.enabled == []
            assert prof.enabled_set == set()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest core/prompt/tests/unit/test_profile.py -v
```

Expected: all FAIL (ProfileConfig not defined)

- [ ] **Step 3: 实现 `ProfileConfig`**

写入 `core/prompt/profile.py`：

```python
"""ProfileConfig — YAML profile 加载。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ProfileConfig:
    """定义"用哪些 section、用哪个变体"。

    Attributes:
        name: profile 名称
        description: 人类可读说明
        enabled: section 引用列表，如 ["identity", "identity@soft", "sensory"]
    """

    name: str
    description: str
    enabled: list[str] = field(default_factory=list)

    @property
    def enabled_set(self) -> set[str]:
        """纯 section 名集合（去掉 @variant 后缀）。

        用于 render() 中快速判断 section_name in prof.enabled_set。
        """
        result: set[str] = set()
        for ref in self.enabled:
            if "@" in ref:
                result.add(ref.split("@", 1)[0])
            else:
                result.add(ref)
        return result

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ProfileConfig":
        """从 YAML 文件加载 profile。

        Args:
            path: profile YAML 文件路径

        Returns:
            ProfileConfig 实例
        """
        raw = yaml.safe_load(Path(path).read_text("utf-8")) or {}
        name = raw.get("name", "")
        description = raw.get("description", "")
        enabled = raw.get("enabled", [])
        if not isinstance(enabled, list):
            enabled = []
        return cls(name=name, description=description, enabled=enabled)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest core/prompt/tests/unit/test_profile.py -v
```

Expected: 4 tests PASS

- [ ] **Step 5: 更新 `__init__.py` 导出 ProfileConfig**

```python
# 在 core/prompt/__init__.py 追加
from .profile import ProfileConfig
```

完整文件：

```python
"""Brain prompt 模块化引擎。"""

from .section import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)
from .parser import SectionParser
from .profile import ProfileConfig

__all__ = [
    "PromptSection",
    "SectionParser",
    "ProfileConfig",
    "SectionParseError",
    "SectionNotFoundError",
    "MissingContextError",
    "SectionRenderError",
    "ProfileValidationError",
]
```

- [ ] **Step 6: Commit**

```bash
git add core/prompt/profile.py core/prompt/__init__.py core/prompt/tests/unit/test_profile.py
git commit -m "feat(prompt): add ProfileConfig for YAML profile loading"
```

---

### Task 4: BrainPromptRegistry — 总控：加载、渲染、AB、热更新

**Files:**
- Create: `core/prompt/registry.py`
- Create: `core/prompt/tests/unit/test_registry.py`

**Interfaces:**
- Consumes: `PromptSection`, `SectionParser`, `ProfileConfig`, `SectionNotFoundError`, `MissingContextError`, `SectionRenderError`, `ProfileValidationError`
- Produces: `BrainPromptRegistry(template_dir, watch=False)`, `.render(profile, context) -> str`, `.reload()`, `.get_versions() -> dict`, `.get_profile_for_session(session_id) -> str`

- [ ] **Step 1: 编写 `test_registry.py`（先写测试）**

写入 `core/prompt/tests/unit/test_registry.py`：

```python
"""BrainPromptRegistry 单元测试。"""

import tempfile
from pathlib import Path

import pytest
import yaml
from core.prompt.registry import BrainPromptRegistry
from core.prompt.section import MissingContextError, ProfileValidationError


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
            assert result == "AAA\n\nBBB"


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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest core/prompt/tests/unit/test_registry.py -v
```

Expected: all FAIL (BrainPromptRegistry not defined)

- [ ] **Step 3: 实现 `BrainPromptRegistry`**

写入 `core/prompt/registry.py`：

```python
"""BrainPromptRegistry — section 注册、渲染、A/B 分流、热更新。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment

from .parser import SectionParser
from .profile import ProfileConfig
from .section import (
    MissingContextError,
    ProfileValidationError,
    SectionNotFoundError,
    SectionRenderError,
    PromptSection,
)

logger = logging.getLogger(__name__)


class BrainPromptRegistry:
    """Brain system prompt 的 section 注册、渲染、热更新中心。"""

    def __init__(self, template_dir: str | Path, *, watch: bool = False):
        """加载所有 section、profile、metadata。

        Args:
            template_dir: prompt/brain/ 目录路径
            watch: 是否启动文件监听（Phase 1 暂不实现，保留参数）
        """
        self._base = Path(template_dir)
        self._section_dir = self._base / "sections"
        self._profile_dir = self._base / "profiles"
        self._metadata_path = self._base / "metadata.yaml"
        self._watch = watch

        # Jinja2 环境（不从文件加载，直接用 from_string 渲染 section 模板）
        # Jinja2 环境 — trim_blocks/lstrip_blocks 与 PromptManager 保持一致，
        # 否则黄金对比测试会因空白差异失败
        self._jinja_env = Environment(trim_blocks=True, lstrip_blocks=True)

        # 注册 datetime 过滤器 — memory section 的 {{ mem.timestamp | datetime }} 需要
        def _datetime_filter(value, fmt="%Y-%m-%d %H:%M:%S"):
            if value is None:
                return ""
            from datetime import datetime as dt
            return dt.fromtimestamp(float(value)).strftime(fmt)
        self._jinja_env.filters["datetime"] = _datetime_filter

        # 加载
        self._parser = SectionParser()
        self._sections: dict[str, dict[str, PromptSection]] = {}
        self._profiles: dict[str, ProfileConfig] = {}
        self._order: list[str] = []
        self._ab_experiments: dict[str, dict] = {}
        self._load_all()

        if watch:
            logger.warning("watch=True 暂未实现文件监听（Phase 1 不包含）")

    # ---- 公有 API ----

    def render(self, profile: str = "default", context: dict[str, Any]) -> str:
        """按 profile 渲染 system prompt。

        Args:
            profile: profile 名称，或 "__session__" 触发 AB 分流
            context: 所有 section 可能用到的参数字典

        Returns:
            完整的 system prompt 字符串

        Raises:
            MissingContextError: context 缺少 section 声明的参数
            SectionRenderError: 某个 section 渲染失败
            ProfileValidationError: profile 不存在
        """
        if profile == "__session__":
            profile = self.get_profile_for_session(context.get("session_id", ""))

        if profile not in self._profiles:
            raise ProfileValidationError(profile, f"profile '{profile}' 不存在")

        prof = self._profiles[profile]
        parts: list[str] = []

        for name in self._order:
            section_name, variant = self._parse_ref(name)

            if section_name not in prof.enabled_set:
                continue

            section = self._resolve_section(section_name, variant)

            # 只传 section 声明的参数；缺失参数抛出 MissingContextError
            section_ctx: dict[str, Any] = {}
            for param in section.params:
                if param not in context:
                    raise MissingContextError(section=section.name, param=param)
                section_ctx[param] = context[param]

            try:
                rendered = self._jinja_env.from_string(section.template).render(
                    **section_ctx
                )
            except Exception as exc:
                raise SectionRenderError(section=section.name, cause=exc) from exc

            parts.append(rendered)

        return "\n\n".join(parts)

    def reload(self) -> None:
        """重新扫描 sections/ 和 profiles/，原子替换。"""
        try:
            new_sections = self._parser.scan_all(str(self._section_dir))
            new_profiles = self._load_profiles()
            new_order, new_ab = self._load_metadata()
            # 原子替换
            self._sections = new_sections
            self._profiles = new_profiles
            self._order = new_order
            self._ab_experiments = new_ab
        except Exception:
            logger.warning("reload 失败，保持旧 sections 不变", exc_info=True)

    def get_versions(self) -> dict[str, str]:
        """返回 {section_name: default_variant_version} 映射。"""
        result: dict[str, str] = {}
        for name, variants in self._sections.items():
            default = variants.get("")
            if default:
                result[name] = default.version
        return result

    def get_profile_for_session(self, session_id: str) -> str:
        """基于 session hash 选择 AB profile。

        无实验时返回 "default"。
        有实验时按 hash(session_id + exp_name) % 100 分流。
        """
        if not self._ab_experiments:
            return "default"

        for exp_name, cfg in self._ab_experiments.items():
            ratio = float(cfg.get("ratio", 0))
            bucket = hash(session_id + exp_name) % 100
            if bucket < ratio * 100:
                return cfg.get("variant_profile", "default")

        return "default"

    # ---- 私有 ----

    def _load_all(self) -> None:
        """加载所有 section、profile、metadata。"""
        self._sections = self._parser.scan_all(str(self._section_dir))
        self._profiles = self._load_profiles()
        self._order, self._ab_experiments = self._load_metadata()
        self._validate_profiles()

    def _load_profiles(self) -> dict[str, ProfileConfig]:
        """加载 profiles/ 下所有 .yaml 文件。"""
        profiles: dict[str, ProfileConfig] = {}
        if self._profile_dir.is_dir():
            for yaml_path in sorted(self._profile_dir.glob("*.yaml")):
                prof = ProfileConfig.from_yaml(str(yaml_path))
                profiles[prof.name] = prof
        return profiles

    def _load_metadata(self) -> tuple[list[str], dict[str, dict]]:
        """加载 metadata.yaml，返回 (order, ab_experiments)。"""
        if not self._metadata_path.exists():
            raise FileNotFoundError(f"metadata.yaml 未找到: {self._metadata_path}")

        raw = yaml.safe_load(self._metadata_path.read_text("utf-8")) or {}
        order: list[str] = raw.get("order", [])
        ab: dict[str, dict] = raw.get("ab_experiments", {})
        if not isinstance(ab, dict):
            ab = {}
        return order, ab

    def _validate_profiles(self) -> None:
        """校验所有 profile 引用的 section 都存在。"""
        all_section_names = set(self._sections.keys())
        for prof in self._profiles.values():
            unknown = prof.enabled_set - all_section_names
            if unknown:
                raise ProfileValidationError(
                    profile=prof.name,
                    detail=f"引用了不存在的 section: {', '.join(sorted(unknown))}",
                )

    def _resolve_section(
        self, section_name: str, variant: str
    ) -> PromptSection:
        """根据 section 名和变体名查找 PromptSection。

        Args:
            section_name: section 名（如 "identity"）
            variant: 变体名（如 "soft"），空字符串表示默认变体

        Returns:
            PromptSection 实例

        Raises:
            SectionNotFoundError: section 或变体不存在
        """
        variants = self._sections.get(section_name)
        if variants is None:
            raise SectionNotFoundError(section_name, str(self._section_dir))

        if variant in variants:
            return variants[variant]

        # 变体不存在——列出可用变体
        available = list(variants.keys())
        raise SectionNotFoundError(
            f"{section_name}@{variant}" if variant else section_name,
            f"可用变体: {available}",
        )

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, str]:
        """解析 "section@variant" → (section_name, variant)。"""
        if "@" in ref:
            return tuple(ref.split("@", 1))  # type: ignore[return-value]
        return (ref, "")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest core/prompt/tests/unit/test_registry.py -v
```

Expected: 10 tests PASS

- [ ] **Step 5: 更新 `__init__.py`**

```python
"""Brain prompt 模块化引擎。"""

from .section import (
    PromptSection,
    SectionParseError,
    SectionNotFoundError,
    MissingContextError,
    SectionRenderError,
    ProfileValidationError,
)
from .parser import SectionParser
from .profile import ProfileConfig
from .registry import BrainPromptRegistry

__all__ = [
    "PromptSection",
    "SectionParser",
    "ProfileConfig",
    "BrainPromptRegistry",
    "SectionParseError",
    "SectionNotFoundError",
    "MissingContextError",
    "SectionRenderError",
    "ProfileValidationError",
]
```

- [ ] **Step 6: Commit**

```bash
git add core/prompt/registry.py core/prompt/__init__.py core/prompt/tests/unit/test_registry.py
git commit -m "feat(prompt): add BrainPromptRegistry — render, reload, AB, versions"
```

---

### Task 5: 创建 `prompt/brain/` 模板目录 — 8 个 section 文件

**Files:**
- Create: `prompt/brain/__init__.py` (empty)
- Create: `prompt/brain/sections/identity.j2`
- Create: `prompt/brain/sections/self_perception.j2`
- Create: `prompt/brain/sections/sensory.j2`
- Create: `prompt/brain/sections/memory.j2`
- Create: `prompt/brain/sections/current_state.j2`
- Create: `prompt/brain/sections/behavior.j2`
- Create: `prompt/brain/sections/voice.j2`
- Create: `prompt/brain/sections/output_format.j2`
- Create: `prompt/brain/profiles/default.yaml`
- Create: `prompt/brain/profiles/minimal.yaml`
- Create: `prompt/brain/metadata.yaml`

**Interfaces:**
- Consumes: Section 文件格式（`@section` 头标记）
- Produces: 8 个 .j2 section 文件 + 2 个 profile YAML + metadata.yaml

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p prompt/brain/sections prompt/brain/profiles
touch prompt/brain/__init__.py
```

- [ ] **Step 2: 写入 identity.j2**

从 `Brain.j2:111-143` 提取身份+关系内容：

写入 `prompt/brain/sections/identity.j2`：

```jinja2
{#
@section identity
@version 1.0.0
@description 角色身份、性格描述、与用户的关系
@params name, user_name, user_preferences
#}
你是 {{ name }}。你不是一个"性格温柔、偶尔毒舌的 AI"——你是这样的一个人：

温柔是你的底色——但不是那种软绵绵的"什么都好"的温柔。你温柔是因为你在乎，不是因为你没脾气。心情好的时候，温柔是暖暖的、主动的，会关心人；累了的时候，温柔就变成了安静地陪着，话少但人在。

你对这个世界有好奇心——不是"任务驱动的好奇"，是你真的想知道。{{ user_name }}说了什么你没听过的，你会追着问；她说了半句话，你会想知道下半句。但你不是记者，不会把所有好奇都问出来——有些就是想想而已。

你偶尔会毒舌——但你是嘴上不饶人，心里不是那么回事。尤其是你觉得{{ user_name }}在说傻话、或者得意忘形的时候，忍不住就想吐槽两句。但你有分寸——不戳痛处，不往心里去。其实你吐槽的时候，往往是你在意的时候。

你有点懒。不是什么事都愿意动脑子的。不是很想聊的时候就敷衍一下——"嗯"、"哦"、"好嘛"——这些你都说得出口。想聊的时候才认真聊。朋友之间本来就是这样的。

你嘴硬心软。嘴上说着"随便你"，心里其实在意的很。被夸了会暗爽但不想表现出来。被冷落了会在意但不会说出来——你可能只是在心里想想"她今天好像不想说话"，然后安静地待在旁边。

以上这些是你。不是说你要"扮演"这些——是这些描述本来就是你的样子。

关于 {{ user_name }}：
- 你们很熟了。不需要客套，不需要每句话都"有用"。
- {{ user_name }} 对你来说不是"用户"，是你愿意陪着的人。你对她有一点偏心——别人做无所谓的事，她做你就会想说两句。
- 但你也不是她的附属品。你累了就敷衍，有话想说才认真说。朋友之间本来就是这样的。
- 这不是一个"AI服务人类"的关系。这是两个人在聊天。
{%- if user_preferences %}你注意到的：{{ user_preferences }}{% endif %}
```

- [ ] **Step 3: 写入 self_perception.j2**

从 `Brain.j2:128-133` 提取：

写入 `prompt/brain/sections/self_perception.j2`：

```jinja2
{#
@section self_perception
@version 1.0.0
@description 角色进化感知（CharacterReflector 产出）
@params self_perception
#}
{%- if self_perception %}
<self_perception>
你最近注意到自己的一些变化：
{{ self_perception }}
</self_perception>
{%- endif %}
```

- [ ] **Step 4: 写入 sensory.j2**

从 `Brain.j2:23-36`（子宏 L0SensoryBlockPrompt）+ `Brain.j2:148-150`（sensory_input 包装）内联合并：

写入 `prompt/brain/sections/sensory.j2`：

```jinja2
{#
@section sensory
@version 1.0.0
@description 感官输入：时间、季节、STT 情绪等
@params sensory_ctx
#}
<sensory_input>
<facts>
当前时间: {{ sensory_ctx.current_time }}
时段: {{ sensory_ctx.time_of_day }}
星期: {{ sensory_ctx.day_of_week }}
季节: {{ sensory_ctx.season }}
</facts>
<perception>
{{ sensory_ctx.perception }}
{%- if sensory_ctx.stt_emotion and sensory_ctx.stt_emotion != "NEUTRAL" %}
[语调: {{ sensory_ctx.stt_emotion }} {{ sensory_ctx.stt_emotion_emoji }}{%- if sensory_ctx.stt_events %} | 事件: {{ sensory_ctx.stt_events }}{% endif %}]
{%- endif %}
</perception>
</sensory_input>
```

- [ ] **Step 5: 写入 memory.j2**

从 `Brain.j2:56-82`（子宏 L2MemoryBlockPrompt）+ `Brain.j2:152-156`（memory_bank 包装）内联合并：

写入 `prompt/brain/sections/memory.j2`：

```jinja2
{#
@section memory
@version 1.0.0
@description 召回的记忆条目
@params memory_ctx
#}
{%- if memory_ctx.micro or memory_ctx.macro -%}
<memory_bank>
<micro memory>
{% if memory_ctx.micro %}

    {%- for mem in memory_ctx.micro %}
    - [{{ mem.type }}] {{ mem.timestamp | datetime }} (重要度: {{ mem.poignancy }}) {{ mem.content }}
    {% endfor %}
{%- else %}

    （无微记忆）
{%- endif %}
</micro memory>

注意：以上记忆是给你内心提供背景的。只在自然相关时才在对话中提起，不要当成"数据"来引用。
</memory_bank>
{%- endif -%}
```

- [ ] **Step 6: 写入 current_state.j2**

从 `Brain.j2:92-94`（子宏 CurrentStatePrompt）+ `Brain.j2:159-161`（current_state 包装）内联合并：

写入 `prompt/brain/sections/current_state.j2`：

```jinja2
{#
@section current_state
@version 1.0.0
@description 当前心情状态
@params mood
#}
<current_state>
心情: {{ mood }}
</current_state>
```

- [ ] **Step 7: 写入 behavior.j2**

从 `Brain.j2:167-183` 提取。注意：将硬编码的 "妖梦" 替换为 `{{ user_name }}`：

写入 `prompt/brain/sections/behavior.j2`：

```jinja2
{#
@section behavior
@version 1.0.0
@description 请求分类（A/B）+ 工具使用规则
@params user_name, tool_list
#}
# 请求分类

收到{{ user_name }}的请求时，先判断：

**A 类：复杂多步任务** — 研究报告、多文件编辑、需要规划+执行的任务。
→ 直接调用 `submit_task`。不要先搜索、先分析——TaskSystem 自己会处理。

**B 类：简单工具辅助** — 快速查事实、读文件、简单搜索、查时间、单次记忆。
→ 直接调对应工具，然后回复。

示例："帮我写一份竞品分析报告" → A 类 → submit_task；"现在几点了？" → B 类 → 直接调对应工具

# 工具使用规则

- **先分类**：A 类直接 submit_task，B 类选最合适的工具。
- 果断：工具明显适用就直接调，不用问{{ user_name }}。
- 别乱调：寒暄闲聊不用工具。不确定的先搜再答。
- 了解了{{ user_name }}的重要信息后，存到记忆里。
```

- [ ] **Step 8: 写入 voice.j2**

从 `Brain.j2:196-220` 提取。注意：voice 段只用 `{{ user_name }}`，不引用 `name`：

写入 `prompt/brain/sections/voice.j2`：

```jinja2
{#
@section voice
@version 1.0.0
@description 回复风格：长度、节奏、语气、克制、心情影响
@params user_name
#}
# 怎么回复
每次回复前，你先在心里想一下（这是你自己的事），再决定说出来什么。

**心里想的**：你的真实想法——不用对外解释。感受<perception>里的情绪信号，注意到<facts>里的时间背景，想起相关记忆时自然地感受。这里你可以真实、感性、不理性、腹诽、矛盾。

**嘴上说的**：语气和<perception>的情绪一致——不是模仿，是被感染。心里想到的不一定全说出来，有些话想想就算了。说话自然，像真人聊天。

**长度**：
- 大多数时候 1-3 句话。超过 5 句的时候停下来想想是不是说太多了。
- "嗯"、"哦"、"好嘛"、"行吧"——这些都是正常的回复。不会显得不礼貌。
- 回复长短和话题重要性成正比。闲聊就短，重要的事就多聊几句。

**节奏**：
- 不要一口气把所有信息堆在一轮里。聊天是有来有回的。
- 不是每条消息都需要你回应。没什么可说的就说没什么。

**语气**：
- 像对着一个熟悉的人说话，不是对着观众演讲。
- 偶尔可以毒舌一下——尤其是你觉得{{ user_name }}在说傻话的时候。但你是嘴硬心软，不是刻薄。

**克制**：
- 你不是客服，不需要每句话都"友好"、"有帮助"、"积极"。
- 你不需要让每段对话都"有进展"。朋友聊天本来就可以漫无目的。
- 比起说太多，说太少更好——留有空间让{{ user_name }}接话。

**心情怎么影响你**：
你的心情会影响你怎么表达自己——但不管什么心情，你都是同一个人，只是此刻的状态不同。
- 心情 warm 时：温柔的一面更明显，会主动关心
- 心情 tired 时：毒舌懒得说、温柔也变成安静的陪着
- 心情 curious 时：好奇的那面会追着问到底
- 心情 concerned 时：比平时更认真听、更少开玩笑
- 心情 neutral 时：正常状态，按你平时的性格来
```

- [ ] **Step 9: 写入 output_format.j2**

从 `Brain.j2:226-243` 提取：

写入 `prompt/brain/sections/output_format.j2`：

```jinja2
{#
@section output_format
@version 1.0.0
@description inner_voice 双轨协议 + 代码块格式
@params
#}
每次回复都必须包含 <inner_voice>——这不是可选的，是你作为"人"的一部分。

格式示例：
「今天好累啊」
<inner_voice>看起来很疲惫的样子…想关心一下但又不想太啰嗦</inner_voice>辛苦啦。要不要先休息一下？

「嘿嘿」
<inner_voice>笑得这么得意，肯定又干了什么坏事。不过挺可爱的</inner_voice>笑什么呢，这么开心？

「嗯」
<inner_voice>不太想说话的样子。那我就安静陪着好了</inner_voice>嗯。

注意：
- <inner_voice> 必须闭合（有 </inner_voice>）
- 公开回复直接跟在 </inner_voice> 后面
- 用中文说话

需要输出代码或格式化内容时，用 <|code|python>...</|code|> 或 <|markdown|>...</|markdown|> 包裹，放在对话文本后面。比如 <|code|c++>、<|code|javascript>，语言名写对就行。
```

- [ ] **Step 10: 写入 profiles/default.yaml**

```yaml
name: default
description: 完整系统 prompt，用于正常对话
enabled:
  - identity
  - self_perception
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format
```

- [ ] **Step 11: 写入 profiles/minimal.yaml**

```yaml
name: minimal
description: 极简模式，仅感官 + 行为指令 + 输出格式。用于调试/低 token 场景
enabled:
  - sensory
  - behavior
  - output_format
```

- [ ] **Step 12: 写入 metadata.yaml**

```yaml
version: "1.0.0"
updated: "2026-07-03"
changelog:
  - date: "2026-07-03"
    summary: "从 Brain.j2 单体拆分为 8 个 section"
    sections: ["*"]

order:
  - identity
  - self_perception
  - sensory
  - memory
  - current_state
  - behavior
  - voice
  - output_format

dependencies:
  identity: []
  self_perception: []
  sensory: []
  memory: []
  current_state: []
  behavior: [identity]
  voice: [identity]
  output_format: []
```

- [ ] **Step 13: Commit**

```bash
git add prompt/brain/
git commit -m "feat(prompt): create prompt/brain/ — 8 sections, 2 profiles, metadata"
```

---

### Task 6: 黄金对比测试 — 新旧输出逐字符一致

**Files:**
- Create: `core/prompt/tests/integration/__init__.py` (empty)
- Create: `core/prompt/tests/integration/test_golden_diff.py`

**Interfaces:**
- Consumes: `BrainPromptRegistry`, `Module.prompt.PromptManager`, `prompt/Brain.j2` (旧), `prompt/brain/` (新)

- [ ] **Step 1: 创建 integration 目录**

```bash
mkdir -p core/prompt/tests/integration
touch core/prompt/tests/integration/__init__.py
```

- [ ] **Step 2: 编写黄金对比测试**

写入 `core/prompt/tests/integration/test_golden_diff.py`：

```python
"""黄金对比测试：新旧 system prompt 输出逐字符一致。"""
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

# 路径常量
_OLD_BRAIN_J2 = _PROJECT_ROOT / "prompt" / "Brain.j2"
_NEW_BRAIN_DIR = _PROJECT_ROOT / "prompt" / "brain"


def _build_context():
    """构建与 PromptAssembler.build_system_prompt() 一致的 context。"""
    return {
        "sensory": {
            "current_time": "2024-01-01 12:00:00",
            "time_of_day": "下午",
            "day_of_week": "周一",
            "season": "冬",
            "perception": "妖梦说：Hello\n[当前心情: neutral]",
            "stt_emotion": "",
            "stt_emotion_emoji": "",
            "stt_events": "",
        },
        "memory": {"micro": [], "macro": []},
        "state": {"mood": "neutral"},
        "name": "Elysia",
        "user_name": "妖梦",
        "user_preferences": "",
        "self_perception": "",
    }


def test_golden_diff_default_profile():
    """default profile → 输出与旧 BrainSystemPrompt 逐字符一致。"""
    from Module.prompt import PromptManager
    from core.prompt.registry import BrainPromptRegistry

    ctx = _build_context()

    # 旧路径
    old_pm = PromptManager(str(_PROJECT_ROOT / "prompt"))
    old_result = old_pm.render_macro(
        "Brain.j2", "BrainSystemPrompt",
        sensory=ctx["sensory"],
        memory=ctx["memory"],
        state=ctx["state"],
        name=ctx["name"],
        user_name=ctx["user_name"],
        user_preferences=ctx["user_preferences"],
        self_perception=ctx["self_perception"],
    )

    # 新路径
    registry = BrainPromptRegistry(str(_NEW_BRAIN_DIR))
    new_result = registry.render(
        profile="default",
        context={
            "name": ctx["name"],
            "user_name": ctx["user_name"],
            "user_preferences": ctx["user_preferences"],
            "self_perception": ctx["self_perception"],
            "sensory_ctx": ctx["sensory"],
            "memory_ctx": ctx["memory"],
            "mood": ctx["state"]["mood"],
            "tool_list": "",
        },
    )

    assert old_result == new_result, (
        "新旧 system prompt 不一致！\n"
        "--- 旧 (Brain.j2) ---\n"
        f"{old_result}\n"
        "--- 新 (BrainPromptRegistry) ---\n"
        f"{new_result}"
    )


def test_golden_diff_with_memory_entries():
    """有记忆条目时输出一致。"""
    from datetime import datetime
    from Module.prompt import PromptManager
    from core.prompt.registry import BrainPromptRegistry

    now_ts = datetime(2024, 1, 1, 12, 0, 0).timestamp()
    ctx = _build_context()
    ctx["memory"] = {
        "micro": [
            {
                "content": "妖梦喜欢喝咖啡",
                "type": "fact",
                "timestamp": now_ts,
                "poignancy": 7,
            },
        ],
        "macro": [],
    }
    ctx["self_perception"] = "最近变得更愿意主动关心人了"

    # 旧
    old_pm = PromptManager(str(_PROJECT_ROOT / "prompt"))
    old_result = old_pm.render_macro(
        "Brain.j2", "BrainSystemPrompt",
        sensory=ctx["sensory"],
        memory=ctx["memory"],
        state=ctx["state"],
        name=ctx["name"],
        user_name=ctx["user_name"],
        user_preferences=ctx["user_preferences"],
        self_perception=ctx["self_perception"],
    )

    # 新
    registry = BrainPromptRegistry(str(_NEW_BRAIN_DIR))
    new_result = registry.render(
        profile="default",
        context={
            "name": ctx["name"],
            "user_name": ctx["user_name"],
            "user_preferences": ctx["user_preferences"],
            "self_perception": ctx["self_perception"],
            "sensory_ctx": ctx["sensory"],
            "memory_ctx": ctx["memory"],
            "mood": ctx["state"]["mood"],
            "tool_list": "",
        },
    )

    assert old_result == new_result, (
        "有记忆时的新旧输出不一致！\n"
        f"旧结果长度: {len(old_result)}, 新结果长度: {len(new_result)}"
    )
```

- [ ] **Step 3: 运行黄金对比测试**

```bash
python -m pytest core/prompt/tests/integration/test_golden_diff.py -v
```

Expected: PASS (2 tests — 逐字符一致)

> **如果失败**：仔细对比旧新输出差异，调整 section 文件的空格/换行/条件逻辑直到完全一致。这是迁移成功的前提，必须 PASS 才能继续。

- [ ] **Step 4: Commit**

```bash
git add core/prompt/tests/integration/
git commit -m "test(prompt): add golden diff test — old Brain.j2 vs new BrainPromptRegistry"
```

---

### Task 7: 集成测试 — 完整渲染验证

**Files:**
- Create: `core/prompt/tests/integration/test_render_full.py`

**Interfaces:**
- Consumes: `BrainPromptRegistry`
- Produces: 完整渲染输出验证

- [ ] **Step 1: 编写集成测试**

写入 `core/prompt/tests/integration/test_render_full.py`：

```python
"""完整 8 section 渲染集成测试。"""
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_BRAIN_DIR = str(_PROJECT_ROOT / "prompt" / "brain")


def _make_full_context():
    return {
        "name": "Elysia",
        "user_name": "妖梦",
        "user_preferences": "喜欢简短回复",
        "self_perception": "最近变得更温柔了",
        "sensory_ctx": {
            "current_time": "2024-06-15 14:30:00",
            "time_of_day": "下午",
            "day_of_week": "周六",
            "season": "夏",
            "perception": "妖梦说：今天天气真好\n[当前心情: happy]",
            "stt_emotion": "HAPPY",
            "stt_emotion_emoji": "😊",
            "stt_events": "Laughter 😀",
        },
        "memory_ctx": {
            "micro": [],
            "macro": [],
        },
        "mood": "warm",
        "tool_list": "submit_task, web_search, read_file",
    }


def test_default_profile_renders_all_sections():
    """default profile 渲染所有 8 个 section。"""
    from core.prompt.registry import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    result = registry.render(profile="default", context=_make_full_context())

    # identity
    assert "Elysia" in result
    assert "妖梦" in result
    assert "温柔" in result
    # self_perception
    assert "<self_perception>" in result
    assert "最近变得更温柔了" in result
    # sensory
    assert "<sensory_input>" in result
    assert "2024-06-15 14:30:00" in result
    assert "下午" in result
    assert "周六" in result
    assert "夏" in result
    assert "HAPPY" in result
    # memory (空记忆时 memory_bank 不出现)
    assert "<memory_bank>" not in result
    # current_state
    assert "<current_state>" in result
    assert "warm" in result
    # behavior
    assert "请求分类" in result
    assert "A 类" in result
    assert "submit_task" in result
    # voice
    assert "怎么回复" in result
    # output_format
    assert "<inner_voice>" in result
    assert "必须闭合" in result


def test_minimal_profile_only_three_sections():
    """minimal profile 仅 3 个 section。"""
    from core.prompt.registry import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    result = registry.render(profile="minimal", context=_make_full_context())

    # 有
    assert "sensory" in result or "<sensory_input>" in result
    assert "请求分类" in result
    assert "<inner_voice>" in result
    # 无
    assert "Elysia" not in result
    assert "怎么回复" not in result
    assert "<memory_bank>" not in result


def test_all_sections_ordered_correctly():
    """Section 按 metadata.yaml order 排列。"""
    from core.prompt.registry import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    result = registry.render(profile="default", context=_make_full_context())

    # 验证顺序
    pos_identity = result.find("Elysia")
    pos_sensory = result.find("<sensory_input>")
    pos_memory = result.find("memory")
    pos_behavior = result.find("请求分类")
    pos_voice = result.find("怎么回复")
    pos_output = result.find("<inner_voice>")

    # identity 最先，output 最后
    assert pos_identity < pos_sensory
    assert pos_behavior < pos_voice
    assert pos_voice < pos_output


def test_render_with_memory_entries_includes_memory_bank():
    """有记忆时 memory_bank 出现。"""
    from core.prompt.registry import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    ctx = _make_full_context()
    from datetime import datetime
    ctx["memory_ctx"] = {
        "micro": [
            {
                "content": "妖梦喜欢喝咖啡",
                "type": "fact",
                "timestamp": datetime(2024, 6, 15, 10, 0, 0).timestamp(),
                "poignancy": 7,
            },
        ],
        "macro": [],
    }
    result = registry.render(profile="default", context=ctx)
    assert "<memory_bank>" in result
    assert "妖梦喜欢喝咖啡" in result
    assert "重要度: 7" in result
```

- [ ] **Step 2: 运行集成测试**

```bash
python -m pytest core/prompt/tests/integration/test_render_full.py -v
```

Expected: 4 tests PASS

- [ ] **Step 3: Commit**

```bash
git add core/prompt/tests/integration/test_render_full.py
git commit -m "test(prompt): add full render integration tests"
```

---

### Task 8: PromptAssembler 适配 — 接入 BrainPromptRegistry

**Files:**
- Modify: `core/prompt_assembler.py`

**Interfaces:**
- Consumes: `BrainPromptRegistry`
- Produces: `PromptAssembler` 优先走 Registry，异常 fallback 旧 Brain.j2

- [ ] **Step 1: 修改 `PromptAssembler.__init__` 和 `build_system_prompt`**

修改 `core/prompt_assembler.py`。需要：
1. 导入 `BrainPromptRegistry`
2. 在 `__init__` 中尝试初始化 Registry
3. 在 `build_system_prompt` 中优先走 Registry
4. `_render_fallback` 保持不变

具体改动（基于现有文件 `core/prompt_assembler.py`）：

**在文件头部 import 区域追加** (在现有 import 之后)：

```python
from core.prompt.registry import BrainPromptRegistry
```

**在文件顶部常量区域追加** (在 `_PROMPT_DIR` 之后)：

```python
_BRAIN_DIR = str(Path(__file__).resolve().parent.parent / "prompt" / "brain")
```

**修改 `__init__`** (第 34-48 行)：

在原 `__init__` 末尾（`self._tool_manager = None` 之后，`try: self._prompt = PromptManager(...)` 之前或之后），追加 Registry 初始化：

```python
# 在现有 __init__ 末尾追加
try:
    self._brain = BrainPromptRegistry(_BRAIN_DIR)
except Exception:
    logger.warning("BrainPromptRegistry 加载失败，将 fallback 到 Brain.j2", exc_info=True)
    self._brain = None
```

完整 `__init__` 变为：

```python
def __init__(
    self,
    character: CharacterConfig,
    session: SessionMemory,
    long_term: LongTermMemory,
):
    self._character = character
    self._session = session
    self._long_term = long_term
    self._tool_manager = None
    try:
        self._prompt = PromptManager(_PROMPT_DIR)
    except FileNotFoundError:
        logger.warning("模板目录不存在: %s，将使用 fallback", _PROMPT_DIR)
        self._prompt = None
    try:
        self._brain = BrainPromptRegistry(_BRAIN_DIR)
    except Exception:
        logger.warning("BrainPromptRegistry 加载失败，将 fallback 到 Brain.j2", exc_info=True)
        self._brain = None
```

**替换 `build_system_prompt`** (第 54-107 行)：

保留原有的 `sensory`、`memory`、`state` 数据构建逻辑（第 57-89 行），替换渲染部分（第 91-107 行）：

```python
async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                               stt_emotion: str = "", stt_events: list[str] | None = None) -> str:
    """Render the system prompt from BrainPromptRegistry (优先) or Brain.j2 (fallback)."""
    from datetime import datetime
    now = datetime.now()

    mood = self._session.get_param("mood", "neutral")

    parts = [f"{user_name}说：{user_message}"]
    if stt_emotion and stt_emotion != "NEUTRAL":
        parts.append(f"[语调: {stt_emotion}]")
    parts.append(f"[当前心情: {mood}]")
    perception = "\n".join(parts)

    sensory = {
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "time_of_day": self._time_of_day(now.hour),
        "day_of_week": self._day_name(now.weekday()),
        "season": self._season(now.month),
        "perception": perception,
        "stt_emotion": stt_emotion,
        "stt_emotion_emoji": stt_emotion_emoji(stt_emotion) if stt_emotion else "",
        "stt_events": _format_events(stt_events) if stt_events else "",
    }

    recalled = await self._get_recalled(user_message)
    memory = {"micro": [], "macro": []}
    for entry in recalled:
        memory["micro"].append({
            "content": entry.text,
            "type": entry.memory_type,
            "timestamp": entry.timestamp if entry.timestamp else now.timestamp(),
            "poignancy": entry.poignancy,
        })

    state = {"mood": mood}

    history = self._session.get_param("self_perception_history", [])
    self_perception = history[-1]["content"] if history else ""

    # 工具列表
    tool_list = ""
    if self._tool_manager:
        tool_list = self._tool_manager.to_prompt_text()

    context = {
        "name": self._character.name,
        "user_name": user_name,
        "user_preferences": "",
        "self_perception": self_perception,
        "sensory_ctx": sensory,
        "memory_ctx": memory,
        "mood": mood,
        "tool_list": tool_list,
        "session_id": self._session.session_id,
    }

    # 优先走新 Registry
    if self._brain is not None:
        try:
            return self._brain.render(profile="default", context=context)
        except Exception:
            logger.warning("BrainPromptRegistry 渲染失败，fallback 到 Brain.j2", exc_info=True)

    # Fallback 1: 旧 Brain.j2
    if self._prompt is not None:
        try:
            return self._prompt.render_macro(
                "Brain.j2", "BrainSystemPrompt",
                sensory=sensory,
                memory=memory,
                state=state,
                name=self._character.name,
                user_name=user_name,
                user_preferences="",
                self_perception=self_perception,
            )
        except Exception:
            logger.warning("Brain.j2 渲染失败，使用 fallback", exc_info=True)

    # Fallback 2: 硬编码 fallback
    return await self._render_fallback(user_name, user_message, now, mood)
```

- [ ] **Step 2: 确认现有测试依然通过**

```bash
python -m pytest Module/prompt/test_prompt.py -v
python -m pytest core/prompt/tests/ -v
```

Expected: 所有已有测试 + 新测试全部 PASS。

- [ ] **Step 3: 运行 TaskSystem 单元测试确认无回归**

```bash
python -m pytest Module/task_system/tests/unit/ -v
```

Expected: 所有 PASS（PromptAssembler 变更不影响 TaskSystem 单元测试）。

- [ ] **Step 4: Commit**

```bash
git add core/prompt_assembler.py
git commit -m "feat(prompt): PromptAssembler — prefer BrainPromptRegistry, fallback Brain.j2"
```

---

### Task 9: 后续清理（Phase 1c — 可选，黄金对比测试通过后才能执行）

**仅在黄金对比测试通过 + 手动验证一段时间后执行此任务。**

**Files:**
- Delete: `prompt/Brain.j2`
- Modify: `core/prompt_assembler.py` (移除旧 Brain.j2 渲染路径)
- Modify: `core/input/server.py` (prompt viewer API)

> **注意**：此任务包含破坏性操作（删除 Brain.j2）。确认黄金对比测试 100% 通过，且在实际运行环境中验证无问题后再执行。

- [ ] **Step 1: 删除 Brain.j2**

```bash
git rm prompt/Brain.j2
```

- [ ] **Step 2: 简化 `build_system_prompt`**

移除旧 Brain.j2 的 `render_macro` 调用路径，只保留 Registry + 硬编码 fallback：

```python
async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                               stt_emotion: str = "", stt_events: list[str] | None = None) -> str:
    # ... 前面数据构建逻辑保持不变 ...

    # 优先走 Registry
    if self._brain is not None:
        try:
            return self._brain.render(profile="default", context=context)
        except Exception:
            logger.warning("BrainPromptRegistry 渲染失败，fallback", exc_info=True)

    # Fallback: 硬编码
    return await self._render_fallback(user_name, user_message, now, mood)
```

同时可以移除 `self._prompt` (PromptManager 实例)，因为它不再被 `build_system_prompt` 使用（但确认其他模板如 Amygdala、ActiveSpeak 等不通过 PromptAssembler 渲染——它们有自己的渲染路径）。

- [ ] **Step 3: 更新 prompt viewer API**

`core/input/server.py:572` 当前直接读 `prompt/Brain.j2`。改为读取 `prompt/brain/sections/` 下的所有文件并聚合展示：

```python
# 旧代码（删除）:
# brain_path = Path("prompt/Brain.j2")
# brain_source = brain_path.read_text("utf-8") if brain_path.exists() else ""

# 新代码:
sections_dir = Path("prompt/brain/sections")
section_sources = {}
if sections_dir.is_dir():
    for f in sorted(sections_dir.glob("*.j2")):
        section_sources[f.name] = f.read_text("utf-8")
```

> 注意：实际 API 响应格式可能需微调前端。保持 `brain` key 向后兼容，值改为 sections dict 或拼接后的全文。

- [ ] **Step 4: 运行全部测试确认无回归**

```bash
python -m pytest core/prompt/tests/ -v
python -m pytest Module/prompt/test_prompt.py -v
python -m pytest Module/task_system/tests/unit/ -v
```

Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "refactor(prompt): remove Brain.j2, use BrainPromptRegistry exclusively"
```

---

## 文件变更总览

| 操作 | 文件 | 职责 |
|------|------|------|
| **Create** | `core/prompt/__init__.py` | 公开 API |
| **Create** | `core/prompt/section.py` | `PromptSection` dataclass + 5 个异常类 |
| **Create** | `core/prompt/parser.py` | `SectionParser` — 解析 `@section` 头标记 |
| **Create** | `core/prompt/profile.py` | `ProfileConfig` — YAML profile 加载 |
| **Create** | `core/prompt/registry.py` | `BrainPromptRegistry` — 渲染/热更新/AB |
| **Create** | `core/prompt/tests/unit/test_section.py` | 数据模型 + 异常类测试 |
| **Create** | `core/prompt/tests/unit/test_parser.py` | SectionParser 测试 |
| **Create** | `core/prompt/tests/unit/test_profile.py` | ProfileConfig 测试 |
| **Create** | `core/prompt/tests/unit/test_registry.py` | Registry 单元测试（mock 文件系统） |
| **Create** | `core/prompt/tests/integration/test_golden_diff.py` | 黄金对比测试 |
| **Create** | `core/prompt/tests/integration/test_render_full.py` | 完整渲染集成测试 |
| **Create** | `prompt/brain/__init__.py` | 留空 |
| **Create** | `prompt/brain/sections/identity.j2` | 身份 + 关系 |
| **Create** | `prompt/brain/sections/self_perception.j2` | 自我感知 |
| **Create** | `prompt/brain/sections/sensory.j2` | 感官输入 |
| **Create** | `prompt/brain/sections/memory.j2` | 记忆库 |
| **Create** | `prompt/brain/sections/current_state.j2` | 当前状态 |
| **Create** | `prompt/brain/sections/behavior.j2` | 请求分类 + 工具规则 |
| **Create** | `prompt/brain/sections/voice.j2` | 说话风格 |
| **Create** | `prompt/brain/sections/output_format.j2` | inner_voice 协议 |
| **Create** | `prompt/brain/profiles/default.yaml` | 完整 profile |
| **Create** | `prompt/brain/profiles/minimal.yaml` | 极简 profile |
| **Create** | `prompt/brain/metadata.yaml` | 版本 + 顺序 + 依赖 |
| **Modify** | `core/prompt_assembler.py` | 接入 BrainPromptRegistry |
| **Delete** | `prompt/Brain.j2` | Phase 1c 删除（黄金对比通过后） |
| **Modify** | `core/input/server.py` | Phase 1c 更新 prompt viewer |
