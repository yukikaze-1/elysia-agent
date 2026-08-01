"""TokenLedger 单元测试——write-through 到 TaskState 的 token 账本。"""
import pytest
from Module.workflow_engine.schema import (
    NodeId,
    SessionId,
    TaskId,
    TaskState,
    TaskStatus,
    TokenComponentType,
    TokenComponent,
    TokenUsage,
)
from Module.workflow_engine.token_ledger import TokenLedger

pytestmark = pytest.mark.unit


def _make_task_state(task_id: str = "test-001") -> TaskState:
    return TaskState(
        task_id=TaskId(task_id),
        status=TaskStatus.PENDING,
        session_id=SessionId("sess-1"),
    )


def test_record_appends_entry():
    """record() 后 token_entries 新增一条 TokenComponent。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(
        component=TokenComponentType.EXECUTOR,
        prompt_tokens=100,
        completion_tokens=50,
        model="deepseek-v4",
        node_id=NodeId("search"),
    )
    assert len(ts.token_entries) == 1
    e = ts.token_entries[0]
    assert e.component == TokenComponentType.EXECUTOR
    assert e.prompt_tokens == 100
    assert e.completion_tokens == 50
    assert e.total_tokens == 150
    assert e.model == "deepseek-v4"
    assert e.node_id == NodeId("search")


def test_record_updates_usage_summary():
    """record() 同步更新 token_usage 汇总。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 200, 100, "deepseek-v4")
    ledger.record(TokenComponentType.PLANNER, 50, 30, "deepseek-v4")
    ledger.record(TokenComponentType.EXECUTOR, 80, 20, "openai/gpt-4o")
    assert ts.token_usage["planner"] == 300 + 80  # 200+100 + 50+30
    assert ts.token_usage["executor"] == 100  # 80+20


def test_record_multiple_calls():
    """多次 record → entries 长度正确 + 汇总一致。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    for i in range(5):
        ledger.record(TokenComponentType.EXECUTOR, 10, 5, "m", NodeId(f"n{i}"))
    assert len(ts.token_entries) == 5
    assert ts.token_usage["executor"] == 75  # 5 * 15


def test_to_token_usage():
    """to_token_usage() 返回完整的 TokenUsage 结构。"""
    ts = _make_task_state("task-42")
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 10, 2, "m1")
    usage = ledger.to_token_usage()
    assert isinstance(usage, TokenUsage)
    assert usage.task_id == TaskId("task-42")
    assert len(usage.components) == 1
    assert usage.components[0].total_tokens == 12


def test_resume_from_existing_entries():
    """已有 token_entries 时，TokenLedger 继续追加不破坏历史。"""
    ts = _make_task_state()
    ts.token_entries.append(TokenComponent(
        component=TokenComponentType.PLANNER,
        prompt_tokens=10, completion_tokens=5, total_tokens=15, model="m",
    ))
    ts.token_usage["planner"] = 15
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.EXECUTOR, 20, 10, "m")
    assert len(ts.token_entries) == 2
    assert ts.token_entries[0].total_tokens == 15  # 历史未变
    assert ts.token_entries[1].total_tokens == 30  # 新追加
    assert ts.token_usage["planner"] == 15
    assert ts.token_usage["executor"] == 30


def test_zero_tokens_safe():
    """prompt_tokens=0, completion_tokens=0 不崩溃。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.EVALUATOR, 0, 0, "m")
    e = ts.token_entries[0]
    assert e.total_tokens == 0


def test_total_tokens_property():
    """total_tokens 属性正确求和。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    assert ledger.total_tokens == 0
    ledger.record(TokenComponentType.PLANNER, 10, 5, "m")
    ledger.record(TokenComponentType.EXECUTOR, 20, 10, "m")
    assert ledger.total_tokens == 45


def test_node_id_none():
    """node_id=None 时正常存储（Planner 调用无节点上下文）。"""
    ts = _make_task_state()
    ledger = TokenLedger(ts)
    ledger.record(TokenComponentType.PLANNER, 100, 50, "m", node_id=None)
    assert ts.token_entries[0].node_id is None
