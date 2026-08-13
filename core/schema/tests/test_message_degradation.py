"""validate_message_legacy_compat — 旧 token_usage 消息降级测试。"""

import pytest
from pydantic import ValidationError

from core.schema.adapters.message_adapter import validate_message_legacy_compat
from core.schema.message import AssistantMessage

pytestmark = pytest.mark.unit  # 无 marker 会被 make test-quick（-m "unit or service"）静默跳过


def _legacy_assistant() -> dict:
    """构造含旧格式 token_usage（prompt_tokens/completion_tokens）的 AssistantMessage dict。"""
    return {
        "role": "assistant",
        "content": [{"type": "text", "content": "你好"}],
        "message_id": "m1",
        "session_id": "default",
        "timestamp": "2026-07-01T00:00:00+00:00",
        "token_usage": {
            "rounds": [{
                "model": "deepseek/deepseek-chat",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "round_num": 0,
                "timestamp": 1780000000.0,
            }],
            "turn_total": 150,
        },
    }


def test_legacy_token_usage_stripped_message_preserved():
    """旧格式 token_usage 被剥除，消息正文保留。"""
    msg = validate_message_legacy_compat(_legacy_assistant())
    assert isinstance(msg, AssistantMessage)
    assert msg.content[0].content == "你好"
    assert msg.token_usage is None


def test_normal_message_passes_through():
    """无 token_usage 消息直接校验通过。"""
    d = _legacy_assistant()
    d["token_usage"] = None
    msg = validate_message_legacy_compat(d)
    assert msg.token_usage is None


def test_new_format_token_usage_passes():
    """新格式 token_usage 不触发剥除。"""
    d = _legacy_assistant()
    d["token_usage"] = {
        "rounds": [{
            "model": "deepseek/deepseek-chat",
            "session_id": "default",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "round_num": 0,
            "timestamp": "2026-07-01T00:00:00+00:00",
        }],
        "turn_total": 150,
    }
    msg = validate_message_legacy_compat(d)
    assert msg.token_usage is not None
    assert msg.token_usage.rounds[0].input_tokens == 100


def test_broken_message_raises_original_error():
    """token_usage 剥除后仍失败 → 抛原始异常（不吞掉）。"""
    d = _legacy_assistant()
    d["content"] = "not-a-list"  # content 类型错误，剥 token_usage 后仍失败
    with pytest.raises(ValidationError):
        validate_message_legacy_compat(d)


@pytest.mark.unit
class TestJsonlThreeSourceConsistency:
    def test_jsonl_legacy_empty_id_derives_same_as_archive(self):
        """同一工具消息经 archive 读侧派生与 JSONL 读侧派生产出同一稳定 ID（spec §4）。"""
        from core.schema.adapters.message_adapter import validate_message_legacy_compat

        doc = {
            "role": "tool", "message_id": "", "tool_call_id": "call_3src",
            "timestamp": "2026-08-13T00:00:00Z", "tool_name": "get_time",
            "content": [{"type": "text", "content": "ok"}],
        }
        archive_msg = validate_message_legacy_compat(doc)
        jsonl_msg = validate_message_legacy_compat({**doc})  # 同一 dict 经两读路径
        assert str(archive_msg.message_id) == str(jsonl_msg.message_id)
        assert str(jsonl_msg.message_id).startswith("msg_")
