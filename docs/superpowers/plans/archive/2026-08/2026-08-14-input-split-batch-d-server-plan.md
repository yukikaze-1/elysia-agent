# Input 层拆分 批次 D 实施计划（server.py 纯装配）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** 把 `core/input/server.py`（759 行）的中间件工厂下沉 `middleware.py`、health/ready 探针下沉 `health.py`，server.py 只剩启动守卫 + create_app 装配 + 管理端点。零行为变更。

**Architecture:** 纯移动。`middleware.py`（4 中间件 + `_nosniff_uploads`）、`health.py`（6 health 函数），server.py 顶部 import 两个新模块，删本地定义。3548 测试为行为锁定。

**Tech Stack:** Python 3.11+ / aiohttp / ruff / pyright / pytest

**Spec:** `docs/superpowers/specs/archive/2026-08/2026-08-14-input-split-batch-d-server-design.md`

## Global Constraints

- **零行为变更**：中间件/health 函数体逐字不动；/health、/ready、认证/限流行为不变
- server.py 保留：`is_loopback_host`/`assert_secure_bind`/`warn_if_unsecured`/`warn_if_attachment_signature_disabled` + `create_app` + 内部闭包（管理端点/ws_handler/debug_handler/index_handler）
- 禁相对导入（ruff TID252）

---

### Task 1: 创建 `middleware.py`

- [ ] **Step 1**: 从 server.py 复制 4 个中间件函数（`_make_access_auth_middleware`/`_make_payload_middleware`/`_rest_rate_category`/`_make_rate_limit_middleware`）+ `_nosniff_uploads`（create_app 内闭包，提为模块级）到 middleware.py，函数体逐字不动。
- [ ] **Step 2**: 顶部 import 补齐（aiohttp `web`、`logging`、`RateLimiter`、`frontend_contract.rest.ProtocolError`、`core.input.errors._protocol_error_response` 等——从 server.py 顶部挑用到的）。
- [ ] **Step 3**: 验证导入：`conda run -n Elysia python -c "import core.input.middleware"`
- [ ] **Step 4**: Commit `refactor(input): 批次 D Task1——middleware.py（4 中间件 + _nosniff_uploads，未接线）`

### Task 2: 创建 `health.py`

- [ ] **Step 1**: 从 server.py 复制 6 个 health 函数（`health_handler`/`ready_handler`/`_runtime_info`/`_liveness_payload`/`_probe`/`_readiness_payload`）到 health.py，函数体逐字不动。
- [ ] **Step 2**: 顶部 import 补齐（aiohttp `web`、`logging`、`core.agent`、`core.config`、`core.input.errors._protocol_error_response`、`core.version` 等）。
- [ ] **Step 3**: 验证导入。
- [ ] **Step 4**: Commit `refactor(input): 批次 D Task2——health.py（6 health 探针函数，未接线）`

### Task 3: `server.py` 接线 + 删本地定义

- [ ] **Step 1**: server.py 顶部加 `from core.input.middleware import _make_access_auth_middleware, _make_payload_middleware, _make_rate_limit_middleware, _rest_rate_category, _nosniff_uploads` + `from core.input.health import health_handler, ready_handler`。
- [ ] **Step 2**: 删除 server.py 中 Task 1/2 迁移的 10 个函数本地定义（`_make_access_auth_middleware`/`_make_payload_middleware`/`_rest_rate_category`/`_make_rate_limit_middleware`/`_nosniff_uploads`/`health_handler`/`ready_handler`/`_runtime_info`/`_liveness_payload`/`_probe`/`_readiness_payload`）。`_nosniff_uploads` 原在 create_app 内闭包，删除后 create_app 用 import 的模块级版本。
- [ ] **Step 3**: 验证导入 + 跑测试：`pytest core/input/tests/test_health.py core/input/tests/test_auth.py core/input/tests/test_limits.py core/input/tests/test_rate_limit.py core/input/tests/test_server.py -m "not external" -q`
- [ ] **Step 4**: Commit `refactor(input): 批次 D Task3——server.py 接线 middleware/health，删本地定义`

### Task 4: 门禁验收 + 文档

- [ ] **Step 1**: `wc -l core/input/server.py` < 500（中间件 + health 下沉后）。
- [ ] **Step 2**: test-quick + test-normal + ruff + pyright 全绿。
- [ ] **Step 3**: CLAUDE.md/todo 更新（server.py 纯装配、middleware.py/health.py 新增）。
- [ ] **Step 4**: Commit `refactor(input): 批次 D Task4——门禁验收 + 文档更新`

---

## 验收清单（批次 D 完成后核对）

- [ ] `wc -l core/input/server.py` < 500
- [ ] `/health`、`/ready`、认证/限流行为不变（test_health/test_auth/test_limits/test_rate_limit 全绿）
- [ ] middleware.py/health.py 独立可导入、函数体逐字一致
- [ ] test-quick 3548 / test-normal 3656 / ruff / pyright 全绿
