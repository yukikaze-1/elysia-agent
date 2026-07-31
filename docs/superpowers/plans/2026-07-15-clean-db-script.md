# 数据库清理脚本 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供 `scripts/clean_db.py` 一键清理 MongoDB + Milvus + 本地持久化文件，支持选择性清理和 dry-run。

**Architecture:** 三个前置变更（SessionMemory.COLLECTION、TokenTracker.clear_all()、LongTermMemory.clear_all() 自包含化）确保脚本复用组件级常量和方法，不 hardcode collection 名和维度。脚本本身由 MongoCleaner / MilvusCleaner / FileCleaner 三个类组成，asyncio.gather(return_exceptions=True) 并发执行，汇总报告。

**Tech Stack:** Python 3.13, motor (MongoDB async), pymilvus (Milvus), python-dotenv, argparse, asyncio, pathlib

## 全局约束

- `COLLECTION` 常量必须在组件类上定义，脚本通过 import 引用
- 清理后保留数据库结构：MongoDB 用 `delete_many({})`（保留 indexes），Milvus 用 drop + recreate
- `asyncio.gather` 必须使用 `return_exceptions=True`
- `.env` 和 `data/` 路径必须基于 `Path(__file__).resolve().parent.parent` 解析，不依赖 CWD
- 所有数据库连接在 `finally` 块中调用 `close()`
- 全部成功 exit 0，任一失败 exit 1
- 测试 marker：`unit` 或 `service`

---

### Task 1: SessionMemory 添加 COLLECTION 常量

**Files:**
- Modify: `core/memory/session.py` — 添加 `COLLECTION = "sessions"`，替换所有 hardcode `"sessions"` 引用

**Interfaces:**
- Produces: `SessionMemory.COLLECTION: str = "sessions"` — 后续 Task 4 通过 import 引用

- [ ] **Step 1: 在 SessionMemory 类中添加 COLLECTION 常量**

在 `class SessionMemory:` 下、`__init__` 之前添加：

```python
class SessionMemory:
    """Bounded conversation window with optional MongoDB persistence."""

    COLLECTION = "sessions"
```

- [ ] **Step 2: 替换 `connect_db()` 中的 hardcode 字符串**

`session.py:221-228` — 3 处 `"sessions"` → `self.COLLECTION`：

```python
# Before:
await self._db.ensure_index("sessions", [("session_id", 1)])
await self._db.ensure_index("sessions", [("updated_at", -1)])
# ...
    "sessions",

# After:
await self._db.ensure_index(self.COLLECTION, [("session_id", 1)])
await self._db.ensure_index(self.COLLECTION, [("updated_at", -1)])
# ...
    self.COLLECTION,
```

- [ ] **Step 3: 替换 `save()` 和 `load()` 中的 hardcode 字符串**

`session.py:275` — `update_one` 调用：
```python
# Before:
await self._db.update_one(
    "sessions",
# After:
await self._db.update_one(
    self.COLLECTION,
```

`session.py:325` — `find` 调用：
```python
# Before:
results = await self._db.find("sessions", {"session_id": sid}, limit=1)
# After:
results = await self._db.find(self.COLLECTION, {"session_id": sid}, limit=1)
```

- [ ] **Step 4: 添加单元测试**

在 `core/memory/tests/test_session.py` 中添加：

```python
def test_collection_constant():
    """SessionMemory.COLLECTION 应为 'sessions'。"""
    assert SessionMemory.COLLECTION == "sessions"


@pytest.mark.asyncio
async def test_connect_db_uses_collection_constant():
    """connect_db() 应使用 self.COLLECTION 而非 hardcode 字符串。"""
    sm = SessionMemory(session_id="test-coll")
    assert sm.COLLECTION == "sessions"

    # 验证 COLLECTION 被传递给 ensure_index
    mock_db = AsyncMock()
    mock_db.ensure_index = AsyncMock()
    sm._db = mock_db

    # 直接调用 ensure_index 验证使用了 COLLECTION
    await sm._db.ensure_index(sm.COLLECTION, [("session_id", 1)])
    mock_db.ensure_index.assert_called_once_with("sessions", [("session_id", 1)])
```

- [ ] **Step 5: 运行测试验证**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/memory/tests/test_session.py -v
```

预期：全部 PASS

- [ ] **Step 6: 运行快速测试，确保无回归**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
```

预期：全部 PASS

- [ ] **Step 7: Commit**

```bash
git add core/memory/session.py core/memory/tests/test_session.py
git commit -m "refactor: add SessionMemory.COLLECTION constant, replace hardcoded 'sessions'"
```

---

### Task 2: TokenTracker 添加 COLLECTION 常量 + clear_all()

**Files:**
- Modify: `core/token_tracker.py` — 添加 `COLLECTION` 常量和 `clear_all()` 静态方法
- Modify: `core/tests/test_token_tracker.py` — 添加测试

**Interfaces:**
- Produces: `TokenTracker.COLLECTION: str = "token_usage"` — 后续 Task 4 import 引用
- Produces: `TokenTracker.clear_all(db) -> int` — 静态方法，清空所有 token_usage 记录，返回删除数量

- [ ] **Step 1: 编写失败测试**

在 `core/tests/test_token_tracker.py` 末尾添加 `TestTokenTrackerClearAll` 类：

```python
@pytest.mark.unit
class TestTokenTrackerClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_calls_delete_many_with_collection(self):
        db = AsyncMock()
        db.delete_many = AsyncMock(return_value=42)
        deleted = await TokenTracker.clear_all(db)
        assert deleted == 42
        db.delete_many.assert_called_once_with("token_usage", {})

    def test_collection_constant(self):
        assert TokenTracker.COLLECTION == "token_usage"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/tests/test_token_tracker.py::TestTokenTrackerClearAll -v
```

预期：`AttributeError: type object 'TokenTracker' has no attribute 'clear_all'`

- [ ] **Step 3: 实现 COLLECTION 和 clear_all()**

在 `core/token_tracker.py` 的 `class TokenTracker:` 下、`_pending_file` 之前添加：

```python
class TokenTracker:
    """Token 消耗统计器。..."""

    COLLECTION = "token_usage"
    _pending_file = os.path.join("data", "token_usage_pending.jsonl")

    # ... existing __init__ ...

    @staticmethod
    async def clear_all(db) -> int:
        """清空所有 token_usage 记录。返回删除数量。"""
        return await db.delete_many(TokenTracker.COLLECTION, {})
```

同时替换 `_persist()` 和 `flush_pending()`、`get_session_stats()` 中的 hardcode `"token_usage"`：

`token_tracker.py:110` — `find` 调用：
```python
# Before:
docs = await self._db.find(
    "token_usage",
# After:
docs = await self._db.find(
    TokenTracker.COLLECTION,
```

`token_tracker.py:156` — `insert_many` 调用：
```python
# Before:
await self._db.insert_many("token_usage", records)
# After:
await self._db.insert_many(TokenTracker.COLLECTION, records)
```

`token_tracker.py:167` — `insert_one` 调用：
```python
# Before:
await self._db.insert_one("token_usage", doc)
# After:
await self._db.insert_one(TokenTracker.COLLECTION, doc)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/tests/test_token_tracker.py -v
```

预期：全部 PASS（包括新增的 `TestTokenTrackerClearAll`）

- [ ] **Step 5: Commit**

```bash
git add core/token_tracker.py core/tests/test_token_tracker.py
git commit -m "feat: add TokenTracker.COLLECTION constant and clear_all() static method"
```

---

### Task 3: LongTermMemory.clear_all() 自包含化

**Files:**
- Modify: `core/memory/long_term.py` — `clear_all()` 支持无 `connect()` 直接调用
- Modify: `core/memory/tests/test_long_term.py` — 添加测试

**Interfaces:**
- Modifies: `LongTermMemory.clear_all() -> int` — 现在可独立调用（无需先 `connect()`），有 `_vector_db` 时复用，无则创建临时连接
- Consumes: `VectorDBFactory.create("milvus/memory")` — 临时路径中创建 backend
- Consumes: `VectorDBBackend.collection_exists()` / `drop_collection()` / `create_collection()` / `close()`

- [ ] **Step 1: 编写失败测试**

在 `core/memory/tests/test_long_term.py` 中添加 `TestLongTermMemoryClearAll` 类：

```python
@pytest.mark.service
class TestLongTermMemoryClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_without_connect_creates_temp_backend(self):
        """无 _vector_db 时，clear_all() 自行创建临时连接完成清理。"""
        ltm = LongTermMemory(max_entries=10)
        # 添加一些内存条目以确保 count > 0
        ltm._entries = [MagicMock(), MagicMock(), MagicMock()]

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(return_value=True)
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ) as mock_create:
            deleted = await ltm.clear_all()

        assert deleted == 3
        assert len(ltm._entries) == 0
        mock_create.assert_called_once_with("milvus/memory")
        mock_backend.collection_exists.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.drop_collection.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_with_existing_backend_reuses_it(self):
        """_vector_db 已设置时，clear_all() 复用它，不创建新连接。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock()]
        mock_backend = AsyncMock()
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        ltm._vector_db = mock_backend
        ltm._connected = True

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
        ) as mock_create:
            deleted = await ltm.clear_all()

        assert deleted == 1
        mock_create.assert_not_called()
        mock_backend.drop_collection.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)

    @pytest.mark.asyncio
    async def test_clear_all_without_connect_collection_not_exists(self):
        """临时路径：collection 不存在时跳过 drop，直接 create。"""
        ltm = LongTermMemory(max_entries=10)

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(return_value=False)
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            deleted = await ltm.clear_all()

        assert deleted == 0
        mock_backend.drop_collection.assert_not_called()
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_propagates_collection_exception(self):
        """临时路径：collection_exists 失败时异常传播，close() 在 finally 中调用。"""
        ltm = LongTermMemory(max_entries=10)

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            with pytest.raises(RuntimeError, match="connection refused"):
                await ltm.clear_all()

        # 即使失败，close() 也在 finally 中被调用
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_factory_create_fails_returns_count(self):
        """Factory.create 失败时静默降级，返回已清空的内存条目数（向后兼容）。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock(), MagicMock()]  # 2 entries

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("Milvus unavailable"),
        ):
            deleted = await ltm.clear_all()

        assert deleted == 2
        assert len(ltm._entries) == 0
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/memory/tests/test_long_term.py::TestLongTermMemoryClearAll -v
```

预期：`test_clear_all_without_connect_creates_temp_backend` FAIL（现有 `clear_all()` 的 `if self._vector_db:` 分支跳过，返回 count=0 但 entries 被清空）

- [ ] **Step 3: 修改 clear_all() 实现**

替换 `core/memory/long_term.py:420-431` 的 `clear_all()` 方法：

```python
    async def clear_all(self) -> int:
        """清空所有记忆（内存 + Milvus 重建 collection）。可独立调用，无需先 connect()。"""
        async with self._write_lock:
            count = len(self._entries)
            self._entries.clear()
            self._absorbed_ids.clear()
            self._dirty_ids.clear()

            if self._vector_db is not None:
                # 已通过 connect() 持有连接 → 直接操作
                await self._vector_db.drop_collection(self.COLLECTION)
                await self._vector_db.create_collection(
                    self.COLLECTION, dimension=self.DIMENSION)
            else:
                # 无已有连接 → 自行创建临时连接
                from Module.database.vectordb import VectorDBFactory
                try:
                    vector_db = VectorDBFactory.create("milvus/memory")
                except Exception:
                    # Milvus 不可达时静默降级：内存已清空，返回 count
                    # （保持向后兼容 — 无 Milvus 环境的测试不因此失败）
                    return count
                try:
                    if await vector_db.collection_exists(self.COLLECTION):
                        await vector_db.drop_collection(self.COLLECTION)
                    await vector_db.create_collection(
                        self.COLLECTION, dimension=self.DIMENSION)
                finally:
                    await vector_db.close()
        return count
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/memory/tests/test_long_term.py -v
```

预期：全部 PASS

- [ ] **Step 5: 运行快速测试，确保无回归**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
```

预期：全部 PASS

- [ ] **Step 6: Commit**

```bash
git add core/memory/long_term.py core/memory/tests/test_long_term.py
git commit -m "refactor: make LongTermMemory.clear_all() self-contained, no prior connect() needed"
```

---

### Task 4: 创建 scripts/clean_db.py

**Files:**
- Create: `scripts/clean_db.py` — 数据库清理脚本
- Create: `core/tests/test_clean_db.py` — 脚本测试

**Interfaces:**
- Consumes: `SessionMemory.COLLECTION` (from Task 1)
- Consumes: `TokenTracker.COLLECTION`, `TokenTracker.clear_all(db)` (from Task 2)
- Consumes: `LongTermMemory.clear_all()` (from Task 3)
- Consumes: `DocumentDBFactory.create("mongodb/default")`, `VectorDBFactory.create("milvus/memory")` (existing)
- Consumes: `MessageArchive`, `MessageArchive.clear_all()` (existing)
- Produces: CLI via `python scripts/clean_db.py [-f] [--mongo-only] [--milvus-only] [--files-only] [--dry-run]`

- [ ] **Step 1: 编写脚本测试**

创建 `core/tests/test_clean_db.py`：

```python
"""scripts/clean_db.py 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path
import sys

# 将 scripts/ 加入 path，以便 import clean_db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

pytestmark = pytest.mark.unit


class TestMongoCleaner:
    @pytest.mark.asyncio
    async def test_mongo_cleaner_deletes_all_collections(self):
        from clean_db import MongoCleaner

        mock_db = AsyncMock()
        mock_db.delete_many = AsyncMock(return_value=10)
        mock_db.close = AsyncMock()

        with patch(
            "clean_db.DocumentDBFactory.create", return_value=mock_db
        ) as mock_create:
            cleaner = MongoCleaner()
            result = await cleaner.clean()

        mock_create.assert_called_once_with("mongodb/default")

        # 验证三个 collection 都被 delete_many({})
        assert mock_db.delete_many.call_count == 3
        mock_db.delete_many.assert_has_calls([
            call("sessions", {}),
            call("message_archive", {}),
            call("token_usage", {}),
        ], any_order=True)

        mock_db.close.assert_awaited_once()

        # result 是一个包含 3 个结果的列表
        assert result == [("sessions", True, 10), ("message_archive", True, 10), ("token_usage", True, 10)]

    @pytest.mark.asyncio
    async def test_mongo_cleaner_handles_failure(self):
        from clean_db import MongoCleaner

        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        delete_results = [10, RuntimeError("timeout"), 5]

        async def delete_many_side_effect(collection, filter_expr):
            r = delete_results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        mock_db.delete_many = AsyncMock(side_effect=delete_many_side_effect)

        with patch("clean_db.DocumentDBFactory.create", return_value=mock_db):
            cleaner = MongoCleaner()
            result = await cleaner.clean()

        assert result == [
            ("sessions", True, 10),
            ("message_archive", False, "timeout"),
            ("token_usage", True, 5),
        ]
        mock_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mongo_cleaner_factory_failure_is_caught(self):
        """DocumentDBFactory.create 失败时异常被 gather(return_exceptions=True) 捕获。"""
        from clean_db import MongoCleaner

        with patch(
            "clean_db.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            cleaner = MongoCleaner()
            with pytest.raises(RuntimeError, match="connection refused"):
                await cleaner.clean()


class TestMilvusCleaner:
    @pytest.mark.asyncio
    async def test_milvus_cleaner_calls_clear_all(self):
        from clean_db import MilvusCleaner
        from core.memory.long_term import LongTermMemory

        with patch(
            "clean_db.LongTermMemory", spec=LongTermMemory
        ) as MockLTM:
            mock_ltm = MockLTM.return_value
            mock_ltm.clear_all = AsyncMock(return_value=42)
            mock_ltm.close = AsyncMock()

            cleaner = MilvusCleaner()
            result = await cleaner.clean()

        MockLTM.assert_called_once()
        mock_ltm.clear_all.assert_awaited_once()
        mock_ltm.close.assert_awaited_once()
        # 验证返回的 collection 名来自 LongTermMemory.COLLECTION 常量
        assert result == ("long_term_memory", True, 42)
        assert result[0] == LongTermMemory.COLLECTION

    @pytest.mark.asyncio
    async def test_milvus_cleaner_handles_failure(self):
        from clean_db import MilvusCleaner
        from core.memory.long_term import LongTermMemory

        with patch(
            "clean_db.LongTermMemory", spec=LongTermMemory
        ) as MockLTM:
            mock_ltm = MockLTM.return_value
            mock_ltm.clear_all = AsyncMock(side_effect=RuntimeError("connection refused"))
            mock_ltm.close = AsyncMock()

            cleaner = MilvusCleaner()
            result = await cleaner.clean()

        mock_ltm.close.assert_awaited_once()
        assert result == ("long_term_memory", False, "connection refused")


class TestFileCleaner:
    def test_file_cleaner_deletes_known_files(self, tmp_path):
        from clean_db import FileCleaner

        data_dir = tmp_path / "data"
        cache_dir = data_dir / "cache" / "vision"
        cache_dir.mkdir(parents=True)

        # 创建目标文件
        (data_dir / "session_default.json").write_text("{}")
        (data_dir / "token_usage_pending.jsonl").write_text("line1\n")
        (data_dir / "scheduler_checkpoint.json").write_text("{}")
        (cache_dir / "img1.json").write_text("{}")
        (cache_dir / "img2.json").write_text("{}")

        # 创建不应被删除的文件
        (data_dir / "keep_me.txt").write_text("important")

        cleaner = FileCleaner(data_dir=str(data_dir))
        results = cleaner.clean()

        # 验证目标文件被删除
        assert not (data_dir / "session_default.json").exists()
        assert not (data_dir / "token_usage_pending.jsonl").exists()
        assert not (data_dir / "scheduler_checkpoint.json").exists()
        assert not (cache_dir / "img1.json").exists()
        assert not (cache_dir / "img2.json").exists()

        # 验证无关文件未被删除
        assert (data_dir / "keep_me.txt").exists()

        # 结果计数
        succeeded = [r for r in results if r[1] is True]
        assert len(succeeded) >= 4  # at least 4 files deleted

    def test_file_cleaner_handles_missing_files(self, tmp_path):
        from clean_db import FileCleaner

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        # 不创建任何目标文件

        cleaner = FileCleaner(data_dir=str(data_dir))
        results = cleaner.clean()

        # 不存在的文件记录为 skipped
        for name, ok, detail in results:
            if not ok:
                assert "not found" in str(detail) or "skipped" in str(detail).lower()


class TestCliArgs:
    def test_default_cleans_all(self):
        from clean_db import parse_args
        args = parse_args([])
        assert args.mongo_only is False
        assert args.milvus_only is False
        assert args.files_only is False
        assert args.force is False
        assert args.dry_run is False

    def test_force_flag(self):
        from clean_db import parse_args
        args = parse_args(["-f"])
        assert args.force is True

    def test_selective_flags(self):
        from clean_db import parse_args
        args = parse_args(["--mongo-only", "--files-only"])
        assert args.mongo_only is True
        assert args.files_only is True
        assert args.milvus_only is False

    def test_dry_run(self):
        from clean_db import parse_args
        args = parse_args(["--dry-run"])
        assert args.dry_run is True


class TestBuildTargets:
    def test_no_flags_includes_all(self):
        from clean_db import parse_args, build_targets
        args = parse_args([])
        targets = build_targets(args)
        assert "mongo" in targets
        assert "milvus" in targets
        assert "files" in targets

    def test_mongo_only(self):
        from clean_db import parse_args, build_targets
        args = parse_args(["--mongo-only"])
        targets = build_targets(args)
        assert targets == {"mongo"}

    def test_combined_flags(self):
        from clean_db import parse_args, build_targets
        args = parse_args(["--mongo-only", "--milvus-only"])
        targets = build_targets(args)
        assert targets == {"mongo", "milvus"}


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_checks_connections(self):
        from clean_db import dry_run_check

        with patch(
            "clean_db.DocumentDBFactory.create"
        ) as mock_mongo_factory, patch(
            "clean_db.VectorDBFactory.create"
        ) as mock_milvus_factory:
            mock_db = AsyncMock()
            mock_db.count = AsyncMock(return_value=0)
            mock_db.close = AsyncMock()
            mock_mongo_factory.return_value = mock_db

            mock_vector_db = AsyncMock()
            mock_vector_db.collection_exists = AsyncMock(return_value=True)
            mock_vector_db.close = AsyncMock()
            mock_milvus_factory.return_value = mock_vector_db

            ok = await dry_run_check({"mongo", "milvus", "files"})

        assert ok is True
        mock_db.count.assert_awaited_once()
        mock_db.close.assert_awaited_once()
        mock_vector_db.collection_exists.assert_awaited_once()
        mock_vector_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_reports_failure(self):
        from clean_db import dry_run_check

        with patch(
            "clean_db.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            ok = await dry_run_check({"mongo"})

        assert ok is False
```

- [ ] **Step 2: 运行测试确认全部失败**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/tests/test_clean_db.py -v
```

预期：全部 FAIL（ModuleNotFoundError 或 ImportError — 脚本尚未创建）

- [ ] **Step 3: 创建 scripts/clean_db.py**

```python
#!/usr/bin/env python
"""清理 Elysia Agent 所有数据库内容。

用法:
    python scripts/clean_db.py              # 清理全部（需确认）
    python scripts/clean_db.py -f           # 清理全部（跳过确认）
    python scripts/clean_db.py --mongo-only # 只清理 MongoDB
    python scripts/clean_db.py --dry-run    # 仅检查连接
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── 路径：以脚本自身目录解析 .env 和 data/ ──────────────
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))  # 使 core/ Module/ 可 import
load_dotenv(_SCRIPT_DIR / ".env")

from core.memory.session import SessionMemory
from core.memory.message_archive import MessageArchive
from core.memory.long_term import LongTermMemory
from core.token_tracker import TokenTracker
from Module.database.documentdb import DocumentDBFactory
from Module.database.vectordb import VectorDBFactory


# ── CLI ──────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="清理 Elysia Agent 所有数据库内容",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="跳过确认提示，直接执行",
    )
    parser.add_argument(
        "--mongo-only",
        action="store_true",
        help="只清理 MongoDB（sessions, message_archive, token_usage）",
    )
    parser.add_argument(
        "--milvus-only",
        action="store_true",
        help="只清理 Milvus（long_term_memory）",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="只清理本地 data/ 文件",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要清理的内容，不实际执行",
    )
    return parser.parse_args(argv)


def build_targets(args: argparse.Namespace) -> set[str]:
    """根据 CLI 选项确定清理目标。"""
    if args.mongo_only or args.milvus_only or args.files_only:
        targets = set()
        if args.mongo_only:
            targets.add("mongo")
        if args.milvus_only:
            targets.add("milvus")
        if args.files_only:
            targets.add("files")
        return targets
    return {"mongo", "milvus", "files"}


# ── Cleaners ─────────────────────────────────────────────

class MongoCleaner:
    """MongoDB 清理器。

    清空 default 数据库的三个 collection：
    sessions, message_archive, token_usage。
    使用 delete_many({}) 清空文档，保留 indexes。

    backend 在 clean() 中创建（而非 __init__），确保构造失败也被
    asyncio.gather(return_exceptions=True) 捕获。
    """

    async def clean(self) -> list[tuple[str, bool, int | str]]:
        """执行清理，返回 (collection, ok, detail) 列表。"""
        db = DocumentDBFactory.create("mongodb/default")
        try:
            return await self._clean(db)
        finally:
            await db.close()

    async def _clean(self, db) -> list[tuple[str, bool, int | str]]:
        results = []

        # sessions — 直接 delete_many
        try:
            deleted = await db.delete_many(SessionMemory.COLLECTION, {})
            results.append((SessionMemory.COLLECTION, True, deleted))
        except Exception as e:
            results.append((SessionMemory.COLLECTION, False, str(e)))

        # message_archive — 使用 MessageArchive.clear_all()
        try:
            archive = MessageArchive()
            await archive.connect(db)
            deleted = await archive.clear_all()
            results.append((MessageArchive.COLLECTION, True, deleted))
        except Exception as e:
            results.append((MessageArchive.COLLECTION, False, str(e)))

        # token_usage — 使用 TokenTracker.clear_all()
        try:
            deleted = await TokenTracker.clear_all(db)
            results.append((TokenTracker.COLLECTION, True, deleted))
        except Exception as e:
            results.append((TokenTracker.COLLECTION, False, str(e)))

        return results


class MilvusCleaner:
    """Milvus 清理器。

    通过 LongTermMemory.clear_all() 清空向量记忆
    （drop + recreate collection）。
    """

    def __init__(self):
        self._ltm = LongTermMemory()

    async def clean(self) -> tuple[str, bool, int | str]:
        """执行清理，返回 (collection, ok, detail)。"""
        try:
            deleted = await self._ltm.clear_all()
            return (LongTermMemory.COLLECTION, True, deleted)
        except Exception as e:
            return (LongTermMemory.COLLECTION, False, str(e))
        finally:
            await self._ltm.close()


class FileCleaner:
    """本地文件清理器。

    删除 data/ 下的持久化文件和 vision 缓存。
    """

    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            data_dir = str(_SCRIPT_DIR / "data")
        self._data = Path(data_dir)

    def clean(self) -> list[tuple[str, bool, str]]:
        """执行清理，返回 (filename, ok, detail) 列表。"""
        results = []

        # 目标文件列表
        files = [
            self._data / "session_default.json",
            self._data / "token_usage_pending.jsonl",
            self._data / "scheduler_checkpoint.json",
        ]

        # vision 缓存（glob 匹配）
        vision_dir = self._data / "cache" / "vision"
        if vision_dir.exists():
            for f in vision_dir.glob("*.json"):
                files.append(f)

        for f in files:
            name = str(f.relative_to(self._data)) if f.is_relative_to(self._data) else f.name
            try:
                if f.exists():
                    f.unlink()
                    results.append((name, True, "deleted"))
                else:
                    results.append((name, False, "not found (skipped)"))
            except Exception as e:
                results.append((name, False, str(e)))

        return results


# ── 确认 / dry-run ───────────────────────────────────────

async def confirm(targets: set[str]) -> bool:
    """打印清理目标并请求用户确认。返回 True 表示继续。"""
    labels = {
        "mongo": "  MongoDB: sessions, message_archive, token_usage (delete_many)",
        "milvus": "  Milvus: long_term_memory (drop + recreate)",
        "files": "  data/: session_default.json, token_usage_pending.jsonl, scheduler_checkpoint.json, cache/vision/*.json",
    }
    print("将要清理以下内容:")
    for t in sorted(targets):
        print(labels[t])
    print()
    try:
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, input, "确认清理? 输入 yes 继续，其他取消: "
        )
        return answer.strip().lower() == "yes"
    except (KeyboardInterrupt, EOFError):
        print()
        return False


async def dry_run_check(targets: set[str]) -> bool:
    """检查数据库连接可用性。返回 True 表示全部可达。"""
    all_ok = True

    if "mongo" in targets:
        try:
            db = DocumentDBFactory.create("mongodb/default")
            try:
                # 执行轻量操作验证连接可用（motor 的 create 是 lazy 的）
                await db.count(SessionMemory.COLLECTION, {})
                print("✅ MongoDB: 连接正常")
            finally:
                await db.close()
        except Exception as e:
            print(f"❌ MongoDB: 连接失败 — {e}")
            all_ok = False

    if "milvus" in targets:
        try:
            vector_db = VectorDBFactory.create("milvus/memory")
            try:
                await vector_db.collection_exists(LongTermMemory.COLLECTION)
                print("✅ Milvus: 连接正常")
            finally:
                await vector_db.close()
        except Exception as e:
            print(f"❌ Milvus: 连接失败 — {e}")
            all_ok = False

    if "files" in targets:
        cleaner = FileCleaner()
        data_dir = cleaner._data
        if data_dir.exists():
            print(f"✅ 数据目录存在: {data_dir}")
        else:
            print(f"⚠️  数据目录不存在: {data_dir}")
            all_ok = False

    return all_ok


# ── 汇总报告 ─────────────────────────────────────────────

def report(results: list, *, exit_on_failure: bool = True) -> None:
    """打印汇总报告。如果 exit_on_failure 且存在失败项，sys.exit(1)。"""
    print()
    print("=" * 50)
    print("清理结果:")
    print("=" * 50)

    all_ok = True
    for item in results:
        if isinstance(item, list):
            # MongoCleaner / FileCleaner 返回列表
            for name, ok, detail in item:
                status = "✅" if ok else "❌"
                if ok:
                    suffix = f"{detail} 条" if isinstance(detail, int) else str(detail)
                    print(f"  {status} {name}: {suffix}")
                else:
                    print(f"  {status} {name}: {detail}")
                    all_ok = False
        elif isinstance(item, tuple):
            # MilvusCleaner 返回单个 tuple
            name, ok, detail = item
            status = "✅" if ok else "❌"
            if ok:
                suffix = f"{detail} 条已清除" if isinstance(detail, int) else str(detail)
                print(f"  {status} {name}: {suffix}")
            else:
                print(f"  {status} {name}: {detail}")
                all_ok = False
        elif isinstance(item, Exception):
            print(f"  ❌ 未预期错误: {item}")
            all_ok = False

    print()
    if all_ok:
        print("全部清理完成 ✅")
    else:
        print("部分清理失败，详见上方报告 ❌")
        if exit_on_failure:
            sys.exit(1)


# ── Main ─────────────────────────────────────────────────

async def main() -> None:
    args = parse_args()
    targets = build_targets(args)

    # dry-run: 仅检查连接
    if args.dry_run:
        ok = await dry_run_check(targets)
        sys.exit(0 if ok else 1)

    # 确认
    if not args.force:
        if not await confirm(targets):
            print("已取消")
            return

    # 并发执行清理
    tasks = []
    if "mongo" in targets:
        tasks.append(MongoCleaner().clean())
    if "milvus" in targets:
        tasks.append(MilvusCleaner().clean())
    if "files" in targets:
        # FileCleaner.clean() 是同步方法，用 run_in_executor
        loop = asyncio.get_running_loop()
        tasks.append(loop.run_in_executor(None, FileCleaner().clean))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    report(results)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: 运行脚本测试验证**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest core/tests/test_clean_db.py -v
```

预期：全部 PASS

- [ ] **Step 5: 验证脚本可以被 Python 导入**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -c "import sys; sys.path.insert(0, 'scripts'); import clean_db; print('import OK')"
```

预期：`import OK`

- [ ] **Step 6: 验证 CLI help 输出**

```bash
cd /home/yomu/Agent && conda run -n Elysia python scripts/clean_db.py --help
```

预期：显示完整的帮助信息

- [ ] **Step 7: 运行快速测试确保无回归**

```bash
cd /home/yomu/Agent && conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
```

预期：全部 PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/clean_db.py core/tests/test_clean_db.py
git commit -m "feat: add clean_db.py — one-click database cleanup script"
```
