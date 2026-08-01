import pytest

pytestmark = pytest.mark.unit

from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    HITLData,
    RBACLevel,
    SessionId,
    WorkflowId,
    WorkflowStatus,
    WorkflowConstraints,
)


def test_delegation_result_tri_state():
    # 三态可构造
    assert DelegationStatus.OK.value == "ok"
    assert DelegationStatus.NO_MATCHING_TOOLS.value == "no_matching_tools"
    assert DelegationStatus.TOO_VAGUE.value == "too_vague"


def test_workflow_id_newtype():
    # NewType 是 str 的运行时别名
    assert WorkflowId("wf-1") == "wf-1"
    assert SessionId("s-1") == "s-1"


def test_delegation_context_roundtrip():
    ctx = DelegationContext(
        intent="查天气",
        tools=[{"name": "web_search", "description": "", "capabilities": []}],
        task_constraints=WorkflowConstraints(),
        session_id=SessionId("s-1"),
    )
    # pydantic 校验 + 序列化往返
    again = DelegationContext.model_validate_json(ctx.model_dump_json())
    assert again.intent == "查天气"
    assert again.session_id == "s-1"
    # tools 字段经契约层从 Module.tool.tool_descriptor.Tool 解析
    assert again.tools[0].name == "web_search"


def test_hitl_data_default_rbac_level():
    # HITLData 引用契约层内解析的 RBACLevel / AgentId / NodeId 等
    data = HITLData(
        task_id=WorkflowId("wf-1"),
        approval_id="ap-1",
        agent_id="agent-1",
        node_id="n-1",
        node_task="发送邮件",
        tool_name="send_email",
    )
    assert data.rbac_level == RBACLevel.MUTATION
    again = HITLData.model_validate_json(data.model_dump_json())
    assert again.rbac_level == RBACLevel.MUTATION


def test_agent_infra_constructible():
    # AgentInfra dataclass：与 ConstraintConfig 同层可解析，无 core/引擎引用
    from unittest.mock import MagicMock

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=object(),
        constraints=ConstraintConfig(),
    )
    assert infra.constraints.max_dag_nodes == 20


def test_constraint_resolve_clamps():
    # WorkflowConstraints.resolve 纯函数逻辑在契约层保持
    resolved = WorkflowConstraints(max_dag_nodes=999).resolve(
        ConstraintConfig(max_dag_nodes=20)
    )
    assert resolved.max_dag_nodes == 20
