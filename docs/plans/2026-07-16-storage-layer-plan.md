# core/storage 统一存储层 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `core/storage/` Repository 存储中间层，消除业务逻辑对数据库的直接操作（`session._db.find(...)`、`archive._db.insert_one(...)` 等）。

**Architecture:** Repository ABC → 5 个具体 Repository（Session/Message/Token/CharacterState/Memory）→ 替换各组件中的裸 DB 访问。Agent 负责创建和注入 Repository 实例。server.py debug handler 不再掏私有 `_db` 属性。

**Tech Stack:** Python 3.13, motor (MongoDB), pymilvus, Pydantic v2, Pytest

## Global Constraints

- 遵循 `docs/readme-conventions.md`：新模块必须创建 README（面包屑 + 相关文档 + 根 README 注册）
- 所有新测试标记 `@pytest.mark.unit`（纯 mock）或 `@pytest.mark.service`（mock 数据库）
- 使用 conda 环境：`conda activate Elysia`
- spec 不在首期范围的：session params dataclass 化、Neo4j 集成
- CharacterStateRepository 仅用于持久化（save/load），不移除 session.set_param/get_param（那是 params dataclass 化的活儿）

---

### Task 1: Schema 类型 — token.py + character.py

**Files:**
- Create: `core/schema/token.py`
- Create: `core/schema/character.py`
- Modify: `core/schema/__init__.py`（新增 re-export）

**Interfaces:**
- Produces: `TokenUsageEntry`, `CharacterState`（Pydantic BaseModel）

- [ ] **Step 1: 创建 TokenUsageEntry**

```python
# core/schema/token.py
"""Token 用量记录类型 — Pydantic BaseModel。"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp, now_iso


class TokenUsageEntry(BaseModel):
    """单次 LLM 调用的 token 消耗记录。"""
    session_id: SessionId
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
```

- [ ] **Step 2: 创建 CharacterState**

```python
# core/schema/character.py
"""角色状态类型 — Pydantic BaseModel。

CharacterStateRepository 用于序列化/反序列化持久化，
Engine 层仍通过 session.set_param/get_param 做内存读写。
"""

from pydantic import BaseModel, Field

from .types import SessionId, IsoTimestamp, now_iso


class CharacterState(BaseModel):
    """角色性格状态快照 — 持久化到 MongoDB character_state 集合。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[dict] = Field(default_factory=list)
    reflector_buffer: list[tuple] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    mood: str = "neutral"
    mood_intensity: float = 0.5
    last_awareness_time: IsoTimestamp | None = None
    compression_last_at: IsoTimestamp | None = None
    compression_last_count: int = 0
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

- [ ] **Step 3: __init__.py 新增 re-export**

在 `core/schema/__init__.py` 末尾追加：

```python
from core.schema.token import TokenUsageEntry  # noqa: F401
from core.schema.character import CharacterState  # noqa: F401
```

- [ ] **Step 4: 验证 import**

```bash
python -c "from core.schema import TokenUsageEntry, CharacterState; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add core/schema/token.py core/schema/character.py core/schema/__init__.py
git commit -m "feat: core/schema/token.py + character.py — TokenUsageEntry + CharacterState Pydantic"
```

---

### Task 2: Repository ABC — base.py

**Files:**
- Create: `core/storage/__init__.py`（空）
- Create: `core/storage/base.py`

**Interfaces:**
- Produces: `Repository` ABC (connect / health_check / close)

- [ ] **Step 1: 创建 base.py**

```python
# core/storage/base.py
"""Repository 抽象基类 — 所有存储后端的最低接口。"""

from abc import ABC, abstractmethod


class Repository(ABC):
    """存储仓库基类。"""

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接。返回 True 表示成功。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查。返回 True 表示可用。"""
        ...
```

- [ ] **Step 2: 创建 __init__.py**

```python
# core/storage/__init__.py
"""core/storage/ — Repository 存储中间层。"""
```

- [ ] **Step 3: 验证 import**

```bash
python -c "from core.storage.base import Repository; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: 提交**

```bash
git add core/storage/__init__.py core/storage/base.py
git commit -m "feat: core/storage/base.py — Repository ABC"
```

---

### Task 3: MessageRepository — 迁移 MessageArchive 的 DB 操作

**Files:**
- Create: `core/storage/message_repo.py`
- Create: `core/storage/tests/test_message_repo.py`
- Modify: `core/memory/message_archive.py`（_db → _repo）

**Interfaces:**
- Consumes: `Repository` ABC, `DocumentDBBackend` protocol, `core.schema.message.Message`
- Produces: `MessageRepository`

- [ ] **Step 1: 创建 MessageRepository**

```python
# core/storage/message_repo.py
"""MessageRepository — MongoDB message_archive 集合操作。"""

import logging
from typing import TYPE_CHECKING

from bson import ObjectId

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.schema.message import Message

logger = logging.getLogger(__name__)

COLLECTION = "message_archive"


class MessageRepository(Repository):
    """消息归档存储。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    # ---- lifecycle ----

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            logger.info("MessageRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("MessageRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    # ---- core operations ----

    async def append(self, session_id: str, msg: dict) -> None:
        """追加一条消息到永久归档。"""
        if self._db is None:
            return
        msg["session_id"] = session_id
        await self._db.insert_one(COLLECTION, msg)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 20, before: ObjectId | None = None
                               ) -> tuple[list[dict], int]:
        """按 session 分页查询消息。返回 (消息列表, 总条数)。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {"session_id": session_id}
        if before:
            filter_expr["_id"] = {"$lt": before}
        sort = [("_id", -1)]
        messages = await self._db.find(COLLECTION, filter_expr, sort=sort, limit=limit)
        total = await self._db.count(COLLECTION, {"session_id": session_id})
        return messages, total

    async def count_by_session(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {"session_id": session_id})

    async def count(self) -> int:
        if self._db is None:
            return 0
        return await self._db.count(COLLECTION, {})

    async def delete_by_session(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"session_id": session_id})

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})

    async def query_offset(self, *, skip: int = 0, limit: int = 50,
                           search: str = "", role: str = "") -> tuple[list[dict], int]:
        """offset 分页 + 搜索 + role 过滤。"""
        if self._db is None:
            return [], 0
        filter_expr: dict = {}
        if search:
            filter_expr["content"] = {"$elemMatch": {"content": {"$regex": search, "$options": "i"}}}
        if role:
            filter_expr["role"] = role
        sort = [("_id", -1)]
        messages = await self._db.find(COLLECTION, filter_expr, sort=sort, skip=skip, limit=limit)
        total = await self._db.count(COLLECTION, filter_expr)
        return messages, total

    async def delete_one(self, msg_id: str) -> int:
        if self._db is None:
            return 0
        from bson import ObjectId as ObjId
        return await self._db.delete_many(COLLECTION, {"_id": ObjId(msg_id)})

    async def update_text(self, msg_id: str, new_text: str) -> int:
        if self._db is None:
            return 0
        from bson import ObjectId as ObjId
        return await self._db.update_one(
            COLLECTION,
            {"_id": ObjId(msg_id)},
            {"$set": {"content": [{"type": "text", "content": new_text}]}},
        )
```

- [ ] **Step 2: 迁移 MessageArchive — _db → _repo**

读 [message_archive.py](core/memory/message_archive.py)。做以下替换：

1. `__init__` 加 `_repo: MessageRepository | None = None` 参数，将 `connect(db)` 改为 `connect(repo=None)`
2. 所有 `self._db.insert_one(COLLECTION, msg)` → `self._repo.append(session_id, msg)`
3. 所有 `self._db.find(COLLECTION, ...)` → `self._repo.query_offset(...)` 或对应方法
4. 所有 `self._db.count(COLLECTION, ...)` → `self._repo.count_by_session(...)` 或 `count()`
5. 所有 `self._db.delete_many(COLLECTION, ...)` → `self._repo.delete_one(...)` 或 `clear_all()`
6. 所有 `self._db.update_one(COLLECTION, ...)` → `self._repo.update_text(...)`
7. 删掉 `COLLECTION = "message_archive"` 常量（移到 repo 内部）
8. 删掉 `connect(db)` 方法（repo 自己管理连接）

实际改动时先通读 message_archive.py 确认每个方法的逻辑，逐一替换。

- [ ] **Step 3: 写 MessageRepository 测试**

```python
# core/storage/tests/test_message_repo.py
"""MessageRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.message_repo import MessageRepository


@pytest.mark.unit
class TestMessageRepository:
    def test_append_calls_insert_one(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()
        # fire-and-forget 在 append 中是 asyncio.create_task，这里直接调
        import asyncio
        asyncio.run(repo.append("sid", {"role": "user", "content": "hi"}))
        repo._db.insert_one.assert_called_once()

    def test_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        msgs, total = asyncio.run(repo.query_by_session("sid"))
        assert msgs == []
        assert total == 0

    def test_count_returns_zero_when_db_none(self):
        repo = MessageRepository(db=None)
        assert asyncio.run(repo.count()) == 0

    def test_clear_all_calls_delete_many(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=5)
        result = asyncio.run(repo.clear_all())
        assert result == 5

import asyncio
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest core/storage/tests/test_message_repo.py -v --tb=short
```

- [ ] **Step 5: 运行全量测试确保无回归**

```bash
python -m pytest core/memory/tests/test_message_archive.py -v --tb=short
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 6: 提交**

```bash
git add core/storage/message_repo.py core/storage/tests/test_message_repo.py core/memory/message_archive.py
git commit -m "refactor: MessageRepository + MessageArchive 迁移 _db→_repo"
```

---

### Task 4: TokenRepository + 迁移 TokenTracker

**Files:**
- Create: `core/storage/token_repo.py`
- Create: `core/storage/tests/test_token_repo.py`
- Modify: `core/token_tracker.py`（_db → _repo）

**Interfaces:**
- Consumes: `Repository` ABC, `TokenUsageEntry` from Task 1
- Produces: `TokenRepository`

- [ ] **Step 1: 创建 TokenRepository**

```python
# core/storage/token_repo.py
"""TokenRepository — MongoDB token_usage 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.schema.token import TokenUsageEntry

logger = logging.getLogger(__name__)

COLLECTION = "token_usage"


class TokenRepository(Repository):
    """Token 用量存储。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            logger.info("TokenRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("TokenRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    async def record(self, entry: "TokenUsageEntry") -> None:
        """记录一条 token 用量。"""
        if self._db is None:
            return
        await self._db.insert_one(COLLECTION, entry.model_dump(mode="json"))

    async def flush(self, entries: list["TokenUsageEntry"]) -> None:
        """批量写入 token 用量。"""
        if self._db is None or not entries:
            return
        docs = [e.model_dump(mode="json") for e in entries]
        await self._db.insert_many(COLLECTION, docs)

    async def query_by_session(self, session_id: str, *,
                               limit: int = 100) -> list[dict]:
        if self._db is None:
            return []
        return await self._db.find(
            COLLECTION, {"session_id": session_id}, limit=limit
        )

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})
```

- [ ] **Step 2: 迁移 TokenTracker**

读 [token_tracker.py](core/token_tracker.py)。做以下替换：
1. `__init__` 加 `repo: TokenRepository | None = None`，替代 `document_db` 参数
2. `_persist()` 构造 `TokenUsageEntry`，调 `self._repo.record(entry)`
3. `get_session_stats()` 调 `self._repo.query_by_session(sid)`
4. `flush_pending()` 构造 TokenUsageEntry 列表，调 `self._repo.flush(entries)`
5. `clear_all()` 调 `self._repo.clear_all()`
6. JSONL fallback 逻辑保留（本地文件备胎）
7. 删掉 COLLECTION 常量、`_db` 属性

- [ ] **Step 3: 写 TokenRepository 测试**

```python
# core/storage/tests/test_token_repo.py
"""TokenRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.token_repo import TokenRepository
from core.schema.token import TokenUsageEntry
from core.schema.types import SessionId


@pytest.mark.unit
class TestTokenRepository:
    def test_record_calls_insert_one(self):
        import asyncio
        repo = TokenRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()
        entry = TokenUsageEntry(
            session_id=SessionId("s1"), model="m", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, round_num=1,
        )
        asyncio.run(repo.record(entry))
        repo._db.insert_one.assert_called_once()

    def test_returns_empty_when_db_none(self):
        import asyncio
        repo = TokenRepository(db=None)
        result = asyncio.run(repo.query_by_session("s1"))
        assert result == []
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest core/storage/tests/test_token_repo.py -v --tb=short
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -3
```

- [ ] **Step 5: 提交**

```bash
git add core/storage/token_repo.py core/storage/tests/test_token_repo.py core/token_tracker.py
git commit -m "refactor: TokenRepository + TokenTracker 迁移 _db→_repo"
```

---

### Task 5: SessionRepository + 迁移 SessionMemory DB 操作

**Files:**
- Create: `core/storage/session_repo.py`
- Create: `core/storage/tests/test_session_repo.py`
- Modify: `core/memory/session.py`（_db → _repo）

**Interfaces:**
- Consumes: `Repository` ABC, `SessionId`
- Produces: `SessionRepository`, `SessionSummary`

- [ ] **Step 1: 创建 SessionSummary**

在 `core/storage/session_repo.py` 顶部：

```python
from dataclasses import dataclass
from core.schema.types import SessionId, IsoTimestamp

@dataclass
class SessionSummary:
    session_id: SessionId
    message_count: int
    updated_at: IsoTimestamp
```

- [ ] **Step 2: 创建 SessionRepository**

```python
# core/storage/session_repo.py（续）
import logging
from typing import Any

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

logger = logging.getLogger(__name__)
COLLECTION = "sessions"


class SessionRepository(Repository):
    """会话存储 — MongoDB sessions 集合。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            await self._db.ensure_index(COLLECTION, [("updated_at", -1)])
            try:
                await self._db.ensure_index(
                    COLLECTION, [("updated_at", 1)], expireAfterSeconds=30 * 86400)
            except Exception as e:
                logger.warning("SessionRepository: TTL index failed (%s)", e)
            logger.info("SessionRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("SessionRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            logger.info("SessionRepository: MongoDB 已恢复连接")
            return True
        except Exception:
            return False

    async def save(self, session_id: str, messages: list[dict],
                   params: dict[str, Any]) -> None:
        """保存或更新 session。"""
        if self._db is None:
            return
        from datetime import datetime
        doc = {
            "session_id": session_id,
            "messages": messages,
            "params": params,
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        await self._db.update_one(
            COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
        )

    async def load(self, session_id: str
                   ) -> tuple[list[dict], dict[str, Any]] | None:
        """加载 session。返回 (messages, params) 或 None。"""
        if self._db is None:
            return None
        results = await self._db.find(COLLECTION, {"session_id": session_id}, limit=1)
        if not results:
            return None
        doc = results[0]
        for m in doc.get("messages", []):
            m.pop("_id", None)
        return doc.get("messages", []), dict(doc.get("params", {}))

    async def delete(self, session_id: str) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {"session_id": session_id})

    async def list_all(self) -> list[SessionSummary]:
        if self._db is None:
            return []
        docs = await self._db.find(COLLECTION, {}, sort=[("updated_at", -1)], limit=200)
        return [
            SessionSummary(
                session_id=SessionId(d.get("session_id", "")),
                message_count=len(d.get("messages", [])),
                updated_at=d.get("updated_at", ""),
            )
            for d in docs
        ]

    async def clear_all(self) -> int:
        if self._db is None:
            return 0
        return await self._db.delete_many(COLLECTION, {})
```

- [ ] **Step 3: 迁移 SessionMemory**

读 [session.py](core/memory/session.py)。做以下替换：

1. `__init__` 加 `repo: SessionRepository | None = None`，移除 `_db` 直接创建
2. `connect_db()` → 调 `self._repo.connect()`
3. `health_check()` → 调 `self._repo.health_check()`
4. `save()` → 调 `self._repo.save(session_id, list(self._messages), dict(self._params))`
5. `load()` → 调 `self._repo.load(session_id)` 返回 (messages, params)，再恢复内存
6. `migrate_archive_session_id()` — 保留（archive 相关，独立 repo）
7. `load_from_jsonl` / `_load_from_jsonl` — 保留（本地文件备胎）
8. 删掉 `COLLECTION` 常量、`_db` 属性、`db` property
9. server.py 中通过 `session._db` 访问的代码在 Task 8 统一修

- [ ] **Step 4: 写 SessionRepository 测试**

```python
# core/storage/tests/test_session_repo.py
"""SessionRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.session_repo import SessionRepository


@pytest.mark.unit
class TestSessionRepository:
    def test_save_calls_update_one(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        asyncio.run(repo.save("sid", [], {"mood": "neutral"}))
        repo._db.update_one.assert_called_once()

    def test_returns_empty_list_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.list_all())
        assert result == []

    def test_load_returns_none_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.load("sid"))
        assert result is None

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.delete("sid"))
        assert result == 0
```

- [ ] **Step 5: 运行测试**

```bash
python -m pytest core/storage/tests/test_session_repo.py -v --tb=short
python -m pytest core/memory/tests/test_session.py -v --tb=short
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -3
```

- [ ] **Step 6: 提交**

```bash
git add core/storage/session_repo.py core/storage/tests/test_session_repo.py core/memory/session.py
git commit -m "refactor: SessionRepository + SessionMemory 迁移 _db→_repo"
```

---

### Task 6: MemoryRepository — 迁移 LongTermMemory 的 Milvus 操作

**Files:**
- Create: `core/storage/memory_repo.py`
- Create: `core/storage/tests/test_memory_repo.py`
- Modify: `core/memory/long_term.py`（_vector_db → _repo）

**Interfaces:**
- Consumes: `Repository` ABC, `VectorDBBackend` protocol, `MemoryEntry`
- Produces: `MemoryRepository`

- [ ] **Step 1: 创建 MemoryRepository**

```python
# core/storage/memory_repo.py
"""MemoryRepository — Milvus long_term_memory 集合操作。"""

import logging
from typing import TYPE_CHECKING

from Module.database.vectordb import VectorDBFactory, VectorDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.memory.long_term import MemoryEntry

logger = logging.getLogger(__name__)
COLLECTION = "long_term_memory"
DIMENSION = 1024


class MemoryRepository(Repository):
    """长期记忆存储 — Milvus 向量数据库。"""

    def __init__(self, db: VectorDBBackend | None = None):
        self._db: VectorDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = VectorDBFactory.create("milvus/memory")
            if not await self._db.collection_exists(COLLECTION):
                await self._db.create_collection(COLLECTION, DIMENSION)
            logger.info("MemoryRepository: Milvus connected")
            return True
        except Exception as e:
            logger.warning("MemoryRepository: Milvus unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        if self._db is not None:
            return True
        try:
            self._db = VectorDBFactory.create("milvus/memory")
            logger.info("MemoryRepository: Milvus 已恢复连接")
            return True
        except Exception:
            return False

    async def insert(self, vectors: list[list[float]],
                     metadata: list[dict]) -> list[int]:
        """插入向量 + 元数据。返回主键列表。"""
        if self._db is None:
            return []
        return await self._db.insert(COLLECTION, vectors, metadata)

    async def search(self, vector: list[float], *,
                     top_k: int = 10, filter_expr: str | None = None,
                     output_fields: list[str] | None = None
                     ) -> list[dict]:
        """ANN 向量搜索。"""
        if self._db is None:
            return []
        return await self._db.search(
            COLLECTION, vector, top_k=top_k,
            filter_expr=filter_expr, output_fields=output_fields,
        )

    async def query(self, *, filter_expr: str | None = None,
                    output_fields: list[str] | None = None,
                    limit: int = 100) -> list[dict]:
        """标量查询（非向量搜索）。"""
        if self._db is None:
            return []
        return await self._db.query(
            COLLECTION, filter_expr=filter_expr,
            output_fields=output_fields, limit=limit,
        )

    async def delete(self, ids: list[int]) -> int:
        if self._db is None:
            return 0
        return await self._db.delete(COLLECTION, ids)

    async def count(self) -> int:
        """返回条目数。"""
        if self._db is None:
            return 0
        results = await self._db.query(
            COLLECTION, filter_expr="id >= 0", output_fields=["id"], limit=1,
        )
        # 返回总行数近似——LongTermMemory 自己维护 count
        return -1  # 由 LongTermMemory.count property 负责

    async def clear_all(self) -> None:
        if self._db is None:
            return
        await self._db.drop_collection(COLLECTION)
        await self._db.create_collection(COLLECTION, DIMENSION)
```

- [ ] **Step 2: 迁移 LongTermMemory**

读 [long_term.py](core/memory/long_term.py)。做以下替换：
1. `__init__` 加 `repo: MemoryRepository | None = None`，移除 `_vector_db` 直接创建
2. `connect()` → 调 `self._repo.connect()`，从 query 加载已有条目改为 `self._repo.query(...)`
3. `health_check()` → 调 `self._repo.health_check()`
4. `insert()` — embedding 生成保留在 LTM，insert 调 `self._repo.insert(vectors, metadata)`
5. `search()` — MILVUS_OK 路径调 `self._repo.search(...)`，本地 fallback 保留
6. `delete()` → 调 `self._repo.delete([pk])`
7. `clear_all()` → 调 `self._repo.clear_all()`
8. `_load_from_milvus()` → 调 `self._repo.query(...)`
9. `_sync_dirty_entries()` → 调 `self._repo.insert(...)` + `self._repo.delete(...)`
10. 保留 `_entries`, `_dirty_ids`, `_absorbed_ids` 等内存状态
11. 保留 embedding 初始化逻辑（`_ensure_embedding`）
12. 删掉 `_vector_db` 属性、`_connected` flag

- [ ] **Step 3: 写 MemoryRepository 测试**

```python
# core/storage/tests/test_memory_repo.py
"""MemoryRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.memory_repo import MemoryRepository


@pytest.mark.unit
class TestMemoryRepository:
    def test_insert_calls_db_insert(self):
        import asyncio
        repo = MemoryRepository(db=MagicMock())
        repo._db.insert = AsyncMock(return_value=[1, 2])
        result = asyncio.run(repo.insert([[0.1]*1024], [{"text": "test"}]))
        assert result == [1, 2]
        repo._db.insert.assert_called_once()

    def test_search_returns_empty_when_db_none(self):
        import asyncio
        repo = MemoryRepository(db=None)
        result = asyncio.run(repo.search([0.1]*1024, top_k=5))
        assert result == []

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        repo = MemoryRepository(db=None)
        result = asyncio.run(repo.delete([1, 2]))
        assert result == 0
```

- [ ] **Step 4: 运行测试**

```bash
python -m pytest core/storage/tests/test_memory_repo.py -v --tb=short
python -m pytest core/memory/tests/test_long_term.py -v --tb=short
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -3
```

- [ ] **Step 5: 提交**

```bash
git add core/storage/memory_repo.py core/storage/tests/test_memory_repo.py core/memory/long_term.py
git commit -m "refactor: MemoryRepository + LongTermMemory 迁移 _vector_db→_repo"
```

---

### Task 7: CharacterStateRepository + PersonalityEngine 持久化

**Files:**
- Create: `core/storage/character_state_repo.py`
- Create: `core/storage/tests/test_character_state_repo.py`
- Modify: `core/personality/engine.py`（restore_state → repo.load, flush → repo.save）
- Modify: `core/agent.py`（start() 传入 repo, stop() 调用 repo.save）

**Interfaces:**
- Consumes: `Repository` ABC, `CharacterState` from Task 1
- Produces: `CharacterStateRepository`
- **重要：仅替换持久化路径，不替换 set_param/get_param 内存操作**

- [ ] **Step 1: 创建 CharacterStateRepository**

```python
# core/storage/character_state_repo.py
"""CharacterStateRepository — MongoDB character_state 集合。"""

import logging
from typing import TYPE_CHECKING

from Module.database.documentdb import DocumentDBFactory, DocumentDBBackend
from .base import Repository

if TYPE_CHECKING:
    from core.schema.character import CharacterState

logger = logging.getLogger(__name__)
COLLECTION = "character_state"


class CharacterStateRepository(Repository):
    """角色状态持久化存储。"""

    def __init__(self, db: DocumentDBBackend | None = None):
        self._db: DocumentDBBackend | None = db

    async def connect(self) -> bool:
        try:
            self._db = DocumentDBFactory.create("mongodb/default")
            await self._db.ensure_index(COLLECTION, [("session_id", 1)])
            logger.info("CharacterStateRepository: MongoDB connected")
            return True
        except Exception as e:
            logger.warning("CharacterStateRepository: MongoDB unavailable (%s)", e)
            self._db = None
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def health_check(self) -> bool:
        return self._db is not None

    async def save(self, state: "CharacterState") -> None:
        """保存或更新角色状态。"""
        if self._db is None:
            return
        doc = state.model_dump(mode="json")
        await self._db.update_one(
            COLLECTION,
            {"session_id": doc["session_id"]},
            {"$set": doc},
            upsert=True,
        )

    async def load(self, session_id: str) -> "CharacterState | None":
        """加载角色状态。"""
        if self._db is None:
            return None
        from core.schema.character import CharacterState
        results = await self._db.find(
            COLLECTION, {"session_id": session_id}, limit=1,
        )
        if not results:
            return None
        return CharacterState.model_validate(results[0])
```

- [ ] **Step 2: 在 PersonalityEngine 中加持久化 hooks**

读 [engine.py](core/personality/engine.py)。新增两个方法，不改现有逻辑：

```python
# engine.py — 新增方法（追加到 class 末尾）

async def save_character_state(self, repo: "CharacterStateRepository") -> None:
    """保存当前性格状态到持久化存储。shutdown 时调用。"""
    from core.schema.character import CharacterState
    state = CharacterState(
        session_id=self._session.session_id,
        self_l2=self._session.get_param("self_l2", ""),
        state_l3=self._session.get_param("state_l3", ""),
        state_history=self._session.get_param("state_history", []),
        reflector_buffer=self._session.get_param("reflector_buffer", []),
        reflector_context=self._session.get_param("reflector_context", []),
        self_perception_history=self._session.get_param(
            "self_perception_history", []),
        mood=self._session.get_param("mood", "neutral"),
        mood_intensity=self._session.get_param("mood_intensity", 0.5),
        last_awareness_time=self._session.get_param("last_awareness_time", None),
        compression_last_at=self._session.get_param("compression_last_at", None),
        compression_last_count=self._session.get_param("compression_last_count", 0),
    )
    await repo.save(state)

def restore_character_state(self, state: "CharacterState") -> None:
    """从持久化存储恢复性格状态。start() 时在 restore_state 之后调用。"""
    self._session.set_param("self_l2", state.self_l2)
    self._session.set_param("state_l3", state.state_l3)
    self._session.set_param("state_history", state.state_history)
    self._session.set_param("reflector_buffer", state.reflector_buffer)
    self._session.set_param("reflector_context", state.reflector_context)
    self._session.set_param("self_perception_history", state.self_perception_history)
    self._session.set_param("mood", state.mood)
    self._session.set_param("mood_intensity", state.mood_intensity)
    if state.last_awareness_time:
        self._session.set_param("last_awareness_time", state.last_awareness_time)
        self._last_awareness_time = state.last_awareness_time
    if state.compression_last_at:
        self._session.set_param("compression_last_at", state.compression_last_at)
    if state.compression_last_count:
        self._session.set_param("compression_last_count", state.compression_last_count)
```

- [ ] **Step 3: 在 agent.py 中接线**

在 `start()` 的持久化恢复段（现有 `restore_state()` 调用之后）加：

```python
# 从 CharacterStateRepository 恢复（替代之前的 session params 恢复）
if self._character_state_repo:
    if state := await self._character_state_repo.load(self._session.session_id):
        self._personality_engine.restore_character_state(state)
```

在 `stop()` 的 buffer flush 段（`_flush_compression()` 之后）加：

```python
# 保存性格状态到独立持久化
if self._character_state_repo:
    await self._personality_engine.save_character_state(self._character_state_repo)
```

在 `__init__` 中新增 `self._character_state_repo` 参数。

- [ ] **Step 4: 写 CharacterStateRepository 测试**

```python
# core/storage/tests/test_character_state_repo.py
"""CharacterStateRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.character_state_repo import CharacterStateRepository
from core.schema.character import CharacterState
from core.schema.types import SessionId


@pytest.mark.unit
class TestCharacterStateRepository:
    def test_save_calls_update_one(self):
        import asyncio
        repo = CharacterStateRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        state = CharacterState(session_id=SessionId("sid"))
        asyncio.run(repo.save(state))
        repo._db.update_one.assert_called_once()

    def test_load_returns_none_when_db_none(self):
        import asyncio
        repo = CharacterStateRepository(db=None)
        result = asyncio.run(repo.load("sid"))
        assert result is None
```

- [ ] **Step 5: 运行测试**

```bash
python -m pytest core/storage/tests/test_character_state_repo.py -v --tb=short
python -m pytest core/personality/tests/ -v --tb=short
```

- [ ] **Step 6: 提交**

```bash
git add core/storage/character_state_repo.py core/storage/tests/test_character_state_repo.py \
        core/personality/engine.py core/agent.py
git commit -m "feat: CharacterStateRepository + PersonalityEngine 持久化 hook"
```

---

### Task 8: 迁移 server.py debug handler + agent.py 接线

**Files:**
- Modify: `Module/input/server.py`（所有 `session._db`、`archive._db` 直接访问 → Repository）
- Modify: `core/agent.py`（start/stop 注入 Repository）
- Modify: `core/debug_info.py`（`_db` 访问 → repo 方法）
- Modify: `core/memory/session.py`（删 `db` property 对外暴露）

**Interfaces:**
- Consumes: All Repositories from Tasks 3-7
- Produces: Cleaned-up wiring

- [ ] **Step 1: 迁移 server.py debug handler**

读 [server.py:340-470](Module/input/server.py) 和 [server.py:787-800](Module/input/server.py)。

**sessions_list_handler (line 342-384):**
```python
# 前：session._db.find("sessions", ...)
# 改为：agent._session_repo.list_all()

# 前：session._db.find("token_usage", ...)
# 改为：agent._token_repo.query_by_session(sid) — 或保持 server.py 自己聚合
# session_id 从 SessionSummary 中取
```

**session_detail_handler (line 386-446):**
```python
# 前：session._db.find("sessions", {"session_id": sid})
# 改为：agent._session_repo.load(sid) — 或新增 get_params(sid) 方法
```

**session_delete_handler (line 448-466):**
```python
# 前：session._db.delete_many("sessions", {"session_id": sid})
# 改为：agent._session_repo.delete(sid)
# 同时：agent._message_repo.delete_by_session(sid)
```

**sessions_clear_handler (line 787-800):**
```python
# 前：session._db.delete_many("sessions", {})
# 改为：agent._session_repo.clear_all()
```

**db_status_handler (line 167-188):**
```python
# 前：session._db is not None / archive._db is not None / ltm._connected
# 改为：agent._session_repo.health_check() / agent._message_repo.health_check() / agent._memory_repo.health_check()
```

**所有 `session_messages.clear()` 调用：**
```python
# 改为：agent._session.clear() — 已有的公共方法
```

- [ ] **Step 2: 更新 agent.py 暴露 Repository**

在 `Agent.__init__` 中创建 Repository 实例（或由外部注入），暴露为属性：

```python
# Agent 新增属性
self._session_repo = SessionRepository()
self._message_repo = MessageRepository()
self._token_repo = TokenRepository()
self._memory_repo = MemoryRepository()
self._character_state_repo = CharacterStateRepository()
```

server.py 中 `create_app(agent)` 接收 agent 后，handler 通过 `agent._session_repo` 等访问。

> **注意：** 属性名用 `_` 前缀（内部属性，但 server 层可以访问——当前项目的约定）

- [ ] **Step 3: 更新 debug_info.py**

```python
# 前：agent._session._db is not None → agent._session_repo.health_check()
# 前：agent._long_term._connected → agent._memory_repo.health_check()
# 前：agent._long_term._entries → agent._memory_repo.query(limit=5)  # 仅取最近 5 条给 debug
# 前：agent._session._params → 保留（params 还没迁移）
```

- [ ] **Step 4: 删 session.db property**

从 `core/memory/session.py` 中删掉 `db` property（line 44-45）——不再有外部调用方依赖它。

- [ ] **Step 5: 运行全量测试**

```bash
python -m pytest core/storage/tests/ -v --tb=short
python -m pytest Module/input/test_server.py -v --tb=short
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -3
```

- [ ] **Step 6: 提交**

```bash
git add Module/input/server.py core/agent.py core/debug_info.py core/memory/session.py
git commit -m "refactor: server.py + agent.py + debug_info 迁移到 Repository"
```

---

### Task 9: 文档 + 验证

**Files:**
- Create: `core/storage/README.md`
- Modify: `README.md`（文档导航新增 storage 条目）
- Modify: `core/schema/README.md`（新增 token.py / character.py 说明）
- Modify: `docs/architecture.md`（数据流图更新）

- [ ] **Step 1: 创建 core/storage/README.md**

遵循 readme-conventions.md Core 子系统模板：

```markdown
> 文档导航：[项目文档](../../README.md)

# core/storage — 统一存储层

系统持久化中间层。Repository 模式封装 MongoDB/Milvus 操作，
消除业务代码对数据库的直接访问。

## 模块速查

| 文件 | 内容 |
|------|------|
| `base.py` | Repository ABC（connect / health_check / close） |
| `session_repo.py` | SessionRepository — MongoDB sessions 集合 |
| `message_repo.py` | MessageRepository — MongoDB message_archive 集合 |
| `token_repo.py` | TokenRepository — MongoDB token_usage 集合 |
| `memory_repo.py` | MemoryRepository — Milvus long_term_memory 集合 |
| `character_state_repo.py` | CharacterStateRepository — MongoDB character_state 集合 |

## 设计原则

- **Repository 模式**：每个集合一个 Repository，统一 connect/health_check/close 生命周期
- **类型化接口**：接受和返回 Pydantic/schema 类型，不暴露裸 dict
- **无 fallback 逻辑**：JSONL 备胎路径保留在调用方（SessionMemory / TokenTracker），Repository 只管数据库

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/schema/README.md](../schema/README.md) | 基础类型定义层 |
| [存储层设计 spec](../../docs/specs/2026-07-16-storage-layer-design.md) | 本模块的 spec |
```

- [ ] **Step 2: 注册到根 README 文档导航**

在 Core 层表中新增：

```markdown
| Storage | [core/storage/README.md](core/storage/README.md) | 统一存储层（Repository 模式） |
```

- [ ] **Step 3: 更新 core/schema/README.md**

模块速查表新增：

```markdown
| `token.py` | TokenUsageEntry — Token 用量记录 |
| `character.py` | CharacterState — 角色性格状态快照 |
```

- [ ] **Step 4: 全量验证**

```bash
# import 验证
python -c "
from core.storage import SessionRepository, MessageRepository, TokenRepository
from core.storage import MemoryRepository, CharacterStateRepository
from core.schema import TokenUsageEntry, CharacterState
print('All imports OK')
"

# 完整快速套件
python -m pytest -m "unit or service" -q --tb=short 2>&1 | tail -3
```

Expected: 1585+ passed，已知 flaky 除外

- [ ] **Step 5: 提交**

```bash
git add core/storage/README.md README.md core/schema/README.md docs/architecture.md
git commit -m "docs: core/storage/README.md + 文档注册 + schema README 更新"
```
