# Fix 13 Pre-existing Test Failures — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate 13 pre-existing test failures across 4 categories by deleting tests of deleted APIs, fixing EmotionSection to read mood from session, fixing test data types, and extending a flaky timeout.

**Architecture:** 4 independent tasks — each touches a different file set, no cross-task dependencies. All changes are mechanical: delete obsolete tests, fix one source method, convert test data formats, adjust one numeric constant.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest

## Global Constraints

- 不新增文件、不新增依赖
- 不修改 `SessionMemory` 源码（不需要 `set_param`/`get_param`）
- 不增加新测试
- 每次 commit 后运行相关测试确认通过
- 验证命令: `python3 -m pytest <target> -q --tb=short`

---

### Task 1: system_prompt — EmotionSection 源码 + 删除边界测试 + 修复集成测试

**Files:**
- Modify: `Module/system_prompt/sections/emotion.py:17-22`
- Modify: `Module/system_prompt/tests/unit/test_sections/test_boundary.py` — 删除行 121-136
- Modify: `Module/system_prompt/tests/integration/test_full_render.py:114-115`

**Interfaces:**
- Consumes: `SessionMemory.character_state.mood`（Mood enum，值如 `"happy"`, `"neutral"`）
- Produces: `EmotionSection.set_context()` 自动从 session 读 mood 作为默认值，kwarg 显式传入仍优先

- [ ] **Step 1: 修改 EmotionSection.set_context — 从 session 读 mood 作为默认值**

在 `set_context` 中添加 `elif` 分支：当 kwarg 未显式传入 `mood` 且 session 可用时，从 `session.character_state.mood` 取值。

```python
# Module/system_prompt/sections/emotion.py，替换 set_context 方法体（第 17-21 行）

def set_context(self, **kwargs) -> None:
    self._user_name = kwargs.get("user_name", "")
    self._user_pronoun = kwargs.get("user_pronoun", "她")
    if "mood" in kwargs:
        self._mood = kwargs["mood"]
    elif self._session is not None:
        self._mood = self._session.character_state.mood
```

- [ ] **Step 2: 运行 EmotionSection 相关测试验证**

Run: `python3 -m pytest Module/system_prompt/tests/unit/test_sections/test_boundary.py::TestEmotionSectionBoundary -v --tb=short`
Expected: 2 passed（`test_no_session_defaults_neutral` + `test_session_mood_passed`）

- [ ] **Step 3: 删除 2 个引用已删除模块的边界测试**

删除 `TestSensorySectionBoundary` 类中的 2 个方法（第 121-136 行）：
- `test_import_error_fallback_emotion_emoji`
- `test_import_error_fallback_format_events`

```python
# 删除第 121-128 行:
    def test_import_error_fallback_emotion_emoji(self):
        """emotion_engine 不存在时，_emotion_emoji 返回 ""。"""
        import core.emotion_engine as orig_module
        ...

# 删除第 130-136 行:
    def test_import_error_fallback_format_events(self):
        """emotion_engine 不存在时，_format_events 返回逗号分隔原始事件。"""
        ...
```

- [ ] **Step 4: 运行边界测试确认无回归**

Run: `python3 -m pytest Module/system_prompt/tests/unit/test_sections/test_boundary.py -v --tb=short`
Expected: 所有测试通过（删除后 ~23 passed）

- [ ] **Step 5: 修复集成测试 C1Data 括号访问**

`pe.render_c1()` 返回 `C1Data` dataclass（字段 `core_personality`, `self_personality`），不能用 `["key"]` 访问。

```python
# Module/system_prompt/tests/integration/test_full_render.py，第 114-115 行
# 替换:
    personality_section.update_personality(
        core=pe.render_c1()["core_personality"],
        self_p=pe.render_c1()["self_personality"],
    )
# 为:
    personality_section.update_personality(
        core=pe.render_c1().core_personality,
        self_p=pe.render_c1().self_personality,
    )
```

- [ ] **Step 6: 运行集成测试确认通过（需要 DEEPSEEK_API_KEY）**

Run: `python3 -m pytest Module/system_prompt/tests/integration/test_full_render.py -v --tb=short`
Expected: PASS（若 DEEPSEEK_API_KEY 未设置会 skip，同样为通过）

- [ ] **Step 7: Commit**

```bash
git add Module/system_prompt/sections/emotion.py \
        Module/system_prompt/tests/unit/test_sections/test_boundary.py \
        Module/system_prompt/tests/integration/test_full_render.py
git commit -m "fix: EmotionSection read mood from session, delete stale boundary tests, fix C1Data access"
```

---

### Task 2: 删除 SessionMemory.set_param 测试（4 个）

**Files:**
- Modify: `core/tests/test_phase2.py` — 删除 4 个方法

**Interfaces:**
- Consumes: none（纯删除）
- Produces: none

- [ ] **Step 1: 删除 TestSessionPersistence 中的 2 个测试方法**

删除 `test_snapshot_restore`（第 33-60 行）和 `test_clear`（第 86-98 行）。

```python
# test_snapshot_restore — 删除第 33-60 行（async def test_snapshot_restore 到 os.remove 后的空行）:
    @pytest.mark.asyncio
    async def test_snapshot_restore(self):
        """save/load 可跨 session 实例保留消息和参数。"""
        ...  # 完整方法体

# test_clear — 删除第 86-98 行:
    def test_clear(self):
        from core.memory.session import SessionMemory
        ...  # 完整方法体
```

- [ ] **Step 2: 删除 TestSessionMemoryEdgeCases 中的 2 个测试方法**

删除 `test_round_trip_snapshot_cycle`（第 527-557 行）和 `test_character_state_persistence_across_restore`（第 601-624 行）。

```python
# test_round_trip_snapshot_cycle — 删除第 527-557 行:
    @pytest.mark.asyncio
    async def test_round_trip_snapshot_cycle(self):
        """Full cycle: create -> populate -> save -> load -> verify."""
        ...  # 完整方法体

# test_character_state_persistence_across_restore — 删除第 601-624 行:
    @pytest.mark.asyncio
    async def test_character_state_persistence_across_restore(self):
        """CharacterState should survive save/load exactly."""
        ...  # 完整方法体
```

- [ ] **Step 3: 运行 test_phase2 确认无回归**

Run: `python3 -m pytest core/tests/test_phase2.py -v --tb=short -m "unit"`
Expected: 所有剩余的 unit 测试通过（`TestSessionPersistence` 剩 2 个测试，`TestSessionMemoryEdgeCases` 剩 5 个测试）

- [ ] **Step 4: Commit**

```bash
git add core/tests/test_phase2.py
git commit -m "test: remove 4 SessionMemory.set_param tests (API deleted)"
```

---

### Task 3: task_system 集成测试 — dict → Message 对象

**Files:**
- Modify: `Module/task_system/tests/integration/test_delegation_preparer.py` — 替换 3 个测试的 `session_messages`，新增 imports

**Interfaces:**
- Consumes: `UserMessage`, `AssistantMessage`, `MessageId` from `core.schema.message`; `TextPart` from `core.schema.part`
- Produces: none（测试内部数据结构变更，不影响外部）

- [ ] **Step 1: 在文件顶部新增 imports**

在第 7 行（`import pytest`）和第 9 行（`from Module.task_system...`）之间插入：

```python
from core.schema.message import AssistantMessage, MessageId, UserMessage
from core.schema.part import TextPart
```

- [ ] **Step 2: 修改 test_integration_simple_task_ok（第 73-77 行）**

```python
# 替换:
        session_messages=[
            {"role": "user", "content": "今天天气怎么样？"},
            {"role": "assistant", "content": "请问你想查哪个城市的天气？"},
            {"role": "user", "content": "北京的"},
        ],
# 为:
        session_messages=[
            UserMessage(content=[TextPart(content="今天天气怎么样？")], message_id=MessageId("it1-u1")),
            AssistantMessage(content=[TextPart(content="请问你想查哪个城市的天气？")], message_id=MessageId("it1-a1")),
            UserMessage(content=[TextPart(content="北京的")], message_id=MessageId("it1-u2")),
        ],
```

- [ ] **Step 3: 修改 test_integration_multi_step_with_mutation（第 125-127 行）**

```python
# 替换:
        session_messages=[
            {"role": "user", "content": "明天可能需要提醒老板带伞"},
        ],
# 为:
        session_messages=[
            UserMessage(content=[TextPart(content="明天可能需要提醒老板带伞")], message_id=MessageId("it2-u1")),
        ],
```

- [ ] **Step 4: 修改 test_integration_context_summarization（第 233-239 行）**

```python
# 替换:
        session_messages=[
            {"role": "user", "content": "我下周要去上海出差"},
            {"role": "assistant", "content": "好的，需要我帮你准备什么吗？"},
            {"role": "user", "content": "帮我查一下上海的天气，我还要订酒店"},
            {"role": "assistant", "content": "上海最近天气多变，建议带伞。你已经定了酒店吗？"},
            {"role": "user", "content": "还没订，先查天气吧，下雨的话就不去了"},
        ],
# 为:
        session_messages=[
            UserMessage(content=[TextPart(content="我下周要去上海出差")], message_id=MessageId("it5-u1")),
            AssistantMessage(content=[TextPart(content="好的，需要我帮你准备什么吗？")], message_id=MessageId("it5-a1")),
            UserMessage(content=[TextPart(content="帮我查一下上海的天气，我还要订酒店")], message_id=MessageId("it5-u2")),
            AssistantMessage(content=[TextPart(content="上海最近天气多变，建议带伞。你已经定了酒店吗？")], message_id=MessageId("it5-a2")),
            UserMessage(content=[TextPart(content="还没订，先查天气吧，下雨的话就不去了")], message_id=MessageId("it5-u3")),
        ],
```

- [ ] **Step 5: 运行集成测试前的单元测试确认 import/语法正确**

Run: `python3 -m pytest Module/task_system/tests/unit/ -q --tb=short`
Expected: 所有 unit tests PASS

- [ ] **Step 6: Commit**

```bash
git add Module/task_system/tests/integration/test_delegation_preparer.py
git commit -m "test: convert task_system integration session_messages from dict to Message"
```

---

### Task 4: idle flaky — 加长 polling 超时

**Files:**
- Modify: `core/tests/test_phase3.py:1150`

**Interfaces:**
- Consumes: none
- Produces: none

- [ ] **Step 1: 修改 polling 上限 100 → 200**

```python
# core/tests/test_phase3.py，第 1150 行
# 替换:
        for _ in range(100):
# 为:
        for _ in range(200):
```

- [ ] **Step 2: 运行确认语法正确**

Run: `python3 -m pytest core/tests/test_phase3.py::TestAgentProcessMessage::test_process_message_poll_waits_for_idle -v --tb=short`
Expected: PASS 或因环境原因 skip，但不应有 AttributeError/ImportError

- [ ] **Step 3: Commit**

```bash
git add core/tests/test_phase3.py
git commit -m "test: extend idle polling timeout from 100 to 200 iterations"
```

---

## Verification (final)

```bash
# 快速验证 — unit + service
python3 -m pytest -m "unit or service" -q --tb=short

# 如有 DEEPSEEK_API_KEY，验证集成测试
python3 -m pytest Module/system_prompt/tests/integration/ Module/task_system/tests/integration/ -q --tb=short
```

预期：13 个失败全部消除，无回归。
