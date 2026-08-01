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
