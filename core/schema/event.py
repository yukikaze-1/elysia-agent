"""事件数据类型 — EventBus 的 Pydantic 模型。

每个模型自带 event_type discriminator，形成 EventData union。
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from core.schema.agent import AgentState, EventType
from core.schema.types import MessageId, RequestId, SessionId
from Module.workflow_contract.schema import WorkflowId


class VoiceReason(StrEnum):
    """SystemTickData 的触发原因枚举。"""
    SILENCE_TIMEOUT = "silence_timeout"
    TIME_OF_DAY = "time_of_day"


class InterruptData(BaseModel):
    event_type: Literal[EventType.INTERRUPT] = EventType.INTERRUPT
    source: str = Field(default="?")


class StateChangeData(BaseModel):
    event_type: Literal[EventType.STATE_CHANGE] = EventType.STATE_CHANGE
    state: AgentState
    request_id: RequestId


class UserInputData(BaseModel):
    event_type: Literal[EventType.USER_INPUT] = EventType.USER_INPUT
    text: str
    user_name: str
    request_id: RequestId
    content: list[dict] | None = None
    emotion: str = ""
    events: list[str] = Field(default_factory=list)
    # 批次 2（v2 消息）：预分配用户消息 ID（ACK 一致）+ 来源协议版本
    message_id: MessageId | None = None
    protocol_version: int = 1
    # 批次 3（multi-session）：可选会话 ID，None = 未归属
    conversation_id: SessionId | None = None
    # 内部生命周期标记：True 表示 Agent/voice 已在 SessionManager.reserve_work()
    # 登记 accepted work；消费终态、丢弃或中断时必须成对 release_work()。
    session_work_reserved: bool = False
    # CR-13：发起本次消息的 WS 连接 ID（v2 握手分配；v1/REST 为 ""）。经
    # ToolContext.services["_origin_connection_id"] 传到工具安全管道，使 HITL
    # 高危工具审批**只认发起连接**——原先审批只查 request_id 是否存在，而该 ID
    # 被广播到所有通道，多客户端下任意一方都能批准别人的高危调用。
    origin_connection_id: str = ""


class SystemTickData(BaseModel):
    event_type: Literal[EventType.SYSTEM_TICK] = EventType.SYSTEM_TICK
    reason: VoiceReason
    elapsed: float | None = None
    time: str | None = None


class TaskUserOutcome(StrEnum):
    """后台任务终态的**用户级**语义（Batch 1.8）。

    值域刻意只有三档。内部 taxonomy（`planning_failed` / `execution_failed` /
    `timeout` / `interrupted` …）可以**压缩**成这三档，但**不得反转**：
    `failed` 永远不许被渲染成成功语义。
    """
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 内部终态 kind → 用户级语义。**唯一映射点**——措辞层（`_build_wake_up_message`）
#: 只消费 `task_user_outcome` 的输出，不得各自 `if error_type == ...` 堆分支
#: （Batch 1.8 §8/§10）。
_INTERNAL_TERMINAL_OUTCOMES: dict[str, TaskUserOutcome] = {
    "completed": TaskUserOutcome.COMPLETED,
    "failed": TaskUserOutcome.FAILED,
    "cancelled": TaskUserOutcome.CANCELLED,
    # `interrupted`：停机/硬杀时补记的终态（"未产生结果"）在库里本来就是**终态记录**、
    # 会被启动补投给用户，所以对用户就是"没做成"，不另立第四档（Batch 1.8 §17：
    # 以真实代码为准，不擅自归类）。
    "interrupted": TaskUserOutcome.FAILED,
}


def task_user_outcome(status: str = "", is_error: bool = False) -> TaskUserOutcome:
    """把内部终态压缩成用户级语义（**唯一映射点**）。

    未知 / 缺失 `status`（Batch 1.8 之前的旧持久化记录没有这个字段）→ 按
    `is_error` 兜底：宁可把"不确定"说成失败，也绝不把失败说成成功——反转语义
    正是本批要消灭的缺陷（§5 U2、§22 向后兼容）。

    **失败是黏性的**：`is_error=True` 与 `status="completed"` 同时出现（数据被外力
    改坏 / 未来某个生产者写错）时取 FAILED。这一条不服务于任何正常输入（生产代码
    的四个构造点都由同一个终态 kind 派生这两个字段，不会自相矛盾），它服务于
    §5 U2 的**方向上**的不对称：细节可以压扁，失败绝不允许翻成成功。
    """
    mapped = _INTERNAL_TERMINAL_OUTCOMES.get(str(status or "").strip().lower())
    if mapped is None:
        return TaskUserOutcome.FAILED if is_error else TaskUserOutcome.COMPLETED
    if is_error and mapped is TaskUserOutcome.COMPLETED:
        return TaskUserOutcome.FAILED
    return mapped


class TaskResultData(BaseModel):
    event_type: Literal[EventType.TASK_RESULT] = EventType.TASK_RESULT
    task_id: WorkflowId
    result: str
    original_intent: str
    dispatched_at: str
    is_error: bool = False
    #: 内部 logical terminal kind（completed / failed / cancelled / interrupted）。
    #: **空串 = 未标注**（reminder 等合成事件、以及 Batch 1.8 之前的旧持久化记录）——
    #: 消费侧经 `task_user_outcome` 用 `is_error` 兜底，故旧记录照常解析、且不会把
    #: 失败翻成成功。`is_error` 单独无法区分 failed 与 cancelled，本字段补的正是
    #: 这一格（Batch 1.8 §6/§7：先证明现有字段不够，才加最小稳定字段）。
    status: str = ""


EventData = Annotated[
    InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData,
    Field(discriminator="event_type"),
]
