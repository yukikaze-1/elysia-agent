"""Batch 18 / KB-12：**预算装配链**的接线守卫。

## 为什么单独一组

估算器本身有合同测试（`Module/system_prompt/tests/test_token_corpus_contract.py`），
但"算得对"不等于"被算到"——`tools=[...]` 曾经整整一类输入不在估算里，根因不是公式，
而是**装配链没人把它传下去**（`loop.py` 先花预算、再去取 tool_defs）。

这一组钉的就是链路上的每一跳：

    CognitiveLoop._build_turn_prompt
        → PromptAssembler.build_messages(model_id=, tools=)
            → MessageBuilder.build_messages(model_id=, tools=)
                → _apply_prompt_budget(model_id=, tools=)
                    → estimate_messages_tokens(model=, tools=)

任何一跳丢掉参数，本组必须红。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive import CognitiveLoop
from core.monitoring.event_bus import EventBus
from core.memory.session import SessionMemory
from core.status.agent_fsm import AgentFSM
from Module.output import OutputManager

pytestmark = pytest.mark.service

_MODEL = "ollama/qwen2.5:3b"

# 一个"很贵"的工具集：足够大到让预算决策翻转（≈1000+ token）。
_BIG_TOOLS = [
    {"type": "function", "function": {
        "name": f"plugin_{i}",
        "description": "对目标对象执行声明式变更并返回执行结果摘要。" * 4,
        "parameters": {"type": "object",
                       "properties": {f"p{j}": {"type": "string",
                                                "description": "参数说明文字" * 3}
                                      for j in range(4)},
                       "required": ["p0"]}}}
    for i in range(6)
]


async def _build(loop: CognitiveLoop):
    """按生产签名调 `_build_turn_prompt`（参数齐了才叫"走同一条路"）。"""
    return await loop._build_turn_prompt(  # noqa: SLF001
        user_message="在吗", user_name="妖梦", content=None,
        stt_emotion="", stt_events=None, is_task_result=False)


def _make_loop(*, tool_defs=None) -> CognitiveLoop:
    llm = MagicMock()
    llm.model = _MODEL
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt._turn_context = None
    prompt.set_session = MagicMock()
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    adapter = MagicMock()
    adapter.definitions = MagicMock(return_value=list(tool_defs or []))
    default = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default

    loop = CognitiveLoop(
        llm=llm, fsm=AgentFSM(), event_bus=EventBus(), output=OutputManager(),
        prompt=prompt, session_resolver=resolver, character_engine=None,
    )
    loop._session = default  # noqa: SLF001 — 组合根注入点，测试直接装配
    loop._tool_adapter = adapter  # noqa: SLF001 — 组合根注入点，测试直接装配
    loop._clock = MagicMock()  # noqa: SLF001
    loop._clock.span = MagicMock(return_value=MagicMock(elapsed_ms=1.0))
    return loop


class TestTurnPromptWiring:
    def test_turn_prompt_hands_the_resolved_model_and_tools_to_the_assembler(self):
        """`_build_turn_prompt` 必须把 **resolved model** 与 **tool_defs** 一起交下去。

        没有这一条，`tools` 会在预算决策之后才被取出来——正是 KB-12 的 false accept
        根因（12 个内置工具实测 1126 token 对预算完全不可见）。
        """
        loop = _make_loop(tool_defs=_BIG_TOOLS)
        asyncio.run(_build(loop))
        kwargs = loop._prompt.build_messages.await_args.kwargs  # noqa: SLF001
        assert kwargs.get("model_id") == _MODEL, (
            "resolved model 必须传给组装层：估算 profile 由它决定")
        assert kwargs.get("tools") == _BIG_TOOLS, (
            "tool_defs 必须传进预算——否则 provider request 的 tools 段完全不进估算")
        assert kwargs.get("prompt_budget_tokens"), "预算仍必须传"

    def test_tool_definitions_are_taken_before_the_budget_is_spent(self):
        """工具定义必须在**预算之前**取到（顺序即正确性，不是风格）。"""
        loop = _make_loop(tool_defs=_BIG_TOOLS)
        order: list[str] = []
        loop._tool_adapter.definitions.side_effect = (  # noqa: SLF001
            lambda *a, **k: order.append("tools") or list(_BIG_TOOLS))
        original = loop._prompt.build_messages  # noqa: SLF001

        async def _record(*a, **k):
            order.append("budget")
            return await original(*a, **k)

        loop._prompt.build_messages = _record  # noqa: SLF001
        asyncio.run(_build(loop))
        assert order == ["tools", "budget"], f"顺序错了：{order}"


class TestAssemblerWiring:
    @staticmethod
    def _assembler():
        from core.config.character_config import CharacterConfig
        from core.prompt.prompt_assembler import PromptAssembler

        character = CharacterConfig(name="Elysia")
        assembler = PromptAssembler(character=character, session=None, long_term=None)
        assembler.enable_preview_mode()
        return assembler

    def test_build_messages_forwards_tools_and_model_into_the_budget(self):
        """`PromptAssembler.build_messages` 不得吞掉 `tools` / `model_id`。"""
        from core.prompt.message_builder import MessageBuilder

        assembler = self._assembler()
        captured: dict = {}
        original = MessageBuilder.build_messages

        def _spy(self, system_prompt, history, user_content=None, **kwargs):
            captured.update(kwargs)
            return original(self, system_prompt, history, user_content, **kwargs)

        MessageBuilder.build_messages = _spy
        try:
            asyncio.run(assembler.build_messages(
                user_message="在吗", prompt_budget_tokens=4096,
                model_id=_MODEL, tools=_BIG_TOOLS))
        finally:
            MessageBuilder.build_messages = original
        assert captured.get("model_id") == _MODEL
        assert captured.get("tools") == _BIG_TOOLS
        assert captured.get("prompt_budget_tokens") == 4096

    def test_system_prompt_ladder_uses_the_same_profile(self):
        """system prompt 侧的裁剪必须与历史侧用同一个 profile（否则两处判据分叉）。"""
        from Module.system_prompt.tests.test_token_corpus_contract import (
            _PROFILE_FAMILIES,
            _profile_model,
        )

        assembler = self._assembler()
        seen: list = []
        original = assembler._manager.render  # noqa: SLF001

        async def _spy(*, budget_tokens=None, model_id=None):
            seen.append(model_id)
            return await original(budget_tokens=budget_tokens, model_id=model_id)

        assembler._manager.render = _spy  # noqa: SLF001
        asyncio.run(assembler.build_system_prompt(
            user_message="在吗", prompt_budget_tokens=2048, model_id=_MODEL))
        assert seen == [_MODEL], f"render 收到的是 {seen}"
        assert _PROFILE_FAMILIES and _profile_model("cjk_opt") == _MODEL


class TestMessageBuilderToolAccounting:
    def test_tool_schemas_can_force_history_trimming(self):
        """行为面：同一份 messages，**加 tools 后**历史必须被裁得更多。

        只断言 token 数会漏掉"参数没传下去"这类接线缺陷——这里断言**行为差异**。
        """
        from core.prompt.message_builder import MessageBuilder
        from core.schema.message import AssistantMessage, MessageId, UserMessage
        from core.schema.part import TextPart

        def _u(text: str, mid: str) -> UserMessage:
            return UserMessage(content=[TextPart(content=text)], message_id=MessageId(mid))

        history = [_u("历史消息内容" * 20, f"u{i}") if i % 2 == 0
                   else AssistantMessage(content=[TextPart(content="回应内容" * 20)],
                                         message_id=MessageId(f"a{i}"))
                   for i in range(8)]
        builder = MessageBuilder()
        budget = 900

        without = builder.build_messages("系统提示", list(history), "当前消息",
                                         prompt_budget_tokens=budget, model_id=_MODEL)
        with_tools = builder.build_messages("系统提示", list(history), "当前消息",
                                            prompt_budget_tokens=budget,
                                            model_id=_MODEL, tools=_BIG_TOOLS)
        assert len(with_tools) < len(without), (
            f"tools 未参与预算：无工具留 {len(without)} 条、有工具留 {len(with_tools)} 条")
        # 两端不可丢：system 恒为 [0]，当前用户消息恒为最后一条
        assert with_tools[0]["role"] == "system"
        assert with_tools[-1]["content"] == "当前消息"

    def test_tools_never_change_a_prompt_that_has_no_budget(self):
        """不设预算时不裁剪（`prompt_budget_tokens=None` 语义不变）。"""
        from core.prompt.message_builder import MessageBuilder

        builder = MessageBuilder()
        messages = builder.build_messages("系统提示", [], "当前消息", model_id=_MODEL,
                                          tools=_BIG_TOOLS)
        assert [m["role"] for m in messages] == ["system", "user"]
