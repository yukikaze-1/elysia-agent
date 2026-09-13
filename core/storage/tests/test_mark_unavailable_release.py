# 测试文件：core/storage/tests/test_mark_unavailable_release.py
"""后端不可用时的资源释放（S2-B5）。

**缺陷原状**：`Repository._mark_unavailable()` 只做 `self._db = None`，**不 close**；
随后 `_health_check` 无条件 `return await self.connect()` 重建客户端。旧客户端失去
唯一引用却从未关闭——

| 后端 | 兜底 | 后果 |
|---|---|---|
| MongoDB | motor/pymongo 有 `__del__` → close | 可回收 |
| **Milvus** | pymilvus 3.0.1 `MilvusClient` 整条 MRO **没有** `__del__` | gRPC channel + 线程池**永不释放** |

Milvus 持续不可达时每轮健康检查（默认 60s）泄漏一个客户端：8 小时宕机 ≈ 480 个孤儿。
`MilvusBackend.close()` 的 docstring 早就写明该风险，只是这条路径没走 close。

`_mark_unavailable` 是 **async** 方法（两个调用点都在协程里）——释放客户端天然异步，
做成同步就只能 `create_task` 火忘，那正是 R6 门禁禁止的裸后台任务。
"""

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _repo():
    """最小 Repository 子类（只测基类的失效语义，不碰真实后端）。"""
    from core.storage.base import Repository

    class _Repo(Repository):
        async def connect(self) -> bool:
            return False

        async def close(self) -> None:
            return None

        async def health_check(self) -> bool:
            return False

    return _Repo()


class _FakeDb:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_mark_unavailable_closes_backend():
    """核心：失效时必须真的 close 后端，而不只是把引用置空。"""
    repo = _repo()
    db = _FakeDb()
    repo._db = db                                   # noqa: SLF001

    await repo._mark_unavailable("ping 失败")              # noqa: SLF001
    await asyncio.sleep(0)                          # 让后台 close 任务跑一轮

    assert repo.backend is None, "失效后不得再暴露旧句柄"
    assert db.closed == 1, "旧客户端必须被 close（否则 Milvus 侧永不释放）"


@pytest.mark.asyncio
async def test_mark_unavailable_is_safe_without_backend():
    """无后端时是 no-op（不抛、不创建任务）。"""
    repo = _repo()
    await repo._mark_unavailable("无事发生")               # noqa: SLF001
    assert repo.backend is None


@pytest.mark.asyncio
async def test_mark_unavailable_tolerates_backend_without_close():
    """后端没有 close（测试替身）时不得抛异常——失效判定优先于清理。"""
    repo = _repo()
    repo._db = object()                             # noqa: SLF001
    await repo._mark_unavailable("no close attr")          # noqa: SLF001
    assert repo.backend is None


@pytest.mark.asyncio
async def test_mark_unavailable_swallows_close_failure():
    """close 自身抛错不得影响"已判定不可用"这一事实。"""
    class _BadDb:
        async def close(self) -> None:
            raise RuntimeError("close 炸了")

    repo = _repo()
    repo._db = _BadDb()                             # noqa: SLF001
    await repo._mark_unavailable("x")                      # noqa: SLF001
    await asyncio.sleep(0)
    assert repo.backend is None
