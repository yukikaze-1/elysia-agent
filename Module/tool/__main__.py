"""集成烟雾测试 — Module/tool/

覆盖: 插件加载 → execute → execute_stream + ToolProgress → 错误分类 → Schema 导出 → Debug 接口

用法:
    conda run -n Elysia python -m Module.tool
"""

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from .protocol import (
    PluginContext, ToolContext, ToolPlugin, ToolProgress, ToolResult,
    ErrorType, ToolEmitter,
)
from .safety import SafetyProfile
from .manager import ToolManager


# ---- 插件定义 ----

class GetTimePlugin(ToolPlugin):
    name = "get_time"
    description = "获取当前日期和时间"
    param_model = None
    capabilities = set()
    safety = SafetyProfile(request_timeout=5.0)

    async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None = None,
                      params: Any = None) -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=20, description="返回的最大结果数")


class SearchMemoryPlugin(ToolPlugin):
    name = "search_memory"
    description = "搜索长期记忆中的相关信息"
    param_model = SearchParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: SearchParams) -> str:
        return f"搜索 '{params.query}' 找到了 {params.limit} 条结果"


class FailingPlugin(ToolPlugin):
    name = "failing"
    description = "一个会失败的工具（测试用）"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None = None,
                      params: Any = None) -> str:
        raise RuntimeError("模拟的错误")


class SlowPlugin(ToolPlugin):
    name = "slow_tool"
    description = "一个慢速工具（测试超时）"
    param_model = None
    capabilities = set()
    safety = SafetyProfile(request_timeout=0.1)

    async def execute(self, *, ctx: ToolContext, emit: ToolEmitter | None = None,
                      params: Any = None) -> str:
        await asyncio.sleep(0.5)
        return "never arrives"


class StreamScanParams(BaseModel):
    scope: str = Field(default="full", description="扫描范围")


class StreamScanPlugin(ToolPlugin):
    name = "stream_scan"
    description = "流式深度扫描（yield ToolProgress）"
    param_model = StreamScanParams
    capabilities = set()
    safety = SafetyProfile(request_timeout=5.0)

    async def execute_stream(self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: StreamScanParams):
        yield ToolProgress(message=f"开始扫描: {params.scope}", percent=0.0)
        yield ToolProgress(message="扫描中...", percent=0.5)
        await asyncio.sleep(0.05)
        yield ToolProgress(message="扫描完成", percent=1.0)
        yield ToolResult.ok(name=self.name, content=f"扫描 '{params.scope}' 完成", duration_ms=0)


class CancellablePlugin(ToolPlugin):
    name = "cancellable"
    description = "演示 is_cancelled() — 在循环中检测打断"
    param_model = None
    capabilities = set()
    safety = SafetyProfile(request_timeout=10.0)

    async def execute_stream(self, *, ctx: ToolContext, emit: ToolEmitter | None = None,
                             params: Any = None):
        for i in range(10):
            yield ToolProgress(message=f"处理中... {i+1}/10", percent=(i + 1) / 10)
            await asyncio.sleep(0.01)
        yield ToolResult.ok(name=self.name, content="完成", duration_ms=0)


# ---- 主流程 ----

async def main() -> None:
    tm = ToolManager()
    ctx = PluginContext(session_id="smoke-test-001")
    tool_ctx = ToolContext(session_id="smoke-test-001", user_id="妖梦",
                           services={"version": "1.0"})

    # ---- 加载 ----
    print("=" * 50)
    print("  Module/tool 集成测试")
    print("=" * 50)

    plugin_classes = [
        GetTimePlugin, SearchMemoryPlugin, FailingPlugin,
        SlowPlugin, StreamScanPlugin, CancellablePlugin,
    ]
    await tm.load_all(plugin_classes, ctx)
    print(f"\n已加载工具: {tm.tool_names}")
    assert len(tm) == 6

    # ---- Schema 导出 ----
    print("\n--- Schema 导出 ---")
    defs = tm.definitions()
    assert len(defs) == 6  # 所有插件（含无 param_model 的）均导出定义
    for d in defs:
        assert d["type"] == "function"
        assert "function" in d
    print(f"  OK: {len(defs)} 个工具已导出 schema")

    # ---- 基础执行 ----
    print("\n--- 基础执行 ---")
    result = await tm.execute("get_time", ctx=tool_ctx)
    print(f"  get_time: OK, content={result.content[:30]!r}")
    assert result.success

    result = await tm.execute("search_memory", ctx=tool_ctx, query="Rust")
    assert result.success and not result.truncated

    # ---- 错误分类 ----
    print("\n--- 错误分类 ---")
    result = await tm.execute("failing", ctx=tool_ctx)
    print(f"  failing: FAIL, error_type={result.error_type.value if result.error_type else 'None'}")
    assert result.error_type == ErrorType.TOOL_ERROR

    result = await tm.execute("slow_tool", ctx=tool_ctx)
    print(f"  slow_tool: FAIL, error_type={result.error_type.value if result.error_type else 'None'}")
    assert result.error_type == ErrorType.TIMEOUT
    assert result.error_type.is_retryable

    result = await tm.execute("nonexistent", ctx=tool_ctx)
    print(f"  nonexistent: FAIL, error_type={result.error_type.value if result.error_type else 'None'}")
    assert result.error_type == ErrorType.UNKNOWN_TOOL
    assert not result.error_type.is_retryable

    # ---- ctx 注入 ----
    print("\n--- ctx 注入 ---")
    assert tool_ctx.get("version") == "1.0"
    assert tool_ctx.get("nonexistent", "default") == "default"
    print("  OK: ToolContext 创建与服务访问")

    # ---- execute_stream (yield ToolProgress) ----
    print("\n--- execute_stream (yield ToolProgress) ---")
    stream_events = []
    async for event in tm.execute_stream("stream_scan", ctx=tool_ctx, scope="full"):
        stream_events.append(event)
    progress_events = [e for e in stream_events if isinstance(e, ToolProgress)]
    result_events = [e for e in stream_events if isinstance(e, ToolResult)]
    print(f"  进度事件: {len(progress_events)}, 结果事件: {len(result_events)}")
    assert len(progress_events) == 3
    assert len(result_events) == 1
    assert result_events[0].success

    # ---- cancellable 流式 ----
    print("\n--- cancellable 流式 ---")
    cancel_events = []
    async for event in tm.execute_stream("cancellable", ctx=tool_ctx):
        cancel_events.append(event)
    final = [e for e in cancel_events if isinstance(e, ToolResult)]
    assert len(final) == 1 and final[0].success
    print(f"  cancellable: OK, {len(cancel_events)} events")

    # ---- Debug 接口 ----
    print(f"\n--- Debug 接口 ---")
    print(f"  plugin_list: {len(tm.plugin_list)} plugins")
    print(f"  executing_now: {tm.executing_now}")
    print(f"  history: {len(tm.history)} entries")
    print(f"  pipeline_stats: {tm.pipeline_stats}")
    print(f"  capability_status: {tm.capability_status}")
    assert len(tm.plugin_list) == 6

    # ---- 历史记录 ----
    print(f"\n--- 执行历史 ---")
    for entry in tm.history[-3:]:
        status = "OK" if entry["success"] else f"FAIL ({entry.get('error_type', '?')})"
        print(f"  {entry['name']}: {status} ({entry['duration_ms']:.1f}ms)")

    # ---- 关闭 ----
    await tm.shutdown()
    print(f"\n关闭后: {len(tm)} plugins")
    assert len(tm) == 0

    print("\n全部通过！")


if __name__ == "__main__":
    asyncio.run(main())
