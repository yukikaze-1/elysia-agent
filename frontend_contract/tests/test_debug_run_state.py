"""Debug 契约字段守卫：`/debug/api` 必须暴露 consolidator 运行状态（2026-09-12）。

背景见 `core/memory/tests/test_consolidator_observability.py`：提取 0 条时面板与
「没跑」「跑挂了」无法区分。修法是把「最近一次运行」暴露到契约里，前端才能显示
「上次运行：12 turns → 提取 0 条」。

本文件守两件事：
1. 契约字段存在且能建模（改名/删字段即红）；
2. **`DebugInfo` → 契约的字段名一一对应**——两者是手写的两份声明，任一侧改名都会
   让字段静默变成默认值（面板显示「从未运行」而实际跑过），这类漂移无运行时错误。
"""

import dataclasses

import pytest

from frontend_contract import debug as contract

pytestmark = pytest.mark.unit

_RUN_FIELDS = (
    "consolidator_running",
    "consolidator_last_run_at",
    "consolidator_last_turns",
    "consolidator_last_extracted",
    "consolidator_last_inserted",
    "consolidator_last_error",
)
_MILVUS_FIELDS = ("lt_count_milvus", "lt_pending_sync_count")


class TestDebugDataExposesRunState:
    def test_fields_present(self):
        fields = contract.DebugData.model_fields
        for name in _RUN_FIELDS + _MILVUS_FIELDS:
            assert name in fields, f"DebugData 缺字段 {name}"

    def test_zero_extraction_is_modelable(self):
        """「跑了、提取 0 条」必须能被契约表达，且与「从未运行」可区分。"""
        ran = contract.DebugData(consolidator_last_run_at=1_700_000_000.0,
                                 consolidator_last_turns=12,
                                 consolidator_last_extracted=0,
                                 consolidator_last_inserted=0)
        never = contract.DebugData()
        assert ran.wire_dict()["consolidator_last_run_at"] == 1_700_000_000.0
        # wire_dict 是 exclude_none=True（base.py）——「从未运行」在线上表现为**字段缺席**，
        # 不是显式 null。前端必须按 `?: number`（缺省）处理，不能按 `number | null`。
        assert "consolidator_last_run_at" not in never.wire_dict()
        assert ran.wire_dict()["consolidator_last_extracted"] == 0

    def test_failure_reason_distinguishable_from_zero_extraction(self):
        """失败与「提取 0 条」靠 last_error 区分——空串 = 成功。"""
        assert contract.DebugData().wire_dict()["consolidator_last_error"] == ""
        failed = contract.DebugData(consolidator_last_error="提取失败: 输出为空")
        assert failed.wire_dict()["consolidator_last_error"].startswith("提取失败")

    def test_lt_count_milvus_defaults_to_unknown(self):
        """默认 -1 = 未取到，而不是 0——0 会被误读成「Milvus 是空的」。"""
        assert contract.DebugData().wire_dict()["lt_count_milvus"] == -1


class TestConsolidationInfoCarriesRunState:
    def test_grouped_props_expose_run_state(self):
        fields = contract.ConsolidationInfo.model_fields
        for name in ("running", "last_run_at", "last_turns",
                     "last_extracted", "last_inserted", "last_error"):
            assert name in fields, f"ConsolidationInfo 缺字段 {name}"


class TestDebugInfoMappingHasNoDrift:
    """`core.debug.info.DebugInfo` 与契约必须同名——两份手写声明的漂移守卫。"""

    def test_every_run_field_exists_on_debug_info(self):
        from core.debug.info import DebugInfo
        names = {f.name for f in dataclasses.fields(DebugInfo)}
        for name in _RUN_FIELDS + _MILVUS_FIELDS:
            assert name in names, f"DebugInfo 缺字段 {name}（契约有、生产侧没有）"

    def test_debug_info_has_no_extra_consolidator_fields(self):
        """反向：DebugInfo 多出来的 consolidator_*/lt_* 字段不会被面板看到（契约会忽略）。"""
        from core.debug.info import DebugInfo
        names = {f.name for f in dataclasses.fields(DebugInfo)}
        contract_names = set(contract.DebugData.model_fields)
        extra = {n for n in names
                 if (n.startswith("consolidator_") or n.startswith("lt_"))
                 and n not in contract_names}
        assert not extra, f"DebugInfo 有契约未声明的字段（面板收不到）：{sorted(extra)}"


class TestHandlerMapping:
    """跑一遍 `/debug/api` handler 的**真实映射**：asdict(DebugInfo) → DebugData → wire_dict。"""

    def test_zero_extraction_reaches_the_wire(self):
        from dataclasses import asdict
        from core.debug.info import DebugInfo
        info = DebugInfo(
            consolidator_buffer_size=0, consolidator_threshold=6,
            consolidator_running=False, consolidator_last_run_at=1_700_000_000.0,
            consolidator_last_turns=12, consolidator_last_extracted=0,
            consolidator_last_inserted=0, consolidator_last_error="",
            lt_count=2, lt_count_milvus=2, lt_pending_sync_count=0,
        )
        wire = contract.DebugData.model_validate(asdict(info)).wire_dict()
        assert wire["consolidator_last_turns"] == 12
        assert wire["consolidator_last_extracted"] == 0     # 「跑了，提取 0 条」
        assert wire["consolidator_last_error"] == ""
        assert wire["lt_count"] == 2 and wire["lt_count_milvus"] == 2

    def test_memory_vs_milvus_mismatch_survives_serialization(self):
        """「内存有条目但 Milvus 没有」必须能传到前端（否则面板无法报警）。"""
        from dataclasses import asdict
        from core.debug.info import DebugInfo
        info = DebugInfo(lt_count=3, lt_count_milvus=0, lt_pending_sync_count=3)
        wire = contract.DebugData.model_validate(asdict(info)).wire_dict()
        assert wire["lt_count"] == 3 and wire["lt_count_milvus"] == 0
        assert wire["lt_pending_sync_count"] == 3
