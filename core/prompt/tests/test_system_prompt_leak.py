# core/prompt/tests/test_system_prompt_leak.py
"""12 段 System Prompt 的 Elysia 泄漏基线测试（B-2 退出门禁）。

阶段 B-2 引入 per-role 模板后，泄漏测试按四组方向拆分（对应 spec §4.2 修正版）：

1. **Cyrene 角色风格词 `not in`**：`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/
   `今天不想振作也没有关系`——这些在共享 C2/C3 模板，而 Cyrene per-role 模板
   不含（去 Elysia 化），故 Cyrene 渲染 `not in`。
2. **共享格式词 `in`**：`♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢`——来自共享
   F2/F3 协议示例，Cyrene 也走共享 → `in`（非 Elysia 泄漏，B-1 Ruling 6）。
3. **cold 回退 `in`**：cold 无角色目录回退共享 C2/C3（含 Elysia 原文）→ `in`。
4. **Elysia 渲染 `in`**（byte-for-byte）：`五模式切换`/`明快` 等 → `in`。

渲染入口：PromptAssembler.build_system_prompt()（async），输入 cold/elysia/cyrene
config + mock 引擎。mood 从 engine.state.mood 读取（本批恒 neutral）。
"""

import pytest

from core.prompt.tests.conftest import (  # noqa: F401
    cold_config, cold_engine_mock, cyrene_config, elysia_config, make_assembler,
    mock_long_term, mock_session, render_system_prompt,
)
from core.prompt.tests.scenarios import SCENARIOS
from Module.system_prompt.sections.emotion import EmotionSection

pytestmark = pytest.mark.service


# ---------------------------------------------------------------------------
# 组 4：Elysia 渲染（byte-for-byte）——保留原断言 `in`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leak_tone_marker(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """C2/C6/F2 泄漏 ♪——冷角色渲染结果含 ♪（共享 F2 协议，回退底线）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "♪" in rendered


@pytest.mark.asyncio
async def test_leak_speaking_mode_structure(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """C6 泄漏五模式切换（Elysia 专属——冷角色 C6 消失后不裂）。"""
    rendered = await render_system_prompt(make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session))
    assert "五模式切换" in rendered


@pytest.mark.asyncio
async def test_leak_fixed_persona(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """C6 泄漏固定人设（明快/可爱的女孩子）。"""
    rendered = await render_system_prompt(make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session))
    assert "明快" in rendered
    assert "可爱的女孩子" in rendered


# ---------------------------------------------------------------------------
# 组 3：cold 回退 `in`（cold 无角色目录 → 回退共享 C2/C3，含 Elysia 原文）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leak_relationship_structure(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """cold 回退 C2 泄漏 Elysia 关系观——注意用实际渲染的词（普遍之爱/制造暧昧）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "普遍之爱" in rendered
    assert "制造暧昧" in rendered


@pytest.mark.asyncio
async def test_leak_relationship_stage(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """cold 回退 C2 泄漏关系阶段标签（初识）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "初识" in rendered


@pytest.mark.asyncio
async def test_leak_emotion_model(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """cold 回退 C3 泄漏固定应对话术与傲娇。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "今天不想振作也没有关系" in rendered
    assert "傲娇" in rendered


# ---------------------------------------------------------------------------
# 组 2：共享格式词 `in`（F2/F3 共享协议示例，非 Elysia 泄漏，B-1 Ruling 6）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leak_template_body_pronoun(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """模板正文泄漏她——user_pronoun=你 仍出现字面她（c2 第 18 行、f2/f3 示例）。

    注意：这不是 section 默认 her 泄漏——build_system_prompt 已把 user_pronoun
    正确覆盖为你。真正来源是模板正文里的字面她（共享格式词，非 Elysia 泄漏）。
    """
    # cold_config 的 user_pronoun 已是"你"
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "她" in rendered


@pytest.mark.asyncio
async def test_leak_output_format_example(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """F2 示例泄漏（诶嘿~被你发现啦）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    assert "诶嘿~被你发现啦" in rendered


@pytest.mark.asyncio
async def test_leak_tts_example(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """F3 minimax 分支示例泄漏她（她果然很聪明呢）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session),
                                          tts_provider="minimax")
    assert "她果然很聪明呢" in rendered


@pytest.mark.asyncio
async def test_section_default_pronoun():
    """section 默认 _user_pronoun=她 未被覆盖时泄漏。

    构造 EmotionSection 但不调 set_context(user_pronoun=...)，
    验证默认 her 会进入渲染结果。这是"section 默认值泄漏"的窄路径
    （区别于 test_leak_template_body_pronoun 的模板正文泄漏）。
    """
    from paths import ProjectPath
    s = EmotionSection(f"{ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()}/c3_emotion.j2")
    # 不调用 set_context(user_pronoun=...)——保持默认 her
    rendered = await s.render()
    assert "她" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id,user_message,stt_emotion,stt_events,tts_provider", SCENARIOS)
async def test_leak_parameterized_over_scenarios(
    scenario_id, user_message, stt_emotion, stt_events, tts_provider,
    cold_config, cold_engine_mock, mock_long_term, mock_session,
):
    """♪ 泄漏跨场景稳定复现（共享 F2 协议）。"""
    rendered = await render_system_prompt(
        make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session),
        user_message=user_message, stt_emotion=stt_emotion,
        stt_events=stt_events, tts_provider=tts_provider,
    )
    assert "♪" in rendered


# ---------------------------------------------------------------------------
# 组 1：Cyrene 角色风格词 `not in`（per-role 去 Elysia 化）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leak_relationship_structure_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C2 无 Elysia 关系观（per-role 去 Elysia 化）。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "普遍之爱" not in rendered
    assert "制造暧昧" not in rendered


@pytest.mark.asyncio
async def test_leak_relationship_stage_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C2 阶段标签不叫初识。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "初识" not in rendered


@pytest.mark.asyncio
async def test_leak_emotion_model_cyrene(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """Cyrene C3 无 Elysia 应对话术/傲娇。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session))
    assert "今天不想振作也没有关系" not in rendered
    assert "傲娇" not in rendered


@pytest.mark.asyncio
async def test_shared_format_words_remain(cyrene_config, cold_engine_mock, mock_long_term, mock_session):
    """共享格式词（♪/她/诶嘿/她果然）仍在 Cyrene 渲染中——F2/F3 共享协议。"""
    rendered = await render_system_prompt(make_assembler(cyrene_config, cold_engine_mock, mock_long_term, mock_session),
                                          tts_provider="minimax")
    assert "♪" in rendered
    assert "她" in rendered
    assert "诶嘿~被你发现啦" in rendered
    assert "她果然很聪明呢" in rendered
