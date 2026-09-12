"""Consolidator 运行可观测性回归测试（2026-09-12）。

背景：提取到 **0 条**时 `_do_consolidate` 全程零日志——`Consolidation compared: N items`
那行在 `if items:` 里且是 DEBUG。于是 debug 面板上「Buffer 0/6 + 历史空」与
「根本没跑」「跑挂了」完全无法区分。

实测一位用户发了 6 轮陪聊消息（"陪陪你~" / "嘿嘿" / "❤️❤️❤️"），consolidator 确实跑了
（`[006] Consolidation: 开始提取 (12 turns)`），LLM 也**正确地**返回了 `[]`（prompt 明令
禁止从玩笑/安慰/角色单方面陈述提取），但面板上毫无痕迹——合理地被怀疑成故障。

本文件守两件事：
1. **每次运行都留痕**（INFO 日志，无论提取 0 条还是失败）——「跑了没提到」≠「没跑」；
2. **`stats()` 快照能区分四种状态**：没跑 / 在跑 / 跑了提取 0 条 / 跑失败。
"""

from types import SimpleNamespace

import pytest

from core.memory.consolidator import MemoryConsolidator

pytestmark = pytest.mark.service

_ITEM = ('[{"type": "Fact", "content": "用户养了一只叫橘子的猫", "poignancy": 6}]')


class _LLM:
    """可编排的 MemoryLLM 桩：按脚本依次返回，或抛异常。"""

    model = "mock"

    def __init__(self, script):
        # 单个 str / 异常 → 包成单元素列表；**不能** list(str)，否则 "[]" 会被拆成 '[' ']'
        self._script = script if isinstance(script, list) else [script]
        self.calls = 0

    async def think(self, messages, **kw):
        self.calls += 1
        nxt = self._script.pop(0) if self._script else "[]"
        if isinstance(nxt, Exception):
            raise nxt
        return SimpleNamespace(text=nxt)

    async def test(self):
        return {"ok": True, "latency_ms": 0}

    async def close(self):
        pass


class _Mem:
    """最小长期记忆桩——记录 insert 的条目。"""

    def __init__(self):
        self.inserted: list[str] = []

    def get_all(self):
        return []

    async def insert(self, text, **meta):
        self.inserted.append(text)

    async def search(self, *a, **k):
        return []


def _build(script, mem, threshold):
    """构造 consolidator；`script` 是逐次 LLM 返回值（str）或异常。"""
    return MemoryConsolidator(
        llm=_LLM(script), memory=mem,  # type: ignore[arg-type]
        character_name="爱莉希雅", user_name="用户", buffer_threshold=threshold)


async def _run_one(cons, user="我养了一只叫橘子的猫", reply="真好呀♪"):
    return await cons.consolidate(user, reply)


class TestZeroExtractionLeavesATrace:
    """核心回归：提取 0 条也是有效结果，必须留痕。"""

    @pytest.mark.asyncio
    async def test_zero_extraction_logs_info(self, caplog):
        cons = _build("[]", _Mem(), 1)
        with caplog.at_level("INFO"):
            await _run_one(cons)
        msgs = [r.message for r in caplog.records if r.levelno == 20]
        assert any("提取 0 条" in m for m in msgs), \
            f"提取 0 条时没有任何 INFO 痕迹，实收 {msgs}"

    @pytest.mark.asyncio
    async def test_zero_extraction_state_is_distinguishable(self):
        """跑完提取 0 条：last_run_at 有值、running=False、last_error 为空。"""
        cons = _build("[]", _Mem(), 1)
        await _run_one(cons)
        s = cons.stats()
        assert s["last_run_at"] is not None, "跑了却记为「从未运行」"
        assert s["running"] is False
        assert s["last_extracted"] == 0
        assert s["last_inserted"] == 0
        assert s["last_error"] == "", "提取 0 条不是错误，不得记为失败"
        assert s["last_turns"] == 2

    @pytest.mark.asyncio
    async def test_never_run_state_is_distinguishable(self):
        """从未运行：last_run_at 为 None。与「跑了提取 0 条」必须不同。"""
        cons = _build("[]", _Mem(), 6)
        s = cons.stats()
        assert s["last_run_at"] is None
        assert s["last_extracted"] == 0
        assert s["running"] is False

    @pytest.mark.asyncio
    async def test_stats_matches_debug_panel_fields(self):
        """stats() 的键必须覆盖 debug 契约消费的那几个（改名不会静默丢字段）。"""
        cons = _build("[]", _Mem(), 1)
        await _run_one(cons)
        for key in ("buffer_size", "threshold", "history", "running",
                    "last_run_at", "last_turns", "last_extracted",
                    "last_inserted", "last_error"):
            assert key in cons.stats(), f"stats() 缺字段 {key}"


class TestSuccessAndFailureStates:
    @pytest.mark.asyncio
    async def test_success_records_counts(self, caplog):
        cons = _build(_ITEM, _Mem(), 1)
        with caplog.at_level("INFO"):
            items = await _run_one(cons)
        assert len(items) == 1
        s = cons.stats()
        assert s["last_extracted"] == 1
        assert s["last_inserted"] == 1
        assert s["last_error"] == ""
        assert len(s["history"]) == 1
        assert any("提取 1 条" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_extraction_failure_recorded_not_silent(self, caplog):
        """LLM 抛异常 → last_error 有值 + batch 重新入队。"""
        cons = _build(RuntimeError("LLM 挂了"), _Mem(), 1)
        with caplog.at_level("INFO"):
            await _run_one(cons)
        s = cons.stats()
        assert s["last_run_at"] is not None
        assert "提取失败" in s["last_error"]
        assert s["last_extracted"] == 0
        # 失败重试语义不变：batch 回到缓冲
        assert s["buffer_size"] == 1

    @pytest.mark.asyncio
    async def test_unparsable_output_recorded_as_failure(self):
        cons = _build("这不是 JSON", _Mem(), 1)
        await _run_one(cons)
        s = cons.stats()
        assert "提取失败" in s["last_error"]

    @pytest.mark.asyncio
    async def test_running_flag_set_during_extraction(self):
        """提取进行中 running=True——面板才能显示「正在提取…」。"""
        import asyncio

        seen: list[bool] = []
        cons = _build("[]", _Mem(), 1)
        gate = asyncio.Event()

        class _SlowLLM(_LLM):
            async def think(self, messages, **kw):
                seen.append(cons.stats()["running"])
                gate.set()
                await asyncio.sleep(0.05)
                return SimpleNamespace(text="[]")

        cons._llm = _SlowLLM([])  # type: ignore[assignment]
        task = asyncio.create_task(_run_one(cons))
        await gate.wait()
        assert seen == [True], "提取期间 running 不是 True"
        await task
        assert cons.stats()["running"] is False


class TestStatsIsThePublicSurface:
    """debug 层走 stats()，不再穿透 _buffer/_history（私有访问逐点收敛）。"""

    def test_stats_returns_copies_not_internals(self):
        cons = _build("[]", _Mem(), 3)
        s = cons.stats()
        s["history"].append({"type": "Fact", "content": "注入"})
        assert cons.stats()["history"] == [], "stats() 泄露了内部 _history 引用"

    def test_module_has_no_private_consumers_left(self):
        """core/debug 不得再直接读 consolidator 的私有字段。"""
        import inspect
        from core.debug import info as info_mod
        src = inspect.getsource(info_mod)
        for bad in ("cons._buffer", "cons._threshold", "cons._history"):
            assert bad not in src, f"debug 层仍在私有穿透 {bad}"
