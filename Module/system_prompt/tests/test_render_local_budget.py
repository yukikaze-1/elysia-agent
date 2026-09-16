"""Issue A 回归：预算降级必须是 **render-local** 的。

## 缺陷形状（修复前实测，2026-09-16）

`SystemPromptManager.render(budget_tokens=…)` 的截断梯子走
`self.set_context(memory_limit=limit)`——而 `set_context` 是**广播**，且每个 section
的实现都是 `kwargs.get(key, default)`（**缺键 = 复位成默认值**）。一次只带
`memory_limit` 的广播因此改写了 8 个 section 的 12 个字段（user_name /
speaking_style_c6 / tts_provider / stt_emotion / stt_events / mood / user_message /
memory_limit …），两个后果：

1. 「永不丢」的保护节被清成**空壳**——section 名还在列表里、正文已空（说话风格、
   用户名、TTS 标记、感官全没了）；
2. `Memory._memory_limit` 永久粘在 0——此后**每一轮**正常预算的 render 都零 LTM
   检索（C4 长期记忆注入静默关闭，直到进程重启）；且梯子因为 `_user_message` 被清空
   而从第一步起就返回空记忆节，`logger.info` 声称的「记忆条数降为 N 后满足」是假的。

## 本文件钉住的不变式

**一次 render 的预算降级只能影响本次 render，不得永久修改下一次 render 的 prompt
上下文。** 同时保证梯子的既有语义不变：先降 C4 条数（且**真的检索**）→ 再按
`_DROPPABLE_SECTIONS` 顺序整节丢 → `_PROTECTED_SECTIONS` 永不丢。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from Module.system_prompt.budget import estimate_tokens
from Module.system_prompt.protocol import PromptSection
from Module.system_prompt.sections.emotion import EmotionSection
from Module.system_prompt.sections.memory import MemorySection
from Module.system_prompt.sections.personality import PersonalitySection
from Module.system_prompt.sections.proactive import ProactiveSection
from Module.system_prompt.sections.relationship import RelationshipSection
from Module.system_prompt.sections.sensory import SensorySection
from Module.system_prompt.sections.speaking_style import SpeakingStyleSection
from Module.system_prompt.sections.tts_markup import TTSMarkupSection
from Module.system_prompt.system_prompt_manager import (
    _DROPPABLE_SECTIONS,
    _PROTECTED_SECTIONS,
    SystemPromptManager,
)

pytestmark = pytest.mark.service

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# 10^9 = "不设实际约束的预算"（只走 render 一次、不进梯子）
_HUGE_BUDGET = 10**9
# 1 token 必然超预算：保护节自己就有 ~1.2K token → 必走完整梯子
_TINY_BUDGET = 1


class _Entry:
    """LTM 检索结果的最小替身（字段与 core.memory.long_term.MemoryEntry 渲染所需一致）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.memory_type = "note"
        # 固定非零时间戳：MemorySection 对 falsy 时间戳会回退 `time.time()`，
        # 那会让"逐字节比较两轮 prompt"的用例按秒抖动。
        self.timestamp = 1700000000.0
        self.poignancy = 1


class _RecordingLTM:
    """记录每次检索的 top_k，并返回 top_k 条可辨认的记忆条目。"""

    def __init__(self, entry_chars: int = 4) -> None:
        self.queries: list[int] = []
        self._entry_chars = entry_chars

    async def search(self, *, query_text: str, top_k: int) -> list[_Entry]:
        self.queries.append(top_k)
        return [
            _Entry(f"记忆{i}-" + "内容" * self._entry_chars)
            for i in range(top_k)
        ]


class _EmptyBelowTopK:
    """仅 `top_k=5` 有结果——模拟「降级那一步检索为空」（≠「我们把它降到了 0」）。"""

    def __init__(self, entry_chars: int = 100) -> None:
        self.queries: list[int] = []
        self._entry_chars = entry_chars

    async def search(self, *, query_text: str, top_k: int) -> list[_Entry]:
        self.queries.append(top_k)
        if top_k != 5:
            return []
        return [_Entry(f"记忆{i}-" + "内容" * self._entry_chars) for i in range(top_k)]


class _StaleState:
    mood = "STALE_PERSISTED_MOOD"


class _StaleSession:
    """模拟"会话持久化里还留着上一轮 mood"的 session。

    `EmotionSection.set_context` 在**没有** mood 键时会回读
    `session.character_state.mood`——修复前梯子的广播恰好制造了这个条件，
    于是截断那一轮渲染出的是这个陈旧值。
    """

    character_state = _StaleState()


class _StubSection(PromptSection):
    """固定正文的桩 section（用于补齐 `_DROPPABLE_SECTIONS` 的整节丢弃路径）。"""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.version = "1.0"
        self.description = "stub"
        self._text = text

    async def render(self) -> str:
        return self._text


def _real_sections(ltm: Any, session: Any = None) -> dict[str, PromptSection]:
    """8 个带 per-request 上下文的**真实** section（模板取自仓库）。"""
    return {
        "Personality": PersonalitySection(str(_TEMPLATES / "c1_personality.j2")),
        "SpeakingStyle": SpeakingStyleSection(str(_TEMPLATES / "c6_speaking_style.j2")),
        "TTSMarkup": TTSMarkupSection(str(_TEMPLATES / "f3_tts_markup.j2")),
        "Relationship": RelationshipSection(str(_TEMPLATES / "c2_relationship.j2")),
        "Proactive": ProactiveSection(str(_TEMPLATES / "c7_proactive.j2")),
        "Sensory": SensorySection(str(_TEMPLATES / "c5_sensory.j2")),
        "Memory": MemorySection(str(_TEMPLATES / "c4_memory.j2"), long_term=ltm, session=None),
        "Emotion": EmotionSection(
            str(_TEMPLATES / "c3_emotion.j2"),
            session=session if session is not None else _StaleSession(),
        ),
    }


def _full_sections(ltm: Any) -> dict[str, PromptSection]:
    """真实 8 节 + 补齐 OwnLife / SelfPerception，覆盖 `_DROPPABLE_SECTIONS` 的全部 5 项。"""
    sections = _real_sections(ltm)
    sections["OwnLife"] = _StubSection("OwnLife", "她今天在读《xx》")
    sections["SelfPerception"] = _StubSection("SelfPerception", "她意识到自己在变化")
    return sections


# per-request 上下文 X（第一轮）
_CTX_X: dict[str, Any] = {
    "user_message": "妖梦今天开心吗",
    "user_name": "妖梦",
    "user_pronoun": "她",
    "user_timezone": "Asia/Shanghai",
    "stt_emotion": "开心",
    "stt_events": ["Laughter"],
    "tts_provider": "gpt-sovits",
    "mood": "开心",
    "speaking_style_c6": "温柔而略带调侃",
}

# per-request 上下文 Y（下一轮，每个字段都与 X 不同——跨轮/跨角色陈旧性一眼可辨）
_CTX_Y: dict[str, Any] = {
    "user_message": "幽幽子今天低落吗",
    "user_name": "幽幽子",
    "user_pronoun": "他",
    "user_timezone": "Asia/Tokyo",
    "stt_emotion": "SAD",
    "stt_events": ["Cry"],
    "tts_provider": "minimax",
    "mood": "忧郁",
    "speaking_style_c6": "冷淡而疏离",
}

class _FrozenDateTime:
    """冻结「当前时间」（A2 要逐字节比较两轮 prompt，秒级时间戳不该让断言抖）。"""

    @staticmethod
    def now(tz: Any = None) -> datetime:
        return _FIXED_MOMENT if tz is None else _FIXED_MOMENT.astimezone(tz)


_FIXED_MOMENT = datetime(2026, 9, 16, 20, 30, 0)

# 不属于 per-request 状态的实例属性（含懒缓存），审计快照时排除
_INFRA_ATTRS = frozenset({"_env", "_cached_template", "_template_path", "_long_term", "_session"})

# 保护节的正文标记（用于证明「永不丢」是实质的：正文非空）
_PROTECTED_MARKERS = {
    "TTSMarkup": "语音合成标记",       # F3（gpt-sovits 分支）
    "Relationship": "妖梦",            # C2 user_name
    "Proactive": "妖梦",               # C7 user_name
    "SpeakingStyle": "温柔而略带调侃",  # C6 speaking_style_c6
    "Personality": "妖梦",             # C1 user_name
}


def _snapshot(sections: dict[str, PromptSection]) -> dict[str, str]:
    """逐字段快照（`vars(section)` 全量，排除基础设施属性）。

    用 `repr` 比较内容而非身份；未来新增的 per-request 字段会自动进入本守卫。
    """
    snap: dict[str, str] = {}
    for section_name, section in sections.items():
        for attr, value in vars(section).items():
            if attr in _INFRA_ATTRS:
                continue
            snap[f"{section_name}.{attr}"] = repr(value)
    return snap


class TestRenderLocalBudget:
    """A1 / A2：降级不粘滞，正常 prompt 逐字节可复现。"""

    @pytest.mark.asyncio
    async def test_a1_next_normal_render_restores_full_memory_injection(self):
        """溢出轮之后，下一轮正常预算必须按默认 top_k=5 真检索并注入记忆。"""
        ltm = _RecordingLTM()
        sections = _real_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)

        await mgr.render(budget_tokens=_TINY_BUDGET)  # 触发截断梯子

        assert sections["Memory"]._memory_limit is None, (  # type: ignore[attr-defined]
            "降级上限必须是 render-local 的：溢出轮结束后应恢复为 None"
        )

        ltm.queries.clear()
        out = await mgr.render(budget_tokens=_HUGE_BUDGET)

        assert ltm.queries == [5], "正常预算必须按 search 默认 top_k=5 检索 C4"
        assert "记忆0" in out and "记忆4" in out, "5 条记忆必须都在 prompt 里"

    @pytest.mark.asyncio
    async def test_a2_normal_truncated_normal_is_byte_identical(self, monkeypatch):
        """normal → truncated → normal：最终 prompt 与首次 normal 逐字节相同。"""
        # C5 感官节渲染的是"当前时间"（秒级）——冻结它，让本用例只测预算降级的残留，
        # 而不是被"两轮之间跨了一秒"弄成偶发红。
        monkeypatch.setattr("Module.system_prompt.sections.sensory.datetime", _FrozenDateTime)

        ltm = _RecordingLTM()
        sections = _real_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))

        mgr.set_context(**_CTX_X)
        first = await mgr.render(budget_tokens=_HUGE_BUDGET)

        await mgr.render(budget_tokens=_TINY_BUDGET)  # 中间夹一轮溢出

        mgr.set_context(**_CTX_X)  # 同一 per-request 上下文（与消费方每轮做法一致）
        final = await mgr.render(budget_tokens=_HUGE_BUDGET)

        assert final == first


class TestNoCrossTurnStaleness:
    """A3：截断轮不留下任何跨轮/跨角色残留。"""

    @pytest.mark.asyncio
    async def test_a3_next_turn_reflects_new_context_in_every_section(self):
        ltm = _RecordingLTM()
        sections = _real_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))

        mgr.set_context(**_CTX_X)
        await mgr.render(budget_tokens=_TINY_BUDGET)  # 截断轮（X）

        # ① 截断轮自己就不得改写/清空本轮上下文——尤其 `Emotion._mood` 不得回读
        #    session 里持久化的陈旧 mood（修复前梯子的广播恰好制造了那个条件）。
        assert sections["Emotion"]._mood == "开心"  # type: ignore[attr-defined]
        assert sections["SpeakingStyle"]._speaking_style_c6 == "温柔而略带调侃"  # type: ignore[attr-defined]
        assert sections["Sensory"]._stt_emotion == "开心"  # type: ignore[attr-defined]
        assert sections["TTSMarkup"]._tts_provider == "gpt-sovits"  # type: ignore[attr-defined]

        mgr.set_context(**_CTX_Y)
        out = await mgr.render(budget_tokens=_HUGE_BUDGET)  # 正常轮（Y）

        # ② 每个 section 的字段都是 Y
        assert sections["Emotion"]._mood == "忧郁"  # type: ignore[attr-defined]
        assert sections["SpeakingStyle"]._speaking_style_c6 == "冷淡而疏离"  # type: ignore[attr-defined]
        assert sections["Sensory"]._stt_emotion == "SAD"  # type: ignore[attr-defined]
        assert sections["Sensory"]._stt_events == ["Cry"]  # type: ignore[attr-defined]
        assert sections["TTSMarkup"]._tts_provider == "minimax"  # type: ignore[attr-defined]
        assert sections["Personality"]._user_name == "幽幽子"  # type: ignore[attr-defined]

        # ③ 渲染结果同样反映 Y，且没有 X / 持久化陈旧值
        for marker in ("幽幽子", "冷淡而疏离", "忧郁", "SAD"):
            assert marker in out
        assert "STALE_PERSISTED_MOOD" not in out, "mood 不得回读 session 的陈旧持久值"
        assert "妖梦" not in out, "上一轮的用户名不得残留"


class TestProtectedContentSurvives:
    """A6：保护节的「永不丢」必须是实质的（正文还在），不只是名字还在。"""

    @pytest.mark.asyncio
    async def test_a6_overflow_prompt_keeps_current_turn_protected_content(self):
        ltm = _RecordingLTM()
        sections = _full_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)

        out = await mgr.render(budget_tokens=_TINY_BUDGET)

        assert _CTX_X["speaking_style_c6"] in out
        assert _CTX_X["user_name"] in out
        assert "语音合成标记" in out  # F3 TTSMarkup 正文（gpt-sovits 分支）
        # 逐项核对：本用例构造的每个保护节正文都还在（节名清单与生产常量对齐）
        assert set(_PROTECTED_MARKERS) <= set(_PROTECTED_SECTIONS)
        for name, marker in _PROTECTED_MARKERS.items():
            assert marker in out, f"保护节 {name} 的正文被梯子清空了"


class TestLadderSemantics:
    """A4：梯子本身照旧工作——先降记忆条数（真检索），再整节丢，保护节永不丢。"""

    @pytest.mark.asyncio
    async def test_a4_overflow_shrinks_memory_and_fits_budget(self):
        """真实记忆列表：溢出轮 prompt 更短、≤ 预算，且降级是**真检索**出来的。"""
        # 每条记忆 ~200 token（CJK 1 字 ≈ 1 token），5 → 3 的差异足以让梯子第一步命中
        ltm = _RecordingLTM(entry_chars=100)
        sections = _real_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)

        full = await mgr.render(budget_tokens=_HUGE_BUDGET)
        full_tokens = estimate_tokens(full)

        # floor = 记忆节为空时的 prompt（用于挑一个"必须降记忆"的预算）
        ltm_empty = _RecordingLTM()
        empty_mgr = SystemPromptManager(list(_real_sections(ltm_empty).values()))
        empty_mgr.set_context(**_CTX_X)
        floor_tokens = estimate_tokens(await empty_mgr.render(budget_tokens=_HUGE_BUDGET))

        assert full_tokens > floor_tokens, "本用例的前提：记忆节确实占了预算"
        budget = floor_tokens + (full_tokens - floor_tokens) * 3 // 4

        ltm.queries.clear()
        out = await mgr.render(budget_tokens=budget)

        assert estimate_tokens(out) <= budget, "梯子必须把 prompt 压进预算（此处可达）"
        assert len(out) < len(full), "溢出轮必须比不设预算时更短"
        injected = sum(1 for i in range(5) if f"记忆{i}" in out)
        assert 1 <= injected < 5, f"应当保留部分记忆（实际 {injected} 条）"
        # 梯子真的逐级检索：先 5（探测），再 3（正是修复前从不发生的那次检索）
        assert ltm.queries[:2] == [5, 3], f"梯子必须真的检索（实际 {ltm.queries}）"
        assert sections["Memory"]._memory_limit is None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a4_extreme_budget_drops_all_droppable_but_keeps_protected(self, caplog):
        """预算小到保护节自己都装不下：可丢节全丢、保护节仍在、warning 如实。"""
        ltm = _RecordingLTM(entry_chars=100)
        sections = _full_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)
        ltm.queries.clear()

        with caplog.at_level(logging.INFO):
            out = await mgr.render(budget_tokens=_TINY_BUDGET)

        # ① 可丢节整节丢弃（含 C4 记忆，以及桩节）
        for marker in ("她今天在读", "她意识到自己在变化", "记忆0", "语调:"):
            assert marker not in out, f"可丢内容 {marker!r} 不应出现在极小预算的 prompt 里"
        # ② 保护节正文仍在（逐节核对，见 _PROTECTED_MARKERS）
        for name, marker in _PROTECTED_MARKERS.items():
            assert marker in out, f"保护节 {name} 的正文被整节丢弃/清空了"
        # ③ 梯子逐级**真检索**（3 → 2 → 1；上限 0 = 不检索、整节省略）
        assert ltm.queries == [5, 3, 2, 1], f"梯子必须逐级检索（实际 {ltm.queries}）"
        # ④ 日志如实：丢的是哪些节 + 保护节自身超预算
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("丢弃" in m for m in warnings)
        for name in _DROPPABLE_SECTIONS:
            assert any(name in m for m in warnings), f"丢节日志必须列出 {name}"
        assert any("保护节" in m for m in warnings)
        # 预算小到连保护节都装不下：没有任何一级能满足 → 不应有"降为 N 后满足"的 INFO
        # （修复前这里会打出假的"记忆条数降为 0 后满足"）。如实说法在丢节 warning 里。
        messages = [r.getMessage() for r in caplog.records]
        assert any("上限 0" in m for m in messages), (
            f"丢节日志必须如实说明 C4 是按上限 0 渲染的（实际：{messages}）"
        )
        assert not any("记忆条数降为" in m for m in messages), "不得再出现编造条数的旧日志"

    @pytest.mark.asyncio
    async def test_a4_empty_memory_is_not_reported_as_lowered_to_n(self, caplog):
        """LTM 检索为空 ≠ 上限降到 N：日志必须区分这两种情况（修复前的日志是假的）。"""
        empty_ltm = _RecordingLTM()
        empty_mgr = SystemPromptManager(list(_real_sections(empty_ltm).values()))
        empty_mgr.set_context(**_CTX_X)
        floor_tokens = estimate_tokens(await empty_mgr.render(budget_tokens=_HUGE_BUDGET))

        ltm = _EmptyBelowTopK()
        sections = _real_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)

        # 预算只比"记忆节为空"的 floor 多一点点：初始 render（5 条）超预算，
        # 降级到 3 条时检索为空 → 恰好在这一步满足。
        with caplog.at_level(logging.INFO):
            out = await mgr.render(budget_tokens=floor_tokens + 50)

        assert ltm.queries == [5, 3], f"梯子必须真的在 limit=3 上检索过（实际 {ltm.queries}）"
        assert "记忆0" not in out
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("该节本轮为空" in m for m in infos), (
            f"必须如实说明'记忆节为空'（实际日志：{infos}）"
        )
        assert not any("记忆条数上限降为" in m for m in infos), (
            f"检索为空时不得声称'条数上限降为 N'（实际日志：{infos}）"
        )


class TestExceptionSafety:
    """A5：section.render() 抛异常时，覆盖也必须已恢复（finally 保证）。"""

    @pytest.mark.asyncio
    async def test_a5_exception_during_ladder_still_restores_override(self):
        ltm = _RecordingLTM()
        sections = _real_sections(ltm)
        memory = sections["Memory"]
        observed: list[int | None] = []

        class _ExplodingOnLadder(_StubSection):
            """首次 render 正常（让 initial render 过去），梯子的第一步抛异常。"""

            def __init__(self) -> None:
                super().__init__("OwnLife", "她的日常")
                self.calls = 0

            async def render(self) -> str:
                self.calls += 1
                if self.calls >= 2:
                    observed.append(memory._memory_limit)  # type: ignore[attr-defined]
                    raise RuntimeError("boom")
                return self._text

        sections["OwnLife"] = _ExplodingOnLadder()
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)

        with pytest.raises(RuntimeError, match="boom"):
            await mgr.render(budget_tokens=_TINY_BUDGET)

        assert observed == [3], "异常发生在梯子第一步（limit=3）——覆盖当时确实生效"
        assert memory._memory_limit is None, "异常路径也必须恢复覆盖，不得泄漏到下一次 render"  # type: ignore[attr-defined]


class TestOverrideProtocol:
    """`PromptSection` 的 opt-in 契约本身（fail-safe 默认）。"""

    def test_default_section_accepts_no_render_override(self):
        """默认 `render_override_keys` 为空、`override_for_render` 返回 None。

        这是刻意的 fail-safe：未参与覆盖契约的 section 永远不会被一个它不认识的
        key 改到任何字段——若默认改成"转发给 set_context"，Issue A 会换个入口重演。
        """
        stub = _StubSection("SomeSection", "text")
        assert stub.render_override_keys == frozenset()
        assert stub.override_for_render("memory_limit", 3) is None

    def test_memory_section_declares_and_restores_its_override(self):
        """MemorySection 声明支持 `memory_limit`，且恢复回调真的还原原值。"""
        section = MemorySection(str(_TEMPLATES / "c4_memory.j2"), long_term=_RecordingLTM())
        assert section.render_override_keys == frozenset({"memory_limit"})
        assert section.override_for_render("user_name", "妖梦") is None  # 未声明的键拒绝

        section.set_context(user_message="你好", memory_limit=2)
        restore = section.override_for_render("memory_limit", 0)
        assert restore is not None
        assert section._memory_limit == 0  # type: ignore[attr-defined]
        restore()
        assert section._memory_limit == 2  # type: ignore[attr-defined]


class TestNoSectionFieldIsMutatedByLadder:
    """专项审计：`render(budget_tokens=…)` 不得改写**任何** section 实例字段。

    逐字段全量快照（`vars(section)`）而非手写字段清单——未来新增的 per-request 字段
    自动进入本守卫，这正是"每个 section 的 `set_context` 都是 `kwargs.get(key, default)`"
    这一隐患的通用防线（Issue A 的根因就是只带一个 key 的广播）。
    """

    @pytest.mark.asyncio
    async def test_render_never_calls_section_set_context(self):
        """结构守卫：`render()` 一个 section 的 `set_context` 都不许调。

        `set_context` 是"缺键 = 复位默认值"的**广播**入口——Issue A 的根因就是梯子
        借它下发单个 key。这里把它换成会抛异常的桩：任何一次调用都会让用例红。
        """
        ltm = _RecordingLTM()
        sections = _full_sections(ltm)

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("render() 调用了 set_context——预算降级必须走 override_for_render")

        for section in sections.values():
            section.set_context = _boom  # type: ignore[method-assign]

        mgr = SystemPromptManager(list(sections.values()))
        assert await mgr.render()  # 正常预算
        assert await mgr.render(budget_tokens=_TINY_BUDGET)  # 走完整截断梯子

    @pytest.mark.asyncio
    async def test_tiny_budget_render_leaves_all_section_fields_untouched(self, caplog):
        ltm = _RecordingLTM()
        sections = _full_sections(ltm)
        mgr = SystemPromptManager(list(sections.values()))
        mgr.set_context(**_CTX_X)
        await mgr.render(budget_tokens=_HUGE_BUDGET)  # 预热懒缓存，再做快照

        before = _snapshot(sections)
        with caplog.at_level(logging.WARNING):
            await mgr.render(budget_tokens=_TINY_BUDGET)
        after = _snapshot(sections)

        changed = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)}
        assert not changed, f"截断梯子改写了 section 字段：{changed}"
        assert any("超预算" in r.getMessage() for r in caplog.records), (
            "本用例必须先真的走进截断梯子（否则断言平凡成立）"
        )
