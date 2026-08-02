# 流式解析器边界情况覆盖补强设计

> 日期：2026-08-02
> 状态：设计定稿
> 范围：`core/text/`（tokenizer/splitter/stream_processor/parts）测试补强 + `core/text/tokenizer.py` 一处内存保护改动 + `core/cognitive/_think_loop` 守卫测试补强。

## 背景与动机

2026-08-02 修复"空 LLM 响应被静默吞掉"（`_think_loop` 空响应守卫）时，暴露出现有测试对**边界情况**的覆盖有 4 类真实缺口 + 1 个未钉死的形态：

1. **跨标签转换序列**——全仓搜不到"标签紧邻转换跨 chunk"测试（如 `</mood><#0.`、`</inner_voice>```` ``` ````）。这正是 2026-08-01 实际踩过的状态残留坑（fence 关闭后 `_held` 残留反引号 → 幽灵 FENCE 吞尾句）。当时只修了那一个具体 bug（`test_fence_closed_then_more_text_*`），没有系统化覆盖"进入→退出→再进入"组合序列。
2. **同一轮多个代码块**——`get_blocks()` 的 `after_sentence_idx` 跨块计数、多块顺序、与 parts 交错，只有 parts 层孤立测试（`test_two_blocks_same_slot`），tokenizer→splitter→get_blocks 全链路无多块测试。
3. **未闭合围栏无长度上界（内存隐患）**——标签扫描都有持有上界（STAGE_M/STAGE_K/ANGLE_MAX），但 `_fence_content` / `_code_content` / `_markdown_content` 三个块缓冲无限累积。LLM 输出永不开合的 ```` ``` ```` 或 `<|code|>` 会无限吃内存。
4. **Loop 层集成边界**——空/纯空白/纯标签/纯 emoji 响应在 `_think_loop` 层的守卫行为未钉死。
5. **tags-only 未钉死**——`<mood>..</mood><inner_voice>..</inner_voice>` 只出标签、无 speech 的形态，守卫测试用的是"全空元组"，没有专门测这个形态（`public_text=""` 但 `iv_think` 非空）。

**现状覆盖良好、不动**：单标签四态、分句保护、各类标签持有上界、围栏 CommonMark 检测细则、emoji-only 尾部合并、parts 四条交错语料、property 切分不变式（tokenizer + stream_processor 双份）。约 110 个既有用例。

## 目标

- 按 4 类缺口 + tags-only 补测试，每项作确定性回归锚点
- 给三个块缓冲加长度上界，堵住无限内存累积（唯一实现改动）
- 自写确定性 fuzz（无新依赖）：跨 chunk 等价 + **序列后状态干净**两条不变式，穷举手写抽样漏掉的组合
- 全部 `unit` marker，`make test-quick` 保持全绿

## 非目标

- 不改流式解析器的**既有行为**（除 C 的内存保护新增截断语义外）
- 不引入 hypothesis / 任何新依赖
- 不碰前端协议 / 显示模型
- 不改 13 点测试策略里已覆盖的行为

---

## A. 跨标签序列测试（手写）

**位置**：`core/text/tests/test_tokenizer.py` + `core/text/tests/test_stream_processor.py` 各加一个测试类（`test_*_sequence.py` 风格命名跟随现有文件）。

### A1. 进入→退出→再进入（每类标签 × 两次）

对每类标签，喂「完整 → 关闭 → 再完整」两次，断言最终 token/事件流与状态干净（无残留前缀、无幽灵状态）：

| 标签类 | 形态 | 两次序列示例 |
|--------|------|--------------|
| PAUSE | `<#x#>` | `<#0.5#>说<#1#>又说` |
| STAGE_CN | `（中文）`/`(中文)` | `（眨眼）说（叹气）又说` |
| STAGE_EN | `(laughs)` 等 | `(laughs)说(sighs)又说` |
| ANGLE_EN | `<pant>` | `<pant>说<pant>又说` |
| mood | `<mood>..</mood>` | `<mood>开心</mood>说<mood>累</mood>又说` |
| inner_voice | `<inner_voice>..</inner_voice>` | 两个 inner_voice → 两个 INNER_VOICE 事件 |
| fence | ```` ```lang ``` ```` | ```` ```py 代码1 ``` `` ```` + 文本 + 再进围栏 |
| code_tag | `<|code|>..</|code|>` | 同上 |

**关键断言**：第二次进入前无上一轮残留（如 fence 关闭后 `_held` 为空、`at_line_start` 正确）；事件序列为"各自完整"的预期。

### A2. 标签紧邻转换跨 chunk

闭合紧接着开启，切分点落在转换处（标签闭合字符与下一标签起始字符跨 chunk）：

- `</mood><#0.` + `5#>`（mood 闭合紧跟 PAUSE 开启）
- `</inner_voice>```` ``` ```` + `python`（inner_voice 闭合紧跟 fence 开启）
- `<#1#><mood>开` + `心</mood>`（PAUSE 紧跟 mood 开启）
- `（笑）<#` + `1#>`（STAGE_CN 闭合紧跟 PAUSE）
- `</|code|>` + `<#0.5#>`（code_tag 闭合紧跟 PAUSE）
- ```` ```py 代码 ``` `` ```` + `（继续说话）`（fence 闭合紧跟 STAGE_CN）

**关键断言**：转换处两个标签各自完整、无串扰；跨 chunk 与一次到达结果一致。

---

## B. 多代码块链路

**位置**：`core/text/tests/test_stream_processor.py`。

同一轮喂 2 个围栏（或围栏 + `<|code|>` 混用），句子交错其间：

```
第一段话。\n```python\ncode1\n```\n第二段话。\n```js\ncode2\n```\n结尾。
```

**断言**：
- `get_blocks()` 返回**按 token 顺序**两块，`after_sentence_idx` **跨块计数**正确（第二个块的 after_idx 基于包含第一个块的累计已完成句数，不是从 0 重计）
- `rebuild_public_text()` 两块内容按原文位置还原
- SENTENCE / TEXT_DELTA 事件全程正确（代码内容不进 TTS/不参与分句）
- `build_parts(all_sentences, blocks)` 多块交错正确（扩展既有 parts 交错语料到双块）

---

## C. 围栏内存保护（唯一实现改动）

**位置**：`core/text/tokenizer.py`。

### 问题

`_fence_content`（围栏）、`_code_content`（`<|code|>`）、`_markdown_content`（`<|markdown|>`）在未闭合时无限累积。

### 改法

1. 新增常量 `FENCE_MAX_CONTENT = 64 * 1024`（64KB；典型代码块 <4KB，16× 富余）。定义在 tokenizer.py 顶部常量区（与 `STAGE_M`/`STAGE_K` 同风格）。
2. 新增一个通用私有 helper（DRY，三个缓冲共用）：
   ```python
   def _append_block_content(self, attr: str, text: str) -> None:
       buf = getattr(self, attr)
       if len(buf) >= FENCE_MAX_CONTENT:
           return
       setattr(self, attr, buf + text)
   ```
3. 将 `_feed_fence_body` 内全部 7 处 `self._fence_content += X`（tokenizer.py:519,532,541,551,554,560,564）替换为 `self._append_block_content("_fence_content", X)`。
4. `_code_content`（tokenizer.py:476）与 `_markdown_content`（tokenizer.py:483）同样改走 helper（`attr` 传 `"_code_content"` / `"_markdown_content"`）。

**语义（已定稿）**：达到上限后**停止累积但继续按块状态消费**——真正的闭合仍照常 emit CODE token（内容截断在上限处），残余内容静默丢弃；**不强制回 NORMAL**（否则残余代码会泄漏进 TTS 被朗读，比截断更糟）。`_clear_fence()`/`reset()` 不变（已清空缓冲）。

**行为影响**：超上限的代码块被截断为前 64KB。合法内容几乎不可能触发（典型 <4KB）。

### 测试（`core/text/tests/test_tokenizer.py`）

- 未闭合围栏喂 >上限内容 → 内存有界（`len(_fence_content) <= FENCE_MAX_CONTENT`）+ flush 产出截断块
- 闭合围栏喂 >上限内容 → 块截断但下游正常（CODE token 内容 = 前 64KB）
- `<|code|>` / `<|markdown|>` 未闭合超上限 → 同上界
- 正常（<上限）围栏 → 行为与现状一致（回归）

---

## D. Loop 层集成边界 + tags-only

**位置**：`core/cognitive/tests/test_empty_response_guard.py` 扩展。

复用既有 `_make_loop()` stub 基建（stub `_build_turn_prompt` / `_stream_llm_response` / `_finalize_turn` / `_execute_tool_round`）。

### D1. tags-only（核心，用户确认的形态）

`<mood>开心</mood><inner_voice>我有点累</inner_voice>` 对应返回元组：`public_text=""`、`iv_think="我有点累"`、`raw_tool_calls=[]`、`mood_applied=True`。

**断言**：
- 守卫**必须**触发（`public_text.strip()` 为空，即使 `iv_think` 非空）→ nudge 重试一次
- 重试后仍 tags-only → 发兜底回复，`iv_think` 内容保留在 `_finalize_turn` 参数中
- 该测试钉死"守卫基于 `public_text` 而非 `acc_text` 判断"的设计决策（若未来改成 `acc_text` 判断会漏掉此 case）

### D2. 纯空白

`"\n\n  "` 对应返回元组（`public_text="\n\n  "`）→ 守卫触发（`strip()` 为空）→ nudge。

### D3. 纯 emoji（守卫不应触发）

`"😊"` 对应返回元组（`public_text="😊"`）→ `strip()` 非空 → 守卫**不**触发 → 直接 `_finalize_turn`（emoji 是可见回复，正确行为）。断言 `_stream_llm_response` 只调用一次、无 nudge。

### D4. 超长单句

一个 >max_buffer_length 的长句（强制切分路径）→ 正常走 `_finalize_turn`，守卫不触发。

---

## E. 自写 fuzz（无新依赖）

**位置**：新文件 `core/text/tests/test_fuzz_sequences.py`。

### 生成器

- 标签语法池（每类可随机生成闭合/未闭合形态）：
  - PAUSE `<#0.5#>`
  - STAGE_CN `（中文）`/`(中文)`
  - STAGE_EN `(laughs)`/`(sighs)`（从 `STAGE_EN_WORDS`）
  - ANGLE_EN `<pant>`
  - mood `<mood>..</mood>`
  - inner_voice `<inner_voice>..</inner_voice>`
  - fence ```` ```lang\n...\n``` ````
  - code_tag `<|code|>..</|code|>`
  - markdown_tag `<|markdown|>..</|markdown|>`
  - 普通中文语音文本（含分句标点 `。！？`）
  - emoji（`♪`/`♡`/`😊`）
- 生成：随机序列长度（0-8 项），每项 = 随机标签或语音段；**平衡序列**（开的必闭）供状态干净性测试；**任意序列**供跨 chunk 等价测试。
- 切分：随机 1-5 个切分点。
- **确定性**：`random.Random(固定种子 + 迭代序号)`，300 组，总耗时 <5s。

### 不变式 1：跨 chunk 等价（任意序列）

任意序列整串一次到达 vs 随机切分喂入 → token/事件流**完全一致**（含未闭合形态）。

### 不变式 2：序列后状态干净（平衡序列 + sentinel）

平衡序列 → 追加 sentinel 语音 `"你好世界"` → 全部喂入 → flush → 断言：
- sentinel 的语音以 SPEECH/SENTENCE 出现（**未被吞进未闭合 CODE/META**）
- 无幽灵 token（sentinel 未被误判为其他 kind）
- 整串 vs 切分结果一致（与不变式 1 合并验证）

> 设计依据：状态残留 bug 的特征是"序列本身解析正常，但污染后续输入"。平衡序列 + 尾随 sentinel 正好暴露此形态——幽灵 FENCE 会把 sentinel 吞掉。

### 断言实现

- token 级：整串 vs 切分的 `[(kind, text, meta_kind, lang)]` 列表逐一相等
- 事件级（StreamProcessor）：`StreamEvent(type, text)` 列表相等
- sentinel：输出 token 中存在 `TokenKind.SPEECH` 且 `text == "你好世界"`（或按拆词断言含 sentinel 文本）

---

## 测试策略汇总

| 组 | 位置 | 形态 |
|----|------|------|
| A1 进入退出再进入 | test_tokenizer.py / test_stream_processor.py | 手写序列 |
| A2 紧邻转换跨 chunk | 同上 | 手写序列 |
| B 多代码块链路 | test_stream_processor.py | 手写全链路 |
| C 围栏内存保护 | test_tokenizer.py | 实现改动 + 测试 |
| D Loop 层集成边界 | test_empty_response_guard.py | 手写守卫测试 |
| E fuzz | test_fuzz_sequences.py（新） | 确定性随机 300 组 |

- 全部 `pytest.mark.unit`
- 回归：`make test-quick` 全绿

## 风险

- **C 截断语义改变块行为**：仅影响 >64KB 的代码块（几乎不可能由 LLM 产出）。测试固化"<上限行为不变"。
- **fuzz 确定性**：固定种子保证可复现；若 fuzz 暴露真实 bug，先修实现再归入回归测试。
- **行为回归**：全部新测试为追加，不改既有行为（除 C）；以"现状行为对齐 + 新语义测试固化"为准。
