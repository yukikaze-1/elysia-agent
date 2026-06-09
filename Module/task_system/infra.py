"""TaskSystem 共享基础设施。

注意：本文件的内容后续会转移到其它地方，不会留在 TaskSystem 模块内。
AgentInfra 最终可能上提到 Core 层或独立的共享层。
当前暂时放在这里，方便 TaskSystem 开发阶段使用。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

from Module.task_system.schema import ConstraintConfig


@dataclass
class AgentInfra:
    """跨任务共享的底层资源容器。

    创建成本高、不含任务状态、协程安全。
    TaskSystem 构造时注入，整个生命周期内共享。
    各个 Sub-Agent / Worker Agent 只读引用，不做修改。

    字段：
        llm:            LLM 客户端（AsyncOpenAI），跨所有 Agent 共享
        milvus:         Milvus 向量数据库客户端
        http:           httpx 异步 HTTP 客户端
        tool_registry:  ToolRegistry，全量工具注册表
        constraints:    全局约束配置（天花板）。这是 TaskSystem 全生命周期的
                        全局约束，DelegationPreparer 产出的 TaskConstraints
                        与其取 min 后生效。
    """

    llm: AsyncOpenAI
    milvus: object  # MilvusClient，避免强依赖 milvus 包
    http: httpx.AsyncClient
    tool_registry: object  # ToolRegistry，避免循环导入
    constraints: ConstraintConfig
