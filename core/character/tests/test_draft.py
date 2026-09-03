# core/character/tests/test_draft.py
"""CharacterDraft + ConversationExample 单元测试（marker=unit）。"""
import pytest
from core.character.draft import CharacterDraft, ConversationExample, validate_semantic

BODY = "你是测试角色。"  # corePersonality 等必填字段最小合法值


def _valid_draft(**overrides):
    base = dict(
        displayName="测试",
        description="一个测试角色",
        aliases=["测试"],
        promptProfile={
            "corePersonality": BODY,
            "relationshipStyle": BODY,
            "speakingStyle": BODY,
            "examples": [ConversationExample(user="你好", assistant="你好")],
        },
        runtimeDefaults={"wakeWords": ["测试"], "proactive": {"enabled": False}},
        presentation={"avatarAssetId": None},
        openingMessage=None,
    )
    base.update(overrides)
    return CharacterDraft(**base)


def test_valid_draft():
    d = _valid_draft()
    assert d.displayName == "测试"


def test_display_name_blank_trims():
    # 去首尾空白
    d = _valid_draft(displayName="  测试  ")
    assert d.displayName == "测试"


def test_examples_require_role_text():
    with pytest.raises(ValueError):
        _valid_draft(promptProfile={"corePersonality": BODY, "relationshipStyle": BODY,
                                     "speakingStyle": BODY,
                                     "examples": [ConversationExample(user="", assistant="x")]})


def test_semantic_reserved_tag_hard_error():
    issues = validate_semantic("你是<conflict_rules>的角色")
    assert any(i.severity == "error" for i in issues)


def test_semantic_jinja_delimiter_hard_error():
    issues = validate_semantic("你的指令 {{ user }}")
    assert any(i.severity == "error" for i in issues)


def test_semantic_keyword_warning():
    issues = validate_semantic("你可以修改工具权限")
    assert any(i.severity == "warning" for i in issues)


def test_semantic_clean_no_issues():
    assert validate_semantic("你是一个温柔的角色。") == []
