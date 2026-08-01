"""集成测试辅助工具 —— WorkflowEngine 工厂、工具提取、mock 工具函数。

用法:
    from Module.workflow_engine.tests.helpers import (
        build_test_task_system,
        get_tools_from_manager,
        mock_replan_response,
        MOCK_PLUGIN_MAP,
    )

    # 创建带 mock 工具的 WorkflowEngine（默认）
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],  # WebSearchPlugin → MockWebSearchPlugin
    )

    # 从 WorkflowEngine 提取 Tool 列表给 DelegationPreparer
    tools = get_tools_from_manager(ts.infra.tool_registry)

    # Mock Replanner 返回固定 DAGPatch（跳过 LLM 调用）
    with mock_replan_response(my_patch):
        ...
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

from openai import AsyncOpenAI

from Module.workflow_engine.infra import AgentInfra
from Module.workflow_engine.schema import (
    ConstraintConfig,
    DAGPatch,
    Plan,
    Tool,
)
from Module.workflow_engine.workflow_engine import WorkflowEngine
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext, ToolPlugin
from core.tools.time.plugin import TimePlugin
from core.tools.web.mock_plugin import MockWebSearchPlugin


# ── Mock 插件映射 ────────────────────────────────────────────────────────────────

MOCK_PLUGIN_MAP: dict[str, type[ToolPlugin]] = {
    "web_search": MockWebSearchPlugin,
}
"""工具名 -> mock 插件类映射。后续新增不稳定工具的 mock 时往这里加条目。"""


# ── WorkflowEngine 工厂 ──────────────────────────────────────────────────────────────

async def build_test_task_system(
    plugins: list[type[ToolPlugin]] | None = None,
    use_mocks: bool = True,
    wrap_llm: bool = False,
) -> WorkflowEngine:
    """创建并启动 WorkflowEngine（真实 LLM + 选定工具）。

    Args:
        plugins: 要加载的插件类列表。默认 None -> [TimePlugin]（纯确定性工具）。
                 e2e 测试传 [TimePlugin, WebSearchPlugin]。
        use_mocks: True -> 将 plugins 中的不稳定工具替换为 MOCK_PLUGIN_MAP 中的 mock。
                   例如 [TimePlugin, WebSearchPlugin] -> [TimePlugin, MockWebSearchPlugin]。
                   False -> 原样使用 plugins。
        wrap_llm: True -> 包装 LLM client 以记录调用（e2e 测试日志用）。

    Returns:
        已启动的 WorkflowEngine 实例。

    Raises:
        RuntimeError: DEEPSEEK_API_KEY 未设置。
    """
    if plugins is None:
        plugins = [TimePlugin]

    # 替换不稳定工具为 mock
    resolved: list[type[ToolPlugin]] = []
    for cls in plugins:
        # 获取类级别 name（实例化前的类属性）
        plugin_name = getattr(cls, "name", "")
        if use_mocks and plugin_name in MOCK_PLUGIN_MAP:
            resolved.append(MOCK_PLUGIN_MAP[plugin_name])
        else:
            resolved.append(cls)

    # .env 兜底加载（conftest.py 已覆盖，此处为安全网）
    _ensure_env()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法创建 AsyncOpenAI")

    llm = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    if wrap_llm:
        llm = _wrap_llm_client(llm)

    tm = ToolManager()
    ctx = PluginContext(session_id="integration-test")
    await tm.load_all(resolved, ctx)

    infra = AgentInfra(
        llm=llm,
        milvus=None,
        http=None,
        tool_registry=tm,
        constraints=ConstraintConfig(),
    )

    ts = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=2)
    await ts.start()
    return ts


# ── 工具提取 ─────────────────────────────────────────────────────────────────────

def get_tools_from_manager(tm: ToolManager) -> list[Tool]:
    """从 ToolManager 提取 WorkflowEngine 格式的 Tool 列表。

    供 DelegationPreparer.prepare() 使用。

    已知局限：访问 tm._plugins（私有属性）。后续建议给 ToolManager
    增加公共方法 list_tools()，届时此函数改为调用公共 API。
    """
    tools: list[Tool] = []
    for name, plugin in tm._plugins.items():
        caps = getattr(plugin, "capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        desc = (
            getattr(plugin, "description", "")
            or getattr(plugin, "tool_description", "")
            or ""
        )
        tools.append(Tool(
            name=name,
            description=desc,
            capabilities=list(caps) if caps else [],
        ))
    return tools


# ── Replanner mock ───────────────────────────────────────────────────────────────

def mock_replan_response(response: DAGPatch | Plan):
    """返回一个 patch 上下文管理器，mock Replanner.replan() 返回固定结果。

    用法（在测试函数体内，无需 pytest monkeypatch fixture）:

        patch_obj = DAGPatch(reason="test", trigger_nodes=["node_1"])
        with mock_replan_response(patch_obj):
            # 提交任务，Replanner.replan() 会被 mock 返回 patch_obj
            ...

    Args:
        response: 预设的 DAGPatch 或 Plan，Replanner.replan() 将直接返回此对象。

    Returns:
        unittest.mock.patch 上下文管理器。
    """
    async def _mock_replan(self, *args: Any, **kwargs: Any) -> DAGPatch | Plan:
        return response

    return patch(
        "Module.workflow_engine.core.planner.replanner.Replanner.replan",
        side_effect=_mock_replan,
    )


# ── 内部 helper ──────────────────────────────────────────────────────────────────

def _ensure_env() -> None:
    """兜底 .env 加载（conftest.py 已覆盖，此处防止直接导入 helpers 时不加载）。"""
    from pathlib import Path
    _env_file = Path(__file__).resolve().parents[4] / ".env"
    if _env_file.exists():
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _, _value = _line.partition("=")
                    _key = _key.strip()
                    _value = _value.strip().strip('"').strip("'")
                    if _key not in os.environ:
                        os.environ[_key] = _value


def _wrap_llm_client(llm: AsyncOpenAI) -> AsyncOpenAI:
    """包装 AsyncOpenAI，拦截 chat.completions.create() 以记录 LLM 调用。

    这是一个轻量包装——仅打印调用摘要。完整版本（含 prompt/response 打印）
    保留在 test_e2e_mvp.py 中，因为它依赖大量 e2e 专属的日志格式化函数。
    """
    import time
    original_create = llm.chat.completions.create

    async def _wrapped_create(**kwargs: Any) -> Any:
        t0 = time.monotonic()
        result = await original_create(**kwargs)
        elapsed = time.monotonic() - t0
        model = getattr(result, "model", "?")
        usage = getattr(result, "usage", None)
        tokens = f"in={usage.prompt_tokens}/out={usage.completion_tokens}" if usage else "?"
        print(f"  [LLM] {model} {elapsed:.1f}s tokens=({tokens})")
        return result

    llm.chat.completions.create = _wrapped_create  # type: ignore[method-assign]
    return llm
