# 结构化流式解析器重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将分散的流式解析管道（StreamProcessor 状态机 + BlockFilter + SentenceBuffer + tts_adapter 散落正则）重构为 `core/text/` 下的统一组件族：标签注册表 + 结构感知 tokenizer + 分句器 + parts 构建，标签成为原子 token，分句器永远接触不到标签字符。

**Architecture:** 流被 tokenizer 切成两类 token——行内（SPEECH/PAUSE/STAGE_CN，保留位置、分句保护）与带外（CODE/MARKDOWN/META，替换语音流）。SpeechSplitter 只消费行内 token（边界扫描跳过受保护 span），CODE/MARKDOWN 收集供 finalize parts，META 闭合时发事件。事件面不变（MOOD/INNER_VOICE/SENTENCE/TEXT_DELTA/TTFT），显示路径零改动。

**Tech Stack:** Python 3.11+、Pydantic（core.schema）、pytest、正则状态机。conda 环境 `Elysia`。

**Spec:** [docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md](../../docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md)（v3.6.2，8 轮 review 收官）

## Global Constraints

- **事件面不变**：`StreamEventType = MOOD | INNER_VOICE | SENTENCE | TEXT_DELTA | TTFT`，无新增类型
- **显示路径零改动**：TTS 模式经 tts_adapter 逐句提取；文本模式经 TEXT_DELTA；代码块 finalize 的 `block` 命令保留原位（显示路径唯一）
- **核心不变式**：对任意输入串 S，在任意字符边界切分为 delta 序列，解析出的 token 流 / 事件序列必须与 S 一次到达时一致
- **行为对照表为唯一行为基准**（spec「行为对照表」节，12 项）："现状"项逐条对齐；"修复"项必须按设计实现并测试固化，不得还原成现状
- **只依赖 `core.schema`**（TextPart 等共享类型），无其他外部依赖
- **前端协议不变**；`make test-quick` 全绿
- 代码注释用中文；测试加 marker（`unit`/`service`）
- 跨 chunk 持有不得无限扣住文本（spec「持有上界」节：PAUSE 尾串正则、STAGE_CN M=64/K=16 窗口、标签前缀 safe_len）
- 收尾围栏判据 = 行首 + ≤3 空格 + 反引号长度 ≥ 开围栏 + 其后仅空白（CommonMark）

---

### Task 1: 标签注册表 `core/text/tags.py`

**Files:**
- Create: `core/text/tags.py`
- Test: `core/text/tests/test_tags.py`

**Interfaces:**
- Consumes: 无（仅 stdlib `re`/`enum`）
- Produces（供 Task 2-7 使用）：
  ```python
  def is_emoji_only(text: str) -> bool                      # emoji 收口（现状双份）
  PAUSE_RE: re.Pattern                                  # <#\d+\.?\d*#>
  PAUSE_TAIL_RE: re.Pattern                              # <#[\d.]*#?$（持有判定，**必须允许尾随 #**——`<#0.5#` 前缀态，否则 `<#0.5#|>` 切分点立即释放为 SPEECH 泄漏）
  STAGE_CN_OPEN: frozenset[str]                          # {"（", "("}
  STAGE_CN_CLOSE: frozenset[str]                         # {"）", ")"}
  STAGE_CN_RE: re.Pattern  # [\(\（][^\)\）]*[一-鿿][^\)\）]*[\)\）]（含半角+全角）
  STAGE_M: int = 64; STAGE_K: int = 16                    # 持有窗口
  STAGE_EN_WORDS: frozenset[str]      # 英文语气词表（laughs/sighs/clears throat/applause…，从旧 _TONE_TAG_RE 提取），仅文档化用途——行为上随 SPEECH 流过
  META_TAGS: dict[str, tuple[str, str]]                  # kind → (open, close)，含 "mood"/"inner_voice"
  MARKDOWN_OPEN/CLOSE, CODE_OPEN_RE, CODE_CLOSE: ...
  FENCE_MARK = "```"; FENCE_MAX_INDENT = 3
  START_PREFIXES: tuple[str, ...]  # 全部起始模式前缀，供 tokenizer 持有判定
  ```

- [ ] **Step 1: 写失败测试**（`core/text/tests/test_tags.py`）：
  - `is_emoji_only("♪♪")` → True、`is_emoji_only("好的♪")` → False
  - `PAUSE_RE` 匹配 `<#0.5#>`/`<#1#>`、不匹配 `<#abc#>`
  - `STAGE_CN_RE` 匹配 `（眨眼）`/`(眨眼)`、不匹配 `(laughs)`（无 CJK）、不匹配 `（纯英文）`
  - `META_TAGS` 含 mood/inner_voice 且 open/close 成对
  - `FENCE_MARK == "```"`、`FENCE_MAX_INDENT == 3`
  - `STAGE_M == 64`、`STAGE_K == 16`
  - `STAGE_EN_WORDS` 含 `laughs`/`sighs`/`clears throat` 且非空（文档化用途）
- [ ] **Step 2: 跑测试确认 FAIL**（`conda run -n Elysia python -m pytest core/text/tests/test_tags.py -q`，模块不存在 → ImportError）
- [ ] **Step 3: 实现 `core/text/tags.py`**——纯数据定义 + emoji 正则（从 `core/cognitive/stream_processor.py:23` 与 `core/adapter/tts_adapter.py:31` 复制 `_EMOJI_RE` 收口为 `is_emoji_only`）
- [ ] **Step 4: 跑测试确认 PASS**
- [ ] **Step 5: 提交** `feat(text): tags 标签注册表（PAUSE/STAGE_CN/META/CODE/MARKDOWN + emoji 收口）`

---

### Task 2: 结构感知 tokenizer `core/text/tokenizer.py`

**Files:**
- Create: `core/text/tokenizer.py`
- Test: `core/text/tests/test_tokenizer.py`

**Interfaces:**
- Consumes: `core.text.tags`（全部定义）
- Produces：
  ```python
  class TokenKind(StrEnum): SPEECH | PAUSE | STAGE_CN | CODE | MARKDOWN | META
  @dataclass(frozen=True) class Token:
      kind: TokenKind; text: str; meta_kind: str = ""; lang: str = ""
  class StreamTokenizer:
      def feed(self, delta: str) -> list[Token]   # 只吐完整 token
      def flush(self) -> list[Token]              # 残留处理（未闭合块丢弃/回落）
      def reset(self) -> None
  ```
  （**不提供 `speech_offset()`**：public_text 重建走 token 流按序拼接——CODE token 在流中就位，顺序即原文顺序，无需字符偏移。见 Task 4）

- [ ] **Step 1: 写失败测试**（`core/text/tests/test_tokenizer.py`）：
  - **核心不变式（property test）**：对语料集 × 每个切分点，`feed` 拆分的 token 序列 == 一次 `feed` 的序列。语料含 `<#0.5#>好的，等一下<#1#>然后说`、`（我有点，犹豫。）`、```` ```python\n代码\n``` ````、`价格<100元`、`看 ```inline``` 示例`、`你好<mood>开心</mood>世界`、`(laughs)`、**`\n`````+``````python`（反引号前缀跨 chunk）**、**`<#0.5#`+`>`（PAUSE 闭口 # 后、> 前切分）**
  - PAUSE：`<#0.5#>` 单 token；`<#100元` 遇非数字释放为 SPEECH
  - STAGE_CN：`（眨眼）` 单 token；`(laughs)`（闭合内容无 CJK）→ SPEECH；`（长文本无闭口…` 超 M=64 → 回落 SPEECH 且**不延迟**（立即输出）；`（纯英文无 CJK…` 超 K=16 → 回落
  - META：`<mood>开心</mood>` 单 token（meta_kind="mood"）；句中 `<mood>` 正常识别；空 `<mood></mood>` → token 但 text 空
  - CODE 围栏：行首 ```` ```python ```` 入 CODE（lang="python"）；**行中 ```` ``` ```` → SPEECH**（at_line_start=False）；缩进 `  ```python`（≤3 空格）入 CODE；代码内容里行首 3 反引号 + 开围栏 4 反引号 → 不终止（长度不足）；收尾反引号 ≥ 开围栏长度即终止
  - `<|code|python>..</|code|>`、`<|markdown|>..</|markdown|>` → CODE/MARKDOWN token
  - **块内容不嵌套解析**：`<|code|><mood>x</mood></|code|>` → CODE token text=`<mood>x</mood>`
- [ ] **Step 2: 跑测试确认 FAIL**
- [ ] **Step 3: 实现 tokenizer**——单一状态机。要点：
  - NORMAL 态扫描起始模式（`START_PREFIXES`）；进入标签态后按标签内容规则扫描
  - **持有上界**：`<` 后 safe_len 前缀匹配（≤ 最长起始模式-1）；PAUSE 尾串 `PAUSE_TAIL_RE`；STAGE_CN 计数窗口（闭合时查 CJK + 超 M/K 回落）；`\n`/空格立即输出
  - **行首双状态**：布尔 `at_line_start`（`\n`/流开头置位、任意非空格内容字符清除、行首空格 ≤3 保持否则清除）+ 缩进计数；围栏判定只看 `at_line_start`
  - **围栏反引号前缀跨 chunk 持有**：`at_line_start` 为 True 且缓冲尾部是 1-2 个反引号（` 或 ``）→ 持有等待第 3 个（反引号是围栏起始标记的一部分，**"`\n`/空格立即输出"规则不适用**——反引号非空白，须持有直到确认非围栏或凑足标记）；`at_line_start` 为 False 时反引号按普通文本即时输出
  - 围栏 lang = 起始标记后第一个空白分隔 token
  - flush：未闭合 META → 按 spec（mood 丢弃、inner_voice 广播残留由协调器处理——tokenizer 产出残留 token 标记）；未闭合 CODE/MARKDOWN → 丢弃；半截 PAUSE → 剥离；未闭合 STAGE_CN → 回落 SPEECH
- [ ] **Step 4: 跑测试确认 PASS（property test 全过）**
- [ ] **Step 5: 提交** `feat(text): tokenizer 结构感知切分（跨 chunk 不变式 + 持有上界 + 行首双状态）`

---

### Task 3: 分句器 `core/text/splitter.py`

**Files:**
- Create: `core/text/splitter.py`
- Test: `core/text/tests/test_splitter.py`

**Interfaces:**
- Consumes: `core.text.tokenizer.Token`/`TokenKind`
- Produces：
  ```python
  class SpeechSplitter:
      def __init__(self, *, min_chunk_length: int = 5, max_buffer_length: int = 2000,
                   abbreviations: Iterable[str] | None = None) -> None
      def feed(self, token: Token) -> list[str]       # 返回完整句子（含内嵌 PAUSE/STAGE_CN 原序）
      def flush(self) -> list[str]                    # 尾句（残留内嵌标签保留；空则无）
      def completed_count(self) -> int                # 已完成句子数（边界已消费，含暂留短句）
      def reset(self) -> None
  ```
- 吸收原 `core/text/sentence_buffer.py` 的标点逻辑（`_is_dot_boundary` 缩写/小数/URL 保护、`_consume_sentence` 连续标点合并、min/max 长度、强制切分），但**输入改为 token 流**：边界扫描只在 SPEECH 文本上进行，PAUSE/STAGE_CN 是受保护 span（位置记录，切句时嵌入原序）

- [ ] **Step 1: 写失败测试**（`core/text/tests/test_splitter.py`）：
  - 复用现有 `test_sentence_buffer.py` 标点用例（缩写/小数/URL/连续标点/min 短句暂留/max 强制切分）——token 序列喂入结果一致
  - **保护**：`（我有点，犹豫。）` 整段不被切（含 `。` 不产生边界）；`<#0.5#>` 整体在句中
  - **PAUSE 嵌入**：`好的。<#0.5#>然后` → feed 返回 `好的。`，flush 返回 `<#0.5#>然后`（PAUSE 归属下一个句子）
  - **完成计数**：`好。\n`（"好。"<min_chunk 暂留）后 `completed_count()==1`；`代码如下：`（5 字符）`\n` 到达即发出
- [ ] **Step 2: 跑测试确认 FAIL**
- [ ] **Step 3: 实现 splitter**——内部维持语音缓冲（仅 SPEECH 文本）用于边界扫描，平行维护内嵌 token 位置表；切句时按位置把 PAUSE/STAGE_CN 插入句子文本原序；`completed_count` 在句边界消费时递增（含暂留短句），与"已发出"区分
- [ ] **Step 4: 跑测试确认 PASS**
- [ ] **Step 5: 提交** `feat(text): splitter 结构感知分句（span 保护 + PAUSE 嵌入 + 完成计数）`

---

### Task 4: parts 构建 `core/text/parts.py`

**Files:**
- Create: `core/text/parts.py`
- Test: `core/text/tests/test_parts.py`

**Interfaces:**
- Consumes: `core.schema.part.TextPart`、`BlockRef`（下述）
- Produces：
  ```python
  BlockRef = tuple[str, str, str, int]   # (kind, content, lang, after_sentence_idx)
  def build_parts(all_sentences: list[str], blocks: list[BlockRef]
                  ) -> tuple[list[TextPart], dict[str, list[int]]]
      # TextPart + content_type（{"markdown": [...], "code": [...]}），与旧 build_parts_with_blocks 输出形状等价
  def rebuild_public_text(tokens: Iterable[Token]) -> str
      # token 流按序拼接：SPEECH 文本 + **PAUSE 保留**（`<#0.5#>` 原样，对齐 spec 279 行「SPEECH + PAUSE + STAGE_CN + 代码块内容」与现状 acc_text 只剥 META）+ STAGE_CN 保留 + 块内容（去 `<|code|>`/``` 标记）；无 META
  ```

- [ ] **Step 1: 写失败测试**（`core/text/tests/test_parts.py`）——**四条交错语料**（spec 测试 #9）：
  1. 块前句子含内嵌标签：`["好的。<#0.5#>"]` + block(after=1) → `[TextPart("好的。<#0.5#>"), 块]`
  2. 块前短句：`["好。"]`（暂留，完成计数=1）+ block(after=1) → `[TextPart("好。"), 块]`（块在句**后**）
  3. 句中块：`[]`（句未落地）+ block(after=0) → `[块]`（句子随后落在块后，与旧实现一致）
  4. 段尾围栏：`["第一段。"]` + block(after=1) → `[TextPart("第一段。"), 块]`
  - `rebuild_public_text`：`看这个<|code|>x=1</|code|>示例` token 流 → `"看这个x=1示例"`；`好的。<#0.5#>继续` → `"好的。<#0.5#>继续"`（**PAUSE 保留**，对齐 spec/现状——public_text 只去 META 与代码块标记）
- [ ] **Step 2: 跑测试确认 FAIL**
- [ ] **Step 3: 实现 parts.py**——`build_parts` 按 `after_sentence_idx` 插入块（块插入点 = 第 idx 个句子之后；`after_sentence_idx` 超过句子数则追加末尾）；`rebuild_public_text` 按 token 流顺序拼接
- [ ] **Step 4: 跑测试确认 PASS**
- [ ] **Step 5: 提交** `feat(text): parts 构建（after_sentence_idx 交错 + public_text 重建）`

---

### Task 5: 新协调器 `core/text/stream_processor.py`

**Files:**
- Create: `core/text/stream_processor.py`（新，替代旧 `core/cognitive/stream_processor.py`）
- Test: `core/text/tests/test_stream_processor.py`

**Interfaces:**
- Consumes: `core.text.tags`/`tokenizer`/`splitter`/`parts`
- Produces：
  ```python
  class StreamEventType(StrEnum): MOOD | INNER_VOICE | SENTENCE | TEXT_DELTA | TTFT   # 与旧一致
  @dataclass(frozen=True) class StreamEvent: type: StreamEventType; text: str = ""
  class StreamProcessor:
      def __init__(self, *, min_chunk_length: int = 5, max_buffer_length: int = 2000) -> None
      def feed(self, delta: str) -> list[StreamEvent]
      def flush(self) -> list[StreamEvent]
      def reset(self) -> None
      def get_blocks(self) -> list[tuple[str, str, str, int]]   # (kind, content, lang, after_sentence_idx)
      def rebuild_public_text(self) -> str
  ```

- [ ] **Step 1: 写失败测试**（`core/text/tests/test_stream_processor.py`，迁移 `core/cognitive/tests/test_stream_processor.py` **不涉及 mood 位置语义的用例** + 新行为）：
  - **行为对照表逐项固化**：修复 #2（``` 围栏不进 SENTENCE/TTS）、#3（句中 `<mood>` 剥离，`你好<mood>…</mood>世界` SENTENCE 序列 == `你好世界`）、#4（两个 `<inner_voice>` → 两个事件）、#5（`<#1#>然后说` flush 尾句含完整标签）、#6（`(眨眼)` 半角剥离由 tts_adapter 保证，此处验证 SENTENCE 含标签）、#7（残缺 `<mood>` → 无 MOOD 事件）、#6a（`（我有点，犹豫。）` SENTENCE 完整）
  - **TEXT_DELTA 粒度**：SPEECH token **即时**产生 TEXT_DELTA（剥离 PAUSE 的增量文本，**不等待分句**）——文本模式显示保持逐 delta 流式刷新；SENTENCE 只在分句完成时产生（供 TTS）。`test_plain_text_with_period` 语义：feed 输入 `好的` 立即有 TEXT_DELTA，`。` 才触发 SENTENCE
  - **TTFT 时机**：首个**非 META 可见文本** delta 到达时发一次（mood/inner_voice 不含）——对齐旧 `test_ttft_not_before_mood_tag`（首 delta 全为 mood 时不发 TTFT）
  - 事件序列：TEXT_DELTA 剥离 PAUSE；SENTENCE 含内嵌 PAUSE/STAGE_CN
  - emoji-only 尾部合并到最后一个 SENTENCE（迁移旧 `flush` 行为）
  - `get_blocks()` 返回按序块 + after_sentence_idx
  - **不照抄** `test_text_before_mood_aborts_detection` / `test_long_prefix_before_partial_mood`——它们固化的是旧 #3 缺陷（只有句首 mood 被剥离），与行为表修复冲突，改写为"句中 mood 剥离"断言
- [ ] **Step 2: 跑测试确认 FAIL**
- [ ] **Step 3: 实现协调器**——组合：`feed(delta)` → `tokenizer.feed` 分三路（行内→splitter→SENTENCE/TEXT_DELTA 事件；CODE/MARKDOWN→收集（闭合时查 splitter.completed_count）；META→事件）；**TTFT 首个非 META 可见文本 delta 到达时发一次**（mood/inner_voice 不含，与 Step 1 测试规格一致）；`flush()` 处理残留（未闭合 inner_voice 广播、尾句、emoji 合并）；`get_blocks`/`rebuild_public_text` 从内部 token 流取
- [ ] **Step 4: 跑测试确认 PASS**
- [ ] **Step 5: 提交** `feat(text): stream_processor 新协调器（事件面兼容 + get_blocks + public_text）`

---

### Task 6: 迁移 `loop.py` 数据流接线 + `tts_adapter.py` 共享定义

**Files:**
- Modify: `core/cognitive/loop.py`（import、`_stream_llm_response`、`_think_loop`、`_finalize_turn`）
- Modify: `core/adapter/tts_adapter.py`
- Modify: `core/cognitive/__init__.py`、`core/text/__init__.py`

**Interfaces:**
- Consumes: `core.text.stream_processor`（Task 5）、`core.text.parts`（Task 4）、`core.text.tags`（Task 1）
- Produces: 无（接线）

- [ ] **Step 1: 更新 import 面**：
  - `core/cognitive/__init__.py`：`from .stream_processor import ...` → `from ..text.stream_processor import StreamProcessor, StreamEvent, StreamEventType`
  - `core/cognitive/loop.py`：`from .stream_processor import StreamProcessor, StreamEvent, StreamEventType` → `from ..text.stream_processor import ...`；`from ..text.block_filter import build_parts_with_blocks` → `from ..text.parts import build_parts`
  - `core/text/__init__.py`：更新 re-export（`StreamProcessor`/`StreamEvent`/`StreamEventType`/`build_parts`/`is_emoji_only` 等，删 BlockFilter/SentenceBuffer）
  - `core/text/tests/`：`test_stream_processor.py` 迁移到 `core/text/tests/`（改 import）
  - 跑 `make test-quick` 确认 import 面改完但功能未接线（预期 test_stream_processor 旧用例与新实现需对齐——Task 5 已迁移）
- [ ] **Step 2: `_stream_llm_response` 接线**（loop.py:437/482）：`proc = StreamProcessor()`；`flush()` 后调用 `blocks = proc.get_blocks()` 与 `public_text = proc.rebuild_public_text()`；返回元组扩为 `(acc_text, raw_tool_calls, speaking_started, all_sentences, iv_think, blocks, public_text)`。**public_text 必须在 `_stream_llm_response` 内重建（proc 是它的局部变量），`_finalize_turn` 只能收返回值**——Step 4 不得引用 `proc`
- [ ] **Step 3: `_think_loop` 透传**（loop.py:798-799）：解包 `blocks` 与 `public_text`；`_finalize_turn(..., blocks, public_text)` 调用处传参
- [ ] **Step 4: `_finalize_turn` 改造**（loop.py:646/695/728-740）：
  - 签名加 `blocks`、`public_text` 参数（public_text 已由 `_stream_llm_response` 重建，**不再字符串 find 剥离**，替换 loop.py:676-685）
  - `assistant_parts, content_type = build_parts(all_sentences, blocks)`（替换 `build_parts_with_blocks(public_text, all_sentences)`）
  - **finalize block 命令推送保留原位**，参数从 `blocks` 取（含 language——修复现状 `getattr(...,"language","")` 恒空）
  - INNER_VOICE 分支（loop.py:923-925）覆盖式赋值 `iv_think_buf_holder[0] = event.text` 改**累积** `iv_think_buf_holder.append(event.text)`；`_stream_llm_response` 返回前 `iv_think = "\n".join(iv_think_buf_holder)`（**替换 loop.py:482 的 `[0]` 取值**，否则 session.inner_voice 仍只存首条）；多个 inner_voice → session 合并文本
  - 确认 `_mood_applied` 语义不变（无 MOOD 事件 → fallback 触发）
- [ ] **Step 5: `tts_adapter.py` 删本地正则**：删 `_PAUSE_TAG_RE`/`_CN_ACTION_RE`/`_EMOJI_RE`/死代码 `_TONE_TAG_RE`，改为 `from core.text.tags import ...`；`is_emoji_only` 复用 tags；逐句提取逻辑（`_download_and_forward` 的 display_text/tts_text 分离）不动
- [ ] **Step 6: 跑相关测试**：`conda run -n Elysia python -m pytest core/cognitive/ core/adapter/tests/ core/text/ -q --tb=short`
- [ ] **Step 7: 提交** `refactor(loop/tts): 数据流接线 + 共享标签定义（blocks 透传 + public_text 重建 + language 修复）`

---

### Task 7: 删除旧文件 + 全量回归

**Files:**
- Delete: `core/cognitive/stream_processor.py`、`core/text/block_filter.py`、`core/text/sentence_buffer.py`
- Delete: `core/cognitive/tests/test_stream_processor.py`（已迁至 `core/text/tests/`）、`core/text/tests/test_block_filter.py`、`core/text/tests/test_sentence_buffer.py`（用例已并入 test_splitter/test_stream_processor）

- [ ] **Step 1: 删除旧文件** + `core/text/__init__.py` 移除 BlockFilter/SentenceBuffer 残留导出
- [ ] **Step 2: grep 零残留校验**：`grep -rn "block_filter\|sentence_buffer\|cognitive\.stream_processor\|build_parts_with_blocks\|build_parts\b" --include="*.py" core/ Module/ | grep -v __pycache__`——除新组件自身引用外必须为空
- [ ] **Step 3: 全量回归**：`make test-quick`（实际 unit+service 收集 **1861** 条，全绿）
- [ ] **Step 4: 提交** `refactor(text): 删除旧解析组件（block_filter/sentence_buffer/旧 stream_processor）`

---

### Task 8: 文档同步（README + 导航）

**Files:**
- Modify: `CLAUDE.md`（core/text 组件描述更新——删 block_filter/sentence_buffer，加 tags/tokenizer/splitter/parts）
- Modify: `core/README.md`（组件清单与路径）
- Modify: `README.md`（若 core/text 在根导航出现则更新）
- Create: `core/text/README.md`（若不存在；按 `docs/readme-conventions.md` 模板：面包屑 + 相关文档节）

- [ ] **Step 1: 更新 CLAUDE.md** core/text 段：`block_filter.py`/`sentence_buffer.py` → `tags.py`/`tokenizer.py`/`splitter.py`/`parts.py`/`stream_processor.py`
- [ ] **Step 2: 更新 core/README.md** + 根 README 导航（若有引用）
- [ ] **Step 3: 按 readme-conventions.md 模板写 core/text/README.md**（组件职责表 + 指向 design spec）
- [ ] **Step 4: 提交** `docs: core/text README + CLAUDE.md 组件清单同步`

---

## 自审清单（writing-plans 要求）

- **Spec 覆盖**：Token 模型（Task 2/3）✓、组件（Task 1-5）✓、行为对照表 12 项（Task 2/3/5 测试固化）✓、持有上界含 PAUSE 尾随 # / 反引号前缀（Task 2）✓、围栏细则（Task 2）✓、数据流接线含 public_text 重建位置与 iv_think join（Task 6）✓、parts/公共文本（Task 4/6）✓、删除+回归（Task 7）✓、README 同步（Task 8）✓、事件面不变 + TEXT_DELTA 粒度 + TTFT 时机（Task 5）✓、emoji 收口 + STAGE_EN_WORDS（Task 1/6）✓
- **无占位符**：每 task 有 Files/Interfaces/测试清单/提交消息 ✓
- **类型一致**：`Token`/`TokenKind`/`StreamEvent`/`BlockRef`/`get_blocks`/`_stream_llm_response` 返回元组（7 元）跨 task 一致 ✓
