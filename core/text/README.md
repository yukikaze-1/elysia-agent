# Text — 文本流处理管线

> 文档导航：[项目文档](../../README.md)

LLM 流式输出 → TTS 输入之间的文本流处理管线：结构感知切分 + 分句保护 + 代码块分拣 + 协调器。

统一组件族（tags/tokenizer/splitter/parts/stream_processor），替代旧的分散解析管道
（Task 7 重构已删除旧分块过滤 + 分句缓冲组件与 cognitive 下的旧协调器）。
事件面完全兼容：MOOD / INNER_VOICE / SENTENCE / TEXT_DELTA / TTFT（无新增事件类型）。

## 输入 / 输出契约

### 输入

**`StreamProcessor.feed(delta: str) -> list[StreamEvent]`** 是唯一输入入口。

| 项 | 说明 |
|----|------|
| 内容 | LLM 流式输出增量文本片段。F2 协议格式：`<mood>...</mood>` + `<inner_voice>...</inner_voice>` + 公开回复 |
| 切分 | **任意**——delta 可以是单字符、半截标签、任意 chunk 边界；核心不变式保证"切分解析"与"整体解析"结果一致 |
| 标签 | `<#x#>` TTS 停顿 / `（中文指示）` 舞台指示（半角+全角）/ `(laughs)` 语气词 / ```` ```lang ```` 代码围栏（仅行首）/ `<\|code\|lang>` / `<\|markdown\|>` |
| 生命周期 | 每轮对话创建新 `StreamProcessor` 实例；`reset()` 可复用（清空内部状态）|

### 输出（三类）

**1. 流式事件**（`feed`/`flush` 返回 `list[StreamEvent]`）：

| 事件类型 | 触发时机 | 内容 | 消费方 |
|----------|----------|------|--------|
| `MOOD` | `<mood>` 闭合 | 心情文本 | character engine（`_mood_applied` 标记，无事件则关键词 fallback）|
| `INNER_VOICE` | `<inner_voice>` 闭合（可多个）| 内心独白文本 | 前端 inner_voice 气泡 + session（累积换行合并）|
| `SENTENCE` | 分句完成 | 完整句子（含内嵌 `<#x#>`/舞台指示）| TTS（tts_adapter 逐句提取显示/TTS 文本）|
| `TEXT_DELTA` | 每个 SPEECH token 即时 | 显示增量（已剥离 PAUSE）| 文本模式直接广播；TTS 模式经 tts_adapter |
| `TTFT` | 首个非 META 可见文本到达 | 空文本（信号事件）| 首 token 延迟统计 |

**2. 回合末 API**（`flush()` 之后调用）：

| API | 返回 | 用途 |
|-----|------|------|
| `flush()` | `list[StreamEvent]` | 尾句 + 残留处理（未闭合 inner_voice 广播 / 未闭合代码块丢弃 / emoji-only 尾句合并）|
| `get_blocks()` | `list[BlockRef]` = `(kind, content, lang, after_sentence_idx)` | finalize 的 `block` 命令推送（含 language，修复旧丢失）|
| `rebuild_public_text()` | `str` | 去标记纯文本（保留 PAUSE/舞台指示，无 META 泄漏）→ analyze_assistant / log / after_response |

**3. parts 层**（session 存储，非流式）：

| API | 输入 → 输出 | 用途 |
|-----|-------------|------|
| `build_parts(all_sentences, blocks)` | → `(list[TextPart], content_type)` | `AssistantMessage.content`（session 归档）|

### 输出流向

```
feed() 事件 ──→ MOOD → character engine
              ├─ INNER_VOICE → 前端气泡 + session
              ├─ SENTENCE → tts_adapter（TTS 合成 + 逐句显示）
              └─ TEXT_DELTA → 前端文本流
flush() 后 ──→ get_blocks() → finalize block 命令（前端代码块）
              └─ rebuild_public_text() → session/日志/记忆沉淀
              └─ build_parts() → AssistantMessage.content
```

## 架构 / 数据流

```
LLM delta → StreamProcessor.feed(delta)              (core/text/stream_processor.py 协调器)
              │
        tokenizer.feed(delta) → token 流              (StreamTokenizer：跨 chunk 缓冲，只吐完整 token)
              │ 按 kind 路由
   ┌──────────┼───────────────┬──────────────┐
 SPEECH     PAUSE/STAGE_CN   CODE/MARKDOWN    META
   │          │               │（收集，不冲刷）│闭合时
   ▼          ▼               ▼              ▼
   └────────→ splitter（分句 + span 保护）   MOOD/INNER_VOICE 事件
                │  │
             SENTENCE  TEXT_DELTA
           （含内嵌标签） （剥离 PAUSE）
                │
        tts_adapter 逐句提取（显示路径）：
        显示 = 句首 STAGE_CN 独立行 + 语音 + 句中 STAGE_CN
        TTS 文本 = 语音 + PAUSE + (laughs)，剥离 STAGE_CN

回复完成后:
  → StreamProcessor.flush()     → 处理残留（未闭合 inner_voice 广播、尾句、emoji-only 合并）
  → StreamProcessor.get_blocks() → list[BlockRef]（(kind, content, lang, after_sentence_idx)）
  → parts.build_parts(all_sentences, blocks) → TextPart + content_type（session 存储）
  → parts.rebuild_public_text(token 流)     → 去标记纯文本（public_text 重建，无 META 泄漏）
```

设计基准：`docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`
（行为对照表 12 项 + 核心不变式 + 持有上界 + 围栏细则）。

## 关键机制

### 核心不变式

**对任意输入串 S，在任意字符边界切分为 delta 序列，解析出的 token 流 / 事件序列
必须与 S 一次到达时一致**（由 property test 强制）。

### 组件职责表

| 文件 | 职责 |
|------|------|
| `tags.py` | 标签注册表：PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部定义 + emoji 收口（`is_emoji_only`）。纯数据模块，仅依赖 stdlib。**META_TAGS 三元组 `(open, close, emit)`——emit 与 `StreamEventType.value` 对齐，标签→事件路由注册表驱动**。未来加 `<` 前缀新标签 = 加一行注册（编译期生效，`_OPEN_PATTERNS` 重新派生；`[action]` 式需另扩 tokenizer 起始识别）。`tts_adapter` 从这里导入共享正则（删本地副本 + 死代码 `_TONE_TAG_RE`） |
| `tokenizer.py` | `StreamTokenizer`：增量输入 → 只吐完整 token。标签起始前缀 / PAUSE 尾串 / STAGE_CN K-M 计数窗口 / 行首反引号持有上界；行首双状态（`at_line_start` + 缩进计数）判定围栏 |
| `splitter.py` | `SpeechSplitter`：消费 SPEECH/PAUSE/STAGE_CN token → SENTENCE + TEXT_DELTA。边界扫描只在 SPEECH 文本上进行，PAUSE/STAGE_CN 以零宽位置保护，内部标点不参与边界判定。`completed_count` 供 `after_sentence_idx` 块定位 |
| `parts.py` | `build_parts(all_sentences, blocks)`：按 `after_sentence_idx` 交错出 `TextPart` + content_type（不再二次扫描 acc_text）。`rebuild_public_text()`：从 token 流重建去标记纯文本。仅服务 session 存储/归档，不参与显示推送 |
| `stream_processor.py` | `StreamProcessor` 协调器：tokenizer + splitter + 事件发射 + `get_blocks()` + public_text 重建。吸收旧状态机 + 旧块过滤逻辑 + TTFT/emoji 逻辑。**META 路由查注册表 `emit` 字段 + `_EMIT_BY_NAME` 枚举自动派生映射，未知 emit 降级 `logger.warning` + 丢弃**。每轮对话创建新实例 |

### 分句保护

- **PAUSE `<#x#>` 是原子 token**：以受保护 span 整体嵌入句子，绝不切开（根除"标签被切碎"类 bug）
- **STAGE_CN `（中文指示）` 半角+全角**：span 整体不切分、从 TTS 剥离；K=16 英文早退窗 / M=64 CJK 上限（`(laughs)` 无 CJK → 回落 SPEECH 保留进 TTS）
- **代码块分拣**：```` ```lang ```` 围栏（仅行首）+ `<|code|lang>` / `<|markdown|>` → CODE/MARKDOWN token，不进 SENTENCE/TTS、不参与分句，finalize 时进 parts + `block` 命令

### META 不冲刷分句缓冲

`你好<mood>开心</mood>世界` → 语音文本跨 META 连续累积，SENTENCE 序列与 `你好世界` 一致。
任意位置的 `<mood>` / `<inner_voice>` 都剥离为 META（重复 inner_voice 每个都发事件）。

## 文件布局

```
core/text/
├── __init__.py       — 重导出 StreamProcessor/StreamEvent/StreamEventType/BlockRef/build_parts/rebuild_public_text/PAUSE_RE/STAGE_CN_RE/is_emoji_only
├── tags.py           — 标签注册表（纯数据）
├── tokenizer.py      — StreamTokenizer 结构感知切分
├── splitter.py       — SpeechSplitter 结构感知分句
├── parts.py          — TextPart + content_type 交错构建 / public_text 重建
├── stream_processor.py — StreamProcessor 协调器
└── tests/
    ├── test_tags.py
    ├── test_tokenizer.py
    ├── test_splitter.py
    ├── test_parts.py
    └── test_stream_processor.py

消费者:
core/cognitive/loop.py — CognitiveLoop._stream_llm_response() 唯一消费方（事件 + get_blocks + public_text）
core/adapter/tts_adapter.py — 从 tags.py 导入共享正则（逐句提取逻辑保留原位）
（文本提取 content_text_from_content / MessageObj 已迁 core/utils/text_utils，见该包 README）
```

## 调试

`core/text` 组件无独立 debug 面板入口。相关行为可通过 `/debug` 的 **LLM Response** 卡片
间接观察——`parts.build_parts()` 的产出（TextPart + content_type）体现在消息渲染中。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [StreamParser 重构设计](../../docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md) | 行为对照表 + 核心不变式 + 持有上界 + 围栏细则（本模块的设计基准） |
| [core/cognitive/README.md](../cognitive/README.md) | 认知循环 — StreamProcessor 的唯一消费方 |
