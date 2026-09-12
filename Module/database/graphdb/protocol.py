from abc import ABC, abstractmethod


class GraphDBBackend(ABC):
    """图数据库后端接口。

    预留（P3-01-RESERVED）：2026-09-12 决策保留作扩展储备，非死代码，勿按引用审计删除。
    用途：Neo4j 等图存储后端（当前无实现、无消费者）。
    """

    @abstractmethod
    async def add_node(self, label: str, properties: dict) -> str:
        """添加节点。

        Args:
            label: 节点标签（如 "Person"、"Memory"）。
            properties: 节点属性字典。

        Returns:
            新节点的唯一标识符。
        """
        ...

    @abstractmethod
    async def add_edge(
        self, from_id: str, to_id: str, rel_type: str,
        properties: dict | None = None,
    ) -> None:
        """添加边。

        Args:
            from_id: 起始节点 ID。
            to_id: 目标节点 ID。
            rel_type: 关系类型（如 "RELATED_TO"）。
            properties: 边的属性字典，可选。
        """
        ...

    @abstractmethod
    async def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """执行 Cypher 查询。

        Args:
            cypher: Cypher 查询语句。
            params: 查询参数，可选。

        Returns:
            查询结果列表，每条为属性字典。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭数据库连接。"""
        ...
