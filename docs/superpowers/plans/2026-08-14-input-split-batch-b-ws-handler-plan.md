# Input 层拆分 批次 B 实施计划（ws_handler 拆分：先提取后注册表）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ws_handler.py（1825 行）的深业务逻辑下移 ws_v2.py + `_send_ack` 迁 `_common.py`，再把 handle_json_message 的 22 分支 if/elif 改 `_WS_HANDLERS` 注册表分发。零行为变更。

**Architecture:** 两阶段。第一阶段纯移动（深业务 + 专属 helper → ws_v2.py；共享 `_send_ack` → _common.py），ws_handler 只剩协议 framing；第二阶段查表分发。3548+ 测试为行为锁定安全网。

**Tech Stack:** Python 3.11+ / aiohttp / pydantic / ruff / pyright / pytest

**Spec:** `docs/superpowers/specs/2026-08-14-input-split-batch-b-ws-handler-design.md`

## Global Constraints

- **零行为变更**：移动的函数体逐字不动；22 种消息类型行为不变（test_voice_ws/test_session_ws 全绿锁定）
- **helper 归属按 spec §3/§4.1**：`_send_ack` → `_common.py`；`_voice_stt_gate`/`_voice_descriptor` → 留 ws_handler（voice 分支未移动）；仅 v2 专属 helper 跟随下移
- **出口稳定**：`server.py` 的 `handle_ws`/`resolve_protocol_version` 不移动；测试 import 的 `handle_json_message`/`_send_voice_error`/`_handle_activate_session`/`do_interrupt`/`do_transcription` 不破坏
- 禁相对导入（ruff TID252）

---

### Task 1: 创建 `_common.py` + 迁移 `_send_ack` + `_find_channel`

**Files:**
- Create: `core/input/_common.py`
- Modify: `core/input/ws_handler.py`（删 `_send_ack` 定义 :547、`_find_channel` 定义 :473；顶部 import 自 _common）

**Interfaces:**
- Consumes: `_send_ack` 现有签名（`(ws, message_id, client_request_id, client_message_id, conversation_id, status="accepted", **extra) -> Awaitable[None]`）、`_find_channel` 签名（`(agent, ws) -> WebSocketChannel | None`）
- Produces: `core.input._common._send_ack`、`core.input._common._find_channel`（ws_v2 与 ws_handler 共用，避免循环）

- [ ] **Step 1: 创建 `core/input/_common.py`**

复制 ws_handler.py 中 `_send_ack`（:547 起）与 `_find_channel`（:473 起）函数体到新文件，逐字不动。`_common.py` 顶部 import 补齐依赖（aiohttp `web`、`frontend_contract`、`core.schema`、`Module.output.WebSocketChannel` 等——从 ws_handler 顶部 import 挑实际用到）。

- [ ] **Step 2: ws_handler.py 删两个定义 + 顶部 import**

- 删除 ws_handler.py 中 `_find_channel`（:473）与 `_send_ack`（:547）函数定义；
- 顶部加 `from core.input._common import _find_channel, _send_ack`。

- [ ] **Step 3: 验证 ws_handler 可导入 + 相关测试绿**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_voice_ws.py core/input/tests/test_session_ws.py -m "not external" -q --tb=short`
Expected: PASS（调用点不变，仅来源改 _common）

- [ ] **Step 4: Commit**

```bash
git add core/input/_common.py core/input/ws_handler.py
git commit -m "refactor(input): 批次 B Task1——_send_ack/_find_channel 迁 _common.py（跨业务共享，防循环）"
```

---

### Task 2: 创建 `ws_v2.py`（迁移深业务 + 专属 helper）

**Files:**
- Create: `core/input/ws_v2.py`
- Modify: 无（ws_handler 接线在 Task 3）

**Interfaces:**
- Consumes: `_route_v2_text`/`_handle_voice_commit` 现有签名（含 `agent`/`ws`/`text_model`/`audio_buf`/`voice_mgr`/`sent_message_ids`/`rate_key` 等参数）；9 个专属 helper
- Produces: `core.input.ws_v2._route_v2_text`、`_handle_voice_commit` + 专属 helper（`_send_rejected`/`_handle_idempotency_hit`/`_idempotency_unavailable_error`/`_in_progress_error`/`_storage_unavailable_error`/`_release_attachment_reservations`/`_session_or_none`/`_attachment_part`/`_build_media_parts`）

- [ ] **Step 1: 从 ws_handler 复制 11 个函数到 ws_v2.py**

从 ws_handler.py 复制以下函数体（逐字不动）：
- 深业务：`_route_v2_text`（:787-1088）、`_handle_voice_commit`（:1088-1266）
- 专属 helper：`_send_rejected`、`_handle_idempotency_hit`、`_idempotency_unavailable_error`、`_in_progress_error`、`_storage_unavailable_error`、`_release_attachment_reservations`、`_session_or_none`、`_attachment_part`、`_build_media_parts`

ws_v2.py 顶部 import 补齐（从 ws_handler 顶部 import 挑实际用到）：aiohttp `web`、`logging`、`core.schema.part`/`types`/`event`、`core.memory.session_manager`、`core.input.idempotency`、`core.storage.attachment_repo`、`core.utils`、`from core.input._common import _send_ack, _find_channel`。**禁止 `from core.input.ws_handler import ...`**（ws_handler Task 3 会 import ws_v2，反向 import 制造循环；`_find_channel`/`_send_ack` 已进 _common）。

- [ ] **Step 2: 验证 ws_v2.py 可导入**

Run: `conda run -n Elysia python -c "import core.input.ws_v2; print('ok')"`
Expected: ok（无 ImportError/循环）

- [ ] **Step 3: Commit（ws_v2.py 单独落地，ws_handler 未接线）**

```bash
git add core/input/ws_v2.py
git commit -m "refactor(input): 批次 B Task2——ws_v2.py 落地深业务 + 专属 helper（未接线）"
```

---

### Task 3: `ws_handler.py` 接线 + 深业务删除

**Files:**
- Modify: `core/input/ws_handler.py`（删 11 个迁移函数、import ws_v2、handle_json_message 分支调 ws_v2）

**Interfaces:**
- Consumes: `core.input.ws_v2._route_v2_text`/`_handle_voice_commit`（Task 2 产物）
- Produces: 无新接口；ws_handler 的 text/voice.commit 分支改调 ws_v2

- [ ] **Step 1: ws_handler 删迁移函数 + 顶部 import ws_v2**

- 删除 ws_handler.py 中 Task 2 迁移的 11 个函数定义（`_route_v2_text`/`_handle_voice_commit` + 9 专属 helper）；
- 顶部加 `from core.input.ws_v2 import _route_v2_text, _handle_voice_commit`（+ 若 ws_v2 需要的 helper 已在 ws_v2 内，无需再 import）。

- [ ] **Step 2: handle_json_message 的 text/voice.commit 分支调 ws_v2**

- text 分支（:1345 起）：`_route_v2_text` 调用不变（现在解析到 ws_v2 的导入名）；
- voice.commit 分支（:1575 起）：`_handle_voice_commit` 调用不变。
  （函数名未变，仅来源改 ws_v2 import——若 handle_json_message 内直接调用名不变，则 Step 2 无需改动，仅 Step 1 的 import 生效。）

- [ ] **Step 3: 跑相关测试确认绿**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_voice_ws.py core/input/tests/test_session_ws.py core/input/tests/test_server.py -m "not external" -q --tb=short`
Expected: PASS（22 消息类型行为不变）

- [ ] **Step 4: Commit**

```bash
git add core/input/ws_handler.py
git commit -m "refactor(input): 批次 B Task3——ws_handler 接线 ws_v2，删 11 个迁移函数（行为锁定）"
```

---

### Task 4: 阶段一验收（ws_handler 瘦身 + 全门禁）

**Files:** 无（验证）

- [ ] **Step 1: 瘦身验证**

```bash
wc -l core/input/ws_handler.py
```

Expected: <1200（深业务 ~780 行下移后）

- [ ] **Step 2: 门禁**

```bash
conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short   # test-quick
conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short  # test-normal
conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__
conda run -n Elysia pyright
```

Expected: test-quick 3548 / test-normal 3656 / ruff passed / pyright 0 errors

- [ ] **Step 3: Commit**

```bash
git commit --allow-empty -m "refactor(input): 批次 B Task4——阶段一验收（ws_handler <1200 行 + 门禁全绿）"
```

---

### Task 5: 阶段二——`_WS_HANDLERS` 注册表分发

**Files:**
- Modify: `core/input/ws_handler.py`（handle_json_message if/elif → 查表；22 分支提为 `_handle_<type>` 模块级函数）

**Interfaces:**
- Consumes: 22 个分支体的现有逻辑（提取为函数）
- Produces: `_WS_HANDLERS: dict[str, Callable]` 注册表；`_handle_<type>(data, agent, ws, ...)` 模块级函数

- [ ] **Step 1: 22 分支提为模块级 `_handle_<type>` 函数**

把 handle_json_message 内每个 `msg_type == "..."` 分支体提取为模块级函数 `_handle_wake_toggle(data, agent, ws, ...)` 等，函数体逻辑不变。深业务型（text/voice.commit）已调 ws_v2，其余分支体直接搬运。

统一签名：`async def _handle_<type>(data, agent, audio_buf=None, ws=None, voice_mgr=None, sent_message_ids=None, limiter=None, rate_key=None) -> None`（参数与 handle_json_message 现状一致；个别分支按需 partial 适配）。

- [ ] **Step 2: 建注册表 + 改 handle_json_message 查表**

```python
_WS_HANDLERS: dict[str, Callable[..., Awaitable[None] | None]] = {
    "wake_toggle": _handle_wake_toggle,
    "text": _handle_text,
    "interrupt": _handle_interrupt,
    # ... 22 项
}
```

handle_json_message 保留：限流前缀、`_WS_MESSAGE_RATE_CATEGORY`、判别联合校验、未知 type 拒绝路径；
分发处改 `handler = _WS_HANDLERS.get(msg_type)` → `if handler is not None: await handler(...)`，保留既有的 `elif` 回退（未知 type → 拒绝）。

- [ ] **Step 3: 跑相关测试确认绿**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_voice_ws.py core/input/tests/test_session_ws.py core/input/tests/test_server.py -m "not external" -q --tb=short`
Expected: PASS（22 分支行为不变）

- [ ] **Step 4: Commit**

```bash
git add core/input/ws_handler.py
git commit -m "refactor(input): 批次 B Task5——handle_json_message 改 _WS_HANDLERS 注册表分发（22 分支）"
```

---

### Task 6: 阶段二验收 + 最终门禁

**Files:** 无（验证）

- [ ] **Step 1: 分发结构验证**

```bash
grep -n "msg_type ==" core/input/ws_handler.py
```

Expected: 无巨型 if/elif 链（handle_json_message 内仅查表分发）

- [ ] **Step 2: 全门禁**

```bash
conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short
conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short
conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__
conda run -n Elysia pyright
```

Expected: 全绿

- [ ] **Step 3: Commit**

```bash
git commit --allow-empty -m "refactor(input): 批次 B Task6——注册表分发验收 + 门禁全绿"
```

---

## 验收清单（批次 B 完成后核对）

- [ ] `wc -l core/input/ws_handler.py` < 1200（阶段一）且无巨型 if/elif（阶段二）
- [ ] 22 种消息类型行为不变（test_voice_ws/test_session_ws 全绿）
- [ ] `_send_ack` 在 `_common.py`；`_route_v2_text`/`_handle_voice_commit` 在 `ws_v2.py`
- [ ] `make test-quick` 3548 / `make test-normal` 3656 / ruff / pyright 全绿
- [ ] 出口稳定：`handle_ws`/`resolve_protocol_version` 未移动
