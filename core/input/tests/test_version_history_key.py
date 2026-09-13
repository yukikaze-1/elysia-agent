# 测试文件：core/input/tests/test_version_history_key.py
"""S3-21：版本历史的**读侧**必须用与写侧相同的键（路由级联测）。

报告把它归为"类型 4：单测与集成测不联测，键不一致在单测中不可见"：

> 写侧单测用任意 `CharacterRuntimeState(session_id=SessionId("test-sid"))` 构造，
> 读侧单测用任意 `sid`；两侧从不联测，**"键必须相同"这个唯一重要的事实无人断言**。
> 全仓**没有任何路由级集成测试**覆盖 `_list_character_versions`。

本文件补上那层：用**两个不同的键**（`engine.version_key` = instanceId、
`agent.session.session_id` = 会话 ID）驱动 route，断言它取的是前者。
修复前路由取后者 → 版本历史恒空、回滚恒 404、Overview 计数恒 0。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.service

#: 刻意让两个键**看起来都像**会话标识——靠命名分辨不出来，只能靠断言钉住。
_INSTANCE_KEY = "chi_builtin_elysia"
_CONVERSATION_ID = "conv_2026_some_session"


def _agent_with_split_keys(*, list_ret=None, get_ret=None):
    """构造 agent：engine.version_key ≠ agent.session.session_id。"""
    repo = MagicMock()
    repo.list_versions = AsyncMock(return_value=list_ret or [])
    repo.count_versions = AsyncMock(return_value=3)
    repo.get_version = AsyncMock(return_value=get_ret)
    repo.append_version = AsyncMock(return_value=2)

    engine = MagicMock()
    engine.version_repo = repo
    engine.version_key = _INSTANCE_KEY          # str（删掉属性 → 落到 MagicMock）
    engine.state = MagicMock()

    agent = MagicMock()
    agent.character_engine = engine
    agent.session.session_id = _CONVERSATION_ID
    return agent, engine, repo


@pytest.mark.asyncio
async def test_list_versions_uses_engine_key_not_conversation_id():
    """**核心**：列表查询的键必须是 `engine.version_key`。"""
    from core.input.debug_routes.character import _list_character_versions

    agent, engine, repo = _agent_with_split_keys()

    result, error, _code, status = await _list_character_versions(
        agent, "l2", 20, None)

    assert status == 200 and error is None, (error, status)
    used = repo.list_versions.await_args.args[0]
    assert str(used) == _INSTANCE_KEY, (
        f"读侧用了 {used!r}——写侧写的是 {_INSTANCE_KEY!r}（版本历史会恒空）")
    assert str(used) != _CONVERSATION_ID
    assert str(repo.count_versions.await_args.args[0]) == _INSTANCE_KEY
    assert result is not None


@pytest.mark.asyncio
async def test_rollback_lookup_uses_engine_key():
    """回滚查询同样：用错键 → 恒 404。"""
    from core.input.debug_routes.character import _character_rollback

    entry = {"text": "旧版 L2", "version": 1}
    agent, engine, repo = _agent_with_split_keys(get_ret=entry)
    engine.state = MagicMock()

    result, error, _code, status = await _character_rollback(agent, "l2", 1)

    assert status == 200, (error, status)
    used = repo.get_version.await_args.args[0]
    assert str(used) == _INSTANCE_KEY, f"回滚查询用了 {used!r}"
    # 回滚会再写一版——也必须落到同一个键
    assert str(repo.append_version.await_args.kwargs["session_id"]) == _INSTANCE_KEY


@pytest.mark.asyncio
async def test_overview_counts_use_engine_key():
    """debug Overview 的 L2/C6 计数同样从单一来源取键（修复前恒 0）。

    直接调聚合函数 `_collect_character_and_reflector`（它内部取计数），而不是
    猜一个入口名再 skip——跳过等于没测。
    """
    from core.debug import info as info_mod

    agent, engine, repo = _agent_with_split_keys()

    info = info_mod.DebugInfo()
    try:
        await info_mod._collect_character_and_reflector(agent, info)   # noqa: SLF001
    except Exception:
        # 该聚合还依赖别的子系统（reflector/awareness 等）；计数点已经跑到就够了
        pass

    assert repo.count_versions.await_count >= 1, "Overview 没有查询版本计数"
    for call in repo.count_versions.await_args_list:
        used = str(call.args[0])
        assert used == _INSTANCE_KEY, (
            f"Overview 计数用了 {used!r}（应为 {_INSTANCE_KEY!r}）——计数会恒 0")
