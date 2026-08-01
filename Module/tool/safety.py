"""工具安全层 — SafetyProfile 声明 + SafetyPipeline 执行中间件 + 旧版工具函数。"""

from __future__ import annotations

import asyncio
import uuid
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _sanitize_session_id(session_id: str) -> str:
    """对 session_id 做安全过滤，防止路径穿越攻击。

    仅允许 [a-zA-Z0-9_-]。不匹配时返回 "default"。

    Args:
        session_id: 原始 session_id 字符串。

    Returns:
        过滤后的安全 session_id，非法时返回 "default"。
    """
    if _SESSION_ID_RE.match(session_id):
        return session_id
    logger.warning("session_id 包含非法字符，降级为 default: %r", session_id[:100])
    return "default"

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
    """粗略 token 估算：英文 ~4 chars/token，中文 ~1 char/token。

    Args:
        text: 待估算的文本。

    Returns:
        估算的 token 数量。
    """
    latin = len(re.findall(r"[a-zA-Z0-9\s\W]", text))
    cjk = len(text) - latin
    return int(latin / 4 + cjk)


def _format_validation_error(exc: ValidationError) -> str:
    """将 Pydantic ValidationError 精简为一行一句话（≤200 字符）。

    Args:
        exc: Pydantic 校验异常。

    Returns:
        格式化后的错误描述字符串。
    """
    messages = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"]
        messages.append(f"字段 '{loc}'：{msg}")
    return "; ".join(messages)[:200]


def _format_args(raw_params: dict, max_len: int = 60) -> str:
    """格式化工具参数用于日志，截断长值。

    Args:
        raw_params: 原始参数字典。
        max_len: 每个参数值的最大展示长度。

    Returns:
        格式化后的参数字符串（如 "key1=val1, key2=val2"）。
    """
    if not raw_params:
        return ""
    parts = []
    for k, v in raw_params.items():
        s = str(v)
        if len(s) > max_len:
            s = s[:max_len] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _truncate_text(
    text: str, max_tokens: int, strategy: str,
) -> tuple[str, bool]:
    """Token-aware 截断。

    Args:
        text: 待截断的文本。
        max_tokens: 最大 token 数。
        strategy: 截断策略（"head_tail" 或 "head_only"）。

    Returns:
        (截断后的文本, 是否被截断)。
    """
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
    """URL 安全检查（内网 IP 过滤 + scheme 检查）。

    Args:
        url: 待验证的 URL 字符串。
        profile: 安全配置声明。

    Returns:
        错误描述字符串；None 表示通过检查。
    """
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
    """Sandbox 路径解析。

    Args:
        path: 原始路径字符串。
        profile: 安全配置声明。
        session_id: 当前会话 ID，用于模板变量替换。
        is_write: 是否为写入操作（影响 sandbox 目录选择）。

    Returns:
        (解析后的绝对路径, 错误信息或 None)。
    """
    raw = Path(path)
    safe_sid = _sanitize_session_id(session_id)
    if not raw.is_absolute():
        if is_write:
            write_dir = profile.write_dir.replace("{session_id}", safe_sid)
            raw = Path(write_dir) / raw
        elif profile.read_dirs:
            raw = Path(profile.read_dirs[0].replace("{session_id}", safe_sid)) / raw
    try:
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return raw, f"路径解析失败: {e}"

    if is_write:
        write_dir = Path(
            profile.write_dir.replace("{session_id}", safe_sid)
        ).resolve()
        if not str(resolved).startswith(str(write_dir)):
            return resolved, f"写入路径必须在 '{write_dir}' 下"

    return resolved, None


def _validate_path(
    resolved: Path, profile: SafetyProfile,
) -> str | None:
    """路径安全检查。

    Args:
        resolved: 已解析的绝对路径。
        profile: 安全配置声明。

    Returns:
        None 表示通过检查，否则返回错误描述字符串。
    """
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
        """初始化安全管道。

        初始化 HITL 审批请求/结果字典、审批超时（默认 60s）、执行统计计数器。
        """
        self._hitl_requests: dict[str, tuple[asyncio.Event, str, dict]] = {}  # request_id → (event, tool_name, params)
        self._hitl_results: dict[str, tuple[bool, str]] = {}
        self._hitl_timeout: float = 60.0
        self._stats: dict[str, int] = {
            "total": 0, "timeout": 0, "denied": 0, "rate_limited": 0,
        }

    # ---- HITL public API ----

    def submit_approval(self, request_id: str, approved: bool) -> bool:
        """前端提交审批结果。

        Args:
            request_id: 审批请求 ID。
            approved: True 表示批准，False 表示拒绝。

        Returns:
            True 如果找到了对应的审批请求并已处理。
        """
        if request_id not in self._hitl_requests:
            return False
        self._hitl_results[request_id] = (
            approved, "confirmed" if approved else "denied",
        )
        self._hitl_requests[request_id][0].set()
        return True

    def request_exists(self, request_id: str) -> bool:
        """检查审批请求是否存在。

        Args:
            request_id: 审批请求 ID。

        Returns:
            True 如果存在对应的审批请求。
        """
        return request_id in self._hitl_requests

    def get_pending_approvals(self) -> list[tuple[str, str, dict]]:
        """返回所有待审批请求。

        用于 WS 重连时重新广播审批弹窗到新客户端。

        Returns:
            [(request_id, tool_name, params), ...] 列表。
        """
        return [
            (rid, tool_name, params)
            for rid, (event, tool_name, params) in self._hitl_requests.items()
            if not event.is_set()
        ]

    def set_hitl_broadcast(self, callback) -> None:
        """设置 HITL 审批广播回调。

        Args:
            callback: async callable(request_id, tool_name, params) -> None，
                      用于向前端广播审批请求。
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
        """执行完整安全管道（非流式）。

        Args:
            plugin: ToolPlugin 实例。
            name: 工具名称。
            ctx: ToolContext 运行时上下文。
            raw_params: 原始参数字典（未经校验）。
            semaphore: 并发控制信号量。

        Returns:
            ToolResult: 工具执行结果。
        """
        from Module.tool.protocol import ToolResult, ErrorType  # noqa: F811

        start = asyncio.get_event_loop().time()
        self._stats["total"] += 1

        logger.info("工具开始: %s(%s)", name, _format_args(raw_params))

        # 统一日志 + 返回
        def _finish(result):
            elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
            if result.success:
                logger.info("工具完成: %s — OK — %dms", name, elapsed)
            else:
                et = result.error_type.value if result.error_type else "UNKNOWN"
                logger.warning("工具失败: %s — %s — %s", name, et, result.error or "unknown")
            return result

        # 1. Pydantic 校验
        if plugin.param_model is not None:
            try:
                params = plugin.param_model(**raw_params)
            except ValidationError as e:
                self._stats["denied"] += 1
                return _finish(ToolResult.fail(
                    name=name,
                    error=_format_validation_error(e),
                    error_type=ErrorType.INVALID_ARGUMENT,
                ))
        else:
            params = None

        # 2. Safety check (URL, file paths)
        safety_err = await self._check_safety(plugin, ctx, raw_params)
        if safety_err:
            self._stats["denied"] += 1
            return _finish(ToolResult.fail(
                name=name, error=safety_err,
                error_type=ErrorType.PERMISSION_DENIED,
            ))

        # 3. HITL approval
        if plugin.safety and plugin.safety.requires_human_approval:
            hitl_ok, hitl_status = await self._run_hitl(plugin, name, raw_params)
            if not hitl_ok:
                return _finish(ToolResult.fail(
                    name=name,
                    error=f"操作被{'拒绝' if hitl_status == 'denied' else '超时' if hitl_status == 'timeout' else '过期'}",
                    error_type=ErrorType.PERMISSION_DENIED,
                ))

        # 4. Acquire semaphore
        if semaphore is not None:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=10.0)
            except asyncio.TimeoutError:
                self._stats["rate_limited"] += 1
                return _finish(ToolResult.fail(
                    name=name, error="并发限制，请稍后重试",
                    error_type=ErrorType.RATE_LIMITED,
                ))

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
                return _finish(ToolResult.fail(
                    name=name, error=f"执行超时 ({timeout}s)",
                    error_type=ErrorType.TIMEOUT,
                ))
            except asyncio.CancelledError:
                return _finish(ToolResult.fail(
                    name=name, error="用户取消",
                    error_type=ErrorType.CANCELLED,
                ))
        except Exception as e:
            return _finish(ToolResult.fail(
                name=name, error=str(e),
                error_type=ErrorType.TOOL_ERROR,
            ))
        finally:
            if semaphore is not None:
                semaphore.release()

        # 6. Wrap + truncate result
        return _finish(self._wrap_result(
            raw, name, asyncio.get_event_loop().time() - start,
            plugin.safety,
        ))

    async def execute_stream(
        self,
        plugin: Any,
        name: str,
        ctx: Any,
        raw_params: dict,
        semaphore: asyncio.Semaphore | None,
    ) -> AsyncIterator[Any]:
        """执行流式安全管道。

        Args:
            plugin: ToolPlugin 实例。
            name: 工具名称。
            ctx: ToolContext 运行时上下文。
            raw_params: 原始参数字典（未经校验）。
            semaphore: 并发控制信号量。

        Yields:
            ToolProgress | ToolResult: 中间进度事件或最终执行结果。
        """
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
        """检查 URL 和文件路径安全。

        Args:
            plugin: ToolPlugin 实例。
            ctx: ToolContext 运行时上下文。
            raw_params: 原始参数字典。

        Returns:
            错误字符串或 None（通过检查）。
        """
        profile = plugin.safety
        if profile is None:
            return None
        session_id = _sanitize_session_id(
            getattr(ctx, "session_id", "default") or "default"
        )

        # Network capability enforcement: plugins that access the network MUST declare Capability.NETWORK
        if profile.network_allowed:
            if Capability.NETWORK not in getattr(plugin, "capabilities", set()):
                return "插件声明了 network_allowed 但未声明 Capability.NETWORK"

        # URL safety: check params whose name contains "url" (e.g. url, source_url, api_url)
        if profile.network_allowed:
            for key, value in raw_params.items():
                if "url" in key.lower() and isinstance(value, str):
                    err = _validate_url(value, profile)
                    if err:
                        return err

        # Path safety: check params whose name contains "path" (e.g. path, file_path, output_path)
        if profile.filesystem_allowed:
            is_write = Capability.FILESYSTEM_WRITE in getattr(plugin, "capabilities", set())
            for key, value in raw_params.items():
                if "path" in key.lower() and isinstance(value, str):
                    resolved, err = _resolve_sandbox_path(
                        value, profile, session_id, is_write,
                    )
                    if err:
                        return err
                    err = _validate_path(resolved, profile)
                    if err:
                        return err
                    # 将验证后的安全路径注入 ctx
                    if key == "path":  # 只对主 "path" 参数注入 _safe_path
                        ctx.services["_safe_path"] = str(resolved)

        return None

    async def _run_hitl(
        self, plugin: Any, name: str, params: dict,
    ) -> tuple[bool, str]:
        """执行 HITL 审批流程。

        Args:
            plugin: ToolPlugin 实例。
            name: 工具名称。
            params: 工具参数。

        Returns:
            (是否批准, 状态字符串)，状态为 "confirmed" / "denied" / "timeout" / "expired"。
        """
        request_id = f"msg_{uuid.uuid4().hex[:26]}"
        event = asyncio.Event()
        self._hitl_requests[request_id] = (event, name, params)

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
        """将原始结果包装为 ToolResult 并进行 token 截断。

        Args:
            raw: 原始执行结果（ToolResult 或任意值）。
            name: 工具名称。
            duration_ms: 执行耗时（毫秒）。
            profile: 安全配置声明，用于 token 截断。

        Returns:
            ToolResult: 标准化工具执行结果。
        """
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
    """带超时的函数执行包装。

    Args:
        func: 异步 callable。
        kwargs: 传递给 func 的关键字参数。
        timeout: 超时秒数。

    Returns:
        func 的返回值。

    Raises:
        asyncio.TimeoutError: 超时。
    """
    return await asyncio.wait_for(func(**kwargs), timeout=timeout)


def truncate_result(result: Any, max_len: int) -> tuple[str, bool]:
    """str() 转换后截断到 max_len 字符。

    Args:
        result: 任意结果对象。
        max_len: 最大字符数。

    Returns:
        (截断后的字符串, 是否被截断)。
    """
    text = str(result)
    if len(text) <= max_len:
        return text, False
    return text[:max_len] + "…", True


def validate_args(params: dict[str, ToolParameter], raw_args: dict) -> tuple[dict, list[str]]:
    """类型转换 + 必填检查。

    Args:
        params: 参数定义映射（name → ToolParameter）。
        raw_args: 原始参数字典。

    Returns:
        (转换后的参数字典, 错误信息列表)。
    """
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
    """将 value 转换为 target_type。

    Args:
        name: 参数名（用于错误消息）。
        value: 原始值。
        target_type: 目标类型（"integer", "number", "boolean" 等）。

    Returns:
        (转换后的值, 错误消息或 None)。
    """
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
