"""S2-H9：prompt 预算与截断策略（唯一执行点 = core/prompt/budget.py + 两个截断点）。

缺陷形状：组装后的 prompt **没有任何总长/预算/截断**。超 context 不是被截断而是变成
provider **400**，而 400 不在可重试集合里 ⇒ 该会话对该角色**永久失败**，日志还只显示
`GENERATION_PROVIDER_UNAVAILABLE`。触发面：`save_memory`/`LongTermMemory.insert` 都不
校验长度，角色档案上限很大，空数据下 system prompt 已 ≈6.5K 字符。
"""

import pytest

from Module.system_prompt.budget import (
    CONTEXT_HEADROOM_RATIO,
    DEFAULT_CONTEXT_WINDOW,
    budget_from_context_window,
    effective_context_window,
    estimate_messages_tokens,
    estimate_tokens,
    system_prompt_budget,
)
from core.prompt.message_builder import MessageBuilder
from core.schema.message import AssistantMessage, MessageId, ToolResultMessage, UserMessage
from core.schema.part import TextPart
from core.schema.tool import ToolCall, ToolCallFunction

pytestmark = pytest.mark.unit


def _user(text: str, mid: str = "u") -> UserMessage:
    return UserMessage(content=[TextPart(content=text)], message_id=MessageId(mid))


def _assistant(text: str, mid: str = "a", tool_calls=None) -> AssistantMessage:
    return AssistantMessage(content=[TextPart(content=text)],
                            message_id=MessageId(mid), tool_calls=tool_calls)


def _tool_result(mid: str = "t") -> ToolResultMessage:
    return ToolResultMessage(content=[TextPart(content="工具结果" * 50)],
                             message_id=MessageId(mid), tool_call_id="tc_1",
                             tool_name="web_search")


def _call(tc_id: str = "tc_1") -> ToolCall:
    return ToolCall(id=tc_id, type="function",
                    function=ToolCallFunction(name="web_search", arguments="{}"))


class TestEstimation:
    """Batch 18 / KB-12：估算器是**有界近似**，按 resolved model 选 profile。

    旧的逐字公式断言（`"你好世界" == 4`、`"abcdefgh" == 2`）随实现一起作废——
    它们钉的是"字符启发式"本身，而那个启发式已被证明双向越界（自然中文高估 57%、
    JSON 低估 43%）。现在钉的是**合同**：全量语料的 est/actual 区间见
    `Module/system_prompt/tests/test_token_corpus_contract.py`。
    """

    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_nonempty_is_never_zero(self):
        """一个字符也必须 ≥1：0 会让"短输入"在预算里凭空消失。"""
        assert estimate_tokens("a") >= 1
        assert estimate_tokens("，", model="ollama/qwen2.5:3b") >= 1

    def test_cjk_dense_profile_matches_the_cjk_optimized_families(self):
        """qwen/deepseek 家族：自然中文实测 ≈0.57–0.64 token/字，不再按 1 字 1 token。"""
        assert estimate_tokens("你好世界", model="ollama/qwen2.5:3b") == 3
        assert estimate_tokens("你好世界", model="deepseek/deepseek-v4-flash") == 4
        # 未识别模型走保守 profile：同一串估得更高（不许比紧界更低）
        assert estimate_tokens("你好世界") > estimate_tokens(
            "你好世界", model="ollama/qwen2.5:3b")

    def test_profile_selection_has_one_authority(self):
        from Module.system_prompt.budget import profile_for_model

        assert profile_for_model("ollama/qwen2.5:3b") == "qwen"
        assert profile_for_model("deepseek/deepseek-v4-flash") == "deepseek"
        assert profile_for_model("ollama/llama3:8b") == "llama"
        assert profile_for_model("ollama/mistral:7b") == "mistral"
        assert profile_for_model("openai/gpt-4o") == "openai"
        # provider 段不参与判定：custom 里跑的 qwen 也该拿紧界
        assert profile_for_model("custom/qwen2.5-7b-instruct") == "qwen"
        for unknown in ("mimo/mimo-v2.5", "custom/whatever", None, ""):
            assert profile_for_model(unknown) == "generic"

    def test_dense_ascii_is_not_underestimated(self):
        """JSON/代码这类 token-dense 内容：低估 25% 以上就会击穿 0.75 余量。"""
        dense = '{"a":1,"b":[2,3],"c":{"d":true}}'
        assert estimate_tokens(dense) >= 10

    def test_emoji_is_not_quarter_token(self):
        """旧公式把 emoji 按 4 字符 1 token 计（实测低估 75%）。"""
        assert estimate_tokens("😀🎉🚀") >= 4

    def test_estimator_is_deterministic_and_read_only(self):
        text = "混合 mixed 内容 123 😀\n\t"
        first = estimate_tokens(text, model="ollama/qwen2.5:3b")
        assert [estimate_tokens(text, model="ollama/qwen2.5:3b") for _ in range(3)] == [first] * 3
        assert text == "混合 mixed 内容 123 😀\n\t"   # 估算不改输入

    def test_messages_estimation_counts_roles_and_overhead(self):
        msgs = [{"role": "system", "content": "你好"}, {"role": "user", "content": "在吗"}]
        assert estimate_messages_tokens(msgs) > 4

    def test_tool_definitions_are_never_invisible(self):
        """Batch 18：provider request = messages + tools，漏算 tools 是 false accept。"""
        from Module.system_prompt.budget import estimate_tools_tokens

        msgs = [{"role": "user", "content": "在吗"}]
        tools = [{"type": "function", "function": {
            "name": "web_search", "description": "执行一次网络搜索并返回摘要。",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "检索查询"}},
                "required": ["query"]}}}]
        without = estimate_messages_tokens(msgs, model="ollama/qwen2.5:3b")
        with_tools = estimate_messages_tokens(msgs, model="ollama/qwen2.5:3b", tools=tools)
        assert estimate_tools_tokens(tools, model="ollama/qwen2.5:3b") > 0
        assert with_tools > without
        # 空/None 不改变结果（向后兼容）
        assert estimate_tools_tokens(None) == 0
        assert estimate_tools_tokens([]) == 0
        assert estimate_messages_tokens(
            msgs, model="ollama/qwen2.5:3b", tools=None) == without

    def test_budget_uses_headroom_and_default(self):
        assert budget_from_context_window(1000) == int(1000 * CONTEXT_HEADROOM_RATIO)
        assert budget_from_context_window(None) == int(
            DEFAULT_CONTEXT_WINDOW * CONTEXT_HEADROOM_RATIO)

    def test_effective_window_is_the_shared_fallback(self):
        """窗口的**有效值**与预算必须同源：未声明/非法 → 保守默认。

        Batch 7（F8）抽出的共享回退点：预览的"是否装得下"本地校验按**窗口**判定，
        而预算是 `窗口×0.75`；若两处各写一份回退规则，一处改 16K、另一处还是 32K
        就会静默漂移。`0` 与负数属"没填/坏元数据"，同样回退默认值——负数若原样
        参与换算会得到退化的极小预算（裁到只剩骨架却不报错）。
        """
        assert effective_context_window(8192) == 8192
        assert effective_context_window(None) == DEFAULT_CONTEXT_WINDOW
        assert effective_context_window(0) == DEFAULT_CONTEXT_WINDOW
        assert effective_context_window(-5) == DEFAULT_CONTEXT_WINDOW
        assert budget_from_context_window(0) == budget_from_context_window(None)

    def test_system_prompt_share_is_a_fraction(self):
        assert 0 < system_prompt_budget(1000) < 1000

    def test_tool_pair_premise_is_pinned_elsewhere(self):
        """成对约束的**前提**由 TestToolTrafficPremise 钉住（此处仅留指针）。"""
        assert True


class TestRegistryContextWindow:
    def test_declared_provider_returns_window(self):
        from Module.llm.registry import LLMFactory

        assert LLMFactory.context_window("deepseek/deepseek-v4-pro") == 131072
        assert LLMFactory.context_window("ollama/qwen2.5:3b") == 8192

    def test_unknown_provider_returns_none(self):
        """未声明 → None，由调用方用保守默认（不是"无上限"）。"""
        from Module.llm.registry import LLMFactory

        assert LLMFactory.context_window("nope/model") is None


class TestHistoryTruncation:
    """超预算时由旧到新丢历史；system 与当前用户消息永不丢；成对单元不可拆。"""

    def _messages(self, *, budget):
        builder = MessageBuilder()
        history = [
            _user("很久以前说的话" * 60, mid="u1"),
            _assistant("很早以前的回答" * 60, mid="a1"),
            _user("刚才说的话", mid="u2"),
            _assistant("刚才的回答", mid="a2"),
        ]
        return builder.build_messages("系统提示", history, "现在这句",
                                      prompt_budget_tokens=budget)

    def test_within_budget_keeps_everything(self):
        """反空转：预算充裕时一条都不许丢。"""
        messages = self._messages(budget=100000)
        assert len(messages) == 6  # system + 4 条历史 + 当前
        assert messages[-1]["content"] == "现在这句"

    def test_over_budget_drops_oldest_first(self):
        """只丢到"装得下"为止：丢掉最旧那一条后即满足预算。

        算得出来的前提（Batch 18 重算：`generic` profile 下 420 汉字 ≈ 637 token）：
        整份 ≈1330 token，预算 900 ⇒ 丢 u1 后 ≈693 ≤ 900 → 停手（恰丢一条）。
        """
        messages = self._messages(budget=900)
        assert messages[0]["role"] == "system"
        assert messages[-1]["content"] == "现在这句"
        assert len(messages) == 5, f"只该丢最旧一条，实际剩 {len(messages)} 条"
        joined = " ".join(str(m.get("content")) for m in messages)
        assert "很久以前说的话" not in joined
        assert "刚才的回答" in joined

    def test_truncation_drops_one_message_at_a_time(self):
        """逐条丢（不是整段清空）：两条 420 字的历史，预算只够丢一条。

        总 ≈1330 token，预算 900 ⇒ 丢 u1（≈637）后 ≈693 即满足 ⇒ 必须**保住** a1。
        若实现改成"超预算就清空历史"，本用例红。
        """
        builder = MessageBuilder()
        history = [
            _user("很久以前说的话" * 60, mid="u1"),
            _assistant("很早以前的回答" * 60, mid="a1"),
            _user("刚才说的话", mid="u2"),
            _assistant("刚才的回答", mid="a2"),
        ]
        messages = builder.build_messages("系统提示", history, "现在这句",
                                          prompt_budget_tokens=900)
        joined = " ".join(str(m.get("content")) for m in messages)
        assert "很久以前说的话" not in joined
        assert "很早以前的回答" in joined, f"不该把历史清空：{messages!r}"

    def test_extreme_budget_keeps_system_and_current(self):
        """预算连近期历史都装不下：system 与当前消息仍在（不静默丢输入）。"""
        builder = MessageBuilder()
        history = [
            _user("很久以前" * 80, mid="u1"),
            _assistant("很早以前" * 80, mid="a1"),
            _user("刚才说的话也很长" * 40, mid="u2"),
            _assistant("刚才的回答也很长" * 40, mid="a2"),
        ]
        messages = builder.build_messages("系统提示", history, "现在这句",
                                          prompt_budget_tokens=200)
        assert messages[0]["role"] == "system"
        assert messages[-1]["content"] == "现在这句"
        assert len(messages) == 2, f"历史全应被丢弃，实际 {len(messages)} 条"

    def test_no_budget_means_no_truncation(self):
        """None = 不设预算（预览等调用点）——行为与修复前一致。"""
        builder = MessageBuilder()
        history = [_user("很长的话" * 200, mid="u1")]
        messages = builder.build_messages("系统", history, "当前")
        assert len(messages) == 3


class TestSystemPromptBudget:
    """system prompt 侧：先减 C4 记忆条数，再按顺序整节丢；保护节永不丢。"""

    @staticmethod
    def _sections(memory_text: str, owned_life: str = ""):
        from Module.system_prompt.protocol import PromptSection

        class _Static(PromptSection):
            def __init__(self, name: str, text: str) -> None:
                self.name = name
                self.version = "1"
                self.description = "test"
                self._text = text
                self.ctx: dict = {}

            def set_context(self, **kwargs):
                self.ctx.update(kwargs)

            async def render(self) -> str:
                return self._text

            def reload(self) -> None:
                pass

            def validate(self) -> bool:
                return True

        sections = [
            _Static("ConflictRules", "冲突规则" * 20),
            _Static("OutputFormat", "输出格式" * 20),
            _Static("OwnLife", owned_life),
            _Static("Memory", memory_text),
        ]
        return sections

    @pytest.mark.asyncio
    async def test_within_budget_keeps_all_sections(self):
        from Module.system_prompt.system_prompt_manager import SystemPromptManager

        manager = SystemPromptManager(self._sections("记忆" * 10, owned_life="她今天在读《xx》"))
        out = await manager.render(budget_tokens=100000)
        assert "记忆" in out and "她今天在读" in out

    @pytest.mark.asyncio
    async def test_protected_sections_survive_extreme_budget(self):
        """保护节（F1/F2）在极小预算下也必须完整保留——宁可不截也不丢身份/格式。"""
        from Module.system_prompt.system_prompt_manager import SystemPromptManager

        manager = SystemPromptManager(self._sections("记忆" * 500, owned_life="生活" * 200))
        out = await manager.render(budget_tokens=100)
        assert "冲突规则" in out
        assert "输出格式" in out

    @pytest.mark.asyncio
    async def test_droppable_section_goes_before_protected(self):
        """可丢节（OwnLife）先走：预算不够时丢它，保护节与记忆优先级更高。"""
        from Module.system_prompt.system_prompt_manager import SystemPromptManager

        manager = SystemPromptManager(self._sections("记得你说过喜欢猫", owned_life="生活" * 300))
        out = await manager.render(budget_tokens=300)
        assert "冲突规则" in out
        assert "生活生活" not in out

    @pytest.mark.asyncio
    async def test_no_budget_renders_everything(self):
        from Module.system_prompt.system_prompt_manager import SystemPromptManager

        manager = SystemPromptManager(self._sections("记忆" * 500, owned_life="生活" * 500))
        out = await manager.render()
        assert "记忆" in out and "生活" in out


class TestMemorySectionLimit:
    """C4 的逐条截断入口：`memory_limit` 从 5 逐级降到 0。"""

    @pytest.mark.asyncio
    async def test_limit_zero_renders_empty(self, tmp_path):
        from Module.system_prompt.sections.memory import MemorySection

        template = tmp_path / "memory.j2"
        template.write_text(
            "<memory>{% for mem in memory_ctx.micro %}{{ mem.content }}{% endfor %}</memory>",
            encoding="utf-8")

        class _LTM:
            calls: list[int] = []

            async def search(self, query_text: str, top_k: int = 5):
                _LTM.calls.append(top_k)

                class _E:
                    text = "记忆内容"
                    memory_type = "note"
                    timestamp = 0.0
                    poignancy = 5
                return [_E() for _ in range(top_k)]

        section = MemorySection(str(template), long_term=_LTM(), session=None)
        section.set_context(user_message="你好", memory_limit=0)
        assert await section.render() == ""

        section.set_context(user_message="你好", memory_limit=2)
        out = await section.render()
        assert out.count("记忆内容") == 2
        assert _LTM.calls[-1] == 2


class TestToolTrafficPremise:
    """钉住"历史侧不需要成对截断"这一前提（S2-H9 的负结论）。

    `_history_message_content` 对 `role="tool"` 返回 None、assistant 消息不带
    `tool_calls` 键——工具流量被汇总成 `"[已调用: web_search]"` 一行文本进 prompt。
    所以 messages 里不存在可拆的"调用/结果"对，逐条丢历史是安全的。

    **这条断言红了意味着前提变了**（有人开始发射工具流量）：那时必须实现成对截断
    （见 `core/prompt/budget.py` 模块 docstring 末节），而不是改这条用例。
    """

    def test_outbound_messages_carry_no_tool_traffic(self):
        builder = MessageBuilder()
        history = [
            _user("查一下", mid="u1"),
            _assistant("好", mid="a1", tool_calls=[_call()]),
            _tool_result("t1"),
        ]
        messages = builder.build_messages("sys", history, "现在")

        assert all(m.get("role") != "tool" for m in messages), (
            "开始发射 role=tool 了：截断必须成对（见 budget 模块 docstring）")
        assert all("tool_calls" not in m for m in messages), (
            "开始发射 tool_calls 了：截断必须成对（见 budget 模块 docstring）")
        # 工具调用以文本摘要形式保留（这是当前的设计，不是遗漏）
        assert "[已调用: web_search]" in str(messages[2]["content"])
