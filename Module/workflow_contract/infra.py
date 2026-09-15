"""WorkflowContract 契约层 — 跨边运行时注入容器。

AgentInfra 是 core 向 workflow_engine 提供运行资源
（AsyncOpenAI/Milvus/http/ToolRegistry/ConstraintConfig）的注入容器，
放在契约层：它是「core 提供资源、engine 消费」的运行时契约，
唯一能同时被两边无环引用的位置。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from openai import AsyncOpenAI

from Module.workflow_contract.schema import ConstraintConfig

if TYPE_CHECKING:
    from pymilvus import MilvusClient


@dataclass
class AgentInfra:
    """跨任务共享的底层资源容器。

    创建成本高、协程安全。
    WorkflowEngine 构造时注入，整个生命周期内共享。
    大部分字段为只读引用；**唯一例外是 `orphan_tools`**——它由 executor
    多协程 add/discard 改写（登记/回收被 shield 保护的孤儿工具任务），
    `WorkflowEngine.stop()` 从其中采集未完成任务等待/取消。core 侧构造
    AgentInfra 但不使用该字段（engine 内部运行时用途，非契约语义）。

    字段：
        llm:            LLM 客户端（AsyncOpenAI），跨所有 Agent 共享
        milvus:         Milvus 向量数据库客户端（None 表示向量搜索不可用）
        http:           httpx 异步 HTTP 客户端
        tool_registry:  ToolRegistry，全量工具注册表（类型为 object 避免循环导入）
        constraints:    全局约束配置（天花板）。这是 WorkflowEngine 全生命周期的
                        全局约束，DelegationPreparer 产出的 WorkflowConstraints
                        与其取 min 后生效。
        orphan_tools:   engine 内部运行时容器（E3）——被 asyncio.shield 保护的
                        在途工具任务集合。executor 创建工具任务时登记、done 回调
                        回收；stop() 采集未完成任务等待 3s 后 cancel。跨任务存活
                        （任务清理后 stop() 仍能从共享 infra 枚举孤儿工具）。
        background_task_results: 后台任务终态结果的持久化 repo（#6 / 决策 #6）。
    """

    llm: AsyncOpenAI
    milvus: MilvusClient | None
    http: httpx.AsyncClient | None  # v1 预留，工具通过 ToolContext 接收 HTTP 客户端
    tool_registry: object  # ToolRegistry，避免循环导入
    constraints: ConstraintConfig
    orphan_tools: set["asyncio.Task"] = field(default_factory=set)  # engine 内部运行时（executor 登记、stop 采集）
    llm_model: str = field(
        default_factory=lambda: os.getenv("TASK_SYSTEM_MODEL", "deepseek-v4-pro")
    )  # LLM 模型名，Planner/Executor/Evaluator 共用（env: TASK_SYSTEM_MODEL）
    background_task_results: object | None = None
    """后台任务终态结果仓储（`BackgroundTaskResultRepository`，core 侧构造注入）。

    #6（决策 #6）：终态跨重启不丢——Agent 的 mailbox 排空在**投递之前**落库
    （`delivered=False`），活跃事件循环消费后置 True，启动时补投未投递的记录。
    类型为 `object`：契约层不依赖 core.storage（与 `tool_registry` 同一处理），
    消费方按鸭子类型调用。
    """
