"""统一延迟基准测试。运行: python -m Module.bench"""

import asyncio

from .bench import LatencyBench


async def main():
    bench = LatencyBench()
    await bench.run_all()


if __name__ == "__main__":
    asyncio.run(main())
