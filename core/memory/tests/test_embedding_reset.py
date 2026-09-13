# 测试文件：core/memory/tests/test_embedding_reset.py
"""嵌入后端在关停后必须失效（S3-09）。

**缺陷原状**：`Agent` 同一实例支持 `stop()` → `start()` 热重启，stop 会关掉嵌入后端；
而 `EmbeddingProvider` 仍缓存着那个**已关闭**的 `_backend`，重启后 `embed()` 恒返回
`[]` → 新记忆没有向量、不进 `_pending_syncs` journal、退出即丢，日志只有一句 DEBUG。
归档侧早有对称修复（`reopen_all_archives`），嵌入侧此前没有。

`cancel()`（打断在途加载）与 `reset()`（作废状态）是两件事——本文件锁住后者。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


class _FakeRepo:
    DIMENSION = 4

    async def connect(self) -> bool:
        return False

    async def health_check(self) -> bool:
        return False

    async def close(self) -> None:
        return None


# ═══════════════════════════════════════════════════════════════════
# provider 层
# ═══════════════════════════════════════════════════════════════════

def test_reset_clears_backend_and_cooldown():
    from core.memory.embedding_provider import EmbeddingProvider

    p = EmbeddingProvider()
    p._backend = MagicMock()          # noqa: SLF001  （模拟"已加载的后端"）
    p._failed_at = 123.0              # noqa: SLF001
    assert p.is_ready is True

    p.reset()

    assert p._backend is None, "已关闭的后端引用必须丢掉"      # noqa: SLF001
    assert p._failed_at is None, "失败冷却必须清掉，否则要等冷却才重试"  # noqa: SLF001
    assert p._task is None            # noqa: SLF001
    assert p.is_ready is False


def test_reset_is_idempotent_and_safe_with_pending_task():
    """在途加载任务也要被打断（等同 cancel），且重复调用安全。"""
    from core.memory.embedding_provider import EmbeddingProvider

    p = EmbeddingProvider()
    task = MagicMock()
    task.done.return_value = False
    p._task = task                    # noqa: SLF001

    p.reset()
    p.reset()

    task.cancel.assert_called_once()
    assert p._task is None            # noqa: SLF001


def test_cancel_alone_does_not_clear_backend():
    """反证对照：`cancel()` **不应**清后端——两者语义不同，混用会让"仅取消加载"
    意外丢掉已就绪的后端。"""
    from core.memory.embedding_provider import EmbeddingProvider

    p = EmbeddingProvider()
    p._backend = MagicMock()          # noqa: SLF001
    p.cancel()
    assert p.is_ready is True


# ═══════════════════════════════════════════════════════════════════
# LTM 层：close() 必须走 reset()
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_long_term_close_invalidates_embedding_backend():
    """`LongTermMemory.close()` 之后不得再宣称嵌入就绪。"""
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory(repo=_FakeRepo())
    ltm._embedding_provider._backend = MagicMock()      # noqa: SLF001
    assert ltm.embedding_ready is True

    await ltm.close()

    assert ltm.embedding_ready is False, "关停后仍宣称就绪 = 重启后静默丢记忆"
    assert ltm._embedding_provider._failed_at is None   # noqa: SLF001


@pytest.mark.asyncio
async def test_hot_restart_reloads_instead_of_reusing_closed_backend():
    """热重启语义：close() 之后 `ensure()` 必须**重新加载**而不复用旧引用。

    用 `_try_load_sync` 计数证明"又试了一次"——原先 `_backend` 非 None 会直接短路。
    """
    from core.memory.embedding_provider import EmbeddingProvider
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory(repo=_FakeRepo())
    provider = ltm._embedding_provider                  # noqa: SLF001
    closed = MagicMock()
    provider._backend = closed                          # noqa: SLF001

    attempts: list[int] = []
    provider._try_load_sync = lambda: (attempts.append(1), None)[1]  # noqa: SLF001

    await ltm.close()
    await provider.ensure(wait=True)

    assert attempts, "关停后再次 ensure() 必须真的重新尝试加载"
    assert provider._backend is not closed              # noqa: SLF001


@pytest.mark.asyncio
async def test_close_without_repo_is_safe():
    """回归网：无 repo（纯内存模式）时 close() 不炸。"""
    from core.memory.long_term import LongTermMemory

    ltm = LongTermMemory(repo=None)
    await ltm.close()
    assert ltm.embedding_ready is False
