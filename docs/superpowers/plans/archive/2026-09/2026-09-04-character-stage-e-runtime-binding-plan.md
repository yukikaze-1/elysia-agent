# 阶段 E：CharacterRuntime 与会话绑定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Plan status: clean (post-2-round-review rewrite).
>
> **Spec：** [`docs/superpowers/specs/archive/2026-09/2026-09-04-character-stage-e-runtime-binding-design.md`](../../../specs/archive/2026-09/2026-09-04-character-stage-e-runtime-binding-design.md)

**Goal:** 把角色运行时从「单例 CharacterEngine + session-attach 状态换绑」（M1/M2）重构为「Engine per-instance + RuntimeRegistry 解析 + CharacterRuntimeState 权威(instance-scoped)」（M3 目标态）。让两个角色可在不同会话生成且 Prompt/状态/声音不串。

**Architecture:** 方案 A——`CharacterRuntimeState` 扁平迁移（与 CharacterState 同构，保留 session_id 版本键 + 新增 instance_id 权威 + source_revision）；`CharacterInstanceRepository`（character_instances 集合 + state_version CAS）；`CharacterRuntimeRegistry.get(instanceId)` 解析 per-instance runtime+engine；`_prepare_turn` 从 session.character_binding 解析 instanceId，不再 attach_session；去 attach_session/_pre_save_hook/sync_reflector_to_state；TurnCharacterContext 扩展（memoryScope/voice/displayName/stateSnapshot）；prompt 层经 set_personality_engine + set_character 路由本回合角色。

**Tech Stack:** Python 3.12 + Pydantic + MongoDB(Motor) + asyncio。零新依赖。

## Global Constraints

- **Elysia byte-for-byte**：`test_elysia_snapshot_unchanged`（`core/prompt/tests/test_system_prompt_snapshot.py:79`，fixture `system_prompt_snapshot_elysia_cf4bc008.txt` 18127B）必须 green。默认会话（default → elysia instance）渲染字节不变。
- **`character_profiles=false` 恒抑制**（`core/services/capabilities.py:107` + `frontend_contract/ws_message.py:275`）——本批仅内置角色（elysia/cyrene）走 instance runtime。
- **禁相对导入**（`from core.xxx` / `from Module.xxx`，ruf TID252）；生产代码参数类型注解（ANN001/002/003，测试豁免）；注释中文。
- **conda env**：`conda run -n Elysia python -m pytest ...`。
- **`core/storage/base.py`** 的 `Repository`/`StorageUnavailableError`/`_call` 为 fail-closed 语义。
- **事实基线**：基于 stage D 完成 HEAD `4089d30f`。旧会话清除（pre-production）。
- **F1-b 语义（spec §6.3）**：M3 `_apply_mood` 写 `engine.state`（engine per-instance 永不换绑，F1-b 跨会话污染风险源消除）；需**翻转** `test_mood_engine.py::test_apply_mood_writes_target_session_state`。

## Task 结构（按正确依赖序）

| Task | 交付 | 依赖 |
|------|------|------|
| 1 | `CharacterRuntimeState` + `CharacterInstance.state` | — |
| 2 | `CharacterInstanceRepository`（character_instances + state_version CAS） | Task 1 |
| 3 | `CharacterRuntime` 激活（engine per-instance + snapshot/flush） | Task 1 |
| 4 | `CharacterRuntimeRegistry` 激活（_busy/_session_binding_index） | Task 3 |
| 5 | `TurnCharacterContext` + `GenerationContext` 扩展 | Task 1 |
| 6 | 去 `attach_session`/`sync_reflector_to_state`（engine.py）+ 删测试 | Task 1 |
| 7 | loop `__init__`/`configure` 加 `runtime_registry`/`runtime_factory`/`engine_factory`；`_character_engine`→`_engine` | Task 3,4 |
| 8 | `_prepare_turn` 重构（binding→registry.get→runtime.engine——不 await，get 同步） | Task 5,6,7,9 |
| 9 | `prompt_assembler` 加 `set_personality_engine`/`set_character` | — |
| 10 | `streaming._apply_mood` 写 engine.state + F1-b 翻转 + `streaming.py:242` 改 `self._engine` | Task 7 |
| 11 | 接线（session_manager/session/agent/_lifecycle_persistence + lifecycle flush） | Task 6,7,8,9 |
| 12 | 读端迁移（ws_handler/debug/archive/debug_routes/character → engine.state） | Task 11 |
| 13 | frontend_contract generation 审计字段 + 契约产物 | Task 5 |
| 14 | README 文档 + 全套验证 | 全部 |

> **body 顺序 = 表顺序**（Task 5 先于 8，prompt 9 先于 _prepare_turn 用,读端 12 在接线 11 后——修正 2 轮 review 前向依赖）。

---

### Task 1: CharacterRuntimeState + CharacterInstance.state

**Files:** Modify `core/schema/character.py`、`core/character/instance.py`；Test `core/schema/tests/test_character_runtime_state.py`

**Interfaces:** Produces `CharacterRuntimeState`（扁平，`instance_id: str` + `session_id: SessionId` + `source_revision: int = 1` + 同构 CharacterState 字段）；`CharacterInstance.state: CharacterRuntimeState | None = None`。
- Consumes: `core/schema/character.py` 已有 `SessionId`/`IsoTimestamp`/`now_iso`/`BaseModel`/`Field`/`StateHistoryEntry`（顶层已导入，无需加）。

- [ ] **Step 1: 写失败测试**

```python
# core/schema/tests/test_character_runtime_state.py (新建)
"""CharacterRuntimeState 扁平模型（与 CharacterState 同构，instance-scoped）。"""
from core.schema.character import CharacterRuntimeState
from core.character.instance import CharacterInstance
from core.schema.types import SessionId

class TestCharacterRuntimeState:
    def test_instance_session_ids_present(self):
        s = CharacterRuntimeState(instance_id="chi_builtin_elysia", session_id=SessionId("default"))
        assert s.instance_id == "chi_builtin_elysia"
        assert s.session_id == "default"
        assert s.source_revision == 1

    def test_flat_fields_match_character_state(self):
        s = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
        assert s.self_l2 == "" and s.state_l3 == ""
        assert s.mood == "平静" and s.mood_intensity == 0.5
        assert s.speaking_style_c6 == ""
        assert s.reflector_buffer == [] and s.self_perception_history == []
        assert s.last_awareness_time is None
        assert s.approved_awareness_text == "" and s.awareness_show_count == 0
        assert s.compression_last_at is None and s.compression_last_count == 0

    def test_instance_state_field(self):
        inst = CharacterInstance(instanceId="chi_x", characterId="chr_x")
        assert inst.state is None
        inst.state = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
        assert inst.state.instance_id == "chi_x"
```

- [ ] **Step 2: 跑测试确认 FAIL** — `conda run -n Elysia python -m pytest core/schema/tests/test_character_runtime_state.py -q` → FAIL（ImportError）
- [ ] **Step 3: 实现** — `core/schema/character.py` 加（CharacterState 之后）：

```python
class CharacterRuntimeState(BaseModel):
    """instance-scoped 角色运行时状态（阶段 E，§5.8 扁平迁移）。与 CharacterState 字段同构
    （子组件 0 改动），身份 instance_id（权威），保留 session_id（版本历史键）。"""
    instance_id: str
    session_id: SessionId
    source_revision: int = 1
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None
    approved_awareness_text: str = ""
    approved_awareness_at: float | None = None
    awareness_show_count: int = 0
    compression_last_at: float | None = None
    compression_last_count: int = 0
    mood: str = "平静"
    mood_intensity: float = 0.5
    speaking_style_c6: str = ""
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

`core/character/instance.py` 加（import `from core.schema.character import CharacterRuntimeState`）：

```python
class CharacterInstance(BaseModel):
    instanceId: str
    characterId: str
    revision: int = Field(default=1, ge=1)
    ownerUserId: str = "usr_local_default"
    stateVersion: int = Field(default=1, ge=1)
    state: CharacterRuntimeState | None = None   # 阶段 E：instance-scoped 状态权威持久化落点
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
```

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/schema/tests/test_character_runtime_state.py -q` → PASS
- [ ] **Step 5: 回归** — `pytest core/schema/ core/character/ -q` → PASS
- [ ] **Step 6: Commit** — `git add core/schema/character.py core/character/instance.py core/schema/tests/test_character_runtime_state.py && git commit -m "feat(stage-e): CharacterRuntimeState(扁平, instance_id 权威 + session_id 版本键) + CharacterInstance.state"`

---

### Task 2: CharacterInstanceRepository（character_instances 集合 + state_version CAS）

**Files:** Create `core/storage/character_instance_repo.py`；Test `core/storage/tests/test_character_instance_repo.py`
- Consumes: `core/storage/base.py` 的 `Repository`/`StorageUnavailableError`/`_call`；`Module.database.documentdb` 的 `DocumentDBBackend`/`DocumentDBFactory`；`core/schema/utils.py` 的 `now_iso`；`core/character/instance.py`。

- [ ] **Step 1: 写失败测试**（用 `core/storage/tests/_fake_documentdb.py` 的真实类名 `FakeDocumentDBBackend`）

```python
# core/storage/tests/test_character_instance_repo.py (新建)
"""CharacterInstanceRepository 测试（marker=service，内存假后端）。"""
import pytest
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.character.instance import CharacterInstance
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

def _inst(**over) -> CharacterInstance:
    base = dict(instanceId="chi_x", characterId="chr_x", revision=1, stateVersion=1)
    base.update(over)
    return CharacterInstance(**base)

@pytest.mark.asyncio
async def test_get_save_roundtrip():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    inst = _inst(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")))
    await repo.save(inst)
    got = await repo.get("chi_x")
    assert got is not None and got.instanceId == "chi_x"
    assert got.state.instance_id == "chi_x"

@pytest.mark.asyncio
async def test_cas_state_version_hit():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.save(_inst(stateVersion=1))
    ok = await repo.cas_state_version("chi_x", expected=1,
        new_state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")), new_version=2)
    assert ok is True
    assert (await repo.get("chi_x")).stateVersion == 2

@pytest.mark.asyncio
async def test_cas_state_version_miss_conflict():
    repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    await repo.connect()
    await repo.save(_inst(stateVersion=2))
    ok = await repo.cas_state_version("chi_x", expected=1,
        new_state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")), new_version=3)
    assert ok is False

@pytest.mark.asyncio
async def test_offline_get_raises():
    repo = CharacterInstanceRepository(db=None)
    with pytest.raises(Exception):
        await repo.get("chi_x")
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/storage/tests/test_character_instance_repo.py -q` → FAIL（ModuleNotFoundError）
- [ ] **Step 3: 实现**

```python
# core/storage/character_instance_repo.py
"""CharacterInstanceRepository — character_instances 集合的 instance 持久化。"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING
from Module.database.documentdb import DocumentDBBackend, DocumentDBFactory
from core.schema.utils import now_iso
from core.storage.base import Repository, StorageUnavailableError
from core.character.instance import CharacterInstance

if TYPE_CHECKING:
    from core.schema.character import CharacterRuntimeState
logger = logging.getLogger(__name__)
COLLECTION = "character_instances"
_TOP_LEVEL_KEYS = {
    "instanceId": "instance_id", "characterId": "character_id",
    "ownerUserId": "owner_user_id", "stateVersion": "state_version",
    "createdAt": "created_at", "updatedAt": "updated_at",
}

class CharacterInstanceRepository(Repository):
    """character_instances 集合 instance CRUD + state_version CAS（fail-closed）。"""
    def __init__(self, db: DocumentDBBackend | None = None, *, base_url: str | None = None):
        self._db = db
        self._base_url = base_url
        self._injected_db = db is not None

    async def connect(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default", base_url=self._base_url)
            await self._db.ensure_index(COLLECTION, [("instance_id", 1)], unique=True)
            return True
        except Exception as e:
            logger.warning("CharacterInstanceRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return await self._health_check()

    async def get(self, instance_id: str) -> CharacterInstance | None:
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        docs = await self._call(lambda: self._db.find(COLLECTION, {"instance_id": instance_id}, limit=1))
        if not docs:
            return None
        return CharacterInstance.model_validate(self._from_doc(docs[0]))

    async def save(self, instance: CharacterInstance) -> None:
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        # $set 包装——update_one 的 update 参数要求 Mongo 更新操作符文档(fake 只识别 $set)。
        await self._call(lambda: self._db.update_one(
            COLLECTION, {"instance_id": instance.instanceId}, {"$set": self._to_doc(instance)}, upsert=True))

    async def cas_state_version(self, instance_id: str, *, expected: int,
                                new_state: "CharacterRuntimeState", new_version: int) -> bool:
        """CAS state_version：filter 恒含 state_version==expected + updated_at 必变。"""
        if self._db is None:
            raise StorageUnavailableError("CharacterInstanceRepository 未连接")
        mod = await self._call(lambda: self._db.update_one(
            COLLECTION,
            {"instance_id": instance_id, "state_version": expected},
            {"$set": {"state": new_state.model_dump(mode="json"), "state_version": new_version,
                      "updated_at": now_iso()}},
        ))
        return mod > 0

    @staticmethod
    def _to_doc(inst: CharacterInstance) -> dict:
        doc = {}
        for k, v in inst.model_dump(mode="json").items():
            doc[_TOP_LEVEL_KEYS.get(k, k)] = v
        return doc

    @staticmethod
    def _from_doc(doc: dict) -> dict:
        data = {}
        for k, v in doc.items():
            if k == "_id":
                continue
            rev_key = next((dk for dk, dk_v in _TOP_LEVEL_KEYS.items() if dk_v == k), k)
            data[rev_key] = v
        return data
```

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/storage/tests/test_character_instance_repo.py -q` → PASS
- [ ] **Step 5: ruff + pyright** — `conda run -n Elysia ruff check core/storage/character_instance_repo.py --exclude __pycache__; conda run -n Elysia pyright core/storage/character_instance_repo.py` → clean / 0 errors
- [ ] **Step 6: Commit** — `git add core/storage/character_instance_repo.py core/storage/tests/test_character_instance_repo.py && git commit -m "feat(stage-e): CharacterInstanceRepository(character_instances 集合 + state_version CAS)"`

---

### Task 3: CharacterRuntime 激活（engine per-instance + snapshot/flush）

**Files:** Modify `core/character/runtime.py`；Test `core/character/tests/test_runtime.py`
- Consumes: `CharacterInstance.state`（Task 1）、`CharacterRuntimeState`、`CharacterDefinition`、`CharacterRevision`、`UserIdentity`、`SessionId`。
- Produces: `runtime.state`/`ensure_engine(factory)`/`set_persist_fn(fn)`/`snapshot_state()`/`flush()`。

- [ ] **Step 1: 写失败测试**（`test_runtime.py` 追加）

```python
# core/character/tests/test_runtime.py (追加)
"""CharacterRuntime 激活测试（M3：engine per-instance + state 真实化）。"""
import pytest
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

def _inst(state=None):
    return CharacterInstance(instanceId="chi_x", characterId="chr_x", state=state)

def _rt(state=None):
    return CharacterRuntime(
        instance=_inst(state), definition=CharacterDefinition(characterId="chr_x"),
        revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
        user_identity=UserIdentity(userId="usr_local_default"),
    )

def test_initial_state_empty_when_none():
    rt = _rt()
    assert rt.state is not None
    assert rt.state.instance_id == "chi_x"

def test_initial_state_loaded_when_present():
    rt = _rt(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"), self_l2="abc"))
    assert rt.state.self_l2 == "abc"

def test_engine_lazy_bind():
    rt = _rt()
    assert rt.engine is None
    rt.ensure_engine(factory=lambda: object())
    assert rt.engine is not None

def test_snapshot_state_returns_copy():
    rt = _rt(state=CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s")))
    snap = rt.snapshot_state()
    snap.self_l2 = "mutated"
    assert rt.state.self_l2 == ""

@pytest.mark.asyncio
async def test_flush_sets_dirty_false_and_persist():
    calls = []
    rt = _rt()
    rt.state.self_l2 = "v2"
    rt.dirty = True
    async def _persist(state):
        calls.append(state.self_l2)
    rt.set_persist_fn(_persist)
    await rt.flush()
    assert rt.dirty is False
    assert calls == ["v2"]
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/character/tests/test_runtime.py -q` → FAIL（AttributeError: no attribute 'state'）
- [ ] **Step 3: 实现**（改 `core/character/runtime.py`——加 `state`/`ensure_engine`/`set_persist_fn`/`snapshot_state` 真实化/`flush` 持久化）：

```python
class CharacterRuntime:
    """单实例运行时容器（M3：engine per-instance + state 真实化）。"""
    def __init__(self, instance, definition, revision, user_identity):
        self.instance = instance
        self.definition = definition
        self.revision = revision
        self.user_identity = user_identity
        self._engine = None
        self.lock = asyncio.Lock()
        self.dirty = False
        self.stateVersion = instance.stateVersion
        self.state = instance.state or CharacterRuntimeState(
            instance_id=instance.instanceId,
            session_id=SessionId(instance.instanceId),
            source_revision=revision.revision,
        )
        self._persist_fn: Callable[[CharacterRuntimeState], Awaitable[None]] | None = None

    @property
    def engine(self):
        return self._engine

    def ensure_engine(self, factory) -> "CharacterEngine":
        if self._engine is None:
            self._engine = factory()
        return self._engine

    def bind_engine(self, engine: "CharacterEngine") -> None:
        self._engine = engine

    def set_persist_fn(self, fn: "Callable[[CharacterRuntimeState], Awaitable[None]] | None") -> None:
        self._persist_fn = fn

    def snapshot_state(self) -> CharacterRuntimeState:
        return self.state.model_copy(deep=True)

    async def flush(self) -> None:
        if self._persist_fn is not None:
            await self._persist_fn(self.state)
        self.dirty = False
```

（`runtime.py` 顶部需加 `from typing import TYPE_CHECKING, Callable, Awaitable`；`from core.schema.character import CharacterRuntimeState`；`from core.schema.types import SessionId`。）

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/character/tests/test_runtime.py -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia ruff check core/character/runtime.py --exclude __pycache__; conda run -n Elysia pyright core/character/runtime.py` → clean / 0 errors；`git add core/character/runtime.py core/character/tests/test_runtime.py && git commit -m "feat(stage-e): CharacterRuntime 激活(engine per-instance + snapshot_state 真实化 + flush 持久化)"`

---

### Task 4: CharacterRuntimeRegistry 激活（_busy/_session_binding_index）

**Files:** Modify `core/character/registry.py`；Test `core/character/tests/test_registry.py`
- Consumes: `CharacterRuntime`（Task 3）。
- Produces: `get(instance_id, factory)`（同步）；`mark_busy(instance_id, busy)`；`set_session_binding_index(idx)`；`close_all()` flush 各 runtime。

- [ ] **Step 1: 写失败测试**（`test_registry.py` 追加）

```python
# core/character/tests/test_registry.py (追加)
"""CharacterRuntimeRegistry 激活测试（M3：_busy/_session_binding_index）。"""
import pytest
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

def _factory(iid="chi_x"):
    def f():
        return CharacterRuntime(
            instance=CharacterInstance(instanceId=iid, characterId="chr_x"),
            definition=CharacterDefinition(characterId="chr_x"),
            revision=CharacterRevision(characterId="chr_x", revision=1, displayName="X"),
            user_identity=UserIdentity(userId="usr_local_default"),
        )
    return f

def test_get_per_instance_lazy():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_x", _factory())
    assert rt is not None
    assert reg.get("chi_x", _factory()) is rt

def test_mark_busy_prevents_evict():
    reg = CharacterRuntimeRegistry(max_size=1)
    reg.get("chi_a", _factory("chi_a"))
    rt_b = reg.get("chi_b", _factory("chi_b"))
    reg.mark_busy("chi_b", True)
    reg.get("chi_c", _factory("chi_c"))
    assert reg.get("chi_b", _factory("chi_b")) is rt_b

def test_session_binding_index_prevents_evict():
    reg = CharacterRuntimeRegistry(max_size=1)
    reg.get("chi_a", _factory("chi_a"))
    rt_b = reg.get("chi_b", _factory("chi_b"))
    reg.set_session_binding_index({"chi_b"})
    reg.get("chi_c", _factory("chi_c"))
    assert reg.get("chi_b", _factory("chi_b")) is rt_b
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/character/tests/test_registry.py -q` → FAIL（mark_busy/set_session_binding_index 不存在）
- [ ] **Step 3: 实现**（改 `core/character/registry.py`——加 `mark_busy`/`set_session_binding_index`；`__init__` 加 `self._session_binding_index: set[str] | None = None`；保留现有 `get`/`_evict_if_needed`/`invalidate`/`release`/`flush`/`flush_all`/`close_all`/`_is_bound_by_live_session`）：

```python
class CharacterRuntimeRegistry:
    def __init__(self, *, max_size: int = 32):
        self._max_size = max_size
        self._runtimes: OrderedDict[str, CharacterRuntime] = OrderedDict()
        self._session_binding_index: set[str] | None = None   # M3 激活

    def get(self, instance_id: str, factory: Callable[[], CharacterRuntime]) -> CharacterRuntime:
        if instance_id in self._runtimes:
            self._runtimes.move_to_end(instance_id)
            return self._runtimes[instance_id]
        rt = factory()
        self._runtimes[instance_id] = rt
        self._evict_if_needed()
        return rt

    def mark_busy(self, instance_id: str, busy: bool) -> None:
        rt = self._runtimes.get(instance_id)
        if rt is not None:
            setattr(rt, "_busy", busy)

    def set_session_binding_index(self, idx: set[str] | None) -> None:
        self._session_binding_index = idx
```

（`registry.py` 顶部已有 `OrderedDict`/`Callable` 导入；`_evict_if_needed`/`_is_bound_by_live_session`/`close_all` 保留现有实现。）

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/character/tests/test_registry.py -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia ruff check core/character/registry.py --exclude __pycache__; conda run -n Elysia pyright core/character/registry.py` → clean / 0 errors；`git add core/character/registry.py core/character/tests/test_registry.py && git commit -m "feat(stage-e): CharacterRuntimeRegistry 激活(_busy/_session_binding_index)"`

---

### Task 5: TurnCharacterContext + GenerationContext 扩展

**Files:** Modify `core/cognitive/turn_context.py`、`core/cognitive/generation.py`；Test `core/cognitive/tests/test_turn_context.py`
- Consumes: `CharacterRuntimeState`（Task 1）。
- Produces: `TurnCharacterContext` 扩展字段（memoryScope/voice/displayName/stateSnapshot）；`GenerationContext` 加 character_id/character_instance_id/character_revision。

- [ ] **Step 1: 写失败测试**（`test_turn_context.py` 追加）

```python
# core/cognitive/tests/test_turn_context.py (追加)
"""TurnCharacterContext 扩展字段（M3：memoryScope/voice/displayName/stateSnapshot frozen）。"""
from dataclasses import FrozenInstanceError
from core.cognitive.turn_context import TurnCharacterContext
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

def test_turn_context_extended_fields():
    st = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("s"))
    ctx = TurnCharacterContext(
        conversationId="conv_1", characterInstanceId="chi_x", characterId="chr_x",
        characterRevision=1, userPronoun="她", memoryScope="chi_x",
        voice={"providerId": None, "voiceId": None}, displayName="X", stateSnapshot=st,
    )
    assert ctx.memoryScope == "chi_x"
    assert ctx.displayName == "X"
    assert ctx.stateSnapshot.instance_id == "chi_x"
    with pytest.raises(FrozenInstanceError):
        ctx.memoryScope = "mutate"
```

（`test_turn_context.py` 顶部加 `import pytest`。）

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/cognitive/tests/test_turn_context.py -q` → FAIL（TurnCharacterContext 无这些字段）
- [ ] **Step 3: 实现**（`core/cognitive/turn_context.py`）：

```python
@dataclass(frozen=True)
class TurnCharacterContext:
    """不可变回合上下文（M3：memoryScope 预埋 F + voice/displayName/stateSnapshot）。"""
    conversationId: str
    characterInstanceId: str
    characterId: str
    characterRevision: int
    userPronoun: str
    memoryScope: str
    voice: dict
    displayName: str
    stateSnapshot: CharacterRuntimeState
```

`core/cognitive/generation.py` 的 `GenerationContext.__init__` 加 `character_id: str = ""`/`character_instance_id: str = ""`/`character_revision: int = 0`。

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/cognitive/tests/test_turn_context.py core/cognitive/tests/test_generation_model.py -q` → PASS
- [ ] **Step 5: Commit** — `git add core/cognitive/turn_context.py core/cognitive/generation.py core/cognitive/tests/test_turn_context.py && git commit -m "feat(stage-e): TurnCharacterContext 扩展(memoryScope/voice/displayName/stateSnapshot) + GenerationContext 审计字段"`

---

### Task 6: 去 attach_session/sync_reflector_to_state（engine.py）+ 删测试

**Files:** Modify `core/personality/engine.py`；Test `core/personality/tests/test_engine.py`（**删除 TestCharacterEngineAttachSession** + 追加）
- Consumes: `CharacterRuntimeState`（Task 1）。
- Produces: 删除 `attach_session()`（:347-395）/`sync_reflector_to_state()`（:339-346）；加 `sync_reflector_to_runtime_state()`；`state` 类型注解 `CharacterState`→`CharacterRuntimeState`。

- [ ] **Step 1: 写失败测试 + 删旧测试**（`test_engine.py`：**删除 `class TestCharacterEngineAttachSession`（:572-660，7 个 attach 测试）**——M3 删 attach_session 后这些必挂；追加以下）：

```python
# core/personality/tests/test_engine.py (追加)
"""去 attach_session 后 engine 构造不变 + sync_reflector_to_runtime_state。"""
from unittest.mock import MagicMock, AsyncMock
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.personality.engine import CharacterEngine
from core.config import CharacterConfig

def _make_runtime_engine(state: CharacterRuntimeState | None = None) -> CharacterEngine:
    s = state or CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("test-sid"))
    m = MagicMock(); m.think = AsyncMock(return_value=MagicMock(text="test"))
    o = MagicMock(); o.expression = MagicMock(return_value=MagicMock())
    return CharacterEngine(
        llm=m, state=s, character_name="Elysia", user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。", character_config=CharacterConfig(name="Elysia"), output=o,
    )

def test_engine_no_attach_session():
    assert not hasattr(_make_runtime_engine(), "attach_session")

def test_engine_state_is_runtime_state():
    assert isinstance(_make_runtime_engine().state, CharacterRuntimeState)

def test_sync_reflector_to_runtime_state():
    eng = _make_runtime_engine()
    eng._reflector._buffer = [("u", "a")]
    eng.sync_reflector_to_runtime_state()
    assert eng.state.reflector_buffer == [("u", "a")]
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/personality/tests/test_engine.py -q` → FAIL（state 是 CharacterState 非 RuntimeState；有 attach_session）
- [ ] **Step 3: 实现**（`core/personality/engine.py`）：
  1. 删 `attach_session()`（:347-395）——含 `_pre_save_hook` 注册、reflector buffer flush/restore、state 换绑。
  2. 删 `sync_reflector_to_state()`（:339-346）——改加 `sync_reflector_to_runtime_state()`：

```python
def sync_reflector_to_runtime_state(self) -> None:
    """reflector buffer/context → runtime.state（回合边界由 Runtime.flush 持久化）。"""
    if self._reflector is not None:
        self.state.reflector_buffer = list(self._reflector._buffer)
        self.state.reflector_context = list(self._reflector._context)
```

  3. `__init__` 的 `state` 类型注解 `"CharacterState"`→`"CharacterRuntimeState"`（子组件构造 `state=state` 不变，扁平字段 0 改动）。

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/personality/tests/test_engine.py -q` → PASS
- [ ] **Step 5: 回归** — `pytest core/personality/ -q` → PASS（reflector/compression/emotion/awareness 扁平字段不破坏）
- [ ] **Step 6: ruff + pyright + Commit** — `conda run -n Elysia ruff check core/personality/engine.py --exclude __pycache__; conda run -n Elysia pyright core/personality/engine.py` → clean / 0 errors；`git add core/personality/engine.py core/personality/tests/test_engine.py && git commit -m "feat(stage-e): 删除 attach_session/sync_reflector_to_state; engine.state 绑 CharacterRuntimeState; 加 sync_reflector_to_runtime_state"`

---

### Task 7: loop `__init__`/`configure` 加 runtime_registry/runtime_factory/engine_factory + `_character_engine`→`_engine`

**Files:** Modify `core/cognitive/loop.py`、`core/cognitive/streaming.py`（class 注解 `_character_engine`→`_engine`）、`core/cognitive/finalize.py`（同上）
- Consumes: `CharacterRuntimeRegistry`（Task 4）。
- Produces: `CognitiveLoop.__init__` 加 `runtime_registry`/`runtime_factory`/`engine_factory` 参数；`_registry`/`_runtime_factory`/`_engine_factory` 赋值；`self._character_engine`→`self._engine`。

- [ ] **Step 1: 写失败测试**（`test_loop.py` 加）

```python
# core/cognitive/tests/test_loop.py (追加)
def test_loop_injects_runtime_registry():
    from core.character.registry import CharacterRuntimeRegistry
    reg = CharacterRuntimeRegistry()
    loop = _make_loop(runtime_registry=reg, runtime_factory=lambda iid: None, engine_factory=lambda rt: None)
    assert loop._registry is reg
    assert loop._runtime_factory is not None and loop._engine_factory is not None
    assert not hasattr(loop, "_character_engine")   # B1: mixin 改名 _engine
```

（`_make_loop` 加 `runtime_registry=None`/`runtime_factory=None`/`engine_factory=None` 透传。）

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/cognitive/tests/test_loop.py -q` → FAIL（loop 无 runtime_registry 参数）
- [ ] **Step 3: 实现**（`core/cognitive/loop.py` `__init__` 加参数 + 赋值）：

```python
# __init__ 参数加
runtime_registry: "CharacterRuntimeRegistry | None" = None,
runtime_factory: "Callable[[str], CharacterRuntime] | None" = None,
engine_factory: "Callable[[CharacterRuntime], CharacterEngine] | None" = None,
# body 加
self._registry = runtime_registry
self._runtime_factory = runtime_factory
self._engine_factory = engine_factory
self._engine = character_engine   # B1: _character_engine → _engine
```

（`loop.py`/`streaming.py`/`finalize.py` 的 `self._character_engine` 全改 `self._engine`；`streaming.py:81`/`finalize.py:39` class 注解 `_character_engine`→`_engine`。）

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/cognitive/tests/test_loop.py -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia ruff check core/cognitive/loop.py core/cognitive/streaming.py core/cognitive/finalize.py --exclude __pycache__; conda run -n Elysia pyright core/cognitive/loop.py` → clean / 0 errors；`git add core/cognitive/loop.py core/cognitive/streaming.py core/cognitive/finalize.py core/cognitive/tests/test_loop.py && git commit -m "feat(stage-e): loop 注入 runtime_registry/runtime_factory/engine_factory; _character_engine → _engine"`

---

### Task 8: _prepare_turn 重构（binding→registry.get→runtime.engine）

**Files:** Modify `core/cognitive/loop.py`（`_prepare_turn`）；Test `core/cognitive/tests/test_loop.py`
- Consumes: `TurnCharacterContext`（Task 5，扩展字段）、去 attach_session（Task 6）、loop 注入（Task 7）、prompt `set_personality_engine`/`set_character`（**Task 9**, 修正 review 第 3 轮——`_prepare_turn` 调它们, Task 8 依赖 Task 9）。
- Produces: `_prepare_turn` 从 binding 解析 instanceId→`registry.get(instance_id, self._runtime_factory)`（**同步，get 非 async**）→`runtime.ensure_engine(lambda: self._engine_factory(runtime))`→`self._engine`；`DEFAULT_INSTANCE_ID`/`DEFAULT_CHARACTER_ID`。
- **⚠️ 现有 test_loop.py 的 4 个 `test_prepare_turn_*` 测试需更新**（review 第 3 轮）：它们现用 `_make_loop(engine=_make_engine())` 不传 runtime_registry——Task 8 后 `_prepare_turn` 用 `self._registry.get(...)`, registry=None → AttributeError。须改为 `_make_loop(runtime_registry=reg, runtime_factory=..., engine_factory=...)`（复用 Task 7 已透传的参数）。Step 4 跑全 `test_loop.py` 需这 4 个旧测试也更新。

- [ ] **Step 1: 写失败测试 + 更新 4 个现有 `test_prepare_turn_*`**（`test_loop.py`）

```python
# core/cognitive/tests/test_loop.py (追加)
import pytest
from unittest.mock import AsyncMock
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity
from core.memory.session import SessionMemory
from core.schema.types import SessionId

def _make_rt():
    return CharacterRuntime(
        instance=CharacterInstance(instanceId="chi_builtin_elysia", characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )

@pytest.mark.asyncio
async def test_prepare_turn_resolves_from_binding():
    reg = CharacterRuntimeRegistry()
    rt = _make_rt(); rt.ensure_engine(lambda: MagicMock())   # 预置 engine
    reg.get("chi_builtin_elysia", lambda: rt)
    loop = _make_loop(runtime_registry=reg)
    sm = SessionMemory(session_id=SessionId("default"))
    sm.character_binding = {"character_id": "chr_builtin_elysia", "instance_id": "chi_builtin_elysia"}
    loop._session_resolver = AsyncMock(return_value=sm)
    loop._session = sm
    setup = await loop._prepare_turn(
        user_message="你好", user_name="妖梦", stt_emotion="", content=None,
        message_id=None, conversation_id=None, protocol_version=1,
    )
    assert setup is not None
    assert loop._engine is rt.engine
    assert loop._turn_context.characterInstanceId == "chi_builtin_elysia"
    await loop._output.close()
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/cognitive/tests/test_loop.py -q` → FAIL（loop 无 _engine/_prepare_turn 旧逻辑）
- [ ] **Step 3: 实现**（`_prepare_turn` 改）：

```python
DEFAULT_INSTANCE_ID = "chi_builtin_elysia"
DEFAULT_CHARACTER_ID = "chr_builtin_elysia"

async def _prepare_turn(self, user_message, user_name, stt_emotion, content,
                        message_id, conversation_id, protocol_version):
    self._generation_ctx = None
    self._generation_completed = False
    self._turn_failed_emitted = False
    try:
        target = await self._session_resolver(conversation_id, protocol_version)
    except SessionNotFoundError:
        logger.warning("会话已删除，拒绝在途消息 conversation_id=%s", conversation_id)
        return None
    gen_conversation_id = conversation_id or target.session_id
    binding = target.character_binding or {}
    instance_id = binding.get("instance_id") or DEFAULT_INSTANCE_ID
    runtime = self._registry.get(instance_id, self._runtime_factory)   # 同步 get, 不 await
    engine = runtime.ensure_engine(lambda: self._engine_factory(runtime))
    self._session = target
    self._prompt.set_session(target)
    self._engine = engine
    self._turn_context = TurnCharacterContext(
        conversationId=gen_conversation_id,
        characterInstanceId=instance_id,
        characterId=runtime.definition.characterId,
        characterRevision=runtime.revision.revision,
        userPronoun=self._resolve_pronouns(),
        memoryScope=instance_id,
        voice=runtime.revision.runtimeDefaults.get("voice", {}),
        displayName=runtime.revision.displayName,
        stateSnapshot=runtime.snapshot_state(),
    )
    self._prompt._turn_context = self._turn_context
    self._prompt.set_personality_engine(engine)
    self._prompt.set_character(runtime.definition.characterId, runtime.revision.displayName)
    if self._tool_adapter is not None:
        self._tool_adapter.tool_context.services["session"] = target
    ... (其余：generation 建档、日志、_set_turn_engine 已删——保留原逻辑)
```

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/cognitive/tests/test_loop.py -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia ruff check core/cognitive/loop.py --exclude __pycache__; conda run -n Elysia pyright core/cognitive/loop.py` → clean / 0 errors；`git add core/cognitive/loop.py core/cognitive/tests/test_loop.py && git commit -m "feat(stage-e): _prepare_turn 从 binding 解析 instanceId → registry.get(instance_id, factory) → runtime.engine; DEFAULT_INSTANCE_ID"`

---

### Task 9: prompt_assembler 加 set_personality_engine/set_character

**Files:** Modify `core/prompt/prompt_assembler.py`；Test `core/prompt/tests/test_prompt_assembler.py`
- Consumes: —（`_prepare_turn` 调用这些方法；用 conftest 的真实 fixture `elysia_config`/`cold_engine_mock`/`mock_long_term`/`mock_session` + `make_assembler()`）。
- Produces: `set_personality_engine(engine)`（设 `self._personality_engine`）；`set_character(characterId, displayName)`（更新 `_personality_section._name` + `_resolve_template` 模板目录路由——spec §6.1/§13 要求，修正 review 第 3 轮 spec-conformance）。

- [ ] **Step 1: 写失败测试**（`test_prompt_assembler.py` 加，用 conftest 真实 fixture）

```python
# core/prompt/tests/test_prompt_assembler.py (追加)
from unittest.mock import MagicMock
from core.prompt.tests.conftest import make_assembler

def test_set_personality_engine(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_personality_engine 设 self._personality_engine。"""
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    a.set_personality_engine(cold_engine_mock)
    assert a._personality_engine is cold_engine_mock

def test_set_character_updates_name(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """set_character 更新 _personality_section._name(修正 review: 用真实 fixture + "Cyrene" 有区分度)。"""
    from unittest.mock import MagicMock
    a = make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    assert a._personality_section._name == "Elysia"   # 初始(elysia_config.name)
    a.set_character("chr_builtin_cyrene", "Cyrene")
    assert a._personality_section._name == "Cyrene"   # 有区分度——set_character 真改 name
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/prompt/tests/test_prompt_assembler.py -q` → FAIL（方法不存在）
- [ ] **Step 3: 实现**（`core/prompt/prompt_assembler.py`）：

```python
def set_personality_engine(self, engine: "CharacterEngine") -> None:
    self._personality_engine = engine

def set_character(self, character_id: str, display_name: str) -> None:
    """更新 C1 name(spec §6.1/§13 主诉求——#9 双角色 C1 "你是 {{ name }}" 不串)。

    review 第 4 轮 re-scope: 本批**只路由 C1 name**(经 _personality_section._name, 渲染时生效)。
    模板目录 c2/c3/c7 的 per-role 路由 defer——_resolve_template 仅在 __init__ 求值,
    set_character 改 _character.name 不重绑已建 section, 故不声称模板目录路由(阶段 F/H 再实现)。
    """
    self._personality_section._name = display_name
```

- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/prompt/tests/ -q` → PASS
- [ ] **Step 5: Commit** — `git add core/prompt/prompt_assembler.py core/prompt/tests/ && git commit -m "feat(stage-e): prompt_assembler 加 set_personality_engine/set_character(含模板目录路由)"`

---

### Task 10: streaming._apply_mood 写 engine.state + F1-b 翻转 + streaming.py:242 改 _engine

**Files:** Modify `core/cognitive/streaming.py`；Test `core/cognitive/tests/test_mood_engine.py`
- Consumes: `self._engine`（Task 7）。
- Produces: `_apply_mood`（streaming.py:60）`state = target_session.character_state if target_session is not None else engine.state` → `state = engine.state`；`_dispatch_stream_event`（streaming.py:242-244）`self._character_engine`→`self._engine`；**翻转** `test_mood_engine.py::test_apply_mood_writes_target_session_state`。

- [ ] **Step 1: 翻转失败测试**（`test_mood_engine.py` 改 `test_apply_mood_writes_target_session_state`）：

```python
def test_apply_mood_writes_engine_state(self):
    """M3: _apply_mood 写 engine.state(实例权威), 不再写 target.character_state(F1-b 翻转, spec §6.3)。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.schema.character import CharacterRuntimeState
    from core.schema.types import SessionId
    from core.cognitive.loop import _apply_mood
    engine = MagicMock()
    engine.state = CharacterRuntimeState(instance_id="chi_x", session_id=SessionId("engine-state"))
    engine.state.mood = "平静"
    target = MagicMock(); target.character_state = MagicMock(); target.character_state.mood = "平静"
    tts_mgr = MagicMock(); output = AsyncMock()
    asyncio.run(_apply_mood("开心|0.8", engine, tts_mgr, output, target))
    assert engine.state.mood == "开心"
    assert engine.state.mood_intensity == 0.8
    assert target.character_state.mood != "开心"   # 不再写 session state
    output.mood.assert_called_once_with("开心", 0.8)
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/cognitive/tests/test_mood_engine.py -q` → FAIL（现写 target.character_state）
- [ ] **Step 3: 实现**（`streaming.py`）：
  - :60 `state = target_session.character_state if target_session is not None else engine.state` → `state = engine.state`（实例权威）。
  - :242 `if self._character_engine:` → `if self._engine:`；:243 `_apply_mood(event.text, self._character_engine, ...)` → `self._engine`。
- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/cognitive/tests/test_mood_engine.py -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia pyright core/cognitive/streaming.py` → 0 errors；`git add core/cognitive/streaming.py core/cognitive/tests/test_mood_engine.py && git commit -m "feat(stage-e): _apply_mood 写 engine.state(实例权威, F1-b 翻转); streaming.py:242 改 self._engine"`

---

### Task 11: 接线（session_manager/session/agent/_lifecycle_persistence + lifecycle flush）

**Files:** Modify `core/memory/session.py`、`core/memory/session_manager.py`、`core/agent/agent.py`、`core/agent/_lifecycle_persistence.py`、`core/agent/lifecycle.py`；Test `core/memory/tests/test_session_manager.py`
- Consumes: `CharacterRuntimeRegistry`（Task 4）、`_engine`（Task 7）、`_prepare_turn`（Task 8）、prompt set_*（Task 9）。
- Produces: 移除 `_pre_save_hook`/`bind_character_engine`/`attach_session`；`agent._runtime_registry` + 默认会话 binding；`agent.character_engine`/`_character_reflector` 解析 instance；lifecycle flush 经 registry。

- [ ] **Step 1: 写失败测试**（`test_session_manager.py` 加，用现有 `_make_manager`(session_manager.py:123)/`FakeEngine`；方法名 `switch`(session_manager.py:340) 非 `switch_active`）：

```python
class FakeEngineM3:
    def __init__(self):
        self.attached = []

@pytest.mark.asyncio
async def test_switch_no_attach():
    m = _make_manager()
    # M3: bind_character_engine 已删, switch 不再 attach_session
    target = await m.switch("conv_eng")
    assert target is not None and m.active_id == "conv_eng"
```

- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/memory/tests/test_session_manager.py -q` → FAIL（M3 前 switch 仍 attach_session）
- [ ] **Step 3: 实现**：
  1. `session.py`：移除 `self._pre_save_hook`（:107）/`save()` 的 `if self._pre_save_hook:`（:300-301）。
  2. `session_manager.py`：移除 `bind_character_engine`（:365-368）/`switch` 里 `attach_session(target)`（:361-362）+ `outgoing._pre_save_hook()`（:358-359）/`:581`。
  3. `agent.py`（A3 修复）：不再 eager 构造全局 `_character_engine`（:206-221 改注册 registry + 默认会话 binding `{"character_id": "chr_builtin_elysia", "instance_id": "chi_builtin_elysia"}`）；`agent.character_engine`（:959-961）→经 `RuntimeRegistry.get(binding.instance_id).engine`；`_character_reflector`（:224）→当前 instance engine.reflector；构造 `PromptAssembler` 传默认 persona engine（**保留**, 快照路径）；构造 `CognitiveLoop` 传 `runtime_registry`/`runtime_factory`/`engine_factory`。
  4. `_lifecycle_persistence.py`：`_start_persistence` 为默认 instance 读 `CharacterInstance.state` 装载；shutdown `close_all` flush；`:124-126` 从默认 instance state 恢复。
  5. `lifecycle.py`：`_flush_reflector`/`_flush_compression`（:246-259）经 `RuntimeRegistry.flush_all()`（不再全局 `_character_engine`）。
- [ ] **Step 4: 跑测试确认 PASS + 回归** — `pytest core/memory/ core/agent/tests/ -q` → PASS
- [ ] **Step 5: ruff + pyright + Elysia snapshot + Commit** — `conda run -n Elysia pyright core/agent/agent.py core/memory/session.py; conda run -n Elysia python -m pytest -k "elysia_snapshot" -q` → 0 errors / 1 passed (18127B)；`git add core/memory/session.py core/memory/session_manager.py core/agent/agent.py core/agent/_lifecycle_persistence.py core/agent/lifecycle.py core/memory/tests/test_session_manager.py && git commit -m "feat(stage-e): 接线(删除 _pre_save_hook/bind_character_engine/attach_session; 默认会话 binding → elysia; agent.character_engine 解析 instance; lifecycle flush 经 registry)"`

---

### Task 12: 读端迁移（ws_handler/debug/archive/debug_routes/character → engine.state）

**Files:** Modify `core/input/ws_handler.py`、`core/debug/info.py`、`core/input/debug_routes/archive.py`、`core/input/debug_routes/character.py`；Test `core/input/tests/test_v2_ws.py`
- Consumes: `agent._runtime_registry`/`agent.character_engine`（instance 解析, Task 11）。
- Produces: 所有 `agent.character_engine.state`/`session.character_state` 读点改 instance `engine.state`。

- [ ] **Step 1: 写失败测试** — `core/input/tests/test_v2_ws.py` 加 mood connect 断言读 instance engine.state。
- [ ] **Step 2: 跑测试确认 FAIL** — `pytest core/input/tests/test_v2_ws.py -k "mood" -q` → FAIL
- [ ] **Step 3: 实现**：
  - `ws_handler.py:235-245`：v2 `agent.character_engine.state` / v1 `default_session.character_state` → `agent._runtime_registry.get(binding.instance_id).engine.state`。
  - `debug/info.py:209/220/230/302/175`：→ 当前 instance `engine.state`。
  - `debug_routes/archive.py:244` → `engine.state.model_dump()`。
  - `debug_routes/character.py:45-98`：`agent.character_engine`→`agent._runtime_registry.get(binding.instance_id).engine`。
- [ ] **Step 4: 跑测试确认 PASS** — `pytest core/input/ core/debug/ -q` → PASS
- [ ] **Step 5: ruff + pyright + Commit** — `conda run -n Elysia pyright core/input/ws_handler.py core/debug/info.py core/input/debug_routes/archive.py core/input/debug_routes/character.py` → 0 errors；`git add core/input/ws_handler.py core/debug/info.py core/input/debug_routes/archive.py core/input/debug_routes/character.py core/input/tests/test_v2_ws.py && git commit -m "feat(stage-e): 读端迁移(ws_handler/debug/archive/debug_routes/character → instance engine.state)"`

---

### Task 13: frontend_contract generation 审计字段 + 契约产物

**Files:** Modify `frontend_contract/ws_message.py`；Test `frontend_contract/tests/test_ws_message.py`；重新生成契约产物
- Consumes: `GenerationContext`（Task 5 已加字段）。
- Produces: 三个 generation 消息（`GenerationStartedMessage`/`GenerationCompletedMessage`/`GenerationFailedMessage`）加 optional `characterId`/`characterInstanceId`/`characterRevision`。

- [ ] **Step 1: 写失败测试** — `test_ws_message.py` 加 audit 字段测试
- [ ] **Step 2: 跑测试确认 FAIL** — `pytest frontend_contract/tests/test_ws_message.py -k "audit" -q` → FAIL
- [ ] **Step 3: 实现** — 三个消息各加：

```python
characterId: Optional[str] = Field(default=None, description="角色 ID（character_profiles=true 后必填）")
characterInstanceId: Optional[str] = Field(default=None, description="角色实例 ID")
characterRevision: Optional[int] = Field(default=None, description="角色修订号")
```

- [ ] **Step 4: 跑测试确认 PASS + 契约产物** — `pytest frontend_contract/tests/test_ws_message.py -q` → PASS；`conda run -n Elysia python -m frontend_contract` → 生成三产物；`conda run -n Elysia python -m frontend_contract --check` → exit 0
- [ ] **Step 5: Commit** — `git add frontend_contract/ws_message.py frontend_contract/tests/test_ws_message.py frontend_contract/contract.schema.json frontend_contract/contract.raw.ts frontend/src/types/contract.generated.ts && git commit -m "feat(stage-e): frontend_contract generation 消息加 optional 角色审计字段 + 契约产物"`

---

### Task 14: README 文档 + 全套验证

**Files:** Modify `core/character/README.md`、`core/cognitive/README.md`、`README.md`

- [ ] **Step 1: 更新 README** — character（RuntimeState/Repo/registry 激活/去 attach_session）、cognitive（_prepare_turn 回合解析/TurnCharacterContext 扩展）
- [ ] **Step 2: 全套回归** — `conda run -n Elysia python -m pytest -k "elysia_snapshot" -q` → 1 passed (18127B)；`conda run -n Elysia python -m pytest -m "unit or service" -q` → 无新增失败；`conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__` → clean；`conda run -n Elysia pyright` → 0 errors
- [ ] **Step 3: Commit** — `git add core/character/README.md core/cognitive/README.md README.md && git commit -m "docs(stage-e): character/cognitive README 补 RuntimeState + 回合解析 + 阶段 E 导航"`
