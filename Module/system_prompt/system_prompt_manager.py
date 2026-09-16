"""SystemPromptManager — 持有 section 列表，按序 async render，拼接最终 system prompt。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from Module.system_prompt.protocol import PromptSection

logger = logging.getLogger(__name__)

_RENDER_ORDER = [
    # ── 静态前缀（最大化 DeepSeek prefix cache 命中率）──
    "ConflictRules",     # F1 — 零变量，固定规则块
    "OutputFormat",      # F2 — 零变量，输出格式
    "TTSMarkup",         # F3 — tts_provider，极少热切换
    "Relationship",      # C2 — 仅 user_name / user_pronoun
    "Proactive",         # C7 — 仅 user_name / user_pronoun
    "SpeakingStyle",     # C6 — L2 更新时才变
    "Personality",       # C1 — L1 不变，L2 缓慢演化
    "SelfPerception",    # C9 — awareness text（通常为空）
    "OwnLife",           # C8 — 会话级增量变化
    # ── 动态后缀（每轮更新）──
    "Emotion",           # C3 — mood 每轮可能变
    "Memory",            # C4 — LTM 搜索结果
    "Sensory",           # C5 — 当前时间戳，每轮不同
]


_PROTECTED_SECTIONS: tuple[str, ...] = (
    # 身份与输出契约：丢了就不是"同一个角色 + 同一个协议"了，宁可不截
    "ConflictRules", "OutputFormat", "TTSMarkup", "Personality",
    "Relationship", "SpeakingStyle", "Proactive",
)
"""永不截断的 section（owner 裁定 F1/F2 永不截断的推广：同属"她是她"的契约）。"""

_DROPPABLE_SECTIONS: tuple[str, ...] = (
    # 可丢顺序：先丢"最不影响身份与格式"的，后丢情绪/记忆类
    "OwnLife", "SelfPerception", "Sensory", "Emotion", "Memory",
)
"""超预算时按此顺序整节丢弃。**顺序即优先级**（前者先丢）。"""


class SystemPromptManager:
    """持有 section 列表，按序 async render，拼接最终 system prompt。"""

    def __init__(self, sections: Sequence[PromptSection]):
        """初始化 system prompt 管理器。

        按 _RENDER_ORDER 对 sections 排序，确保前缀缓存命中率最大化。

        Args:
            sections: PromptSection 实例序列（任意顺序）。
        """
        order_map = {name: i for i, name in enumerate(_RENDER_ORDER)}
        self._sections = sorted(sections, key=lambda s: order_map.get(s.name, 999))

    async def render(self, *, budget_tokens: int | None = None) -> str:
        """渲染完整 system prompt（可选按预算截断，S2-H9）。

        截断优先级（owner 2026-09-16 裁定）：**F1/F2 永不截断 → C4 记忆逐条截 →
        其余可丢节按 `_DROPPABLE_SECTIONS` 顺序整节丢**。`_PROTECTED_SECTIONS` 里的
        节永不丢——它们是"她是谁 / 怎么说话 / 输出契约"，丢了就不是同一个角色了。

        为什么先减记忆条数而不是直接丢整节：C4 是**弹性最大**的一块（LTM 检索结果，
        条目之间可独立取舍），而整节丢会让"她完全失忆"，减条数只是"少记起几件事"。

        Args:
            budget_tokens: system prompt 的 token 预算；None = 不设预算（预览/测试）。

        Returns:
            所有 section 拼接后的完整 system prompt 字符串（超预算时已按上述优先级裁剪）。
        """
        rendered: list[tuple[str, str]] = []
        for section in self._sections:
            result = await section.render()
            if result:
                rendered.append((section.name, result))

        if budget_tokens is None:
            return "\n\n".join(text for _name, text in rendered)

        from Module.system_prompt.budget import estimate_tokens

        def _joined(items: list[tuple[str, str]]) -> str:
            return "\n\n".join(text for _n, text in items)

        total = estimate_tokens(_joined(rendered))
        if total <= budget_tokens:
            return _joined(rendered)

        # ① C4 记忆逐条截：从 5 条起按 3/2/1/0 递减（0 = 该节渲染为空）
        for limit in (3, 2, 1, 0):
            self.set_context(memory_limit=limit)
            retry: list[tuple[str, str]] = []
            for section in self._sections:
                result = await section.render()
                if result:
                    retry.append((section.name, result))
            rendered = retry
            total = estimate_tokens(_joined(rendered))
            if total <= budget_tokens:
                logger.info(
                    "system prompt 超预算 → 记忆条数降为 %d 后满足（估算 %d / 预算 %d）",
                    limit, total, budget_tokens)
                return _joined(rendered)

        # ② 仍超预算：按可丢顺序整节丢弃（保护节绝不丢）
        dropped: list[str] = []
        for name in _DROPPABLE_SECTIONS:
            rendered = [(n, t) for n, t in rendered if n != name]
            if any(n == name for n, _t in rendered):
                continue  # 该节本来就没渲染出内容
            dropped.append(name)
            total = estimate_tokens(_joined(rendered))
            if total <= budget_tokens:
                break
        logger.warning(
            "system prompt 超预算：丢弃 %s（记忆条数已降至 0）后估算 %d / 预算 %d",
            dropped or "无可丢节", total, budget_tokens)
        if total > budget_tokens:
            # 保护节自己就超预算——不静默截断身份/格式契约，如实报出
            logger.warning(
                "保护节（%s）自身已达 %d token > 预算 %d：请提高 context_window "
                "或缩减角色档案，本系统 prompt 不做进一步截断",
                "/".join(_PROTECTED_SECTIONS), total, budget_tokens)
        return _joined(rendered)

    def set_context(self, **kwargs: Any) -> None:
        """透传 per-request 上下文给所有 section。

        Args:
            **kwargs: 上下文键值对（如 user_name、mood 等）。
        """
        for s in self._sections:
            s.set_context(**kwargs)

    def get_versions(self) -> dict[str, str]:
        """获取所有 section 的版本号。

        Returns:
            {section_name: version, ...} 映射。
        """
        return {s.name: s.version for s in self._sections}

    async def reload_all(self) -> None:
        """热重载所有 section 的模板和数据。"""
        for s in self._sections:
            s.reload()

    def validate_all(self) -> list[str]:
        """自检所有 section。

        Returns:
            未通过自检的 section 名称列表。
        """
        return [s.name for s in self._sections if not s.validate()]
