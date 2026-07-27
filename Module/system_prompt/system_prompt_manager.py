"""SystemPromptManager — 持有 section 列表，按序 async render，拼接最终 system prompt。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from .protocol import PromptSection

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


class SystemPromptManager:
    """持有 section 列表，按序 async render，拼接最终 system prompt。"""

    def __init__(self, sections: Sequence[PromptSection]):
        order_map = {name: i for i, name in enumerate(_RENDER_ORDER)}
        self._sections = sorted(sections, key=lambda s: order_map.get(s.name, 999))

    async def render(self) -> str:
        parts = []
        for s in self._sections:
            result = await s.render()
            if result:
                parts.append(result)
        return "\n\n".join(parts)

    def set_context(self, **kwargs) -> None:
        """透传 per-request 上下文给所有 section。"""
        for s in self._sections:
            s.set_context(**kwargs)

    def get_versions(self) -> dict[str, str]:
        return {s.name: s.version for s in self._sections}

    async def reload_all(self) -> None:
        for s in self._sections:
            s.reload()

    def validate_all(self) -> list[str]:
        return [s.name for s in self._sections if not s.validate()]
