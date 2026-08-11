"""ToolManager — 实现 ToolExecutor，管理插件生命周期、安全管道、并发、热重载。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import types
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import yaml

from Module.output.manager import OutputManager
from Module.tool.capability import Capability
from Module.tool.protocol import ErrorType, PluginContext, ToolContext, ToolDefinition, ToolEmitter, ToolPlugin, ToolResult, ToolExecutor
from Module.tool.safety import SafetyPipeline

logger = logging.getLogger(__name__)

# 结构化参数摘要（BE-042 §7）中单参数 preview 的最大字符数
_PARAM_PREVIEW_LIMIT = 100


@dataclass(frozen=True)
class ToolMetadata:
    """工具元数据 — 序列化给客户端校验（BE-042）与工具面板/注册表导出。

    派生字段（approval_authority / permission_required / available）由
    ToolPlugin 属性或 desktop_available 推导，见 ToolManager._plugin_metadata。
    """

    name: str
    description: str
    runtime: str  # backend | desktop | local_worker
    risk_level: str  # safe | medium | high
    permission_required: bool
    approval_authority: str  # backend | client
    version: str
    input_schema: dict
    output_schema: dict
    available: bool  # desktop 工具 = desktop_available；backend 恒 true


class ToolManager:
    """工具管理器 — 实现 ToolExecutor Protocol。"""

    def __init__(self) -> None:
        """初始化工具管理器。

        初始化插件注册表、安全管道、执行历史、in-flight 任务追踪等内部数据结构。
        """
        self._plugins: dict[str, ToolPlugin] = {}
        self._plugin_classes: dict[str, type[ToolPlugin]] = {}
        self._plugin_modules: dict[str, str] = {}
        self._plugin_context: PluginContext | None = None
        self._pipeline = SafetyPipeline()
        self._history: deque[ToolResult] = deque(maxlen=50)
        self._executing: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # reload 并发控制（E1/P2-06）：_reload_lock 仅覆盖原子替换微秒窗口；
        # _inflight 记录进行中 execute/execute_stream 任务（reload 排空快照）；
        # _teardown_tasks 独立登记延迟 teardown（不被 cancel_all 取消）。
        self._reload_lock: asyncio.Lock = asyncio.Lock()
        self._inflight: dict[str, set[asyncio.Task]] = {}
        self._teardown_tasks: dict[str, asyncio.Task] = {}
        # desktop 运行时可用性（bridge.is_available() 同步用，BE-040）——
        # 影响 plugin_list/registry_metadata 中 desktop 工具的 available
        self._desktop_available: bool = False

    # ---- Lifecycle ----

    async def load_all(
        self,
        plugin_classes: list[type[ToolPlugin]],
        ctx: PluginContext,
    ) -> None:
        """加载并检测所有插件。

        逐个调用 setup()，从插件目录加载 config.yaml 覆盖 SafetyProfile。
        setup() 失败则跳过并 log warning。

        Args:
            plugin_classes: 待注册的 ToolPlugin 子类列表。
            ctx: 依赖注入容器。
        """
        self._plugin_context = ctx
        for cls in plugin_classes:
            instance = cls()
            try:
                # 从插件所在目录加载 config.yaml，覆盖 SafetyProfile 字段
                self._load_safety_config(instance, cls)
                await instance.setup(ctx)
                self._plugins[instance.name] = instance
                self._plugin_classes[instance.name] = cls
                self._plugin_modules[instance.name] = cls.__module__

                if instance.safety and instance.safety.max_concurrency > 0:
                    self._semaphores[instance.name] = asyncio.Semaphore(
                        instance.safety.max_concurrency,
                    )

                logger.debug("工具 '%s' 已加载", instance.name)
            except Exception as e:
                logger.warning(
                    "工具 '%s' setup() 失败，跳过: %s", instance.name, e,
                )
        names = list(self._plugins.keys())
        logger.info("工具: 已加载 %d 个 (%s)", len(names), ", ".join(names))

    def _load_safety_config(self, instance: ToolPlugin, cls: type[ToolPlugin]) -> None:
        """从插件目录加载 config.yaml 覆盖 SafetyProfile 字段（load/reload 共用）。

        Args:
            instance: 待配置的插件实例。
            cls: 插件类（用于定位模块文件所在目录）。
        """
        module_file = inspect.getfile(cls)
        plugin_dir = Path(module_file).parent
        config_file = plugin_dir / "config.yaml"
        if not config_file.is_file():
            return
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

    async def shutdown(self) -> None:
        """优雅关闭管理器。

        取消所有 in-flight 工具执行任务，依次调用所有插件的 teardown()。
        """
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
        self, name: str, *, ctx: ToolContext, **raw_params: Any,
    ) -> ToolResult:
        """执行工具（非流式）。

        Args:
            name: 工具名称。
            ctx: 运行时上下文。
            **raw_params: 工具参数。

        Returns:
            ToolResult: 工具执行结果。
        """
        async with self._reload_lock:                    # 与 swap 互斥（微秒级，无 await）
            plugin = self._plugins.get(name)
            if plugin is None:
                return self._unknown_tool_result(name)
            sem = self._semaphores.get(name)
        self._executing.add(name)                        # 保留——debug executing_now 依赖
        task = asyncio.create_task(
            self._pipeline.execute(plugin, name, ctx, raw_params, sem),
        )
        # C2/P1-13：_tasks 按 call_id 跟踪（同工具可同时多个调用，互不覆盖）
        self._tasks[ctx.call_id or name] = task
        self._inflight.setdefault(name, set()).add(task)  # 供 reload 排空快照
        try:
            result = await task
            self._record(result)
            return result
        finally:
            self._executing.discard(name)
            self._inflight.setdefault(name, set()).discard(task)
            self._tasks.pop(ctx.call_id or name, None)

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: ToolEmitter | None = None, **raw_params: Any,
    ) -> AsyncIterator[Any]:
        """执行工具（流式）。

        Args:
            name: 工具名称。
            ctx: 运行时上下文。
            emit: 进度上报器（注入到 ctx.services["_emit"]）。
            **raw_params: 工具参数。

        Yields:
            ToolProgress | ToolResult: 中间进度事件或最终执行结果。
        """
        # 取值段加锁（与 execute 一致）；yield 必须在锁外——async generator
        # 锁跨 yield 持有到下一 __anext__/GC，锁内 yield 会阻塞后续 reload/execute
        async with self._reload_lock:
            plugin = self._plugins.get(name)
            sem = self._semaphores.get(name)
        if plugin is None:
            yield self._unknown_tool_result(name)   # 锁外 yield
            return

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
        # C2/P1-13：按 call_id 跟踪（流式同理）
        self._tasks[ctx.call_id or name] = task
        self._inflight.setdefault(name, set()).add(task)  # 供 reload 排空快照
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
            self._inflight.setdefault(name, set()).discard(task)
            self._tasks.pop(ctx.call_id or name, None)

    # ---- Schema Export ----

    def definitions(
        self,
        format: str = "openai",
        filter_runtime: Callable[[str], bool] | None = None,
    ) -> list[dict]:
        """导出已激活插件的 LLM function calling schema。

        支持 openai / anthropic / gemini 三种格式。

        Args:
            format: 输出格式 ("openai", "anthropic", "gemini")。
            filter_runtime: 运行时过滤回调，接收 plugin.runtime，返回 True 保留。
                默认 None = 全量（不做过滤）。D-j 用 lambda rt: rt != "desktop" or
                bridge.is_available() 在 desktop 未连接时从 LLM schema 移除 desktop 工具。
                ToolExecutor protocol 签名（无此参数）保持不变，过滤由 ToolAdapter 注入。

        Returns:
            各 provider 格式的 function/tool schema 字典列表。

        Raises:
            ValueError: 不支持的格式。
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
            if filter_runtime is not None and not filter_runtime(plugin.runtime):
                continue
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
        """内联展开 $defs/$ref，确保 LLM 兼容性。

        Args:
            schema: 包含 $defs 的 JSON Schema 字典。

        Returns:
            已展开 $ref 引用的 JSON Schema 字典。
        """
        schema = dict(schema)  # 浅拷贝避免修改原始 schema
        defs = schema.pop("$defs", {})
        if not defs:
            return schema
        return self._resolve_refs(schema, defs)

    def _resolve_refs(self, node: Any, defs: dict) -> Any:
        """递归解析 $ref 引用。

        Args:
            node: 当前节点（dict、list 或标量）。
            defs: $defs 定义字典。

        Returns:
            已解析引用的节点副本。
        """
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                return self._resolve_refs(defs.get(ref_name, {}), defs)
            return {k: self._resolve_refs(v, defs) for k, v in node.items()}
        if isinstance(node, list):
            return [self._resolve_refs(item, defs) for item in node]
        return node

    # ---- Metadata Export (BE-040) ----

    def _plugin_metadata(self, plugin: ToolPlugin, desktop_available: bool) -> ToolMetadata:
        """从插件派生 ToolMetadata。

        input_schema 复用 definitions() 的 param_model JSON schema 路径（含 _flatten_refs）；
        available：desktop 工具 = desktop_available、backend 恒 true。
        派生字段（approval_authority/permission_required）由 ToolPlugin 属性提供（Task 2）。

        Args:
            plugin: 待导出的插件实例。
            desktop_available: desktop 运行时是否可用（bridge.is_available()）。

        Returns:
            ToolMetadata 完整元数据。
        """
        if plugin.param_model is not None:
            input_schema = plugin.param_model.model_json_schema()
            input_schema = self._flatten_refs(input_schema)
        else:
            input_schema = {"type": "object", "properties": {}}
        return ToolMetadata(
            name=plugin.name,
            description=plugin.description,
            runtime=plugin.runtime,
            risk_level=plugin.risk_level,
            permission_required=plugin.permission_required,
            approval_authority=plugin.approval_authority,
            version=plugin.version,
            input_schema=input_schema,
            output_schema=plugin.output_schema,
            available=desktop_available if plugin.runtime == "desktop" else True,
        )

    def registry_metadata(self, desktop_available: bool = False) -> list[ToolMetadata]:
        """遍历 _plugins，返回完整元数据（BE-042 注册表导出）。

        Args:
            desktop_available: desktop 运行时可用性。false 时 desktop 工具仍列出
                （available=false），供客户端知晓存在但不可调。

        Returns:
            list[ToolMetadata] 完整元数据列表。
        """
        return [
            self._plugin_metadata(p, desktop_available)
            for p in self._plugins.values()
        ]

    def set_desktop_available(self, available: bool) -> None:
        """设置 desktop 运行时可用性（bridge.is_available() 同步用）。

        影响 plugin_list / registry_metadata 中 desktop 工具的 available 字段。

        Args:
            available: desktop 客户端是否已连接可执行。
        """
        self._desktop_available = bool(available)

    # ---- Control ----

    async def cancel_all(self) -> None:
        """取消所有 in-flight 工具执行任务（按 call_id 全量，C2/P1-13）。"""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._executing.clear()
        self._tasks.clear()

    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """提交 HITL 审批结果。

        由 server 层在收到前端审批响应后调用。

        Args:
            request_id: 审批请求 ID。
            approved: True 表示批准，False 表示拒绝。

        Returns:
            True 如果找到了对应的审批请求。
        """
        return self._pipeline.submit_approval(request_id, approved)

    def get_pending_approvals(self) -> list[tuple[str, str, dict]]:
        """返回待审批请求列表。

        用于 WS 重连时重新广播审批弹窗。

        Returns:
            [(request_id, tool_name, params), ...] 列表。
        """
        return self._pipeline.get_pending_approvals()

    def set_hitl_broadcast(self, output_manager: OutputManager | None) -> None:
        """绑定 HITL 审批广播回调到 OutputManager。

        版本门控（spec §7）：按 protocol_version 两路广播——v1 通道收原样
        `{tool, params: str(params)[:200], request_id}`（逐字节不变），v2 通道收
        enrich 版（+ summary 结构化参数摘要 + runtime/version/riskLevel/
        permissionRequired/approvalAuthority，取自 `_plugins[tool_name]` 元数据）。

        Args:
            output_manager: OutputManager 实例，用于 command("tool_approval", ...) 广播。
        """
        if output_manager is not None:
            async def _broadcast(request_id: str, tool_name: str,
                                 params: dict) -> None:
                try:
                    # 基础字段（v1 wire 逐字节不变）
                    base = {
                        "tool": tool_name,
                        "params": str(params)[:200],
                        "request_id": request_id,
                    }
                    plugin = self._plugins.get(tool_name)
                    split_versions = getattr(output_manager, "_split_versions", None)
                    if plugin is None or split_versions is None:
                        # 未知工具或 output_manager 无版本拆分能力：兜底广播基础字段到全部通道
                        await output_manager.command("tool_approval", params=base)
                        return
                    v1, v2 = split_versions()
                    if v1:
                        await output_manager.command(
                            "tool_approval", params=base, channels=v1,
                        )
                    if v2:
                        enriched: dict[str, Any] = dict(base)
                        enriched["summary"] = self._build_param_summary(params)
                        enriched["runtime"] = plugin.runtime
                        enriched["version"] = plugin.version
                        enriched["riskLevel"] = plugin.risk_level
                        enriched["permissionRequired"] = plugin.permission_required
                        enriched["approvalAuthority"] = plugin.approval_authority
                        await output_manager.command(
                            "tool_approval", params=enriched, channels=v2,
                        )
                except Exception:
                    # OutputManager 长期故障时审批弹窗永不弹出且无人知晓，
                    # 需审批工具等到 60s 超时——必须留 warning 可见
                    logger.warning(
                        "HITL 审批广播失败（审批弹窗可能不出现）", exc_info=True)
            self._pipeline.set_hitl_broadcast(_broadcast)

    @staticmethod
    def _build_param_summary(params: dict) -> dict:
        """构造结构化参数摘要（BE-042 §7）：逐参数 {type, preview, truncated}。

        保留 `str(params)[:200]` 兼容字段之外的结构化视图，供 v2 前端审批弹窗
        逐参数展示。preview 超长截断到 _PARAM_PREVIEW_LIMIT。

        Args:
            params: 工具原始参数字典。

        Returns:
            {param_name: {"type": str, "preview": str, "truncated": bool}}。
        """
        summary = {}
        for key, value in params.items():
            preview = str(value)
            truncated = len(preview) > _PARAM_PREVIEW_LIMIT
            summary[key] = {
                "type": type(value).__name__,
                "preview": preview[:_PARAM_PREVIEW_LIMIT] if truncated else preview,
                "truncated": truncated,
            }
        return summary

    async def reload_plugin(self, name: str) -> tuple[bool, str]:
        """热重载单个插件——原子替换注册表 + 失败回滚（E1/P2-06）。

        流程：锁外构造/setup 新实例 → 持锁身份复核 + 原子替换四个注册表 dict →
        锁外排空旧实例 in-flight 后 teardown。新 setup 失败保留旧实例。

        Args:
            name: 要重载的工具名称。

        Returns:
            tuple[bool, str]: (是否成功, 失败原因/成功时空串)。
        """
        old = self._plugins.get(name)
        module_path = self._plugin_modules.get(name)
        if old is None or module_path is None:
            return False, f"工具 '{name}' 不存在或无法定位模块"

        # 1. 隔离构造 + 共用 config + setup（锁外——慢 I/O 不阻塞其他工具）
        try:
            module = importlib.import_module(module_path)
            module = importlib.reload(module)

            # 2. 从新 module 重新 discover class（不读旧 _plugin_classes）
            new_cls = self._discover_class(module, name)
            if new_cls is None:
                return False, f"模块 '{module_path}' 中未找到名为 '{name}' 的插件类"

            new_instance = new_cls()
            self._load_safety_config(new_instance, new_cls)
            if self._plugin_context is None:
                return False, "PluginContext 未初始化，请先调用 load_all()"
            await new_instance.setup(self._plugin_context)
        except Exception as e:
            logger.error("工具 '%s' 热重载失败: %s", name, e)
            return False, f"热重载失败: {e}"

        # 3. 持锁做身份复核 + 原子替换（微秒窗口；setup 期间并发 reload/shutdown
        #    可能已改动注册表——复核 _plugins.get(name) is old，不等则不覆盖他人结果）
        superseded = False
        async with self._reload_lock:
            if self._plugins.get(name) is not old:
                superseded = True
            else:
                new_sem = None
                if new_instance.safety and new_instance.safety.max_concurrency > 0:
                    new_sem = asyncio.Semaphore(new_instance.safety.max_concurrency)
                old_tasks = set(self._inflight.get(name, ()))   # 快照：排空目标 = 替换前已在执行的任务
                self._plugins[name] = new_instance
                self._plugin_classes[name] = new_cls
                self._plugin_modules[name] = module_path
                if new_sem is not None:
                    self._semaphores[name] = new_sem
                else:
                    self._semaphores.pop(name, None)

        if superseded:
            # 弃用新实例——锁外 teardown（慢 I/O 不进微秒锁窗口）+ try/except（异常不逸出端点）
            try:
                await new_instance.teardown()
            except Exception as e:
                logger.warning("工具 '%s' 并发覆盖后新实例 teardown 异常: %s", name, e)
            return False, f"工具 '{name}' 在 reload 期间被并发修改"

        # 4. 排空旧实例 in-flight + teardown（锁已释放——新调用立即走新实例，不受阻塞）
        await self._drain_and_teardown(name, old, old_tasks)
        logger.info("工具 '%s' 已热重载", name)
        return True, ""

    @staticmethod
    def _discover_class(module: types.ModuleType, name: str) -> type[ToolPlugin] | None:
        """从模块中扫描名为 name 的 ToolPlugin 子类（不读旧 _plugin_classes）。

        Args:
            module: importlib.reload 后的新模块。
            name: 插件名称。

        Returns:
            匹配的插件类；未找到返回 None。
        """
        for attr in dir(module):
            obj = getattr(module, attr)
            if (
                isinstance(obj, type)
                and issubclass(obj, ToolPlugin)
                and obj is not ToolPlugin
                and getattr(obj, "name", "") == name
            ):
                return obj
        return None

    async def _drain_and_teardown(
        self, name: str, old: ToolPlugin, old_tasks: set[asyncio.Task], timeout: float = 10.0,
    ) -> None:
        """等旧实例 in-flight 归零后 teardown。超时转后台延迟 teardown，不阻塞 reload。

        Args:
            name: 工具名称。
            old: 替换前的旧实例。
            old_tasks: swap 瞬间快照的旧 in-flight 任务（不含替换后的新调用）。
            timeout: 排空等待超时（秒），超时后登记延迟 teardown。
        """
        if old_tasks:
            _done, pending = await asyncio.wait(old_tasks, timeout=timeout)
            if pending:
                logger.warning(
                    "工具 '%s' 热重载：%d 个 in-flight 未在 %.0fs 内排空，延迟 teardown",
                    name, len(pending), timeout,
                )
                self._schedule_teardown(name, old)
                return
        try:
            await old.teardown()
        except Exception as e:
            logger.warning("旧插件 '%s' teardown() 异常: %s", name, e)

    def _schedule_teardown(self, name: str, old: ToolPlugin) -> None:
        """后台延迟 teardown——登记到 _teardown_tasks + done 回调收异常。

        _teardown_tasks 独立登记（非 _tasks），不被 cancel_all 取消；shutdown()
        显式 gather 收尾。

        Args:
            name: 工具名称。
            old: 排空超时后待清理的旧实例。
        """
        task = asyncio.create_task(old.teardown())
        # 独立登记——不被 cancel_all 取消
        self._teardown_tasks[f"{name}:teardown:{id(old)}"] = task

        def _on_done(t: asyncio.Task) -> None:
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("延迟 teardown 工具 '%s' 异常: %s", name, e)
            finally:
                self._teardown_tasks.pop(f"{name}:teardown:{id(old)}", None)

        task.add_done_callback(_on_done)

    # ---- Prompt Generation ----

    # ---- Debug Interface ----

    @property
    def tool_names(self) -> list[str]:
        """保留旧接口兼容性 — server.py health endpoint 使用。"""
        return list(self._plugins.keys())

    @property
    def plugin_list(self) -> list[dict]:
        """工具面板列表 — 每项含 runtime/version/risk_level/permission_required/
        approval_authority/available（BE-040 §3.2，复用 registry_metadata 派生逻辑）。

        desktop 工具 available 随 self._desktop_available 翻转、backend 恒 true。
        """
        result = []
        for p in self._plugins.values():
            meta = self._plugin_metadata(p, self._desktop_available)
            result.append({
                "name": meta.name,
                "description": meta.description,
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
                "runtime": meta.runtime,
                "version": meta.version,
                "risk_level": meta.risk_level,
                "permission_required": meta.permission_required,
                "approval_authority": meta.approval_authority,
                "available": meta.available,
            })
        return result

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
        """将工具执行结果写入历史环形缓冲区。

        Args:
            result: 工具执行结果。
        """
        self._history.append(result)

    def _unknown_tool_result(self, name: str) -> ToolResult:
        """生成"未知工具"的错误结果。

        Args:
            name: 工具名称。

        Returns:
            ToolResult.fail(...)，error_type=UNKNOWN_TOOL。
        """
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
