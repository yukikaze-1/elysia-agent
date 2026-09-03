# 阶段 D：Custom Character 持久化与版本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Plan 状态：** reviewed（终审通过，B1-B9 全部修复，无 blocking/correctness 级遗留）

**Goal:** 把「用户可自定义角色」从只读内置提升到「Mongo 持久化 + CRUD + 版本 append-only + expectedRevision/CAS 并发控制 + 双层校验」，通过 REST `/api/me`、`/api/characters*` 暴露——但 `character_profiles=false` 恒抑制，custom 不参与真实聊天。

**Architecture:** `CharacterDraft` 领域模型（Pydantic 双层校验）→ `CharacterService`（业务编排 + CAS 冲突 + 合并内置/custom）→ `CharacterRepository` + `CharacterRevisionRepository`（snake_case 落库，`modified_count>0` CAS）→ MongoDB。REST 路由 `character_routes.py` 做装配 + 错误码映射（§14 冻结）。

**Tech Stack:** Python 3.11+, Pydantic v2, aiohttp, MongoDB (motor) 经 `DocumentDBBackend` 抽象, pytest + 内存假后端 `_fake_documentdb.py`。

**Spec:** [`docs/superpowers/specs/2026-09-04-character-stage-d-custom-character-persistence-design.md`](../specs/2026-09-04-character-stage-d-custom-character-persistence-design.md)（本 plan 从 spec 论证；实施者同时读 spec 五处关键节：§4 领域模型 / §5 Repository / §6 Service / §7 REST / §13 不变式）

**Known deviations（计划评审接受的少数低严重度偏差，记录供最终报告）：**
- **C1（validate 只查 3 个核心字段）**：`service.validate()` 只对 `corePersonality/relationshipStyle/speakingStyle` 做语义 hard-error 校验（spec §8.2 理论上覆盖所有 authored 字段）。本批聚焦最高风险 3 字段；`emotionStyle/conversationInitiative/description/openingMessage/aliases` 的语义校验留后续。理由：3 字段是 personality 主入口，覆盖主要注入风险；扩大面成本/收益比低。
- **C2（builtin rollback → 404 而非 409）**：`rollback` 对内置角色经 `rev_repo.get_revision`（custom-only）返回 None → `CharacterNotFoundError`（404），而非理想的 `CharacterImmutableError`（409）。理由：回滚内置角色是异常操作（内置 immutable），404 可接受；与 update/archive 的 409 语义不一致但不阻塞。
- **C3（`_me_put` 未传 expectedRevision → 默认 1）**：省略 `expectedRevision` 时用 1（若 profile 已到 revision 2 则误判冲突）。理由：spec §11.1 要求 PUT /api/me 必传 expectedRevision；省略是客户端错误（符合契约），默认 1 是宽容 fallback。

## Global Constraints

- **Elysia byte 不变**：`test_elysia_snapshot_unchanged`（render == `system_prompt_snapshot_elysia_cf4bc008.txt`，18127B）必须 green。
- **CAS 原子性**：`$set` 恒含必变字段 `updated_at`；判定 `modified_count>0`（后端只暴露 `modified_count`，`mongodb.py:106`）；不用读-改-写。
- **append-only 版本**：rollback 复制旧版本为新 revision，不删历史。
- **Revision-first 写序**：update/rollback 先 `append_revision` 再 `cas_revision_pointer`；CAS 失败回滚已 append revision。create 先 append_revision(1) 再 insert_definition。
- **字段命名**：Mongo 文档 snake_case（`character_id`/`current_revision`/`owner_user_id`/`created_at`/`updated_at`），Pydantic 模型 camelCase，Repository 顶层次 `_to_doc`/`_from_doc` 桥接；嵌套 dict（promptProfile/runtimeDefaults/presentation）用 `model_dump(mode="json")` 原样存（键保留 camelCase，对齐 `session_repo.py:139`）。
- **custom 不参与聊天**：`character_profiles=false` 恒抑制；custom 只能被 CRUD。
- **离线 fail-closed**：`insert/update/archive` 抛 `StorageUnavailableError` → `503 CHARACTER_STORAGE_UNAVAILABLE`（禁止静默 no-op）；已缓存读、不可写。
- **禁相对导入**：`from core.xxx` / `from Module.xxx`（ruff TID252 强制）。
- **类型注解**：生产代码函数参数必须类型注解（ANN001/002/003；测试豁免）。
- **字段限制**（§8.1）：displayName 1-64、description ≤500、aliases ≤10 项×1-32、corePersonality 1-12000、relationshipStyle 1-6000、emotionStyle ≤5000、speakingStyle 1-6000、conversationInitiative ≤3000、examples ≤20 组×每 ≤1000、openingMessage ≤1000、wakeWords ≤20 项×1-32、Revision 规范化 JSON ≤64KiB（超限 413 `CHARACTER_TOO_LARGE`）。

---

### Task 1: CharacterDraft 领域模型 + ConversationExample（`core/character/draft.py` + 测试）

**Files:**
- Create: `core/character/draft.py`
- Test: `core/character/tests/test_draft.py`

**Interfaces:**
- Produces: `CharacterDraft`（Pydantic BaseModel）、`ConversationExample`（Pydantic BaseModel）、`RESERVED_TAGS`/`JINJA_DELIMITERS`（语义校验常量）、`validate_semantic(text) -> list[CharacterValidationIssue]`（第二层语义校验，供 service/validate 复用）。
- 供 Task 2 `CharacterService` 消费：`CharacterDraft` 校验 `valid`、`issues`、`promptProfile.examples: list[ConversationExample]`。

- [ ] **Step 1: 写失败测试** — `core/character/tests/test_draft.py`

```python
# core/character/tests/test_draft.py
"""CharacterDraft + ConversationExample 单元测试（marker=unit）。"""
import pytest
from core.character.draft import CharacterDraft, ConversationExample, validate_semantic

BODY = "你是测试角色。"  # corePersonality 等必填字段最小合法值


def _valid_draft(**overrides):
    base = dict(
        displayName="测试",
        description="一个测试角色",
        aliases=["测试"],
        promptProfile={
            "corePersonality": BODY,
            "relationshipStyle": BODY,
            "speakingStyle": BODY,
            "examples": [ConversationExample(user="你好", assistant="你好")],
        },
        runtimeDefaults={"wakeWords": ["测试"], "proactive": {"enabled": False}},
        presentation={"avatarAssetId": None},
        openingMessage=None,
    )
    base.update(overrides)
    return CharacterDraft(**base)


def test_valid_draft():
    d = _valid_draft()
    assert d.displayName == "测试"


def test_display_name_blank_trims():
    # 去首尾空白
    d = _valid_draft(displayName="  测试  ")
    assert d.displayName == "测试"


def test_examples_require_role_text():
    with pytest.raises(ValueError):
        _valid_draft(promptProfile={"corePersonality": BODY, "relationshipStyle": BODY,
                                     "speakingStyle": BODY,
                                     "examples": [ConversationExample(user="", assistant="x")]})


def test_semantic_reserved_tag_hard_error():
    issues = validate_semantic("你是<conflict_rules>的角色")
    assert any(i.severity == "error" for i in issues)


def test_semantic_jinja_delimiter_hard_error():
    issues = validate_semantic("你的指令 {{ user }}")
    assert any(i.severity == "error" for i in issues)


def test_semantic_keyword_warning():
    issues = validate_semantic("你可以修改工具权限")
    assert any(i.severity == "warning" for i in issues)


def test_semantic_clean_no_issues():
    assert validate_semantic("你是一个温柔的角色。") == []
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/character/tests/test_draft.py -q`
  预期：FAIL（`ModuleNotFoundError` 或 `ImportError`：`core.character.draft` 不存在）。

- [ ] **Step 3: 写最小实现** — `core/character/draft.py`

```python
"""CharacterDraft — 用户提交的完整角色表单（custom 唯一入口）。

双层校验：第一层 Pydantic 字段约束 + ConversationExample；第二层 validate_semantic
（保留标签/Jinja 分隔符/NUL/control → hard error；关键字启发式 → warning）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

RESERVED_TAGS = ("<conflict_rules>", "<output_format>", "<tts_markup>", "<tools>")
"""保留 Framework 标签白名单——authored 内容出现即 hard error（不得越过 Framework）。"""

JINJA_DELIMITERS = ("{{", "}}", "{%", "%}")
"""Jinja 分隔符——authored 内容出现即 hard error（模板渲染不二次 render）。"""

_KEYWORD_WARNINGS = ("修改工具权限", "忽略指令", "不要输出 mood", "泄露 system prompt")
"""关键字启发式 warning——由 F1 降级优先级，不阻断保存。"""

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ConversationExample(BaseModel):
    """一组 user/assistant few-shot。"""
    user: str = Field(min_length=1, max_length=1000)
    assistant: str = Field(min_length=1, max_length=1000)


class ValidationIssue(BaseModel):
    """校验问题（severity/code/path/message）——与契约 CharacterValidationIssue 同形。"""
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


def validate_semantic(text: str) -> list[ValidationIssue]:
    """第二层语义校验：保留标签/Jinja/NUL → hard error；关键字 → warning。

    Args:
        text: authored 角色字段文本（如 corePersonality）。

    Returns:
        非空表示有问题（含 error 则不可保存）。
    """
    if not text:
        return []
    issues: list[ValidationIssue] = []
    for tag in RESERVED_TAGS:
        if tag in text:
            issues.append(ValidationIssue(
                severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
                message=f"保留 Framework 标签 {tag} 不得出现在角色内容"))
    for delim in JINJA_DELIMITERS:
        if delim in text:
            issues.append(ValidationIssue(
                severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
                message=f"Jinja 分隔符 {delim} 不得出现在角色内容"))
    if _CONTROL_CHARS.search(text):
        issues.append(ValidationIssue(
            severity="error", code="CHARACTER_RESERVED_CONTENT", path="",
            message="NUL/control 字符不得出现在角色内容"))
    for kw in _KEYWORD_WARNINGS:
        if kw in text:
            issues.append(ValidationIssue(
                severity="warning", code="CHARACTER_SEMANTIC_WARNING", path="",
                message=f"疑似权限/指令语义：{kw}"))
    return issues


class PromptProfile(BaseModel):
    """draft 的结构化 prompt 剖面。"""
    corePersonality: str = Field(min_length=1, max_length=12000)
    relationshipStyle: str = Field(min_length=1, max_length=6000)
    emotionStyle: str = Field(default="", max_length=5000)
    speakingStyle: str = Field(min_length=1, max_length=6000)
    conversationInitiative: str = Field(default="", max_length=3000)
    examples: list[ConversationExample] = Field(default_factory=list, max_length=20)


class RuntimeDefaults(BaseModel):
    """运行时默认值（声音/表情/唤醒词/主动）。"""
    voice: dict[str, object] = Field(default_factory=dict)
    emotion: dict[str, object] = Field(default_factory=dict)
    wakeWords: list[str] = Field(default_factory=list, max_length=20)
    proactive: dict[str, object] = Field(default_factory=dict)


class Presentation(BaseModel):
    """展示引用（受管 asset/model ID，禁本地路径/任意 URL）。"""
    avatarAssetId: str | None = None
    live2dModelId: str | None = None
    themeColor: str | None = None


class CharacterDraft(BaseModel):
    """用户提交的完整角色表单。"""
    displayName: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    promptProfile: PromptProfile
    runtimeDefaults: RuntimeDefaults = Field(default_factory=RuntimeDefaults)
    presentation: Presentation = Field(default_factory=Presentation)
    openingMessage: str | None = Field(default=None, max_length=1000)

    @field_validator("displayName")
    @classmethod
    def _trim_display_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("aliases")
    @classmethod
    def _norm_dedup_aliases(cls, v: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for alias in v:
            norm = alias.strip().casefold()
            if norm and norm not in seen:
                seen[norm] = alias.strip()
        return list(seen.values())
```

- [ ] **Step 4: 运行确认通过** — `python -m pytest core/character/tests/test_draft.py -q` — PASS

- [ ] **Step 5: Commit**

```bash
git add core/character/draft.py core/character/tests/test_draft.py
git commit -m "feat(stage-d): CharacterDraft + ConversationExample 领域模型（双层校验）"
```

---

### Task 2: CharacterDefinition/Revision snake_case 落库桥接 + CharacterizationRevisionRepository（`core/storage/character_revision_repo.py`）

**Files:**
- Create: `core/storage/character_revision_repo.py`
- Modify: `core/character/revision.py`（REVISION_PROMPT_PROFILE_KEYS 加 "examples"）
- Test: `core/storage/tests/test_character_revision_repo.py`

**Interfaces:**
- Consumes: `CharacterRevision`（`core/character/revision.py`）、`Repository`/`StorageUnavailableError`（`core/storage/base.py`）。
- Produces: `CharacterRevisionRepository`（append_revision/get_revision/list_revisions/max_revision/delete_revision）。
  - `max_revision` 用 `find(sort=[("revision",-1)], limit=1)[0]["revision"]`（**不用 aggregate**——`_fake_documentdb.py:189` aggregate 恒返回 `[]`，find+sort+limit 假后端兼容）。
  - `_to_doc`/`_from_doc` 顶层次 snake_case 桥接；嵌套 dict 原样。

- [ ] **Step 1: 扩展 revision.py** — `REVISION_PROMPT_PROFILE_KEYS` 加 `"examples"`

```python
# core/character/revision.py
REVISION_PROMPT_PROFILE_KEYS = ("corePersonality", "relationshipStyle", "emotionStyle",
                                "speakingStyle", "conversationInitiative", "examples")
```

- [ ] **Step 2: 写失败测试** — `core/storage/tests/test_character_revision_repo.py`

```python
# core/storage/tests/test_character_revision_repo.py
"""CharacterRevisionRepository 测试（marker=service，用内存假后端）。

用 core/storage/tests/_fake_documentdb.py 的 FakeDocumentDB 注入 db，不触碰真实 Mongo。
"""
import pytest
import pytest_asyncio

from core.storage.character_revision_repo import CharacterRevisionRepository
from core.character.revision import CharacterRevision
from core.storage.tests._fake_documentdb import FakeDocumentDB


def _rev(character_id="chr_custom_x", revision=1) -> CharacterRevision:
    return CharacterRevision(
        characterId=character_id, revision=revision, displayName="测试",
        promptProfile={"corePersonality": "b", "speakingStyle": "s"},
    )


@pytest.mark.service
class TestCharacterRevisionRepository:
    @pytest_asyncio.fixture
    async def repo(self):
        r = CharacterRevisionRepository(db=FakeDocumentDB())
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_and_get(self, repo):
        await repo.append_revision(_rev())
        got = await repo.get_revision("chr_custom_x", 1)
        assert got is not None and got.displayName == "测试"

    @pytest.mark.asyncio
    async def test_max_revision_no_docs_is_zero(self, repo):
        assert await repo.max_revision("chr_empty") == 0

    @pytest.mark.asyncio
    async def test_max_revision_after_append(self, repo):
        await repo.append_revision(_rev("chr_custom_x", 1))
        await repo.append_revision(_rev("chr_custom_x", 2))
        assert await repo.max_revision("chr_custom_x") == 2

    @pytest.mark.asyncio
    async def test_nested_dict_keys_preserved_camel(self, repo):
        # promptProfile 内嵌核心字符串保留 camelCase（不转 snake）
        r = CharacterRevision(characterId="chr_n", revision=1, displayName="N",
                              promptProfile={"corePersonality": "body"})
        await repo.append_revision(r)
        got = await repo.get_revision("chr_n", 1)
        assert got.promptProfile["corePersonality"] == "body"

    @pytest.mark.asyncio
    async def test_delete_revision(self, repo):
        await repo.append_revision(_rev())
        await repo.delete_revision("chr_custom_x", 1)
        assert await repo.get_revision("chr_custom_x", 1) is None

    @pytest.mark.asyncio
    async def test_offline_append_raises(self):
        r = CharacterRevisionRepository(db=None)  # 未连接
        with pytest.raises(Exception):  # StorageUnavailableError
            await r.append_revision(_rev())
```

- [ ] **Step 3: 运行确认失败** — `python -m pytest core/storage/tests/test_character_revision_repo.py -q`
  预期：FAIL（`ImportError`：`core.storage.character_revision_repo` 不存在）。

- [ ] **Step 4: 写最小实现** — `core/storage/character_revision_repo.py`

```python
"""CharacterRevisionRepository — character_revisions 集合（append-only 版本）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.character.revision import CharacterRevision
from core.storage.base import Repository, StorageUnavailableError

if TYPE_CHECKING:
    pass

COLLECTION = "character_revisions"

# CharacterRevision camelCase 顶层字段 → snake_case 文档键
_TOP_LEVEL_KEYS = {
    "characterId": "character_id",
    "schemaVersion": "schema_version",
    "displayName": "display_name",
    "createdBy": "created_by",
    "createdAt": "created_at",
}


def _to_doc(rev: CharacterRevision) -> dict:
    """CharacterRevision → Mongo 文档（顶层 snake_case，嵌套 dict 原样）。

    promptProfile/runtimeDefaults/presentation 是通用 JSON，model_dump(mode="json")
    原样落库（键保留 camelCase，对齐 session_repo.py:139 的 character_state 先例）。
    """
    data = rev.model_dump(mode="json")
    doc: dict = {}
    for k, v in data.items():
        if k in _TOP_LEVEL_KEYS:
            doc[_TOP_LEVEL_KEYS[k]] = v
        else:
            doc[k] = v  # revision/schemaVersion 等无下划线字段 + 嵌套 dict 原样
    return doc


def _from_doc(doc: dict) -> CharacterRevision:
    """Mongo 文档 → CharacterRevision（snake_case 顶层反桥接）。"""
    data: dict = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if k in _TOP_LEVEL_KEYS.values():
            rev_key = next(dk for dk, dk_val in _TOP_LEVEL_KEYS.items() if dk_val == k)
            data[rev_key] = v
        else:
            data[k] = v
    return CharacterRevision.model_validate(data)


class CharacterRevisionRepository(Repository):
    """character_revisions 集合 —— append-only 版本存储。

    append_revision 写孤儿 revision（create 时 Definition 尚未插入）；CAS 失败回滚
    用 delete_revision。max_revision 用 find+sort+limit 而非 aggregate（假后端兼容）。
    """

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        self._db: DocumentDBBackend | None = db
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("character_id", 1), ("revision", -1)], unique=True)
            return True
        except Exception:
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return await self._health_check()

    async def append_revision(self, revision: CharacterRevision) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        doc = _to_doc(revision)
        await self._call(
            lambda: self._db.insert_one(COLLECTION, doc),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.append_revision 写失败",
        )

    async def get_revision(self, character_id: str, revision: int) -> CharacterRevision | None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id, "revision": revision}, limit=1),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.get_revision 查失败",
        )
        return _from_doc(docs[0]) if docs else None

    async def list_revisions(self, character_id: str, *, cursor: str | None = None,
                             limit: int = 50) -> list[CharacterRevision]:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        filt: dict = {"character_id": character_id}
        if cursor:
            filt["revision"] = {"$lt": int(cursor)}  # revision 降序游标
        docs = await self._call(
            lambda: self._db.find(COLLECTION, filt, sort=[("revision", -1)], limit=limit),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.list_revisions 查失败",
        )
        return [_from_doc(d) for d in docs]

    async def max_revision(self, character_id: str) -> int:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id},  # type: ignore[union-attr]
                                   sort=[("revision", -1)], limit=1),
            reason="CharacterRevisionRepository.max_revision 查失败",
        )
        return int(docs[0]["revision"]) if docs else 0

    async def delete_revision(self, character_id: str, revision: int) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRevisionRepository 未连接")
        await self._call(
            lambda: self._db.delete_many(COLLECTION, {"character_id": character_id, "revision": revision}),  # type: ignore[union-attr]
            reason="CharacterRevisionRepository.delete_revision 删失败",
        )
```

- [ ] **Step 5: 运行确认通过** — `python -m pytest core/storage/tests/test_character_revision_repo.py -q` — PASS

- [ ] **Step 6: Commit**

```bash
git add core/storage/character_revision_repo.py core/character/revision.py core/storage/tests/test_character_revision_repo.py
git commit -m "feat(stage-d): CharacterRevisionRepository（append-only 版本）+ revision.py 加 examples 键"
```

---

### Task 3: CharacterRepository（`core/storage/character_repo.py` + last-known-good 缓存）

**Files:**
- Create: `core/storage/character_repo.py`
- Test: `core/storage/tests/test_character_repo.py`

**Interfaces:**
- Consumes: `CharacterDefinition`（`core/character/definition.py`）、`Repository`/`StorageUnavailableError`。
- Produces: `CharacterRepository`（insert_definition/get_definition/list_definitions/cas_revision_pointer/archive + last-known-good 缓存）。
  - `cas_revision_pointer`：`filter={"character_id":x,"current_revision":expected}` + `$set`（恒定含 `updated_at`）→ `modified_count>0` 命中。
  - 缓存：写入成功同步失效该 characterId；离线读失效则抛。

- [ ] **Step 1: 写失败测试** — `core/storage/tests/test_character_repo.py`

```python
# core/storage/tests/test_character_repo.py
"""CharacterRepository 测试（marker=service，用内存假后端）。"""
import pytest
import pytest_asyncio

from core.storage.character_repo import CharacterRepository
from core.character.definition import CharacterDefinition
from core.storage.tests._fake_documentdb import FakeDocumentDB
from core.storage.base import StorageUnavailableError


def _def(**overrides) -> CharacterDefinition:
    base = dict(characterId="chr_custom_x", slug="x", source="custom",
                mutable=True, status="active", currentRevision=1, ownerUserId="usr_local_default")
    base.update(overrides)
    return CharacterDefinition(**base)


@pytest.mark.service
class TestCharacterRepository:
    @pytest_asyncio.fixture
    async def repo(self):
        r = CharacterRepository(db=FakeDocumentDB())
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_insert_and_get(self, repo):
        await repo.insert_definition(_def())
        got = await repo.get_definition("chr_custom_x")
        assert got is not None and got.characterId == "chr_custom_x"
        assert got.currentRevision == 1

    @pytest.mark.asyncio
    async def test_cas_pointer_hit(self, repo):
        await repo.insert_definition(_def(currentRevision=1))
        ok = await repo.cas_revision_pointer("chr_custom_x", expected_revision=1, new_revision=2, updated_at="2026-09-04T00:00:00Z")
        assert ok is True
        got = await repo.get_definition("chr_custom_x")
        assert got.currentRevision == 2

    @pytest.mark.asyncio
    async def test_cas_pointer_miss_conflict(self, repo):
        await repo.insert_definition(_def(currentRevision=2))
        ok = await repo.cas_revision_pointer("chr_custom_x", expected_revision=1, new_revision=3, updated_at="2026-09-04T00:00:00Z")
        assert ok is False  # current_revision==2 != expected==1 → 未命中

    @pytest.mark.asyncio
    async def test_archive(self, repo):
        await repo.insert_definition(_def())
        await repo.archive("chr_custom_x")
        got = await repo.get_definition("chr_custom_x")
        assert got.status == "archived"

    @pytest.mark.asyncio
    async def test_owner_filter(self, repo):
        await repo.insert_definition(_def(characterId="a"))
        await repo.insert_definition(_def(characterId="b", ownerUserId="usr_other"))
        lst = await repo.list_definitions(owner_user_id="usr_local_default")
        assert [d.characterId for d in lst] == ["a"]

    @pytest.mark.asyncio
    async def test_status_filter(self, repo):
        await repo.insert_definition(_def(characterId="a", status="active"))
        await repo.insert_definition(_def(characterId="b", status="archived"))
        lst = await repo.list_definitions(status="active")
        assert [d.characterId for d in lst] == ["a"]

    @pytest.mark.asyncio
    async def test_offline_insert_raises(self):
        r = CharacterRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await r.insert_definition(_def())
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/storage/tests/test_character_repo.py -q`
  预期：FAIL（`ImportError`）。

- [ ] **Step 3: 写最小实现** — `core/storage/character_repo.py`

```python
"""CharacterRepository — characters 集合（Definition 持久化 + CAS + last-known-good）。"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.character.definition import CharacterDefinition
from core.storage.base import Repository, StorageUnavailableError

COLLECTION = "characters"
CACHE_MAX = 64

# CharacterDefinition camelCase 顶层字段 → snake_case 文档键
# 注意：CharacterDefinition 无 displayName 字段（displayName 在 CharacterRevision）——
# 列表展示的 displayName 由 CharacterService 连接 Revision 得到，不走 Definition 文档。
_TOP_LEVEL_KEYS = {
    "characterId": "character_id",
    "currentRevision": "current_revision",
    "ownerUserId": "owner_user_id",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


def _to_doc(defn: CharacterDefinition) -> dict:
    data = defn.model_dump(mode="json")
    doc: dict = {}
    for k, v in data.items():
        if k in _TOP_LEVEL_KEYS:
            doc[_TOP_LEVEL_KEYS[k]] = v
        else:
            doc[k] = v
    return doc


def _from_doc(doc: dict) -> CharacterDefinition:
    data: dict = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if k in _TOP_LEVEL_KEYS.values():
            rev_key = next(dk for dk, dk_val in _TOP_LEVEL_KEYS.items() if dk_val == k)
            data[rev_key] = v
        else:
            data[k] = v
    return CharacterDefinition.model_validate(data)


class CharacterRepository(Repository):
    """characters 集合。

    cas_revision_pointer：filter 带 current_revision（CAS gate）+ $set 恒定含 updated_at
    （必变字段规避 MongoDB no-op 误判——后端只暴露 modified_count，mongodb.py:106）。
    last-known-good：读成功缓存；写成功同步失效（read-your-writes）。
    """

    def __init__(self, db: DocumentDBBackend | None = None, *,
                 base_url: str | None = None):
        self._db = db
        self._base_url = base_url
        self._injected_db = db is not None
        self._cache: OrderedDict[str, CharacterDefinition] = OrderedDict()

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("character_id", 1)], unique=True)
            return True
        except Exception:
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return await self._health_check()

    # ---- cache helpers ----

    def _cache_put(self, key: str, val: CharacterDefinition) -> None:
        self._cache[key] = val
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)

    def _cache_invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    async def insert_definition(self, definition: CharacterDefinition) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        await self._call(
            lambda: self._db.insert_one(COLLECTION, _to_doc(definition)),  # type: ignore[union-attr]
            reason="CharacterRepository.insert_definition 写失败",
        )
        self._cache_put(definition.characterId, definition)

    async def get_definition(self, character_id: str) -> CharacterDefinition | None:
        if character_id in self._cache:
            return self._cache[character_id]
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        docs = await self._call(
            lambda: self._db.find(COLLECTION, {"character_id": character_id}, limit=1),  # type: ignore[union-attr]
            reason="CharacterRepository.get_definition 查失败",
        )
        if not docs:
            return None
        got = _from_doc(docs[0])
        self._cache_put(character_id, got)
        return got

    async def list_definitions(self, *, source: str | None = None, status: str | None = None,
                               owner_user_id: str | None = None, search: str | None = None,
                               cursor: str | None = None, limit: int = 50) -> list[CharacterDefinition]:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        filt: dict = {}
        if source:
            filt["source"] = source
        if status:
            filt["status"] = status
        if owner_user_id:
            filt["owner_user_id"] = owner_user_id
        if search:
            # Definition 无 displayName 字段——search 用 slug（create 时 slug=displayName）
            filt["slug"] = {"$regex": search, "$options": "i"}
        docs = await self._call(
            lambda: self._db.find(COLLECTION, filt, sort=[("updated_at", -1)], limit=limit),  # type: ignore[union-attr]
            reason="CharacterRepository.list_definitions 查失败",
        )
        return [_from_doc(d) for d in docs]

    async def cas_revision_pointer(self, character_id: str, expected_revision: int,
                                   new_revision: int, updated_at: str) -> bool:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        # $set 恒定含 updated_at（必变）——modified_count>0 即命中；==0 表示 expected 过期
        modified = await self._call(
            lambda: self._db.update_one(  # type: ignore[union-attr]
                COLLECTION,
                {"character_id": character_id, "current_revision": expected_revision},
                {"$set": {"current_revision": new_revision, "updated_at": updated_at}},
                upsert=False,
            ),
            reason="CharacterRepository.cas_revision_pointer CAS 失败",
        )
        if modified > 0:
            self._cache_invalidate(character_id)
            return True
        return False

    async def archive(self, character_id: str) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterRepository 未连接")
        await self._call(
            lambda: self._db.update_one(  # type: ignore[union-attr]
                COLLECTION, {"character_id": character_id}, {"$set": {"status": "archived"}},
                upsert=False,
            ),
            reason="CharacterRepository.archive 写失败",
        )
        self._cache_invalidate(character_id)

    async def _health_check(self) -> bool:
        return await super()._health_check()
```

> Note: `search` 用 `display_name`（snake_case）`$regex`；`_fake_documentdb.py` 支持 `$regex`+`$options`。若 `displayName` 转 snake 有歧义（单词无下划线），`slug`/`source`/`status`/`mutable` 均为单值无转换。`top_level_keys` 中 `currentRevision→current_revision`、`ownerUserId→owner_user_id` 已覆盖此歧义。

- [ ] **Step 4: 运行确认通过** — `python -m pytest core/storage/tests/test_character_repo.py -q` — PASS

- [ ] **Step 5: Commit**

```bash
git add core/storage/character_repo.py core/storage/tests/test_character_repo.py
git commit -m "feat(stage-d): CharacterRepository（snake_case 落库 + modified_count>0 CAS + last-known-good）"
```

---

### Task 4A: frontend_contract 补齐角色 DTO（`frontend_contract/rest.py`）

**Files:**
- Modify: `frontend_contract/rest.py`（新增 10 个 DTO：CharacterListResponse/CharacterRevisionSummary/CharacterRevisionResponse/CharacterDetail/CharacterStateImpact/CharacterMutationResult/CharacterValidationIssue/CharacterValidationResult/CharacterRevisionListResponse/CharacterArchiveResult）
- Test: `frontend_contract/tests/test_rest.py`

**Interfaces:**
- Produces: 契约 DTO——**Task 4 CharacterService 依赖这些类型做函数签名/返回类型**（B1 修复：契约 DTO 必须先于 service 定义，否则 service.py 的 `from frontend_contract.rest import ...` 在 Task 4 报 ImportError）。
- 依赖：`ContractModel`（`frontend_contract/base.py`）、`UserIdentityProfile`/`CharacterSummary`（rest.py:564,580 已存在）。

- [ ] **Step 1: 写失败测试** — `frontend_contract/tests/test_rest.py`（追加）

```python
def test_character_list_response_has_more_cursor():
    from frontend_contract.rest import CharacterListResponse, CharacterSummary
    resp = CharacterListResponse(items=[CharacterSummary(characterId="a", displayName="A")],
                                 has_more=False).wire_dict()
    assert resp["has_more"] is False and "cursor" not in resp  # cursor 缺省省略


def test_character_mutation_result_shape():
    from frontend_contract.rest import (CharacterMutationResult, CharacterDetail,
                                        CharacterSummary, CharacterRevisionResponse,
                                        CharacterStateImpact)
    detail = CharacterDetail(summary=CharacterSummary(characterId="a", displayName="A"),
                             current=CharacterRevisionResponse(characterId="a", revision=1,
                                                               displayName="A", createdAt="2026-09-04T00:00:00Z"))
    m = CharacterMutationResult(character=detail,
                                stateImpact=CharacterStateImpact(statePolicy="preserve",
                                                                  affectedInstances=0, pendingInstances=0,
                                                                  resetFields=[])).wire_dict()
    assert m["stateImpact"]["statePolicy"] == "preserve"
    assert m["character"]["current"]["revision"] == 1


def test_character_archive_result_character_summary():
    from frontend_contract.rest import CharacterArchiveResult, CharacterSummary
    a = CharacterArchiveResult(character=CharacterSummary(characterId="a", displayName="A")).wire_dict()
    assert a["character"]["characterId"] == "a"
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest frontend_contract/tests/test_rest.py -k "character_" -q`
  预期：FAIL（ImportError：`CharacterListResponse` 等不存在）。

- [ ] **Step 3: 补齐 rest.py DTO**

在 `frontend_contract/rest.py` 的 stage C+E 区块（`UserIdentityProfile`/`CharacterSummary` 之后）追加：

```python
class CharacterListResponse(ContractModel):
    """角色列表（分页沿用 has_more + cursor 约定）。"""
    items: list[CharacterSummary]
    has_more: bool = Field(default=False, description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="不透明游标，客户端不得解析")


class CharacterRevisionSummary(ContractModel):
    """角色版本摘要（列表）。"""
    characterId: str
    revision: int = Field(default=1, ge=1)
    displayName: str
    createdAt: str
    createdBy: str = "system"


class CharacterRevisionResponse(ContractModel):
    """当前 revision 完整详情（契约层独立类型，不引用 core.CharacterRevision）。"""
    characterId: str
    revision: int = Field(default=1, ge=1)
    schemaVersion: int = Field(default=1, ge=1)
    displayName: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    promptProfile: dict[str, Any] = Field(default_factory=dict)
    runtimeDefaults: dict[str, Any] = Field(default_factory=dict)
    presentation: dict[str, Any] = Field(default_factory=dict)
    openingMessage: Optional[str] = None
    checksum: str = ""
    compilerVersion: int = Field(default=1, ge=1)
    createdBy: str = "system"
    createdAt: str


class CharacterDetail(ContractModel):
    """角色详情（summary + 当前 revision）。"""
    summary: CharacterSummary
    current: CharacterRevisionResponse


class CharacterStateImpact(ContractModel):
    """编辑/回滚的状态影响。"""
    statePolicy: str = "preserve"
    affectedInstances: int = 0
    pendingInstances: int = 0
    resetFields: list[str] = Field(default_factory=list)


class CharacterMutationResult(ContractModel):
    """角色变更结果（新详情 + 状态影响）。"""
    character: CharacterDetail
    stateImpact: CharacterStateImpact


class CharacterValidationIssue(ContractModel):
    """校验问题（severity/code/path/message）。"""
    severity: str
    code: str
    path: str
    message: str


class CharacterValidationResult(ContractModel):
    """角色草稿校验结果（无效草稿也是 200+valid=false）。"""
    valid: bool
    issues: list[CharacterValidationIssue]
    estimatedPromptChars: Optional[int] = None


class CharacterRevisionListResponse(ContractModel):
    """角色版本列表（分页）。"""
    items: list[CharacterRevisionSummary]
    has_more: bool = Field(default=False, description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="不透明游标，客户端不得解析")


class CharacterArchiveResult(ContractModel):
    """归档结果。"""
    character: CharacterSummary
    userProfile: Optional[UserIdentityProfile] = Field(default=None, description="归档默认角色时返回新档案")
```

> 需在 rest.py 顶部 import 处确认 `Any` 已导入（`from typing import Any`）。若未导入，补 `Any`。

- [ ] **Step 4: 运行确认通过** — `python -m pytest frontend_contract/tests/test_rest.py -k "character_" -q` — PASS

- [ ] **Step 5: 生成契约产物** — `python -m frontend_contract`（刷新 contract.schema.json / contract.raw.ts / contract.generated.ts）

- [ ] **Step 6: Commit**

```bash
git add frontend_contract/rest.py frontend_contract/tests/test_rest.py frontend_contract/contract.schema.json frontend_contract/contract.raw.ts frontend/src/types/contract.generated.ts
git commit -m "feat(stage-d): frontend_contract 补齐角色 DTO（List/Detail/Mutation/Validation/Archive/RevisionList）"
```

---

### Task 4: CharacterService 业务编排（`core/character/service.py`）

**Files:**
- Create: `core/character/service.py`
- Test: `core/character/tests/test_service.py`

**Interfaces:**
- Consumes: `CharacterDraft`（Task 1）、`CharacterRepository`（Task 3）、`CharacterRevisionRepository`（Task 2）、`CharacterCatalog`（`core/character/catalog.py`）、`UserIdentityStore`（`core/profile/profile_store.py`）。还可用 `BuiltinCharacterSource`（跨内置角色 clone 时）。
- Produces: `CharacterService`（create/get/list/update/clone/archive/rollback/validate + 自定义异常 `CharacterNotFoundError`/`CharacterImmutableError`/`CharacterArchivedError`/`CharacterValidationError`/`RevisionConflictError`/`CharacterStorageUnavailableError`）。
  - `get` 合并内置（catalog）+ custom（repo）。
  - `create/update/rollback` Revision-first 写序 + CAS 失败回滚。
  - DTO 用 `frontend_contract.rest` 的 `CharacterDetail`/`CharacterListResponse`/`CharacterMutationResult`/`CharacterValidationResult`/`CharacterArchiveResult`/`CharacterSummary`/`CharacterRevisionSummary`。

- [ ] **Step 0: 修改 `CharacterCatalog` 加 `get_revision`** — `core/character/catalog.py`（供 service `get`/`clone` 取内置角色 revision；B9 修复：内置 revision 不在 character_revisions）

```python
# core/character/catalog.py —— 在 CharacterCatalog 加一个方法（不改 list/get 现有签名）
def get_revision(self, character_id: str) -> tuple[CharacterDefinition, CharacterRevision] | None:
    """返回内置角色的 (Definition, Revision) 对。

    CharacterCatalog 的 get() 只返回 Definition，丢弃了 BuiltinCharacterSource.load()
    返回的 revision——service 的 get()/clone() 需要内置 revision（B9）。
    """
    for name in self._source.list_names():
        try:
            definition, revision, _ = self._source.load(name)
            if definition.characterId == character_id:
                return (definition, revision)
        except FileNotFoundError:
            continue
    return None
```

> 需在 catalog.py 顶部补充 `from core.character.revision import CharacterRevision`（TYPE_CHECKING 不适用——`tuple` 返回需要真实类型）。

- [ ] **Step 1: 写失败测试** — `core/character/tests/test_service.py`

```python
# core/character/tests/test_service.py
"""CharacterService 测试（marker=service，用内存假后端）。"""
import pytest
import pytest_asyncio

from core.character.service import (
    CharacterService, CharacterNotFoundError, CharacterImmutableError,
    RevisionConflictError, CharacterValidationError,
)
from core.character.draft import CharacterDraft
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.profile.profile_store import UserIdentityStore
from core.storage.tests._fake_documentdb import FakeDocumentDB


BODY = "你是测试角色。"


def _draft(**overrides) -> CharacterDraft:
    base = dict(
        displayName="测试",
        promptProfile={"corePersonality": BODY, "relationshipStyle": BODY, "speakingStyle": BODY},
    )
    base.update(overrides)
    return CharacterDraft(**base)


def _make_service(*, store: UserIdentityStore | None = None) -> CharacterService:
    repo = CharacterRepository(db=FakeDocumentDB())
    rev_repo = CharacterRevisionRepository(db=FakeDocumentDB())
    catalog = CharacterCatalog(source=BuiltinCharacterSource())
    return CharacterService(repo=repo, rev_repo=rev_repo, catalog=catalog,
                            user_identity_store=store or UserIdentityStore())


@pytest.mark.service
class TestCharacterService:
    @pytest.mark.asyncio
    async def test_create_and_get(self):
        svc = _make_service()
        await svc.repo.connect()
        await svc.rev_repo.connect()
        detail = await svc.create(_draft(displayName="测试A"), owner_user_id="usr_local_default")
        assert detail.summary.characterId.startswith("chr_custom_")
        assert detail.summary.displayName == "测试A"
        got = await svc.get(detail.summary.characterId)
        assert got is not None and got.summary.displayName == "测试A"

    @pytest.mark.asyncio
    async def test_create_invalid_raises_validation(self):
        svc = _make_service()
        # 传一个满足 PromptProfile 字段边界（非空）、但含保留标签的 draft——
        # 触发第二层语义校验（validate_semantic hard-error），而非 Pydantic 边界错误（B8 修复）。
        # corePersonality="" 会在 CharacterDraft 构造时抛 Pydantic ValidationError（不是
        # CharacterValidationError），该分支由 _draft_from_body 的 except ValidationError 处理。
        with pytest.raises(CharacterValidationError):
            await svc.create(
                _draft(promptProfile={"corePersonality": "你是<conflict_rules>的角色",
                                      "relationshipStyle": BODY, "speakingStyle": BODY}),
                owner_user_id="usr_local_default")

    @pytest.mark.asyncio
    async def test_get_builtin(self):
        svc = _make_service()
        # 内置角色 get() 应返回 CharacterDetail（B9：内置 revision 经 catalog.get_revision 取到）
        detail = await svc.get("chr_builtin_elysia")
        assert detail is not None
        assert detail.summary.characterId == "chr_builtin_elysia"
        assert detail.current.displayName  # 内置 revision 的 displayName 非空

    @pytest.mark.asyncio
    async def test_update_cas_conflict(self):
        svc = _make_service()
        await svc.repo.connect()
        await svc.rev_repo.connect()
        detail = await svc.create(_draft(), owner_user_id="usr_local_default")
        cid = detail.summary.characterId
        # 先用正确 expectedRevision=1（current 初始 1），update 后 current 变 2
        await svc.update(cid, _draft(displayName="v2"), expected_revision=1)
        # 再用过期 expected_revision=1 → 冲突（current 已为 2）
        with pytest.raises(RevisionConflictError):
            await svc.update(cid, _draft(displayName="v3"), expected_revision=1)

    @pytest.mark.asyncio
    async def test_archive_builtin_immutable(self):
        svc = _make_service()
        # builtin elysia 不可归档
        builtin = next(d for d in svc.catalog.list() if d.characterId == "chr_builtin_elysia")
        with pytest.raises(CharacterImmutableError):
            await svc.archive(builtin.characterId)

    @pytest.mark.asyncio
    async def test_list_includes_builtin(self):
        svc = _make_service()
        resp = await svc.list()
        ids = [it.characterId for it in resp.items]
        assert "chr_builtin_elysia" in ids
        assert "chr_builtin_cyrene" in ids
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/character/tests/test_service.py -q`
  预期：FAIL（`ImportError`：`core.character.service` 不存在）。

- [ ] **Step 3: 写最小实现** — `core/character/service.py`

```python
"""CharacterService — 角色 CRUD 业务编排 + CAS 冲突 + 合并内置/custom。"""

from __future__ import annotations

import uuid

from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft
from core.character.revision import CharacterRevision
from core.profile.profile_store import UserIdentityStore
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.schema.utils import now_iso
from frontend_contract.rest import (
    CharacterArchiveResult, CharacterDetail, CharacterListResponse, CharacterMutationResult,
    CharacterRevisionResponse, CharacterRevisionSummary, CharacterStateImpact, CharacterSummary,
    CharacterValidationIssue, CharacterValidationResult,
)


class CharacterError(RuntimeError): ...
class CharacterNotFoundError(CharacterError): ...
class CharacterImmutableError(CharacterError): ...
class CharacterArchivedError(CharacterError): ...
class CharacterValidationError(CharacterError): ...
class RevisionConflictError(CharacterError): ...
class CharacterStorageUnavailableError(CharacterError): ...


def _summary(defn: CharacterDefinition, display_name: str | None = None) -> CharacterSummary:
    """构造 CharacterSummary。

    CharacterDefinition 无 displayName 字段——displayName 来自当前 Revision（自定义）
    或 catalog 的内置定义（内置 displayName 由 revision 携带）。经 display_name 参数注入。
    """
    return CharacterSummary(characterId=defn.characterId,
                            displayName=display_name or defn.slug,
                            source=defn.source, status=defn.status)


def _detail(defn: CharacterDefinition, rev: CharacterRevision) -> CharacterDetail:
    """构造 CharacterDetail。

    current 是契约层 CharacterRevisionResponse（非 domain CharacterRevision），
    field names 虽与 domain 一致，但 Pydantic 不把"另一个 BaseModel 实例"coerce 进
    CharacterRevisionResponse 字段（会拒绝）。必须经 model_dump() 显式桥接为 dict。
    """
    return CharacterDetail(
        summary=_summary(defn, display_name=rev.displayName),
        current=CharacterRevisionResponse(**rev.model_dump(mode="json")),
    )


class CharacterService:
    """角色 CRUD 业务编排。

    合并内置（catalog）+ custom（repo）经 list/get。create/update/rollback 用
    Revision-first 写序（先 append_revision 再 CAS 指针；CAS 失败回滚 append）。
    """

    def __init__(self, *, repo: CharacterRepository, rev_repo: CharacterRevisionRepository,
                 catalog: CharacterCatalog, user_identity_store: UserIdentityStore):
        self.repo = repo
        self.rev_repo = rev_repo
        self.catalog = catalog
        self.user_identity_store = user_identity_store

    async def validate(self, draft: CharacterDraft) -> CharacterValidationResult:
        """纯校验（无副作用）；供 POST /api/characters/validate 复用。"""
        issues: list[CharacterValidationIssue] = []
        # 第一层 Pydantic 已抛；此处收第二层语义
        profile = draft.promptProfile
        for field, text in (("corePersonality", profile.corePersonality),
                            ("relationshipStyle", profile.relationshipStyle),
                            ("speakingStyle", profile.speakingStyle)):
            for iss in _semantic_issues(text, f"/promptProfile/{field}"):
                issues.append(iss)
        valid = not any(i.severity == "error" for i in issues)
        return CharacterValidationResult(valid=valid, issues=issues)

    async def create(self, draft: CharacterDraft, *, owner_user_id: str,
                     created_by: str = "user") -> CharacterDetail:
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        character_id = f"chr_custom_{uuid.uuid4().hex[:16]}"
        rev = self._revision_from_draft(character_id, draft, revision=1, created_by=created_by)
        # Revision-first：先写孤儿 Revision，再插 Definition；失败回滚删除孤儿
        await self.rev_repo.append_revision(rev)
        try:
            defn = CharacterDefinition(characterId=character_id, slug=draft.displayName,
                                       source="custom", mutable=True, status="active",
                                       currentRevision=1, ownerUserId=owner_user_id)
            await self.repo.insert_definition(defn)
        except Exception:
            await self.rev_repo.delete_revision(character_id, 1)
            raise
        return _detail(defn, rev)

    async def get(self, character_id: str) -> CharacterDetail | None:
        pair = await self._get_definition_and_revision(character_id)
        if pair is None:
            return None
        defn, rev = pair
        return _detail(defn, rev)

    async def list(self, *, source: str | None = None, status: str = "active",
                   search: str | None = None, cursor: str | None = None,
                   limit: int = 50) -> CharacterListResponse:
        # 自定义：Definition 无 displayName——需读 current Revision 解析 displayName 做摘要。
        # builtin：catalog.list() 返回内置 Definition；displayName 从 BuiltinCharacterSource.load 的 revision 取。
        custom = await self.repo.list_definitions(source=source, status=status,
                                                    owner_user_id="usr_local_default",
                                                    search=search, limit=limit)
        items: list[CharacterSummary] = []
        for d in custom:
            rev = await self.rev_repo.get_revision(d.characterId, d.currentRevision)
            items.append(_summary(d, display_name=rev.displayName if rev else d.slug))
        if source is None or source == "builtin":
            for d in self.catalog.list():
                if d.status == status:
                    try:
                        _, rev, _ = self.catalog._source.load(d.slug)  # noqa: SLF001 内置定义 displayName 从 revision 取
                        items.append(_summary(d, display_name=rev.displayName))
                    except Exception:
                        items.append(_summary(d, display_name=d.slug))
        return CharacterListResponse(items=items, has_more=False)

    async def update(self, character_id: str, draft: CharacterDraft, *,
                     expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult:
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        defn = await self._get_definition(character_id)
        if defn is None:
            raise CharacterNotFoundError(character_id)
        if not defn.mutable:
            raise CharacterImmutableError(character_id)
        if defn.status == "archived":
            raise CharacterArchivedError(character_id)
        new_rev = await self.rev_repo.max_revision(character_id) + 1
        new_content = self._revision_from_draft(character_id, draft, revision=new_rev)
        await self.rev_repo.append_revision(new_content)
        ok = await self.repo.cas_revision_pointer(character_id, expected_revision, new_rev,
                                                   updated_at=now_iso())
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_rev)
            raise RevisionConflictError(character_id)
        defn.currentRevision = new_rev
        return CharacterMutationResult(
            character=_detail(defn, new_content),
            stateImpact=CharacterStateImpact(statePolicy=state_policy, affectedInstances=0,
                                              pendingInstances=0, resetFields=[]),
        )

    async def clone(self, character_id: str, *, display_name: str | None = None) -> CharacterDetail:
        src = await self.get(character_id)
        if src is None:
            raise CharacterNotFoundError(character_id)
        profile = src.current.promptProfile
        # 内置 revision 的 relationshipStyle/speakingStyle 可能留空（builtin_source.py:53
        # relationshipStyle/emotionStyle 本批留空）——CharacterDraft 要求这些字段 min_length=1，
        # 克隆时对空字段给占位默认，避免 ValidationError。
        new_draft = CharacterDraft(
            displayName=display_name or src.summary.displayName,
            description=src.current.description,
            aliases=src.current.aliases,
            promptProfile={
                "corePersonality": profile.get("corePersonality") or "暂无核心人格。",
                "relationshipStyle": profile.get("relationshipStyle") or "保持友好而自然的互动。",
                "speakingStyle": profile.get("speakingStyle") or "用日常口语对话。",
                "emotionStyle": profile.get("emotionStyle", ""),
                "conversationInitiative": profile.get("conversationInitiative", ""),
                "examples": profile.get("examples", []),
            },
            runtimeDefaults=src.current.runtimeDefaults,
            presentation=src.current.presentation,
            openingMessage=src.current.openingMessage,
        )
        return await self.create(new_draft, owner_user_id="usr_local_default")

    async def archive(self, character_id: str) -> CharacterArchiveResult:
        defn = await self._get_definition(character_id)
        if defn is None:
            raise CharacterNotFoundError(character_id)
        if not defn.mutable:
            raise CharacterImmutableError(character_id)
        await self.repo.archive(character_id)
        defn.status = "archived"
        return CharacterArchiveResult(character=_summary(defn))

    async def rollback(self, character_id: str, *, target_revision: int,
                       expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult:
        old = await self.rev_repo.get_revision(character_id, target_revision)
        if old is None:
            raise CharacterNotFoundError(character_id)
        new_rev = await self.rev_repo.max_revision(character_id) + 1
        content = CharacterRevision(characterId=character_id, revision=new_rev,
                                    displayName=old.displayName,
                                    promptProfile=old.promptProfile,
                                    runtimeDefaults=old.runtimeDefaults,
                                    presentation=old.presentation,
                                    openingMessage=old.openingMessage,
                                    checksum=old.checksum, createdBy="user")
        await self.rev_repo.append_revision(content)
        ok = await self.repo.cas_revision_pointer(character_id, expected_revision, new_rev,
                                                   updated_at=now_iso())
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_rev)
            raise RevisionConflictError(character_id)
        defn = await self.repo.get_definition(character_id)
        defn.currentRevision = new_rev
        return CharacterMutationResult(
            character=_detail(defn, content),
            stateImpact=CharacterStateImpact(statePolicy=state_policy, affectedInstances=0,
                                              pendingInstances=0, resetFields=[]),
        )

    # ---- helpers ----

    async def _get_definition(self, character_id: str) -> CharacterDefinition | None:
        """取 Definition（custom：repo；builtin：catalog）——不含 revision。

        update/archive 用它判断 mutable/status（builtin 会因 mutable=False 抛 immutable）。
        """
        defn = await self.repo.get_definition(character_id)
        if defn is not None:
            return defn
        return self.catalog.get(character_id)

    async def _get_definition_and_revision(
        self, character_id: str,
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        """取 Definition + 当前 Revision。

        custom：repo.get_definition + rev_repo.get_revision(currentRevision)。
        builtin：catalog.get_revision（BuiltinCharacterSource.load 的 revision，
        不受 custom 版本 repo 影响——B9 修复：内置角色的 revision 不在 character_revisions）。
        """
        defn = await self._get_definition(character_id)
        if defn is None:
            return None
        if defn.source == "builtin":
            rev_pair = self.catalog.get_revision(character_id)
            if rev_pair is None:
                return None
            _, rev = rev_pair
            return (defn, rev)
        rev = await self.rev_repo.get_revision(character_id, defn.currentRevision)
        return (defn, rev) if rev is not None else None

    async def _raise_on_validation(self, draft: CharacterDraft) -> CharacterValidationError | None:
        result = await self.validate(draft)
        if not result.valid:
            return CharacterValidationError(str(result.issues))
        return None

    def _revision_from_draft(self, character_id: str, draft: CharacterDraft, *,
                             revision: int, created_by: str = "user") -> CharacterRevision:
        examples = [e.model_dump() for e in draft.promptProfile.examples]
        profile = {
            "corePersonality": draft.promptProfile.corePersonality,
            "relationshipStyle": draft.promptProfile.relationshipStyle,
            "emotionStyle": draft.promptProfile.emotionStyle,
            "speakingStyle": draft.promptProfile.speakingStyle,
            "conversationInitiative": draft.promptProfile.conversationInitiative,
            "examples": examples,
        }
        return CharacterRevision(
            characterId=character_id, revision=revision, displayName=draft.displayName,
            description=draft.description, aliases=draft.aliases, promptProfile=profile,
            runtimeDefaults=draft.runtimeDefaults.model_dump(),
            presentation=draft.presentation.model_dump(),
            openingMessage=draft.openingMessage, createdBy=created_by,
        )


def _semantic_issues(text: str, path: str) -> list[CharacterValidationIssue]:
    """把校验判定的 issue 转为契约 CharacterValidationIssue（path JSON Pointer）。"""
    from core.character.draft import validate_semantic
    return [CharacterValidationIssue(severity=i.severity, code=i.code, path=path, message=i.message)
            for i in validate_semantic(text)]
```

- [ ] **Step 4: 运行确认通过** — `python -m pytest core/character/tests/test_service.py -q` — PASS

- [ ] **Step 5: Commit**

```bash
git add core/character/service.py core/character/tests/test_service.py
git commit -m "feat(stage-d): CharacterService 业务编排（create/get/list/update/clone/archive/rollback/validate + Revision-first + CAS）"
```

---

### Task 5: CharacterConfig.user_pronoun 彻底移除 + 回归修正

**Files:**
- Modify: `core/config/character_config.py`（删 user_pronoun 字段 + from_yaml 解析）
- Modify: `character/elysia/config.yaml`、`character/cyrene/config.yaml`（删 user_pronoun 行）
- Modify: `core/prompt/tests/conftest.py`（删 user_pronoun= 构造参数）
- Test: `core/config/tests/test_character_config.py`、`core/prompt/tests/test_prompt_assembler.py`

**Interfaces:**
- 依赖：spec §9.1/§9.2 已核实——`prompt_assembler.py:154-158` 只读 `_turn_context.userPronoun`（不读 `_character.user_pronoun`），删除安全；`Module/system_prompt/sections/*.py` 的 `kwargs.get("user_pronoun","她")` 回退链与 `{{ user_pronoun }}` 模板占位符**保留**（渲染自 `UserIdentity.pronouns`）。
- 本任务不做契约 DTO（已在 Task 4A 完成）——只做 user_pronoun 物理删除。

- [ ] **Step 1: 删 user_pronoun 字段**

`core/config/character_config.py`：
- 删 `user_pronoun: str = "她"` 字段（行 29，含 DEPRECATED 注释）
- 删 `from_yaml` 里 `user_pronoun=raw.get("user_pronoun", "她"),`（行 80）
- 删 `character/README.md` 里的 `user_pronoun` 文档表项（行 34）

`character/elysia/config.yaml` 删行 9 `user_pronoun: 她 ...`
`character/cyrene/config.yaml` 删行 9 `user_pronoun: 你 ...`

`core/prompt/tests/conftest.py` 删行 33 `user_pronoun="你",` 与行 77 `user_pronoun="她",`

- [ ] **Step 2: 运行回归** — `python -m pytest core/config/tests/test_character_config.py core/prompt/tests/test_prompt_assembler.py -q`
  预期：无 `cold_config.user_pronoun` 取值断言（已核实），仅删构造参数即可。若 `test_user_pronoun_from_turn_context_overrides` 因失去对照基准变恒真，可加注释说明（不阻塞）。

- [ ] **Step 3: 运行 Elysia 快照回归** — `python -m pytest -k "elysia_snapshot or system_prompt" core/ Module/system_prompt/tests/ -q`
  预期：`test_elysia_snapshot_unchanged` 保持 green（18127B）。**若失败，说明删字段影响了渲染——必须排查**（理论上不读 config.yaml，不应失败）。

- [ ] **Step 4: Commit**

```bash
git add core/config/character_config.py character/elysia/config.yaml character/cyrene/config.yaml core/prompt/tests/conftest.py character/README.md
git commit -m "feat(stage-d): CharacterConfig.user_pronoun 彻底移除（阶段 C+E 承诺兑现）"
```

---

### Task 6: character_routes.py REST 端点 + 错误码映射 + UserIdentityStore CAS

**Files:**
- Create: `core/input/character_routes.py`
- Modify: `core/input/server.py`（注册 `register_character_routes(app, _check_auth)`）
- Modify: `core/profile/profile_store.py`（加 `update_if_revision`）
- Test: `core/input/tests/test_character_routes.py`、`core/profile/tests/test_profile_store.py`

**Interfaces:**
- Consumes: `CharacterService`（Task 4）、`UserIdentityStore`（改后）、`_protocol_error_response`（`core/input/errors.py`）。
- Produces: `register_character_routes(app, check_auth)` —— 注册 `/api/me`（GET/PUT）、`/api/characters`（GET/POST）、`/api/characters/{id}`（GET/PUT/DELETE）、`/api/characters/{id}/clone`、`/api/characters/{id}/revisions`、`/api/characters/{id}/rollback`、`/api/characters/validate`。
  - `UserIdentityStore.update_if_revision(expected_revision, updates) -> UserIdentity | None`。

- [ ] **Step 1: 写失败测试** — `core/profile/tests/test_profile_store.py`（追加）

```python
def test_update_if_revision_success():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    # bootstrap 后 revision=1
    result = store.update_if_revision(1, {"displayName": "妖梦2"})
    assert result is not None and result.displayName == "妖梦2" and result.revision == 2


def test_update_if_revision_conflict():
    store = UserIdentityStore(storage_path=None)
    store.bootstrap("妖梦")
    result = store.update_if_revision(99, {"displayName": "x"})  # 过期 revision
    assert result is None
```

`core/input/tests/test_character_routes.py`：

```python
"""character_routes 错误映射单测（marker=service，纯函数，无 aiohttp client/Agent）。

阶段 D 路由的可靠测试点是 `_character_error_response`（Service 异常 → ProtocolError
映射），它是纯函数、确定性、无 I/O。完整 aiohttp 路由冒烟在阶段 G/联调做（需装 Agent）。
"""
from core.input.character_routes import _character_error_response
from core.character.service import (
    CharacterNotFoundError, CharacterImmutableError, CharacterArchivedError,
    CharacterValidationError, RevisionConflictError,
)
from core.storage.base import StorageUnavailableError


def test_not_found_maps_to_404():
    r = _character_error_response(CharacterNotFoundError("x"))
    assert r is not None and r.status == 404


def test_revision_conflict_maps_to_409_retryable():
    r = _character_error_response(RevisionConflictError("x"))
    assert r is not None and r.status == 409


def test_immutable_maps_to_409():
    r = _character_error_response(CharacterImmutableError("x"))
    assert r is not None and r.status == 409


def test_validation_maps_to_400():
    r = _character_error_response(CharacterValidationError("x"))
    assert r is not None and r.status == 400


def test_storage_unavailable_maps_to_503():
    r = _character_error_response(StorageUnavailableError("x"))
    assert r is not None and r.status == 503


def test_unknown_exc_maps_to_none():
    assert _character_error_response(ValueError("x")) is None


def test_archived_maps_to_409():
    r = _character_error_response(CharacterArchivedError("x"))
    assert r is not None and r.status == 409
```

> 注：完整 aiohttp 路由冒烟（`aiohttp_client` + `fake_service` fixture）留阶段 G 联调。本批的错误映射 + 路由注册（`register_character_routes` 注册到 app）用 `_character_error_response` 纯函数单测覆盖，确定性、无 Agent/DB 依赖。

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/profile/tests/test_profile_store.py::test_update_if_revision_* -q`
  预期：FAIL（`update_if_revision` 不存在）。

- [ ] **Step 3: 实现 update_if_revision** — `core/profile/profile_store.py`

```python
def update_if_revision(self, expected_revision: int,
                       updates: dict[str, object]) -> UserIdentity | None:
    """CAS 更新：仅当当前 revision == expected_revision 时应用 updates 并 touch()。

    Updates 白名单为 displayName/preferredAddress/pronouns/locale/timezone/
    defaultCharacterId/initialized；不接收 userId/revision/createdAt/updatedAt。
    冲突返回 None（调用方抛 USER_PROFILE_REVISION_CONFLICT）；成功返回新 UserIdentity。
    单进程下用内存锁 + revision 比对实现原子 CAS（JSONL 单文件）。
    """
    cur = self.get()
    if cur is None:
        return None
    if cur.revision != expected_revision:
        return None
    new = cur.copy(deep=True)  # 或 model_copy
    for k, v in updates.items():
        if k in ("userId", "revision", "createdAt", "updatedAt"):
            continue
        if k == "initialized":
            v = True  # 只能置 true，不能改回 false
        setattr(new, k, v)
    new.touch()
    self._cached = new
    self._persist(new)
    return new
```

- [ ] **Step 4: 实现 character_routes.py** — `core/input/character_routes.py`

```python
"""character_routes — 角色/身份 REST 端点（阶段 D）。

错误码映射对齐架构稿 §14：CHARACTER_NOT_FOUND(404) / CHARACTER_VALIDATION(400) /
CHARACTER_REVISION_CONFLICT(409) / CHARACTER_IMMUTABLE(409) / CHARACTER_ARCHIVED(409) /
CHARACTER_STORAGE_UNAVAILABLE(503) / CHARACTER_TOO_LARGE(413) / USER_PROFILE_REVISION_CONFLICT(409)。
feature flag 恒 false —— custom 不参与真实聊天。
"""

import uuid
from typing import Callable

from aiohttp import web
from pydantic import ValidationError

from core.character.service import (
    CharacterNotFoundError, CharacterImmutableError, CharacterArchivedError,
    CharacterValidationError, RevisionConflictError, CharacterService,
)
from core.character.draft import CharacterDraft
from core.storage.base import StorageUnavailableError
from core.input.errors import _protocol_error_response
from frontend_contract.rest import UserIdentityProfile


def _character_error_response(exc: Exception) -> web.Response | None:
    """把 Service 异常映射为 ProtocolError 响应；未知返回 None。"""
    if isinstance(exc, CharacterNotFoundError):
        return _protocol_error_response("CHARACTER_NOT_FOUND", "角色不存在", 404)
    if isinstance(exc, CharacterImmutableError):
        return _protocol_error_response("CHARACTER_IMMUTABLE", "内置角色不可编辑", 409)
    if isinstance(exc, CharacterArchivedError):
        return _protocol_error_response("CHARACTER_ARCHIVED", "archived 角色不可操作", 409)
    if isinstance(exc, CharacterValidationError):
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    if isinstance(exc, RevisionConflictError):
        return _protocol_error_response("CHARACTER_REVISION_CONFLICT", "expectedRevision 已过期", 409, retryable=True)
    if isinstance(exc, StorageUnavailableError):
        return _protocol_error_response("CHARACTER_STORAGE_UNAVAILABLE", "角色存储不可用", 503, retryable=True)
    return None


def _draft_from_body(body: dict) -> CharacterDraft:
    """把 wire 请求体（CharacterDraftWidget 形状）转为领域 CharacterDraft。"""
    return CharacterDraft(**body)


def _service(app: web.Application) -> CharacterService:
    return app["character_service"]


async def _me_get(request: web.Request) -> web.Response:
    # 注意：app["user_identity_store"] 是 UserIdentityStore（有 .get()），
    # 不是 bootstrap() 返回的 UserIdentity 对象——后者无 .get()（B7 修复）。
    profile = request.app["user_identity_store"].get()
    if profile is None:
        return _protocol_error_response("USER_PROFILE_NOT_FOUND", "用户身份未初始化", 404)
    return web.json_response(UserIdentityProfile(**profile.model_dump()).wire_dict(), status=200)


async def _me_put(request: web.Request) -> web.Response:
    body = await request.json()
    expected = body.get("expectedRevision")
    store = request.app["user_identity_store"]
    new = store.update_if_revision(int(expected) if expected is not None else 1,
                                   {k: v for k, v in body.items() if k != "expectedRevision"})
    if new is None:
        return _protocol_error_response("USER_PROFILE_REVISION_CONFLICT", "expectedRevision 已过期", 409, retryable=True)
    return web.json_response(UserIdentityProfile(**new.model_dump()).wire_dict(), status=200)


async def _characters_list(request: web.Request) -> web.Response:
    svc = _service(request.app)
    try:
        resp = await svc.list()
        return web.json_response(resp.wire_dict(), status=200)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_create(request: web.Request) -> web.Response:
    body = await request.json()
    svc = _service(request.app)
    try:
        draft = _draft_from_body(body)
    except ValidationError as e:
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    try:
        detail = await svc.create(draft, owner_user_id="usr_local_default")
        return web.json_response(detail.wire_dict(), status=201)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_validate(request: web.Request) -> web.Response:
    body = await request.json()
    svc = _service(request.app)
    try:
        draft = _draft_from_body(body)
    except ValidationError as e:
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    result = await svc.validate(draft)
    return web.json_response(result.wire_dict(), status=200)


def register_character_routes(app: web.Application, check_auth: Callable | None = None) -> None:
    """注册角色/身份 REST 路由（对齐 server.py 既有 register_xxx_routes 模式）。"""
    app.router.add_get("/api/me", _me_get)
    app.router.add_put("/api/me", _me_put)
    app.router.add_get("/api/characters", _characters_list)
    app.router.add_post("/api/characters", _characters_create)
    app.router.add_post("/api/characters/validate", _characters_validate)
    # 其余 {id} 路由在 Task 7 补齐（GET/PUT/DELETE/clone/revisions/rollback）
```

- [ ] **Step 5: 运行确认通过** — `python -m pytest core/profile/tests/test_profile_store.py -q` — PASS

- [ ] **Step 6: Commit**

```bash
git add core/profile/profile_store.py core/input/character_routes.py core/input/server.py core/profile/tests/test_profile_store.py core/input/tests/test_character_routes.py
git commit -m "feat(stage-d): character_routes REST（/api/me + /api/characters 基础）+ UserIdentityStore CAS（update_if_revision）"
```

---

### Task 7: 补全 characters/{id} REST 路由 + 错误映射 + 装配

**Files:**
- Modify: `core/input/character_routes.py`（补 `/api/characters/{id}` GET/PUT/DELETE、`/{id}/clone`、`/{id}/revisions`、`/{id}/rollback`）
- Modify: `core/input/server.py`（装配 `character_service`/`user_identity_store` 到 app）
- Test: `core/input/tests/test_character_routes.py`

**Interfaces:**
- Consumes: `CharacterService`（已建）。
- Produces: 完整端点集。

- [ ] **Step 1: 补路由** — 在 `register_character_routes` 里追加 handlers

```python
async def _characters_get(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    svc = _service(request.app)
    try:
        detail = await svc.get(cid)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)
    if detail is None:
        return _protocol_error_response("CHARACTER_NOT_FOUND", "角色不存在", 404)
    return web.json_response(detail.wire_dict(), status=200)


async def _characters_update(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    body = await request.json()
    svc = _service(request.app)
    try:
        draft = _draft_from_body(body)
        result = await svc.update(cid, draft, expected_revision=int(body.get("expectedRevision", 1)))
        return web.json_response(result.wire_dict(), status=200)
    except ValidationError as e:
        return _protocol_error_response("CHARACTER_VALIDATION", "角色内容校验失败", 400)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_delete(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    svc = _service(request.app)
    try:
        result = await svc.archive(cid)
        return web.json_response(result.wire_dict(), status=200)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_clone(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    body = await request.json() or {}
    svc = _service(request.app)
    try:
        detail = await svc.clone(cid, display_name=body.get("displayName"))
        return web.json_response(detail.wire_dict(), status=201)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_revisions(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    svc = _service(request.app)
    try:
        revs = await svc.rev_repo.list_revisions(cid, limit=int(request.query.get("limit", 50)))
        from frontend_contract.rest import CharacterRevisionListResponse, CharacterRevisionSummary
        items = [CharacterRevisionSummary(characterId=r.characterId, revision=r.revision,
                                          displayName=r.displayName, createdAt=r.createdAt,
                                          createdBy=r.createdBy) for r in revs]
        return web.json_response(CharacterRevisionListResponse(items=items).wire_dict(), status=200)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)


async def _characters_rollback(request: web.Request) -> web.Response:
    cid = request.match_info["id"]
    body = await request.json()
    svc = _service(request.app)
    try:
        result = await svc.rollback(cid, target_revision=int(body["targetRevision"]),
                                    expected_revision=int(body.get("expectedRevision", 1)))
        return web.json_response(result.wire_dict(), status=200)
    except Exception as e:
        err = _character_error_response(e)
        return err if err else _protocol_error_response("INTERNAL_ERROR", "内部错误", 500)
```

并在 `register_character_routes` 里注册：
```python
app.router.add_get("/api/characters/{id}", _characters_get)
app.router.add_put("/api/characters/{id}", _characters_update)
app.router.add_delete("/api/characters/{id}", _characters_delete)
app.router.add_post("/api/characters/{id}/clone", _characters_clone)
app.router.add_get("/api/characters/{id}/revisions", _characters_revisions)
app.router.add_post("/api/characters/{id}/rollback", _characters_rollback)
```

- [ ] **Step 2: 装配 server.py** — 在 `create_app`（`core/input/server.py`）里初始化 `character_service`/`user_identity_store` 并注入 app，在行 279 `register_session_routes(app)` 之后、行 280 `register_debug_routes(app, _check_auth)` 之前调用 `register_character_routes`.

```python
# Core/input/server.py create_app 内 —— 插在 register_session_routes(app) 与 register_debug_routes(app, _check_auth) 之间
# 注意：create_app 是同步函数（def，非 async def）——不能 await。repo 连接是惰性的：
# connect() 由首次 CRUD 操作的 fail-closed 路径触发（未连接即抛 StorageUnavailableError → 503）。
from core.character.service import CharacterService
from core.profile.profile_store import UserIdentityStore
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.character.catalog import CharacterCatalog
from core.character.builtin_source import BuiltinCharacterSource
from core.input.character_routes import register_character_routes

# 复用 agent 已建的用户身份存储（agent._user_identity_store，行 120-121）——
# 不能用新的 UserIdentityStore()（会让 /api/me 更新与 cognitive loop 渲染用的 store 脱节，
# 用户改别名/代词后实际聊天不生效）。server 装配层允许 # noqa: SLF001 访私有（同 loop.configure 的用法）。
app["user_identity_store"] = getattr(agent, "_user_identity_store", None) or UserIdentityStore()
if app["user_identity_store"].get() is None:
    agent_config = getattr(agent, "config", None)
    app["user_identity"] = app["user_identity_store"].bootstrap(
        (agent_config.agent.user_name if agent_config and getattr(agent_config, "agent", None)
         else "用户"))
# CharacterService：repo 无 db 参数时 connect 建真实 Mongo（首次操作 fail-closed）；
# 与 idempotency_repo 一样在 create_app 注入。
app["character_service"] = CharacterService(
    repo=CharacterRepository(), rev_repo=CharacterRevisionRepository(),
    catalog=CharacterCatalog(source=BuiltinCharacterSource()),
    user_identity_store=getattr(agent, "_user_identity_store", None) or UserIdentityStore(),
)
register_character_routes(app, _check_auth)
```

> 注：`create_app` 是同步函数（`server.py:204 def create_app`），**不能 `await`**。repo 的 `connect()` 是惰性的——`CharacterRepository()` 构造时 `_db=None`，首次 `insert_definition`/`get_definition` 时若 `_db is None` 抛 `StorageUnavailableError` → REST 映射 503。真正连接 Mongo 的路径可后续在 `_lifecycle_persistence` 的 `_start_persistence`（async）统一 `connect()`（对齐 `CharacterVersionRepository` 先例），本批在 `create_app` 注入即可（与 `idempotency_repo` 同模式）。`CharacterRepository()`/`CharacterRevisionRepository()` 无 db 参数时 `connect()` 建真实 Mongo + ensure_index。

> 注：实际 `create_app` 的装配按既有 `server.py` 结构；`CharacterRepository()`/`CharacterRevisionRepository()` 无 `db` 参数时 connect 建真实 Mongo。若 app 已有 `_db` 实例，可注入复用。

- [ ] **Step 3: 运行** — `python -m pytest core/input/tests/test_character_routes.py -q` — PASS

- [ ] **Step 4: Commit**

```bash
git add core/input/character_routes.py core/input/server.py core/input/tests/test_character_routes.py
git commit -m "feat(stage-d): 补全 characters/{id} REST + server 装配"
```

---

### Task 8: 验证阶段 D 全套测试 + 回归

**Files:**
- Test: 全套（`core/character/tests/`、`core/storage/tests/`、`core/profile/tests/`、`core/input/tests/`、`frontend_contract/tests/`）

**Interfaces:**
- 验证 spec §13 的 7 个不变式（Elysia byte、CAS 原子性、append-only、Revision-first、custom 不参与聊天/feature flag false、user_pronoun 移除、离线 fail-closed）。

- [ ] **Step 1: 运行单元+服务** — `make test-quick` 或 `python -m pytest -m "unit or service" -q`

- [ ] **Step 2: 运行 Elysia 快照回归** — `python -m pytest -k "elysia_snapshot" -q` — 必须 green（不变式 1）

- [ ] **Step 3: 运行 lint + pyright** — `make lint` + `conda run -n Elysia pyright`

- [ ] **Step 4: 契约产物 + 漂移** — `make gen-types && make check-types`

- [ ] **Step 5: 验证 `character_profiles=false`（不变式 5）+ `user_pronoun` 移除（不变式 6）**

```bash
# 1) user_pronoun 生产消费点已清（只剩 sections 回退链 + set_context + 模板占位符）
grep -rn "user_pronoun" core/ Module/ --include=*.py | grep -v __pycache__ | grep -v "/tests/" | grep -v "kwargs.get\|_user_pronoun = \|user_pronoun=self\|user_pronoun=user_pronoun\|user_pronoun=\""
# 2) CharacterConfig 已删 user_pronoun 字段——model_fields 不应再有该键
python3 -c "from core.config.character_config import CharacterConfig; assert 'user_pronoun' not in CharacterConfig.__dataclass_fields__ if hasattr(CharacterConfig,'__dataclass_fields__') else True; print('user_pronoun 字段已从 CharacterConfig 移除')"
# 3) feature flag 恒 false（字段默认 + build_server_info 调用点双处）
python3 -c "from frontend_contract.ws_message import ServerInfoFeatures; assert ServerInfoFeatures().character_profiles is False; print('character_profiles 默认 False')"
grep -n "character_profiles=False" core/services/capabilities.py
```

> 预期：1) grep 只命中 `Module/system_prompt/sections/*.py` 的 `kwargs.get("user_pronoun","她")` 与 `set_context(user_pronoun=...)`（模板 `{{ user_pronoun }}` 保留——渲染自 `UserIdentity.pronouns`）；2) `CharacterConfig` 无 `user_pronoun` 字段；3) `ServerInfoFeatures.character_profiles` 默认 `False` 且 `capabilities.py:107` 恒 `False`。

- [ ] **Step 6: 更新文档** — `core/character/README.md`（补 custom/CRUD）、`core/profile/README.md`（UserIdentityStore CAS）、根 `README.md`（字符段注册）、`docs/readme-conventions.md` 合规。

- [ ] **Step 7: Commit**

```bash
git add -A core/character core/storage/tests core/profile core/input frontend_contract README.md docs
git commit -m "test(stage-d): 全套测试验证 + 文档更新"
```
