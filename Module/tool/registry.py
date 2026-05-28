"""ToolRegistry — 工具注册、查询、执行、Schema 导出。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import threading
import time
import typing
from typing import AsyncGenerator, Literal

from .protocol import ErrorType, ToolContext, ToolDefinition, ToolEmitter, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册中心 — 注册、卸载、执行、Schema 导出。

    用法:
        registry = ToolRegistry()
        registry.register(ToolDefinition(...))

        # Schema 导出
        registry.definitions()           # OpenAI (default)
        registry.definitions("anthropic")

        # 执行
        result = await registry.execute("search", {"query": "..."})
        print(result.content)
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._history: list[dict] = []
        self._max_history: int = 20
        # Schema 缓存 — register/unregister 时失效
        self._cache: dict[str, list[dict]] = {}
        self._cache_lock: threading.Lock = threading.Lock()

    # ---- 注册 ----

    def register(self, definition: ToolDefinition) -> None:
        """注册工具定义。同名工具会覆盖旧定义。"""
        self._tools[definition.name] = definition
        self._cache.clear()

    def register_many(self, definitions: list[ToolDefinition]) -> None:
        """批量注册工具定义。"""
        for d in definitions:
            self.register(d)

    def unregister(self, name: str) -> bool:
        """卸载工具。返回 True 表示存在并被移除。"""
        if name in self._tools:
            del self._tools[name]
            self._cache.clear()
            return True
        return False

    def get(self, name: str) -> ToolDefinition | None:
        """获取工具定义。"""
        return self._tools.get(name)

    # ---- Schema 导出 (带缓存) ----

    def definitions(
        self, format: Literal["openai", "anthropic", "gemini"] = "openai"
    ) -> list[dict]:
        """导出所有已注册工具的 schema，按 provider 格式。

        结果被缓存，register/unregister 时自动失效。
        返回深拷贝以避免调用方意外修改。
        threading.Lock 保证并发安全。
        """
        with self._cache_lock:
            if format not in self._cache:
                result: list[dict] = []
                for defn in self._tools.values():
                    if format == "openai":
                        result.append(defn.to_openai())
                    elif format == "anthropic":
                        result.append(defn.to_anthropic())
                    elif format == "gemini":
                        result.append(defn.to_gemini())
                self._cache[format] = result
            return copy.deepcopy(self._cache[format])

    # ---- 执行 ----

    async def execute(self, name: str, arguments: dict, ctx: ToolContext | None = None) -> ToolResult:
        """执行工具并返回 ToolResult。

        内置安全机制:
          1. 工具不存在 → ToolResult.fail("Tool not found")
          2. 参数校验 → ToolResult.fail(validate errors)
          3. 超时守卫 → ToolResult.fail("Timeout")
          4. 结果截断 → truncated=True
          5. 异常隔离 → ToolResult.fail(exception message)

        ctx 参数: 运行时上下文，若工具声明了 ctx 参数则自动注入。
        若工具 ctx 无默认值且未提供 ctx → Fail Fast。
        """
        from .safety import execute_with_timeout, truncate_result, validate_args

        t_start = time.monotonic()

        # 1. Lookup
        defn = self._tools.get(name)
        if defn is None:
            result = ToolResult.fail(
                name, f"工具 '{name}' 未注册。可用工具: {self.tool_names}",
                error_type=ErrorType.UNKNOWN_TOOL,
            )
            self._record(name, arguments, result)
            return result

        if defn.func is None:
            result = ToolResult.fail(name, f"工具 '{name}' 未绑定函数",
                                     error_type=ErrorType.UNKNOWN_TOOL)
            self._record(name, arguments, result)
            return result

        # 1b. Fail Fast: ctx 必需但未提供
        if defn._ctx_required and ctx is None:
            logger.warning(
                "工具 '%s' 需要 ToolContext（声明 requires=%s），但调用方未提供 ctx",
                name, defn.requires,
            )
            result = ToolResult.fail(
                name, f"工具 '{name}' 需要运行时上下文 (ToolContext)，但未提供",
                error_type=ErrorType.TOOL_ERROR,
            )
            self._record(name, arguments, result)
            return result

        # 2. Validate args（跳过 hidden params — 它们由 registry 注入）
        visible_params = {
            k: v for k, v in defn.parameters.items()
            if k not in defn._hidden_params
        }
        coerced, errors = validate_args(visible_params, arguments)
        if errors:
            result = ToolResult.fail(name, "; ".join(errors),
                                     error_type=ErrorType.INVALID_ARGUMENT)
            self._record(name, arguments, result)
            return result

        # 2b. Inject hidden params (ctx, emit) — 对 LLM 隐藏的参数
        if "ctx" in defn._hidden_params and ctx is not None:
            coerced["ctx"] = ctx
        if "emit" in defn._hidden_params:
            coerced["emit"] = ToolEmitter(_queue=None)

        # 3. Execute with timeout
        try:
            raw = await execute_with_timeout(defn.func, coerced, defn.timeout)
        except TimeoutError:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, f"工具执行超时 ({defn.timeout}s)", duration_ms=duration_ms,
                error_type=ErrorType.TIMEOUT,
            )
            self._record(name, arguments, result)
            return result
        except asyncio.CancelledError:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, "工具执行被取消", duration_ms=duration_ms,
                error_type=ErrorType.CANCELLED,
            )
            self._record(name, arguments, result)
            raise  # 重新抛出 CancelledError，让上层 cognitive loop 处理
        except Exception as e:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, str(e), duration_ms=duration_ms,
                error_type=ErrorType.TOOL_ERROR,
            )
            self._record(name, arguments, result)
            return result

        # 4. Convert + truncate
        duration_ms = (time.monotonic() - t_start) * 1000

        # 工具返回了 ToolResult → 直接使用（补全工具名和耗时）
        if isinstance(raw, ToolResult):
            raw.tool_name = name
            raw.duration_ms = duration_ms
            self._record(name, arguments, raw)
            return raw

        # 工具返回了 str/其他 → 包装为 ToolResult（向后兼容）
        content, truncated = truncate_result(raw, defn.max_result_len)
        result = ToolResult.ok(
            name=name, content=content, duration_ms=duration_ms, truncated=truncated,
            result_type=defn.result_type,
        )
        self._record(name, arguments, result)
        return result

    async def execute_safe(self, name: str, arguments: dict, ctx: ToolContext | None = None) -> ToolResult:
        """执行工具，所有异常（包括 CancelledError）都转为 ToolResult。

        用于不希望 CancelledError 传播的场景（如后台任务）。
        """
        try:
            return await self.execute(name, arguments, ctx=ctx)
        except asyncio.CancelledError:
            return ToolResult.fail(name, "工具执行被取消", error_type=ErrorType.CANCELLED)
        except Exception as e:
            return ToolResult.fail(name, str(e), error_type=ErrorType.TOOL_ERROR)

    # ---- 异步流式执行 ----

    async def execute_stream(
        self, name: str, arguments: dict, ctx: ToolContext | None = None,
    ) -> AsyncGenerator[ToolResult, None]:
        """流式执行工具，支持两种工具模式。

        模式 A — 异步生成器 (async def ... yield):
            每一步 yield ToolProgress/ToolResult 都会实时产出。
            最终 yield ToolResult 结束。

        模式 B — ToolEmitter (普通 async def + emit 参数):
            工具通过 emit.progress() 上报进度，正常 return ToolResult。
            emit 由 execute_stream() 创建并注入。

        异常兜底: 工具内部未捕获的异常会被拦截并 yield ToolResult(success=False)。
        CancelledError 会 yield CANCELLED ToolResult 后 re-raise。
        调用方的 async for 循环不会因工具异常而直接崩溃。

        用法:
            async for event in registry.execute_stream("scan", {}):
                if isinstance(event, ToolResult):
                    print(f"Final: {event.content}")
                else:
                    print(f"Progress: {event.percent}%")
        """
        from .safety import validate_args
        from .protocol import ToolProgress

        defn = self._tools.get(name)
        if defn is None:
            result = ToolResult.fail(
                name, f"工具 '{name}' 未注册。可用工具: {self.tool_names}",
                error_type=ErrorType.UNKNOWN_TOOL,
            )
            self._record(name, arguments, result)
            yield result
            return

        if defn.func is None:
            result = ToolResult.fail(name, f"工具 '{name}' 未绑定函数",
                                     error_type=ErrorType.UNKNOWN_TOOL)
            self._record(name, arguments, result)
            yield result
            return

        is_generator = inspect.isasyncgenfunction(defn.func)

        # Detect if function has ToolEmitter param in its signature
        try:
            fhints = typing.get_type_hints(defn.func, include_extras=False)
        except Exception:
            fhints = {}
        has_emitter = any(t is ToolEmitter for t in fhints.values())
        if not has_emitter:
            for t in fhints.values():
                origin = typing.get_origin(t)
                if origin is not None and ToolEmitter in typing.get_args(t):
                    has_emitter = True
                    break

        if not is_generator and not has_emitter:
            result = ToolResult.fail(
                name,
                f"工具 '{name}' 不支持流式执行。"
                f"请使用 execute()，或让工具声明 emit: ToolEmitter 参数，"
                f"或改为 async generator。",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
            self._record(name, arguments, result)
            yield result
            return

        if defn._ctx_required and ctx is None:
            logger.warning(
                "工具 '%s' 需要 ToolContext（声明 requires=%s），但调用方未提供 ctx",
                name, defn.requires,
            )
            result = ToolResult.fail(
                name, f"工具 '{name}' 需要运行时上下文 (ToolContext)，但未提供",
                error_type=ErrorType.TOOL_ERROR,
            )
            self._record(name, arguments, result)
            yield result
            return

        # Validate args（跳过 hidden params）
        visible_params = {
            k: v for k, v in defn.parameters.items()
            if k not in defn._hidden_params
        }
        coerced, errors = validate_args(visible_params, arguments)
        if errors:
            result = ToolResult.fail(name, "; ".join(errors),
                                     error_type=ErrorType.INVALID_ARGUMENT)
            self._record(name, arguments, result)
            yield result
            return

        if "ctx" in defn._hidden_params and ctx is not None:
            coerced["ctx"] = ctx

        t_start = time.monotonic()

        # ---- 模式 B: ToolEmitter (async function with emit parameter) ----
        if has_emitter:
            from .protocol import ToolProgress as TP
            queue: asyncio.Queue = asyncio.Queue()
            emit = ToolEmitter(_queue=queue)
            coerced["emit"] = emit

            task = asyncio.create_task(defn.func(**coerced))
            emit._task = task

            try:
                last_progress = time.monotonic()

                while not task.done():
                    # 计算轮询间隔（基于 keep-alive timeout）
                    poll_timeout = 0.1  # default 100ms
                    if defn.progress_timeout > 0:
                        elapsed_since_last = time.monotonic() - last_progress
                        remaining = defn.progress_timeout - elapsed_since_last
                        if remaining <= 0:
                            task.cancel()
                            duration_ms = (time.monotonic() - t_start) * 1000
                            result = ToolResult.fail(
                                name,
                                f"工具执行中断: {defn.progress_timeout:.0f}s 内无进度更新",
                                duration_ms=duration_ms, error_type=ErrorType.TIMEOUT,
                            )
                            self._record(name, arguments, result)
                            yield result
                            return
                        poll_timeout = min(0.1, remaining)

                    try:
                        progress = await asyncio.wait_for(queue.get(), timeout=poll_timeout)
                        yield progress
                        last_progress = time.monotonic()
                    except asyncio.TimeoutError:
                        continue

                # Drain remaining progress events
                while not queue.empty():
                    try:
                        progress = queue.get_nowait()
                        yield progress
                    except asyncio.QueueEmpty:
                        break

                # Task completed — get final result
                raw = await task
            except asyncio.CancelledError:
                task.cancel()
                duration_ms = (time.monotonic() - t_start) * 1000
                result = ToolResult.fail(
                    name, "工具执行被取消", duration_ms=duration_ms,
                    error_type=ErrorType.CANCELLED,
                )
                self._record(name, arguments, result)
                yield result
                raise  # 重新抛出，让上层 cognitive loop 处理
            except TimeoutError:
                task.cancel()
                duration_ms = (time.monotonic() - t_start) * 1000
                result = ToolResult.fail(
                    name, f"工具执行超时 ({defn.timeout}s)", duration_ms=duration_ms,
                    error_type=ErrorType.TIMEOUT,
                )
                self._record(name, arguments, result)
                yield result
                return
            except Exception as e:
                duration_ms = (time.monotonic() - t_start) * 1000
                result = ToolResult.fail(
                    name, str(e), duration_ms=duration_ms,
                    error_type=ErrorType.TOOL_ERROR,
                )
                self._record(name, arguments, result)
                yield result
                return

            duration_ms = (time.monotonic() - t_start) * 1000

            if isinstance(raw, ToolResult):
                raw.tool_name = name
                raw.duration_ms = duration_ms
                self._record(name, arguments, raw)
                yield raw
            else:
                from .safety import truncate_result
                content, truncated = truncate_result(raw, defn.max_result_len)
                result = ToolResult.ok(
                    name=name, content=content, duration_ms=duration_ms,
                    truncated=truncated, result_type=defn.result_type,
                )
                self._record(name, arguments, result)
                yield result
            return

        # ---- 模式 A: 异步生成器 ----
        # 注意: async generator 没有全局超时（asyncio.wait_for 无法直接包装生成器）。
        # 长时间运行的工具应使用 ToolEmitter 模式（模式 B）+ progress_timeout。
        gen = defn.func(**coerced)
        try:
            async for item in gen:
                if isinstance(item, ToolResult):
                    duration_ms = (time.monotonic() - t_start) * 1000
                    item.tool_name = name
                    item.duration_ms = duration_ms
                    self._record(name, arguments, item)
                    yield item
                    return
                else:
                    yield item
        except TimeoutError:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, f"工具执行超时 ({defn.timeout}s)", duration_ms=duration_ms,
                error_type=ErrorType.TIMEOUT,
            )
            self._record(name, arguments, result)
            yield result
        except asyncio.CancelledError:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, "工具执行被取消", duration_ms=duration_ms,
                error_type=ErrorType.CANCELLED,
            )
            self._record(name, arguments, result)
            yield result
            raise  # 重新抛出，让上层 cognitive loop 处理
        except Exception as e:
            duration_ms = (time.monotonic() - t_start) * 1000
            result = ToolResult.fail(
                name, str(e), duration_ms=duration_ms,
                error_type=ErrorType.TOOL_ERROR,
            )
            self._record(name, arguments, result)
            yield result

    # ---- History / Info ----

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名称列表。"""
        return list(self._tools.keys())

    @property
    def history(self) -> list[dict]:
        """最近 20 条执行记录。"""
        return list(self._history)

    def _record(self, name: str, args: dict, result: ToolResult) -> None:
        entry: dict = {
            "name": name, "args": args,
            "success": result.success,
            "result_type": result.result_type,
        }
        if result.success:
            entry["result"] = result.content[:200]
            if result.truncated:
                entry["truncated"] = True
        else:
            entry["error"] = result.error
            entry["error_type"] = result.error_type.value if result.error_type else None
        entry["duration_ms"] = round(result.duration_ms, 1)
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    # ---- Container ----

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __repr__(self) -> str:
        return f"ToolRegistry({', '.join(self.tool_names) or 'empty'})"
