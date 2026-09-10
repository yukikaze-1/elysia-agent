# Agent 公开只读接口设计

> 2026-08-01 · 设计文档

## 动机

`Agent` 是组合根，持有约 33 个内部成员（`self._session`/`_long_term`/`_tool_adapter` 等，其中 29 个在 `__init__` 赋值、4 个在启动流程创建），但只暴露 1 个公开方法（`process_message`）+ `agent.status`。下游层（`core/input`、`core/debug`）为访问子系统，只能 `agent._xxx` 硬取——27 个私有成员被跨模块消费（23 子系统 + 4 标量），约 **100+ 处跨模块访问**（`core/input` 17+6+3=26 处 + `debug_routes` 40 处 + `debug/info.py` 33 处 + getattr 字符串式 8 处）。

`_` 前缀语义（"请勿触碰"）与真实使用契约矛盾：私有标注了，下游却必须用。`agent.status`（`AgentStatus` 接口）是成功的公开接口范例，本次按同样思路补齐其余子系统的访问路径。

## 目标

- 为被跨模块消费的 27 个私有成员（23 子系统 + 4 标量）加同名 `@property`，作为公开访问路径
- 迁移 `core/input`（26 处）+ `core/input/debug_routes`（40 处）+ `core/debug/info.py`（33 处）的点式 `agent._xxx` 浅读
- 迁移 8 处 `getattr(agent, "_xxx")` 字符串式访问（recording.py 4 + character.py 2 + info.py 2）→ `getattr(agent, "xxx")`（或改 `agent.xxx if hasattr(...)` 形式）
- debug 深度下钻（`agent.<sub>._<attr>` 约 25 处）保留 `_` 访问，标注显式内省豁免
- 迁移后 `agent._xxx` 只剩深读豁免点

## 非目标

- 不做行为聚合接口（Agent 保持薄组合根，不变成子系统门面）
- 不做强制只读视图（property 返回对象引用，可写性由子系统自身 API 自管）
- 不改动 `core.schema._utils` 跨包导入（独立遗留项）
- 不动 `Module → core` 反向依赖（独立遗留项）

## 方案：薄 getter 直通

**核心判断**：`Agent` 是组合根（composition root）而非服务门面。消费者现在写的不是裸属性访问，而是 `agent._session.messages_for_client()`、`agent._tool_adapter._manager.get_pending_approvals()`——**已经在调用子系统的公开方法**，只是通往子系统的路径是私有属性。问题只在「路径」，不在「API」。

因此在 `core/agent/agent.py` 加一组 `@property`，同名直通：

```python
# core/agent/agent.py — Agent 内部新增
@property
def session(self) -> SessionMemory:
    """当前会话内存（公开访问路径，非强制只读）。"""
    return self._session

@property
def long_term(self) -> LongTermMemory:
    return self._long_term
# ... 其余同理，每个 getter 一行
```

### 为什么薄 getter 比行为聚合接口更易维护

| 维度 | 薄 getter 直通 | 行为聚合接口 |
|------|--------------|-------------|
| 迁移成本 | 改名（机械） | 每点设计方法 |
| 新需求扩展 | 用子系统已有方法 | 每次加 Agent 方法 |
| Agent 职责 | 保持薄组合根 | 膨胀成 God Object |
| 封装深度 | 暴露子系统对象 | 隐藏子系统 |
| 维护面 | 子系统自管 API | Agent 需跟进所有子系统变化 |

## 接口清单

### 子系统对象（23 个）

| property | 返回类型 | 消费方 |
|----------|---------|--------|
| `session` | `SessionMemory` | input/debug |
| `long_term` | `LongTermMemory` | input/debug |
| `message_repo` | `MessageRepository` | input/debug |
| `session_repo` | `SessionRepository` | input/debug |
| `memory_repo` | `MemoryRepository` | input/debug |
| `token_repo` | `TokenRepository` | debug |
| `tool_adapter` | `ToolAdapter` | input/debug |
| `output` | `OutputManager` | input/debug |
| `llm` | `LLMAdapter` | input/debug |
| `tts` | `TTSStreamManager \| None` | input/debug |
| `config` | `AppConfig` | input/debug |
| `bus` | `EventBus` | input/debug |
| `cognitive` | `CognitiveLoop` | debug |
| `idle` | `IdleMonitor` | input/debug |
| `scheduler` | `TaskScheduler` | debug |
| `consolidator` | `MemoryConsolidator` | debug |
| `memory_reflector` | `MemoryReflector` | debug |
| `character_engine` | `CharacterEngine` | debug |
| `character_reflector` | `CharacterReflector` | debug |
| `character` | `CharacterConfig` | debug |
| `token_tracker` | `TokenTracker` | input/debug |
| `prompt_recorder` | `PromptRecorder` | debug |
| `response_recorder` | `ResponseRecorder` | debug |

### 标量/枚举（4 个）

| property | 类型 | 说明 |
|----------|------|------|
| `user_name` | `str` | 注入的用户名 |
| `state` | `RunState` | Agent 运行状态（只读） |
| `started_at` | `float` | 启动时间戳（monotonic） |
| `stt_ok` | `bool` | STT 可用标志 |

### 状态写方法（2 个）

`state` property 只读；server.py 的 2 处 `_state` 写入（`/shutdown`、`/restart`）改为调用公开写方法。这两个方法与 `stop()` 区分——它们只设 `_state` 信号，引导主循环（`while self._state == RunState.RUNNING`）退出，不执行清理步骤：

```python
def request_shutdown(self) -> None:
    """请求关机——设 _state = STOPPING，引导主循环退出。"""
    self._state = RunState.STOPPING

def request_restart(self) -> None:
    """请求重启——设 _state = RESTARTING，引导主循环退出。"""
    self._state = RunState.RESTARTING
```

### 方法型（1 个）

`_purge_message_from_session` 公开为普通方法 `purge_message_from_session(...)`（去掉下划线）。它是 Agent 的公开行为，不是"属性里的方法"。

## 消费方迁移

### 浅读迁移（`agent._xxx` → `agent.xxx`）

| 文件 | 处数 | 说明 |
|------|------|------|
| `core/input/ws_handler.py` | 17 | `_output`/`_session`/`_user_name`/`_config`/`_llm`/`_tts`/`_bus`/`_idle`/`_token_tracker`/`_tool_adapter` 等（无 `_message_repo`——该文件未引用） |
| `core/input/server.py` | 6 | 浅读：`_llm`/`_session`/`_long_term`/`_config`；写迁移：`_state` 2 处 → `request_shutdown()`/`request_restart()`；深读豁免：`_tool_adapter._manager` 3 处 |
| `core/input/api_routes.py` | 3 | `_config`/`_user_name`/`_message_repo` |
| `core/input/debug_routes/*.py` | 40 | `_session`/`_session_repo`/`_message_repo`/`_long_term`/`_memory_repo`/`_token_repo` 等 |
| `core/debug/info.py` | 浅读 33 处 | `_session`/`_message_repo`/`_long_term.count`/`_memory_repo`/`_session_repo`/`_character`/`_character_engine`/`_character_reflector`/`_tts`/`_stt_ok`/`_llm`/`_scheduler`/`_memory_reflector`/`_consolidator`/`_token_tracker`/`_prompt_recorder`/`_response_recorder`/`_started_at` |
| `getattr(agent, "_xxx")` 字符串式 | 8 | recording.py:30,47,90,107（`_prompt_recorder`/`_response_recorder`）、character.py:38,65（`_character_engine`）、info.py:233,243（`_character_engine`）→ 字符串去掉 `_` 前缀（`getattr(agent, "prompt_recorder", None)`） |

### 深度下钻豁免（约 25 处，迁移后仍用 `_`）

以下点迁移后变为 `agent.<sub>.<_attr>`，`_` 访问保留并标注豁免。全量清单以实现时 `grep -rn "agent\.[a-z]*\._"` 为准，主要位置：

| 位置 | 迁移后形式 |
|------|-----------|
| info.py:155,157,277 | `agent.cognitive._phase` / `._current_task` / `._timing_history` |
| info.py:164-167 | `agent.idle._character.proactive.enabled` / `._last_interaction` 等 |
| info.py:198,200,202 | `agent.long_term._entries` / `._repo` / `._embedding_provider._backend` |
| info.py:206 | `agent.tool_adapter._manager` |
| info.py:212 | `agent.consolidator._history` |
| info.py:236,245 | `agent.character_engine._version_repo` |
| info.py:269 | `agent.output._channels` |
| info.py:303-316 | `agent.memory_reflector._round_count` / `._new_memory_count` / `._lock` / `._last_summary`、`agent.consolidator._buffer` / `._threshold` |
| ws_handler.py:79,227 | `agent.tool_adapter._manager` |
| ws_handler.py:210 | `agent.output._channels` |
| server.py:150,208,237 | `agent.tool_adapter._manager` |
| debug_routes/ltm.py:40, overview.py:46, character.py:42,46,69,80,90 | `agent.long_term._repo` / `engine._version_repo` 等 |

豁免方式：`# noqa: SLF001`（ruff private-member-access）标注 + 文档声明 debug 层为**显式内省层**。server.py 的 `_tool_adapter._manager` 属网关层深读，同样豁免标注。

## 错误处理

- `@property` 均为纯读取，不抛异常。子系统对象 29 个在 `__init__` 创建、4 个在启动流程创建（`token_tracker`/`prompt_recorder`/`response_recorder`/`started_at`）。启动期创建的成员在 `start()` 前访问会抛 `AttributeError`——但现状 `getattr(agent, "_xxx", None)` 已防御，迁移为 `getattr(agent, "xxx", None)` 保持相同行为。`_tts` 初始为 `None`（`_start_tts()` 后创建），getter 原样返回——消费者已有 `is not None` 判断，行为不变。
- `state` 返回 `RunState` 枚举，消费者（`agent.state == RunState.RUNNING`）语义不变。
- **写路径**：`state` 只读，`/shutdown`、`/restart` 的写由 `request_shutdown()`/`request_restart()` 承接，设 `_state` 信号引导主循环退出。这两个方法不做清理（`stop()` 才做），语义与现有 `stop()` 区分。

## 测试

- `core/agent/tests/test_public_interface.py`（新增）：断言每个 property 存在且返回正确的内部对象（`agent.session is agent._session`）。因实例化 `Agent`（不调 `start()`），声明 `pytestmark = pytest.mark.service`（见 [docs/testing.md](../../../../testing.md) 分层 + `core/agent/tests/test_agent_methods.py` 现成范例）。注：启动期创建的 4 个成员（`token_tracker` 等）在 `start()` 前的 getter 访问需在测试中单独处理（mock 或调 `_start_services` 相关步骤）
- `make test-quick` 全绿（迁移不改行为，只改属性访问路径）。注：全量约 1927 测试（`docs/testing.md`），日常验证用 `test-quick`（~1526）

## 影响范围

- `core/agent/agent.py` — 新增 27 个 property + `request_shutdown()`/`request_restart()` + `purge_message_from_session()` 公开化
- `core/input/ws_handler.py`、`server.py`、`api_routes.py` — 浅读迁移 + 写路径迁移
- `core/input/debug_routes/*.py` — 浅读迁移
- `core/debug/info.py` — 浅读迁移 + 深读豁免标注
- `core/agent/tests/test_public_interface.py` — 新增（`pytestmark = pytest.mark.service`）
- `docs/superpowers/specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md` — 本文档
