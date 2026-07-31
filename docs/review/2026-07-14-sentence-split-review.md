# 分句（Sentence Splitting）代码 Review 报告

> 日期：2026-07-14 | 范围：`core/` 下分句相关全部代码和文档

---

## 1. 文件清单

| 文件 | 行数 | 角色 |
|------|------|------|
| [core/sentence_buffer.py](../../core/sentence_buffer.py) | 142 | **核心**：流式分句引擎 |
| [core/block_filter.py](../../core/block_filter.py) | 142 | 流式过滤 `<|markdown|>` `<|code|>` 块，分句的前置步骤 |
| [core/cognitive_loop.py](../../core/cognitive_loop.py) | ~700+ | **消费者**：串联 BlockFilter → SentenceBuffer → TTS/output |
| [core/tts_stream.py](../../core/tts_stream.py) | 309 | **消费者**：接收完整句，TTS 合成 + 绑定发送文字/音频 |
| [core/tests/test_sentence_buffer.py](../../core/tests/test_sentence_buffer.py) | 439 | 65 个 unit test，11 组 |
| [core/tests/test_phase3.py](../../core/tests/test_phase3.py) | L420-427 | 集成测试：验证每次 `process()` 重置 SentenceBuffer |
| [docs/architecture.md](../architecture.md) | L440-452 | 架构文档 §4.6 分句缓冲 |
| [docs/frontend-api.md](../frontend-api.md) | L474-529 | 前端 API 文档 §分句策略 |
| [docs/sync-lessons.md](../sync-lessons.md) | 117 | 文字/语音同步演进史，大量分句踩坑记录 |
| [docs/completed.md](../completed.md) | 多处 | SentenceBuffer 历次修复/升级的 changelog |

外部关联（非 core/ 但相关）：

| 文件 | 说明 |
|------|------|
| [Module/tts/gpt_sovits.py](../../Module/tts/gpt_sovits.py) L106 | `text_split_method: "cut5"` — GPT-SoVITS 服务端内部切分参数，与应用层 `SentenceBuffer` 独立 |

---

## 2. 数据流架构

```
LLM stream tokens
  │
  ├─ inner_voice 状态机 (PRE → THINK → REPLY)
  │    └─ 剥离 <inner_voice>...</inner_voice>，think 内容不走分句/TTS
  │
  ├─ BlockFilter.feed(delta)
  │    └─ 流式过滤 <|markdown|> <|code|> 块标记，只留纯对话文本
  │
  ├─ SentenceBuffer.feed(clean_text)
  │    └─ 逐字符累积 → 检测分隔符 → 返回完整句 or None
  │
  └─ 完整句子
       ├─ TTS 开启 → TTSStreamManager.stream_sentence(sentence)
       │               └─ 后台合成 + 等待首块音频 → 绑定发送 delta + audio chunks
       │                  (workers=1 FIFO 保证句间顺序)
       └─ TTS 关闭 → output._broadcast("on_text_delta", sentence)
                       └─ 逐句发送文字 delta，前端逐条气泡
```

流结束后：
```
BlockFilter.flush() → SentenceBuffer.feed() → SentenceBuffer.flush()
build_parts_with_blocks(acc_text, all_sentences) → session 存储
```

---

## 3. 核心模块详解

### 3.1 SentenceBuffer（`core/sentence_buffer.py`）

**公开 API：**

```
feed(delta: str) → str | None    # 喂入文本增量，有完整句时返回，否则 None
flush() → str | None              # 清空缓冲，返回残留文本
reset() → None                    # 重置缓冲
```

**关键设计决策：**

| 特性 | 实现 | 位置 |
|------|------|------|
| 默认分隔符 | `。！？!?\n～…〜.` | L10, L39 |
| 英文句号检测 | `_is_dot_boundary()` — 过滤数字(3.14)、缩写(Dr./Mr./U.S.)、URL | L14-33 |
| 连续分隔符合并 | `啊？！` → 一个句子，`。！？` 不拆成两句 | L93-105 |
| 空格在连续分隔符间 | `"啊？ ！"` 也合并为一句（允许单个空格） | L97-105 |
| 最小句长 | `min_chunk_length=5`，短句暂存，后续有文字跟随时释放 | L119-123 |
| 尾部空白处理 | `complete.rstrip()` 去掉尾部 `\n` 和空格 | L126 |
| 输出保护 | `before.strip()` 为空时跳过（纯分隔符输入不产生空句） | L110-112 |

**分隔符逐字符扫描（`_find_split`）：**

```
对每个分隔符字符: text.find(ch) → 找到最早出现位置
  ├─ 如果是 '.' → 调 _is_dot_boundary() 判断是否为真句号
  └─ 否则 → 直接作为候选
返回最早位置，无则 -1
```

**`_is_dot_boundary` 判断规则：**

```
. 前面是数字 → False（小数点）
. 前面 3 字符窗口命中缩写列表 → False (Dr./Mr./Ms./Mrs./Prof./Sr./Jr./St./Mt./
                                          Capt./Col./Gen./Sgt./Lt./Rev./Hon./Rep./Sen.)
. 前 1-2 字符全大写 → False (U.S./D.C.)
. 前后都是字母 → False (example.com / word boundary)
其他 → True（句子边界）
```

### 3.2 BlockFilter（`core/block_filter.py`）

**公开 API：**

```
feed(chunk: str) → str        # 喂入 delta，返回去掉块标记的干净文本
flush() → str                 # 清空缓冲，返回残留文本
```

**设计要点：**

- `_depth` 计数器追踪嵌套深度（进入 `<|markdown|>` 或 `<|code|>` 时 +1，遇到对应结束标记时 -1）
- 保留末尾 13 字符（`_BLOCK_LOOKBEHIND`）不立即输出，防止标记被 chunk 边界截断
- `flush()` 时用 `_BLOCK_START_RE.sub("", result)` 清理可能残留的未闭合标记

**双模式：**

| 模式 | 类/函数 | 用途 |
|------|---------|------|
| 流式过滤 | `BlockFilter` | 实时过滤，输出送 SentenceBuffer |
| 事后解析 | `build_parts_with_blocks()` | 从完整文本解析块结构，构建 session parts |

### 3.3 cognitive_loop.py 中的集成点

| 位置 | 操作 |
|------|------|
| L107 | `self._sentence_buf = SentenceBuffer()` — 构造（复用整个 Agent 生命周期） |
| L226 | `self._sentence_buf.reset()` — 每轮 `process()` 开始时重置 |
| L339 | `block_filter = BlockFilter()` — 每轮流式循环新建 |
| L360-380 | `_handle_sentence(clean_text)` — 喂入 SentenceBuffer，完整句送 TTS/output |
| L385-461 | 流式循环 — PRE/THINK/REPLY 三状态下分别调 `_handle_sentence()` |
| L480-481 | `iv_tail` flush — PRE 状态尾部文本送分句 |
| L494-497 | `block_filter.flush()` + `_handle_sentence()` — 流结束清空 block filter |
| L498-506 | `sentence_buf.flush()` — 残留文本处理，emoji-only 跳过 |
| L699 | `build_parts_with_blocks(public_text, all_sentences)` — 构建 session parts |
| L1032 | `self._sentence_buf.reset()` — interrupt 处理时重置 |

---

## 4. 测试覆盖

[test_sentence_buffer.py](../../core/tests/test_sentence_buffer.py) — 65 个 unit test，11 组，全部标记 `pytest.mark.unit`：

| 组 | 测试内容 | 数量 |
|----|---------|------|
| Group 1 | 基本标点分隔符（中英文句号/感叹/疑问/换行/多句） | 8 |
| Group 2 | 遗漏分隔符（～ … …… 〜 ——） | 6 |
| Group 3 | 连续分隔符（？！、！？、!!、??、。。。、～！、空格间隔） | 7 |
| Group 4 | 最小句长阈值（hold/merge/emit/边界/单字/默认值/精确边界） | 8 |
| Group 5 | 英文标点不拆分（逗号/分号/冒号/中文逗号） | 4 |
| Group 6 | 中英混合（英文单词/中文分隔符/code block/URL/数字/缩写/混句） | 8 |
| Group 7 | 流式行为（逐字/部分/各种 flush/reset） | 7 |
| Group 8 | 边界情况（空输入/空白/纯分隔符/超长句/换行居中/回车） | 6 |
| Group 9 | 日文标点（日语句号/日文逗号不拆分） | 2 |
| Group 10 | 批量喂入（三句一次/hold+批量/跨 feed 分隔符） | 3 |
| Group 11 | 可配置分隔符（自定义/无分隔符/纯英文/中文自定义/min_chunk） | 6 |

---

## 5. 历史演进（按时间线）

来源：[sync-lessons.md](../sync-lessons.md) + [completed.md](../completed.md)

| 阶段 | commit | 内容 |
|------|--------|------|
| 阶段 0 | — | 没有分句，整段发送，整段 TTS |
| 阶段 1 | `f031850` | 引入 SentenceBuffer，按 `。！？\n～…` 切句，每句触发 TTS |
| 阶段 2 | `a7e77d8`~`984c48d` | 逐句气泡、sync 机制（3 层缓冲：event + buffer + sentences） |
| 阶段 3 | `8815d47` | 前端逐条弹出 + 语音对齐（pendingBubbles / scheduleReveal） |
| 阶段 4 | `402035b` | 绑定发送，去掉 sync 机制（删除约 100 行服务端+前端代码） |
| 阶段 5 | `7849386`~`7ec8590` | 前端排队弹出修复（后句文字等前句音频播完才显示） |
| 分离 | `286a95d` | 修复 SentenceBuffer 残余前缀截断（sync flush 后残留前半句） |
| 升级 | 多处 | 分隔符扩展 (～…〜)、英文句号检测、连续合并、min_chunk 5、配置化 |
| 修复 | L342 | 删除 `_skip_dot_and_retry()` 死代码（`_find_split` 已过滤假句号） |
| 修复 | L460-462 | `_extract()` 尾部 `\n` 导致气泡空白行 → `complete.rstrip()` |

**关键教训：**

1. **不要用 chunk 位置做分句** — LLM chunk 边界 ≠ 语义边界
2. **Sync flush 后检查 SentenceBuffer 残余** — `flush()` 释放残留前缀
3. **`_broadcast` 的 `return_exceptions=True` 会静默吞错**
4. **CSS animation 覆盖 inline style** — 设 `opacity:0` 前先 `animation:none`
5. **`opacity:0` 仍占布局空间** — 延迟 DOM append 比隐藏元素更干净
6. **服务端保证顺序，前端只做缓存** — workers=1 FIFO 是绑定发送方案的前提

---

## 6. 观察到的问题 / 可优化点

### 6.1 死代码：`_split_pat` 编译但从未使用

[sentence_buffer.py L51-53](../../core/sentence_buffer.py#L51-L53) — `self._split_pat` 编译了正则表达式，但 `_find_split()` 实际走的是手动 `text.find(ch)` 逐字符循环。regex 从未被调用。

```python
self._split_pat = re.compile(
    rf"([{re.escape(delimiters.replace('.', ''))}]|[.]?)"
) if delimiters else re.compile(r"()")
```

### 6.2 `_find_split` 性能 O(n×d)

每次 `feed()` 都对当前 buffer 全文扫描所有分隔符（10+ 个字符各自 `text.find()`）。正常 buffer 只有几十到几百字符，影响不大；但若遇到无标点的长段输出，每次 feed 都全量重扫。

### 6.3 缩写列表硬编码且不完整

[sentence_buffer.py L22-24](../../core/sentence_buffer.py#L22-L24) — 18 个英文缩写，无法扩展：

```python
prefix in {"Dr", "Mr", "Ms", "Mrs", "Prof", "Sr", "Jr", "St", "Mt",
           "Capt", "Col", "Gen", "Sgt", "Lt", "Rev", "Hon", "Rep", "Sen"}
```

缺少例如 `vs.`、`etc.`、`approx.`、`dept.`、`est.` 等常见缩写。也没有考虑中文语境下对英文句号的特殊处理可配置化。

### 6.4 `_extract` 连续分隔符逻辑复杂

[sentence_buffer.py L93-105](../../core/sentence_buffer.py#L93-L105) — 连续分隔符合并 + 空格跳过用嵌套 while 手写，分支多、不够直观。可考虑用单个 regex 匹配 `[分隔符]+[ ]?[分隔符]*` 模式。

### 6.5 `min_chunk` 的 "un-stick" 行为是隐式的

[sentence_buffer.py L119-123](../../core/sentence_buffer.py#L119-L123) — 短句后面有文字时立即释放（防止 buffer 卡死），没有文字时 hold（等更多内容或 flush）。这个"释放 vs hold"的行为逻辑只靠测试覆盖，代码注释没有解释设计意图。

### 6.6 BlockFilter 和 SentenceBuffer 生命周期不一致

- `SentenceBuffer` 在 `CognitiveLoop.__init__` 创建一次，复用
- `BlockFilter` 在每轮流式循环中新建

两者都是流式处理器，但生命周期不对称。`SentenceBuffer` 依赖 `reset()` 清状态，如果有遗漏的 reset 调用会导致跨轮污染。

### 6.7 `build_parts_with_blocks` 丢失交错顺序

[block_filter.py L104-141](../../core/block_filter.py#L104-L141) — sentences 全部先入 parts，然后 markdown/code 块追加在列表末尾。原始文本中句子和块的交替顺序丢失。例如：

```
原始: "你好。" <|markdown|> 表格 </|markdown|> "再见。"
parts: ["你好。", "再见。", "表格"]  ← 块被排到最后
```

### 6.8 缺少中文省略号上下文判断

中文省略号 `……` 作为分隔符会拆句（"这个嘛……算了。" → 两句），但有时只是语气停顿不应拆。当前行为是无条件拆分。

### 6.9 没有考虑引号包裹的句子

`他说："你好。今天天气不错。"` — 引号内的 `。` 正确触发分句，但右引号本身不作为分隔符。这是合理行为，但如果需要把整个引语作为一句，则不支持。

### 6.10 Buffer 无上限

如果 LLM 输出极端长且无标点的文本，`_buffer` 会无限增长直到流结束。没有内存保护机制。

### 6.11 `_DELIMITER_RUN` 和 `_HARD_DELIMITERS` 风格不一致

```python
_HARD_DELIMITERS = "。！？!?\n～…〜"   # 模块级字符串
_DELIMITER_RUN = set(_HARD_DELIMITERS + ".")  # 模块级集合
```

一个是字符串，一个是集合。两者定位模糊。

---

## 7. 依赖关系图

```
cognitive_loop.py
  ├── SentenceBuffer (core/sentence_buffer.py)
  │     └── _is_dot_boundary()  [模块级函数]
  │     └── _HARD_DELIMITERS / _DELIMITER_RUN  [模块级常量]
  ├── BlockFilter (core/block_filter.py)
  │     └── build_parts_with_blocks()  [模块级函数]
  │     └── _BLOCK_PATTERN / _BLOCK_START_RE  [模块级 regex 常量]
  └── TTSStreamManager (core/tts_stream.py)
        └── is_emoji_only()  [模块级函数]
        └── _EMOJI_RE / _PAUSE_TAG_RE / _TONE_TAG_RE / _CN_ACTION_RE  [模块级 regex]

测试:
  test_sentence_buffer.py → SentenceBuffer
  test_phase3.py → CognitiveLoop (间接测试 SentenceBuffer.reset)
```

---

## 8. 小结

**做得好的：**

- API 简洁（`feed` / `flush` / `reset`），与流式处理天然契合
- 英文句号检测避免了大量误分割（数字、缩写、URL）
- 连续分隔符合并保证了自然的 TTS 输出（"啊？！" 不拆成两个字）
- 测试覆盖充分（65 个 test，11 个维度）
- 历史踩坑经验丰富，文档记录完整（sync-lessons.md）

**值得优化的（按影响从大到小）：**

1. 死代码清理（`_split_pat`）
2. `_extract` 逻辑简化（连续分隔符合并可用 regex）
3. `_find_split` 性能优化（单 regex pass 替代逐字符扫描）
4. BlockFilter 和 SentenceBuffer 生命周期统一
5. `build_parts_with_blocks` 保持交错顺序
6. 缩写列表可配置化
7. Buffer 上限保护
8. `min_chunk` un-stick 行为文档化
