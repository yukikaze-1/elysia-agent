# Input 层拆分 批次 A 实施计划（错误信封下沉 errors.py）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `_protocol_error_response` 从 `core/input/server.py` 下沉到新建 `errors.py`，消除 97 处（61 调用 + 36 懒导入行）懒导入绕环。

**Architecture:** 纯移动重构，零行为变更。`errors.py` 为无环叶子（仅依赖 `frontend_contract.rest.ProtocolError` + aiohttp `web` + `uuid`）；server/api_routes/debug_routes 改顶部 eager import，删除函数内懒导入。函数逻辑、调用点函数名均不变。

**Tech Stack:** Python 3.11+ / aiohttp / ruff / pyright / pytest

**Spec:** `docs/superpowers/specs/archive/2026-08/2026-08-14-input-split-batch-a-errors-design.md`

## Global Constraints

- **零行为变更**：`_protocol_error_response` 函数体逐字不动，3548 测试为行为锁定安全网
- **保持私有名**：`_protocol_error_response`（D-A1），不改名为公开
- **禁相对导入**（ruff TID252 门禁）
- **调用点函数名不变**：只改 import 路径，不改 61 处调用行
- 每步完成后跑相关测试确认仍绿（纯移动无新测试——行为锁定靠既有测试）

---

### Task 1: 创建 `errors.py` + `server.py` 接线

**Files:**
- Create: `core/input/errors.py`
- Modify: `core/input/server.py:28`（删 ProtocolError import）、`core/input/server.py:150-166`（删定义）、顶部加 import

**Interfaces:**
- Consumes: `_protocol_error_response` 现有签名（`code: str, message: str, status: int = 401, retryable: bool = False -> web.Response`）
- Produces: `core/input.errors._protocol_error_response`（模块级，可被 server/api_routes/debug_routes import）

- [ ] **Step 1: 创建 `core/input/errors.py`**

```python
"""input 层统一错误信封响应构造器。

_protocol_error_response 原寄生于 core/input/server.py，被 api_routes/debug_routes 经
函数级懒导入绕环访问（2026-08-14 批次 A 下沉）。errors.py 仅依赖 frontend_contract +
aiohttp + uuid，不依赖 server.py——调用方改顶层 eager import 即可消除绕环。
"""

import uuid

from aiohttp import web

from frontend_contract.rest import ProtocolError


def _protocol_error_response(code: str, message: str, status: int = 401,
                             retryable: bool = False) -> web.Response:
    """构造统一错误信封响应（code/message/retryable/requestId/error/details）。

    Args:
        code: 稳定错误码。
        message: 公开说明。
        status: HTTP 状态码。
        retryable: 是否可重试（错误码目录定义；CONNECTION_LIMITED=true，AUTH/REQUEST 等 false）。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)
```

- [ ] **Step 2: 验证 errors.py 可独立导入**

Run: `conda run -n Elysia python -c "from core.input.errors import _protocol_error_response; print('ok')"`
Expected: `ok`（无 ImportError、无循环）

- [ ] **Step 3: 修改 `server.py`——删定义与死 import，顶部 import errors**

- 删除 `server.py:28` 的 `from frontend_contract.rest import ProtocolError`（移走后仅剩死引用；`server.py:552` 是注释非代码）
- 删除 `server.py:150-166` 的 `_protocol_error_response` 定义（整个函数）
- 在 `server.py` 顶部 import 区（如 `from core.input.ws_handler import ...` 附近）加：

```python
from core.input.errors import _protocol_error_response
```

- server.py 内部 11 处 `_protocol_error_response(...)` 调用不变（现在解析到 errors 模块的导入名）

- [ ] **Step 4: 跑 server 相关测试确认绿**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py core/input/tests/test_auth.py core/input/tests/test_limits.py -m "not external" -q --tb=short`
Expected: PASS（错误信封行为不变）

- [ ] **Step 5: Commit**

```bash
git add core/input/errors.py core/input/server.py
git commit -m "refactor(input): 批次 A Task1——errors.py 下沉错误信封 + server.py 接线（零行为变更）"
```

---

### Task 2: `api_routes.py` 顶部 import + 删懒导入

**Files:**
- Modify: `core/input/api_routes.py`（顶部加 import；删除函数内 1 处懒导入行 `from core.input.server import _protocol_error_response`，位于 `upload_handler` 函数内约 650 行）

**Interfaces:**
- Consumes: `core/input.errors._protocol_error_response`（Task 1 产物）
- Produces: 无新接口；15 处调用解析到 errors import

- [ ] **Step 1: 顶部加 import + 删懒导入行**

- 在 `api_routes.py` 顶部 import 区加：

```python
from core.input.errors import _protocol_error_response
```

- 删除函数内懒导入行（用 grep 定位）：

```bash
grep -n "from core.input.server import _protocol_error_response" core/input/api_routes.py
```

删除该行（仅 1 处，`upload_handler` 内）。

- [ ] **Step 2: 跑 api_routes 相关测试确认绿**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_providers_api.py core/input/tests/test_tts_voices.py core/input/tests/test_tools_api.py core/input/tests/test_upload.py core/input/tests/test_attachment_message.py -m "not external" -q --tb=short`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add core/input/api_routes.py
git commit -m "refactor(input): 批次 A Task2——api_routes 改顶部 import errors，删懒导入绕环"
```

---

### Task 3: `debug_routes` 4 文件顶部 import + 删懒导入

**Files:**
- Modify: `core/input/debug_routes/character.py`（删 14 处懒导入 + 顶部 import）
- Modify: `core/input/debug_routes/archive.py`（删 13 处懒导入 + 顶部 import）
- Modify: `core/input/debug_routes/ltm.py`（删 7 处懒导入 + 顶部 import）
- Modify: `core/input/debug_routes/__init__.py`（删 1 处懒导入 + 顶部 import）

**Interfaces:**
- Consumes: `core/input.errors._protocol_error_response`（Task 1 产物）
- Produces: 无新接口

- [ ] **Step 1: character.py——顶部 import + 删 14 处懒导入**

在 `core/input/debug_routes/character.py` 顶部 import 区加：

```python
from core.input.errors import _protocol_error_response
```

删除全部函数内懒导入行（机械删除）：

```bash
sed -i '/from core.input.server import _protocol_error_response/d' core/input/debug_routes/character.py
```

验证删除数：`grep -c "from core.input.server import _protocol_error_response" core/input/debug_routes/character.py` 应为 0。

- [ ] **Step 2: archive.py / ltm.py / __init__.py 同样处理**

```bash
for f in core/input/debug_routes/archive.py core/input/debug_routes/ltm.py core/input/debug_routes/__init__.py; do
  sed -i '/from core.input.server import _protocol_error_response/d' "$f"
done
```

三个文件顶部各加 `from core.input.errors import _protocol_error_response`（与 Step 1 同）。

- [ ] **Step 3: 跑 core/input/tests 全目录确认绿（覆盖 debug_routes 相关）**

Run: `conda run -n Elysia python -m pytest core/input/tests/ -m "not external" -q --tb=short`
Expected: PASS（全目录含 test_debug_ltm.py 及 server/api 路由用例；Task 4 再做全量门禁）

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/
git commit -m "refactor(input): 批次 A Task3——debug_routes 4 文件改顶部 import errors，删懒导入绕环"
```

---

### Task 4: 全量门禁 + grep 验收

**Files:** 无新改动（验证）

- [ ] **Step 1: grep 验收——无 server 懒导入残留**

```bash
grep -rn "from core.input.server import _protocol_error_response" core/ --include="*.py"
grep -rn "from core.input.server import" core/input/ --include="*.py" | grep -v "/tests/"
```

Expected: 两条都为空。第一条查全局信封懒导入；第二条查 core/input 生产代码（排除 tests/，
因为 `core/input/tests/*` 合法 import `create_app`）不再有 server 懒导入。`core/agent/server.py`
的 `create_app`/`assert_secure_bind` import 是合法入口引用，不在 core/input/ 范围。

- [ ] **Step 2: test-quick + test-full**

```bash
conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short   # test-quick 子集
conda run -n Elysia python -m pytest -q --tb=short                        # test-full 全量（3548 passed 基线）
```

Expected: 全绿

- [ ] **Step 3: lint + pyright**

```bash
conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__
conda run -n Elysia pyright
```

Expected: ruff All checks passed / pyright 0 errors

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(input): 批次 A Task4——门禁回归 + 绕环消除验收（test-quick + test-full + ruff + pyright 全绿）"
```

---

## 验收清单（批次 A 完成后核对）

- [ ] `grep -rn "from core.input.server import" core/input/` 为空（排除 tests/）
- [ ] `core/input/errors.py` 存在且 `_protocol_error_response` 可导入
- [ ] `make test-full` 3548 passed
- [ ] ruff + pyright 全绿
- [ ] 函数体逐字未动（git diff 仅 import/删除行）
