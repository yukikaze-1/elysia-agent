# get_runtime_config 增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_runtime_config` 从 6 行稀疏输出增强为支持 query 过滤的多 section 运行时报告，让 LLM 清楚知道自己的能力/权限/限制。

**Architecture:** 4 文件改动——manager.py 扩展 plugin_list safety 字段 → agent.py 注入 ToolManager/TaskSystem/启动时间 → plugin.py 重写 execute() 拆为 7 个 section 构建函数 + query 路由 → 7 个新测试。

**Tech Stack:** Python 3.13, Pydantic, asyncio

**Design spec:** `docs/superpowers/specs/2026-06-22-runtime-config-enhance-design.md`

## Global Constraints

- 不改变 `RuntimeConfigPlugin` 的公开属性（name / description / param_model / capabilities / safety）
- 不改变 `get_self_state` 的任何行为
- 不改变 SafetyProfile / SafetyPipeline 的任何逻辑
- `get_runtime_config` 只读，不产生副作用
- 所有 ToolContext 改动向后兼容——缺失新增 key 时优雅降级，不崩溃
- `plugin_list` safety 子字典仅新增字段，不删除/重命名现有字段
- 保持现有 5 个测试通过

---

### Task 1: 扩展 `ToolManager.plugin_list` safety 字段

**Files:**
- Modify: `Module/tool/manager.py:311-326`

**Interfaces:**
- Produces: `plugin_list` 属性返回的 dict 中 `safety` 子字典新增 10 个字段（read_dirs, write_dir, max_file_bytes, allow_symlinks, max_dir_depth, block_private_ips, max_redirects, max_response_bytes, request_timeout, database_allowed）

- [ ] **Step 1: 修改 `plugin_list` 属性**

在 `Module/tool/manager.py` 第 311-326 行，将 `plugin_list` property 的 safety 子字典扩展为 14 个字段：

```python
@property
def plugin_list(self) -> list[dict]:
    return [
        {
            "name": p.name,
            "description": p.description,
            "capabilities": [c.value for c in p.capabilities],
            "safety": {
                # 现有 4 字段 — 保持不变
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
```

- [ ] **Step 2: 验证现有测试仍然通过**

```bash
conda activate Elysia && python -m pytest core/tools/runtime_config/test_runtime_config.py -v
```

预期：5 passed（现有测试不检查 safety 字段内容，不受影响）

- [ ] **Step 3: 快速验证 plugin_list 返回新字段**

```bash
conda activate Elysia && python -c "
from Module.tool.manager import ToolManager
# 检查属性签名——不需要加载插件即可验证字段存在
import inspect
src = inspect.getsource(ToolManager.plugin_list.fget)
assert 'read_dirs' in src
assert 'write_dir' in src
assert 'block_private_ips' in src
assert 'database_allowed' in src
print('OK: 所有新字段已添加')
"
```

- [ ] **Step 4: Commit**

```bash
git add Module/tool/manager.py
git commit -m "feat: plugin_list safety 子字典扩展 10 个字段（文件系统/网络/数据库）"
```

---

### Task 2: agent.py 注入新 services + 修复 _tool_names 时机

**Files:**
- Modify: `core/agent.py:1-20` (imports)
- Modify: `core/agent.py:144-146` (start() 开头)
- Modify: `core/agent.py:215-230` (_start_tool_context)
- Modify: `core/agent.py:262-288` (_start_task_system)

**Interfaces:**
- Produces: `ToolContext.services` 新增 4 个 key：`_tool_manager`, `_started_at`, `_token_tracker`, `_task_system`
- Fixes: `_tool_names` 和 `_tool_count` 在 `load_all()` 之后更新，不再为空

- [ ] **Step 1: 添加 `import time`**

在 `core/agent.py` 第 11 行 `import asyncio` 之后插入：

```python
import asyncio
import time                # 新增：启动时刻打点
import os
```

- [ ] **Step 2: 在 `start()` 开头打点**

在 `core/agent.py` 第 146 行 `self._state = RunState.RUNNING` 之后插入：

```python
async def start(self) -> None:
    """Start the agent: connect persistence, idle monitor, event loop."""
    self._state = RunState.RUNNING
    self._started_at = time.monotonic()           # 新增：尽早打点，用于运行时长显示

    # Reset FSM — may be stuck from a previous run that was interrupted
    self._fsm.reset()
```

- [ ] **Step 3: `_start_tool_context()` 注入 ToolManager + 修复 _tool_names 时机**

将 `core/agent.py` 第 215-237 行的 `_start_tool_context()` 修改为：

```python
async def _start_tool_context(self) -> None:
    """注入 ToolContext，加载工具插件，设置 HITL broadcast。"""
    self._tool_ctx = ToolContext(
        session_id=getattr(self._session, "_session_id", "default"),
        user_id=self._user_name,
        services={
            "memory": self._long_term,
            "scheduler": self._scheduler,
            "config": self._config,
            "session": self._session,
            "_tool_manager": self._tool_manager,          # 新增：ToolManager 实例
            "_started_at": self._started_at,               # 新增：启动时刻
            "_token_tracker": self._token_tracker,         # 新增：TokenTracker 实例
            "_tool_names": list(self._tool_manager.tool_names),
            "_fsm_state": str(self._fsm.state),
            "_session_msg_count": len(self._session._messages),
            "_tool_count": len(self._tool_manager._plugins),
        },
    )
    self._cognitive.set_tool_context(self._tool_ctx)

    self._plugin_ctx.session_id = getattr(self._session, "_session_id", "default")
    self._plugin_ctx.memory = self._long_term
    self._plugin_ctx.scheduler = self._scheduler
    await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
    # 修复：load_all 后更新工具名列表（之前在此处快照时 _plugins 为空）
    self._tool_ctx.services["_tool_names"] = list(self._tool_manager.tool_names)
    self._tool_ctx.services["_tool_count"] = len(self._tool_manager._plugins)
    self._tool_manager.set_hitl_broadcast(self._output)
    self._scheduler.set_tool_context(self._tool_ctx)
```

- [ ] **Step 4: `_start_task_system()` 注入 TaskSystem 到 services**

在 `core/agent.py` 第 286 行 `await self._task_system.start()` 之后插入：

```python
async def _start_task_system(self) -> None:
    """创建 TaskSystem + DelegationPreparer + HITLNotifier，注入 CognitiveLoop。"""
    from Module.task_system.task_system import TaskSystem
    from Module.task_system.delegation_preparer import DelegationPreparer
    from Module.task_system.infra import AgentInfra
    from Module.task_system.schema import ConstraintConfig

    _ts_infra = AgentInfra(
        llm=self._llm.get_raw_client(),
        milvus=None,
        http=None,
        tool_registry=self._tool_manager,
        constraints=ConstraintConfig(),
    )

    class _EventBusHITLNotifier:
        def __init__(self, bus):
            self._bus = bus
        async def request_approval(self, event_data):
            pass  # v1: 审批由 ToolManager SafetyPipeline 直接处理
        async def cancel_approval(self, approval_ids):
            pass  # v1: 同上

    _hitl = _EventBusHITLNotifier(self._bus)
    self._task_system = TaskSystem(infra=_ts_infra, hitl_notifier=_hitl)
    await self._task_system.start()
    # 新增：注入 TaskSystem 到 ToolContext，供 get_runtime_config 查询活跃任务
    self._tool_ctx.services["_task_system"] = self._task_system
    self._delegation_preparer = DelegationPreparer()
```

剩余代码（DelegationPreparer + CognitiveLoop 注入）保持不变。

- [ ] **Step 5: 验证 import 和语法正确**

```bash
conda activate Elysia && python -c "from core.agent import Agent; print('import OK')"
```

- [ ] **Step 6: Commit**

```bash
git add core/agent.py
git commit -m "feat: agent.py 注入 ToolManager/TaskSystem/启动时间到 ToolContext，修复 tool_names 时机"
```

---

### Task 3: 重写 `get_runtime_config` 插件

**Files:**
- Modify: `core/tools/runtime_config/plugin.py` (75 行 → ~200 行)

**Interfaces:**
- Consumes: `ctx.get("_tool_manager")` → ToolManager（Task 2 注入），`ctx.get("_task_system")` → TaskSystem | None（Task 2 注入），`ctx.get("_started_at")` → float | None（Task 2 注入），`ctx.get("config")` → AppConfig | None（已有）
- Produces: 字符串输出，按 query 参数选择 section

- [ ] **Step 1: 完整重写 `plugin.py`**

用以下代码替换 `core/tools/runtime_config/plugin.py` 的全部内容：

```python
"""运行时配置工具插件 — 查看 agent 配置、能力、权限和限制。"""

import asyncio
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
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
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: RuntimeConfigParams,
    ) -> str:
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
        llm_cfg = getattr(config, "llm", None)
        tts_cfg = getattr(config, "tts", None)
        stt_cfg = getattr(config, "stt", None)
        server_cfg = getattr(config, "server", None)

        if llm_cfg:
            lines.append(f"模型: {getattr(llm_cfg, 'model', 'unknown')}")
        if tts_cfg or stt_cfg:
            parts = []
            if tts_cfg:
                tts_model = getattr(tts_cfg, 'model', '')
                parts.append(
                    f"TTS: {'启用' if getattr(tts_cfg, 'enabled', False) else '禁用'}"
                    + (f" ({tts_model})" if tts_model else "")
                )
            if stt_cfg:
                stt_model = getattr(stt_cfg, 'model', '')
                parts.append(
                    f"STT: {'启用' if getattr(stt_cfg, 'enabled', False) else '禁用'}"
                    + (f" ({stt_model})" if stt_model else "")
                )
            lines.append("  |  ".join(parts))
        if server_cfg:
            lines.append(
                f"服务地址: {getattr(server_cfg, 'host', '?')}:{getattr(server_cfg, 'port', '?')}"
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

        # 获取文件系统工具名
        fs_tools = [p["name"] for p in plugins if p.get("safety", {}).get("filesystem_allowed")]

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
        if fs_tools:
            lines.append("")
            lines.append("  可用操作:")
            for p in plugins:
                if p.get("safety", {}).get("filesystem_allowed"):
                    name = p["name"]
                    desc = p.get("description", "")
                    approval = " 🔒" if p.get("safety", {}).get("requires_human_approval") else ""
                    lines.append(f"    {name}{approval}  — {desc}")
        return "\n".join(lines) if lines else ""

    def _section_network(self, ctx: ToolContext) -> str:
        """聚合所有工具的网络权限。"""
        plugins = self._get_plugin_list(ctx)
        net_plugins = [p for p in plugins if p.get("safety", {}).get("network_allowed")]
        if not net_plugins:
            return "🌐 网络: 禁止"
        
        # 聚合参数（取最严格的值）
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

    def _section_tools_detail(self, ctx: ToolContext) -> str:
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
                timeout = safety.get("request_timeout", 0)
                if timeout:
                    tags.append(f"超时:{timeout:.0f}s")
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
            return "📋 后台任务: TaskSystem 不可用"

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
            if hasattr(t, 'created_at') and t.created_at:
                from Module.task_system.schema import parse_iso
                try:
                    created = parse_iso(str(t.created_at))
                    elapsed = int((datetime.now(timezone.utc) - created).total_seconds())
                    m, s = divmod(elapsed, 60)
                    duration = f"已运行 {m}m{s:02d}s"
                except Exception:
                    pass
            lines.append(f"  {task_id} — {status}" + (f"  — \"{intent}\"" if intent else "") + (f"  — {duration}" if duration else ""))
        return "\n".join(lines)

    async def _section_token_usage(self, ctx: ToolContext) -> str:
        """Token 消耗（如果有 token_tracker）。"""
        tracker = ctx.get("_token_tracker")
        if tracker is None:
            return ""
        try:
            input_tokens = getattr(tracker, 'input_tokens', 0)
            output_tokens = getattr(tracker, 'output_tokens', 0)
            total = input_tokens + output_tokens
            if total == 0:
                return ""
            return (
                f"💾 Token 消耗: "
                f"输入 {input_tokens / 1000:.1f}K / "
                f"输出 {output_tokens / 1000:.1f}K / "
                f"合计 {total / 1000:.1f}K"
            )
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
        sections.append("使用 query 参数查看详情: \"tools\" | \"filesystem\" | \"network\" | \"tasks\" | \"all\"")
        return "\n".join(s for s in sections if s)

    def _render_tools_detail(self, ctx: ToolContext) -> str:
        return self._section_tools_detail(ctx)

    def _render_filesystem(self, ctx: ToolContext) -> str:
        return self._section_filesystem(ctx)

    def _render_network(self, ctx: ToolContext) -> str:
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
            self._section_tools_detail(ctx),
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
```

注意：文件顶部需要新增 `import time` 和 `from datetime import datetime, timezone`：

```python
import asyncio
import time
from datetime import datetime, timezone
from pydantic import BaseModel, Field
```

- [ ] **Step 2: 运行现有测试确认向后兼容**

```bash
conda activate Elysia && python -m pytest core/tools/runtime_config/test_runtime_config.py -v
```

现有 5 个测试可能因输出格式变化而失败——记录失败情况，将在 Task 4 中更新测试。

- [ ] **Step 3: 手动验证 query 路由（无 ToolManager 降级路径）**

```bash
conda activate Elysia && python -c "
import asyncio
from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
from Module.tool.protocol import ToolContext

async def test():
    plugin = RuntimeConfigPlugin()
    ctx = ToolContext(session_id='test', user_id='test', trace_id='test', services={})
    
    # 默认摘要——无 ToolManager 时优雅降级
    result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams())
    print('=== 默认摘要 ===')
    print(result)
    print()
    
    # query=tools
    result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams(query='tools'))
    print('=== query=tools ===')
    print(result)
    print()
    
    # query=all
    result = await plugin.execute(ctx=ctx, params=RuntimeConfigParams(query='all'))
    print('=== query=all ===')
    print(result)
    
    print('OK: 所有路径无崩溃')

asyncio.run(test())
"
```

- [ ] **Step 4: Commit**

```bash
git add core/tools/runtime_config/plugin.py
git commit -m "feat: get_runtime_config 重写——支持 query 过滤 + 7 个 section 构建函数"
```

---

### Task 4: 更新 + 新增测试

**Files:**
- Modify: `core/tools/runtime_config/test_runtime_config.py`

**Interfaces:**
- Consumes: Task 1 的扩展 `plugin_list`，Task 2 的 services 注入，Task 3 的新 `execute()` 签名
- Produces: 12 个测试（5 个旧 + 7 个新），覆盖所有 query 值和降级路径

- [ ] **Step 1: 完整重写测试文件**

用以下代码替换 `core/tools/runtime_config/test_runtime_config.py` 的全部内容：

```python
"""RuntimeConfigPlugin 单元测试。"""

import pytest
from unittest.mock import MagicMock

from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
from Module.tool.protocol import ToolContext


def _make_plugin_list() -> list[dict]:
    """构建模拟 plugin_list——含文件系统/网络/数据库/普通工具各一个。"""
    return [
        {
            "name": "read_file",
            "description": "读取文件内容",
            "capabilities": ["filesystem:read"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": True,
                "read_dirs": ["/home/yomu/data/sandbox"],
                "write_dir": "",
                "max_file_bytes": 10_485_760,
                "allow_symlinks": False,
                "max_dir_depth": 3,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "write_file",
            "description": "写入/创建文件",
            "capabilities": ["filesystem:write"],
            "safety": {
                "requires_human_approval": True,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": True,
                "read_dirs": None,
                "write_dir": "data/sandbox/{session_id}",
                "max_file_bytes": 10_485_760,
                "allow_symlinks": False,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "web_search",
            "description": "搜索网页",
            "capabilities": ["network", "web_search"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 3,
                "network_allowed": True,
                "filesystem_allowed": False,
                "read_dirs": None,
                "write_dir": "",
                "max_file_bytes": 0,
                "allow_symlinks": True,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 5_242_880,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "search_memory",
            "description": "搜索长期记忆",
            "capabilities": ["database:read"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": False,
                "read_dirs": None,
                "write_dir": "",
                "max_file_bytes": 0,
                "allow_symlinks": True,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": True,
            },
            "active": True,
        },
    ]


class TestRuntimeConfigPlugin:
    """RuntimeConfigPlugin 的测试用例。"""

    @pytest.fixture
    def plugin(self) -> RuntimeConfigPlugin:
        return RuntimeConfigPlugin()

    @pytest.fixture
    def base_ctx(self) -> ToolContext:
        """创建基础 ToolContext，包含 ToolManager mock。"""
        tm = MagicMock()
        tm.plugin_list = _make_plugin_list()

        config = MagicMock()
        config.llm.model = "deepseek/deepseek-chat"
        config.tts.enabled = True
        config.tts.model = "gpt-sovits"
        config.stt.enabled = False
        config.server.host = "127.0.0.1"
        config.server.port = 8080

        return ToolContext(
            session_id="test-session",
            user_id="test-user",
            trace_id="test-trace",
            services={
                "config": config,
                "_tool_manager": tm,
                "_started_at": 1000.0,
            },
        )

    # ── 已有测试（适配新输出格式）────────────────────────────

    @pytest.mark.asyncio
    async def test_no_config_service_shows_capability_info(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx 中没有 config 时，仍显示工具、文件系统、网络等能力信息。"""
        base_ctx.services.pop("config", None)
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        # 有 ToolManager → 显示工具列表
        assert "已加载工具" in result
        assert "read_file" in result
        # 不应有模型/TTS/STT（没有 config 服务）
        assert "模型:" not in result
        assert "TTS:" not in result

    @pytest.mark.asyncio
    async def test_no_tool_manager_shows_empty_tools(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx 中没有 _tool_manager 时，工具列表为空。"""
        base_ctx.services.pop("_tool_manager", None)
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)
        assert "已加载工具 (0)" in result

    @pytest.mark.asyncio
    async def test_full_config_shows_all_sections(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """默认摘要包含模型、文件系统、网络、工具、进程信息。"""
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "deepseek/deepseek-chat" in result
        assert "TTS: 启用 (gpt-sovits)" in result
        assert "STT: 禁用" in result
        assert "127.0.0.1:8080" in result
        assert "已加载工具 (4)" in result
        assert "read_file" in result
        # 文件系统权限概要
        assert "📁 文件系统" in result
        assert "/home/yomu/data/sandbox" in result
        # 网络权限概要
        assert "🌐 网络" in result

    @pytest.mark.asyncio
    async def test_none_sections_does_not_crash(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 config sections 为 None 时，不崩溃。"""
        config = MagicMock()
        config.llm = None
        config.tts = None
        config.stt = None
        config.server = None
        base_ctx.services["config"] = config

        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)
        # 不崩溃，工具列表正常
        assert "已加载工具 (4)" in result
        assert "模型:" not in result

    @pytest.mark.asyncio
    async def test_no_tool_manager_graceful(
        self, plugin: RuntimeConfigPlugin,
    ) -> None:
        """ctx 无 _tool_manager 时优雅降级，不崩溃。"""
        ctx = ToolContext(
            session_id="test", user_id="test", trace_id="test",
            services={},
        )
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=ctx, params=params)
        assert "已加载工具 (0)" in result

    # ── 新增测试：query 参数 ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_query_tools_shows_details(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='tools' 返回每个工具的 name + description + 能力标签。"""
        params = RuntimeConfigParams(query="tools")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "已加载工具 (4)" in result
        assert "read_file" in result
        assert "读取文件内容" in result
        assert "write_file" in result
        assert "写入/创建文件" in result
        assert "🔒审批" in result  # write_file 需要审批
        assert "web_search" in result
        assert "搜索网页" in result
        assert "并发:3" in result

    @pytest.mark.asyncio
    async def test_query_filesystem_shows_dirs(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='filesystem' 聚合展示 read_dirs + write_dir。"""
        params = RuntimeConfigParams(query="filesystem")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "📁 文件系统权限" in result
        assert "/home/yomu/data/sandbox" in result
        assert "max_depth=3" in result
        assert "禁止符号链接" in result
        # write_dir 模板已解析
        assert "data/sandbox/test-session" in result
        assert "需审批" in result
        # 文件系统工具列表
        assert "read_file" in result
        assert "write_file" in result

    @pytest.mark.asyncio
    async def test_query_network_shows_restrictions(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='network' 展示网络限制参数。"""
        params = RuntimeConfigParams(query="network")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "🌐 网络权限" in result
        assert "允许出站请求: 是" in result
        assert "私有IP过滤: 启用" in result
        assert "最大重定向: 5" in result
        assert "请求超时: 30" in result
        assert "web_search" in result
        assert "最多3并发" in result

    @pytest.mark.asyncio
    async def test_query_all_returns_all_sections(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='all' 包含所有 section。"""
        params = RuntimeConfigParams(query="all")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "deepseek/deepseek-chat" in result     # model
        assert "📁 文件系统权限" in result              # filesystem
        assert "🌐 网络权限" in result                  # network
        assert "已加载工具 (4)" in result               # tools detail
        assert "📊 进程" in result                      # process
        # 不应包含 query 提示（那是摘要专用的）
        assert "使用 query 参数查看详情" not in result

    @pytest.mark.asyncio
    async def test_default_summary_has_query_hint(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """无 query 参数时，摘要末尾提示可用 query 值。"""
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "使用 query 参数查看详情" in result
        assert '"tools"' in result
        assert '"filesystem"' in result
        assert '"network"' in result
        assert '"tasks"' in result
        assert '"all"' in result

    @pytest.mark.asyncio
    async def test_query_tasks_no_task_system(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='tasks' 但 TaskSystem 不可用时优雅降级。"""
        base_ctx.services.pop("_task_system", None)
        params = RuntimeConfigParams(query="tasks")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "TaskSystem 不可用" in result

    # ── RuntimeConfigParams ──────────────────────────────────────

    def test_params_default(self) -> None:
        """RuntimeConfigParams 默认 query=None。"""
        params = RuntimeConfigParams()
        assert params.query is None

    def test_params_with_query(self) -> None:
        """RuntimeConfigParams 接受 query 参数。"""
        params = RuntimeConfigParams(query="tools")
        assert params.query == "tools"
```

- [ ] **Step 2: 运行全部新测试**

```bash
conda activate Elysia && python -m pytest core/tools/runtime_config/test_runtime_config.py -v
```

预期：12 passed（5 个旧 + 7 个新）

- [ ] **Step 3: Commit**

```bash
git add core/tools/runtime_config/test_runtime_config.py
git commit -m "test: get_runtime_config 新增 7 个测试（query 路由 + 降级 + section 覆盖）"
```

---

## 验证

```bash
conda activate Elysia

# 1. runtime_config 全部测试
python -m pytest core/tools/runtime_config/test_runtime_config.py -v

# 2. agent import 正常
python -c "from core.agent import Agent; print('import OK')"

# 3. self_state 不受影响
python -c "from core.tools.self_state.plugin import SelfStatePlugin; print('OK')"

# 4. ToolManager plugin_list 兼容
python -c "
from Module.tool.manager import ToolManager
tm = ToolManager()
# 无插件时 plugin_list 返回空列表
assert tm.plugin_list == []
print('OK')
"
```
