"""AgentLifecycle flush 方法 mock 测试。

Task 11（M3）：_flush_reflector/_flush_compression 经 RuntimeRegistry 遍历各 instance
engine 触发——不再用全局 _character_engine。测试用带 runtime+engine 的 registry mock。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.agent.lifecycle import AgentLifecycle
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.service


def _runtime_with_engine():
    """构造带 mock engine 的 CharacterRuntime，供 registry 预置。"""
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId="chi_builtin_elysia",
                                   characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(characterId="chr_builtin_elysia",
                                   revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default"),
    )
    engine = MagicMock()
    engine.flush_reflector = AsyncMock()
    engine.flush_compression = AsyncMock()
    rt.bind_engine(engine)
    return rt, engine


def _registry_with_runtime():
    """registry 预置 default runtime（带 mock engine）。"""
    reg = CharacterRuntimeRegistry()
    rt, _ = _runtime_with_engine()
    reg.get("chi_builtin_elysia", lambda: rt)
    return reg


class TestFlushConsolidator:
    @pytest.mark.asyncio
    async def test_flush_with_items(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.return_value = ["item1", "item2"]
        await AgentLifecycle._flush_consolidator(fake_agent)
        fake_agent._consolidator.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_timeout_does_not_raise(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.side_effect = asyncio.TimeoutError
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常

    @pytest.mark.asyncio
    async def test_no_consolidator_does_not_raise(self, fake_agent):
        fake_agent._consolidator = None
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常


class TestFlushReflector:
    @pytest.mark.asyncio
    async def test_flush_delegates_to_engine(self, fake_agent):
        rt, engine = _runtime_with_engine()
        fake_agent._runtime_registry = CharacterRuntimeRegistry()
        fake_agent._runtime_registry.get("chi_builtin_elysia", lambda: rt)
        await AgentLifecycle._flush_reflector(fake_agent)
        engine.flush_reflector.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_registry_does_not_raise(self, fake_agent):
        fake_agent._runtime_registry = None
        await AgentLifecycle._flush_reflector(fake_agent)
        # 不应抛异常

    @pytest.mark.asyncio
    async def test_flush_survives_runtime_without_engine(self, fake_agent):
        """无 engine 的 runtime（registry 预置但 engine 未建）应跳过，不抛。"""
        rt, _ = _runtime_with_engine()
        empty_rt = CharacterRuntime(
            instance=CharacterInstance(instanceId="chi_x", characterId="chr_x"),
            definition=CharacterDefinition(characterId="chr_x"),
            revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
            user_identity=UserIdentity(userId="usr_local_default"),
        )
        fake_agent._runtime_registry = CharacterRuntimeRegistry()
        fake_agent._runtime_registry.get("chi_builtin_elysia", lambda: rt)
        fake_agent._runtime_registry.get("chi_x", lambda: empty_rt)
        await AgentLifecycle._flush_reflector(fake_agent)
        engine = rt.engine
        engine.flush_reflector.assert_awaited_once()


class TestFlushCompression:
    @pytest.mark.asyncio
    async def test_flush_delegates_to_engine(self, fake_agent):
        rt, engine = _runtime_with_engine()
        fake_agent._runtime_registry = CharacterRuntimeRegistry()
        fake_agent._runtime_registry.get("chi_builtin_elysia", lambda: rt)
        await AgentLifecycle._flush_compression(fake_agent)
        engine.flush_compression.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_registry_does_not_raise(self, fake_agent):
        fake_agent._runtime_registry = None
        await AgentLifecycle._flush_compression(fake_agent)
        # 不应抛异常

    @pytest.mark.asyncio
    async def test_flush_survives_runtime_without_engine(self, fake_agent):
        rt, engine = _runtime_with_engine()
        empty_rt = CharacterRuntime(
            instance=CharacterInstance(instanceId="chi_x", characterId="chr_x"),
            definition=CharacterDefinition(characterId="chr_x"),
            revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
            user_identity=UserIdentity(userId="usr_local_default"),
        )
        fake_agent._runtime_registry = CharacterRuntimeRegistry()
        fake_agent._runtime_registry.get("chi_builtin_elysia", lambda: rt)
        fake_agent._runtime_registry.get("chi_x", lambda: empty_rt)
        await AgentLifecycle._flush_compression(fake_agent)
        engine.flush_compression.assert_awaited_once()
