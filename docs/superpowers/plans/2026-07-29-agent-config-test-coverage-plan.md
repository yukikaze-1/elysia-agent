# agent + config 测试补全 & 测试文件迁移 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `core/config/`（+~18 tests）和 `core/agent/`（+~37 tests）补全测试，迁移 4 个测试文件到模块内 `tests/`，更新 docs

**Architecture:** 纯 pytest + unittest.mock。config 测试纯 dataclass 无需 mock；agent 测试分 L1（纯单元，直接实例化 AgentFSM/AgentState）和 L2（MagicMock 模拟 30+ 子系统）。不改变现有 marker 体系。

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, unittest.mock, tempfile

## Global Constraints

- 所有测试必须加 pytest marker：`unit` 或 `service`
- 遵循现有测试命名规范：`TestXxx` 类 → `test_xxx` 方法
- 使用 `pytestmark = pytest.mark.unit` 模块级标记
- 迁移文件时保持绝对 import 路径不变（最小 diff）
- 每个 task 末尾 commit，message 格式 `test: <description>`
- 禁止 TDD 步骤——已有代码，直接写验证测试

---

### Task 1: config/ tests — DatabaseConfig + scaffolding

**Files:**
- Create: `core/config/tests/__init__.py`
- Create: `core/config/tests/conftest.py`
- Create: `core/config/tests/test_database_config.py`

**Interfaces:**
- Consumes: `core.config.app_config.DatabaseConfig`, `Module.config.ConfigLoader`
- Produces: `core/config/tests/` 目录就绪，6 个 database config 测试

- [ ] **Step 1: Create directory + __init__.py**

```bash
mkdir -p core/config/tests
touch core/config/tests/__init__.py
```

- [ ] **Step 2: Create conftest.py（空，占位）**

```python
# core/config/tests/conftest.py
"""core/config/ 测试 fixtures。"""
```

- [ ] **Step 3: Write test_database_config.py**

```python
# core/config/tests/test_database_config.py
"""DatabaseConfig 默认值 + 环境变量覆盖测试。"""

import pytest
from Module.config import ConfigLoader
from core.config.app_config import DatabaseConfig

pytestmark = pytest.mark.unit


class TestDatabaseConfigDefaults:
    """默认值验证"""

    def test_mongodb_uri_default(self):
        cfg = DatabaseConfig()
        assert cfg.mongodb_uri == "mongodb://localhost:27017"

    def test_milvus_uri_default(self):
        cfg = DatabaseConfig()
        assert cfg.milvus_uri == "http://localhost:19530"

    def test_milvus_token_default_empty(self):
        cfg = DatabaseConfig()
        assert cfg.milvus_token == ""

    def test_db_name_default(self):
        cfg = DatabaseConfig()
        assert cfg.db_name == "default"


class TestDatabaseConfigEnvOverride:
    """环境变量覆盖——env var 通过 ConfigLoader.load() 解析"""

    def test_mongodb_uri_from_env(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://custom:27017")
        cfg = ConfigLoader(DatabaseConfig).load()
        assert cfg.mongodb_uri == "mongodb://custom:27017"

    def test_milvus_uri_from_env(self, monkeypatch):
        monkeypatch.setenv("MILVUS_URI", "http://milvus:19531")
        cfg = ConfigLoader(DatabaseConfig).load()
        assert cfg.milvus_uri == "http://milvus:19531"
```

- [ ] **Step 4: Run tests**

```bash
pytest core/config/tests/test_database_config.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add core/config/tests/__init__.py core/config/tests/conftest.py core/config/tests/test_database_config.py
git commit -m "test: config/ — DatabaseConfig 默认值 + env 覆盖 (6 tests)"
```

---

### Task 2: config/ tests — AppConfig + CharacterConfig

**Files:**
- Create: `core/config/tests/test_app_config.py`
- Create: `core/config/tests/test_character_config.py`

**Interfaces:**
- Consumes: `core.config.app_config.AppConfig`, `core.config.character_config.CharacterConfig`
- Produces: 12 个新测试，config/ 测试完成

- [ ] **Step 1: Write test_app_config.py**

```python
# core/config/tests/test_app_config.py
"""AppConfig 嵌套 dataclass 默认值验证。"""

import pytest
from core.config.app_config import AppConfig, DatabaseConfig

pytestmark = pytest.mark.unit


class TestAppConfigNesting:
    """AppConfig 嵌套 dataclass 默认值"""

    def test_database_section_exists(self):
        cfg = AppConfig()
        assert isinstance(cfg.database, DatabaseConfig)

    def test_all_top_level_sections_have_defaults(self):
        """验证 10 个顶层段都可无参实例化"""
        cfg = AppConfig()
        assert cfg.agent is not None
        assert cfg.llm is not None
        assert cfg.memory is not None
        assert cfg.server is not None
        assert cfg.tts is not None
        assert cfg.stt is not None
        assert cfg.wake_word is not None
        assert cfg.live2d is not None
        assert cfg.vision is not None
        assert cfg.database is not None

    def test_database_nested_default_value(self):
        cfg = AppConfig()
        assert cfg.database.mongodb_uri == "mongodb://localhost:27017"

    def test_memory_section_defaults(self):
        cfg = AppConfig()
        assert cfg.memory.session_max_messages == 20
        assert cfg.memory.ltm_max_entries == 10000
```

- [ ] **Step 2: Run app_config tests**

```bash
pytest core/config/tests/test_app_config.py -v
```
Expected: 4 passed

- [ ] **Step 3: Write test_character_config.py**

```python
# core/config/tests/test_character_config.py
"""CharacterConfig YAML 加载测试。"""

import pytest
from pathlib import Path
from core.config.character_config import CharacterConfig

pytestmark = pytest.mark.unit


class TestCharacterConfigFromYAML:
    """从临时 YAML 文件加载角色配置"""

    @pytest.fixture
    def char_dir(self, tmp_path: Path):
        """创建最小角色目录：config.yaml + {name}_personality.md"""
        (tmp_path / "config.yaml").write_text(
            "name: 测试角色\n"
            "wake_words: [你好, 测试]\n"
            "personality:\n"
            "  traits: [温柔, 细心]\n"
            "  speaking_style: 轻声细语\n"
        )
        # 自动推导: {name}_personality.md → 测试角色_personality.md
        (tmp_path / "测试角色_personality.md").write_text("我是测试角色。")
        return tmp_path

    def test_load_name(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.name == "测试角色"

    def test_load_wake_words(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert "你好" in cfg.wake_words

    def test_traits_property(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.traits == ["温柔", "细心"]

    def test_speaking_style_property(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.speaking_style == "轻声细语"

    def test_loads_l1_personality_text(self, char_dir):
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert "测试角色" in cfg.base_personality

    def test_missing_personality_md_does_not_crash(self, tmp_path: Path):
        """personality.md 不存在时 L1 文本为空或默认值"""
        (tmp_path / "config.yaml").write_text("name: 无L1\nwake_words: []\n")
        cfg = CharacterConfig.from_yaml(tmp_path / "config.yaml")
        assert cfg.name == "无L1"
        # 不应抛异常

    def test_no_reference_dir_does_not_crash(self, char_dir):
        """无 reference/ 目录不报错"""
        cfg = CharacterConfig.from_yaml(char_dir / "config.yaml")
        assert cfg.name == "测试角色"

    def test_missing_config_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            CharacterConfig.from_yaml(tmp_path / "nonexistent.yaml")
```

- [ ] **Step 4: Run character_config tests**

```bash
pytest core/config/tests/test_character_config.py -v
```
Expected: 8 passed

- [ ] **Step 5: Run full config suite**

```bash
pytest core/config/tests/ -v
```
Expected: 18 passed

- [ ] **Step 6: Commit**

```bash
git add core/config/tests/test_app_config.py core/config/tests/test_character_config.py
git commit -m "test: config/ — AppConfig nested + CharacterConfig YAML (12 tests)"
```

---

### Task 3: agent/ L1 测试 — FSM + types + lifecycle steps

**Files:**
- Create: `core/agent/tests/__init__.py`
- Create: `core/agent/tests/test_agent_fsm.py`
- Create: `core/agent/tests/test_types.py`
- Create: `core/agent/tests/test_lifecycle_steps.py`

**Interfaces:**
- Consumes: `core.agent.agent_fsm.AgentFSM`, `core.schema.AgentState`, `core.agent._types`, `core.agent.lifecycle.AgentLifecycle`
- Produces: 21 个 L1 单元测试

- [ ] **Step 1: Create directory + __init__.py**

```bash
mkdir -p core/agent/tests
touch core/agent/tests/__init__.py
```

- [ ] **Step 2: Write test_agent_fsm.py**

```python
# core/agent/tests/test_agent_fsm.py
"""AgentFSM 状态机单元测试。"""

import pytest
from core.agent.agent_fsm import AgentFSM
from core.schema import AgentState

pytestmark = pytest.mark.unit


class TestAgentFSM:
    """AgentFSM 纯单元测试——无外部依赖"""

    def test_initial_state_is_idle(self):
        fsm = AgentFSM()
        assert fsm.state == AgentState.IDLE

    def test_initial_previous_is_none(self):
        fsm = AgentFSM()
        assert fsm.previous is None

    def test_idle_to_thinking(self):
        fsm = AgentFSM()
        assert fsm.can_transition(AgentState.THINKING)
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING
        assert fsm.previous == AgentState.IDLE

    def test_thinking_to_speaking(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        assert fsm.can_transition(AgentState.SPEAKING)
        fsm.transition(AgentState.SPEAKING)
        assert fsm.state == AgentState.SPEAKING

    def test_speaking_to_idle(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        assert fsm.can_transition(AgentState.IDLE)
        fsm.transition(AgentState.IDLE)
        assert fsm.state == AgentState.IDLE

    def test_invalid_transition_raises_value_error(self):
        """非法状态转换抛 ValueError——IDLE 不能直接到 SPEAKING"""
        fsm = AgentFSM()
        with pytest.raises(ValueError):
            fsm.transition(AgentState.SPEAKING)

    def test_same_state_transition_is_idempotent(self):
        """同状态转换幂等——IDLE→IDLE 不抛异常"""
        fsm = AgentFSM()
        fsm.transition(AgentState.IDLE)  # 不应抛异常
        assert fsm.state == AgentState.IDLE

    def test_reset_returns_to_idle(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.reset()
        assert fsm.state == AgentState.IDLE
        assert fsm.previous == AgentState.THINKING

    def test_interrupt_from_thinking(self):
        """INTERRUPTED 可从 THINKING 进入"""
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        assert fsm.interrupt()
        assert fsm.state == AgentState.INTERRUPTED

    def test_interrupt_from_idle_returns_false(self):
        """IDLE 状态不可 interrupt，返回 False"""
        fsm = AgentFSM()
        assert not fsm.interrupt()
        assert fsm.state == AgentState.IDLE
```

- [ ] **Step 3: Run FSM tests**

```bash
pytest core/agent/tests/test_agent_fsm.py -v
```
Expected: 10 passed

- [ ] **Step 4: Write test_types.py**

```python
# core/agent/tests/test_types.py
"""Agent _types dataclass 验证。"""

import pytest
from core.agent._types import _PendingMessage, _BackgroundTaskEntry, RunState

pytestmark = pytest.mark.unit


class TestPendingMessage:
    def test_fields_defaults(self):
        msg = _PendingMessage(text="hello", user_name="test", request_id="req-1")
        assert msg.text == "hello"
        assert msg.user_name == "test"
        assert msg.request_id == "req-1"
        assert msg.content is None

    def test_with_content(self):
        content = [{"type": "text", "content": "hi"}]
        msg = _PendingMessage(text="", user_name="u", request_id="r",
                              content=content)
        assert msg.content == content


class TestBackgroundTaskEntry:
    def test_fields(self):
        from asyncio import Queue
        q = Queue()
        entry = _BackgroundTaskEntry(
            intent="test intent",
            dispatched_at="2026-01-01T00:00:00Z",
            mailbox=q,
        )
        assert entry.intent == "test intent"
        assert entry.dispatched_at == "2026-01-01T00:00:00Z"
        assert entry.mailbox is q


class TestRunState:
    def test_four_states(self):
        states = set(RunState)
        assert RunState.STOPPED in states
        assert RunState.RUNNING in states
        assert RunState.STOPPING in states
        assert RunState.RESTARTING in states
        assert len(states) == 4

    def test_state_uniqueness(self):
        values = [s.value for s in RunState]
        assert len(values) == len(set(values))
```

- [ ] **Step 5: Run types tests**

```bash
pytest core/agent/tests/test_types.py -v
```
Expected: 5 passed

- [ ] **Step 6: Write test_lifecycle_steps.py**

```python
# core/agent/tests/test_lifecycle_steps.py
"""AgentLifecycle _SHUTDOWN_STEPS / _STARTUP_STEPS 结构验证。"""

import pytest
from core.agent.lifecycle import AgentLifecycle

pytestmark = pytest.mark.unit


class TestShutdownSteps:
    """验证 _SHUTDOWN_STEPS 数据驱动列表"""

    def test_steps_not_empty(self):
        assert len(AgentLifecycle._SHUTDOWN_STEPS) > 0

    def test_all_step_names_unique(self):
        names = [name for name, _ in AgentLifecycle._SHUTDOWN_STEPS]
        assert len(names) == len(set(names))

    def test_all_method_names_exist_on_class(self):
        """每个 method_name 对应 AgentLifecycle 或其父类 mixin 的方法"""
        for name, method_name in AgentLifecycle._SHUTDOWN_STEPS:
            assert hasattr(AgentLifecycle, method_name), \
                f"Step '{name}': method '{method_name}' not found"

    def test_flush_steps_before_stop_steps(self):
        """flush 步骤排在 stop/close 步骤之前"""
        flush_indices = [
            i for i, (name, _) in enumerate(AgentLifecycle._SHUTDOWN_STEPS)
            if name.startswith("flush")
        ]
        stop_indices = [
            i for i, (name, _) in enumerate(AgentLifecycle._SHUTDOWN_STEPS)
            if name.startswith("stop")
        ]
        if flush_indices and stop_indices:
            assert max(flush_indices) < min(stop_indices), \
                "flush steps must run before stop steps"


class TestStartupSteps:
    """验证 _STARTUP_STEPS 数据驱动列表"""

    def test_steps_not_empty(self):
        assert len(AgentLifecycle._STARTUP_STEPS) > 0

    def test_all_step_names_unique(self):
        names = [name for name, _ in AgentLifecycle._STARTUP_STEPS]
        assert len(names) == len(set(names))

    def test_all_method_names_exist_on_class(self):
        for name, method_name in AgentLifecycle._STARTUP_STEPS:
            assert hasattr(AgentLifecycle, method_name), \
                f"Step '{name}': method '{method_name}' not found"
```

- [ ] **Step 7: Run lifecycle steps tests**

```bash
pytest core/agent/tests/test_lifecycle_steps.py -v
```
Expected: 7 passed

- [ ] **Step 8: Run all L1 tests**

```bash
pytest core/agent/tests/test_agent_fsm.py core/agent/tests/test_types.py core/agent/tests/test_lifecycle_steps.py -v
```
Expected: 22 passed

- [ ] **Step 9: Commit**

```bash
git add core/agent/tests/__init__.py core/agent/tests/test_agent_fsm.py core/agent/tests/test_types.py core/agent/tests/test_lifecycle_steps.py
git commit -m "test: agent/ L1 — FSM + types + lifecycle steps (22 tests)"
```

---

### Task 4: agent/ L2 测试 — flush + banner + agent methods + conftest

**Files:**
- Create: `core/agent/tests/conftest.py`
- Create: `core/agent/tests/test_lifecycle_flush.py`
- Create: `core/agent/tests/test_server_banner.py`
- Create: `core/agent/tests/test_agent_methods.py`

**Interfaces:**
- Consumes: `core.agent.lifecycle.AgentLifecycle`, `core.agent.server.AgentServer`, `core.agent.agent.Agent`, `unittest.mock`
- Produces: 16 个 L2 service 测试，fake_agent fixture

- [ ] **Step 1: Write conftest.py with fake_agent fixture**

```python
# core/agent/tests/conftest.py
"""agent/ 测试共享 fixtures。"""

import asyncio
from collections import deque
from unittest.mock import MagicMock, AsyncMock
import pytest


@pytest.fixture
def fake_agent():
    """返回一个属性可动态设置的 mock agent——模拟 30+ 子系统。

    测试只需设自己关心的属性，其余保持默认。
    """
    a = MagicMock()
    # 持久化
    a._consolidator = None
    a._character_engine = None
    a._memory_reflector = None
    # session（带 deque messages）
    a._session = MagicMock()
    a._session.save = AsyncMock()
    a._session.messages = deque(maxlen=20)
    # event bus + fsm
    a._bus = MagicMock()
    a._bus.post = MagicMock()
    a._fsm = MagicMock()
    a._fsm.state = MagicMock()
    # 后台任务
    a._background_tasks = {}
    a._task_system = MagicMock()
    a._task_system.list_active_tasks = AsyncMock(return_value=[])
    a._task_system.get_task_mailbox = MagicMock()
    # config（banner 测试用）
    a._config = MagicMock()
    a._character = MagicMock()
    a._llm = MagicMock()
    a._long_term = MagicMock()
    a._tool_adapter = MagicMock()
    return a
```

- [ ] **Step 2: Write test_lifecycle_flush.py**

```python
# core/agent/tests/test_lifecycle_flush.py
"""AgentLifecycle flush 方法 mock 测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.agent.lifecycle import AgentLifecycle

pytestmark = pytest.mark.service


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
        fake_agent._character_engine = MagicMock()
        fake_agent._character_engine.flush_reflector = AsyncMock()
        await AgentLifecycle._flush_reflector(fake_agent)
        fake_agent._character_engine.flush_reflector.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_engine_does_not_raise(self, fake_agent):
        fake_agent._character_engine = None
        await AgentLifecycle._flush_reflector(fake_agent)
        # 不应抛异常


class TestFlushCompression:
    @pytest.mark.asyncio
    async def test_flush_delegates_to_engine(self, fake_agent):
        fake_agent._character_engine = MagicMock()
        fake_agent._character_engine.flush_compression = AsyncMock()
        await AgentLifecycle._flush_compression(fake_agent)
        fake_agent._character_engine.flush_compression.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_engine_does_not_raise(self, fake_agent):
        fake_agent._character_engine = None
        await AgentLifecycle._flush_compression(fake_agent)
        # 不应抛异常
```

- [ ] **Step 3: Run flush tests**

```bash
pytest core/agent/tests/test_lifecycle_flush.py -v
```
Expected: 7 passed

- [ ] **Step 4: Write test_server_banner.py**

```python
# core/agent/tests/test_server_banner.py
"""AgentServer._print_startup_banner 测试。"""

from unittest.mock import MagicMock
import pytest
from core.agent.server import AgentServer

pytestmark = pytest.mark.service


class TestStartupBanner:
    def _setup_banner_attrs(self, agent):
        """设置 _print_startup_banner 需要的实例属性。"""
        agent._config.tts.model = "test-tts/model"
        agent._character.name = "测试角色"
        agent._character.base_personality = "我是测试人格文本"
        agent._llm.model = "test-llm/model"
        agent._long_term.count = 0
        agent._tool_adapter.execute_now = MagicMock(return_value=["echo", "web_search"])

    def test_both_databases_ok(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=True, milvus_ok=True)
        assert "MongoDB" in banner
        assert "Milvus" in banner
        assert "测试角色" in banner
        assert "test-llm/model" in banner
        assert "2 个已加载" in banner

    def test_mongo_offline(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=False, milvus_ok=True)
        assert "Milvus" in banner

    def test_both_offline_shows_in_memory(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=False, milvus_ok=False)
        assert "in-memory" in banner.lower()

    def test_truncates_long_personality(self, fake_agent):
        self._setup_banner_attrs(fake_agent)
        fake_agent._character.base_personality = "A" * 100
        banner = AgentServer._print_startup_banner(
            fake_agent, mongo_ok=True, milvus_ok=True)
        assert "A" * 60 in banner
        assert "..." in banner
```

- [ ] **Step 5: Run banner tests**

```bash
pytest core/agent/tests/test_server_banner.py -v
```
Expected: 4 passed

- [ ] **Step 6: Write test_agent_methods.py**

```python
# core/agent/tests/test_agent_methods.py
"""Agent 实例方法 mock 测试。

注：Agent 是顶层类（继承 AgentLifecycle + AgentServer），
方法定义在 agent.py 中。
"""

from collections import deque
from unittest.mock import MagicMock, AsyncMock
import pytest
from core.agent.agent import Agent

pytestmark = pytest.mark.service


class TestPurgeMessageFromSession:
    def test_purge_existing_message(self, fake_agent):
        """从 session deque 移除匹配 message_id 的消息"""
        msg1 = MagicMock(message_id="id-1")
        msg2 = MagicMock(message_id="id-2")
        msg3 = MagicMock(message_id="id-3")
        fake_agent._session.messages = deque([msg1, msg2, msg3], maxlen=20)

        purged = Agent._purge_message_from_session(fake_agent, "id-2")
        assert purged == 1
        assert len(fake_agent._session.messages) == 2
        remaining_ids = [m.message_id for m in fake_agent._session.messages]
        assert "id-2" not in remaining_ids
        assert "id-1" in remaining_ids
        assert "id-3" in remaining_ids

    def test_purge_nonexistent_message(self, fake_agent):
        msg1 = MagicMock(message_id="id-1")
        fake_agent._session.messages = deque([msg1], maxlen=20)

        purged = Agent._purge_message_from_session(fake_agent, "no-such-id")
        assert purged == 0
        assert len(fake_agent._session.messages) == 1

    def test_purge_all_messages(self, fake_agent):
        msg1 = MagicMock(message_id="id-1")
        fake_agent._session.messages = deque([msg1], maxlen=20)

        purged = Agent._purge_message_from_session(fake_agent, "id-1")
        assert purged == 1
        assert len(fake_agent._session.messages) == 0


class TestRegisterBackgroundTask:
    def test_register_stores_entry(self, fake_agent):
        fake_agent._background_tasks = {}

        Agent._register_background_task(
            fake_agent, "task-1",
            mailbox=MagicMock(),
            intent="test intent",
            dispatched_at="2026-01-01T00:00:00Z",
        )
        assert "task-1" in fake_agent._background_tasks
        entry = fake_agent._background_tasks["task-1"]
        assert entry.intent == "test intent"
        assert entry.dispatched_at == "2026-01-01T00:00:00Z"

    def test_register_multiple_tasks(self, fake_agent):
        fake_agent._background_tasks = {}

        Agent._register_background_task(
            fake_agent, "task-1", MagicMock(), "intent-1", "t1")
        Agent._register_background_task(
            fake_agent, "task-2", MagicMock(), "intent-2", "t2")
        assert len(fake_agent._background_tasks) == 2
```

- [ ] **Step 7: Run agent methods tests**

```bash
pytest core/agent/tests/test_agent_methods.py -v
```
Expected: 5 passed

- [ ] **Step 8: Run all L2 tests**

```bash
pytest core/agent/tests/test_lifecycle_flush.py core/agent/tests/test_server_banner.py core/agent/tests/test_agent_methods.py -v
```
Expected: 16 passed

- [ ] **Step 9: Run full agent suite**

```bash
pytest core/agent/tests/ -v
```
Expected: 38 passed

- [ ] **Step 10: Commit**

```bash
git add core/agent/tests/conftest.py core/agent/tests/test_lifecycle_flush.py core/agent/tests/test_server_banner.py core/agent/tests/test_agent_methods.py
git commit -m "test: agent/ L2 — flush + banner + purge/register (16 tests)"
```

---

### Task 5: 测试文件迁移 — 4 files → module tests/

**Files:**
- Move: `core/tests/test_token_tracker.py` → `core/monitoring/tests/test_token_tracker.py`
- Move: `core/tests/test_task_scheduler.py` → `core/task/tests/test_task_scheduler.py`
- Move: `core/tests/test_task_model.py` → `core/task/tests/test_task_model.py`
- Move: `core/tests/test_mood_engine.py` → `core/cognitive/tests/test_mood_engine.py`
- Create: `core/monitoring/tests/__init__.py`
- Create: `core/task/tests/__init__.py`

- [ ] **Step 1: Create target directories**

```bash
mkdir -p core/monitoring/tests core/task/tests
touch core/monitoring/tests/__init__.py core/task/tests/__init__.py
```

- [ ] **Step 2: Move test_token_tracker.py → core/monitoring/tests/**

```bash
git mv core/tests/test_token_tracker.py core/monitoring/tests/test_token_tracker.py
```

验证——文件使用绝对 import `from core.monitoring.token_tracker import ...`，无需修改。
```bash
pytest core/monitoring/tests/test_token_tracker.py -v
```

- [ ] **Step 3: Move task tests → core/task/tests/**

```bash
git mv core/tests/test_task_scheduler.py core/task/tests/test_task_scheduler.py
git mv core/tests/test_task_model.py core/task/tests/test_task_model.py
pytest core/task/tests/ -v
```

- [ ] **Step 4: Move test_mood_engine.py → core/cognitive/tests/**

```bash
git mv core/tests/test_mood_engine.py core/cognitive/tests/test_mood_engine.py
pytest core/cognitive/tests/test_mood_engine.py -v
```

- [ ] **Step 5: Run all migrated + verify core/tests/**

```bash
pytest core/monitoring/tests/ core/task/tests/ core/cognitive/tests/test_mood_engine.py core/tests/ -v
```

- [ ] **Step 6: Commit**

```bash
git add core/monitoring/tests/__init__.py core/task/tests/__init__.py
git commit -m "test: 迁移 4 个测试文件到模块内 tests/ 目录

- test_token_tracker.py → core/monitoring/tests/
- test_task_scheduler.py + test_task_model.py → core/task/tests/
- test_mood_engine.py → core/cognitive/tests/"
```

---

### Task 6: 文档更新 — testing.md + CLAUDE.md + todo.md + completed.md

**Files:**
- Modify: `docs/testing.md`
- Modify: `CLAUDE.md`
- Modify: `todo.md`
- Modify: `docs/completed.md`

- [ ] **Step 1: Update docs/testing.md — 按目录运行命令区**

在 `pytest core/tests/ -v` 行下方追加：
```
pytest core/agent/tests/ -v                  # Agent 生命周期
pytest core/config/tests/ -v                 # 配置 dataclass
pytest core/monitoring/tests/ -v             # Token 监控
pytest core/task/tests/ -v                   # Task 调度
```

- [ ] **Step 2: Update docs/testing.md — core/ 层表格**

移除 `core/tests/` 下的迁移行：`test_task_model.py`、`test_task_scheduler.py`、`test_token_tracker.py`。

新增目录分组：
```
| `core/agent/tests/` | `test_agent_fsm.py` | 10 | unit |
| | `test_types.py` | 5 | unit |
| | `test_lifecycle_steps.py` | 7 | unit |
| | `test_lifecycle_flush.py` | 7 | service |
| | `test_server_banner.py` | 4 | service |
| | `test_agent_methods.py` | 5 | service |
| `core/config/tests/` | `test_database_config.py` | 6 | unit |
| | `test_app_config.py` | 4 | unit |
| | `test_character_config.py` | 8 | unit |
| `core/monitoring/tests/` | `test_token_tracker.py` | 17 | unit |
| `core/task/tests/` | `test_task_model.py` | 15 | unit |
| | `test_task_scheduler.py` | 26 | service+slow |
```

更新文件计数：`core/ 层（24 文件）` → `core/ 层（31 文件）`

- [ ] **Step 3: Update CLAUDE.md — pytest 按模块运行命令**

在 `# 其他模块` 之前追加：
```
python -m pytest core/agent/tests/ -v
python -m pytest core/config/tests/ -v
python -m pytest core/monitoring/tests/ -v
python -m pytest core/task/tests/ -v
```

- [ ] **Step 4: Update CLAUDE.md — 文件结构树**

在 `core/agent/`、`core/config/`、`core/monitoring/`、`core/task/` 下补 `tests/` 目录。

- [ ] **Step 5: Update todo.md**

将以下 4 行标记为 ✅（`~~高~~ ✅ \| **~~...~~**`）：
- `core/agent/ 零测试`
- `core/config/ 零测试`
- `core/monitoring/ 零测试`
- `core/task/ 零测试`

- [ ] **Step 6: Update docs/completed.md**

在最近 2026-07-29 区段新增：
```
### 2026-07-29: core/ 测试补全 & 迁移 — agent+config +55 tests

**新增**：core/config/tests/ (18 tests) + core/agent/tests/ (38 tests)
**迁移**：test_token_tracker → core/monitoring/tests/,
test_task_scheduler + test_task_model → core/task/tests/,
test_mood_engine → core/cognitive/tests/
**文档**：testing.md + CLAUDE.md 更新
```

- [ ] **Step 7: Verify make test-quick**

```bash
make test-quick
```

- [ ] **Step 8: Commit**

```bash
git add docs/testing.md CLAUDE.md todo.md docs/completed.md
git commit -m "docs: 更新 testing.md + CLAUDE.md — 新测试目录 + 迁移"
```

---
