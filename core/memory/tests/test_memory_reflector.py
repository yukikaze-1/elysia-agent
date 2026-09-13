"""MemoryReflector 管线测试：数据注入 + 解析 + 执行。"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.long_term import MemoryEntry, LongTermMemory
from core.memory.memory_reflector import MemoryReflector, _get_reflector_prompt
from core.memory.scope import MemoryScope
from core.schema.memory_reflector import ReflectorActions


class FakeLLM:
    """捕获 prompt 并按序返回预设响应的假 LLM。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def think(self, messages, extra_body=None):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self._responses.pop(0))


def _entry(id_: str, text: str, mem_type: str = "Fact") -> MemoryEntry:
    return MemoryEntry(id=id_, text=text, memory_type=mem_type,
                       poignancy=5, timestamp=1720000000.0)


def _reflector(llm, memory=None) -> MemoryReflector:
    return MemoryReflector(
        llm=llm,
        memory=memory if memory is not None else MagicMock(),
        session=MagicMock(),
    )


@pytest.mark.service
class TestPromptInjection:
    """🔴 防回归：j2 占位符丢失会导致 LLM 收不到记忆数据、反思器静默空转。"""

    @pytest.mark.asyncio
    async def test_review_group_prompt_contains_entry_data(self):
        llm = FakeLLM(responses=["{}"])
        r = _reflector(llm)
        entries = [_entry("mem-id-001", "妖梦养了一只叫橘子的猫")]

        await r._review_group(entries)

        assert len(llm.prompts) == 1
        prompt = llm.prompts[0]
        # 序列化条目必须真正出现在发给 LLM 的 prompt 中
        assert "mem-id-001" in prompt
        assert "妖梦养了一只叫橘子的猫" in prompt
        # 占位符必须已被替换掉
        assert "{memories}" not in prompt
        assert "{last_summary}" not in prompt


@pytest.mark.service
class TestParseActions:
    """_parse_actions: dict -> ReflectorActions 转换点。"""

    def test_valid_json(self):
        a = MemoryReflector._parse_actions(
            '{"merges": [], "updates": [{"id": "m1", "new_content": "新内容"}], "deletes": []}'
        )
        assert isinstance(a, ReflectorActions)
        assert a.updates[0].id == "m1"

    def test_fence_wrapped(self):
        a = MemoryReflector._parse_actions('```json\n{"merges": []}\n```')
        assert isinstance(a, ReflectorActions)

    def test_empty_object_valid(self):
        a = MemoryReflector._parse_actions("{}")
        assert a.merges == [] and a.updates == [] and a.deletes == []

    def test_plain_text_raises(self):
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("这不是 JSON")

    def test_non_dict_json_raises(self):
        """合法 JSON 但非 dict（null / 数组）-> 走 ValidationError 路径。"""
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("null")
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("[1, 2]")

    def test_schema_invalid_raises(self):
        """6 merges 超限 -> ValueError。"""
        merge = '{"ids": ["a", "b"], "merged_content": "x"}'
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions(
                '{"merges": [%s]}' % ", ".join([merge] * 6))


@pytest.mark.service
class TestExecuteActions:
    """_execute_actions: 属性访问 + 返回值守卫 + IO 容错。"""

    def _memory_stub(self):
        m = MagicMock()
        m.update_text = AsyncMock(return_value=True)
        m.delete = AsyncMock(return_value=True)
        m.mark_absorbed = AsyncMock()
        return m

    @pytest.mark.asyncio
    async def test_merge_executes(self):
        mem = self._memory_stub()
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2", "m3"], "merged_content": "合并内容",
                        "type": "Fact", "poignancy": 8}],
        })
        merges, updates, deletes = await r._execute_actions(actions)
        assert merges == 1
        mem.update_text.assert_awaited_once_with(
            "m1", "合并内容", poignancy=8, memory_type="Fact")
        assert mem.mark_absorbed.call_count == 2  # m2, m3

    @pytest.mark.asyncio
    async def test_merge_type_none_passthrough(self):
        mem = self._memory_stub()
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并"}],
        })
        await r._execute_actions(actions)
        mem.update_text.assert_awaited_once_with(
            "m1", "合并", poignancy=None, memory_type=None)

    @pytest.mark.asyncio
    async def test_merge_target_missing_no_absorb(self):
        """update_text 返回 False（LLM 幻觉 id）-> 不 mark_absorbed、不计数。"""
        mem = self._memory_stub()
        mem.update_text = AsyncMock(return_value=False)
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["ghost", "m2"], "merged_content": "合并"}],
        })
        merges, _, _ = await r._execute_actions(actions)
        assert merges == 0
        mem.mark_absorbed.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_absorb_exception_partial(self):
        """mark_absorbed 中途抛异常 -> 该 merge 不计数，后续 item 继续。"""
        mem = self._memory_stub()
        mem.mark_absorbed.side_effect = RuntimeError("boom")
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并"}],
            "deletes": [{"id": "m9"}],
        })
        merges, _, deletes = await r._execute_actions(actions)
        assert merges == 0      # 异常中断计数
        assert deletes == 1     # 后续 delete 不受影响

    @pytest.mark.asyncio
    async def test_update_counts_by_return(self):
        mem = self._memory_stub()
        mem.update_text = AsyncMock(side_effect=[True, False])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "a"},
                        {"id": "m2", "new_content": "b"}],
        })
        _, updates, _ = await r._execute_actions(actions)
        assert updates == 1  # False 不计数

    @pytest.mark.asyncio
    async def test_delete_counts_by_return(self):
        mem = self._memory_stub()
        mem.delete = AsyncMock(side_effect=[True, False])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "deletes": [{"id": "m1"}, {"id": "m2"}],
        })
        _, _, deletes = await r._execute_actions(actions)
        assert deletes == 1

    @pytest.mark.asyncio
    async def test_update_exception_isolated(self):
        """update_text 抛异常 -> 该项跳过，其余继续（IO 容错）。"""
        mem = self._memory_stub()
        mem.update_text = AsyncMock(side_effect=[RuntimeError("io"), True])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "a"},
                        {"id": "m2", "new_content": "b"}],
        })
        _, updates, _ = await r._execute_actions(actions)
        assert updates == 1


@pytest.mark.service
class TestDoReflect:
    """_do_reflect: 坏组隔离 + 计数器复位。"""

    @pytest.mark.asyncio
    async def test_bad_group_skipped_others_execute(self):
        mem = MagicMock()
        mem.update_text = AsyncMock(return_value=True)
        mem.delete = AsyncMock(return_value=True)
        mem.mark_absorbed = AsyncMock()
        group_a = [_entry("a1", "内容A")]
        group_b = [_entry("b1", "内容B")]
        mem.get_all.return_value = group_a + group_b
        # 第一组返回非法输出（坏组），第二组返回一条合法 delete
        llm = FakeLLM(responses=[
            "这不是 JSON",
            '{"deletes": [{"id": "b1"}]}',
        ])
        r = _reflector(llm, memory=mem)
        r._round_count = 50  # 触发条件
        r._cluster_entries = AsyncMock(return_value=[group_a, group_b])

        summary = await r._do_reflect()

        assert summary["deletes"] == 1      # 坏组被跳过，好组正常执行
        assert summary["merges"] == 0
        assert r._round_count == 0          # 计数器复位
        mem.delete.assert_awaited_once_with("b1")


@pytest.mark.unit
class TestPromptPlaceholder:
    """B-2 Task 5：MemoryReflector prompt 占位符替换，无 Elysia/妖梦 字面。"""

    def test_reflector_prompt_placeholder_replaced(self):
        """MemoryReflector prompt 的 {character_name}/{user_name} 被替换，无 Elysia/妖梦。"""
        p = _get_reflector_prompt()
        assert "Elysia" not in p and "妖梦" not in p, "MemoryReflector 仍含 Elysia/妖梦"
        assert "{character_name}" in p and "{user_name}" in p

    def test_reflector_prompt_no_elysia_render(self):
        """MemoryReflector 渲染后无 Elysia/妖梦（replace 已传 character_name/user_name）。"""
        p = _get_reflector_prompt().replace(
            "{character_name}", "Cyrene",
        ).replace(
            "{user_name}", "用户",
        ).replace(
            "{memories}", "[]",
        ).replace(
            "{last_summary}", "{}",
        )
        assert "Elysia" not in p and "妖梦" not in p


@pytest.mark.service
@pytest.mark.asyncio
async def test_reflector_accepts_scoped_view_and_none_session():
    """reflector 应接受 ScopedLongTermMemory + session=None（类型放宽为 union / Optional）。"""
    ltm = LongTermMemory(enable_eviction=False)
    view = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    r = MemoryReflector(
        llm=FakeLLM(responses=["{}"]),
        memory=view,
        session=None,  # Task 7 起构造时传 None
    )
    assert r._memory is view
    assert r._session is None



# ══════════════════════════════════════════════════════════════════
# S3-14：逐组失败不得静默，且不得无条件复位计数器
# ══════════════════════════════════════════════════════════════════

class TestGroupFailureIsVisible:
    """每组失败原先只 `logger.debug` 且**无条件** `_reset_counters()` →
    **去重永久静默失效**：日志上看不出异常（"日志说成功但实际全失败"的典型），
    而计数器被复位意味着"已做过去重"，下一轮要等再攒够阈值才重试。"""

    def _reflector(self, *, fail_groups: int, total_groups: int):
        from core.memory.memory_reflector import MemoryReflector

        ref = MemoryReflector.__new__(MemoryReflector)
        ref._last_summary = {}                     # noqa: SLF001
        ref._memory = MagicMock()                  # noqa: SLF001
        ref._memory.get_all = MagicMock(return_value=[MagicMock()])   # noqa: SLF001
        ref._reset_counters = MagicMock()          # noqa: SLF001
        ref._cluster_entries = AsyncMock(          # noqa: SLF001
            return_value=[[MagicMock()] for _ in range(total_groups)])

        state = {"n": 0}

        async def _review(_group):
            state["n"] += 1
            if state["n"] <= fail_groups:
                raise RuntimeError("llm down")
            return []

        ref._review_group = _review                # noqa: SLF001
        ref._execute_actions = AsyncMock(return_value=(0, 0, 0))   # noqa: SLF001
        return ref

    @pytest.mark.asyncio
    async def test_partial_failure_logs_warning_and_resets(self, caplog):
        import logging

        ref = self._reflector(fail_groups=1, total_groups=3)

        with caplog.at_level(logging.WARNING):
            summary = await ref._do_reflect()

        assert summary["failed_groups"] == 1
        assert summary["groups"] == 3
        assert any(r.levelno >= logging.WARNING and "审查失败" in r.getMessage()
                   for r in caplog.records), "失败必须在默认级别可见"
        ref._reset_counters.assert_called_once()      # 有成功的组 → 正常复位  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_all_failed_keeps_counters(self, caplog):
        """**核心**：全组失败**不得**复位计数器——否则下一轮要等很久才重试。"""
        import logging

        ref = self._reflector(fail_groups=3, total_groups=3)

        with caplog.at_level(logging.WARNING):
            summary = await ref._do_reflect()

        assert summary["failed_groups"] == 3
        ref._reset_counters.assert_not_called()       # noqa: SLF001
        assert any("保留触发计数" in r.getMessage() for r in caplog.records), \
            "保留计数这件事本身也要可见"

    @pytest.mark.asyncio
    async def test_no_failure_resets(self):
        """回归网：全部成功 → 正常复位。"""
        ref = self._reflector(fail_groups=0, total_groups=2)
        summary = await ref._do_reflect()
        assert summary["failed_groups"] == 0
        ref._reset_counters.assert_called_once()      # noqa: SLF001
