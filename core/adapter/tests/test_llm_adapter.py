"""LLMAdapter 单元测试 — think() 结构化解析与降级。"""
import logging

import pytest

from core.adapter.llm_adapter import LLMAdapter
from core.tests.fake_llm import FakeLLMBackend


@pytest.mark.unit
class TestThink:
    """think() 工具模式 JSON 解析降级。"""

    @pytest.mark.asyncio
    async def test_think_tool_json_parses_tool_calls(self):
        """合法 tool_calls JSON → 返回 ThoughtResult.tool_calls。"""
        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=(
            '{"tool_calls": [{"id": "call_1", "function": {"name": "search", '
            '"arguments": "{\\"q\\": \\"test\\"}"}}]}'
        )))
        try:
            result = await adapter.think([{"role": "user", "content": "hi"}], tools=[{}])
            assert result.text == ""
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0].function.name == "search"
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_think_tool_mode_malformed_json_logs_warning(self, caplog):
        """工具模式下响应不是合法 JSON → 降级为纯文本但必须留 warning（S5）。

        原实现 pass 静默——畸形/截断的工具调用 JSON 会被原样当回复发给用户，
        且无任何日志可诊断。
        """
        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response='{"tool_calls": [truncated'))
        try:
            with caplog.at_level(logging.WARNING, logger="core.adapter.llm_adapter"):
                result = await adapter.think([{"role": "user", "content": "hi"}], tools=[{}])
            assert result.text == '{"tool_calls": [truncated'
            assert any("think()" in r.message for r in caplog.records)
        finally:
            await adapter.close()

    @pytest.mark.asyncio
    async def test_think_text_mode_no_warning(self, caplog):
        """纯文本模式（无 tools）→ 无工具 JSON warning（本就纯文本，不算降级）。"""
        adapter = LLMAdapter(_test_backend=FakeLLMBackend(response="普通的回复文本"))
        try:
            with caplog.at_level(logging.WARNING, logger="core.adapter.llm_adapter"):
                result = await adapter.think([{"role": "user", "content": "hi"}])
            assert result.text == "普通的回复文本"
            assert not any("think()" in r.message for r in caplog.records)
        finally:
            await adapter.close()
