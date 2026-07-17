# 测试指南

## 分层体系

1384 个测试按 4 层 markers 组织:

| 层 | marker | 标准 | 测试数 | 耗时 |
|---|--------|------|--------|------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent | ~835 | ~40s |
| 服务 | `service` | 单模块 + mock，不启动 Agent | ~518 | 秒级 |
| 集成 | `integration` | 需真实数据库或外部 API | ~34 | 不定 |
| 慢速 | `slow` | 完整 Agent 启动/端到端/单测 >1s | ~33 | ~15min |

## 日常命令

```bash
conda activate Elysia

# 日常开发 — 秒级验证（~60s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow" -q --tb=short

# 全量 — 推之前（~20min）
make test-full
# 或: pytest -q
```

## 按模块运行

```bash
# 按 marker 过滤
pytest -m unit -q                          # 只跑纯单元测试
pytest -m "unit or service" -q             # 快速验证
pytest -m "not slow" -q                    # 跑除了慢速之外的所有
pytest -m slow -q                          # 只跑慢速测试

# 按目录运行
pytest Module/llm/ -v                      # LLM 模块
pytest Module/tts/ -v                      # TTS 模块
pytest Module/database/ -v                 # 数据库模块
pytest core/tests/ -v                      # Core 测试
pytest core/tools/ -v                      # 工具插件
pytest Module/task_system/tests/unit/ -v   # TaskSystem 单元
pytest Module/task_system/tests/integration/ -v  # TaskSystem 集成

# 按文件运行
pytest Module/llm/test_llm.py -v
pytest Module/config/test_config.py -v
pytest core/tests/test_phase3.py -v
```

## 测试文件索引

### Module/ 层（39 文件）

| 目录 | 文件 | 测试数 | 层级 |
|------|------|--------|------|
| `Module/_common/tests/` | `test_registry_helpers.py` | 11 | unit |
| | `test_test_helpers.py` | 3 | unit |
| | `test_main_runner.py` | 2 | unit |
| | `test_config_test_helpers.py` | 4 | unit |
| `Module/config/` | `test_config.py` | 64 | unit |
| | `test_preflight.py` | 19 | unit |
| `Module/logger/` | `test_logger.py` | 33 | unit |
| `Module/database/documentdb/` | `test_documentdb.py` | 29 | service |
| | `test_config.py` | 2 | unit |
| `Module/database/vectordb/` | `test_vectordb.py` | 33 | service |
| | `test_config.py` | 2 | unit |
| `Module/embedding/` | `test_embedding.py` | 31 | service |
| | `test_config.py` | 2 | unit |
| `Module/llm/` | `test_llm.py` | 38 | service |
| | `test_config.py` | 2 | unit |
| `Module/stt/` | `test_stt.py` | 37 | service |
| | `test_config.py` | 2 | unit |
| `Module/tts/` | `test_tts.py` | 70 | service |
| | `test_config.py` | 2 | unit |
| `Module/input/` | `test_server.py` | 30 | unit+service |
| | `test_audio_buffer.py` | 29 | unit+service |
| | `test_wakeword.py` | 8 | unit |
| `Module/output/` | `test_output.py` | 54 | unit+service |
| `Module/system_prompt/` | `test_prompt.py` | 19 | service |
| | `tests/unit/test_protocol.py` | 9 | unit |
| | `tests/unit/test_sections/test_boundary.py` | 25 | unit |
| | `tests/unit/test_system_prompt_manager.py` | 6 | unit |
| | `tests/integration/test_full_render.py` | 3 | integration |
| | `tests/integration/test_message_builder.py` | 12 | integration |
| `Module/tool/` | `test_framework.py` | 96 | unit+service |
| | `test_tool_safety.py` | 33 | unit |
| `Module/task_system/tests/unit/` | `test_checkpoint.py` | 9 | unit |
| | `test_delegation_preparer.py` | 53 | unit |
| | `test_evaluator.py` | 29 | unit |
| | `test_executor.py` | 12 | unit |
| | `test_graph.py` | 46 | unit |
| | `test_mailbox.py` | 10 | unit |
| | `test_orchestrator.py` | 26 | unit |
| | `test_planner.py` | 41 | unit |
| | `test_sub_agent.py` | 27 | unit |
| | `test_task_system.py` | 44 | unit |
| | `test_token_ledger.py` | 8 | unit |
| `Module/task_system/tests/integration/` | `test_delegation_preparer.py` | 11 | integration |
| | `test_e2e_mvp.py` | 7 | slow |
| | `test_executor.py` | 4 | integration |
| | `test_planner.py` | 4 | integration |
| | `test_task_system_lifecycle.py` | 10 | slow |

### core/ 层（24 文件）

| 目录 | 文件 | 测试数 | 层级 |
|------|------|--------|------|
| `core/tests/` | `test_phase1.py` | 32 | unit+service+slow+integration |
| | `test_phase2.py` | 72 | unit+service+slow+integration |
| | `test_phase3.py` | 110 | unit+service+slow |
| | `test_sentence_buffer.py` | 65 | unit |
| | `test_task_model.py` | 15 | unit |
| | `test_task_scheduler.py` | 26 | service+slow |
| | `test_timing.py` | 16 | service |
| | `test_log_setup.py` | 3 | unit |
| | `test_prompt_recorder.py` | 7 | unit |
| | `test_token_tracker.py` | 17 | unit |
| `core/memory/tests/` | `test_long_term.py` | 8 | service |
| | `test_message_archive.py` | 12 | service |
| | `test_session_archive_integration.py` | 3 | service |
| | `test_session.py` | 4 | service |
| `core/personality/tests/` | `test_engine.py` | 14 | unit |
| | `test_reflector.py` | 15 | service |
| `core/tools/filesystem/` | `test_filesystem.py` | 16 | service |
| | `test_write_file.py` | 11 | service |
| `core/tools/memory/` | `test_memory.py` | 9 | service |
| `core/tools/reminder/` | `test_reminder.py` | 2 | unit |
| `core/tools/runtime_config/` | `test_runtime_config.py` | 13 | unit |
| `core/tools/self_state/` | `test_self_state.py` | 3 | unit |
| `core/tools/time/` | `test_time.py` | 8 | unit |
| `core/tools/web/` | `test_web.py` | 20 | service |

## Marker 标准

### unit

- 只依赖 Python 标准库和被测模块
- 无 `asyncio`、无 mock 外部服务
- 不访问文件系统（tempfile 除外）、网络、数据库
- 不创建 Agent 实例

```python
# 模块级标记
pytestmark = pytest.mark.unit

# 或类级标记
@pytest.mark.unit
class TestMyFunction:
    ...
```

### service

- 单模块测试，mock 外部依赖（用 `unittest.mock`、`FakeLLMBackend` 等）
- 可能使用 `asyncio`，但不启动完整 Agent
- 可以访问临时文件、mock 的数据库连接
- 创建 `Agent` 但不调用 `agent.start()` → service

```python
pytestmark = pytest.mark.service
```

### integration

- 需要真实 MongoDB/Milvus 连接
- 需要真实外部 API（LLM/TTS/STT）
- 不 mock 关键依赖

```python
pytestmark = pytest.mark.integration
```

### slow

- 创建 `Agent(config=AppConfig())` 并调用 `agent.start()`
- 完整管线端到端测试
- 单测耗时 >1s
- 大量 `asyncio.sleep()` 累积

```python
@pytest.mark.slow
class TestAgentLifecycle:
    ...
```

## 已知问题

- **`test_touch_resets_timer`** — `IdleMonitor.__init__` 中 `asyncio.get_event_loop().time()` 在全量 suite 中偶现失败。已加 `time.sleep(0.001)` 缓解，建议后续改用 `time.monotonic()`。
- **`test_resume_idempotent_on_running`** — TaskSystem race condition（task 过快完成导致 `resume()` 返回不符合预期）。
- **`test_ok_returns_latency`** — 之前 `ok_check(self)` 多余参数已修复（commit `963ee6b`）。

## 添加新测试

1. 确定层级：对照上述标准，选择 `unit` / `service` / `integration` / `slow`
2. 加 marker：class 或 module 级别
3. 文件名：`test_<功能>.py`，放在对应模块的 `tests/` 目录下
4. 验证：`pytest -m <marker> 新文件路径 -v`
