"""dotenv 统一入口。所有 __main__.py 应通过此函数加载环境变量。"""
import os
from pathlib import Path


def ensure_dotenv() -> None:
    """加载 .env 文件（如果存在）。幂等——重复调用不重复加载。

    查找顺序：项目根目录。使用 load_dotenv(override=False) ——
    已存在的环境变量不被覆盖。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv 不是核心依赖，缺失时静默跳过

    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
