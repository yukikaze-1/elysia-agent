"""debug 契约建模测试：DebugData 顶层字段 + 嵌套子类型可序列化（marker=unit）。

对照 `frontend/src/debug/types.ts` 的 17 接口 + `debug_routes/` 实际响应：
- DebugData 顶层字段（OverviewTab 消费）按 plan Step 1 原样锁定
- TaskData 等嵌套子类型可独立构造 + wire_dict（TasksTab 拓扑）
"""
import pytest

from frontend_contract import debug as d


@pytest.mark.unit
def test_debug_data_key_fields():
    """DebugData 顶层字段存在（对照 frontend/src/debug/types.ts 的 OverviewTab 消费）。"""
    data = d.DebugData(
        fsm_state="idle", run_state="RUNNING", mood="平静", mood_intensity=0.5,
        llm_model="deepseek/deepseek-v4-flash", stt_ok=True,
        session_id="test", last_interaction_ago=0.0,
    )
    w = data.wire_dict()
    assert w["fsm_state"] == "idle"
    assert w["stt_ok"] is True


@pytest.mark.unit
def test_debug_nested_types_topo():
    """嵌套子类型（如 TasksTab 的 TaskData）可独立构造 + 序列化。"""
    task = d.TaskData(task_id="t1", status="active", title="任务")
    assert task.wire_dict()["status"] == "active"
