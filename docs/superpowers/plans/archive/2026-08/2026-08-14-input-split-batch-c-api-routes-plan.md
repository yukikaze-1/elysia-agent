# Input 层拆分 批次 C 实施计划（api_routes 按领域拆 5 文件）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 `core/input/api_routes.py`（1182 行，14 个闭包 handler 8 组路由）按领域拆成 5 个文件（provider/tts/upload/tools/chat），`api_routes.py` 退役删除。零行为变更。

**Architecture:** 闭包 handler 提为模块级函数（不捕获外层局部，仅 `request.app.get("agent")` + 模块级 import），每文件末尾 `register_<group>_routes(app)`。共享 helper 归位（`_archive_*`→`_common.py`、`_sign_url`→upload、`_PREVIEW_TEXT`→tts）。

**Tech Stack:** Python 3.11+ / aiohttp / ruff / pyright / pytest

**Spec:** `docs/superpowers/specs/archive/2026-08/2026-08-14-input-split-batch-c-api-routes-design.md`

## Global Constraints

- **零行为变更**：handler 函数体逐字不动（仅缩进调整）；14 个端点路径不变；3548 测试为行为锁定
- **handler 无闭包捕获**（已核实：仅用 request 参数 + 模块级 import）
- 禁相对导入（ruff TID252）
- 共享 helper 归属按 spec §4.2

---

### Task 1: 创建 `provider_routes.py`（4 handler）

**Files:**
- Create: `core/input/provider_routes.py`

**Interfaces:**
- Produces: `register_provider_routes(app)`（4 端点：list/save/delete/test）

- [ ] **Step 1**: 从 `api_routes.py` 的 `register_api_routes` 提取 4 个 provider handler（`providers_list/save/delete/test_handler`）为模块级函数，函数体逐字不动（缩进 8→4）。复制私有 helper：`_strip_secret_keys`、`_custom_profile_in_use`、`_run_usable_test`。
- [ ] **Step 2**: 顶部 import 补齐（`web`、`logging`、`frontend_contract.rest`、`core.input.custom_providers`、`core.input.provider_registry`、`core.input.errors` 等——从 api_routes 顶部挑用到的）。末尾加 `def register_provider_routes(app): app.router.add_*("...", handler)` 4 行。
- [ ] **Step 3**: 验证导入 + 跑 provider 测试
  Run: `conda run -n Elysia python -m pytest core/input/tests/test_providers_api.py -m "not external" -q --tb=short`
  Expected: PASS（暂未接线，仅验证可导入）
- [ ] **Step 4**: Commit `refactor(input): 批次 C Task1——provider_routes.py（4 handler，未接线）`

### Task 2: 创建 `tts_routes.py`（2 handler）

- [ ] **Step 1**: 提取 `tts_voices_handler`/`tts_preview_handler` + `_PREVIEW_TEXT` 常量（从 api_routes）。
- [ ] **Step 2**: import 补齐 + `register_tts_routes(app)`。
- [ ] **Step 3**: 验证导入（`python -c "import core.input.tts_routes"`）。
- [ ] **Step 4**: Commit `refactor(input): 批次 C Task2——tts_routes.py（2 handler + _PREVIEW_TEXT）`

### Task 3: 创建 `upload_routes.py`（3 handler）

- [ ] **Step 1**: 提取 `image_upload_handler`/`upload_handler`/`stickers_handler` + `_sign_url`（含 `_ALLOWED_TYPES` 等模块级常量）。`upload_handler` 内的 `_UploadError` 异常类、`_cleanup` 闭包随函数体原样搬移。
- [ ] **Step 2**: import 补齐 + `register_upload_routes(app)`（3 端点：images/uploads/stickers）。
- [ ] **Step 3**: 验证导入 + `attachment_routes.py` 的 `_sign_url` import 改 `from core.input.upload_routes import _sign_url`。
- [ ] **Step 4**: 跑上传测试：`pytest core/input/tests/test_upload.py core/input/tests/test_attachment_download.py -m "not external" -q`
- [ ] **Step 5**: Commit `refactor(input): 批次 C Task3——upload_routes.py（3 handler + _sign_url）`

### Task 4: 创建 `tools_routes.py`（3 handler）

- [ ] **Step 1**: 提取 `tools_registry_handler`/`tools_call_status_handler`/`desktop_upload_handler` + `_tool_error`/`_tool_metadata_to_contract`/`_desktop_call_to_status` 闭包 helper（提为模块级）。
- [ ] **Step 2**: import 补齐 + `register_tools_routes(app)`（3 端点）。
- [ ] **Step 3**: 验证导入 + 跑测试：`pytest core/input/tests/test_tools_api.py core/input/tests/test_desktop_ws.py -m "not external" -q`
- [ ] **Step 4**: Commit `refactor(input): 批次 C Task4——tools_routes.py（3 handler + 私有 helper）`

### Task 5: 创建 `chat_routes.py` + `_archive_*` 迁 `_common.py`

- [ ] **Step 1**: 提取 `chat_api_handler`/`messages_handler` 为模块级（chat_routes.py）。
- [ ] **Step 2**: `_archive_to_client`/`_archive_msg_to_contract` 移 `core/input/_common.py`（跨组共享：session_routes/debug_routes/archive 用）；chat_routes 从 _common import。
- [ ] **Step 3**: 更新 `session_routes.py`、`debug_routes/archive.py` 的 `_archive_*` import → `from core.input._common import ...`；测试 import 同步。
- [ ] **Step 4**: 跑测试：`pytest core/input/tests/test_session_routes.py core/input/tests/test_messages_api_compat.py -m "not external" -q`
- [ ] **Step 5**: Commit `refactor(input): 批次 C Task5——chat_routes.py + _archive_* 迁 _common`

### Task 6: `server.py` 接线 5 注册 + 删 `api_routes.py`

- [ ] **Step 1**: `server.py` 的 `from core.input.api_routes import register_api_routes` 改为 5 个 import + 5 个 `register_<group>_routes(app)` 调用（在 create_app 内原 `register_api_routes(app)` 位置）。
- [ ] **Step 2**: 删除 `core/input/api_routes.py`；确认全仓无 `from core.input.api_routes import` 残留（grep）。
- [ ] **Step 3**: 跑全目录测试：`pytest core/input/tests/ -m "not external" -q`
- [ ] **Step 4**: Commit `refactor(input): 批次 C Task6——server 接 5 注册 + api_routes 退役删除`

### Task 7: 门禁验收

- [ ] **Step 1**: `grep -rn "from core.input.api_routes import" core/` 为空（api_routes 删除确认）。
- [ ] **Step 2**: test-quick + test-normal + ruff + pyright 全绿。
- [ ] **Step 3**: Commit `refactor(input): 批次 C Task7——门禁验收 + 文档更新（CLAUDE.md/todo）`

---

## 验收清单（批次 C 完成后核对）

- [ ] `api_routes.py` 删除，无 import 残留
- [ ] 14 端点路径不变（provider/tts/upload/tools/chat 5 文件覆盖）
- [ ] `_sign_url`→upload、`_archive_*`→_common、`_PREVIEW_TEXT`→tts 归属正确
- [ ] test-quick 3548 / test-normal 3656 / ruff / pyright 全绿
