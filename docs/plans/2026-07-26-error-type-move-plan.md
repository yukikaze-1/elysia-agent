# ErrorType Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `ErrorType` definition from `Module/tool/protocol.py` to `core/schema/error_type.py`, eliminating the core/schema → Module/tool reverse dependency.

**Architecture:** Create `core/schema/error_type.py` with the identical `ErrorType` class, replace the definition in `protocol.py` with a `from core.schema.error_type import ErrorType` re-export for backward compatibility, and update direct import sites in `core/schema/message.py`, `core/adapter/__init__.py`, and `core/adapter/tool_adapter.py`.

**Tech Stack:** Python 3.11+, `str, Enum`

## Global Constraints

- `ErrorType` class is **identical** to original — same 9 members, same `is_retryable` property, same docstrings
- `Module.tool.protocol.ErrorType` import path continues to work (backward compatible via re-export)
- No API changes — `ErrorType.TIMEOUT == "timeout"` still True, `.value`, `.is_retryable` all unchanged
- All existing tests pass: `make test-quick`

---

### Task 1: Move ErrorType definition + update import sites

**Files:**
- Create: `core/schema/error_type.py`
- Modify: `Module/tool/protocol.py:24-44`
- Modify: `core/schema/message.py:13`
- Modify: `core/adapter/__init__.py:15`
- Modify: `core/adapter/tool_adapter.py:13`

**Interfaces:**
- Produces: `core.schema.error_type.ErrorType` — identical `str, Enum` with 9 members + `is_retryable` property
- Re-export chain: `Module.tool.protocol.ErrorType` → `core.schema.error_type.ErrorType` (backward compat)

- [ ] **Step 1: Create `core/schema/error_type.py`**

```python
"""工具错误分类 — 跨层共享类型。"""
from __future__ import annotations
from enum import Enum


class ErrorType(str, Enum):
    """工具执行错误类型 — 帮助认知循环决定重试策略。"""

    INVALID_ARGUMENT = "invalid_argument"    # LLM 传错了参数 → 反馈后可重试
    TIMEOUT = "timeout"                      # 超时 → 可重试（缩小范围后）
    PERMISSION_DENIED = "permission_denied"  # 安全/权限拒绝 → 不可重试
    RATE_LIMITED = "rate_limited"            # 并发/频率限制 → 可重试（退避后）
    NETWORK_ERROR = "network_error"          # 短暂网络故障 → 可重试
    UNAVAILABLE = "unavailable"              # 依赖后端不可用 → 重试无意义
    CANCELLED = "cancelled"                  # 被打断 → 不重试
    TOOL_ERROR = "tool_error"                # 工具内部异常 → 不重试
    UNKNOWN_TOOL = "unknown_tool"            # 工具未注册 → 不重试

    @property
    def is_retryable(self) -> bool:
        """是否适合重试。"""
        return self in (
            ErrorType.TIMEOUT,
            ErrorType.RATE_LIMITED,
            ErrorType.NETWORK_ERROR,
        )
```

- [ ] **Step 2: Verify new file works**

Run: `python -c "from core.schema.error_type import ErrorType; assert ErrorType.TIMEOUT == 'timeout'; assert ErrorType.TIMEOUT.is_retryable; print('OK')"`
Expected: OK

- [ ] **Step 3: Replace definition in `Module/tool/protocol.py`**

Delete lines 24-44 (the entire `ErrorType` class) and replace with an import:

```diff
- class ErrorType(str, Enum):
-     """工具执行错误类型 — 帮助认知循环决定重试策略。"""
- 
-     INVALID_ARGUMENT = "invalid_argument"
-     ...
-     @property
-     def is_retryable(self) -> bool:
-         ...
+ from core.schema.error_type import ErrorType
```

- [ ] **Step 4: Verify backward compatibility**

Run: `python -c "from Module.tool.protocol import ErrorType; assert ErrorType.TIMEOUT == 'timeout'; assert ErrorType.CANCELLED == 'cancelled'; print('OK')"`
Expected: OK (import chain: `Module.tool.protocol → core.schema.error_type`)

- [ ] **Step 5: Update `core/schema/message.py` line 13**

```diff
- from Module.tool.protocol import ErrorType
+ from .error_type import ErrorType
```

- [ ] **Step 6: Update `core/adapter/__init__.py` line 15**

```diff
- from Module.tool.protocol import ToolResult, ErrorType
+ from Module.tool.protocol import ToolResult
+ from core.schema.error_type import ErrorType
```

- [ ] **Step 7: Update `core/adapter/tool_adapter.py` line 13**

```diff
- from Module.tool.protocol import ToolResult, ErrorType  # noqa: F401 — 由 __init__.py 重导出
+ from Module.tool.protocol import ToolResult
+ from core.schema.error_type import ErrorType  # noqa: F401 — 由 __init__.py 重导出
```

- [ ] **Step 8: Verify `message.py` no longer depends on Module.tool**

Run: `python -c "from core.schema.message import ToolResultMessage; print('OK')"`
Expected: OK (no ImportError from missing Module.tool.protocol)

- [ ] **Step 9: Run full regression**

```bash
make test-quick
```

Expected: All pass.

- [ ] **Step 10: Commit**

```bash
git add core/schema/error_type.py Module/tool/protocol.py core/schema/message.py core/adapter/__init__.py core/adapter/tool_adapter.py
git commit -m "refactor: move ErrorType from Module/tool to core/schema"
```
