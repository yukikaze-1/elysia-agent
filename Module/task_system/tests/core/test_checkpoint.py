"""CheckpointManager 单元测试。

覆盖 save / load / delete 完整生命周期：
    - 正常存取
    - 覆盖写
    - 不存在时 load 返回 None
    - delete 后 load 返回 None
    - 多任务隔离
    - 对象恒等（v1 内存，无序列化往返）
"""

from __future__ import annotations

import pytest

from Module.task_system.checkpoint import CheckpointManager
from Module.task_system.schema import (
    ExecutionState,
    TaskCheckpoint,
    TaskId,
    TaskState,
    TaskStatus,
)


# ── 测试固件 ────────────────────────────────────────────────────────────────


def _make_checkpoint(task_id: str = "t1") -> TaskCheckpoint:
    """构造最小合法 TaskCheckpoint。"""
    ts = TaskState(
        task_id=TaskId(task_id),
        status=TaskStatus.RUNNING,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )
    es = ExecutionState(
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
    )
    return TaskCheckpoint(task=ts, exec=es)


# ── save + load ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_and_load():
    """save 后 load 返回相同数据。"""
    mgr = CheckpointManager()
    cp = _make_checkpoint("t1")

    await mgr.save(TaskId("t1"), cp)
    loaded = await mgr.load(TaskId("t1"))

    assert loaded is not None
    assert loaded.task.task_id == TaskId("t1")
    assert loaded.task.status == TaskStatus.RUNNING
    assert loaded.exec.created_at == "2026-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_load_not_found():
    """不存在的 task_id → 返回 None。"""
    mgr = CheckpointManager()
    loaded = await mgr.load(TaskId("nonexistent"))
    assert loaded is None


@pytest.mark.asyncio
async def test_save_overwrites():
    """同一 task_id 再次 save → 覆盖旧数据。"""
    mgr = CheckpointManager()
    cp1 = _make_checkpoint("t1")
    cp1.task.status = TaskStatus.RUNNING

    cp2 = _make_checkpoint("t1")
    cp2.task.status = TaskStatus.COMPLETED

    await mgr.save(TaskId("t1"), cp1)
    await mgr.save(TaskId("t1"), cp2)

    loaded = await mgr.load(TaskId("t1"))
    assert loaded is not None
    assert loaded.task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_object_identity():
    """v1 内存存储——load 返回同一对象实例（无序列化往返）。"""
    mgr = CheckpointManager()
    cp = _make_checkpoint("t1")
    await mgr.save(TaskId("t1"), cp)

    loaded = await mgr.load(TaskId("t1"))
    assert loaded is cp  # 同一对象引用


# ── delete ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_existing():
    """delete 后 load 返回 None。"""
    mgr = CheckpointManager()
    cp = _make_checkpoint("t1")
    await mgr.save(TaskId("t1"), cp)

    await mgr.delete(TaskId("t1"))
    loaded = await mgr.load(TaskId("t1"))
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_nonexistent():
    """delete 不存在的 task_id → 无异常。"""
    mgr = CheckpointManager()
    await mgr.delete(TaskId("nonexistent"))  # 不抛异常


@pytest.mark.asyncio
async def test_delete_idempotent():
    """同一 task_id 删除两次——第二次无异常。"""
    mgr = CheckpointManager()
    cp = _make_checkpoint("t1")
    await mgr.save(TaskId("t1"), cp)

    await mgr.delete(TaskId("t1"))
    await mgr.delete(TaskId("t1"))  # 幂等，无异常

    loaded = await mgr.load(TaskId("t1"))
    assert loaded is None


# ── 多任务隔离 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_tasks_isolation():
    """不同 task_id 的 checkpoint 互不干扰。"""
    mgr = CheckpointManager()
    cp1 = _make_checkpoint("t1")
    cp2 = _make_checkpoint("t2")

    await mgr.save(TaskId("t1"), cp1)
    await mgr.save(TaskId("t2"), cp2)

    loaded1 = await mgr.load(TaskId("t1"))
    loaded2 = await mgr.load(TaskId("t2"))

    assert loaded1 is not None
    assert loaded2 is not None
    assert loaded1.task.task_id == TaskId("t1")
    assert loaded2.task.task_id == TaskId("t2")
    assert loaded1 is not loaded2  # 不同对象


@pytest.mark.asyncio
async def test_delete_only_target():
    """delete t1 不影响 t2。"""
    mgr = CheckpointManager()
    await mgr.save(TaskId("t1"), _make_checkpoint("t1"))
    await mgr.save(TaskId("t2"), _make_checkpoint("t2"))

    await mgr.delete(TaskId("t1"))

    assert await mgr.load(TaskId("t1")) is None
    assert await mgr.load(TaskId("t2")) is not None
