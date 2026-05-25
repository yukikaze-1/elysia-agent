"""Jinja2 模板管理器 — 加载、渲染模板与命名 macro。"""

import os
import logging
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


class PromptManager:
    """Jinja2 模板渲染器。

    Usage:
        pm = PromptManager("prompt/")
        system = pm.render_macro("Brain.j2", "BrainSystemPrompt", ...)
        full = pm.render("simple.j2", name="world")
    """

    def __init__(
        self,
        template_dir: str,
        *,
        auto_reload: bool = False,
        logger: logging.Logger | None = None,
    ):
        if not os.path.isdir(template_dir):
            raise FileNotFoundError(
                f"Template directory not found: {template_dir}"
            )

        self._logger = logger or logging.getLogger(__name__)
        self.template_dir = template_dir

        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=auto_reload,
        )

        def _datetime_filter(value, fmt="%Y-%m-%d %H:%M:%S"):
            if value is None:
                return ""
            return datetime.fromtimestamp(float(value)).strftime(fmt)

        self.env.filters["datetime"] = _datetime_filter

    def render(self, template_name: str, **kwargs) -> str:
        """渲染整个模板文件。

        :param template_name: 模板文件名 (如 "simple.j2")
        :param kwargs: 传递给模板的变量
        :return: 渲染后的字符串
        """
        try:
            template = self.env.get_template(template_name)
            return template.render(**kwargs).strip()
        except TemplateNotFound:
            raise FileNotFoundError(
                f"Template '{template_name}' not found in {self.template_dir}"
            )
        except Exception as e:
            self._logger.error(
                "Error rendering template '%s': %s", template_name, e
            )
            raise RuntimeError(
                f"Error rendering template '{template_name}': {e}"
            ) from e

    def render_macro(
        self, template_name: str, macro_name: str, **kwargs
    ) -> str:
        """渲染模板中的指定 macro。

        :param template_name: 模板文件名 (如 "Brain.j2")
        :param macro_name: 模板内定义的 macro 名
        :param kwargs: 传递给 macro 的参数
        :return: 渲染后的字符串
        """
        try:
            template = self.env.get_template(template_name)
            module = template.make_module()

            if not hasattr(module, macro_name):
                raise ValueError(
                    f"Macro '{macro_name}' not found in '{template_name}'"
                )

            macro_func = getattr(module, macro_name)
            return str(macro_func(**kwargs)).strip()

        except TemplateNotFound:
            raise FileNotFoundError(
                f"Template '{template_name}' not found in {self.template_dir}"
            )
        except (ValueError, FileNotFoundError):
            raise
        except Exception as e:
            self._logger.error(
                "Error rendering macro '%s' in '%s': %s", macro_name, template_name, e
            )
            raise
