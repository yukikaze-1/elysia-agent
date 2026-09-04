# core/agent/tests/test_agent.py
"""Task 7（B1 闭环）：agent 注入 UserIdentityStore + engine 传 instance 身份。

覆盖 plan Task 7 Step 5：
- __init__ bootstrap 后 self._user_identity 非 None；
- store.get().pronouns == "她"（B1 全局唯一兜底）；
- engine 以 chi_builtin_<name>/rev=1 构造（供 _prepare_turn 冻结 instance 身份）。
"""

import pytest

from core.agent import Agent
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
