# Phase 5: 测试缺口修复 — Module/tool 剩余 + Core P0 逻辑

> **Status: COMPLETE** — 2026-06-02, Tasks 1,3,4 done (Task 2 _think_loop deferred). 15 new tests + 1 bugfix.
> Commits: `f228cfa` `8c5ff7e` `6931ca7`

**Goal:** 补充 Module/tool 的 SafetyPipeline 流式路径/ToolManager 剩余方法/并发 + Core 的 _think_loop/_process_events/_check_proactive 核心逻辑测试

**Architecture:** Module/tool 追加到 `test_framework.py`；Core 逻辑需要构造 mock agent/event_bus/cognitive_loop 来测试关键路径；提示 assembler 单独测试

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock

**Spec:** [2026-06-02-test-audit-design.md](../specs/2026-06-02-test-audit-design.md) 第三节(剩余) + 第十四节

---

## File Structure

```
Module/tool/test_framework.py    # 追加: SafetyPipeline streams, reload_plugin, cancel_all, definitions, concurrency
core/tests/test_phase3.py        # 追加: _think_loop 路径, _process_events 路径, _execute_tools_directly
core/tests/test_phase1.py        # 追加: _check_proactive, FSM edges
core/tests/test_phase2.py        # 追加: build_system_prompt happy path, consolidator BUFFER_THRESHOLD=6
core/tests/test_log_setup.py     # 追加: setup(), log_conversation()
```

---

### Task 1: Module/tool/test_framework.py 追加 — SafetyPipeline 流式 / ToolManager 剩余方法

**Files:**
- Modify: `Module/tool/test_framework.py` — 追加
- Read first: `Module/tool/safety.py` (SafetyPipeline), `Module/tool/manager.py` (ToolManager)

**Test scenarios:**

#### A. SafetyPipeline.execute_stream() 错误路径——全部零覆盖
- Pydantic 验证失败 → 验证返回 `ToolResult.fail(..., error_type=INVALID_ARGUMENT)`
- `_check_safety()` 返回错误 → 验证返回权限拒绝结果
- `_run_hitl()` 超时/拒绝 → 验证流式返回拒绝结果
- Semaphore 获取超时 → 验证返回 `RATE_LIMITED`
- 流式执行中 `asyncio.CancelledError` → 验证正确处理
- 流式执行中通用异常 → 验证返回 `TOOL_ERROR`
- Token 截断在流中触发 → 输出超过 `max_result_tokens` → 验证截断
- 流式插件返回 `ToolResult` chunk (line 364-366) → 验证提前终止

#### B. SafetyPipeline._check_safety() — URL / 文件系统
- 插件 `network_allowed=True` 但无 `Capability.NETWORK` → 验证拒绝
- 参数含 URL → `_validate_url` 被调用 → 验证可阻止私有 IP
- 参数含路径 → `_validate_path` 被调用 → 验证限制在 `read_dirs` 内
- 文件系统写操作 → `FILESYSTEM_WRITE` capability 检查

#### C. SafetyPipeline._run_hitl() 成功批准路径
- 设置 `_hitl_broadcast` callback (mock)
- 创建 approval request → `submit_approval(request_id, approved=True)` 在超时前 → 验证广播回调被调用
- `request_exists(request_id)` → 验证返回 `True`

#### D. ToolManager.reload_plugin()——零覆盖
- 重新加载已存在的插件 → 验证旧插件被 teardown + 新插件被 setup
- 插件不存在 → 验证返回/抛出适当错误
- 模块导入失败 → 验证错误处理
- 类未在重载模块中找到 → 验证错误处理

#### E. ToolManager 其他方法
- `cancel_all()` → 取消所有 inflight execution → 验证 `_executing` 清空
- `set_hitl_broadcast(output_manager)` → 验证对所有已加载 HITL 插件的 pipeline 设置 broadcast
- `definitions("anthropic")` → 验证返回 Anthropic 格式的工具定义
- `definitions("gemini")` → 验证返回 Gemini 格式
- `definitions("invalid_format")` → 验证 `ValueError`
- History `maxlen=50` 超限 → 写入 51+ 条 → 验证最早的被移除
- `_executing` 跟踪 — 并发提交 3 个 execution → 验证 `executing_now` 包含 3 个工具名

#### F. 并发场景
- `asyncio.gather(tm.execute("tool_a", ...), tm.execute("tool_b", ...))` → 两个并发调用各自成功
- `max_concurrency > 0` 的插件 — 并发超过 limit → 验证 queue/FIFO

**运行:** `conda run -n Elysia python -m pytest Module/tool/test_framework.py -v`

---

### Task 2: core/tests/test_phase3.py 追加 — _think_loop 路径 / _process_events / _execute_tools_directly

**Files:**
- Modify: `core/tests/test_phase3.py` — 追加
- Read first: `core/cognitive_loop.py`, `core/agent.py`
- Test double: `core/tests/fake_llm.py`

**Test scenarios:**

#### A. cognitive_loop:_think_loop() 关键路径——零直接单元测试
使用 `FakeLLMBackend` + mock 其他依赖来构造可测试的 CognitiveLoop：

- **单轮文本响应**: `FakeLLMBackend(response="Hello world")` → `_think_loop()` 跑完一轮 → FSM 进入 SPEAKING → TTS 被调用 → FSM 回到 IDLE
- **工具调用+重入**: `FakeLLMBackend` 第一次返回 tool_calls → `_execute_tools_directly` 执行 → 第二次返回最终文本
- **MAX_TOOL_ROUNDS 限制**: 连续 4 轮工具调用 → 第 5 轮被拒绝 → 强制输出文本
- **TTS 流式生命周期**: mock `_start_tts_stream()` → 验证首 chunk 到达时 text+audio 绑定 → `audio_end` 后 pending_emoji 发送
- **中断处理**: `cancel_current()` 在 THINKING 阶段调用 → 验证 `_task.cancel()` + FSM 转换到 INTERRUPTED

#### B. cognitive_loop:_execute_tools_directly()——零覆盖
- mock `ToolManager.execute()` 返回成功结果 → 验证结果被格式化
- mock `ToolManager.execute()` 返回错误结果 → 验证错误被记录
- JSON 参数解析失败 (JSONDecodeError) → 验证返回错误
- 多个 tool_calls 并发执行 → 使用 `asyncio.gather` → 验证所有结果被收集

#### C. agent:_process_events() 关键路径——零直接测试
- SYSTEM_TICK `silence_timeout` reason → 验证调用 `idle_monitor._check_proactive()`
- SYSTEM_TICK `time_of_day` reason → 同上
- STATE_CHANGE + 非空 `_pending_messages` → 验证 dequeue 逻辑
- INTERRUPT + 队列中的 STATE_CHANGE → 验证 INTERRUPT 优先 + STATE_CHANGE 被保留
- `bus.next()` 抛 RuntimeError → 验证循环不崩溃 (line 422-424)

#### D. agent.stop() 各超时路径
- Consolidator flush 超时 → 验证 CancelledError 被处理
- Event task cancel 超时 → 同上
- Cognitive/TTS task cancel → 同上
- EmbeddingFactory.shutdown 超时 → 同上

**运行:** `conda run -n Elysia python -m pytest core/tests/test_phase3.py -v`

---

### Task 3: core/tests/test_phase1.py + test_phase2.py 追加 — FSM / proactive / prompt / consolidator

**Files:**
- Modify: `core/tests/test_phase1.py` — 追加 idle_monitor / emotion_engine 补充
- Modify: `core/tests/test_phase2.py` — 追加 prompt_assembler / consolidator 补充
- Read first: `core/idle_monitor.py`, `core/prompt_assembler.py`, `core/consolidator.py`, `core/emotion_engine.py`

**Test scenarios:**

#### A. idle_monitor:_check_proactive()——零覆盖
- `proactive.enabled=True` + silence 达到 threshold → 验证 `SYSTEM_TICK(silence_timeout)` 事件被 post
- `proactive.enabled=True` + time_of_day trigger 到时间 → 验证事件被 post
- `proactive.triggers` 为空列表 → 验证不崩溃
- FSM 不是 IDLE → 验证跳过 (line 59-60)
- `_loop()` 中 CancelledError → 验证正确处理
- `_loop()` 中通用 Exception → 验证不崩溃

#### B. prompt_assembler:build_system_prompt() 正常路径——零覆盖
- 提供完整的 sensory/memory/state 数据 → 验证 Brain.j2 被成功渲染 → 输出非空
- LongTermMemory.search() 返回结果 → 验证 recalled memories 在 build_messages 中
- LongTermMemory.search() 无结果 → 验证不崩溃

#### C. consolidator BUFFER_THRESHOLD=6
- 当前所有测试使用 `buffer_threshold=1`。追加：
- `buffer_threshold=6` — 添加 5 条消息 → 验证不触发 consolidate → 添加第 6 条 → 验证触发
- 多个 consolidate 同时触发 → buffer 清空后立即又有消息 → 验证不丢数据

#### D. emotion_engine 补充
- STT emotion "angry" → 验证 `_push_expression` 映射到正确表情
- STT event "Cry" → 验证 expression
- STT event "Applause" → 验证 expression
- 多个 mood 同时 tie — 更全面的 tie-breaker 测试

#### E. log_setup 补充
- `log_conversation()` 写入日志 → 验证 RotatingFileHandler 被使用
- `setup(verbose=True)` 创建 LOG_DIR + 配置 library logger → 验证不崩溃（仅 smoke）

**运行:** `conda run -n Elysia python -m pytest core/tests/test_phase1.py core/tests/test_phase2.py core/tests/test_log_setup.py -v`

---

### Task 4: 补充 emotion_engine 关键词 fallback

**Files:**
- Read first: `core/emotion_engine.py`

**Not 测试——这是代码修复。** 审计发现 `MOOD_KEYWORDS` 只覆盖 happy/excited/sad/curious/neutral 5 种情绪，但 STT emotion 映射了 angry/fearful/disgusted/surprised。这些情绪无法通过文本触发。

- 给 angry/fearful/disgusted/surprised 各添加 3-5 个中文关键词
- 确保 `_analyze_keywords()` 能识别这些新关键词

**验证**: 对应测试在 `test_phase1.py` 的 `TestEmotionEngine` 中追加测试用例

**运行:** `conda run -n Elysia python -m pytest core/tests/test_phase1.py::TestEmotionEngine -v`

---

## 验证

```bash
# Module/tool remaining
conda run -n Elysia python -m pytest Module/tool/test_framework.py -v

# Core logic
conda run -n Elysia python -m pytest core/tests/ -v

# Full project
conda run -n Elysia python -m pytest Module/ core/tests/ core/tools/ -v
```
