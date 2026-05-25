"""Embedding 后端抽象接口。所有文本向量化后端必须实现此协议。"""

from abc import ABC, abstractmethod


class EmbeddingBackend(ABC):
    """文本向量化后端抽象基类。

    遵循 LangChain 惯例：embed_documents 用于批量文本（插入），
    embed_query 用于单条文本（搜索）。
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """模型输出的向量维度（如 1024、3072），供向量数据库创建 schema。"""
        ...

    @abstractmethod
    async def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        """批量文本 → 向量列表（用于插入）。"""
        ...

    @abstractmethod
    async def embed_query(self, text: str, **kwargs) -> list[float]:
        """单条文本 → 单个向量（用于搜索）。等价于 embed_documents([text])[0]。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭底层资源。本地模型无操作，云端 API 关闭 HTTP client。"""
        ...
