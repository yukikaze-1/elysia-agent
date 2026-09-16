# 测试文件：core/storage/tests/test_background_task_repo.py
"""#6：`BackgroundTaskResultRepository` 的行为（用内存 fake 文档库，不连真实 Mongo）。

覆盖：upsert 幂等（同 task_id 不产生两条）、`delivered` 语义、`list_pending` 只返回
未投递且按建档时间升序、离线 fail-closed（`StorageUnavailableError`）、索引声明
（task_id unique + TTL）与 `schema_drift` 表一致（后者由 test_schema_drift 用 AST 反向校验）。
"""

from __future__ import annotations

import pytest

from core.schema.types import TaskId
from core.storage.background_task_repo import (
    BACKGROUND_TASK_RESULT_TTL_SECONDS,
    COLLECTION,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    BackgroundTaskResultRepository,
)
from core.storage.base import StorageUnavailableError
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


def _repo() -> BackgroundTaskResultRepository:
    return BackgroundTaskResultRepository(db=FakeDocumentDBBackend())


async def _write(repo: BackgroundTaskResultRepository, task_id: str, *,
                 result: str = "结果", status: str = STATUS_COMPLETED,
                 delivered: bool = False) -> None:
    await repo.upsert_pending(
        task_id=TaskId(task_id),
        result=result,
        original_intent="写季度报告",
        dispatched_at="2026-09-15T00:00:00Z",
        status=status,
        is_error=status != STATUS_COMPLETED,
        delivered=delivered,
    )


class TestUpsertAndQuery:
    @pytest.mark.asyncio
    async def test_upsert_then_list_pending(self):
        repo = _repo()

        await _write(repo, "wf-1")

        pending = await repo.list_pending()
        assert [d["task_id"] for d in pending] == ["wf-1"]
        assert pending[0]["result"] == "结果"
        assert pending[0]["original_intent"] == "写季度报告"
        assert pending[0]["delivered"] is False
        assert await repo.count_pending() == 1

    @pytest.mark.asyncio
    async def test_same_task_id_is_idempotent(self):
        """轮询路径 + 关停排空可能对同一任务各写一次——不得出现两条记录。"""
        repo = _repo()

        await _write(repo, "wf-1", result="第一次")
        await _write(repo, "wf-1", result="第二次")

        pending = await repo.list_pending()
        assert len(pending) == 1
        assert pending[0]["result"] == "第二次"
        assert await repo.count_pending() == 1

    @pytest.mark.asyncio
    async def test_mark_delivered_removes_from_pending(self):
        repo = _repo()
        await _write(repo, "wf-1")

        await repo.mark_delivered(TaskId("wf-1"))

        assert await repo.list_pending() == []
        assert await repo.count_pending() == 0

    @pytest.mark.asyncio
    async def test_delivered_flag_can_be_set_at_write(self):
        """写入期 `delivered=True` 仍是仓储能力（Issue B 后生产调用方一律传 False）。"""
        repo = _repo()

        await _write(repo, "wf-1", delivered=True)

        assert await repo.list_pending() == []
        # 记录仍在（审计痕迹），只是不再"待补投"
        assert len(await repo._require_db().find(COLLECTION, {}, limit=10)) == 1

    @pytest.mark.asyncio
    async def test_list_pending_is_ordered_by_created_at(self):
        """补投顺序 = 建档顺序（用户按发生顺序收到告知）。"""
        repo = _repo()
        backend = repo._require_db()
        for idx, task_id in enumerate(("wf-b", "wf-a", "wf-c")):
            await repo.upsert_pending(
                task_id=TaskId(task_id), result="r", original_intent="i",
                dispatched_at="2026-09-15T00:00:00Z", status=STATUS_COMPLETED,
                is_error=False,
            )
            # 手工打时间戳（内存 fake 无时钟）
            await backend.update_one(
                COLLECTION, {"task_id": task_id},
                {"$set": {"created_at": f"2026-09-15T00:00:0{idx}Z"}})

        pending = await repo.list_pending()

        assert [d["task_id"] for d in pending] == ["wf-b", "wf-a", "wf-c"]

    @pytest.mark.asyncio
    async def test_limit_caps_batch(self):
        repo = _repo()
        for i in range(5):
            await _write(repo, f"wf-{i}")

        assert len(await repo.list_pending(limit=2)) == 2


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_offline_raises_storage_unavailable(self):
        """未连接 → raise（调用方据此降级为"只投递不落库"并记 warning）。"""
        repo = BackgroundTaskResultRepository()   # 未 connect

        with pytest.raises(StorageUnavailableError):
            await _write(repo, "wf-1")
        with pytest.raises(StorageUnavailableError):
            await repo.list_pending()
        with pytest.raises(StorageUnavailableError):
            await repo.mark_delivered(TaskId("wf-1"))

    @pytest.mark.asyncio
    async def test_interrupted_status_round_trips(self):
        repo = _repo()

        await _write(repo, "wf-1", result="任务因服务重启被中断，未产生结果",
                     status=STATUS_INTERRUPTED)

        pending = await repo.list_pending()
        assert pending[0]["status"] == STATUS_INTERRUPTED
        assert pending[0]["is_error"] is True


class TestIndexDeclaration:
    def test_ttl_constant_is_seven_days(self):
        assert BACKGROUND_TASK_RESULT_TTL_SECONDS == 7 * 86400

    @pytest.mark.asyncio
    async def test_connect_declares_task_id_unique_and_ttl(self, monkeypatch):
        """`ensure_index` 声明必须与 schema_drift 的派生表一致（AST 反向校验另有单测）。

        这里断言的是**连上之后库里真有两个索引、且选项对**——`task_id` 唯一（同任务
        重复投递不产生两条）与 `ttl_expires_at` TTL（"再也没人启动"时的兜底回收）。
        注入 db 的路径 `connect()` 会直接返回（不建索引），故走工厂注入 fake。
        """
        backend = FakeDocumentDBBackend()
        monkeypatch.setattr(
            "core.storage.background_task_repo.DocumentDBFactory.create",
            lambda *a, **kw: backend)
        repo = BackgroundTaskResultRepository()

        assert await repo.connect() is True
        specs = {s["name"]: s for s in backend.indexes[COLLECTION]}
        assert specs["task_id_1"]["unique"] is True
        assert specs["task_id_1"]["keys"] == [("task_id", 1)]
        assert specs["ttl_expires_at_1"]["ttl"] == BACKGROUND_TASK_RESULT_TTL_SECONDS
        assert await repo.health_check() is True
        await repo.close()
