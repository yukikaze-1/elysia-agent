# 分句系统优化 — 设计 Spec

> 日期：2026-07-14 | 状态：待审阅 | Review: [分句 Review 报告](../../review/2026-07-14-sentence-split-review.md)

---

## 1. 概述

对 `core/sentence_buffer.py` 和 `core/block_filter.py` 进行一轮集中优化，修复 2 个正确性 bug + 4 个工程改进 + 死代码清理。

**核心原则**：公开 API 不变，调用层（`cognitive_loop.py`）改动最小化，所有现有测试保持通过。

---

## 2. 变更清单

### 2.1 `core/sentence_buffer.py` — 重写

| 变更 | 类型 | 说明 |
|------|------|------|
| 删除 `_split_pat` | 死代码清理 | 编译了从未使用 |
| 修复 `_is_dot_boundary` 对 `3.14.` 的误判 | bug fix | 原来只检查"前面是数字"，改为"前后都是数字"才认定为小数点 |
| 缩写检测改为完整单词扫描 | 正确性改进 | 原来固定 3 字符窗口 + 大小写敏感；改为向后扫描完整单词 + 大小写不敏感 |
| 增加 `max_buffer_length`（默认 2000） | 防御性保护 | 无标点长文本超过阈值时在空格处强制切分，无空格则在阈值处硬切 |
| `_extract` 拆分出 `_consume_sentence` | 结构优化 | 连续分隔符合并逻辑独立成方法，更清晰的循环替代嵌套 while |
| `_find_split` 单次遍历 | 微优化 | 替代逐分隔符 `str.find()`，对短 buffer 影响可忽略 |
| `_HARD_DELIMITERS` → `frozenset` | 风格统一 | 和 `_DELIMITER_RUN` 一致走 frozenset |

**API 兼容性**：

```
# 现有调用完全兼容
SentenceBuffer()                                              # 默认配置不变
SentenceBuffer(delimiters="。！", min_chunk_length=10)        # 现有参数不变

# 新增可选参数
SentenceBuffer(max_buffer_length=2000)                        # 新增，有默认值
SentenceBuffer(abbreviations={"dr", "mr", "etc"})             # 新增，可扩展缩写
```

**`_is_dot_boundary` 行为变化**：

```
现有: "3.14." → 最后 . 前是 4（数字）→ False → 不切句 ✗
新版: "3.14." → 最后 . 前是 4（数字）但后为空 → 不是"前后都是数字"→ True → 切句 ✓

现有: "Dr. Who" → 固定窗口匹配 "Dr" → False ✓
新版: "Dr. Who" → 完整单词扫描 "dr" → False ✓  (大小写不敏感)
```

### 2.2 `core/block_filter.py` — 重写

| 变更 | 类型 | 说明 |
|------|------|------|
| `_depth` → `_mode: Literal["markdown","code"] \| None` | bug fix | 解决不同类型结束标签互相关闭的 bug（`<\|markdown\|>...</\|code\|>` 不会错误关闭）。**取舍**：不再支持跨类型嵌套（`<\|markdown\|>...<\|code\|>...</\|code\|>...</\|markdown\|>`），但 LLM 输出中不存在这种模式。 |
| `flush()` 未闭合块丢弃 | **安全 bug fix** | 旧实现可能把未闭合块内容泄漏给 TTS；新版检测到 `_mode is not None` 时直接丢弃 |
| 新增 `reset()` | API 补充 | 和 SentenceBuffer 生命周期对称，方便 interrupt 时清理 |
| `flush()` 尾部半截标签处理 | 防御性改进 | `rfind("<\|")` 检测尾部被截断的起始标签，丢弃而不泄漏 |
| `build_parts_with_blocks` 保持交错顺序 | **正确性 bug fix** | 旧实现 sentences 全排前面、blocks 追加末尾；新版按原文位置交替插入 |

**`BlockFilter.flush()` 行为变化**：

```
旧: feed("<|code|python>secret") + flush() → 可能返回 "secret"（泄漏！）
新: feed("<|code|python>secret") + flush() → ""（丢弃）
```

**`build_parts_with_blocks` 行为变化**：

```
输入文本: "你好。<|markdown|>表格</|markdown|>再见。"

旧 parts: ["你好。", "再见。", "表格"]           ← blocks 全在末尾
新 parts: ["你好。", "表格", "再见。"]           ← 保持原文顺序
```

**`build_parts_with_blocks` 交错排序算法**：

输入是 `acc_text`（含块标记的完整 LLM 输出）和 `sentences`（SentenceBuffer 已分句的纯文本列表）。句子不携带位置信息，因此算法需要从原文中定位每个句子：

```
1. _parse_blocks(acc_text) → [(start, end, ParsedBlock), ...]
   — 扫描 acc_text，找到所有完整块标记及其在原文中的起止位置

2. 按原文顺序交替构建 parts:
   text_cursor = 0
   for each block in blocks (按位置排序):
       text_segment = acc_text[text_cursor : block.start]  # 块之前的纯文本
       _append_sentences_from_segment(text_segment, sentences, ...)
           → 在 text_segment 中按顺序匹配 sentences（子串查找）
           → 匹配到的句子追加到 parts，推进 sentence_index
       parts.append(block_part)  # 插入块
       text_cursor = block.end

3. 处理最后一个块之后的剩余文本:
   _append_sentences_from_segment(acc_text[text_cursor:], ...)

4. 兜底: sentences 中未匹配到的句子追加到末尾（防止因空白差异丢失数据）
```

**句子匹配的健壮性**：SentenceBuffer 会对句子做 `rstrip()`，可能与 `acc_text` 中的原始文本不完全一致。`_append_sentences_from_segment` 先用原始句子做子串查找，失败时 fallback 到 `sentence.strip()` 再查找。仍未匹配的句子在步骤 4 兜底追加。

**`_CODE_START_RE` 兼容性**：

GPT 建议用 `[A-Za-z0-9_+.-]*` 匹配语言标识符。保持与现有 LLM 输出格式兼容——现有 `\w+` 已足够，但 `[A-Za-z0-9_+.-]*` 更宽容。采用后者。

### 2.3 `core/cognitive_loop.py` — 集成调整

| 变更 | 类型 | 说明 |
|------|------|------|
| SentenceBuffer 每轮新建 | 生命周期统一 | `process()` 入口处新建并赋给 `self._sentence_buf`，替代 `__init__` 中的一次性创建 |
| BlockFilter 每轮新建 | 生命周期统一 | 同 SentenceBuffer，`process()` 入口处新建 `block_filter`，存为实例属性供 interrupt 清理 |
| flush 顺序明确 | 流程规范 | `block_filter.flush() → sentence_buf.feed()/flush()` 顺序已正确，加注释说明 |
| interrupt 清理 | 保持现有调用 | `_handle_interrupt()` 中 `self._sentence_buf.reset()` 保持不变——buffer 是实例属性，只是每轮重新赋值 |

**关键设计决策 — 为什么保留 `self._sentence_buf` 而非纯局部变量**：

`_handle_interrupt()` 从 `process()` 的 `except CancelledError` / `except Exception` handler 中调用（L245、L259）。它是独立方法，无法访问 `process()` 的局部变量。若 `sentence_buf` 是纯局部变量，interrupt 时 `_handle_interrupt()` 无法清理它。

**方案**：保留 `self._sentence_buf` 作为实例属性，但在 `process()` 入口处**重新赋值**：

```python
# process() 入口
self._sentence_buf = SentenceBuffer(min_chunk_length=5, max_buffer_length=2000)
self._block_filter = BlockFilter()
```

**变更前**：
```python
# __init__
self._sentence_buf = SentenceBuffer()

# process() 入口
self._sentence_buf.reset()

# _handle_interrupt
self._sentence_buf.reset()
```

**变更后**：
```python
# __init__ — 删除 self._sentence_buf 的初始化（或初始化为 None）

# process() 入口 — 每轮新建
self._sentence_buf = SentenceBuffer(min_chunk_length=5, max_buffer_length=2000)
self._block_filter = BlockFilter()

# _handle_interrupt — 保持不变
self._sentence_buf.reset()
self._block_filter.reset()  # 新增
```

**注意**：`_think_loop` 内部创建的 `block_filter`（L339）需要改为使用 `self._block_filter`，使 interrupt 路径能访问到同一个实例。

### 2.4 测试

**`core/tests/test_sentence_buffer.py`** — 新增 ~4 个 test：

```
test_decimal_terminal_period     # 3.14. 末尾句号正确切句
test_decimal_without_terminal    # 3.14 中间不切句
test_abbreviation_case_insensitive  # dr. / DR. 都不切句
test_max_buffer_forced_split     # 超长无标点文本强制切分
```

**`core/tests/test_block_filter.py`** — 新建文件，~8 个 test：

```
test_code_content_filtered       # 代码块内容不进 TTS
test_unclosed_code_no_leak       # flush 不泄漏
test_marker_split_across_chunks  # 标记跨 chunk 正确处理
test_mismatched_end_tags         # </|code|> 不关闭 <|markdown|>
test_markdown_nested_code        # <|markdown|> 内嵌 <|code|> 跨类型嵌套行为
test_blockfilter_reset           # reset 后状态干净
test_parts_preserve_order_simple     # 单块+单句交替
test_parts_preserve_order_complex    # 多块多句、块在开头/结尾/中间
test_parts_preserve_order_unmatched  # 句子在原文中找不到时的兜底追加
```

---

## 3. 不变的部分

- SentenceBuffer 公开方法：`feed()` / `flush()` / `reset()` 签名不变（**`__init__` 新增两个可选参数**：`max_buffer_length` 和 `abbreviations`，均有默认值，现有调用方无需修改）
- BlockFilter 公开方法：`feed()` / `flush()` 签名不变，新增 `reset()`
- `build_parts_with_blocks()` 签名不变（返回值语义修正但不影响现有调用方）
- 默认分隔符集合不变
- `min_chunk_length` 默认值不变（5）
- TTSStreamManager 不变
- cognitive_loop 的外部行为不变（同样的输入产生同样的输出，除了 bug 修复带来的正确性变化）

---

## 4. 不在本次范围内的

- 中文省略号上下文判断（`……` 始终作为分隔符）
- 引号包裹句子的特殊处理
- `_find_split` 改用正则（收益太小，不引入复杂度）
- 前端代码变更
- TTS 管线变更
- 前导小数点 `.5` 的句号误判（如 "probability is .5" 中的 `.` 会被当作句号）——这是已有问题，新规则"前后都是数字才是小数点"也未覆盖此场景，不改

---

## 5. 风险与回滚

| 风险 | 概率 | 缓解 |
|------|------|------|
| `_is_dot_boundary` 新规则过于激进地切句 | 低 | 有 65 个现有 test 保护回归；新增 `3.14.` test 验证目标场景 |
| 缩写检测重写（3 字符窗口 → 完整单词扫描）行为不一致 | 中 | 新算法：向后扫描完整单词 + 大小写不敏感。`U.S.` 类缩写（含内部句号）由独立的正则分支 `[A-Z]\.?[A-Z]{1,2}` 处理，不依赖单词扫描。现有 test `test_abbreviation` / `test_mixed_sentences` 覆盖回归 |
| `build_parts_with_blocks` 顺序变更影响前端渲染 | 低 | 前端本身按 parts 数组顺序渲染，顺序更正确只会有正面影响 |
| `_mode` 不支持跨类型嵌套 | 极低 | LLM 输出中不存在 `<\|markdown\|>...<\|code\|>...</\|code\|>...</\|markdown\|>` 模式 |
| `max_buffer_length` 强制切分产生不自然断句 | 极低 | 阈值 2000 字符，触发条件极端（无标点长文本）；优先在空格处切 |
| 每轮新建 SentenceBuffer 丢失状态 | 无 | 保留 `self._sentence_buf` 实例属性，每轮重新赋值；`_handle_interrupt()` 仍可通过 `self._sentence_buf.reset()` 清理 |

回滚：两个文件各自独立，可分别回滚。API 兼容意味着回滚只需 `git revert`。

---

## 6. 实现步骤

1. 重写 `core/sentence_buffer.py`
2. 更新 `core/tests/test_sentence_buffer.py`（新增 4 个 test，现有 65 个全部通过）
3. 重写 `core/block_filter.py`（`_depth` → `_mode`，flush 防泄漏，`build_parts_with_blocks` 交错顺序）
4. 新建 `core/tests/test_block_filter.py`（~9 个 test）
5. 更新 `core/cognitive_loop.py`：
   - `__init__`：删除 `self._sentence_buf = SentenceBuffer()`
   - `process()` 入口：`self._sentence_buf = SentenceBuffer(...)` + `self._block_filter = BlockFilter()`
   - `_think_loop` 内：使用 `self._block_filter` 替代局部 `block_filter`
   - `_handle_interrupt`：新增 `self._block_filter.reset()`
6. 运行 `make test-quick` + 分句相关测试
7. 运行 `make test-normal` 确认无回归
