# core/cognitive/tests/test_generation_lease.py
"""BE-STAGEH-01：CognitiveLoop.process 持有/释放 runtime 的 generation lease。

真实联调复现：generation 在途时 reset-evolution 未被拒绝。lease 的持有范围必须覆盖
整个回合（含回合边界的状态收尾），并在异常/取消路径由 finally 释放。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.output import OutputManager
from core.adapter.llm_adapter import LLMAdapter
from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.character.registry import CharacterRuntimeRegistry
from core.character.revision import CharacterRevision
from core.character.runtime import CharacterRuntime
from core.cognitive import CognitiveLoop
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.memory.session import SessionMemory
from core.monitoring.event_bus import EventBus
from core.profile.user_identity import UserIdentity
from core.status.agent_fsm import AgentFSM

pytestmark = pytest.mark.service


def _rt() -> CharacterRuntime:
    rt = CharacterRuntime(
        instance=CharacterInstance(
            instanceId=DEFAULT_INSTANCE_ID, characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(
            characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    from core.cognitive.tests.conftest import mock_engine_with_state
    rt.ensure_engine(
        lambda: mock_engine_with_state(instance_id=DEFAULT_INSTANCE_ID))
    return rt


def _make_loop(rt: CharacterRuntime) -> CognitiveLoop:
    reg = CharacterRuntimeRegistry()
    reg.get(DEFAULT_INSTANCE_ID, lambda: rt)
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(
        return_value=[{"role": "user", "content": "hi"}])
    default = SessionMemory(session_id="default")

    async def resolver(conversation_id, protocol_version):
        return default

    return CognitiveLoop(
        llm=llm, fsm=AgentFSM(), event_bus=EventBus(), output=OutputManager(),
        prompt=prompt, session_resolver=resolver, runtime_registry=reg,
    )


@pytest.mark.asyncio
async def test_process_holds_generation_lease_until_turn_finishes():
    rt = _rt()
    loop = _make_loop(rt)
    seen: list[bool] = []

    async def fake_think(*args, **kwargs):
        seen.append(rt.is_generation_busy)

    loop._think_loop = fake_think  # type: ignore[method-assign]
    await loop.process("hello")

    assert seen == [True]
    assert rt.is_generation_busy is False
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_releases_lease_after_turn_exception():
    rt = _rt()
    loop = _make_loop(rt)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    loop._think_loop = boom  # type: ignore[method-assign]
    loop._handle_turn_exception = AsyncMock()  # type: ignore[method-assign]
    await loop.process("hello")

    assert rt.is_generation_busy is False
    loop._handle_turn_exception.assert_awaited_once()
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_releases_lease_after_cancel():
    import asyncio

    rt = _rt()
    loop = _make_loop(rt)
    started = asyncio.Event()

    async def blocking_think(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    loop._think_loop = blocking_think  # type: ignore[method-assign]
    task = asyncio.create_task(loop.process("hello"))
    await started.wait()
    assert rt.is_generation_busy is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # process() 内部吞掉 CancelledError 走 _handle_interrupt，两种结果都可接受

    assert rt.is_generation_busy is False
    await loop._output.close()


# ══════════════════════════════════════════════════════════════════
# S3-15：去重必须跑在 tail lease **之外**
# ══════════════════════════════════════════════════════════════════

class TestDedupRunsOutsideTailLease:
    """tail lease 计入 `runtime.is_generation_busy`，而 `CharacterService._runtime_is_busy()`
    把 generation 在途判为 busy → `reset_evolution` 与角色 revision 变更会 **409
    CHARACTER_BUSY**。

    MemoryReflector 去重（KMeans 全量 + 逐组串行 LLM）**数十秒到分钟级**，原先内联在
    lease 内的 `_after_response` 里 await → 这整段时间里"改角色/回滚/重置演化"全部
    是**可重试的失败**（用户看到"操作失败请重试"）。

    移出去是安全的：去重只动 LTM（Milvus），不碰 session/runtime 状态——lease 保护的
    正是后者。
    """

    @pytest.mark.asyncio
    async def test_dedup_is_deferred_not_awaited_in_after_response(self):
        """`_after_response` 必须**返回**去重调用，而不是就地 await 它。"""
        from unittest.mock import AsyncMock, MagicMock

        from core.cognitive.loop import CognitiveLoop

        loop = CognitiveLoop.__new__(CognitiveLoop)
        loop._shutting_down = False          # noqa: SLF001
        loop._session = MagicMock()          # noqa: SLF001
        loop._session.save = AsyncMock()     # noqa: SLF001
        loop._engine = None                  # noqa: SLF001
        loop._consolidator = None            # noqa: SLF001
        # #20b：收尾域改由 AsyncTaskScope 持有（`_pending_after_response` 现在是只读视图，
        # 写它是 AttributeError）。本用例走 `__new__` 跳过 __init__，故显式装配。
        from Module._common.task_scope import AsyncTaskScope
        loop._after_response_scope = AsyncTaskScope("test.after_response")  # noqa: SLF001
        loop._memory_reflector = None        # noqa: SLF001

        reflector = MagicMock()
        reflector.increment_round = MagicMock()
        reflector.increment_new_memories = MagicMock()
        reflector.check_and_reflect = AsyncMock(return_value=None)

        _task, dedup = await loop._after_response(             # noqa: SLF001
            "u", "a", memory_reflector=reflector)

        # 计数器当场更新（廉价），但**去重没被调用**
        reflector.increment_round.assert_called_once()
        reflector.check_and_reflect.assert_not_awaited()
        assert dedup is not None, "去重调用必须交给调用方（否则又回到 lease 内执行）"

        # 调用返回的去重调用才真正执行
        await dedup()
        reflector.check_and_reflect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finalize_releases_lease_before_dedup(self):
        """**顺序即语义**：`end_generation()` 必须早于去重调用。

        用 AST 断言而不是跑整条链路：这里要钉的是**语句顺序**，跑起来反而看不清。
        """
        import ast
        import pathlib

        src = pathlib.Path("core/cognitive/finalize.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_after_response")

        end_line = None
        dedup_line = None
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "end_generation":
                end_line = node.lineno
            # `await dedup()`——被调用者是 Name，不是 Attribute
            if isinstance(f, ast.Name) and f.id == "dedup":
                dedup_line = node.lineno

        assert end_line is not None, "找不到 end_generation()"
        assert dedup_line is not None, "找不到去重调用（await dedup()）"
        assert end_line < dedup_line, (
            "end_generation() 必须在 await dedup() **之前**——"
            "顺序反了就等于去重仍在 lease 内，409 会回来")

    def test_runtime_doc_records_the_exclusion(self):
        """`begin_generation_tail` 的 docstring 不得再说 tail 覆盖 MemoryReflector。"""
        import pathlib

        src = pathlib.Path("core/character/runtime.py").read_text(encoding="utf-8")
        idx = src.index("def begin_generation_tail")
        doc = src[idx:idx + 1200]
        assert "不含 MemoryReflector 去重" in doc, (
            "tail lease 的文档必须写明它**不**覆盖去重，否则下一个人会把去重挪回去")
