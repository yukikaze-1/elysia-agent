"""TokenUsage 权威模型测试。"""

import pytest
from pydantic import ValidationError

from core.schema.token import TokenUsage, TokenUsageSummary
from core.schema.types import SessionId

pytestmark = pytest.mark.unit  # 无 marker 会被 make test-quick（-m "unit or service"）静默跳过


def test_from_api_full_keys():
    u = TokenUsage.from_api(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 30,
            "reasoning_tokens": 10,
        },
        model="deepseek/deepseek-chat",
        session_id=SessionId("default"),
        round_num=1,
    )
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.total_tokens == 150
    assert u.cached_tokens == 30
    assert u.reasoning_tokens == 10
    assert u.model == "deepseek/deepseek-chat"
    assert u.session_id == "default"
    assert u.round_num == 1


def test_from_api_optional_keys_omitted():
    """可选键缺失（provider 未返回）→ 对应字段 None。"""
    u = TokenUsage.from_api(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        model="ollama/llama3",
        session_id=SessionId("default"),
        round_num=0,
    )
    assert u.cached_tokens is None
    assert u.reasoning_tokens is None


def test_from_api_missing_required_key_raises():
    """必填键缺失 → ValidationError。"""
    with pytest.raises(ValidationError):
        TokenUsage.from_api(
            {"input_tokens": 10, "total_tokens": 15},
            model="ollama/llama3",
            session_id=SessionId("default"),
            round_num=0,
        )


def test_timestamp_accepts_legacy_float():
    """timestamp 兼容旧 float Unix 输入（IsoTimestamp 归一化）。"""
    u = TokenUsage(
        model="m",
        session_id=SessionId("default"),
        round_num=0,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        timestamp=1780000000.0,
    )
    assert isinstance(u.timestamp, str)
    assert u.timestamp.startswith("2026-")


def test_summary_rounds_accept_token_usage():
    s = TokenUsageSummary(
        rounds=[TokenUsage.from_api(
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            model="m", session_id=SessionId("default"), round_num=0,
        )],
        turn_total=2,
    )
    assert s.rounds[0].total_tokens == 2
