"""DelegationPreparer: LLM 准备委派上下文。

CognitiveLoop 调用 submit_task 后、TaskSystem.submit() 之前，
用一次 LLM 调用准备干净的委派上下文。

当前骨架——Prompt 待后续测试时详细调优。
Prompt 全文见 architecture.md Section 9。
"""

from __future__ import annotations

import logging

from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    TaskConstraints,
    Tool,
    UserProfile,
)

logger = logging.getLogger(__name__)


class DelegationPreparer:
    """一次 LLM 调用，准备委派上下文。

    代码在 TaskSystem 模块内，调用者却是 CognitiveLoop。
    将来可独立为单独的 delegation 服务。

    四个职责（仅 status=ok 时产出 DelegationContext）：
    1. 意图透传（intent/hints 原样写入 DelegationContext）
    2. 工具筛选（从全量工具中选出相关工具名）
    3. 上下文摘要（从对话历史中提取相关信息，≤500 字）
    4. 任务级约束（估算 max_dag_nodes、max_mutations）
    """

    # TODO: __init__ 接收 LLM client 和 prompt template
    # TODO: _validate_output 后处理校验——剔除 LLM 幻觉出的不存在的工具名
    # TODO: Prompt 设计见 architecture.md Section 9，待测试调优

    async def prepare(
        self,
        intent: str,
        hints: str | None,
        session_messages: list[dict],
        user_profile: UserProfile,
        tools: list[Tool],
        global_constraints: ConstraintConfig,
    ) -> DelegationResult:
        """一次 LLM 调用，准备委派上下文。

        Args:
            intent:              LLM 输出的完整任务意图（透传，不修改）
            hints:               LLM 观察到的元信息（紧急程度、偏好等）
            session_messages:    全部对话消息（不截断，LLM 自己判断相关性）
            user_profile:        用户画像（只读消费）
            tools:               全量工具列表
            global_constraints:  TaskSystem 全局约束（天花板）

        Returns:
            DelegationResult——ok / no_matching_tools / too_vague
        """
        # TODO: 实现 LLM 调用
        raise NotImplementedError(
            "DelegationPreparer.prepare() 尚未实现。"
            " Prompt 待设计，见 architecture.md Section 9。"
        )
