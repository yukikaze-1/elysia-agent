# 全量代码审查报告 — 2026-07-31

## 审查概览

- **范围**：全仓库代码（`core/` 194 文件、`Module/` 202 文件、`frontend/src/` 54 文件、根目录 `app.py`/`paths.py`/`config.yaml`/`scripts/`/`tests/`）
- **排除**：`docs/` 全部（含 specs/plans/superpowers/completed）、`character/`（角色资源）、`static/`、`data/`、`log/`、构建产物
- **方法**：7 个并行审查 agent 按子系统分片（core/agent+cognitive+status；core/memory+personality+storage；core/input+monitoring+debug；core/adapter+config+schema；Module AI 后端五模块；Module 基础设施；frontend）→ 47 条原始发现 → 47 个独立验证 agent 按 rubric 打分（0-100，过滤 <75）→ 3 条被过滤（1 条误报、2 条置信度不足）
- **实证**：4 条发现经实际运行复现（`python -m Module.system_prompt` / `python -m Module.config` 必崩、`_format_tool_summary` 对非 dict arguments 抛 AttributeError、openai 流式收尾 chunk IndexError 经项目自身测试注释证实）
- 审查基线：commit `e782de9`（工作树干净）

## 统计

| 严重度 | 数量 | 说明 |
|--------|------|------|
| Critical | 1 | 事件循环冻结 |
| Major | 20 | 功能错误 / 数据丢失 / 安全 |
| Minor | 23 | 健壮性 / 一致性 / UX |
| **合计** | **44** | 另有 3 条被过滤（含 1 条误报，见文末） |

置信度分布：100 分 ×7、90 分 ×4、85 分 ×2、80 分 ×2、75 分 ×29。100 与 75 的差异主要在触发频率而非真实性。

---

## Critical

### C1. `_poll_mailboxes` 空 mailbox 无限自旋，事件循环永久冻结 【置信度 100】

**位置**：`core/agent/agent.py:357-365`（配合 `Module/task_system/mailbox.py:68-74`）

内层 `while True` 中 `mailbox.get_nowait()` 空队列返回 `None` 而非抛 `asyncio.QueueEmpty`，因此 `except asyncio.QueueEmpty: break` 是死代码，`if event is None: continue` 造成无任何 await 的无限同步自旋，占死事件循环。

任何 `submit_task` 成功注册后台任务后，首次轮询到空 mailbox（~500ms 内几乎必然）即触发：WS/HTTP/STT/TTS/认知循环全部冻结，无法响应、无法停止。`TaskMailbox.get_nowait` 的契约有单元测试锁定（"空队列 get_nowait 返回 None"），说明该路径从未被端到端验证过。

**修复**：`if event is None: continue` 改为 `break`。另注意 restart 后 `_background_tasks` 未清空（`agent.py:373/383` 只在终态事件时 pop，stop 路径不清理），旧条目会再次进入自旋。

---

## Major

### M1. `conversation_text[:2000]` 截断方向错误 — 最新轮次记忆提取永久丢失 【置信度 75】

**位置**：`core/memory/consolidator.py:243`（配合 234-238、254-255）

`turns` 按时间顺序构建后取 `[:2000]`（保留最旧、丢弃最新），而 LLM 成功后 `self._buffer.clear()` 无条件执行——被截掉的最新轮次没有任何补偿机制（MemoryReflector 只对 LTM 去重，不重新提取对话），永远不会再被提取。按每轮 300+500 字符上限，6 轮缓冲轻松超过 2000 字符，正常对话量下频繁触发。应取 `[-2000:]`。

### M2. `_sync_dirty_entries` 是死代码 — Milvus 写入失败的记忆永不重试，重启后永久丢失 【置信度 80】

**位置**：`core/memory/long_term.py:282-315`（配合 192-196、493-499）

`insert()` 在 Milvus 失败时把 entry 加入 `_dirty_ids` 并保留在内存，但全仓库（除测试外）无任何调用者同步脏条目；`_db_health_loop` 只做 health_check。后果：故障窗口期的新记忆 `_milvus_pk=None`，`search()` 的 Milvus 路径按 pk 匹配永远排除它们；重启后 `_load_from_milvus` 只从 Milvus 恢复，脏条目彻底丢失。专门设计的脏标记机制未接线。

### M3. CharacterReflector buffer 竞态 — 反思期间新到的对话对被清空，从未被反思 【置信度 75】

**位置**：`core/personality/reflector.py:107-123`（配合 312-314）

`buffer()` 在获取 `_reflect_lock` **之前** append 对话对；若反思正在进行（LLM 调用数秒），本轮 append 后因锁被占直接 return，而 `_reflect()` 成功后无条件 `clear()` —— 把反思期间新到的对话对（未进入已渲染的 prompt）一并清掉，永久丢失。失败路径不清 buffer 的反差证明这是成功路径上的遗漏。

### M4. Gate 拒绝路径未恢复 THINKING，FSM 卡 EXECUTING 后触发非法转换崩溃 【置信度 75】

**位置**：`core/cognitive/loop.py:517-518`（配合 `core/status/fsm.py:166-196`、`core/schema/agent.py:27`）

`_execute_tool_round` 中 `enter_executing()` 之后 gate 拒绝直接 `return`，跳过 `return_to_thinking()`（loop.py:631）；`_think_loop` 继续下一轮，首个 SENTENCE 触发 `enter_speaking()`（EXECUTING→SPEAKING 非法）或收尾 `transition(IDLE)`（非法）均抛 ValueError，最终向用户广播虚假的 "❌ LLM 调用失败: Invalid transition..."。触发前提：LLM 幻觉出未知工具名（低频但真实）。单测只测 `_gate_tool_calls` 本身，未覆盖状态机联动。

### M5. text-only 模式下同一句文本双路径重复广播，输出翻倍 【置信度 100】

**位置**：`core/cognitive/loop.py:928-951`（配合 `core/cognitive/stream_processor.py:134-137`）

StreamProcessor 对同一 chunk 同时产出 TEXT_DELTA 与 SENTENCE（句子为已发 chunk 的累积）；`_dispatch_stream_event` 在 `_tts_mgr.active` 为 False 时两个分支都向同一 TEXT_DELTA 通道广播相同文本。text-only 模式（`config.tts.enabled=false` 或 TTS 连通性测试失败降级，`core/agent/_lifecycle_services.py:67-82`）下每个完整句子被展示两次，前端 appendDelta 纯追加无去重。

### M6. `chat_stream` 在 usage 终结 chunk（空 choices）上抛 IndexError 【置信度 100】

**位置**：`Module/llm/openai_compat.py:199-209`

流式分支请求 `include_usage=True`（line 190），而 OpenAI 规范的 usage 收尾 chunk `choices` 恒为空数组——line 200 `delta = chunk.choices[0].delta` 先于 usage 判断执行，必然 IndexError。所有 provider（deepseek/openai/ollama/mimo/custom）均经此路径（registry.py 全部映射到 OpenAICompatBackend），`LLMAdapter.think_stream` 无兜底：**每次流式回复结束时崩溃**，usage 统计在流式模式下永远不可达。同文件 `_stream_structured` 有 `if not chunk.choices: continue` 守卫（line 140-141），测试注释也明确"usage chunk 的 choices 为空（真实 API 行为）"（test_llm.py:527），唯独 `_call_api` 漏掉。

### M7. `_format_tool_summary` 对非 dict 的合法 JSON arguments 抛 AttributeError 【置信度 75，已实测复现】

**位置**：`core/adapter/message_builder.py:116-121`

`json.loads(tc.function.arguments)` 只捕获 JSONDecodeError/TypeError，但 `[1, 2]`、`"hello"`、`42` 都是合法 JSON——`args.items()` 抛 AttributeError 未被捕获。该函数在每轮 `build_messages()` 回放历史时被调用，异常穿透到 `process()` 以 "LLM 调用失败" 中止整轮；且非 dict arguments 会经流式路径（`normalize_tool_calls` 对 str 型原样保留）持久化进 session，**下一轮必然再次触发**，持续毒化会话直至清空。验证还发现同轮 `tool_executing` emit 循环（`args.items()`）同样未捕获，比原描述更糟。

### M8. MiniMax 停顿标签 `<#x#>` 从 TTS 文本中被剥离，F3 停顿功能端到端失效 【置信度 90】

**位置**：`core/adapter/tts_adapter.py:258-268`（docstring 52-53 行）

模块 docstring 声明停顿标签"送到 TTS 但不显示在前端"，但 `_download_and_forward` 在送往 `synthesize()` 的文本上执行 `_PAUSE_TAG_RE.sub("", ...)`；StreamProcessor 在分句前又剥一次（提交 ec02ec34 因 `<#0.5#>` 中的 `.` 被误判句末而加的 workaround，却把标签直接删除）。五处证据（docstring、`strip_for_minimax` 显式保留 `<#x#>`、F3 模板仍在指导 LLM 输出、`Module/tts/README.md:106` 设计表、minimax API 文档）均声明标签应送 TTS，端到端无任何恢复路径。修复需额外设计流式分句下的标签保留（不能只删 267 行）。

### M9. gpt-sovits `_pull` 任务异常被静默吞掉，调用方收到截断/空音频 【置信度 85】

**位置**：`Module/tts/gpt_sovits.py:162-205`

`resp.raise_for_status()` 抛的 `HTTPStatusError` 不匹配 `except httpx.RequestError`（实测 MRO 确认，二者均继承 HTTPError 但互不继承），异常从 task 冒出但 `finally` 无条件入队 `None` 哨兵；消费者收到哨兵正常 `return`，`await task` 复抛的异常被 `except Exception` 以 debug 级吞掉。TTS 服务端报错/断连时 `synthesize()` 返回 `b""`，`tts_adapter.py:273` 不检查空音频照常转发——前端静默无声（HTTP 错误路径尚有 warning 日志，断连路径仅 debug）。核心机制（调用方无异常、得到空音频）完全属实。

### M10. `/debug/api` 精确路径绕过认证中间件 【置信度 90】

**位置**：`core/input/debug_routes/__init__.py:31`（配合 `overview.py:14`、`frontend/src/stores/debugStore.ts:22`）

中间件 `request.path.startswith("/debug/api/")` 对精确路径 `/debug/api` 恒为 False——而前端 debugStore 每 2 秒轮询的正是此路径。配置 `SERVER_AUTH_TOKEN` 后，其他全部 `/debug/api/*` 端点需 Bearer 认证，唯独返回聚合对话敏感信息（会话最近消息、prompt/response 摘要、L2 自我认知）的概览端点完全开放，且服务器默认绑定 0.0.0.0:8765，绕过立即可被利用（唯一缓解：auth_token 默认关闭、需显式启用）。

### M11. `time_of_day` 主动触发去重键只有 "HH:MM"，跨天被永久抑制 【置信度 75】

**位置**：`core/monitoring/idle_monitor.py:120-127`

`_last_time_of_day_trigger` 无日期成分（line 120 仅 `%H:%M`），触发后次日同刻恒 return——每日定时主动搭话在进程生命周期内仅触发一次；`stop()`/`start()` 也不重置该属性。与注释声明的"同一分钟只触发一次"意图不符，修复需在键中加入日期。消费端 `_handle_system_tick` 目前以 TODO 静默丢弃 time_of_day 事件（`agent.py:479-480`），暂为潜在缺陷。

### M12. STT 语音输入不调用 `idle.touch()`，纯语音对话触发 silence_timeout 误搭话 【置信度 75】

**位置**：`core/input/ws_handler.py:271-279`（配合 `agent.py:292`、`idle_monitor.py`）

语音路径直投 `UserInputData` 到 event bus，绕过 `process_message`（全库唯一 `touch()` 调用点）。`_last_interaction` 无其他更新途径，启用 proactive 后纯语音对话超 interval（默认 300s）必在对话间隙误触发"用户已经 N 秒没有说话了"的主动发言（对正在语音对话的用户还是事实错误）。默认角色配置 `proactive.enabled: false` 故开箱不触发。

### M13. 重连时 HITL 审批重广播缺 `type` 字段且参数名不匹配，前端永远收不到 【置信度 75】

**位置**：`core/input/ws_handler.py:73-89`（对比 `Module/output/websocket.py:108`、`Module/tool/manager.py:327-330`、`frontend/src/composables/useWebSocket.ts:31,97-110`）

注释声称"前端重连后恢复审批弹窗"，但重连广播 `{"command": "tool_approval", ...}` 缺 `type` 字段（前端按 `m.type` 分发，undefined 落入 default 被丢弃）；即便有 type，参数名 `tool_name` vs `tool`、`params` 对象 vs 字符串（正常路径为 `str(params)[:200]`）也全部不匹配，且违反 `protocol.ts` 声明的 `CommandMessage` 契约。正常路径与前端期望三方一致，唯独重连路径三个维度全部偏离。断线重连时挂起审批永远悬空。

### M14. 历史消息加载游标 `_id` 不存在且取错方向，分页产生大量重复气泡 【置信度 100】

**位置**：`frontend/src/stores/chatStore.ts:243, 258-292`（配合 `core/schema/message.py:82-100`、`core/input/api_routes.py:292`）

session 消息（`messages_for_client`）无 `_id` 字段（只有 `message_id`），游标恒为 null → 首次 `loadMoreMessages` 不带 `before` 返回归档最新 50 条，与可见 session 窗口几乎完全重叠并整体（反向顺序）prepend；随后游标取 `serverMsgs[0]`（降序页**最新**一条），正确应取页尾最旧一条 → 每页与上一页重叠 49/50 条。前端无按 message_id 去重，且 `convertServerMessage` 每次生成新客户端 id，重复气泡必然全部渲染。**上滑加载历史是常态路径，历史分页实质不可用。**

### M15. 排队气泡在音频流中断时永久隐藏（queued 标记无兜底清除） 【置信度 85】

**位置**：`frontend/src/stores/chatStore.ts:51-58, 225-234`（配合 `audioStore.ts:64-96`）

`queued` 标记唯一清除路径是 `revealNextQueued()`，仅当 `playNext` 成功取到播放项时调用。最致命的路径：**播放中用户继续发消息**（`addUserMessage` 只重置 `nextBubbleQueue` 数组不清标记）——被移出队列的气泡标记永远无法清除，从 `visibleMessages`（MessageList 直接渲染的列表）永久消失，完整回复内容在会话内不可见。历史提交 2434b92 只修了"队列排空"分支，未覆盖"音频从未到达/被移出队列"分支。

### M16. 唤醒词开关是死 UI — 从不发送 `wake_toggle`，对后端无任何效果 【置信度 90】

**位置**：`frontend/src/stores/settingsStore.ts:23, 51-53`（配合 `core/input/ws_handler.py:172-176`）

后端有专门的 `wake_toggle` 处理（`audio_buf.set_wake_enabled`，运行时在 WakeState.AWAITING 与透传间切换，`core/input/README.md` 亦有文档），前端全仓库无任何代码发送该消息；开关只写 localStorage 并门控 'wake' 事件闪光提示。实际唤醒词启用状态由启动配置 `wake_word_enabled` 决定（`core/agent/server.py:166`），用户切换后行为不变。与 `switchModel`（会发 WS 消息）形成鲜明对比，证明是接线缺失。

### M17. `python -m Module.system_prompt` 入口必崩 【置信度 100，已实测复现】

**位置**：`Module/system_prompt/__main__.py:27`

`ConflictRulesSection()` 无参实例化，但其继承的 `Jinja2Section.__init__(template_path: str)` 参数必填。实测运行立即抛 `TypeError: Jinja2Section.__init__() missing 1 required positional argument: 'template_path'`。生产路径 `core/adapter/prompt_assembler.py:73` 正确传入了 `f1_conflict_rules.j2` 路径（模板存在），修复只需照抄。架构规定的 Integration smoke test 入口 100% 失效。

### M18. `python -m Module.config` 入口必崩 【置信度 90，已实测复现】

**位置**：`Module/config/__main__.py:23`（配合 `loader.py:215,240`）

`ConfigLoader(dict, ...)` 传入普通类型而非 dataclass，loader 管线 `_merge_yaml`/`_override_from_env` 无条件调用 `dataclasses.fields()` 抛 TypeError。实测运行输出 `加载失败: must be called with a dataclass type or instance` 并 exit 1。其他所有调用方均传真实 dataclass（`app.py:88`、`bench.py:131`、`server.py:240` 均传 `AppConfig`），仅 `__main__.py` 传错；loader 对非 dataclass 也缺少明确校验（报错晦涩）。

### M19. resume() 恢复后再次 pause 的任务被销毁而非保持 PAUSED 【置信度 75】

**位置**：`Module/task_system/task_system.py:384-393`（对比 248-254）

`_run_resume` 的 finally 无条件执行 `_cleanup_task` + `_cleanup_storage`（删 checkpoint）+ `semaphore.release()`，缺少 `_run_and_cleanup` 的 PAUSED early-return 保护。pause → resume → 再次 pause：handle_paused 刚保存的 checkpoint 随即被删（`resume_from_pause` 因 v1 CheckpointManager 按引用存 Pydantic 对象得以清除 pause_requested，但第二次 pause 在合并状态上重新设置），任务从 `_tasks`/`_mailboxes` 注册表移除；之后任何 `resume()` 返回 CHECKPOINT_NOT_FOUND，mailbox 消费者（`mailbox.get()` 无超时）永久阻塞。测试仅覆盖单次 pause→resume→COMPLETED，未覆盖此序列。

### M20. cancel() 在规划/校验阶段不推送任何终态事件，任务以 SUCCESS 收尾 【置信度 75，实际影响比描述更广】

**位置**：`Module/task_system/lifecycle.py:157-167`（配合 `orchestrator.py:143,182,186-189`、`planning.py:143-145`）

`_exec_state` 为 None（Planner/PlanValidator 阶段，`planner.plan()` 为真实 LLM 调用、耗时数秒）时 cancel() 只置 CANCELLED 不推事件；orchestrator 因 `is_terminal()` 跳过循环、status != RUNNING 跳过 finalize，返回 `OrchestratorResult(status=SUCCESS)`，"成功完成"日志、无任何终态事件。验证发现更严重的事实：`planning.plan_and_validate` 会**无条件把 status 覆写回 RUNNING**——最常见窗口（规划 LLM 调用中）的取消被彻底静默吞掉、任务照常执行完毕。注意 `TaskSystem.cancel()` 目前尚无调用方接线（API 已暴露未接入），缺陷暂为潜在。

---

## Minor

### m1. shutdown 时 flush_after_response 与 cancel 认知任务的竞态窗口 【置信度 75】

**位置**：`core/agent/lifecycle.py:50-66`（配合 `loop.py:713-716, 862-882`）

`_SHUTDOWN_STEPS` 中 flush（[0]，含 3 个 5s flush）先于取消认知任务（[5]），中间步骤全部 await 让出事件循环；turn 尾部 `create_task(_after_response)` 无任何 shutdown 状态门禁（loop.py 全文 grep 不到 RunState 检查）。stop() 恰逢 turn 收尾时，orphan after_response task 不在 flush 快照内，与已关闭的 LLM/session repo 并发执行——最后轮记忆沉淀丢失、session 降级写 JSONL（JSONL fallback 缓解数据完全丢失）。

### m2. start() 任一步骤失败即 raise，留下 _state=RUNNING 的僵尸 Agent 【置信度 75】

**位置**：`core/agent/lifecycle.py:124-145`

先置 `_state = RUNNING` 再执行 `_STARTUP_STEPS`，失败仅 log+raise 无任何回滚；四个 create_task 点（`_start_idle_and_events`、`_start_db_health`、worker pool、`_start_mailbox_polling`）均无幂等保护，重试 start() 会生成重复的后台循环。主入口（`app.py:133` 裸 `asyncio.run`）不捕获异常，实际失败即进程崩溃，僵尸场景仅在调用方捕获/重试（程序化 API 用法）时显现——但缺回滚与幂等保护本身是确凿缺陷。

### m3. `fire-and-forget consume_trigger` 无异常处理，压缩路径 MongoDB 写入失败丢失 C6 刷新 【置信度 75】

**位置**：`core/personality/reflector.py:325-327` + `core/personality/compression.py:243-250`

裸 `asyncio.create_task` 无 done callback；`_compress` 的 `append_version`（未保护的 MongoDB 真实 I/O，`character_version_repo.py:109/127`）抛异常时 `self_l2`/`compression_last_count` 已更新（不重试）、版本历史未写、C6 刷新被跳过，异常以 "Task exception was never retrieved" 形式未处理。同文件 `flush()` 有完整 try/except，唯独此路径没有。engine.py `buffer_message`（349 行）同样裸 create_task。

### m4. KMeans 聚类在事件循环内同步执行，大 LTM 下阻塞整个 agent 【置信度 75】

**位置**：`core/memory/memory_reflector.py:200-221`

`KMeans(n_clusters=..., n_init=10).fit_predict(X)` 纯同步调用（sklearn 1.6.1 单线程 CPU 密集），无 `run_in_executor`（`core/memory/` 中唯一 executor 用法在 embedding_provider）。触发条件（轮次≥50 或新记忆≥30、条目>20 且 100% 有 embedding）在 LTM 增长后是常态；数百条即数秒、近万条（max_entries=10000）达分钟级，期间消息处理/TTS/WS 全部冻结。

### m5. `update_text` 在 embedding 不可用时只更新内存文本，陈旧向量永久残留 【置信度 75】

**位置**：`core/memory/long_term.py:344-373`

`emb` 为空（后端超时/不可用，`embedding_provider.py:82-92` 文档化的正常返回路径）时整个 `if emb:` 块被跳过，无 `_dirty_ids` 标记（对比 371-373 行 except 分支，不对称确凿）；`search()` 按 PK 匹配，旧向量距离与新文本绑定参与 rerank，重启后 `_load_from_milvus` 还会用旧文本静默回滚更新。`_sync_dirty_entries` 无调用者故无法补救。

### m6. 图片上传先整读 body 再校验大小，multipart 绕过 client_max_size 【置信度 80】

**位置**：`core/input/api_routes.py:217-218`（配合 `server.py:56`）

`request.multipart()` 原始流读取不受 aiohttp `client_max_size` 限制（该限制只作用于 `request.read()/post()`），10MB 上限在整块 `read()` 之后才检查。该端点无认证（`_check_auth` 仅覆盖 shutdown/restart/tools/debug 路由）且服务绑定 0.0.0.0:8765，任意局域网客户端可发超大 body 造成内存尖峰/OOM。真实 DoS 向量。

### m7. archive 列表 `role` 参数非法值未捕获 ValueError → 500 【置信度 75】

**位置**：`core/input/debug_routes/archive.py:49`

`MessageRole(role_str)`（StrEnum）对非法值抛 ValueError，位于 try 块之外（limit/skip 都有兜底，51-61 行 query_offset 亦有）。`GET /debug/api/archive?role=xxx` 返回 500 而非 400，并留下 traceback。

### m8. 版本历史路由 `int()` 转换无保护 → 非法输入 500 【置信度 75】

**位置**：`core/input/debug_routes/character.py:107-109, 156-158`

`?limit=abc`/`?before=xyz` 抛未捕获 ValueError → 500；同包 ltm.py 与本文件 rollback 处理器（127-131、176-181 行）均有 try/except → 400 惯例，仅此两处 GET handler 缺失，属输入校验不一致。

### m9. `deafen(2.0)` 只在 SPEAKING 上升沿触发，长回复期间回声可自我打断 【置信度 75】

**位置**：`core/input/ws_handler.py:113-126`（配合 `audio_buffer.py:150-159`、`core/status/fsm.py:47-48`）

SPEAKING 横跨整个多句 TTS 播放（`finish_turn` 才 `wait_all()`，5-30s+），deafen 2 秒后麦克风恢复拾音；外放时 TTS 回声经 VAD（aggressiveness=3，3 个连续语音块即可）触发 `do_interrupt`，且 SPEAKING 刻意不在 `should_suppress_vad_interrupt` 抑制集合（测试明确断言"SPEAKING 允许打断"）→ 剩余回复被 `cancel_all()` 截断。注释"echo prevention"证明这正是该机制要防的场景。

### m10. WS 接收循环只捕获 JSONDecodeError/ValidationError，其他异常直接断开连接 【置信度 75，示例已修正】

**位置**：`core/input/ws_handler.py:130-141`

原发现举的 `parts_from_dicts` 例子有误（该路径经 pydantic `TypeAdapter.validate_python`，抛 ValidationError 会被 line 138 捕获）；真实触发路径：客户端发送合法 JSON 但非 dict（如 `[1,2]`）→ `data.get("type")`（line 171）抛 AttributeError，两个 except 都捕不到 → 连接无 error 帧直接死亡（1006），无诊断信息。健壮性缺口，正常前端流量不会触发。

### m11. WS 无 heartbeat/接收超时，半开连接产生僵尸 channel + monitor task 【置信度 75】

**位置**：`core/input/ws_handler.py:43`（配合 `output/websocket.py:54`、`manager.py:85-88`）

`WebSocketResponse` 未设 heartbeat/receive_timeout（aiohttp 默认不发 ping）。客户端休眠/断 Wi-Fi 不发 FIN 时 `async for msg in ws` 永久阻塞，finally 不执行：channel 残留在 `_output._channels`（全库唯一 `remove_channel` 在 finally），`_send_json` 每次广播（每个 token）打一条 warning，且 `asyncio.gather` 广播被僵尸 channel 拖慢健康 channel；STT monitor_task 每 100ms 空转，连接数无限累积。

### m12. `flush_emojis()` 在最后一句 TTS 音频下载完成前广播 emoji，破坏依附顺序 【置信度 75】

**位置**：`core/adapter/tts_adapter.py:181-190`（配合 `loop.py:654`、`fsm.py:172`）

`_finalize_turn` 在 `wait_all()` 之前 flush 残留 `_pending_emojis`（无 stream_id）并清空 dict，最后一句 TTS 的 `pop(sid)`（下载完成后才执行）尚未运行 → 前端先显示 emoji 后显示句子文字（"😊句子"而非文档声明的"句子😊"，tts_adapter.py:8-9 设计契约）。时序竞争在流结束时几乎必然（最后一句下载仍在进行），触发需回复含独立 emoji 句。展示顺序缺陷，非崩溃。

### m13. OpenAI embedding 客户端未配置超时，上游挂起可阻塞 600s 【置信度 75】

**位置**：`Module/embedding/openai_compat.py:42`

`AsyncOpenAI` 未传 timeout（SDK 2.9.0 默认 600s 总超时），与 llm 后端显式 `httpx.Timeout(60.0, connect=10.0)`（`Module/llm/openai_compat.py:261`）形成对照；`embedding_provider` 的 10s 超时只覆盖模型加载（且加载失败后后端永久 None 不重试），`embed_query` 本身无 wait_for 兜底，`long_term.py` 的 insert/recall 路径（每轮 C4 记忆检索）会被拖住长达 10 分钟。

### m14. `get_caller_logger` 双跳 `f_back.f_back` 取错调用方模块 【置信度 75】

**位置**：`Module/logger/factory.py:54`

全部 3 个调用点（`output/websocket.py:30`、`output/manager.py:30`、`logger/__main__.py:12`）都是 `__init__`/`main()` 内直接调用，正确层级应为 `f_back` 一层（docstring 所称的 Factory.create() 中间层不存在，已 grep 确认）。后果：默认路径下（未传 logger 参数）logger 名归属为 `core.input.ws_handler`/`core.agent.agent` 而非 `Module.output.*`，`%(name)s` 日志来源错乱；module_levels 前缀匹配失效（当前 app 走 log_setup，LoggerFactory 未被实例化，影响暂有限）。

### m15. MilvusBackend.close() 为空实现，连接/线程池随重启泄漏 【置信度 75】

**位置**：`Module/database/vectordb/milvus.py:192-193`

`pass` + 注释声称"MilvusClient has no explicit close"事实错误（已装 pymilvus 2.5.14 实测 `MilvusClient.close()` 存在，源码为 `connections.disconnect(self._using)`，requirements 要求 ≥2.4）。`MemoryRepository.close()`（memory_repo.py:57-61）调用链无实际效果；`/restart`（server.py:229-243）在同一 Agent 实例上 stop→start，每次 start 经 `connect()` 无条件新建 MilvusBackend/MilvusClient，旧 gRPC 通道/线程/pymilvus 全局 connections 注册表条目随重启累积。

### m16. DelegationPreparer 自建 AsyncOpenAI 客户端从不关闭 【置信度 75】

**位置**：`Module/task_system/delegation_preparer.py:118-128`（配合 `core/agent/_lifecycle_task_system.py:85,110-116`）

全文无 close/aclose，类无关闭接口；每次生命周期启动创建、`_stop_task_system` 只停 TaskSystem 不关 preparer，`/restart` 进程内重启路径每次泄漏一个 httpx 连接池（首次委托调用时惰性建池，httpx 在 GC 时仅能警告 "Unclosed client session"）。

### m17. Module/system_prompt eager import core.adapter，破坏 Module 层分层 【置信度 75，部分细节修正】

**位置**：`Module/system_prompt/__init__.py:9`

顶层 `from core.adapter.message_builder import MessageBuilder as _MessageBuilder` 使任何 `import Module.system_prompt` 连带拉入整个 core.adapter 包及其传递依赖（Module.llm/tts/output/tool），违反 CLAUDE.md"Module/ — backend-agnostic library"分层（该层唯一被认可的共享依赖是 core.schema），且违背文件自身 lazy 过渡 re-export 的设计意图。**修正**：验证发现 `__getattr__` 并非死代码（别名绑定为 `_MessageBuilder`，访问 `MessageBuilder` 仍会触发 DeprecationWarning）；"全仓库唯一反向导入"也不成立（`delegation_preparer.py:20-21`、`tool/protocol.py:15` 顶层导入 core.schema，属共享 schema 层，性质较轻）。当前无实际循环导入，影响为分层合规 + 导入开销 + 潜在循环风险。

### m18. token_usage 会话统计字段名不匹配（by_model vs byModel） 【置信度 75】

**位置**：`frontend/src/stores/chatStore.ts:197-199` + `CharStatus.vue:17`

后端统一发 `by_model`（token_tracker.py:110/147/150，grep 确认无 camelCase 转换），前端按 `byModel` 读取恒为 undefined，按模型拆分统计永不渲染（总计正常）。另 `restoreFromServer` 的 `_tokenStats` 形参完全未用（useWebSocket.ts:52 有传），页面重载后 session token 显示归零/陈旧直到下一次 token_usage 广播。

### m19. 恢复/加载的消息时间戳恒为当前时间，归档 tool 卡片名称与内容错乱 【置信度 100】

**位置**：`frontend/src/stores/chatStore.ts:332-411`（配合 `core/schema/message.py:24-76`、`api_routes.py:28-44`）

前端 6 处 `Date.parse(m.created_at)` 读取的字段在两个数据源（WS session 恢复与 /api/messages 归档）中均不存在（只有 `timestamp`）→ NaN → `Date.now()`，每次页面加载/恢复都触发，时间分隔符失效、TimeDivider 显示当前时钟。归档 tool 消息经 `model_dump` 保留 `tool_name`（前端读 `m.name || 'tool'`）与数组 `content`（ToolCard 数组化渲染）——该子问题仅影响归档加载路径（session 恢复路径已正确平铺 `name`/字符串 content），时间戳 bug 两条路径均受影响。

### m20. SSE 重连"指数退避"失效，服务器宕机时固定每秒重连 【置信度 75】

**位置**：`frontend/src/debug/components/TasksTab.vue:17-69`

`connect()` 入口无条件 `reconnectDelay = 1000`（:22）把 onerror 翻倍的值立刻清零，退避是死代码，间隔恒为 1000ms（EventSource 原生重连被 `es.close()` 禁用，手动定时器是唯一机制，30s 上限永远不可达）。与 CLAUDE.md 宣称的指数退避不符；单客户端 1 req/s 压力有限，恢复后 1s 内即可重连。

### m21. 模型切换乐观插入 + 结果回包双插状态消息 【置信度 75】

**位置**：`frontend/src/components/panels/SettingsPanel.vue:80-83` + `useWebSocket.ts:113-120`

后端对每次 `switch_model` 必定回发 `switch_model_result`（ws_handler.py:189-214，含失败路径），前端先乐观插入"已切换至 Y"、`handleSwitchResult` 再按结果插入一条。成功时逐字重复；失败时先显示虚假的"已切换至 Y"再显示"切换失败"，自相矛盾。切换后无页面重载，每次切换触发。

### m22. 时间分隔符索引错位（visibleMessages vs messages） 【置信度 75】

**位置**：`frontend/src/components/chat/MessageList.vue:22-27`

遍历 `visibleMessages`（:118）却用下标索引 `messages`（:24），queued 气泡隐藏期间（chatStore.ts:51-52 置 queued、:294-298 过滤）两数组长度不同：首条被隐藏时 `index===0` 直接 return false 导致分隔符漏显（即使间隔 >5 分钟），后续消息与错误的前一条比较导致错位/误显。触发需 TTS 排队气泡与 5 分钟间隔叠加，UI 级缺陷。

### m23. 审批弹窗打开时 Escape 同时触发"拒绝审批"和"打断生成" 【置信度 100】

**位置**：`frontend/src/App.vue:23-28` + `components/ui/ApprovalDialog.vue:39-46`

两个 document 级 keydown 监听（App.vue 无条件发 `interrupt`、ApprovalDialog Escape → `deny()`）都不阻止传播，同一次 Escape 必然同时触发：`interrupt`（agent.py:437 → `cancel_current()` → 回合中止、TTS 取消）与 `deny()`。审批弹窗出现时回合必在进行中，**每次用 Escape 拒绝工具审批都会连带杀掉整个回合**。（验证修正一处措辞：Vue 3 子组件先挂载，ApprovalDialog 监听注册更早，但顺序不影响结论。）

---

## 已过滤的发现（3 条）

| 发现 | 分数 | 判定 | 说明 |
|------|------|------|------|
| 重试上限 off-by-one（`Module/task_system/core/execution.py:313-334`） | 25 | **误报** | 行为是刻意设计：测试注释（`test_orchestrator.py:244` "第 4 次尝试（初始 + 3 次重试）"）与 PROGRESS.md 历史修复记录（"max_retries=3 只重试 2 次"被修复）确认"初始 + max_retries 次重试"是标准语义。真正问题仅是 `execution.py:319` 的过时注释（声称"允许 attempt 1,2,3 共 3 次"），文档瑕疵而非功能 bug |
| WS 吞 CancelledError 后 finally 中 30s STT flush（`core/input/ws_handler.py:149-155`） | 50 | 置信度不足 | 结构属实，但影响被缓解：`server.py:232` 的 `runner.cleanup()` 已有 3s 外层超时封顶；且吞 CancelledError 并非阻塞原因（finally 的 await 无论是否重抛都会执行）。残留的健壮性问题（finally 中最长 30s 阻塞式 STT await）触发需 STT 开启 + 断连时正在说话 + 通过唤醒词 gate，概率中等 |
| 音色 YAML 解析无 None/类型防护（`Module/tts/minimax_tts.py:35-38` 等） | 50 | 置信度不足 | 代码属实（`yaml.safe_load` 空文件返回 None 无防护，`gpt_sovits.py:56`、`registry.py:27` 同模式），但仅自定义空/损坏配置触发，默认内置 YAML 正常，属防御性编程缺口而非高频故障 |

---

## 修复优先级建议

1. **立即（Critical，一行修复）**：C1 — `_poll_mailboxes` 自旋（`continue` → `break`）+ 补 `_poll_mailboxes` 单元测试；顺带清理 restart 后的 `_background_tasks`。
2. **高优先（Major，日常路径必触发）**：M5 重复广播（text-only 输出翻倍）、M6 流式收尾 IndexError（每次流式回复崩溃）、M14 历史分页重复气泡、M17/M18 两个 smoke test 入口、M15 queued 气泡隐藏、M23 Escape 双重触发。
3. **中优先（Major，错误路径/特定场景）**：M1 截断方向、M2 脏同步死代码、M3/M4 反射器竞态与状态机、M7 tool summary 毒化、M8 停顿标签、M9 TTS 静默失败、M10 认证绕过、M13 审批重连协议、M16 唤醒词死 UI、M19/M20 TaskSystem 状态机（cancel 尚未接线，可随任务系统接入时修复）。
4. **低优先（Minor）**：按 m1-m23 逐项，多数为健壮性/防御性修复，可结合 `docs/todo.md` 排期。
5. **回归测试**：C1/M6/M14/M19/M23 应补测试后再合并；修复完成后对受影响子系统跑 `make test-quick`。

## 方法论说明

- 审查 agent（7 个并行，Sonnet 级）：每个负责一个子系统分片，检查逻辑 bug、async/并发问题、错误处理、CLAUDE.md 合规，要求只报高置信度真实缺陷。
- 验证 agent（47 个并行，独立打分）：按 rubric 0-100 打分（0=误报/已存在，25=疑似，50=中等/nitpick，75=高置信会触发，100=确定频繁触发），保留 ≥75 分。
- 过滤结果：1 条误报（retry 语义，被项目自身测试与历史记录否决）+ 2 条置信度不足（50 分），详见上节。
- 行号基于审查基线 commit `e782de9`，验证 agent 已逐条复核；个别发现的行号/示例以验证修正为准（如 F37 为 `__init__.py:9`、m10 触发示例已修正）。
