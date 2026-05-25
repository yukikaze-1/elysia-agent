from abc import ABC, abstractmethod


class GraphDBBackend(ABC):
    """图数据库后端接口 — 预留（Neo4j 等）"""

    @abstractmethod
    async def add_node(self, label: str, properties: dict) -> str: ...

    @abstractmethod
    async def add_edge(self, from_id: str, to_id: str, rel_type: str, properties: dict | None = None) -> None: ...

    @abstractmethod
    async def query(self, cypher: str, params: dict | None = None) -> list[dict]: ...

    @abstractmethod
    async def close(self) -> None: ...
