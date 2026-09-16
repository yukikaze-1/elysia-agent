"""dotenv 统一入口 —— **入口点**（`__main__` / 脚本 / pytest）专用。

本模块会**写** `os.environ`；库代码不要调用它。批 1.5 起 `ConfigLoader` 只读环境、
不再自己加载 .env，加载责任全部收口到入口（目标结构：入口加载环境一次 →
`ConfigLoader` 只读 `os.environ` → 其余系统）。
"""
from pathlib import Path

from paths import ProjectPath

_LOAD_ATTEMPTED: bool = False
"""是否已经尝试过加载（无论成没成功）——保证 `.env` 每个进程最多读一次。"""

_LOADED_PATH: Path | None = None
"""实际加载的 .env 路径；没找到（或 python-dotenv 缺失）时为 None。"""


def ensure_dotenv() -> Path | None:
    """加载仓库根 `.env`（**入口点专用**，每个进程最多一次）。

    查找顺序：`ProjectPath.DOTENV`（可用 `ELYSIA_PATH_DOTENV` 覆盖）。使用
    `load_dotenv(override=False)` —— 已存在的环境变量不被覆盖，因此**外部显式设置
    的环境变量优先于 .env**。

    **幂等是"真"幂等**（批 1.5 修正）：本函数带模块级一次性缓存，第二次调用直接返回
    首次结果、**不重新读文件**。原实现的 docstring 声称幂等，实际每次都重读文件——
    只对"已存在的键"无可见影响，而对**被删掉的键**会重新注入（`override=False` 只保护
    已存在的键）。那种"删掉又回来"的语义正是批 1.5 要消除的顺序依赖。

    Returns:
        Path | None: 实际加载的 `.env` 路径；未找到时 None。
    """
    global _LOAD_ATTEMPTED, _LOADED_PATH
    if _LOAD_ATTEMPTED:
        return _LOADED_PATH
    _LOAD_ATTEMPTED = True

    try:
        from dotenv import load_dotenv
    except ImportError:
        return None  # dotenv 不是核心依赖，缺失时静默跳过

    env_path = ProjectPath.DOTENV.resolve()
    if env_path.exists():
        load_dotenv(env_path, override=False)
        _LOADED_PATH = env_path
    return _LOADED_PATH
