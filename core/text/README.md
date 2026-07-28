# Text — 文本流处理管线

> 文档导航：[项目文档](../../README.md)

LLM 流式输出 → TTS 输入之间的文本处理管线：过滤结构化块 + 分句 + 文本提取。

## 架构

```
LLM 流式 token
  → StreamProcessor._strip_pause_tags()
    → 剥离 <#0.5#> TTS 停顿标签（防止标签内 . 干扰 SentenceBuffer 分句）
  → BlockFilter.feed(token)
    → 剥离 <|markdown|> / <|code|> 块（不进 TTS）
    → 纯文本 token 流出
  → SentenceBuffer.feed(token)
    → 检测完整句边界（。！？!?\n～…〜）
    → 完整句 → TTSStreamManager.stream_sentence()
  → flush(): 输出尾部残留，纯 emoji 合并到前一 SENTENCE

后处理（回复完成后）:
  → BlockFilter.flush()     → 未闭合块丢弃
  → BlockFilter._blocks     → build_parts_with_blocks() → TextPart/MarkdownPart/CodePart 列表
  → content_text_from_content() → 纯文本提取（用于日志/prompt/调试）
```

## 关键机制

### BlockFilter

实时过滤 LLM 输出的 `<|markdown|>` 和 `<|code|>` 结构化块标记，防止代码/markdown 进入 TTS 合成：

- **流式过滤**: `feed(text) → str` — 传入 token，返回过滤后的纯文本
- **块标记**: `_CODE_START_RE` / `_CODE_END_RE` 匹配 `<|markdown|>`、`<|code lang|>` 等
- **嵌套保护**: depth 计数器，嵌套块正确处理
- **flush()**: 未闭合块自动丢弃（防泄漏），返回缓冲区残留
- **reset()**: interrupt 时清理状态，防止跨轮污染
- **每轮新建**: CognitiveLoop.process() 每次创建新实例

### SentenceBuffer

逐 token feed，检测完整句边界后输出：

- **分隔符**: 默认 `。！？!?\n～…〜`，支持中文/英文/日文标点
- **最小块长**: `min_chunk_length=5`，避免短句碎片化 TTS
- **最大缓冲**: `max_buffer_length=2000`，超长缓冲强制切分
- **缩写保护**: 英文缩写（Mr./Dr./etc.）和数字小数点不误分割
- **reset()**: interrupt 时清空内部缓冲

### text_utils

- `content_text_from_content(content)` — 从多模态 content 数组提取纯文本
- `MessageObj` — 非流式文本发送 dataclass（role + content + end_turn）

## 文件布局

```
core/text/
├── __init__.py      — 重导出 BlockFilter, SentenceBuffer, content_text_from_content, MessageObj
├── block_filter.py  — BlockFilter + build_parts_with_blocks
├── sentence_buffer.py — SentenceBuffer 分句缓冲
├── text_utils.py    — content_text_from_content + MessageObj
└── tests/
    ├── test_block_filter.py
    └── test_sentence_buffer.py

消费者:
core/cognitive/loop.py — CognitiveLoop._think_loop() 唯一消费方
core/prompt_assembler.py — content_text_from_content（提示词组装时提取纯文本）
```

## 调试

`BlockFilter` 和 `SentenceBuffer` 无独立 debug 面板入口。相关行为可通过 `/debug` 的 **LLM Response** 卡片间接观察——`build_parts_with_blocks()` 的产出体现在消息渲染中。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 文本流数据流 |
| [core/cognitive/README.md](../cognitive/README.md) | 认知循环 — BlockFilter/SentenceBuffer 的唯一消费方 |
