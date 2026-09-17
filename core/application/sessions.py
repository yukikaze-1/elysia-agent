"""会话用例（应用层，ARCH-INC-P1-02）。

**为什么需要它**：「激活会话」的业务规则原先在**两处各写了一遍**，而且顺序不同——

| 入口 | 判定顺序 | 未知会话时返回 |
|---|---|---|
| `core/input/ws_handlers/session.py` | 忙门禁 → 存在性 | `AGENT_BUSY`（**错**：那个会话根本不存在）|
| `core/input/session_routes.py` | 存在性 → 忙门禁 | `SESSION_NOT_FOUND` |

同一件事两套顺序、两种码，客户端按哪个写都会在另一个入口上错。此处合成**一个**实现：
**存在性优先**（更具体的事实先判——不存在的会话谈不上"忙"），两个入口都调它。

依赖按**最小面**注入（三个 callable），不接收整个 `Agent` 再做 Service Locator——
这是评估稿对应用层的明确约束。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from core.memory.session_manager import SessionBusyError

if TYPE_CHECKING:
    from core.memory.session import SessionMemory

logger = logging.getLogger(__name__)

#: 稳定错误码（契约层已登记）。
CODE_SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
CODE_AGENT_BUSY = "AGENT_BUSY"


class SessionUseCaseError(Exception):
    """会话用例的稳定失败（携带契约错误码，由传输层映射状态码）。

    Attributes:
        code: 契约错误码。
        message: 面向用户的中文说明。
        retryable: 客户端是否可稍后重试。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SessionApplicationService:
    """会话读写用例（当前只有激活；新增用例须有第二个消费者才加进来）。"""

    def __init__(
        self,
        *,
        has_session: Callable[[str], Awaitable[bool]],
        switch_session: Callable[[str], Awaitable["SessionMemory"]],
        is_accepting_input: Callable[[], bool],
    ) -> None:
        """初始化。

        Args:
            has_session: 会话存在性判定（`SessionManager.has_session`）。
            switch_session: 切换活动会话（`Agent.switch_session`）。
            is_accepting_input: 是否允许此时切换（`AgentStatus.is_accepting_input`）。
        """
        self._has_session = has_session
        self._switch_session = switch_session
        self._is_accepting_input = is_accepting_input

    async def activate(self, conversation_id: str) -> "SessionMemory":
        """激活指定会话。

        Args:
            conversation_id: 目标会话 ID（wire 裸 str）。

        Returns:
            激活后的 `SessionMemory`（`Agent.switch_session` 的返回值——WS 侧需要它
            做回放；REST 侧只取 `session_id`）。

        Raises:
            SessionUseCaseError: 会话不存在（`SESSION_NOT_FOUND`）或 agent 忙
                （`AGENT_BUSY`，retryable）。
        """
        if not conversation_id:
            raise SessionUseCaseError(
                "CONTRACT_VALIDATION", "缺少 conversationId")
        if not await self._has_session(conversation_id):
            logger.debug("activate: 未知会话 %s → SESSION_NOT_FOUND", conversation_id)
            raise SessionUseCaseError(
                CODE_SESSION_NOT_FOUND, f"会话不存在: {conversation_id}")
        if not self._is_accepting_input():
            logger.info("activate: agent 忙，拒绝切换 %s", conversation_id)
            # 忙时拒绝而非排队：切换会与在途的 v2 流式/TTS 并发，
            # 导致 mood/L3 状态跨会话污染（批次 3 review F1-a）。
            raise SessionUseCaseError(
                CODE_AGENT_BUSY, "Agent 忙（生成/TTS 中），无法切换会话",
                retryable=True)
        try:
            return await self._switch_session(conversation_id)
        except SessionBusyError:
            # 双保险：`SessionManager.switch()` 内部还会再查一次忙（检查与切换之间
            # 存在窗口）。原先只有 WS 入口捕获它、REST 入口没有 → 同一竞态在两个
            # 入口一个回 AGENT_BUSY、一个回 500。收进用例层后两边一致。
            logger.info("activate: switch() 双保险 busy check 命中 %s", conversation_id)
            raise SessionUseCaseError(
                CODE_AGENT_BUSY, "Agent 忙（生成/TTS 中），无法切换会话",
                retryable=True) from None


def service_for_agent(agent: object) -> SessionApplicationService:
    """按 Agent 装配用例服务（显式取最小面，不做 Service Locator）。

    Args:
        agent: 具备 `session_manager` / `switch_session` / `status` 的 Agent。
    """
    manager = agent.session_manager                      # type: ignore[attr-defined]
    status = getattr(agent, "status", None)
    return SessionApplicationService(
        has_session=manager.has_session,
        switch_session=agent.switch_session,             # type: ignore[attr-defined]
        is_accepting_input=(lambda: bool(
            getattr(status, "is_accepting_input", True))),
    )
