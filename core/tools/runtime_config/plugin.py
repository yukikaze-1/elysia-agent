"""运行时配置工具插件 — 查看 agent 配置、能力、权限和限制。"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class RuntimeConfigParams(BaseModel):
    query: str | None = Field(
        default=None,
        description=(
            "可选：查询特定项。"
            "'tools'=工具详情, 'filesystem'=文件系统权限, "
            "'network'=网络权限, 'tasks'=活跃后台任务, "
            "'all'=全部信息。留空返回摘要。"
        ),
    )


class RuntimeConfigPlugin(ToolPlugin):
    name = "get_runtime_config"
    description = "查看当前运行时配置：模型、TTS/STT、文件/网络权限、工具列表、后台任务、内存使用"
    param_model = RuntimeConfigParams
    capabilities = {Capability.PROCESS}
    risk_level = "medium"  # 读进程内存/服务配置/后台任务状态（§3.3）
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: RuntimeConfigParams,
    ) -> str:
        """查看当前运行时配置。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 查询参数，可指定要查看的配置项。

        Returns:
            运行时配置信息字符串。
        """
        query = (params.query or "").strip().lower()

        if query == "tools":
            return self._render_tools_detail(ctx)
        elif query == "filesystem":
            return self._render_filesystem(ctx)
        elif query == "network":
            return self._render_network(ctx)
        elif query == "tasks":
            return await self._render_tasks_detail(ctx)
        elif query == "all":
            return await self._render_all(ctx)
        else:
            return await self._render_summary(ctx)

    # ── Section 构建函数 ─────────────────────────────────────────────

    def _section_model(self, ctx: ToolContext) -> str:
        """模型 / TTS / STT / 服务地址。"""
        config = ctx.get("config")
        if config is None:
            return ""
        lines = []
        # config 恒为 AppConfig（上句已排除 None），字段恒存在——直接访问暴露改名 bug
        llm_cfg = config.llm
        tts_cfg = config.tts
        stt_cfg = config.stt
        server_cfg = config.server

        if llm_cfg:
            lines.append(f"模型: {llm_cfg.model}")
        if tts_cfg or stt_cfg:
            parts = []
            if tts_cfg:
                tts_model = tts_cfg.model
                parts.append(
                    f"TTS: {'启用' if tts_cfg.enabled else '禁用'}"
                    + (f" ({tts_model})" if tts_model else "")
                )
            if stt_cfg:
                stt_model = stt_cfg.model
                parts.append(
                    f"STT: {'启用' if stt_cfg.enabled else '禁用'}"
                    + (f" ({stt_model})" if stt_model else "")
                )
            lines.append("  |  ".join(parts))
        if server_cfg:
            lines.append(
                f"服务地址: {server_cfg.host}:{server_cfg.port}"
            )
        return "\n".join(lines)

    def _section_filesystem(self, ctx: ToolContext) -> str:
        """聚合所有工具的读/写目录权限。"""
        plugins = self._get_plugin_list(ctx)
        read_dirs: set[str] = set()
        write_dir: str = ""
        write_approval = False
        max_depth = 0
        allow_symlinks = True
        max_file_bytes = 0

        for p in plugins:
            s = p.get("safety", {})
            if not s.get("filesystem_allowed"):
                continue
            for rd in (s.get("read_dirs") or []):
                read_dirs.add(rd)
            if s.get("write_dir"):
                session_id = ctx.session_id or "default"
                write_dir = s["write_dir"].replace("{session_id}", session_id)
                if s.get("requires_human_approval"):
                    write_approval = True
            max_depth = max(max_depth, s.get("max_dir_depth", 0))
            if not s.get("allow_symlinks", True):
                allow_symlinks = False
            max_file_bytes = max(max_file_bytes, s.get("max_file_bytes", 0))

        lines = ["📁 文件系统权限:", ""]
        if read_dirs:
            lines.append("  读取目录:")
            for d in sorted(read_dirs):
                restrictions = []
                if max_depth > 0:
                    restrictions.append(f"max_depth={max_depth}")
                if not allow_symlinks:
                    restrictions.append("禁止符号链接")
                if max_file_bytes > 0:
                    restrictions.append(f"max {max_file_bytes // (1024*1024)}MB/文件")
                lines.append(f"    {d}" + (f"  ({', '.join(restrictions)})" if restrictions else ""))
        if write_dir:
            restrictions = []
            if write_approval:
                restrictions.append("需审批 🔒")
            if max_file_bytes > 0:
                restrictions.append(f"max {max_file_bytes // (1024*1024)}MB/文件")
            lines.append("")
            lines.append("  写入目录:")
            lines.append(f"    {write_dir}" + (f"  ({', '.join(restrictions)})" if restrictions else ""))
        # 文件系统工具列表
        fs_tools = [p for p in plugins if p.get("safety", {}).get("filesystem_allowed")]
        if fs_tools:
            lines.append("")
            lines.append("  可用操作:")
            for p in fs_tools:
                name = p["name"]
                desc = p.get("description", "")
                approval = " 🔒" if p.get("safety", {}).get("requires_human_approval") else ""
                lines.append(f"    {name}{approval}  — {desc}")
        return "\n".join(lines) if len(lines) > 2 else ""

    def _section_network(self, ctx: ToolContext) -> str:
        """聚合所有工具的网络权限。"""
        plugins = self._get_plugin_list(ctx)
        net_plugins = [p for p in plugins if p.get("safety", {}).get("network_allowed")]
        if not net_plugins:
            return "🌐 网络: 禁止"

        block_private = any(p.get("safety", {}).get("block_private_ips", True) for p in net_plugins)
        max_redirects = max((p.get("safety", {}).get("max_redirects", 0) for p in net_plugins), default=0)
        max_resp = max((p.get("safety", {}).get("max_response_bytes", 0) for p in net_plugins), default=0)
        timeout = max((p.get("safety", {}).get("request_timeout", 0) for p in net_plugins), default=0)

        lines = ["🌐 网络权限:", ""]
        lines.append("  允许出站请求: 是")
        lines.append(f"  私有IP过滤: {'启用' if block_private else '禁用'}"
                     f"{' (禁止 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)' if block_private else ''}")
        if max_redirects > 0:
            lines.append(f"  最大重定向: {max_redirects} 次")
        if max_resp > 0:
            lines.append(f"  最大响应体: {max_resp // (1024*1024)} MB")
        if timeout > 0:
            lines.append(f"  请求超时: {timeout:.0f} 秒")
        lines.append("")
        lines.append("  可用操作:")
        for p in net_plugins:
            name = p["name"]
            desc = p.get("description", "")
            concurrency = p.get("safety", {}).get("max_concurrency", 0)
            extra = f", 最多{concurrency}并发" if concurrency > 0 else ""
            lines.append(f"    {name}  — {desc}{extra}")
        return "\n".join(lines)

    def _section_tools_summary(self, ctx: ToolContext) -> str:
        """工具名列表（一行）。"""
        plugins = self._get_plugin_list(ctx)
        names = [p["name"] for p in plugins]
        return f"📦 已加载工具 ({len(names)}): {', '.join(names)}"

    def _render_tools_detail(self, ctx: ToolContext) -> str:
        """工具详情——每工具一行：名称 + 描述 + 能力 + 安全标记。"""
        plugins = self._get_plugin_list(ctx)
        if not plugins:
            return "📦 已加载工具 (0)"

        lines = [f"📦 已加载工具 ({len(plugins)}):", ""]
        for p in plugins:
            name = p["name"]
            desc = p.get("description", "")
            caps = [c.split(":")[-1] if ":" in c else c for c in p.get("capabilities", [])]
            cap_str = ",".join(caps) if caps else ""
            safety = p.get("safety", {})
            tags = []
            if safety.get("requires_human_approval"):
                tags.append("🔒审批")
            if safety.get("max_concurrency", 0) > 0:
                tags.append(f"并发:{safety['max_concurrency']}")
            if safety.get("filesystem_allowed"):
                rd = safety.get("read_dirs") or []
                if rd:
                    tags.append(f"读:{rd[0]}")
            if safety.get("network_allowed"):
                t = safety.get("request_timeout", 0)
                if t:
                    tags.append(f"超时:{t:.0f}s")
            tag_str = ("  " + ", ".join(tags)) if tags else ""
            cap_part = f"  [{cap_str}]" if cap_str else ""
            lines.append(f"  {name:<18} — {desc}{cap_part}{tag_str}")
        return "\n".join(lines)

    async def _section_process(self, ctx: ToolContext) -> str:
        """内存 / 线程 / 运行时长。"""
        parts = []
        if HAS_PSUTIL:
            def _get_memory():
                proc = psutil.Process()
                mem = proc.memory_info()
                return mem, proc.num_threads()
            try:
                mem, threads = await asyncio.to_thread(_get_memory)
                parts.append(
                    f"RSS={mem.rss / 1024 / 1024:.1f}MB, "
                    f"VMS={mem.vms / 1024 / 1024:.1f}MB, 线程={threads}"
                )
            except Exception:
                pass

        started_at = ctx.get("_started_at")
        if started_at is not None:
            elapsed = int(time.monotonic() - started_at)
            h, m = divmod(elapsed, 3600)
            m, s = divmod(m, 60)
            if h > 0:
                time_str = f"{h}h{m:02d}m"
            elif m > 0:
                time_str = f"{m}m{s:02d}s"
            else:
                time_str = f"{s}s"
            parts.append(f"运行 {time_str}")

        if parts:
            return "📊 进程: " + ", ".join(parts)
        return ""

    async def _section_tasks_summary(self, ctx: ToolContext) -> str:
        """活跃任务数量（一行）。"""
        ts = ctx.get("_task_system")
        if ts is None:
            return ""
        try:
            active = await ts.list_active_tasks()
            return f"📋 活跃后台任务: {len(active)} 个"
        except Exception:
            return ""

    async def _section_tasks_detail(self, ctx: ToolContext) -> str:
        """活跃任务详情——每个任务一行。"""
        ts = ctx.get("_task_system")
        if ts is None:
            return "📋 后台任务: WorkflowEngine 不可用"

        try:
            active = await ts.list_active_tasks()
        except Exception as e:
            return f"📋 后台任务: 查询失败 ({e})"

        if not active:
            return "📋 活跃后台任务: 0 个"

        lines = [f"📋 活跃后台任务 ({len(active)}):", ""]
        for t in active:
            task_id = str(t.task_id) if hasattr(t, 'task_id') else str(t.get("task_id", "?"))
            status = str(t.status) if hasattr(t, 'status') else str(t.get("status", "?"))
            intent = ""
            if hasattr(t, 'intent'):
                intent = str(t.intent or "")[:60]
            elif isinstance(t, dict):
                intent = str(t.get("intent", ""))[:60]
            duration = ""
            # dict 先行排除——pyright 对 Any 的 hasattr 会错误收窄为 dict[Unknown, Unknown]
            # （dict 无 created_at 属性、key 走 []），排除后 hasattr 命中对象路径（WorkflowSummary）
            if not isinstance(t, dict) and hasattr(t, 'created_at') and t.created_at:
                from Module.workflow_contract.utils import parse_iso
                try:
                    created = parse_iso(str(t.created_at))
                    elapsed = int((datetime.now(timezone.utc) - created).total_seconds())
                    m, s = divmod(elapsed, 60)
                    duration = f"已运行 {m}m{s:02d}s"
                except Exception:
                    pass
            lines.append(
                f"  {task_id} — {status}"
                + (f"  — \"{intent}\"" if intent else "")
                + (f"  — {duration}" if duration else "")
            )
        return "\n".join(lines)

    async def _section_token_usage(self, ctx: ToolContext) -> str:
        """Token 消耗（如果有 token_tracker）。"""
        tracker = ctx.get("_token_tracker")
        if tracker is None:
            return ""
        try:
            total = tracker.session_total
            if total == 0:
                return ""
            per_model = tracker.per_model
            parts = [f"合计 {total / 1000:.1f}K"]
            if per_model:
                model_parts = [f"{m}: {t / 1000:.1f}K" for m, t in per_model.items()]
                parts.append("(" + ", ".join(model_parts) + ")")
            return f"💾 Token 消耗: " + " ".join(parts)
        except Exception:
            return ""

    # ── 渲染入口 ─────────────────────────────────────────────────────

    async def _render_summary(self, ctx: ToolContext) -> str:
        """默认摘要——模型+文件系统概要+网络概要+工具列表+进程+任务数。"""
        sections = [
            self._section_model(ctx),
            "",
            self._section_filesystem(ctx),
            "",
            self._section_network(ctx),
            "",
            self._section_tools_summary(ctx),
            "",
            await self._section_process(ctx),
        ]
        tasks = await self._section_tasks_summary(ctx)
        if tasks:
            sections.append(tasks)
        token_section = await self._section_token_usage(ctx)
        if token_section:
            sections.append(token_section)
        sections.append("")
        sections.append(
            "使用 query 参数查看详情: \"tools\" | \"filesystem\""
            " | \"network\" | \"tasks\" | \"all\""
        )
        return "\n".join(s for s in sections if s)

    def _render_filesystem(self, ctx: ToolContext) -> str:
        """渲染文件系统权限详情（query="filesystem" 入口）。"""
        return self._section_filesystem(ctx)

    def _render_network(self, ctx: ToolContext) -> str:
        """渲染网络权限详情（query="network" 入口）。"""
        return self._section_network(ctx)

    async def _render_tasks_detail(self, ctx: ToolContext) -> str:
        return await self._section_tasks_detail(ctx)

    async def _render_all(self, ctx: ToolContext) -> str:
        """全量输出——所有 section。"""
        sections = [
            self._section_model(ctx),
            "",
            self._section_filesystem(ctx),
            "",
            self._section_network(ctx),
            "",
            self._render_tools_detail(ctx),
            "",
            await self._section_process(ctx),
        ]
        tasks = await self._section_tasks_detail(ctx)
        sections.append(tasks)
        token_section = await self._section_token_usage(ctx)
        if token_section:
            sections.append(token_section)
        return "\n".join(s for s in sections if s)

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _get_plugin_list(ctx: ToolContext) -> list[dict]:
        """从 ToolManager 获取插件列表，缺失时优雅降级返回空列表。"""
        tm = ctx.get("_tool_manager")
        if tm is None:
            return []
        try:
            return tm.plugin_list
        except Exception:
            return []
