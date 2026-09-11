"""CompressionManager._refresh_c6 首刷读 character.speaking_style（阶段 B-2）。

Cyrene 的 _refresh_c6 首刷（state.speaking_style_c6 == ""）应读
character_config.speaking_style 而非 speaking_style 模块的 C6_L1_DEFAULT 常量，
防止 Cyrene L2→C6 被 Elysia 常量污染。

实现说明：_refresh_c6 内对 SpeakingStyleSection 的导入是函数级本地
（`from Module.system_prompt.sections.speaking_style import SpeakingStyleSection`），
因此必须 patch 该 section 模块路径上的类（模块级替换），patch
compression.SpeakingStyleSection 无效。本测试用模块级替换 + 捕获构造参数。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import Module.system_prompt.sections.speaking_style as ss_mod
from core.config.character_config import CharacterConfig
from core.personality.compression import CompressionManager
from Module.system_prompt import PromptManager
from paths import ProjectPath

# Cyrene 角色配置绝对路径（基于 ProjectPath 项目根，不依赖 cwd）
_CYRENE_CONFIG = ProjectPath.CHARACTERS.resolve() / "cyrene" / "config.yaml"

# CompressionManager 单模块测试——用 AsyncMock 替身驱动 LLM，不启动 Agent、不连真实 DB。
pytestmark = pytest.mark.service


def _make_cm(character_config):
    """构造 CompressionManager + CharacterState mock（首刷路径）。

    配置 llm.think 返回带 XML 标签的文本，使 _refresh_c6 能走到解析/更新 state，
    避免 .text 是 AsyncMock 子协程对象导致 _extract_tag 报 TypeError。
    """
    state = MagicMock()
    state.speaking_style_c6 = ""  # 首刷路径
    pm = PromptManager(str(ProjectPath.PROMPTS.resolve()))

    llm = AsyncMock()
    llm_result = MagicMock()
    llm_result.text = "<new_speaking_style>测试风格</new_speaking_style>\n<changes>调整</changes>"
    llm.think.return_value = llm_result

    cm = CompressionManager(
        llm=llm,
        state=state,
        character_name=character_config.name,
        l1_core=character_config.base_personality,
        pm=pm,
        version_repo=None,
        character_config=character_config,
    )
    return cm, state


def test_refresh_c6_uses_character_speaking_style():
    """_refresh_c6 首刷读 character.speaking_style（非 C6_L1_DEFAULT）。"""
    cfg = CharacterConfig.from_yaml(_CYRENE_CONFIG)
    assert cfg.speaking_style.strip(), "前置：Cyrene speaking_style 非空"
    assert cfg.speaking_style != ss_mod.C6_L1_DEFAULT, "前置：Cyrene speaking_style 不同于 Elysia 常量"

    cm, state = _make_cm(cfg)

    captured: dict[str, object] = {}
    orig = ss_mod.SpeakingStyleSection

    def fake_init(template_path: str, speaking_style: str | None = None):
        captured["speaking_style"] = speaking_style
        # 用注入的 speaking_style 构造真实章节，渲染返回它（校验传参路径）
        effective = speaking_style if speaking_style is not None else ss_mod.C6_L1_DEFAULT
        return orig(template_path, speaking_style=effective)

    ss_mod.SpeakingStyleSection = fake_init
    try:
        asyncio.run(cm._refresh_c6())
    finally:
        ss_mod.SpeakingStyleSection = orig

    # 断言首刷传给 SpeakingStyleSection 的是 Cyrene 的 speaking_style，而非 Elysia 常量
    assert captured.get("speaking_style") == cfg.speaking_style
    assert captured.get("speaking_style") != ss_mod.C6_L1_DEFAULT


def test_refresh_c6_uses_default_when_character_style_empty():
    """character.speaking_style 为空时回退 C6_L1_DEFAULT（行为兼容）。"""
    cfg = CharacterConfig.from_yaml(_CYRENE_CONFIG)
    cfg.personality["speaking_style"] = ""  # 模拟空 speaking_style
    assert cfg.speaking_style == "", "前置：speaking_style 为空"

    cm, state = _make_cm(cfg)

    captured: dict[str, object] = {}
    orig = ss_mod.SpeakingStyleSection

    def fake_init(template_path: str, speaking_style: str | None = None):
        captured["speaking_style"] = speaking_style
        effective = speaking_style if speaking_style is not None else ss_mod.C6_L1_DEFAULT
        return orig(template_path, speaking_style=effective)

    ss_mod.SpeakingStyleSection = fake_init
    try:
        asyncio.run(cm._refresh_c6())
    finally:
        ss_mod.SpeakingStyleSection = orig

    assert captured.get("speaking_style") == ss_mod.C6_L1_DEFAULT
