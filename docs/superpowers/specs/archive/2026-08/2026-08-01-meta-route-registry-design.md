# META 路由注册表化 — 增量重构设计

> 日期：2026-08-01
> 状态：设计定稿，待实现
> 范围：仅机制重构——把 `core/text/stream_processor.py` 的 META 路由从写死 if/elif 改为注册表驱动。保持事件面 5 种不变，不预注册未来标签。

## 背景与动机

`core/text/` 流式解析组件族已完成重构（tags/tokenizer/splitter/parts/stream_processor）。当前**标签定义与解析机制已分离**（`tags.META_TAGS` 注册表 + tokenizer 按注册表识别标签、透传 `meta_kind`），但**"标签 → 事件"路由仍硬编码**在协调器 `stream_processor._route` 的 META 分支：

```python
if token.meta_kind == "mood":
    events.append(StreamEvent(type=StreamEventType.MOOD, text=text))
elif token.meta_kind == "inner_voice":
    events.append(StreamEvent(type=StreamEventType.INNER_VOICE, text=text))
```

后果：加**新语义类别**（如未来 `<<x>>` 环境更新 → ENV）必须改协调器 if/elif + 加事件类型 + 消费者分支——"模块加入需单独开发"的根源。

**目标**：标签 → 事件的路由注册表化。未来加**`<` 前缀**的新语义类别 = 注册表加一行 + 枚举加值（若新类型），**协调器零改动**。

**重要边界（review 确认）**：注册表驱动是**编译期/重启时**生效——改 tags 源码 → 重新 import → tokenizer 的 `_OPEN_PATTERNS` 重新派生。**非运行时热注入**。且新标签**必须满足 tokenizer 的起始形态**（`<` 前缀 / 行首反引号 / STAGE 括号）；`[action]`（`[` 前缀）需要额外给 tokenizer 加起始识别，本次范围外（未来做 `[action]` 时扩展 tokenizer，见风险节）。

## 非目标

- 不预注册未来标签（`<<x>>`/`[action]`）——仅让机制到位；未来 `<` 前缀标签（如 `<<x>>`）加一行即可，`[action]` 式需另扩 tokenizer 起始识别
- 不扩展 `StreamEventType` 枚举（保持 MOOD/INNER_VOICE/SENTENCE/TEXT_DELTA/TTFT 五种）
- 不做"标签 → 动作函数"模型（数据类标签如 mood/inner_voice 是存状态/传前端，非调函数）
- 核心行为零变化（mood/inner_voice 现有行为不变）；唯一变化是未知 emit 从静默丢弃改为 log warning（见行为变化节）

## 设计

### 1. `core/text/tags.py` — `META_TAGS` 值升级为三元组

```python
# kind → (open, close, emit)——emit 为该标签闭合时发的事件类型 value
# （与 StreamEventType.value 对齐，tags 保持纯数据层，不依赖 stream_processor 枚举）
META_TAGS: dict[str, tuple[str, str, str]] = {
    "mood": ("<mood>", "</mood>", "mood"),
    "inner_voice": ("<inner_voice>", "</inner_voice>", "inner_voice"),
    # 未来: "env": ("<<", ">>", "env"),   ← 加一行 + StreamEventType 加 ENV 值即可（须 `<` 前缀）
}
```

### 2. 解包适配三元组（tokenizer 2 处 + test_tags 2 处）

**`core/text/tokenizer.py`（2 处真正解包）**：

- `open_ for open_, _ in META_TAGS.values()` → `open_ for open_, _, _ in ...`（`_OPEN_PATTERNS` 派生，:79）
- `for kind, (open_, _) in META_TAGS.items()` → `for kind, (open_, _, _) in ...`（`_complete_open`，:109）
- `META_TAGS[self._meta_kind][1]`（close 取值，:404）→ 索引访问非解包，`[1]` 不变（close 仍是第二个）

**`core/text/tests/test_tags.py`（2 处）**：

- `:159` `for open_tag, close_tag in META_TAGS.values()` → `for open_tag, close_tag, _emit in ...`（解包，会 ValueError）
- `:154-155` `META_TAGS["mood"] == ("<mood>", "</mood>")` 等 2 元组相等断言 → 更新为 3 元组（含 emit）

行为不变：tokenizer 仍按 open/close 识别标签，`meta_kind` 透传。

### 3. `core/text/stream_processor.py` — `_route` META 分支注册表化

```python
# 模块级：从枚举自动派生 {value: type} 映射——未来加事件类型零改动
# （当前文件无 logger，需补 import logging + logger = logging.getLogger(__name__)）
_EMIT_BY_NAME: dict[str, StreamEventType] = {v.value: v for v in StreamEventType}

# _route META 分支（替换 if/elif）：
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

- **`emit` 是字符串**：tags 保持纯数据层（仅 stdlib），不产生 tags → stream_processor 反向依赖
- **自动派生映射**：`{v.value: v for v in StreamEventType}`——协调器不维护显式映射表，未来枚举加值即自动覆盖
- **未知 emit 降级**：log warning + 丢弃（替换原静默丢弃——补 Task 5 review 遗留 minor）

## 行为变化（唯一一处）

| 场景 | 现状 | 新行为 |
|------|------|--------|
| mood/inner_voice | 正常发事件 | **不变** |
| 未知 meta_kind（如注册表临时加了但枚举没有）| 静默丢弃 | `logger.warning` + 丢弃 |

## 测试

1. **test_tags**：`META_TAGS` 断言更新——每个 kind 为三元组，emit 字段存在且非空；`for open_tag, close_tag, _emit in META_TAGS.values()` 解包适配
2. **test_stream_processor**：
   - 现有 MOOD/INNER_VOICE 用例**保持通过**（行为不变）
   - 新增「未知 emit 降级」：**直接构造 `Token(TokenKind.META, text="x", meta_kind="action")` 喂 `_route`**，断言无事件 + `caplog` 断言 warning。**注意不能用 `[action]x[/action]` 走 feed 路径**——`[` 前缀不被 tokenizer 识别（`_feed_normal` 只对 `<`/行首反引号/STAGE 括号进入持有态），会落为 SPEECH 产生 TEXT_DELTA/SENTENCE 事件且永不进 META 分支（review 实测确认）
3. **test_tokenizer**：META 识别用例保持通过（解包适配不改变行为）
4. 全量 `make test-quick` 回归

## 风险

- **低**。改动集中于 3 个文件 + 测试，核心行为零变化（除未知 emit 从静默变 warning，属改进）
- `META_TAGS` 结构变更影响 tokenizer 2 处解包 + test_tags 2 处断言（§2），同步更新
- **已知限制（未来项）**：`[action]`（`[` 前缀）等非 `<` 前缀标签**不在本次注册表化覆盖范围**——tokenizer 的 `_feed_normal` 只对 `<`/行首反引号/STAGE 括号进入持有态，`[` 落为 SPEECH。未来做 `[action]` 需给 tokenizer 增加 `[` 起始识别 + 注册表加一行，属独立小扩展
