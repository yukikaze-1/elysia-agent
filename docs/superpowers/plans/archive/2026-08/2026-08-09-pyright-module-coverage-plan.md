# pyright 覆盖扩展到 Module/ + frontend_contract/ 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 pyright 门禁从 core/ 扩展到 Module/ + frontend_contract/，全仓生产代码纳入类型验证。

**Architecture:** `pyrightconfig.json` include 扩展（Task 1 前置）；Module 28 errors 按 4 类修（Optional 守卫 assert / 契约枚举收口 / int|float 泛型 / 散落真修）；最后全量验证 + 文档同步。

**Tech Stack:** pyright（Elysia env）+ ruff + pytest + Python 3.12。

## Global Constraints

- **include 扩展**：`pyrightconfig.json` `include` 改为 `["core/", "Module/", "frontend_contract/"]`；`exclude`（tests/test_*.py/__pycache__）不变——测试文件继续豁免。
- **28 errors 全清**：真修，不 `# type: ignore`（唯一豁免：milvus.py:118 库 stub 误报，允许 `cast(bool, ...)` 或 `# type: ignore[reportReturnType]`，openai_compat 已有同类先例）。
- **无运行时行为变化**：所有修法都是类型层（assert 窄化 / 枚举收口 / 泛型 / 容器注解）；StrEnum 收口后 wire 字节不变（§2.2）。
- **验证命令**：pyright = `conda run -n Elysia pyright`（预期 0 errors + core monitoring 1 条既有 warning 非 error）；lint = `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`；测试 = `conda run -n Elysia python -m pytest <path> -q --tb=short`。
- **运行顺序**：Task 1 前置（改 include 后 pyright 才检查 Module），Task 2-5 按文件分组可并行（不同文件），Task 6 收尾。

---

### Task 1: pyrightconfig include 扩展 + frontend 验证

**Files:**
- Modify: `pyrightconfig.json`
- Verify: frontend_contract/（预期 0 error）

- [ ] **Step 1: 扩展 include**

`pyrightconfig.json`：

```json
{
  "include": ["core/", "Module/", "frontend_contract/"],
  "exclude": ["**/tests/**", "**/test_*.py", "**/__pycache__"],
  "pythonVersion": "3.12",
  "typeCheckingMode": "basic",
  "reportMissingImports": false
}
```

- [ ] **Step 2: 验证基线**

Run: `conda run -n Elysia pyright`（Expected: 28 errors，全在 Module/；core monitoring 1 warning）
Run: `conda run -n Elysia pyright frontend_contract`（Expected: 0 errors）——frontend 无需清理，直接纳入。

- [ ] **Step 3: Commit**

```bash
git add pyrightconfig.json
git commit -m "chore(pyright): include 扩展到 Module/ + frontend_contract/（基线 Module 28 errors 待清）"
```

---

### Task 2: sub_agent.py 11 errors（Optional 守卫 assert 窄化）

**Files:**
- Modify: `Module/workflow_engine/core/sub_agent/sub_agent.py`

**背景**：组件字段 `Optional`（:99-105 `self._planner: Planner | None = None` 等），`_require()`（:116，返回 None）守护后 pyright 不窄化。修法：每个 `_require(...)` 调用后紧跟 `assert self._xxx is not None`（多组件调用点逐组件 assert）。`_require` 本身不改。

**6 个调用点**（各需 assert 的组件，对照 pyright error 逐点补）：

- [ ] **Step 1: :219** `self._require("_checkpoint_mgr", "_mailbox")` → 后加 `assert self._checkpoint_mgr is not None` + `assert self._mailbox is not None`（消除 :220 `load`/`task_id` 2 errors）
- [ ] **Step 2: :231** `self._require("_context")` → 后加 `assert self._context is not None`（消除 :241）
- [ ] **Step 3: :316** `self._require("_planner", "_plan_validator")` → 后加两个 assert（消除 :318 两处）
- [ ] **Step 4: :341** `self._require("_orchestrator")` → 后加 assert（消除 :343）
- [ ] **Step 5: :412** `self._require("_checkpoint_mgr")` → 后加 assert（消除 :414）
- [ ] **Step 6: :440** `self._require("_replanner", "_plan_validator", "_orchestrator", "_checkpoint_mgr")` → 后加四个 assert（消除 :443 三处 + :444）

- [ ] **Step 7: 验证**

Run: `conda run -n Elysia pyright Module`（Expected: 本文件 0 error；可能仍有 Task 3-5 未修的 error，忽略）
Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_sub_agent.py -q --tb=short`

- [ ] **Step 8: Commit**

```bash
git add Module/workflow_engine/core/sub_agent/sub_agent.py
git commit -m "fix(pyright): sub_agent Optional 组件 _require 后 assert 窄化（11 errors / 6 调用点）"
```

---

### Task 3: websocket.py 6 errors（契约枚举/Literal 收口）

**Files:**
- Modify: `Module/output/websocket.py`

**背景**：契约构造点传 str 给枚举/Literal 字段。已核对字段类型（frontend_contract/ws_message.py）。

- [ ] **Step 1: on_user_message_ack 的 status 收口**

`:226` `UserAckMessage(..., status=status, ...)`：`status` 参数是 `str`（:220），字段是 `AckStatus`。改 `status=AckStatus(status)`（`AckStatus` 定义于 `frontend_contract/ws_message.py`，并入 :43 现有 import 行）。

- [ ] **Step 2: on_generation_completed 参数 Literal 化 + finishReason 收口**

签名（:270-275）：
```python
async def on_generation_completed(self, *, generation_id: str, message_id: str,
                                  finish_reason: str, usage: Any, model: str,
                                  mood: str | None = None,
                                  completeness: Literal["complete"] = "complete",
                                  channel: Literal[MessageChannel.PUBLIC_REPLY] = MessageChannel.PUBLIC_REPLY,
                                  conversation_id: str | None = None, ...)
```
- **import**：`from typing import Any` → `from typing import Any, Literal`（websocket.py 当前无 `Literal`、无 `from __future__ import annotations`，不补会 NameError 模块加载崩溃）
- **import 来源**：`AckStatus`/`AudioFormat` 定义于 `frontend_contract/ws_message.py`——并入 :43 现有行 `from frontend_contract.ws_message import MessageChannel, SegmentKind`；`FinishReason` 定义于 `frontend_contract/rest.py`——新增 `from frontend_contract.rest import FinishReason`
- `completeness: str = "complete"` → `Literal["complete"] = "complete"`
- `channel: str = "public_reply"` → `Literal[MessageChannel.PUBLIC_REPLY] = MessageChannel.PUBLIC_REPLY`
- 构造点（:289-291）：`channel=channel`、`completeness=completeness` 直接传（已 Literal）；`:290` `finishReason=FinishReason(finish_reason)`
- 调用方 `manager.py:487` 未传 channel/completeness 走默认——无改动（manager 经 `**kwargs: Any` 分发，改签名不产生新 error）。

- [ ] **Step 3: audio_start/audio_end 的 format 收口**

`:165` `AudioStartMessage(format=format, ...)`、`:182` `AudioEndMessage(format=format, ...)`：`format` 参数是 `str`，字段 `AudioFormat` / `AudioFormat | None`。改 `format=AudioFormat(format)`（`AudioFormat` 定义于 `frontend_contract/ws_message.py`，并入 :43 现有 import 行）。

- [ ] **Step 4: 验证**

Run: `conda run -n Elysia pyright Module/output`（Expected: 本文件 0 error）
Run: `conda run -n Elysia python -m pytest Module/output/tests/ -q --tb=short`

- [ ] **Step 5: Commit**

```bash
git add Module/output/websocket.py
git commit -m "fix(pyright): output/websocket 契约枚举/Literal 收口（AudioFormat/AckStatus/FinishReason + channel/completeness 参数 Literal 化）"
```

---

### Task 4: schema.py 5 errors（_resolve_field 泛型化 + 守卫）

**Files:**
- Modify: `Module/workflow_contract/schema.py`

**背景**：`_resolve_field`（:193 附近）闭包内 `task_val > global_val`（global_val 可能 None）报错；返回值 `int | float | None` 传给 `int | None`/`float | None` 混合字段。

- [ ] **Step 1: _resolve_field 泛型化 + global_val 守卫**

```python
T = TypeVar("T", bound=int | float)

def _resolve_field(
    name: str, task_val: T | None, global_val: T | None, clamp_reason: str = "",
) -> T | None:
    """解析单个字段：None → global；> global → clamp+warn；≤ global → task。"""
    if task_val is None:
        return global_val
    if global_val is not None and task_val > global_val:
        ...
        return global_val
    return task_val
```
- 加 `from typing import TypeVar`（若未引）
- `global_val is not None and` 守卫消除 :193 算术 error
- 泛型 `T` 让调用处（:202 max_dag_nodes: int、:214 max_task_duration_s: float 等）按字段注解实例化，消除 :202/:206/:210/:218 传参 error

- [ ] **Step 2: 验证**

Run: `conda run -n Elysia pyright Module/workflow_contract`（Expected: 本文件 0 error）
Run: `conda run -n Elysia python -m pytest Module/workflow_contract/tests/ -q --tb=short`

- [ ] **Step 3: Commit**

```bash
git add Module/workflow_contract/schema.py
git commit -m "fix(pyright): workflow_contract _resolve_field 泛型化 + global_val None 守卫（5 errors）"
```

---

### Task 5: 散落 6 errors（逐文件真修）

**Files:**
- Modify: `Module/tool/manager.py`、`Module/tool/protocol.py`、`Module/tts/registry.py`、`Module/llm/openai_compat.py`、`Module/database/vectordb/milvus.py`

- [ ] **Step 1: manager.py:443,447**

`:443` `enriched = dict(base)` → `enriched: dict[str, Any] = dict(base)`（显式注解，消除 :443/:447 setitem 类型不匹配；import 已有 Any 则直接用）

- [ ] **Step 2: protocol.py:152**

`:152` `ToolProgress(status="processing", ...)` → `status=ToolStatus("processing")`（或 `ToolStatus.PROCESSING` 若存在该成员；`ToolStatus` 定义于同文件 protocol.py:33，直接使用）

- [ ] **Step 3: tts/registry.py:126**

`:123` `kwargs = {"base_url": config.base_url}` → `kwargs: dict[str, Any] = {"base_url": config.base_url}`（消除 :126 `rate_limit_rpm` int setitem error；**不能 `str(...)` 值转换**——create() pop 后按 int 传后端）

- [ ] **Step 4: openai_compat.py:222**

`:222` `stream_options=stream_opts`（`stream_opts = {"include_usage": True}` 推断 `dict[str, bool]` 不匹配 OpenAI `ChatCompletionStreamOptionsParam` TypedDict）——修法二选一：
- (a) 内联字面量：`stream_options={"include_usage": True}`（TypedDict 字面量匹配）
- (b) 若 (a) 仍报（库 stub 问题），`cast(Any, stream_opts)` 或 `# type: ignore[arg-type]`（同文件已有 5 处同类库 stub ignore 先例）
实测后选能过 pyright 且运行时不变的。

- [ ] **Step 5: milvus.py:118**

`:118` `collection_exists` 返回 `await asyncio.to_thread(self._client.has_collection, name)`——pymilvus `has_collection` 运行时同步、pyright 因 stub 误判 coroutine。修法：
```python
return cast(bool, await asyncio.to_thread(self._client.has_collection, name))
```
（`from typing import cast`；或 `# type: ignore[reportReturnType]`——决策 2 豁免，二选一）

- [ ] **Step 6: 验证**

Run: `conda run -n Elysia pyright Module`（Expected: 0 errors——Task 2-5 全部完成后 Module 清零）
Run: `conda run -n Elysia python -m pytest Module/tool/tests/ Module/tts/tests/ Module/llm/tests/ Module/database/vectordb/tests/ -q --tb=short`

- [ ] **Step 7: Commit**

```bash
git add Module/tool/manager.py Module/tool/protocol.py Module/tts/registry.py Module/llm/openai_compat.py Module/database/vectordb/milvus.py
git commit -m "fix(pyright): Module 散落 6 errors——容器注解/枚举字面量/milvus 库 stub cast"
```

---

### Task 6: 全量验证 + 文档

- [ ] **Step 1: 门禁三连**

Run: `conda run -n Elysia pyright`（Expected: **0 errors** + core monitoring 1 warning 基线）
Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`（All checks passed）
Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short --ignore=Module/workflow_engine/tests`（test-quick 回归）
Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests -q --tb=short`（workflow_engine 全量——sub_agent 改动影响）

- [ ] **Step 2: 文档更新**

- `CLAUDE.md` 两处 pyright 描述：:36-38 与 :448（:448 明确写「include core/」）→ 改为「include core/ + Module/ + frontend_contract/」；基线描述若提「Elysia env 92」等更新
- `docs/code-style-guide.md` §1.4（:55「检 core/ 生产代码」）→ 同步为全仓生产代码
- `README.md` changelog 若提「core 生产代码 pyright 门禁」→ 扩为全仓（changelog 行，核对后决定）
- `todo.md`：若「pyright 门禁」相关条目，更新完成状态（核对）

- [ ] **Step 3: Commit（文档）**

```bash
git add CLAUDE.md docs/code-style-guide.md README.md todo.md
git commit -m "docs: pyright 门禁扩展到 Module/ + frontend_contract/——CLAUDE.md/code-style-guide 同步"
```

## Self-Review 记录

- **Spec 覆盖**：spec 决策 1-4 + 验证标准全部映射到 Task 1-6（Task 1=include 扩展、Task 2=sub_agent 11、Task 3=websocket 6、Task 4=schema 5、Task 5=散落 6、Task 6=验证+文档）。无 spec 要求遗漏。
- **占位符扫描**：所有步骤含具体文件:行 + 改动 + 验证命令。Step 2（sub_agent assert 具体变量）以 pyright error 逐点消除为准（error 位置已知，变量名 = 组件名）。
- **类型一致性**：枚举收口类型（AudioFormat/AckStatus/FinishReason/MessageChannel）与 frontend_contract 字段一致；`_resolve_field` 泛型 `T: int | float` 与调用处字段一致；容器注解 `dict[str, Any]` 消除所有 setitem 类型 mismatch。
