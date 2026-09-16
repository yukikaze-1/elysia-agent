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
from typing import TYPE_CHECKING, Callable

from Module.workflow_contract.schema import ConstraintConfig

if TYPE_CHECKING:
    # 仅类型层导入（与下方 MilvusClient 同款）：本模块**只持有**外部客户端实例，
    # 从不构造它们——`from __future__ import annotations` 下字段注解是字符串，
    # 运行期不需要类对象。
    #
    # 为什么必须收进 TYPE_CHECKING（Issue D）：core/schema/types.py 与 event.py 要
    # 契约层的 SessionId/WorkflowId，而 `from Module.workflow_contract.schema import …`
    # 会先执行父包 __init__.py（唯一装配点，re-export AgentInfra → 本模块）。
    # 于是 `import core.schema` 连带付 ~1s 的 openai+httpx 导入税——而它只是数据模型层。
    import httpx
    from openai import AsyncOpenAI
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

    #6（决策 #6）+ Issue B：终态跨重启不丢——Agent 的 mailbox 排空在**投递之前**落库
    （写库时恒 `delivered=False`，`_state` 不参与判据），消费方真正接手（spawn 出宣告
    回合）后置 True，启动时补投未投递的记录（只投递，不标记）。
    类型为 `object`：契约层不依赖 core.storage（与 `tool_registry` 同一处理），
    消费方按鸭子类型调用。
    """

    terminal_result_sink: Callable[[str, str, dict], None] | None = None
    """终态事件**同步**落盘回调（core 侧 `TaskResultWAL` 的 bound method）。

    #6 硬杀窗口：`background_task_results` 的落库点在 Agent 的 mailbox 轮询上
    （周期 0.5s），进程在该窗口内被硬杀则终态随内存消失。engine 在 `submit()` 时
    把它挂到 `WorkflowMailbox.terminal_sink`，`push_mailbox_event()` 推终态事件时
    调用——于是"终态产生"与"记录落盘"是同一个同步动作。

    **必须同步**（返回 None，不是协程）：若落盘要 await，任意 await 点被取消或
    进程死亡都会让落盘不发生，恰好丢掉的正是此刻唯一需要记住的那件事。
    签名 `(task_id, event, data)`——data 是终态事件的原始 dict；措辞是 core 的事，
    契约层只传事实（详见 `core/storage/task_result_wal.py`）。
    """
