"""core/personality 共享工具函数。"""
import asyncio
import logging
import re


def _extract_tag(text: str, tag: str) -> str:
    """从 XML 标签中提取内容。

    Args:
        text: 包含 XML 标签的原始文本。
        tag: 标签名（不含尖括号）。

    Returns:
        标签内的文本内容，未找到时返回空字符串。
    """
    pattern = rf"<{tag}>\s*(.*?)\s*</{tag}>"
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _schedule(coro: object) -> None:
    """在事件循环上调度一个协程（fire-and-forget）。

    Wrapped so exceptions in the fire-and-forget task are logged.

    Args:
        coro: 要调度的协程对象。无事件循环时静默忽略（测试环境兼容）。
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
