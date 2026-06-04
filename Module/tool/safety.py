"""工具安全层 — SafetyProfile 声明 + SafetyPipeline 执行中间件 + 旧版工具函数。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .capability import Capability
from .protocol import ToolParameter, ToolProgress  # ToolParameter 旧版参数类型 — 过渡期保留

logger = logging.getLogger(__name__)


# ============================================================================
# SafetyProfile — 每个插件声明自己的安全边界
# ============================================================================

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


# ============================================================================
# Token 估算 + 截断工具
# ============================================================================

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
    # head_only (including forced for streaming)
    chars = int(len(text) * (max_tokens / est))
    return text[:chars] + "\n...[截断]", True


# ============================================================================
# 路径 + URL 安全检查
# ============================================================================

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

    if is_write:
        write_dir = Path(
            profile.write_dir.replace("{session_id}", session_id)
        ).resolve()
        if not str(resolved).startswith(str(write_dir)):
            return resolved, f"写入路径必须在 '{write_dir}' 下"

    return resolved, None


def _validate_path(
    resolved: Path, profile: SafetyProfile,
) -> str | None:
    """路径安全检查。返回 None 表示通过，否则返回错误字符串。"""
    path_str = str(resolved)

    if profile.read_dirs:
        allowed = any(
            path_str.startswith(str(Path(d).resolve()))
            for d in profile.read_dirs
        )
        if not allowed:
            return f"路径 '{path_str}' 不在允许的目录中"

    if not profile.allow_symlinks:
        for parent in [resolved, *resolved.parents]:
            if parent.is_symlink():
                return f"路径 '{path_str}' 包含符号链接，已被拒绝"

    if resolved.exists() and resolved.is_file():
        if resolved.stat().st_size > profile.max_file_bytes:
            return f"文件过大 ({resolved.stat().st_size} bytes)"

    return None


# ============================================================================
# SafetyPipeline — 执行中间件
# ============================================================================

class SafetyPipeline:
    """工具执行中间件管道：validate → safety → hitl → execute → truncate。"""

    def __init__(self) -> None:
        self._hitl_requests: dict[str, asyncio.Event] = {}
        self._hitl_results: dict[str, tuple[bool, str]] = {}
        self._hitl_timeout: float = 60.0
        self._stats: dict[str, int] = {
            "total": 0, "timeout": 0, "denied": 0, "rate_limited": 0,
        }

    # ---- HITL public API ----

    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """前端提交审批结果。返回 True 如果找到了对应的审批请求。"""
        if request_id not in self._hitl_requests:
            return False
        self._hitl_results[request_id] = (
            approved, "confirmed" if approved else "denied",
        )
        self._hitl_requests[request_id].set()
        return True

    def request_exists(self, request_id: str) -> bool:
        return request_id in self._hitl_requests

    def set_hitl_broadcast(self, callback):
        """Set the HITL approval broadcast callback.

        callback(request_id, tool_name, params) -> None
        Should broadcast approval request to frontend via OutputManager.
        """
        self._broadcast_hitl = callback

    # ---- Pipeline execution ----

    async def execute(
        self,
        plugin: Any,
        name: str,
        ctx: Any,
        raw_params: dict,
        semaphore: asyncio.Semaphore | None,
    ) -> Any:  # ToolResult
        """执行完整管道（非流式）。"""
        from Module.tool.protocol import ToolResult, ErrorType  # noqa: F811

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
        if plugin.safety and plugin.safety.requires_human_approval:
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
        timeout = plugin.safety.request_timeout if plugin.safety else 30.0
        try:
            try:
                raw = await asyncio.wait_for(
                    plugin.execute(ctx=ctx, params=params),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                self._stats["timeout"] += 1
                return ToolResult.fail(
                    name=name, error=f"执行超时 ({timeout}s)",
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
    ) -> Any:  # AsyncIterator
        """执行流式管道。"""
        from Module.tool.protocol import ToolResult, ErrorType  # noqa: F811

        # Pre-checks
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

        if plugin.safety and plugin.safety.requires_human_approval:
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
        max_tokens = plugin.safety.max_result_tokens if plugin.safety else 2000
        gen = None
        try:
            gen = plugin.execute_stream(ctx=ctx, emit=ctx.services.get("_emit"), params=params)
            async for chunk in gen:
                if isinstance(chunk, ToolResult):
                    yield self._wrap_result(chunk, name, 0, plugin.safety if plugin.safety else None)
                    return
                if not isinstance(chunk, ToolProgress):
                    acc_text += str(chunk)
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
                try:
                    await gen.aclose()
                except Exception:
                    pass  # aclose() 失败不应阻止返回取消结果
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
        if profile is None:
            return None
        session_id = getattr(ctx, "session_id", "default") or "default"

        # Network capability enforcement: plugins that access the network MUST declare Capability.NETWORK
        if profile.network_allowed:
            if Capability.NETWORK not in getattr(plugin, "capabilities", set()):
                return "插件声明了 network_allowed 但未声明 Capability.NETWORK"

        if "url" in raw_params and profile.network_allowed:
            err = _validate_url(raw_params["url"], profile)
            if err:
                return err

        if "path" in raw_params and profile.filesystem_allowed:
            raw_path = raw_params["path"]
            is_write = Capability.FILESYSTEM_WRITE in getattr(plugin, "capabilities", set())
            resolved, err = _resolve_sandbox_path(
                raw_path, profile, session_id, is_write,
            )
            if err:
                return err
            err = _validate_path(resolved, profile)
            if err:
                return err
            # 将验证后的安全路径注入 ctx，插件不再自行从 CWD 解析
            ctx.services["_safe_path"] = str(resolved)

        return None

    async def _run_hitl(
        self, plugin: Any, name: str, params: dict,
    ) -> tuple[bool, str]:
        """HITL 审批流程。返回 (approved, status)。"""
        from core.message_id import new_message_id
        request_id = new_message_id()
        event = asyncio.Event()
        self._hitl_requests[request_id] = event

        # 广播审批请求 — 由 ToolManager 通过 callback 注入
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
        self, raw: Any, name: str, duration_ms: float,
        profile: SafetyProfile | None,
    ) -> Any:  # ToolResult
        """Wrap raw result into ToolResult with token truncation."""
        from Module.tool.protocol import ToolResult  # noqa: F811

        if isinstance(raw, ToolResult):
            result = raw
        else:
            result = ToolResult.ok(
                name=name, content=str(raw), duration_ms=duration_ms,
            )

        if profile and result.content and not result.truncated:
            content, was_truncated = _truncate_text(
                result.content, profile.max_result_tokens,
                profile.truncate_strategy,
            )
            if was_truncated:
                result.content = content
                result.truncated = True

        result.tool_name = name
        if not isinstance(raw, ToolResult):
            result.duration_ms = duration_ms
        return result

    # ---- Debug ----

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# ============================================================================
# 旧版工具函数 — register.py 过渡期仍需使用，后续删除
# ============================================================================

async def execute_with_timeout(func, kwargs: dict, timeout: float) -> Any:
    """asyncio.wait_for 包裹执行。"""
    return await asyncio.wait_for(func(**kwargs), timeout=timeout)


def truncate_result(result: Any, max_len: int) -> tuple[str, bool]:
    """str() 转换后截断到 max_len 字符。"""
    text = str(result)
    if len(text) <= max_len:
        return text, False
    return text[:max_len] + "…", True


def validate_args(params: dict[str, ToolParameter], raw_args: dict) -> tuple[dict, list[str]]:
    """类型转换 + 必填检查。"""
    coerced: dict[str, Any] = {}
    errors: list[str] = []

    for name, param in params.items():
        if name not in raw_args:
            if param.required and not param.default_set:
                errors.append(f"缺少必填参数: {name}")
            elif param.default_set:
                coerced[name] = param.default
            continue

        value = raw_args[name]
        coerced_value, err = _coerce_value(name, value, param.type)
        if err:
            errors.append(err)
        else:
            coerced[name] = coerced_value

    for name in raw_args:
        if name not in params:
            coerced[name] = raw_args[name]

    return coerced, errors


def _coerce_value(name: str, value: Any, target_type: str) -> tuple[Any, str | None]:
    """将 value 转换为 target_type。"""
    if target_type == "integer":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, int):
            return value, None
        try:
            return int(value), None
        except (ValueError, TypeError):
            return None, f"参数 {name} 需要整数类型，收到: {value!r}"

    if target_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), None
        try:
            return float(value), None
        except (ValueError, TypeError):
            return None, f"参数 {name} 需要数字类型，收到: {value!r}"

    if target_type == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes"):
                return True, None
            if v in ("false", "0", "no"):
                return False, None
        return None, f"参数 {name} 需要布尔类型，收到: {value!r}"

    return value, None
