# Input 层拆分 批次 D：server.py 纯装配（middleware / health 下沉）

**文档版本：** v0.1（初稿，待评审）
**文档状态：** 待评审
**日期：** 2026-08-14
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** 会话记录「core/input 内聚性评估」（2026-08-14）问题 3
**前置决策：** AskUserQuestion Q4（**server.py 纯装配**）；依赖批次 A（errors.py 已建立）
**依赖批次：** 批次 A（`_protocol_error_response` 已移 errors.py）

---

## 1. 背景

`core/input/server.py`（759 行）不是纯 glue：`create_app` 装配仅 126/759 行，其余是
4 个中间件工厂 + health/ready 探针 + 启动安全守卫。批次 A 已把错误信封移走，本批继续
下沉中间件与健康探针，让 server.py 回归「纯装配」。

## 2. 目标

- 中间件工厂 → `middleware.py`，health/ready 探针 → `health.py`；
- `server.py` 只留：启动安全守卫 + `create_app` 装配 + 管理端点（shutdown/restart/tools）；
- **零行为变更**（3548 测试锁定）。

## 3. 现状（事实基线）

| 组 | 函数（行号） | 行数 |
|---|---|---|
| 中间件 | `_make_access_auth_middleware`(167-194)、`_make_payload_middleware`(195-230)、`_rest_rate_category`(231-238)、`_make_rate_limit_middleware`(239-267) | ~100 |
| health | `_runtime_info`(268-300)、`_liveness_payload`(301-313)、`_probe`(314-326)、`_readiness_payload`(327-386)、`health_handler`(387-398)、`ready_handler`(399-421) | ~154 |
| 启动守卫 | `is_loopback_host`(48-56)、`assert_secure_bind`(57-83)、`warn_if_unsecured`(84-126)、`warn_if_attachment_signature_disabled`(127-149) | ~102 |
| create_app | `create_app`(422-759) + 内部闭包（`_nosniff_uploads`/`_check_auth`/`ws_handler`/`debug_handler`/`index_handler`/`shutdown_handler`/`restart_handler`/`tools_handler`/`tool_reload_handler`） | ~338 |

## 4. 方案

### 4.1 新建 `core/input/middleware.py`

移动 4 个中间件相关函数（函数体零改动）：`_make_access_auth_middleware`、
`_make_payload_middleware`、`_rest_rate_category`、`_make_rate_limit_middleware`。
依赖：aiohttp `web`、`RateLimiter`（core.input.rate_limiter）、`ProtocolError` 信封（errors.py，
批次 A）。

### 4.2 新建 `core/input/health.py`

移动 6 个健康探针函数：`_runtime_info`、`_liveness_payload`、`_probe`、`_readiness_payload`、
`health_handler`、`ready_handler`。依赖：core.agent、core.config、errors.py（信封）。

### 4.3 `server.py` 保留 + import

- 保留：启动守卫（is_loopback_host/assert_secure_bind/warn_if_unsecured/
  warn_if_attachment_signature_disabled）+ `create_app` + 内部闭包；
- 顶部加 `from core.input.middleware import _make_access_auth_middleware, _make_payload_middleware, _make_rate_limit_middleware`（+ `_rest_rate_category` 若 create_app 内部用）+ `from core.input.health import health_handler, ready_handler`；
- `_nosniff_uploads`（create_app 内部中间件式闭包）若适合随 middleware 下沉则移，否则留 server。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D-D1 中间件归属 | `core/input/middleware.py` | Q4 敲定；中间件是独立关注点 |
| D-D2 health 归属 | `core/input/health.py` | Q4 敲定；探针与装配解耦 |
| D-D3 启动守卫去留 | **留 server.py** | 与 create_app 强绑定（create_app 前调用），且仅 ~100 行 |
| D-D4 管理端点 | **留 server.py**（create_app 内部闭包） | shutdown/restart/tools 是装配的一部分 |
| D-D5 `_nosniff_uploads` | 随 middleware 下沉 | 它是中间件式逻辑（Content-Type 守卫），归 middleware.py |

## 6. 波及面

- 新增：`core/input/middleware.py`、`core/input/health.py`
- 修改：`core/input/server.py`（瘦身 ~250 行，顶部 import 两个新模块）
- 测试：`core/input/tests/test_health.py`（import health_handler? 需核对）、`test_server.py`、
  `test_auth.py`、`test_limits.py`、`test_rate_limit.py`（中间件行为）——import/mock 路径若引用
  server.py 内部中间件/health 函数需同步
- **无**：wire 行为、契约、错误码

## 7. 验收标准

1. `make test-quick` 全绿 + `make test-full`（3548 基线）全绿；
2. `wc -l core/input/server.py` 显著下降（目标 <500，中间件 + health 下沉后）；
3. `/health`、`/ready`、中间件（auth/payload/rate_limit）行为不变（test_health/test_auth/test_limits/test_rate_limit 全绿）；
4. ruff + pyright 全绿（middleware.py/health.py 纳入门禁）。

## 8. 风险

- **中**：中间件/health 函数依赖面需核对（`_make_access_auth_middleware` 用 ProtocolError 信封、
  `_readiness_payload` 依赖多个 repo probe）；移动后 import 路径更新，3548 测试锁定；
- create_app 内部 `_check_auth`/`_nosniff_uploads` 闭包若捕获 create_app 局部变量，需确认不随移动破坏捕获；
- 测试 mock 到 `core.input.server._make_*` 或 `health_handler` 的路径需同步。

## 9. 后续衔接

- 本批完成后 input 层拆分 ABCD 全部收官；`server.py` 回归纯装配，input 最大文件已从
  ws_handler 1825 行大幅瘦身；
- 可选后续：`_protocol_error_response` 公开化（改名批次）、`ws_handler` 协议层再细分。
