"""PromptSection 抽象基类 + StaticSection + Jinja2Section。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, TemplateSyntaxError

from Module.system_prompt.sections.authored import escape_template_vars

logger = logging.getLogger(__name__)


def _datetime_filter(ts: float) -> str:
    """Unix 时间戳 → 可读日期字符串。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class PromptSection(ABC):
    """System prompt 的一个区块。自管数据、自管模板、自管生命周期。"""

    name: str = ""
    version: str = "0.0.0"
    description: str = ""

    #: 本 section 支持的「render 期临时覆盖」键集合。默认空 = 不接受任何覆盖。
    render_override_keys: frozenset[str] = frozenset()

    @abstractmethod
    async def render(self) -> str:
        """渲染该 section 的 prompt 片段。

        Returns:
            渲染后的 prompt 文本。
        """

    def reload(self) -> None:
        """热更新模板/数据。默认空实现。

        Returns:
            None
        """

    def validate(self) -> bool:
        """自检模板/数据有效性。

        Returns:
            True 表示通过自检。默认返回 True。
        """
        return True

    def dependencies(self) -> list[str]:
        """返回依赖的其他 section 名称列表。

        Returns:
            依赖的 section 名称列表。默认空列表。
        """
        return []

    def set_context(self, **kwargs: Any) -> None:
        """注入 per-request 上下文。

        子类覆盖以接收需要的参数（如 user_name、mood 等）。

        ⚠️ 各子类实现是 `kwargs.get(key, default)` 语义——**缺键 = 复位成默认值**。
        故本方法只接受「一次完整的 per-request 上下文」的广播；**预算降级之类的
        部分键试探不得走这里**（见 `override_for_render` 与
        `SystemPromptManager.render` 的 render-local 不变式）。

        Args:
            **kwargs: 上下文键值对。
        """

    def override_for_render(self, key: str, value: Any) -> Callable[[], None] | None:
        """应用一个**仅在本轮 render 内有效**的覆盖，返回恢复原值的回调。

        ## 为什么存在（Issue A，2026-09-16）

        `SystemPromptManager.render(budget_tokens=…)` 的截断梯子需要在不污染
        per-request 上下文的前提下试探「记忆条数降到 N 能不能装下」。它曾经走
        `set_context(memory_limit=N)`——而那是**广播**：打到全部 section，且每个
        section 的实现都是 `kwargs.get(key, default)`（缺键 = 复位默认值）。只带
        `memory_limit` 的一次广播因此把整片 per-request 上下文清掉（实测 8 个
        section / 12 个字段：user_name、speaking_style_c6、tts_provider、
        stt_emotion、mood …），把「永不丢」的保护节清成空壳，并让记忆上限永久粘在
        0（此后每一轮都不再检索 C4）。回归守卫见
        `Module/system_prompt/tests/test_render_local_budget.py`。

        ## 为什么默认「什么都不接受」

        默认返回 None（不接受任何覆盖）是 **fail-safe**：覆盖是 opt-in 的显式契约，
        未参与该契约的 section 永远不会被一个它不认识的 key 改到任何字段。反过来
        （默认把 key 转发给 `set_context`）等于把「缺键 = 复位」的隐患换个入口重演。

        Args:
            key: 覆盖键（section 需在 `render_override_keys` 里显式声明支持）。
            value: 覆盖值。

        Returns:
            恢复回调（调用即还原为本轮 render 之前的值）；本 section 不支持该 key
            时返回 None。
        """
        return None


class StaticSection(PromptSection):
    """纯静态文本 section。"""

    def __init__(self, name: str, version: str, description: str, text: str):
        """初始化静态文本 section。

        Args:
            name: section 名称（如 "ConflictRules"）。
            version: 版本号。
            description: 描述。
            text: 静态 prompt 文本。
        """
        self.name = name
        self.version = version
        self.description = description
        self._text = text

    async def render(self) -> str:
        return self._text

    def render_sync(self) -> str:
        """同步版 render，测试用。

        Returns:
            静态 prompt 文本。
        """
        return self._text


class Jinja2Section(PromptSection):
    """持有独立 .j2 模板的 section。"""

    def __init__(self, template_path: str):
        """初始化 Jinja2 模板 section。

        Args:
            template_path: .j2 模板文件的绝对路径。
        """
        self._template_path = template_path
        # nosec B701——本地受信 .j2 模板（非用户 HTML），autoescape 关闭是刻意的
        # （prompt 模板需原始控制，输出给 LLM 非浏览器）；bandit 静态误报 XSS。
        self._env = Environment(trim_blocks=True, lstrip_blocks=True)  # nosec B701
        self._env.filters["datetime"] = _datetime_filter
        self._cached_template = None

    def switch_template(self, template_path: str) -> None:
        """切换模板路径并失效缓存（幂等：同路径不重复失效）。

        消费方（`core/prompt/prompt_assembler.py::_switch_template`）按角色
        切换到运行时模板；此前逐个戳 `_template_path` / `_cached_template`，
        缓存失效逻辑因此散落在消费方——那是本类的内部不变式，收口在此。

        Args:
            template_path: 新的 .j2 模板绝对路径。
        """
        if self._template_path == template_path:
            return
        self._template_path = template_path
        self._cached_template = None

    def _load_template(self) -> str:
        """从磁盘读取模板源文件。

        Returns:
            模板源文本。
        """
        return Path(self._template_path).read_text("utf-8")

    def _render_template(self, **vars: Any) -> str:
        """用给定变量渲染模板（**所有字符串叶子在此统一转义**）。

        这是 system prompt 各段注入变量的**唯一收口点**：CR-12/S2-H2 的逐点转义
        实测漏了三处（其中 `displayName` 与 `speaking_style_c6` 是活的注入通道），
        故改在此处递归转义。需要原文的位置用 `Trusted` 显式豁免——豁免是显式且
        可 grep 的，而遗漏不再是默认结果。详见
        `Module/system_prompt/sections/authored.py`。

        Args:
            **vars: 传递给 Jinja2 模板的变量（字符串叶子会被转义）。

        Returns:
            渲染后的 prompt 文本。
        """
        if self._cached_template is None:
            self._cached_template = self._load_template()
        tmpl = self._env.from_string(self._cached_template)
        safe = {key: escape_template_vars(value) for key, value in vars.items()}
        return tmpl.render(**safe).strip()

    def render_sync(self) -> str:
        """同步版 render，测试用。子类覆盖 render() 即可。

        Returns:
            渲染后的 prompt 文本。
        """
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
        except FileNotFoundError:
            logger.warning("模板文件不存在: %s", self._template_path, exc_info=True)
            return False
        try:
            self._env.parse(source)
            return True
        except TemplateSyntaxError:
            logger.warning("模板语法错误: %s", self._template_path, exc_info=True)
            return False
