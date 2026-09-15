# 测试文件：core/memory/tests/test_consolidator_pending.py
"""#10（决策 #10）：未整合对话缓冲落盘 + 下次启动补提。

**背景**：关机时 `_flush_consolidator` 只给 `flush()` 5 秒，而一次记忆提取要调
LLM——超时取消后 `_do_consolidate` 会把 batch requeue 回缓冲（S2-A1），但进程随即
退出，那最后 <6 轮对话的提取机会永久丢失。决策是**落盘 + 下次启动补提**。

**本文件钉住的不变式**（每条都能注入证伪）：

1. 未达阈值的轮次**当轮即落盘**（不是只在关机时写——SIGKILL 没有关机钩子）；
2. 关机 flush **超时**（被取消）后，requeue 的 batch 在磁盘上（这是 #10 的原缺陷）；
3. 提取失败（LLM 抛错）同样落盘；
4. 提取成功 → journal 清空（不会重复提取出重复记忆）；
5. 新进程构造 consolidator 时读回缓冲，且**下一次对话立刻补提**（不等阈值）；
6. 无 `pending_key`（测试/一次性实例）→ 完全不碰磁盘；
7. 多 instance 共用一个文件、互不串档；损坏文件不阻塞启动。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.memory.consolidator import MemoryConsolidator
from core.memory.consolidator_pending import (
    ConsolidatorPendingStore,
    get_store,
    reset_store,
)
from core.schema.memory import ExtractedMemory

pytestmark = pytest.mark.service

_KEY = "chi_builtin_elysia"


class _FakeLLM:
    """可编程的记忆域 LLM 窄接口替身（记录调用次数 + 可选延迟/异常）。"""

    def __init__(self, text: str = '[{"type": "Fact", "content": "记住我", "poignancy": 5}]',
                 *, delay: float = 0.0, error: Exception | None = None):
        self.text = text
        self.delay = delay
        self.error = error
        self.calls = 0

    async def think(self, messages, extra_body=None):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

        class _R:
            pass

        r = _R()
        r.text = self.text          # type: ignore[attr-defined]
        return r


def _memory() -> MagicMock:
    """长期记忆替身：get_all() 空 → 走 insert-all 路径（不需要向量检索）。"""
    mem = MagicMock()
    mem.get_all = MagicMock(return_value=[])
    mem.insert = MagicMock(return_value=None)
    return mem


async def _async_noop(*args, **kwargs):
    return None


def _make(llm, *, threshold: int = 6, key: str | None = _KEY) -> MemoryConsolidator:
    mem = _memory()
    mem.insert = _async_noop
    return MemoryConsolidator(llm, mem, "TestBot", buffer_threshold=threshold,
                              pending_key=key)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """journal 落到 tmp_path（根 conftest 也做了同样的隔离，这里显式化以免依赖顺序）。"""
    monkeypatch.setenv("ELYSIA_PATH_CONSOLIDATOR_PENDING",
                       str(tmp_path / "consolidator_pending.json"))
    reset_store()
    yield
    reset_store()


def _journal() -> dict:
    path = Path(get_store().path)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class TestJournalWrites:
    @pytest.mark.asyncio
    async def test_below_threshold_persists_immediately(self):
        """未达阈值也要落盘——否则 SIGKILL（无关机钩子）会丢这批轮次。"""
        c = _make(_FakeLLM(), threshold=6)

        await c.consolidate("u1", "a1")

        assert _journal() == {_KEY: [["u1", "a1"]]}

    @pytest.mark.asyncio
    async def test_success_clears_journal(self):
        """提取成功 → 缓冲已消化，journal 必须清空（否则下次启动会重复提取）。"""
        c = _make(_FakeLLM(), threshold=1)

        items = await c.consolidate("u1", "a1")

        assert items, "阈值 1 → 应当提取出条目"
        assert _journal() == {}
        assert not Path(get_store().path).exists()

    @pytest.mark.asyncio
    async def test_extraction_failure_persists(self):
        """LLM 抛错 → requeue → 落盘（不静默丢）。"""
        c = _make(_FakeLLM(error=RuntimeError("llm down")), threshold=1)

        items = await c.consolidate("u1", "a1")

        assert items == []
        assert _journal() == {_KEY: [["u1", "a1"]]}

    @pytest.mark.asyncio
    async def test_shutdown_timeout_requeues_and_persists(self):
        """#10 的原缺陷复现：**关机 flush 超时（被取消）后，那批对话必须在磁盘上**。

        用 `asyncio.wait_for(flush(), 0.05)` 复刻 `_flush_consolidator` 的
        `wait_for(..., timeout=5.0)` 语义（同样的取消路径，不硬等 5 秒）。
        """
        c = _make(_FakeLLM(delay=5.0), threshold=6)
        await c.consolidate("关机前的用户消息", "关机前的回复")
        assert c._buffer, "阈值 6 → 还在缓冲里"          # noqa: SLF001

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c.flush(), timeout=0.05)

        assert c._buffer == [("关机前的用户消息", "关机前的回复")], "取消后 batch 已 requeue"  # noqa: SLF001
        assert _journal() == {_KEY: [["关机前的用户消息", "关机前的回复"]]}, \
            "requeue 之后必须已在磁盘上——这正是 #10 缺失的一步"

    @pytest.mark.asyncio
    async def test_no_key_never_touches_disk(self):
        """pending_key=None（测试/一次性实例）→ 不落盘、不读盘。"""
        c = _make(_FakeLLM(), threshold=6, key=None)

        await c.consolidate("u1", "a1")

        assert _journal() == {}
        assert not Path(get_store().path).exists()


class TestRestoreAndCatchUp:
    @pytest.mark.asyncio
    async def test_new_instance_restores_buffer(self):
        """"重启"：新实例读回上次的缓冲，并标记待补提。"""
        c1 = _make(_FakeLLM(error=RuntimeError("down")), threshold=1)
        await c1.consolidate("u1", "a1")

        c2 = _make(_FakeLLM(), threshold=6)
        assert c2._buffer == [("u1", "a1")]                # noqa: SLF001
        assert c2.has_pending_restore() is True

    @pytest.mark.asyncio
    async def test_catchup_ignores_threshold_on_next_turn(self):
        """补提不看阈值：读回 1 轮 + 用户再说 1 轮 → 立刻提取（不等攒够 6 轮）。

        只读回、不补提的话，那批轮次要等用户再聊够阈值——而他可能再也不聊。
        """
        c1 = _make(_FakeLLM(error=RuntimeError("down")), threshold=1)
        await c1.consolidate("u1", "a1")

        llm = _FakeLLM()
        c2 = _make(llm, threshold=6)
        items = await c2.consolidate("u2", "a2")

        assert llm.calls == 1, "下一次对话必须立刻补提（而非等阈值）"
        assert items
        assert c2._buffer == []                            # noqa: SLF001

    @pytest.mark.asyncio
    async def test_flush_drains_restored_buffer(self):
        """启动步骤走的是 `flush()`：读回的缓冲被提取掉、journal 清空。"""
        c1 = _make(_FakeLLM(error=RuntimeError("down")), threshold=1)
        await c1.consolidate("u1", "a1")

        llm = _FakeLLM()
        c2 = _make(llm, threshold=6)
        assert c2.has_pending_restore() is True

        items = await c2.flush()

        assert items and llm.calls == 1
        assert c2.has_pending_restore() is False
        assert _journal() == {}

    @pytest.mark.asyncio
    async def test_catchup_failure_keeps_journal_for_next_start(self):
        """补提再失败 → 缓冲仍在磁盘上（下次启动继续试，不丢）。"""
        c1 = _make(_FakeLLM(error=RuntimeError("down")), threshold=1)
        await c1.consolidate("u1", "a1")

        c2 = _make(_FakeLLM(error=RuntimeError("still down")), threshold=6)
        assert await c2.flush() == []

        assert _journal() == {_KEY: [["u1", "a1"]]}


class TestStore:
    def test_multi_instance_keys_do_not_collide(self):
        store = get_store()
        store.put("chi_a", [("u", "a")])
        store.put("chi_b", [("x", "y")])

        assert store.get("chi_a") == [("u", "a")]
        assert store.get("chi_b") == [("x", "y")]
        assert sorted(store.keys()) == ["chi_a", "chi_b"]

    def test_empty_put_drops_key_and_file(self):
        store = get_store()
        store.put("chi_a", [("u", "a")])
        store.put("chi_a", [])

        assert store.get("chi_a") == []
        assert not Path(store.path).exists()

    def test_corrupt_journal_is_ignored(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        store = ConsolidatorPendingStore(str(path))

        assert store.keys() == []

    def test_malformed_entries_are_skipped(self, tmp_path):
        path = tmp_path / "partial.json"
        path.write_text(json.dumps({"chi_a": [["u", "a"], ["bad"], "nope"]}),
                        encoding="utf-8")

        store = ConsolidatorPendingStore(str(path))

        assert store.get("chi_a") == [("u", "a")]

    def test_instance_id_is_not_used_as_filename(self):
        """key 不落路径：伪造的 instance_id 不得穿越目录（安全断言）。"""
        store = get_store()
        evil = "../../etc/passwd"
        store.put(evil, [("u", "a")])

        assert Path(store.path).parent == Path(get_store().path).parent
        assert store.get(evil) == [("u", "a")]


class TestShutdownFlushComposition:
    """关机路径的**组合**：`_flush_consolidator` 超时 → requeue → 磁盘上有那批轮次。

    落盘发生在 `_requeue` 内部（不是关机步骤里）——这样"进程被 kill、无关机钩子"
    的情形也覆盖得到。本用例守的是组合：若有人把 requeue 的落盘摘掉、或让 flush
    在取消时静默吞掉 batch，这里立刻红。
    """

    @pytest.mark.asyncio
    async def test_shutdown_flush_timeout_leaves_batch_on_disk(self, monkeypatch):
        from core.agent.lifecycle import AgentLifecycle

        llm = _FakeLLM(delay=30.0)
        c = _make(llm, threshold=6)
        await c.consolidate("关机前的话", "回复")

        rt = MagicMock()
        rt.consolidator = c
        registry = MagicMock()
        registry._runtimes = {"chi_builtin_elysia": rt}     # noqa: SLF001

        class _Agent(AgentLifecycle):
            pass

        agent = _Agent()
        agent._runtime_registry = registry                    # noqa: SLF001

        # 把 `_flush_consolidator` 里的 5s 外层预算压成 0.05s——复刻"超时"分支，
        # 不真等 5 秒（被测的是取消后的落盘，不是那个字面量）。
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(aw, timeout=None):  # type: ignore[no-untyped-def]
            return await real_wait_for(aw, 0.05 if timeout else timeout)

        monkeypatch.setattr("core.agent.lifecycle.asyncio.wait_for", _fast_wait_for)

        await agent._flush_consolidator()                     # noqa: SLF001

        assert _journal() == {_KEY: [["关机前的话", "回复"]]}, \
            "超时分支（最需要落盘的那条）必须把那批轮次留在磁盘上"
