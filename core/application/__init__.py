"""core/application/ — 窄应用用例层（ARCH-INC-P1-02）。

传输层（`core/input`）与运行时组件（Agent/Repository/工具子系统）之间的**用例边界**。
只收已有第二消费者或含真实业务规则的用例，不做通用 Service 层、不做 Repository 的
无行为代理、不引入 DI 容器（仍由组合根显式装配）。

见 `docs/review/2026-08-30-后端架构分层与模块化增量评估.md` §ARCH-INC-P1-02。
"""
from core.application.attachment_signing import (
    AttachmentSigningService,
    VerifyOutcome,
    service_for_agent as signing_service_for_agent,
)
from core.application.sessions import (
    SessionApplicationService,
    SessionUseCaseError,
    service_for_agent as session_service_for_agent,
)

__all__ = [
    "AttachmentSigningService",
    "VerifyOutcome",
    "signing_service_for_agent",
    "SessionApplicationService",
    "SessionUseCaseError",
    "session_service_for_agent",
]
