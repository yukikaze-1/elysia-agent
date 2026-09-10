# META 路由注册表化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `core/text/stream_processor.py` 的 META 路由从写死 if/elif 改为注册表驱动——`tags.META_TAGS` 携带 emit 信息，协调器查注册表 + 从枚举自动派生映射，未来加 `<` 前缀新标签 = 注册表加一行 + 枚举加值（若新类型），协调器零改动。

**Architecture:** `tags.META_TAGS` 值从二元组 `(open, close)` 升级为三元组 `(open, close, emit)`（emit 与 `StreamEventType.value` 对齐，tags 保持纯数据层）。tokenizer 解包适配（行为不变）。`stream_processor._route` META 分支删 if/elif，改查注册表 emit + `{v.value: v for v in StreamEventType}` 自动派生映射；未知 emit → `logger.warning` + 丢弃。

**Tech Stack:** Python 3.11+、pytest、StrEnum。conda 环境 `Elysia`。

**Spec:** [docs/superpowers/specs/archive/2026-08/2026-08-01-meta-route-registry-design.md](../../docs/superpowers/specs/2026-08-01-meta-route-registry-design.md)（v2.1，2 轮 review）

## Global Constraints

- **行为对照**：mood/inner_voice 现有行为不变；唯一变化 = 未知 emit 从静默丢弃改为 `logger.warning` + 丢弃
- **`emit` 是字符串**且与 `StreamEventType.value` 对齐——tags 保持纯数据层（仅 stdlib），不产生 tags → stream_processor 反向依赖
- **保持 5 种事件类型**（MOOD/INNER_VOICE/SENTENCE/TEXT_DELTA/TTFT），不预注册未来标签
- **注册表驱动是编译期/重启生效**（`_OPEN_PATTERNS` 在 import 时从 META_TAGS 派生），非运行时热注入
- 注释中文；测试 marker unit；`make test-quick` 全绿

---

### Task 1: tags 三元组 + tokenizer 解包适配

**Files:**
- Modify: `core/text/tags.py:83-86`
- Modify: `core/text/tokenizer.py:79,109`（解包）
- Test: `core/text/tests/test_tags.py`、`core/text/tests/test_tokenizer.py`、`core/text/tests/test_stream_processor.py`（Step 5 回归确认）

**Interfaces:**
- Consumes: 无
- Produces: `META_TAGS: dict[str, tuple[str, str, str]]`（`(open, close, emit)`），emit 与 `StreamEventType.value` 对齐

- [ ] **Step 1: 写失败测试**（test_tags 更新）：
  - `META_TAGS["mood"] == ("<mood>", "</mood>", "mood")`（:154-155 元组相等断言改 3 元组）
  - `for open_tag, close_tag, _emit in META_TAGS.values()`（:159 解包）
  - 新增断言：每个 emit 非空、与 kind 一致
- [ ] **Step 2: 跑测试确认 FAIL**（test_tags 的元组相等 + 解包 → AssertionError/ValueError）
- [ ] **Step 3: 改 tags.py**：`META_TAGS` 值加第三个元素 emit（`"mood"` / `"inner_voice"`），注释说明 emit 与 `StreamEventType.value` 对齐
- [ ] **Step 4: 改 tokenizer.py** 两处解包：
  - `:79` `open_ for open_, _ in META_TAGS.values()` → `open_ for open_, _, _ in ...`
  - `:109` `for kind, (open_, _) in META_TAGS.items()` → `for kind, (open_, _, _) in ...`
  - `:404` 索引访问 `META_TAGS[self._meta_kind][1]` **不变**（close 仍是第二个）
- [ ] **Step 5: 跑测试确认 PASS**（test_tags + test_tokenizer 全绿；test_stream_processor 现有 MOOD/INNER_VOICE 用例保持通过——协调器不访问 META_TAGS 结构，仍工作）
- [ ] **Step 6: 提交** `refactor(text): META_TAGS 三元组（open/close/emit）+ tokenizer 解包适配`

---

### Task 2: stream_processor 路由注册表化 + 未知降级

**Files:**
- Modify: `core/text/stream_processor.py`（`import logging`、模块级 `logger`、`_EMIT_BY_NAME`、`_route` META 分支）
- Test: `core/text/tests/test_stream_processor.py`

**Interfaces:**
- Consumes: `core.text.tags.META_TAGS`（Task 1 的三元组）、`StreamEventType`
- Produces: META 路由查注册表；未知 emit → warning + 丢弃

- [ ] **Step 1: 写失败测试**（test_stream_processor 新增；**文件顶部补 `from core.text.tokenizer import Token, TokenKind`**——当前只 import StreamProcessor/StreamEvent/StreamEventType）：
  - **未知 emit 降级**：`proc = StreamProcessor()`；`events: list = []`；`proc._route(Token(TokenKind.META, text="x", meta_kind="action"), events)` → `events == []` 且 `caplog` 捕获 `logger.warning`（**注意：不能用 `[action]x[/action]` 走 feed 路径——`[` 前缀不被 tokenizer 识别，见 spec 测试 #2 说明**）
  - 现有 MOOD/INNER_VOICE 用例保持通过（行为不变）
- [ ] **Step 2: 跑测试确认 FAIL**（未知 emit 当前静默丢弃，无 warning → caplog 断言失败）
- [ ] **Step 3: 改 stream_processor.py**：
  - 顶部 `import logging` + `logger = logging.getLogger(__name__)`
  - **tags import 补 `META_TAGS`**：`from core.text.tags import META_TAGS, PAUSE_RE, is_emoji_only`（当前只 import 后两者）
  - 模块级 `_EMIT_BY_NAME: dict[str, StreamEventType] = {v.value: v for v in StreamEventType}`
  - `_route` META 分支替换 if/elif：
    ```python
    elif kind is TokenKind.META:
        text = token.text.strip()
        if text:
            emit_name = META_TAGS.get(token.meta_kind, ("", "", ""))[2]
            emit_type = _EMIT_BY_NAME.get(emit_name)
            if emit_type is not None:
                events.append(StreamEvent(type=emit_type, text=text))
            else:
                logger.warning("未知 META 类型 %r，事件丢弃", token.meta_kind)
    ```
- [ ] **Step 4: 跑测试确认 PASS**（test_stream_processor 全绿，含新增未知降级用例）
- [ ] **Step 5: 全量回归**：`make test-quick`（1665+ 全绿）
- [ ] **Step 6: 提交** `refactor(text): stream_processor META 路由注册表化 + 未知 emit 降级 warning`

---

## 自审清单

- **Spec 覆盖**：tags 三元组（Task 1）✓、tokenizer 解包 2 处（Task 1）✓、test_tags 2 处断言（Task 1）✓、stream_processor 路由 + 未知降级 + logger（Task 2）✓、行为不变 + 唯一变化（Task 2 测试固化）✓
- **无占位符**：每 task 有 Files/Interfaces/步骤/提交消息 ✓
- **类型一致**：`META_TAGS` 三元组、`_EMIT_BY_NAME`、`_route` 签名跨 task 一致 ✓
