# core/memory/tests/test_prompt_leak_memory.py
"""记忆 Prompt（ExtractMemory/ConsolidationCompare/MemoryReflector）的 Elysia 泄漏基线。

关键：这三个 prompt 用 str.replace() 注入变量（只替换 {conversation}/{memories}/
{new_items}/{existing_items}），{character_name}/{user_name}/妖梦/Elysia/女性AI伴侣
是写死在正文里的，不会被替换——这就是泄漏。
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.consolidator import (
    MemoryConsolidator, _get_extraction_prompt, _get_compare_prompt,
)
from core.memory.memory_reflector import MemoryReflector, REFLECTOR_PROMPT_PATH

pytestmark = pytest.mark.service


class _CapturingLLM:
    """捕获发给 LLM 的 prompt 并返回预设响应的假 LLM。"""

    def __init__(self, response: str):
        self._response = response
        self.prompts: list[str] = []

    async def think(self, messages, extra_body=None):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self._response)


def _cold_consolidator() -> MemoryConsolidator:
    """构造 cold 角色的 MemoryConsolidator（mock llm + memory）。

    传 buffer_threshold=1：consolidate() 只有 len(buffer) >= threshold 才会
    调用 _do_consolidate()（进而向 LLM 发 prompt）。默认阈值 6，单条消息不会触发；
    设 1 保证单条对话就触发提取，从而捕获发给 LLM 的 prompt。
    """
    llm = _CapturingLLM("[]")
    mem = MagicMock()
    mem.search = AsyncMock(return_value=[])
    mem.get_all = MagicMock(return_value=[])
    return MemoryConsolidator(
        llm=llm, memory=mem,
        character_name="ReservedColdCharacter", user_name="用户",
        buffer_threshold=1,
    )


def test_extract_memory_leak_placeholder_unreplaced():
    """ExtractMemory.j2 的 {character_name}/{user_name} 占位符未被替换。"""
    text = _get_extraction_prompt()
    assert "{character_name}" in text
    assert "{user_name}" in text


def test_extract_memory_leak_identity():
    """ExtractMemory.j2 正文硬编码「女性 AI 伴侣」（即使角色是 cold 也写入 prompt）。"""
    text = _get_extraction_prompt()
    assert "女性 AI 伴侣" in text


@pytest.mark.asyncio
async def test_extract_memory_prompt_sent_to_llm_leaks():
    """走真实 consolidate()，捕获发给 LLM 的 prompt，仍含硬编码女性 AI 伴侣。"""
    c = _cold_consolidator()
    results = await c.consolidate("用户：我养了一只猫。", "ReservedColdCharacter：好的。")
    assert len(c._llm.prompts) >= 1  # 触发提取（由于 get_all 空，走 insert 分支，无 compare）
    prompt = c._llm.prompts[0]
    assert "女性 AI 伴侣" in prompt


def test_consolidation_compare_leak_identity():
    """ConsolidationCompare.j2 硬编码 Elysia 身份。"""
    text = _get_compare_prompt()
    assert "你是 Elysia 的记忆一致性检查器" in text


def test_memory_reflector_leak_identity():
    """MemoryReflector.j2 文件正文硬编码 Elysia/妖梦。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "Elysia 是妖梦的女性 AI 伴侣" in content


def test_memory_reflector_leak_user_reference():
    """MemoryReflector.j2 全篇硬编码妖梦（用户指代）。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "妖梦" in content
