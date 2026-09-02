# core/prompt/tests/test_system_prompt_snapshot.py
"""12 段 System Prompt 现况快照测试。

锁定基线 commit e793a782 的渲染结果，供阶段 B 回归比对。
C5 含 datetime.now() 时间字段，快照比对前用 normalize_snapshot 归一化。
"""

import re
from pathlib import Path

import pytest

from core.prompt.tests.conftest import (  # noqa: F401
    cold_config, cold_engine_mock, elysia_config, make_assembler,
    mock_long_term, mock_session, render_system_prompt,
)

pytestmark = pytest.mark.service

_FIXTURE = Path(__file__).with_name("fixtures") / "system_prompt_snapshot_e793a782.txt"
_ELYSIA_FIXTURE = Path(__file__).with_name("fixtures") / "system_prompt_snapshot_elysia_cf4bc008.txt"


def normalize_snapshot(text: str) -> str:
    """去掉 C5 时间相关行，避免 datetime.now() 引起快照漂移。"""
    text = re.sub(r"当前时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "当前时间: <TIME>", text)
    text = re.sub(r"时段: (早晨|中午|下午|傍晚|夜晚)", "时段: <TOD>", text)
    text = re.sub(r"星期: (周一|周二|周三|周四|周五|周六|周日)", "星期: <DOW>", text)
    text = re.sub(r"季节: (春|夏|秋|冬)", "季节: <SEASON>", text)
    return text


@pytest.mark.asyncio
async def test_snapshot_matches_current(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """当前渲染结果与已入库快照一致（锁定现况）。"""
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    expected = _FIXTURE.read_text(encoding="utf-8")
    assert normalize_snapshot(rendered) == normalize_snapshot(expected)


@pytest.mark.asyncio
async def test_12_section_order(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """结果按 _RENDER_ORDER 顺序出现（只含确定渲染的 section）。

    注意：C9 self_perception（awareness_text 空）与 C8 own_life（session 空）
    在本配置下渲染为空，<self_perception>/<own_life> 标签不出现；
    C4 memory 因 mock_long_term.search 返回 [] 也为空。故只断言确定渲染的
    标签：conflict_rules→output_format→tts_markup→relationship→proactive→
    speaking_style→personality→emotion→sensory_input（Memory/C8/C9 略去）。
    """
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    ordered_tags = [
        "conflict_rules", "output_format", "tts_markup", "relationship",
        "proactive", "speaking_style", "personality",
        "emotion", "sensory_input",
    ]
    last = -1
    for tag in ordered_tags:
        pos = rendered.find(tag)
        assert pos > last, f"section order broken: {tag} not after previous (pos={pos}, last={last})"
        last = pos


@pytest.mark.asyncio
async def test_frame_wrappers_present(cold_config, cold_engine_mock, mock_long_term, mock_session):
    """关键 XML wrapper 标签都在（仅限确定渲染的 section）。

    不做 memory_bank/self_perception/own_life 断言——本配置下它们不渲染。
    """
    rendered = await render_system_prompt(make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session))
    for tag in ["conflict_rules", "output_format", "tts_markup", "relationship",
                "proactive", "speaking_style", "personality", "emotion",
                "sensory_input"]:
        assert tag in rendered


@pytest.mark.asyncio
async def test_elysia_snapshot_unchanged(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """Elysia 专属快照：现网 Elysia 渲染结果 byte-for-byte 不变。"""
    rendered = await render_system_prompt(
        make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    )
    expected = _ELYSIA_FIXTURE.read_text(encoding="utf-8")
    assert normalize_snapshot(rendered) == normalize_snapshot(expected)
