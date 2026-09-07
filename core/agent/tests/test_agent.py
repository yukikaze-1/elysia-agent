# core/agent/tests/test_agent.py
"""Task 7（B1 闭环）：agent 注入 UserIdentityStore + engine 传 instance 身份。

覆盖 plan Task 7 Step 5：
- __init__ bootstrap 后 self._user_identity 非 None；
- store.get().pronouns == "她"（B1 全局唯一兜底）；
- engine 以 chi_builtin_<name>/rev=1 构造（供 _prepare_turn 冻结 instance 身份）。
"""

from unittest.mock import AsyncMock

import pytest

from core.agent import Agent
from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.config import AppConfig

pytestmark = pytest.mark.service


@pytest.fixture
def agent():
    return Agent(config=AppConfig())


class TestUserIdentityStoreInjection:
    def test_agent_injects_user_identity_store(self, agent):
        """bootstrap 后 _user_identity 非 None，store.get().pronouns == '她'。"""
        assert agent._user_identity is not None
        assert agent._user_identity_store.get().pronouns == "她"


class TestCharacterEngineInstanceIdentity:
    def test_engine_gets_builtin_instance_id(self, agent):
        """engine 以 instance_id='chi_builtin_<name>' + revision=1 构造（M3 经 registry 解析）。

        Task 11：agent.character_engine 经 registry 解析当前 instance（默认 Elysia），
        其 engine 以 chi_builtin_<name>/rev=1 构造。
        """
        engine = agent.character_engine
        assert engine._instance_id == "chi_builtin_" + agent._character.name.lower()
        assert engine._character_revision == 1


@pytest.mark.asyncio
async def test_custom_runtime_engine_uses_persisted_revision_not_boot_character(
    agent,
):
    """Custom runtime identity, prompt baseline and memory scope share one source."""
    definition = CharacterDefinition(
        characterId="chr_custom_cold", source="custom", mutable=True
    )
    revision = CharacterRevision(
        characterId=definition.characterId,
        revision=4,
        displayName="Cold",
        promptProfile={
            "corePersonality": "冷静、克制，不使用爱莉希雅口癖。",
            "relationshipStyle": "保持礼貌距离。",
            "emotionStyle": "情绪表达含蓄。",
            "speakingStyle": "使用简短陈述句。",
            "conversationInitiative": "不连续追问。",
            "examples": [],
        },
        runtimeDefaults={"emotion": {"idleExpressions": ["neutral"]}},
    )
    instance = CharacterInstance(
        instanceId="chi_custom_cold",
        characterId=definition.characterId,
        revision=4,
    )
    agent._runtime_source_loader.load = AsyncMock(
        return_value=(definition, revision, instance)
    )

    runtime = await agent.ensure_character_runtime(instance.instanceId)
    engine = runtime.engine

    assert engine is not None
    assert engine._character_name == "Cold"
    assert engine._l1_core == "冷静、克制，不使用爱莉希雅口癖。"
    assert engine._character.speaking_style == "使用简短陈述句。"
    assert engine._character_revision == 4
    assert runtime.memory_view.scope.character_instance_id == instance.instanceId
    assert runtime.consolidator._character_name == "Cold"
    assert runtime.reflector._character_name == "Cold"


def test_sync_runtime_factory_rejects_custom_instance(agent):
    with pytest.raises(RuntimeError, match="异步存储 loader"):
        agent._make_runtime("chi_custom_missing")
