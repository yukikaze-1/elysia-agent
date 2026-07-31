# Planner 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Module/task_system/core/planner.py` (739行) 拆分为 `planner/` 目录下 5 个聚焦模块，`plan()` 和 `replan()` 分离为 Planner / Replanner 两个类。

**Architecture:** 原 `planner.py` 删除 → 新建 `planner/` 包（`prompts.py` + `parser.py` + `planner.py` + `replanner.py` + `__init__.py`）→ 更新唯一消费者 `sub_agent.py` 和测试文件。

**Tech Stack:** Python 3.13, Pydantic, 无新依赖

## Global Constraints

- 不改变任何运行时行为——纯代码搬移 + 类拆分
- `PlanParseError` 由 `_PlanParseError` 改名而来，公开导出
- `call_llm()` 参数从 `self._infra`/`self._token_ledger` 改为显式传入 `infra`/`token_ledger`
- `parse_plan_from_dict()` 内部 `self._parse_execution_mode(...)` → `parse_execution_mode(...)`（从 parser 模块导入的独立函数）

---

### Task 1: 删除旧 `planner.py` + 创建 `planner/` 包（5 个文件）

**Files:**
- Delete: `Module/task_system/core/planner.py`
- Create: `Module/task_system/core/planner/__init__.py`
- Create: `Module/task_system/core/planner/prompts.py`
- Create: `Module/task_system/core/planner/parser.py`
- Create: `Module/task_system/core/planner/planner.py`
- Create: `Module/task_system/core/planner/replanner.py`

**Interfaces:**
- Produces:
  - `Planner(infra, token_ledger)` — `plan(context, global_constraints, experience_records) -> Plan`
  - `Replanner(infra, token_ledger)` — `replan(original_plan, failed_node_ids, failure_diagnosis, node_results, intent, tools, max_nodes, max_mutations) -> DAGPatch | Plan`
  - `PlanParseError` — 公开异常类
  - `call_llm(infra, token_ledger, system, user) -> str`
  - `parse_plan_from_dict(data) -> Plan`
  - `parse_patch_from_dict(data) -> DAGPatch`
  - `parse_execution_mode(raw) -> ExecutionMode`

- [ ] **Step 1: 删除旧 `planner.py` + 创建 `planner/` 目录**

```bash
mkdir -p Module/task_system/core/planner
git rm Module/task_system/core/planner.py
```

- [ ] **Step 2: 创建 `prompts.py`** — 从原 `planner.py:44-203` 搬 5 个字符串常量，去掉下划线前缀

```python
# Module/task_system/core/planner/prompts.py
"""Planner / Replanner 的 LLM prompt 模板。"""

OUTPUT_SCHEMA_DESCRIPTION = """{
  "nodes": [
    {
      "node_id": "string (唯一标识，如 search_weather)",
      "description": "string (这一步做什么，给 Executor 看的自然语言任务描述)",
      "output_contract": {
        "description": "string (期望产出什么，给 Evaluator 做业务判定用)"
      },
      "required": true,
      "execution_mode": "tool_call 或 llm_direct（按上述规则）",
      "input_bindings": ["output:search_weather"],
      "output_as": "output:send_email"
    }
  ],
  "edges": [["from_node_id", "to_node_id"]]
}"""

SYSTEM_PROMPT = """你是任务规划器。根据用户意图、可用工具和约束，将任务拆解为有向无环图（DAG）。

## DAG 构建规则
1. 每个节点有唯一的 node_id（短名，如 "search_weather"、"send_email"）
2. edges 定义执行顺序：(from, to) 表示 from 完成后 to 才能开始
3. input_bindings 引用上游节点的 output_as，声明数据依赖
4. output_as 用 "output:<node_id>" 格式
5. 可选步骤（失败后不影响整体）设 required: false
6. output_contract.description 是自然语言期望——Evaluator 用它判断步骤是否成功
7. **多节点任务必须有边**——存在 >1 个节点时，edges 不能为空。
   每个节点要么依赖上游节点，要么被下游节点依赖。
   所有独立并行节点必须汇聚到一个最终汇总/报告/输出节点。
8. **始终包含一个最终输出节点**（如 compile_report、compose_answer）——
   它汇总上游所有节点的结果，生成用户可读的最终产物。

## execution_mode 字段
每个节点必须标注 execution_mode：
- **tool_call**（默认）: 节点需要调用外部工具（搜索、读文件、发邮件等）。Executor 会匹配工具并执行。
- **llm_direct**: 节点不需要外部工具。Executor 直接调 LLM 处理上游数据。
  **仅在以下情况使用 llm_direct**：
  · 汇总/编译/总结上游搜索结果
  · 改写/润色/翻译已有文本
  · 综合多个上游产物生成报告
  · 注意：llm_direct 节点仍可引用 input_bindings 读取上游数据

**关键规则**：
- 搜索/查询/调用工具 → tool_call（即使是"搜索 X"也标注 tool_call）
- 汇总/编写/整理报告 → llm_direct（禁止为这类节点标注 tool_call）
- 最终输出节点（compile_report、compose_answer、summarize 等）→ 一律 llm_direct

## 可用工具
{tools}

## 约束
- DAG 节点数上限: {max_nodes}
- 敏感操作上限: {max_mutations}

## 输出格式
严格返回以下 JSON（只返回 JSON，不要 markdown 代码块）：
{output_schema}"""

USER_PROMPT = """## 任务意图
{intent}

## 提示
{hints}

## 上下文摘要
{context}"""

REPLAN_SYSTEM_PROMPT = """你是任务修复器。一个 DAG 执行过程中有节点失败了。根据失败诊断和已成功节点的产物，决定修复策略。

## 可用策略
1. **局部修复**（推荐，大多数情况适用）：
   - replace_nodes: 替换失败节点——改描述/改工具/拆分任务
   - add_nodes: 在失败节点之前插入缺失的步骤（如先获取时间再搜索）
   - remove_nodes: 移除不必要的可选节点
   - 配合 add_edges / remove_edges 调整拓扑
2. **全量重建**（仅当 DAG 结构根本有问题——如规划逻辑错误、依赖关系矛盾）

## 修复原则
- 已成功的节点**不要动**——它们的结果会被保留复用
- 优先用最小的修改解决问题，避免不必要的全量重建
- 如果失败原因是工具选错，换工具；如果是参数问题，改 description 让 Executor 理解更精确
- 如果原因是"工具正常返回但业务目标未达成"（goal_failed），考虑拆分为多个更简单步骤
- 如果原因是"工具本身报错/超时/找不到"，尝试替代方案
- 新节点数不要超过 {max_nodes}（含已成功的节点）
- 敏感操作不要超过 {max_mutations}
- **汇总/编译/整理/报告节点必须标注 execution_mode: "llm_direct"**——见下方 execution_mode 规则

## execution_mode 字段
新增或替换节点时必须标注 execution_mode：
- **tool_call**: 节点需要调用外部工具（搜索、读文件、发邮件等）
- **llm_direct**: 节点不需要外部工具，Executor 直接调 LLM 处理上游数据。
  **仅在以下情况使用 llm_direct**：汇总/编译/总结上游结果、改写/润色/翻译文本、
  综合多个上游产物生成报告、最终输出节点（compile_report、compose_answer、summarize 等）
- 搜索/查询/调用工具 → tool_call；汇总/编写/整理报告 → llm_direct

## 可用工具
{tools}

## 输出格式

**CRITICAL**: `full_replan` 是互斥的格式选择器，两种格式绝不能混用字段。

- `full_replan: false` → 只输出 DAGPatch 字段（patch_type, trigger_nodes, replace_nodes, add_nodes, remove_nodes, add_edges, remove_edges, reason）。**禁止**输出 nodes/edges。
- `full_replan: true` → 只输出 nodes + edges（全量重建 DAG）。**禁止**输出 patch_type/replace_nodes/add_nodes/remove_nodes/add_edges/remove_edges。

### 局部修复:
{{
  "full_replan": false,
  "patch_type": "replace_nodes | add_nodes | remove_nodes",
  "reason": "修复理由",
  "trigger_nodes": ["failed_node_id"],
  "replace_nodes": [["old_node_id", {{"node_id": "new_id", "description": "...", "output_contract": {{"description": "..."}}, "required": true, "execution_mode": "tool_call 或 llm_direct（按上述规则）", "input_bindings": ["output:upstream"], "output_as": "output:new_id"}}]],
  "add_nodes": [{{"node_id": "...", "description": "...", "execution_mode": "tool_call 或 llm_direct（按上述规则）", ...}}],
  "remove_nodes": ["node_id_to_remove"],
  "remove_edges": [["from_id", "to_id"]],
  "add_edges": [["from_id", "to_id"]]
}}

### 全量重建:
{{
  "full_replan": true,
  "nodes": [
    {{
      "node_id": "string",
      "description": "string",
      "output_contract": {{"description": "string"}},
      "required": true,
      "execution_mode": "tool_call 或 llm_direct（按上述规则）",
      "input_bindings": ["output:upstream"],
      "output_as": "output:this_node"
    }}
  ],
  "edges": [["from_id", "to_id"]]
}}

严格返回以上两种 JSON 之一（只返回 JSON，不要 markdown 代码块）。"""

REPLAN_USER_PROMPT = """## 任务意图
{intent}

## 约束
- DAG 节点数上限（含已完成节点）: {max_nodes}
- 敏感操作上限: {max_mutations}

## 已成功的节点
{successful_nodes}

## 失败的节点
{failed_nodes}

## 尚未执行的节点
{pending_nodes}

## 原 DAG 拓扑 (edges)
{edges}"""
```

- [ ] **Step 3: 创建 `parser.py`** — 从原 `planner.py` 提取解析函数 + `_call_llm` + `_PlanParseError`

```python
# Module/task_system/core/planner/parser.py
"""Planner / Replanner 共享的解析函数和 LLM 调用。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import strip_markdown_fence

from Module.task_system.schema import (
    ArtifactId,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    ExecutionMode,
    NodeId,
    OutputContract,
    Plan,
    TokenComponentType,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class PlanParseError(Exception):
    """LLM 输出的 JSON 无法解析为 Plan。"""


async def call_llm(
    infra: AgentInfra,
    token_ledger: TokenLedger | None,
    system: str,
    user: str,
) -> str:
    """调用 LLM，推理模式 + JSON Output。"""
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
    """从 dict 解析 Plan（nodes + edges）。"""
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
    """从 dict 解析 DAGPatch（局部修复）。"""
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


def parse_execution_mode(raw) -> ExecutionMode:
    """解析 execution_mode 字段，容错 LLM 输出。"""
    if raw is None:
        return ExecutionMode.TOOL_CALL
    if isinstance(raw, ExecutionMode):
        return raw
    try:
        return ExecutionMode(str(raw))
    except ValueError:
        logger.warning("Planner: 未知 execution_mode=%r，fallback tool_call", raw)
        return ExecutionMode.TOOL_CALL
```

- [ ] **Step 4: 创建 `planner.py`** — `Planner` 类，仅含 `plan()` + `_parse_response()`

```python
# Module/task_system/core/planner/planner.py
"""Planner — 首次规划：DelegationContext → DAG 骨架。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    ExperienceRecord,
    Plan,
)

from . import prompts
from .parser import call_llm, parse_plan_from_dict, PlanParseError

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class Planner:
    """首次规划——一次 LLM 调用，DelegationContext → DAG 骨架。

    plan() 流程:
        1. 构建 prompt（含可用工具、约束、输出 schema）
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → Plan
        4. 返回 Plan（校验由 PlanValidator 在 SubAgent 层完成）
    """

    def __init__(
        self,
        infra: AgentInfra,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._infra = infra
        self._token_ledger = token_ledger

    async def plan(
        self,
        context: DelegationContext,
        global_constraints: ConstraintConfig,
        experience_records: list[ExperienceRecord] | None = None,
    ) -> Plan:
        """首次规划——DelegationContext → DAG 骨架。

        Args:
            context:            委派上下文（intent/hints/tools/task_constraints/…）
            global_constraints: 全局约束天花板，TaskConstraints.resolve() 的填充来源
            experience_records: v1 忽略（经验记忆未实现）

        Returns:
            Plan（DAG 节点列表 + 依赖关系）

        Raises:
            PlanParseError: LLM 返回的 JSON 无法解析为合法 Plan
        """
        # ── 计算生效约束 ──
        tc = context.task_constraints.resolve(global_constraints)
        max_nodes = tc.max_dag_nodes
        max_mutations = tc.max_mutations

        # ── 构建 prompt ──
        tools_desc = format_tools(context.tools, include_capabilities=False)
        hints = context.hints or "（无）"
        ctx_text = context.summarized_context or "（无）"

        system = prompts.SYSTEM_PROMPT.format(
            tools=tools_desc,
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            output_schema=prompts.OUTPUT_SCHEMA_DESCRIPTION,
        )
        user = prompts.USER_PROMPT.format(
            intent=context.intent,
            hints=hints,
            context=ctx_text,
        )

        # ── LLM 调用 ──
        logger.debug("Planner system prompt:\n%s", system)
        logger.debug("Planner user prompt:\n%s", user)
        raw = await call_llm(self._infra, self._token_ledger, system, user)
        logger.debug("Planner LLM 响应:\n%s", raw)
        plan = self._parse_response(raw)
        return plan

    def _parse_response(self, raw: str) -> Plan:
        """解析 LLM 返回的 JSON → Plan。

        Raises:
            PlanParseError: JSON 不合法或缺少必要字段
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PlanParseError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        return parse_plan_from_dict(data)
```

- [ ] **Step 5: 创建 `replanner.py`** — `Replanner` 类，`replan()` + `_parse_replan_response()` + `_format_nodes_by_status()`

```python
# Module/task_system/core/planner/replanner.py
"""Replanner — 失败修复：诊断 → DAGPatch（局部修复）或 Plan（全量重建）。"""

from __future__ import annotations

import json
import logging

from typing import TYPE_CHECKING

from Module.task_system.utils import format_tools, strip_markdown_fence

from Module.task_system.schema import (
    DAGPatch,
    DAGPatchType,
    NodeId,
    NodeState,
    NodeStatus,
    Plan,
    Tool,
)

from . import prompts
from .parser import call_llm, parse_plan_from_dict, parse_patch_from_dict, PlanParseError

if TYPE_CHECKING:
    from Module.task_system.infra import AgentInfra
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class Replanner:
    """失败修复——一次 LLM 调用，产出 DAGPatch（局部修复）或 Plan（全量重建）。

    replan() 流程:
        1. 格式化节点状态（成功/失败/未执行） + 拓扑 + 失败诊断
        2. LLM 调用（json_object 模式）
        3. 解析 JSON → DAGPatch（局部修复）或 Plan（全量重建）
        4. SubAgent 根据返回类型走增量路径或全量重建路径
    """

    def __init__(
        self,
        infra: AgentInfra,
        token_ledger: TokenLedger | None = None,
    ) -> None:
        self._infra = infra
        self._token_ledger = token_ledger

    async def replan(
        self,
        original_plan: Plan,
        failed_node_ids: list[NodeId],
        failure_diagnosis: str,
        node_results: dict[str, NodeState],
        intent: str = "",
        tools: list[Tool] | None = None,
        max_nodes: int = 20,
        max_mutations: int = 10,
    ) -> DAGPatch | Plan:
        """Replan——失败诊断 → 增量 DAGPatch 或全量重建 Plan。

        Args:
            original_plan:     当前 DAG（含 nodes + edges）
            failed_node_ids:   失败节点列表（并行执行时可能多个）
            failure_diagnosis: Orchestrator/Evaluator 产出的失败原因
            node_results:      所有节点的 NodeState（含 status/summary/error/evaluation）
            intent:            原始任务意图（用于理解上下文）
            tools:             可用工具列表
            max_nodes:         生效的 DAG 节点数上限
            max_mutations:     生效的敏感操作上限

        Returns:
            DAGPatch — 局部修复
            Plan     — 全量重建

        Raises:
            PlanParseError: LLM 返回的 JSON 无法解析
        """
        if tools is None:
            tools = []

        # ── 格式化节点状态 ──
        successful = self._format_nodes_by_status(
            node_results, original_plan, {NodeStatus.SUCCESS, NodeStatus.SKIPPED},
        )
        failed = self._format_nodes_by_status(
            node_results, original_plan,
            {NodeStatus.FAILED, NodeStatus.UNRECOVERABLE},
        )
        pending = self._format_nodes_by_status(
            node_results, original_plan,
            {NodeStatus.PENDING, NodeStatus.RUNNING},
        )

        # ── 格式化拓扑 ──
        edges_text = "\n".join(
            f"  {from_id} → {to_id}"
            for from_id, to_id in original_plan.dag.edges
        ) if original_plan.dag.edges else "（无依赖关系——所有节点并行）"

        # ── 构建 prompt ──
        tools_desc = format_tools(tools, include_capabilities=False)
        system = prompts.REPLAN_SYSTEM_PROMPT.format(
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            tools=tools_desc,
        )
        user = prompts.REPLAN_USER_PROMPT.format(
            intent=intent or "（未知）",
            max_nodes=max_nodes,
            max_mutations=max_mutations,
            successful_nodes=successful or "（无）",
            failed_nodes=failed or "（无）",
            pending_nodes=pending or "（无）",
            edges=edges_text,
        )
        user += f"\n\n## 失败诊断\n{failure_diagnosis}"

        # ── LLM 调用 ──
        logger.debug("Replanner system prompt:\n%s", system)
        logger.debug("Replanner user prompt:\n%s", user)
        raw = await call_llm(self._infra, self._token_ledger, system, user)
        logger.debug("Replanner LLM 响应:\n%s", raw)
        return self._parse_replan_response(raw)

    # ── 内部：节点状态格式化 ───────────────────────────────────────────────

    @staticmethod
    def _format_nodes_by_status(
        node_results: dict[str, NodeState],
        plan: Plan,
        statuses: set[NodeStatus],
    ) -> str:
        """将指定状态的节点格式化为 prompt 可读文本。"""
        lines: list[str] = []
        for node in plan.dag.nodes:
            nid_str = str(node.node_id)
            ns = node_results.get(nid_str)
            if ns is None or ns.status not in statuses:
                continue

            parts = [f"- {nid_str}: {node.description}"]
            if ns.summary:
                summary = ns.summary[:200] + "..." if len(ns.summary) > 200 else ns.summary
                parts.append(f"  产物摘要: {summary}")
            if ns.error:
                parts.append(f"  错误: {ns.error}")
            if ns.evaluation is not None:
                ev = ns.evaluation
                parts.append(
                    f"  评估: execution={ev.execution_status.value}, "
                    f"business={ev.business_status.value}, "
                    f"action={ev.suggested_action.value}"
                )

            lines.append("\n".join(parts))

        return "\n".join(lines)

    # ── 内部：replan 响应解析 ─────────────────────────────────────────────

    def _parse_replan_response(self, raw: str) -> DAGPatch | Plan:
        """解析 replan LLM 响应 → DAGPatch 或 Plan。

        Raises:
            PlanParseError: JSON 不合法或缺少必要字段
        """
        text = strip_markdown_fence(raw)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PlanParseError(f"Replan JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            raise PlanParseError(f"期望 JSON object，收到 {type(data).__name__}")

        full_replan = data.get("full_replan", False)

        if full_replan:
            if "patch_type" in data or "replace_nodes" in data:
                logger.warning(
                    "Replanner: full_replan=true 但响应包含 DAGPatch 字段 "
                    "(patch_type/replace_nodes)——这些字段将被忽略，仅使用 nodes/edges"
                )
        else:
            if "nodes" in data or "edges" in data:
                logger.warning(
                    "Replanner: full_replan=false 但响应包含全量重建字段 "
                    "(nodes/edges)——这些字段将被忽略，仅使用 DAGPatch 字段。"
                    "请检查 Planner prompt 是否充分强调格式互斥。"
                )

        if full_replan:
            return parse_plan_from_dict(data)

        return parse_patch_from_dict(data)
```

- [ ] **Step 6: 创建 `__init__.py`** — 公开导出

```python
# Module/task_system/core/planner/__init__.py
"""Planner 包——任务规划与修复。

Planner:  首次规划，DelegationContext → DAG 骨架
Replanner: 失败修复，诊断 → DAGPatch（局部修复）或 Plan（全量重建）
"""

from .parser import PlanParseError
from .planner import Planner
from .replanner import Replanner

__all__ = ["Planner", "Replanner", "PlanParseError"]
```

- [ ] **Step 7: 验证导入**

```bash
python -c "from Module.task_system.core.planner import Planner, Replanner, PlanParseError; print('OK')"
```

预期: `OK`

- [ ] **Step 8: 提交**

```bash
git add Module/task_system/core/planner/ Module/task_system/core/planner.py
git commit -m "refactor: 拆分 planner.py 为 planner/ 包 (planner + replanner + prompts + parser)"
```

---

### Task 2: 更新 `sub_agent.py` — `_planner` 拆为 `_planner` + `_replanner`

**Files:**
- Modify: `Module/task_system/core/sub_agent.py`

**Interfaces:**
- Consumes: `Planner`, `Replanner`, `PlanParseError` from `Module.task_system.core.planner`
- Produces: 同一个 `SubAgent` 类，运行时行为不变

- [ ] **Step 1: 更新 import（L58）**

```python
# 旧:
from Module.task_system.core.planner import Planner
# 新:
from Module.task_system.core.planner import Planner, Replanner
```

- [ ] **Step 2: 添加 `_replanner` 属性（L100 附近）**

```python
# 在 self._planner: Planner | None = None 之后添加:
self._replanner: Replanner | None = None
```

- [ ] **Step 3: 创建 Replanner 实例（L158 附近，`self._planner = Planner(...)` 之后）**

```python
# 在 self._planner = Planner(infra=self._infra, token_ledger=self._token_ledger) 之后添加:
self._replanner = Replanner(infra=self._infra, token_ledger=self._token_ledger)
```

- [ ] **Step 4: 替换 `self._planner.replan(...)` → `self._replanner.replan(...)`（L647 附近）**

```python
# 旧:
replan_result = await self._planner.replan(
    original_plan=plan,
    ...
)
# 新:
replan_result = await self._replanner.replan(
    original_plan=plan,
    ...
)
```

- [ ] **Step 5: 更新 assert（L645 附近，`self._planner.replan()` 之前）**

```python
# 旧:
assert self._planner is not None
# 新:
assert self._replanner is not None
```

- [ ] **Step 6: 更新 `_handle_replan` 中 `except NotImplementedError` 的错误消息（L658-660）**

```python
# 旧:
"SubAgent: task_id=%s NEEDS_REPLAN 但 Planner.replan() 尚未实现，"
# 新:
"SubAgent: task_id=%s NEEDS_REPLAN 但 Replanner.replan() 调用异常，"
```

- [ ] **Step 7: 验证**

```bash
python -c "from Module.task_system.core.sub_agent import SubAgent; print('OK')"
```

预期: `OK`

- [ ] **Step 8: 提交**

```bash
git add Module/task_system/core/sub_agent.py
git commit -m "refactor: sub_agent 适配 planner 拆分——_planner + _replanner"
```

---

### Task 3: 更新测试 + 验证全量通过

**Files:**
- Modify: `Module/task_system/tests/unit/test_planner.py`
- Modify: `Module/task_system/tests/integration/test_planner.py`
- Modify: `Module/task_system/tests/unit/test_sub_agent.py`

- [ ] **Step 1: 更新 `test_planner.py`（单元测试）import**

```python
# 旧:
from Module.task_system.core.planner import Planner, _PlanParseError
# 新:
from Module.task_system.core.planner import Planner, PlanParseError
```

同时全文替换 `_PlanParseError` → `PlanParseError`。

- [ ] **Step 2: 更新 `test_planner.py`（集成测试）import**

```python
# 旧:
from Module.task_system.core.planner import Planner
# 新:
from Module.task_system.core.planner import Planner
```
（路径不变，但 `Planner` 现在来自 `planner/planner.py` 而非 `planner.py`）

- [ ] **Step 3: 更新 `test_sub_agent.py` patch 路径（L159）**

```python
# 旧:
"Module.task_system.core.planner.Planner",
# 现在 Planner 在 planner.planner 中，但通过 planner.__init__ 导出
# mock 路径应指向真正的定义位置：
"Module.task_system.core.planner.planner.Planner",
```

检查 `test_sub_agent.py` 中所有引用 `Planner` 和 `replan` 的地方，确保 mock 路径更新。

- [ ] **Step 4: 运行单元测试**

```bash
python -m pytest Module/task_system/tests/unit/test_planner.py -v
```

预期: 全部 PASS

- [ ] **Step 5: 运行 SubAgent 单元测试**

```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v
```

预期: 全部 PASS

- [ ] **Step 6: TaskSystem 全量测试**

```bash
python -m pytest Module/task_system/tests/ -v
```

预期: 298 unit tests + 36 integration tests 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add Module/task_system/tests/
git commit -m "test: 适配 planner 拆分——更新 import 路径和 mock 目标"
```
