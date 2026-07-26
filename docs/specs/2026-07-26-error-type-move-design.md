# ErrorType 定义迁移 — 从 Module/tool 移至 core/schema

> 状态: spec  
> 日期: 2026-07-26  
> 关联: [ToolAdapter](2026-07-26-tool-adapter-design.md) · Schema 层反向依赖修复

## 背景

`core/schema/message.py` 从 `Module.tool.protocol` 导入 `ErrorType`（`str, Enum`，9 个成员），形成 core/schema → Module/tool 的反向依赖。

`ErrorType` 是纯数据——9 个字符串标签 + 一个 `.is_retryable` property。它不依赖 Module/ 的任何东西，应该放在 schema/ 层作为共享类型。

## 目标

将 `ErrorType` 定义从 `Module/tool/protocol.py` 移至 `core/schema/error_type.py`，使依赖方向变为正确的 `Module/tool → core/schema`。

## 设计

### 新建 `core/schema/error_type.py`

从 `Module/tool/protocol.py:24-44` 提取 `ErrorType` 类，原样放置：

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

### 修改 `Module/tool/protocol.py`

删除 `ErrorType` 类定义（lines 24-44），替换为从 schema 的 import：

```diff
- class ErrorType(str, Enum):
-     ...（删除 ~20 行）
+ from core.schema.error_type import ErrorType
```

`protocol.py` 保持对其他模块的向后兼容——所有通过 `from Module.tool.protocol import ErrorType` 导入的代码继续工作（通过重导出）。

### 修改 `core/schema/message.py`

```diff
- from Module.tool.protocol import ErrorType
+ from .error_type import ErrorType
```

这是本次变更的核心目的——消除反向依赖。

### 修改 `core/adapter/__init__.py`

```diff
- from Module.tool.protocol import ToolResult, ErrorType
+ from Module.tool.protocol import ToolResult
+ from core.schema.error_type import ErrorType
```

同样修改 `core/adapter/tool_adapter.py`：
```diff
- from Module.tool.protocol import ToolResult, ErrorType  # noqa: F401
+ from Module.tool.protocol import ToolResult
+ from core.schema.error_type import ErrorType  # noqa: F401 — 由 __init__.py 重导出
```

### 其他文件

以下文件通过 `Module.tool.protocol` 间接导入 `ErrorType`——导入链 `File → protocol.py → core.schema.error_type` 自动解析，**无需修改**：

| 文件 | Import 形式 | 是否兼容 |
|------|-----------|---------|
| `Module/tool/__init__.py` | `from .protocol import ... ErrorType` | ✅ protocol.py 重导出 |
| `Module/tool/manager.py` | `from .protocol import ErrorType, ...` | ✅ |
| `Module/tool/safety.py` | `from Module.tool.protocol import ToolResult, ErrorType` | ✅ |
| `Module/tool/__main__.py` | `from .protocol import ... ErrorType` | ✅ |
| `Module/tool/tests/test_framework.py` | `from Module.tool.protocol import ... ErrorType` | ✅ |
| `Module/task_system/core/executor.py` | `from Module.tool.protocol import ErrorType as ToolErrorType` | ✅ |
| `Module/task_system/tests/unit/test_executor.py` | `from Module.tool.protocol import ErrorType` | ✅ |
| `core/task_scheduler.py` | `from core.adapter import ... ErrorType` | ✅ adapter 重导出 |
| `core/cognitive/tools.py` | `from core.adapter import ... ErrorType` | ✅ |
| `core/tests/test_task_scheduler.py` | `from Module.tool.protocol import ... ErrorType` | ✅ |
| `core/schema/tests/test_message.py` | `from Module.tool.protocol import ErrorType` → `from core.schema.error_type import ErrorType` | 直接修改 |

### 不复用的旧代码

`Module/tool/protocol.py` 中原有 `ErrorType` 类定义（lines 24-44）——删除，替换为 import。

## 验证

```bash
# 确认 import 正常
python -c "from core.schema.error_type import ErrorType; print(ErrorType.TIMEOUT, ErrorType.TIMEOUT.is_retryable)"

# 确认 protocol.py 向后兼容
python -c "from Module.tool.protocol import ErrorType; print(ErrorType.INVALID_ARGUMENT)"

# 确认 message.py 不再依赖 Module.tool
python -c "from core.schema.message import ToolResultMessage"

# 快速回归
make test-quick
```
