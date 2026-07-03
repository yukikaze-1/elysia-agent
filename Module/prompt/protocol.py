"""PromptSection ABC + StaticSection + Jinja2Section。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError

logger = logging.getLogger(__name__)


class PromptSection(ABC):
    """System prompt 的一个区块。自管数据、自管模板、自管生命周期。"""

    name: str = ""
    version: str = "0.0.0"
    description: str = ""

    @abstractmethod
    async def render(self) -> str:
        """渲染该 section 的 prompt 片段。"""

    def reload(self) -> None:
        """热更新。默认空实现。"""

    def validate(self) -> bool:
        """自检。默认返回 True。"""
        return True

    def dependencies(self) -> list[str]:
        """依赖的其他 section 名称列表。"""
        return []

    def set_context(self, **kwargs) -> None:
        """注入 per-request 上下文。子类覆盖以接收需要的参数。"""


class StaticSection(PromptSection):
    """纯静态文本 section。"""

    def __init__(self, name: str, version: str, description: str, text: str):
        self.name = name
        self.version = version
        self.description = description
        self._text = text

    async def render(self) -> str:
        return self._text

    def render_sync(self) -> str:
        """同步版 render，测试用。"""
        return self._text


class Jinja2Section(PromptSection):
    """持有独立 .j2 模板的 section。"""

    def __init__(self, template_path: str):
        self._template_path = template_path
        self._env = Environment(trim_blocks=True, lstrip_blocks=True)
        self._cached_template = None

    def _load_template(self) -> str:
        """从磁盘读模板源。"""
        return Path(self._template_path).read_text("utf-8")

    def _render_template(self, **vars) -> str:
        """用给定变量渲染模板。"""
        if self._cached_template is None:
            self._cached_template = self._load_template()
        tmpl = self._env.from_string(self._cached_template)
        return tmpl.render(**vars).strip()

    def render_sync(self) -> str:
        """同步版 render，测试用。子类覆盖 render() 即可。"""
        return self._render_template()

    async def render(self) -> str:
        return self.render_sync()

    def reload(self) -> None:
        """重读模板文件。"""
        try:
            self._cached_template = None
            self._load_template()
        except Exception:
            logger.warning("reload 模板失败: %s", self._template_path, exc_info=True)

    def validate(self) -> bool:
        """检查模板可解析。"""
        try:
            source = self._load_template()
            self._env.parse(source)
            return True
        except TemplateSyntaxError:
            logger.warning("模板语法错误: %s", self._template_path, exc_info=True)
            return False
        except Exception:
            logger.warning("模板加载失败: %s", self._template_path, exc_info=True)
            return False
