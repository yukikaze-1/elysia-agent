# 流式解析器边界情况覆盖补强设计

> 日期：2026-08-02
> 状态：设计定稿
> 范围：`core/text/`（tokenizer/splitter/stream_processor/parts）测试补强 + `core/text/tokenizer.py` 一处内存保护改动 + `core/cognitive/_think_loop` 守卫测试补强。

## 背景与动机

2026-08-02 修复"空 LLM 响应被静默吞掉"（`_think_loop` 空响应守卫）时，暴露出现有测试对**边界情况**的覆盖有 4 类真实缺口 + 1 个未钉死的形态：

1. **跨标签转换序列**——全仓搜不到"标签紧邻转换跨 chunk"测试（如 `</mood><#0.`、`</inner_voice>\n```` ``` ````）。这正是 2026-08-01 实际踩过的状态残留坑（fence 关闭后 `_held` 残留反引号 → 幽灵 FENCE 吞尾句）。该 bug 在重构（f86b94d）中修复并附带回归测试（`test_fence_closed_then_more_text_*`），但**组合序列**（进入→退出→再进入、紧邻转换）的系统化覆盖仍未做。
2. **同一轮多个代码块**——`get_blocks()` 的 `after_sentence_idx` 跨块计数、多块顺序、与 parts 交错，只有 parts 层孤立测试（`test_two_blocks_same_slot`），tokenizer→splitter→get_blocks 全链路无多块测试。
3. **无界缓冲（内存隐患）**——块缓冲 `_fence_content` / `_code_content` / `_markdown_content` 及 `_meta_content` / `_fence_infostr` / `_fence_pending` / `_pause_buf` 均无限累积。注意：背景"标签扫描都有持有上界"不准确——tokenizer.py:14 自述 **PAUSE 内容无上界**（`<#111…` 长数字串持续命中 `PAUSE_TAIL_RE`），`_meta_content`（未闭合 `<mood>`）、`_fence_infostr`（围栏首行无 `\n`）同样无界。LLM 输出永不开合的 ```` ``` ```` / `<|code|>` 或长无界前缀会无限吃内存。
4. **Loop 层集成边界**——空/纯空白/纯标签/纯 emoji 响应在 `_think_loop` 层的守卫行为未钉死。
5. **tags-only 未钉死**——`<mood>..</mood><inner_voice>..</inner_voice>` 只出标签、无 speech 的形态，守卫测试用的是"全空元组"，没有专门测这个形态（`public_text=""` 但 `iv_think` 非空）。

**现状覆盖良好、不动**：单标签四态、分句保护、STAGE/ANGLE 持有上界、围栏 CommonMark 检测细则、emoji-only 尾部合并、parts 四条交错语料、property 切分不变式（tokenizer + stream_processor 双份）。既有 221 个用例。

## 目标

- 按 4 类缺口 + tags-only 补测试，每项作确定性回归锚点
- 给无界缓冲（块内容/元内容/pending/infostr/pause）加长度上界，堵住无限内存累积（唯一实现改动）
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
| fence | ```` ```lang\n…\n``` ````（多行，行首开启，尾部 `\n` 闭合） | `第一段。\n```` ```py\n代码1\n``` ````\n又说。\n```` ```js\n代码2\n``` ````\n结尾。` |
| code_tag | `<|code|>..</|code|>` | 同上 |

> **围栏语法约束**：围栏开启要求 `_at_line_start`（tokenizer.py:248），闭合要求**收尾行尾部的 `\n`**（tokenizer.py:510-516）。因此围栏示例**必须**用多行形式（内容在 `\n` 之后的行、收尾围栏行以 `\n` 结束）；单行 `` ```` ```py 代码1 ``` ```` `` 会在 info 态把整行吞进 `_fence_infostr`，flush 时整体丢弃，测不到围栏语义。

**关键断言**：第二次进入前无上一轮残留（如 fence 关闭后 `_held` 为空、`at_line_start` 正确）；事件序列为"各自完整"的预期。

> **PAUSE 行与既有 `test_inline_pause_not_cut` 部分重叠**：A1 PAUSE 的价值在"两次 pause 的 re-enter 序列输出"（`<#0.5#>说<#1#>又说`），单 pause 内嵌已由既有测试覆盖。PAUSE 不持有状态，A1 的 re-enter 断言对 PAUSE 是低价值重复，可省略该行或仅保留序列输出断言。

### A2. 标签紧邻转换跨 chunk

闭合紧接着开启，切分点落在转换处（标签闭合字符与下一标签起始字符跨 chunk）：

- `</mood><#0.` + `5#>`（mood 闭合紧跟 PAUSE 开启）
- `</inner_voice>\n```` ``` ```` + `python\n…\n```` ``` ````（inner_voice 闭合 → `\n` 置行首 → fence 开启；`\n` 必须作为独立 chunk 边界测试）
- `<#1#><mood>开` + `心</mood>`（PAUSE 紧跟 mood 开启）
- `（笑）<#` + `1#>`（STAGE_CN 闭合紧跟 PAUSE）
- `</|code|>` + `<#0.5#>`（code_tag 闭合紧跟 PAUSE）
- `第一句。\n```` ```py\ncode\n``` ```` + `\n（继续说话）`（fence 闭合 → `\n` → 下一句行首紧跟 STAGE_CN）

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

## C. 无界缓冲加固（唯一实现改动）

**位置**：`core/text/tokenizer.py`。

### 问题

七个累积缓冲均无限界。LLM 输出长无界前缀（永不开合的 ```` ``` ```` / `<|code|>` / `<mood>`、长数字串 PAUSE、无 `\n` 的围栏首行）会无限吃内存。

### 关键约束：闭合检测机制决定截断策略

tokenizer 有两类闭合检测，截断策略必须与之兼容：

| 闭合检测 | 缓冲 | 含义 |
|----------|------|------|
| **结构化**（换行 + 反引号串、正则 break、闭合字符）| `_fence_content`、`_fence_pending`、`_pause_buf` | 闭合与内容缓冲无关，可安全"截断" |
| **`endswith(close)`**（tokenizer.py:470/477/484）| `_meta_content`、`_code_content`、`_markdown_content` | 闭合靠**缓冲尾部**匹配闭合标签——**若截断保头，闭合标签永远进不了缓冲 → 永不闭合 → flush 整块丢弃，比现状更糟** |

**因此块缓冲必须"前截断保尾"**（保留最近 N 字符，闭合标签在尾部存活，`endswith` 仍命中），**不能"停止累积保头"**。

### 改法

1. **常量**（tokenizer.py 顶部常量区，与 `STAGE_M`/`STAGE_K` 同风格）：
   - `FENCE_MAX_CONTENT = 64 * 1024`——块/元内容缓冲上限。**注意是码点数**（`len()` 对 str 计数码点，非字节）。典型代码块 <4KB，16× 富余。
   - `FENCE_INFO_MAX = 256`——围栏信息串（首行）上限。
   - `PAUSE_MAX = 32`——PAUSE 数字串上限。

2. **两个 helper，按闭合检测机制分流**：
   ```python
   # 结构化闭合缓冲（_fence_content/_fence_pending）——保头，保留代码开头（函数头）
   def _append_head_bounded(self, attr: str, text: str) -> None:
       buf = getattr(self, attr) + text
       if len(buf) > FENCE_MAX_CONTENT:
           buf = buf[:FENCE_MAX_CONTENT]     # 保头截断
       setattr(self, attr, buf)

   # endswith 闭合缓冲（_code_content/_markdown_content/_meta_content）——保尾，
   # 闭合标签靠尾部存活，截断保头会让 endswith 永不命中 → 整块丢弃（比现状更糟）
   def _append_tail_bounded(self, attr: str, text: str) -> None:
       buf = getattr(self, attr) + text
       if len(buf) > FENCE_MAX_CONTENT:
           buf = buf[-FENCE_MAX_CONTENT:]    # 保尾截断
       setattr(self, attr, buf)
   ```
   应用：`_fence_content` 走保头（tokenizer.py:519,532,541,551,554,560,564 共 7 处）、`_code_content` 走保尾（:476）、`_markdown_content` 走保尾（:483）、`_meta_content` 走保尾（:468）。

3. **`_fence_pending`**（:508/526 累积空白/反引号）——走 `_append_head_bounded`（保头；`_fence_run_len` 是独立计数器，不依赖 pending 内容，结构化闭合不受影响）。

4. **`_fence_infostr`**（:499）——**保头**（`_fence_lang()` 取首段 token）：达 `FENCE_INFO_MAX` 后停止累积（首段保留）。

5. **`_pause_buf`**（:399）——**不能截断**（`PAUSE_RE.fullmatch(candidate)` 依赖 `<#` 前缀）：达 `PAUSE_MAX` 后按非数字字符处理——回落 SPEECH 释放。

**语义**：超上限的闭合块 emit 截断内容——**围栏块保头 = 前 FENCE_MAX_CONTENT 字符**（保留函数头等开头，闭合检测不读内容故不受影响）；**tag 块保尾 = 最近 FENCE_MAX_CONTENT 字符**（闭合标签存活，endswith 仍命中）。**不强制回 NORMAL**（否则残余代码会泄漏进 TTS 被朗读，比截断更糟）。`_clear_fence()`/`reset()` 不变（已清空缓冲）。

**实现要求**：helper 与各截断点必须加中文注释，说明保头/保尾的取舍依据（结构化闭合可保头；endswith 闭合必须保尾），对齐项目注释规范（CLAUDE.md 规则 2）。

**行为影响（新行为，已登记 08-01 行为表 #12）**：>64KB 的闭合围栏块截断为前 64KB、tag 块截断为最近 64KB；未闭合超限块仍整体丢弃（同现状 #11），仅内存有界。合法内容（典型 <4KB）零影响。

### 测试（`core/text/tests/test_tokenizer.py`）

- **闭合围栏**喂 >上限内容 → 内存有界 + CODE token 内容 = **前 64KB**（保头截断）
- **闭合 `<|code|>` / `<|markdown|>`** 喂 >上限内容 → **闭合仍被检测**（endswith 靠保尾存活）+ emit 截断块 = **最近 64KB**（关键：验证不落入"永不闭合丢弃"）
- **未闭合**围栏 / code_tag / markdown 喂 >上限内容 → 内存有界 + flush 整体丢弃（对齐 #11）
- 未闭合 `<mood>` 喂 >上限内容 → `_meta_content` 有界（保尾截断）+ flush 不发事件（对齐 #7）
- 长数字串 `<#111…`（>PAUSE_MAX）→ 回落 SPEECH
- 长围栏首行无 `\n`（>FENCE_INFO_MAX）→ 信息串截断 + lang 保留首段
- **正常（<上限）** 全部 → 行为与现状一致（回归）
- 长数字串 `<#111…`（>PAUSE_MAX）→ 回落 SPEECH
- 长围栏首行无 `\n`（>FENCE_INFO_MAX）→ 信息串截断 + lang 保留首段
- **正常（<上限）** 全部 → 行为与现状一致（回归）

---

## D. Loop 层集成边界 + tags-only

**位置**：`core/cognitive/tests/test_empty_response_guard.py` 扩展。

复用既有 `_make_loop()` stub 基建（stub `_build_turn_prompt` / `_stream_llm_response` / `_finalize_turn` / `_execute_tool_round`）。

### D1. tags-only（核心，用户确认的形态）

**返回元组契约**：`_stream_llm_response` 返回 **7 元组** `(acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, blocks, public_text)`（loop.py:804-806，现有测试已固定正确形态 test_empty_response_guard.py:46-48）。`mood_applied` 是**实例属性**（loop.py:944），**不在返回元组里**——守卫也只读 `raw_tool_calls` + `public_text`，不读 `mood_applied`。

tags-only 的 stub 返回元组：`("<mood>开心</mood><inner_voice>我有点累</inner_voice>", [], False, [], "我有点累", [], "")`——**`acc_text` 是含标签的原始输出**（loop.py:425 "LLM 原始输出全文（含标签）"），`public_text=""`、`iv_think="我有点累"`、`raw_tool_calls=[]`。

**断言**：
- 守卫**必须**触发（`public_text.strip()` 为空，即使 `iv_think` 非空）→ nudge 重试一次
- 重试后仍 tags-only → 发兜底回复，`iv_think` 内容保留在 `_finalize_turn` 参数中
- **钉死守卫判定依据**：`acc_text` **非空**（含标签）但守卫仍触发 → 证明守卫基于 `public_text` 而非 `acc_text`。若未来误改成 `acc_text.strip()` 判断，此测试立即失败（acc_text 非空 → 守卫不触发 → 断言落空）。若 stub 用 `acc_text=""` 则两种守卫都触发，无法区分，故此处必须用含标签的非空 `acc_text`。

### D2. 纯空白

stub 返回元组 `("", [], False, [], "", [], "\n\n  ")`（`public_text="\n\n  "`）→ 守卫触发（`strip()` 为空）→ nudge。

### D3. 纯 emoji（守卫不应触发）

stub 返回元组 `("😊", [], False, ["😊"], "", [], "😊")`（`public_text="😊"`）→ `strip()` 非空 → 守卫**不**触发 → 直接 `_finalize_turn`（emoji 是可见回复，正确行为）。断言 `_stream_llm_response` 只调用一次、无 nudge。

### D4. 删除（原本无效）

~~超长单句强制切分~~：`max_buffer_length`/`_forced_split` 在真实 `_stream_llm_response` 内部（loop.py:451、splitter.py:241-262），stub 下 D4 只是重复 `test_normal_response_not_retried`。强制切分路径已由既有 splitter 测试覆盖（`test_forced_split_*`，test_splitter.py:273/279/285），loop 层不需要重复。

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

平衡序列 → 追加 **sentinel = `"\n你好世界"`**（前导 `\n` 是硬分隔符，保证 sentinel 独立成句）→ 全部喂入 → flush → 断言：
- 输出 token 中存在 `TokenKind.SPEECH` 且 **`text == "你好世界"` 精确相等**（未被吞进未闭合 CODE/META、未被并入前一句、未被误判为其他 kind）
- 整串 vs 切分结果一致（与不变式 1 合并验证）

> 设计依据：状态残留 bug 的特征是"序列本身解析正常，但污染后续输入"。平衡序列 + 尾随 sentinel 正好暴露此形态——幽灵 FENCE 会把 sentinel 吞掉。
> **sentinel 断言必须精确匹配**：前导 `\n` 强制独立成句，使 `text == "你好世界"` 成立（`\n` 自身消费为独立 SPEECH token，`你好世界` 紧随其后为完整 SPEECH token）；若不带 `\n`，sentinel 可能并入前一句（如 `又说你好世界`），精确相等会假失败；若退化成"含 sentinel 文本"子串断言，在残留污染时又可能假通过（sentinel 被吞进 CODE 但子串仍存在）。

### 断言实现

- token 级：整串 vs 切分的 `[(kind, text, meta_kind, lang)]` 列表逐一相等
- 事件级（StreamProcessor）：`StreamEvent(type, text)` 列表相等
- sentinel：`TokenKind.SPEECH` 且 `text == "你好世界"`（精确相等）

---

## 测试策略汇总

| 组 | 位置 | 形态 |
|----|------|------|
| A1 进入退出再进入 | test_tokenizer.py / test_stream_processor.py | 手写序列 |
| A2 紧邻转换跨 chunk | 同上 | 手写序列 |
| B 多代码块链路 | test_stream_processor.py | 手写全链路 |
| C 无界缓冲加固 | test_tokenizer.py | 实现改动 + 测试 |
| D1-D3 Loop 层集成边界 | test_empty_response_guard.py | 手写守卫测试 |
| E fuzz | test_fuzz_sequences.py（新） | 确定性随机 300 组 |

- 全部 `pytest.mark.unit`
- 回归：`make test-quick` 全绿

## 风险

- **C 加固改变缓冲行为**：>64KB 的闭合块截断为最近 64KB（几乎不可能由 LLM 产出）、>32 的数字串 PAUSE 回落 SPEECH、>256 的围栏信息串截断——均为病理输入。测试固化"<上限行为与现状一致"。
- **fuzz 确定性**：固定种子保证可复现；若 fuzz 暴露真实 bug，先修实现再归入回归测试。
- **行为回归**：全部新测试为追加，不改既有行为（除 C）；以"现状行为对齐 + 新语义测试固化"为准。
