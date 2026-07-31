# Agent 公开只读接口设计

> 2026-08-01 · 设计文档

## 动机

`Agent` 是组合根，持有 22 个内部成员（`self._session`/`_long_term`/`_tool_adapter` 等），但只暴露 1 个公开方法（`process_message`）+ `agent.status`。下游层（`core/input`、`core/debug`）为访问子系统，只能 `agent._xxx` 硬取——约 **28 个私有成员、160+ 处跨模块访问**。

`_` 前缀语义（"请勿触碰"）与真实使用契约矛盾：私有标注了，下游却必须用。`agent.status`（`AgentStatus` 接口）是成功的公开接口范例，本次按同样思路补齐其余子系统的访问路径。

## 目标

- 为被跨模块消费的 ~24 个私有成员加同名 `@property`，作为公开访问路径
- 迁移 `core/input`（76 处）+ `core/input/debug_routes`（~40 处）+ `core/debug/info.py` 的浅读
- debug 深度下钻（`agent.long_term._entries` 等 9 处）保留 `_` 访问，标注显式内省豁免
- 迁移后 `agent._xxx` 只剩 debug 深读豁免点

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

### 子系统对象（19 个）

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
| `config` | `AgentConfig` | input/debug |
| `bus` | `EventBus` | input/debug |
| `cognitive` | `CognitiveLoop` | debug |
| `idle` | `IdleMonitor` | input/debug |
| `scheduler` | `TaskScheduler` | debug |
| `consolidator` | `MemoryConsolidator` | debug |
| `memory_reflector` | `MemoryReflector` | debug |
| `character_engine` | `CharacterEngine` | debug |
| `character_reflector` | `CharacterReflector` | debug |

### 标量/枚举（4 个）

| property | 类型 | 说明 |
|----------|------|------|
| `user_name` | `str` | 注入的用户名 |
| `state` | `RunState` | Agent 运行状态 |
| `started_at` | `float` | 启动时间戳（monotonic） |
| `stt_ok` | `bool` | STT 可用标志 |

### 方法型（1 个）

`_purge_message_from_session` 公开为普通方法 `purge_message_from_session(...)`（去掉下划线）。它是 Agent 的公开行为，不是"属性里的方法"。

## 消费方迁移

### 浅读迁移（`agent._xxx` → `agent.xxx`）

| 文件 | 处数 | 说明 |
|------|------|------|
| `core/input/ws_handler.py` | 18 | `_output`/`_session`/`_user_name`/`_config`/`_llm`/`_tts`/`_bus`/`_idle`/`_message_repo`/`_tool_adapter` 等 |
| `core/input/server.py` | 8 | `_llm`/`_session`/`_long_term`/`_tool_adapter._manager`(豁免)/`_state` 等 |
| `core/input/api_routes.py` | 3 | `_config`/`_user_name`/`_message_repo` |
| `core/input/debug_routes/*.py` | ~40 | `_session`/`_session_repo`/`_message_repo`/`_long_term`/`_memory_repo`/`_token_repo` 等 |
| `core/debug/info.py` | 浅读部分 | `_session`/`_message_repo`/`_long_term.count`/`_memory_repo`/`_session_repo`/`_character`/`_character_engine`/`_tts`/`_stt_ok`/`_llm`/`_scheduler`/`_memory_reflector`/`_consolidator`/`_token_tracker`/`_prompt_recorder`/`_response_recorder`/`_started_at`/`_character_reflector` |

### 深度下钻豁免（9 处，迁移后仍用 `_`）

以下点迁移后变为 `agent.<sub>. <_attr>`，`_` 访问保留并标注豁免：

| 位置 | 迁移后形式 |
|------|-----------|
| info.py:155 | `agent.cognitive._phase` |
| info.py:157 | `agent.cognitive._current_task` |
| info.py:198 | `agent.long_term._entries[-5:]` |
| info.py:200 | `agent.long_term._repo` |
| info.py:202 | `agent.long_term._embedding_provider._backend` |
| info.py:206 | `agent.tool_adapter._manager` |
| info.py:212 | `agent.consolidator._history` |
| info.py:277 | `agent.cognitive._timing_history` |
| ws_handler.py:210 | `agent.output._channels` |

豁免方式：`# noqa: SLF001`（ruff private-member-access）标注 + 文档声明 debug 层为**显式内省层**。

## 错误处理

- `@property` 均为纯读取，不抛异常。子系统对象在 `__init__` 中创建（`_tts` 初始为 `None`，启动后才创建），getter 原样返回——`tts` 可能为 `None`，消费者已有 `is not None` 判断，行为不变。
- `state` 返回 `RunState` 枚举，消费者（`agent.state == RunState.RUNNING`）语义不变。

## 测试

- `core/agent/tests/test_public_interface.py`（新增）：断言每个 property 存在且返回正确的内部对象（`agent.session is agent._session`）
- 现有 1526 测试全绿（迁移不改行为，只改属性访问路径）

## 影响范围

- `core/agent/agent.py` — 新增 ~24 个 property + 1 个公开方法
- `core/input/ws_handler.py`、`server.py`、`api_routes.py` — 浅读迁移
- `core/input/debug_routes/*.py` — 浅读迁移
- `core/debug/info.py` — 浅读迁移 + 深读豁免标注
- `core/agent/tests/test_public_interface.py` — 新增
- `docs/superpowers/specs/2026-08-01-agent-public-readonly-interface-design.md` — 本文档
