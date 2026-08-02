# CognitiveLoop — 认知循环核心引擎

> 文档导航：[项目文档](../../README.md)

ReAct 调度器 + inner_voice 状态机，是认知流水线的核心执行引擎。流式接收 LLM 输出，实时解析 `<inner_voice>` 标签，检测工具调用，管理 TTS 流式播放。

使用 mixin 模式：`CognitiveLoop(CognitiveTools)`。

## 架构

```
用户消息 → process()
  → _think_loop()（ReAct 主循环：build → [stream → decide] * 6 rounds）
    ├─ _build_turn_prompt()     — 构建 messages + tool_defs
    ├─ _stream_llm_response()   — LLM 流式消费 + StreamProcessor 事件分发
    ├─ [判定] tool_calls?
    │   ├─ YES → _execute_tool_round()  — submit_task 拦截 + 工具调度 → 回到 stream
    │   └─ NO  → _finalize_turn()       — 标签剥离 + 归档 + 结构化块推送
    └─ _after_response()
      ├─ MemoryConsolidator.consolidate()
      ├─ CharacterReflector tick
      ├─ MemoryReflector check
      └─ SessionMemory 持久化
```

### TurnPhase — 内部处理阶段追踪

`_think_loop()` 在每个步骤设置 `self._phase`，使 debug 面板可以观测 THINKING 状态下的精确位置：

| Phase | 对应方法 | 说明 |
|-------|---------|------|
| `BUILD_PROMPT` | `_build_turn_prompt()` | 构建 messages + tool_defs |
| `STREAMING` | `_stream_llm_response()` | LLM 流式调用 + StreamProcessor 解析 |
| `EXECUTING_TOOLS` | `_execute_tool_round()` | submit_task 拦截 + 工具调度 |
| `FINALIZING` | `_finalize_turn()` | 标签剥离 + 归档 + 结构化块推送 |

`TurnPhase` 是内部追踪标记，不同于 `AgentFSM`（外部状态机）。不设状态转换验证，不改 FSM 接口。

## 数据流

```
LLM stream
  ├─ content delta ──► StreamProcessor.feed(delta)  (core/text 协调器)
  │                     ├─ META（mood）剥离 ──► MOOD 事件 → _apply_mood()
  │                     ├─ META（inner_voice）分离 ──► INNER_VOICE 事件 → output.command()
  │                     ├─ tokenizer + splitter（span 保护分句，code/markdown 不进语音流）
  │                     │   └─ SENTENCE 事件 → TTS / 直接输出
  │                     │   └─ TEXT_DELTA 事件 → 前端增量
  │                     │   └─ TTFT 事件 → 延迟统计
  └─ tool_call delta → merge_stream_tool_calls()
                        → normalize_tool_calls()
                          → 直接执行 / submit_task 拦截
```

## 关键机制

### inner_voice 协议

LLM 每条回复内嵌一个 `<inner_voice>` 块（1-2 句：当前感受 + 回应意图，不做推理链）。解析结果提取为 `mood`（当前情绪），影响 TTS 情感参数。标签检测逻辑由 `StreamProcessor` 独立实现（纯状态机，零 I/O），位于 `core/text/stream_processor.py`。

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

### StreamProcessor 流式解析管道

纯状态机组件，位于 `core/text/stream_processor.py`（Task 7 重构后统一到 core/text 组件族：tags + tokenizer + splitter + parts）。每轮对话创建新实例。

- `feed(delta: str) → list[StreamEvent]` — 输入流式文本片段，内部经 tokenizer（结构感知切分）→ splitter（span 保护分句）产出事件
- `flush() → list[StreamEvent]` — 流结束吐出尾句、丢弃未闭合块、广播未闭合 inner_voice；尾部纯 emoji 合并到前一 SENTENCE 避免独占气泡
- `get_blocks() → list[BlockRef]` — 本轮全部代码块 `(kind, content, lang, after_sentence_idx)`，供 `_finalize_turn` 构建 parts + block 命令
- `reset()` — 重置所有状态

事件类型: `MOOD` / `INNER_VOICE` / `SENTENCE` / `TEXT_DELTA` / `TTFT`

`CognitiveLoop._dispatch_stream_event()` 负责将事件翻译为副作用（mood 更新、inner_voice 广播、TTS 分发、FSM 切换）。

**TTS 停顿标签保护** — `<#0.5#>` 由 tokenizer 识别为 PAUSE 原子 token（持有上界由尾串正则驱动，跨 chunk 不变式保证不切碎），以受保护 span 整体嵌入句子；分句边界只在 SPEECH 文本上扫描，停顿标签内的 `.` 永不参与边界判定。停顿标签不显示在 UI 中。

详细设计见 `core/text/README.md` 与 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`。

## 文件布局

```
core/cognitive/
├── __init__.py    — 重导出 CognitiveLoop
├── loop.py               — CognitiveLoop(CognitiveTools): process + ReAct 主循环 + TurnPhase 追踪（_build_turn_prompt / _stream_llm_response / _execute_tool_round / _finalize_turn / _dispatch_stream_event）
├── tools.py              — CognitiveTools mixin: _execute_tools_directly + _handle_submit_task
├── timing.py      — Timer + TimingClock 延迟统计
├── delegation.py  — DelegationPreparer: submit_task 前的委派准备（工具筛选/摘要/约束）
├── user_profile.py — UserProfile: 用户画像模型（CognitiveLoop 管理）
└── tests/
    ├── test_timing.py
    ├── test_tools.py
    └── ...（6 个测试文件）

关联文件:
core/
├── adapter/llm_adapter.py  — LLM 流式适配器
├── adapter/tool_call_parser.py — 流式 tool_call 合并/归一化
├── adapter/tts_adapter.py  — TTSStreamManager（句队列 + emoji 同步，导入 core/text/tags 共享正则）
├── text/tags.py             — 标签注册表（PAUSE/STAGE_CN/META/CODE/MARKDOWN + emoji 收口）
├── text/tokenizer.py        — StreamTokenizer 结构感知切分
├── text/splitter.py         — SpeechSplitter 结构感知分句
├── text/parts.py            — TextPart + content_type 构建 / public_text 重建
└── text/stream_processor.py — StreamProcessor 协调器（本模块消费）

（文本提取 content_text_from_content / MessageObj 已迁 core/utils/text_utils）

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
| [core/text/README.md](../text/README.md) | 文本流处理管线（tags + tokenizer + splitter + parts + 协调器） |
| [core/memory/README.md](../memory/README.md) | 记忆子系统（_after_response 触发沉淀） |
