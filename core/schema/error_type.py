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
    GATE_REJECTED = "gate_rejected"          # 调度前被 gate 拦截 → 不可重试

    @property
    def is_retryable(self) -> bool:
        """是否适合重试。"""
        return self in (
            ErrorType.TIMEOUT,
            ErrorType.RATE_LIMITED,
            ErrorType.NETWORK_ERROR,
        )
