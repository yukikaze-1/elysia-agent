# Fix 13 Pre-existing Test Failures

**Date:** 2026-07-19
**Status:** approved

## Scope

修复 13 个已知 pre-existing test failures，分 4 类：

| 类别 | 数量 | 根因 | 处理方式 |
|------|------|------|----------|
| SessionMemory.set_param | 4 | 测试使用已删除的 `set_param`/`get_param` API | 删除测试 |
| system_prompt 边界测试 | 2 | 测试引用已删除的 `core.emotion_engine` 模块 | 删除测试 |
| system_prompt EmotionSection | — | `EmotionSection` 接受 `session` 参数但不读取 mood | 修改源码 |
| system_prompt 集成测试 | 1 | `C1Data` dataclass 用了 dict 式括号访问 | 修改测试 |
| task_system integration | 3 | 测试传裸 `list[dict]`，源码期望 `list[Message]` | 修改测试 |
| idle flaky | 1 | polling loop 超时不够（100×0.01s） | 加长超时 |

## Design

### 1. 删除 SessionMemory.set_param 测试（4 个）

修改文件: `core/tests/test_phase2.py`

删除以下 4 个测试方法及其完整方法体：

- `TestSessionPersistence::test_snapshot_restore` — 使用 `set_param("mood")` / `get_param("mood")` 验证持久化
- `TestSessionPersistence::test_clear` — 使用 `set_param("key")` / `get_param("key")` 验证 clear 行为
- `TestSessionMemoryEdgeCases::test_round_trip_snapshot_cycle` — 使用 `set_param` 验证含自定义 key 的持久化
- `TestSessionMemoryEdgeCases::test_character_state_persistence_across_restore` — 使用 `get_param` 验证未知 key 返回 None

这些测试调用的是已删除的 `set_param`/`get_param` 方法。`mood`/`mood_intensity` 已迁移到 `CharacterState` Pydantic 模型，`session.save()`/`load()` 已覆盖 `character_state` 持久化。`custom_key` 等任意键在代码库中没有生产使用。

### 2. 修改 EmotionSection：从 session 读 mood

修改文件: `Module/system_prompt/sections/emotion.py`

`EmotionSection.__init__` 已接受 `session: SessionMemory` 参数，存为 `self._session`，但从未读取。

**修改点：**
- `set_context()` 或 `render()` 中，当外部未显式传入 mood 时，从 `self._session.character_state.mood` 取值
- 如果用 kwarg 显式传入 mood，仍优先使用显式值（保持覆盖语义）

### 3. 删除 system_prompt 边界测试（2 个）

修改文件: `Module/system_prompt/tests/unit/test_sections/test_boundary.py`

删除以下 2 个测试方法：

- `TestSensorySectionBoundary::test_import_error_fallback_emotion_emoji`
- `TestSensorySectionBoundary::test_import_error_fallback_format_events`

根因：测试 `import core.emotion_engine`（已删除的模块），且 `patch.dict` 目标模块名错误。实际 fallback 代码在 `core.schema.mood`。

### 4. 修改 system_prompt 集成测试

修改文件: `Module/system_prompt/tests/integration/test_full_render.py`

第 114-115 行：`pe.render_c1()["core_personality"]` → `pe.render_c1().core_personality`（`@dataclass` 用属性访问）。

### 5. 修改 task_system 集成测试（3 个）

修改文件: `Module/task_system/tests/integration/test_delegation_preparer.py`

3 个测试的 `session_messages` 参数从裸 `list[dict]` 转为 Pydantic `Message` 对象。

role → Message 子类映射：`"user"` → `UserMessage`，`"assistant"` → `AssistantMessage`。
content → `content=[TextPart(content=...)]`。每条消息需分配 `message_id=MessageId(...)`。

需要新增 import（文件顶部）：
```python
from core.schema.message import UserMessage, AssistantMessage, MessageId
from core.schema.part import TextPart
```

**test_integration_simple_task_ok (line 73-77)**

Before:
```python
session_messages=[
    {"role": "user", "content": "今天天气怎么样？"},
    {"role": "assistant", "content": "请问你想查哪个城市的天气？"},
    {"role": "user", "content": "北京的"},
],
```

After:
```python
session_messages=[
    UserMessage(content=[TextPart(content="今天天气怎么样？")], message_id=MessageId("it1-u1")),
    AssistantMessage(content=[TextPart(content="请问你想查哪个城市的天气？")], message_id=MessageId("it1-a1")),
    UserMessage(content=[TextPart(content="北京的")], message_id=MessageId("it1-u2")),
],
```

**test_integration_multi_step_with_mutation (line 125-127)** — 与 simple_task_ok 模式相同：

Before:
```python
session_messages=[
    {"role": "user", "content": "明天可能需要提醒老板带伞"},
],
```

After:
```python
session_messages=[
    UserMessage(content=[TextPart(content="明天可能需要提醒老板带伞")], message_id=MessageId("it2-u1")),
],
```

**test_integration_context_summarization (line 233-239)**

Before:
```python
session_messages=[
    {"role": "user", "content": "我下周要去上海出差"},
    {"role": "assistant", "content": "好的，需要我帮你准备什么吗？"},
    {"role": "user", "content": "帮我查一下上海的天气，我还要订酒店"},
    {"role": "assistant", "content": "上海最近天气多变，建议带伞。你已经定了酒店吗？"},
    {"role": "user", "content": "还没订，先查天气吧，下雨的话就不去了"},
],
```

After:
```python
session_messages=[
    UserMessage(content=[TextPart(content="我下周要去上海出差")], message_id=MessageId("it5-u1")),
    AssistantMessage(content=[TextPart(content="好的，需要我帮你准备什么吗？")], message_id=MessageId("it5-a1")),
    UserMessage(content=[TextPart(content="帮我查一下上海的天气，我还要订酒店")], message_id=MessageId("it5-u2")),
    AssistantMessage(content=[TextPart(content="上海最近天气多变，建议带伞。你已经定了酒店吗？")], message_id=MessageId("it5-a2")),
    UserMessage(content=[TextPart(content="还没订，先查天气吧，下雨的话就不去了")], message_id=MessageId("it5-u3")),
],
```

### 6. 修改 idle flaky 测试

修改文件: `core/tests/test_phase3.py`

`test_process_message_poll_waits_for_idle`：polling loop 上限从 `range(100)` 改为 `range(200)`（超时从 1s → 2s）。

## 不改的内容

- 不新增文件、不新增依赖
- 不修改 `SessionMemory` 源码（不需要 `set_param`/`get_param`）
- 不增加新测试

## 验证方式

```bash
pytest -m "unit or service" -q --tb=short
```

预期：13 个失败全部消除，无回归。
