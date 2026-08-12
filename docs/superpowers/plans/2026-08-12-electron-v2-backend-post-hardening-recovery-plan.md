# Electron v2 后端加固后复核整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 review `docs/review/electron-v2-backend-post-hardening-review-2026-08-12.md` 的 BE-P1-06（完整记录恢复 API）、BE-P1-07（恢复触发条件）、BE-P2-04（runbook 修正），并诚实记录 BE-RISK-01。

**Architecture:** 幂等恢复改为「条件 upsert」完整文档补落（`IdempotencyRepository.restore`）；`IdempotencyStore` 新增 `_needs_recovery` 显式标志并强化 `durable`（无关成功操作不再静默清零）；协调器新增 `_dependency_needs_recovery` 钩子补首轮直接 healthy 漏补偿；product 模式 `/ready` 门禁含补偿状态（`ping AND not needs_recovery`）。

**Tech Stack:** Python 3.12、aiohttp、motor（MongoDB）、pytest（unit/service marker）。

## Global Constraints

- **报告门禁（§9）**：BE-P1-06/07 所有组合故障测试通过；必须含「清空/重建 IdempotencyStore 后从 Mongo 读取」断言；首轮 probe 直接 true 与 recover 连续失败重试均有测试；accepted/rejected 重放比较完整 wire JSON；既有并发 claim/DuplicateKey/三态/限流/会话/readiness 测试不回归；`python -m frontend_contract --check` 与 `python scripts/gen_contract_frames.py --check` 通过；Runbook 不再要求 wire 中不存在的字段；todo.md 与报告诚实区分「已修缺陷」vs「BE-RISK-01 接受风险」；复跑报告 §2.1 的 230 项定向测试 + quick/normal。
- **CLAUDE.md**：禁相对导入（ruff TID252，`__init__`/`__main__` 豁免）；代码注释中文；生产代码参数类型注解（ANN001/002/003）；conda env `Elysia` 跑测试/门禁。
- **存储语义**：`StorageUnavailableError` fail-closed（base.py）；DuplicateKey 业务冲突经 `_call(non_fatal=...)` 裸抛不判死 repo；墙钟 `time.time()`（勿用 monotonic 与墙钟相减）；`GRACE_SECONDS` 全局单源（`core.input.idempotency`，repo 惰性 import）。
- **内存权威 overlay（D5）**：lookup/store 恒先查内存；Mongo 写穿；recover 只负责「补落/对齐」，不改变内存权威的常规路径。
- **遗留状态常量**：legacy `"in_progress"` 视同 claimed、`"completed"` 视同 accepted（`IdempotencyEntry.from_doc` 防御映射）。

---

### Task 1: repo 层 `restore(doc)` 条件 upsert + 身份键守卫

**Files:**
- Modify: `core/storage/idempotency_repo.py`（新增 `restore` 方法，置于 `release` 后）
- Test: `core/storage/tests/test_idempotency_repo.py`（新增 `TestRestore` 类）

**Interfaces:**
- Consumes: `_is_duplicate_key`、`_call`、`STATUS_CLAIMED`（均已在 repo 层存在）
- Produces: `IdempotencyRepository.restore(doc: dict) -> None`——完整记录原子 upsert（缺失插入 / claimed 升级 / 终态 E11000 防御读回）；Task 3 的 `store.recover()` 与 Task 4 的 fake 镜像依赖它。

- [ ] **Step 1: 写失败测试**

在 `core/storage/tests/test_idempotency_repo.py` 的 `TestConnect` 类之后新增：

```python
class _DuplicateKeyError(RuntimeError):
    """模拟 pymongo.errors.DuplicateKeyError（repo 按异常类名匹配，驱动无关）。"""


class _UpsertDuplicateFake(FakeDocumentDBBackend):
    """update_one upsert 对已存在同键 doc 抛 DuplicateKeyError（模拟唯一索引竞态）。

    与 _DuplicateOnceFake（拦 insert_one）不同——本 fake 拦 upsert 的 update_one，
    覆盖 restore() 的 E11000 防御层路径（终态同键 doc 过滤器失配 → upsert 尝试插入
    → 唯一索引冲突）。
    """

    def __init__(self, winner: dict | None = None) -> None:
        super().__init__()
        self._winner = winner
        self.duplicate_key_raised = 0  # E11000 触发计数（断言防御层真实走到的信号）

    async def update_one(self, collection, filter_expr, update, upsert=False):
        coll = self._coll(collection)
        key = (filter_expr.get("conversation_id"), filter_expr.get("client_request_id"))
        if upsert and any(
            d.get("conversation_id") == key[0] and d.get("client_request_id") == key[1]
            for d in coll
        ):
            self.duplicate_key_raised += 1
            raise _DuplicateKeyError("E11000 duplicate key error")
        return await super().update_one(collection, filter_expr, update, upsert=upsert)

    async def find(self, collection, filter_expr, **kwargs):
        if self._winner is not None:
            if (self._winner.get("conversation_id") == filter_expr.get("conversation_id")
                    and self._winner.get("client_request_id") == filter_expr.get("client_request_id")):
                return [dict(self._winner)]
        return await super().find(collection, filter_expr, **kwargs)


class _RecordingUpdateFake(FakeDocumentDBBackend):
    """记录 update_one 调用（filter/$set/upsert）供断言 restore 升级形态。"""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[dict, dict, bool]] = []

    async def update_one(self, collection, filter_expr, update, upsert=False):
        self.updates.append((filter_expr, update, upsert))
        return await super().update_one(collection, filter_expr, update, upsert=upsert)


class TestRestore:
    """IdempotencyRepository.restore——完整记录条件 upsert（BE-P1-06）。"""

    pytestmark = pytest.mark.service

    def _full_doc(self, *, status: str = STATUS_CLAIMED, message_id: str = "msg_1") -> dict:
        """对齐生产 _build_full_doc 形态：终态才含 ACK 负载键，None 键不写入。"""
        doc = {
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "d1",
            "message_id": message_id,
            "status": status,
            "claimed_at": time.time(),
            "expires_at": _expires(),
        }
        if status != STATUS_CLAIMED:
            doc["received_at"] = "2026-08-12T00:00:00Z"
            doc["client_message_id"] = "cm1"
            doc["conversation_id_wire"] = "wire-1"
            if status == STATUS_REJECTED:
                doc["error"] = {"code": "RATE_LIMITED", "message": "限流",
                                "retryable": True, "retryAfterMs": 30000}
        return doc

    def test_restore_missing_identity_keys_raises(self):
        """缺 conversation_id/client_request_id → ValueError（编程错误，非存储故障）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        with pytest.raises(ValueError):
            _run(repo.restore({"content_digest": "d1", "message_id": "m"}))

    def test_restore_inserts_full_doc(self):
        """Mongo 缺失 → upsert 插入完整文档（含终态 ACK 负载）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        doc = self._full_doc(status=STATUS_ACCEPTED)
        _run(repo.restore(doc))
        got = _run(repo.get("c1", "r1"))
        assert got is not None
        assert got["status"] == STATUS_ACCEPTED
        assert got["message_id"] == "msg_1"
        assert got["received_at"] == "2026-08-12T00:00:00Z"
        assert got["client_message_id"] == "cm1"
        assert got["conversation_id_wire"] == "wire-1"

    def test_restore_upgrades_claimed_to_terminal(self):
        """Mongo 已有 claimed → $set 升级为完整终态（保留 claimed 的身份键）。"""
        fake = _RecordingUpdateFake()
        fake.collections[COLLECTION] = [{
            "conversation_id": "c1", "client_request_id": "r1",
            "content_digest": "old", "message_id": "old-msg",
            "status": STATUS_CLAIMED, "claimed_at": time.time() - 60.0,
            "expires_at": _expires(),
        }]
        repo = IdempotencyRepository(db=fake)
        doc = self._full_doc(status=STATUS_ACCEPTED)
        _run(repo.restore(doc))
        # 过滤器含 status∈{claimed,in_progress}（命中升级）
        filt, update, upsert = fake.updates[0]
        assert upsert is True
        assert filt["status"] == {"$in": [STATUS_CLAIMED, "in_progress"]}
        set_expr = update["$set"]
        assert set_expr["status"] == STATUS_ACCEPTED
        assert set_expr["received_at"] == "2026-08-12T00:00:00Z"
        assert "error" not in set_expr  # accepted 无 error 键（对齐 _build_full_doc）
        # Mongo 已升级（$set 生效）
        got = _run(repo.get("c1", "r1"))
        assert got["status"] == STATUS_ACCEPTED
        assert got["received_at"] == "2026-08-12T00:00:00Z"

    def test_restore_terminal_winner_not_overwritten(self):
        """Mongo 已终态（另一进程 winner）→ 过滤器失配 → upsert 触发 E11000 →
        防御层读回 winner，不覆盖、repo 不判死。"""
        winner = self._full_doc(status=STATUS_REJECTED, message_id="winner-msg")
        fake = _UpsertDuplicateFake(winner=winner)
        fake.collections[COLLECTION] = [dict(winner)]
        repo = IdempotencyRepository(db=fake)
        repo._injected_db = False  # noqa: SLF001  # 强制非注入路径：E11000 必须不 _mark_unavailable
        loser = self._full_doc(status=STATUS_CLAIMED, message_id="loser-msg")
        # 不抛异常（E11000 防御层读回后返回）；断言防御层真实触发（非 upsert 漏掉后
        # 的 no-op 假绿——filter 对终态失配 + 无 upsert → update_one 无操作即过）
        assert fake.duplicate_key_raised >= 1, "E11000 防御层必须真实触发（upsert 插入同键）"
        assert repo.is_connected is True  # 不判死
        assert _run(repo.health_check()) is True  # 后续可用
        # Mongo 仍是 winner（未被 loser 覆盖）
        got = _run(repo.get("c1", "r1"))
        assert got["message_id"] == "winner-msg"
        assert got["status"] == STATUS_REJECTED

    def test_restore_offline_raises(self):
        """Mongo 离线（db=None）→ StorageUnavailableError（fail-closed）。"""
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.restore(self._full_doc()))

    def test_restore_inserts_terminal_status_from_set(self):
        """Mongo 缺失 + filter 含 status $in → upsert 插入文档的 status 来自 $set
        （非等值条件不并入插入文档）。"""
        fake = _RecordingUpdateFake()
        repo = IdempotencyRepository(db=fake)
        doc = self._full_doc(status=STATUS_REJECTED)
        _run(repo.restore(doc))
        _, update, upsert = fake.updates[0]
        assert upsert is True
        # 插入文档含 filter 等值字段（conversation_id/client_request_id）——upsert 语义
        got = _run(repo.get("c1", "r1"))
        assert got["status"] == STATUS_REJECTED
        assert got["error"]["code"] == "RATE_LIMITED"
        assert got["error"]["retryAfterMs"] == 30000
```

`_run`/`_expires`/`FakeDocumentDBBackend`/`pytest`/`time` 文件已有。需在 `test_idempotency_repo.py` 顶部 import 补 `COLLECTION`（现有 import 为 `IdempotencyRepository, STATUS_ACCEPTED, STATUS_CLAIMED, StorageUnavailableError`）——改为：

```python
from core.storage.idempotency_repo import (
    COLLECTION,
    IdempotencyRepository,
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_REJECTED,
    StorageUnavailableError,
)
```

`_RecordingUpdateFake`/`_UpsertDuplicateFake`/`_DuplicateKeyError` 均为本文件新增（上表内已含完整定义）。离线用例直接用 `IdempotencyRepository(db=None)`（对齐既有 `TestFailClosed`，无需 fake 类）。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest core/storage/tests/test_idempotency_repo.py::TestRestore -v --asyncio-mode=auto`
Expected: FAIL——`AttributeError: 'IdempotencyRepository' object has no attribute 'restore'`（无身份键用例先挂 `ValueError` 断言）

- [ ] **Step 3: 实现 `restore`**

在 `core/storage/idempotency_repo.py` 的 `release` 方法之后新增（复用 `_is_duplicate_key` 与 `_call(non_fatal)` 既有模式）：

```python
    async def restore(self, doc: dict) -> None:
        """完整记录恢复（BE-P1-06）：缺失插入 / claimed 升级 / 终态防御读回（原子 upsert）。

        区别于 claim()（只写最小 claim 字段），restore() 写入完整 ACK 重放负载
        （received_at/client_message_id/conversation_id_wire/error）——降级期终态补落
        Mongo 后跨重启重放逐字一致。

        过滤器 status∈{claimed,in_progress} + upsert：
        - Mongo 缺失 → 插入完整 doc；
        - Mongo 为 claimed（本进程写穿失败的前进状态）→ $set 升级为完整终态；
        - Mongo 已终态（另一进程 winner / 已落库）→ 过滤器失配，upsert 尝试插入同键 →
          唯一索引 E11000 → non_fatal 防御层读回 winner（不覆盖）。recover() 的
          get-first 已保证终态 doc 不进入本方法（Task 3），E11000 层仅兜底并发/直调。

        Args:
            doc: 完整记录（conversation_id/client_request_id 必含——身份键守卫，缺失
                抛 ValueError；其余为 content_digest/message_id/status/claimed_at/
                expires_at + 终态负载）。

        Raises:
            ValueError: 缺 conversation_id/client_request_id（编程错误，非存储故障）。
            StorageUnavailableError: 存储不可用。
        """
        db = self._require_db()
        if doc.get("conversation_id") is None or doc.get("client_request_id") is None:
            raise ValueError("IdempotencyRepository.restore 缺少身份键")
        set_expr = {k: v for k, v in doc.items()
                    if k not in ("conversation_id", "client_request_id")}
        try:
            await self._call(
                lambda: db.update_one(
                    COLLECTION,
                    {"conversation_id": doc["conversation_id"],
                     "client_request_id": doc["client_request_id"],
                     "status": {"$in": [STATUS_CLAIMED, "in_progress"]}},
                    {"$set": set_expr},
                    upsert=True,
                ),
                reason="IdempotencyRepository.restore 写入失败",
                non_fatal=_is_duplicate_key,
            )
        except Exception as e:
            if not _is_duplicate_key(e):
                raise
            # 并发 upsert 竞态（罕见 E11000）：读回 winner，不判死 repo（防御层）
            await self._call(
                lambda: db.find(COLLECTION,
                                {"conversation_id": doc["conversation_id"],
                                 "client_request_id": doc["client_request_id"]},
                                limit=1),
                reason="IdempotencyRepository.restore 冲突读回失败",
            )
            # winner 已在 Mongo；调用方（recover）内存已含完整终态或按 Mongo 优先刷新
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest core/storage/tests/test_idempotency_repo.py -v --asyncio-mode=auto`
Expected: PASS（既有 TestFailClosed/TestClaim/TestConnect 也不回归）

- [ ] **Step 5: Commit**

```bash
git add core/storage/idempotency_repo.py core/storage/tests/test_idempotency_repo.py
git commit -m "feat(idempotency): IdempotencyRepository.restore 完整记录条件 upsert（BE-P1-06）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: store 状态扩展 `_needs_recovery` + durable 强化

**Files:**
- Modify: `core/input/idempotency.py`（`__init__`/`durable`/新增 `configured`/`needs_recovery`/`store` 降级分支/`complete`/`reject` 写穿失败/`reset`）
- Test: `core/input/tests/test_idempotency_durable.py`（新增 `TestNeedsRecovery` 类）

**Interfaces:**
- Consumes: `_FlipRepo`（既有，Task 3 才加 `restore` 镜像；本任务不触碰 recover）
- Produces: `IdempotencyStore._needs_recovery`、`IdempotencyStore.configured`/`needs_recovery` property、durable 强化；Task 3 的 recover 清位、Task 4 协调器触发、Task 5 /ready 门禁全部依赖本任务的属性。

- [ ] **Step 1: 写失败测试**

在 `test_idempotency_durable.py` 的 `TestStoreOverlay` 类之后新增：

```python
class TestNeedsRecovery:
    """BE-P1-07 store 状态：_needs_recovery 显式标志 + durable 强化（S2 修复）。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_degraded_write_sets_needs_recovery(self, fresh_store):
        """dev 降级写内存 → _needs_recovery=True、durable=False。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))
        assert store.needs_recovery is True
        assert store.durable is False
        assert store.configured is True

    @pytest.mark.asyncio
    async def test_unrelated_success_does_not_clear_needs_recovery(self, fresh_store):
        """S2：降级记录后另一 key 的 store() 成功只清 fallback、不清 needs_recovery →
        durable 仍 False（无关操作不静默清零）。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid_1", _entry("msg_1", "abc"))  # 降级
        assert store.needs_recovery is True
        repo.available = True  # Mongo 恢复
        # 另一 key 的 store() 成功 → repo.claim 成功 → fallback=False，但 needs_recovery 保持
        result = await store.store("default", "crid_2", _entry("msg_2", "abc"))
        assert result.acquired is True
        assert store._in_memory_fallback is False  # noqa: SLF001  # 操作级已恢复
        assert store.needs_recovery is True        # 未 reconcile 记录仍在
        assert store.durable is False              # durable 强化：不因无关操作翻 True

    @pytest.mark.asyncio
    async def test_complete_write_through_failure_sets_needs_recovery(self, fresh_store):
        """complete() 写穿失败（best-effort 吞错）→ _needs_recovery=True（内存终态超前 Mongo）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))  # Mongo=claimed
        repo.available = False
        await store.complete("default", "crid", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        assert store.needs_recovery is True
        assert store.durable is False
        # 内存仍为 accepted（写穿失败不丢内存）
        mem = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem.status == STATUS_ACCEPTED
        assert mem.received_at == "2026-08-12T00:00:00Z"

    @pytest.mark.asyncio
    async def test_reject_write_through_failure_sets_needs_recovery(self, fresh_store):
        """reject() 写穿失败（best-effort 吞错）→ _needs_recovery=True（内存 rejected 超前 Mongo）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))  # Mongo=claimed
        repo.available = False
        await store.reject("default", "crid", code="CONTRACT_VALIDATION",
                           message="digest mismatch", retryable=False,
                           received_at="2026-08-12T00:00:00Z",
                           client_message_id="cm1", conversation_id_wire="wire-1")
        assert store.needs_recovery is True
        assert store.durable is False
        mem = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem.status == STATUS_REJECTED
        assert mem.error["code"] == "CONTRACT_VALIDATION"
        # Mongo 仍 claimed（写穿失败未落库）→ 需 recover 升级
        doc = await repo.get("default", "crid")
        assert doc["status"] == STATUS_CLAIMED

    @pytest.mark.asyncio
    async def test_reset_clears_needs_recovery(self, fresh_store):
        """reset() 清空 _needs_recovery（测试隔离，防顺序污染）。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        await get_store().store("default", "crid", _entry("msg_1", "abc"))
        assert get_store().needs_recovery is True
        reset_store()
        assert get_store().needs_recovery is False
        assert get_store().durable is False  # repo=None → durable False

    @pytest.mark.asyncio
    async def test_configured_property(self, fresh_store):
        """configured：repo 非 None True；repo=None False。"""
        assert get_store().configured is False
        configure_store(_FlipRepo(), product_mode=False)
        assert get_store().configured is True
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest core/input/tests/test_idempotency_durable.py::TestNeedsRecovery -v --asyncio-mode=auto`
Expected: FAIL——`AttributeError: 'IdempotencyStore' object has no attribute 'needs_recovery'`

- [ ] **Step 3: 实现 store 状态扩展**

在 `core/input/idempotency.py`：

(1) `__init__` 新增标志：

```python
        self._in_memory_fallback = True
        self._needs_recovery = False   # BE-P1-07：存在未 reconcile 到 Mongo 的内存记录
```

(2) `durable` 强化 + 新增 `configured`/`needs_recovery` property（替换现 `durable` 实现）：

```python
    @property
    def durable(self) -> bool:
        """当前是否完整持久化（BE-P1-07 强化）：repo 已配置 且 非操作级降级
        且 无未 reconcile 记录。server_info 能力暴露用——无关成功操作不再能静默清零。
        """
        return (self._repo is not None and not self._in_memory_fallback
                and not self._needs_recovery)

    @property
    def configured(self) -> bool:
        """是否配置了持久化后端（repo 非 None；恢复触发 guard，防 repo-None 死循环）。"""
        return self._repo is not None

    @property
    def needs_recovery(self) -> bool:
        """是否有未 reconcile 到 Mongo 的内存记录（/ready 门禁信号）。"""
        return self._repo is not None and self._needs_recovery
```

(3) `store()` dev 降级分支（`except StorageUnavailableError` 内，写内存后）置位：

```python
                self._apply_timestamps(entry)
                self._entries[(conversation_id, client_request_id)] = entry
                self._needs_recovery = True  # BE-P1-07：降级写入未 reconcile
                return StoreResult(entry=entry, acquired=True)
```

(4) `complete()` 写穿失败分支（`except StorageUnavailableError` 内）置位——**仅当内存有 entry 可 reconcile 才置位**（无 entry 的写穿失败无记录可补落，置位会成为假 flag）：

```python
            except StorageUnavailableError:
                # 消息已 ACK：仅丢状态字段，不抛；内存终态超前 Mongo → 需 recover 补落
                logger.warning("IdempotencyStore.complete 写穿失败（key=%s/%s）",
                               conversation_id, client_request_id)
                self._in_memory_fallback = True
                if entry is not None:
                    # 仅内存有 entry（可 reconcile）才置位——无 entry 的写穿失败
                    # 无记录可补落，置位会成假 flag（recover 空转清位）
                    self._needs_recovery = True  # BE-P1-06/07：终态未落 Mongo
```

(5) `reject()` 写穿失败分支同样置位（同样 `if entry is not None` 守卫）：

```python
            except StorageUnavailableError:
                logger.warning("IdempotencyStore.reject 写穿失败（key=%s/%s）",
                               conversation_id, client_request_id)
                self._in_memory_fallback = True
                if entry is not None:
                    self._needs_recovery = True  # BE-P1-06/07：终态未落 Mongo
```

(6) `reset()` 清位（现清 `_entries`/`_repo`/`_product_mode`/`_in_memory_fallback` 后补）：

```python
        self._entries.clear()
        self._repo = None
        self._product_mode = False
        self._in_memory_fallback = True
        self._needs_recovery = False   # BE-P1-07：测试隔离清位（configure 不清，防 S2 复开）
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest core/input/tests/test_idempotency_durable.py -v --asyncio-mode=auto`
Expected: PASS（既有 TestMemoryDefault/TestProductFailClosed/TestDevDegradation/TestDurable/TestWsHandlerProductMode/TestServerInfoExposure 不回归——`reset_store` 已清位，无顺序污染）

- [ ] **Step 5: Commit**

```bash
git add core/input/idempotency.py core/input/tests/test_idempotency_durable.py
git commit -m "feat(idempotency): store 显式 needs_recovery + durable 强化（BE-P1-07 S2）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: store() 短路扩展 + recover() 重写（四分支 + restore 补落）

**Files:**
- Modify: `core/input/idempotency.py`（`store` 短路条件、`recover` 重写、模块级 `_build_full_doc`/`_is_terminal_status`）
- Modify: `core/input/tests/test_idempotency_durable.py`（`_FlipRepo` 加 `restore` 镜像；`TestStoreOverlay::test_recover_reconciles_memory_to_mongo_and_durable` 微调；新增 `TestRecoverRestart` 类）

**Interfaces:**
- Consumes: Task 1 的 `IdempotencyRepository.restore(doc) -> None`；Task 2 的 `_needs_recovery`/`needs_recovery`
- Produces: `IdempotencyStore.recover()` 重写（四分支 reconcile + 末尾清 fallback+needs_recovery）；`_build_full_doc(conversation_id, client_request_id, entry, ttl_seconds)`、`_is_terminal_status(status)`（模块级）；Task 4 协调器经 `_recover_dependency("idempotency")` 调用 recover。

- [ ] **Step 1: 先给 `_FlipRepo`（test_idempotency_durable.py）与 `_FlipIdemRepo`（test_lifecycle_idempotency.py）加 `restore` 镜像**

两者必须在 Task 3 一起加（recover() 重写后 `test_recover_reconciles_memory_to_mongo_and_durable` 与 lifecycle 的 recover 路径都会调 `restore`）——避免中间红态（Task 3 提交后、Task 4 前若跑全量，lifecycle 既有用例 AttributeError）。

在 `test_idempotency_durable.py` 的 `_FlipRepo` 类 `release` 方法后新增：

```python
    async def restore(self, doc: dict) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.restore(doc)
```

在 `test_lifecycle_idempotency.py` 的 `_FlipIdemRepo` 类 `release` 方法后新增（同一镜像）：

```python
    async def restore(self, doc: dict) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.restore(doc)
```

- [ ] **Step 2: 写失败测试**

在 `test_idempotency_durable.py` 的 `TestNeedsRecovery` 类之后新增：

```python
def _ack_fields(entry: IdempotencyEntry) -> dict:
    """从 entry 重建 ACK 重放字段（对齐 _send_ack 的形状；逐字一致断言用）。"""
    return {
        "receivedAt": entry.received_at,
        "clientMessageId": entry.client_message_id,
        "conversationId": entry.conversation_id_wire,
        "error": entry.error,
        "status": entry.status,
    }


class TestRecoverRestart:
    """recover() 完整补落 + 跨 store 重启逐字重放（BE-P1-06 §8.1）。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_recover_claimed_entry_survives_store_restart(self, fresh_store):
        """降级 claim → recover → 重启 store（reset+重配）→ lookup 从 Mongo 命中。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False
        await store.store("default", "crid", _entry("msg_1", "abc"))
        repo.available = True
        await store.recover()
        assert store.durable is True
        # 重启：清内存、同 repo 重配置 → 从 Mongo 读取
        reset_store()
        configure_store(repo, product_mode=False)
        entry = await get_store().lookup("default", "crid")
        assert entry is not None and entry.user_message_id == "msg_1"
        assert entry.status == STATUS_CLAIMED

    @pytest.mark.asyncio
    async def test_recover_accepted_replays_identical_ack_after_restart(self, fresh_store):
        """降级 complete → recover → 重启 → 重放 accepted 完整字段逐字一致。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False
        await store.store("default", "crid", _entry("msg_1", "abc"))
        await store.complete("default", "crid", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        repo.available = True
        await store.recover()
        reset_store()
        configure_store(repo, product_mode=False)
        entry = await get_store().lookup("default", "crid")
        assert entry is not None
        assert _ack_fields(entry) == {
            "receivedAt": "2026-08-12T00:00:00Z",
            "clientMessageId": "cm1",
            "conversationId": "wire-1",
            "error": None,
            "status": STATUS_ACCEPTED,
        }

    @pytest.mark.asyncio
    async def test_recover_rejected_replays_identical_ack_after_restart(self, fresh_store):
        """降级 reject（RATE_LIMITED.retryAfterMs）→ recover → 重启 → 重放逐字一致。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False
        await store.store("default", "crid", _entry("msg_1", "abc"))
        await store.reject(
            "default", "crid", code="RATE_LIMITED", message="限流", retryable=True,
            received_at="2026-08-12T00:00:00Z", client_message_id="cm1",
            conversation_id_wire="wire-1", retry_after_ms=30000)
        repo.available = True
        await store.recover()
        reset_store()
        configure_store(repo, product_mode=False)
        entry = await get_store().lookup("default", "crid")
        assert entry is not None and entry.status == STATUS_REJECTED
        assert entry.error == {"code": "RATE_LIMITED", "message": "限流",
                               "retryable": True, "retryAfterMs": 30000}
        assert entry.received_at == "2026-08-12T00:00:00Z"
        assert entry.client_message_id == "cm1"
        assert entry.conversation_id_wire == "wire-1"

    @pytest.mark.asyncio
    async def test_recover_rejected_storage_unavailable_details(self, fresh_store):
        """rejected STORAGE_UNAVAILABLE.details.idempotency_durable 跨重启重放。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False
        await store.store("default", "crid", _entry("msg_1", "abc"))
        await store.reject(
            "default", "crid", code="STORAGE_UNAVAILABLE", message="幂等存储不可用",
            retryable=True, received_at="2026-08-12T00:00:00Z",
            client_message_id=None, conversation_id_wire=None,
            details={"idempotency_durable": False})
        repo.available = True
        await store.recover()
        reset_store()
        configure_store(repo, product_mode=False)
        entry = await get_store().lookup("default", "crid")
        assert entry.error["code"] == "STORAGE_UNAVAILABLE"
        assert entry.error["details"] == {"idempotency_durable": False}

    @pytest.mark.asyncio
    async def test_recover_upgrades_mongo_claimed_to_memory_accepted(self, fresh_store):
        """S1：Mongo 停留 claimed + 内存 accepted → recover 升级 Mongo 含完整负载，
        重启后重放逐字一致（不再被陈旧 claimed 覆盖）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        # Mongo=claimed（写穿成功）
        await store.store("default", "crid", _entry("msg_1", "abc"))
        # complete 写穿失败 → 内存 accepted、Mongo 仍 claimed
        repo.available = False
        await store.complete("default", "crid", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        repo.available = True
        assert store.needs_recovery is True
        await store.recover()
        # Mongo 已升级为 accepted 含完整负载
        doc = await repo.get("default", "crid")
        assert doc["status"] == STATUS_ACCEPTED
        assert doc["received_at"] == "2026-08-12T00:00:00Z"
        assert doc["client_message_id"] == "cm1"
        assert doc["conversation_id_wire"] == "wire-1"
        # 重启后逐字一致
        reset_store()
        configure_store(repo, product_mode=False)
        entry = await get_store().lookup("default", "crid")
        assert entry.status == STATUS_ACCEPTED
        assert _ack_fields(entry)["receivedAt"] == "2026-08-12T00:00:00Z"

    @pytest.mark.asyncio
    async def test_recover_failure_keeps_durable_false_and_retries(self, fresh_store):
        """recover 中途失败 → durable 不翻、needs_recovery 保持，下一轮可继续补偿。"""
        class _DegradeThenRestoreFailFake(FakeDocumentDBBackend):
            """首 insert 失败（降级写内存）→ 首次 upsert update_one 失败（restore 注入）→ 恢复。"""

            def __init__(self) -> None:
                super().__init__()
                self.insert_fail = 1
                self.upsert_fail = 1

            async def insert_one(self, collection, document):
                if self.insert_fail > 0:
                    self.insert_fail -= 1
                    raise RuntimeError("Mongo down")
                return await super().insert_one(collection, document)

            async def update_one(self, collection, filter_expr, update, upsert=False):
                if upsert and self.upsert_fail > 0:
                    self.upsert_fail -= 1
                    raise RuntimeError("Mongo down")
                return await super().update_one(collection, filter_expr, update, upsert=upsert)

        repo = IdempotencyRepository(db=_DegradeThenRestoreFailFake())
        configure_store(repo, product_mode=False)
        store = get_store()
        result = await store.store("default", "crid", _entry("msg_1", "abc"))
        assert result.acquired is True
        assert store.needs_recovery is True
        # 第一轮 recover：restore upsert 失败 → StorageUnavailableError 冒泡
        with pytest.raises(StorageUnavailableError):
            await store.recover()
        assert store.durable is False
        assert store.needs_recovery is True
        # 第二轮 recover 成功 → 落库、durable 翻回
        await store.recover()
        assert store.durable is True
        assert (await repo.get("default", "crid")) is not None

    @pytest.mark.asyncio
    async def test_short_circuit_covers_needs_recovery_window(self, fresh_store):
        """S2 重复处理窗口：fallback 已被无关操作清零、needs_recovery 仍 True 时，
        同键 store() 仍走内存短路（不重新 insert、不重复 acquired）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False
        await store.store("default", "crid_1", _entry("msg_1", "abc"))  # 降级
        repo.available = True
        await store.store("default", "crid_2", _entry("msg_2", "abc"))  # 无关成功 → fallback 清零
        assert store._in_memory_fallback is False  # noqa: SLF001
        assert store.needs_recovery is True
        # 同键 crid_1 再 store → 短路（needs_recovery 窗口），不触碰 Mongo
        r = await store.store("default", "crid_1", _entry("msg_new", "abc"))
        assert r.acquired is False
        assert r.entry.user_message_id == "msg_1"  # 既有 entry，不新生成
        assert (await repo.get("default", "crid_1")) is None  # 内存 claim 未落 Mongo
```

- [ ] **Step 3: 运行测试验证失败**

Run: `python -m pytest core/input/tests/test_idempotency_durable.py::TestRecoverRestart -v --asyncio-mode=auto`
Expected: FAIL——旧 recover()（Task 2 后状态）三个失败机制：① `test_recover_claimed_entry_survives_store_restart` 断言 `durable is True` 但旧 recover 末尾不清 `_needs_recovery` → durable 仍 False；② `test_recover_accepted/rejected_*` 重放字段缺失（旧 recover 只 claim 最小字段、不落完整负载）；③ `test_recover_upgrades_mongo_claimed_to_memory_accepted` 断言 Mongo 升级为 accepted 但旧 recover 对 Mongo-claimed 走「刷新内存」分支（内存被覆盖回 claimed）。短路窗口用例（`test_short_circuit_covers_needs_recovery_window`）断言 needs_recovery 窗口短路，旧短路只认 fallback → 失败。

- [ ] **Step 4: 实现 store() 短路扩展 + recover() 重写**

在 `core/input/idempotency.py`：

(1) `store()` 短路条件扩展（现 `if self._in_memory_fallback:` 改为）：

```python
        if self._repo is not None:
            # 降级/未 reconcile 窗口：同键有效内存 entry 恒去重短路（不触碰 Mongo）——
            # 关闭「down→up 且 recover() 未跑时 repo.claim 对内存 claim 重新 insert 导致
            # 重复处理」窗口（BE-P1-07）。正常路径（已 reconcile）不走短路，保留
            # repo.claim 的 stale 接管（TAKEOVER）语义。
            if self._in_memory_fallback or self._needs_recovery:
                prev = self._memory_lookup(conversation_id, client_request_id)
                if prev is not None:
                    return StoreResult(entry=prev, acquired=False)
```

(2) `recover()` 重写（替换现有实现）：

```python
    async def recover(self) -> None:
        """Mongo 恢复 reconcile（D5 + BE-P1-06/07）：对每条内存 entry 与 Mongo 对齐。

        内存权威 overlay 逐条对齐，四分支：
        - Mongo 缺失 → restore() 插入完整文档（含终态 ACK 负载）；
        - Mongo claimed + 内存终态（本进程写穿失败的前进状态）→ restore() 升级为终态；
        - Mongo claimed + 内存 claimed → 刷新内存为 Mongo 状态（Mongo 的 message_id
          权威，防多 worker 翻转）；
        - Mongo 已终态 → 刷新内存为 Mongo 状态（Mongo 优先、读时一致）。
        跨重启重放逐字一致（修复 S1：不再用陈旧 claimed 覆盖前进终态）。结束后
        ``_in_memory_fallback = False`` 且 ``_needs_recovery = False``（durable 翻回）。
        任一 get/restore 失败 → 异常冒泡，不翻 durable（下轮健康循环重试）。
        """
        if self._repo is None:
            return  # 纯内存路径无可 reconcile
        for (conversation_id, client_request_id), entry in list(self._entries.items()):
            if self._is_stale(entry):
                # 过期内存记录（Mongo 已 TTL 清除）→ 不复活
                self._entries.pop((conversation_id, client_request_id), None)
                continue
            doc = await self._repo.get(conversation_id, client_request_id)
            mongo_terminal = doc is not None and _is_terminal_status(doc.get("status"))
            memory_terminal = entry.status in (STATUS_ACCEPTED, STATUS_REJECTED)
            if doc is not None and (mongo_terminal or not memory_terminal):
                # Mongo 已终态（另一进程 winner / 已落库）或双方皆 claimed（Mongo 的
                # message_id 权威）→ 刷新内存为 Mongo 状态
                self._entries[(conversation_id, client_request_id)] = IdempotencyEntry.from_doc(doc)
                continue
            # Mongo 缺失，或（Mongo claimed + 内存终态 = 本进程写穿失败的前进状态）→
            # restore 原子 upsert 完整文档（缺失→插入 / claimed→升级终态，含重放负载）
            await self._repo.restore(_build_full_doc(conversation_id, client_request_id,
                                                     entry, self._ttl))
        self._in_memory_fallback = False
        self._needs_recovery = False
```

(3) 模块级辅助（置于 `_mongo_expiry` 附近）：

```python
def _is_terminal_status(status: str | None) -> bool:
    """Mongo status 是否终态（accepted/rejected + legacy completed）。"""
    return status in (STATUS_ACCEPTED, STATUS_REJECTED, "completed")


def _build_full_doc(conversation_id: str, client_request_id: str,
                    entry: IdempotencyEntry, ttl_seconds: float) -> dict:
    """从内存 entry 构建 Mongo 完整文档（claim 字段 + 终态 ACK 负载）。

    client_request_id 是 _entries 的 dict key 部分、不在 entry 内，由调用方传入；
    ttl_seconds 传 store 的 self._ttl（与 claim() 写穿同基，防自定义 TTL 分裂）。
    """
    full = {
        "conversation_id": conversation_id,
        "client_request_id": client_request_id,
        "content_digest": entry.content_digest,
        "message_id": entry.user_message_id,
        "status": entry.status,
        "claimed_at": entry.claimed_at if entry.claimed_at is not None else time.time(),
        "expires_at": _mongo_expiry(ttl_seconds),
    }
    if entry.received_at is not None:
        full["received_at"] = entry.received_at
    if entry.client_message_id is not None:
        full["client_message_id"] = entry.client_message_id
    if entry.conversation_id_wire is not None:
        full["conversation_id_wire"] = entry.conversation_id_wire
    if entry.error is not None:
        full["error"] = entry.error
    return full
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m pytest core/input/tests/test_idempotency_durable.py -v --asyncio-mode=auto`
Expected: PASS（含既有 `test_recover_reconciles_memory_to_mongo_and_durable`——crid_mongo 终态刷新、crid_dev 经 restore 插入；`test_down_up_before_recover_no_dedup_loss`/`test_failover_up_down_up_no_dedup_loss` 短路行为不变）

若既有 `test_recover_reconciles_memory_to_mongo_and_durable` 断言 recover 后 `_in_memory_fallback is False` 通过（recover 末尾仍清 fallback）且新增 `needs_recovery is False` 未断言——无需改。若该用例因 `_FlipRepo.restore` 缺失报 AttributeError，确认 Step 1 已加镜像。

- [ ] **Step 6: Commit**

```bash
git add core/input/idempotency.py core/input/tests/test_idempotency_durable.py
git commit -m "feat(idempotency): store 短路扩展 + recover 四分支完整补落（BE-P1-06/07）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 协调器 `_dependency_needs_recovery` 触发钩子 + lifecycle 测试

**Files:**
- Modify: `core/agent/_lifecycle_persistence.py`（`_db_health_loop` 触发条件 + 新增 `_dependency_needs_recovery`）
- Modify: `core/agent/tests/test_lifecycle_idempotency.py`（`_FlipIdemRepo` 加 `restore` 镜像；新增 `_SplitIdemRepo` fixture；新增 `TestFirstRoundRecovery` 类；同步 `test_offline_start_recovers_after_availability_flip` docstring）

**Interfaces:**
- Consumes: Task 2 的 `configured`/`needs_recovery`/`durable`；Task 3 的 `store.recover()`
- Produces: `_LifecyclePersistence._dependency_needs_recovery(name: str) -> bool`（idempotency 接线：`configured and not durable`）；`_db_health_loop` 首轮直接 healthy 触发 recover。

**注**：`test_lifecycle_idempotency.py` 顶部 import 块（现有 `ClaimResult/IdempotencyRepository/STATUS_ACCEPTED/StorageUnavailableError`）需补 `STATUS_REJECTED`——本任务 `test_first_round_recovers_terminal_states_across_restart` 引用它（`er.status == STATUS_REJECTED`），漏补则 NameError。

- [ ] **Step 1: 新增 `_SplitIdemRepo` fixture**（`_FlipIdemRepo` 的 `restore` 镜像已在 Task 3 加过，本任务不重复）

在 `_FlipIdemRepo` 类之后新增：

```python
class _SplitIdemRepo:
    """is_connected 与 health_check 解耦的 repo fake（BE-P1-07 首轮直接 true 用例）。

    connected: 启动建连状态（configure 的 durable 初值依据——False = 启动离线）。
    ping_ok:   当前 Mongo 可达性（health_check/connect/操作 gate——False = 断网）。
    两者独立翻转：connected=False + ping_ok=True 表达「启动建连失败、首轮探测前 Mongo
    已恢复」——首轮 health_check 直接 true、durable 仍 False（未 recover）。
    """

    def __init__(self, connected: bool, ping_ok: bool) -> None:
        self.connected = connected
        self.ping_ok = ping_ok
        self.connect_calls = 0
        self._backend = FakeDocumentDBBackend()
        self._impl = IdempotencyRepository(db=self._backend)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def health_check(self) -> bool:
        return self.ping_ok

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self.ping_ok:
            self.connected = True
        return self.ping_ok

    async def close(self) -> None:
        pass

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    claimed_at: float | None = None,
                    expires_at: object = None) -> ClaimResult:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.claim(
            conversation_id, client_request_id,
            content_digest=content_digest, message_id=message_id, status=status,
            claimed_at=claimed_at, expires_at=expires_at)

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.get(conversation_id, client_request_id)

    async def restore(self, doc: dict) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.restore(doc)

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       status: str = STATUS_ACCEPTED,
                       payload: dict | None = None) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.complete(conversation_id, client_request_id,
                                  status=status, payload=payload)

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     error: dict, payload: dict | None = None) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.reject(conversation_id, client_request_id,
                                error=error, payload=payload)

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        if not self.ping_ok:
            raise StorageUnavailableError("Mongo down")
        await self._impl.release(conversation_id, client_request_id)
```

- [ ] **Step 2: 写失败测试**

在 `test_lifecycle_idempotency.py` 的 `TestHealthLoopProbeRecovery` 类中新增：

```python
    @pytest.mark.asyncio
    async def test_first_round_healthy_recovers(self, fresh_store):
        """BE-P1-07：离线启动 + 首轮探测前 Mongo 已恢复 → 首轮 health_check 直接 true，
        当轮执行 recover（不因 prev={} 基线跳过），记录落库、durable=true。"""
        repo = _SplitIdemRepo(connected=False, ping_ok=False)  # 启动离线（configure durable=False）
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is False
        # 降级窗口写一条内存记录
        result = await store.store("default", "crid_1", _entry("msg_1", "abc"))
        assert result.acquired is True
        assert store.needs_recovery is True
        # Mongo 在首轮探测前恢复（ping_ok 翻 true，connect 未跑）；同时另一 key 的
        # store() 成功（S2：fallback 清零但 needs_recovery 保持 → durable 仍 False）
        repo.ping_ok = True
        r2 = await store.store("default", "other_key", _entry("msg_2", "abc"))
        assert r2.acquired is True
        assert store._in_memory_fallback is False  # noqa: SLF001
        assert store.needs_recovery is True
        assert store.durable is False  # S2：无关成功操作不静默清零
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)  # 首轮直接 true
        # 当轮已 recover（needs_recovery/durable 触发，非边界）
        assert repo.connect_calls >= 1, "首轮直接 true 应触发 recover"
        doc = await repo.get("default", "crid_1")
        assert doc is not None and doc["message_id"] == "msg_1"
        assert store.durable is True
        assert store.needs_recovery is False

    @pytest.mark.asyncio
    async def test_consecutive_recover_failures_retried(self, fresh_store):
        """报告 §9#3：recover 连续失败后重试——前两轮失败、第三轮成功，每轮都重试。"""
        class _UpsertFailFake(FakeDocumentDBBackend):
            """upsert update_one 前 N 次抛 RuntimeError（restore 失败注入）。"""

            def __init__(self, fail_times: int) -> None:
                super().__init__()
                self.fail_times = fail_times

            async def update_one(self, collection, filter_expr, update, upsert=False):
                if upsert and self.fail_times > 0:
                    self.fail_times -= 1
                    raise RuntimeError("Mongo down")
                return await super().update_one(collection, filter_expr, update, upsert=upsert)

        class _RestoreFailRepo(_FlipIdemRepo):
            """restore 的 upsert 可注入失败次数的 _FlipIdemRepo。"""

            def __init__(self, fail_times: int = 0, available: bool = True) -> None:
                super().__init__(available=available)
                self._backend = _UpsertFailFake(fail_times)
                self._impl = IdempotencyRepository(db=self._backend)

        repo = _RestoreFailRepo(fail_times=2, available=False)  # 启动离线 + restore 前两次失败
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid_1", _entry("msg_1", "abc"))  # 降级
        repo.available = True
        agent = _make_health_agent(repo)
        # 三轮：r1 restore 失败、r2 失败、r3 成功——每轮都重试（不被 prev 污染跳过）
        await _run_health_rounds(agent, n_rounds=3)
        doc = await repo.get("default", "crid_1")
        assert doc is not None and doc["message_id"] == "msg_1"
        assert store.durable is True
        assert repo.connect_calls >= 3, "连续失败每轮都应重试（connect+recover）"

    @pytest.mark.asyncio
    async def test_healthy_startup_no_duplicate_compensation(self, fresh_store):
        """启动即健康且无降级记录 → 不产生破坏性重复补偿（recover no-op、不触发）。"""
        repo = _FlipIdemRepo(available=True)
        configure_store(repo, product_mode=False)
        store = get_store()
        assert store.durable is True  # 健康启动
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)
        assert store.durable is True
        assert repo.connect_calls == 0, "durable=True → 不触发 recover（无破坏性补偿）"

    @pytest.mark.asyncio
    async def test_unconfigured_store_does_not_spin(self, fresh_store):
        """S4：store 未配置（repo=None）→ 健康循环 probe true 也不触发（configured guard）。"""
        reset_store()  # store 未配置
        repo = _FlipIdemRepo(available=True)  # agent 侧 repo 已连接
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=2)
        assert repo.connect_calls == 0, "_dependency_needs_recovery = configured and not durable"

    @pytest.mark.asyncio
    async def test_first_round_recovers_terminal_states_across_restart(self, fresh_store):
        """§8.2#7（P1-06+P1-07 组合）：首轮直接恢复 accepted/rejected → 重启后逐字一致。"""
        repo = _SplitIdemRepo(connected=False, ping_ok=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        # 降级窗口：accepted + rejected 各一条（完整负载）
        await store.store("default", "crid_a", _entry("msg_a", "da"))
        await store.complete("default", "crid_a", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        await store.store("default", "crid_r", _entry("msg_r", "dr"))
        await store.reject("default", "crid_r", code="RATE_LIMITED", message="限流",
                           retryable=True, received_at="2026-08-12T00:00:01Z",
                           client_message_id="cm2", conversation_id_wire="wire-2",
                           retry_after_ms=30000)
        # 首轮探测前 Mongo 已恢复 → 首轮直接 recover
        repo.ping_ok = True
        agent = _make_health_agent(repo)
        await _run_health_rounds(agent, n_rounds=1)
        assert store.durable is True
        assert store.needs_recovery is False
        # 重启：清内存、同 repo 重配 → 从 Mongo 逐字重放
        reset_store()
        configure_store(repo, product_mode=False)
        ea = await get_store().lookup("default", "crid_a")
        er = await get_store().lookup("default", "crid_r")
        assert ea is not None and ea.status == STATUS_ACCEPTED
        assert ea.received_at == "2026-08-12T00:00:00Z"
        assert ea.client_message_id == "cm1" and ea.conversation_id_wire == "wire-1"
        assert er is not None and er.status == STATUS_REJECTED
        assert er.error == {"code": "RATE_LIMITED", "message": "限流",
                            "retryable": True, "retryAfterMs": 30000}
        assert er.received_at == "2026-08-12T00:00:01Z"
```

- [ ] **Step 3: 运行测试验证失败**

Run: `python -m pytest core/agent/tests/test_lifecycle_idempotency.py -v --asyncio-mode=auto`
Expected: FAIL——三个用例：① `test_first_round_healthy_recovers` 首轮 recover 未触发（旧代码 prev={} 无边界、`_dependency_needs_recovery` 不存在 → `connect_calls == 0`）；② `test_consecutive_recover_failures_retried` 旧代码同样不触发 recover（`connect_calls == 0 < 3`）；③ `test_first_round_recovers_terminal_states_across_restart` 首轮不 recover → durable 仍 False（断言失败）。`test_healthy_startup_no_duplicate_compensation`/`test_unconfigured_store_does_not_spin` 旧代码下也红（无 `_dependency_needs_recovery` → `AttributeError`）。

- [ ] **Step 4: 实现协调器钩子**

在 `core/agent/_lifecycle_persistence.py`：

(1) `_db_health_loop` 的恢复循环（现 `if name not in prev or prev[name]: continue`）改为：

```python
                # 恢复触发：False→True 边界（既有）∪ 依赖自报需补偿（BE-P1-07：首轮
                # prev={} 无边界可依，按依赖补偿状态补触发；durable 已强化——无关成功
                # 操作不再静默清零）
                for name, ok in results.items():
                    if not ok:
                        continue
                    edge_recovery = name in prev and not prev[name]
                    if not edge_recovery and not self._dependency_needs_recovery(name):
                        continue
                    if not await self._recover_dependency(name):
                        results[name] = False  # 补偿未完成 → 下轮重试
```

(2) 新增方法（置于 `_recover_dependency` 之后）：

```python
    def _dependency_needs_recovery(self, name: str) -> bool:
        """依赖自报需补偿（BE-P1-07）：probe 为 true 不足以判定「已对齐」。

        首轮 prev={} 时 False→True 边界不可得（离线启动、首轮探测前 Mongo 已恢复——
        health_check 内部重连直接返回 true）。本轮仅 idempotency 接线：configured 且
        durable=False（= 操作级降级 ∨ 未 reconcile 记录）→ 执行 recover；configured
        guard 防 repo-None（纯内存/未配置）时 durable 恒 False 的每轮空转死循环（S4）。
        其余依赖保持边界触发（已知潜在同类项，YAGNI 不推广）。
        """
        if name != "idempotency":
            return False
        from core.input.idempotency import get_store
        return get_store().configured and not get_store().durable
```

(3) 同步既有 `test_offline_start_recovers_after_availability_flip` 的 docstring（首轮语义已过时）：

```python
        """Mongo 离线启动 durable=False；不重启翻转可用 → health loop 检测 False→True 后恢复。

        协调器语义：False→True 边界触发恢复（首轮离线入 prev，二轮翻转可用触发
        reconcile）；BE-P1-07 后首轮直接 healthy 也会经 _dependency_needs_recovery
        （configured and not durable）当轮触发。离线期间 dev 降级 store 的内存 entry
        经 recover() 补落库（Mongo 无 → restore 完整文档）。
        """
```

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m pytest core/agent/tests/test_lifecycle_idempotency.py -v --asyncio-mode=auto`
Expected: PASS（含既有 `test_offline_start_recovers_after_availability_flip`/`test_healthy_disconnect_then_recover`——边界触发不变）

- [ ] **Step 6: Commit**

```bash
git add core/agent/_lifecycle_persistence.py core/agent/tests/test_lifecycle_idempotency.py
git commit -m "feat(lifecycle): _dependency_needs_recovery 首轮直接 healthy 补触发（BE-P1-07）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: /ready product 门禁含补偿状态 + server_info 时序

**Files:**
- Modify: `core/input/server.py`（`_readiness_payload` idempotency 门禁 + docstring）
- Test: `core/input/tests/test_health.py`（新增 `TestReadyIdempotencyNeedsRecovery` 类）

**Interfaces:**
- Consumes: Task 2 的 `IdempotencyStore.needs_recovery`；`get_store()` 惰性 import（capabilities.py:79 先例）
- Produces: product 模式 `/ready` 幂等门禁 = `ping AND not needs_recovery`（补偿完成前 503）；dev 模式不变。

**注意**：`_readiness_payload` 经 `get_store()` 读进程全局单例——既有 `test_ready_product_idempotency_down_503`/`test_ready_dev_idempotency_down_200` 未 reset store（其 `_idem_repo(MagicMock)` 会让 store 残留配置）。本任务给这两个既有用例补 `reset_store()`（test 开头），防未来用例泄漏 `_needs_recovery` 到这两个用例。

- [ ] **Step 1: 写失败测试**

在 `test_health.py` 末尾新增：

```python
@pytest.mark.service
class TestReadyIdempotencyNeedsRecovery:
    """D4：product 模式 /ready 幂等门禁含补偿状态——needs_recovery 时 503，补偿后 200。"""

    @pytest.fixture
    def _clean_store(self):
        from core.input.idempotency import reset_store
        reset_store()
        yield
        reset_store()

    def _server_info_agent(self):
        """build_server_info 可用的最小 agent stub（config/services 齐备，无需真实 Agent）。"""
        class _Cfg:
            class _L:
                static_image = ""
            class _M:
                model = "test-model"
            live2d = _L()
            llm = _M()
            stt = _M()
            tts = _M()

        class _Agent:
            config = _Cfg()
            llm = _Cfg._M()
            stt = None
            stt_enabled = False
            tts = None
            tts_enabled = False

        return _Agent()

    @pytest.mark.asyncio
    async def test_ready_503_until_recover_then_200(self, _clean_store, aiohttp_client):
        from core.input.capabilities import build_server_info
        from core.input.idempotency import configure_store, get_store
        from core.input.tests.test_idempotency_durable import _FlipRepo
        from core.input.tests.test_idempotency_durable import _entry as _make_entry
        from core.input.server import create_app

        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        # 降级窗口写一条记录 → needs_recovery=True
        await store.store("default", "crid", _make_entry("msg_1", "abc"))
        assert store.needs_recovery is True
        repo.available = True  # Mongo 恢复，但 recover 未跑
        assert get_store().durable is False
        # create_app 会以 product_mode=True 重配 store（configure 不清 needs_recovery）
        app = create_app(_make_mock_agent(), serve_static=False,
                         product_mode=True, idempotency_repo=repo)
        client = await aiohttp_client(app)
        # 补偿未完成 → /ready 503（chatReady=false）+ server_info durable=False
        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["chatReady"] is False
        assert data["dependencies"]["idempotency"] is False
        assert build_server_info(self._server_info_agent()).features.idempotency_durable is False
        # 补偿完成 → /ready 200（durable=true，server_info 同步反映）
        await get_store().recover()
        assert get_store().durable is True
        assert build_server_info(self._server_info_agent()).features.idempotency_durable is True
        resp2 = await client.get("/ready")
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["chatReady"] is True
        assert data2["dependencies"]["idempotency"] is True

    @pytest.mark.asyncio
    async def test_ready_dev_mode_unaffected(self, _clean_store, aiohttp_client):
        """dev 模式：needs_recovery 不门禁（内存降级允许），仍 200。"""
        from core.input.idempotency import configure_store, get_store
        from core.input.tests.test_idempotency_durable import _FlipRepo
        from core.input.tests.test_idempotency_durable import _entry as _make_entry
        from core.input.server import create_app

        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        await get_store().store("default", "crid", _make_entry("msg_1", "abc"))
        repo.available = True
        app = create_app(_make_mock_agent(), serve_static=False,
                         product_mode=False, idempotency_repo=repo)
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["chatReady"] is True
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest core/input/tests/test_health.py::TestReadyIdempotencyNeedsRecovery -v --asyncio-mode=auto`
Expected: FAIL——`test_ready_503_until_recover_then_200` 首 GET 返回 200（现门禁只 ping、不看 needs_recovery）

- [ ] **Step 3: 实现 /ready 门禁**

在 `core/input/server.py` 的 `_readiness_payload` 中：

(1) idempotency 探测分支改为：

```python
    # idempotency：repo 为 None（纯内存 dev）→ 无持久依赖可探视为 ok；product 模式
    # 真实 ping 且补偿完成（ping AND not needs_recovery——D4：降级期记录未 reconcile
    # 时 chatReady 保持 false，用 needs_recovery 而非 durable 避免零降级瞬时窗口误伤）。
    idempotency_ok = True
    if idempotency_repo is not None:
        idempotency_ok = await _probe(idempotency_repo.health_check)
        if product_mode:
            from core.input.idempotency import get_store
            idempotency_ok = idempotency_ok and not get_store().needs_recovery
```

(2) `_readiness_payload` docstring 同步（现「idempotency_ok 真实 ping」→ 补「product 模式含补偿状态 needs_recovery」；并注明 `get_store()` 进程全局单例与注入 repo 同实例的耦合由 create_app 单点维持——configure_store + app["idempotency_repo"] 同源）。

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest core/input/tests/test_health.py -v --asyncio-mode=auto`
Expected: PASS（含既有 `test_ready_product_idempotency_down_503`——ping False 仍 503；`test_ready_dev_idempotency_down_200`——dev 不门禁）

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py core/input/tests/test_health.py
git commit -m "feat(server): product /ready 幂等门禁含补偿状态（BE-P1-07 D4）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 文档收口（runbook BE-P2-04 + todo.md BE-RISK-01）

**Files:**
- Modify: `docs/electron-v2-first-integration-runbook.md`（:60/:125 删 server_info schemaHash；:15 措辞确认）
- Modify: `todo.md`（D6c 行补 2026-08-12 缺口 + BE-RISK-01；新增 post-hardening 复核 blockquote）

**Interfaces:** 无代码依赖。

- [ ] **Step 1: 修 runbook `server_info` schemaHash 断言**

在 `docs/electron-v2-first-integration-runbook.md`：

(1) `:60` 行改为：

```markdown
2. WS 首帧 `session{conversationId:"default", ...}`（回放）→ `server_info`（features/version——**无 schemaHash**；schemaHash 仅离线门禁，见 :15）；
```

(2) `:125` 行改为：

```markdown
2. **server_info**：握手快照（版本 + `features`；**不携带 schemaHash**——schemaHash 是 `contract.schema.json` 的离线身份，联调前经 `python -m frontend_contract --check` 验证，不上握手帧）；
```

(3) `:15` schemaHash 行措辞确认（保留「唯一出处」+ 联调前离线门禁，明确「非 server_info wire 字段」）：

```markdown
| **schemaHash** | `7a5348a4767a9fc6` | 机器契约身份标识（`frontend_contract/contract.schema.json`，**非 server_info wire 字段**）；联调前 `python -m frontend_contract --check` + `python scripts/gen_contract_frames.py --check` 必须通过，并记录冻结产物 hash |
```

(4) 若 runbook 含「未来握手协商 schemaHash」留白，追加一句：

```markdown
> 未来若需握手协商 schemaHash，另起跨仓契约变更（不得只改文档或后端私自加字段）。
```

- [ ] **Step 2: 更新 todo.md 诚实口径**

在 `todo.md` 的 2026-08-11 加固批次 blockquote（约 :35 行）后新增 post-hardening blockquote：

```markdown
> **Electron v2 后端加固后复核（2026-08-12，报告见 `docs/review/electron-v2-backend-post-hardening-review-2026-08-12.md`）**：BE-P1-06（recover 只补最小 claim、终态 ACK 负载丢失——重启后 rejected 变 `INTERNAL/未知错误`）+ BE-P1-07（离线启动首轮探测前 Mongo 恢复 → 首轮直接 true 漏补偿）**本批修复**：完整记录 restore 条件 upsert + store 显式 `needs_recovery`/durable 强化 + 协调器首轮补触发 + product `/ready` 门禁含补偿状态；BE-P2-04（runbook `server_info` 不携带 schemaHash）修正。**BE-RISK-01 接受风险保持开放**（accepted ACK 证明消息已入 EventBus、不等于业务工作已持久化——「ACCEPTED 后、loop `register()` 前 kill」窗口；首轮联调只能标记 happy path，生产封口需独立 outbox/可恢复 generation 证据 + 启动恢复重新入队机制）。设计见 [spec](docs/superpowers/specs/2026-08-12-electron-v2-backend-post-hardening-recovery-design.md)。
```

并确认 D6c 行（约 :49 行）不得声称「accepted 后任务绝不丢失」（若已有此措辞删除；补一句「BE-RISK-01 残余窗口未封口」）。

- [ ] **Step 3: 验证文档一致性**

Run: `grep -n "schemaHash" docs/electron-v2-first-integration-runbook.md`
Expected: schemaHash 只出现在 :15（离线门禁口径）与 :5（唯一出处），不再出现在 server_info 断言；新增 blockquote 不冲突。

- [ ] **Step 4: Commit**

```bash
git add docs/electron-v2-first-integration-runbook.md todo.md
git commit -m "docs: runbook server_info schemaHash 修正 + todo BE-RISK-01 诚实口径（BE-P2-04）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 全门禁（报告 §2.1 定向 230 + quick/normal/pyright/ruff/check-types/golden）

**Files:** 无生产改动；仅运行门禁并修复。

**Interfaces:** 依赖 Task 1-6 全部完成。

- [ ] **Step 1: 复跑报告 §2.1 定向 230 项**

Run（Elysia env）:
```bash
conda run -n Elysia python -m pytest \
  core/input/tests/test_idempotency_durable.py \
  core/agent/tests/test_lifecycle_idempotency.py \
  core/input/tests/test_v2_ws.py \
  core/input/tests/test_rate_limit.py \
  core/input/tests/test_health.py \
  core/input/tests/test_session_routes.py \
  frontend_contract/tests/test_task6_ack_union.py -q
```
Expected: PASS（≥230，含新增用例）

- [ ] **Step 2: 生成/漂移门禁**

Run:
```bash
conda run -n Elysia python -m frontend_contract --check
conda run -n Elysia python scripts/gen_contract_frames.py --check
git diff --check
```
Expected: 全部通过（本批不改契约产物，应无漂移）

- [ ] **Step 3: 提交前全门禁**

Run:
```bash
make lint && make pyright && make test-quick
```
Expected: ruff clean、pyright 0、quick 全绿。随后跑 `make test-normal`（提交前 ~5min）确认排除 slow 全绿。

- [ ] **Step 4: 修复发现的任何回归**

若门禁失败，按 `superpowers:systematic-debugging` 定位根因、补测试、修复，重新跑 Step 1-3 直至全绿。记录失败与修复于提交说明。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test(gates): post-hardening recovery 全门禁绿（quick/normal/pyright/ruff/check-types/golden）
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
（若 Step 1-4 无改动，跳过本 commit，仅确认全绿。）
