"""MemoryConsolidator — extracts key facts from conversations.

After each assistant response, runs a lightweight extraction to pull out
plans, preferences, facts, and decisions. These are stored in long-term
memory for future recall.

Uses the same LLM backend as the cognitive loop (or mock).
"""

import asyncio
import json
import logging

from .memory.long_term import LongTermMemory
from .llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """从以下对话中提取值得长期记住的关键信息。

**提取规则**:
- 提取事实(Fact)、偏好(Preference)、事件(Event)、计划(Plan)
- 过滤噪音: 问候、闲聊、日常寒暄不需要记住
- **事件合并**: 将有因果/反应/结果关系的多轮对话合并为一个综合记忆，不拆碎片
- 每条记忆写为第一人称中文叙事: "我注意到...", "我发现...", "用户告诉我...", "我和用户..."

**重要性评分 (poignancy)**:
- 1-3: 琐碎信息，不太重要
- 4-7: 中等重要，值得记住
- 8-10: 核心记忆，与用户关系或{character_name}自身密切相关

返回 JSON 数组，每个元素: {{"type": "Fact|Preference|Event|Plan", "content": "...", "poignancy": 1-10}}。

如果没有值得记住的信息，返回空数组 []。

对话:
{conversation}

JSON:"""


class MemoryConsolidator:
    """Extracts and stores key facts from conversations."""

    BUFFER_THRESHOLD = 6  # 攒够 N 条消息再调 LLM 提取

    def __init__(self, llm: LLMAdapter, memory: LongTermMemory, character_name: str = "Elysia",
                 buffer_threshold: int | None = None):
        self._llm = llm
        self._memory = memory
        self._character_name = character_name
        self._history: list[dict] = []  # recent consolidation results
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg) 缓冲
        self._threshold = buffer_threshold if buffer_threshold is not None else self.BUFFER_THRESHOLD
        self._consolidate_lock = asyncio.Lock()

    async def consolidate(self, user_message: str, assistant_response: str) -> list[dict]:
        """缓冲对话，攒够阈值后调 LLM 提取。返回本次提取的条目（可能为空）。

        如果已有 consolidation 正在运行则跳过本轮（防止并发 LLM 调用）。
        """
        if not assistant_response.strip() or not user_message.strip():
            return []

        self._buffer.append((user_message, assistant_response))

        if len(self._buffer) < self._threshold:
            return []

        if self._consolidate_lock.locked():
            logger.debug("Consolidation skipped — previous run still active")
            return []

        async with self._consolidate_lock:
            return await self._do_consolidate()

    async def _do_consolidate(self) -> list[dict]:
        """执行实际的 LLM 提取和存储（需持有 consolidate_lock）。"""
        # 合并缓冲中的多轮对话为一段文本
        turns: list[str] = []
        for u, a in self._buffer:
            turns.append(f"用户: {u[:300]}")
            turns.append(f"{self._character_name}: {a[:500]}")
        conversation_text = "\n".join(turns)
        self._buffer.clear()

        prompt = EXTRACTION_PROMPT.format(
            conversation=conversation_text[:2000],
            character_name=self._character_name,
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
            poignancy = int(item.get("poignancy", 5))
            if content:
                await self._memory.insert(
                    content,
                    memory_type=item_type,
                    poignancy=poignancy,
                    source="consolidation",
                )
                logger.debug("Consolidated: [%s|P%d] %s", item_type, poignancy, content[:60])

        for item in items:
            self._history.append({
                "type": item.get("type", "?"),
                "content": item.get("content", ""),
                "poignancy": item.get("poignancy", 5),
            })
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
