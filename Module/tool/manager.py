"""ToolManager — 实现 ToolExecutor，管理插件生命周期、安全管道、并发、热重载。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from .capability import Capability
from .protocol import (
    ErrorType, PluginContext, ToolContext, ToolDefinition,
    ToolPlugin, ToolResult, ToolExecutor,
)
from .safety import SafetyPipeline

logger = logging.getLogger(__name__)


class ToolManager:
    """工具管理器 — 实现 ToolExecutor Protocol。"""

    def __init__(self) -> None:
        self._plugins: dict[str, ToolPlugin] = {}
        self._plugin_classes: dict[str, type[ToolPlugin]] = {}
        self._plugin_modules: dict[str, str] = {}
        self._plugin_context: PluginContext | None = None
        self._pipeline = SafetyPipeline()
        self._history: deque[ToolResult] = deque(maxlen=50)
        self._executing: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ---- Lifecycle ----

    async def load_all(
        self,
        plugin_classes: list[type[ToolPlugin]],
        ctx: PluginContext,
    ) -> None:
        """加载并检测所有插件。setup() 失败则跳过并 log warning。"""
        self._plugin_context = ctx
        for cls in plugin_classes:
            instance = cls()
            try:
                # 从插件所在目录加载 config.yaml，覆盖 SafetyProfile 字段
                module_file = inspect.getfile(cls)
                plugin_dir = Path(module_file).parent
                config_file = plugin_dir / "config.yaml"
                if config_file.is_file():
                    try:
                        raw = yaml.safe_load(config_file.read_text("utf-8"))
                        if isinstance(raw, dict):
                            for key, value in raw.items():
                                if hasattr(instance.safety, key):
                                    setattr(instance.safety, key, value)
                    except (yaml.YAMLError, OSError) as e:
                        logger.warning(
                            "工具 '%s' config.yaml 加载失败: %s", cls.__name__, e,
                        )
                await instance.setup(ctx)
                self._plugins[instance.name] = instance
                self._plugin_classes[instance.name] = cls
                self._plugin_modules[instance.name] = cls.__module__

                if instance.safety and instance.safety.max_concurrency > 0:
                    self._semaphores[instance.name] = asyncio.Semaphore(
                        instance.safety.max_concurrency,
                    )

                logger.info("工具 '%s' 已加载", instance.name)
            except Exception as e:
                logger.warning(
                    "工具 '%s' setup() 失败，跳过: %s", instance.name, e,
                )

    async def shutdown(self) -> None:
        """优雅关闭：取消所有 in-flight 任务，调用所有 teardown()。"""
        await self.cancel_all()
        teardowns = [
            p.teardown()
            for p in self._plugins.values()
        ]
        results = await asyncio.gather(*teardowns, return_exceptions=True)
        for name, result in zip(self._plugins.keys(), results):
            if isinstance(result, Exception):
                logger.warning(
                    "工具 '%s' teardown() 异常: %s", name, result,
                )
        self._plugins.clear()
        logger.info("ToolManager 已关闭")

    # ---- Execution ----

    async def execute(
        self, name: str, *, ctx: ToolContext, **raw_params,
    ) -> ToolResult:
        """执行工具（非流式）。"""
        plugin = self._plugins.get(name)
        if plugin is None:
            return self._unknown_tool_result(name)

        sem = self._semaphores.get(name)
        self._executing.add(name)
        task = asyncio.create_task(
            self._pipeline.execute(plugin, name, ctx, raw_params, sem),
        )
        self._tasks[name] = task
        try:
            result = await task
            self._record(result)
            return result
        finally:
            self._executing.discard(name)
            self._tasks.pop(name, None)

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: Any = None, **raw_params,
    ) -> AsyncIterator[Any]:
        """执行工具（流式）。"""
        plugin = self._plugins.get(name)
        if plugin is None:
            yield self._unknown_tool_result(name)
            return

        sem = self._semaphores.get(name)
        self._executing.add(name)
        queue: asyncio.Queue = asyncio.Queue()

        async def _drive() -> None:
            try:
                async for chunk in self._pipeline.execute_stream(
                    plugin, name, ctx, raw_params, sem,
                ):
                    await queue.put(chunk)
            except asyncio.CancelledError:
                await queue.put(ToolResult.fail(
                    name=name, error="用户取消",
                    error_type=ErrorType.CANCELLED,
                ))
            except Exception as e:
                await queue.put(ToolResult.fail(
                    name=name, error=str(e),
                    error_type=ErrorType.TOOL_ERROR,
                ))
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(_drive())
        self._tasks[name] = task
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, ToolResult):
                    self._record(item)
                yield item
        finally:
            self._executing.discard(name)
            self._tasks.pop(name, None)

    # ---- Schema Export ----

    def definitions(self, format: str = "openai") -> list[dict]:
        """导出已激活插件的 LLM function calling schema。

        支持 openai / anthropic / gemini 三种格式。
        """
        format_methods = {
            "openai": "to_openai",
            "anthropic": "to_anthropic",
            "gemini": "to_gemini",
        }
        method = format_methods.get(format)
        if method is None:
            raise ValueError(f"不支持的格式: {format!r}，可选: {list(format_methods)}")

        schemas = []
        for name, plugin in self._plugins.items():
            if plugin.param_model is not None:
                schema = plugin.param_model.model_json_schema()
                schema = self._flatten_refs(schema)
            else:
                schema = {"type": "object", "properties": {}}

            td = ToolDefinition(
                name=name,
                description=plugin.description,
                _schema=schema,
                timeout=plugin.safety.request_timeout if plugin.safety else 30.0,
            )
            schemas.append(getattr(td, method)())
        return schemas

    def _flatten_refs(self, schema: dict) -> dict:
        """内联展开 $defs/$ref，确保 LLM 兼容性。"""
        schema = dict(schema)  # 浅拷贝避免修改原始 schema
        defs = schema.pop("$defs", {})
        if not defs:
            return schema
        return self._resolve_refs(schema, defs)

    def _resolve_refs(self, node: Any, defs: dict) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                return self._resolve_refs(defs.get(ref_name, {}), defs)
            return {k: self._resolve_refs(v, defs) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve_refs(item, defs) for item in node]
        return node

    # ---- Control ----

    async def cancel_all(self) -> None:
        """显式取消所有 in-flight 工具执行。"""
        for name in list(self._executing):
            task = self._tasks.get(name)
            if task and not task.done():
                task.cancel()
        self._executing.clear()
        self._tasks.clear()

    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """提交 HITL 审批结果。由 server 层在收到前端审批响应后调用。"""
        return self._pipeline.submit_approval(request_id, approved)

    def get_pending_approvals(self) -> list[tuple[str, str, dict]]:
        """返回待审批请求列表，用于 WS 重连时重新广播审批弹窗。"""
        return self._pipeline.get_pending_approvals()

    def set_hitl_broadcast(self, output_manager) -> None:
        """Wire HITL approval broadcast through OutputManager."""
        if output_manager is not None:
            async def _broadcast(request_id: str, tool_name: str,
                                 params: dict) -> None:
                try:
                    await output_manager.command(
                        "tool_approval",
                        params={
                            "tool": tool_name,
                            "params": str(params)[:200],
                            "request_id": request_id,
                        },
                    )
                except Exception:
                    pass  # broadcast failure should not block pipeline
            self._pipeline.set_hitl_broadcast(_broadcast)

    async def reload_plugin(self, name: str) -> bool:
        """热重载单个插件 — 不中断正在执行的任务。"""
        old = self._plugins.get(name)
        module_path = self._plugin_modules.get(name)
        if old is None or module_path is None:
            return False

        try:
            await old.teardown()
        except Exception as e:
            logger.warning("旧插件 '%s' teardown() 异常: %s", name, e)

        self._plugins.pop(name, None)
        self._semaphores.pop(name, None)

        try:
            module = importlib.import_module(module_path)
            module = importlib.reload(module)
            cls = self._plugin_classes.get(name)
            if cls is None:
                for attr in dir(module):
                    obj = getattr(module, attr)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, ToolPlugin)
                        and obj is not ToolPlugin
                        and getattr(obj, "name", "") == name
                    ):
                        cls = obj
                        break
            if cls is None:
                logger.error("无法在模块 '%s' 中找到插件类", module_path)
                return False

            new_instance = cls()
            if self._plugin_context is None:
                raise RuntimeError(
                    f"无法加载工具 '{name}': PluginContext 尚未初始化，请先调用 load_all()"
                )
            await new_instance.setup(self._plugin_context)
            self._plugins[name] = new_instance
            self._plugin_classes[name] = cls
            self._plugin_modules[name] = module_path

            if new_instance.safety and new_instance.safety.max_concurrency > 0:
                self._semaphores[name] = asyncio.Semaphore(
                    new_instance.safety.max_concurrency,
                )

            logger.info("工具 '%s' 已热重载", name)
            return True
        except Exception as e:
            logger.error("工具 '%s' 热重载失败: %s", name, e)
            return False

    # ---- Prompt Generation ----

    def to_prompt_text(self) -> str:
        """从已加载的工具插件自动生成 prompt 中的工具列表。

        遍历 self._plugins，将每个工具的 name/description/param_model/prompt_hint
        拼成 LLM 可读的格式。热重载后下次 prompt 自动反映最新工具。
        """
        if not self._plugins:
            return "（无可用工具）"

        lines: list[str] = []
        for plugin in self._plugins.values():
            lines.append(f"**{plugin.name}** — {plugin.description or ''}")
            if plugin.prompt_hint:
                lines.append(f"  {plugin.prompt_hint}")
            if plugin.param_model is not None:
                params: list[str] = []
                for field_name, field_info in plugin.param_model.model_fields.items():
                    ptype = field_info.annotation
                    type_name = ptype.__name__ if hasattr(ptype, '__name__') else str(ptype)
                    type_name = type_name.replace('typing.', '').replace('builtins.', '')
                    d = field_info.description or ""
                    params.append(f"{field_name}({type_name}): {d}")
                if params:
                    lines.append("  参数: " + "; ".join(params))
            lines.append("")
        return "\n".join(lines).rstrip()

    # ---- Debug Interface ----

    @property
    def tool_names(self) -> list[str]:
        """保留旧接口兼容性 — server.py health endpoint 使用。"""
        return list(self._plugins.keys())

    @property
    def plugin_list(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "capabilities": [c.value for c in p.capabilities],
                "safety": {
                    "requires_human_approval": p.safety.requires_human_approval if p.safety else False,
                    "max_concurrency": p.safety.max_concurrency if p.safety else 0,
                    "network_allowed": p.safety.network_allowed if p.safety else False,
                    "filesystem_allowed": p.safety.filesystem_allowed if p.safety else False,
                    # 新增 — 文件系统
                    "read_dirs": p.safety.read_dirs if p.safety else None,
                    "write_dir": p.safety.write_dir if p.safety else "",
                    "max_file_bytes": p.safety.max_file_bytes if p.safety else 0,
                    "allow_symlinks": p.safety.allow_symlinks if p.safety else False,
                    "max_dir_depth": p.safety.max_dir_depth if p.safety else 0,
                    # 新增 — 网络
                    "block_private_ips": p.safety.block_private_ips if p.safety else True,
                    "max_redirects": p.safety.max_redirects if p.safety else 5,
                    "max_response_bytes": p.safety.max_response_bytes if p.safety else 0,
                    "request_timeout": p.safety.request_timeout if p.safety else 30.0,
                    # 新增 — 数据库
                    "database_allowed": p.safety.database_allowed if p.safety else False,
                },
                "active": True,
            }
            for p in self._plugins.values()
        ]

    @property
    def executing_now(self) -> list[str]:
        return list(self._executing)

    @property
    def history(self) -> list[dict]:
        return [
            {
                "name": h.tool_name,
                "success": h.success,
                "error": h.error,
                "error_type": h.error_type.value if h.error_type else None,
                "duration_ms": h.duration_ms,
                "truncated": h.truncated,
                "result": h.content[:200] if h.content else "",
            }
            for h in self._history
        ]

    @property
    def capability_status(self) -> dict[str, bool]:
        caps: dict[str, bool] = {}
        for plugin in self._plugins.values():
            for cap in plugin.capabilities:
                caps[cap.value] = True
        return caps

    @property
    def pipeline_stats(self) -> dict:
        return self._pipeline.stats

    def _record(self, result: ToolResult) -> None:
        self._history.append(result)

    def _unknown_tool_result(self, name: str) -> ToolResult:
        return ToolResult.fail(
            name=name,
            error=(
                f"工具 '{name}' 当前未激活"
                f"（可能未加载或依赖的后端不可用），请使用其他可用工具完成该任务。"
            ),
            error_type=ErrorType.UNKNOWN_TOOL,
        )

    # ---- Container protocol (旧接口兼容) ----

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        return name in self._plugins

    def __repr__(self) -> str:
        return f"ToolManager(plugins={list(self._plugins.keys())})"
