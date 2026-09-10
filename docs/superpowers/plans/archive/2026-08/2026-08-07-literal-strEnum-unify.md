# Literal → StrEnum 统一重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把项目中非判别受限字段的 `Literal` 全部换成 `StrEnum`，判别字段统一为 `Literal[Enum.MEMBER]` 引用形式，消除魔数散落与枚举未引用。

**Architecture:** 枚举按消费方归属定义（`frontend_contract` 契约层零依赖，不能 import core/Module；`core/schema` 共享类型；`Module/*` 自持）。判别字段受 Pydantic 约束必须 `Literal`，但引用枚举成员做单一来源。前端 gen-ts 发射命名别名（TS 形状变化）记录不阻塞。

**Tech Stack:** Python 3.11+ / Pydantic 2 / StrEnum / gen_ts 发射器 / vue-tsc 门禁。

**Spec:** `docs/superpowers/specs/archive/2026-08/2026-08-07-literal-strEnum-unify-design.md`

## Global Constraints

- **判别字段必须 Literal**（Pydantic 硬约束）：`type`/`event_type`/`action` 判别字段只能 `Literal[Enum.MEMBER]`，不能换 StrEnum 类型注解。
- **frontend_contract 零依赖**：不能 import `core/`/`Module/`。`rest.py` 的 `Role` 必须自建，值对齐 `core.schema.message.MessageRole`（user/assistant/tool）。
- **StrEnum 统一**：新建枚举一律 `from enum import StrEnum`（不用 `(str, Enum)`）。
- **禁相对导入**（ruff TID252）：`from core.xxx` / `from Module.xxx` 绝对导入。
- **中文注释**、**中文 docstring**。
- **每任务 TDD**：先写失败测试 → 确认红 → 实现 → 确认绿 → 提交。
- 改 `frontend_contract/` 后必须跑 `make gen-types` + `make check-types`；改前端后跑 `cd frontend && npx vue-tsc --noEmit`。
- **执行顺序约束**：Task 6（ws_message）的 `GenerationCompletedMessage.finishReason` 依赖 `FinishReason`（Task 7 在 rest.py 定义）。实施时**先做 Task 7 的 rest 枚举（至少 FinishReason）再实现 Task 6**；或将 Task 6/7 合并为一次 frontend_contract 提交。禁止在 Task 6 内临时定义 FinishReason（rest 与 ws_message 有 import 依赖，双向会成环）。

---

### Task 1: `Module/tool/protocol.py` 枚举化（ToolResultType + ToolStatus）

**Files:**
- Modify: `Module/tool/protocol.py:18`（ResultType 别名）、`:88`（status）、`:259/:507/:525`（result_type 字段，**共 3 处使用点**）
- Modify: `Module/tool/__init__.py:7,35`（`ResultType` import + `__all__`——删除别名后必须同步，否则 `Module.tool` import 失败）
- Test: `Module/tool/tests/test_protocol.py`（新建或现有）

**Interfaces:**
- Produces: `ToolResultType(StrEnum)`（text/image/audio/file/mixed）、`ToolStatus(StrEnum)`（pending/processing/done/failed）
- Consumes: 无（Module 层独立）

- [ ] **Step 1: 写失败测试**（验证枚举类型 + 值 + 默认值字段）

```python
"""ToolResultType/ToolStatus 枚举化测试。"""
from Module.tool.protocol import ToolResultType, ToolStatus


class TestToolResultType:
    def test_members_match_old_literal(self):
        assert {m.value for m in ToolResultType} == {"text", "image", "audio", "file", "mixed"}

    def test_is_str_enum(self):
        assert ToolResultType.TEXT == "text"


class TestToolStatus:
    def test_members_match_old_literal(self):
        assert {m.value for m in ToolStatus} == {"pending", "processing", "done", "failed"}

    def test_default_is_processing(self):
        # 既有默认值 "processing"
        assert ToolStatus.PROCESSING == "processing"
```

- [ ] **Step 2: 运行确认红**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest Module/tool/tests/test_protocol.py -v`
Expected: FAIL（ImportError: cannot import name 'ToolResultType'）

- [ ] **Step 3: 实现**——`Module/tool/protocol.py`

```python
from enum import StrEnum

class ToolResultType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    MIXED = "mixed"

class ToolStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
```
删除 `ResultType = Literal[...]` 别名；`result_type: ToolResultType = ToolResultType.TEXT`（**:259/:507/:525 三处**）；`status: ToolStatus = ToolStatus.PROCESSING`；**同步** `Module/tool/__init__.py` 的 `ResultType` import（→ `ToolResultType`）与 `__all__`。

- [ ] **Step 4: 运行确认绿 + 既有测试**

Run: `pytest Module/tool/tests/ -q`
Expected: PASS（若 `Module/tool/__init__.py` 未同步会导致 collection ImportError，务必先改）

- [ ] **Step 5: 提交**

```bash
git add Module/tool/protocol.py Module/tool/__init__.py Module/tool/tests/test_protocol.py
git commit -m "refactor(tool): ResultType/status Literal → StrEnum 枚举"
```

---

### Task 2: `Module/workflow_engine/schema/internal.py` 枚举化（ExperienceStatus）

**Files:**
- Modify: `Module/workflow_engine/schema/internal.py:522,540`（`ExperienceRecord.status`）
- Test: `Module/workflow_engine/tests/...`（找现有 schema 测试，无则建 `test_schema.py`）

**Interfaces:**
- Produces: `ExperienceStatus(StrEnum)`（unverified/verified/deprecated）
- Consumes: 无
- **命名注意**：**不能叫 `WorkflowStatus`**——`Module/workflow_engine/schema/internal.py:18` 已 import `Module/workflow_contract.schema.WorkflowStatus`（11 态：PENDING…COMPLETED），新建同名会遮蔽 import 导致 `WorkflowStatus.PENDING`（internal.py:119）AttributeError 崩溃。目标类名是 `ExperienceStatus`。

- [ ] **Step 1: 写失败测试**

```python
from Module.workflow_engine.schema.internal import ExperienceRecord, ExperienceStatus


class TestExperienceStatus:
    def test_members_match_old_literal(self):
        assert {m.value for m in ExperienceStatus} == {"unverified", "verified", "deprecated"}

    def test_experience_record_status_default(self):
        # 既有默认值 "unverified"
        assert ExperienceRecord().status == "unverified"
```

- [ ] **Step 2: 确认红** → ImportError（`ExperienceStatus` 未定义）
- [ ] **Step 3: 实现**：`from enum import StrEnum` + `class ExperienceStatus(StrEnum): UNVERIFIED="unverified"; VERIFIED="verified"; DEPRECATED="deprecated"`；`ExperienceRecord.status: ExperienceStatus = ExperienceStatus.UNVERIFIED`
- [ ] **Step 4: 确认绿**（含既有 workflow_engine 测试不被遮蔽破坏）
- [ ] **Step 5: 提交** `refactor(workflow_engine): ExperienceRecord.status Literal → ExperienceStatus 枚举`

---

### Task 3: `core/schema/event.py` 枚举统一（判别字段引用 EventType + VoiceReason）

**Files:**
- Modify: `core/schema/event.py:14-52`
- Test: `core/schema/tests/test_event.py`（现有）

**Interfaces:**
- Consumes: `core/schema/agent.py` 已有 `EventType`（INTERRUPT/STATE_CHANGE/USER_INPUT/SYSTEM_TICK/TASK_RESULT）
- Produces: `VoiceReason(StrEnum)`（silence_timeout/time_of_day）

- [ ] **Step 1: 写失败测试**（判别字段 annotation 真实引用 EventType 成员 + 非法拒绝）

```python
from typing import get_args, get_origin, Literal
from pydantic import TypeAdapter
from core.schema.event import InterruptData, SystemTickData, EventData
from core.schema.agent import EventType


class TestEventTypeEnumBinding:
    def test_event_type_discriminator_references_enum_member(self):
        """判别字段必须是 Literal[EventType.MEMBER] 形式（防裸字符串漂移）。"""
        ann = InterruptData.model_fields["event_type"].annotation
        assert get_origin(ann) is Literal
        assert get_args(ann) == (EventType.INTERRUPT,)

    def test_discriminator_still_rejects_bad_value(self):
        # 判别值引用枚举后，非法值仍被拒（part.py 已证此模式）
        try:
            TypeAdapter(EventData).validate_python({"event_type": "nope"})
            raise AssertionError("should reject")
        except Exception:
            pass

    def test_system_tick_reason_is_enum(self):
        from core.schema.event import VoiceReason
        assert {m.value for m in VoiceReason} == {"silence_timeout", "time_of_day"}
        ann = SystemTickData.model_fields["reason"].annotation
        assert ann is VoiceReason
        ta = TypeAdapter(SystemTickData)
        v = ta.validate_python({"event_type": "system_tick", "reason": "silence_timeout"})
        assert v.reason == "silence_timeout"
```

- [ ] **Step 2: 确认红** → ImportError VoiceReason
- [ ] **Step 3: 实现**

```python
from core.schema.agent import EventType

class InterruptData(BaseModel):
    event_type: Literal[EventType.INTERRUPT] = EventType.INTERRUPT
    ...
class SystemTickData(BaseModel):
    event_type: Literal[EventType.SYSTEM_TICK] = EventType.SYSTEM_TICK
    reason: VoiceReason
```
（其余三个类同理；新建 `class VoiceReason(StrEnum): SILENCE_TIMEOUT="silence_timeout"; TIME_OF_DAY="time_of_day"`）

- [ ] **Step 4: 确认绿**（`core/schema/tests/test_event.py` + 既有）
- [ ] **Step 5: 提交** `refactor(schema): event.py 判别字段引用 EventType + VoiceReason 枚举`

---

### Task 4: `core/schema/memory.py` 枚举统一（CompareActionType）

**Files:**
- Modify: `core/schema/memory.py:30-58`
- Test: `core/schema/tests/test_memory.py`（现有）

**Interfaces:**
- Produces: `CompareActionType(StrEnum)`（skip/insert/update/merge）
- **命名注意**：不能叫 `CompareAction`——已被 `memory.py:59` 判别联合别名占用

- [ ] **Step 1: 写失败测试**

```python
from typing import get_args, get_origin, Literal
from pydantic import TypeAdapter
from core.schema.memory import CompareAction, CompareActionType, SkipAction


class TestCompareActionType:
    def test_members_match(self):
        assert {m.value for m in CompareActionType} == {"skip", "insert", "update", "merge"}

    def test_action_discriminator_references_enum_member(self):
        """判别字段必须是 Literal[CompareActionType.X]（防裸字符串漂移）。"""
        ann = SkipAction.model_fields["action"].annotation
        assert get_origin(ann) is Literal
        assert get_args(ann) == (CompareActionType.SKIP,)

    def test_discriminator_rejects_bad(self):
        ta = TypeAdapter(CompareAction)
        try:
            ta.validate_python({"action": "nope"})
            raise AssertionError("should reject")
        except Exception:
            pass
```

- [ ] **Step 2: 确认红** → ImportError CompareActionType
- [ ] **Step 3: 实现**：新建 `CompareActionType` 枚举；四个 Action 类的 `action: Literal[CompareActionType.X] = CompareActionType.X`
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 提交** `refactor(schema): memory.py 判别字段引用 CompareActionType 枚举`

---

### Task 5: `core/schema/message.py` role 枚举化

**Files:**
- Modify: `core/schema/message.py:106`（`ClientToolMessage.role: Literal["tool"]`）
- Test: `core/schema/tests/test_message.py`（现有）

**Interfaces:**
- Consumes: 已有 `MessageRole`（USER/ASSISTANT/TOOL/SYSTEM）
- Produces: 无新枚举

- [ ] **Step 1: 写失败测试**

```python
from core.schema.message import ClientToolMessage, MessageRole


class TestClientToolMessageRole:
    def test_role_is_enum_member(self):
        # TypedDict 字段类型引用 MessageRole.TOOL
        assert ClientToolMessage.__annotations__["role"] == MessageRole.TOOL
```

- [ ] **Step 2: 确认红**（当前是 `Literal["tool"]`）
- [ ] **Step 3: 实现**：`role: MessageRole.TOOL`
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 提交** `refactor(schema): ClientToolMessage.role 引用 MessageRole.TOOL`

---

### Task 6: `frontend_contract/ws_message.py` 枚举化（channel/kind/format/service/reason/status + 恒值 channel）

**Files:**
- Modify: `frontend_contract/ws_message.py`（枚举新建 + 字段替换）
- Test: `frontend_contract/tests/test_ws_message.py` + `test_gen_ts.py`（新增枚举发射测试）

**Interfaces:**
- Produces: `MessageChannel`、`SegmentKind`、`AudioFormat`、`ServiceName`、`VoiceEndReason`、`AckStatus`（全部 `StrEnum`）
- Consumes: `FinishReason`（from `frontend_contract.rest`，Task 7 定义）——ws_message 的 `GenerationCompletedMessage.finishReason` 用其值（Task 6 先于 Task 7 完成时，需先建 `FinishReason` 或调整顺序；实际 Task 6/7 可合并执行或按依赖先建 rest 枚举）

- [ ] **Step 1: 写失败测试**（gen_ts 发射命名别名 + 字段引用）

```python
from frontend_contract.gen_ts import generate_ts
from frontend_contract.ws_message import (
    AckStatus, AudioFormat, GenerationCompletedMessage, GenerationSegmentStartedMessage,
    MessageChannel, SegmentKind, ServiceName, UserAckMessage, VoiceEndReason,
)


class TestWsEnums:
    def test_new_enums_defined(self):
        assert MessageChannel.INNER_VOICE == "inner_voice"
        assert SegmentKind.MARKDOWN == "markdown"
        assert AudioFormat.WAV == "wav"
        assert ServiceName.STT == "stt"
        assert VoiceEndReason.TIMEOUT == "timeout"

    def test_user_ack_status_annotation_is_enum(self):
        """UserAckMessage.status 必须是 AckStatus 类型（防回落 Literal）。"""
        assert UserAckMessage.model_fields["status"].annotation is AckStatus

    def test_gen_ts_emits_named_aliases(self):
        out = generate_ts([GenerationSegmentStartedMessage, GenerationCompletedMessage])
        assert "export type MessageChannel =" in out
        assert "export type SegmentKind =" in out
        assert "'inner_voice'" in out
```

- [ ] **Step 2: 确认红** → ImportError 新枚举
- [ ] **Step 3: 实现**：
  - 新建 6 枚举（`from enum import StrEnum`）；
  - `channel: MessageChannel`（GenerationSegmentStartedMessage/CompletedMessage 2 处非恒值）+ `GenerationCompletedMessage.channel: Literal[MessageChannel.PUBLIC_REPLY] = MessageChannel.PUBLIC_REPLY`（**必须 `Literal[...]` 包裹**——裸成员注解 `channel: MessageChannel.PUBLIC_REPLY` 会让 gen_ts `_ts_type` 落 `__name__` 抛 AttributeError，实测确认）；
  - `kind: SegmentKind`（2 处）；
  - `format: AudioFormat`（AudioMessage/AudioStartMessage 2 处，默认 `AudioFormat.WAV`）；
  - `service: ServiceName`（ServiceModeResultMessage 含 llm）；`SetServiceMessage.service: Literal[ServiceName.STT, ServiceName.TTS]`（**不直接用 `ServiceName`**——那会加宽契约接受 "llm"）；
  - `reason: VoiceEndReason`（VoiceSessionEndedMessage）；
  - `UserAckMessage.status: AckStatus = Field(description="...")`（无默认——原字段无默认）；
  - `GenerationCompletedMessage.finishReason: FinishReason`（直接用 StrEnum——非判别字段 D1 全换；`FinishReason` from `frontend_contract.rest` import——ws_message 已 import rest 的 `ConversationUsage`，同模块互引可行）；
  - 判别字段 `type: Literal["..."]` **保持不动**；`ActionMessage.action`/`CommandMessage.command` 保持。
- [ ] **Step 4: 确认绿 + 门禁**：`pytest frontend_contract/tests/ -q` + `make gen-types` + `make check-types`
- [ ] **Step 5: 提交** `refactor(frontend_contract): ws_message 非判别字段 Literal → StrEnum`

---

### Task 7: `frontend_contract/rest.py` 枚举化（FinishReason/RepoCallStatus/GenerationStatusEnum/Role）

**Files:**
- Modify: `frontend_contract/rest.py`
- Test: `frontend_contract/tests/test_rest.py`（现有）

**Interfaces:**
- Produces: `FinishReason`（stop/tool_calls）、`RepoCallStatus`（9 值）、`GenerationStatusEnum`（5 值）、`Role`（user/assistant/tool，契约层自建）
- Consumes: 无（不 import core）

- [ ] **Step 1: 写失败测试**

```python
from frontend_contract.rest import (
    DesktopCallStatus, FinishReason, GenerationStatus, GenerationStatusEnum,
    RepoCallStatus, Role, SearchMessageItem,
)


class TestRestEnums:
    def test_finish_reason(self):
        assert {m.value for m in FinishReason} == {"stop", "tool_calls"}

    def test_repo_call_status(self):
        assert {m.value for m in RepoCallStatus} == {
            "pending", "accepted", "running", "succeeded", "failed",
            "rejected", "cancelled", "expired", "offline",
        }

    def test_generation_status(self):
        assert {m.value for m in GenerationStatusEnum} == {
            "running", "completed", "failed", "cancelled", "interrupted",
        }

    def test_role(self):
        assert {m.value for m in Role} == {"user", "assistant", "tool"}
```

- [ ] **Step 2: 确认红** → ImportError
- [ ] **Step 3: 实现**：新建 4 枚举；`GenerationStatus.finishReason: Optional[FinishReason]`（**rest.py 仅 1 处**——另一处 finishReason 在 ws_message.py:321 归 Task 6）；`DesktopCallStatus.status: RepoCallStatus`、`GenerationStatus.status: GenerationStatusEnum`、`SearchMessageItem.role: Role`；`ok: Literal[True]/[False]` 保持
- [ ] **Step 4: 确认绿 + 门禁**
- [ ] **Step 5: 提交** `refactor(frontend_contract): rest 非判别字段 Literal → StrEnum`

---

### Task 8: 前端消费点验证（vue-tsc 门禁 + 记录）

**Files:**
- Verify: `frontend/src/types/contract.generated.ts`、`frontend/src/**/*.ts/.vue`
- Modify: `docs/superpowers/specs/archive/2026-08/2026-08-07-literal-strEnum-unify-design.md`（确认限制记录）

**Interfaces:**
- Consumes: Task 6/7 生成的 `contract.generated.ts`

- [ ] **Step 1: 重新生成 TS + 前端类型检查**

```bash
cd /home/yomu/elysia-agent && make gen-types && make check-types
cd frontend && npx vue-tsc --noEmit
```

- [ ] **Step 2: 处理 TS 形状变化**：若 vue-tsc 报错（如 `status` 类型从内联字面量变 `ToolResultType`），修复前端消费点或确认类型兼容（`msg.channel === 'inner_voice'` 字符串比较仍兼容）
- [ ] **Step 3: 记录**：确认 spec §7「前端 TS 形状变化」限制准确
- [ ] **Step 4: 提交** `chore: 前端 vue-tsc 通过（StrEnum 命名别名形状确认）`（若前端无改动则 amend 进 Task 6/7 或跳过）

---

### Task 9: 全量回归 + 文档更新

**Files:**
- Modify: `docs/electron-backend-baseline.md`（如提及 Literal/枚举，同步）、`README.md`（无必要则跳过）
- Test: 全量

- [ ] **Step 1: 全量测试**

```bash
cd /home/yomu/elysia-agent
/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest -m "unit or service" -q --ignore=Module/workflow_engine/tests
```

- [ ] **Step 2: lint**

```bash
/home/yomu/miniconda3/envs/Elysia/bin/python -m ruff check core/ Module/ frontend_contract/ --exclude __pycache__
```

- [ ] **Step 3: 文档**：确认 spec §7 限制记录完整；无其他文档需改（todo.md 若无相关条目跳过）
- [ ] **Step 4: 提交** `docs: Literal→StrEnum 重构完成，spec 限制记录确认`
