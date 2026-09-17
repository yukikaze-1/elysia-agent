"""记忆层——会话上下文（MongoDB 或内存兜底）。"""

from .session import SessionMemory
from .long_term import LongTermMemory

# 公开面逐名声明（2026-09-17 Batch 2.3）：本包是「单一装配点」，re-export 是
# **有意**的公开 API。显式 `__all__` 让它从「碰巧没被用到」变成「声明导出」——
# 这正是 F401 判据要求的区分（否则只能靠 noqa 糊过去）。
#
# 刻意**不**把 `embedding_provider`（子模块）列进来：`from core.memory import
# embedding_provider` 走的是**子模块导入**机制，与 `__all__` 无关（实测可用）；
# 而把一个不在模块属性表里的名字写进 `__all__` 会被 pyright 判
# `reportUnsupportedDunderAll`（实测 1 warning）。子模块不是「重导出的名字」。
__all__ = [
    "SessionMemory",
    "LongTermMemory",
]
