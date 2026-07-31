# 待做

> **CI 配置**：`Module/task_system/` 尚未完善、非当前开发重点，`test-quick` 和 `test-normal` 已通过 Makefile 跳过该目录（`--ignore=Module/task_system/tests`）。完整跑 `test-full` 或手动指定路径时仍会执行。

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **LLM 有时不输出 <mood> 标签** | deepseek-v4-flash 遵循结构化输出不稳定，部分轮次 `<mood>` 缺失，回退到关键词 fallback。需强化 prompt 或在 F2 加更强制性的约束 |
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 低 | **`test_integration_global_constraints_respected` 偶发失败** | `Module/task_system/tests/integration/test_delegation_preparer.py`: 全量跑偶发 FAIL，单独跑 PASS，疑似 TaskSystem 集成测试时序/资源竞争 |
| 低 | **`test_multi_step_task` 偶发失败** | `Module/task_system/tests/integration/test_planner.py`: DeepSeek API 偶发返回截断 JSON (`Unterminated string`)，LLM 输出不稳定导致 `PlanParseError` |
| 中 | **TTS 密集短句导致 wait_all 阻塞** | TTS 连续 5+ 短句密集入队时 MiniMax 部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING。已缩短 read timeout 120s→30s 缓解，根因（wait_all 无超时）待重现后修复。 |
| 低 | **`flush_after_response` 超时层不匹配** | 内层 `loop.py:875` 每 task 5s，外层 `lifecycle.py:170` 整体 10s。3+ task 同时超时可能被外层截断。实际 pending 典型 0-2 个、亚秒级完成，impact 极低。修法：外层 timeout 改为 `max(10.0, len(pending) * 5.0 + 1.0)` |
| 低 | **`ConfirmModal` @keydown 在不可聚焦 `<div>`** | `frontend/src/debug/components/shared/ConfirmModal.vue:35` — overlay div 无 `tabindex`，依赖按钮聚焦后事件冒泡。弹窗内只有文字+两个按钮，点击文字不会失焦，现有行为已可靠。加固：加 `tabindex="0"` |

## 来自 code-review-2026-07-31 的遗留问题（44 项）

> 来源：[docs/code-review-2026-07-31.md](docs/code-review-2026-07-31.md)。全量审查（core/ + Module/ + frontend/），7 agent 审查 + 47 agent 验证（≥75 分保留），4 条已实测复现。基线 commit `e782de9`。

### Critical（1 项）

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | **C1 `_poll_mailboxes` 空 mailbox 无限自旋冻结事件循环** | [agent.py:357-365](core/agent/agent.py#L357-L365) + [mailbox.py:68-74](Module/task_system/mailbox.py#L68-L74)。`get_nowait()` 空队列返回 `None` 而非抛 `QueueEmpty`，`if event is None: continue` 造成无 await 无限同步自旋。任何 `submit_task` 后 ~500ms 内触发，WS/HTTP/STT/TTS/认知循环全部冻结。修法：`continue`→`break`。另注意 restart 后 `_background_tasks` 未清空 |

### Major（20 项）

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | **M6 `chat_stream` usage 终结 chunk 抛 IndexError** | [openai_compat.py:199-209](Module/llm/openai_compat.py#L199-L209)。流式请求 `include_usage=True`，usage 收尾 chunk `choices` 恒为空，`chunk.choices[0].delta` 先于 usage 判断必然 IndexError。所有 provider 均经此路径，**每次流式回复结束崩溃**。`_stream_structured` 有 `if not chunk.choices: continue` 守卫，唯独 `_call_api` 漏掉 |
| **高** | **M14 历史消息分页游标 `_id` 不存在且取错方向** | [chatStore.ts:243,258-292](frontend/src/stores/chatStore.ts#L243) + [message.py:82-100](core/schema/message.py#L82-L100) + [api_routes.py:292](core/input/api_routes.py#L292)。session 消息无 `_id` 字段，游标恒 null → 每页与上一页重叠 49/50 条。上滑加载历史是常态路径，历史分页实质不可用 |
| **高** | **M15 排队气泡音频流中断时永久隐藏** | [chatStore.ts:51-58,225-234](frontend/src/stores/chatStore.ts#L51) + [audioStore.ts:64-96](frontend/src/stores/audioStore.ts#L64)。`queued` 标记唯一清除路径是 `revealNextQueued()`，播放中用户继续发消息时被移出队列的气泡永久消失 |
| **高** | **M17 `python -m Module.system_prompt` 入口必崩** | [__main__.py:27](Module/system_prompt/__main__.py#L27)。`ConflictRulesSection()` 无参实例化，但 `Jinja2Section.__init__(template_path)` 参数必填。**已实测复现**。修法照抄 `prompt_assembler.py:73` 传 `f1_conflict_rules.j2` |
| **高** | **M18 `python -m Module.config` 入口必崩** | [__main__.py:23](Module/config/__main__.py#L23) + [loader.py:215,240](Module/config/loader.py#L215)。`ConfigLoader(dict, ...)` 传普通类型，loader 无条件 `dataclasses.fields()` 抛 TypeError。**已实测复现**。其他调用方均传 `AppConfig` |
| **高** | **M23 Escape 同时触发拒绝审批 + 打断生成** | [App.vue:23-28](frontend/src/App.vue#L23) + [ApprovalDialog.vue:39-46](frontend/src/components/ui/ApprovalDialog.vue#L39)。两个 document 级 keydown 都不阻止传播，Escape 拒绝审批连带杀掉整个回合 |
| **中** | **M1 `conversation_text[:2000]` 截断方向错误** | [consolidator.py:243](core/memory/consolidator.py#L243)。`[:2000]` 保留最旧丢弃最新，而 `_buffer.clear()` 无条件执行，被截掉的最新轮次永远不再提取。应取 `[-2000:]` |
| **中** | **M2 `_sync_dirty_entries` 死代码，Milvus 失败记忆永不重试** | [long_term.py:282-315](core/memory/long_term.py#L282)。insert 失败入 `_dirty_ids` 但全仓库无调用者同步；重启后 `_load_from_milvus` 只从 Milvus 恢复，脏条目彻底丢失 |
| **中** | **M3 CharacterReflector buffer 竞态** | [reflector.py:107-123](core/personality/reflector.py#L107)。`buffer()` 在获取 `_reflect_lock` **之前** append；反思期间新到的对话对在 `_reflect()` 成功后无条件 `clear()` 时被清空，永久丢失 |
| **中** | **M4 Gate 拒绝路径未恢复 THINKING，FSM 卡 EXECUTING** | [loop.py:517-518](core/cognitive/loop.py#L517) + [fsm.py:166-196](core/status/fsm.py#L166)。gate 拒绝直接 `return` 跳过 `return_to_thinking()`，下一轮 SENTENCE 触发非法转换抛 ValueError，向用户广播虚假错误 |
| **中** | **M5 text-only 模式同一句文本双路径重复广播** | [loop.py:928-951](core/cognitive/loop.py#L928) + [stream_processor.py:134-137](core/cognitive/stream_processor.py#L134)。`_tts_mgr.active` False 时 TEXT_DELTA 与 SENTENCE 都向同一通道广播相同文本，text-only 模式输出翻倍 |
| **中** | **M8 MiniMax 停顿标签 `<#x#>` 从 TTS 文本剥离** | [tts_adapter.py:258-268](core/adapter/tts_adapter.py#L258)。docstring 声明标签送 TTS，但 `_PAUSE_TAG_RE.sub("", ...)` 在合成前剥离，StreamProcessor 又剥一次，F3 停顿功能端到端失效。需额外设计流式分句下的标签保留 |
| **中** | **M9 gpt-sovits `_pull` 任务异常静默吞掉** | [gpt_sovits.py:162-205](Module/tts/gpt_sovits.py#L162)。`raise_for_status()` 抛的 `HTTPStatusError` 不匹配 `except httpx.RequestError`，异常冒出但 finally 无条件入队 None 哨兵，调用方收到空音频静默无声 |
| **中** | **M10 `/debug/api` 精确路径绕过认证中间件** | [debug_routes/__init__.py:31](core/input/debug_routes/__init__.py#L31) + [overview.py:14](core/input/debug_routes/overview.py#L14)。中间件 `startswith("/debug/api/")` 对精确路径 `/debug/api` 恒 False，而前端轮询的正是此路径。**（2026-07-31 #9 修复引入的边界漏洞）** |
| **中** | **M11 `time_of_day` 去重键无日期成分，跨天永久抑制** | [idle_monitor.py:120-127](core/monitoring/idle_monitor.py#L120)。仅 `%H:%M`，触发后次日同刻恒 return，每日定时主动搭话在进程生命周期内仅触发一次。注：消费端 `_handle_system_tick` 目前以 TODO 静默丢弃 time_of_day 事件 |
| **中** | **M13 重连时 HITL 审批重广播缺 type 字段，前端永远收不到** | [ws_handler.py:73-89](core/input/ws_handler.py#L73)。缺 `type` 字段 + 参数名 `tool_name` vs `tool` + `params` 对象 vs 字符串，三方全偏离。断线重连时挂起审批永远悬空 |
| **中** | **M16 唤醒词开关是死 UI** | [settingsStore.ts:23,51-53](frontend/src/stores/settingsStore.ts#L23) + [ws_handler.py:172-176](core/input/ws_handler.py#L172)。前端无任何代码发送 `wake_toggle`，开关只写 localStorage，实际启用状态由启动配置决定 |
| **中** | **M19 resume 后再次 pause 的任务被销毁而非保持 PAUSED** | [task_system.py:384-393](Module/task_system/task_system.py#L384)。`_run_resume` finally 无条件 cleanup + 删 checkpoint，缺 PAUSED early-return 保护。之后 resume 返回 CHECKPOINT_NOT_FOUND，mailbox 消费者永久阻塞 |
| **中** | **M20 cancel 在规划/校验阶段不推终态事件** | [lifecycle.py:157-167](Module/task_system/lifecycle.py#L157) + [planning.py:143-145](Module/task_system/core/planning.py#L143)。`planning.plan_and_validate` 无条件把 status 覆写回 RUNNING，规划窗口取消被静默吞掉。`TaskSystem.cancel()` 尚无调用方接线，暂为潜在 |
| **低** | **M7 `_format_tool_summary` 对非 dict arguments 抛 AttributeError** | [message_builder.py:116-121](core/adapter/message_builder.py#L116)。`[1,2]`/`"hello"`/`42` 都是合法 JSON，`args.items()` 抛 AttributeError 未被捕获；非 dict arguments 持久化进 session 后**下一轮必然再次触发**，持续毒化会话。同轮 tool_executing emit 循环同样未捕获 |
| **低** | **M12 STT 语音输入不调用 `idle.touch()`** | [ws_handler.py:271-279](core/input/ws_handler.py#L271)。语音路径直投 `UserInputData` 绕过 `process_message`（全库唯一 touch 调用点），纯语音对话超 interval 误触发"用户没有说话"主动发言。默认 `proactive.enabled: false` 不触发 |

### Minor（23 项）

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 低 | m1 shutdown flush 与 cancel 认知任务竞态 | `lifecycle.py:50-66` flush 先于取消任务，orphan after_response task 与已关闭的 LLM/session repo 并发，最后轮记忆沉淀丢失 |
| 低 | m2 start() 失败留下僵尸 Agent | `lifecycle.py:124-145` 先置 RUNNING 再跑 STARTUP_STEPS，失败无回滚；4 个 create_task 点无幂等保护，重试生成重复循环 |
| 低 | m3 fire-and-forget consume_trigger 无异常处理 | `reflector.py:325-327` + `compression.py:243-250` 裸 create_task 无 done callback，MongoDB 写入失败丢 C6 刷新，异常 "Task exception was never retrieved" |
| 低 | m4 KMeans 聚类在事件循环内同步执行 | `memory_reflector.py:200-221` 纯同步 CPU 密集，无 `run_in_executor`，大 LTM 下阻塞整个 agent |
| 低 | m5 `update_text` embedding 不可用时陈旧向量残留 | `long_term.py:344-373` emb 为空跳过整个块无 `_dirty_ids` 标记（对比 except 分支不对称），旧向量与新文本绑定参与 rerank |
| 低 | m6 图片上传先整读 body 再校验大小 | `api_routes.py:217-218` multipart 原始流不受 `client_max_size` 限制，10MB 上限在整块 read 后才检查。无认证端点 + 0.0.0.0 绑定 = 真实 DoS 向量 |
| 低 | m7 archive `role` 非法值 500 | `debug_routes/archive.py:49` `MessageRole(role_str)` 抛 ValueError 在 try 外 |
| 低 | m8 版本历史路由 `int()` 无保护 500 | `debug_routes/character.py:107-109,156-158` `?limit=abc`/`?before=xyz` 抛 ValueError；同包 ltm.py 有 400 惯例 |
| 低 | m9 `deafen(2.0)` 只在 SPEAKING 上升沿触发 | `ws_handler.py:113-126` 长回复期间回声可自我打断（SPEAKING 不在抑制集合） |
| 低 | m10 WS 接收循环只捕获 JSONDecodeError/ValidationError | `ws_handler.py:130-141` 客户端发非 dict JSON → `data.get` 抛 AttributeError 连接直接 1006 断开，无诊断 |
| 低 | m11 WS 无 heartbeat/接收超时 | `ws_handler.py:43` 半开连接产生僵尸 channel + monitor task，广播被拖慢、连接无限累积 |
| 低 | m12 `flush_emojis()` 在最后一句 TTS 下载完成前广播 | `tts_adapter.py:181-190` 最后一句 emoji 先于句子文字显示，破坏"句子😊"契约 |
| 低 | m13 OpenAI embedding 无超时 | `openai_compat.py:42` AsyncOpenAI 默认 600s 总超时，`embed_query` 无 wait_for 兜底，insert/recall 被拖住 10 分钟 |
| 低 | m14 `get_caller_logger` 双跳 f_back.f_back 取错模块 | `Module/logger/factory.py:54` 正确层级应一层，logger 名归属错乱（当前 app 走 log_setup，影响暂有限） |
| 低 | m15 MilvusBackend.close() 空实现 | `milvus.py:192-193` pass + 事实错误注释；`/restart` 每次新建连接，gRPC 通道/线程随重启累积 |
| 低 | m16 DelegationPreparer 自建 AsyncOpenAI 客户端从不关闭 | `delegation_preparer.py:118-128` 无 close 接口，`/restart` 每次泄漏一个 httpx 连接池 |
| 低 | m17 `Module/system_prompt/__init__.py:9` eager import core.adapter | 任何 `import Module.system_prompt` 连带拉入整个 core.adapter，违反 Module 层 backend-agnostic 分层 |
| 低 | m18 token_usage 字段名不匹配（by_model vs byModel） | 后端统一 `by_model`，前端按 `byModel` 读取恒 undefined，按模型拆分统计永不渲染。`restoreFromServer` 的 `_tokenStats` 形参完全未用 |
| 低 | m19 消息时间戳恒为当前时间 | `chatStore.ts:332-411` 读 `m.created_at`（两数据源均只有 `timestamp`）→ NaN → `Date.now()`，时间分隔符失效。归档 tool 消息 `tool_name`/数组 content 渲染错乱 |
| 低 | m20 SSE 重连"指数退避"失效 | `TasksTab.vue:17-69` `connect()` 无条件 `reconnectDelay = 1000` 把退避清零，间隔恒 1000ms |
| 低 | m21 模型切换乐观插入 + 结果回包双插 | `SettingsPanel.vue:80-83` + `useWebSocket.ts:113-120` 成功时逐字重复，失败时先显示虚假成功再显示失败 |
| 低 | m22 时间分隔符索引错位 | `MessageList.vue:22-27` 遍历 visibleMessages 却用 messages 下标，queued 隐藏期间错位/漏显 |
| 低 | m23 审批弹窗 Escape 双重触发 | `App.vue:23-28` + `ApprovalDialog.vue:39-46` 同一次 Escape 同时 interrupt + deny |

> 已过滤 3 条（1 误报：重试 off-by-one 是刻意设计；2 条置信度 50：WS 吞 CancelledError、音色 YAML 解析）——见源文档"已过滤的发现"。修复优先级建议见源文档末尾。

## 功能改进

- **README 截图**: 添加聊天界面 / debug 面板 / 启动流程截图，让新用户第一眼就知道项目长什么样
- **Live2D 集成**: Cubism SDK v5 需 TypeScript + npm + bundler 构建，当前仅 SDK 文件到位、默认关闭
- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **MiniMax SSE 真流式**: 已尝试并回退 (2026-07-10)。MiniMax SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复，只取最后 event 又等于同步下载。待 MiniMax API 支持增量 SSE 或换方案（WebSocket）
- **MiniMax 异步长文本合成**: `t2a_async_v2`，适合 >3000 chars。需创建任务 → 轮询状态 → 下载文件
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **Claude 原生持久记忆**: `.claude/projects/-home-yomu-Agent/memory/` 目录为空，从未启用
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **CharacterEngine mood 升级**（原 EmotionEngine 升级）: LLM `<mood>` 标签驱动已实现。生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充等后续 Phase 待做。
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **多角色 prompt 模板**: `PromptAssembler` 通过 `SystemPromptManager` + 12 `PromptSection` 渲染，多角色时加载不同 `CharacterConfig` 即可切换 section 数据源
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + SenseVoice + GPT-SoVits）。数据库已完成 docker-compose.yml（MongoDB + Milvus）
- **F2 代码块格式重新启用**: `f2_output_format.j2` 中 `<|code|>` 和 `<|markdown|>` 格式已注释。等需要代码/Markdown 渲染功能时取消注释即可启用
- **意图路由 + 渐进式工具披露**: 当前 13 个工具全量暴露给 LLM，每人次 ~1,643 schema tokens。大部分对话只用 2–4 个工具，~70% schema token 是浪费的。评估结论：13 个工具未到临界点，暂不做。触发条件：工具数 > 15 考虑规则分类，> 20 必须引入分类层，或累计 ≥ 3 次工具误用事件。推荐方案：关键词匹配分组（filesystem/web/memory/utility），零延迟零 token 成本，无命中则全量兜底。
- **CharacterEngine Phase 2**（原 PersonalityEngine Phase 2）:
  - CharacterReflector.j2 模板 ~140 行偏长，LLM 注意力可能被稀释，后续观察产出质量决定是否精简
  - C3 情绪传染规则动态化
  - C7 主动性动态化
  - L4→L3 情绪汇聚统计（需 CharacterEngine 改接口）
  - 用户话题检测触发 C9（目前只看状态存在+冷却）


## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |
| 低 | **FilePart/AudioPart/VideoPart 预留代码** | `part.py` ~70 行 + `message_adapter.py` 3 个 helper + `Part` union。`message_adapter.py:188` 注释明确 Phase 2。暂保留 |

## 模块化改进

| 优先级 | 目标 | 说明 |
|--------|------|------|
| ~~低~~ ❌ | **`Module/tool/safety.py`（618 行）** | 评估结论：不拆 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` 仍含真实 API Key | DeepSeek、MiMo、Tavily、MiniMax 的真实 key 仍在 `.env` 文件中。已有 `.env.example` + `setup.sh` + `app.py --check`，但 `.env` 不应提交真实密钥。建议：将 `.env` 中 key 替换为占位符，真实 key 通过 shell 环境变量或密钥管理器注入 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |
| **高** | **无数据库迁移机制** | MongoDB 索引在 `connect()` 时 ad-hoc 创建，Milvus collection 硬编码创建。若字段/索引/维度需变更，无 upgrade path。`memory_repo.clear_all()` 是破坏性操作无安全守卫 |
| 低 | **`character/cyrene/` 不可用** | 缺少 `config.yaml` 和 `personality.md`，仅含 GPT 参考材料，无法作为角色加载 |
| 低 | **依赖无版本上限** | `requirements.txt` 中 pydantic、openai、torch 等均无上限约束，上游 breaking change 可导致不可预期问题。`torch` 是 2GB+ 重依赖 |
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |


## 来自 code-review-report-2026-06-22 的遗留问题

### 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN `integrity`、Content-Security-Policy |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线换新 API |

### 中低危（待排期）

| 问题 | 文件 |
|------|------|
| llm_adapter close fire-and-forget | `core/adapter/llm_adapter.py:140-143` |
| openai_tts test() 假阴性 | `tts/openai_tts.py:73` |
| TTS 取消+HTTP 流清理交互可能泄漏 | `tts/gpt_sovits.py:134-150` |
| 缺失测试覆盖 | DescribeImagePlugin (0)、CognitiveLoop 边界、OutputManager 错误隔离 |

## 测试补充

- `core/input/audio_buffer.py` (3)
- `core/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时
- 上述覆盖率缺口中标记为"关键"和"高"的路径

> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
