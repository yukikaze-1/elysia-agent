# GenerationContext.channel/kind 对齐契约层 StrEnum — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `GenerationContext.channel/kind` 与 `open_segment` 参数从裸 `str` 对齐为 frontend_contract 契约层的
`MessageChannel`/`SegmentKind` StrEnum，并贯通到 wire 发射边界（manager inner_voice 去硬编码 + websocket handler 参数）。
**运行时零变化**（StrEnum 序列化输出值，wire 字节不变）。

**Architecture:** 单层改动。`generation.py` 顶层 import 契约枚举（frontend_contract 零依赖，无环）；字段+签名对齐 →
`loop.py`/`manager.py` 调用点改枚举值 + manager inner_voice 发射改透传 ctx → `websocket.py` handler 参数枚举化 →
测试 7 文件适配。

**Tech Stack:** Python 3.12 `enum.StrEnum`。测试 pytest（marker=unit/service），conda 环境 `Elysia`
（`/home/yomu/miniconda3/envs/Elysia/bin/python`）。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入：`from frontend_contract.ws_message import MessageChannel, SegmentKind`。
- 代码注释/README 用中文；中英混合可接受。
- **复用契约层枚举，不 core 自建**——frontend_contract 是 wire 契约单源。
- **StrEnum 是 str 子类**：`isinstance(x, MessageChannel)` 有效；`str()/json.dumps()/pydantic` 输出成员值。
- **测试断言 `get_type_hints(GenerationContext.open_segment)`**（`channel is MessageChannel`、`kind is SegmentKind`）——
  StrEnum 是 class，也可 `isinstance`，但注解断言用 get_type_hints 更精确。
- spec: `docs/superpowers/specs/2026-08-08-channel-kind-strenum-design.md`（设计依据，触及点全表）。

---

## File Structure

- **Modify:** `core/cognitive/generation.py` — `GenerationContext.channel/kind` 字段 + `open_segment` 签名
- **Modify:** `core/cognitive/loop.py` — 3 处 open_segment 调用点（:949,1340,1355）
- **Modify:** `Module/output/manager.py` — inner_voice 调用点（:436）+ 发射去硬编码（:442,449 透传 ctx）
- **Modify:** `Module/output/websocket.py` — 2 个 handler 参数（:239,259）
- **Test:** `core/cognitive/tests/test_generation.py`、`core/cognitive/tests/test_v2_empty_response.py`、
  `Module/output/tests/test_generation_conversation_id.py`、`Module/output/tests/test_output.py`、
  `Module/output/tests/test_manager_v2.py`、`Module/output/tests/test_generation_model.py`、
  `frontend_contract/tests/test_ws_message.py`
- **Docs:** `docs/architecture.md`、`CLAUDE.md`（Task 5）

---

### Task 1: generation.py 对齐契约枚举

**Files:**
- Modify: `core/cognitive/generation.py`
- Test: `core/cognitive/tests/test_generation.py`

**Interfaces:**
- Produces: `GenerationContext.channel: MessageChannel | None`、`kind: SegmentKind | None`；
  `open_segment(channel: MessageChannel, kind: SegmentKind = SegmentKind.TEXT) -> SegmentId`。
  Task 2（调用点）、Task 3（wire 边界）依赖。

- [ ] **Step 1: 写失败测试**

`core/cognitive/tests/test_generation.py` 顶部 import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`
（**注意：`from typing import get_type_hints` 已在 :3 存在**——ID 批次加的，不要重复 import）。文件末尾加：

```python
@pytest.mark.unit
def test_open_segment_channel_kind_enum_types():
    """open_segment 的 channel/kind 对齐契约层 StrEnum（get_type_hints 查注解）。"""
    hints = get_type_hints(GenerationContext.open_segment)
    assert hints["channel"] is MessageChannel
    assert hints["kind"] is SegmentKind
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py -q --tb=short`
Expected: FAIL — `hints["channel"]` 为裸 `str`（`is MessageChannel` 失败）。

- [ ] **Step 3: 最小实现**

`core/cognitive/generation.py` 顶层 import 加：

```python
from frontend_contract.ws_message import MessageChannel, SegmentKind
```

`GenerationContext` 字段（:48-49）：

```python
    channel: MessageChannel | None = None
    kind: SegmentKind | None = None
```

`open_segment` 签名（:51）：

```python
    def open_segment(self, channel: MessageChannel, kind: SegmentKind = SegmentKind.TEXT) -> SegmentId:
```

**同时适配既有测试**（test_generation.py）：
- :37 `ctx.open_segment("inner_voice")` → `ctx.open_segment(MessageChannel.INNER_VOICE)`
- :40 `assert ctx.channel == "inner_voice"` → `assert ctx.channel == MessageChannel.INNER_VOICE`
- :45,58-60 `ctx.open_segment("public_reply")` → `ctx.open_segment(MessageChannel.PUBLIC_REPLY)`

- [ ] **Step 4: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py -q --tb=short`
Expected: PASS（新增类型断言 + 适配后既有全绿——运行时 StrEnum==str，裸 str 传参本就通过）。

- [ ] **Step 5: 提交**

```bash
git add core/cognitive/generation.py core/cognitive/tests/test_generation.py
git commit -m "refactor(cognitive): GenerationContext.channel/kind 对齐契约层 StrEnum + open_segment 签名"
```

### Task 2: loop.py + manager.py 调用点

**Files:**
- Modify: `core/cognitive/loop.py`（3 处 open_segment 调用）
- Modify: `Module/output/manager.py`（inner_voice 调用 + 发射去硬编码）
- Test: `core/cognitive/tests/test_v2_empty_response.py`、`Module/output/tests/test_manager_v2.py`、
  `Module/output/tests/test_generation_model.py`、`Module/output/tests/test_generation_conversation_id.py`（open_segment 调用）

**Interfaces:**
- Consumes: `open_segment(channel: MessageChannel, kind: SegmentKind = SegmentKind.TEXT)`（Task 1）。
- Produces: loop 3 处 + manager inner_voice 调用点改枚举；manager inner_voice 发射 `channel=ctx.channel, kind=ctx.kind`
  （去硬编码）。Task 3（wire 边界）依赖。

- [ ] **Step 1: 写失败测试**

无新增断言——本任务把调用点改枚举，验证方式是既有 open_segment 相关测试仍绿。**本任务唯一 wire 相关改动
（manager.py:442/449 去硬编码透传 ctx）由既有断言守卫：`test_manager_v2.py:63-74` 的
`v2_sent[0]["channel"] == "inner_voice"`——透传后 ctx.channel == MessageChannel.INNER_VOICE == "inner_voice"，
该断言验证 wire 不变；若误把 close_segment 提前到广播前 → ctx.channel 为 None → 契约校验失败 → 测试红。**
**Step 3 改完跑 Task 1 新增的类型断言 + 本任务相关测试确认不回归。**

- [ ] **Step 2: 最小实现**

`core/cognitive/loop.py`（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`）：

```python
# :949,1340,1355
ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
```

`Module/output/manager.py`（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`）：
- :436 `ctx.open_segment("inner_voice", "text")` → `ctx.open_segment(MessageChannel.INNER_VOICE, SegmentKind.TEXT)`
- :442,449 硬编码 `channel="inner_voice", kind="text"` → `channel=ctx.channel, kind=ctx.kind`（对齐 :376/:420 透传模式）

**同时适配既有测试**：
- `test_v2_empty_response.py:50`、`test_manager_v2.py:25`、`test_generation_model.py:33`、
  `test_generation_conversation_id.py:29`：`open_segment("public_reply", "text")` →
  `open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)`（各文件 import 加两个枚举）。

- [ ] **Step 3: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests/test_generation.py core/cognitive/tests/test_v2_empty_response.py Module/output/tests/test_manager_v2.py Module/output/tests/test_generation_model.py Module/output/tests/test_generation_conversation_id.py -q --tb=short`
Expected: PASS（manager inner_voice 透传后 wire 值不变——`ctx.channel == MessageChannel.INNER_VOICE == "inner_voice"`）。

- [ ] **Step 4: 提交**

```bash
git add core/cognitive/loop.py Module/output/manager.py core/cognitive/tests/test_v2_empty_response.py Module/output/tests/test_manager_v2.py Module/output/tests/test_generation_model.py Module/output/tests/test_generation_conversation_id.py
git commit -m "refactor(cognitive): loop/manager 调用点改契约枚举 + manager inner_voice 发射去硬编码透传 ctx"
```

### Task 3: websocket.py handler 参数枚举化

**Files:**
- Modify: `Module/output/websocket.py`
- Test: `Module/output/tests/test_generation_conversation_id.py`（:53,75 handler 调用）、`Module/output/tests/test_output.py`（:791）

**Interfaces:**
- Consumes: `MessageChannel`/`SegmentKind`（契约层）。
- Produces: `on_generation_segment_started/completed(channel: MessageChannel, kind: SegmentKind, ...)`。
  Task 4（frontend_contract 测试）独立。

- [ ] **Step 1: 写失败测试**

无新增断言——handler 参数枚举化是类型卫生，pydantic 接受枚举与 str 等价。适配测试调用点并跑既有测试确认不回归。

- [ ] **Step 2: 最小实现**

`Module/output/websocket.py`（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`）：

```python
    # :239
    async def on_generation_segment_started(self, *, generation_id: str, segment_id: str,
                                            segment_index: int, channel: MessageChannel, kind: SegmentKind,
                                            conversation_id: str | None = None) -> None:
    # :259
    async def on_generation_segment_completed(self, *, generation_id: str, segment_id: str,
                                              segment_index: int, channel: MessageChannel, kind: SegmentKind,
                                              content: str | None = None,
                                              conversation_id: str | None = None) -> None:
```
（body 构造契约模型 `channel=channel, kind=kind` 直接传枚举。**`on_generation_completed`（:273）`channel: str = "public_reply"` 不改**——父回复 channel。）

**同时适配既有测试**（websocket handler 调用点）：
- `test_generation_conversation_id.py:53,75`、`test_output.py:791`：`channel="public_reply"` →
  `channel=MessageChannel.PUBLIC_REPLY`、`kind="text"` → `kind=SegmentKind.TEXT`（import 加两个枚举）。

- [ ] **Step 3: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest Module/output/tests/test_generation_conversation_id.py Module/output/tests/test_output.py -q --tb=short`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add Module/output/websocket.py Module/output/tests/test_generation_conversation_id.py Module/output/tests/test_output.py
git commit -m "refactor(output): websocket segment handler 参数对齐契约枚举（wire 边界）"
```

### Task 4: frontend_contract 测试适配

**Files:**
- Test: `frontend_contract/tests/test_ws_message.py`

**Interfaces:**
- Consumes: `MessageChannel`/`SegmentKind`（契约层自身枚举）。
- Produces: 契约模型构造点改枚举（类型卫生；frontend_contract 生产代码零改动）。

- [ ] **Step 1: 写失败测试**

无新增断言——frontend_contract 生产代码零改动，仅测试构造点改枚举（pydantic 接受枚举，运行时本就通过）。

- [ ] **Step 2: 最小实现**

`frontend_contract/tests/test_ws_message.py`（import 加 `from frontend_contract.ws_message import MessageChannel, SegmentKind`）：
- :98,183 `channel="public_reply", kind="text"` → `channel=MessageChannel.PUBLIC_REPLY, kind=SegmentKind.TEXT`
- :205 `channel="public_reply", kind="text", content="你好", ...` → 同上
- :125,216,225 `channel="public_reply"`（GenerationCompletedMessage）→ `channel=MessageChannel.PUBLIC_REPLY`

- [ ] **Step 3: 跑测试确认通过**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest frontend_contract/tests/test_ws_message.py -q --tb=short`
Expected: PASS。

- [ ] **Step 4: 提交**

```bash
git add frontend_contract/tests/test_ws_message.py
git commit -m "test(frontend_contract): 契约模型构造点 channel/kind 用枚举（类型卫生，生产零改动）"
```

### Task 5: 文档更新

**Files:**
- Modify: `docs/architecture.md`（generation.py 描述）
- Modify: `CLAUDE.md`（cognitive 描述）

**Interfaces:**
- 无代码接口。spec §5 要求文档同步——初稿遗漏，plan review 补。

- [ ] **Step 1: 更新 `docs/architecture.md`**

`docs/architecture.md` 的 cognitive 树 `generation.py` 行（约 :1024，含上批次 ID NewType 描述）补 channel/kind 对齐：

```
│   │   ├── generation.py     #     GenerationContext: generation_id/assistant_message_id/conversation_id 预分配（ID 字段 NewType；channel/kind 对齐契约层 MessageChannel/SegmentKind StrEnum）
```

- [ ] **Step 2: 更新 `CLAUDE.md`**

`CLAUDE.md` cognitive 树 `generation.py` 描述（约 :1024-1025 区）补一句 channel/kind 契约枚举；
`Module/output` 描述如提及 segment 通道/类型，同步补枚举。

- [ ] **Step 3: 提交**

```bash
git add docs/architecture.md CLAUDE.md
git commit -m "docs: GenerationContext.channel/kind 对齐契约枚举——architecture/CLAUDE 记录"
```

### Task 6: 全量回归 + 门禁

**Files:** 无新增（验证任务）。

- [ ] **Step 1: 定向回归受影响模块**

```bash
/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/cognitive/tests Module/output/tests frontend_contract/tests core/input/tests -m "not slow" -q --tb=short
```
Expected: PASS。

- [ ] **Step 2: 全项目门禁**

```bash
make test-normal
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests
make check-types
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m frontend_contract --check
make lint
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m ruff check core/ Module/ frontend_contract/ --exclude __pycache__
```
Expected: 全绿（`make check-types` 必须过——frontend_contract 生产零改动，快照无漂移）。

- [ ] **Step 3: 提交（若无代码改动则跳过）**

```bash
git status
# 若有遗漏修复，git add + commit；否则无提交
```

## Self-Review（已执行）

1. **Spec 覆盖**：字段+签名（T1）、loop+manager 调用点（T2）、wire 边界 handler（T3）、frontend_contract 测试（T4）、
   文档（T5）、回归门禁（T6）——全部映射到 spec §1-5（T5 对应 spec §5 文档，plan review 补）。
2. **占位符扫描**：无 TBD/TODO；每步含可执行代码与运行命令。
3. **类型一致性**：`MessageChannel`/`SegmentKind` 引用与契约层定义一致；测试适配统一用枚举成员。
4. **已知耦合**：T1 改签名后，T2/T3 调用点运行时仍安全（StrEnum==str）；manager inner_voice 去硬编码（T2）是
   唯一行为对齐（wire 值不变，由 test_manager_v2.py:63-74 断言守卫）；`on_generation_completed`（websocket.py:273）
   父回复 channel 明确不动。

## Plan Review 修复记录（code-review 后并入）

| # | Review 发现 | 修复 |
|---|------------|------|
| 1 | spec §5 文档更新（architecture/CLAUDE.md）未入 plan，Self-Review 却称映射 §1-5 | 新增 Task 5 文档更新，原回归任务重编号为 Task 6 |
| 2 | Task 1 Step 1 指示加 `from typing import get_type_hints`，但 test_generation.py:3 已存在 | 修正指令：仅加 frontend_contract import，注明勿重复 |
| 3 | Task 2 唯一 wire 改动（manager 去硬编码）未点名守卫测试 | 补 test_manager_v2.py:63-74 断言为 wire 不变性守卫 |
