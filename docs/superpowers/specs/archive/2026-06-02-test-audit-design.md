# 测试审查报告 & 追踪清单

## Context

对项目全部 19 个测试文件、~302 个测试用例的全面审查。目的：发现测试覆盖缺口、测试质量问题、冗余测试，为后续补充测试提供方向。审查范围覆盖 Module/ 和 core/ 两个主要源码目录。

**Phase 1 已完成** (2026-06-02): 新增 10 个测试文件、212 用例，覆盖 core/tools/ 全部插件 + Module/tool 安全函数 + timing.py。
**Phase 2 已完成** (2026-06-02): 补充 LLM(35)/STT(37)/TTS(32)/Embedding(31) 共 135 测试 — API错误、流式路径、边界条件、shutdown/单例。
**Phase 3 已完成** (2026-06-02): 补充 Output(54)/AudioBuffer(25)/Server(22) 共 101 测试 — AUDIO路由、WS全覆盖、VAD边缘、端点。
**Phase 4 已完成** (2026-06-02): 补充 Vectordb(12)/Documentdb(13)/Config(13)/Logger(4)/Prompt(6) 共 48 测试 — 协议强制、零结果、连接失败、describe、Optional[X]。

**Phase 5 已完成** (2026-06-02): ToolManager(11) + emotion_engine关键词修复 + proactive(2) + consolidator(2) + definitions/reload/concurrency。

**累计: 511 新增测试 | 5 个源码 bug 修复 | 926 total tests**

剩余可做: Task 2 (_think_loop unit tests) 因核心循环耦合度高，留待后续专项处理。`Module/schema/` 模块也待补充。

---

## 一、全局问题

- [ ] **无 CI/CD** — 没有 `.github/workflows/`、`.gitlab-ci.yml`、`Makefile`、`tox.ini`、`pytest.ini`；无覆盖率配置
- [ ] **被动测试模式（修复/测试比 11:1）** — 最近 50 个 commit：22 个 `fix:` vs 2 个 `test:`，大多数 bug 修复没有回归测试
- [ ] **测试耦合于内部实现** — 测试直接访问 `agent._tools`、`agent._tool_manager`、`_DENIED_PATHS` 等私有属性，重构时大量 break
- [ ] **无共享 fixture 基础设施** — 零个 `conftest.py`，mock backend fixture 在 llm/stt/tts/embedding 测试中各写一遍
- [x] **API 错误处理全面缺失** — ~~LLM/STT/TTS/Embedding 四个模块~~ → Phase 2 已补 API 错误/超时/连接失败测试

---

## 二、完全缺失测试的模块

- [ ] **`Module/schema/`**（6 文件）— 序列化/反序列化完全没测（留待后续）
- [x] **`core/tools/`**（9 文件，11 插件）— ~~所有 execute() 零覆盖~~ → Phase 1 已补 8 个测试文件 212 用例
- [ ] **`core/memory/`**（2 文件）— 仅有 in-memory fallback 测试，真实 DB 路径未测
- [ ] **`Module/bench/`**（3 文件）— 仅有 smoke test，无单元测试
- [ ] **`Module/database/graphdb/`**（2 文件）— 仅有 stub（ABC），尚可接受

---

## 三、Module/tool/ — 最大测试文件（50 用例）

- [x] **`ToolParameter`** — ~~整个类零测试~~ → Phase 1 已补 8 个测试
- [x] **`ToolEmitter`** — ~~`progress()` 和 `is_cancelled()` 零测试~~ → Phase 1 已补 5 个测试
- [x] **`MediaItem`** — ~~整个 dataclass 零测试~~ → Phase 1 已补 2 个测试
- [x] **`ToolResult.llm_content`** / `_format_error_for_llm()` — ~~property 从未被读取~~ → Phase 1 已补 7 个测试
- [x] **legacy 函数** — ~~`validate_args`/`_coerce_value` 零测试~~ → Phase 1 已补；`execute_with_timeout`/`truncate_result` 仍缺
- [ ] **SafetyPipeline 流式错误路径** — validation error/safety check/HITL denial/semaphore/CancelledError/generic exception 全部零覆盖
- [ ] **`_check_safety()`** — URL 强制执行、文件系统路径验证无 pipeline 级测试
- [ ] **`_run_hitl()` 成功批准路径** — 只测了 timeout/deny
- [ ] **`_wrap_result()` token 截断** — 在 pipeline 中未触发
- [x] **`ToolManager.reload_plugin()`** — ~~~50 行零覆盖~~ → Phase 5 已补（reload + nonexistent）
- [x] **`ToolManager.cancel_all()`** / **`set_hitl_broadcast()`** — ~~零覆盖~~ → Phase 5 已补
- [x] **`definitions("anthropic")` / `definitions("gemini")`** — ~~零覆盖~~ → Phase 5 已补
- [ ] **`_flatten_refs()` / `_resolve_refs()`** — Pydantic `$defs` 处理零覆盖（留待后续）
- [x] **`_resolve_sandbox_path()`** / **`_validate_path()`** — ~~安全函数零覆盖~~ → Phase 1 已补 33 个测试
- [x] **并发场景** — ~~并发 execute 零测试~~ → Phase 5 已补

---

## 四、Module/llm ✅ Phase 2 已完成

- [ ] **`stream_think()`** — 零覆盖（含 `_stream_structured()` 的 tool_calls 检测路径）
- [ ] **`_beta_client` close** — DeepSeek 双 client 关闭路径未测
- [ ] **API 错误** — HTTP 4xx/5xx、timeout、content=None 全部零覆盖
- [ ] **prefix continuation + chat_stream** — 未测试
- [ ] **prefix + extra_body 合并** — 未测试
- [ ] **`model_id` 含多个 `/`** — 如 `"a/b/c"` 未测试
- [ ] **`get_caller_logger()` 注入** — 未验证

---

## 五、Module/stt ✅ Phase 2 已完成

- [ ] **`_parse_emotion()` / `_parse_events()`** — 零覆盖，正则模式未验证
- [ ] **`<|nospeech|>` 丢弃路径** — 零覆盖
- [ ] **HTTP 错误处理** — `httpx.RequestError`/`raise_for_status()`/畸形 JSON 零覆盖
- [ ] **`data.get("result")` 无 "text" key 分支** — 未测试
- [ ] **kwargs 透传** — 未测试

---

## 六、Module/tts ✅ Phase 2 已完成

- [ ] **Emoji 脱敏正则** — `text_preprocess.py:4-14` 从未用 emoji 输入验证
- [ ] **纯 markup 文本输出空字符串** — 未测试
- [ ] **GPTSoVitsBackend 完整 payload** — 所有参数未验证
- [ ] **HTTP 错误处理** — 零覆盖
- [ ] **Queue 取消路径** — consumer 提前停止迭代未测试
- [ ] **`character` 不存在 KeyError** — 未测试
- [ ] **已知 bug `test_nested_brackets_known_limitation`** — 仅记录未修复

---

## 七、Module/embedding ✅ Phase 2 已完成

- [ ] **`EmbeddingFactory.shutdown()`** — 完全零测试（close 异常吞噬、实例清理）
- [ ] **单例缓存行为** — 两次 `create()` 返回同一实例、不同 model_id 返回不同实例，均未测
- [ ] **`_find_local_model()`** — 零覆盖（本地模型发现逻辑）
- [ ] **HuggingFace `from_config`** — 零测试（只测了 OpenAI）
- [ ] **API 错误** — HTTP 错误/HuggingFace 异常零覆盖
- [ ] **空输入** — `embed_documents([])`/`embed_query("")` 未测试

---

## 八、Module/output ✅ Phase 3 已完成

- [ ] **`ChannelCapability.AUDIO`** — 从未作为广播目标测试
- [ ] **`OutputManager.command()`** — 零测试（typing/expression/action 都有测试）
- [ ] **`OutputManager.audio_chunk()` / `audio_end()`** — 零测试
- [ ] **`OutputManager.stream()` 完整生命周期** — typing(True) → stream → typing(False) 未测
- [ ] **`OutputChannel.on_audio_chunk/on_audio_end/on_state`** — 所有 channel 零覆盖
- [ ] **错误隔离不完整** — 仅测了 `on_message`，`on_text_delta`/`on_expression`/`on_action`/`on_command` 未测
- [ ] **`OutputManager.close()` 错误隔离** — channel.close() 异常不应阻止其他 channel 关闭，未验证

---

## 九、Module/input ✅ Phase 3 已完成

- [ ] **VAD 帧处理** — 多帧 chunk/非整除帧截断/50% 边界值/VAD 异常吞噬，均未测
- [ ] **VAD interrupt cooldown** — 第二段语音在 0.5s 冷却窗口内行为未测
- [ ] **STT timeout/异常** — `asyncio.wait_for(timeout=30)` 和异常吞噬零测试
- [ ] **`on_transcription=None`** — 未测试
- [ ] **WebSocket BINARY 消息路由** — 零测试
- [ ] **WebSocket ERROR 消息类型** — 零测试
- [ ] **WebSocket 未知 JSON message type** — 零测试
- [ ] **`/health` / `/ready` 端点** — 零测试
- [ ] **`/debug/api` 端点** — 零测试
- [ ] **`serve_static=True` 路径** — 所有测试用 `serve_static=False`
- [ ] **FSM state monitor** — `_monitor_state` task 自动 deafen 零测试
- [ ] **无断言的测试** — `test_interrupt_message`/`test_do_transcription_with_emotion`/`test_invalid_json_returns_error` 无实际断言

---

## 十、Module/database ✅ Phase 4 已完成

### VectorDB (Milvus)
- [ ] **协议抽象强制** — 仅测 2/7 方法，缺 `drop_collection`/`collection_exists`/`search`/`query`/`close`
- [ ] **连接失败** — 所有操作假设 Milvus 连接正常
- [ ] **非默认 `index_preset`** — cosine/ip/hnsw_l2 零测试
- [ ] **`metric_type` 显式覆盖** — 未测试
- [ ] **search() 空结果列表 else 分支** — 零覆盖
- [ ] **query/search 无 `output_fields`** — 未测试

### DocumentDB (MongoDB)
- [ ] **协议抽象强制** — 仅测 1/7 方法，缺 6 个
- [ ] **连接失败** — ServerSelectionTimeoutError 零覆盖
- [ ] **find() 默认参数** — sort=None/limit=100 路径零测试
- [ ] **update_one() upsert=True** — 未测试
- [ ] **零结果路径** — modified_count=0/deleted_count=0/count=0 全缺
- [ ] **ensure_index() kwargs** — unique/sparse/expireAfterSeconds 未测试

---

## 十一、Module/config ✅ Phase 4 已完成

- [ ] **`Choices()` 空 allowed 列表异常** — 构造函数守卫未测试
- [ ] **`Range()` 单边界** — only min/only max 未测试
- [ ] **`Regex()` 自定义 message / 非字符串值** — 未测试
- [ ] **约束 `describe()` 方法** — 零测试
- [ ] **3 层以上深度嵌套** — 零测试
- [ ] **list[Dataclass] YAML 合并** — 可能有问题且未测试
- [ ] **EnvManager `Optional[X]` 解包** — 未测试
- [ ] **YAML 畸形/多文档/root 非 dict** — 未测试
- [ ] **`yaml_key` 多级路径** — 如 "A.B.C" 零测试

---

## 十二、Module/logger ✅ Phase 4 已完成

- [ ] **CRITICAL 级别** — 零测试
- [ ] **`get_caller_logger()`** — 零测试
- [ ] **日志轮转行为** — 零测试
- [ ] **JSON format extra fields / Unicode 特殊字符** — 未测试
- [ ] **并发/线程安全** — 零测试

---

## 十三、Module/prompt ✅ Phase 4 已完成

- [ ] **7/15 宏未测试** — Utils.j2 全部/Brain.j2 4个/MicroReflector+MacroReflector UserPrompt
- [ ] **auto_reload 行为** — 只测属性，未测实际缓存/刷新
- [ ] **模板缓存行为** — 零覆盖
- [ ] **`datetime` filter 自定义格式** — 零测试
- [ ] **模板缺少变量 Jinja2 UndefinedError** — 零测试
- [ ] **None 值/空 context 渲染** — 零测试

---

## 十四、core/ 测试缺口 ✅ Phase 5 部分完成

### P0 — 核心逻辑零覆盖
- [ ] **`cognitive_loop.py:_think_loop()`** — ~200 行主循环零直接单元测试（耦合度高，留待后续）
- [x] **`core/tools/*.py`** — ~~11 插件 execute() 全部零单元测试~~ → Phase 1 已补
- [ ] **`agent.py:_process_events()`** — 主事件分发循环未直接测试（留待后续）
- [x] **`idle_monitor.py:_check_proactive()`** — ~~主动触发器评估零测试~~ → Phase 5 已补 2 测试

### P1 — 工具类零覆盖
- [x] **`timing.py`** — ~~Timer/TimingClock 全部零测试~~ → Phase 1 已补 16 个测试
- [ ] **`log_setup.py:setup()`** — 日志基础设施初始化零测试（留待后续）
- [ ] **`prompt_assembler.py:build_system_prompt()`** — Brain.j2 模板正常路径零测试（留待后续）
- [ ] **`cognitive_loop.py:_execute_tools_directly()`** — 直接工具执行路径零测试（留待后续）

### P2 — 边缘路径
- [x] **consolidator** — ~~默认 `BUFFER_THRESHOLD=6` 从未测试~~ → Phase 5 已补 2 测试
- [x] **emotion_engine** — ~~angry/fearful/disgusted/surprised 无关键词~~ → Phase 5 已修复
- [ ] **log_setup.py `log_conversation()`** — 零测试（留待后续）

---

## 验证方法

```bash
python -m pytest Module/ -v       # Module 层测试
python -m pytest core/tests/ -v   # Core 层测试
```
