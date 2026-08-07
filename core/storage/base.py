# core/storage/base.py — Repository 抽象基类
"""Repository 抽象基类 — 所有存储后端的最低接口。"""

from abc import ABC, abstractmethod


class Repository(ABC):
    """存储仓库基类。"""

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接。

        Returns:
            True 表示连接成功，False 表示失败。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查。

        Returns:
            True 表示可用，False 表示不可用。
        """
        ...
