# 阶段 G:CharacterPublicState 实际发送 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CharacterRuntimeState(内部扁平态)→ CharacterPublicState(产品裁剪视图)的 wire 发射做出来:v2 `character.state.changed` 事件(WS 快照 + 回合边界变化)+ 只读 REST `GET /api/character-instances/{id}/state`;开启 `character_state_stream=true`,保持 `character_profiles=false`(归 H)。

**Architecture:** 契约层(frontend_contract)新增 `MoodState`/`CharacterPublicStateView`/`CharacterStateChangedMessage` + `ServerInfoFeatures.character_state_stream`;core 侧 `make_public_state(runtime)` 映射 RuntimeState→产品态(内部态不外泄),`CharacterRuntime` 维护 `_last_public_state` 变化检测缓存;WS 在连接/激活发 snapshot、回合边界 flush 后按 mood 变化发事件;REST 只读 fail-closed 503。REST 并入 `register_character_routes`(不建新模块),agent 新增 `character_runtime_registry`/`character_state_stream` 公开 property。

**Tech Stack:** Python 3.11+ / pydantic ContractModel / aiohttp / pytest / ruff / pyright。零新依赖。

**Spec:** [`docs/superpowers/specs/2026-09-05-character-stage-g-public-state-wire-design.md`](../specs/2026-09-05-character-stage-g-public-state-wire-design.md)

## Global Constraints

- 禁相对导入(`from .xxx`),一律绝对导入(ruff TID252;`__init__.py`/`__main__.py` 豁免)。
- 生产代码函数参数必须类型注解(ruff ANN001/002/003;测试文件豁免)。
- 代码/docstring 用中文(中英混合可)。
- `frontend_contract` 零依赖(仅 pydantic + stdlib),不 import core/ Module/。
- **Elysia byte-for-byte 不变**:`test_elysia_snapshot_unchanged`(core/prompt/tests/test_system_prompt_snapshot.py,18127B system prompt 渲染快照)必须保持绿;v1 wire 序列(`test_v1_wire_sequence_unchanged`,test_v2_generation.py:247)`character.state.changed` 不出现。
- **Event 只在 `v2 && character_state_stream=true` 发射**:v1 路径零影响。
- `character_profiles` 恒 false(capabilities.py:107 保持)。
- **变化检测只比较 `mood.label`/`mood.intensity`**:不比较 `updated_at`(展示字段)、不比较 `proactiveEnabled`(immutable 配置常量)。
- `lastInteractionAt`/`updatedAt` 从 `runtime.instance.updatedAt` 派生(每 flush 更新,agent.py:432);`mood.updatedAt` 用 `CharacterRuntimeState.updated_at` 近似(frozen,已知限制)。

---

### Task 1: 契约层 — MoodState / CharacterPublicStateView / CharacterStateChangedMessage / ServerInfoFeatures.character_state_stream

**Files:**
- Modify: `frontend_contract/rest.py`(:598 附近,`CharacterPublicState` 之前/之后)
- Modify: `frontend_contract/ws_message.py`(:264-275 `ServerInfoFeatures` + :1040-1083 `ServerMessage` union + :1043 附近)
- Modify: `frontend_contract/__init__.py`(re-export + `__all__`)
- Test: `frontend_contract/tests/test_stage_g_contract.py`(新)

**Interfaces:**
- Produces:
  - `MoodState(label: str, intensity: float, updatedAt: str)`
  - `CharacterPublicStateView(mood: MoodState, currentActivity: Optional[str], lastInteractionAt: Optional[str], proactiveEnabled: bool, updatedAt: str)`
  - `CharacterStateChangedMessage(type=Literal["character.state.changed"], conversationId: Optional[str], characterId: str, characterInstanceId: str, characterRevision: int, stateVersion: int, state: CharacterPublicStateView)`
  - `ServerInfoFeatures.character_state_stream: bool = False`

- [ ] **Step 1: Write the failing contract test**

Create `frontend_contract/tests/test_stage_g_contract.py`:

```python
"""阶段 G:CharacterPublicState 契约(unit)。"""
import pytest
from frontend_contract.ws_message import (
    CharacterStateChangedMessage, ServerInfoFeatures, ServerMessage,
)
from frontend_contract.rest import CharacterPublicStateView, MoodState

pytestmark = pytest.mark.unit


class TestMoodState:
    def test_mood_state_fields(self):
        m = MoodState(label="开心", intensity=0.72, updatedAt="2026-09-05T00:00:00Z")
        assert m.label == "开心" and m.intensity == 0.72


class TestPublicStateView:
    def test_view_fields(self):
        v = CharacterPublicStateView(
            mood=MoodState(label="开心", intensity=0.72, updatedAt="t"),
            currentActivity=None, lastInteractionAt=None,
            proactiveEnabled=False, updatedAt="t")
        assert v.proactiveEnabled is False

    def test_view_wire_dict(self):
        v = CharacterPublicStateView(
            mood=MoodState(label="开心", intensity=0.72, updatedAt="t"),
            proactiveEnabled=False, updatedAt="t")
        d = v.wire_dict()
        assert d["mood"]["label"] == "开心"
        assert "currentActivity" not in d  # None 缺省省略


class TestCharacterStateChangedMessage:
    def test_type_literal(self):
        msg = CharacterStateChangedMessage(
            characterId="chr_elysia", characterInstanceId="chi_builtin_elysia",
            stateVersion=3, state=CharacterPublicStateView(
                mood=MoodState(label="开心", intensity=0.72, updatedAt="t"),
                proactiveEnabled=False, updatedAt="t"))
        assert msg.type == "character.state.changed"

    def test_is_server_message_member(self):
        from typing import get_args, get_origin
        from typing import Annotated, Union
        origin = get_origin(ServerMessage)
        inner = get_args(ServerMessage)[0]
        members = get_args(inner) if get_origin(inner) is Union else [inner]
        assert CharacterStateChangedMessage in members


class TestServerInfoFeaturesCharacterStateStream:
    def test_default_false(self):
        assert ServerInfoFeatures().character_state_stream is False

    def test_explicit_true(self):
        f = ServerInfoFeatures(character_state_stream=True)
        assert f.character_state_stream is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_stage_g_contract.py -x`
Expected: FAIL — `MoodState`/`CharacterPublicStateView`/`CharacterStateChangedMessage`/`character_state_stream` not defined.

- [ ] **Step 3: Implement MoodState + CharacterPublicStateView + CharacterPublicState 泛化 (rest.py)**

In `frontend_contract/rest.py`, add just before `class CharacterPublicState` (:598):

```python
class MoodState(ContractModel):
    """心情产品态(架构稿 §12.4)——CharacterPublicStateView/CharacterStateChangedMessage 共享。"""
    label: str = Field(description="心情标签")
    intensity: float = Field(default=0.5, description="心情强度 0-1")
    updatedAt: str = Field(description="心情更新时间(ISO)")


class CharacterPublicStateView(ContractModel):
    """角色产品态子视图(架构稿 §12.4;REST 与 WS 共享)。

    mood 本批必填(产品态恒有心情,state.mood 默认「平静」);currentActivity/lastInteractionAt
    可缺省(None→wire 省略)。内部态(self_l2/state_l3/reflector_buffer 等)绝不外泄。
    """
    mood: MoodState = Field(description="当前心情")
    currentActivity: Optional[str] = Field(default=None, description="当前活动(可缺省)")
    lastInteractionAt: Optional[str] = Field(default=None, description="最近交互时间(可缺省)")
    proactiveEnabled: bool = Field(default=False, description="主动搭话是否开启")
    updatedAt: str = Field(description="产品态更新时间(ISO)")
```

Then replace the existing `CharacterPublicState` class (:598-605):

```python
class CharacterPublicState(ContractModel):
    """角色产品态(REST GET /api/character-instances/{id}/state)。

    由阶段 C+E 扁平 mood:str + instanceId 泛化而来——flat mood 移除、instanceId 改名
    characterInstanceId(对齐 WS CharacterStateChangedMessage 与架构稿 §12.4)。
    """
    characterId: str = Field(description="角色 ID")
    characterInstanceId: str = Field(description="角色实例 ID")
    characterRevision: int = Field(default=1, description="角色修订号")
    stateVersion: int = Field(default=1, description="实例状态版本")
    state: CharacterPublicStateView = Field(description="产品态视图")
```

- [ ] **Step 4: Implement CharacterStateChangedMessage + ServerInfoFeatures.character_state_stream + ServerMessage union (ws_message.py)**

In `frontend_contract/ws_message.py`, add `character_state_stream` to `ServerInfoFeatures` (:264-275, after `character_profiles`):

```python
    character_profiles: bool = Field(default=False, description="多角色 Profile(阶段 C+E;本批次恒 false,架构稿决策 #12)")
    character_state_stream: bool = Field(default=False, description="角色状态流(阶段 G;character.state.changed 事件;character_profiles=false 时独立可用)")
```

Add the new message class (near other generation messages, e.g. after `GenerationFailedMessage`):

```python
class CharacterStateChangedMessage(ContractModel):
    """角色状态变化事件(架构稿 §12.4;v2,character_state_stream=true 后发送)。

    只含产品态字段;state 是完整 CharacterPublicStateView,不是 patch;未知 optional 字段省略。
    客户端只按同一 instance 内严格递增的 stateVersion 应用事件,不能用时间字符串替代顺序。
    """
    type: Literal["character.state.changed"] = "character.state.changed"
    conversationId: Optional[str] = Field(default=None, description="会话 ID(可缺省)")
    characterId: str = Field(description="角色 ID")
    characterInstanceId: str = Field(description="角色实例 ID")
    characterRevision: int = Field(default=1, description="角色修订号")
    stateVersion: int = Field(default=1, description="实例状态版本")
    state: "CharacterPublicStateView" = Field(description="完整产品态视图(非 patch)")
```

Add `CharacterStateChangedMessage` to the `ServerMessage` union module-level alias (:1040-1083, before the closing `]`):

```python
    # 阶段 G(CharacterPublicState 实际发送)
    CharacterStateChangedMessage,
```

Use `from frontend_contract.rest import CharacterPublicStateView` (add to existing rest import at top of ws_message.py — verify it's already imported; if not, add).

- [ ] **Step 5: Re-export in `frontend_contract/__init__.py`**

Add imports + `__all__` entries for `CharacterStateChangedMessage`, `CharacterPublicStateView`, `MoodState`(at :90 import block + :322 __all__ block).

- [ ] **Step 6: Run contract tests**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_stage_g_contract.py -v`
Expected: PASS.

- [ ] **Step 7: Run test_stage_ce_contract (test_stage_ce_contract.py will FAIL on CharacterPublicState.mood/instanceId — postponed to Task 2 fix)**

Actually Task 1 only adds new classes + field; `CharacterPublicState` generalization changes the old flat mood/instanceId, so existing `test_stage_ce_contract.py::test_character_public_state_dto` breaks NOW. Fix it in Step 8.

- [ ] **Step 8: Update test_stage_ce_contract.py for CharacterPublicState generalization**

Modify `frontend_contract/tests/test_stage_ce_contract.py:72-82`:

```python
def test_character_public_state_dto():
    dto = CharacterPublicState(characterId="elysia", characterInstanceId="inst_1",
                               state=CharacterPublicStateView(
                                   mood=MoodState(label="平静", intensity=0.5, updatedAt="t"),
                                   proactiveEnabled=False, updatedAt="t"))
    assert dto.characterId == "elysia"
    assert dto.characterInstanceId == "inst_1"
    assert dto.characterRevision == 1
    assert dto.state.mood.label == "平静"
    assert dto.stateVersion == 1
    # wire_dict 同步可用(lib 序列化路径)
    assert dto.wire_dict()["characterId"] == "elysia"
    rt = CharacterPublicState.model_validate(dto.model_dump())
    assert rt.model_dump() == dto.model_dump()
```

Add to imports: `from frontend_contract.rest import CharacterPublicState, CharacterPublicStateView, MoodState`.

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_stage_ce_contract.py -v`
Expected: PASS.

- [ ] **Step 9: Run full frontend_contract tests + check-types**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/ -v` — expect all unit contract tests pass.
Run: `conda run -n Elysia python -m frontend_contract --check` — expect 0 drift (before regenerating; this may FAIL because TS/schema not yet regenerated — that's expected, Task 1 不 regen; postpone to Task 8 golden commit).
See Task 8 for regeneration.

- [ ] **Step 10: Commit**

```bash
git add frontend_contract/
git commit -m "feat(stage-g): 契约层 CharacterPublicStateView/MoodState/CharacterStateChangedMessage + character_state_stream
  
- MoodState/CharacterPublicStateView(rest.py): 产品态子视图(内部态不外泄)
- CharacterPublicState 泛化: flat mood 移除 + instanceId→characterInstanceId
- CharacterStateChangedMessage(ws_message.py): wire character.state.changed,入 ServerMessage union
- ServerInfoFeatures.character_state_stream 字段(默认 False,本批 capabilities 置 true)
- test_stage_g_contract.py 契约测试 + test_stage_ce_contract.py 同步更新"
```

---

### Task 2: core 映射 — make_public_state + CharacterRuntime._last_public_state/public_state/has_public_state_changed + CharacterRuntimeRegistry.get_existing

**Files:**
- Create: `core/character/public_state.py`
- Modify: `core/character/runtime.py`
- Modify: `core/character/registry.py`
- Test: `core/character/tests/test_public_state.py`(新)

**Interfaces:**
- Consumes: `CharacterRuntimeState`(core/schema/character.py:37,含 mood/mood_intensity/updated_at)、`CharacterRuntime.instance.updatedAt`(每 flush 更新)、`runtime.revision.runtimeDefaults["proactive"]["enabled"]`
- Produces:
  - `make_public_state(runtime) -> CharacterPublicStateView`(模块级)
  - `CharacterRuntime.public_state() -> CharacterPublicStateView`
  - `CharacterRuntime.has_public_state_changed(new_view) -> bool`(vs `_last_public_state`)
  - `CharacterRuntimeRegistry.get_existing(instance_id) -> CharacterRuntime | None`(dict 只读,不懒建)

- [ ] **Step 1: Write failing test**

Create `core/character/tests/test_public_state.py`:

```python
"""阶段 G:make_public_state 映射 + 变化检测(service)。"""
import pytest
from unittest.mock import MagicMock

from core.character.public_state import make_public_state
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

pytestmark = pytest.mark.service


def _mock_runtime(mood="平静", mood_intensity=0.5, updated_at="2026-09-05T00:00:00Z"):
    rt = MagicMock()
    rt.state = CharacterRuntimeState(
        instance_id="chi_builtin_elysia", session_id=SessionId("default"),
        mood=mood, mood_intensity=mood_intensity, updated_at=updated_at)
    rt.instance = MagicMock()
    rt.instance.updatedAt = "2026-09-05T01:00:00Z"
    rt.revision = MagicMock()
    rt.revision.runtimeDefaults = {"proactive": {"enabled": True}}
    return rt


class TestMakePublicState:
    def test_maps_mood_and_intensity(self):
        rt = _mock_runtime(mood="开心", mood_intensity=0.9)
        view = make_public_state(rt)
        assert view.mood.label == "开心"
        assert view.mood.intensity == 0.9

    def test_last_interaction_from_instance_updated_at(self):
        rt = _mock_runtime()
        view = make_public_state(rt)
        assert view.lastInteractionAt == "2026-09-05T01:00:00Z"
        assert view.updatedAt == "2026-09-05T01:00:00Z"

    def test_proactive_from_runtime_defaults(self):
        rt = _mock_runtime()
        assert make_public_state(rt).proactiveEnabled is True

    def test_internal_state_not_exposed(self):
        rt = _mock_runtime()
        rt.state.self_l2 = "SECRET_L2"
        rt.state.state_l3 = "SECRET_L3"
        rt.state.self_perception_history = [{"a": 1}]
        view = make_public_state(rt)
        d = view.model_dump()
        assert "self_l2" not in d and "state_l3" not in d
        assert "self_perception_history" not in d


class TestRuntimePublicState:
    def test_has_public_state_changed_detects_mood(self):
        from core.character.runtime import CharacterRuntime
        from core.schema.character import CharacterRuntimeState
        rt = CharacterRuntime(
            instance=MagicMock(instanceId="chi_builtin_elysia", stateVersion=1,
                               updatedAt="t"),
            definition=MagicMock(), revision=MagicMock(revision=1),
            user_identity=MagicMock())
        rt.state = CharacterRuntimeState(
            instance_id="chi_builtin_elysia", session_id=SessionId("default"),
            mood="开心", mood_intensity=0.9)
        view1 = rt.public_state()
        # same mood → no change
        assert rt.has_public_state_changed(view1) is False
        # mood changed
        rt.state.mood = "难过"
        view2 = rt.public_state()
        assert rt.has_public_state_changed(view2) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_public_state.py -x`
Expected: FAIL — `make_public_state`/`public_state`/`has_public_state_changed` not defined.

- [ ] **Step 3: Implement `make_public_state` (core/character/public_state.py)**

```python
"""make_public_state — CharacterRuntimeState(内部扁平态)→ CharacterPublicStateView(产品裁剪视图)。

阶段 G(架构稿 §12.4)。产品态只暴露 mood/currentActivity/lastInteractionAt/proactiveEnabled/
updatedAt;内部态(self_l2/state_l3/self_perception_history/reflector_buffer 等)绝不外泄。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from frontend_contract.rest import CharacterPublicStateView, MoodState

if TYPE_CHECKING:
    from core.character.runtime import CharacterRuntime


def make_public_state(runtime: "CharacterRuntime") -> CharacterPublicStateView:
    """从 runtime 构造产品态视图。

    数据源:
      - mood.label/intensity: runtime.state.mood / mood_intensity(engine.state 共享引用)
      - mood.updatedAt: runtime.state.updated_at(frozen,已知近似——无独立 mood 时间戳)
      - lastInteractionAt / updatedAt: runtime.instance.updatedAt(每 flush 更新,agent.py:432)
      - proactiveEnabled: runtime.revision.runtimeDefaults["proactive"]["enabled"](immutable 配置)
    内部态绝不外泄——只读上述白名单字段。
    """
    state = runtime.state
    runtime_defaults: dict[str, Any] = dict(runtime.revision.runtimeDefaults or {})
    proactive = runtime_defaults.get("proactive") or {}
    return CharacterPublicStateView(
        mood=MoodState(label=state.mood, intensity=state.mood_intensity,
                       updatedAt=state.updated_at),
        currentActivity=None,
        lastInteractionAt=runtime.instance.updatedAt,
        proactiveEnabled=bool(proactive.get("enabled", False)),
        updatedAt=runtime.instance.updatedAt,
    )
```

(Note: `runtime.instance.updatedAt` is `str` — matches `CharacterPublicStateView.lastInteractionAt: Optional[str]` / `updatedAt: str`. Good.)

- [ ] **Step 4: Add `public_state()`/`has_public_state_changed()` to CharacterRuntime (runtime.py)**

Add `_last_public_state` in `__init__` (:48-57 area, e.g. after `self.state: CharacterRuntimeState = ...`):

```python
        self._persist_fn: Callable[[CharacterRuntimeState], Awaitable[None]] | None = None
        self._last_public_state: "CharacterPublicStateView | None" = None
```

Add methods (after `snapshot_state` :76-78):

```python
    def public_state(self) -> "CharacterPublicStateView":
        """当前产品态视图(经 make_public_state)。不做变化检测——仅计算视图。"""
        from core.character.public_state import make_public_state
        return make_public_state(self)

    def has_public_state_changed(self, new_view: "CharacterPublicStateView") -> bool:
        """对比 _last_public_state,仅 mood.label/mood.intensity 视为变化信号。

        不比较 updated_at(展示字段,每次 flush 都变)、proactiveEnabled(immutable 配置常量)。
        首次(_last_public_state None)视为有变化(供第一次发射)。
        """
        last = self._last_public_state
        if last is None:
            return True
        return (last.mood.label != new_view.mood.label
                or last.mood.intensity != new_view.mood.intensity)
```

Add `from frontend_contract.rest import CharacterPublicStateView` under TYPE_CHECKING (:21-25).

- [ ] **Step 5: Add `get_existing()` to CharacterRuntimeRegistry (registry.py)**

In `core/character/registry.py`, add near `get` (:25-33):

```python
    def get_existing(self, instance_id: str) -> "CharacterRuntime | None":
        """只读解析已构建 runtime——不懒建(区别于 get,后者 factory 会建)。

        REST 只读端点用:registry 命中即读、未命中返回 None(fail-closed 语义)。
        """
        return self._runtimes.get(instance_id)
```

- [ ] **Step 6: Run tests**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_public_state.py -v`
Expected: PASS.

- [ ] **Step 7: Run existing runtime + registry tests**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_runtime.py core/character/tests/test_registry.py -v`
Expected: PASS(新增方法不破坏现有)。

- [ ] **Step 8: Commit**

```bash
git add core/character/
git commit -m "feat(stage-g): make_public_state 映射 + runtime 变化检测 + registry.get_existing
  
- core/character/public_state.py: RuntimeState→CharacterPublicStateView(内部态不外泄)
- runtime.py: public_state()/has_public_state_changed()(仅 mood 视为变化信号)+ _last_public_state
- registry.py: get_existing()(只读不懒建)
- test_public_state.py 单元测试 + runtime/registry 现有测试回归"
```

---

### Task 3: agent — character_runtime_registry + character_state_stream 公开 property

**Files:**
- Modify: `core/agent/agent.py`
- Test: `core/agent/tests/test_multi_session.py`(或新增)

**Interfaces:**
- Produces:
  - `agent.character_runtime_registry -> CharacterRuntimeRegistry`(公开,返回 `self._runtime_registry`)
  - `agent.character_state_stream -> bool`(恒 True,本批)

- [ ] **Step 1: Write failing test**

Append to `core/agent/tests/test_multi_session.py` (or a small new test):

```python
class TestCharacterPublicAccessors:
    def test_character_runtime_registry_exposed(self, agent):
        """character_runtime_registry 公开访问 self._runtime_registry。"""
        assert agent.character_runtime_registry is agent._runtime_registry  # noqa: SLF001

    def test_character_state_stream_true(self, agent):
        """character_state_stream 恒 true(本批,与 build_server_info 一致)。"""
        assert agent.character_state_stream is True
```

Note: use `agent` fixture already in test_multi_session.py (returns `Agent(config=AppConfig())`). The fixture exists; verify it has `_runtime_registry`.

- [ ] **Step 2: Run to verify fails**

Run: `conda run -n Elysia python -m pytest core/agent/tests/test_multi_session.py::TestCharacterPublicAccessors -x`
Expected: FAIL — `character_runtime_registry`/`character_state_stream` not defined.

- [ ] **Step 3: Add properties to Agent (agent.py)**

Add near `character_engine` (:1080-1090):

```python
    @property
    def character_runtime_registry(self) -> "CharacterRuntimeRegistry":
        """角色运行时注册表(公开只读)。

        阶段 G: REST /api/character-instances/{id}/state 经此解析 instance runtime。
        跨模块消费禁止 agent._xxx,故公开;注册表内部 get_existing 为只读不懒建。
        """
        return self._runtime_registry

    @property
    def character_state_stream(self) -> bool:
        """角色状态流开关(阶段 G,恒 true——本批实现 snapshot/回合边界事件)。

        与 build_server_info 的 ServerInfoFeatures.character_state_stream 保持一致,
        避免 ws_handler/finalize 的发射 gate 与 capabilities 各自硬编码漂移。
        character_profiles 恒 false(capabilities.py:107 已硬编码,不引此属性)。
        """
        return True
```

Add `from core.character.registry import CharacterRuntimeRegistry` import if not present (verify; agent.py:216 uses `CharacterRuntimeRegistry()`, so it's imported).

- [ ] **Step 4: Run tests**

Run: `conda run -n Elysia python -m pytest core/agent/tests/test_multi_session.py::TestCharacterPublicAccessors -v`
Expected: PASS.

- [ ] **Step 5: Run broader agent tests**

Run: `conda run -n Elysia python -m pytest core/agent/tests/ -v`
Expected: PASS(新增 property 不破坏现有 39 个公开访问器)。

- [ ] **Step 6: Commit**

```bash
git add core/agent/agent.py core/agent/tests/
git commit -m "feat(stage-g): agent.character_runtime_registry + character_state_stream 公开 property
  
- character_runtime_registry: 公开 self._runtime_registry(REST 只读端点用)
- character_state_stream: 恒 true(与 build_server_info 一致,发射 gate 同源)
- test_multi_session.py 增 accessor 断言"
```

---

### Task 4: REST — GET /api/character-instances/{id}/state(只读,入 register_character_routes)

**Files:**
- Modify: `core/input/character_routes.py`
- Test: `core/input/tests/test_character_routes.py`(或现有 character routes 测试)

**Interfaces:**
- Consumes: `agent.character_runtime_registry.get_existing(instance_id)`, `agent.character_runtime_registry` + `make_public_state(runtime)`
- Produces: `GET /api/character-instances/{id}/state -> CharacterPublicState` fail-closed StorageUnavailableError→503

- [ ] **Step 1: Write failing test**

In `core/input/tests/test_character_routes.py` (or new `test_instance_state.py`), add:

```python
@pytest.mark.asyncio
async def test_instance_state_returns_public_state(aiohttp_client):
    """GET /api/character-instances/{id}/state 返回产品态(含 state 子结构)。"""
    from core.input.server import create_app

    agent = _make_agent()  # test helper; ensure has character_runtime_registry
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        # default elysia instance 已注册
        resp = await client.get("/api/character-instances/chi_builtin_elysia/state")
        assert resp.status == 200
        data = await resp.json()
        assert data["characterId"] == "chr_builtin_elysia"
        assert data["characterInstanceId"] == "chi_builtin_elysia"
        assert "state" in data and "mood" in data["state"]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_instance_state_missing_returns_503(aiohttp_client):
    """未注册 instance → fail-closed 503(不懒建)。"""
    from core.input.server import create_app

    agent = _make_agent()
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        resp = await client.get("/api/character-instances/chi_builtin_nonexistent/state")
        assert resp.status == 503
    finally:
        await agent.stop()
```

- [ ] **Step 2: Run to verify fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_character_routes.py -k "instance_state" -x`
Expected: FAIL — route not defined(404).

- [ ] **Step 3: Implement handler in character_routes.py**

Add to `core/input/character_routes.py`, inside `register_character_routes` (:185-196) after `/api/characters/{id}` GET:

```python
    app.router.add_get("/api/character-instances/{id}/state", _instance_state_get)
```

Add the handler function (near the route, e.g. before `register_character_routes`):

```python
async def _instance_state_get(request: web.Request) -> web.Response:
    """GET /api/character-instances/{id}/state — 只读产品态。

    registry 命中即读;未命中/异常 → fail-closed 503。复用 _character_error_response
    (character_routes.py:24,catch StorageUnavailableError → 503 CHARACTER_STORAGE_UNAVAILABLE),
    不新建错误信封(与现有 /api/characters 路由同模式)。
    """
    from core.storage.attachment_repo import StorageUnavailableError  # re-export from base
    agent = request.app["agent"]
    instance_id = request.match_info.get("id", "")
    try:
        runtime = None
        if instance_id:
            registry = getattr(agent, "character_runtime_registry", None)
            if registry is not None:
                runtime = registry.get_existing(instance_id)
        if runtime is None:
            raise StorageUnavailableError(f"character instance not found: {instance_id}")
        from core.character.public_state import make_public_state
        from frontend_contract.rest import CharacterPublicState
        view = make_public_state(runtime)
        public = CharacterPublicState(
            characterId=runtime.definition.characterId,
            characterInstanceId=runtime.instance.instanceId,
            characterRevision=runtime.revision.revision,
            stateVersion=runtime.stateVersion,
            state=view,
        )
        return web.json_response(public.wire_dict())
    except Exception as e:
        err = _character_error_response(e)  # StorageUnavailableError → 503 CHARACTER_STORAGE_UNAVAILABLE
        if err is not None:
            return err
        raise
```

Note: `_character_error_response` is already defined at character_routes.py:24 and catches `StorageUnavailableError`(:40)→503. `StorageUnavailableError` re-exported from `core.storage.attachment_repo`(which imports from `core.storage.base:49`).

- [ ] **Step 3b: 确认 `_character_error_response` 覆盖 StorageUnavailableError**

已核实:`_character_error_response`(character_routes.py:24)在 :40 显式 `isinstance(exc, StorageUnavailableError)` → `_protocol_error_response("CHARACTER_STORAGE_UNAVAILABLE", ..., 503, retryable=True)`。handler 无需额外错误映射逻辑。

(如 `StorageUnavailableError` 未在 character_routes.py 顶部 import,需添加:`from core.storage.attachment_repo import StorageUnavailableError`;它 re-export 自 `core.storage.base:49`。)

- [ ] **Step 4: Run tests**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_character_routes.py -k "instance_state" -v`
Expected: PASS.

- [ ] **Step 5: Run input tests regression**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_server.py core/input/tests/test_character_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add core/input/character_routes.py core/input/tests/
git commit -m "feat(stage-g): REST GET /api/character-instances/{id}/state(只读 fail-closed)
  
- register_character_routes 内新增 handler(不建新模块)
- registry.get_existing 命中即读,未命中→503(不懒建)
- 返回 CharacterPublicState(含 state 子结构)"
```

---

### Task 5: WS snapshot — 连接/激活发射 character.state.changed

**Files:**
- Modify: `core/input/ws_handler.py`
- Test: `core/input/tests/test_ws_handler.py`(或 test_version)

**Interfaces:**
- Consumes: `agent.character_state_stream`, `agent.character_runtime_registry`(发当前活动会话 runtime 产品态)
- Produces: v2 连接/activate_session 后发 `CharacterStateChangedMessage`(snapshot,强制)

- [ ] **Step 1: Write failing test**

In `core/input/tests/test_ws_handler.py` (or test_version), add a test that v2 connect emits a `character.state.changed` snapshot. Note: real WS integration is complex; use a verified-pattern test. Look at existing `test_v1_wire_sequence_unchanged` (test_v2_generation.py:247) for the harness. In a new test:

```python
@pytest.mark.asyncio
async def test_v2_connect_emits_state_snapshot(aiohttp_client):
    """v2 建连发 server_info 后应发一条 character.state.changed 快照。"""
    from core.input.server import create_app

    agent = _make_agent()
    await agent.start()
    agent.session._messages.clear()  # noqa: SLF001 空会话
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            # 排空建连帧: 读到 server_info 后,应再收一条 character.state.changed
            saw_server_info = False
            saw_state_changed = False
            for _ in range(10):
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
                if msg.type != WSMsgType.TEXT:
                    continue
                t = json.loads(msg.data).get("type")
                if t == "server_info":
                    saw_server_info = True
                if t == "character.state.changed":
                    saw_state_changed = True
                    break
            assert saw_server_info, "v2 建连应先收 server_info"
            assert saw_state_changed, "v2 建连应发 character.state.changed 快照"
    finally:
        await agent.stop()
```

Note: 用 `core/cognitive/tests/test_v2_generation.py` 的 `_send_and_collect`/`_make_agent_with` 作为参考 harness;`_make_agent()` 来自 `core/input/tests/test_server.py:54`。若该测试需真实 LLM,用 `_make_agent_with(_UsageLLM())`(test_v2_generation.py:109)。

- [ ] **Step 2: Run to verify fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_ws_handler.py -k "state_snapshot" -x`
Expected: FAIL — no `character.state.changed` frame emitted.

- [ ] **Step 3: Implement snapshot emission in ws_handler.py**

In `ws_handler.py`, in the v2 connect handler (after `build_server_info` is sent, near `:228`), and after `activate_session` switch, emit snapshot.

Find the connect handler flow. In the v2 connect path, after `await ws.send_json(build_server_info(...).wire_dict())`, add:

```python
        # 阶段 G(§7.1):v2 + character_state_stream=true 时发角色状态快照(强制,不看 last)
        if getattr(agent, "character_state_stream", False):
            await _emit_state_snapshot(agent, ws)
```

Define helper `_emit_state_snapshot`:

```python
async def _emit_state_snapshot(agent, ws) -> None:
    """发当前活动会话 instance 的产品态快照(character.state.changed)。

    取活动会话的 runtime → make_public_state → CharacterStateChangedMessage。
    建连时强制发射(客户端刚连上需要初始态);失败静默(不影响握手)。
    """
    try:
        from core.character.public_state import make_public_state
        from frontend_contract.ws_message import CharacterStateChangedMessage
        registry = getattr(agent, "character_runtime_registry", None)
        if registry is None:
            return
        # 当前活动会话的 binding → instance_id → runtime
        binding = getattr(agent.session_manager.active, "character_binding", None) or {}
        instance_id = binding.get("instance_id") or "chi_builtin_elysia"
        runtime = registry.get_existing(instance_id)
        if runtime is None:
            return
        view = make_public_state(runtime)
        msg = CharacterStateChangedMessage(
            conversationId=str(agent.session.active.session_id),
            characterId=runtime.definition.characterId,
            characterInstanceId=runtime.instance.instanceId,
            characterRevision=runtime.revision.revision,
            stateVersion=runtime.stateVersion,
            state=view,
        )
        await ws.send_json(msg.wire_dict())
    except Exception:
        logger.debug("emit state snapshot 失败", exc_info=True)
```

Add to `activate_session` handler (ws_v2.py or ws_handler.py, where activate_session switch succeeds): after switch, call `_emit_state_snapshot(agent, ws)`.

- [ ] **Step 4: Run tests**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_ws_handler.py -k "state_snapshot" -v`
Expected: PASS.

- [ ] **Step 5: Run v1 wire regression**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_v2_generation.py::test_v1_wire_sequence_unchanged -v`
Expected: PASS — v1 路径不发 character.state.changed(snapshot 只在 v2 + flag)。

- [ ] **Step 6: Commit**

```bash
git add core/input/ws_handler.py core/input/tests/
git commit -m "feat(stage-g): WS 连接/激活发 character.state.changed 快照
  
- v2 connect 建连后 + activate_session 切换后强制发射 snapshot
- 取活动会话 runtime → make_public_state → CharacterStateChangedMessage
- 失败静默(不影响握手);v1 路径不发"
```

---

### Task 6: 回合边界 — finalize.py 按 mood 变化发射 character.state.changed

**Files:**
- Modify: `core/cognitive/finalize.py`
- Test: `core/cognitive/tests/test_finalize.py`

**Interfaces:**
- Consumes: `self._current_runtime`(loop 设置)、`agent.character_state_stream`,`_output` 广播
- Produces: `_finalize_turn` 末(`_flush_runtime_at_turn_boundary` 后)若 v2+flag 且 mood 变化 → 广播 `CharacterStateChangedMessage`

- [ ] **Step 1: Write failing test**

Create `core/cognitive/tests/test_finalize.py`:

```python
"""阶段 G:回合边界 state change 发射(service)。

用最小宿主(CognitiveFinalize mixin unbound)直接调 _emit_state_change_if_needed,
mock _output —— 不跑完整 turn(完整链路由 Task 5 snapshot 测试 + test_v2_empty_response 覆盖)。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.cognitive.finalize import CognitiveFinalize
from core.schema.types import SessionId

pytestmark = pytest.mark.service


def _host(protocol_version=2):
    """构造 CognitiveFinalize mixin 宿主(无 spec MagicMock——spec 会阻止设置 _output,
    因 _output 只是类型占位注解 finalize.py:43,非类属性,spec 判定后 _output 不可设)。

    用平凡 MagicMock(不设 spec)让 _generation_ctx/_current_runtime/_output 可设;
    调 CognitiveFinalize._emit_state_change_if_needed(host, ...) 走类的真实 unbound 方法。
    """
    host = MagicMock()
    host._generation_ctx = MagicMock(protocol_version=protocol_version)
    host._current_runtime = MagicMock()
    v2_channel = MagicMock()
    host._output._split_versions.return_value = ([], [v2_channel])
    host._output._broadcast = AsyncMock()  # _broadcast 是 async 方法——实现里 await self._output._broadcast(...)
    return host, v2_channel


@pytest.mark.asyncio
async def test_v2_mood_change_emits_state():
    """v2 + mood 变化 → 经 _broadcast(on_character_state_changed, channels=v2)发射。"""
    host, v2_channel = _host(protocol_version=2)
    runtime = host._current_runtime
    runtime.public_state.return_value = MagicMock()
    runtime.has_public_state_changed.return_value = True
    runtime.definition.characterId = "chr_elysia"
    runtime.instance.instanceId = "chi_elysia"
    runtime.revision.revision = 1
    runtime.stateVersion = 3

    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))

    host._output._broadcast.assert_awaited_once()
    args = host._output._broadcast.await_args
    assert args.kwargs["channels"] == [v2_channel]
    assert args.kwargs["character_id"] == "chr_elysia"


@pytest.mark.asyncio
async def test_v1_does_not_emit():
    """v1(protocol_version=1)不发射——不调用 _broadcast。"""
    host, _ = _host(protocol_version=1)
    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))
    host._output._broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_mood_change_does_not_emit():
    """mood 无变化(has_public_state_changed=False)不发射。"""
    host, v2_channel = _host(protocol_version=2)
    host._current_runtime.has_public_state_changed.return_value = False
    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))
    host._output._broadcast.assert_not_awaited()
```

Note: `CognitiveFinalize` 是 mixin(`finalize.py:36 class CognitiveFinalize`)。测试用**无 spec 的 MagicMock host**(不能用 `spec=CognitiveFinalize`——会阻止设置 `_output`,因 `_output` 只是类型占位注解非类属性)+ unbound 方法调用(`CognitiveFinalize._emit_state_change_if_needed(host, ...)`)最直接——`_emit_state_change_if_needed` 只依赖 `_generation_ctx`/`_current_runtime`/`_output`(都是宿主属性,已 mock)。

> TDD 说明:Step 1 写测试时 `_emit_state_change_if_needed` 尚未实现,`CognitiveFinalize._emit_state_change_if_needed(host, ...)` 会 AttributeError(方法不存在)→ 测试 fail(正确)。Step 3 实现该方法后,unbound 调用经类真实方法以 host 为 self,读取 host 的已 mock 属性,测试通过。

- [ ] **Step 2: Run to verify fails**

Expected: FAIL — no emission.

- [ ] **Step 3: Implement in finalize.py `_finalize_turn`**

In `_finalize_turn`, after `await self._flush_runtime_at_turn_boundary()` (:236), before `await self._transition_idle()` (:237), add:

```python
        await self._emit_state_change_if_needed(conversation_id)
```

Add method:

```python
    async def _emit_state_change_if_needed(self, conversation_id: SessionId | None) -> None:
        """回合边界按 mood 变化发射 character.state.changed。

        发射点:flush 调完之后(stateVersion 已 CAS 递增,见 spec §3.3/§7.2)。
        仅 v2 + character_state_stream=true 且 mood 真变化时发射——不比较 updated_at
        (展示字段)/proactiveEnabled(immutable 常量)。

        [B2 修复] 必须 gate protocol_version >= 2(对齐 finalize.py:82 现有 turn_completed
        模式)——否则 v1 turn 也会进此广播,破坏 v1 wire 不变性。channels 用
        self._output._split_versions() 取 v2 通道(不广播到 v1)。
        [B1 修复] OutputManager 只有私有 _broadcast(无公开 broadcast),且是 async——
        需 await self._output._broadcast(...)。
        """
        ctx = self._generation_ctx
        if ctx is None or ctx.protocol_version < 2:
            return
        runtime = self._current_runtime
        if runtime is None:
            return
        try:
            view = runtime.public_state()
            if not runtime.has_public_state_changed(view):
                return
            runtime._last_public_state = view  # noqa: SLF001
            _v1, v2 = self._output._split_versions()  # noqa: SLF001 —— OutputManager 无公开 broadcast
            await self._output._broadcast(  # noqa: SLF001
                "on_character_state_changed",
                conversation_id=conversation_id,
                character_id=runtime.definition.characterId,
                character_instance_id=runtime.instance.instanceId,
                character_revision=runtime.revision.revision,
                state_version=runtime.stateVersion,
                state=view,
                channels=v2,
            )
        except Exception:
            logger.debug("回合边界 state change 发射失败", exc_info=True)
```

Note: `_output._split_versions()` 返回 `(v1, v2)` 通道列表(manager.py:405-410 的 `_split_versions`)。传 `channels=v2` 过滤到 v2 通道。`_broadcast` 是 async 需 await。这是最终实现——不再依赖 `_character_state_stream` 魔术默认(已由 protocol_version gate 替代安全,character_state_stream 本批恒 true 不移除)。

- [ ] **Step 4: Run tests**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_finalize.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/cognitive/finalize.py core/cognitive/tests/
git commit -m "feat(stage-g): 回合边界按 mood 变化发射 character.state.changed
  
- _finalize_turn 在 flush 后调 _emit_state_change_if_needed
- 变化检测仅 mood.label/intensity(has_public_state_changed)
- 无变化/非 v2/flag off 不发射"
```

---

### Task 7: OutputManager — on_character_state_changed 广播 handler

**Files:**
- Modify: `Module/output/manager.py`
- Modify: `Module/output/protocol.py`(可能)
- Modify: `Module/output/websocket.py`(WebSocketChannel.on_character_state_changed)

**Interfaces:**
- Consumes: Task 6 的 `_output._broadcast("on_character_state_changed", ..., channels=v2)`(OutputManager 私有 broadcast,经 _split_versions 取 v2 通道)
- Produces: WebSocketChannel `on_character_state_changed` → 构建 `CharacterStateChangedMessage` wire_dict 发送(仅 v2)

- [ ] **Step 1: Write failing test** — a unit test on WebSocketChannel that `on_character_state_changed` builds a wire frame, and ignores when protocol_version < 2.

- [ ] **Step 2: Run to verify fails**

- [ ] **Step 3: Implement**

In `Module/output/websocket.py`, add method `on_character_state_changed` [B2 修复:加 v2 guard 防 v1 channel 收到——Task 6 已经 _split_versions 只传 v2 通道,但 WebSocketChannel 自身也按 protocol_version 防御]:

```python
    async def on_character_state_changed(self, *, conversation_id=None,
                                         character_id, character_instance_id,
                                         character_revision, state_version, state) -> None:
        """发送 character.state.changed(架构稿 §12.4)。

        guard: 仅 protocol_version >= 2(防御——Task 6 已经 _split_versions 过滤 v2 通道)。
        """
        if self.protocol_version < 2:
            return
        await self._send_json(CharacterStateChangedMessage(
            conversationId=conversation_id, characterId=character_id,
            characterInstanceId=character_instance_id,
            characterRevision=character_revision, stateVersion=state_version,
            state=state,
        ).wire_dict())
```

Add to `Module/output/protocol.py` OutputChannel ABC (or duck-typed):

```python
    async def on_character_state_changed(self, **kwargs) -> None:  # pragma: no cover - 默认 no-op
        """角色状态变化(架构稿 §12.4;v2,character_state_stream=true 后发送)。"""
```

And `OutputManager._broadcast` routes to any channel with the method (verify existing broadcast uses `getattr` fallback; if so, no extra wiring).

Verify `OutputManager` broadcast mechanism dispatches to channel methods by name via `getattr(channel, method)`; if channels without the method need a default, protocol.py ABC default covers it.

- [ ] **Step 4: Run tests**

- [ ] **Step 5: Commit**

```bash
git add Module/output/
git commit -m "feat(stage-g): OutputChannel/WebSocketChannel on_character_state_changed 广播
  
- websocket.py: on_character_state_changed → CharacterStateChangedMessage wire 发送
- protocol.py: OutputChannel ABC 默认 no-op
- OutputManager._broadcast 按名路由(现有机制)"
```

---

### Task 8: capabilities + golden frame 更新(build_server_info 置 true + _server_info + chat golden state 帧)

**Files:**
- Modify: `core/services/capabilities.py`
- Modify: `scripts/gen_contract_frames.py`
- Modify: `scripts/tests/test_gen_contract_frames.py`(如需)
- Regenerate: `frontend/src/types/contract.generated.ts`, `frontend_contract/contract.schema.json`, `frontend_contract/contract.raw.ts`

**Interfaces:**
- Produces: `build_server_info` 设 `character_state_stream=True`;`_server_info()` golden 加 `character_state_stream=True`;`_chat()` 末尾加 `character.state.changed` 帧

- [ ] **Step 1: Modify build_server_info (capabilities.py)**

In `core/services/capabilities.py`, add `character_state_stream=True` to `ServerInfoFeatures(...)` (after `character_profiles=False`):

```python
            character_profiles=False,  # 恒 false(决策 #12)
            character_state_stream=True,  # 阶段 G: character.state.changed 事件已实现
```

- [ ] **Step 2: Modify _server_info() golden (gen_contract_frames.py)**

In `scripts/gen_contract_frames.py` `_server_info()` (:43-65), add `character_state_stream=True` to `ServerInfoFeatures(...)`:

```python
            idempotency_durable=True,
            character_state_stream=True),  # 阶段 G: 角色状态流已实现(恒 true,独立于 character_profiles)
```

Verify the param list: add after `idempotency_durable=True` inside `wm.ServerInfoFeatures(...)`.

- [ ] **Step 3: Modify _chat() to append state frame**

In `scripts/gen_contract_frames.py` `_chat()` (:73-130), after `idle = wm.StateMessage(state="idle")`, append a `character.state.changed` frame:

```python
    state_changed = wm.CharacterStateChangedMessage(
        conversationId="default", characterId="chr_builtin_elysia",
        characterInstanceId="chi_builtin_elysia", characterRevision=1,
        stateVersion=1,
        state=rest.CharacterPublicStateView(
            mood=rest.MoodState(label="开心", intensity=0.72, updatedAt=_TS),
            proactiveEnabled=False, updatedAt=_TS))
```

[C3 修复] 用 `rest.CharacterPublicStateView`/`rest.MoodState`——gen_contract_frames.py:28 `from frontend_contract import rest, ws_message as wm`,故 `rest.*` 可用;而 `wm.MoodState` 不在 ws_message 命名空间(MoodState 定义于 rest.py,ws_message 只 import CharacterPublicStateView)。`wm.CharacterStateChangedMessage` 在 ws_message(符合——它定义于 ws_message.py)。

Verify: gen_contract_frames.py:28 `from frontend_contract import rest, ws_message as wm`。`rest.MoodState`/`rest.CharacterPublicStateView` 经 rest 模块属性可访问,无 AttributeError。
```

And add `state_changed` to the `_f(...)` return list (after `idle`).

- [ ] **Step 4: Regenerate contract artifacts**

Run: `conda run -n Elysia python -m frontend_contract`
Run: `conda run -n Elysia python scripts/gen_contract_frames.py`

- [ ] **Step 5: Run check-types + golden tests**

Run: `conda run -n Elysia python -m frontend_contract --check` — expect 0 drift.
Run: `conda run -n Elysia python -m pytest scripts/tests/test_gen_contract_frames.py -v`
Expected: PASS — `test_cancel_and_fail` `all(features.values())` now includes character_state_stream=True; `test_stage4_frames_present` now counts character.state.changed (extra present, fine).

- [ ] **Step 6: Commit**

```bash
git add core/services/capabilities.py scripts/gen_contract_frames.py scripts/tests/test_gen_contract_frames.py frontend/src/types/contract.generated.ts frontend_contract/contract.schema.json frontend_contract/contract.raw.ts
git commit -m "feat(stage-g): build_server_info 置 character_state_stream=true + golden state 帧
  
- capabilities.py: character_state_stream=True(恒 true,独立于 character_profiles)
- gen_contract_frames.py: _server_info 加该字段 + _chat 末尾加 character.state.changed 帧
- 重生成 TS/schema/raw_ts(契约产物)"
```

---

### Task 9: 守门 — v1 wire 序列断言扩充 + 全量回归

**Files:**
- Modify: `core/cognitive/tests/test_v2_generation.py`(test_v1_wire_sequence_unchanged)
- Test add: assert v1 不发 `character.state.changed`

**Interfaces:**
- Produces: v1 序列断言 `character.state.changed` 不出现

- [ ] **Step 1: Extend test_v1_wire_sequence_unchanged**

In `core/cognitive/tests/test_v2_generation.py:247`, add to the assertion block (after `assert not any("generation" in t ...)`):

```python
            # v1 不应收到 character.state.changed(阶段 G 只 v2 + character_state_stream=true)
            assert "character.state.changed" not in types
```

- [ ] **Step 2: Run v1 wire test**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_v2_generation.py::test_v1_wire_sequence_unchanged -v`
Expected: PASS — v1 不发该帧(需 Task 5 snapshot 只在 v2 发的正确性).

- [ ] **Step 3: Commit**

```bash
git add core/cognitive/tests/test_v2_generation.py
git commit -m "test(stage-g): v1 wire 序列断言 character.state.changed 不出现"
```

---

### Task 10: 守门全量 — lint / pyright / test-quick / test-normal

**Files:** 无新增,仅验证

- [ ] **Step 1: ruff lint**

Run: `make lint`
Expected: 0 错误.

- [ ] **Step 2: pyright (Elysia env)**

Run: `conda run -n Elysia pyright`
Expected: 0 错误(agent.py 新增 property,public_state.py 新文件).

- [ ] **Step 3: test-quick**

Run: `make test-quick`
Expected: 全绿(3774+ 用例).

- [ ] **Step 4: test-normal (排除 slow)**

Run: `make test-normal`
Expected: 全绿.

- [ ] **Step 5: Commit(如有 lint/pyright 修复)**

```bash
git add -A
git commit -m "chore(stage-g): lint/pyright 修复"
```

(如无修复则跳过 commit.)

---

## Self-Review

**Spec coverage:**
- §4.1 MoodState / §4.2 CharacterPublicStateView → Task 1 ✓
- §4.3 CharacterStateChangedMessage + ServerMessage union → Task 1 ✓
- §4.4 CharacterPublicState 泛化(instanceId→characterInstanceId,state 子结构)→ Task 1 ✓
- §4.5 ServerInfoFeatures.character_state_stream → Task 1 + Task 8 ✓
- §4.6 上 wire 列表__init__ → Task 1 ✓
- §4.7 golden frame → Task 8 ✓
- §5 make_public_state → Task 2 ✓
- §6 REST GET state → Task 4 ✓
- §7.1 WS snapshot → Task 5 ✓
- §7.2 回合边界 → Task 6 ✓
- §7.3 OutputManager 广播 → Task 7 ✓
- §8 变更清单 → Task 1-8 ✓
- §9 测试 → Task 1-9 ✓
- §9.3 守门 C3(test_v1_wire_sequence_unchanged)→ Task 9 ✓
- §12 验收 → Task 10 ✓

**Placeholder scan:** 无 TBD/TODO;每步有确切代码。

**Type consistency:** `make_public_state -> CharacterPublicStateView`;`CharacterStateChangedMessage.state: CharacterPublicStateView`;`has_public_state_changed(new_view) -> bool`;`get_existing -> CharacterRuntime | None`;`character_state_stream -> bool`(恒 true)——全链路一致。

**已核实的关键绑定**(避免 plan 实现偏差):
- `engine.state` 与 `runtime.state` 共享同一引用(agent.py:281)——change detection 读 runtime.state.mood 即读 engine 写的值。
- `instance.updatedAt` 每 flush 更新(agent.py:432 `inst.updatedAt = now_iso()`)——B1 的时间戳数据源。
- `state.updated_at` 冻结(frozen)——mood.updatedAt 的已知近似。
- `ServerMessage` union 在 ws_message.py:1040-1083,gen_ts 经 `__main__.py:77-81` 收集。
- `test_v1_wire_sequence_unchanged`(test_v2_generation.py:247)是 v1 wire 回归,非 18127B(system prompt 快照)。
- `_MISSING_FRAMES`(test_gen_contract_frames.py:100)不含 character.state.changed——加帧不影响缺帧断言。
- `register_character_routes`(character_routes.py:185)已注册在 server.py:308——REST 并入,不建新模块。
