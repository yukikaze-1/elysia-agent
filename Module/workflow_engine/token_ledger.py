"""TokenLedger — 单次任务 token 账本，write-through 到 WorkflowState。

每次 LLM 调用后立即 record()，直接写入 WorkflowState.token_entries + token_usage。
任务暂停/崩溃/取消时数据已持久在 WorkflowState 中，不依赖最终 flush。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from Module.workflow_contract import NodeId
from Module.workflow_engine.schema import (
    TokenComponent,
    TokenComponentType,
    TokenUsage,
)

if TYPE_CHECKING:
    from Module.workflow_engine.schema import WorkflowState


class TokenLedger:
    """单次任务 token 账本，write-through 到 WorkflowState。

    用法:
        ledger = TokenLedger(task_state)
        # LLM 调用后:
        ledger.record(
            component=TokenComponentType.PLANNER,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            model="deepseek-v4",
        )
    """

    def __init__(self, task_state: WorkflowState) -> None:
        """初始化 token 台账。

        Args:
            task_state: 用于写入 token 条目的 WorkflowState。
        """
        self._ts = task_state

    def record(
        self,
        component: TokenComponentType,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        node_id: NodeId | None = None,
    ) -> None:
        """记录一次 LLM 调用，立即写入 WorkflowState。

        Args:
            component:         消耗来源（planner / executor / evaluator）
            prompt_tokens:     输入 token 数
            completion_tokens: 输出 token 数
            model:             LLM 模型名
            node_id:           关联节点 ID（Executor/Evaluator 有，Planner 为 None）
        """
        total = prompt_tokens + completion_tokens
        entry = TokenComponent(
            component=component,
            node_id=node_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            model=model,
        )
        self._ts.token_entries.append(entry)
        key = component.value
        self._ts.token_usage[key] = self._ts.token_usage.get(key, 0) + total

    def to_token_usage(self) -> TokenUsage:
        """导出 TokenUsage（供查询 API）。

        Returns:
            TokenUsage: 包含完整 components 列表的 token 使用明细。
        """
        return TokenUsage(
            task_id=self._ts.task_id,
            components=list(self._ts.token_entries),
        )

    @property
    def total_tokens(self) -> int:
        """返回所有条目累计消耗的 token 总数。"""
        return sum(e.total_tokens for e in self._ts.token_entries)
