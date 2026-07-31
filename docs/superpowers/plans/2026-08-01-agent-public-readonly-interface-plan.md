# Agent 公开只读接口实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `Agent` 添加 27 个公开 `@property`（23 子系统对象 + 4 标量）+ 2 个状态写方法 + `purge_message_from_session()` 公开化，并迁移全部跨模块消费方（点式 `agent._xxx` + `getattr(agent, "_xxx")`），使 `agent._xxx` 只剩深读豁免点。

**Architecture:** `Agent` 是组合根，子系统已有成熟公开 API——本次只补「通往子系统的公开路径」。在 `core/agent/agent.py` 加同名 `@property` 直通（`return self._xxx`）；`/shutdown`、`/restart` 的 `_state` 写入改为 `request_shutdown()`/`request_restart()` 公开方法。消费方（`core/input` + `debug_routes` + `debug/info.py`）从 `agent._xxx` 迁移到 `agent.xxx`。debug 层深度下钻（`agent.<sub>._<attr>`）保留 `_` 访问并标注 `# noqa: SLF001` 豁免。

**Tech Stack:** Python 3.13+，pytest（`pytest.mark.service`），conda env `Elysia`。

## Global Constraints

- `make test-quick` 全绿（迁移不改行为，只改属性访问路径）
- 测试 marker 分层：实例化 `Agent`（不调 `start()`）→ `pytest.mark.service`（见 `core/agent/tests/test_agent_methods.py` 范例）
- 新测试文件：`core/agent/tests/test_public_interface.py`，声明 `pytestmark = pytest.mark.service`
- 深读豁免方式：`# noqa: SLF001`（ruff private-member-access）标注 + 文档声明 debug 层为显式内省层
- 不新增依赖，不加行为聚合接口，不强制只读视图
- 项目惯例：直接在 main 上提交（不建 worktree），conda env `Elysia`

---

### Task 1: Agent 加公开接口 + 写方法 + 方法公开化

**Files:**
- Modify: `core/agent/agent.py` — 在文件末尾（`process_message` 等公开方法之后）新增 property 块 + 写方法 + `purge_message_from_session` 公开化
- Test: `core/agent/tests/test_public_interface.py`（新建）

**Interfaces:**
- Produces: 27 个 `@property` + `request_shutdown()`/`request_restart()` + `purge_message_from_session(msg_id) -> int`。后续 Task 2-4 消费这些接口。

- [ ] **Step 1: 写失败测试 `core/agent/tests/test_public_interface.py`**

```python
"""Agent 公开只读接口测试。

验证 27 个 @property 存在且返回正确的内部对象，以及状态写方法。
因实例化 Agent（不调 start()），标记 service。
注：启动期创建的 4 个成员（token_tracker/prompt_recorder/response_recorder/started_at）
在 start() 前不存在，需单独断言 AttributeError。
"""

import pytest
from core.agent import Agent
from core.agent._types import RunState
from core.config import AppConfig

pytestmark = pytest.mark.service

# (property 名, 内部属性名, 是否启动期创建)
SUBSYSTEM_PROPS = [
    ("session", "_session", False),
    ("long_term", "_long_term", False),
    ("message_repo", "_message_repo", False),
    ("session_repo", "_session_repo", False),
    ("memory_repo", "_memory_repo", False),
    ("token_repo", "_token_repo", False),
    ("tool_adapter", "_tool_adapter", False),
    ("output", "_output", False),
    ("llm", "_llm", False),
    ("tts", "_tts", False),
    ("config", "_config", False),
    ("bus", "_bus", False),
    ("cognitive", "_cognitive", False),
    ("idle", "_idle", False),
    ("scheduler", "_scheduler", False),
    ("consolidator", "_consolidator", False),
    ("memory_reflector", "_memory_reflector", False),
    ("character_engine", "_character_engine", False),
    ("character_reflector", "_character_reflector", False),
    ("character", "_character", False),
]
STARTUP_PROPS = [
    ("token_tracker", "_token_tracker"),
    ("prompt_recorder", "_prompt_recorder"),
    ("response_recorder", "_response_recorder"),
    ("started_at", "_started_at"),
]
SCALAR_PROPS = [("user_name", "_user_name"), ("state", "_state"), ("stt_ok", "_stt_ok")]


@pytest.fixture
def agent():
    return Agent(config=AppConfig())


class TestSubsystemProperties:
    @pytest.mark.parametrize("pub,priv,_", SUBSYSTEM_PROPS)
    def test_returns_internal_object(self, agent, pub, priv):
        assert getattr(agent, pub) is getattr(agent, priv)


class TestStartupProperties:
    @pytest.mark.parametrize("pub,priv", STARTUP_PROPS)
    def test_raises_before_start(self, agent, pub, priv):
        with pytest.raises(AttributeError):
            getattr(agent, pub)


class TestScalarProperties:
    @pytest.mark.parametrize("pub,priv", SCALAR_PROPS)
    def test_returns_value(self, agent, pub, priv):
        assert getattr(agent, pub) == getattr(agent, priv)


class TestWriteMethods:
    def test_request_shutdown_sets_state(self, agent):
        agent.request_shutdown()
        assert agent.state == RunState.STOPPING

    def test_request_restart_sets_state(self, agent):
        agent.request_restart()
        assert agent.state == RunState.RESTARTING


class TestPurgeMessagePublic:
    def test_purge_is_public_method(self, agent):
        assert callable(agent.purge_message_from_session)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `conda run -n Elysia python -m pytest core/agent/tests/test_public_interface.py -q --tb=short`
Expected: FAIL with `AttributeError: 'Agent' object has no attribute 'session'` 等

- [ ] **Step 3: 实现接口**

在 `core/agent/agent.py` 末尾新增：

```python
    # ---- 公开只读接口（薄 getter 直通）----
    # 27 个 @property：通往 Agent 组合根持有的子系统，可写性由子系统 API 自管。

    @property
    def session(self):
        """当前会话内存。"""
        return self._session

    @property
    def long_term(self):
        """长期记忆（Milvus 向量库）。"""
        return self._long_term

    @property
    def message_repo(self):
        """消息归档仓库。"""
        return self._message_repo

    @property
    def session_repo(self):
        """会话仓库。"""
        return self._session_repo

    @property
    def memory_repo(self):
        """长期记忆仓库。"""
        return self._memory_repo

    @property
    def token_repo(self):
        """token 用量仓库。"""
        return self._token_repo

    @property
    def tool_adapter(self):
        """工具子系统适配器。"""
        return self._tool_adapter

    @property
    def output(self):
        """多通道输出管理器。"""
        return self._output

    @property
    def llm(self):
        """LLM 适配器。"""
        return self._llm

    @property
    def tts(self):
        """TTS 流管理器（启动后才有，可能为 None）。"""
        return self._tts

    @property
    def config(self):
        """应用配置。"""
        return self._config

    @property
    def bus(self):
        """事件总线。"""
        return self._bus

    @property
    def cognitive(self):
        """认知循环。"""
        return self._cognitive

    @property
    def idle(self):
        """空闲监控。"""
        return self._idle

    @property
    def scheduler(self):
        """任务调度器。"""
        return self._scheduler

    @property
    def consolidator(self):
        """记忆整合器。"""
        return self._consolidator

    @property
    def memory_reflector(self):
        """长期记忆反思。"""
        return self._memory_reflector

    @property
    def character_engine(self):
        """角色引擎。"""
        return self._character_engine

    @property
    def character_reflector(self):
        """角色反思器。"""
        return self._character_reflector

    @property
    def character(self):
        """角色配置（L1 Core + config）。"""
        return self._character

    @property
    def token_tracker(self):
        """token 统计（启动时创建）。"""
        return self._token_tracker

    @property
    def prompt_recorder(self):
        """LLM prompt 记录器（启动时创建）。"""
        return self._prompt_recorder

    @property
    def response_recorder(self):
        """LLM 响应记录器（启动时创建）。"""
        return self._response_recorder

    @property
    def user_name(self):
        """注入的用户名。"""
        return self._user_name

    @property
    def state(self):
        """Agent 运行状态（只读）。"""
        return self._state

    @property
    def started_at(self):
        """启动时间戳（启动时创建）。"""
        return self._started_at

    @property
    def stt_ok(self):
        """STT 可用标志。"""
        return self._stt_ok

    # ---- 状态写方法（引导主循环退出，不做清理）----

    def request_shutdown(self) -> None:
        """请求关机——设 _state = STOPPING，引导主循环退出。"""
        self._state = RunState.STOPPING

    def request_restart(self) -> None:
        """请求重启——设 _state = RESTARTING，引导主循环退出。"""
        self._state = RunState.RESTARTING
```

同时把 `_purge_message_from_session` 改名 `purge_message_from_session`（去掉下划线）。**同步更新 3 处现有测试引用**（`core/agent/tests/test_agent_methods.py:23,35,43`——`Agent._purge_message_from_session(fake_agent, ...)` → `Agent.purge_message_from_session(fake_agent, ...)`），否则改名立即破坏这 3 个 service 测试。`debug_routes/archive.py:84` 的调用延迟到 Task 3。

- [ ] **Step 4: 运行测试确认通过**

Run: `conda run -n Elysia python -m pytest core/agent/tests/test_public_interface.py core/agent/tests/test_agent_methods.py -q --tb=short`
Expected: PASS（`test_startup_*` 4 个验证 `AttributeError`，其余验证 `is` 同一对象；test_agent_methods 3 个 purge 测试通过）

- [ ] **Step 5: 提交**

```bash
git add core/agent/agent.py core/agent/tests/test_public_interface.py core/agent/tests/test_agent_methods.py
git commit -m "feat(agent): 新增 27 个公开只读 property + request_shutdown/restart + purge 公开化"
```

---

### Task 2: 迁移 core/input 层（ws_handler/server/api_routes）

**Files:**
- Modify: `core/input/ws_handler.py`（17 处 + 3 深读豁免）
- Modify: `core/input/server.py`（3 浅读 + 2 写迁移 + 3 深读豁免）
- Modify: `core/input/api_routes.py`（3 处）

**Interfaces:**
- Consumes: Task 1 的 `agent.session`/`agent.output`/`agent.bus`/`agent.user_name`/`agent.llm`/`agent.tts`/`agent.config`/`agent.idle`/`agent.tool_adapter`/`agent.token_tracker`/`agent.long_term`/`agent.message_repo` + `request_shutdown()`/`request_restart()`
- Produces: 无（纯迁移）

- [ ] **Step 1: 迁移 ws_handler.py 浅读（agent._xxx → agent.xxx）**

逐行替换以下 `_` 前缀（保留深读豁免点 `_manager`/`_channels`）：

| 行 | 改前 | 改后 |
|----|------|------|
| 52 | `agent._output.add_channel(channel)` | `agent.output.add_channel(channel)` |
| 56 | `agent._session.messages_for_client()` | `agent.session.messages_for_client()` |
| 58 | `hasattr(agent, '_token_tracker') and agent._token_tracker` | `hasattr(agent, 'token_tracker') and agent.token_tracker` |
| 59 | `agent._token_tracker.get_session_stats()` | `agent.token_tracker.get_session_stats()` |
| 69 | `agent._tts is not None` | `agent.tts is not None` |
| 70 | `agent._llm.model` | `agent.llm.model` |
| 71 | `agent._config.live2d.static_image` | `agent.config.live2d.static_image` |
| 78 | `hasattr(agent, '_tool_adapter') and agent._tool_adapter` | `hasattr(agent, 'tool_adapter') and agent.tool_adapter` |
| 79 | `agent._tool_adapter._manager.get_pending_approvals()` | `agent.tool_adapter._manager.get_pending_approvals()`  `# noqa: SLF001` |
| 164 | `agent._output.remove_channel(channel)` | `agent.output.remove_channel(channel)` |
| 189 | `user_name=agent._user_name` | `user_name=agent.user_name` |
| 210 | `for ch in agent._output._channels:` | `for ch in agent.output._channels:  # noqa: SLF001` |
| 227 | `agent._tool_adapter._manager.submit_approval(...)` | `agent.tool_adapter._manager.submit_approval(...)  # noqa: SLF001` |
| 247 | `agent._bus.post(...)` | `agent.bus.post(...)` |
| 285 | `agent._idle.touch()` | `agent.idle.touch()` |
| 286 | `agent._bus.post(...)` | `agent.bus.post(...)` |
| 287 | `user_name=agent._user_name` | `user_name=agent.user_name` |

- [ ] **Step 2: 迁移 server.py**

浅读：
| 行 | 改前 | 改后 |
|----|------|------|
| 145 | `getattr(agent._llm, 'model', 'unknown')` | `getattr(agent.llm, 'model', 'unknown')` |
| 147 | `getattr(agent._session, '_messages', [])` | `getattr(agent.session, '_messages', [])`（`'_messages'` 字符串保留） |
| 148 | `getattr(agent._long_term, 'count', 0)` | `getattr(agent.long_term, 'count', 0)` |
| 150 | `getattr(agent._tool_adapter._manager, 'tool_names', ...)` | `getattr(agent.tool_adapter._manager, 'tool_names', ...)  # noqa: SLF001` |
| 208 | `agent._tool_adapter._manager.plugin_list` | `agent.tool_adapter._manager.plugin_list  # noqa: SLF001` |
| 237 | `await agent._tool_adapter._manager.reload_plugin(name)` | `await agent.tool_adapter._manager.reload_plugin(name)  # noqa: SLF001` |

写迁移：
| 行 | 改前 | 改后 |
|----|------|------|
| 170 | `request.app["agent"]._state = RunState.STOPPING` | `request.app["agent"].request_shutdown()` |
| 190 | `request.app["agent"]._state = RunState.RESTARTING` | `request.app["agent"].request_restart()` |

同时删除不再使用的 `RunState` import（若迁移后无其他引用）。

- [ ] **Step 3: 迁移 api_routes.py**

| 行 | 改前 | 改后 |
|----|------|------|
| 206 | `getattr(agent._config, "server", None)` | `getattr(agent.config, "server", None)` |
| 259 | `agent._user_name` | `agent.user_name` |
| 284 | `repo = agent._message_repo` | `repo = agent.message_repo` |

- [ ] **Step 4: 验证**

Run: `conda run -n Elysia python -m pytest core/tests/test_phase1.py core/tests/test_phase2.py core/tests/test_phase3.py -q --tb=short`
Expected: PASS（这些测试覆盖 agent 生命周期 + ws/server 集成路径）

Run: `grep -n "agent\._" core/input/ws_handler.py core/input/server.py core/input/api_routes.py`
Expected: 仅剩 `_manager`/`_channels` 深读豁免点（带 `# noqa: SLF001`）

- [ ] **Step 5: 提交**

```bash
git add core/input/ws_handler.py core/input/server.py core/input/api_routes.py
git commit -m "refactor(input): 迁移 core/input 层 agent._xxx → 公开接口（含 _state 写迁移）"
```

---

### Task 3: 迁移 debug_routes（40 处点式 + 8 处 getattr）

**Files:**
- Modify: `core/input/debug_routes/archive.py`（19 处）
- Modify: `core/input/debug_routes/ltm.py`（5 处 + 1 深读豁免）
- Modify: `core/input/debug_routes/overview.py`（6 处）
- Modify: `core/input/debug_routes/character.py`（2 点式 + 2 getattr + 6 深读豁免）
- Modify: `core/input/debug_routes/recording.py`（4 getattr）

**Interfaces:**
- Consumes: Task 1 的 `agent.session`/`agent.message_repo`/`agent.session_repo`/`agent.token_repo`/`agent.long_term`/`agent.memory_repo`/`agent.purge_message_from_session`/`agent.token_tracker`/`agent.prompt_recorder`/`agent.response_recorder`/`agent.character_engine`

- [ ] **Step 1: archive.py 迁移**

逐行替换（全部浅读，无深读）：
`_message_repo`→`message_repo`（行 38,77,101,124,197,200）、`_session`→`session`（行 86,126,150,151,216,219,220,251,252,253,276）、`_session_repo`→`session_repo`（行 145,191,213,245,249,270,274）、`_token_repo`→`token_repo`（行 158）、`_purge_message_from_session`→`purge_message_from_session`（行 84）

- [ ] **Step 2: ltm.py 迁移**

行 38 `agent._long_term is None` → `agent.long_term is None`；行 84,102,124,139 `_long_term` → `long_term`；行 40 `agent._long_term._entries` → `agent.long_term._entries  # noqa: SLF001`（深读豁免）

- [ ] **Step 3: overview.py 迁移**

行 41 `agent._long_term` → `agent.long_term`；行 45 `agent._memory_repo` → `agent.memory_repo`；行 50,51,54,55 `_session`/`_session_repo`/`_message_repo` → 去 `_`

- [ ] **Step 4: character.py 迁移 + 深读豁免**

点式浅读：
行 41,68 `agent._session.session_id` → `agent.session.session_id`；行 38,65 `getattr(agent, "_character_engine", None)` → `getattr(agent, "character_engine", None)`

深读豁免（`engine` 来自 `getattr(agent, "character_engine", None)` 的局部变量，`_` 访问保留并标注 `# noqa: SLF001`）：
- 行 42 `engine._version_repo.list_versions(...)`
- 行 46 `engine._version_repo.count_versions(...)`
- 行 69 `engine._version_repo.get_version(...)`
- 行 80 `engine._version_repo.append_version(...)`
- 行 90 `engine._refresh_c6()`

- [ ] **Step 5: recording.py 迁移**

行 30,47 `getattr(agent, "_prompt_recorder", None)` → `getattr(agent, "prompt_recorder", None)`；行 90,107 `getattr(agent, "_response_recorder", None)` → `getattr(agent, "response_recorder", None)`

- [ ] **Step 6: 验证**

Run: `conda run -n Elysia python -m pytest core/input/ -q --tb=short 2>&1 | tail -5`
Expected: 无 FAIL（debug_routes 无独立测试，此步验证 import 不炸）

Run: `grep -rn "agent\._\|\._version_repo\|\._refresh_c6" core/input/debug_routes/ | grep -v noqa`
Expected: 空（所有深读点均已带 `# noqa: SLF001`）

- [ ] **Step 7: 提交**

```bash
git add core/input/debug_routes/
git commit -m "refactor(debug_routes): 迁移 agent._xxx → 公开接口 + getattr 去下划线"
```

---

### Task 4: 迁移 debug/info.py + 深读豁免标注

**Files:**
- Modify: `core/debug/info.py`（浅读迁移 + 深读豁免）

**Interfaces:**
- Consumes: Task 1 全部 27 property + `request_shutdown`/`request_restart` 不涉及

- [ ] **Step 1: 浅读迁移（agent._xxx → agent.xxx）**

| 行 | 改前 | 改后 |
|----|------|------|
| 160 | `agent._started_at` | `agent.started_at` |
| 163 | `im = agent._idle` | `im = agent.idle` |
| 170 | `agent._session.messages_for_client()` | `agent.session.messages_for_client()` |
| 174 | `agent._message_repo.count()` | `agent.message_repo.count()` |
| 186 | `agent._session.character_state.model_dump(...)` | `agent.session.character_state.model_dump(...)` |
| 189 | `agent._long_term.count` | `agent.long_term.count` |
| 201 | `agent._memory_repo.health_check()` | `agent.memory_repo.health_check()` |
| 203 | `agent._session_repo.health_check()` | `agent.session_repo.health_check()` |
| 215 | `agent._character.name` | `agent.character.name` |
| 216 | `agent._character.base_personality` | `agent.character.base_personality` |
| 221 | `pe = agent._character_engine` | `pe = agent.character_engine` |
| 233 | `getattr(agent, "_character_engine", None)` | `getattr(agent, "character_engine", None)` |
| 235 | `agent._session.session_id` | `agent.session.session_id` |
| 243 | `getattr(agent, "_character_engine", None)` | `getattr(agent, "character_engine", None)` |
| 246 | `agent._session.session_id` | `agent.session.session_id` |
| 251 | `ref = agent._character_reflector` | `ref = agent.character_reflector` |
| 266 | `agent._tts is not None` | `agent.tts is not None` |
| 267 | `agent._stt_ok` | `agent.stt_ok` |
| 274 | `agent._llm.model` | `agent.llm.model` |
| 292 | `sched = agent._scheduler` | `sched = agent.scheduler` |
| 305 | `mr = agent._memory_reflector` | `mr = agent.memory_reflector` |
| 315 | `cons = agent._consolidator` | `cons = agent.consolidator` |
| 321 | `agent._session.character_state.mood` | `agent.session.character_state.mood` |
| 336 | `tracker = agent._token_tracker` | `tracker = agent.token_tracker` |
| 354 | `recorder = agent._prompt_recorder` | `recorder = agent.prompt_recorder` |
| 359 | `resp_recorder = agent._response_recorder` | `resp_recorder = agent.response_recorder` |

- [ ] **Step 2: 深读豁免标注（agent.<sub>._<attr> → 加 # noqa: SLF001）**

| 行 | 改前 | 改后 |
|----|------|------|
| 155 | `agent._cognitive._phase` | `agent.cognitive._phase  # noqa: SLF001` |
| 157 | `agent._cognitive._current_task` | `agent.cognitive._current_task  # noqa: SLF001` |
| 198 | `agent._long_term._entries[-5:]` | `agent.long_term._entries[-5:]  # noqa: SLF001` |
| 200 | `agent._long_term._repo` | `agent.long_term._repo  # noqa: SLF001` |
| 202 | `agent._long_term._embedding_provider._backend` | `agent.long_term._embedding_provider._backend  # noqa: SLF001` |
| 206 | `agent._tool_adapter._manager` | `agent.tool_adapter._manager  # noqa: SLF001` |
| 212 | `agent._consolidator._history` | `agent.consolidator._history  # noqa: SLF001` |
| 277 | `agent._cognitive._timing_history` | `agent.cognitive._timing_history  # noqa: SLF001` |
| 269 | `agent._output._channels` | `agent.output._channels  # noqa: SLF001` |
| 236 | `engine._version_repo`（局部变量 `engine`，来自 getattr） | 保持 `engine._version_repo` 并加 `# noqa: SLF001` |
| 245 | 同上 | 保持 `engine._version_repo` 并加 `# noqa: SLF001` |
| 303-316 | `mr._round_count` 等（经 `mr = agent.memory_reflector`） | 保持 `mr._xxx` 并加 `# noqa: SLF001` 于每行 |

- [ ] **Step 3: 验证**

Run: `conda run -n Elysia python -m pytest core/debug/ -q --tb=short 2>&1 | tail -5`
Expected: 无 FAIL（info.py 无独立测试，此步验证 import 不炸）

Run: `grep -rn "agent\._" core/ Module/ --include="*.py" | grep -v __pycache__ | grep -vE "tests/|test_|noqa"`
Expected: 空（全仓点式 `agent._xxx` 已清零，只剩带 noqa 豁免点）

- [ ] **Step 4: 提交**

```bash
git add core/debug/info.py
git commit -m "refactor(debug): 迁移 info.py 浅读 → 公开接口 + 深读豁免标注 SLF001"
```

---

### Task 5: 文档同步 + 最终验证

**Files:**
- Modify: `docs/superpowers/specs/2026-08-01-agent-public-readonly-interface-design.md` — 标注实现状态（可选）
- Modify: `CLAUDE.md` — Agent 接口说明（可选）
- Modify: `todo.md` — 模块化评估小节「接口暴露不足」标记已修

**Interfaces:**
- 无

- [ ] **Step 1: 更新 todo.md**

在「接口暴露不足」小节的修法部分追加：

```
**已实现（2026-08-01）**：Agent 加 27 个公开 property + request_shutdown/request_restart + purge 公开化；core/input + debug_routes + debug/info.py 全部浅读迁移；8 处 getattr 去下划线；深读豁免 `# noqa: SLF001` 标注。
```

- [ ] **Step 2: 全量验证**

Run: `make test-quick`
Expected: **1526 passed, 2 skipped, 76 deselected, 0 failed**

Run: `grep -rn "agent\._" core/ Module/ --include="*.py" | grep -v __pycache__ | grep -vE "tests/|test_|noqa"`
Expected: 空

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md todo.md
git commit -m "docs: 标注 Agent 公开接口已实现 + 模块化评估接口不足项闭环"
```

---

## Self-Review

**Spec coverage：**
- ✅ 27 property（Task 1 实现清单，与 spec 接口清单逐一对齐：23 子系统 + 4 标量）
- ✅ `request_shutdown`/`request_restart`（Task 1 + Task 2 server.py 写迁移）
- ✅ `purge_message_from_session` 公开化（Task 1 + Task 3 archive.py）
- ✅ core/input 点式浅读（Task 2：ws_handler 17 + server 3 + api_routes 3 = 23，server 另有 2 写迁移 + 3 深读豁免）
- ✅ debug_routes 点式浅读 + 8 处 getattr + 深读豁免（Task 3：archive 19 + ltm 5(+1豁免) + overview 6 + character 2(+6豁免) + recording 4）
- ✅ info.py 33 处浅读 + 深读豁免（Task 4）
- ✅ 测试 marker `pytest.mark.service`（Task 1）
- ✅ 深读豁免 `# noqa: SLF001`（Task 2/3/4）

**Placeholder scan：** 无 TBD/TODO；每行迁移给了具体行号与改前改后值。

**Type consistency：** `request_shutdown`/`request_restart` 在 Task 1 定义、Task 2 server.py 消费，签名一致；`purge_message_from_session(msg_id)` 在 Task 1 定义（保留原签名）、Task 3 archive.py 调用；27 个 property 名在 Task 1 定义、Task 2-4 消费，与 spec 清单逐一对齐。
