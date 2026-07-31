# AgentLifecycle 拆分为 mixin

> 状态: 待实现 | 日期: 2026-07-28

## 动机

`core/agent/lifecycle.py`（522 行）包含 2 个公开方法 + 24 个私有方法，mix 了三个关注点：
1. **启动编排** — `start()` + 10 个 `_start_*`
2. **关闭编排** — `stop()` + 15 个 `_stop_*`/`_flush_*`
3. **跨子系统耦合** — 每个方法通过 `self._*` 访问 15+ 个子系统属性

shutdown 侧已经是数据驱动（`_SHUTDOWN_STEPS` + `getattr` 循环），但 startup 侧仍是手写的 10 步序列。新增子系统需要在同一个文件里同时加 `_start_*`、`_stop_*`、`_flush_*` 方法，文件只会越来越长。

## 设计

### 模式

和 CharacterEngine 拆分一致：按关注点拆成 4 个 mixin + 1 个 facade 主类。每个 mixin 负责一组相关子系统的启停。方法体逻辑不变，只搬家。

### 文件结构

```
core/agent/
├── lifecycle.py                  # AgentLifecycle facade + registries + start()/stop() + 3 flush
├── _lifecycle_persistence.py     # MongoDB/Milvus/session/repos
├── _lifecycle_services.py        # LLM/TTS/Output/TokenTracker
├── _lifecycle_tools.py           # ToolAdapter/Scheduler/embedding/IdleMonitor/event loop
├── _lifecycle_task_system.py     # TaskSystem/DelegationPreparer/mailbox
```

### facade 类

```python
# core/agent/lifecycle.py

class AgentLifecycle(
    _LifecyclePersistence,
    _LifecycleServices,
    _LifecycleTools,
    _LifecycleTaskSystem,
):
    """Agent 生命周期 mixin — start/stop 编排。"""

    _STARTUP_STEPS: list[tuple[str, str]] = [
        # persistence (_start_persistence) 在 start() 中单独处理——需要捕获返回值
        ("token_and_prompt", "_start_token_and_prompt"),
        ("tool_context",     "_start_tool_context"),
        ("worker_pool",      "_start_worker_pool"),
        ("idle_and_events",  "_start_idle_and_events"),
        ("tts",              "_start_tts"),
        ("task_system",      "_start_task_system"),
        ("mailbox_polling",  "_start_mailbox_polling"),
        ("db_health",        "_start_db_health"),
    ]

    _SHUTDOWN_STEPS: list[tuple[str, str]] = [
        # 不变，和当前一模一样
    ]

    async def start(self) -> None:
        logger.info("Agent starting...")
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()
        self._fsm.reset()
        await self._llm.reconnect()

        # Step 0: persistence 特殊——返回值需保存，不经过循环
        mongo_ok, milvus_ok = await self._start_persistence()
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok

        # Steps 1-8: 走 _STARTUP_STEPS 循环（和 stop() 同模式）
        for name, method_name in self._STARTUP_STEPS:
            logger.debug("Startup step: %s", name)
            method = getattr(self, method_name)
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.error("Startup step %s failed", name, exc_info=True)
                raise

    async def stop(self) -> None:
        # 不变——继续用 _SHUTDOWN_STEPS 循环

    # 3 个 flush 方法保留在此——它们是 CharacterEngine 的简单委托
```

### startup 数据流

`_start_persistence()` 把 `mongo_ok`/`milvus_ok` 写入 `self._mongo_ok`/`self._milvus_ok`（属性已存在于 TYPE_CHECKING 声明中）。`_start_token_and_prompt()` 从 `self._mongo_ok` 读取。不引入新的数据传递机制。

### 方法分配

**`lifecycle.py` — facade（~80 行）：**

| 方法 | 说明 |
|------|------|
| `start()` | 先调 `_start_persistence()`，保存返回值；其余走 `_STARTUP_STEPS` 循环 |
| `stop()` | 走 `_SHUTDOWN_STEPS` 循环（不变） |
| `_flush_consolidator()` | 委托 `self._consolidator.flush()` |
| `_flush_reflector()` | 委托 `self._character_engine.flush_reflector()` |
| `_flush_compression()` | 委托 `self._character_engine.flush_compression()` |
| TYPE_CHECKING 属性声明 | 不变，所有 `self._*` 类型标注保留 |

**`_lifecycle_persistence.py` — PersistenceMixin（~90 行）：**

| 方法 | 说明 |
|------|------|
| `_start_persistence()` | MongoDB/Milvus 连接 + session 加载 + JSONL 恢复，返回 `(mongo_ok, milvus_ok)` 并写入 `self` |
| `_start_db_health()` | 创建 `self._db_health_task = asyncio.create_task(self._db_health_loop())` |
| `_db_health_loop()` | 120s 间隔数据库健康检查 + 自动重连 |
| `_stop_db_health()` | cancel health check task |
| `_stop_persistence()` | session.save() → LTM.close() → 5 个 repo.close() → EmbeddingFactory.shutdown() |

**`_lifecycle_services.py` — ServicesMixin（~100 行）：**

| 方法 | 说明 |
|------|------|
| `_start_token_and_prompt()` | 创建 TokenTracker + PromptRecorder + ResponseRecorder，注入 CognitiveLoop。`mongo_ok` 参数移除，改为读 `self._mongo_ok` |
| `_start_tts()` | TTS factory 创建 + 连通性测试，注入 CognitiveLoop |
| `_stop_llm_and_tts()` | LLM.close() + TTS.close() |
| `_stop_output()` | output.close() |

**`_lifecycle_tools.py` — ToolsMixin（~130 行）：**

| 方法 | 说明 |
|------|------|
| `_start_tool_context()` | ToolAdapter.start() + 注入 CognitiveLoop + Scheduler |
| `_start_worker_pool()` | Scheduler.start() + embedding 预加载 task |
| `_start_idle_and_events()` | IdleMonitor.start() + 主事件循环 task |
| `_stop_event_task()` | cancel 事件循环 |
| `_stop_cognitive_and_tts()` | cognitive.cancel_current() + TTS cancel_all() |
| `_stop_embedding_if_running()` | cancel embedding task |
| `_stop_scheduler()` | scheduler.stop() |
| `_stop_tool_adapter()` | tool_adapter.shutdown() |
| `_stop_idle()` | idle.stop() |

**`_lifecycle_task_system.py` — TaskSystemMixin（~90 行）：**

| 方法 | 说明 |
|------|------|
| `_start_task_system()` | TaskSystem + DelegationPreparer + HITLNotifier 创建 + 注入 CognitiveLoop |
| `_start_mailbox_polling()` | mailbox 轮询 task |
| `_stop_mailbox()` | cancel mailbox task |
| `_stop_task_system()` | task_system.stop() |

## 修改范围

| 文件 | 改动 |
|------|------|
| `core/agent/lifecycle.py` | 删减为 ~80 行 facade：`_STARTUP_STEPS` 注册表 + `start()`/`stop()` 编排循环 + 3 flush + TYPE_CHECKING 属性声明。mixin 导入合并 |
| `core/agent/_lifecycle_persistence.py` | **新建** — PersistenceMixin，5 个方法 |
| `core/agent/_lifecycle_services.py` | **新建** — ServicesMixin，4 个方法 |
| `core/agent/_lifecycle_tools.py` | **新建** — ToolsMixin，9 个方法 |
| `core/agent/_lifecycle_task_system.py` | **新建** — TaskSystemMixin，4 个方法 |

### 方法体变更

除搬家外，唯一的逻辑变更在 `_start_token_and_prompt`：

| 方法 | 当前签名 | 新签名 | 原因 |
|------|---------|--------|------|
| `_start_token_and_prompt` | `(self, mongo_ok: bool) -> None` | `(self) -> None` | `mongo_ok` 改为读 `self._mongo_ok`——persistence 之后已在 `start()` 中写入 |

其余 23 个方法只搬家，逻辑和签名都不变。

不改动：`__init__.py`、`agent.py`、所有测试。

## 兼容性

- `AgentLifecycle` 类名 + 公开方法签名不变——外部调用方无感知
- 私有方法仍可通过 `self._*` 访问——mixin 继承后和原来一样
- 所有 import 语句移到各自 mixin 文件中——`lifecycle.py` 的 import 大幅精简
- `_SHUTDOWN_STEPS` 列表不变——`getattr` 循环仍然能通过 MRO 找到各 mixin 上的方法
- 测试不依赖 lifecycle 内部模块路径——无需修改

## 验证

```bash
# Agent 单元测试
pytest core/tests/test_phase3.py -v -k "Lifecycle" --tb=short

# 全量 phase3 测试
pytest core/tests/test_phase3.py -v --tb=short

# 导入检查
python -c "from core.agent.lifecycle import AgentLifecycle; print('OK')"
```
