"""错误码单源（F5）— 协议文档 §9.1 错误码目录的权威列表。

契约 `code` 字段为自由 str（wire 不枚举），但公开错误码必须可追溯。本模块是
**唯一权威列表**；`scripts/check_error_codes.py` 双向校验：

1. `docs/electron-backend-protocol-v2.md` §9.1 表 ↔ 本文——增删错误码必须同时改两处；
2. **发射端**（`core/` + `Module/`）实际写入的 `code` 字面量 ⊆ 本文——防止路由发射了
   未登记的码而文档/列表看不见（2026-09-11 发现 `character_routes.py` 曾发射 14 个
   未登记码，旧门禁只比对文档与列表、对路由无约束）。

两个**命名空间**不要混淆：
- `KNOWN_ERROR_CODES` — 顶层错误信封的 `code`（HTTP 错误响应 / WS AckError / generation.failed）。
- `KNOWN_ISSUE_CODES` — `details.issues[].code` 内的**校验问题码**（`ValidationIssue` 产出），
  不是顶层错误码，不参与 §9.1 目录。

零依赖（仅 stdlib），符合 frontend_contract 契约层约束。
"""

KNOWN_ERROR_CODES: frozenset[str] = frozenset({
    "AUTH_REQUIRED",
    "AUTH_INVALID",
    "RATE_LIMITED",
    "CONNECTION_LIMITED",
    "REQUEST_TOO_LARGE",
    "INVALID_REQUEST",
    "INTERNAL",
    "GENERATION_PROVIDER_UNAVAILABLE",
    "GENERATION_TIMEOUT",
    "SESSION_NOT_FOUND",
    "GENERATION_NOT_FOUND",
    "RESOURCE_NOT_FOUND",
    "INVALID_TIMEZONE",
    "CONTRACT_VALIDATION",
    "SESSION_PROTECTED",
    "AGENT_BUSY",
    "GENERATION_CANCELLED",
    "DESKTOP_UNAVAILABLE",
    "CALL_NOT_FOUND",
    "CALL_EXPIRED",
    "CALL_IDEMPOTENCY_CONFLICT",
    "SSRF_BLOCKED",
    "PROFILE_IN_USE",
    "PROFILE_INVALID_RUNTIME",
    "PROFILE_RUNTIME_NOT_BACKEND",
    "VOICE_SESSION_CONFLICT",
    "VOICE_SESSION_NOT_FOUND",
    "VOICE_RECORDING_NOT_FOUND",
    "VOICE_RECORDING_TOO_LONG",
    "DICTATION_NOT_FOUND",
    "STT_UNAVAILABLE",
    "STT_PROFILE_NOT_FOUND",
    "PLAYBACK_NOT_FOUND",
    "ATTACHMENT_NOT_FOUND",
    "SIGNATURE_EXPIRED",
    "INVALID_SIGNATURE",
    "UNSUPPORTED_MEDIA_TYPE",
    "TOO_MANY_FILES",
    "ATTACHMENT_SIGNATURE_DISABLED",
    "STORAGE_UNAVAILABLE",
    "QUEUE_FULL",
    # ── 多角色/身份（Stage A–H，2026-09-11 补登记）──────────────────────────────
    # 这些码自 `character_routes.py` / `core/cognitive/loop.py` 发射，但此前从未
    # 登记，致使 §9.1 目录（41）与实际可达码集（54）不一致。
    "CHARACTER_NOT_FOUND",
    "CHARACTER_INSTANCE_NOT_FOUND",
    "CHARACTER_IMMUTABLE",
    "CHARACTER_ARCHIVED",
    "CHARACTER_TOO_LARGE",
    "CHARACTER_VALIDATION",
    "CHARACTER_REVISION_CONFLICT",
    "CHARACTER_STATE_CONFLICT",
    "CHARACTER_BUSY",
    "CHARACTER_PREVIEW_FAILED",
    "CHARACTER_STORAGE_UNAVAILABLE",
    "USER_PROFILE_NOT_FOUND",
    "USER_PROFILE_REVISION_CONFLICT",
})

KNOWN_ISSUE_CODES: frozenset[str] = frozenset({
    # details.issues[].code —— ValidationIssue（core/character/draft.py）产出的
    # 校验问题码，与顶层错误码是不同命名空间，不进 §9.1 目录。
    "CHARACTER_RESERVED_CONTENT",
    "CHARACTER_SEMANTIC_WARNING",
})
