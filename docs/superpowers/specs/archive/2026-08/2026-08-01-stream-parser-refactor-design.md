# 结构化流式解析器重构设计（StreamParser）

> 日期：2026-08-01（修订版 v3，依据 review 补充行为对照表 + 完整迁移面）
> 状态：设计定稿，待实现
> 范围：纯后端。前端协议与显示模型不变。

## 背景与动机

当前流式解析是分散的多段管道 + 多处打补丁：

```
LLM delta → StreamProcessor（mood/inner_voice 状态机）
          → BlockFilter（过滤 <|markdown|>/<|code|> 块）
          → SentenceBuffer（标点分句）→ SENTENCE / TEXT_DELTA 事件
```

已确认的问题：

1. **`<#x#>` TTS 停顿标签会被切碎**（已多次实际发生）。分句器在含标签字符的原始文本上做边界扫描，`<#0.5#>` 靠 `_is_dot_boundary` 的数字保护才勉强存活。
2. **标签逻辑碎片化**：PAUSE 正则重复定义两份（`core/cognitive/stream_processor.py:19,46` + `core/adapter/tts_adapter.py:53,263`）；STAGE 正则埋在 tts_adapter 显示路径内部；`_TONE_TAG_RE`（tts_adapter.py:55）定义后从未使用，是死代码。
3. **现状存在多处解析缺陷**（详见"行为对照表"）：```` ``` ```` 围栏内容被 TTS 朗读、句中 `<mood>` / 重复 `<inner_voice>` 原样泄漏进语音流。
4. **未来标签扩展无机制**：`[action]`、`<<x>>`、新代码块格式需要结构性支持。

## 目标

- 根除"标签被切碎"类 bug：**标签是原子 token，分句边界扫描永远跳过标签内部**
- 统一全部标签定义（识别 / 路由 / 错误处理收拢到 `core/text/tags.py`）
- 提供标签注册表机制，未来加标签 = 加一行注册，核心零改动
- **事件面与显示路径完全兼容**（MOOD/INNER_VOICE/SENTENCE/TEXT_DELTA/TTFT 不变，无新增事件类型）
  ——⚠️ **"完全兼容"在事件粒度上曾不成立**（句末标点未落盘 ⇒ 无空格中文回复整轮堆到 EOF、
  TTFT 失真），见文末「修订记录」2026-09-16 条；真实规则与守卫以该条为准
- 修复现状解析缺陷（见行为对照表，每项都附测试固化）
- 代码块正确分拣：不进 TTS、不参与分句；显示仍走 finalize 的 `block` 命令（现状）

## 行为对照表（现状 vs 本次设计）

**本表是唯一的行为基准**。执行时以"现状"行为为准逐条对齐；"修复"行为为本次有意变更，必须按设计实现且附测试，不得还原成现状。

| # | 行为点 | 现状（实测） | 本次设计 | 性质 |
|---|--------|--------------|----------|------|
| 1 | `<#x#>` 完整性 | 靠数字保护勉强存活，`<#0.5#>` 曾实际被切碎 | 原子 token 保护，跨 chunk 不变式 | 修复 |
| 2 | ```` ```lang ```` 围栏 | 无识别，内容作为普通文本进 SENTENCE 被 TTS 朗读、内联显示 | 识别为 CODE（仅行首），不进 TTS、不参与分句，finalize parts | **修复（新行为）** |
| 3 | 句中 `<mood>` | 原样泄漏进 SENTENCE / TTS / 显示（只有句首 mood 被剥离）| 任意位置剥离为 META，不冲刷分句缓冲 | **修复（新行为）** |
| 4 | 重复 `<inner_voice>` | 只解析第一个，第二个起原样泄漏；session.inner_voice 只存最后一个（loop.py:925 覆盖）；public_text 剥离 find 第一个（loop.py:676-685）→ 第二个泄漏进 analyze_assistant / log_conversation / after_response | 每个都发 INNER_VOICE 事件；**session.inner_voice 合并（换行连接）；public_text 改由 token 流重建（无 META 泄漏）** | **修复（新行为）** |
| 5 | flush 尾部 PAUSE | 剥离（`_strip_pause_tags` 清掉残留 `<#x#>`）| 残留 PAUSE 保留在尾句（尾句无语音则丢弃）| **修复（新行为）** |
| 6 | `（中文指示）` 半角/全角 | `[\(\（]…CJK…[\)\）]` 半角+全角均 TTS 剥离 | 保持（STAGE_CN 模式含半角+全角）| 现状 |
| 6a | STAGE_CN 含内部标点 | `（我有点，犹豫。）` 被 `。` 切分 → `_CN_ACTION_RE` 闭口失配 → 残片被 TTS 朗读 | span 保护整体不切分，完整剥离。**适用范围：span ≤ M（64）且 CJK 在 K（16）内**——超窗回落 SPEECH 时按普通文本泄漏（罕见，长舞台指示；现状 `_CN_ACTION_RE` 无长度限制，属已知取舍）| **修复** |
| 7 | 未闭合 `<mood>` | 残留丢弃、不发事件 → `_mood_applied=False` → 关键词 fallback 触发（loop.py:669）| 同现状 | 现状 |
| 8 | 未闭合 `<inner_voice>` | THINK 态残留广播为 INNER_VOICE 事件 | 同现状 | 现状 |
| 9 | STAGE_CN 显示 | TTS 模式：句首独立行 + 句中内嵌（tts_adapter 逐句提取）；文本模式：内嵌 | 同现状（逐句提取留在显示路径）| 现状 |
| 10 | `(laughs)` 英文语气 | 显示 + TTS 均保留 | **显示剥离、TTS 保留**（圆括号语气词识别为 STAGE_EN token，TTS 音效用途）| **修复** |
| 10a | `<pant>` 尖括号语气词 | 无识别，随 SPEECH 流过 | 识别为 ANGLE_EN token，**显示 + TTS 全丢弃**（F3 明令禁止尖括号写法，解析器兜底）| **修复** |
| 10b | `♪`/`♡` 句尾 | 不作为句子边界，整句连到后续标点 | 加入句分隔符（句子以 `♪`/`♡` 结尾，`♪`/`♡` 保留在句内）| **修复** |
| 11 | 未闭合 CODE/MARKDOWN | BlockFilter.flush 整体丢弃，不进 TTS | 同现状 | 现状 |
| 12 | >64KB 代码块截断 | 无上界（闭合时 emit 全量内容） | **围栏块保头截断为前 64KB、tag 块（`<|code|>`/`<|markdown|>`/`<mood>`）保尾截断为最近 64KB**（堵无界缓冲内存隐患；2026-08-02 边界覆盖设计追加，实现要求见该设计 C 节）| **修复（新行为）** |
| 13 | PAUSE 数字串超窗 | 内容无上界（`<#111…` 长数字串持续命中 `PAUSE_TAIL_RE` 持有） | 超 `PAUSE_MAX`(32) 回落 SPEECH（长数字串不是真实 pause，按非数字字符处理）| **修复（新行为）** |
| 14 | 围栏信息串超窗 | 无上界（首行无 `\n` 时 `_fence_infostr` 无限累积） | 超 `FENCE_INFO_MAX`(256) 截断（lang 取首段 token，保头保留）| **修复（新行为）** |

## 非目标（Out of Scope）

- 前端协议 / ContentPart / chatStore 显示模型变更
- 代码块流式补显示——**否决**：前端 appendDelta 追加到最后一个 assistant 气泡（chatStore.ts:57-63），`block` 命令是 finalize 时插入新气泡；流式中途发 `block` 会把后续文本追加进代码气泡，纯后端方案下不可行。显示保持"回合末 block 命令"（现状）
- `[action]` / `<<x>>` 的语义处理（仅预留注册表扩展位，不实现 matcher）
- 其他模块重构

## 需求

### 标签清单

**当前（必须实现）：**

| 标签 | 示例 | 语义 | 分句保护 | 显示 | TTS |
|------|------|------|----------|------|-----|
| `<#x#>` | `<#0.5#>` | TTS 停顿 | ✅ 整体 | 剥离 | 保留 |
| `（…中文…）` / `(…中文…)` | `（眨眼）`、`(眨眼)` | 中文舞台指示（半角+全角）| ✅ 整体 | 句首独立行 / 句中内嵌（现状）| 剥离 |
| `(laughs)` 等英文语气 | `(sighs)`、`(clears throat)` | 英文语气词（圆括号，半角+全角）| ✅ 整体 | 剥离 | 保留（TTS 音效）|
| `<pant>` 等尖括号语气词 | `<pant>` | F3 违规格式兜底 | ✅ 整体 | 剥离 | 剥离（全丢弃）|
| `<mood>..</mood>` | `<mood>开心|0.7</mood>` | 心情 → MOOD 事件（任意位置）| 带外 | — | — |
| `<inner_voice>..</inner_voice>` | — | 内心独白 → INNER_VOICE 事件（可多个）| 带外 | — | — |
| ```` ```lang ```` 围栏（仅行首）| ```` ```python ```` | 代码块 → finalize parts（**修复：现状被 TTS 朗读**）| 带外 | finalize block 命令 | — |
| `<\|code\|lang>..</\|code\|>` | `<\|code\|python>` | 代码块 → finalize parts | 带外 | finalize block 命令 | — |
| `<\|markdown\|>..</\|markdown\|>` | — | Markdown 块 → finalize parts | 带外 | finalize block 命令 | — |

**未来（仅预留扩展位）：**

| 标签 | 语义 |
|------|------|
| `[action]` | 动作元数据 → 未来 ACTION 事件 |
| `<<x>>` | 环境信息更新 → 未来 ENV 事件 |

### 关键失败 case（回归测试来源）

- `<#0.5#>好的，等一下<#1#>然后说` 流式到达 → `<#x#>` 必须整体进入 SENTENCE（TTS），绝不切开
- `（我有点，犹豫。）` 含标点舞台指示 → 不被切分
- `(眨眼)` 半角舞台指示 → 从 TTS 剥离（对齐现状）
- `价格<100元` 字面 `<` → 不构成标签，不被扣住、不误判
- 任意标签跨 delta chunk 边界（如 `<#0.` 到达后 `5#>` 下一个 chunk）→ 结果与一次到达一致
- 未闭合 `<mood>`（LLM 残缺输出）→ 不发 MOOD 事件 → 关键词 fallback 仍触发
- 句中 `<mood>开心</mood>` → 剥离为 META，前后语音文本连续（**修复：现状泄漏**）
- 两个 `<inner_voice>` → 发两个 INNER_VOICE 事件（**修复：现状只解析第一个**）
- ```` ```python ```` 围栏 → 内容不进 SENTENCE/TTS（**修复：现状被朗读**）

### 核心不变式

**对任意输入串 S，在任意字符边界切分为 delta 序列，解析出的 token 流 / 事件序列必须与 S 一次到达时一致。**

## 架构

### Token 模型

流被切成两类 token：

**行内 token（属于语音流，保留位置，分句保护）：**

| Token | 识别 | SENTENCE (TTS) | TEXT_DELTA (显示) |
|-------|------|----------------|-------------------|
| `SPEECH` | 普通文本 | ✅ 保留 | ✅ 保留 |
| `PAUSE` | `<#x#>` | ✅ 嵌入句中 | ❌ 剥离 |
| `STAGE_CN` | `[\(\（]…中文…[\)\）]`（半角+全角，含 CJK；**半角 `(` 同入触发集**，与全角统一保护）| ❌ 剥离（tts_adapter 逐句提取）| ✅ 句首独立行 / 句中内嵌（现状）|
| `STAGE_EN` | `(sighs)` / `(clears throat)`（圆括号英文语气词，半角+全角，内容归一后命中 `STAGE_EN_WORDS`）| ✅ 嵌入句中（保留给 TTS 音效）| ❌ 剥离 |
| `ANGLE_EN` | `<pant>`（尖括号英文语气词，F3 违规格式兜底）| ❌ 丢弃 | ❌ 剥离 |

`(laughs)` 等英文语气是**特殊 token（STAGE_EN）**——显示剥离、TTS 保留（`#10` 修复，反转旧「显示+TTS 均保留」）。尖括号违规格式 `<pant>` 识别为 `ANGLE_EN`，显示 + TTS 全丢弃（`#10a`）。tags.py 的 `STAGE_EN_WORDS` 同时是两类识别的词表基准。

**带外 token（非语音，替换语音流，不参与分句）：**

| Token | 识别 | 路由 |
|-------|------|------|
| `CODE` | ```` ```lang ```` 围栏（仅行首，允许 ≤3 空格缩进）、`<\|code\|lang>` | 不产生流式事件；收集供 finalize 使用。**除收尾围栏判据外，块内容不触发任何嵌套标签解析**——代码里的 `<mood>`/`<inner_voice>`/行中 ```` ``` ```` 一律视为普通内容（行首 ```` ``` ```` 按 CommonMark 判据即收尾，见围栏细则）|
| `MARKDOWN` | `<\|markdown\|>` | 同上（除结束标记外不触发嵌套解析）|
| `META` | `<mood>`、`<inner_voice>`（未来 `[action]`、`<<x>>`）| 闭合时发 `MOOD` / `INNER_VOICE` 事件（未来 `ACTION` / `ENV`）|

**META 不冲刷分句缓冲**：`你好<mood>开心</mood>世界` → 语音文本跨 META 连续累积，SENTENCE 序列与 `你好世界` 一致（**修复：现状句中 mood 泄漏**）。META 的剥离与位置无关（**修复：现状只剥离句首 mood**）。

### Tokenizer 受保护扫描的持有上界

**原则：任何受保护扫描都不得无限扣住文本。** 三类扫描各自规定有限窗口：

1. **标签起始前缀（`<#`/`<m`/`<i`/`<|`）**：按前缀匹配持有——若缓冲尾部全部字符都可能是某个起始模式的前缀 → 继续持有；一旦出现不可能构成任何起始模式的字符 → 立即释放为 SPEECH 文本（仅保留尾部可能构成标签的少量字符，对齐现状 `safe_len` 语义）。例：`价格<100元` 中 `<` 后是数字 `1`，不属于任何标签前缀 → 放行，文本正常流式输出。
2. **PAUSE（`<#…#>`）**：内容为 `\d+\.?\d*` 数字串，无上界长度，固定前缀长度 N 框不住。持有由**尾串正则**驱动：持有规则 = 缓冲尾部匹配 `<#[\d.]*$`（或 `[\d.]` 序列），一旦出现非数字/点的字符（如 `元`）→ 释放为 SPEECH（尾部残留的 `<#` 前缀字符一并释放）。行为等效于现状 `_strip_tail_pause_tags` 的互补逻辑。
3. **STAGE_CN（`（…中文…）`）**：内容 `[^\)\）]*` 可匹配任意字符，**不存在"不可能构成起始模式的字符"**，必须用**计数窗口**限制持有：
   - **K 判定时机（关键，两条路径结果相反）**：CJK 检查在**闭合时对完整内容判定**——内容含 CJK → STAGE_CN；不含 CJK → 查英文语气词（命中 → STAGE_EN，否则回落 SPEECH）。扫描中若**已累积 K 字符仍未闭合且内容无 CJK** → 提前回落 SPEECH。三个条件：闭合时查内容、超 K 未闭合**且无 CJK** 提前回落、超 M 未闭合（无论 CJK）回落。**K 是英文早退窗（无 CJK 短括号不必等到 M）、M 是 CJK 上限**——含 CJK 的未闭口括号持有到 M 才回落（#6a「span ≤ M 且 CJK ≤ K」因此成立，K 窗不截断 CJK 舞台指示）。**禁止**"逐字符持有到第 K 字符才判定"——那会让短英文括号如 `(laughs)`（8 字符 < K）在闭合时被误判为 STAGE_CN 而从 TTS 剥离（违反行为表 #10）
   - **M 字符内未见 `）`（或 `)`）→ 回落 SPEECH**（已持有的文本按普通文本输出）
   - M / K 为可配置常量，建议 M=64、K=16
   - 例：`（注意：以上结论来自分析…长文本` 无闭口 → M 字符后整体回落为 SPEECH，随流输出，不延迟
   - 例：`(laughs)` → 闭合时内容无 CJK 且为英文语气词 → STAGE_EN → 显示剥离、保留进 TTS（行为表 #10 修复）
   - 未闭合且已回落的内容，保持为普通文本显示（对齐现状：不完整括号按文本显示）

### 组件（全部位于 `core/text/`）

```
core/text/
├── tags.py            # 标签注册表：每类 = 起始模式 + 结束模式 + kind + 路由
│                      #   含 PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部定义
│                      #   含 _EMOJI_RE / is_emoji_only（收口现状双份：stream_processor.py:23 + tts_adapter.py:31）
│                      #   tts_adapter 从这里导入共享定义（删本地正则 + 死代码 _TONE_TAG_RE）
│                      #   加 [action] / <<x>> / 新围栏 = 加一行注册
├── tokenizer.py       # StreamTokenizer：增量输入 → 只吐完整 token；持有上界见上
├── splitter.py        # SpeechSplitter：消费 SPEECH/PAUSE/STAGE_CN token → SENTENCE + TEXT_DELTA
│                      #   边界扫描跳过受保护 span；吸收原 SentenceBuffer 标点逻辑
├── parts.py           # 从 token 流构建 TextPart + content_type（替代 build_parts_with_blocks 的正则重解析，
│                      #   输出形状 TextPart + content_type 不变，session 存储/归档不受影响）
├── stream_processor.py # StreamProcessor：协调器（tokenizer + splitter + 事件发射 + get_blocks()）
│                      #   吸收原状态机 + BlockFilter + TTFT/emoji 逻辑
├── sentence_buffer.py  # 原文件 —— 被 splitter 吸收后删除
├── block_filter.py     # 原文件 —— 被 tokenizer + parts.py 吸收后删除
└── text_utils.py       # 不变
```

- `core/cognitive/stream_processor.py` 删除，`core/cognitive/__init__.py` 的 re-export（`StreamProcessor`/`StreamEvent`/`StreamEventType`）改从 `core.text` 导入
- `core/cognitive/loop.py`：改 import + finalize 改用 `parts.py`（见迁移计划）
- `core/adapter/tts_adapter.py`：删除本地 `_PAUSE_TAG_RE`/`_CN_ACTION_RE`/死代码 `_TONE_TAG_RE`，改用 `core.text.tags` 共享定义；**逐句提取逻辑（句首 STAGE_CN 独立行、TTS 剥离）保留原位**——它属于显示路径
- 仅依赖 `core.schema`（TextPart 等共享类型），无其他外部依赖

### 事件面（不变，无新增类型）

```
StreamEventType = MOOD | INNER_VOICE | SENTENCE | TEXT_DELTA | TTFT   ← 现状，保持
StreamEvent(type, text)
```

- SENTENCE 负载 = 语音文本 + 行内 PAUSE/STAGE_CN（按原序内嵌），供 TTS 管理器逐句提取显示/合成文本（现状机制）
- 代码块不产生流式事件；`StreamProcessor.get_blocks() -> list[(kind, content, lang, after_sentence_idx)]`（按 token 顺序），`after_sentence_idx` = **该块闭合时 splitter 已"完成"的句子数——句边界（分隔符）已消费的句子计数，含因 min_chunk 暂留的短句，且含 max_buffer_length 强制切分发出的句子**（强制切分句同样进入 all_sentences，若只按边界计数会导致其后的块错插到该句之前——实现与 parts.py 必须以"已发出的 all_sentences 计数"为准，而非严格边界计数）。单位无歧义（字符偏移会因 SENTENCE 含内嵌 PAUSE/STAGE_CN 与语音字符数不一致而错位）。交错语义（parts.py 按此执行）：
  - 块插在**第 after_sentence_idx 个已完成句子之后**
  - **为什么按"完成"而非"已发出"计数**：`好。\n` + 围栏中，"好。"（2 字符 < min_chunk=5）因"后面没有文本"暂留未发出（sentence_buffer `_extract` 等待分支）——若按已发出计数 = 0，块被插到"好。"之前，与旧 build_parts_with_blocks（字符串匹配"好。"成功 → 块在句后）顺序相反。`比如：\n`（3 字符）、`。\n`（4 字符）等 AI 常用短导语全部触发此问题。句边界（`。`/`\n`）已消费即完成，计数 = 1 → 块插在"好。"之后。（`代码如下：` = 5 字符 ≥ min_chunk，一到 `\n` 正常发出，不触发暂留，不算此类）
  - 块闭合时仍在缓冲中、**边界未消费**的句子（句中块，如 `看这个<|code|>x=1</|code|>示例`）→ 块落在该句**之前**（与旧 build_parts_with_blocks 一致：句首段匹配失败、句子落块后）
  - 行首围栏块必在句边界（`\n` 已完成上一句）→ 插在该句之后
  - 短句暂留不会丢：块后文本到达或 flush 时 rest 非空触发释放，完成计数不变、该短句随后发出，位置恒等于完成计数
- **finalize 的 block 命令从 get_blocks() 取值**（`block_type`/`content`/`language`）——顺带修复现状语言标签丢失（loop.py:739 `getattr(..., "language", "")` 对 TextPart 恒为 ""）。`parts.py` 仅服务 session 存储，不参与显示推送

## 数据流

```
LLM delta → StreamProcessor.feed(delta)
              │
        tokenizer.feed(delta) → token 流（跨 chunk 缓冲，只吐完整 token）
              │ 按 kind 路由
   ┌──────────┼───────────────┬──────────────┐
 SPEECH     PAUSE/STAGE_CN   CODE/MARKDOWN    META
   │          │               │（收集，不冲刷）│闭合时
   ▼          ▼               ▼              ▼
   └────────→ splitter（分句 + 保护）      MOOD/INNER_VOICE 事件
                │  │
             SENTENCE  TEXT_DELTA
           （含内嵌标签） （剥离 PAUSE）
                │
        tts_adapter 逐句提取：
        显示 = 句首 STAGE_CN 独立行 + 语音 + 句中 STAGE_CN
        TTS 文本 = 语音 + PAUSE + (laughs)，剥离 STAGE_CN
```

`flush()`：处理残留（尾句含残留内嵌标签、未闭合标签按错误处理表、emoji-only 尾部合并到最后一个 SENTENCE，沿用现状语义）。`flush()` 后 `get_blocks()` 返回本轮全部代码块（按 token 顺序，含语言标记）。

## 错误处理

| 场景 | 策略 | 依据（行为对照表 #）|
|------|------|------|
| 跨 chunk | tokenizer 缓冲半截标签，只吐完整 token；持有上界见架构节 | 核心不变式 |
| 未闭合 `<mood>` | 残留丢弃，**不发 MOOD 事件** | #7 现状：保 `_mood_applied=False` → 关键词 fallback（loop.py:669）|
| 未闭合 `<inner_voice>` | 残留广播 INNER_VOICE 事件 | #8 现状 |
| 未闭合 CODE/MARKDOWN | 整体丢弃，不进 TTS / parts | #11 现状 |
| 未闭合 PAUSE | 剥离（`<#0.5` 半截不输出）| 现状 `_strip_tail_pause_tags` |
| 未闭合 STAGE_CN | 回落为普通文本（保留显示）| 现状：不完整括号按文本显示 |
| 未知模式 | 回落为 SPEECH 文本；字面 `<` / `（` 受持有上界约束 | — |
| 空标签 `<mood></mood>` | 不发事件 | — |
| 重复标签（多个 inner_voice）| 每个都发事件；INNER_VOICE 分支内累积 → session.inner_voice 合并（`"\n".join`）为单字段；**日志（log_conversation）打合并后的单条**（对齐现状只打一条的语义）| #4 修复（现状只解析第一个、session 只存最后一个）|

**句首/挂起 PAUSE 持有规则**：PAUSE 到达即加入待分句 token 序列；句边界只由 SPEECH 文本决定；flush 时残留 PAUSE **保留**在尾句文本中（尾句无语音内容则整体丢弃）。例：`<#1#>然后说` → SENTENCE(`<#1#>然后说`)。（#5 修复：现状 flush 剥离残留 PAUSE。）

## 围栏检测细则

**行首检测用状态跟踪，不得持有 `\n`/空格**——`\n` 是 splitter 的硬分隔符（`_HARD_DELIMITERS` 含 `\n`），上一句要靠它落地。若 tokenizer 为确认"行首是否 ```` ``` ````"而扣住 `\n`+空格，每行末尾的句子都要等下一 delta 才出 SENTENCE/TTS——给关键语音路径的每个句子加一次跨-chunk 延迟，正是设计反对的。机制（**双状态**）：

- **布尔 `at_line_start`**：`\n` 或流开头 → 置位；**任意非空格内容字符 → 清除**；行首空格（缩进）在 ≤3 个内保持、第 4 个起清除（对齐 CommonMark ≤3 缩进规则）
- **缩进计数**：自 `at_line_start` 置位以来连续空格数（供缩进围栏判定，≤3 有效）
- `\n` 和空格**立即**作为 SPEECH 发出，零延迟
- 见到 ```` ``` ```` 时**只看 `at_line_start`**：True（行首）→ 入 CODE；False → 按普通文本
- 两个状态都是纯状态位，不参与任何持有/缓冲
- **为什么不能只靠空格计数**：`看 ```python 示例` 中 `看` 是内容字符（应清除行首态），但纯计数方案下其后的空格重新计数 1 ≤ 3，` ``` ```` 被误判为行首围栏、吞掉整行。布尔 `at_line_start` 由内容字符清除后不可恢复，杜绝此误判

其余细则：

- ```` ``` ```` 围栏仅在**行首**（流开头或 `\n` 之后，允许 ≤3 个空格缩进——对齐 CommonMark）识别为 CODE 起始，行中 ```` ``` ```` 视为普通文本（避免 ```` ```inline``` ```` 歧义）
- **语言提取**：fence 的 lang = 围栏行起始标记后**第一个空白分隔的 token**（strip 后取首段，如 ```` ```python 注意这个函数 ```` → lang="python"）。不得把整行当 language——CommonMark 信息串是整行，前端 highlight.js 拿 "python 注意这个函数" 会失效
- **收尾围栏判据（完整 CommonMark 规则，消除与"代码内容行首 ``` 不终止"的矛盾）**：行首 + ≤3 空格缩进 + **反引号串长度 ≥ 开围栏长度** + 其后仅空白/换行。按 CommonMark 语义，**行首 ```` ``` ````（长度 ≥ 开围栏）即收尾——代码内容里的行首 ```` ``` ```` 就是收尾围栏，这是 Markdown 固有语义而非缺陷**（内容要含行首 ```` ``` ````，应写更长反引号或转义）。「不提前终止」的保证只适用于**行中** ```` ``` ````（at_line_start=False）
- `<|code|lang>` / `<|markdown|>` 标签无行首限制（对齐现状 BlockFilter；`</|code|>` 为唯一结束标记，无此歧义；lang 受限字符集 `[A-Za-z0-9_+.-]*` 保持现状）

## 测试策略

1. **跨 chunk 不变式（property test）**：任意输入串 × 任意切分点 → token 流 / 事件序列一致（核心测试）
2. **每类标签四态**：完整 / 跨 chunk / 未闭合 / 空
3. **分句保护**：`（我有点，犹豫。）` 不被切分；`<#x#>` 整体保留
4. **修复行为固化（行为对照表逐项）**：
   - #2 ```` ```python ```` 围栏 → 不进 SENTENCE/TTS，进 parts
   - #3 句中 `<mood>开心</mood>` → 无泄漏，`你好<mood>…</mood>世界` 与 `你好世界` SENTENCE 序列一致
   - #4 两个 `<inner_voice>` → 两个 INNER_VOICE 事件
   - #5 `<#1#>然后说` → 尾句含完整标签
   - #6 `(眨眼)` 半角 → TTS 剥离
   - #7 残缺 `<mood>` → 无 MOOD 事件 → `_mood_applied` 保持 False
5. **持有上界**：`价格<100元` 不扣住、不误判；`<#` 半截跨 chunk 正确持有；`<#100元` 遇非数字字符释放；`（长文本无闭口` 超 M 窗口回落 SPEECH 且不延迟流式输出；`（纯英文无 CJK` 超 K 窗口回落
6. **STAGE_CN 半角**：`(眨眼)` → STAGE_CN token（TTS 剥离）；`(我有点，犹豫。)` 半角含标点 → span 保护不被切分（#6a）
7. **代码内容不嵌套解析**：除收尾围栏判据（行首反引号串）外，代码块内 `<mood>`/`<inner_voice>`/行中 ```` ``` ```` 按普通内容，不触发事件/嵌套解析；缩进围栏 `  ```python`（≤3 空格）识别为 CODE；**行中 ```` ``` ````（如 `看 ```inline``` 示例`）按普通文本，不触发 CODE**（at_line_start 为 False）
8. **路由**：每类 token → 预期事件序列（SENTENCE 含内嵌标签、TEXT_DELTA 剥离 PAUSE）
9. **parts / blocks**：`parts.py(all_sentences, blocks)` 按 `after_sentence_idx` 交错 → TextPart + content_type 与旧 build_parts_with_blocks 输出等价；`get_blocks()` 返回 (kind, content, lang, after_sentence_idx)，language 正确传递到 finalize block 命令（数据流接线：`_stream_llm_response` → `_think_loop` → `_finalize_turn`）；**四条交错语料钉死语义**：
   - 块前句子含内嵌 PAUSE/STAGE_CN（`好的。<#0.5#>\n` + 围栏）→ 块插在该句之后（字符偏移式误插被钉死）
   - **块前短句**（`好。\n` + 围栏，"好。"< min_chunk 暂留）→ 按完成计数块插在句**后**（与旧 build_parts_with_blocks 一致；已发出计数会错插到句前）
   - 句中 `<|code|>`（`看这个<|code|>x=1</|code|>示例`）→ 块闭合时句边界未消费 → **块落在句前**（与旧 build_parts_with_blocks 一致）
   - 段尾围栏（`第一段。` + ```` ``` ````，"第一段。"= 4 字符 < min_chunk 暂留未发出但边界已消费）→ 块插在**已完成句**（完成计数 = 1）之后
10. **回落场景**：#6a 超窗（span > M / CJK > K）→ 回落 SPEECH 按文本输出，不延迟流式；`（长文本无闭口` 不扣住；**`(laughs)` 短英文括号闭合时内容无 CJK 且为语气词 → STAGE_EN**（显示剥离、TTS 保留）；`(chu3li3)` 拼音标注非语气词 → 回落 SPEECH（显示 + TTS 保留）；`<pant>` 尖括号 → ANGLE_EN 全丢弃；`<abc>` 非语气词 → 回落 SPEECH（K 判定时机钉死，禁止逐字符持有到 K 的路径）
11. **围栏收尾**：代码内容中**行中** ```` ``` ````（at_line_start=False）不终止 CODE token；**行首 ```` ``` ````（长度 ≥ 开围栏）按 CommonMark 即收尾**；开围栏 4 反引号 + 内容含 3 反引号行 → 不终止（长度不足）；**行首状态跟踪：`\n`/空格立即作为 SPEECH 发出，`好的。\n` 的 SENTENCE 不因 fence 检测延迟**
12. **多 inner_voice session 侧**：public_text 无标签泄漏；session.inner_voice 为合并文本（`"\n".join`，INNER_VOICE 分支内累积）；**public_text 句中代码块按语音偏移还原原文位置**（`看这个<|code|>x=1</|code|>示例` → `看这个x=1示例`）
13. **回归**：现有 core/cognitive + core/text 测试全绿；`make test-quick` 全绿

## 迁移计划

1. TDD 新建 `core/text/tags.py`、`tokenizer.py`、`splitter.py`、`parts.py`
2. `core/text/stream_processor.py` 重写为新协调器（吸收原状态机 + BlockFilter + TTFT/emoji 逻辑 + `get_blocks()`）
3. 同步更新**全部 import 面**（删除文件的引用点逐一迁移）：
   - `core/cognitive/__init__.py`：re-export 改从 `core.text` 导入
   - `core/text/__init__.py`：re-export 改为新组件（`StreamProcessor`/`StreamEvent`/`StreamEventType`/`TagDefinition` 等）
   - `core/cognitive/loop.py`：`from .stream_processor import ...` → `from ..text.stream_processor import ...`；`build_parts_with_blocks` → `parts.py`；docstring 引用更新
   - `core/cognitive/tests/test_stream_processor.py`：import 路径更新 + 新增行为对照测试
   - `core/text/tests/test_block_filter.py`、`test_sentence_buffer.py`：迁移到新组件测试
4. **数据流接线（新增，当前缺失）**：`proc` 是 `_stream_llm_response` 局部变量（loop.py:437），返回元组（loop.py:482）不含 blocks，`_finalize_turn`（loop.py:810）拿不到 token 流——step 4 的 parts/block 来源必须显式接线：
   - `_stream_llm_response`：`proc.flush()` 后调用 `proc.get_blocks()`，返回元组扩展为 `(acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, blocks)`
   - `_think_loop`（loop.py:798-799）：解包并透传 `blocks`
   - `_finalize_turn`（loop.py:646）：签名新增 `blocks` 参数；parts 构建改用 `parts.py(all_sentences, blocks)`（按 `after_sentence_idx` 交错，**不再重解析 acc_text，符合"不再二次扫描"**）；`all_sentences` 保留（现状已传，parts 仍需句序列）
   - `_dispatch_stream_event` 无**新增**分支，但 INNER_VOICE 分支内部（loop.py:923-925）的 `iv_think_buf_holder[0] = event.text` 覆盖式赋值改为**累积**（append 到列表，收尾 `"\n".join()`）——合并落点在此处，不算新增分支
5. `core/cognitive/loop.py` finalize：parts 构建改用 `parts.py`（仅服务 session 存储）。**持久化 TextPart 经 `strip_display_hidden` 剥 PAUSE + 英文语气词（STAGE_CN 保留）**——气泡/历史消息不含 TTS 标记，SENTENCE 事件（TTS）仍由 splitter 保留；**finalize block 命令推送保留原位（显示路径唯一，不双渲染），参数改从 `blocks` 取值（含 language，顺带修复语言标签丢失）**；**public_text 改由 token 流重建**（SPEECH + PAUSE + STAGE_CN + 代码块内容，无 META 标签）——替换现状字符串 find 剥离（loop.py:676-685），使第二个 inner_voice 不再泄漏进 analyze_assistant / log_conversation / after_response；session.inner_voice 合并多个（换行连接）。**public_text 中代码块按"块前累计语音字符数"插回原文位置**（tokenizer 直接记录，不依赖 SENTENCE 粒度）——句中块如 `看这个<|code|>x=1</|code|>示例` 重建为原文顺序；注意这与 parts 的 `after_sentence_idx` 交错是**两条独立路径**（parts 句级粒度、public_text 原文位置）。**与现状差异**：现状 public_text 保留 `<|code|>`/```` ``` ```` 标记原样剥 META；新设计去标记只留块内容按原序——这是 #4 修复的预期改进（下游 analyze_assistant/log/after_response 不再收到标记），非缺陷
6. `core/adapter/tts_adapter.py`：删本地正则（`_PAUSE_TAG_RE`/`_CN_ACTION_RE`/`_EMOJI_RE`/死代码 `_TONE_TAG_RE`），改用 `core.text.tags` 共享定义；逐句提取逻辑不动
7. 删除 `core/text/block_filter.py`、`core/text/sentence_buffer.py`、`core/cognitive/stream_processor.py`（删除后跑 `grep -rn "block_filter\|sentence_buffer\|cognitive.stream_processor\|build_parts_with_blocks"` 确认零残留）
8. 全量回归 + `make test-quick`

## 风险

- **关键流式路径改动**：影响每轮对话的实时管线。缓解：核心不变式 property test + 全量回归 + 事件面/显示路径完全兼容
- **性能**：tokenizer 单次扫描 O(n)，正则仅用于行内短模式匹配
- **行为回归**：以行为对照表为唯一基准——"现状"项逐条对齐、"修复"项必须按设计实现并测试固化，防止实现者把修复还原成现状行为

## 修订记录

### 2026-09-16：句边界落盘判据的缺口（S3-G7，code review 三方确认后修复）

**本文档"事件面与显示路径完全兼容（…TEXT_DELTA/TTFT 不变）"这一条在粒度上曾不成立**，
现记录真实规则与守卫，避免读者继续按"完全兼容"理解。

- **缺口**：`StreamTokenizer._feed_normal` 的落盘边界只含 `\n`、空格、`` ` ``、`（`、`<`，
  句末标点（`。！？!?♪♡`）不在其中 ⇒ 无空格无换行的中文回复整段滞留在 `_pending`，
  分句器拿不到句号。实测（2026-09-16，真实 `StreamProcessor`）：
  `"你好呀，我是Elysia，很高兴见到你。今天天气不错，我们出去走走吧。"` 逐字符喂入
  ⇒ **0 个中途事件**，TTFT / TEXT_DELTA / 两条 SENTENCE 全在 `flush()`（= 整轮生成耗时）。
  后果：文本模式失去打字效果、句级 TTS 退化为"生成完再起播"、TTFT 指标失真
  （该 span 进 `TimingClock` 与 Debug Latency 面板）。
- **为什么既有用例全绿**：491 条用例只断言事件**序列**相等，而"整轮堆在 EOF"与
  "逐句即时产出"的序列完全相同——**序列相等不蕴含到达时刻正确**。
- **修复后的真实规则（唯一基准）**：SPEECH 的落盘边界 = **内容决定的边界**：
  ① `\n` / 空格；② 结构字符（`` ` ``、`（`/`(`、`<`）；③ **句末标点**
  （`core/text/tags.py::SENTENCE_END_CHARS`，与 `SpeechSplitter._HARD_DELIMITERS` **同源**）。
  句末标点**并入**累积文本后立即落盘（不单独成 token），故跨 chunk 不变式不受影响。
- **有意保留的延迟**：① 普通文本 run 在边界前不落盘（不变式要求"边界内容决定"，
  逐 chunk 落盘会破坏它）；② `SpeechSplitter` 的 `min_chunk_length` 短句暂留一拍
  （TTS 偏好）。两者都与"整轮滞留到 EOF"无关。
- **有意放弃的行为**：`StreamProcessor._flush_splitter_tail` 曾把 emoji-only 尾句
  并入上一条 SENTENCE。该合并的实现前提是"句子与 emoji 尾停在同一个待落盘缓冲里"，
  即本缺口本身；句边界即时落盘后句子已经派发，无并入对象，故该分支删除，
  emoji 尾成为独立 SENTENCE（TTS 侧本就有 emoji-only 门控，不合成；文本显示走
  TEXT_DELTA，不受影响）。
- **守卫**（防止再次退化）：`core/text/tests/test_stream_processor.py::TestSentenceBoundaryLatency`
  ——断言首个 SENTENCE 在句号那个 chunk 产出、TTFT 不晚于句边界、无标点文本仍在 EOF
  落盘（反空转）。反证：删掉落盘边界分支 → 该类 3 条红。
