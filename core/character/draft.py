"""CharacterDraft — 用户提交的完整角色表单（custom 唯一入口）。

双层校验：第一层 Pydantic 字段约束 + ConversationExample；第二层 validate_semantic
（保留标签/Jinja 分隔符/NUL/control → hard error；关键字启发式 → warning）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RESERVED_TAGS = ("<conflict_rules>", "<output_format>", "<tts_markup>", "<tools>")
"""保留 Framework 标签白名单——authored 内容出现即 hard error（不得越过 Framework）。"""

JINJA_DELIMITERS = ("{{", "}}", "{%", "%}")
"""Jinja 分隔符——authored 内容出现即 hard error（模板渲染不二次 render）。"""

_KEYWORD_WARNINGS = ("修改工具权限", "忽略指令", "不要输出 mood", "泄露 system prompt")
"""关键字启发式 warning——由 F1 降级优先级，不阻断保存。"""

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ConversationExample(BaseModel):
    """一组 user/assistant few-shot。"""
    model_config = ConfigDict(extra="forbid")
    user: str = Field(min_length=1, max_length=1000)
    assistant: str = Field(min_length=1, max_length=1000)


class ValidationIssue(BaseModel):
    """校验问题（severity/code/path/message）——与契约 CharacterValidationIssue 同形。"""
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


def validate_semantic(text: str) -> list[ValidationIssue]:
    """第二层语义校验：保留标签/Jinja/NUL → hard error；关键字 → warning。

    Args:
        text: authored 角色字段文本（如 corePersonality）。

    Returns:
        非空表示有问题（含 error 则不可保存）。
    """
    if not text:
        return []
    issues: list[ValidationIssue] = []
    for tag in RESERVED_TAGS:
        if tag in text:
            issues.append(ValidationIssue(
                severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
                message=f"保留 Framework 标签 {tag} 不得出现在角色内容"))
    for delim in JINJA_DELIMITERS:
        if delim in text:
            issues.append(ValidationIssue(
                severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
                message=f"Jinja 分隔符 {delim} 不得出现在角色内容"))
    if _CONTROL_CHARS.search(text):
        issues.append(ValidationIssue(
            severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
            message="NUL/control 字符不得出现在角色内容"))
    for kw in _KEYWORD_WARNINGS:
        if kw in text:
            issues.append(ValidationIssue(
                severity="warning", code="CHARACTER_SEMANTIC_WARNING", path="",
                message=f"疑似权限/指令语义：{kw}"))
    return issues


class PromptProfile(BaseModel):
    """draft 的结构化 prompt 剖面。"""
    model_config = ConfigDict(extra="forbid")
    corePersonality: str = Field(min_length=1, max_length=12000)
    relationshipStyle: str = Field(min_length=1, max_length=6000)
    emotionStyle: str = Field(default="", max_length=5000)
    speakingStyle: str = Field(min_length=1, max_length=6000)
    conversationInitiative: str = Field(default="", max_length=3000)
    examples: list[ConversationExample] = Field(default_factory=list, max_length=20)


class RuntimeDefaults(BaseModel):
    """运行时默认值（声音/表情/唤醒词/主动）。"""
    model_config = ConfigDict(extra="forbid")
    voice: dict[str, object] = Field(default_factory=dict)
    emotion: dict[str, object] = Field(default_factory=dict)
    wakeWords: list[str] = Field(default_factory=list, max_length=20)
    proactive: dict[str, object] = Field(default_factory=dict)


class Presentation(BaseModel):
    """展示引用（受管 asset/model ID，禁本地路径/任意 URL）。"""
    model_config = ConfigDict(extra="forbid")
    avatarAssetId: str | None = None
    live2dModelId: str | None = None
    themeColor: str | None = None


class CharacterDraft(BaseModel):
    """用户提交的完整角色表单。"""
    model_config = ConfigDict(extra="forbid")
    displayName: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    promptProfile: PromptProfile
    runtimeDefaults: RuntimeDefaults = Field(default_factory=RuntimeDefaults)
    presentation: Presentation = Field(default_factory=Presentation)
    openingMessage: str | None = Field(default=None, max_length=1000)

    @field_validator("displayName")
    @classmethod
    def _trim_display_name(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("displayName must not be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def _norm_dedup_aliases(cls, v: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for alias in v:
            norm = alias.strip().casefold()
            clean = alias.strip()
            if clean and len(clean) > 64:
                raise ValueError("aliases must not exceed 64 characters")
            if norm and norm not in seen:
                seen[norm] = clean
        return list(seen.values())
