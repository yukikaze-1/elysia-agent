# Phase 3 测试计划

现有 127 个测试全部通过，但多个核心组件缺少直接测试。本文档列出需要补充的测试，按优先级排列。

---

## P0: 核心引擎 — `core/cognitive_loop.py`

当前零直接测试，只通过 DemoAgent 集成 test 间接覆盖。

### _merge_stream_tool_calls (staticmethod)

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | 空列表 + 单个 delta | `accum=[], delta=[{"index":0,"id":"call_1","function":{"name":"search_memory","arguments":"{\"q\""}}]` | accum 新增 1 个 entry，id=call_1，func.name=search_memory |
| 2 | 同一 index 多个 delta | 连续两次 delta，同一个 index 0，分别推送 name 和 arguments | name 和 arguments 正确拼接（字符串累加，不是覆盖） |
| 3 | delta 只有 id（无 function） | `delta=[{"index":0,"id":"call_x"}]` | accum entry 有 id，function 保持初始空值 |
| 4 | 多 index 交替 | delta 分别推送 index 0 和 index 1 | accum 有两个 entry，各自独立 |
| 5 | 空 delta 列表 | `delta=[]` | accum 不变 |

### _normalize_tool_calls (staticmethod)

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 6 | 全部完整 | 2 个完整 tool calls（都有 id + name） | 返回 2 个 |
| 7 | 全部不完整 | id 为空或 name 为空 | 返回空列表 |
| 8 | 混合 | 1 个完整 + 1 个不完整 | 只返回完整的那个 |

### process()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 9 | 拒绝无效 FSM 转换 | FSM 在 SPEAKING 状态 | 返回 None，不抛异常，log warning |
| 10 | 正常流程 | FSM 在 IDLE，传入消息 | FSM 转换到 THINKING，STATE_CHANGE 事件被 post |
| 11 | CancelledError 传播 | 在 _think_loop 中 raise CancelledError | _handle_interrupt 被调用 |

### _handle_interrupt()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 12 | 完整清理 | FSM 在 THINKING | sentence buffer reset → typing off → INTERRUPTED → 0.05s → IDLE |
| 13 | STATE_CHANGE 事件 | 同上 | INTERRUPTED 和 IDLE 两个事件被 post |

### _synthesize_sentence()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 14 | TTS=None | self._tts 为 None | 直接返回，不调用任何方法 |
| 15 | TTS 成功 | Mock TTS 返回 bytes | audio_chunk 被调用一次，参数是返回的 bytes |
| 16 | TTS 失败 | Mock TTS raise Exception | 不抛异常，只 log debug |
| 17 | 角色名传递 | char name = "TestBot" | TTS.synthesize 收到的 character="TestBot" |

### _after_response()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 18 | Consolidator=None | 不设 consolidator | 直接返回，不抛异常 |
| 19 | Consolidator 成功 | Mock consolidator 返回 2 items | consolidation 被调，session.save 被调 |
| 20 | Consolidator 异常 | Mock raise Exception | session.save 仍然被调，不抛异常 |

---

## P0: WebSocket 服务 — `Module/input/server.py`

当前零测试。

### create_app()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 21 | 基本创建 | 传入 agent (mock) | 返回 aiohttp Application 实例 |
| 22 | /ws 路由 | 检查 app 路由 | GET /ws 已注册 |
| 23 | serve_static=True + 目录存在 | static/ 目录有 index.html | GET / 返回 200，Content-Type text/html |
| 24 | serve_static=True + 目录不存在 | static/ 目录缺失 | 不抛异常，只有 /ws 路由 |
| 25 | serve_static=False | | 只有 /ws 路由，无静态路由 |

### _handle_json()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 26 | text 消息 | `{"type":"text","content":"你好"}` | agent.process_message("你好") 被调用 |
| 27 | 空内容 | `{"type":"text","content":"  "}` | process_message 不被调用 |
| 28 | interrupt | `{"type":"interrupt"}` | INTERRUPT 事件被 post 到 EventBus |
| 29 | 未知 type | `{"type":"unknown"}` | 不抛异常，peacefully ignored |
| 30 | 无效 JSON | 发送 "not json" | 返回 error JSON `{"type":"error",...}` |
| 31 | 缺少 type 字段 | `{"content":"hi"}` | 不抛异常 |

### _do_interrupt() / _do_transcription()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 32 | interrupt 事件 | 调用 _do_interrupt(agent) | EventBus post INTERRUPT, priority=0, source="websocket" |
| 33 | transcription 事件 | 调用 _do_transcription(agent, "你好世界") | EventBus post USER_INPUT, data={"text":"你好世界","user_name":"妖梦"} |

### WebSocket 生命周期 (需要 aiohttp test client)

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 34 | 连接注册 | 客户端连接 /ws | agent._output 中新增 1 个 WebSocketChannel |
| 35 | 断开清理 | 客户端断开 | channel 从 _output 移除，AudioBuffer flush 被调 |
| 36 | 有 STT 时创建 AudioBuffer | stt_transcribe 不为 None | AudioBuffer 被创建，on_interrupt 和 on_transcription 回调已设置 |
| 37 | 无 STT 时跳过 AudioBuffer | stt_transcribe 为 None | AudioBuffer 不被创建，BINARY 消息被忽略 |
| 38 | 二进制消息 → feed | 发送 binary frame | AudioBuffer.feed 被调用 |
| 39 | CancelledError 处理 | task 被 cancel | 不抛异常，finally 清理正常 |

---

## P1: DemoAgent — `core/demo_agent.py`

### process_message()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 40 | 空消息 | `""` | 直接返回，idle touch 不被调用 |
| 41 | 纯空白 | `"   "` | 同上 |
| 42 | IDLE 状态下正常 | `"你好"`, FSM=IDLE | idle.touch() 被调，USER_INPUT 事件 post |
| 43 | 非 IDLE 状态下中断 | `"你好"`, FSM=SPEAKING | INTERRUPT 事件 post (priority=0)，sleep 0.1s，然后 USER_INPUT post |

### _process_events()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 44 | USER_INPUT → 创建 task | post USER_INPUT 事件 | asyncio.create_task 被调，_current_task 被赋值 |
| 45 | INTERRUPT → cancel task | _current_task 是一个 running task | task.cancel() 被调用，drain 被调用 |
| 46 | INTERRUPT → task 已完成 | _current_task.done() → True | cancel 不被调用 |
| 47 | INTERRUPT → task 为 None | _current_task → None | cancel 不被调用 |
| 48 | SYSTEM_TICK → 主动搭话 | reason="silence_timeout", elapsed=30 | cognitive.process 被调用，消息包含 30 秒提示 |
| 49 | STATE_CHANGE → 广播 | post STATE_CHANGE {"state":"thinking"} | 所有 channel 的 on_state("thinking") 被调用 |
| 50 | STATE_CHANGE → channel 异常 | channel.on_state raise Exception | 其他 channel 不受影响，不抛异常 |
| 51 | RuntimeError 退出 | _bus.next() raise RuntimeError | _running 循环退出，不抛异常 |

### start() / stop()

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 52 | start banner 显示 DB 状态 | MongoDB=ok, Milvus=ok | Banner 显示 "MongoDB + Milvus" |
| 53 | start banner 全部无 | DB 全部不可用 | Banner 显示 "in-memory" |
| 54 | stop 清理顺序 | agent running | idle_monitor.stop → event_task.cancel → session.save → long_term.close → llm.close → output.close |

---

## P1: LLM 适配器 — `core/llm_adapter.py`

### LLMAdapter

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 55 | 无 API key → mock | DEEPSEEK_API_KEY="" | _using_mock=True, backend 是 MockLLMBackend 实例 |
| 56 | 有 API key → real | DEEPSEEK_API_KEY 存在 | _using_mock=False |
| 57 | think() 返回纯文本 | MockLLM 返回普通回复 | ThoughtResult(text="...", tool_calls=[]) |
| 58 | think() 返回工具调用 | MockLLM 返回 JSON tool_call | ThoughtResult(tool_calls=[...]) |
| 59 | think() real→mock 降级 | real backend 创建成功但 chat() 失败 | 自动 switch_to_mock，返回 mock 结果 |
| 60 | stream_think() mock 路径 | _using_mock=True | 逐字符 yield |
| 61 | model 属性 | | 返回正确的 model 字符串 |
| 62 | close() 清理 | | backend.close() 被调用 |

### MockLLMBackend

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 63 | chat_stream 普通回复 | "你好" | 逐字符 yield，"今天过得怎么样" in 合并文本 |
| 64 | chat_stream 工具调用 | "回忆" + tools | yield JSON 字符串 |
| 65 | 无匹配关键词和触发器 | "xyz随机文本" | 返回 generic reply |

---

## P2: SentenceBuffer

### StreamingSentenceBuffer

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 66 | consume 正常流 | 逐个 yield "你好。今天不错。" | 分别 yield "你好。" 和 "今天不错。" |
| 67 | 无完整句 | "hello world" (无分隔符) | yield 不产生，flush 返回全部 |
| 68 | reset 后重用 | consume → reset → consume | 正确重置状态 |

### SentenceBuffer 补充

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 69 | 多个连续分隔符 | "你好。。。！" | 正确逐句分离 |
| 70 | 超长句 | 500 字无分隔符 | 不 split，flush 返回全文 |
| 71 | delim 作为内容的一部分 | "3.14 是圆周率" | "." 不触发分句（min_chunk_length 过滤） |
| 72 | flush 后空 buffer | 已 flush 全部内容，再 flush | 返回 None |

---

## P2: FSM + EventBus 补充

### AgentFSM

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 73 | can_transition(True) | fsm=IDLE, to=THINKING | True |
| 74 | can_transition(False) | fsm=IDLE, to=SPEAKING | False |
| 75 | interrupt_from_executing | fsm=EXECUTING | interrupt() 返回 True，状态变 INTERRUPTED |
| 76 | interrupt_from_speaking | fsm=SPEAKING | interrupt() 返回 True，状态变 INTERRUPTED |
| 77 | interrupt_from_interrupted | fsm=INTERRUPTED | interrupt() 返回 False，状态不变 |
| 78 | previous 属性 | 连续两次 transition | previous 记录第一次的状态 |
| 79 | SPEAKING→EXECUTING | fsm=SPEAKING, transition EXECUTING | 成功（工具调用回归） |
| 80 | SPEAKING→THINKING | fsm=SPEAKING, transition THINKING | 成功 |

### EventBus

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 81 | pending_count | post 3 个 event | pending_count==3, next 3 次后 ==0 |
| 82 | 同优先级按 seq 排序 | post 两个 NORMAL event | 先 post 的先返回 |
| 83 | drain 空队列 | 队列为空 | 返回空列表 |

---

## P3: AudioBuffer 补充 — `Module/input/audio_buffer.py`

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 84 | STT 超时 | Mock STT sleep 31s | TimeoutError 被 catch，on_transcription 不被调用 |
| 85 | STT 抛异常 | Mock STT raise RuntimeError | 异常被 catch，不传播 |
| 86 | STT 返回空/空白 | STT 返回 "" 或 "   " | on_transcription 不被调用 |
| 87 | flush 空 buffer | 从未 feed，直接 flush | 不抛异常，stt 不被调用 |
| 88 | _rms 恰好等于 threshold | RMS==0.02 | is_speech=False（严格 > 比较） |
| 89 | 段落长度恰好等于 MIN | len(audio) == 0.3 * sample_rate * 2 | 不忽略，触发 STT |
| 90 | 段落长度恰好小于 MIN | len(audio) < MIN | 忽略 |
| 91 | feed 无 silence 状态 | 持续 loud chunk | 不触发 _process_segment |

---

## P3: WebSocketChannel 补充 — `Module/output/websocket.py`

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 92 | AUDIO 在 capabilities | capabilities | ChannelCapability.AUDIO in caps |
| 93 | on_audio_chunk | bytes WAV | send_json {"type":"audio","data":"<base64>","format":"wav","sample_rate":32000} |
| 94 | on_state | "thinking" | send_json {"type":"state","state":"thinking"} |
| 95 | on_expression 默认 intensity | 不传 intensity | send_json 中 intensity=1.0 |

---

## P3: OutputManager 补充 — `Module/output/manager.py`

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 96 | audio_chunk 广播 | mgr.audio_chunk(data) | AUDIO-capable channel 的 on_audio_chunk 被调用 |
| 97 | audio_chunk 过滤 | 同上，自定义 capability=TEXT_DELTA | 只有 TEXT_DELTA channel 收到（无 channel 匹配） |
| 98 | command 广播 | mgr.command("reload") | COMMAND-capable channel 的 on_command 被调用 |
| 99 | stream 空迭代器 | deltas=empty async iterator | on_text_delta 不被调用，on_message 被调用一次（空文本） |

---

## P4: EmotionEngine 补充 — `core/emotion_engine.py`

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 100 | 多个 mood 同分 | "哈哈太棒了！" (happy 和 excited 各匹配) | 返回匹配关键词最多的 mood |
| 101 | _push_idle 空列表 | idle_expressions=[] | 不抛异常 |
| 102 | intensity 上限 | mood_intensity=0.96, 同 mood 再触发 | capped at 1.0 |
| 103 | intensity 下限 | mood_intensity=0.12, decay | min 0.1, mood 变 neutral |

---

## 手动测试

### 浏览器客户端 (static/index.html)

| # | 场景 | 步骤 | 预期 |
|---|------|------|------|
| M1 | 页面加载 | 浏览器打开 http://localhost:8765 | WS 连接成功，state 显示 idle |
| M2 | 文字对话 | 输入 "你好" → 回车 | user 消息在右边，assistant 流式出现，state 变化 |
| M3 | 情绪表情 | 输入 "哈哈太开心了！" | expression 区域显示 happy |
| M4 | 中断功能 | 输入 "写一首长诗" → 立即输入 "停" | 第一条被中断，第二条正常响应 |
| M5 | 多次对话 | 连续 5 轮对话 | 聊天记录完整，滚动正常，state 流转正确 |
| M6 | 特殊字符 | 输入含 emoji、日文、英文混排 | 正确显示，不崩溃 |
| M7 | WebSocket 重连 | 杀掉 server 再重启 | 前端显示 disconnected，刷新后恢复 |
| M8 | 麦克风权限拒绝 | 点麦克风 → 浏览器拒绝 | 不崩溃，mic 按钮不变色 |
| M9 | 麦克风正常 | 点麦克风 → 说话 | 按钮变色（active），音频二进制帧发送 |
| M10 | 中文输入法 | 使用输入法输入中文 | 回车提交正确，不发送拼音中间态 |

### 服务器 (python demo.py --server)

| # | 场景 | 步骤 | 预期 |
|---|------|------|------|
| M11 | 启动检查 | python demo.py --server | banner 正确显示 IP:port，无 traceback |
| M12 | Ctrl+C 退出 | 运行中 Ctrl+C | 优雅退出，显示 "再见！"，session 已保存 |
| M13 | 多客户端 | 2 个浏览器 tab 同时连接 | 各自独立 channel，不互相干扰 |
| M14 | 无可用端口 | --port 被占用 | 明确报错（Address already in use） |
| M15 | --verbose 模式 | python demo.py --server --verbose | log/ 文件正常生成，stderr 显示 DEBUG 日志 |
| M16 | --stt 模式 | SenseVoice 运行中 | AudioBuffer 创建，"STT: 已连接" 显示 |

### 性能

| # | 指标 | 测量方法 | 预期 |
|---|------|---------|------|
| M17 | 启动时间 | time python demo.py --once "test" | < 5 秒 |
| M18 | 首字延迟 (TTFT) | 输入 "你好" → 计时首个 token | < 3 秒 |
| M19 | 连续对话内存 | 50 轮对话后检查 RSS | 增长 < 50MB |
| M20 | WebSocket 消息延迟 | JS 计时 发送 → 收到 delta | < 500ms |

---

## 测试统计

| 优先级 | 数量 | 说明 |
|--------|------|------|
| P0 | 39 | cognitive_loop (20) + server (19) |
| P1 | 24 | demo_agent (15) + llm_adapter (9) |
| P2 | 18 | sentence_buffer (7) + fsm (8) + eventbus (3) |
| P3 | 20 | audiobuffer (8) + webchannel (4) + outmanager (4) + emotion (4) |
| P4 (手动) | 20 | 浏览器 (10) + 服务器 (6) + 性能 (4) |
| **合计** | **121** | |

当前: 127 已实现。完成后: ~248。