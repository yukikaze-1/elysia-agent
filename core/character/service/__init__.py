"""CharacterService 包 —— 角色/身份 REST 领域编排（#44 按写入链拆分后的单一装配点）。

本包是 Stage H REST 契约与领域存储之间的唯一桥接层：所有写入先完成结构/
语义校验，再以 revision CAS 发布；公开响应一律经过严格的 frontend_contract
模型构造，避免把 domain 中的松散 dict 漂移到 wire。

拆分理由（CODE_REVIEW_REPORT #44）：原 `core/character/service.py` 1,384 行的
职责密度让「哪条链负责什么」必须通读全文才能回答——本次审查定位的三项存储缺陷
（S2-B2 `character_revision_repo`、S2-B7 `character_version_repo`、S3-21 版本键
不一致）**全部落在同一条写入链上**。按写入链拆分后：

| 模块 | 负责的链 |
|---|---|
| `errors.py` | `CharacterError` 层级（跨模块稳定契约，路由按异常类型映射错误码） |
| `helpers.py` | 模块级**纯函数**（游标/校验和/归一化/wire 构造）+ 三条链共享常量 |
| `base.py` | 共享管道：`__init__` / 连接 / 取定义 / 草稿二层校验 / 草稿→Revision |
| `queries.py` | 读链：get / list / list_revisions / resolve_runtime_source / preview |
| `definitions.py` | 定义写链：create / update / clone / archive / rollback |
| `instances.py` | 实例/状态写链：reset_evolution / reconcile / 传播 / pending / busy |

本模块是**装配点**：按 C3 线性化把三个链混入组合成 `CharacterService`
（`CharacterDefinitionWriteChain` 显式继承读链与实例写链），并把拆分前
`core.character.service` 命名空间里的名字**逐名重导出**——包括私有名
（`_summary` / `_checksum` / `_clear_evolution` / …，`scripts/dev/seed_stage_h_pinned.py`
等按名显式导入）与历史上顺带泄漏的 stdlib/typing/契约名。`from core.character.service
import X` 的每一个 X 都不变。

刻意**不**定义 `__all__`：拆分前也没有，因此 `from core.character.service import
_summary`（显式导入）与 `import *` 的可见性语义都与拆分前一致。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import (
    CharacterDraft,
    ConversationExample,
    Presentation,
    PromptProfile,
    RuntimeDefaults,
)
from core.character.revision import CharacterRevision
from core.character.service.base import CharacterServiceBase
from core.character.service.definitions import (
    CharacterDefinitionWriteChain,
    _append_revision_or_conflict,
)
from core.character.service.errors import (
    CharacterArchivedError,
    CharacterBusyError,
    CharacterError,
    CharacterImmutableError,
    CharacterInstanceNotFoundError,
    CharacterNotFoundError,
    CharacterPreviewUnavailableError,
    CharacterQueryError,
    CharacterStateConflictError,
    CharacterStorageUnavailableError,
    CharacterTooLargeError,
    CharacterValidationError,
    RevisionConflictError,
)
from core.character.service.helpers import (
    BUILTIN_FALLBACK_CHARACTER_ID,
    INSTANCE_RECONCILE_RETRIES,
    MAX_CHARACTER_LIST_SCAN,
    RESET_FIELDS,
    _checksum,
    _decode_cursor,
    _detail,
    _encode_cursor,
    _limit,
    _mapping,
    _normalise_revision,
    _parse_state_policy,
    _revision_wire,
    _semantic_issues,
    _state_impact,
    _summary,
)
from core.character.service.instances import (
    CharacterInstanceStateWriteChain,
    _clear_evolution,
    _clear_runtime_components,
)
from core.character.service.queries import CharacterQueryChain
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.schema.utils import now_iso
from frontend_contract.rest import (
    CharacterArchiveResult,
    CharacterCreatedBy,
    CharacterResetField,
    CharacterSource,
    CharacterStatus,
    CharacterValidationSeverity,
    CharacterDetail,
    CharacterListResponse,
    CharacterMutationResult,
    CharacterPreviewResult,
    CharacterRevisionContent,
    CharacterRevisionListResponse,
    CharacterRevisionSummary,
    CharacterStateImpact,
    CharacterStatePolicy,
    CharacterSummary,
    CharacterValidationIssue,
    CharacterValidationResult,
)

# TYPE_CHECKING 边与拆分前一致（`check-architecture` 的 R5 统计口径不变）；
# 这些类型现在由各子模块自己按需引入，此处保留是为「命名空间逐名对齐」。
if TYPE_CHECKING:
    from core.character.instance import CharacterInstance
    from core.character.preview import CharacterPreviewPort
    from core.character.registry import CharacterRuntimeRegistry
    from core.character.runtime import CharacterRuntime
    from core.character.runtime_profile import RuntimeCharacterProfile
    from core.profile.user_identity import UserIdentity
    from core.schema.character import CharacterRuntimeState
    from core.storage.character_instance_repo import CharacterInstanceRepository

# 日志名与拆分前一致（`__name__` 在本包内即 "core.character.service"）：拆分不改日志分流。
logger = logging.getLogger(__name__)


class CharacterService(
    CharacterDefinitionWriteChain,
    CharacterQueryChain,
    CharacterInstanceStateWriteChain,
    CharacterServiceBase,
):
    """角色 CRUD、分页、版本 CAS 与安全 preview/reset 编排。"""
