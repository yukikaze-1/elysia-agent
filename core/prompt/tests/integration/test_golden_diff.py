"""黄金对比测试：新旧 system prompt 输出逐字符一致。"""
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

# 路径常量
_OLD_BRAIN_J2 = _PROJECT_ROOT / "prompt" / "Brain.j2"
_NEW_BRAIN_DIR = _PROJECT_ROOT / "prompt" / "brain"


def _build_context():
    """构建与 PromptAssembler.build_system_prompt() 一致的 context。"""
    return {
        "sensory": {
            "current_time": "2024-01-01 12:00:00",
            "time_of_day": "下午",
            "day_of_week": "周一",
            "season": "冬",
            "perception": "妖梦说：Hello\n[当前心情: neutral]",
            "stt_emotion": "",
            "stt_emotion_emoji": "",
            "stt_events": "",
        },
        "memory": {"micro": [], "macro": []},
        "state": {"mood": "neutral"},
        "name": "Elysia",
        "user_name": "妖梦",
        "user_preferences": "",
        "self_perception": "",
    }


def test_golden_diff_default_profile():
    """default profile → 输出与旧 BrainSystemPrompt 逐字符一致。"""
    from Module.prompt import PromptManager
    from core.prompt.registry import BrainPromptRegistry

    ctx = _build_context()

    # 旧路径
    old_pm = PromptManager(str(_PROJECT_ROOT / "prompt"))
    old_result = old_pm.render_macro(
        "Brain.j2", "BrainSystemPrompt",
        sensory=ctx["sensory"],
        memory=ctx["memory"],
        state=ctx["state"],
        name=ctx["name"],
        user_name=ctx["user_name"],
        user_preferences=ctx["user_preferences"],
        self_perception=ctx["self_perception"],
    )

    # 新路径
    registry = BrainPromptRegistry(str(_NEW_BRAIN_DIR))
    new_result = registry.render(
        profile="default",
        context={
            "name": ctx["name"],
            "user_name": ctx["user_name"],
            "user_preferences": ctx["user_preferences"],
            "self_perception": ctx["self_perception"],
            "sensory_ctx": ctx["sensory"],
            "memory_ctx": ctx["memory"],
            "mood": ctx["state"]["mood"],
            "tool_list": "",
        },
    )

    assert old_result == new_result, (
        "新旧 system prompt 不一致！\n"
        "--- 旧 (Brain.j2) ---\n"
        f"{old_result}\n"
        "--- 新 (BrainPromptRegistry) ---\n"
        f"{new_result}"
    )


def test_golden_diff_with_memory_entries():
    """有记忆条目时输出一致。"""
    from datetime import datetime
    from Module.prompt import PromptManager
    from core.prompt.registry import BrainPromptRegistry

    now_ts = datetime(2024, 1, 1, 12, 0, 0).timestamp()
    ctx = _build_context()
    ctx["memory"] = {
        "micro": [
            {
                "content": "妖梦喜欢喝咖啡",
                "type": "fact",
                "timestamp": now_ts,
                "poignancy": 7,
            },
        ],
        "macro": [],
    }
    ctx["self_perception"] = "最近变得更愿意主动关心人了"

    # 旧
    old_pm = PromptManager(str(_PROJECT_ROOT / "prompt"))
    old_result = old_pm.render_macro(
        "Brain.j2", "BrainSystemPrompt",
        sensory=ctx["sensory"],
        memory=ctx["memory"],
        state=ctx["state"],
        name=ctx["name"],
        user_name=ctx["user_name"],
        user_preferences=ctx["user_preferences"],
        self_perception=ctx["self_perception"],
    )

    # 新
    registry = BrainPromptRegistry(str(_NEW_BRAIN_DIR))
    new_result = registry.render(
        profile="default",
        context={
            "name": ctx["name"],
            "user_name": ctx["user_name"],
            "user_preferences": ctx["user_preferences"],
            "self_perception": ctx["self_perception"],
            "sensory_ctx": ctx["sensory"],
            "memory_ctx": ctx["memory"],
            "mood": ctx["state"]["mood"],
            "tool_list": "",
        },
    )

    assert old_result == new_result, (
        "有记忆时的新旧输出不一致！\n"
        f"旧结果长度: {len(old_result)}, 新结果长度: {len(new_result)}"
    )
