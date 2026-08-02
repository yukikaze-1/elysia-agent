# 服务运行时状态 + 热插拔实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 STT/TTS 的启用/禁用从「前端本地偏好 + 启动期配置」提升为后端运行时状态，支持前端热插拔（运行时开关服务、热切换模型）并跨重启持久化（ServiceState）。前端 TTS/STT 开关走后端模式。

**Architecture:** ServiceSlot 概念模式（llm 用现有 `agent.llm` / tts 用 `agent.tts` + `_tts_mgr.set_enabled` / stt 新增 `agent.stt` 公开槽位）；ServiceState 为 SessionMemory 专用类型字段（MongoDB 30d TTL）；`set_service` + `service_mode_result` WS 消息；capabilities 改嵌套 per-service 形状并在状态变化时全量重播。完整设计见 spec：`docs/superpowers/specs/2026-08-02-service-runtime-state-design.md`（8 轮评审后定稿）。

**Tech Stack:** Python 3.11+ / aiohttp / Pydantic / motor (MongoDB) / Vue 3 + Pinia + TypeScript。

## Global Constraints

- 禁止相对导入（`make lint`，ruff TID252）；`__init__.py` / `__main__.py` 豁免。
- 新测试必须加 marker（unit/service/integration/slow，见 docs/testing.md）。
- 使用 conda 环境：`conda activate Elysia`。
- 跨模块消费禁止 `agent._xxx`，一律走公开 `@property`（CLAUDE.md）。
- `agent.stt` 是只读 `@property → self._stt`（无 setter）；写入/测试注入用 `self._stt`。
- 代码注释用中文。
- 每任务结束对应测试子集全绿；全部完成后 `make test-quick` 全绿。
- `set_service` / `switch_*` 变更后 `await session.save()`（非 fire-and-forget，防 last-write-wins）。

## File Structure

```
后端（依赖链 Task1→8，每任务自带测试）：
  core/schema/service_state.py            # 新建：ServiceState 模型（enabled 三态 Optional[bool]）
  core/memory/session.py                  # ServiceState 字段 + save/load/JSONL 贯通
  core/storage/session_repo.py            # save 加 service_state= 参 + load 4 元组 + docstring
  core/input/debug_routes/archive.py      # 3 元解包 → 4 元
  Module/stt/protocol.py                  # provider_name 抽象 + model 拼接
  Module/stt/registry.py                  # create 传 provider
  Module/stt/sensevoice.py                # provider_name="sensevoice"
  Module/stt/openai_stt.py                # provider_name="custom"
  core/agent/agent.py                     # agent.stt 槽位 + stt_transcribe + 4 个公开属性
  core/agent/_lifecycle_services.py       # STT 生命周期 + TTS None 复位 + _start_tts 建门
  core/adapter/llm_adapter.py             # switch_model 先建后换
  core/agent/server.py                    # switch_* 重写 + set_service + build_capabilities 重播 + run_server STT 块 + banner
  core/adapter/tts_adapter.py             # async set_enabled + per-task flush_on_cancel + drain
  core/cognitive/loop.py                  # 972/979 分支加 enabled
  core/input/ws_handler.py                # set_service 路由 + audio_buf 改读 agent.stt + BINARY 防御
  core/input/capabilities.py              # 新建：build_capabilities(agent)

前端（Task9→10）：
  frontend/src/types/protocol.ts          # Capabilities 嵌套 + SetService + ServiceModeResult
  frontend/src/stores/settingsStore.ts    # ttsEnabled 语义变更 + sttEnabled + 乐观更新 + 迁移
  frontend/src/composables/useWebSocket.ts# 路由 service_mode_result + capabilities 随时处理
  frontend/src/components/panels/SettingsPanel.vue  # STT/TTS 模式 toggle
  frontend/src/components/input/InputBar.vue        # 麦克风门控 stt.available && stt.enabled
  frontend/src/stores/audioStore.ts       # ttsEnabled 防御 gate + 禁用取消播放

文档（Task11）：
  docs/frontend-api.md                    # capabilities 嵌套 + set_service/service_mode_result
```

---

### Task 1: ServiceState 模型 + SessionMemory 持久化贯通

**Files:**
- Create: `core/schema/service_state.py`
- Modify: `core/memory/session.py`
- Modify: `core/storage/session_repo.py`   <!-- 持久化链必需——session.save() 经 _repo.save 落库，缺它每次 save TypeError -->
- Modify: `core/input/debug_routes/archive.py`
- Test: `core/memory/tests/test_session.py`

**Interfaces:**
- Consumes: `CharacterState` 的字段习惯（`core/schema/character.py`）、`_repo.save/load`（`core/storage/session_repo.py`，save 有 `character_state=`/`reflector_state=` 命名参，load 返回 3 元组 `tuple[list[Message], CharacterState, MemoryReflectorState]`）。
- Produces: `session.service_state`（`ServiceState` 实例，自动建默认）；`session.save()` 写 `service_state`（MongoDB + JSONL fallback）；`session.load()` 返回 4 元组 `(messages, cs, rs, ss)` 并恢复 `service_state`；`SessionRepository.save` 加 `service_state: ServiceState` 参数、`load` 返回 4 元组。

- [ ] **Step 1: 写失败测试** — 在 `core/memory/tests/test_session.py` 追加（**test_session.py 无 fixture——现有测试全部内联 `mock_repo = MagicMock()`（:26/40/77/105/134）；round-trip 需 mock_repo.load 回显保存的 4 元组**，否则 load() 走 except → JSONL fallback → 断言必挂）：

```python
@pytest.mark.unit
async def test_service_state_roundtrip():
    repo = MagicMock()
    doc: dict = {}
    async def fake_save(sid, msgs, *, character_state=None, reflector_state=None, service_state=None, **kw):
        doc.update(messages=list(msgs), character_state=character_state,
                   reflector_state=reflector_state, service_state=service_state)
    async def fake_load(sid):
        return (doc.get("messages", []), doc.get("character_state"),
                doc.get("reflector_state"), doc.get("service_state"))   # 回显 4 元组
    repo.save = fake_save
    repo.load = fake_load

    s = SessionMemory(session_id="t", repo=repo)
    s.service_state.tts_enabled = False
    s.service_state.llm_model = "deepseek/deepseek-v4-pro"
    await s.save()
    loaded = SessionMemory(session_id="t", repo=repo)
    await loaded.load("t")
    assert loaded.service_state.tts_enabled is False
    assert loaded.service_state.llm_model == "deepseek/deepseek-v4-pro"
    fresh = SessionMemory(session_id="fresh")
    assert fresh.service_state.stt_enabled is None   # 默认实例三态 None，非 True
```

（`repo.save` 用 `*` 强制关键字匹配新签名 `service_state=`；若现有 save 调用链不兼容需按 `_repo.save(sid, msgs, character_state=..., reflector_state=...)` 现有调用核对。）

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/memory/tests/test_session.py -q`（当前无 `service_state`，AttributeError/断言失败）。

- [ ] **Step 3: 建 ServiceState 模型** — `core/schema/service_state.py`：

```python
"""服务运行时状态（session 专用字段）。"""
from pydantic import BaseModel


class ServiceState(BaseModel):
    """服务运行时状态（SessionMemory 专用字段，MongoDB 30d TTL，重启存活）。

    stt_enabled / tts_enabled 三态：None = 用户未显式改，回退 config.<svc>.enabled。
    """
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    stt_enabled: bool | None = None
    tts_enabled: bool | None = None
```

- [ ] **Step 4: SessionRepository 贯通（session_repo.py，缺它持久化链断）** — `save`（:102-104）签名加 `service_state: ServiceState | None = None` 参数（**必须带默认 None**——`core/storage/tests/test_session_repo.py:19/27/34` 的 `repo.save(sid, [], cs, rs)` 调用点不传它，无默认会 TypeError）；docstring 同步；`load`（:127-151）返回类型 `tuple[list[Message], CharacterState, MemoryReflectorState]` → 4 元 `(messages, cs, rs, ss)`，从 DB doc 读 `service_state` 构造（缺省 `ServiceState()`）。

- [ ] **Step 5: SessionMemory 加字段** — `session.py`：`__init__` 加 `service_state: ServiceState | None = None`，初始化 `self.service_state = service_state or ServiceState()`（仿 `character_state` 惯例，session.py:94）；`save()` 同步（session.py:248-249 的 `character_state=`/`reflector_state=` 旁加 `service_state=self.service_state`，JSONL fallback doc 同步加 `"service_state": self.service_state.model_dump(mode="json")`）；`load()`（session.py:361-368）`messages, cs, rs = result` → `messages, cs, rs, ss = result` 并恢复 `self.service_state`（仿 cs 的 field-by-field：`for field in type(ss).model_fields: setattr(self.service_state, field, getattr(ss, field))`）；`load_from_jsonl` 同步读 `service_state`。

- [ ] **Step 6: 改写既有 3 元 mock** — `test_session.py:136` `mock_repo.load = AsyncMock(return_value=([msg], cs, MemoryReflectorState()))` → 4 元 `([msg], cs, MemoryReflectorState(), ServiceState())`（否则 4 元解包后 ValueError 被 `except Exception` 吞掉 → fallback JSONL → 断言全挂）。

- [ ] **Step 7: 改写 archive.py 3 元解包** — `archive.py:193` `_, character_state, _ = result` → `_, character_state, _, _ = result`；`:223` 同。grep `= result` 确认无其他 3 元解包残留。

- [ ] **Step 8: 跑测试** — `python -m pytest core/memory/tests/test_session.py core/input/tests/test_archive*.py -q` 全绿（archive 测试文件如无则 `core/input/debug_routes/` 相关测试）。

- [ ] **Step 9: Commit** — `git add core/schema/service_state.py core/memory/session.py core/storage/session_repo.py core/input/debug_routes/archive.py core/memory/tests/test_session.py && git commit -m "feat(session): ServiceState 专用字段持久化贯通（save/load/JSONL + archive 4 元）"`

---

### Task 2: STTBackend provider_name/model + factory 传 provider

**Files:**
- Modify: `Module/stt/protocol.py`
- Modify: `Module/stt/registry.py`
- Modify: `Module/stt/sensevoice.py`
- Modify: `Module/stt/openai_stt.py`
- Test: `Module/stt/tests/test_stt.py`

**Interfaces:**
- Produces: `STTBackend.provider_name`（@property 抽象）、`STTBackend.model`（拼接 `provider_name/_model`）；`STTFactory.create("sensevoice/sense-voice")` 后端 `model` 属性返回完整 `"sensevoice/sense-voice"`。

- [ ] **Step 1: 写失败测试** — `Module/stt/tests/test_stt.py`：GoodBackend（:42-56）补 `provider_name` 实现，加：

```python
def test_model_composes_provider(self):
    class B(STTBackend):
        async def transcribe(self, audio, **kwargs): return ""
        async def transcribe_stream(self, audio, **kwargs): yield ""
        async def test(self): return {"ok": True, "latency_ms": 0}
        async def close(self): pass
        @property
        def provider_name(self): return "sensevoice"

    b = B()
    assert b.model == ""          # 无 _model → 空串
    b._model = "sense-voice"      # noqa: SLF001（测试直设后备字段）
    assert b.model == "sensevoice/sense-voice"
```

- [ ] **Step 2: 运行确认失败** — `python -m pytest Module/stt/tests/test_stt.py -q`（GoodBackend 因抽象 `provider_name` TypeError + 新测试失败）。

- [ ] **Step 3: protocol 补属性** — `protocol.py`：加 `@property @abstractmethod def provider_name(self) -> str: ...` 和具体 `model` property（`f"{self.provider_name}/{name}" if name else ""`，`name = getattr(self, "_model", "")`）。

- [ ] **Step 4: 后端补 provider_name** — `sensevoice.py`：`provider_name → "sensevoice"`；`openai_stt.py`（OpenAICompatSTTBackend）：`provider_name → "custom"`。

- [ ] **Step 5: registry 传 provider** — `registry.py`：`SenseVoiceBackend(model=model, ...)` → 加 `provider_name="sensevoice"`（或后端硬编码返回，二选一；**确保后端构造拿到 provider**）；custom 分支 OpenAiCompat 后端同理。**目标**：`STTFactory.create("sensevoice/sense-voice").model == "sensevoice/sense-voice"`。

- [ ] **Step 6: 跑测试** — `python -m pytest Module/stt/tests/test_stt.py -q` 全绿。

- [ ] **Step 7: Commit** — `git commit -m "feat(stt): STTBackend provider_name/model 补齐 + factory 传 provider"`

---

### Task 3: agent.stt 公开槽位 + stt_transcribe + 生命周期 None 复位

**Files:**
- Modify: `core/agent/agent.py`
- Modify: `core/agent/_lifecycle_services.py`
- Test: `core/agent/tests/test_public_interface.py` + `core/agent/tests/test_lifecycle_flush.py`

**Interfaces:**
- Produces: 公开只读 `agent.stt`（`STTBackend | None`）、`agent.stt_enabled`、`agent.tts_enabled`、`agent.stt_emotion_state`（dict 存储实例）；公开方法 `agent.stt_transcribe(audio)`（空守卫返回 ""）；`self._stt` 后备字段（写入用）。

- [ ] **Step 1: 写失败测试** — `test_public_interface.py` 加（**用现有 `agent` fixture，test_public_interface.py:48**；无 `agent_fixture` 存在）：

```python
@pytest.mark.unit
async def test_stt_slot_public_interface(agent):
    assert agent.stt is None                 # 初始 None
    assert agent.stt_enabled is False        # 三态回退：_stt_enabled=None → 有效 = config.stt.enabled
                                             #（AppConfig 默认 False；不用 bool 默认 true——spec 明令禁止）
    assert isinstance(agent.stt_emotion_state, dict)
    assert await agent.stt_transcribe(b"audio") == ""   # None 空守卫，不 AttributeError
```

（断言 `agent.stt` 只读无 setter——`with pytest.raises(AttributeError): agent.stt = None`；注入用 `agent._stt = fake`。）

- [ ] **Step 2: 运行确认失败** — `python -m pytest core/agent/tests/test_public_interface.py -q`。

- [ ] **Step 3: agent.py 加槽位** — `__init__` 加后备字段（**enabled 用 `None` 初始化，不用 bool 默认 true**——否则三态回退是死代码，且测试为错误原因通过）：`self._stt: STTBackend | None = None`、`self._stt_enabled: bool | None = None`、`self._tts_enabled: bool | None = None`、`self._stt_emotion_state: dict = {"emotion": "NEUTRAL", "events": []}`；4 个公开 `@property`（`stt → self._stt` / `stt_enabled → 三态回退` / `tts_enabled → 三态回退` / `stt_emotion_state → self._stt_emotion_state`）。**注意 `stt_emotion_state` property 与同名实例属性冲突——必须用 `_stt_emotion_state` 后备字段 + property 返回它**。`stt_enabled`/`tts_enabled` 的**有效语义**：`self._stt_enabled if self._stt_enabled is not None else self._config.stt.enabled`（property 内三态回退）。

- [ ] **Step 4: stt_transcribe** — agent.py 加：

```python
async def stt_transcribe(self, audio: bytes) -> str:
    """STT 转写（公开接口）。读取当前 agent.stt 后端，支持热切换。"""
    backend = self.stt
    if backend is None:
        return ""  # shutdown 窗口 _stop_* 置 None 后 _stop_output 才关 channel
    text = await backend.transcribe(audio)
    self.stt_emotion_state["emotion"] = getattr(backend, "last_emotion", "NEUTRAL")
    self.stt_emotion_state["events"] = list(getattr(backend, "last_events", []))
    return text
```

- [ ] **Step 5: 生命周期 None 复位** — `_lifecycle_services.py`：
  - `_stop_llm_and_tts`：TTS close 后 `self._tts = None` + `self._cognitive._tts_mgr.set_backend(None)`；**加** STT 分支：`if self._stt: await close; self._stt = None`。
  - `_stop_llm_and_tts` 里对 `_tts_mgr` 的访问——若 stop 时 loop 不在，用现有安全访问模式。

- [ ] **Step 6: 跑测试** — `python -m pytest core/agent/tests/test_public_interface.py core/agent/tests/test_lifecycle_flush.py -q` 全绿。

- [ ] **Step 7: Commit** — `git commit -m "feat(agent): agent.stt 公开槽位 + stt_transcribe + 生命周期 None 复位"`

---

### Task 4: switch_* 先建后关 + ServiceState 写入

**Files:**
- Modify: `core/adapter/llm_adapter.py`
- Modify: `core/agent/server.py`
- Test: `core/agent/tests/test_agent_methods.py`

**Interfaces:**
- Produces: `LLMAdapter.switch_model`（先建后换，create 失败旧后端不动）；`switch_llm/switch_stt/switch_tts`（先建后关 + 连通性测试 + 写 ServiceState.*_model + 不写 config）。

- [ ] **Step 1: 写失败测试** — `test_agent_methods.py` 加（**先补 import**：文件现只 import `MagicMock, AsyncMock` + `Agent`（:7-10）——测试用的 `mock.patch`/`ServiceState`/`LLMAdapter` 会 NameError/ImportError，红阶段因错误原因失败。文件头加 `import unittest.mock as mock`、`from core.schema.service_state import ServiceState`、`from core.adapter.llm_adapter import LLMAdapter`。`fake_agent` 是 `MagicMock()`（conftest.py:12），必须用绑定未绑定方法 `Agent.switch_stt(agent, ...)`（文件既有 `Agent.purge_message_from_session(fake_agent, ...)` 同款），并显式配置 `agent.session.save = AsyncMock()` + `agent.session.service_state = ServiceState()`（auto-mock 的 session 与 _session 是不同对象、不可 await））：

```python
@pytest.mark.service
async def test_switch_stt_writes_service_state_and_ok(fake_agent):
    agent = fake_agent
    agent._stt = None                            # 必须——auto-create 的 _stt truthy 会让"close 旧后端"走 await MagicMock.close() TypeError
    agent.session.save = mock.AsyncMock()        # 必须——真实 switch_stt await self.session.save()
    agent.session.service_state = ServiceState() # 必须——auto-mock 会建不同对象，写读不一致
    orig_cfg_model = agent.config.stt.model
    with mock.patch("Module.stt.STTFactory.create") as mc, \
         mock.patch("core.input.custom_providers.resolve_model_params", return_value={}):
        backend = mock.MagicMock(); backend.model = "sensevoice/sense-voice"
        backend.test = mock.AsyncMock(return_value={"ok": True, "latency_ms": 1})
        mc.return_value = backend
        result = await Agent.switch_stt(agent, "sensevoice/sense-voice")  # 绑定方法
    assert result["ok"] is True
    assert agent._stt is backend                 # 新后端已挂槽（switch_stt 写 self._stt）
    assert agent.session.service_state.stt_model == "sensevoice/sense-voice"
    assert agent.config.stt.model == orig_cfg_model   # config 不被改写（同对象 → == 成立）
    assert agent._stt_ok is True                 # 断言后备字段（agent.stt_ok 在 mock 上是 auto-mock，is True 恒 False）

@pytest.mark.service
async def test_switch_llm_create_failure_keeps_old(fake_agent):
    agent = fake_agent
    agent.session.save = mock.AsyncMock()
    agent.session.service_state = ServiceState()
    test_backend = mock.MagicMock()
    # 用 _test_backend 构造真实 LLMAdapter（llm_adapter.py:40 测试参）——避免构造时真建网络 client；
    # mock 的 _llm.switch_model 不会走 LLMFactory.create，无法测 create 失败
    agent._llm = LLMAdapter(model="deepseek/deepseek-v4-flash", _test_backend=test_backend)
    with mock.patch("Module.llm.LLMFactory.create", side_effect=RuntimeError("boom")):
        result = await Agent.switch_llm(agent, "deepseek/deepseek-v4-pro")  # 绑定方法
    assert result["ok"] is False
    assert agent._llm._backend is test_backend   # 先建后换：create 失败 _backend 未动
    # 关键：_requested_model 必须保留旧值——当前代码（llm_adapter.py:195-196）先
    # _requested_model = new 再 create，create 抛错时 _requested_model 被 clobber 成
    # "deepseek/deepseek-v4-pro"；先建后换保留旧值。**没有这条断言，该测试对现状代码也是绿的**
    # （_backend 未赋值 + ok is False 现状已成立），TDD 红阶段空转。
    assert agent._llm._requested_model == "deepseek/deepseek-v4-flash"
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: LLMAdapter.switch_model 先建后换** — llm_adapter.py:185-196 改为：`new_backend = LLMFactory.create(new_model_id, **kwargs)` 先建；成功后 `old, self._backend, self._requested_model = self._backend, new_backend, new_model_id`；`old.close()` fire-and-forget（`asyncio.get_running_loop().create_task(old.close())`，RuntimeError 时 pass）。

- [ ] **Step 4: switch_stt 重写** — server.py（当前 :111-122 只改 config）改为对齐 switch_tts：`STTFactory.create(model_id, **resolve_model_params("stt", model_id))` + 1s 连通性测试（`asyncio.wait_for(backend.test(), timeout=1.0)`，`not result.get("ok")` 抛错）→ **成功后才** close 旧 `self._stt` + `self._stt = backend` + `self._stt_ok = True` + 写 `ServiceState.stt_model` + `await session.save()`；失败 → 保留旧后端 + `_stt_ok = False` + `{"ok": False, "error": ...}`。

- [ ] **Step 5: switch_tts 改先建后关 + configure 先于 close** — server.py:124-151：建新 → `await new.test()` → **替换 `self._tts = new` → `self._cognitive.configure(tts=self._tts)` → 才 close 旧** → 写 `ServiceState.tts_model` + save。失败保留旧。

- [ ] **Step 6: switch_llm 写 ServiceState** — server.py:89-109：成功后 `ServiceState.llm_model = model_id` + `await session.save()`（不写 config）。

- [ ] **Step 7: 跑测试** — `python -m pytest core/agent/tests/test_agent_methods.py -q` 全绿。

- [ ] **Step 8: Commit** — `git commit -m "feat(agent): switch_* 先建后关 + 写 ServiceState（含 LLMAdapter 先建后换）"`

---

### Task 5: TTSStreamManager async set_enabled + 禁用 flush 机制 + loop 分支

**Files:**
- Modify: `core/adapter/tts_adapter.py`
- Modify: `core/cognitive/loop.py`
- Test: `core/adapter/tests/`（新建 `test_tts_adapter.py`）

**Interfaces:**
- Produces: `await _tts_mgr.set_enabled(bool)`（async def）；`_tts_mgr.enabled`；loop 972/979 分支 `active and enabled` / `not (active and enabled)`。**注意 `set_enabled` 必须 async 且所有调用点 await（Task 6/8 会用）。**

- [ ] **Step 1: 写失败测试** — `core/adapter/tests/test_tts_adapter.py`（新建，marker=unit）。**TTSStreamManager 构造签名 `(tts, output, character_name)` 三个必需位置参（tts_adapter.py:64-69）——测试必须传第三个（如 `"test"`），只传 2 个会 TypeError**。用 gate Event 保证取消发生在下载中途（确定性，非时序碰运气）：

```python
import asyncio
import pytest
from core.adapter.tts_adapter import TTSStreamManager

class _FakeTTS:
    def __init__(self, gate):
        self.gate = gate
    async def synthesize(self, text, **kw):
        await self.gate.wait()            # 挂起直到测试取消——保证任务停在下载窗口
        return b"RIFF" + b"\0" * 16
    def get_preprocess(self):
        return None
    @property
    def stream_format(self): return "wav"
    async def close(self): pass

class _FakeOutput:
    def __init__(self):
        self.texts = []                   # 已广播的 on_text_delta 文本
    async def _broadcast(self, kind, text, **kw):
        if kind == "on_text_delta":
            self.texts.append(text)
    async def send(self, marker=""): pass
    async def audio_chunk(self, data, **kw): pass
    async def audio_end(self, sid, **kw): pass
    async def close(self): pass

@pytest.mark.unit
async def test_set_enabled_false_flushes_pending_text():
    out = _FakeOutput()
    mgr = TTSStreamManager(_FakeTTS(asyncio.Event()), out, "test")
    mgr.stream_sentence("这是排队句")
    await asyncio.sleep(0)               # 任务启动，停在 synthesize 的 gate.wait()
    await mgr.set_enabled(False)         # 置位 → cancel → drain → flush_on_cancel 自刷
    assert "这是排队句" in out.texts     # 禁用兜底广播，排队未播句不丢

@pytest.mark.unit
async def test_active_stays_backend_and_enabled_gates():
    mgr = TTSStreamManager(None, _FakeOutput(), "test")
    assert mgr.active is False           # 无后端
    mgr.set_backend(_FakeTTS(asyncio.Event()))
    assert mgr.active is True            # active 保持"后端可用"语义
    await mgr.set_enabled(False)
    assert mgr.enabled is False
    assert mgr.active is True            # enabled 独立于 active，不重定义
```

（核心断言：禁用后排队未播句子文本不丢（`flush_on_cancel` 兜底）；`active` 不被重定义为 enabled。）

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: tts_adapter 改动**（全部按 spec §3）：
  1. `self._enabled = True` 字段；`async def set_enabled(self, enabled)`：置位 → 若 False：`pending = [t for t in self._tasks if not t.done()]`（**先捕获，cancel_all 会 clear**）→ 给每个 `t` 置 `t.flush_on_cancel = True` → `self.cancel_all()` → drain：
     ```python
     try:
         await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
         await self.flush_emojis()
     except asyncio.TimeoutError:
         logger.warning("TTS disable drain 超时，跳过 emoji flush")
     ```
  2. `stream_sentence`：enqueue 时算一次 `display_text`（`strip_display_hidden(sentence).strip()` + `STAGE_CN_RE` 舞台指示剥离），作为参数传入 `_download_and_forward(sid, seq, sentence, display_text, action_text)`；`_download_and_forward` 用传入的 display_text（不再内部重算）。
  3. `_download_and_forward`：`text_broadcast = False` 局部，文本广播前置 true；`except asyncio.CancelledError:` 分支改为（复用正常广播路径，tts_adapter.py:281-286 同款）：
     ```python
     except asyncio.CancelledError:
         if getattr(asyncio.current_task(), "flush_on_cancel", False) and not text_broadcast:
             await self._output._broadcast(
                 "on_text_delta", display_text,
                 capability=ChannelCapability.TEXT_DELTA, stream_id=sid)
             await self._output.send("")
         raise
     ```
  4. `stream_sentence` / `_download_and_forward` 开头加 `if not self._enabled: return` 守卫。
  5. `reset_for_turn` 复位 `_pending_emojis`（已有 clear，确认 `_enabled` 不在此处复位——`_enabled` 只由 set_enabled 控制）。

- [ ] **Step 4: loop.py 972/979** — 972 `if self._tts_mgr.active:` → `if self._tts_mgr.active and self._tts_mgr.enabled:`；979 `if not self._tts_mgr.active and event.text:` → `if not (self._tts_mgr.active and self._tts_mgr.enabled) and event.text:`（**不可统一替换**）。

- [ ] **Step 5: 跑测试** — `python -m pytest core/adapter/tests/test_tts_adapter.py core/cognitive/ -q` 全绿。

- [ ] **Step 6: Commit** — `git commit -m "feat(tts): async set_enabled + per-task flush_on_cancel 禁用兜底 + loop 分支"`

---

### Task 6: set_service 消息 + agent.set_service + service_mode_result

**Files:**
- Modify: `core/agent/server.py`
- Modify: `core/input/ws_handler.py`
- Test: `core/input/tests/test_server.py`

**Interfaces:**
- Consumes: Task 1（ServiceState）、Task 3（槽位）、Task 4（switch_* 补建）、Task 5（set_enabled）。
- Produces: `agent.set_service(service, enabled) -> dict`；`service_mode_result` 广播（含失败时 `enabled` 字段）。

- [ ] **Step 1: 写失败测试** — test_server.py 加（**用 `_make_agent()` helper + `create_app`，test_server.py 现有模式；enabled 断言是 `None` 不是 `False`——AppConfig 默认 `config.tts.enabled=False`（app_config.py:42），统一规则 `persist = enabled if enabled != cfg_enabled else None` → `False == False` → 写 None**；`create_app` 本任务仍用现有签名，Task 7 才改）：

```python
@pytest.mark.service
async def test_set_service_tts_toggles_and_broadcasts(aiohttp_client):
    agent = _make_agent()                    # AppConfig 默认 config.tts.enabled=False
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    async with client.session.ws_connect(client.make_url("/ws")) as ws:
        await ws.send_json({"type": "set_service", "service": "tts", "enabled": False})
        resp = None
        for _ in range(5):
            msg = await ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                resp = json.loads(msg.data); break
        assert resp is not None and resp["ok"] is True and resp["enabled"] is False
        assert agent.session.service_state.tts_enabled is None   # False == config(False) → 写 None
        assert agent._tts_enabled is False                        # 运行时槽位同步（原始 bool，非 None）

@pytest.mark.service
async def test_set_service_llm_rejected(aiohttp_client):
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    async with client.session.ws_connect(client.make_url("/ws")) as ws:
        await ws.send_json({"type": "set_service", "service": "llm", "enabled": False})
        resp = None
        for _ in range(5):
            msg = await ws.receive(timeout=5)
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                resp = json.loads(msg.data); break
        assert resp is not None and resp["ok"] is False   # llm 拒绝
```

（注意 `create_app` 现在仍收 `stt_transcribe`——Task 7 才改签名；本任务测试用现有签名。）

- [ ] **Step 2: 运行确认失败**（`agent.set_service` 不存在）。

- [ ] **Step 3: agent.set_service** — server.py 加（三分支 + 统一规则，spec §2 L133-142）：

```python
async def set_service(self, service: str, enabled: bool) -> dict:
    if service not in ("stt", "tts"):
        return {"ok": False, "error": "LLM 不可禁用"} if service == "llm" else {"ok": False, "error": f"未知 service: {service}"}
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "enabled 必须为 bool"}
    cfg_enabled = self._config.stt.enabled if service == "stt" else self._config.tts.enabled
    persist = enabled if enabled != cfg_enabled else None   # 统一规则：== config 写 None
    if enabled:
        backend = self._stt if service == "stt" else self._tts
        if backend is None:                                 # 补建（热插拔"开"路径）
            model = (self.session.service_state.stt_model or self._config.stt.model) \
                if service == "stt" else (self.session.service_state.tts_model or self._config.tts.model)
            if service == "stt":
                r = await self.switch_stt(model)
            else:
                r = await self.switch_tts(model)
            if not r["ok"]:
                return {"ok": False, "enabled": self.stt_enabled if service=="stt" else self.tts_enabled, "error": r.get("error")}
    # 置运行时槽位（stt 或 tts）
    if service == "stt":
        self._stt_enabled = enabled
    else:
        self._tts_enabled = enabled
        await self._cognitive._tts_mgr.set_enabled(enabled)   # noqa: SLF001（loop gate 唯一来源）
    # 持久化 enabled（补建成功后才写，失败已 return）
    setattr(self.session.service_state, f"{service}_enabled", persist)
    await self.session.save()
    # 重播 capabilities（Task 7 实现后替换为 build_capabilities 广播；此前可先跳过）
    return {"ok": True, "enabled": enabled}
```

（`await self._cognitive._tts_mgr.set_enabled` 处 `# noqa: SLF001` 豁免——内部接线非跨模块。capabilities 重播在 Task 7 接入。）

- [ ] **Step 4: ws_handler 路由** — `handle_json_message` 加分支：

```python
elif msg_type == "set_service":
    service = data.get("service", ""); enabled = data.get("enabled", False)
    result = await agent.set_service(service, enabled)
    await ws.send_json({"type": "service_mode_result", "service": service,
                        "enabled": result.get("enabled", enabled),
                        "ok": result.get("ok", False), "error": result.get("error")})
```

（广播给所有 channel 而非仅发送者——遍历 `agent.output._channels` + `isinstance(ch, WebSocketChannel)` + `await ch._send_json(...)`，见 Task 7 Step 6 的正确广播写法；本任务至少回给发送者。）

- [ ] **Step 5: 跑测试** — `python -m pytest core/input/tests/test_server.py -q` 全绿。

- [ ] **Step 6: Commit** — `git commit -m "feat(ws): set_service 消息 + agent.set_service + service_mode_result"`

---

### Task 7: capabilities 嵌套 + build_capabilities + 全量重播

**Files:**
- Create: `core/input/capabilities.py`
- Modify: `core/input/ws_handler.py`
- Modify: `core/input/server.py`
- Modify: `core/agent/server.py`
- Rewrite: `core/input/tests/test_server.py`（:239、:270、:282）

**Interfaces:**
- Consumes: Task 2（stt model）、Task 3（槽位）、Task 4（switch）、Task 6（set_service 重播点）。
- Produces: `build_capabilities(agent) -> dict`（嵌套 `services` 形状，available=false 时 `model: null`）；建连发送 + switch_*/set_service 成功后经 `agent.output` 全量广播。

- [ ] **Step 1: 写失败测试 / 改写既有断言** — test_server.py：
  - :239 `test_ws_connect_capabilities_includes_models`：`cap["llm_model"]` → `cap["services"]["llm"]["model"]`；`cap["stt_model"]` → `cap["services"]["stt"]["model"]`（未连接为 None）。
  - :270 `test_ws_connect_capabilities_stt_model_when_connected`：`cap["stt"] is True` → `cap["services"]["stt"]["available"] is True`；`cap["stt_model"]` → `cap["services"]["stt"]["model"]`（**fake_backend.model 必须等于 `agent.config.stt.model`**（默认 `"sensevoice/sense-voice"`）——嵌套断言 `model == config.stt.model`，值不符会挂）。
  - :282 `create_app(agent, serve_static=False, stt_transcribe=fake_transcribe)` → **移除 `stt_transcribe` 参数，改 `agent._stt = fake_backend`**（fake 实现 STTBackend：transcribe/transcribe_stream/test/close/provider_name/**`model`**——`build_capabilities` 读 `agent.stt.model`，缺 `model` 会 AttributeError 被 try/except 吞掉 → `cap is not None` 断言挂）。

- [ ] **Step 2: 运行确认失败**（形状变了断言挂）。

- [ ] **Step 3: 建 build_capabilities** — `core/input/capabilities.py`：

```python
def build_capabilities(agent) -> dict:
    """嵌套 per-service capabilities。available=false 时 model: null（不读 None.model）。"""
    def svc(available, enabled, model, default):
        return {"available": available, "enabled": enabled,
                "model": model if available else None, "default": default}
    cfg = agent.config
    return {
        "type": "capabilities",
        "live2d_static_image": cfg.live2d.static_image,
        "services": {
            "llm": svc(True, True, agent.llm.model, cfg.llm.model),
            "stt": svc(agent.stt is not None, agent.stt_enabled,
                       agent.stt.model if agent.stt else None, cfg.stt.model),
            "tts": svc(agent.tts is not None, agent.tts_enabled,
                       agent.tts.model if agent.tts else None, cfg.tts.model),
        },
    }
```

- [ ] **Step 4: ws_handler 建连改用** — ws_handler.py:67-77 的扁平 capabilities 改为 `await ws.send_json(build_capabilities(agent))`。

- [ ] **Step 5: create_app/ws_handler 签名变更（读 agent.stt 槽位）** — `core/input/server.py` `create_app` **删 `stt_transcribe`/`stt_emotion_state` 参数**（:282 改写依赖此签名）；`handle_ws` 改读 `agent.stt` + `agent.stt_transcribe` + `agent.stt_emotion_state`（建连时 `agent.stt is not None` 才建 audio_buf，STT 注入从 create_app 参数改为 fake `agent._stt`）；BINARY 分支加 `if audio_buf and agent.stt_enabled:` 防御（后端防御）。

- [ ] **Step 6: 重播** — server.py：`switch_llm/switch_stt/switch_tts` 成功 + `set_service` 成功后，经 `agent.output` 广播 `build_capabilities(agent)` 到所有 channel——**遍历 `agent.output._channels`，`isinstance(ch, WebSocketChannel)` 过滤（agent.py:113 无条件注册 ConsoleChannel，无 `_ws`），对每个 WebSocketChannel 调 `await ch._send_json(payload)`**（**WebSocketChannel 无公开 send——只有私有 `_send_json`，websocket.py:45**）。**不要"复用 switch_model_result 遍历模式"**——ws_handler.py:226-239 那个只回给请求方（`ch._ws is ws` + break），照抄会让重播只达一个客户端。

- [ ] **Step 7: 跑测试** — `python -m pytest core/input/tests/test_server.py -q` 全绿（:239/:270 嵌套断言 + :282 新签名 + set_service 重播）。

- [ ] **Step 8: Commit** — `git commit -m "feat(capabilities): 嵌套 per-service 形状 + build_capabilities + ws_handler 读 agent.stt 槽位 + 重播"`

---

### Task 8: _apply_service_state 启动步骤 + 建门 + run_server STT 块 + banner

**Files:**
- Modify: `core/agent/lifecycle.py`
- Modify: `core/agent/server.py`
- Modify: `core/agent/_lifecycle_services.py`
- Test: `core/agent/tests/test_lifecycle_steps.py` + `test_server_banner.py`

**Interfaces:**
- Consumes: Tasks 1-7。
- Produces: `_STARTUP_STEPS` 加 `("service_state", "_apply_service_state")`；`_start_tts` 建门 + startup_tts_model；run_server STT 块显式 `self._stt = 后端` + 建门；banner 读 `agent.stt`。

- [ ] **Step 1: 写失败测试** — `test_lifecycle_steps.py` 加：mock session 的 service_state 有 `tts_model`/`stt_enabled` → 断言 `_apply_service_state` 被调用（step 序）且 `_start_tts` 用 startup model；`test_server_banner.py`：加测**新抽出的 `AgentServer._stt_banner_status(fake_agent)`**（现有 `_print_startup_banner` 不含 STT 行——STT 行在 run_server 内联 server_info，必须先抽出才能测）。

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: _apply_service_state（放 `_LifecycleServices` mixin，`core/agent/_lifecycle_services.py`——**不能放 server.py（AgentServer）：`_STARTUP_STEPS` 的数据驱动循环按 mixin 解析方法名，放错类 test_lifecycle_steps.py:52 会解析失败**）**（spec §6 L220-223）：

```python
async def _apply_service_state(self) -> None:
    """启动步骤：读 ServiceState → 设模式槽位 → LLM 应用。位于 tts 步骤前。"""
    ss = self.session.service_state
    self._stt_enabled = ss.stt_enabled if ss.stt_enabled is not None else self._config.stt.enabled
    self._tts_enabled = ss.tts_enabled if ss.tts_enabled is not None else self._config.tts.enabled
    if ss.llm_model and ss.llm_model != self._config.llm.model:
        await self.switch_llm(ss.llm_model)
```

- [ ] **Step 4: _STARTUP_STEPS 加步骤** — lifecycle.py:71-80 在 `("tts", "_start_tts")` **之前**插入 `("service_state", "_apply_service_state")`。

- [ ] **Step 5: _start_tts 建门 + 模型** — `_lifecycle_services.py`：

```python
async def _start_tts(self) -> None:
    ss = self.session.service_state
    model = ss.tts_model or self._config.tts.model        # startup_tts_model
    gate = ss.tts_enabled if ss.tts_enabled is not None else self._config.tts.enabled  # 有效 enabled
    if gate:
        from Module.tts import TTSFactory
        from core.input.custom_providers import resolve_model_params
        try:
            # 关键：custom/<name> 必须经 resolve_model_params 补 base_url（registry.py:73-75
            # 对 custom 无 base_url 抛 ValueError——裸 create 会让运行时切好的 custom TTS
            # 重启后静默失效，与 STT 同款 bug）
            self._tts = TTSFactory.create(
                model, **resolve_model_params("tts", model),
                rate_limit_rpm=self._config.tts.rate_limit_rpm)
            import asyncio as _asyncio
            result = await _asyncio.wait_for(self._tts.test(), timeout=1.0)
            if result.get("ok"):
                self._cognitive.configure(tts=self._tts)
                await self._cognitive._tts_mgr.set_enabled(self._tts_enabled)  # noqa: SLF001
            else:
                self._tts = None
        except Exception:
            self._tts = None
```

- [ ] **Step 6: run_server STT 块改写** — server.py:174-200：删局部 `stt_backend`/`transcribe_wrapper`/`stt_emotion_state`，改为：

```python
ss = self.session.service_state
stt_model = ss.stt_model or self._config.stt.model
gate = ss.stt_enabled if ss.stt_enabled is not None else self._config.stt.enabled
if gate:
    try:
        from Module.stt import STTFactory
        from core.input.custom_providers import resolve_model_params
        # 关键：custom/<name> 必须经 resolve_model_params 补 base_url（registry 对 custom 无
        # base_url 抛 ValueError，裸 create 会让运行时切好的 custom STT 重启后 available=false）
        stt_backend = STTFactory.create(stt_model, **resolve_model_params("stt", stt_model))
        result = await asyncio.wait_for(stt_backend.test(), timeout=1.0)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "服务不可用"))
        self._stt = stt_backend                # 显式赋槽位（只建一个）
        self._stt_ok = True
    except Exception:
        self._stt = None                       # 失败复位，防 stale
        self._stt_ok = False
```

`create_app` 调用不再传 `stt_transcribe`/`stt_emotion_state`（Task 7 已改签名）；**banner：抽出可测方法 `AgentServer._stt_banner_status()`**（返回 `"已连接 (model=...)"` / `"未连接"`，读 `self._stt` + `self._stt.model`），run_server 的 server_info 调它（:221），test_server_banner.py 测它——STT 行在 run_server 内联无法直接测，必须先抽出。

- [ ] **Step 7: 跑测试** — `python -m pytest core/agent/tests/test_lifecycle_steps.py core/agent/tests/test_server_banner.py core/input/tests/test_server.py -q` 全绿。**跑 `make lint`。**

- [ ] **Step 8: Commit** — `git commit -m "feat(lifecycle): _apply_service_state 启动步骤 + 建门 + run_server STT 槽位"`

---

### Task 9: 前端协议 + 状态（protocol.ts / settingsStore / useWebSocket）

**Files:**
- Modify: `frontend/src/types/protocol.ts`
- Modify: `frontend/src/stores/settingsStore.ts`
- Modify: `frontend/src/composables/useWebSocket.ts`
- Verify: `cd frontend && npx vue-tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: 后端 Task 6/7 的协议（`set_service` / `service_mode_result` / 嵌套 capabilities）。
- Produces: 前端 store 的 `ttsEnabled`/`sttEnabled`（后端模式，乐观更新）、`updateCapabilities`（幂等可随时调）。

- [ ] **Step 1: protocol.ts** — `Capabilities` 类型改嵌套：

```ts
export interface ServiceCapability {
  available: boolean
  enabled: boolean
  model: string | null
  default: string
}
export interface Capabilities {
  type: 'capabilities'
  live2d_static_image: string
  services: { llm: ServiceCapability; stt: ServiceCapability; tts: ServiceCapability }
}
export interface SetService { type: 'set_service'; service: 'stt' | 'tts'; enabled: boolean }
export interface ServiceModeResult { type: 'service_mode_result'; service: 'stt' | 'tts' | 'llm'; enabled: boolean; ok: boolean; error: string | null }
```

- [ ] **Step 2: settingsStore.ts** —
  - `ttsEnabled` 改为后端模式：初始 `ref(localStorage.getItem('elysia_tts') !== 'off')` 保留（迁移用），但 **capabilities `services.tts.enabled` 播种覆盖**。
  - 新增 `sttEnabled`。
  - `updateCapabilities(msg)`：改嵌套——`services.stt/tts.enabled → sttEnabled/ttsEnabled`；`available/model/default` 全量回填；**幂等、无"仅建连"假设**。
  - 新增 action `setService(service, enabled)`：乐观写 ref + localStorage → `send({ type: 'set_service', service, enabled })`；收到 `service_mode_result.ok=false` 回滚（靠 store 里注册的处理或路由层）。
  - **旧值迁移**：首次建连且 `localStorage.getItem('elysia_tts') === 'off'` → 播种 `ttsEnabled=false` + `setService('tts', false)`。
  - 新增 `llmDefault`/`sttDefault`/`ttsDefault`（capabilities `default`）。

- [ ] **Step 3: useWebSocket.ts** — 路由 `service_mode_result`（按 service 更新 sttEnabled/ttsEnabled，`ok=false` 时回滚）；`capabilities` **到达都调 `settingsStore.updateCapabilities`**（**"任何时刻"已是现状 useWebSocket.ts:55**；真正变化是 `updateCapabilities` 处理嵌套 `services` 形状并播种 sttEnabled/ttsEnabled——见 settingsStore 改动）。

- [ ] **Step 4: 验证** — `cd frontend && npx vue-tsc --noEmit && npm run build`（`/debug` 应用引用了旧 capabilities 形状的检查一并修）。

- [ ] **Step 5: Commit** — `git commit -m "feat(frontend): capabilities 嵌套类型 + set_service/service_mode_result 路由 + settings 后端模式"`

---

### Task 10: 前端 UI（SettingsPanel / InputBar / audioStore）

**Files:**
- Modify: `frontend/src/components/panels/SettingsPanel.vue`
- Modify: `frontend/src/components/input/InputBar.vue`
- Modify: `frontend/src/stores/audioStore.ts`
- Verify: `cd frontend && npx vue-tsc --noEmit && npm run build`

**Interfaces:**
- Consumes: Task 9 的 store API。

- [ ] **Step 1: SettingsPanel.vue** — 新增「STT 语音」toggle（`v-model="settings.sttEnabled"`，`@change` 调 `settings.setService('stt', $event)`）；「TTS 语音」toggle 改 `settings.setService('tts', $event)`（不再纯本地）；**toggle 在 `available=false` 时不禁用**（启用即触发后端补建；仅当该服务完全无 provider 时禁用并引导）。模型下拉显示「当前/默认」（可选「恢复默认」→ `switchModel(service, default)`）。

- [ ] **Step 2: InputBar.vue** — 麦克风门控改 `settings.sttAvailable && settings.sttEnabled`（替代仅 `sttAvailable`）；`sttEnabled` 变 false 且正在录音 → 停录音 + 停发 BINARY。

- [ ] **Step 3: audioStore.ts** — `if (!settingsStore.ttsEnabled) return` gate 保留（防御）；监听 `ttsEnabled` 变 false 且正在播放 → 取消当前播放 + 清队列。

- [ ] **Step 4: 验证** — `cd frontend && npx vue-tsc --noEmit && npm run build`。

- [ ] **Step 5: Commit** — `git commit -m "feat(frontend): 设置面板 STT/TTS 模式 toggle + 麦克风门控 + 音频防御"`

---

### Task 11: 文档（frontend-api.md 协议变更）

**Files:**
- Modify: `docs/frontend-api.md`

- [ ] **Step 1: 更新 capabilities 形状** — frontend-api.md `capabilities` 章节改嵌套 `services` 形状（`{available, enabled, model, default}` per service + `live2d_static_image`），标注 **破坏性变更** + 建连 + 状态变化重播。

- [ ] **Step 2: 新增消息** — Client→Server `set_service {service, enabled}`；Server→Client `service_mode_result {service, enabled, ok, error}`（含失败带 `enabled`）。

- [ ] **Step 3: 更新 .NET 接入要点** — 模型切换/设置部分引用嵌套形状。

- [ ] **Step 4: Commit** — `git commit -m "docs: frontend-api capabilities 嵌套 + set_service/service_mode_result"`

---

### Task 12: 全量回归

- [ ] **Step 1: make test-quick 全绿** — `conda activate Elysia && make test-quick`。
- [ ] **Step 2: make lint** — `make lint`。
- [ ] **Step 3: 前端构建** — `cd frontend && npx vue-tsc --noEmit && npm run build`。
- [ ] **Step 4: 一致性 grep** — 扁平 `cap["*"]` 残留、`stt_transcribe=` 调用点残留、`set_param/get_param` 无新增。若失败回退对应 Task 修。
