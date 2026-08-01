"""WorkflowEngine 共享基础设施。

注意：本文件的内容后续会转移到其它地方，不会留在 WorkflowEngine 模块内。
AgentInfra 最终可能上提到 Core 层或独立的共享层。
当前暂时放在这里，方便 WorkflowEngine 开发阶段使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from openai import AsyncOpenAI

from Module.workflow_engine.schema import ConstraintConfig

if TYPE_CHECKING:
    from pymilvus import MilvusClient


@dataclass
class AgentInfra:
    """跨任务共享的底层资源容器。

    创建成本高、不含任务状态、协程安全。
    WorkflowEngine 构造时注入，整个生命周期内共享。
    各个 Sub-Agent / Worker Agent 只读引用，不做修改。

    字段：
        llm:            LLM 客户端（AsyncOpenAI），跨所有 Agent 共享
        milvus:         Milvus 向量数据库客户端（None 表示向量搜索不可用）
        http:           httpx 异步 HTTP 客户端
        tool_registry:  ToolRegistry，全量工具注册表（类型为 object 避免循环导入）
        constraints:    全局约束配置（天花板）。这是 WorkflowEngine 全生命周期的
                        全局约束，DelegationPreparer 产出的 WorkflowConstraints
                        与其取 min 后生效。
    """

    llm: AsyncOpenAI
    milvus: MilvusClient | None
    http: httpx.AsyncClient | None  # v1 预留，工具通过 ToolContext 接收 HTTP 客户端
    tool_registry: object  # ToolRegistry，避免循环导入
    constraints: ConstraintConfig
    llm_model: str = field(
        default_factory=lambda: os.getenv("TASK_SYSTEM_MODEL", "deepseek-v4-pro")
    )  # LLM 模型名，Planner/Executor/Evaluator 共用（env: TASK_SYSTEM_MODEL）
