"""core/personality 共享工具函数。"""
import asyncio
import logging
import re


def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。"""
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _schedule(coro) -> None:
    """Schedule a coroutine on the event loop, if one is running.

    Wrapped so exceptions in the fire-and-forget task are logged.
    """
    try:
        loop = asyncio.get_running_loop()

        async def _safe_wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger = logging.getLogger(__name__)
                _logger.warning("CharacterEngine scheduled task 异常", exc_info=True)

        loop.create_task(_safe_wrapper())
    except RuntimeError:
        pass  # no event loop running — test environment, ignore
