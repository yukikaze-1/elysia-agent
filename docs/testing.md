# 测试指南

## 分层体系

测试统计由 `scripts/print_test_stats.py` 生成（F5：不手写易漂移数字）——`conda run -n Elysia python scripts/print_test_stats.py`。当前（2026-08-16）全量收集 3804：

| 层 | marker | 标准 | 测试数 |
|---|--------|------|--------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent | 2070 |
| 服务 | `service` | 单模块 + mock，不启动 Agent | 1580 |
| 集成 | `integration` | 需真实数据库或外部 API | 52 |
| 慢速 | `slow` | 完整 Agent 启动/端到端/单测 >1s | 57 |
| 外部 | `external` | 需真实 LLM/DB 凭据（F1，独立 opt-in） | 36 |

> **口径说明（F1 起）**：`external` 独立于四层——真实 LLM E2E（WorkflowEngine 集成）不进合并门禁；合并门禁 `-m "not external"` 收集 3768。2026-08-16 实测 `make test-quick`（`-m "unit or service"`）3618 passed / 2 skipped；`make test-normal`（`-m "not slow and not external"`）3726 passed / 2 skipped。各层相加与全量的差异（多 marker 重复计入 / 未标记四层）以脚本输出为准。

### 外部测试门禁（E4，`RUN_EXTERNAL_TESTS=1`）

`external` 测试（真实 LLM/DB E2E）**双保险门禁**：

1. 合并门禁 `-m "unit or service"` / `-m "not slow and not external"` 已排除 `external` marker。
2. `Module/conftest.py` 的 collection hook：未设 `RUN_EXTERNAL_TESTS=1` 时，所有 `external` 测试整体 `skip`（用 `item.add_marker(skipif)`——裸 `pytest.skip()` 在 collection 阶段会中断收集）。

即「环境恰有 `DEEPSEEK_API_KEY` + 显式 `-m external` 但忘设 env」也不会误跑真实 LLM。显式 opt-in：

```bash
make test-external
# 或: conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short
```

真实 LLM 测试必须同时标 `integration + external`。workflow 确定性测试基建在
`Module/workflow_engine/tests/helpers/fake_llm.py`：`FakeLLM`（对齐 `chat.completions.create`
的 `tool_calls`/`content`/`usage` 访问面 + `gate`/`calls`/`prompts`）、`with_deadline`（gated 测试
挂死防线，`asyncio.wait_for` 封装）、`CountingLLM`（外部成本计数包装，委托透传 `messages`）。

## 日常命令

```bash
conda activate Elysia

# 日常开发 — 秒级验证（~60s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow and not external" -q --tb=short

# 全量 — 推之前（~20min）
make test-full
# 或: pytest -q

# 导入风格门禁（秒级）
make lint
# 或: ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__
```

## Import 门禁（ruff TID252）

强制绝对导入：`from core.xxx import y` / `from Module.xxx import y`，禁止相对导入（`from .module` / `from ..module`）。`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免。配置见根目录 `ruff.toml`；提交前用 `make lint` 验证。

```bash
# 自动修复相对导入 → 绝对（一次性迁移用）
ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__ --fix --unsafe-fixes

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

### Module/ 层（59 文件 / 1190 测试）

按目录聚合，括号内为该文件收集数。层级以各文件 `pytestmark` 为准（unit/service/integration/slow/external），此处不再逐文件标注。

| 目录 | 文件（收集数） |
|------|----------------|
| `Module/_common/tests/`（5 文件 / 56 测试） | `test_config_test_helpers.py`(8)、`test_main_runner.py`(2)、`test_registry_helpers.py`(11)、`test_ssrf.py`(32)、`test_test_helpers.py`(3) |
| `Module/config/tests/`（3 文件 / 88 测试） | `test_config.py`(66)、`test_preflight.py`(19)、`test_providers.py`(3) |
| `Module/database/documentdb/tests/`（2 文件 / 32 测试） | `test_config.py`(3)、`test_documentdb.py`(29) |
| `Module/database/vectordb/tests/`（2 文件 / 54 测试） | `test_config.py`(3)、`test_vectordb.py`(51) |
| `Module/embedding/tests/`（2 文件 / 34 测试） | `test_config.py`(3)、`test_embedding.py`(31) |
| `Module/llm/tests/`（3 文件 / 49 测试） | `test_config.py`(3)、`test_llm.py`(44)、`test_registry.py`(2) |
| `Module/logger/tests/`（1 文件 / 33 测试） | `test_logger.py`(33) |
| `Module/output/tests/`（5 文件 / 94 测试） | `test_audio_control.py`(3)、`test_generation_conversation_id.py`(17)、`test_generation_model.py`(4)、`test_manager_v2.py`(7)、`test_output.py`(63) |
| `Module/stt/tests/`（3 文件 / 51 测试） | `test_config.py`(4)、`test_sensevoice_base_url.py`(6)、`test_stt.py`(41) |
| `Module/system_prompt/tests/`（5 文件 / 62 测试） | `test_full_render.py`(3)、`test_prompt.py`(19)、`test_protocol.py`(9)、`test_boundary.py`(25)、`test_system_prompt_manager.py`(6) |
| `Module/tool/tests/`（6 文件 / 191 测试） | `test_framework.py`(116)、`test_metadata.py`(18)、`test_per_call_context.py`(5)、`test_plugin_metadata.py`(9)、`test_protocol.py`(4)、`test_tool_safety.py`(39) |
| `Module/tts/tests/`（3 文件 / 102 测试） | `test_config.py`(5)、`test_tts.py`(86)、`test_voices.py`(11) |
| `Module/workflow_contract/tests/`（1 文件 / 9 测试） | `test_schema.py`(9) |
| `Module/workflow_engine/tests/integration/`（5 文件 / 36 测试） | `test_delegation_preparer.py`(11)、`test_e2e_mvp.py`(7)、`test_executor.py`(4)、`test_planner.py`(4)、`test_workflow_engine_lifecycle.py`(10) |
| `Module/workflow_engine/tests/unit/`（13 文件 / 299 测试） | `test_checkpoint.py`(9)、`test_evaluator.py`(30)、`test_executor.py`(17)、`test_fake_llm.py`(6)、`test_graph.py`(46)、`test_mailbox.py`(11)、`test_orchestrator.py`(26)、`test_planner.py`(41)、`test_schema.py`(4)、`test_sub_agent.py`(29)、`test_token_ledger.py`(8)、`test_workflow_cancel_deterministic.py`(8)、`test_workflow_engine.py`(64) | |

### core/ 层（129 文件 / 2397 测试）

| 目录 | 文件（收集数） |
|------|----------------|
| `core/tests/`（6 文件 / 236 测试） | `test_build_info.py`(3)、`test_clean_db.py`(31)、`test_phase1.py`(32)、`test_phase2.py`(57)、`test_phase3.py`(110)、`test_request_id.py`(3) |
| `core/status/tests/`（1 文件 / 20 测试） | `test_agent_fsm.py`(20) |
| `core/agent/tests/`（13 文件 / 172 测试） | `test_agent_methods.py`(7)、`test_desktop_bridge_integration.py`(16)、`test_lifecycle_flush.py`(7)、`test_lifecycle_idempotency.py`(9)、`test_lifecycle_steps.py`(18)、`test_mailbox_polling.py`(2)、`test_multi_session.py`(25)、`test_public_interface.py`(33)、`test_server_banner.py`(7)、`test_status.py`(27)、`test_switch_guard.py`(9)、`test_types.py`(7)、`test_v2_message_id.py`(5) |
| `core/config/tests/`（3 文件 / 26 测试） | `test_app_config.py`(11)、`test_character_config.py`(9)、`test_database_config.py`(6) |
| `core/monitoring/tests/`（1 文件 / 25 测试） | `test_token_tracker.py`(25) |
| `core/task/tests/`（2 文件 / 50 测试） | `test_task_model.py`(14)、`test_task_scheduler.py`(36) |
| `core/text/tests/`（6 文件 / 251 测试） | `test_fuzz_sequences.py`(4)、`test_parts.py`(21)、`test_splitter.py`(61)、`test_stream_processor.py`(59)、`test_tags.py`(30)、`test_tokenizer.py`(76) |
| `core/debug/tests/`（1 文件 / 7 测试） | `test_prompt_recorder.py`(7) |
| `core/cognitive/tests/`（17 文件 / 181 测试） | `test_delegation_preparer.py`(51)、`test_empty_response_guard.py`(7)、`test_flush.py`(5)、`test_generation.py`(6)、`test_generation_model.py`(2)、`test_generation_registry.py`(14)、`test_generation_wiring.py`(5)、`test_interrupt_source.py`(3)、`test_mood_engine.py`(16)、`test_session_resolver.py`(11)、`test_timing.py`(16)、`test_tool_gate.py`(11)、`test_tool_image_parts.py`(14)、`test_tool_session_rebind.py`(6)、`test_tts_voice_injection.py`(3)、`test_v2_empty_response.py`(3)、`test_v2_generation.py`(8) |
| `core/desktop/tests/`（2 文件 / 29 测试） | `test_bridge.py`(26)、`test_bridge_sender.py`(3) |
| `core/input/tests/`（29 文件 / 673 测试） | `test_attachment_download.py`(22)、`test_attachment_message.py`(18)、`test_audio_buffer.py`(32)、`test_auth.py`(30)、`test_custom_providers.py`(12)、`test_debug_ltm.py`(2)、`test_desktop_roundtrip.py`(9)、`test_desktop_ws.py`(10)、`test_health.py`(12)、`test_idempotency_durable.py`(52)、`test_limits.py`(6)、`test_log_sensitivity.py`(3)、`test_messages_api_compat.py`(3)、`test_provider_registry.py`(8)、`test_providers_api.py`(22)、`test_rate_limit.py`(32)、`test_request_id.py`(8)、`test_server.py`(70)、`test_session_routes.py`(102)、`test_session_ws.py`(11)、`test_stickers.py`(3)、`test_tools_api.py`(11)、`test_tts_voices.py`(12)、`test_upload.py`(45)、`test_v2_ws.py`(51)、`test_version.py`(8)、`test_voice_sessions.py`(45)、`test_voice_ws.py`(26)、`test_wakeword.py`(8) |
| `core/memory/tests/`（5 文件 / 131 测试） | `test_consolidator.py`(24)、`test_long_term.py`(21)、`test_memory_reflector.py`(15)、`test_session.py`(21)、`test_session_manager.py`(50) |
| `core/personality/tests/`（2 文件 / 58 测试） | `test_engine.py`(46)、`test_reflector.py`(12) |
| `core/prompt/tests/`（2 文件 / 12 测试） | `test_message_builder.py`(10)、`test_prompt_assembler.py`(2) |
| `core/schema/adapters/tests/`（1 文件 / 5 测试） | `test_attachment_resolver.py`(5) |
| `core/schema/tests/`（13 文件 / 132 测试） | `test_adapters.py`(22)、`test_character.py`(2)、`test_compression.py`(5)、`test_event.py`(16)、`test_memory.py`(18)、`test_memory_reflector.py`(12)、`test_message.py`(19)、`test_message_degradation.py`(5)、`test_message_id.py`(4)、`test_part.py`(10)、`test_token.py`(5)、`test_tool.py`(4)、`test_types.py`(10) |
| `core/storage/tests/`（12 文件 / 268 测试） | `test_attachment_gc.py`(22)、`test_attachment_repo.py`(34)、`test_character_version_repo.py`(14)、`test_desktop_call_repo.py`(62)、`test_generation_repo.py`(15)、`test_health_semantics.py`(22)、`test_idempotency_repo.py`(17)、`test_memory_repo.py`(3)、`test_message_repo.py`(37)、`test_secret_store.py`(12)、`test_session_repo.py`(19)、`test_token_repo.py`(11) |
| `core/tools/tests/`（9 文件 / 108 测试） | `test_desktop.py`(14)、`test_filesystem.py`(18)、`test_memory.py`(15)、`test_reminder.py`(2)、`test_runtime_config.py`(13)、`test_self_state.py`(3)、`test_time.py`(8)、`test_web.py`(22)、`test_write_file.py`(13) |
| `core/adapter/tests/`（4 文件 / 28 测试） | `test_llm_adapter.py`(3)、`test_tool_adapter.py`(11)、`test_tts_adapter.py`(7)、`test_tts_adapter_control.py`(7) | |

### frontend_contract/ 层（12 文件 / 138 测试）

| 目录 | 文件（收集数） |
|------|----------------|
| `frontend_contract/tests/` | `test___main___check.py`(4)、`test_batch5_contract.py`(11)、`test_batch6_contract.py`(5)、`test_batch7_contract.py`(8)、`test_debug.py`(2)、`test_desktop_tools.py`(12)、`test_gen_schema.py`(3)、`test_gen_ts.py`(10)、`test_rest.py`(32)、`test_rest_enums.py`(5)、`test_task6_ack_union.py`(14)、`test_ws_message.py`(32) |

`frontend_contract/` 为前后端契约单源包（Pydantic 模型 + TS 生成器，详见
[frontend_contract/README.md](../frontend_contract/README.md)），测试为纯函数级（无 I/O、无 Agent）→
marker=unit。`make gen-types` 重新生成 `frontend/src/types/contract.generated.ts`，
`make check-types` 漂移门禁。

### 仓库根/脚本层（8 文件 / 79 测试）

| 目录 | 文件（收集数） |
|------|----------------|
| `scripts/tests/`（7 文件 / 65 测试） | `test_backfill_message_ids.py`(5)、`test_capture_ws_trace.py`(3)、`test_check_ci_isolation.py`(4)、`test_check_compose_security.py`(11)、`test_download_model_manifest.py`(26)、`test_gen_contract_frames.py`(15)、`test_stage4_smoke.py`(1) |
| `tests/`（1 文件 / 14 测试） | `test_paths.py`(14) |

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

- **`test_touch_resets_timer`** — ✅ 已修复：`IdleMonitor` 计时已改用 `time.monotonic()`（测试内保留 `time.sleep(0.001)` 仅作时间推进）。
- **`test_resume_idempotent_on_running`** — WorkflowEngine 历史 race（task 过快完成导致 `resume()` 返回不符合预期），保留为集成测试观测项。
- **`test_ok_returns_latency`** — ✅ 已修复：`ok_check(self)` 多余参数已移除（`Module/_common/tests/test_test_helpers.py`）。

## 添加新测试

1. 确定层级：对照上述标准，选择 `unit` / `service` / `integration` / `slow`
2. 加 marker：class 或 module 级别
3. 文件名：`test_<功能>.py`，放在对应模块的 `tests/` 目录下
4. 验证：`pytest -m <marker> 新文件路径 -v`
