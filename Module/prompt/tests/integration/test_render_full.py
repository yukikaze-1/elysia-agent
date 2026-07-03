"""完整 9 section 渲染集成测试。"""
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_BRAIN_DIR = str(_PROJECT_ROOT / "prompt" / "brain")


def _make_full_context():
    """构建完整的 context，覆盖所有 9 个 section 的参数。"""
    return {
        "name": "Elysia",
        "user_name": "妖梦",
        "user_preferences": "喜欢简短回复",
        "self_perception": "最近变得更温柔了",
        "sensory_ctx": {
            "current_time": "2024-06-15 14:30:00",
            "time_of_day": "下午",
            "day_of_week": "周六",
            "season": "夏",
            "perception": "妖梦说：今天天气真好\n[当前心情: happy]",
            "stt_emotion": "HAPPY",
            "stt_emotion_emoji": "😊",
            "stt_events": "Laughter 😀",
        },
        "memory_ctx": {
            "micro": [],
            "macro": [],
        },
        "mood": "warm",
        "tool_list": "submit_task, web_search, read_file",
    }


def test_default_profile_renders_all_sections():
    """default profile 渲染所有 9 个 section。"""
    from Module.prompt import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    result = registry.render(profile="default", context=_make_full_context())

    # identity
    assert "Elysia" in result
    assert "妖梦" in result
    assert "温柔" in result
    # self_perception
    assert "<self_perception>" in result
    assert "最近变得更温柔了" in result
    # relationship
    assert "你们很熟了" in result
    assert "喜欢简短回复" in result
    # sensory
    assert "<sensory_input>" in result
    assert "2024-06-15 14:30:00" in result
    assert "下午" in result
    assert "周六" in result
    assert "夏" in result
    assert "HAPPY" in result
    # memory（空记忆时 memory_bank 不出现）
    assert "<memory_bank>" not in result
    # current_state
    assert "<current_state>" in result
    assert "warm" in result
    # behavior
    assert "请求分类" in result
    assert "A 类" in result
    assert "submit_task" in result
    # voice
    assert "怎么回复" in result
    # output_format
    assert "<inner_voice>" in result
    assert "必须闭合" in result


def test_minimal_profile_only_three_sections():
    """minimal profile 仅 3 个 section：sensory + behavior + output_format。"""
    from Module.prompt import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    result = registry.render(profile="minimal", context=_make_full_context())

    # 有
    assert "sensory" in result or "<sensory_input>" in result
    assert "请求分类" in result
    assert "<inner_voice>" in result
    # 无 — identity 和 relationship 被跳过
    assert "Elysia" not in result
    # 无 — voice 被跳过
    assert "怎么回复" not in result
    # 无 — memory 为空，即使启用也不出现
    assert "<memory_bank>" not in result


def test_all_sections_ordered_correctly():
    """Section 按 metadata.yaml order 排列：identity → self_perception → relationship →
    sensory → memory → current_state → behavior → voice → output_format。"""
    from datetime import datetime
    from Module.prompt import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    ctx = _make_full_context()
    # 确保 self_perception 非空，memory 有数据，才能在输出中找到对应标记
    ctx["self_perception"] = "最近变得更愿意主动关心人了"
    ctx["memory_ctx"] = {
        "micro": [
            {
                "content": "妖梦喜欢喝咖啡",
                "type": "fact",
                "timestamp": datetime(2024, 6, 15, 10, 0, 0).timestamp(),
                "poignancy": 7,
            },
        ],
        "macro": [],
    }
    result = registry.render(profile="default", context=ctx)

    # 选取每个 section 的唯一标记
    pos_identity = result.find("你不是一个")
    pos_self_perception = result.find("<self_perception>")
    pos_relationship = result.find("你们很熟了")
    pos_sensory = result.find("<sensory_input>")
    pos_memory = result.find("<memory_bank>")
    pos_current_state = result.find("<current_state>")
    pos_behavior = result.find("请求分类")
    pos_voice = result.find("心里想的")
    pos_output = result.find("<inner_voice>")

    # 验证 9 个 section 按序排列
    assert pos_identity < pos_self_perception
    assert pos_self_perception < pos_relationship
    assert pos_relationship < pos_sensory
    assert pos_sensory < pos_memory
    assert pos_memory < pos_current_state
    assert pos_current_state < pos_behavior
    assert pos_behavior < pos_voice
    assert pos_voice < pos_output


def test_render_with_memory_entries_includes_memory_bank():
    """有记忆时 memory_bank 出现，且内容正确。"""
    from datetime import datetime
    from Module.prompt import BrainPromptRegistry

    registry = BrainPromptRegistry(_BRAIN_DIR)
    ctx = _make_full_context()
    ctx["memory_ctx"] = {
        "micro": [
            {
                "content": "妖梦喜欢喝咖啡",
                "type": "fact",
                "timestamp": datetime(2024, 6, 15, 10, 0, 0).timestamp(),
                "poignancy": 7,
            },
        ],
        "macro": [],
    }
    result = registry.render(profile="default", context=ctx)
    assert "<memory_bank>" in result
    assert "妖梦喜欢喝咖啡" in result
    assert "重要度: 7" in result
