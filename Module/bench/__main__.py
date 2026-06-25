"""统一延迟基准测试。运行: python -m Module.bench"""

from Module._common.main_runner import run_module_main
from .bench import LatencyBench


async def main():
    bench = LatencyBench()
    await bench.run_all()


if __name__ == "__main__":
    run_module_main(main, load_env=False)
