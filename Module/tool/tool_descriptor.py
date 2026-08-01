"""Tool 描述符 — 工具轻量投影（workflow 世界跨模块消费）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Tool(BaseModel):
    """工具描述符——WorkflowEngine 对 Module/tool 工具的轻量投影。

    只持有 WorkflowEngine 层需要的字段：
        name:         工具唯一标识符（与 ToolPlugin.name 对应，Executor 据此过滤 definitions）
        description:  工具功能描述（Planner 注入 prompt 供 LLM 选工具）
        capabilities: 工具能力标签（对齐 Module/tool.Capability，v2 RBAC 校验用）

    注意：参数 schema 不在此重复——Executor 运行时从 ToolManager.definitions() 获取
    完整的 OpenAI function-calling 格式 schema，此模型仅作为"许可列表"传递。
    不含执行逻辑，不含 SafetyProfile——这些由 ToolManager 内部持有。
    """
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
