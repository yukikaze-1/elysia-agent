# CognitiveLoop — 认知循环核心引擎

> 文档导航：[项目文档](../../README.md)

ReAct 调度器 + inner_voice 状态机，是认知流水线的核心执行引擎。流式接收 LLM 输出，实时解析 `<inner_voice>` 标签，检测工具调用，管理 TTS 流式播放。

使用 mixin 模式：`CognitiveLoop(CognitiveTools)`。

## 架构

```
用户消息 → process()
  → _think_loop()（PRE → THINK → REPLY 三阶段）
    ├─ PRE:   收集 inner_voice + 同时检测 tool_calls
    ├─ THINK: 等待 inner_voice 闭合，提取 Mood
    └─ REPLY: 流式输出 → SentenceBuffer → BlockFilter → TTS
  → _execute_tools_directly()  （如果有 tool_calls）
  → _submit_task()            （如果 LLM 调用了 submit_task）
  → _after_response()
    ├─ MemoryConsolidator.consolidate()
    ├─ CharacterReflector tick
    ├─ MemoryReflector check
    └─ SessionMemory 持久化
```

## 数据流

```
LLM stream
  ├─ content delta ──► SentenceBuffer.feed(token)
  │                     → 完整句 → stream_sentence(sentence)
  │                       → TTSStreamManager.stream_sentence()
  │                         → Phase 1: 并行下载音频
  │                         → Phase 2: 串行转发到前端（按 seq 门控）
  └─ tool_call delta → merge_stream_tool_calls()
                        → normalize_tool_calls()
                          → 直接执行 / submit_task 拦截
```

## 关键机制

### inner_voice 协议

LLM 每条回复内嵌一个 `<inner_voice>` 块（1-2 句：当前感受 + 回应意图，不做推理链）。解析结果提取为 `mood`（当前情绪），影响 TTS 情感参数。

### 工具循环（最多 5 轮）

```
THINKING → LLM 返回 tool_calls → EXECUTING → 工具结果注入 → THINKING
```

- `_execute_tools_directly()`: 并行执行轻量工具
- `_handle_submit_task()`: 拦截后台任务委托，提交到 TaskSystem
- 超时保护：每轮 60s，中断时级联 cancel

### TTS 流式播放

`TTSStreamManager` 管理句级流式合成：
- workers=1 FIFO 保证句间顺序
- emoji 排队依附 TTS 句：emoji-only 句先排队，下一 TTS 句开始时分发
- `reset_for_turn()` 每轮重置，`cancel_all()` 中断时清理
- Phase 1（并行下载）+ Phase 2（串行门控播放）保证顺序

### 认知时钟

`TimingClock` 请求级聚合（span 嵌套），记录 prompt/ttft/tools 等阶段耗时。通过 `_timing_history` 保留最近 20 轮，供 debug 面板和延迟分析。

## 文件布局

```
core/cognitive/
├── __init__.py    — 重导出 CognitiveLoop
├── loop.py        — CognitiveLoop(CognitiveTools): process + _think_loop
├── tools.py       — CognitiveTools mixin: _execute_tools_directly + _handle_submit_task
├── timing.py      — Timer + TimingClock 延迟统计
└── tests/
    └── test_timing.py

关联文件:
core/
├── adapter/llm_adapter.py  — LLM 流式适配器
├── adapter/tool_call_parser.py — 流式 tool_call 合并/归一化
├── adapter/tts_adapter.py  — TTSStreamManager（句队列 + emoji 同步）
├── text/block_filter.py     — 结构化块过滤
├── text/sentence_buffer.py  — 分句缓冲
└── text/text_utils.py       — 文本提取 + MessageObj

Module/
├── tts/          — TTS 后端（GPT-SoVits / MiniMax）
└── tool/         — ToolManager（工具注册 + 安全管线）
```

## 调试

访问 `http://localhost:8765/debug` → **Latency** 卡片，可查看最近 20 轮各阶段耗时分布。

后端 API: `GET /debug/api` → `info.latency_history` / `info.current_task`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
| [core/text/README.md](../text/README.md) | 文本流处理管线（BlockFilter + SentenceBuffer） |
| [core/memory/README.md](../memory/README.md) | 记忆子系统（_after_response 触发沉淀） |
