"""Planner / Replanner 共享的解析函数和 LLM 调用。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.workflow_engine.utils import strip_markdown_fence

from Module.workflow_contract import NodeId
from Module.workflow_engine.schema import (
    ArtifactId,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    ExecutionMode,
    OutputContract,
    Plan,
    TokenComponentType,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_contract import AgentInfra
    from Module.workflow_engine.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class PlanParseError(Exception):
    """LLM 输出的 JSON 无法解析为 Plan。"""


async def call_llm(
    infra: AgentInfra,
    token_ledger: TokenLedger | None,
    system: str,
    user: str,
) -> str:
    """调用 LLM，推理模式 + JSON Output。

    Args:
        infra: Agent 基础设施，提供 LLM 模型引用。
        token_ledger: Token 账本，用于记录 token 消耗；可为 None。
        system: 系统提示词。
        user: 用户提示词。

    Returns:
        LLM 返回的文本内容。

    Raises:
        PlanParseError: LLM 返回空响应。
    """
    model = infra.llm_model
    response = await infra.llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
        max_tokens=8192,
    )
    # ── token 追踪 ──
    if token_ledger and (usage := response.usage):
        try:
            token_ledger.record(
                component=TokenComponentType.PLANNER,
                prompt_tokens=usage.prompt_tokens or 0,
                completion_tokens=usage.completion_tokens or 0,
                model=model,
            )
        except Exception:
            logger.warning("TokenLedger.record 失败，跳过", exc_info=True)
    content = response.choices[0].message.content
    if not content:
        raise PlanParseError("LLM 返回空响应")
    return content


def parse_plan_from_dict(data: dict) -> Plan:
    """从 dict 解析 Plan（nodes + edges）。

    Args:
        data: 包含 nodes 和 edges 字段的字典。

    Returns:
        解析后的 Plan 对象。

    Raises:
        PlanParseError: nodes 为空或不是数组，edges 不是数组，或节点字段缺失/非法。
    """
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanParseError("nodes 为空或不是数组")

    nodes: list[DAGNode] = []
    for i, rn in enumerate(raw_nodes):
        if not isinstance(rn, dict):
            raise PlanParseError(f"nodes[{i}] 不是 object")
        nid = rn.get("node_id", "")
        if not nid or not isinstance(nid, str):
            raise PlanParseError(
                f"nodes[{i}].node_id 为空或不是字符串: {nid!r}"
            )

        oc_data = rn.get("output_contract", {}) or {}
        nodes.append(DAGNode(
            node_id=NodeId(nid),
            description=rn.get("description", ""),
            output_contract=OutputContract(
                description=oc_data.get("description", ""),
            ),
            required=rn.get("required", True),
            execution_mode=parse_execution_mode(rn.get("execution_mode")),
            input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
            output_as=ArtifactId(str(raw_as)) if (raw_as := (rn.get("output_as") or "").strip()) else None,
        ))

    raw_edges = data.get("edges", [])
    if not isinstance(raw_edges, list):
        raise PlanParseError("edges 不是数组")

    edges: list[tuple[NodeId, NodeId]] = []
    for i, e in enumerate(raw_edges):
        if not isinstance(e, list) or len(e) != 2:
            raise PlanParseError(f"edges[{i}] 不是二元组 [from, to]")
        edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

    now = now_iso()
    return Plan(
        dag=DAG(nodes=nodes, edges=edges),
        plan_version=1,
        created_at=now,
        updated_at=now,
    )


def parse_patch_from_dict(data: dict) -> DAGPatch:
    """从 dict 解析 DAGPatch（局部修复）。

    Args:
        data: 包含 patch_type、trigger_nodes、replace_nodes、add_nodes、remove_nodes、
            remove_edges、add_edges 等字段的字典。

    Returns:
        解析后的 DAGPatch 对象。

    Raises:
        PlanParseError: patch_type 未知，或必要字段格式非法。
    """
    patch_type_str = data.get("patch_type", "replace_nodes")
    try:
        patch_type = DAGPatchType(patch_type_str)
    except ValueError:
        raise PlanParseError(
            f"未知 patch_type: {patch_type_str!r}，"
            f"有效值: {[p.value for p in DAGPatchType]}"
        )

    # 解析 trigger_nodes
    trigger_nodes_raw = data.get("trigger_nodes", [])
    if not isinstance(trigger_nodes_raw, list):
        raise PlanParseError("DAGPatch: trigger_nodes 不是数组")
    trigger_nodes = [NodeId(str(n)) for n in trigger_nodes_raw]

    # 解析 replace_nodes: [["old_id", {new_node_dict}], ...]
    replace_nodes_raw = data.get("replace_nodes", [])
    if not isinstance(replace_nodes_raw, list):
        raise PlanParseError("DAGPatch: replace_nodes 不是数组")
    replace_nodes: list[tuple[NodeId, DAGNode]] = []
    for i, item in enumerate(replace_nodes_raw):
        if not isinstance(item, list) or len(item) != 2:
            raise PlanParseError(
                f"DAGPatch: replace_nodes[{i}] 不是二元组 [old_id, new_node]"
            )
        old_id = NodeId(str(item[0]))
        new_node_dict = item[1]
        if not isinstance(new_node_dict, dict):
            raise PlanParseError(
                f"DAGPatch: replace_nodes[{i}][1] 不是 node object"
            )
        nid = new_node_dict.get("node_id", "")
        if not nid or not isinstance(nid, str):
            raise PlanParseError(
                f"DAGPatch: replace_nodes[{i}][1].node_id 为空或不是字符串"
            )
        oc_data = new_node_dict.get("output_contract", {}) or {}
        new_node = DAGNode(
            node_id=NodeId(nid),
            description=new_node_dict.get("description", ""),
            output_contract=OutputContract(
                description=oc_data.get("description", "") if isinstance(oc_data, dict) else "",
            ),
            required=new_node_dict.get("required", True),
            input_bindings=[
                ArtifactId(b) for b in new_node_dict.get("input_bindings", []) or []
            ],
            output_as=ArtifactId(str(raw_as))
            if (raw_as := (new_node_dict.get("output_as") or "").strip())
            else None,
        )
        replace_nodes.append((old_id, new_node))

    # 解析 add_nodes
    add_nodes_raw = data.get("add_nodes", [])
    if not isinstance(add_nodes_raw, list):
        raise PlanParseError("DAGPatch: add_nodes 不是数组")
    add_nodes: list[DAGNode] = []
    for i, rn in enumerate(add_nodes_raw):
        if not isinstance(rn, dict):
            raise PlanParseError(f"DAGPatch: add_nodes[{i}] 不是 object")
        nid = rn.get("node_id", "")
        if not nid or not isinstance(nid, str):
            raise PlanParseError(
                f"DAGPatch: add_nodes[{i}].node_id 为空或不是字符串"
            )
        oc_data = rn.get("output_contract", {}) or {}
        add_nodes.append(DAGNode(
            node_id=NodeId(nid),
            description=rn.get("description", ""),
            output_contract=OutputContract(
                description=oc_data.get("description", "") if isinstance(oc_data, dict) else "",
            ),
            required=rn.get("required", True),
            input_bindings=[ArtifactId(b) for b in rn.get("input_bindings", []) or []],
            output_as=ArtifactId(str(raw_as))
            if (raw_as := (rn.get("output_as") or "").strip())
            else None,
        ))

    # 解析 remove_nodes
    remove_nodes_raw = data.get("remove_nodes", [])
    if not isinstance(remove_nodes_raw, list):
        raise PlanParseError("DAGPatch: remove_nodes 不是数组")
    remove_nodes = [NodeId(str(n)) for n in remove_nodes_raw]

    # 解析 remove_edges
    remove_edges_raw = data.get("remove_edges", [])
    if not isinstance(remove_edges_raw, list):
        raise PlanParseError("DAGPatch: remove_edges 不是数组")
    remove_edges: list[tuple[NodeId, NodeId]] = []
    for i, e in enumerate(remove_edges_raw):
        if not isinstance(e, list) or len(e) != 2:
            raise PlanParseError(f"DAGPatch: remove_edges[{i}] 不是二元组 [from, to]")
        remove_edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

    # 解析 add_edges
    add_edges_raw = data.get("add_edges", [])
    if not isinstance(add_edges_raw, list):
        raise PlanParseError("DAGPatch: add_edges 不是数组")
    add_edges: list[tuple[NodeId, NodeId]] = []
    for i, e in enumerate(add_edges_raw):
        if not isinstance(e, list) or len(e) != 2:
            raise PlanParseError(f"DAGPatch: add_edges[{i}] 不是二元组 [from, to]")
        add_edges.append((NodeId(str(e[0])), NodeId(str(e[1]))))

    return DAGPatch(
        patch_type=patch_type,
        reason=data.get("reason", ""),
        trigger_nodes=trigger_nodes,
        created_at=now_iso(),
        replace_nodes=replace_nodes,
        remove_nodes=remove_nodes,
        add_nodes=add_nodes,
        remove_edges=remove_edges,
        add_edges=add_edges,
    )


def parse_execution_mode(raw: str | ExecutionMode | None) -> ExecutionMode:
    """解析 execution_mode 字段，容错 LLM 输出。

    Args:
        raw: LLM 输出的原始值，可为字符串、ExecutionMode 或 None。

    Returns:
        解析后的 ExecutionMode 枚举值（None 或未知值时默认 TOOL_CALL）。
    """
    if raw is None:
        return ExecutionMode.TOOL_CALL
    if isinstance(raw, ExecutionMode):
        return raw
    try:
        return ExecutionMode(str(raw))
    except ValueError:
        logger.warning("Planner: 未知 execution_mode=%r，fallback tool_call", raw)
        return ExecutionMode.TOOL_CALL
