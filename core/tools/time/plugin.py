"""时间工具插件 — 获取当前时间（支持时区和格式）。"""

from datetime import datetime, timezone, timedelta, tzinfo
from typing import Any

try:
    import zoneinfo as _zoneinfo
    HAS_ZONEINFO = True
except ImportError:
    HAS_ZONEINFO = False

from pydantic import BaseModel, Field
from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter, tool_failure, ToolResult
from Module.tool.safety import SafetyProfile


class TimeParams(BaseModel):
    timezone: str = Field(
        default="UTC",
        description="时区，如 'UTC', 'Asia/Shanghai', 'US/Eastern'，或 '+8' 表示 UTC+8",
    )
    format: str = Field(
        default="iso",
        description="输出格式: 'iso' (ISO 8601) 或 'human' (人类可读)",
    )


class TimePlugin(ToolPlugin):
    name = "get_time"
    description = "获取当前日期和时间，支持指定时区和格式"
    param_model = TimeParams
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: TimeParams,
    ) -> str | ToolResult:
        """获取当前日期和时间。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 时间参数，包含时区和输出格式。

        Returns:
            当前时间字符串。
        """
        tz_str = params.timezone.strip()
        # 可变 tz：timezone（±偏移/UTC）与 zoneinfo.ZoneInfo（IANA 名称）均为 tzinfo 子类
        tz: tzinfo | None = None
        if tz_str.startswith(("+", "-")):
            try:
                hours = int(tz_str)
                tz = timezone(timedelta(hours=hours))
            except ValueError:
                return tool_failure(
                    f"无效的时区偏移: {tz_str}，请使用如 '+8' 或 'UTC'",
                    error_type=ErrorType.INVALID_ARGUMENT)
        elif tz_str == "UTC":
            tz = timezone.utc
        else:
            if not HAS_ZONEINFO:
                return tool_failure("时区功能不可用（缺少 zoneinfo 模块）",
                            error_type=ErrorType.UNAVAILABLE)
            try:
                tz = _zoneinfo.ZoneInfo(tz_str)
            except Exception:
                return tool_failure(
                    f"无效的时区名称: '{tz_str}'，请使用 'UTC'、'Asia/Shanghai' 或 '+8'",
                    error_type=ErrorType.INVALID_ARGUMENT)

        now = datetime.now(tz)

        if params.format == "human":
            return now.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            return now.isoformat()
