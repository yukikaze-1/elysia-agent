# agent + config 测试补全 & 测试文件迁移 — 设计文档

> 目标：为 `core/agent/`（1606 行，当前 0 测试）和 `core/config/`（263 行，当前 0 测试）补全测试，
> 同时将 `core/tests/` 中的 4 个测试文件迁移到模块内 `tests/` 目录。

## 1. 范围

### 新增测试

| 模块 | 目录 | 目标测试数 | marker | 难度 |
|------|------|-----------|--------|------|
| `config/` | `core/config/tests/` | ~18 | unit | 低 — 纯 dataclass，无外部依赖 |
| `agent/` | `core/agent/tests/` | ~34 | unit + service | 高 — 需 mock 30+ 子系统 |

### 测试文件迁移

| 原路径 | → 目标 | 现有测试数 |
|--------|--------|-----------|
| `core/tests/test_token_tracker.py` | `core/monitoring/tests/test_token_tracker.py` | ~6 |
| `core/tests/test_task_scheduler.py` | `core/task/tests/test_task_scheduler.py` | ~10 |
| `core/tests/test_task_model.py` | `core/task/tests/test_task_model.py` | ~8 |
| `core/tests/test_mood_engine.py` | `core/cognitive/tests/test_mood_engine.py` | ~4 |

### 保留不动

- `core/tests/test_phase1/2/3.py` — 跨模块集成测试
- `core/tests/test_log_setup.py` / `test_clean_db.py` — 工具性质
- `core/tests/fake_llm.py` — 共享 test double

### 文档更新

- `docs/testing.md` — 更新 core/ 层表格 + 运行命令
- `CLAUDE.md` — 更新测试目录列表 + pytest 命令
- `todo.md` — 标记完成
- `docs/completed.md` — 记录

## 2. config/ 测试设计

### 定位

纯 dataclass 验证，全部 `unit` 级别。不需要 mock、不需要外部服务。

### 数据库配置：`test_database_config.py`（~6 tests）

```python
class TestDatabaseConfigDefaults:
    """默认值验证"""
    def test_mongodb_uri_default(self):
        assert DatabaseConfig().mongodb_uri == "mongodb://localhost:27017"

    def test_milvus_uri_default(self):
        ...

    def test_milvus_token_default_empty(self):
        ...

    def test_db_name_default(self):
        ...

class TestDatabaseConfigEnvOverride:
    """环境变量覆盖——用 monkeypatch.setenv 设 env var，
    然后创建 ConfigField 验证 resolve 逻辑"""
    def test_mongodb_uri_from_env(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://custom:27017")
        cfg = ConfigLoader.load(config_class=DatabaseConfig, data={"base_url": ""})
        assert cfg.base_url == "mongodb://custom:27017"
```

### 应用配置：`test_app_config.py`（~4 tests）

```python
class TestAppConfigNesting:
    """AppConfig 嵌套 dataclass 默认值"""
    def test_database_section_exists(self):
        cfg = AppConfig()
        assert isinstance(cfg.database, DatabaseConfig)

    def test_all_top_level_sections_exist(self):
        """验证 10 个顶层段都有默认工厂"""
        cfg = AppConfig()
        assert cfg.agent is not None
        assert cfg.llm is not None
        ...
        assert cfg.database is not None
```

### 角色配置：`test_character_config.py`（~8 tests）

```python
class TestCharacterConfigYAML:
    """从临时 YAML 文件加载角色配置"""

    @pytest.fixture
    def char_dir(self, tmp_path):
        """创建最小角色目录：config.yaml + personality.md"""
        (tmp_path / "config.yaml").write_text("""
            name: 测试角色
            wake_words: ["你好", "测试"]
            personality:
              traits: ["温柔", "细心"]
        """)
        (tmp_path / "personality.md").write_text("我是一个测试角色。")
        return tmp_path

    def test_load_name(self, char_dir):
        cfg = CharacterConfig.load(char_dir / "config.yaml")
        assert cfg.name == "测试角色"

    def test_traits_property(self, char_dir):
        cfg = CharacterConfig.load(char_dir / "config.yaml")
        assert cfg.traits == ["温柔", "细心"]

    def test_missing_personality_md(self, tmp_path):
        """personality.md 不存在时 L1 文本为空"""
        ...

    def test_no_reference_dir(self, tmp_path):
        """无 reference/ 目录不报错"""
        ...
```

### 文件布局

```
core/config/tests/
├── __init__.py
├── conftest.py                  # tmp_path 角色目录 fixtures
├── test_database_config.py
├── test_app_config.py
└── test_character_config.py
```

## 3. agent/ 测试设计

### 分层策略

agent 的核心挑战：`Agent.__init__` 创建 30+ 子系统。不能每个测试都拉真对象。

**L1 — 纯单元（无需 Agent 实例）**
直接实例化被测类或调用纯函数，不涉及 Agent 实例化。

**L2 — service（mock Agent 属性）**
利用 `TYPE_CHECKING` 声明的属性动态赋给测试对象。
Python 的特性：给对象 `setattr` 动态创建属性，`cast(Agent, obj)` 骗过类型检查器。

```python
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

@pytest.fixture
def fake_agent():
    """返回一个属性可动态设置的 mock agent。"""
    a = MagicMock()
    a._consolidator = None
    a._character_engine = None
    a._session = AsyncMock()
    a._session.save = AsyncMock()
    a._bus = MagicMock()
    a._fsm = MagicMock()
    return a
```

**L3 — 集成（暂不做）**
需 MongoDB + Milvus + LLM + TTS 全部可用，保留在 `core/tests/test_phase*.py`。

### 第一层测试文件

#### `test_agent_fsm.py`（~8 tests）— unit

```python
class TestAgentFSM:
    """AgentFSM 状态机——纯单元测试"""
    
    def test_initial_state_is_idle(self):
        fsm = AgentFSM()
        assert fsm.state == AgentState.IDLE

    def test_idle_to_thinking(self):
        fsm = AgentFSM()
        assert fsm.can_transition(AgentState.THINKING)
        fsm.transition(AgentState.THINKING)
        assert fsm.state == AgentState.THINKING

    def test_thinking_to_speaking(self):
        ...

    def test_speaking_to_idle(self):
        ...

    def test_invalid_transition_raises(self):
        """非法状态转换抛 RuntimeError"""
        fsm = AgentFSM()
        with pytest.raises(RuntimeError):
            fsm.transition(AgentState.SPEAKING)  # IDLE → SPEAKING 需先 THINKING

    def test_transition_from_any_interrupted(self):
        """INTERRUPTED 可从 SPEAKING 或 EXECUTING 进入"""
        ...

    def test_reset(self):
        fsm = AgentFSM()
        fsm.transition(AgentState.THINKING)
        fsm.reset()
        assert fsm.state == AgentState.IDLE
```

#### `test_types.py`（~4 tests）— unit

```python
class TestPendingMessage:
    def test_fields(self): ...

class TestBackgroundTaskEntry:
    def test_fields(self): ...

class TestRunStateEnum:
    def test_all_states(self):
        assert RunState.STOPPED.value == ...
```

#### `test_lifecycle_steps.py`（~5 tests）— unit

```python
class TestShutdownSteps:
    """验证 _SHUTDOWN_STEPS 列表正确性"""
    
    def test_steps_not_empty(self):
        assert len(AgentLifecycle._SHUTDOWN_STEPS) > 0

    def test_all_steps_have_methods(self):
        """每个 step 的 method_name 对应 AgentLifecycle 或其 mixin 的方法"""
        ...

    def test_flush_before_stop(self):
        """flush 步骤排在 stop 步骤之前"""
        flush_indices = [i for i, (name, _) in enumerate(...) if name.startswith("flush")]
        stop_indices = [i for i, (name, _) in enumerate(...) if name.startswith("stop")]
        assert max(flush_indices) < min(stop_indices)
```

#### `test_server_banner.py`（~5 tests）— unit

```python
class TestStartupBanner:
    def test_both_ok(self):
        banner = AgentServer._print_startup_banner(
            AgentServer, mongo_ok=True, milvus_ok=True)
        assert "MongoDB" in banner and "Milvus" in banner

    def test_mongo_offline(self):
        banner = AgentServer._print_startup_banner(
            AgentServer, mongo_ok=False, milvus_ok=True)
        assert "offline" in banner.lower() or "❌" in banner
```

### 第二层测试文件

#### `test_lifecycle_flush.py`（~6 tests）— service

```python
class TestFlushConsolidator:
    @pytest.mark.asyncio
    async def test_flush_with_items(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.return_value = ["item1", "item2"]
        await AgentLifecycle._flush_consolidator(fake_agent)
        fake_agent._consolidator.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_timeout(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.side_effect = asyncio.TimeoutError
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常

    @pytest.mark.asyncio
    async def test_no_consolidator(self, fake_agent):
        fake_agent._consolidator = None
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常
```

#### `test_agent_methods.py`（~6 tests）— service

```python
class TestPurgeMessageFromSession:
    def test_purge_existing_message(self, fake_agent):
        """从 session deque 中移除匹配 message_id 的消息"""
        msg1 = MagicMock(message_id="id-1")
        msg2 = MagicMock(message_id="id-2")
        fake_agent._session = MagicMock()
        fake_agent._session.messages = deque([msg1, msg2], maxlen=20)

        count = Agent._purge_message_from_session(fake_agent, "id-1")
        assert count == 1
        assert len(fake_agent._session.messages) == 1

    def test_purge_nonexistent_message(self, fake_agent):
        ...

    def test_purge_all_messages(self, fake_agent):
        ...

class TestRegisterBackgroundTask:
    def test_register_stores_entry(self, fake_agent):
        fake_agent._background_tasks = {}
        fake_agent._task_system = MagicMock()
        fake_agent._task_system.list_active_tasks = AsyncMock(return_value=[])

        Agent._register_background_task(
            fake_agent, "task-1", MagicMock(), "test intent", "2026-01-01T00:00:00Z")
        assert "task-1" in fake_agent._background_tasks
```

### 文件布局

```
core/agent/tests/
├── __init__.py
├── conftest.py                 # fake_agent fixture + 共享 mock
├── test_agent_fsm.py           # L1: 状态机
├── test_types.py               # L1: dataclass
├── test_lifecycle_steps.py     # L1: STARTUP/SHUTDOWN 步骤验证
├── test_server_banner.py       # L1: banner 格式化
├── test_lifecycle_flush.py     # L2: flush 方法
└── test_agent_methods.py       # L2: purge/register
```

## 4. 测试文件迁移

### 迁移规则

1. 移动文件到目标目录
2. 调整 `import` 路径（相对路径可能变）
3. 调整 `conftest.py` / 共享 fixture（如有依赖 `core/tests/conftest.py` 的，改为模块内）
4. 验证 `pytest <new_dir>` 通过

### test_token_tracker.py → core/monitoring/tests/

```
原 import: from core.monitoring.token_tracker import TokenTracker
新 import: from ..token_tracker import TokenTracker（或保持绝对 import）
```

需创建 `core/monitoring/tests/__init__.py` 和 `conftest.py`（如需要）。

### test_task_scheduler.py → core/task/tests/

```
原 import: from core.task import TaskScheduler
新 import: from ..scheduler import TaskScheduler
```

需创建 `core/task/tests/__init__.py` 和 `conftest.py`。

### test_task_model.py → core/task/tests/

同上，测试 `Task` dataclass。

### test_mood_engine.py → core/cognitive/tests/

测试 `_apply_mood`（来自 `core.cognitive.loop`）。`core/cognitive/tests/` 已存在，直接加入。

### 迁移后 `core/tests/` 保留内容

```
core/tests/
├── __init__.py
├── fake_llm.py
├── conftest.py（如有）
├── test_phase1.py
├── test_phase2.py
├── test_phase3.py
├── test_log_setup.py
└── test_clean_db.py
```

## 5. 文档更新

### testing.md

1. "按目录运行" 命令区增加：
   ```
   pytest core/agent/tests/ -v
   pytest core/config/tests/ -v
   pytest core/monitoring/tests/ -v
   pytest core/task/tests/ -v
   ```

2. "core/ 层（24 文件）" 表格更新：
   - 移除已迁移的 4 行
   - 新增 `core/agent/tests/`、`core/config/tests/`、`core/monitoring/tests/`、`core/task/tests/` 行
   - 更新文件计数

### CLAUDE.md

- pytest 按模块运行命令：新增 `core/agent/`、`core/config/`、`core/monitoring/`、`core/task/` 条目
- 文件结构树：`core/agent/`、`core/config/`、`core/monitoring/`、`core/task/` 下补 `tests/` 目录
- marker 表不变化

### todo.md

- "core/agent/ 零测试" → ✅ 已修复
- "core/config/ 零测试" → ✅ 已修复
- "core/monitoring/ 零测试" → ✅ 已修复
- "core/task/ 零测试" → ✅ 已修复

### completed.md

- 新增条目记录此次工作

## 6. 验证

```bash
# 新增测试
pytest core/config/tests/ -v
pytest core/agent/tests/ -v

# 迁移后的测试
pytest core/monitoring/tests/ -v
pytest core/task/tests/ -v
pytest core/cognitive/tests/ -v

# 集成测试不退化
pytest core/tests/ -v

# make test-quick 通过
make test-quick
```

## 7. 不做的事

- 不给 agent 写集成测试（需 MongoDB + Milvus + LLM + TTS 全栈）
- 迁移 `test_phase1/2/3.py`（跨模块集成，留在 `core/tests/`）
- 改变 marker 体系或测试分层规则
