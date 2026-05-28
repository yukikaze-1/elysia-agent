"""时间相关工具 — 获取当前时间、设置提醒。"""

from datetime import datetime

from Module.tool import tool


@tool(description="获取当前日期和时间", timeout=5.0)
async def get_time() -> str:
    """返回当前时间字符串。"""
    return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
