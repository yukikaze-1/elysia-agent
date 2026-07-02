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
        # 去掉头部块和模板体之间的空行，以及文件末尾的换行
        template = template.lstrip("\n").rstrip("\n")

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
