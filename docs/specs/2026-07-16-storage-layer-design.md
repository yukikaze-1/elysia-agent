# core/storage — 统一存储层设计

> 日期: 2026-07-16 | 状态: design | 作者: yukikaze-1

## 目标

建立 `core/storage/` Repository 存储中间层，消除业务逻辑对数据库的直接操作。

当前问题：
- SessionMemory 混杂消息窗口管理 + MongoDB 连接管理 + CRUD 操作
- server.py 绕过 SessionMemory 直接掏 `session._db` 做 session 管理查询
- PersonalityEngine 的状态通过 session params 间接持久化，没有独立存储
- `Module/database` 暴露裸 collection 操作，上层直接写 `find("sessions", {"session_id": ...})`

## 目录结构

```
core/storage/
├── __init__.py                     — re-export 所有 Repository
├── base.py                         — Repository ABC（connect / health_check / close）
├── session_repo.py                 — SessionRepository（MongoDB sessions）
├── message_repo.py                 — MessageRepository（MongoDB message_archive）
├── token_repo.py                   — TokenRepository（MongoDB token_usage）
├── character_state_repo.py         — CharacterStateRepository（MongoDB character_state，新建集合）
├── memory_repo.py                  — MemoryRepository（Milvus long_term_memory）
└── README.md                       — 遵循 readme-conventions.md

core/schema/                         新增：
├── token.py                        — TokenUsageEntry（Pydantic BaseModel）
├── character.py                    — CharacterState（Pydantic BaseModel，PersonalityEngine 字段）
├── emotion.py                      — EmotionState（Pydantic BaseModel，EmotionEngine 字段）
└── reflector.py                    — ReflectorState（Pydantic BaseModel，CharacterReflector + MemoryReflector 字段）
```

## base.py — Repository ABC

```python
class Repository(ABC):
    async def connect(self) -> bool: ...
    async def close(self) -> None: ...
    async def health_check(self) -> bool: ...
```

## 各 Repository 接口

### SessionRepository（MongoDB sessions）

```python
class SessionRepository(Repository):
    async def save(self, session_id: SessionId,
                   messages: list[Message],
                   params: dict[str, Any]) -> None: ...
    async def load(self, session_id: SessionId
                   ) -> tuple[list[Message], dict[str, Any]] | None: ...
    async def delete(self, session_id: SessionId) -> int: ...
    async def list_all(self) -> list[SessionSummary]: ...
    async def clear_all(self) -> int: ...

@dataclass
class SessionSummary:
    session_id: SessionId
    message_count: int
    updated_at: IsoTimestamp
    # token_total 不在 SessionSummary 中——token 数据在单独的 token_usage
    # 集合，由上层（server.py debug handler）自行跨集合聚合
```

### MessageRepository（MongoDB message_archive）

```python
class MessageRepository(Repository):
    async def append(self, session_id: SessionId, msg: Message) -> None: ...
    async def query_by_session(self, session_id: SessionId, *,
                               limit: int = 20, before: ObjectId | None = None
                               ) -> tuple[list[Message], int]: ...
        # ObjectId 是 MongoDB 特有类型，泄漏到接口层。当前 MongoDB
        # 是唯一文档数据库后端，可接受；若未来支持其他后端需改为通用游标。
    async def count_by_session(self, session_id: SessionId) -> int: ...
    async def count(self) -> int: ...
    async def delete_by_session(self, session_id: SessionId) -> int: ...
    async def clear_all(self) -> int: ...
```

### TokenRepository（MongoDB token_usage）

```python
class TokenRepository(Repository):
    async def record(self, session_id: SessionId, *,
                     model: str, prompt_tokens: int,
                     completion_tokens: int,
                     round_num: int) -> None: ...
    async def flush(self, records: list[TokenUsageEntry]) -> None: ...
        # 接受 TokenUsageEntry 而非 list[dict]——与 record() 风格一致。
        # 调用方（TokenTracker）持有 list[dict]，改为构造 TokenUsageEntry 再传。
    async def query_by_session(self, session_id: SessionId, *,
                               limit: int = 100) -> list[TokenUsageEntry]: ...
```

### CharacterStateRepository（MongoDB session_state，新建集合）

单一集合 `session_state` 存储 CharacterState / EmotionState / ReflectorState 三种文档。
doc_type 字段区分（"character" / "emotion" / "reflector"）。

```python
class CharacterStateRepository(Repository):
    async def save_character(self, state: CharacterState) -> None: ...
    async def load_character(self, session_id: SessionId) -> CharacterState | None: ...
    async def save_emotion(self, state: EmotionState) -> None: ...
    async def load_emotion(self, session_id: SessionId) -> EmotionState | None: ...
    async def save_reflector(self, state: ReflectorState) -> None: ...
    async def load_reflector(self, session_id: SessionId) -> ReflectorState | None: ...
```

### MemoryRepository（Milvus long_term_memory）

```python
class MemoryRepository(Repository):
    async def insert(self, entry: MemoryEntry) -> int: ...
    async def search(self, vector: list[float], *,
                     top_k: int = 10,
                     filter_expr: str | None = None,
                     output_fields: list[str] | None = None
                     ) -> list[MemoryEntry]: ...
    async def delete(self, ids: list[int]) -> int: ...
    async def count(self) -> int: ...
    async def query(self, *, filter_expr: str | None = None,
                    output_fields: list[str] | None = None,
                    limit: int = 100) -> list[MemoryEntry]: ...
```

## 数据类型

### TokenUsageEntry（`core/schema/token.py`）

遵循 core/schema Pydantic 统一风格：

```python
class TokenUsageEntry(BaseModel):
    session_id: SessionId
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: IsoTimestamp
```

### CharacterState（`core/schema/character.py`）

PersonalityEngine 专用——L2/L3 性格状态 + 压缩追踪。不含情绪和 reflector 域字段。

```python
class CharacterState(BaseModel):
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[dict] = Field(default_factory=list)
    last_awareness_time: IsoTimestamp | None = None
    compression_last_at: IsoTimestamp | None = None
    compression_last_count: int = 0
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

### EmotionState（`core/schema/emotion.py`）

EmotionEngine 专用——当前情绪快照。

```python
class EmotionState(BaseModel):
    session_id: SessionId
    mood: str = "neutral"
    mood_intensity: float = 0.5
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

### ReflectorState（`core/schema/reflector.py`）

CharacterReflector + MemoryReflector 专用——buffer/context/计数状态。

```python
class ReflectorState(BaseModel):
    session_id: SessionId
    reflector_buffer: list[tuple] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

> `own_life` 仍作为 sesssion param 保留——它本质是 system prompt section 的注入文本，不属于状态引擎的领域。

## 调用方迁移

### SessionMemory

- 移除 `_db`、`connect_db`、`health_check`、`save`、`load`
- 改为持有 `SessionRepository` 引用，通过它存取
- `set_param` / `get_param` 保持不动（params 层下次改）

### agent.py

- 不再创建 `MessageArchive`、`TokenTracker`（改为接收 Repository 实例）
- 创建各 Repository 实例，注入给 SessionMemory、CognitiveLoop 等组件
- `_start_persistence` 中改为 `await repo.connect()` 而非 `session.connect_db()`

### server.py

- 所有 session CRUD handler（sessions_list、session_detail、session_delete、sessions_clear）改为通过 SessionRepository
- 所有 archive handler 改为通过 MessageRepository
- 所有 token 查询改为通过 TokenRepository
- 移除所有 `session._db` 和 `agent._archive._db` 直接访问

### debug_info.py

- MongoDB 可用性判断改为 `session_repo.health_check()`
- LTM 信息改为 `memory_repo.count()`、`memory_repo.query()`

### PersonalityEngine

- `restore_state()` — 改为从 `CharacterStateRepository.load_character()` 恢复
- 持久化 — shutdown 时通过 hook `save_character_state()` 写入 `CharacterStateRepository.save_character()`
- 内存读写仍通过 `session.set_param/get_param`（`render_c1`/`render_c9` 每轮读取性能要求）

### EmotionEngine

- `restore_state()` — 改为从 `CharacterStateRepository.load_emotion()` 恢复
- 持久化 — shutdown 时写入 `CharacterStateRepository.save_emotion()`
- 内存读写仍通过 `session.set_param/get_param`

### CharacterReflector / MemoryReflector

- `restore_state()` — 改为从 `CharacterStateRepository.load_reflector()` 恢复
- 持久化 — shutdown 时写入 `CharacterStateRepository.save_reflector()`
- 内存读写仍通过 `session.set_param/get_param`

## 文档更新

- `core/storage/README.md` — 新建，遵循 readme-conventions.md
- `README.md` — 文档导航新增 storage 条目
- `docs/architecture.md` — 更新数据流图，加入 storage 层
- `core/schema/README.md` — 新增 token.py / character.py 说明

## 不在首期范围内的

- Neo4j 集成（预留 GraphRepository stub）
- `Module/database/graphdb/` 的实现
- session params 的 dataclass 化（单独 spec）
