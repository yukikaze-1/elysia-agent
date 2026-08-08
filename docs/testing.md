# 测试指南

## 分层体系

3186 个测试按 4 层 markers 组织（`pytest --collect-only` 实测，2026-08-08）：

| 层 | marker | 标准 | 测试数 | 耗时 |
|---|--------|------|--------|------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent | 1833 | ~40s |
| 服务 | `service` | 单模块 + mock，不启动 Agent | 1258 | 秒级 |
| 集成 | `integration` | 需真实数据库或外部 API | 35 | 不定 |
| 慢速 | `slow` | 完整 Agent 启动/端到端/单测 >1s | 56 | ~15min |

> 各层相加（1833+1258+35+56=3182）比全量 3186 少 4：极少数测试未标记四层 marker（`-m` 收集漏计），个别标记多 marker（如 `unit+service`）则重复计入。
> **口径说明**：全量 3186（含 `Module/workflow_engine` 293 个）；`make test-normal`（`-m "not slow" --ignore=Module/workflow_engine/tests`）为 2854；纯 not-slow 全量 3130。

## 日常命令

```bash
conda activate Elysia

# 日常开发 — 秒级验证（~60s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short --ignore=Module/workflow_engine/tests

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests

# 全量 — 推之前（~20min）
make test-full
# 或: pytest -q

# 导入风格门禁（秒级）
make lint
# 或: ruff check core/ Module/ --exclude __pycache__
```

## Import 门禁（ruff TID252）

强制绝对导入：`from core.xxx import y` / `from Module.xxx import y`，禁止相对导入（`from .module` / `from ..module`）。`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免。配置见根目录 `ruff.toml`；提交前用 `make lint` 验证。

```bash
# 自动修复相对导入 → 绝对（一次性迁移用）
ruff check core/ Module/ --exclude __pycache__ --fix --unsafe-fixes

# 查非豁免文件残留相对导入（应输出为空）
grep -rEn "^from \." core/ Module/ --include="*.py" \
  | grep -v __pycache__ | grep -vE "__init__\.py|__main__\.py|/tests/"
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
pytest core/agent/tests/ -v                # Agent 生命周期
pytest core/config/tests/ -v               # 配置 dataclass
pytest core/monitoring/tests/ -v           # Token 监控
pytest core/task/tests/ -v                 # Task 调度
pytest core/tools/ -v                      # 工具插件
pytest Module/workflow_engine/tests/unit/ -v   # WorkflowEngine 单元
pytest Module/workflow_engine/tests/integration/ -v  # WorkflowEngine 集成
pytest frontend_contract/tests/ -v         # 前后端契约（建模 + TS 生成器）

# 按文件运行
pytest Module/llm/tests/ -v
pytest Module/config/tests/ -v
pytest core/tests/test_phase3.py -v
```

## 测试文件索引

### Module/ 层（55 文件）

按目录聚合，括号内为该文件测试数。

| 目录 | 文件（测试数） | 层级 |
|------|----------------|------|
| `Module/_common/tests/` | `test_registry_helpers.py`(11)、`test_test_helpers.py`(3)、`test_main_runner.py`(2)、`test_config_test_helpers.py`(8) | unit |
| `Module/config/tests/` | `test_config.py`(66)、`test_preflight.py`(19)、`test_providers.py`(3) | unit |
| `Module/logger/tests/` | `test_logger.py`(33) | unit |
| `Module/database/documentdb/tests/` | `test_documentdb.py`(29) / `test_config.py`(3) | service / unit |
| `Module/database/vectordb/tests/` | `test_vectordb.py`(33) / `test_config.py`(3) | service / unit |
| `Module/embedding/tests/` | `test_embedding.py`(31) / `test_config.py`(3) | service / unit |
| `Module/llm/tests/` | `test_llm.py`(44)、`test_registry.py`(2) / `test_config.py`(3) | service / unit |
| `Module/stt/tests/` | `test_stt.py`(41) / `test_config.py`(4) | service / unit |
| `Module/tts/tests/` | `test_tts.py`(86)、`test_voices.py`(11) / `test_config.py`(5) | service / unit |
| `Module/output/tests/` | `test_output.py`(63)、`test_generation_conversation_id.py`(17)、`test_manager_v2.py`(7)、`test_generation_model.py`(4)、`test_audio_control.py`(3) | unit+service / unit |
| `Module/system_prompt/tests/` | `test_prompt.py`(19) | service |
| `Module/system_prompt/tests/unit/` | `test_protocol.py`(9)、`test_sections/test_boundary.py`(25)、`test_system_prompt_manager.py`(6) | unit |
| `Module/system_prompt/tests/integration/` | `test_full_render.py`(3) | integration |
| `Module/tool/tests/` | `test_framework.py`(96)、`test_metadata.py`(14)、`test_tool_safety.py`(33)、`test_plugin_metadata.py`(9)、`test_protocol.py`(4) | unit+service / unit |
| `Module/workflow_contract/tests/` | `test_schema.py`(6) | unit |
| `Module/workflow_engine/tests/unit/` | `test_graph.py`(46)、`test_workflow_engine.py`(45)、`test_planner.py`(41)、`test_evaluator.py`(30)、`test_sub_agent.py`(28)、`test_orchestrator.py`(26)、`test_executor.py`(12)、`test_checkpoint.py`(9)、`test_mailbox.py`(10)、`test_token_ledger.py`(8)、`test_schema.py`(2) | unit |
| `Module/workflow_engine/tests/integration/` | `test_delegation_preparer.py`(11)、`test_e2e_mvp.py`(7, slow)、`test_workflow_engine_lifecycle.py`(10, slow)、`test_executor.py`(4)、`test_planner.py`(4) | integration / slow |

### core/ 层（123 文件）

按目录聚合，括号内为该文件测试数。

| 目录 | 文件（测试数） | 层级 |
|------|----------------|------|
| `core/tests/` | `test_phase1.py`(32)、`test_phase2.py`(57)、`test_phase3.py`(109)、`test_clean_db.py`(16)、`test_request_id.py`(3)、`test_build_info.py`(3) | unit+service+slow+integration / unit |
| `core/status/tests/` | `test_agent_fsm.py`(20) | unit |
| `core/agent/tests/` | `test_public_interface.py`(33)、`test_status.py`(27)、`test_multi_session.py`(20)、`test_lifecycle_steps.py`(18)、`test_desktop_bridge_integration.py`(15)、`test_switch_guard.py`(9)、`test_agent_methods.py`(7)、`test_lifecycle_flush.py`(7)、`test_types.py`(7)、`test_server_banner.py`(6)、`test_v2_message_id.py`(4)、`test_mailbox_polling.py`(1) | service / unit / unit+service |
| `core/config/tests/` | `test_app_config.py`(9)、`test_character_config.py`(9)、`test_database_config.py`(6) | unit |
| `core/monitoring/tests/` | `test_token_tracker.py`(25) | unit |
| `core/task/tests/` | `test_task_scheduler.py`(31)、`test_task_model.py`(14) | service+slow / unit |
| `core/text/tests/` | `test_tokenizer.py`(76)、`test_splitter.py`(61)、`test_stream_processor.py`(59)、`test_tags.py`(30)、`test_parts.py`(21)、`test_fuzz_sequences.py`(4) | unit |
| `core/debug/tests/` | `test_prompt_recorder.py`(7) | unit |
| `core/cognitive/tests/` | `test_delegation_preparer.py`(51)、`test_mood_engine.py`(16)、`test_timing.py`(16)、`test_tool_image_parts.py`(11)、`test_tool_gate.py`(10)、`test_generation_registry.py`(10)、`test_session_resolver.py`(9)、`test_generation.py`(6)、`test_generation_wiring.py`(5)、`test_flush.py`(5)、`test_interrupt_source.py`(3)、`test_v2_empty_response.py`(3)、`test_tts_voice_injection.py`(3)、`test_empty_response_guard.py`(7)、`test_v2_generation.py`(8)、`test_generation_model.py`(2) | unit / service / service+unit |
| `core/desktop/tests/` | `test_bridge.py`(25) / `test_bridge_sender.py`(3) | unit / service |
| `core/input/tests/` | `test_session_routes.py`(70)、`test_server.py`(56)、`test_ssrf.py`(32)、`test_audio_buffer.py`(29)、`test_voice_sessions.py`(25)、`test_upload.py`(25)、`test_auth.py`(24)、`test_v2_ws.py`(21)、`test_attachment_download.py`(20)、`test_providers_api.py`(19)、`test_voice_ws.py`(14)、`test_custom_providers.py`(12)、`test_tts_voices.py`(12)、`test_tools_api.py`(11)、`test_attachment_message.py`(11)、`test_session_ws.py`(10)、`test_desktop_ws.py`(10)、`test_request_id.py`(8)、`test_provider_registry.py`(8)、`test_wakeword.py`(8)、`test_desktop_roundtrip.py`(8)、`test_limits.py`(6)、`test_health.py`(5)、`test_log_sensitivity.py`(3)、`test_stickers.py`(3)、`test_messages_api_compat.py`(3)、`test_debug_ltm.py`(2)、`test_version.py`(3) | service / unit / unit+service |
| `core/memory/tests/` | `test_session_manager.py`(30)、`test_consolidator.py`(22)、`test_long_term.py`(16)、`test_memory_reflector.py`(15)、`test_session.py`(14) | unit / service / service+unit |
| `core/personality/tests/` | `test_engine.py`(44) / `test_reflector.py`(12) | unit / service |
| `core/schema/adapters/tests/` | `test_attachment_resolver.py`(5) | unit |
| `core/schema/tests/` | `test_adapters.py`(18)、`test_memory.py`(18)、`test_event.py`(16)、`test_message.py`(16)、`test_memory_reflector.py`(12)、`test_types.py`(10)、`test_part.py`(10)、`test_token.py`(5)、`test_compression.py`(5)、`test_message_degradation.py`(4)、`test_tool.py`(4)、`test_character.py`(2) | unit |
| `core/storage/tests/` | `test_desktop_call_repo.py`(60)、`test_attachment_repo.py`(27)、`test_message_repo.py`(31)、`test_session_repo.py`(17)、`test_character_version_repo.py`(14)、`test_secret_store.py`(12)、`test_attachment_gc.py`(11)、`test_generation_repo.py`(11)、`test_token_repo.py`(10)、`test_memory_repo.py`(3) | unit / service |
| `core/tools/…` | `filesystem/test_filesystem.py`(16)、`filesystem/test_write_file.py`(11)、`memory/test_memory.py`(15)、`tests/test_desktop.py`(14)、`runtime_config/test_runtime_config.py`(13)、`web/test_web.py`(20)、`time/test_time.py`(8)、`self_state/test_self_state.py`(3)、`reminder/test_reminder.py`(2) | service / unit |
| `core/adapter/tests/` | `test_tool_adapter.py`(11)、`test_message_builder.py`(10)、`test_tts_adapter_control.py`(7)、`test_tts_adapter.py`(7)、`test_llm_adapter.py`(3)、`test_prompt_assembler.py`(2) | service / unit / integration |

### frontend_contract/ 层（8 文件 / 102 测试）

| 目录 | 文件 | 测试数 | 层级 |
|------|------|--------|------|
| `frontend_contract/tests/` | `test_ws_message.py` | 26 | unit |
| | `test_rest.py` | 30 | unit |
| | `test_desktop_tools.py` | 12 | unit |
| | `test_batch5_contract.py` | 11 | unit |
| | `test_batch7_contract.py` | 8 | unit |
| | `test_gen_ts.py` | 8 | unit |
| | `test_batch6_contract.py` | 5 | unit |
| | `test_debug.py` | 2 | unit |

`frontend_contract/` 为前后端契约单源包（Pydantic 模型 + TS 生成器，详见
[frontend_contract/README.md](../frontend_contract/README.md)），测试为纯函数级（无 I/O、无 Agent）→
marker=unit。`make gen-types` 重新生成 `frontend/src/types/contract.generated.ts`，
`make check-types` 漂移门禁。

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
- **`test_resume_idempotent_on_running`** — WorkflowEngine race condition（task 过快完成导致 `resume()` 返回不符合预期）。
- **`test_ok_returns_latency`** — 之前 `ok_check(self)` 多余参数已修复（commit `963ee6b`）。

## 添加新测试

1. 确定层级：对照上述标准，选择 `unit` / `service` / `integration` / `slow`
2. 加 marker：class 或 module 级别
3. 文件名：`test_<功能>.py`，放在对应模块的 `tests/` 目录下
4. 验证：`pytest -m <marker> 新文件路径 -v`
