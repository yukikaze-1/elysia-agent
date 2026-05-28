"""集成烟雾测试 — Module/tool/

覆盖: 基础 execute → Schema 导出 → 超时/异常隔离 → execute_stream + ToolEmitter → ctx 注入 → 错误分类

用法:
    conda run -n Elysia python -m Module.tool
"""

import asyncio

from .protocol import ToolContext, ToolDefinition, ToolEmitter, ToolParameter, ToolProgress, ToolResult
from .registry import ToolRegistry
from .decorator import tool


# ---- 基础工具 ----

@tool(description="获取当前日期和时间", timeout=5.0)
async def get_time() -> str:
    """返回当前时间字符串。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool(description="搜索长期记忆中的相关信息")
async def search_memory(query: str, limit: int = 5) -> str:
    """
    在长期记忆中搜索。

    :param query: 搜索关键词
    :param limit: 返回的最大结果数
    """
    return f"搜索 '{query}' 找到了 {limit} 条结果"


@tool(description="一个会失败的工具（测试用）")
async def failing_tool() -> str:
    raise RuntimeError("模拟的错误")


@tool(description="一个慢速工具（测试超时）", timeout=0.1)
async def slow_tool() -> str:
    await asyncio.sleep(0.5)
    return "never arrives"


# ---- ToolEmitter 流式工具 ----

@tool(description="流式深度扫描（Emitter 模式）", timeout=5.0)
async def stream_scan(scope: str, emit: ToolEmitter) -> str:
    """Emitter 模式 — 进度上报 + 正常 return。"""
    import time as _time
    await emit.progress(0.0, f"开始扫描: {scope}")
    await emit.progress(0.5, "扫描中...")
    # 模拟工作
    await asyncio.sleep(0.05)
    await emit.progress(1.0, "扫描完成")
    return f"扫描 '{scope}' 完成，耗时 {_time.monotonic():.1f}s"


@tool(description="演示 is_cancelled() — 在循环中检测打断")
async def cancellable_loop(emit: ToolEmitter) -> str:
    """CPU 密集循环中用 is_cancelled() 检测取消。"""
    for i in range(10):
        if emit.is_cancelled():
            return "已取消"
        await emit.progress(i / 10, f"处理中... {i+1}/10")
        await asyncio.sleep(0.01)
    return "完成"


async def main() -> None:
    registry = ToolRegistry()

    # ---- 注册 ----
    print("=" * 50)
    print("  Module/tool 集成测试")
    print("=" * 50)

    registry.register_many([
        get_time._tool_definition,
        search_memory._tool_definition,
        failing_tool._tool_definition,
        slow_tool._tool_definition,
        stream_scan._tool_definition,
        cancellable_loop._tool_definition,
    ])
    print(f"\n已注册工具: {registry.tool_names}")
    assert len(registry) == 6

    # ---- Schema 导出 ----
    print("\n--- Schema 导出 ---")
    oai = registry.definitions("openai")
    ant = registry.definitions("anthropic")
    gem = registry.definitions("gemini")
    assert len(oai) == len(ant) == len(gem) == 6
    # 验证缓存命中（同一 format 两次调用应返回不同引用但相同内容）
    oai2 = registry.definitions("openai")
    assert oai == oai2 and oai is not oai2
    print("  OK: 3 providers × 6 tools，缓存命中，深拷贝")

    # ---- 基础执行 ----
    print("\n--- 基础执行 ---")
    result = await registry.execute("get_time", {})
    print(f"  get_time: OK, content={result.content[:30]!r}")
    assert result.success and result.error_type is None

    result = await registry.execute("search_memory", {"query": "Rust"})
    assert result.success and result.truncated is False

    # ---- 错误分类 ----
    print("\n--- 错误分类 ---")
    result = await registry.execute("failing_tool", {})
    print(f"  failing_tool: FAIL, error_type={result.error_type.value}")
    assert result.error_type is not None

    result = await registry.execute("slow_tool", {})
    print(f"  slow_tool: FAIL, error_type={result.error_type.value}")
    assert result.error_type is not None
    assert result.error_type.is_retryable  # TIMEOUT is retryable

    result = await registry.execute("nonexistent", {})
    print(f"  nonexistent: FAIL, error_type={result.error_type.value}")
    assert result.error_type is not None
    assert not result.error_type.is_retryable  # UNKNOWN_TOOL is not

    # ---- ctx 注入 ----
    print("\n--- ctx 注入 ---")
    ctx = ToolContext(session_id="smoke-test-001", user_id="妖梦",
                      services={"version": "1.0"})
    assert ctx.get("version") == "1.0"
    assert ctx.get("nonexistent", "default") == "default"
    print("  OK: ToolContext 创建与服务访问")

    # ---- execute_stream (Emitter) ----
    print("\n--- execute_stream (Emitter 模式) ---")
    stream_events = []
    async for event in registry.execute_stream("stream_scan", {"scope": "full"}):
        stream_events.append(event)
    # 进度事件 + 最终结果
    progress_events = [e for e in stream_events if isinstance(e, ToolProgress)]
    result_events = [e for e in stream_events if isinstance(e, ToolResult)]
    print(f"  进度事件: {len(progress_events)}, 结果事件: {len(result_events)}")
    assert len(progress_events) == 3
    assert len(result_events) == 1
    assert result_events[0].success

    # ---- is_cancelled() ----
    print("\n--- is_cancelled() 演示 ---")
    cancel_events = []
    async for event in registry.execute_stream("cancellable_loop", {}):
        cancel_events.append(event)
    final = [e for e in cancel_events if isinstance(e, ToolResult)]
    assert len(final) == 1 and final[0].success
    print(f"  cancellable_loop: OK, {len(cancel_events)} events")

    # ---- 历史记录 ----
    print(f"\n执行历史: {len(registry.history)} 条")
    for entry in registry.history[-3:]:
        status = "OK" if entry["success"] else f"FAIL ({entry.get('error_type', '?')})"
        print(f"  {entry['name']}: {status} ({entry['duration_ms']:.1f}ms)")

    # ---- 卸载 ----
    assert registry.unregister("slow_tool")
    assert registry.unregister("cancellable_loop")
    assert not registry.unregister("nonexistent")
    print(f"\n卸载后: {registry.tool_names} (4 tools)")

    print("\n全部通过！")


if __name__ == "__main__":
    asyncio.run(main())
