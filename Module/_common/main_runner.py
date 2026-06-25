"""统一 __main__ 入口：ensure_dotenv + asyncio.run。"""
import asyncio
from collections.abc import Awaitable, Callable

from Module._common.env import ensure_dotenv


def run_module_main(
    async_fn: Callable[[], Awaitable[None]],
    *,
    load_env: bool = True,
) -> None:
    """加载 .env 并运行 async 主函数。

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
