"""Evaluator 单元测试。

测试双重判定全流程：ExecutionStatus 映射 + BusinessStatus LLM 判定 + action 推导。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from Module.task_system.core.evaluator import Evaluator
from Module.task_system.schema import (
    Artifact,
    ArtifactId,
    BusinessStatus,
    DAGNode,
    EvaluatorAction,
    ExecutionStatus,
    NodeErrorCode,
    NodeId,
    NodeResult,
    OutputContract,
    Plan,
    TaskId,
    TokenComponentType,
)


# ── 测试固件 ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm_client():
    """Mock AsyncOpenAI——返回 goal_achieved 的 LLM 响应。"""
    client = MagicMock()

    async def _respond(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content='{"business_status": "goal_achieved", "reason": "产物完全符合期望"}',
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )

    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_respond)
    return client


@pytest.fixture
def mock_artifact_store():
    """Mock ArtifactStore——返回预设产物。"""
    store = MagicMock()
    store._artifacts: dict = {}
    store.get = AsyncMock(side_effect=lambda aid: store._artifacts.get(aid))
    store.put = AsyncMock()
    return store


@pytest.fixture
def mock_infra(mock_llm_client):
    """Mock AgentInfra。"""
    infra = MagicMock()
    infra.llm = mock_llm_client
    return infra


@pytest.fixture
def token_ledger_mock():
    """Mock TokenLedger。"""
    return MagicMock()


@pytest.fixture
def evaluator(mock_infra, mock_artifact_store, token_ledger_mock):
    """创建被测 Evaluator 实例。"""
    return Evaluator(infra=mock_infra, artifact_store=mock_artifact_store, token_ledger=token_ledger_mock)


@pytest.fixture
def sample_plan():
    """创建示例 Plan。"""
    return Plan(
        plan_version=1,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def sample_node():
    """创建示例 DAGNode（必需节点）。"""
    return DAGNode(
        node_id=NodeId("node_1"),
        description="搜索今天的天气",
        output_contract=OutputContract(
            description="天气信息的 JSON 对象，包含温度和湿度",
        ),
        required=True,
        output_as=ArtifactId("artifact_1"),
    )


@pytest.fixture
def sample_result_success():
    """执行成功的 NodeResult。"""
    return NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[ArtifactId("artifact_1")],
        error=None,
        attempt=1,
        duration_ms=100,
    )


# ── ExecutionStatus 映射测试 ────────────────────────────────────────────────


def test_execution_status_ok():
    """无错误 → ExecutionStatus.OK。"""
    result = NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100)
    status = Evaluator._determine_execution_status(result)
    assert status == ExecutionStatus.OK


def test_execution_status_mappings():
    """所有 NodeErrorCode → ExecutionStatus 映射。"""
    cases = [
        (NodeErrorCode.TOOL_TIMEOUT, ExecutionStatus.TIMEOUT),
        (NodeErrorCode.PERMISSION_DENIED, ExecutionStatus.PERMISSION_DENIED),
        (NodeErrorCode.TOOL_ERROR, ExecutionStatus.ERROR),
        (NodeErrorCode.TOOL_NOT_FOUND, ExecutionStatus.ERROR),
        (NodeErrorCode.NETWORK_ERROR, ExecutionStatus.ERROR),
        (NodeErrorCode.WORKER_CRASH, ExecutionStatus.ERROR),
        (NodeErrorCode.CONTEXT_OVERFLOW, ExecutionStatus.ERROR),
        (NodeErrorCode.TOKEN_BUDGET_EXCEEDED, ExecutionStatus.ERROR),
    ]
    for error_code, expected in cases:
        result = NodeResult(node_id=NodeId("n1"), error=error_code, attempt=1, duration_ms=100)
        status = Evaluator._determine_execution_status(result)
        assert status == expected, f"{error_code} → {status}, 期望 {expected}"


# ── Action 推导测试 ─────────────────────────────────────────────────────────


def test_derive_action_ok_achieved():
    """OK + goal_achieved → CONTINUE。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_ACHIEVED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.CONTINUE


def test_derive_action_ok_partial():
    """OK + goal_partial → CONTINUE。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_PARTIAL,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.CONTINUE


def test_derive_action_ok_failed_with_artifacts():
    """OK + goal_failed + required + 有产物 → REPLAN（垃圾数据不应静默传播）。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100,
                   artifact_refs=[ArtifactId("art_1")]),
    )
    assert action == EvaluatorAction.REPLAN


def test_derive_action_ok_failed_with_artifacts_optional():
    """OK + goal_failed + !required + 有产物 → SKIP（可选节点失败跳过即可）。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=False),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100,
                   artifact_refs=[ArtifactId("art_1")]),
    )
    assert action == EvaluatorAction.SKIP


def test_derive_action_ok_failed_no_artifacts_required():
    """OK + goal_failed + required + 无产物 → REPLAN（工具可能吞了异常）。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.REPLAN


def test_derive_action_ok_failed_no_artifacts_optional():
    """OK + goal_failed + !required + 无产物 → SKIP。"""
    action = Evaluator._derive_action(
        ExecutionStatus.OK, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=False),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.SKIP


def test_derive_action_timeout_required():
    """TIMEOUT + required → RETRY。"""
    action = Evaluator._derive_action(
        ExecutionStatus.TIMEOUT, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.TOOL_TIMEOUT, attempt=1, duration_ms=30000),
    )
    assert action == EvaluatorAction.RETRY


def test_derive_action_timeout_optional():
    """TIMEOUT + !required → SKIP。"""
    action = Evaluator._derive_action(
        ExecutionStatus.TIMEOUT, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=False),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.TOOL_TIMEOUT, attempt=1, duration_ms=30000),
    )
    assert action == EvaluatorAction.SKIP


def test_derive_action_error_required():
    """ERROR + required → RETRY。"""
    action = Evaluator._derive_action(
        ExecutionStatus.ERROR, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.TOOL_ERROR, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.RETRY


def test_derive_action_tool_not_found():
    """TOOL_NOT_FOUND + required → REPLAN。"""
    action = Evaluator._derive_action(
        ExecutionStatus.ERROR, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.TOOL_NOT_FOUND, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.REPLAN


def test_derive_action_permission_denied():
    """PERMISSION_DENIED → ABORT。"""
    action = Evaluator._derive_action(
        ExecutionStatus.PERMISSION_DENIED, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.PERMISSION_DENIED, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.ABORT


def test_derive_action_cancelled():
    """CANCELLED → ABORT。

    注意：error=None 是有意的——CANCELLED 不由 _determine_execution_status 产出，
    而是 Orchestrator 在任务级取消时直接传入，此时 NodeResult 可能尚未设置 error。
    """
    action = Evaluator._derive_action(
        ExecutionStatus.CANCELLED, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=None, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.ABORT


def test_derive_action_tool_not_found_optional():
    """TOOL_NOT_FOUND + !required → SKIP。"""
    action = Evaluator._derive_action(
        ExecutionStatus.ERROR, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=False),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.TOOL_NOT_FOUND, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.SKIP


def test_derive_action_context_overflow_required():
    """CONTEXT_OVERFLOW + required → REPLAN。"""
    action = Evaluator._derive_action(
        ExecutionStatus.ERROR, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=True),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.CONTEXT_OVERFLOW, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.REPLAN


def test_derive_action_context_overflow_optional():
    """CONTEXT_OVERFLOW + !required → SKIP。"""
    action = Evaluator._derive_action(
        ExecutionStatus.ERROR, BusinessStatus.GOAL_FAILED,
        DAGNode(node_id=NodeId("n1"), required=False),
        NodeResult(node_id=NodeId("n1"), error=NodeErrorCode.CONTEXT_OVERFLOW, attempt=1, duration_ms=100),
    )
    assert action == EvaluatorAction.SKIP


# ── 完整 evaluate() 流程测试 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_success_goal_achieved(evaluator, sample_node, sample_result_success, sample_plan):
    """执行成功 + LLM 判定 goal_achieved → CONTINUE。"""
    evaluation = await evaluator.evaluate(sample_result_success, sample_node, sample_plan)

    assert evaluation.execution_status == ExecutionStatus.OK
    assert evaluation.business_status == BusinessStatus.GOAL_ACHIEVED
    assert evaluation.suggested_action == EvaluatorAction.CONTINUE
    assert evaluation.result == sample_result_success


@pytest.mark.asyncio
async def test_evaluate_success_goal_failed(evaluator, sample_node, sample_plan, mock_llm_client):
    """执行成功 + LLM 判定 goal_failed + required + 有产物 → REPLAN。"""
    async def _respond_failed(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content='{"business_status": "goal_failed", "reason": "产物缺少温度数据"}',
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )
    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_respond_failed)

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[ArtifactId("artifact_1")],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    # required + goal_failed → REPLAN（即使有产物，也不静默传播垃圾数据）
    assert evaluation.suggested_action == EvaluatorAction.REPLAN


@pytest.mark.asyncio
async def test_evaluate_execution_error(evaluator, sample_node, sample_plan):
    """执行失败（TOOL_ERROR）→ 直接 goal_failed，不调 LLM。"""
    result = NodeResult(
        node_id=NodeId("node_1"),
        error=NodeErrorCode.TOOL_ERROR,
        attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.execution_status == ExecutionStatus.ERROR
    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    # TOOL_ERROR + required → RETRY
    assert evaluation.suggested_action == EvaluatorAction.RETRY


@pytest.mark.asyncio
async def test_evaluate_llm_error_fallback(evaluator, sample_node, sample_plan, mock_llm_client):
    """LLM 调用异常 → fallback goal_failed。"""
    mock_llm_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("LLM 不可用"))

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[ArtifactId("artifact_1")],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    # 重试 2 次都异常 → 返回 None → fallback
    assert "LLM 返回空响应" in evaluation.reason


@pytest.mark.asyncio
async def test_evaluate_with_artifact_data(evaluator, sample_node, sample_plan, mock_artifact_store):
    """产物数据被正确读取并传给 LLM。"""
    # 预存产物
    mock_artifact_store._artifacts[ArtifactId("artifact_1")] = Artifact(
        artifact_id=ArtifactId("artifact_1"),
        artifact_type="text",
        node_id=NodeId("node_1"),
        task_id=TaskId("task_1"),
        content={"temperature": 25, "humidity": 60},
    )

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[ArtifactId("artifact_1")],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.execution_status == ExecutionStatus.OK
    # ArtifactStore.get 被调用
    mock_artifact_store.get.assert_called_once_with(ArtifactId("artifact_1"))


@pytest.mark.asyncio
async def test_evaluate_no_artifacts(evaluator, sample_node, sample_plan, mock_llm_client):
    """无 artifact_refs → LLM 判定时产物为空，LLM prompt 包含"（无产物数据）"。"""
    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.execution_status == ExecutionStatus.OK
    # LLM mock 返回 goal_achieved，验证流程不崩溃
    assert evaluation.business_status == BusinessStatus.GOAL_ACHIEVED

    # 验证 LLM 收到了包含"（无产物数据）"的 prompt
    call_args = mock_llm_client.chat.completions.create.call_args
    user_msg = call_args[1]["messages"][1]["content"]
    assert "（无产物数据）" in user_msg


@pytest.mark.asyncio
async def test_evaluate_llm_empty_content(evaluator, sample_node, sample_plan, mock_llm_client):
    """LLM 返回空 content → fallback goal_failed。"""
    async def _empty(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(role="assistant", content=""),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )
    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_empty)

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    assert "空响应" in evaluation.reason


@pytest.mark.asyncio
async def test_evaluate_llm_missing_biz_status(evaluator, sample_node, sample_plan, mock_llm_client):
    """LLM 返回 JSON 缺少 business_status 键 → fallback goal_failed。"""
    async def _missing_key(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content='{"reason": "忘了写 business_status 字段"}',
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )
    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_missing_key)

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    assert "缺少 business_status" in evaluation.reason


@pytest.mark.asyncio
async def test_evaluate_llm_unknown_biz_status(evaluator, sample_node, sample_plan, mock_llm_client):
    """LLM 返回未知 business_status 值 → fallback goal_failed。"""
    async def _unknown(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content='{"business_status": "total_disaster", "reason": "完全失败"}',
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )
    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_unknown)

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, sample_node, sample_plan)

    assert evaluation.business_status == BusinessStatus.GOAL_FAILED
    assert "total_disaster" in evaluation.reason


@pytest.mark.asyncio
async def test_evaluate_contract_fallback_to_description(evaluator, sample_plan, mock_llm_client):
    """output_contract.description 为空 → fallback 到 node.description。"""
    node = DAGNode(
        node_id=NodeId("node_2"),
        description="发送邮件通知",
        output_contract=OutputContract(description=""),  # 空
        required=True,
    )
    result = NodeResult(
        node_id=NodeId("node_2"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    evaluation = await evaluator.evaluate(result, node, sample_plan)

    assert evaluation.execution_status == ExecutionStatus.OK

    # 验证 LLM prompt 使用了 node.description 而非空的 output_contract.description
    call_args = mock_llm_client.chat.completions.create.call_args
    user_msg = call_args[1]["messages"][1]["content"]
    assert "发送邮件通知" in user_msg


@pytest.mark.asyncio
async def test_evaluate_json_schema_in_prompt(evaluator, sample_plan, mock_llm_client):
    """output_contract.json_schema 被拼入 LLM prompt。"""
    node = DAGNode(
        node_id=NodeId("node_3"),
        description="返回天气数据",
        output_contract=OutputContract(
            description="温度数据",
            json_schema={"type": "object", "properties": {"temp": {"type": "number"}}},
        ),
        required=True,
    )
    result = NodeResult(
        node_id=NodeId("node_3"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    await evaluator.evaluate(result, node, sample_plan)

    # 验证 LLM prompt 包含 json_schema
    call_args = mock_llm_client.chat.completions.create.call_args
    user_msg = call_args[1]["messages"][1]["content"]
    assert "JSON Schema" in user_msg
    assert '"type": "object"' in user_msg


# ── TokenLedger 集成测试 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_ledger_record_after_llm(evaluator, sample_node, sample_plan,
                                              mock_llm_client, mock_infra, token_ledger_mock):
    """LLM 调用成功后 token_ledger.record() 被调用。"""
    # 构造带 usage 的 CompletionUsage
    usage = CompletionUsage(prompt_tokens=150, completion_tokens=50, total_tokens=200)

    # 设置 llm_model，确保 token record 中 model 字段正确
    mock_infra.llm_model = "deepseek-v4-pro"

    async def _respond_with_usage(**kwargs):
        return ChatCompletion(
            id="eval-test",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content='{"business_status": "goal_achieved", "reason": "产物完全符合期望"}',
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
            usage=usage,
        )

    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_respond_with_usage)

    result = NodeResult(
        node_id=NodeId("node_1"),
        artifact_refs=[],
        error=None, attempt=1, duration_ms=100,
    )
    await evaluator.evaluate(result, sample_node, sample_plan)

    # 验证 record 被调用
    token_ledger_mock.record.assert_called_once()
    call_kwargs = token_ledger_mock.record.call_args[1]
    assert call_kwargs["component"] == TokenComponentType.EVALUATOR
    assert call_kwargs["node_id"] == sample_node.node_id
    assert call_kwargs["prompt_tokens"] == 150
    assert call_kwargs["completion_tokens"] == 50
    assert call_kwargs["model"] == "deepseek-v4-pro"
