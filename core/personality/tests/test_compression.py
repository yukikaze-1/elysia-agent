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
from core.schema.compression import CompressionTrigger
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


# ══════════════════════════════════════════════════════════════════
# S3-21：L2/C6 版本历史的**写键必须等于读键**
# ══════════════════════════════════════════════════════════════════

class TestVersionHistoryKeyIsSingleSourced:
    """写侧（`CompressionManager` / 回滚后再写一版）原先用 `state.session_id`，
    读侧（`_list_character_versions` / 回滚查询 / Overview 计数）用
    `agent.session.session_id`。

    这两个值**永不相同**：`CharacterRuntimeState` 构造时把 `session_id` 设成
    `SessionId(instance.instanceId)`（`core/character/runtime.py`），而
    `agent.session.session_id` 是会话 ID。后果是**版本历史恒空、回滚恒 404、
    Overview 计数恒 0**。

    报告把它归为"类型 4：单测与集成测不联测，键不一致在单测中不可见"——写侧单测用
    任意 `sid` 构造、读侧单测用另一个任意 `sid`，**"键必须相同"这个唯一重要的事实
    无人断言**。本类就是那条联测。
    """

    @staticmethod
    def _cm_with_repo(state_session_id: str):
        """构造带真实 RecordingVersionRepo 的 CompressionManager。"""
        state = MagicMock()
        state.session_id = state_session_id
        state.speaking_style_c6 = "既有风格"
        state.self_l2 = "既有 L2"

        repo = MagicMock()
        repo.append_version = AsyncMock(return_value=1)
        repo.list_versions = AsyncMock(return_value=[])
        repo.count_versions = AsyncMock(return_value=0)

        cm = CompressionManager(
            llm=AsyncMock(), state=state, character_name="Elysia", l1_core="L1",
            pm=None, version_repo=repo, character_config=MagicMock(),
        )
        return cm, state, repo

    def test_write_key_matches_engine_version_key(self):
        """**核心**：写侧用的键必须等于 `CharacterEngine.version_key`。"""
        from core.personality.engine import CharacterEngine

        cm, state, _ = self._cm_with_repo("chi_builtin_elysia")
        engine = MagicMock(spec=CharacterEngine)
        engine.state = state
        engine.version_key = CharacterEngine.version_key.__get__(engine)

        assert cm._version_key() == engine.version_key, (      # noqa: SLF001
            f"写键 {cm._version_key()!r} ≠ 读键 {engine.version_key!r}——"  # noqa: SLF001
            f"版本历史会恒空、回滚恒 404")

    @pytest.mark.asyncio
    async def test_save_version_uses_same_key_as_reader(self):
        """端到端：写一版 → 用读侧同一个键能查到。"""
        cm, state, repo = self._cm_with_repo("chi_builtin_elysia")
        await cm._save_version("新的 L2", ["改动"],              # noqa: SLF001
                               CompressionTrigger.MANUAL)

        repo.append_version.assert_awaited_once()
        written_key = repo.append_version.await_args.kwargs["session_id"]

        from core.personality.engine import CharacterEngine

        engine = MagicMock(spec=CharacterEngine)
        engine.state = state
        engine.version_key = CharacterEngine.version_key.__get__(engine)
        assert str(written_key) == engine.version_key

    @pytest.mark.asyncio
    async def test_reader_key_is_not_conversation_id(self):
        """反例记录：**会话 ID 不是版本键**——这正是修复前读侧的错误取值。

        之所以把它写成用例：`state.session_id` 存的是 instanceId，而
        `agent.session.session_id` 是会话 ID；两者看起来都"像"会话标识，
        靠命名分辨不出来，只能靠断言钉住。
        """
        cm, state, repo = self._cm_with_repo("chi_builtin_elysia")
        await cm._save_version("L2", [], CompressionTrigger.MANUAL)   # noqa: SLF001
        written_key = str(repo.append_version.await_args.kwargs["session_id"])

        assert written_key == "chi_builtin_elysia"
        assert written_key != "conv_2026_some_session", (
            "版本键若变成会话 ID，同一角色的历史会被拆到每个会话下")


class TestVersionHistoryRoundTrip:
    """S3-21 端到端：**写进去的版本，用读侧的键必须查得到**。

    用真实的 `CharacterVersionRepository`（内存 fake 后端）+ 真实 engine，
    跑"写一版 → 列表/计数/取单版"整条链——这正是原来缺失的那层联测。
    """

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self):
        from core.schema.types import SessionId
        from core.storage.character_version_repo import CharacterVersionRepository
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        repo = CharacterVersionRepository(db=FakeDocumentDBBackend())
        state = MagicMock()
        state.session_id = SessionId("chi_builtin_elysia")
        cm = CompressionManager(
            llm=AsyncMock(), state=state, character_name="Elysia", l1_core="L1",
            pm=None, version_repo=repo, character_config=MagicMock(),
        )
        key = cm._version_key()                                  # noqa: SLF001

        await cm._save_version("第一版 L2", ["初始"],              # noqa: SLF001
                               CompressionTrigger.MANUAL)

        listed = await repo.list_versions(key, content_type="l2")
        assert len(listed) == 1, f"写进去了却查不到——键不一致：{key}"
        assert listed[0]["text"] == "第一版 L2"
        assert await repo.count_versions(key, content_type="l2") == 1

        # c6 与 l2 分列（同一键、不同 content_type）
        await cm._save_version("第一版 C6", ["风格"],              # noqa: SLF001
                               CompressionTrigger.C6_REFRESH, content_type="c6")
        assert await repo.count_versions(key, content_type="c6") == 1
        assert await repo.count_versions(key, content_type="l2") == 1

    @pytest.mark.asyncio
    async def test_read_with_conversation_id_finds_nothing(self):
        """反例：用**会话 ID** 读（修复前的读侧取值）必须查不到东西。

        这条把"键选错了"与"没有版本"两种情形区分开——后者是正常空历史，
        前者是缺陷。修复前两者在观测上完全一样（都返回空），所以没人发现。
        """
        from core.schema.types import SessionId
        from core.storage.character_version_repo import CharacterVersionRepository
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        repo = CharacterVersionRepository(db=FakeDocumentDBBackend())
        state = MagicMock()
        state.session_id = SessionId("chi_builtin_elysia")
        cm = CompressionManager(
            llm=AsyncMock(), state=state, character_name="Elysia", l1_core="L1",
            pm=None, version_repo=repo, character_config=MagicMock(),
        )
        await cm._save_version("L2", [], CompressionTrigger.MANUAL)   # noqa: SLF001

        wrong = SessionId("conv_some_conversation")
        assert await repo.list_versions(wrong, content_type="l2") == []
        assert await repo.count_versions(wrong, content_type="l2") == 0


# ══════════════════════════════════════════════════════════════════
# S3-11：L2 演化链未完成时不得推进"已处理"计数
# ══════════════════════════════════════════════════════════════════

class TestEvolutionChainCompleteness:
    """`_compress` 的链是「改内存 L2 → 写版本历史 → 刷 C6」，而
    `compression_last_count` 原先在**写版本之前**就推进了。

    于是版本 repo 离线（`append_version` 静默返回 0）或 C6 刷新抛错时：
    **L2 已变、无版本记录、C6 未刷新，且因为计数已推进而永不重试**。
    而"C6 与 L2 一致"正是这条联动的存在理由。
    """

    def _engine(self, *, version_repo="ok", c6="ok"):
        """构造能走到"链尾"的 engine（LLM/模板都打桩）。"""
        from core.personality.engine import CharacterEngine
        from core.schema.character import CharacterState

        state = CharacterState(session_id="test-sid")
        state.state_history = [
            {"timestamp": f"2026-01-0{i}", "summary": f"s{i}", "trigger": "t"}
            for i in range(1, 6)
        ]

        pm = MagicMock()
        pm.render_macro.return_value = "prompt"
        llm = AsyncMock()
        result = MagicMock()
        result.text = "<new_self>新的自我</new_self>\n<changes>- 变化一</changes>"
        llm.think = AsyncMock(return_value=result)

        if version_repo == "ok":
            repo = MagicMock()
            repo.append_version = AsyncMock(return_value=1)
        elif version_repo == "down":
            repo = MagicMock()
            repo.append_version = AsyncMock(side_effect=RuntimeError("mongo down"))
        else:
            repo = None

        cm = CompressionManager(
            llm=llm, state=state, character_name="E", l1_core="L1",
            pm=pm, version_repo=repo, character_config=MagicMock(),
        )
        cm._refresh_c6 = (                                  # noqa: SLF001
            AsyncMock() if c6 == "ok"
            else AsyncMock(side_effect=RuntimeError("template boom")))

        # engine 只做委托——链的编排在 CompressionManager 里
        engine = MagicMock()
        engine._state = state                              # noqa: SLF001
        engine._compression = cm                           # noqa: SLF001
        return engine, state

    @pytest.mark.asyncio
    async def test_happy_path_advances_counter(self):
        """回归网：链完整 → 正常推进计数。"""
        engine, state = self._engine()
        await engine._compression._compress(_refresh_cb=engine._compression._refresh_c6)   # noqa: SLF001
        assert state.self_l2 == "新的自我"
        assert state.compression_last_count == 5

    @pytest.mark.asyncio
    async def test_version_write_failure_keeps_counter(self, caplog):
        """**核心**：版本写失败 → 不推进计数（下一批条目到来时重试）。"""
        import logging

        engine, state = self._engine(version_repo="down")

        with caplog.at_level(logging.WARNING):
            await engine._compression._compress(_refresh_cb=engine._compression._refresh_c6)   # noqa: SLF001

        assert state.self_l2 == "新的自我", "L2 的即时变化保留（用户可见）"
        assert state.compression_last_count == 0, (
            "链未完成却推进了计数 → 版本记录永远不会补上")
        assert state.compression_last_at > 0, "时间戳仍要推进（否则变成热循环）"
        assert any("未完成" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_c6_refresh_failure_keeps_counter(self):
        """C6 刷新失败同样算链未完成——C6 与 L2 不一致正是这条联动要防的。"""
        engine, state = self._engine(c6="down")
        await engine._compression._compress(_refresh_cb=engine._compression._refresh_c6)   # noqa: SLF001
        assert state.compression_last_count == 0

    @pytest.mark.asyncio
    async def test_unconfigured_repo_is_not_a_failure(self):
        """**对照**：没配置版本历史的部署里这条链不存在，必须照常推进计数。

        否则每来一批新条目就重跑一次压缩 LLM（纯浪费）——本用例第一版就是这么
        把 `test_compress_updates_self_l2` 打红的。
        """
        engine, state = self._engine(version_repo=None)
        await engine._compression._compress(_refresh_cb=engine._compression._refresh_c6)   # noqa: SLF001
        assert state.compression_last_count == 5
