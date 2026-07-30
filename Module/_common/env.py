"""dotenv 统一入口。所有 __main__.py 应通过此函数加载环境变量。"""
import os
from pathlib import Path

from paths import ProjectPath, get_project_root


def ensure_dotenv() -> None:
    """加载 .env 文件（如果存在）。幂等——重复调用不重复加载。

    查找顺序：项目根目录。使用 load_dotenv(override=False) ——
    已存在的环境变量不被覆盖。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # dotenv 不是核心依赖，缺失时静默跳过

    project_root = get_project_root()
    env_path = ProjectPath.DOTENV.resolve()
    if env_path.exists():
        load_dotenv(env_path, override=False)
