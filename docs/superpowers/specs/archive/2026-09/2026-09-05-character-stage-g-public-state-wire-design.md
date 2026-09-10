# 阶段 G:CharacterPublicState 实际发送(v2 state wire)Spec

> **状态:** 方案已定案(三决策自动采纳,见 §0 决策记录)
> **架构稿:** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../../../2026-08-30-character-prompt-customization-backend-architecture-design.md)(§12.3 端点矩阵 / §12.4 state event / §12.8 放量顺序 / §5.8 RuntimeState)
> **fact 基线:** 阶段 F 完成后 HEAD `2ee1453d`
> **上一批:** 阶段 F(记忆 v2 隔离)——`long_term_memory_v2` / `MemoryScope` / `ScopedLongTermMemory` 落地;`CharacterRuntimeState` 已含 mr_* 计数器字段
> **本批交付:** v2 `character.state.changed` 事件 + REST `GET /api/character-instances/{id}/state`(产品态只读),`character_state_stream=true`,`character_profiles` 恒 false

## 0. 一句话目标

把 **CharacterRuntimeState(内部扁平态)→ CharacterPublicState(产品裁剪视图)** 的 wire 发射做出来:后端在 WS 新连接/会话激活时推 `character.state.changed` 快照、状态实际变化时再推增量;并提供只读 REST `GET /api/character-instances/{id}/state`。开启 `character_state_stream=true`,但不开启 `character_profiles`(UI 角色选择仍关,归阶段 H)。

## 1. 背景与问题

### 1.1 阶段 E/F 已交付的底座(已核实)

阶段 E(`main@4089d30f`→`2ee1453d` 前)+ 阶段 F 已交付:

- **`CharacterRuntimeState`**(core/schema/character.py:37)**instance-scoped 权威态**:`mood` / `mood_intensity` / `updated_at`(IsoTimestamp)等扁平字段与 `CharacterState` 同构,子组件 0 改动。**无 proactive 字段**(proactive 是角色配置,在 revision.runtimeDefaults,见 §5.2/D6)。
- **`CharacterPublicState` DTO**(frontend_contract/rest.py:598)已契约建模:扁平 `characterId/instanceId/characterRevision/mood/stateVersion`。**仅契约建模,无 wire 用途**。
- **generation 审计字段已完整接线**(阶段 E):`GenerationContext.character_id/character_instance_id/character_revision`(loop.py:482-484)→ `OutputManager.turn_*` 透传(manager.py:427/558/590)→ websocket.py 构造进 `generation.started/completed/failed` 帧。**本批不重复做 generation 身份**。
- **`CharacterRuntimeRegistry`**(core/character/registry.py):`get(instance_id, factory)` 解析/懒建 per-instance runtime;runtime 持有 `snapshot_state()` / `flush()`。
- **`CharacterInstanceRepository`**(core/storage/character_instance_repo.py):`character_instances` 集合 instance CRUD + `state_version` CAS。
- **`ServerInfoFeatures`**(frontend_contract/ws_message.py:264):有 `character_profiles`(恒 false),**无 `character_state_stream` 字段**——G 需新增。

### 1.2 缺口:state 产品态 wire 完全未做(已核实)

1. **`character.state.changed` WS event 缺失**:无 `CharacterStateChangedMessage` DTO、无发射点。架构稿 §12.4 定义了事件形状(含 `state: CharacterPublicStateView`)。
2. **`CharacterPublicStateView` 子结构缺失**:rest.py 的 `CharacterPublicState` 只有扁平 `mood: str`,缺 §12.4 要求的 `{mood:{label,intensity,updatedAt}, currentActivity, lastInteractionAt, proactiveEnabled, updatedAt}`。REST 与 WS 共享同一产品态形状。
3. **REST `GET /api/character-instances/{id}/state` 缺失**:架构稿 §12.3 矩阵,无路由。
4. **`character_state_stream` flag 字段缺失**:`ServerInfoFeatures` 无此字段,`build_server_info`(capabilities.py:107)未设。

### 1.3 放量顺序(架构稿 §12.8)的约束

§12.8 明确:
- 步骤 4「Prompt/runtime/memory 隔离门禁通过后才置 `character_profiles=true`」——**阶段 E/F 已把 Prompt/runtime/memory/记忆 scope 四道门禁全过**,但 CE spec §10 把 `character_profiles=true` 归 **阶段 H**(含旧数据迁移 + 前端联调)。故 G **保持 `character_profiles=false`**。
- 步骤 5「state snapshot/重连路径通过后才置 `character_state_stream=true`」——G 本批就实现 snapshot/重连路径,故 G 完成后**置 `character_state_stream=true`**。

## 2. 交付边界

### 本批做

1. **`CharacterPublicStateView`** 新增(frontend_contract):产品态子结构 `{mood:{label,intensity,updatedAt}, currentActivity?, lastInteractionAt?, proactiveEnabled, updatedAt}`。作为 `CharacterPublicState` 的 `state` 字段 + `CharacterStateChangedMessage` 的 `state` 字段共享类型(契约层零依赖,不引 core)。
2. **`CharacterStateChangedMessage`** 新增(frontend_contract/ws_message.py):wire `character.state.changed`,含 `conversationId/characterId/characterInstanceId/characterRevision/stateVersion/state(CharacterPublicStateView)`。**加入 `ServerMessage` 判别联合**(ws_message.py:1040-1083),而非伪造 `__all__`([修复 C2])。
3. **`CharacterPublicState` 泛化**(frontend_contract/rest.py):由扁平 `mood:str` 改为含 `state: CharacterPublicStateView` 的产品态(与架构稿 §12.3 的 REST `GET state` 响应形状对齐)。保留 `characterId/characterRevision/stateVersion` 顶层;`instanceId` **改名为 `characterInstanceId`**([修复 C4],对齐 WS 事件与架构稿 §12.4);新增 `state` 子结构。
   - 兼容性注:扁平的 `mood: str` 是阶段 C+E 契约建模产物,无 wire 消费者;改为含 `state` 子结构是契约演进,需 `make gen-types` 重生成 TS/schema/raw_ts。**决策记录 D1**。
4. **`CharacterRuntimeState → CharacterPublicState` 映射 helper**(core/character/):`make_public_state(runtime)` 或 `runtime.public_state()` ——从 runtime snapshot 读 mood/mood_intensity/updated_at,映射到产品态。**mood.updatedAt / lastInteractionAt 用 `updated_at` 派生,不新增 state 字段**。决策记录 D2。
   - 产品态裁剪逻辑:只暴露 `mood/currentActivity/lastInteractionAt/proactiveEnabled/updatedAt`,**不暴露** `self_l2/state_l3/self_perception_history/reflector_buffer` 等内部态。
   - `stateVersion` 从 `runtime.stateVersion`(CharacterRuntimeState 有 state_version 字段,复用 instance 的 CAS 计数;见 core/character/runtime.py:51)。
5. **WS snapshot 发射**(core/input/ws_handler.py):协议 v2 且 `character_state_stream=true` 时,新连接/`activate_session` 成功后发一条 `character.state.changed`(初始快照),含当前活动会话的 instance 产品态。
6. **WS 状态变化发射**(core/cognitive/finalize.py 回合边界):turn 完成、mood 变化时,若 v2 + flag 开,发 `character.state.changed`(仅状态实际变化时——见 §3 变化检测)。**变化检测**:比较 `(mood.label, mood.intensity)` 与上次快照,无变化不发射(proactiveEnabled 是常量不参与;[修复 C5])。决策记录 D3。
7. **REST `GET /api/character-instances/{id}/state`**(core/input/character_routes.py 的 `register_character_routes` 内):返回产品态 `CharacterPublicState`。registry 命中即读;**未命中不懒建** → fail-closed 抛 StorageUnavailableError → 503(对齐附件 repo 的 BE 先例;[修复 A1/C1])。
8. **`ServerInfoFeatures.character_state_stream` 字段** + `build_server_info` 设 `character_state_stream=True`(恒 true,见 D4)。`character_profiles=False` 保持。
9. **前端 Electron 无本批联调**(归 H)。

### 本批不做(明确排除)

- **`character_profiles=true`**:UI 角色选择/多角色 Profile 仍关(CE spec §10 归 H,含旧数据迁移 + 前端联调)。
- **`reset-evolution` 写端点**(`POST /api/character-instances/{id}/reset-evolution`):涉及写权限 + instance busy 409 + 前端 UI,归 H。
- **前端 Electron 联调**:本批仅生成 TS/schema/golden。
- **旧数据迁移**:归 H。
- **expression/live2d.directive/audio_start 的角色身份字段**(§12.5/§12.7):本批只做 state 事件;expression/audio 身份字段是独立批次(或归 H 联调前)。**决策记录 D5**。
- **`character.catalog.changed` 事件**(§12.6):归 H(自定义角色保存/归档的前端缓存同步)。

## 3. 架构方案

### 3.1 数据流

```
[CharacterRuntimeState(内部扁平,mood/mood_intensity/updated_at)]
        ↓ make_public_state()  (core/character/, 裁剪产品态)
[CharacterPublicStateView(mood{label,intensity,updatedAt}, proactiveEnabled, updatedAt)]
        ↓
 ┌──────────────┬─────────────────┐
 │ WS snapshot   │ WS 变化事件      │
 │ (新连接/激活) │ (回合边界,有变才发)│
 └──────────────┴─────────────────┘
        ↓ CharacterStateChangedMessage(state=view, characterId, characterInstanceId,
        │      characterRevision, stateVersion)
[Electron 前端 Live2D/状态展示]
```

REST 只读路径:`GET /api/character-instances/{id}/state` → registry 命中 → runtime.snapshot_state() → make_public_state() → `CharacterPublicState`(未命中 fail-closed 503,不懒建)。

### 3.2 变化检测(关键,防事件风暴)

架构稿 §12.4「仅在实际状态变化时发新的完整公开视图」。实现方式:

- runtime 维护 `_last_public_state`(或内存 dict:instance_id → 上次发射的产品态 hash)。
- 回合边界(或 mood 变化时)调 `make_public_state()` 得新 view;与 `_last_public_state` 比较**关键字段**。
- **比较字段**(避免误报):`mood.label`, `mood.intensity`。**不比较 `proactiveEnabled`**(它是不可变角色配置,恒不变化——见 D6,比较它无意义);**不比较 `updated_at`**——它是展示字段,不驱动发射与否。仅当 mood 真变了才发射;`updatedAt`(视图里的)取发射时的当前值作展示。
- 无变化 → 不发射;**有变化** → 发射完整 view + 更新 `_last_public_state`。
- 新连接/激活的 snapshot 强制发射(不管是否与 last 相同——客户端刚连上需要初始态)。
- **`updatedAt` 在视图里的语义**:发射时取 `runtime.instance.updatedAt` 当前值(每 flush 更新),作展示用;但它**不驱动发射与否**([修复 B1]——不能用 `state.updated_at`,它冻结于构造)。

> 注:§3.3 stateVersion 时序、§5.1 的 mood.updatedAt / lastInteractionAt 派生字段,与本节「不比较 updated_at」不冲突——`updated_at` 用于**视图填充**,但不作为**变化信号**。

### 3.3 stateVersion 数据源

`CharacterRuntimeState` 不含独立 state_version 字段(见 core/schema/character.py:37-63,只有 `updated_at`)。架构稿 §12.4 要求严格递增的 `stateVersion`。数据源:
- `CharacterRuntime`(core/character/runtime.py:51)`__init__` 时 `self.stateVersion = instance.stateVersion` ——用 `CharacterInstance.stateVersion` 的 CAS 计数。
- 但**每次 state 变化都要递增 stateVersion**——当前回合边界 flush 到 `CharacterInstance.state` 时才 CAS 递增。G 的 state event 发射点若在 flush 前,stateVersion 可能滞后。
- **决策记录 D2 补充**:state event 的 stateVersion 用 `runtime.stateVersion` 当前值;若回合边界 flush 时递增(经 `CharacterInstanceRepository.update` CAS),则 event 在 flush 后发射可拿到新值。**G 在 spec 里明确发射点时序:turn 收尾(flush 调完)后发射**,保证 stateVersion 单调。

## 4. 契约层(阶段 G)

### 4.1 新增 `CharacterPublicStateView`(frontend_contract/rest.py)

```python
class CharacterPublicStateView(ContractModel):
    """角色产品态子视图(架构稿 §12.4;REST 与 WS 共享)。"""
    mood: MoodState  # {label, intensity, updatedAt}
    currentActivity: Optional[str] = None
    lastInteractionAt: Optional[str] = None
    proactiveEnabled: bool = False
    updatedAt: str
```

（注[N1]:`mood` 本批**必填**——产品态恒有 mood(`CharacterState.mood` 默认「平静」)。架构稿 §12.4 标 `mood?` 可选是「放量期可缺省」约定,本批实现时必填可行,与现有 `ExpressionMessage`/`turn_completed.mood` 必填惯例一致。）

### 4.2 新增 `MoodState`(frontend_contract/rest.py)

```python
class MoodState(ContractModel):
    """心情产品态(架构稿 §12.4)。"""
    label: str
    intensity: float
    updatedAt: str
```

### 4.3 新增 `CharacterStateChangedMessage`(frontend_contract/ws_message.py)

```python
class CharacterStateChangedMessage(ContractModel):
    """角色状态变化事件(架构稿 §12.4;v2,character_state_stream=true 后发送)。"""
    type: Literal["character.state.changed"] = "character.state.changed"
    conversationId: Optional[str] = None
    characterId: str
    characterInstanceId: str
    characterRevision: int = 1
    stateVersion: int = 1
    state: CharacterPublicStateView
```

### 4.4 `CharacterPublicState` 泛化(frontend_contract/rest.py)

由扁平 `mood: str` 改为:

```python
class CharacterPublicState(ContractModel):
    """角色产品态(REST GET /api/character-instances/{id}/state)。"""
    characterId: str
    characterInstanceId: str
    characterRevision: int = 1
    stateVersion: int = 1
    state: CharacterPublicStateView
```

(去掉扁平 `mood: str`;保留 `characterId/characterRevision/stateVersion`;`instanceId` **改名为 `characterInstanceId`**——修复 C4,对齐架构稿 §12.4 的 `CharacterPublicState` 与 WS `CharacterStateChangedMessage`,避免 REST/WS 同一实例身份用两个字段名。)

**契约演进确认**:`CharacterPublicState` 的 `mood` 由 `str` 变 `CharacterPublicStateView`、`instanceId` 改名 `characterInstanceId`,是 breaking change,但**无 wire 消费者**(rest.py:598 注释「本批仅契约建模」;frontend/src/types/contract.generated.ts:1733 的 `CharacterPublicState` 接口无前端代码消费 `.mood`/`.instanceId`),`make gen-types` 重生成即可。须同步更新 `frontend_contract/tests/test_stage_ce_contract.py` (`test_character_public_state_dto`,:72-82)——**`instanceId="inst_1"`(:73)/`dto.instanceId`(:75)** 改名 `characterInstanceId`,**:77 `dto.mood == "平静"`** 改为读 `dto.state.mood.label`。

### 4.5 `ServerInfoFeatures.character_state_stream`(frontend_contract/ws_message.py)

```python
character_state_stream: bool = Field(default=False, description="角色状态流(阶段 G;character.state.changed 事件)")
```

### 4.6 上 wire 列表

- [修复 C2] `ServerMessage` 判别联合(`frontend_contract/ws_message.py:1040-1083`)**必须加入** `CharacterStateChangedMessage`——否则 `gen_ts`(经 `__main__.py:77-81` `get_args(ServerMessage)`)不生成 TS 判别成员、`gen_schema` 不产对应 `oneOf`、契约层也无法 `validate_python`。ws_message.py **无 `__all__`**(review 核实 `grep -c "__all__"` = 0),spec 原引用的 `ws_message.py:1043 __all__` 是错误的——真正要求是加入 `ServerMessage` union。
- `frontend_contract/__init__.py` 加 `CharacterStateChangedMessage` / `CharacterPublicStateView` / `MoodState`(该文件有 `__all__`,:181 与 re-export :90/:322 需同步,见 §9 契约测试)。

### 4.7 golden frame 调整

- `scripts/gen_contract_frames.py` 的 `_server_info()`(:43-65)需**显式加 `character_state_stream=True`**(与 `idempotency_durable=True` 同风格)。**原因**:`ServerInfoFeatures` 的 bool 字段经 `wire_dict()` 序列化时**不排除 `False`**(只 `exclude_none`),导致默认 false 的 `character_state_stream` 会以 `"character_state_stream": false` 出现在 wire。若构造时不显式设 true,则该字段在 frame 里为 false,`test_cancel_and_fail_are_current_contract_goldens`(:73)的 `all(features.values())` 会把它算成 false 而挂掉——故必须显式设 true。
- `scripts/tests/test_gen_contract_frames.py` 的 `test_cancel_and_fail_are_current_contract_goldens`(:71-73)排除逻辑需追加 `character_state_stream`(与 `character_profiles` 同列,因为该 frame 是「当前契约能力全开」的 golden;`character_state_stream` 本批应 true 而非 false,故**不需排除**——但若 golden frame 构造未加该字段,test 会因默认 false 挂。安全起见:frame 构造显式 true,test 排除逻辑不变)。**决策:D4 已定 character_state_stream 恒 true,故 frame 构造加 `character_state_stream=True`;`all(features.values())` 不含该字段的 false 分支,test 排除逻辑维持仅 `character_profiles`。**
- 新增 `character.state.changed` golden frame(可选,若需在 `ws-v2-chat.jsonl` 或新 `ws-v2-state.jsonl` 中覆盖该事件)。**决策记录 D7**:本批在 `ws-v2-chat.jsonl` 中追加该事件帧(chat 完整序列末尾),供前端离线对拍。

## 5. 数据源映射(core/character/)

### 5.1 `make_public_state(runtime) -> CharacterPublicStateView`

从 `runtime.snapshot_state()`(CharacterRuntimeState)读。**内部态(如 `self_l2`/`state_l3`/`self_perception_history`/`reflector_buffer`)绝不外泄**——产品态只允许 §12.4 声明的字段。

| 产品态字段 | 内部数据源 | 注 |
|-----------|-----------|----|
| `mood.label` | `state.mood` | 字符串心情 |
| `mood.intensity` | `state.mood_intensity` | float |
| `mood.updatedAt` | `state.updated_at` | 派生——无独立 mood 时间戳(**D2**),作近似;见下方注 |
| `currentActivity` | 本批恒 `None` | 架构稿定义,现无数据源,留空 |
| `lastInteractionAt` | `runtime.instance.updatedAt` | **每 flush 更新时间**(agent.py:432 `inst.updatedAt = now_iso()`)——见 B1 修复 |
| `proactiveEnabled` | `runtime.revision.runtimeDefaults["proactive"]["enabled"]` | 见 §5.2;不可变配置 |
| `updatedAt` | `runtime.instance.updatedAt` | **每 flush 更新**;见 B1 修复 |

> **注(B1 修复)**:`mood.updatedAt`/`lastInteractionAt`/`updatedAt` 不能统一用 `state.updated_at`(CharacterRuntimeState)——它只在构造时赋值一次,回合中从不刷新(agent.py:432 更新的是 `instance.updatedAt`,不是 `state.updated_at`)。故 `lastInteractionAt`/`updatedAt` 改从 `runtime.instance.updatedAt`(每 flush 更新)派生;`mood.updatedAt` 无独立 mood 时间戳,暂用 `state.updated_at` 近似(若要求精确,后续给 CharacterRuntimeState 加 `mood_updated_at` 字段——本批不做,见 D2 注)。

### 5.2 proactiveEnabled 数据源

架构稿 §12.4 `proactiveEnabled` 是产品态字段。当前 `CharacterRuntimeState` 无 proactive 字段,且它是**不可变角色配置**(instance immutable,非 runtime 演化态)。数据源:

- `make_public_state(runtime)` 直接读 `runtime.revision.runtimeDefaults.get("proactive", {}).get("enabled", False)`(revision.py:25 + builtin_source.py:58 已含该值)——**无需 agent/config 句柄**,runtime 持有 revision。

`make_public_state` 签名建议:`make_public_state(runtime) -> CharacterPublicStateView`,内部从 `runtime` 取 `instance`(updatedAt)/`revision`(runtimeDefaults)/`state`(mood, mood_intensity, updated_at)。

## 6. REST 路由(core/input/)

### 6.1 `GET /api/character-instances/{id}/state`

- **新增公开属性 `agent.character_runtime_registry`**([修复 C1]):`core/agent/agent.py` 加 `@property character_runtime_registry` 返回 `self._runtime_registry`(仿 `agent.character_engine` 公开访问;现有 `consolidator`/`memory_reflector` 用 `_runtime_registry._runtimes.get` 但那是 debug 展示,本路由需要完整 registry 访问)。跨模块消费**禁止** `agent._xxx`,故必须公开。同时给 `CharacterRuntimeRegistry` 加**公开只读方法 `get_existing(instance_id)`**(直接 dict 读、不懒建、未命中返回 None)——避免路由访问私有 `_runtimes`(那会暴露内部 dict)。
- **不新建 `instance_routes.py`**([修复 A1]):`/api/character-instances/{id}/state` 与 `/api/characters` 同属角色 catalog 域,现有 `register_character_routes`(server.py:308 已调;core/input/character_routes.py:185 定义)已有现成 handler 结构。**处理方式**:在 `register_character_routes` 内新增该路由,不另建独立模块——避免重复建模 app bind、更符合现有组织。
  
- handler 逻辑(**只读,不懒建**,修复 A1):
  1. 解析 `{id}`。
  2. 经 `agent.character_runtime_registry.get_existing(instance_id)` 直接读已构建 runtime(**不做懒建**)——`_make_runtime` 只处理 `chi_builtin_*`(agent.py:228-273 `removeprefix("chi_builtin_")`),对任意非内置 instanceId 会产出垃圾/异常。当前 `character_profiles=false` 只命中内置 instance,但只读 GET 语义更稳妥的是「registry 命中即读,未命中→ fail-closed 503」。
  3. 命中 → `make_public_state(runtime)` 得 `CharacterPublicStateView`,包装 `CharacterPublicState` 返回。
  4. 未命中 / mongo 离线 → `StorageUnavailableError` → 503(对齐 attachment_repo 的 BE 先例 fail-closed)。

### 6.2 端点注册

`register_character_routes`(core/input/character_routes.py:185)内新增 `app.router.add_get("/api/character-instances/{id}/state", _instance_state_get)`——与 `/api/characters/{id}` 等同属角色域,共享 app bind 与 `check_auth`。`/api/character-instances/{id}` 与 `/api/characters/{id}` 前缀不同(aiohttp 按路径前缀区分),无遮蔽冲突。`create_app`(server.py:308)已调 `register_character_routes`,无需 seed 额外注册。

## 7. WS 发射(core/input/ws_handler.py + core/cognitive/finalize.py)

### 7.1 snapshot(新连接 / activate_session)

- **新增 `agent.character_state_stream` 布尔属性**(修复 C1 同源):`build_server_info` 设 wire 字段 `character_state_stream=True`,但 WS 发射路径需要 agent 侧同源开关。**建议**:agent 加 `@property character_state_stream -> bool` 返回 `True`(本批恒真,与 `build_server_info` 一致;不引 config 表)——避免 `build_server_info` 与 ws_handler/finalize 的 gate 各自硬编码导致漂移。`character_profiles` 同理恒 `False` 已在 capabilities.py:107 硬编码——**不**引 agent 属性(保持现状),仅 `character_state_stream` 新加。

`ws_handler.py` v2 握手成功后(发 `server_info` 之后),若 `agent.character_state_stream` 为 true:
- 取当前活动会话的 runtime → `make_public_state()` → `CharacterStateChangedMessage`。
- 强制发射(不看 last)。

`activate_session` 成功切换后同样发射(换角色时客户端需要新快照)。

> **N2 注(建连双 mood 来源)**:ws_handler.py:239-242 建连已发 `ExpressionMessage(expression=mood,…)`,§7.1 又发 `character.state.changed`(含 `state.mood`)。前端建连有两个 mood 来源(不同消息类型),实现者需注意二义性——非 bug,但需前端筛选(按消息类型区分:expression 是即时表情,state.changed 是权威产品态)。

### 7.2 回合边界变化事件

`core/cognitive/finalize.py:_finalize_turn` 收尾处:
1. `_flush_runtime_at_turn_boundary()` 之后(`runtime.flush()` 已执行,stateVersion CAS 已递增)。
2. 若 v2 + `agent.character_state_stream` → `make_public_state()` → 与 runtime `_last_public_state` 比较**关键字段(`mood.label`, `mood.intensity`)**(修复 C5:proactiveEnabled 是常量不比较)。
3. 有变化 → `_output` 广播 `CharacterStateChangedMessage`。
4. 更新 `_last_public_state`。

> **A3 注(interrupt/cancel 路径)**:上述只覆盖成功回合。interrupt/cancel(`_handle_interrupt`,loop.py:972)**不调用** `_finalize_turn`,也不 `_flush_runtime_at_turn_boundary`——流式中途 mood 变化后被取消,既不改库也不发 `character.state.changed`。本批**明示仅覆盖成功回合**;interrupt 路径的 state 发射归后续(属既有「interrupt 不 flush」gap,非本批引入)。

## 8. 变更文件清单

| 文件 | 改动 |
|------|------|
| `frontend_contract/rest.py` | 加 `MoodState` / `CharacterPublicStateView`;`CharacterPublicState` 泛化(flat mood 去掉,加 `state` 子结构,`instanceId`→`characterInstanceId`) |
| `frontend_contract/ws_message.py` | 加 `CharacterStateChangedMessage`(并加入 `ServerMessage` 判别联合 :1040-1083)+ `ServerInfoFeatures.character_state_stream` |
| `frontend_contract/__init__.py` | 重导出 `CharacterStateChangedMessage` / `CharacterPublicStateView` / `MoodState` |
| `core/character/public_state.py` (新) | `make_public_state(runtime)` 映射 helper |
| `core/character/runtime.py` | 加 `_last_public_state` 缓存 + `public_state()`/`has_public_state_changed()` |
| `core/character/registry.py` | 加公开只读 `get_existing(instance_id)`(不懒建,未命中 None) |
| `core/input/character_routes.py` | `register_character_routes` 内新增 `GET /api/character-instances/{id}/state` handler |
| `core/agent/agent.py` | 新增公开 `character_runtime_registry` property + `character_state_stream` property |
| `core/input/ws_handler.py` | snapshot 发射(连接/激活) |
| `core/cognitive/finalize.py` | 回合边界变化发射 |
| `core/services/capabilities.py` | `build_server_info` 设 `character_state_stream=True` |
| `frontend_contract/tests/test_stage_ce_contract.py` | 更新 `test_character_public_state_dto`:`instanceId`→`characterInstanceId` + `dto.mood`→`dto.state.mood.label` |
| `scripts/gen_contract_frames.py` + `scripts/tests/test_gen_contract_frames.py` | golden frame:`_server_info` 加 `character_state_stream=True`;chat 序列末尾加 state 事件帧 |
| golden frames (`scripts/gen_contract_frames.py` 相关) | 增 `character.state.changed` golden frame(可选,若需覆盖) |

## 9. 契约与测试

### 9.1 契约测试

- `frontend_contract/tests/test_stage_g_contract.py`(新):`CharacterStateChangedMessage` 判别联合成员、`CharacterPublicStateView` 字段、`ServerInfoFeatures.character_state_stream` 默认 False / 可显式 true。
- `frontend_contract/tests/test_ws_message.py`:现有 `character_profiles` 断言保持;若 golden frame 含 server_info,检查是否受影响。

### 9.2 单元测试

- `core/character/tests/test_public_state.py`(新):`make_public_state` 映射正确性(mood/intensity/updatedAt/proactiveEnabled/lastInteractionAt),内部态(`self_l2` 等)**不外泄**;`lastInteractionAt`/`updatedAt` 从 `instance.updatedAt` 派生(非 `state.updated_at`)。
- `core/character/tests/test_runtime.py`:runtime `public_state()` / `has_public_state_changed()` 变化检测(仅 mood 驱动;proactiveEnabled 常量不参与)。
- `core/input/tests/test_character_routes.py` 或新增 instance state 测试:GET state 返回产品态;instance 不存在 → 503(fail-closed);不做懒建。
- `core/cognitive/tests/test_finalize.py`:回合边界发射(有变化)、无变化不发射、v1 不发射。
- `core/input/tests/test_ws_handler.py`:v2 连接 snapshot 发射;v1 不发射;`character_state_stream` 关闭不发射。

### 9.3 守门

- `make lint`(ruff)、`make pyright`、`make check-types`(golden frames + ts/schema/raw_ts 漂移)。
- **Elysia byte-invariance**:新增 state 事件只在 `v2 && character_state_stream=true` 发射,v1/Elysia 单角色路径不发射、不放帧。
  - **注意[修复 C3]**:`test_elysia_snapshot_unchanged`(core/prompt/tests/test_system_prompt_snapshot.py:79,**18127B**)是 **system prompt 渲染快照** byte-for-byte 测试,与 WS 帧/state 事件**毫无关系**——不能作为「v1 不放 state 帧」的守门。v1 wire 序列真正的守卫是 `core/cognitive/tests/test_v2_generation.py:247 test_v1_wire_sequence_unchanged`(按 type 顺序断言 v1 无 generation 帧,`assert not any("generation" in t ...)`)。**G 应新增/扩充一条断言**:v1 路径下 `character.state.changed` 不出现(type 不在 v1 帧列)。
- `make test-quick`(3774+ 用例)。

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 事件风暴(state 每 segment 都变) | §3.2 变化检测:关键字段(mood)diff,无变化不发射;snapshot 仅在连接/激活强制 |
| stateVersion 滞后(event 在 flush 前发) | §3.3/§7.2:发射点放回合边界 flush 之后(flush 内 agent.py:431 CAS 递增已同步回 runtime.stateVersion) |
| `CharacterPublicState.mood` str→view + `instanceId`→`characterInstanceId` breaking | 无 wire 消费者;契约建模产物重生成;golden `--check` 守门;更新 test_stage_ce_contract.py |
| instance 不在 registry / mongo 离线 | §6.1 只读 GET fail-closed → 503(对齐 BE 先例);不懒建 |
| `state.updated_at` 冻结 → 派生时间戳 stale | §5.1 修复:`lastInteractionAt`/`updatedAt` 从 `instance.updatedAt` 派生(每 flush 更新);mood.updatedAt 用 state 近似 |
| Elysia 单角色 byte 变化 | v2+flag 双守卫;Elysia v1 路径不发射;test_v1_wire_sequence_unchanged 扩充 |
| 前端仍按旧 schema 消费 | `character_profiles=false`(UI 关);state 事件仅 v2 + flag;旧客户端忽略未知事件 |

## 11. 决策记录(自动采纳,最终报告汇总)

- **D1: `CharacterPublicState.mood` 由扁平 `str` 泛化为 `CharacterPublicStateView`(含 mood 子结构)**。理由:架构稿 §12.4 要求完整产品态视图,REST 与 WS 共享;现无 wire 消费者(仅测试 test_stage_ce_contract.py:77 依赖扁平字段,本批同步更新)。成本:契约 breaking(契约建模产物重生成)。
- **D2: `mood.updatedAt` 用 `CharacterRuntimeState.updated_at` 近似;`lastInteractionAt`/`updatedAt` 用 `runtime.instance.updatedAt`(每 flush 更新)**。[修复 B1] 理由:`state.updated_at` 只在构造时赋值,回合中从不刷新(agent.py:432 更新的是 `inst.updatedAt`,非 `state.updated_at`),不能作为「最近交互」信号;`instance.updatedAt` 每 flush 更新,是真实「最后交互时间」。成本:粒度粗(mood 时间戳为回合级近似);若要求精确 mood 时间戳,后续加 `CharacterRuntimeState.mood_updated_at` 字段(本批不做)。
- **D3: 变化检测比较关键字段(`mood.label`, `mood.intensity`),无变化不发射;不比较 `updated_at`(展示字段)、不比较 `proactiveEnabled`(常量)**。[修复 C5] 理由:架构稿 §12.4 明确"仅实际状态变化时发";proactiveEnabled 是 immutable 角色配置恒不变化,比较无意义。成本:需 runtime 维护 `_last_public_state` 缓存;检测逻辑实际仅由 mood 驱动。
- **D4: `character_state_stream` 本批置 true**(恒 true);`character_profiles` 恒 false。理由:§12.8 步骤 5"snapshot/重连路径通过后置 true"——G 即重连路径批;`character_profiles` 归 H(CE spec §10)。
- **D5: expression/live2d.directive/audio_start 的角色身份字段(§12.5/§12.7)本批不做**。理由:独立批次,归 H 联调前再加;本批聚焦 state 事件。
- **D6: proactiveEnabled 从 `runtime.revision.runtimeDefaults["proactive"]["enabled"]` 读**(不可变角色配置,非 runtime 演化态)。理由:proactive 开关是角色配置;runtime 持有 revision,无需 config/agent 句柄。
- **D7: `character.state.changed` 事件在 `ws-v2-chat.jsonl` golden 中追加**(完整 chat 序列末尾),供前端离线对拍。理由:阶段 4 确定性 smoke 要求 WS 帧对拍;state 事件是 v2 新增事件类型,应纳入 golden。成本:`_chat()` 帧序列末尾需加 state 帧,`gen_contract_frames` 的 `_chat()` 相应调整。

## 12. 验收标准

1. `make gen-types` 重生成 TS/schema/raw_ts,`make check-types` 0 漂移。
2. 契约测试 + 单元测试全绿;`CharacterPublicState` 扁平 `mood`/`instanceId` 移除后 `test_stage_ce_contract.py` 同步更新;system prompt 渲染快照(`test_elysia_snapshot_unchanged`,18127B)不变;**v1 wire 序列**(`test_v1_wire_sequence_unchanged`)扩充断言 `character.state.changed` 不出现。
3. `GET /api/character-instances/{id}/state` 返回产品态(含 `state` 子结构,`characterInstanceId` 命名);instance 不存在/registry 未命中 → 503(fail-closed,不懒建)。
4. v2 + `character_state_stream=true` 时,连接/激活发 snapshot;回合边界 mood 变化发 `character.state.changed`;无变化不发(proactiveEnabled 常量不触发);v1 不发射。
5. `character_profiles` 恒 false(UI 角色选择关)。
6. `make pyright` 0 错误(agent.py 新增 `character_runtime_registry`/`character_state_stream` property)。
