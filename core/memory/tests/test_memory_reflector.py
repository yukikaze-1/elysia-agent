"""MemoryReflector 管线测试：数据注入 + 解析 + 执行。"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.long_term import MemoryEntry
from core.memory.memory_reflector import MemoryReflector


class FakeLLM:
    """捕获 prompt 并按序返回预设响应的假 LLM。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def think(self, messages, extra_body=None):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self._responses.pop(0))


def _entry(id_: str, text: str, mem_type: str = "Fact") -> MemoryEntry:
    return MemoryEntry(id=id_, text=text, memory_type=mem_type,
                       poignancy=5, timestamp=1720000000.0)


def _reflector(llm, memory=None) -> MemoryReflector:
    return MemoryReflector(
        llm=llm,
        memory=memory if memory is not None else MagicMock(),
        session=MagicMock(),
    )


@pytest.mark.service
class TestPromptInjection:
    """🔴 防回归：j2 占位符丢失会导致 LLM 收不到记忆数据、反思器静默空转。"""

    @pytest.mark.asyncio
    async def test_review_group_prompt_contains_entry_data(self):
        llm = FakeLLM(responses=["{}"])
        r = _reflector(llm)
        entries = [_entry("mem-id-001", "妖梦养了一只叫橘子的猫")]

        await r._review_group(entries)

        assert len(llm.prompts) == 1
        prompt = llm.prompts[0]
        # 序列化条目必须真正出现在发给 LLM 的 prompt 中
        assert "mem-id-001" in prompt
        assert "妖梦养了一只叫橘子的猫" in prompt
        # 占位符必须已被替换掉
        assert "{memories}" not in prompt
        assert "{last_summary}" not in prompt
