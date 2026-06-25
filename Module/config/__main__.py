"""集成测试：加载 config.yaml 并打印配置摘要。运行: python -m Module.config"""

import os
import sys

from Module._common.main_runner import run_module_main
from .loader import ConfigLoader

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")


async def main():
    print("Config 模块 — 配置加载测试")
    print(f"  配置文件: {CONFIG_PATH}")
    print()

    if not os.path.exists(CONFIG_PATH):
        print("config.yaml 不存在，跳过")
        return

    try:
        loader = ConfigLoader(dict, yaml_path=CONFIG_PATH)
        data = loader.load()
        print("已加载的 YAML 顶级键:")
        for key in sorted(data.keys()):
            val = data[key]
            if isinstance(val, dict):
                print(f"  {key}: {{{len(val)} 个子键}}")
            elif isinstance(val, list):
                print(f"  {key}: [{len(val)} 个元素]")
            else:
                print(f"  {key}: {val}")
    except Exception as e:
        print(f"加载失败: {e}")
        sys.exit(1)

    print()
    print("测试完成!")


if __name__ == "__main__":
    run_module_main(main)
