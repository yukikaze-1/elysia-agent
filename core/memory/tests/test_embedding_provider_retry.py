"""`EmbeddingProvider` 加载生命周期回归测试（2026-09-12）。

守三件事，都是「失败被当成终态 / 预算给错」这一类：

1. **失败不是终态**（评审 CR-10）：旧实现 `self._tried = True` 写在尝试**之前**，
   失败后 `ensure()` 直接返回 None，本进程内再无重试入口——启动时一次超时就永久
   降级成「不写长期记忆」，而下游 `long_term.insert` 在 `embedding == []` 时两个
   分支都不进，**零日志零告警**。
2. **加载预算 ≠ 调用方等待预算**：两者共用一个 `timeout=10s`，而真实冷加载
   （重依赖 import + manifest 全量 sha256 校验 1.3GB + 权重载入）实测 8–20s。
   关键细节：`run_in_executor` 的 future 被 `wait_for` 取消后**线程仍在跑但结果被
   丢弃**——预算给短了不是"少等一下"，是白干。
3. **失败必须留 WARNING**：旧实现两条 provider 失败路径都是 `logger.debug`，只进
   `app-debug.log`；主日志与启动屏幕一个字都没有，唯一信号是 debug 面板一个 ✗。
"""

import asyncio
import logging
import time

import pytest

from core.memory.embedding_provider import (
    DEFAULT_LOAD_TIMEOUT,
    EmbeddingProvider,
)

pytestmark = pytest.mark.unit


class _FakeBackend:
    name = "fake"

    async def embed_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    def dimension(self) -> int:
        return 3


async def _wait_until(cond, *, budget: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class TestRetryAfterFailure:
    """失败可重试，冷却期内不重试。"""

    @pytest.mark.asyncio
    async def test_failure_is_not_permanent(self, monkeypatch):
        """CR-10 回归：第一次失败后，冷却期一过必须还能再试。"""
        attempts = []

        def flaky() -> _FakeBackend:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("第一次失败")
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=1.0, retry_cooldown=0.0)
        monkeypatch.setattr(p, "_try_load_sync", flaky)

        assert await p.ensure(wait=True) is None
        assert p._failed_at is not None

        backend = await p.ensure(wait=True)
        assert backend is not None, "失败被当成终态——重试入口丢失（CR-10）"
        assert len(attempts) == 2
        assert p._failed_at is None

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeated_attempts(self, monkeypatch):
        """冷却期内不重复开加载线程（否则每次 embed 都开一个）。"""
        attempts = []

        def always_fail() -> None:
            attempts.append(1)
            raise RuntimeError("永远失败")

        p = EmbeddingProvider(embed_timeout=1.0, retry_cooldown=60.0)
        monkeypatch.setattr(p, "_try_load_sync", always_fail)

        assert await p.ensure(wait=True) is None
        for _ in range(5):
            assert await p.ensure(wait=True) is None
        assert len(attempts) == 1, f"冷却期内重复尝试 {len(attempts)} 次"

    @pytest.mark.asyncio
    async def test_success_latches_and_never_retries(self, monkeypatch):
        attempts = []

        def ok() -> _FakeBackend:
            attempts.append(1)
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=1.0, retry_cooldown=0.0)
        monkeypatch.setattr(p, "_try_load_sync", ok)

        assert await p.ensure(wait=True) is not None
        for _ in range(5):
            assert await p.ensure(wait=True) is not None
        assert len(attempts) == 1

    @pytest.mark.asyncio
    async def test_inflight_load_not_duplicated(self, monkeypatch):
        """加载进行中再调 ensure() 不得再开一个线程。"""
        attempts = []

        def slow() -> _FakeBackend:
            attempts.append(1)
            time.sleep(0.3)
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=0.05, load_timeout=5.0)
        monkeypatch.setattr(p, "_try_load_sync", slow)

        await p.ensure()               # 起一个
        await p.ensure()               # 应复用，不再起
        await p.ensure()
        await _wait_until(lambda: p._backend is not None, budget=3.0)
        assert len(attempts) == 1


class TestLoadBudgetSeparateFromWaitBudget:
    """加载预算必须容得下真实冷加载；调用方等待预算保持窄。"""

    @pytest.mark.asyncio
    async def test_slow_load_survives_short_embed_timeout(self, monkeypatch):
        """核心回归：加载比 embed_timeout 慢、但比 load_timeout 快 → 必须成功。

        旧实现两者共用一个 10s 预算，取消后线程白跑、结果被丢。
        """
        def slower_than_wait_budget() -> _FakeBackend:
            time.sleep(0.4)
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=0.05, load_timeout=5.0)
        monkeypatch.setattr(p, "_try_load_sync", slower_than_wait_budget)

        assert await p.ensure(wait=False) is None      # 立即返回，加载在后台
        assert await _wait_until(lambda: p._backend is not None, budget=3.0), \
            "加载结果被丢弃——预算给错（线程仍在跑却拿不到值）"
        assert p._failed_at is None, "成功加载却仍被标记失败"

    @pytest.mark.asyncio
    async def test_load_slower_than_load_timeout_fails_loudly(self, monkeypatch, caplog):
        def too_slow() -> _FakeBackend:
            time.sleep(2.0)
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=0.05, load_timeout=0.1, retry_cooldown=60.0)
        monkeypatch.setattr(p, "_try_load_sync", too_slow)

        with caplog.at_level(logging.WARNING):
            await p.ensure()
            assert await _wait_until(lambda: p._failed_at is not None, budget=3.0)
        assert any("加载超时" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_wait_true_does_not_block_for_load_timeout(self, monkeypatch):
        """调用方只等 embed_timeout——不得被 load_timeout 拖住一轮对话。"""
        def very_slow() -> _FakeBackend:
            time.sleep(1.5)
            return _FakeBackend()

        p = EmbeddingProvider(embed_timeout=0.1, load_timeout=30.0)
        monkeypatch.setattr(p, "_try_load_sync", very_slow)

        started = time.monotonic()
        result = await p.ensure(wait=True)
        elapsed = time.monotonic() - started
        assert result is None
        assert elapsed < 1.0, f"wait=True 阻塞了 {elapsed:.1f}s，应为 embed_timeout 量级"

    def test_default_load_budget_covers_measured_cold_load(self):
        """默认值必须显著大于实测冷加载（8–20s），否则回归会重演。"""
        assert DEFAULT_LOAD_TIMEOUT >= 60.0


class TestFailureIsLoud:
    """失败必须落 WARNING——旧实现是 DEBUG，主日志里完全看不见。"""

    @pytest.mark.asyncio
    async def test_load_failure_warns(self, monkeypatch, caplog):
        def fail() -> None:
            raise RuntimeError("模型本地缓存缺失")

        p = EmbeddingProvider(embed_timeout=1.0, retry_cooldown=0.0)
        monkeypatch.setattr(p, "_try_load_sync", fail)

        with caplog.at_level(logging.WARNING):
            await p.ensure(wait=True)
        messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("不会" in m and "Milvus" in m for m in messages), \
            f"数据丢失风险未以 WARNING 说明，实收 {messages}"

    def test_provider_failure_logs_are_warning_level(self):
        """provider 内部两条 fallback 失败路径不得再用 debug。"""
        import inspect

        from core.memory import embedding_provider as mod
        src = inspect.getsource(mod)
        assert 'logger.debug("HuggingFace embedding unavailable' not in src
        assert 'logger.debug("OpenAI embedding unavailable' not in src
        assert 'logger.debug("Embedding failed' not in src

    @pytest.mark.asyncio
    async def test_embed_failure_warns(self, monkeypatch, caplog):
        """单条嵌入失败返回 [] → 下游静默不落库，属丢数据，必须 WARNING。"""

        class _Boom:
            async def embed_query(self, text: str) -> list[float]:
                raise RuntimeError("推理炸了")

        p = EmbeddingProvider()
        p._backend = _Boom()   # type: ignore[assignment]

        with caplog.at_level(logging.WARNING):
            assert await p.embed("测试") == []
        assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_loads_go_through_a_single_worker_executor(monkeypatch):
    """S2-R2-3 残余：加载必须走**自持的单 worker 执行器**，重试不得叠加线程。

    机理：`wait_for` 超时**杀不掉**正在跑的加载线程（本模块顶部注释已记"线程仍在跑但
    结果被丢弃"），而 `run_in_executor(None, …)` 用的是**共享默认池**——冷却结束后的
    重试会**再开一个**加载线程。真加载 1.3GB 模型时，叠加 = 内存成倍增长。

    单 worker 把"最多一个加载线程"变成**构造级**事实：第二次提交只会排队。
    """
    from concurrent.futures import ThreadPoolExecutor

    from core.memory.embedding_provider import EmbeddingProvider

    p = EmbeddingProvider(embed_timeout=0.01, load_timeout=0.05, retry_cooldown=0.0)

    def _stuck() -> None:
        import time as _t
        _t.sleep(1.0)          # 模拟"超时后线程仍在跑"
        return None
    monkeypatch.setattr(p, "_try_load_sync", _stuck)

    await p.ensure()                        # 第一次：等待超时，线程仍在跑
    await asyncio.sleep(0.05)
    await p.ensure()                        # 冷却为 0 → 会再提交一次
    await asyncio.sleep(0.05)

    ex = p._executor()                      # noqa: SLF001
    assert isinstance(ex, ThreadPoolExecutor), f"加载走的不是自持执行器：{ex!r}"
    assert ex._max_workers == 1, "不是单 worker → 重试会叠加加载线程"  # noqa: SLF001
    assert p._executor() is ex, "每次 ensure 都新建了执行器（等于没约束）"  # noqa: SLF001

    # 只看**本 provider 的执行器**开了几个线程——用 `threading.enumerate()` 会把别的
    # 用例遗留的加载线程一起数进来（第一版就因此变成顺序相关：单跑绿、整文件红）。
    assert len(ex._threads) == 1, f"该执行器开了不止一个线程：{ex._threads}"

    p.cancel()
