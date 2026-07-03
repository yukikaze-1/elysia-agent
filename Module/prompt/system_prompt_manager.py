"""SystemPromptManager — 持有 section 列表，按序 async render，拼接最终 system prompt。"""

from __future__ import annotations

import logging
from .protocol import PromptSection

logger = logging.getLogger(__name__)

_RENDER_ORDER = [
    "ConflictRules",     # F1
    "OutputFormat",      # F2
    "Sensory",           # C5
    "Emotion",           # C3
    "Memory",            # C4
    "Personality",       # C1
    "Relationship",      # C2
    "SpeakingStyle",     # C6
    "Proactive",         # C7
    "OwnLife",           # C8
    "SelfPerception",    # C9
]


class SystemPromptManager:
    """持有 section 列表，按序 async render，拼接最终 system prompt。"""

    def __init__(self, sections: list[PromptSection]):
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
