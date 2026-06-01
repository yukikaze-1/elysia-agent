# Tool System Deep Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace @tool decorator + ToolRegistry with a Pydantic-based ToolPlugin architecture: SafetyPipeline middleware, typed DI, HITL approval, token-aware truncation, concurrency control, session-aware sandbox, hot reload, streaming progress, and 4 new tools.

**Architecture:** `Module/tool/` becomes a zero-dependency reusable framework; `core/tools/` holds Elysia-specific plugins. CognitiveLoop depends on the `ToolExecutor` Protocol (not ToolManager directly). ToolManager implements ToolExecutor, runs plugins through SafetyPipeline (validate → safety → HITL → execute → truncate).

**Tech Stack:** Python 3.13, Pydantic v2, asyncio, httpx, existing Module/* infrastructure.

---

## Phase 1: Framework Foundation (Module/tool/)

### Task 1: Expand ErrorType + add ToolProgress

**Files:**
- Modify: `Module/tool/protocol.py` (lines 24-37)

- [ ] **Step 1: Update ErrorType enum**

Replace the existing `ErrorType` at line 24-37 with:

```python
class ErrorType(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"    # retryable (fix args)
    TIMEOUT = "timeout"                      # retryable (narrow scope)
    PERMISSION_DENIED = "permission_denied"  # NEW — non-retryable
    RATE_LIMITED = "rate_limited"            # NEW — retryable (backoff)
    NETWORK_ERROR = "network_error"          # NEW — retryable (transient)
    UNAVAILABLE = "unavailable"              # backend down
    CANCELLED = "cancelled"
    TOOL_ERROR = "tool_error"                # internal, non-retryable
    UNKNOWN_TOOL = "unknown_tool"            # not registered

    @property
    def is_retryable(self) -> bool:
        return self in (
            ErrorType.TIMEOUT,
            ErrorType.RATE_LIMITED,
            ErrorType.NETWORK_ERROR,
        )
```

- [ ] **Step 2: Verify ErrorType change**

Run: `python -c "from Module.tool.protocol import ErrorType; print(list(ErrorType))"`
Expected: prints 9 values including PERMISSION_DENIED, RATE_LIMITED, NETWORK_ERROR

- [ ] **Step 3: Commit**

```bash
git add Module/tool/protocol.py
git commit -m "feat: expand ErrorType with PERMISSION_DENIED, RATE_LIMITED, NETWORK_ERROR; narrow is_retryable to TIMEOUT/RATE_LIMITED/NETWORK_ERROR"
```

---

### Task 2: Add Capability enum

**Files:**
- Create: `Module/tool/capability.py`

- [ ] **Step 1: Write capability.py**

```python
"""工具所需能力枚举 — 取代字符串 requires，支持能力可用性检测。"""

from enum import Enum


class Capability(Enum):
    DATABASE_READ = "database:read"
    DATABASE_WRITE = "database:write"
    NETWORK = "network"
    WEB_SEARCH = "web_search"
    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_WRITE = "filesystem:write"
    PROCESS = "process"
    SCHEDULE = "schedule"
```

- [ ] **Step 2: Verify import**

Run: `python -c "from Module.tool.capability import Capability; print(Capability.NETWORK.value)"`
Expected: `network`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/capability.py
git commit -m "feat: add Capability enum for structured tool capability declaration"
```

---

### Task 3: Add PluginContext dataclass

**Files:**
- Modify: `Module/tool/protocol.py` (add after ErrorType, before ToolContext)

- [ ] **Step 1: Add PluginContext to protocol.py**

Insert after the ErrorType block (after line 37):

```python
@dataclass
class PluginContext:
    """类型化的依赖注入容器 — 插件 setup() 时注入。"""
    session_id: str = ""
    # 数据库
    memory: Any = None             # LongTermMemory | None
    mongodb: Any = None            # DocumentDB | None
    milvus: Any = None             # MilvusClient | None
    # 网络
    http_client: Any = None        # httpx.AsyncClient | None
    # 调度
    scheduler: Any = None          # TaskScheduler | None
    # 配置
    config: Any = None             # AppConfig | None
```

Use `Any` type to avoid circular imports (actual types imported lazily by plugins).

- [ ] **Step 2: Verify import**

Run: `python -c "from Module.tool.protocol import PluginContext; pc = PluginContext(session_id='test'); print(pc.session_id)"`
Expected: `test`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/protocol.py
git commit -m "feat: add PluginContext dataclass for typed dependency injection"
```

---

### Task 4: Update ToolContext with trace_id + agent_id

**Files:**
- Modify: `Module/tool/protocol.py` (lines 44-75)

- [ ] **Step 1: Update ToolContext**

Replace the existing `ToolContext` class (lines 44-75):

```python
@dataclass
class ToolContext:
    """每次工具调用的运行时上下文 — 支持多 Agent 链路追踪。"""
    session_id: str = ""
    trace_id: str = ""             # NEW — 一次请求的全局追踪 ID
    agent_id: str = "default"      # NEW — 触发工具的 Agent 标识
    services: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)
```

- [ ] **Step 2: Verify**

Run: `python -c "from Module.tool.protocol import ToolContext; tc = ToolContext(session_id='s1', trace_id='t1', agent_id='a1'); print(tc.trace_id, tc.agent_id)"`
Expected: `t1 a1`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/protocol.py
git commit -m "feat: add trace_id and agent_id to ToolContext for multi-agent tracing"
```

---

### Task 5: Add ToolPlugin base class

**Files:**
- Modify: `Module/tool/protocol.py` (add after ToolDefinition, before ToolResult)

- [ ] **Step 1: Add the import for pydantic**

At the top of protocol.py, add:
```python
from pydantic import BaseModel
```
(if not already present — check existing imports)

- [ ] **Step 2: Add ToolPlugin base class**

Insert after the `ToolDefinition` class (after line 306):

```python
class ToolPlugin:
    """工具插件基类 — 每个工具一个子类。

    插件开发约束：
    - 模块级无状态（Stateless at Module Level）：所有持久化状态必须依托于
      PluginContext 或 self 属性，禁止模块级全局变量或静态类属性。
    - execute_stream 的 finally 块禁止耗时异步清理（如 HTTP 请求），
      此类清理应在 teardown() 中完成。
    """

    # 元数据（子类必须定义）
    name: str = ""
    description: str = ""
    param_model: type[BaseModel] | None = None

    # 能力与安全声明
    capabilities: set[Capability] = field(default_factory=set)
    safety: "SafetyProfile" = field(default_factory=lambda: SafetyProfile())

    # ---- 生命周期 ----

    async def setup(self, ctx: PluginContext) -> None:
        """初始化 — 检测能力可用性，失败则抛出异常（工具不注册）。"""

    async def teardown(self) -> None:
        """清理资源 — 关闭连接池、释放锁等。"""

    # ---- 执行 ----

    async def execute(
        self, *, ctx: ToolContext, emit: Any = None, params: BaseModel,
    ) -> Any:
        """执行工具 — params 是 Pydantic Model 实例。"""
        raise NotImplementedError

    async def execute_stream(
        self, *, ctx: ToolContext, emit: Any, params: BaseModel,
    ) -> AsyncIterator[Any]:
        """流式执行 — yield ToolProgress | ToolResult。"""
        raise NotImplementedError
        yield  # make this an async generator
```

Note: circular import of `Capability` and `SafetyProfile` handled by defining `SafetyProfile` below or using `from __future__ import annotations`.

- [ ] **Step 3: Add forward reference handling**

At the top of protocol.py (line 1), ensure `from __future__ import annotations` is present. The `safety` field uses `field(default_factory=lambda: SafetyProfile())` with a lambda to defer SafetyProfile evaluation until runtime, avoiding circular import issues.

- [ ] **Step 4: Verify import**

Run: `python -c "from Module.tool.protocol import ToolPlugin; print(ToolPlugin.__name__)"`
Expected: `ToolPlugin`

- [ ] **Step 5: Commit**

```bash
git add Module/tool/protocol.py
git commit -m "feat: add ToolPlugin base class with Pydantic params, lifecycle, streaming support"
```

---

### Task 6: Add SafetyProfile dataclass

**Files:**
- Modify: `Module/tool/safety.py` (prepend to existing content)

- [ ] **Step 1: Write SafetyProfile**

Add at the top of `Module/tool/safety.py` (before line 17):

```python
"""工具安全层 — SafetyProfile 声明 + SafetyPipeline 执行中间件。"""

from dataclasses import dataclass, field


@dataclass
class SafetyProfile:
    """每个插件声明自己的安全边界。"""

    # 网络
    network_allowed: bool = False
    block_private_ips: bool = True
    max_redirects: int = 5
    max_response_bytes: int = 5 * 1024 * 1024      # 5MB
    request_timeout: float = 30.0
    max_concurrency: int = 0                        # 0=不限

    # 文件系统
    filesystem_allowed: bool = False
    read_dirs: list[str] | None = None              # 读白名单
    write_dir: str = "data/sandbox/{session_id}"     # 写锁定目录（模板变量）
    max_file_bytes: int = 10 * 1024 * 1024           # 10MB
    allow_symlinks: bool = False
    max_dir_depth: int = 3

    # 数据库
    database_allowed: bool = False
    database_readonly: bool = True

    # 结果处理
    max_result_tokens: int = 2000
    truncate_strategy: str = "head_tail"             # "head_tail" | "head_only"

    # 安全审批
    requires_human_approval: bool = False
```

- [ ] **Step 2: Verify import**

Run: `python -c "from Module.tool.safety import SafetyProfile; sp = SafetyProfile(); print(sp.block_private_ips)"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/safety.py
git commit -m "feat: add SafetyProfile dataclass with network/fs/db/token/hitl settings"
```

---

### Task 7: Add SafetyPipeline

**Files:**
- Modify: `Module/tool/safety.py` (add after SafetyProfile, after line ~45)

- [ ] **Step 1: Add token estimation and Pydantic error formatting helpers**

Add to `Module/tool/safety.py`:

```python
import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from .protocol import ToolResult, ErrorType


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：英文 ~4 chars/token，中文 ~1 char/token。"""
    latin = len(re.findall(r"[a-zA-Z0-9\s\W]", text))
    cjk = len(text) - latin
    return int(latin / 4 + cjk)


def _format_validation_error(exc: ValidationError) -> str:
    """将 Pydantic ValidationError 精简为一行一句话（≤200 字符）。"""
    messages = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"]
        messages.append(f"字段 '{loc}'：{msg}")
    return "; ".join(messages)[:200]


def _truncate_text(
    text: str, max_tokens: int, strategy: str,
) -> tuple[str, bool]:
    """Token-aware 截断。返回 (文本, 是否截断)。"""
    est = _estimate_tokens(text)
    if est <= max_tokens:
        return text, False

    if strategy == "head_tail":
        head_ratio = 0.8
        head_chars = int(len(text) * head_ratio * (max_tokens / est))
        tail_chars = int(len(text) * 0.2 * (max_tokens / est))
        omitted = est - max_tokens
        return (
            text[:head_chars]
            + f"\n...[省略约{omitted} tokens]...\n"
            + text[-tail_chars:],
            True,
        )
    else:  # head_only
        chars = int(len(text) * (max_tokens / est))
        return text[:chars] + "\n...[截断]", True
```

- [ ] **Step 2: Add path safety check**

```python
def _validate_path(
    resolved: Path, profile: SafetyProfile, session_id: str,
) -> str | None:
    """路径安全检查。返回 None 表示通过，否则返回错误字符串。"""
    path_str = str(resolved)

    # 检查白名单目录
    if profile.read_dirs:
        allowed = any(
            path_str.startswith(str(Path(d).resolve()))
            for d in profile.read_dirs
        )
        if not allowed:
            return f"路径 '{path_str}' 不在允许的目录中"

    # Symbolic link 检查
    if not profile.allow_symlinks:
        for parent in [resolved, *resolved.parents]:
            if parent.is_symlink():
                return f"路径 '{path_str}' 包含符号链接，已被拒绝"

    # 文件大小预检
    if resolved.exists() and resolved.is_file():
        if resolved.stat().st_size > profile.max_file_bytes:
            return f"文件过大 ({resolved.stat().st_size} bytes)"

    return None


def _resolve_sandbox_path(
    path: str, profile: SafetyProfile, session_id: str, is_write: bool,
) -> tuple[Path, str | None]:
    """Sandbox 路径解析。返回 (resolved_path, error_or_none)。"""
    raw = Path(path)
    if not raw.is_absolute():
        if is_write:
            write_dir = profile.write_dir.replace("{session_id}", session_id)
            raw = Path(write_dir) / raw
        elif profile.read_dirs:
            raw = Path(profile.read_dirs[0]) / raw
    try:
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return raw, f"路径解析失败: {e}"

    # 写入限制在 write_dir
    if is_write:
        write_dir = Path(
            profile.write_dir.replace("{session_id}", session_id)
        ).resolve()
        if not str(resolved).startswith(str(write_dir)):
            return resolved, f"写入路径必须在 '{write_dir}' 下"

    return resolved, None


def _validate_url(url: str, profile: SafetyProfile) -> str | None:
    """URL 安全检查（内网 IP 过滤 + scheme 检查）。返回错误字符串或 None。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不支持的 URL scheme: {parsed.scheme}"
    if profile.block_private_ips and parsed.hostname:
        import ipaddress
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return f"禁止访问内网地址: {parsed.hostname}"
        except ValueError:
            pass
    return None
```

- [ ] **Step 3: Add SafetyPipeline class**

```python
class SafetyPipeline:
    """工具执行中间件管道：validate → safety → hitl → execute → truncate → record。"""

    def __init__(self) -> None:
        self._hitl_requests: dict[str, asyncio.Event] = {}
        self._hitl_results: dict[str, tuple[bool, str]] = {}
        self._hitl_lock = asyncio.Lock()
        self._hitl_timeout: float = 60.0
        self._stats: dict[str, int] = {
            "total": 0, "timeout": 0, "denied": 0, "rate_limited": 0,
        }

    # ---- HITL public API ----

    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """前端提交审批结果。返回 True 如果找到了对应的审批请求。"""
        if request_id not in self._hitl_requests:
            return False
        self._hitl_results[request_id] = (approved, "confirmed" if approved else "denied")
        self._hitl_requests[request_id].set()
        return True

    def request_exists(self, request_id: str) -> bool:
        return request_id in self._hitl_requests

    # ---- Pipeline execution ----

    async def execute(
        self,
        plugin: Any,               # ToolPlugin
        name: str,
        ctx: Any,                  # ToolContext
        raw_params: dict,
        semaphore: asyncio.Semaphore | None,
    ) -> Any:                      # ToolResult
        """执行完整管道（非流式）。"""
        start = asyncio.get_event_loop().time()
        self._stats["total"] += 1

        # 1. Pydantic 校验
        if plugin.param_model is not None:
            try:
                params = plugin.param_model(**raw_params)
            except ValidationError as e:
                self._stats["denied"] += 1
                return ToolResult.fail(
                    name=name,
                    error=_format_validation_error(e),
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
        else:
            params = None

        # 2. Safety check (URL, file paths)
        safety_err = await self._check_safety(plugin, ctx, raw_params)
        if safety_err:
            self._stats["denied"] += 1
            return ToolResult.fail(
                name=name, error=safety_err,
                error_type=ErrorType.PERMISSION_DENIED,
            )

        # 3. HITL approval
        if plugin.safety.requires_human_approval:
            hitl_ok, hitl_status = await self._run_hitl(plugin, name, raw_params)
            if not hitl_ok:
                return ToolResult.fail(
                    name=name,
                    error=f"操作被{'拒绝' if hitl_status == 'denied' else '超时' if hitl_status == 'timeout' else '过期'}",
                    error_type=ErrorType.PERMISSION_DENIED,
                )

        # 4. Acquire semaphore
        if semaphore is not None:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=10.0)
            except asyncio.TimeoutError:
                self._stats["rate_limited"] += 1
                return ToolResult.fail(
                    name=name, error="并发限制，请稍后重试",
                    error_type=ErrorType.RATE_LIMITED,
                )

        # 5. Execute with timeout
        try:
            try:
                raw = await asyncio.wait_for(
                    plugin.execute(ctx=ctx, params=params),
                    timeout=plugin.safety.request_timeout,
                )
            except asyncio.TimeoutError:
                self._stats["timeout"] += 1
                return ToolResult.fail(
                    name=name, error=f"执行超时 ({plugin.safety.request_timeout}s)",
                    error_type=ErrorType.TIMEOUT,
                )
            except asyncio.CancelledError:
                return ToolResult.fail(
                    name=name, error="用户取消",
                    error_type=ErrorType.CANCELLED,
                )
        except Exception as e:
            return ToolResult.fail(
                name=name, error=str(e),
                error_type=ErrorType.TOOL_ERROR,
            )
        finally:
            if semaphore is not None:
                semaphore.release()

        # 6. Wrap + truncate result
        return self._wrap_result(
            raw, name, asyncio.get_event_loop().time() - start,
            plugin.safety,
        )

    async def execute_stream(
        self,
        plugin: Any,
        name: str,
        ctx: Any,
        raw_params: dict,
        semaphore: asyncio.Semaphore | None,
    ) -> AsyncIterator[Any]:
        """执行流式管道。"""
        # Pre-checks (same as execute)
        if plugin.param_model is not None:
            try:
                params = plugin.param_model(**raw_params)
            except ValidationError as e:
                yield ToolResult.fail(
                    name=name, error=_format_validation_error(e),
                    error_type=ErrorType.INVALID_ARGUMENT,
                )
                return
        else:
            params = None

        safety_err = await self._check_safety(plugin, ctx, raw_params)
        if safety_err:
            yield ToolResult.fail(
                name=name, error=safety_err,
                error_type=ErrorType.PERMISSION_DENIED,
            )
            return

        if plugin.safety.requires_human_approval:
            hitl_ok, _ = await self._run_hitl(plugin, name, raw_params)
            if not hitl_ok:
                yield ToolResult.fail(
                    name=name, error="操作被拒绝/超时",
                    error_type=ErrorType.PERMISSION_DENIED,
                )
                return

        if semaphore is not None:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=10.0)
            except asyncio.TimeoutError:
                yield ToolResult.fail(
                    name=name, error="并发限制",
                    error_type=ErrorType.RATE_LIMITED,
                )
                return

        # Stream execution
        acc_text = ""
        max_tokens = plugin.safety.max_result_tokens
        gen = None
        try:
            gen = plugin.execute_stream(ctx=ctx, emit=ctx.get("_emit"), params=params)
            async for chunk in gen:
                if isinstance(chunk, ToolResult):
                    yield self._wrap_result(chunk, name, 0, plugin.safety)
                    return
                # ToolProgress — pass through
                if hasattr(chunk, "message"):
                    acc_text += str(getattr(chunk, "message", ""))
                else:
                    acc_text += str(chunk)
                # Stream truncation (head_only)
                if _estimate_tokens(acc_text) > max_tokens:
                    truncated, _ = _truncate_text(acc_text, max_tokens, "head_only")
                    yield ToolResult(
                        success=True, content=truncated, truncated=True,
                        tool_name=name, error_type=None,
                    )
                    if gen is not None:
                        await gen.aclose()
                    return
                yield chunk
        except asyncio.CancelledError:
            if gen is not None:
                await gen.aclose()
            yield ToolResult.fail(name=name, error="用户取消", error_type=ErrorType.CANCELLED)
        except Exception as e:
            yield ToolResult.fail(name=name, error=str(e), error_type=ErrorType.TOOL_ERROR)
        finally:
            if semaphore is not None:
                semaphore.release()

    # ---- Internal helpers ----

    async def _check_safety(
        self, plugin: Any, ctx: Any, raw_params: dict,
    ) -> str | None:
        """Check URL and file path safety. Returns error string or None."""
        profile = plugin.safety
        session_id = getattr(ctx, "session_id", "default") or "default"

        # URL check
        if "url" in raw_params and profile.network_allowed:
            err = _validate_url(raw_params["url"], profile)
            if err:
                return err

        # File path check
        if "path" in raw_params and profile.filesystem_allowed:
            raw_path = raw_params["path"]
            is_write = "write" in plugin.name.lower()
            resolved, err = _resolve_sandbox_path(
                raw_path, profile, session_id, is_write,
            )
            if err:
                return err
            err = _validate_path(resolved, profile, session_id)
            if err:
                return err

        return None

    async def _run_hitl(
        self, plugin: Any, name: str, params: dict,
    ) -> tuple[bool, str]:
        """HITL 审批流程。返回 (approved, status)。"""
        import uuid
        request_id = uuid.uuid4().hex[:12]
        event = asyncio.Event()
        self._hitl_requests[request_id] = event

        # 广播审批请求（通过 output manager — 由 ToolManager 注入的 callback）
        if hasattr(self, "_broadcast_hitl"):
            await self._broadcast_hitl(request_id, name, params)

        try:
            await asyncio.wait_for(event.wait(), timeout=self._hitl_timeout)
            result = self._hitl_results.pop(request_id, (False, "expired"))
            self._hitl_requests.pop(request_id, None)
            return result[0], result[1]
        except asyncio.TimeoutError:
            self._hitl_requests.pop(request_id, None)
            self._hitl_results.pop(request_id, None)
            return False, "timeout"

    def _wrap_result(
        self, raw: Any, name: str, duration_ms: float, profile: SafetyProfile,
    ) -> Any:
        """Wrap raw result into ToolResult with truncation + token truncation."""
        if isinstance(raw, ToolResult):
            result = raw
        else:
            result = ToolResult.ok(name=name, content=str(raw), duration_ms=duration_ms)

        # Token truncation
        if result.content and not result.truncated:
            content, was_truncated = _truncate_text(
                result.content, profile.max_result_tokens,
                profile.truncate_strategy,
            )
            if was_truncated:
                result.content = content
                result.truncated = True

        result.tool_name = name
        result.duration_ms = duration_ms
        return result

    # ---- Debug ----

    @property
    def stats(self) -> dict:
        return dict(self._stats)
```

> **Circular import note:** `ToolResult` and `ErrorType` are defined in `protocol.py`, which imports `SafetyProfile` from `safety.py`. To avoid circular imports, `ToolResult` and `ErrorType` must be imported lazily inside SafetyPipeline methods (not at module level). Use `from Module.tool.protocol import ToolResult, ErrorType` as the first line of `execute()`, `execute_stream()`, and `_wrap_result()`. Alternatively, move SafetyPipeline to its own file `Module/tool/safety_pipeline.py` to break the cycle cleanly.

- [ ] **Step 4: Verify import**

Run: `python -c "from Module.tool.safety import SafetyPipeline; sp = SafetyPipeline(); print(sp.stats)"`

- [ ] **Step 5: Commit**

```bash
git add Module/tool/safety.py
git commit -m "feat: add SafetyPipeline with Pydantic validate, safety checks, HITL, token truncation, streaming"
```

---

### Task 8: Add ToolExecutor Protocol

**Files:**
- Modify: `Module/tool/protocol.py` (add after ToolPlugin)

- [ ] **Step 1: Add ToolExecutor Protocol**

```python
from typing import Protocol, AsyncIterator


class ToolExecutor(Protocol):
    """CognitiveLoop 通过此接口使用工具系统 — 接口抽象，不依赖具体实现。"""

    async def execute(
        self, name: str, *, ctx: ToolContext, **raw_params,
    ) -> ToolResult: ...

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: Any, **raw_params,
    ) -> AsyncIterator[Any]: ...

    def definitions(self, format: str = "openai") -> list[dict]: ...

    async def cancel_all(self) -> None: ...

    def execute_now(self) -> list[str]: ...
```

- [ ] **Step 2: Verify import**

Run: `python -c "from Module.tool.protocol import ToolExecutor; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/protocol.py
git commit -m "feat: add ToolExecutor Protocol for interface dependency inversion"
```

---

### Task 9: Add ToolManager

**Files:**
- Create: `Module/tool/manager.py`

- [ ] **Step 1: Write ToolManager**

```python
"""ToolManager — 实现 ToolExecutor，管理插件生命周期、安全管道、并发、热重载。"""

import asyncio
import importlib
import logging
from collections import deque
from typing import AsyncIterator

from .protocol import (
    ErrorType, PluginContext, ToolContext, ToolPlugin, ToolResult, ToolExecutor,
)
from .capability import Capability
from .safety import SafetyPipeline

logger = logging.getLogger(__name__)


class ToolManager:
    """工具管理器 — 实现 ToolExecutor Protocol。"""

    def __init__(self) -> None:
        self._plugins: dict[str, ToolPlugin] = {}
        self._plugin_classes: dict[str, type[ToolPlugin]] = {}
        self._plugin_modules: dict[str, str] = {}       # name → module path
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
                await instance.setup(ctx)
                self._plugins[instance.name] = instance
                self._plugin_classes[instance.name] = cls
                self._plugin_modules[instance.name] = cls.__module__

                # 初始化并发控制
                if instance.safety.max_concurrency > 0:
                    self._semaphores[instance.name] = asyncio.Semaphore(
                        instance.safety.max_concurrency,
                    )

                logger.info("工具 '%s' 已加载", instance.name)
            except Exception as e:
                logger.warning("工具 '%s' setup() 失败，跳过: %s", instance.name, e)

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
                logger.warning("工具 '%s' teardown() 异常: %s", name, result)
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
        if sem is not None:
            async with sem:
                return await self._pipeline.execute(
                    plugin, name, ctx, raw_params, None,
                )

        return await self._pipeline.execute(
            plugin, name, ctx, raw_params, None,
        )

    async def execute_stream(
        self, name: str, *, ctx: ToolContext, emit: Any, **raw_params,
    ) -> AsyncIterator[Any]:
        """执行工具（流式）。"""
        plugin = self._plugins.get(name)
        if plugin is None:
            yield self._unknown_tool_result(name)
            return

        sem = self._semaphores.get(name)
        if sem is not None:
            async with sem:
                async for chunk in self._pipeline.execute_stream(
                    plugin, name, ctx, raw_params, None,
                ):
                    yield chunk
                return

        async for chunk in self._pipeline.execute_stream(
            plugin, name, ctx, raw_params, None,
        ):
            yield chunk

    # ---- Schema Export ----

    def definitions(self, format: str = "openai") -> list[dict]:
        """导出已激活插件的 LLM function calling schema。"""
        schemas = []
        for name, plugin in self._plugins.items():
            if plugin.param_model is not None:
                schema = plugin.param_model.model_json_schema()
                schema = self._flatten_refs(schema)
            else:
                schema = {"type": "object", "properties": {}}

            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": plugin.description,
                    "parameters": schema,
                },
            })
        return schemas

    def _flatten_refs(self, schema: dict) -> dict:
        """内联展开 $defs/$ref，确保 LLM 兼容性。"""
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

    async def reload_plugin(self, name: str) -> bool:
        """热重载单个插件 — 不中断正在执行的任务。"""
        old = self._plugins.get(name)
        module_path = self._plugin_modules.get(name)
        if old is None or module_path is None:
            return False

        # Teardown old instance
        try:
            await old.teardown()
        except Exception as e:
            logger.warning("旧插件 '%s' teardown() 异常: %s", name, e)

        # Remove old, keep executing tasks using old instance
        self._plugins.pop(name, None)
        self._semaphores.pop(name, None)

        # Reload module and create new instance
        try:
            module = importlib.import_module(module_path)
            module = importlib.reload(module)
            cls = self._plugin_classes.get(name)
            if cls is None:
                # 从模块中重新查找
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
            await new_instance.setup(self._plugin_context)
            self._plugins[name] = new_instance
            self._plugin_classes[name] = cls
            self._plugin_modules[name] = module_path

            if new_instance.safety.max_concurrency > 0:
                self._semaphores[name] = asyncio.Semaphore(
                    new_instance.safety.max_concurrency,
                )

            logger.info("工具 '%s' 已热重载", name)
            return True
        except Exception as e:
            logger.error("工具 '%s' 热重载失败: %s", name, e)
            return False

    # ---- Debug Interface ----

    @property
    def plugin_list(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "capabilities": [c.value for c in p.capabilities],
                "safety": {
                    "requires_human_approval": p.safety.requires_human_approval,
                    "max_concurrency": p.safety.max_concurrency,
                    "network_allowed": p.safety.network_allowed,
                    "filesystem_allowed": p.safety.filesystem_allowed,
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
        # Check which capabilities are available based on loaded plugins
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
```

- [ ] **Step 2: Verify import**

Run: `python -c "from Module.tool.manager import ToolManager; tm = ToolManager(); print(tm.plugin_list)"`
Expected: `[]`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/manager.py
git commit -m "feat: add ToolManager with load_all, execute, execute_stream, reload_plugin, debug interface"
```

---

### Task 10: Update Module/tool/__init__.py exports

**Files:**
- Modify: `Module/tool/__init__.py`

- [ ] **Step 1: Rewrite exports**

```python
"""Module.tool — 可复用工具框架（零业务依赖）。"""

from .protocol import (
    ErrorType,
    MediaItem,
    ResultType,
    ToolContext,
    ToolDefinition,
    ToolEmitter,
    ToolParameter,
    ToolProgress,
    ToolResult,
    ToolPlugin,
    PluginContext,
    ToolExecutor,
)
from .capability import Capability
from .safety import SafetyProfile, SafetyPipeline
from .manager import ToolManager

__all__ = [
    # Base classes
    "ToolPlugin",
    "ToolExecutor",
    # Management
    "ToolManager",
    # Data models
    "ErrorType",
    "Capability",
    "MediaItem",
    "PluginContext",
    "ResultType",
    "ToolContext",
    "ToolDefinition",
    "ToolEmitter",
    "ToolParameter",
    "ToolProgress",
    "ToolResult",
    # Safety
    "SafetyProfile",
    "SafetyPipeline",
]
```

- [ ] **Step 2: Verify imports**

Run: `python -c "from Module.tool import ToolPlugin, ToolManager, Capability, SafetyProfile, ToolExecutor; print('all imports ok')"`
Expected: `all imports ok`

- [ ] **Step 3: Commit**

```bash
git add Module/tool/__init__.py
git commit -m "feat: update Module.tool exports — ToolPlugin, ToolManager, Capability, SafetyProfile, ToolExecutor"
```

---

## Phase 2: Migrate Existing Tools (core/tools/)

### Task 11: Migrate Memory plugins

**Files:**
- Modify: `core/tools/memory.py`

- [ ] **Step 1: Rewrite as ToolPlugin classes**

```python
"""记忆工具插件 — 搜索和保存长期记忆。"""

import logging
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, PluginContext, ToolContext, ToolResult, SafetyProfile
from Module.tool.capability import Capability

logger = logging.getLogger(__name__)

_RUNTIME_KEY_BLACKLIST = {
    "model", "tts_enabled", "stt_enabled", "mood", "mood_intensity",
    "silence_timeout", "proactive_interval", "max_pending_messages",
}


class SearchMemoryParams(BaseModel):
    query: str = Field(..., description="搜索关键词或语义描述")
    limit: int = Field(default=5, ge=1, le=20, description="返回结果数量，最大20")


class MemorySearchPlugin(ToolPlugin):
    name = "search_memory"
    description = "搜索长期记忆库，查找相关的历史信息、偏好、事件或计划"
    param_model = SearchMemoryParams
    capabilities = {Capability.DATABASE_READ}
    safety = SafetyProfile(database_allowed=True, database_readonly=True)

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SearchMemoryParams,
    ) -> str:
        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        try:
            results = await memory.search(
                query_text=params.query, top_k=min(params.limit, 20),
            )
        except Exception as e:
            logger.warning("search_memory 失败: %s", e)
            return f"搜索记忆时出错: {e}"

        if not results:
            return "未找到相关记忆。"

        lines = []
        for r in results:
            entry_id = getattr(r, "id", "?")
            entry_type = getattr(r, "memory_type", "unknown")
            poignancy = getattr(r, "poignancy", 0)
            text = getattr(r, "text", str(r))
            lines.append(
                f"[ID:{entry_id}] (类型:{entry_type}, 重要性:{poignancy}) {text}"
            )
        return "\n".join(lines)


class SaveMemoryParams(BaseModel):
    content: str = Field(..., description="要保存的记忆内容")
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), plan(计划), preference(偏好), note(笔记)",
    )


class MemorySavePlugin(ToolPlugin):
    name = "save_memory"
    description = "将重要信息保存到长期记忆库以供未来检索"
    param_model = SaveMemoryParams
    capabilities = {Capability.DATABASE_WRITE}
    safety = SafetyProfile(
        database_allowed=True,
        database_readonly=False,
        requires_human_approval=True,
    )

    _VALID_TYPES = {"fact", "plan", "preference", "note"}

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: SaveMemoryParams,
    ) -> str:
        if params.type not in self._VALID_TYPES:
            return f"无效的记忆类型 '{params.type}'，允许: {', '.join(sorted(self._VALID_TYPES))}"

        # 运行时参数过滤
        content = params.content
        try:
            import json
            data = json.loads(content)
            if isinstance(data, dict):
                data = {
                    k: v for k, v in data.items()
                    if k not in _RUNTIME_KEY_BLACKLIST
                }
                content = json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        try:
            await memory.insert(content, type=params.type)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return f"保存记忆时出错: {e}"

        return f"已保存 {params.type} 类型记忆: {content[:200]}"
```

- [ ] **Step 2: Verify plugins instantiate**

Run: `python -c "from core.tools.memory import MemorySearchPlugin, MemorySavePlugin; ms = MemorySearchPlugin(); print(ms.name, ms.capabilities); mv = MemorySavePlugin(); print(mv.name, mv.safety.requires_human_approval)"`
Expected: `search_memory {<Capability.DATABASE_READ: 'database:read'>}` and `save_memory True`

- [ ] **Step 3: Commit**

```bash
git add core/tools/memory.py
git commit -m "refactor: migrate search_memory and save_memory to ToolPlugin classes"
```

---

### Task 12: Migrate Time plugin

**Files:**
- Modify: `core/tools/time.py`

- [ ] **Step 1: Rewrite as TimePlugin**

```python
"""时间工具插件 — 获取当前时间（支持时区和格式）。"""

from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext, SafetyProfile


class TimeParams(BaseModel):
    timezone: str = Field(
        default="UTC",
        description="时区，如 'UTC', 'Asia/Shanghai', 'US/Eastern'，或 '+8' 表示 UTC+8",
    )
    format: str = Field(
        default="iso",
        description="输出格式: 'iso' (ISO 8601) 或 'human' (人类可读)",
    )


class TimePlugin(ToolPlugin):
    name = "get_time"
    description = "获取当前日期和时间，支持指定时区和格式"
    param_model = TimeParams
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: TimeParams,
    ) -> str:
        # 解析时区
        tz_str = params.timezone.strip()
        tz: timezone | None = None
        if tz_str.startswith(("+", "-")):
            try:
                hours = int(tz_str)
                tz = timezone(timedelta(hours=hours))
            except ValueError:
                return f"无效的时区偏移: {tz_str}，请使用如 '+8' 或 'UTC'"
        elif tz_str == "UTC":
            tz = timezone.utc
        else:
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(tz_str)
            except Exception:
                tz = timezone.utc

        now = datetime.now(tz)

        if params.format == "human":
            return now.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            return now.isoformat()
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.time import TimePlugin; t = TimePlugin(); print(t.name, list(t.capabilities))"`
Expected: `get_time []`

- [ ] **Step 3: Commit**

```bash
git add core/tools/time.py
git commit -m "refactor: migrate get_time to ToolPlugin with timezone and format params"
```

---

### Task 13: Migrate Web plugins

**Files:**
- Modify: `core/tools/web.py`

- [ ] **Step 1: Rewrite as ToolPlugin classes**

(Write the full WebSearchPlugin + WebFetchPlugin with SSRF fixes, URL validation, response size limits, concurrency control. Due to length, key structural changes:)

```python
"""网络工具插件 — 网页搜索和抓取。"""

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, PluginContext, ToolContext, ToolResult, SafetyProfile
from Module.tool.capability import Capability

# ... SSRF-safe _TextExtractor, _brave_search, _ddg_search (keep existing + add safety)

class WebSearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=10, description="结果数量，最大10")


class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网获取最新信息"
    param_model = WebSearchParams
    capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_concurrency=3,
    )
    # ... execute() implementation


class WebFetchParams(BaseModel):
    url: str = Field(..., description="要抓取的网页 URL")


class WebFetchPlugin(ToolPlugin):
    name = "web_fetch"
    description = "抓取网页内容并提取文本"
    param_model = WebFetchParams
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_redirects=5,
        max_response_bytes=5 * 1024 * 1024,
        request_timeout=30.0,
    )
    # ... execute() with SSRF-safe URL validation
```

(Full implementation mirrors existing web.py logic but wrapped in Plugin classes.)

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.web import WebSearchPlugin, WebFetchPlugin; print(WebSearchPlugin().name, WebFetchPlugin().name)"`

- [ ] **Step 3: Commit**

```bash
git add core/tools/web.py
git commit -m "refactor: migrate web_search and web_fetch to ToolPlugin with SSRF fixes"
```

---

### Task 14: Migrate Filesystem plugins

**Files:**
- Modify: `core/tools/filesystem.py`

- [ ] **Step 1: Rewrite as ToolPlugin classes**

```python
"""文件系统工具插件 — 读取文件和列出目录。"""

from pathlib import Path
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext, SafetyProfile
from Module.tool.capability import Capability

# Keep _DENIED_PATHS, _DENIED_NAMES but use SafetyPipeline's _validate_path instead
# Fix: visited set for symlink loop detection (ListDirPlugin)
# Fix: line-by-line read with early stop (ReadFilePlugin)

class ReadFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对或绝对）")
    start_line: int = Field(default=0, ge=0, description="起始行号（0-indexed）")
    max_lines: int = Field(default=200, ge=1, le=500, description="最大读取行数")


class ReadFilePlugin(ToolPlugin):
    name = "read_file"
    description = "读取文本文件内容"
    param_model = ReadFileParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["data/sandbox"],
        max_file_bytes=10 * 1024 * 1024,
        allow_symlinks=False,
    )
    # ... execute() with line-by-line read, binary detection, early stop


class ListDirParams(BaseModel):
    path: str = Field(default=".", description="目录路径")
    depth: int = Field(default=1, ge=1, le=3, description="遍历深度，1-3")


class ListDirPlugin(ToolPlugin):
    name = "list_directory"
    description = "列出目录内容"
    param_model = ListDirParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["data/sandbox"],
        max_dir_depth=3,
        allow_symlinks=False,
    )
    # ... execute() with visited set for symlink loop prevention
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.filesystem import ReadFilePlugin, ListDirPlugin; print(ReadFilePlugin().name, ListDirPlugin().name)"`

- [ ] **Step 3: Commit**

```bash
git add core/tools/filesystem.py
git commit -m "refactor: migrate read_file and list_directory to ToolPlugin with symlink fix and line-by-line read"
```

---

## Phase 3: New Tools (core/tools/)

### Task 15: WriteFilePlugin

**Files:**
- Create: `core/tools/write_file.py`

- [ ] **Step 1: Write the plugin**

```python
"""文件写入工具插件 — 在 sandbox 目录下安全写入文件。"""

from pathlib import Path
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext, ToolResult, SafetyProfile
from Module.tool.capability import Capability


class WriteFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对路径，自动限定在 sandbox 目录下）")
    content: str = Field(..., description="要写入的内容")


class WriteFilePlugin(ToolPlugin):
    name = "write_file"
    description = "在安全目录下写入文件"
    param_model = WriteFileParams
    capabilities = {Capability.FILESYSTEM_WRITE}
    safety = SafetyProfile(
        filesystem_allowed=True,
        write_dir="data/sandbox/{session_id}",
        max_file_bytes=10 * 1024 * 1024,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: WriteFileParams,
    ) -> str:
        session_id = ctx.session_id or "default"
        write_dir = Path(
            self.safety.write_dir.replace("{session_id}", session_id)
        ).resolve()
        write_dir.mkdir(parents=True, exist_ok=True)

        file_path = write_dir / params.path
        resolved = file_path.resolve()

        if not str(resolved).startswith(str(write_dir)):
            return f"拒绝写入: 路径 '{params.path}' 越过了 sandbox 边界"

        try:
            resolved.write_text(params.content, encoding="utf-8")
            size = resolved.stat().st_size
            return f"已写入 {resolved.name} ({size} bytes)"
        except Exception as e:
            return f"写入失败: {e}"
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.write_file import WriteFilePlugin; p = WriteFilePlugin(); print(p.name, p.safety.requires_human_approval)"`
Expected: `write_file True`

- [ ] **Step 3: Commit**

```bash
git add core/tools/write_file.py
git commit -m "feat: add WriteFilePlugin with sandbox path enforcement and HITL approval"
```

---

### Task 16: SetReminderPlugin

**Files:**
- Create: `core/tools/reminder.py`

- [ ] **Step 1: Write the plugin**

```python
"""提醒工具插件 — 设置定时提醒。"""

import asyncio
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, PluginContext, ToolContext, SafetyProfile
from Module.tool.capability import Capability


class ReminderParams(BaseModel):
    message: str = Field(..., description="提醒内容")
    delay_seconds: int = Field(..., ge=1, le=86400, description="延迟秒数，最大86400(24小时)")


class SetReminderPlugin(ToolPlugin):
    name = "set_reminder"
    description = "设置定时提醒，在指定秒数后通知用户"
    param_model = ReminderParams
    capabilities = {Capability.SCHEDULE}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ReminderParams,
    ) -> str:
        scheduler = ctx.get("scheduler")
        if scheduler is None:
            return "提醒功能当前不可用（调度器未加载）。"

        # 创建 BACKGROUND 任务
        # scheduler 的 dispatch_background 接口需要传入 Task
        from core.task import Task, TaskType
        task = Task(
            type=TaskType.BACKGROUND,
            payload={
                "tool_name": "set_reminder",
                "message": params.message,
                "delay": params.delay_seconds,
            },
            priority=9,
        )
        task_id = task.id

        # 注册延迟回调
        async def _remind():
            await asyncio.sleep(params.delay_seconds)
            # 通过 output channel 发送提醒
            return f"⏰ 提醒: {params.message}"

        # 提交到 scheduler
        tid = await scheduler.dispatch_background(_remind())
        return f"已设置提醒 (ID: {tid})，{params.delay_seconds} 秒后通知: {params.message}"
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.reminder import SetReminderPlugin; print(SetReminderPlugin().name)"`
Expected: `set_reminder`

- [ ] **Step 3: Commit**

```bash
git add core/tools/reminder.py
git commit -m "feat: add SetReminderPlugin for timed reminders via TaskScheduler"
```

---

### Task 17: RuntimeConfigPlugin

**Files:**
- Create: `core/tools/runtime_config.py`

- [ ] **Step 1: Write the plugin**

```python
"""运行时配置工具插件 — 查看 agent 配置和状态。"""

import os
from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, PluginContext, ToolContext, SafetyProfile
from Module.tool.capability import Capability

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class RuntimeConfigParams(BaseModel):
    query: str | None = Field(
        default=None,
        description="可选：查询特定项，如 'model', 'tts', 'memory'。留空返回全部。",
    )


class RuntimeConfigPlugin(ToolPlugin):
    name = "get_runtime_config"
    description = "查看当前运行时配置：模型名、TTS/STT 状态、内存使用、已加载工具"
    param_model = RuntimeConfigParams
    capabilities = {Capability.PROCESS}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: RuntimeConfigParams,
    ) -> str:
        config = ctx.get("config")
        tools = ctx.get("_tool_names", [])

        lines = []

        if config is not None:
            llm_cfg = getattr(config, "llm", None)
            tts_cfg = getattr(config, "tts", None)
            stt_cfg = getattr(config, "stt", None)
            server_cfg = getattr(config, "server", None)

            if llm_cfg:
                lines.append(f"LLM 模型: {getattr(llm_cfg, 'model', 'unknown')}")
            if tts_cfg:
                lines.append(f"TTS: {'启用' if getattr(tts_cfg, 'enabled', False) else '禁用'}")
            if stt_cfg:
                lines.append(f"STT: {'启用' if getattr(stt_cfg, 'enabled', False) else '禁用'}")
            if server_cfg:
                lines.append(f"服务: {getattr(server_cfg, 'host', '?')}:{getattr(server_cfg, 'port', '?')}")

        lines.append(f"已加载工具 ({len(tools)}): {', '.join(tools[:20])}")

        if HAS_PSUTIL:
            proc = psutil.Process()
            mem = proc.memory_info()
            lines.append(
                f"进程内存: RSS={mem.rss / 1024 / 1024:.1f}MB, "
                f"VMS={mem.vms / 1024 / 1024:.1f}MB, "
                f"线程数={proc.num_threads()}"
            )

        # 环境信息（脱敏）
        env_vars = ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY"]
        for var in env_vars:
            val = os.getenv(var)
            status = "已设置" if val else "未设置"
            lines.append(f"环境变量 {var}: {status}")

        return "\n".join(lines)
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.runtime_config import RuntimeConfigPlugin; print(RuntimeConfigPlugin().name)"`

- [ ] **Step 3: Commit**

```bash
git add core/tools/runtime_config.py
git commit -m "feat: add RuntimeConfigPlugin for agent config introspection"
```

---

### Task 18: SelfStatePlugin

**Files:**
- Create: `core/tools/self_state.py`

- [ ] **Step 1: Write the plugin**

```python
"""自我感知工具插件 — agent 查看自身状态。"""

from Module.tool.protocol import ToolPlugin, ToolContext, SafetyProfile


class SelfStatePlugin(ToolPlugin):
    name = "get_self_state"
    description = "查看 agent 的当前运行状态：FSM 状态、会话信息、工具使用情况"
    param_model = None
    capabilities: set = set()
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit=None, params=None,
    ) -> str:
        fsm_state = ctx.get("_fsm_state", "unknown")
        session_count = ctx.get("_session_msg_count", 0)
        tool_count = ctx.get("_tool_count", 0)
        trace_id = ctx.trace_id or "N/A"
        agent_id = ctx.agent_id or "default"

        lines = [
            f"Agent ID: {agent_id}",
            f"当前 Trace ID: {trace_id}",
            f"FSM 状态: {fsm_state}",
            f"会话消息数: {session_count}",
            f"已加载工具数: {tool_count}",
        ]
        return "\n".join(lines)
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools.self_state import SelfStatePlugin; print(SelfStatePlugin().name)"`

- [ ] **Step 3: Commit**

```bash
git add core/tools/self_state.py
git commit -m "feat: add SelfStatePlugin for agent introspection (FSM, session, tools)"
```

---

## Phase 4: Integration (core/)

### Task 19: Update core/tools/__init__.py

**Files:**
- Modify: `core/tools/__init__.py`

- [ ] **Step 1: Rewrite to return plugin classes**

```python
"""Elysia 工具插件集合。"""

from .memory import MemorySearchPlugin, MemorySavePlugin
from .time import TimePlugin
from .web import WebSearchPlugin, WebFetchPlugin
from .filesystem import ReadFilePlugin, ListDirPlugin
from .write_file import WriteFilePlugin
from .reminder import SetReminderPlugin
from .runtime_config import RuntimeConfigPlugin
from .self_state import SelfStatePlugin

# 所有插件类（按顺序加载）
ALL_PLUGINS: list[type] = [
    MemorySearchPlugin,
    MemorySavePlugin,
    TimePlugin,
    WebSearchPlugin,
    WebFetchPlugin,
    ReadFilePlugin,
    ListDirPlugin,
    WriteFilePlugin,
    SetReminderPlugin,
    RuntimeConfigPlugin,
    SelfStatePlugin,
]
```

- [ ] **Step 2: Verify**

Run: `python -c "from core.tools import ALL_PLUGINS; print(len(ALL_PLUGINS))"`
Expected: `11`

- [ ] **Step 3: Commit**

```bash
git add core/tools/__init__.py
git commit -m "refactor: replace register_all with ALL_PLUGINS plugin class list"
```

---

### Task 20: Update TaskScheduler retry intelligence

**Files:**
- Modify: `core/task_scheduler.py` (lines 300-350, 175-195, 352-392)

- [ ] **Step 1: Update _execute_tool retry logic**

In `_execute_tool` (line 309), change the while loop (lines 320-328) to check `ErrorType.is_retryable`:

```python
async def _execute_tool(self, task: Task) -> ToolResult:
    tool_name = task.payload.get("tool_name", "")
    arguments = task.payload.get("arguments", {})

    while True:
        try:
            result = await asyncio.wait_for(
                self._tool_executor.execute(tool_name, ctx=self._tool_ctx, **arguments),
                timeout=_TOOL_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            result = ToolResult.fail(
                name=tool_name,
                error=f"执行超时 ({_TOOL_TIMEOUT:.0f}s)",
                error_type=ErrorType.TIMEOUT,
            )
        except Exception as e:
            result = ToolResult.fail(
                name=tool_name, error=str(e),
                error_type=ErrorType.TOOL_ERROR,
            )

        if result.success:
            return result

        # 仅对可重试的错误类型重试
        error_type = result.error_type
        retryable = (
            error_type is not None and error_type.is_retryable
            and task.can_retry()
        )
        if not retryable:
            return result

        task.retry_count += 1
        await asyncio.sleep(task.retry_backoff_seconds())
```

- [ ] **Step 2: Fix cancel() cycle detection**

In `cancel()` (line 175), add a `_cancelling` set:

```python
async def cancel(self, task_id: str, _visited: set | None = None) -> bool:
    if _visited is None:
        _visited = set()
    if task_id in _visited:
        return False
    _visited.add(task_id)
    # ... rest of cancel logic, passing _visited to children
```

- [ ] **Step 3: Fix checkpoint save locking**

In `_save_checkpoint` (line 352), add lock:

```python
async def _save_checkpoint(self) -> None:
    async with self._checkpoint_lock:
        pending_bg = [
            t for t in self._tasks.values()
            if t.type.value == "background"
            and t.status.value == "pending"
        ]
        # ... rest of save logic
```

Also add `self._checkpoint_lock = asyncio.Lock()` to `__init__`.

- [ ] **Step 4: Update TaskScheduler to accept ToolExecutor instead of ToolRegistry**

Change the constructor parameter from `tools: ToolRegistry` to `tool_executor: ToolExecutor`:

```python
def __init__(
    self,
    tool_executor: Any = None,     # ToolExecutor
    tool_ctx: Any = None,          # ToolContext
    num_workers: int = 5,
    checkpoint_path: str = "data/scheduler_checkpoint.json",
) -> None:
    self._tool_executor = tool_executor
```

And in `_execute_tool` (line 316), change:
```python
result = await asyncio.wait_for(
    self._tool_executor.execute(tool_name, ctx=self._tool_ctx, **arguments),
    timeout=_TOOL_TIMEOUT,
)
```

- [ ] **Step 5: Verify**

Run: `python -m pytest core/test_task_scheduler.py -v 2>/dev/null || python -c "from core.task_scheduler import TaskScheduler; print('import ok')"`

- [ ] **Step 6: Commit**

```bash
git add core/task_scheduler.py
git commit -m "refactor: TaskScheduler retry checks ErrorType.is_retryable, cancel cycle detection, checkpoint locking, accept ToolExecutor"
```

---

### Task 21: Update CognitiveLoop

**Files:**
- Modify: `core/cognitive_loop.py`

- [ ] **Step 1: Change constructor to accept ToolExecutor**

```python
def __init__(
    self,
    # ... other params unchanged ...
    tool_executor: Any = None,     # ToolExecutor (was: tools: ToolRegistry)
    # ...
) -> None:
    self._tool_executor = tool_executor
```

- [ ] **Step 2: Update _execute_tools_directly**

In `_execute_tools_directly` (line 450), replace `self._tools.execute(...)` with `self._tool_executor.execute(...)`:

```python
async def _execute_one(tc: dict):
    func_name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        args = {}
    try:
        tr = await self._tool_executor.execute(
            func_name, ctx=self._ctx, **args,
        )
    except asyncio.CancelledError:
        tr = ToolResult.fail(name=func_name, error="用户取消")
    except Exception as e:
        tr = ToolResult.fail(name=func_name, error=str(e))
    return tc, tr
```

- [ ] **Step 3: Generate trace_id at process() start**

In `process()` (line 108), generate trace_id:

```python
import uuid

async def process(self, user_input: str) -> None:
    # ... existing lock acquisition ...
    trace_id = uuid.uuid4().hex
    self._ctx.trace_id = trace_id
    # ... rest of process ...
```

- [ ] **Step 4: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "refactor: CognitiveLoop depends on ToolExecutor Protocol, generates trace_id per request"
```

---

### Task 22: Update Agent initialization

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: Change imports**

```python
from Module.tool import ToolManager, ToolContext, PluginContext
from core.tools import ALL_PLUGINS
```

- [ ] **Step 2: Replace ToolRegistry with ToolManager**

```python
# In __init__:
self._tool_manager = ToolManager()

# In start():
plugin_ctx = PluginContext(
    session_id=self._session_id,
    memory=self._long_term,
    mongodb=getattr(self, "_mongodb", None),
    milvus=getattr(self, "_milvus", None),
    http_client=self._http_client if hasattr(self, "_http_client") else None,
    scheduler=self._scheduler,
    config=self._config,
)
await self._tool_manager.load_all(ALL_PLUGINS, plugin_ctx)
```

- [ ] **Step 3: Update CognitiveLoop and TaskScheduler injection**

```python
# CognitiveLoop:
self._cognitive = CognitiveLoop(
    # ...
    tool_executor=self._tool_manager,
    # ...
)

# TaskScheduler:
self._scheduler = TaskScheduler(
    tool_executor=self._tool_manager,
    tool_ctx=None,
    num_workers=5,
    checkpoint_path="data/scheduler_checkpoint.json",
)
```

- [ ] **Step 4: Update _register_tools**

Remove `_register_tools` method (tool loading is now in `start()`).

- [ ] **Step 5: Update shutdown**

```python
async def shutdown(self):
    # ... existing shutdown sequence ...
    await self._tool_manager.shutdown()
```

- [ ] **Step 6: Update print_summary**

```python
names = [p.name for p in self._tool_manager._plugins.values()]
print(f"  工具: {', '.join(names) or '无'}")
```

- [ ] **Step 7: Commit**

```bash
git add core/agent.py
git commit -m "refactor: Agent uses ToolManager with PluginContext, loads ALL_PLUGINS at start()"
```

---

### Task 23: Update DebugInfo

**Files:**
- Modify: `core/debug_info.py`

- [ ] **Step 1: Update tool data collection**

Replace lines 104-123 with ToolManager debug interface:

```python
# ---- Tools ----
tm = getattr(agent, "_tool_manager", None)
if tm is not None:
    info.currently_executing = list(tm.executing_now)
    info.tools = tm.plugin_list
    info.tool_history = [
        {
            "name": h.get("name", ""),
            "success": h.get("success", False),
            "error": h.get("error"),
            "result": h.get("result", ""),
            "duration_ms": h.get("duration_ms", 0),
        }
        for h in tm.history[-5:]
    ]
```

- [ ] **Step 2: Update health endpoint in server.py**

Change line 90 from `agent._tools.tool_names` to:
```python
"tools": [p["name"] for p in tm.plugin_list] if tm else [],
```

- [ ] **Step 3: Commit**

```bash
git add core/debug_info.py Module/input/server.py
git commit -m "refactor: DebugInfo uses ToolManager debug interface, no longer accesses private _tools dict"
```

---

### Task 24: Cleanup old files

**Files:**
- Delete: `Module/tool/registry.py`
- Delete: `Module/tool/decorator.py`
- Delete: `Module/tool/schema.py`

- [ ] **Step 1: Delete files**

```bash
git rm Module/tool/registry.py Module/tool/decorator.py Module/tool/schema.py
```

- [ ] **Step 2: Verify nothing is broken**

Run: `python -c "from Module.tool import ToolPlugin, ToolManager, Capability, SafetyProfile, ToolExecutor; print('clean import ok')"`
Expected: `clean import ok`

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove deprecated registry.py, decorator.py, schema.py"
```

---

## Phase 5: Tests + Verification

### Task 25: Write framework unit tests

**Files:**
- Create: `Module/tool/test_capability.py`
- Create: `Module/tool/test_safety.py`
- Create: `Module/tool/test_manager.py`

- [ ] **Step 1: test_capability.py**

```python
"""Capability 枚举测试。"""
from Module.tool.capability import Capability


def test_capability_values():
    assert Capability.NETWORK.value == "network"
    assert Capability.DATABASE_READ.value == "database:read"
    assert Capability.SCHEDULE.value == "schedule"


def test_capability_uniqueness():
    values = [c.value for c in Capability]
    assert len(values) == len(set(values))
```

- [ ] **Step 2: test_safety.py — SafetyProfile defaults**

```python
"""SafetyProfile + SafetyPipeline 测试。"""
import pytest
from Module.tool.safety import SafetyProfile, SafetyPipeline, _estimate_tokens, _format_validation_error


def test_safety_profile_defaults():
    sp = SafetyProfile()
    assert sp.block_private_ips is True
    assert sp.max_concurrency == 0
    assert sp.max_result_tokens == 2000
    assert sp.requires_human_approval is False


def test_estimate_tokens_english():
    text = "Hello world this is a test"
    tokens = _estimate_tokens(text)
    assert tokens > 0


def test_estimate_tokens_chinese():
    text = "这是一段中文测试文本"
    tokens = _estimate_tokens(text)
    assert tokens > 0
```

- [ ] **Step 3: test_manager.py — basic lifecycle**

```python
"""ToolManager 测试。"""
import pytest
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext


@pytest.mark.asyncio
async def test_tool_manager_empty():
    tm = ToolManager()
    await tm.load_all([], PluginContext(session_id="test"))
    assert len(tm.plugin_list) == 0
    assert tm.capability_status == {}
    await tm.shutdown()
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest Module/tool/test_capability.py Module/tool/test_safety.py Module/tool/test_manager.py -v
```

- [ ] **Step 5: Commit**

```bash
git add Module/tool/test_capability.py Module/tool/test_safety.py Module/tool/test_manager.py
git commit -m "test: add framework unit tests for Capability, SafetyProfile, ToolManager"
```

---

### Task 26: Write plugin unit tests

**Files:**
- Create: `core/tools/test_plugins.py`

- [ ] **Step 1: Basic tests for each plugin**

```python
"""插件单元测试。"""
import pytest
from core.tools.time import TimePlugin, TimeParams
from core.tools.self_state import SelfStatePlugin
from core.tools.write_file import WriteFilePlugin, WriteFileParams


def test_time_plugin_params():
    p = TimeParams(timezone="UTC", format="iso")
    assert p.timezone == "UTC"
    assert p.format == "iso"


@pytest.mark.asyncio
async def test_self_state_plugin():
    from Module.tool.protocol import ToolContext
    plugin = SelfStatePlugin()
    ctx = ToolContext(
        session_id="test",
        trace_id="trace-1",
        agent_id="agent-1",
    )
    ctx.services["_fsm_state"] = "idle"
    ctx.services["_session_msg_count"] = 5
    ctx.services["_tool_count"] = 11
    result = await plugin.execute(ctx=ctx, params=None)
    assert "idle" in str(result)
    assert "trace-1" in str(result)


def test_write_file_params():
    p = WriteFileParams(path="test.txt", content="hello")
    assert p.path == "test.txt"
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest core/tools/test_plugins.py -v
```

- [ ] **Step 3: Commit**

```bash
git add core/tools/test_plugins.py
git commit -m "test: add plugin unit tests"
```

---

### Task 27: Run regression tests

- [ ] **Step 1: Run all existing Module tests**

```bash
python -m pytest Module/ -v --tb=short
```

Expected: All previously passing tests still pass. Fix any failures caused by import changes.

- [ ] **Step 2: Run new tests**

```bash
python -m pytest Module/tool/ core/tools/ -v --tb=short
```

- [ ] **Step 3: Commit any fixes**

```bash
git add -u
git commit -m "fix: regression test fixes after tool system refactor"
```

---

### Task 28: End-to-end verification

- [ ] **Step 1: Start the agent**

```bash
python app.py &
sleep 3
```

- [ ] **Step 2: Check health endpoint**

```bash
curl -s http://localhost:8080/health | python -m json.tool
```

Expected: `"status": "ok"`, `"tools"` list contains all 11 tool names.

- [ ] **Step 3: Check debug API**

```bash
curl -s http://localhost:8080/debug/api | python -m json.tool | head -40
```

Expected: `tools` array with plugin data, `tool_history` field.

- [ ] **Step 4: Stop agent**

```bash
kill %1
```

- [ ] **Step 5: Commit if needed**

---

## Summary

| Phase | Tasks | Files Changed |
|-------|-------|---------------|
| Phase 1: Framework | 1-10 | `Module/tool/protocol.py`, `capability.py` (new), `safety.py`, `manager.py` (new), `__init__.py` |
| Phase 2: Migrate | 11-14 | `core/tools/memory.py`, `time.py`, `web.py`, `filesystem.py` |
| Phase 3: New tools | 15-18 | `core/tools/write_file.py`, `reminder.py`, `runtime_config.py`, `self_state.py` (all new) |
| Phase 4: Integration | 19-24 | `core/tools/__init__.py`, `core/task_scheduler.py`, `core/cognitive_loop.py`, `core/agent.py`, `core/debug_info.py`, `Module/input/server.py`, delete 3 old files |
| Phase 5: Tests | 25-28 | 4 test files (new), regression run |

**Total: 28 tasks, ~15 files created/modified, 3 files deleted.**
