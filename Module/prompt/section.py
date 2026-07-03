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
