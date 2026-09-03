# 阶段 C+E：角色域 Runtime + UserIdentity + user_pronoun 正位实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把角色域从「进程启动加载唯一角色、多会话共享、attach_session 换绑 state」提升到「**领域模型 + 多角色运行时容器就绪**（`CharacterDefinition`/`CharacterRevision`/`CharacterInstance` + `CharacterRuntimeRegistry` + `CharacterRuntime` + `TurnCharacterContext`），把错放的 `user_pronoun` 正位到全局唯一 `UserIdentity`（B1：Elysia「她」byte 不变，Cyrene「你」→「她」意图修复）。**本批 M1/M2 阶段 engine 仍为全局单例**（batch-3 现状），多角色运行时靠 `character_binding` 标记 + 领域模型支撑，**M3 才分片为多 instance engine**。Elysia 现网渲染 byte-for-byte 不变；`character_profiles` feature flag 恒 `false`。

**Architecture:** 渐进式——在现有 `character/{name}/config.yaml` + `personality.md` 之上**加一层薄壳**（`CharacterDefinition`/`CharacterRevision`/`CharacterInstance` Pydantic 模型），不重写 manifest v2。`CharacterRuntimeRegistry`/`CharacterRuntime` 作为**多角色运行时容器骨架**（本批不驱动多 instance engine，仅缓存/解析标记）。`TurnCharacterContext` 在 `_prepare_turn()` 冻结一次，持 `userPronoun`（来源 `UserIdentity.pronouns`，B1 全局唯一「她」）。M1/M2 阶段 state 写入**仍走 legacy `session.character_state`**（保 Elysia byte-for-byte）。`attach_session` 保留但收窄为**state 视图换绑**（batch-3 多会话隔离），**不再代表角色身份切换**。

**Tech Stack:** Python 3.12 + Pydantic + PyYAML + pytest（conda Elysia env）。无新依赖。

**Spec:** `docs/superpowers/specs/2026-09-03-character-stage-ce-runtime-domain-design.md`

## Global Constraints

- **Elysia byte-for-byte 不变**：`test_elysia_snapshot_unchanged`（Elysia 渲染==`system_prompt_snapshot_elysia_cf4bc008.txt`）必须保持绿。`elysia_config` 渲染不得改变。**任何改动若导致 Elysia 渲染字节变化即阻断**。
- **Cyrene `user_pronoun` 意图修复**：Cyrene 从 `你`→`她` 是本批**意图修复**（B1），测试断言同步更新——但 Cyrene 的 **C2/C3/C7 内容、speaking_style、wrapper 不得变**（只有 pronouns 变）。
- **M1/M2 阶段 state 权威写入目标不变**：`session.character_state` 仍是权威（`session._pre_save_hook = sync_reflector_to_state` + `session.save()` 落 `character_state`）。`CharacterRuntimeState` 只 backfill + 读对比，**不切换权威**。
- **禁相对导入**：`from core.xxx` / `from Module.xxx`（`make lint` TID252；`__init__.py`/`__main__.py` 豁免）。
- **生产代码函数参数必须类型注解**（`make lint` ANN001/002/003；测试文件豁免）。
- **conda env**：`conda run -n Elysia` 跑测试/pyright。
- **测试 marker**：新测试加 `unit`（纯函数/模型，无 I/O）/`service`（单模块 mock）/`integration`（需 DB）。日常 `make test-quick`（`unit or service`）。
- **README 中文**、代码注释中文（docstring 同理，中英混合可接受）。
- **`ServerInfoFeatures` 严禁超前声明**：`character_profiles` 字段加到契约模型但 build 时**恒 `False`**（能力的如实声明，不超前）。
- **`UserIdentity`（域模型）vs `UserIdentityProfile`（契约 DTO）命名区分**：域模型在 `core/profile`，契约 DTO 在 `frontend_contract`。不混用。
- **新测试必须加 marker**（否则 `make test-quick` 用 `-m "unit or service"` 静默跳过，假绿）：每个新测试文件顶部加 `pytestmark = pytest.mark.unit`（纯模型/无 I/O，如 UserIdentity/runtime/turn_context/migration）或 `pytestmark = pytest.mark.service`（mock 单模块，如 builtin_source/profile_store/registry/loop/agent/prompt_assembler/contract）。文件内所有测试统一该 marker；若个别需要 `slow`/`integration` 标 `@pytest.mark.slow`/`integration` 覆盖。

---

### Task 1: 领域模型（core/profile + core/character）

**Files:**
- Create: `core/profile/__init__.py`
- Create: `core/profile/user_identity.py`（`UserIdentity` Pydantic 模型）
- Create: `core/character/__init__.py`
- Create: `core/character/definition.py`（`CharacterDefinition`）
- Create: `core/character/revision.py`（`CharacterRevision`）
- Create: `core/character/instance.py`（`CharacterInstance`）
- Test: `core/profile/tests/test_user_identity.py`
- Test: `core/character/tests/test_models.py`

**Interfaces:**
- Produces: `core.profile.user_identity.UserIdentity` (pydantic BaseModel, fields per spec §3.1), `core.character.definition.CharacterDefinition` (spec §3.2), `core.character.revision.CharacterRevision` (spec §3.3), `core.character.instance.CharacterInstance` (spec §3.4).

- [ ] **Step 1: Write failing tests for `UserIdentity`**

```python
# core/profile/tests/test_user_identity.py
import pytest
pytestmark = pytest.mark.unit   # 纯模型，无 I/O
from pydantic import ValidationError
from core.profile.user_identity import UserIdentity


def test_user_identity_defaults():
    ui = UserIdentity()
    assert ui.userId == "usr_local_default"
    assert ui.displayName == ""
    assert ui.pronouns == "她"
    assert ui.locale == "zh-CN"
    assert ui.timezone == "Asia/Shanghai"
    assert ui.initialized is False
    assert ui.revision == 1


def test_user_identity_custom():
    ui = UserIdentity(displayName="妖梦", pronouns="她", timezone="Asia/Shanghai")
    assert ui.displayName == "妖梦"
    assert ui.pronouns == "她"


def test_user_identity_revision_validation():
    with pytest.raises(ValidationError):
        UserIdentity(revision=0)
```

- [ ] **Step 2: Run and verify fail**

Run: `conda run -n Elysia python -m pytest core/profile/tests/test_user_identity.py -v`
Expected: FAIL — module/class not found.

- [ ] **Step 3: Implement `UserIdentity`**

```python
# core/profile/user_identity.py
"""UserIdentity — 用户"最小身份"模型。

架构稿决策 #7：『我』的最小身份是角色功能前置依赖。
首版不做完整用户画像，但 Prompt 所需的身份从角色/全局配置移出到 UserIdentity。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso

# 首页本地用户稳定 ID（架构稿 §5.1：首版可为确定性 usr_local_default）
LOCAL_DEFAULT_USER_ID = "usr_local_default"
LOCAL_DEFAULT_PRONOMS = "她"   # B1 决断：全局唯一，非 per-role
LOCAL_DEFAULT_LOCALE = "zh-CN"
LOCAL_DEFAULT_TIMEZONE = "Asia/Shanghai"


class UserIdentity(BaseModel):
    """用户最小身份。"""

    userId: str = LOCAL_DEFAULT_USER_ID
    displayName: str = ""                    # 用户显示名（来源 config.agent.user_name bootstrap）
    preferredAddress: str | None = None      # 希望角色通常如何称呼；空则用 displayName
    pronouns: str = LOCAL_DEFAULT_PRONOMS    # 用户代词，全局唯一，可填 {{ user_pronoun }}（她/他/你）
    locale: str = LOCAL_DEFAULT_LOCALE
    timezone: str = LOCAL_DEFAULT_TIMEZONE
    initialized: bool = False                # 仅 UX 标记（用户是否已确认），不 gate pronouns
    defaultCharacterId: str | None = None    # 新会话默认角色
    revision: int = Field(default=1, ge=1)   # 乐观并发版本，从 1 起
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)

    def touch(self) -> None:
        """更新 updatedAt + 递增 revision（写前调用）。"""
        self.updatedAt = now_iso()
        self.revision += 1
```

- [ ] **Step 4: Run and verify pass**

Run: `conda run -n Elysia python -m pytest core/profile/tests/test_user_identity.py -v`
Expected: PASS.

- [ ] **Step 5: Write + implement `CharacterDefinition`/`CharacterRevision`/`CharacterInstance`**

```python
# core/character/tests/test_models.py
import pytest
pytestmark = pytest.mark.unit   # 纯模型，无 I/O
from pydantic import ValidationError
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance


def test_definition_defaults():
    d = CharacterDefinition(characterId="chr_builtin_elysia")
    assert d.source == "builtin"
    assert d.mutable is False
    assert d.status == "active"
    assert d.currentRevision == 1


def test_revision_defaults():
    r = CharacterRevision(characterId="chr_builtin_elysia", displayName="Elysia")
    assert r.revision == 1
    assert r.schemaVersion == 1
    assert r.promptProfile == {}           # 本批 promptProfile 只承载有源字段，默认空
    assert r.createdBy == "system"


def test_revision_revision_validation():
    with pytest.raises(ValidationError):
        CharacterRevision(characterId="chr", displayName="X", revision=0)


def test_instance_defaults():
    i = CharacterInstance(instanceId="chi_builtin_elysia", characterId="chr_builtin_elysia")
    assert i.revision == 1
    assert i.stateVersion == 1
    assert i.ownerUserId == "usr_local_default"
```

```python
# core/character/definition.py
"""CharacterDefinition — 稳定角色身份（架构稿 §5.2）。不存 Prompt 正文/状态/session。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso


class CharacterDefinition(BaseModel):
    characterId: str                       # 稳定 ID，如 chr_builtin_elysia
    slug: str = ""                         # 文件/检索友好标识
    source: str = "builtin"                # builtin/custom
    mutable: bool = False                  # 内置 false
    status: str = "active"                 # active/archived
    currentRevision: int = Field(default=1, ge=1)
    ownerUserId: str | None = None         # custom 所有者；builtin 为空
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
```

```python
# core/character/revision.py
"""CharacterRevision — 不可变角色设定快照（架构稿 §5.3，精简到当前系统所需字段）。

本批渐进式：promptProfile 只承载有源字段（corePersonality/speakingStyle），
relationshipStyle/emotionStyle/conversationInitiative 本批显式留空（渲染走模板）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso

REVISION_PROMPT_PROFILE_KEYS = ("corePersonality", "relationshipStyle", "emotionStyle",
                              "speakingStyle", "conversationInitiative")


class CharacterRevision(BaseModel):
    characterId: str
    revision: int = Field(default=1, ge=1)
    schemaVersion: int = Field(default=1, ge=1)
    displayName: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    promptProfile: dict[str, object] = Field(default_factory=dict)  # 见 REVISION_PROMPT_PROFILE_KEYS
    runtimeDefaults: dict[str, object] = Field(default_factory=dict)  # 声音/表情/唤醒词/主动触发
    presentation: dict[str, object] = Field(default_factory=dict)     # 头像/Live2D 等引用
    openingMessage: str | None = None
    checksum: str = ""
    compilerVersion: int = Field(default=1, ge=1)
    createdBy: str = "system"              # system/user/migration
    createdAt: str = Field(default_factory=now_iso)
```

```python
# core/character/instance.py
"""CharacterInstance — 用户正在相处并持续演化的角色（架构稿 §5）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso


class CharacterInstance(BaseModel):
    instanceId: str
    characterId: str
    revision: int = Field(default=1, ge=1)   # 创建时 1
    ownerUserId: str = "usr_local_default"   # 拥有者
    stateVersion: int = Field(default=1, ge=1)  # 状态版本，CAS
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
```

- [ ] **Step 6: Run model tests, verify pass**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_models.py core/profile/tests/test_user_identity.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add core/profile core/character
git commit -m "feat(stage-ce): 领域模型 UserIdentity + CharacterDefinition/Revision/Instance"
```

---

### Task 2: BuiltinCharacterSource + CharacterCatalog（只读内置）

**Files:**
- Create: `core/character/builtin_source.py`（`BuiltinCharacterSource`：从 `character/{name}/config.yaml` + personality.md 构建）
- Create: `core/character/catalog.py`（`CharacterCatalog`：列/校验内置角色）
- Test: `core/character/tests/test_builtin_source.py`

**Interfaces:**
- Consumes: Task 1 的 `CharacterDefinition`/`CharacterRevision`/`CharacterInstance`.
- Produces: `core.character.builtin_source.BuiltinCharacterSource`（`load(role_name) -> (Definition, Revision, Instance)`，从 config.yaml + personality.md 构建），`core.character.catalog.CharacterCatalog`（`list() -> list[Definition]`、`get(character_id)`、`validate()`）。

- [ ] **Step 1: Write failing test**

```python
# core/character/tests/test_builtin_source.py
pytestmark = pytest.mark.service   # 从 config.yaml 构建，mock 无 DB
from pathlib import Path
from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog


def test_builtin_source_load_elysia():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    definition, revision, instance = src.load("elysia")
    assert definition.characterId == "chr_builtin_elysia"
    assert definition.mutable is False
    assert revision.displayName == "Elysia"
    # corePersonality 从 personality.md 加载（Elysia L1 以"爱莉希雅"自称，不含字面"Elysia"——断言非空）
    assert revision.promptProfile["corePersonality"]
    assert len(revision.promptProfile["corePersonality"]) > 100
    # speakingStyle 从 config.yaml personality.speaking_style 加载
    assert revision.promptProfile["speakingStyle"]
    # C1/C2/C3 无源字段本批留空
    assert revision.promptProfile.get("relationshipStyle") in (None, "")


def test_builtin_source_load_cyrene():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    definition, revision, instance = src.load("cyrene")
    assert definition.characterId == "chr_builtin_cyrene"
    assert "Cyrene" in revision.promptProfile["corePersonality"]
    assert revision.promptProfile["speakingStyle"]


def test_catalog_lists_builtins():
    src = BuiltinCharacterSource(character_dir=Path("character"))
    catalog = CharacterCatalog(source=src)
    defs = catalog.list()
    ids = {d.characterId for d in defs}
    assert "chr_builtin_elysia" in ids
    assert "chr_builtin_cyrene" in ids
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_builtin_source.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# core/character/builtin_source.py
"""BuiltinCharacterSource — 从现有 character/{name}/config.yaml + personality.md 构建内置角色。

渐进式：不重写 manifest v2，直接复用 CharacterConfig.from_yaml + personality.md。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from paths import get_project_root
from core.config.character_config import CharacterConfig
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance

_BUILTIN_IDS = {
    "elysia": "chr_builtin_elysia",
    "cyrene": "chr_builtin_cyrene",
}


class BuiltinCharacterSource:
    """从 character/<name>/ 构建内置角色的只读来源。"""

    def __init__(self, *, character_dir: Path | None = None):
        self._character_dir = character_dir or (get_project_root() / "character")

    def _config(self, role_name: str) -> CharacterConfig:
        return CharacterConfig.from_yaml(self._character_dir / role_name / "config.yaml")

    def load(self, role_name: str) -> tuple[CharacterDefinition, CharacterRevision, CharacterInstance]:
        cfg = self._config(role_name)
        character_id = self._builtin_id(role_name)
        definition = CharacterDefinition(
            characterId=character_id,
            slug=role_name,
            source="builtin",
            mutable=False,
            status="active",
            currentRevision=1,
        )
        # corePersonality = personality.md 全文；speakingStyle = config.yaml personality.speaking_style
        core_personality = cfg.base_personality
        speaking_style = cfg.speaking_style
        revision = CharacterRevision(
            characterId=character_id,
            revision=1,
            displayName=cfg.name,
            promptProfile={
                "corePersonality": core_personality,
                "speakingStyle": speaking_style,
                # relationshipStyle/emotionStyle/conversationInitiative 本批留空（渲染走模板）
            },
            runtimeDefaults={
                "emotion": {"idleExpressions": cfg.emotion.idle_expressions},
                "wakeWords": cfg.wake_words,
                "proactive": {"enabled": cfg.proactive.enabled,
                              "interval": cfg.proactive.interval,
                              "triggers": cfg.proactive.triggers},
            },
            checksum=hashlib.sha256(core_personality.encode("utf-8")).hexdigest(),
        )
        instance = CharacterInstance(
            instanceId=f"chi_builtin_{role_name}",
            characterId=character_id,
            revision=1,
        )
        return definition, revision, instance

    @staticmethod
    def _builtin_id(role_name: str) -> str:
        return _BUILTIN_IDS.get(role_name, f"chr_builtin_{role_name}")

    def list_names(self) -> list[str]:
        return [d.name for d in self._character_dir.iterdir() if d.is_dir()]
```

```python
# core/character/catalog.py
"""CharacterCatalog — 只读内置角色目录（无 Mongo 也能列出/校验 Elysia、Cyrene）。"""

from __future__ import annotations

from core.character.definition import CharacterDefinition
from core.character.builtin_source import BuiltinCharacterSource


class CharacterCatalog:
    """先只读内置角色；custom 留阶段 D。"""

    def __init__(self, *, source: BuiltinCharacterSource):
        self._source = source

    def list(self) -> list[CharacterDefinition]:
        defs = []
        for name in self._source.list_names():
            try:
                definition, _, _ = self._source.load(name)
                defs.append(definition)
            except FileNotFoundError:
                continue
        return defs

    def get(self, character_id: str) -> CharacterDefinition | None:
        for d in self.list():
            if d.characterId == character_id:
                return d
        return None

    def validate(self) -> list[str]:
        """校验所有内置角色，返回失败项（空=全部有效）。"""
        failures = []
        for name in self._source.list_names():
            try:
                self._source.load(name)
            except Exception as e:
                failures.append(f"{name}: {e}")
        return failures
```

- [ ] **Step 4: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_builtin_source.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/character
git commit -m "feat(stage-ce): BuiltinCharacterSource + CharacterCatalog（只读内置，渐进式复用 config.yaml）"
```

---

### Task 3: UserIdentity bootstrap + CRUD（无 Mongo 内存/JSONL fallback）

**Files:**
- Create: `core/profile/profile_store.py`（`UserIdentityStore`：bootstrap + get/save）
- Test: `core/profile/tests/test_profile_store.py`

**Interfaces:**
- Consumes: Task 1 `UserIdentity`.
- Produces: `core.profile.profile_store.UserIdentityStore`（`bootstrap(user_name: str) -> UserIdentity`、`get() -> UserIdentity`、`save(identity) -> UserIdentity`），无 Mongo 时降级内存 + JSONL fallback（架构稿 §5.1「启动迁移用 config.agent.user_name」）。

- [ ] **Step 1: Write failing test**

```python
# core/profile/tests/test_profile_store.py
pytestmark = pytest.mark.service   # JSONL fallback，无 DB
from pathlib import Path
import pytest
from core.profile.profile_store import UserIdentityStore


def test_bootstrap_creates_from_user_name(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    ui = store.bootstrap(user_name="妖梦")
    assert ui.displayName == "妖梦"
    assert ui.initialized is False
    assert ui.pronouns == "她"   # B1：全局唯一


def test_bootstrap_idempotent(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    store.bootstrap(user_name="妖梦")
    ui2 = store.bootstrap(user_name="妖梦")
    assert ui2.displayName == "妖梦"
    assert ui2.revision == 1   # 二次 bootstrap 不重置


def test_save_touch_increments_revision(tmp_path):
    store = UserIdentityStore(storage_path=str(tmp_path / "profile.json"))
    ui = store.bootstrap(user_name="妖梦")
    saved = store.save(ui)
    assert saved.revision == 2
    assert saved.updatedAt > ui.updatedAt
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/profile/tests/test_profile_store.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/profile/profile_store.py
"""UserIdentityStore — 用户身份持久化（bootstrap + CRUD）。

无 Mongo 时降级内存 + JSONL fallback（架构稿 §5.1：启动用 config.agent.user_name 建
initialized=false 的 bootstrap profile；Mongo profile 就绪后为权威）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.profile.user_identity import UserIdentity, LOCAL_DEFAULT_PRONOMS

logger = logging.getLogger(__name__)


class UserIdentityStore:
    """UserIdentity 存取（本次先内存 + JSONL file，Mongo 集成就绪后换 repo）。"""

    def __init__(self, *, storage_path: str | None = None):
        self._storage_path = Path(storage_path) if storage_path else None
        self._cached: UserIdentity | None = None

    def bootstrap(self, user_name: str) -> UserIdentity:
        """若不存在则从 user_name 建 initialized=false 的 bootstrap profile（幂等）。"""
        if self._cached is not None:
            return self._cached
        if self._storage_path and self._storage_path.exists():
            self._cached = self._load_from_file()
            if self._cached is not None:
                return self._cached
        self._cached = UserIdentity(displayName=user_name, pronouns=LOCAL_DEFAULT_PRONOMS)
        self._persist(self._cached)
        return self._cached

    def get(self) -> UserIdentity | None:
        if self._cached is not None:
            return self._cached
        if self._storage_path and self._storage_path.exists():
            self._cached = self._load_from_file()
        return self._cached

    def save(self, identity: UserIdentity) -> UserIdentity:
        """写前 touch（updatedAt + revision++）。"""
        identity.touch()
        self._cached = identity
        self._persist(identity)
        return identity

    # ---- 持久化 ----

    def _load_from_file(self) -> UserIdentity | None:
        try:
            data = json.loads(self._storage_path.read_text("utf-8"))
            return UserIdentity.model_validate(data)
        except Exception:
            logger.warning("UserIdentity 文件解析失败，重新 bootstrap：%s", self._storage_path)
            return None

    def _persist(self, identity: UserIdentity) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._storage_path.write_text(
            identity.model_dump_json(), encoding="utf-8")
```

- [ ] **Step 4: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/profile/tests/test_profile_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/profile
git commit -m "feat(stage-ce): UserIdentityStore bootstrap + CRUD（内存 + JSONL fallback）"
```

---

### Task 4: CharacterRuntime（装载快照）

**Files:**
- Create: `core/character/runtime.py`（`CharacterRuntime`）
- Test: `core/character/tests/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 `CharacterDefinition`/`CharacterRevision`/`CharacterInstance` + `UserIdentity`.
- Produces: `core.character.runtime.CharacterRuntime`（构造 `(instance, definition, revision, user_identity)`，持有 `state`（CharacterRuntimeState 占位）、`engine`（懒建）、`lock`（asyncio.Lock）、`dirty`、`stateVersion`；`flush()` / `snapshot_state()`）。

- [ ] **Step 1: Write failing test**

```python
# core/character/tests/test_runtime.py
pytestmark = pytest.mark.unit   # 纯容器
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def _make_runtime():
    instance = CharacterInstance(instanceId="chi_test", characterId="chr_test")
    definition = CharacterDefinition(characterId="chr_test")
    revision = CharacterRevision(characterId="chr_test", displayName="Test")
    ui = UserIdentity()
    return CharacterRuntime(instance, definition, revision, ui)


def test_runtime_holds_snapshot():
    rt = _make_runtime()
    assert rt.instance.instanceId == "chi_test"
    assert rt.definition.characterId == "chr_test"
    assert rt.revision.displayName == "Test"
    assert rt.user_identity.pronouns == "她"


def test_runtime_lazy_engine():
    rt = _make_runtime()
    assert rt.engine is None   # 懒建
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_runtime.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/character/runtime.py
"""CharacterRuntime — 单个角色实例的运行时容器（架构稿 §7.1/§7.2）。

装载 Definition/Revision 快照 + UserIdentity 快照 + CharacterRuntimeState，
持有 per-instance Lock + dirty/stateVersion。
engine 懒建（M1/M2：不绑 runtime，engine 仍全局单例；「每 instance 一个 engine、
去掉 attach_session」是 M3 语义）。M1/M2 阶段 state 写入仍走
session.character_state（本 runtime 的 state 只做快照/读对比，不切换权威）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance
from core.profile.user_identity import UserIdentity

if TYPE_CHECKING:
    from core.personality.engine import CharacterEngine

# CharacterRuntimeState：本批占位（M1 阶段权威仍是 session.character_state）
# 架构稿 §7 / §15.4：backfill + 读对比（不启用），M3 才切换权威。


class CharacterRuntime:
    """单实例运行时容器。"""

    def __init__(
        self,
        instance: CharacterInstance,
        definition: CharacterDefinition,
        revision: CharacterRevision,
        user_identity: UserIdentity,
    ):
        self.instance = instance
        self.definition = definition
        self.revision = revision
        self.user_identity = user_identity
        self._engine: "CharacterEngine | None" = None
        self.lock = asyncio.Lock()
        self.dirty = False
        self.stateVersion = instance.stateVersion

    @property
    def engine(self) -> "CharacterEngine | None":
        return self._engine

    def bind_engine(self, engine: "CharacterEngine") -> None:
        self._engine = engine

    def snapshot_state(self) -> dict:
        """返回运行时状态快照（本批占位，M1 权威在 session.character_state）。"""
        return {
            "instanceId": self.instance.instanceId,
            "stateVersion": self.stateVersion,
            "dirty": self.dirty,
        }

    async def flush(self) -> None:
        # M1 阶段：state 权威仍在 session.character_state，runtime 不写独立 state。
        # 仅标记 dirty=False（真实 flush 逻辑留 M3）。
        self.dirty = False
```

- [ ] **Step 4: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/character
git commit -m "feat(stage-ce): CharacterRuntime 容器（快照 + 懒 engine + lock/dirty）"
```

---

### Task 5: CharacterRuntimeRegistry（get/invalidate/flush/close_all + M2 evict 耦合）

**Files:**
- Create: `core/character/registry.py`（`CharacterRuntimeRegistry`）
- Test: `core/character/tests/test_registry.py`

**Interfaces:**
- Consumes: Task 4 `CharacterRuntime`.
- Produces: `core.character.registry.CharacterRuntimeRegistry`（`get(instance_id, factory) -> CharacterRuntime`、`invalidate(instance_id, reason)`、`flush(instance_id)`、`flush_all()`、`close_all()`、`release(instance_id)`（M2：session 被驱逐时释放绑定））。LRU 缓存，驱逐前 flush dirty，生成中/反思中不得驱逐。

- [ ] **Step 1: Write failing test**

```python
# core/character/tests/test_registry.py
pytestmark = pytest.mark.service   # 内存 LRU
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def _factory(instance_id):
    def _make():
        instance = CharacterInstance(instanceId=instance_id, characterId="chr_test")
        definition = CharacterDefinition(characterId="chr_test")
        revision = CharacterRevision(characterId="chr_test", displayName="Test")
        ui = UserIdentity()
        return CharacterRuntime(instance, definition, revision, ui)
    return _make


def test_registry_get_caches():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_1", _factory("chi_1"))
    assert rt.instance.instanceId == "chi_1"
    rt2 = reg.get("chi_1", _factory("chi_1"))
    assert rt2 is rt   # 缓存命中


def test_registry_invalidate():
    reg = CharacterRuntimeRegistry()
    rt = reg.get("chi_1", _factory("chi_1"))
    reg.invalidate("chi_1", "test")
    rt2 = reg.get("chi_1", _factory("chi_1"))
    assert rt2 is not rt


def test_registry_release_evicts():
    reg = CharacterRuntimeRegistry()
    reg.get("chi_1", _factory("chi_1"))
    reg.release("chi_1", "session_evicted")
    assert reg.get("chi_1", _factory("chi_1")) is not None  # 释放后可重建，但缓存清掉了
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_registry.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/character/registry.py
"""CharacterRuntimeRegistry — 按 instance_id 解析 runtime（架构稿 §7.1）。

LRU 缓存；驱逐前必须 flush dirty；生成/反思/压缩中的 runtime 不得驱逐；
M2：被任一缓存 session 绑定的 instance 的 runtime 不得驱逐——session 被
SessionManager.evict_if_needed() 逐出后才 release()。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Callable

from core.character.runtime import CharacterRuntime

logger = logging.getLogger(__name__)


class CharacterRuntimeRegistry:
    def __init__(self, *, max_size: int = 32):
        self._max_size = max_size
        self._runtimes: OrderedDict[str, CharacterRuntime] = OrderedDict()

    def get(self, instance_id: str, factory: Callable[[], CharacterRuntime]) -> CharacterRuntime:
        if instance_id in self._runtimes:
            self._runtimes.move_to_end(instance_id)
            return self._runtimes[instance_id]
        rt = factory()
        self._runtimes[instance_id] = rt
        self._evict_if_needed()
        return rt

    def invalidate(self, instance_id: str, reason: str) -> None:
        if instance_id in self._runtimes:
            self._runtimes.pop(instance_id, None)
            logger.debug("runtime invalidated: %s (%s)", instance_id, reason)

    def release(self, instance_id: str, reason: str) -> None:
        """M2：session 被逐出时释放其绑定 instance 的 runtime（先 flush 再清）。"""
        import asyncio
        rt = self._runtimes.pop(instance_id, None)
        if rt is not None:
            try:
                asyncio.get_running_loop().create_task(rt.flush())
            except RuntimeError:
                pass
            logger.debug("runtime released: %s (%s)", instance_id, reason)

    def flush(self, instance_id: str) -> None:
        rt = self._runtimes.get(instance_id)
        if rt is not None:
            rt.dirty = False

    def flush_all(self) -> None:
        for rt in self._runtimes.values():
            rt.dirty = False

    async def close_all(self) -> None:
        """关机 flush 全部（A1：shutdown 走这里，非单 engine）。"""
        for rt in self._runtimes.values():
            await rt.flush()
        self._runtimes.clear()

    def __len__(self) -> int:
        return len(self._runtimes)

    def _evict_if_needed(self) -> None:
        """LRU 驱逐，但受保护（CORRECTNESS #7）：dirty / busy / 仍被 session 绑定的不驱逐。"""
        while len(self._runtimes) > self._max_size:
            evicted: str | None = None
            for instance_id, rt in list(self._runtimes.items()):
                if rt.dirty:
                    continue            # 未 flush 不驱逐
                if getattr(rt, "_busy", False):
                    continue            # 生成/反思/压缩中不驱逐（_busy 为 M3 预留——本批 runtime 是 skeleton，无代码设置它，保护 inert 但无害）
                if self._is_bound_by_live_session(instance_id):
                    continue            # M2：仍被缓存 session 绑定不驱逐
                evicted = instance_id
                break
            if evicted is None:
                break                   # 全受保护，无法驱逐
            self._runtimes.pop(evicted, None)

    def _is_bound_by_live_session(self, instance_id: str) -> bool:
        """M2：该 instance 是否仍被 SessionManager 缓存池中的 session 引用。

        本批 M1/M2 骨架——由 SessionManager 注入 binding 索引；无索引时返回 False。
        """
        idx = getattr(self, "_session_binding_index", None)
        if idx is None:
            return False
        return instance_id in idx
```

- [ ] **Step 4: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/character
git commit -m "feat(stage-ce): CharacterRuntimeRegistry（LRU + invalidate/release/close_all + M2 耦合）"
```

---

### Task 6: CharacterEngine 携带 instance 身份 + TurnCharacterContext（M1/M2 骨架）

**Files:**
- Modify: `core/personality/engine.py`（构造加 `instance_id`/`character_revision`；`attach_session` 加 docstring 标注 M1/M2 收窄）
- Create: `core/cognitive/turn_context.py`（`TurnCharacterContext`）
- Test: `core/cognitive/tests/test_turn_context.py` + `core/personality/tests/test_engine.py`（补 instance 标记断言）

**Interfaces:**
- Consumes: Task 1 领域模型（`TurnCharacterContext` 引 `characterId` 等字段概念）。
- Produces: `core.cognitive.turn_context.TurnCharacterContext`（frozen dataclass：`conversationId`/`characterInstanceId`/`characterId`/`characterRevision`/`userPronoun`）；`CharacterEngine` 构造可传 `instance_id`/`character_revision`（**本批 M1/M2 不绑定 CharacterRuntime**——engine 仍全局单例，仅记录 instance 身份标记）。

> ⚠️ **本批 M1/M2 范围**：engine **仍是全局单例**（batch-3 现状），`attach_session` 保留为 **state 视图换绑 shim**。本 Task 只做「engine 构造携带 instance 身份标记 + TurnCharacterContext 冻结」——为 M3 的多 instance engine 铺路，但不实现多 instance。`instance-scoped`/「每 instance 一个 engine」是 M3 语义，**不在本批**。

- [ ] **Step 1: Write failing test for `TurnCharacterContext`**

```python
# core/cognitive/tests/test_turn_context.py
pytestmark = pytest.mark.unit   # 纯 dataclass
from dataclasses import FrozenInstanceError
import pytest
from core.cognitive.turn_context import TurnCharacterContext


def test_context_is_frozen():
    ctx = TurnCharacterContext(
        conversationId="conv_1", characterInstanceId="chi_1",
        characterId="chr_elysia", characterRevision=1, userPronoun="她",
    )
    assert ctx.userPronoun == "她"
    with pytest.raises(FrozenInstanceError):
        ctx.userPronoun = "你"
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_turn_context.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/cognitive/turn_context.py
"""TurnCharacterContext — 单个生成回合的不可变角色/用户/状态/版本快照。

架构稿 §7.3：_prepare_turn() 一次性解析并冻结。此后 Prompt/TTS/mood/记忆/generation
全用同一 context，不能在回合中重新读取『当前活动角色』。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnCharacterContext:
    """不可变回合上下文。"""

    conversationId: str
    characterInstanceId: str
    characterId: str
    characterRevision: int
    userPronoun: str   # 来自 UserIdentity.pronouns（B1 全局唯一「她」）
```

- [ ] **Step 4: Modify `engine.py` — 构造带 instance 身份标记**

在 `CharacterEngine.__init__` 增加可选参数（默认 None，兼容旧构造）：

```python
# engine.py __init__（在 base_personality 后）
instance_id: str | None = None,
character_revision: int = 1,
```
设置 `self._instance_id = instance_id` / `self._character_revision = character_revision`。
`attach_session` 加 docstring：

```python
def attach_session(self, session: "SessionMemory") -> None:
    """会话切换时重绑 state 视图（M1/M2 阶段）。

    本批（M1/M2）保留为 state 视图换绑 shim——batch-3 多会话 state 隔离依赖它。
    角色身份（instance_id/character_revision）由构造时绑定，attach_session 不再代表
    角色身份切换。完整删除并改由多 instance engine 替代，是 M3 语义。
    """
```

- [ ] **Step 5: Add instance-identity test to `test_engine.py`**

```python
# core/personality/tests/test_engine.py 新增
def test_engine_instance_identity():
    engine = CharacterEngine(
        llm=llm, state=state, character_name="Elysia", user_name="妖梦",
        base_personality="...", character_config=cfg,
        output=output, instance_id="chi_builtin_elysia", character_revision=1,
    )
    assert engine._instance_id == "chi_builtin_elysia"
    assert engine._character_revision == 1
```

- [ ] **Step 6: Run, verify pass + no regression**

Run: `conda run -n Elysia python -m pytest core/personality/tests/test_engine.py core/cognitive/tests/test_turn_context.py -v`
Expected: PASS (test_engine.py 现有 attach_session 测试仍绿——因 shim 保留)。

- [ ] **Step 7: Commit**

```bash
git add core/personality/engine.py core/cognitive/turn_context.py core/personality/tests/test_engine.py core/cognitive/tests/test_turn_context.py
git commit -m "feat(stage-ce): TurnCharacterContext + CharacterEngine instance 身份标记（attach_session 收窄为 state 换绑）"
```

---

### Task 7: B1 下游接线 + _prepare_turn 冻结 TurnCharacterContext（B1 闭环）

**Files:**
- Modify: `core/agent/agent.py`（注入 `UserIdentityStore`；engine 构造传 instance 身份）
- Modify: `core/cognitive/loop.py:340`（`_prepare_turn` 建 `TurnCharacterContext` + 传给 `prompt._turn_context`；读 `UserIdentityStore` 取 `userPronoun`）
- Modify: `core/prompt/prompt_assembler.py`（加 `self._turn_context` 属性）
- Test: `core/cognitive/tests/test_loop.py`（补 `_turn_context` 冻结断言）
- Test: `core/agent/tests/test_agent.py`（补 UserIdentityStore 注入断言）

**Interfaces:**
- Consumes: Task 3 `UserIdentityStore`；Task 6 `TurnCharacterContext`。
- Produces: `_prepare_turn` 设置 `self._turn_context` **并** `self._prompt._turn_context = self._turn_context`（B1 闭环关键）；`agent` 持 `self._user_identity_store`（从 `UserIdentityStore` bootstrap，读 `pronouns`/`displayName`）。

> ⚠️ **B1 依赖链（本 Task 的闭环）**：`UserIdentityStore`（Task 3）→ `agent` 注入 → `_prepare_turn` 读 store 的 `pronouns`（B1 全局唯一「她」）→ 设 `self._turn_context.userPronoun` → **传给 `prompt._turn_context`** → `build_system_prompt` 读 `prompt._turn_context.userPronoun`。**缺任一环，Cyrene 仍渲染「你」**。本 Task 补齐前两环，Task 8 补 prompt 读。

- [ ] **Step 1: `PromptAssembler` 加 `_turn_context` 属性**

```python
# prompt_assembler.py __init__ 加
self._turn_context: TurnCharacterContext | None = None
```
Type hint: `from core.cognitive.turn_context import TurnCharacterContext`（函数级或 TYPE_CHECKING 引）。

- [ ] **Step 2: `agent.py` 注入 UserIdentityStore + engine 传 instance 身份**

```python
# agent.py 构造段（_user_name 设置后）
from core.profile.profile_store import UserIdentityStore
self._user_identity_store = UserIdentityStore()
self._user_identity = self._user_identity_store.bootstrap(user_name=self._user_name)

# engine 构造传 instance 身份
self._character_engine = CharacterEngine(
    llm=self._llm,
    state=self._session_manager.active.character_state,
    character_name=self._character.name,
    user_name=self._user_name,
    base_personality=self._character.base_personality,
    character_config=self._character,
    output=self._output,
    version_repo=self._version_repo,
    instance_id="chi_builtin_" + self._character.name.lower(),
    character_revision=1,
)
```

- [ ] **Step 3: `_prepare_turn` 建 TurnCharacterContext + 传给 prompt**

在 `loop.py` 的 `_prepare_turn`（`attach_session(target)` 之后，`self._prompt.set_session(target)` 附近）：

```python
# loop.py 先加：从 agent 传入的 UserIdentityStore 读 pronouns + 构建 context
# CognitiveLoop 需新增对 _user_identity_store 的引用（由 agent.configure 或构造传入）
self._turn_context = TurnCharacterContext(
    conversationId=gen_conversation_id,
    characterInstanceId=(
        self._character_engine._instance_id
        if self._character_engine is not None else "chi_default"
    ),
    characterId=self._character_engine._character.name.lower() if self._character_engine else "agent",
    characterRevision=self._character_engine._character_revision if self._character_engine else 1,
    userPronoun=self._resolve_pronouns(),  # 读 UserIdentityStore.get().pronouns（B1 全局唯一「她」）
)
# B1 闭环：传给 prompt
self._prompt._turn_context = self._turn_context
```

> ⚠️ **C（M1/M2 inert）**：`characterInstanceId` 取 `_character_engine._instance_id`（恒 `chi_builtin_elysia`，agent Step 2 默认）。
> 对 **legacy session**（Task 10 写入 `chi_legacy_*` binding）该 context 的 `characterInstanceId` 与 session 的 binding 不匹配——
> 但 M1/M2 是单例 engine + 标记，此字段本批不参与解析（仅冻结存储），故 inert、可接受。M3 改为从 session.character_binding 读。**本批不读 binding 解析 instance**（避免与 Task 10 时序耦合）。

`_resolve_pronouns()` helper（loop.py 新增）：

```python
def _resolve_pronouns(self) -> str:
    """读 UserIdentity.pronouns（B1 全局唯一「她」）。无 store 时兜底「她」。"""
    store = getattr(self, "_user_identity_store", None)
    if store is not None:
        identity = store.get()
        if identity is not None and identity.pronouns:
            return identity.pronouns
    return "她"
```

> **注**：`CognitiveLoop` 需接收 `_user_identity_store`（构造/configure 传入，见 Task 7 Step 4 agent 侧注册）。`_character_engine._instance_id`/`_character_revision`/`_character.name` 在 Task 6 已加到 engine。

- [ ] **Step 4: `agent` 把 store 传给 loop**

```python
# agent.py CognitiveLoop 构造/configure
self._cognitive.configure(
    memory_reflector=self._memory_reflector,
    user_identity_store=self._user_identity_store,  # 新增
    ...
)
```
`CognitiveLoop.configure` 增加 `user_identity_store` 可选参数，存 `self._user_identity_store`。

- [ ] **Step 5: Add/modify test**

```python
# core/cognitive/tests/test_loop.py 新增
def test_prepare_turn_freeze_context():
    # mock session + engine → _prepare_turn 后 self._turn_context 非 None
    # 且 prompt._turn_context == self._turn_context
    ...
    assert loop._turn_context.userPronoun == "她"
    assert loop._prompt._turn_context is loop._turn_context
```

```python
# core/agent/tests/test_agent.py 新增
def test_agent_injects_user_identity_store():
    assert agent._user_identity is not None
    assert agent._user_identity_store.get().pronouns == "她"
```

- [ ] **Step 6: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_loop.py core/agent/tests/test_agent.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add core/agent/agent.py core/cognitive/loop.py core/prompt/prompt_assembler.py
git commit -m "feat(stage-ce): B1 下游接线——_prepare_turn 冻结 TurnCharacterContext（userPronoun 来自 UserIdentity）+ 传给 prompt"
```

---

### Task 8: user_pronoun 正位（PromptAssembler 读 TurnCharacterContext）

**Files:**
- Modify: `core/prompt/prompt_assembler.py:152`（`user_pronoun=self._character.user_pronoun` → 读 context）
- Modify: `core/config/character_config.py`（`user_pronoun` 加 deprecated 标记）
- Test: `core/prompt/tests/test_prompt_assembler.py`（补 user_pronoun 来自 context / 兜底"她"）
- Run（B1 连带）: 重生成 `core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt`（cold 反例角色 user_pronoun 全局「她」）

**Interfaces:**
- Consumes: Task 6 `TurnCharacterContext`（新增 `userPronoun` 字段需在 task 6 已含）.
- Produces: `build_system_prompt` 优先读 `self._turn_context.userPronoun`，否则**兜底常量「她」**（spec §5.2「no per-role fallback」，B1 决断）——**不回退 `self._character.user_pronoun`**。

- [ ] **Step 1: Update `PromptAssembler` 数据源**

```python
# prompt_assembler.py:152 改（B1 正位：全局唯一「她」，不 per-role 回退）
user_pronoun = (
    self._turn_context.userPronoun
    if self._turn_context is not None and self._turn_context.userPronoun
    else "她"   # 兜底常量（spec §5.2），不回退 self._character.user_pronoun
)
self._manager.set_context(..., user_pronoun=user_pronoun, ...)
```
新增属性 `self._turn_context: TurnCharacterContext | None = None`（`_prepare_turn` 设置）。

> ⚠️ **为什么兜底「她」而非 per-role**：spec §5.2（B1 决断）明确「不再保留 per-role 回退」。回退到 `_character.user_pronoun` 会让 Elysia（她）碰巧对、但 cold_config/Cyrene（你）在无 turn_context 时漏成「你」——违背「pronouns 全局唯一」。兜底「她」一致：无 turn_context 时 Elysia 快照（她→她）byte 不变，cold 也恒「她」。
> ⚠️ **Elysia byte-for-byte 守门**：Elysia `_character.user_pronoun="她"` 且无 turn_context（快照测试路径）→ 兜底「她」与 _character 一致 → byte 不变。`test_elysia_snapshot_unchanged` 必须仍绿。

- [ ] **Step 2: Update `CharacterConfig.user_pronoun` deprecated 标记**

```python
# character_config.py:29 加注释
user_pronoun: str = "她"   # DEPRECATED（阶段 C+E）：已被 UserIdentity.pronouns 权威替代（B1）；
                          # 保留仅供测试夹具构造，阶段 D 删除。
```

- [ ] **Step 3: Add functional test：turn_context.userPronoun 覆盖 per-role**

```python
# test_prompt_assembler.py 新增（CORRECTNESS #10：断言敏感 section，非泛泛"她"）
def test_user_pronoun_from_turn_context_overrides(make_assembler, cold_config,
                                                   cold_engine_mock, mock_long_term,
                                                   mock_session):
    """_turn_context.userPronoun=她 时，即使 _character.user_pronoun=你 也渲染她。"""
    # cold_config.user_pronoun="你"；context.userPronoun="她"
    assembler = make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session)
    assembler._turn_context = TurnCharacterContext(
        conversationId="c", characterInstanceId="chi", characterId="chr",
        characterRevision=1, userPronoun="她",
    )
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="xx")
    # cold 走共享 C2（Elysia 原文），句式 "此刻{{ user_pronoun }}只注视着我" → context=她 时渲染
    # "此刻她只注视着我"（若 cold_config.user_pronoun=你 泄漏则 "此刻你只注视着我"）
    assert "此刻她只注视着我" in rendered
    assert "此刻你只注视着我" not in rendered
```

```python
# test_prompt_assembler.py 新增：无 turn_context 时兜底"她"
def test_user_pronoun_no_context_fallback_she(make_assembler, cold_config, cold_engine_mock,
                                              mock_long_term, mock_session):
    # 无 _turn_context → 兜底"她"（不是 cold_config.user_pronoun="你"）
    assembler = make_assembler(cold_config, cold_engine_mock, mock_long_term, mock_session)
    rendered = await assembler.build_system_prompt(user_message="hi", user_name="xx")
    assert "此刻她只注视着我" in rendered
    assert "此刻你只注视着我" not in rendered
```

- [ ] **Step 4: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/prompt/tests/test_prompt_assembler.py core/prompt/tests/test_system_prompt_leak.py core/prompt/tests/test_system_prompt_snapshot.py -v`
Expected: PASS. **关键**：`test_elysia_snapshot_unchanged`（Elysia 无 turn_context → 兜底「她」，与 `_character.user_pronoun="她"` byte 不变）必须仍绿；`test_snapshot_matches_current`（cold config user_pronoun="你"）——⚠️ 兜底「她」会使 cold 快照里「此刻你」变「此刻她」→ **需同步重生成 cold snapshot**（cold 快照是 B-2 已定的，改兜底后 cold 整体变「她」，快照要更新）。

> ⚠️ **cold 快照影响**：`system_prompt_snapshot_e793a782.txt`（cold 渲染）含 cold_config.user_pronoun="你" → 兜底改「她」后 cold 渲染变。**必须重生成 cold 快照**（B-2 的「cold 回退共享 C2=Elysia 原文」断言保留，但 user_pronoun 全局「她」后那几处「你」变「她」）。此为 B1 意图修复的连带——cold 是反例角色，其 user_pronoun 也应随全局正位改「她」。**重生成 cold snapshot 是本 Task 的一部分。**

- [ ] **Step 5: Commit**

```bash
git add core/prompt/prompt_assembler.py core/config/character_config.py core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt
git commit -m "refactor(stage-ce): PromptAssembler user_pronoun 读 TurnCharacterContext（B1 正位，兜底她 非 per-role）+ 重生成 cold snapshot"
```

---

### Task 9: 契约层 DTO + feature flag false + flag-off 序列化抑制

**Files:**
- Modify: `frontend_contract/ws_message.py`（`ServerInfoFeatures` 加 `character_profiles`）
- Modify: `frontend_contract/rest.py`（新增 `UserIdentityProfile`/`CharacterSummary`/`CharacterInstanceSummary`/`CharacterPublicState`）
- Modify: `frontend_contract/base.py`（若需 flag-off 序列化抑制机制）
- Modify: `core/services/capabilities.py:98`（`character_profiles=False`）
- Test: `frontend_contract/tests/test_stage_ce_contract.py`
- Run: `make gen-types` + `make check-types`

**Interfaces:**
- Consumes: Task 1 域模型（字段对齐）.
- Produces: `frontend_contract` 新增契约模型（`UserIdentityProfile`/`CharacterSummary`/`CharacterInstanceSummary`/`CharacterPublicState`）+ `gen_ts`/`gen_schema`/`gen_raw_ts` 产物更新；`character_profiles=False` 恒 false；**flag-off 序列化抑制**（`character_profiles=false` 时 CharacterPublicState 等不进 wire，仅存 schema/golden 供离线对拍）。

- [ ] **Step 1: Write contract test**

```python
# frontend_contract/tests/test_stage_ce_contract.py
from frontend_contract.ws_message import ServerInfoFeatures
from frontend_contract.rest import UserIdentityProfile  # 若在 rest.py


def test_server_info_character_profiles_false():
    f = ServerInfoFeatures(character_profiles=False)
    assert f.character_profiles is False


def test_user_identity_profile_dto():
    dto = UserIdentityProfile(
        userId="usr_local_default", displayName="妖梦", pronouns="她", initialized=False)
    assert dto.pronouns == "她"
```

- [ ] **Step 2: Implement contract models**

```python
# frontend_contract/ws_message.py ServerInfoFeatures 加字段
    character_profiles: bool = Field(default=False, description="多角色 Profile（阶段 C+E；本批次恒 false，架构稿决策 #12）")

# frontend_contract/rest.py 新增
class UserIdentityProfile(ContractModel):
    """用户最小身份（阶段 C+E；GET /api/me / PUT /api/me）。"""
    userId: str = "usr_local_default"
    displayName: str = ""
    preferredAddress: str | None = None
    pronouns: str = "她"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    initialized: bool = False
    defaultCharacterId: str | None = None
    revision: int = 1

class CharacterSummary(ContractModel):
    """内置角色摘要（阶段 C+E，角色列表）。"""
    characterId: str
    displayName: str
    source: str = "builtin"
    status: str = "active"

class CharacterInstanceSummary(ContractModel):
    """角色实例摘要（阶段 C+E，会话绑定展示）。"""
    instanceId: str
    characterId: str
    revision: int = 1
    stateVersion: int = 1

class CharacterPublicState(ContractModel):
    """角色产品态（阶段 C+E/general，feature flag 开启后走 wire）。"""
    characterId: str
    instanceId: str
    characterRevision: int = 1
    mood: str = "平静"
    stateVersion: int = 1
```

> ⚠️ **flag-off 序列化抑制**（架构稿 §12.8）：`character_profiles=false` 时，`CharacterPublicState`/`CharacterInstanceSummary` 等新 DTO **不进实际 wire 序列化**（后端发送点不构造它们，或 `server_info` 不发 character 相关字段）。契约模型本身存在于 schema/golden 中供离线对拍（`make gen-types` 生成），**运行时 `capabilities.build_server_info` 的 `character_profiles=False` 保证 v2 `server_info` 帧不含 character 能力**。本批**不实现** CharacterPublicState 的实际发送（那是阶段 G），仅在契约层建模 + gen-types 产物。

```python
# core/services/capabilities.py:98
features=ServerInfoFeatures(
    ...,
    character_profiles=False,  # 恒 false（决策 #12：四道门禁未全过，不开启）
)
```

- [ ] **Step 3: Regenerate contracts + run check**

```bash
conda run -n Elysia python -m frontend_contract --check  # 若不通过则重新生成
conda run -n Elysia python -m frontend_contract  # 生成 ts/schema/raw_ts
make check-types
```

- [ ] **Step 4: Run contract tests**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_stage_ce_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend_contract core/services/capabilities.py
git commit -m "feat(stage-ce): 契约层 UserIdentityProfile/CharacterSummary DTO + character_profiles=False"
```

---

### Task 10: 旧会话 shadow migration + session character_binding（收尾）

**Files:**
- Modify: `core/memory/session.py`（`character_binding` 字段 + save/load 两侧同步）
- Create: `core/character/migration.py`（旧 session → legacy instance）
- Test: `core/character/tests/test_migration.py`

**Interfaces:**
- Consumes: Task 4 `CharacterRuntime`, Task 1 `CharacterInstance`.
- Produces: `core.character.migration.migrate_legacy_sessions(sessions)`（每旧 session 建确定性 legacy instance，sessionId 哈希；state 原样复制到 instance.state；session 加 character_binding）；session 新增 `character_binding` 字段（save/load 同步，架构稿 §15.3）。

- [ ] **Step 1: Write failing test**

```python
# core/character/tests/test_migration.py
pytestmark = pytest.mark.unit   # 纯哈希函数
from core.character.migration import legacy_instance_id, migrate_legacy_sessions


def test_legacy_instance_id_deterministic():
    id1 = legacy_instance_id("conv_abc")
    id2 = legacy_instance_id("conv_abc")
    assert id1 == id2   # 同 session 同 instance
    assert id1 != legacy_instance_id("conv_def")


def test_legacy_instance_id_from_hash():
    assert legacy_instance_id("conv_abc").startswith("chi_legacy_")
```

- [ ] **Step 2: Run, verify fail**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_migration.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# core/character/migration.py
"""旧会话 CharacterState shadow migration（架构稿 §15.3）。

为每个旧 session 建确定性 legacy instance（sessionId 哈希），state 原样复制到
instance.state；session 加 character_binding 指向 legacy instance；旧会话行为/状态不变。
"""

from __future__ import annotations

import hashlib


def legacy_instance_id(session_id: str) -> str:
    """由 sessionId 哈希生成确定性 legacy instance id。"""
    h = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"chi_legacy_{h}"


def migrate_legacy_sessions(sessions: list) -> list[dict]:
    """为每个旧 session 建 deterministic legacy instance；返回迁移记录。

    Args:
        sessions: SessionMemory 列表（旧会话，character_state 以 session_id 为身份）。

    Returns:
        迁移记录 list[dict]，每项含 session_id / legacy_instance_id / character_id。
    """
    records = []
    for session in sessions:
        sid = session.session_id
        iid = legacy_instance_id(sid)
        # 每旧 session 独立 legacy instance，state 原样（不合并）
        session.character_binding = {"instanceId": iid, "characterId": "legacy"}
        records.append({"session_id": sid, "legacy_instance_id": iid, "character_id": "legacy"})
    return records
```

- [ ] **Step 4: Add `character_binding` to session + 持久化（save/load/repo 两侧）**

```python
# session.py —— dataclass 字段（在 character_state 后）
character_binding: dict | None = None   # 架构稿 §15.3：session 绑定角色实例（M1 只写不权威）
```

**持久化（CORRECTNESS #8：binding 必须落库，否则重启丢失）**——覆盖 JSONL 与 Mongo 两条路径：

```python
# session.py —— 字段（在 character_state 后）
character_binding: dict | None = None   # 架构稿 §15.3：session 绑定角色实例（M1 只写不权威）

# ---- JSONL 路径（Mongo 离线 fallback）----
# session.py save() 的 JSONL doc（:330 附近）加：
"character_binding": self.character_binding or {},
# session.py _load_from_jsonl()（:363 附近）恢复：
self.character_binding = json_doc.get("character_binding") or None
```

```python
# ---- Mongo 路径（生产主路径）----
# 1. 扩展 SessionRepository.save 签名（session_repo.py:112）
async def save(self, session_id, messages, character_state,
               reflector_state=None, service_state=None,
               character_binding=None):   # 新增
    ...
    doc["character_binding"] = character_binding or {}

# 2. session.py:305 调 repo.save 时传 character_binding（否则签名改了但调用不传，不落库）
await self._repo.save(
    self._session_id,
    list(self._messages),
    character_state=self.character_state,
    reflector_state=(...),
    service_state=self.service_state,
    character_binding=self.character_binding,   # 新增
)

# 3. 扩展 SessionRepository.load 返回 5-tuple（session_repo.py:257，原来是 4-tuple (messages, cs, rs, ss)）
async def load(self, session_id) -> tuple[list[Message], CharacterState, MemoryReflectorState, ServiceState, dict] | None:
    ...
    cb_data = doc.get("character_binding") or {}
    return messages, cs, rs, ss, cb_data

# 4. session.py:load() 解构改 5-tuple（:443，原来是 messages, cs, rs, ss = result）
messages, cs, rs, ss, cb = result
self.character_binding = cb or None
```

> ⚠️ **CORRECTNESS #8 关键**（review 二轮确认的残留）：**Mongo `load()` 必须返回 5-tuple 且 `session.py:load()` 对应解构 5-tuple**——不能只加 save 侧。若只改 save 签名不改 load 返回，binding 在 Mongo 路径 write 但不 read，重启丢失。同时 `session.py:305` 的 repo.save 调用、`session.py:443` 的 load 解构，两处调用点都须更新（`repo.load` 的 4-tuple → 5-tuple 是接口变更，所有解构点同步）。
> **注**：新增字段走 `character_state` 同款 save/load/repo 三处同步模式（session.py 字段 + SessionRepository 序列化 + 恢复）。测试用 `SessionMemory` 直构 + save/load 断言 binding 往返（含 Mongo mock 路径）。

- [ ] **Step 5: Run, verify pass**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_migration.py core/memory/tests/test_session.py -v`
Expected: PASS（binding 落库往返 + migration 确定性）。

- [ ] **Step 6: Commit**

```bash
git add core/character/migration.py core/memory/session.py core/storage/session_repo.py
git commit -m "feat(stage-ce): 旧会话 shadow migration（legacy instance）+ session character_binding 持久化"
```

---

## Final Verification (all tasks done)

- [ ] `conda run -n Elysia python -m pytest core/character/tests/ core/profile/tests/ -v` — 领域模型/注册表/migration 全绿
- [ ] `conda run -n Elysia python -m pytest core/prompt/tests/ -v` — **Elysia 快照** + 泄漏四组 + user_pronoun 正位全绿
- [ ] `conda run -n Elysia python -m pytest core/personality/tests/ core/memory/tests/ -v` — engine instance 身份 + attach_session shim（batch-3 隔离）无回归
- [ ] `conda run -n Elysia python -m pytest core/cognitive/tests/ -v` — TurnCharacterContext 冻结 + B1 下游接线无回归
- [ ] `conda run -n Elysia python -m pytest frontend_contract/tests/ -v` — 契约 DTO 全绿
- [ ] `conda run -n Elysia python -m frontend_contract --check` + `make check-types` — 契约漂移门禁过
- [ ] `make lint` — import 风格 + 类型注解全过
- [ ] `conda run -n Elysia make pyright` — 0 errors
- [ ] `conda run -n Elysia python -m pytest -m "unit or service" -q` — 全量 quick 全绿（**注意**：新测试必须带 `unit`/`service` marker，否则 `make test-quick` 静默跳过）
- [ ] **多角色 binding 解析（M1/M2 本批）**：新会话/旧会话 `character_binding` 正确记录绑定实例（默认→Elysia canonical；旧→legacy instance）；**「双角色并行不串」是 M3 语义，本批不做**（M1/M2 单例 engine）。

> ⚠️ **本批范围边界（明确声明）**：
> - **不做「每 instance 一个 engine」**（M3 语义）——本批 engine 全局单例 + instance 身份标记 + `character_binding` 标记。
> - **不做 session create 主动绑定**——M1/M2 由 backfill（迁移）写 `character_binding`；新建会话默认绑定 default/Elysia canonical（Task 10 覆盖标记写入）。
> - **不做 generation/message 角色审计字段**（spec §2 stage E item 11）——本批范围外，阶段 G 做。
> - **不做 CharacterPublicState 实际发送**（阶段 G）——仅契约层建模 + gen-types。

---

## 关键实现风险与防线

| 风险 | 防线 |
|------|------|
| Elysia byte-for-byte 破坏 | `test_elysia_snapshot_unchanged` 全程守门；Task 8 明确 Elysia 无 turn_context 时兜底常量「她」（与 `_character.user_pronoun="她"` byte 不变） |
| batch-3 multi-session state 隔离 | Task 6 `attach_session` 保留为 state 换绑 shim（M1/M2 不删），M3 才改多 instance engine |
| Cyrene/cold `你→她` 连带（B1 正位） | Task 8 更新 leak 断言 + **重生成 cold snapshot**（cold 反例角色 user_pronoun 全局「她」）；Cyrene 仅 pronouns 变，内容不变 |
| `character_profiles` 超前声明 | Task 9 恒 `False`，契约测试断言默认 False |
| B1 下游接线断（prompt 读不到 context） | Task 7 明确 `_prepare_turn` 设 `prompt._turn_context = self._turn_context` 闭环 |
| 记忆 scope（F）混入 | 全程不建 ScopedLongTermMemory 实现，`CharacterRuntime` 仅占位 |
| 契约生成打破 golden | Task 9 `--check` 若红则重新生成三产物 + 更新 golden frames |

---

## 自审（对照 spec 覆盖）

- spec §3.1 UserIdentity → Task 1; §3.2/3.3/3.4 → Task 1; §2 阶段C 1/2 → Task 1/2/3
- spec §2 阶段E 6/7 → Task 4/5; §4.3 engine instance 身份 → Task 6; §7.2 attach_session 调用面 → Task 7（M1/M2 保留 shim）
- spec §2 阶段E 9 TurnCharacterContext → Task 6; B1 下游接线 → Task 7
- spec §5 user_pronoun 正位 → Task 8（兜底常量「她」非 per-role）
- spec §6 契约层（含 CharacterPublicState/flag-off）→ Task 9
- spec §7.2 shadow migration + character_binding 持久化 → Task 10
- spec §7.5 行为不变（Elysia byte 不变 / Cyrene pronouns 修复其余不变）→ Task 8 + Final Verification
- spec §7.2「多角色 binding 解析」（M1/M2 本批）+ M3 边界 → Final Verification 声明
- **明确不在本批**（M3 语义）：多 instance engine、双角色并行不串、generation/message 审计字段、CharacterPublicState 实际发送 —— 已标注
