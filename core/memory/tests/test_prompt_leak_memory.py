# core/memory/tests/test_prompt_leak_memory.py
"""记忆 Prompt（ExtractMemory/ConsolidationCompare/MemoryReflector）的 Elysia 泄漏基线。

B-2 Task 5：这三个 prompt 从硬编码 Elysia/妖梦/女性AI伴侣 改为占位符
（{character_name}/{user_name}）注入。本文件翻转 Stage A 的泄漏基线——
不再断言 Elysia/妖梦/女性AI伴侣 出现在正文，而是断言占位符存在且无角色泄漏，
并在真实 consolidate()/prompt 加载路径上验证 replace 已注入值、占位符不复现。
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.consolidator import (
    MemoryConsolidator, _get_extraction_prompt, _get_compare_prompt,
)
from core.memory.memory_reflector import REFLECTOR_PROMPT_PATH

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


def test_extract_memory_leak_placeholder_present():
    """ExtractMemory.j2 用 {character_name}/{user_name} 占位符（正文不再硬编码角色名）。"""
    text = _get_extraction_prompt()
    assert "{character_name}" in text
    assert "{user_name}" in text
    # 翻转：正文不再硬编码 Elysia/妖梦/女性AI伴侣
    assert "Elysia" not in text
    assert "妖梦" not in text


def test_extract_memory_leak_identity_flipped():
    """ExtractMemory.j2 正文不再硬编码「女性 AI 伴侣」（角色无关，cold 也不写死）。"""
    text = _get_extraction_prompt()
    assert "女性 AI 伴侣" not in text
    assert "俏皮" not in text and "柔软而直接" not in text


@pytest.mark.asyncio
async def test_extract_memory_prompt_sent_to_llm_no_leak():
    """走真实 consolidate()，捕获发给 LLM 的 prompt：占位符被替换，无角色泄漏。"""
    c = _cold_consolidator()
    results = await c.consolidate("用户：我养了一只猫。", "ReservedColdCharacter：好的。")
    assert len(c._llm.prompts) >= 1  # 触发提取（由于 get_all 空，走 insert 分支，无 compare）
    prompt = c._llm.prompts[0]
    # 占位符已被 character_name/user_name 注入
    assert "ReservedColdCharacter" in prompt
    assert "{character_name}" not in prompt and "{user_name}" not in prompt
    # 无 Elysia/妖梦 字面泄漏
    assert "Elysia" not in prompt and "妖梦" not in prompt
    assert "女性 AI 伴侣" not in prompt


def test_consolidation_compare_leak_identity_flipped():
    """ConsolidationCompare.j2 用 {character_name} 占位符，不再硬编码 Elysia。"""
    text = _get_compare_prompt()
    assert "你是 {character_name} 的记忆一致性检查器" in text
    assert "Elysia" not in text
    assert "妖梦" not in text


def test_memory_reflector_leak_identity_flipped():
    """MemoryReflector.j2 文件正文用 {character_name}/{user_name}，不再硬编码 Elysia/妖梦。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "{character_name}" in content and "{user_name}" in content
    assert "Elysia" not in content and "妖梦" not in content


def test_memory_reflector_leak_user_reference_flipped():
    """MemoryReflector.j2 全篇用 {user_name}，不再硬编码妖梦（用户指代）。"""
    content = REFLECTOR_PROMPT_PATH.read_text(encoding="utf-8")
    assert "妖梦" not in content
