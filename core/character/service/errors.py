"""角色领域错误层级（自 core/character/service.py 按写入链拆出）。

只放 `CharacterError` 家族：路由层按异常类型映射 wire 错误码，故这些类型是跨模块
的稳定契约——`core.character.session_binding`、`core.input.*_routes`、
`core.cognitive.loop` 都按名从 `core.character.service` 导入它们。
"""

from __future__ import annotations

from core.storage.base import StorageUnavailableError
from frontend_contract.rest import CharacterValidationIssue


class CharacterError(RuntimeError):
    """角色领域错误的基类。"""


class CharacterNotFoundError(CharacterError):
    pass


class CharacterInstanceNotFoundError(CharacterError):
    pass


class CharacterImmutableError(CharacterError):
    pass


class CharacterArchivedError(CharacterError):
    pass


class CharacterValidationError(CharacterError):
    def __init__(
        self,
        message: str = "角色内容校验失败",
        issues: list[CharacterValidationIssue] | None = None,
    ):
        super().__init__(message)
        self.issues = issues or []


class CharacterTooLargeError(CharacterError):
    pass


class CharacterQueryError(CharacterError):
    pass


class RevisionConflictError(CharacterError):
    def __init__(self, character_id: str, current_revision: int | None = None):
        super().__init__(character_id)
        self.character_id = character_id
        self.current_revision = current_revision


class CharacterStateConflictError(CharacterError):
    def __init__(self, instance_id: str, current_state_version: int | None = None):
        super().__init__(instance_id)
        self.instance_id = instance_id
        self.current_state_version = current_state_version


class CharacterBusyError(CharacterError):
    pass


class CharacterPreviewUnavailableError(CharacterError):
    """Preview provider is deliberately unavailable until a stateless adapter exists."""


# 继承 StorageUnavailableError，使连接/持久化失败统一 fail-closed 为 503。
class CharacterStorageUnavailableError(StorageUnavailableError):
    pass
