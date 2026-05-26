"""MemoryConsolidator — extracts key facts from conversations.

After each assistant response, runs a lightweight extraction to pull out
plans, preferences, facts, and decisions. These are stored in long-term
memory for future recall.

Uses the same LLM backend as the cognitive loop (or mock).
"""

import json
import logging

from .memory.long_term import LongTermMemory
from .llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """从以下对话中提取需要记住的关键信息。只提取客观事实，不要包含闲聊。

返回 JSON 数组，每个元素是 {{"type": "plan|preference|fact|note", "content": "..."}}。

如果没有值得记住的信息，返回空数组 []。

用户: {user_message}
{character_name}: {assistant_response}

JSON:"""


class MemoryConsolidator:
    """Extracts and stores key facts from conversations."""

    def __init__(self, llm: LLMAdapter, memory: LongTermMemory, character_name: str = "Elysia"):
        self._llm = llm
        self._memory = memory
        self._character_name = character_name
        self._history: list[dict] = []  # recent consolidation results

    async def consolidate(self, user_message: str, assistant_response: str) -> list[dict]:
        """Extract key facts from a turn and store them. Returns extracted items."""
        if not assistant_response.strip() or not user_message.strip():
            return []

        prompt = EXTRACTION_PROMPT.format(
            user_message=user_message[:500],
            character_name=self._character_name,
            assistant_response=assistant_response[:1000],
        )

        try:
            result = await self._llm.think([{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}})
            items = self._parse_result(result.text)
        except Exception:
            logger.debug("Consolidation extraction failed", exc_info=True)
            return []

        for item in items:
            content = item.get("content", "").strip()
            item_type = item.get("type", "note")
            if content:
                await self._memory.insert(
                    content,
                    type=item_type,
                    source="consolidation",
                )
                logger.debug("Consolidated: [%s] %s", item_type, content[:60])

        for item in items:
            self._history.append({"type": item.get("type", "?"), "content": item.get("content", "")})
        if len(self._history) > 20:
            self._history = self._history[-20:]

        return items

    @staticmethod
    def _parse_result(text: str) -> list[dict]:
        """Parse LLM output as JSON array. Robust to markdown wrapping."""
        text = text.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            items = json.loads(text)
            if isinstance(items, list):
                return items
        except json.JSONDecodeError:
            # Try to find JSON array inside text
            import re
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    items = json.loads(match.group())
                    if isinstance(items, list):
                        return items
                except json.JSONDecodeError:
                    pass
        return []
