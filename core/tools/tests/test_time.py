"""TimePlugin 单元测试 — 覆盖时区偏移、IANA 时区、格式、无效输入和默认行为。"""

import pytest

from Module.tool.protocol import ToolContext, ToolResult, ErrorType

from core.tools.time.plugin import TimePlugin, TimeParams

from core.tools.tests._failure_helpers import assert_failed

pytestmark = pytest.mark.unit

# ============================================================================
# 辅助函数 — 使用给定参数调用 TimePlugin.execute 并返回结果字符串
# ============================================================================

async def _run(timezone: str = "UTC", fmt: str = "iso") -> str:
    """创建插件实例，用指定参数执行，返回结果字符串。"""
    plugin = TimePlugin()
    params = TimeParams(timezone=timezone, format=fmt)
    result = await plugin.execute(
        ctx=ToolContext(),
        emit=None,
        params=params,
    )
    return result


# ============================================================================
# TimePlugin 基础测试
# ============================================================================

class TestTimePlugin:
    @pytest.mark.asyncio
    async def test_utc_iso_contains_t(self):
        """UTC 时区 iso 格式返回 ISO 8601 字符串（包含 'T'）。"""
        result = await _run(timezone="UTC", fmt="iso")
        assert "T" in result, f"ISO 8601 应包含 'T'，实际: {result}"

    @pytest.mark.asyncio
    async def test_utc_human_format_contains_utc(self):
        """UTC 时区 human 格式返回包含 'UTC' 的格式化字符串。"""
        result = await _run(timezone="UTC", fmt="human")
        assert "UTC" in result, f"human 格式应包含 'UTC'，实际: {result}"

    @pytest.mark.asyncio
    async def test_offset_plus_8_returns_plus_08_00(self):
        """偏移时区 '+8' 返回包含 '+08:00' 的结果。"""
        result = await _run(timezone="+8", fmt="iso")
        assert "+08:00" in result, f"应包含 '+08:00'，实际: {result}"

    @pytest.mark.asyncio
    async def test_offset_minus_5_returns_minus_05_00(self):
        """负偏移 '-5' 返回包含 '-05:00' 的结果。"""
        result = await _run(timezone="-5", fmt="iso")
        assert "-05:00" in result, f"应包含 '-05:00'，实际: {result}"

    @pytest.mark.asyncio
    async def test_invalid_offset_returns_error_message(self):
        """无效偏移 '+abc' 返回错误信息。"""
        result = await _run(timezone="+abc", fmt="iso")
        assert_failed(result, contains="无效的时区偏移")

    @pytest.mark.asyncio
    async def test_invalid_iana_timezone_returns_error(self):
        """无效 IANA 时区 'Mars/Olympus' 返回错误信息。"""
        result = await _run(timezone="Mars/Olympus", fmt="iso")
        assert_failed(result, contains="无效的时区名称")

    @pytest.mark.asyncio
    async def test_valid_iana_timezone_asia_shanghai(self):
        """有效 IANA 时区 'Asia/Shanghai' 返回非空结果。"""
        result = await _run(timezone="Asia/Shanghai", fmt="iso")
        assert len(result) > 0, f"Asia/Shanghai 应返回非空结果，实际: {result}"

    @pytest.mark.asyncio
    async def test_default_params_returns_valid_iso_string(self):
        """默认参数（无时区/格式指定）返回有效的 ISO 字符串。"""
        result = await _run()  # 使用默认 UTC / iso
        assert "T" in result, f"默认参数应返回有效 ISO 字符串，实际: {result}"
