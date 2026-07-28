"""统一 __main__ 入口：ensure_dotenv + asyncio.run。"""
import asyncio
from collections.abc import Coroutine, Callable

from Module._common.env import ensure_dotenv


def run_module_main(
    async_fn: Callable[[], Coroutine[None, None, None]],
    *,
    load_env: bool = True,
) -> None:
    """加载 .env 并运行 async 主函数。

    Args:
        async_fn: 不接受参数、返回 None 的异步主函数。
        load_env: 是否在运行前加载 .env 文件，默认为 True。

    Returns:
        None

    用法（在 __main__.py 中）：
        from Module._common.main_runner import run_module_main

        async def main():
            ...

        if __name__ == "__main__":
            run_module_main(main)
    """
    if load_env:
        ensure_dotenv()
    asyncio.run(async_fn())
