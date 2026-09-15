"""工具安全层 — SafetyProfile 声明 + SafetyPipeline 执行中间件 + 旧版工具函数。"""

from __future__ import annotations

import asyncio
import uuid
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_session_id(session_id: str) -> str:
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

from Module._common.task_scope import cancellation_is_external
from Module.tool.capability import Capability
from Module.tool.protocol import ToolProgress

logger = logging.getLogger(__name__)

#: 兼容旧名（本模块内历史引用）。跨包一律用公开名——R3 禁止跨包导入下划线符号。
_sanitize_session_id = sanitize_session_id


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

    # 重试语义：声明"重复调用不产生额外副作用"（只读 / 可安全重入）。
    #
    # 调度器**只**在插件声明 True 时重试**运行时自己合成的超时**——两个合成点都算：
    # ① 本模块 `SafetyPipeline.execute` 的内层 `wait_for(request_timeout)`；
    # ② `scheduler._execute_tool` 的 `asyncio.TimeoutError` 分支。
    # 两处都在结果上置 `ToolResult.synthesised_timeout=True`，调度器的门控只读那一个
    # 字段（"谁合成的"由数据承载，不由代码位置推断）。
    #
    # 插件**自己返回**的可重试错误（`NETWORK_ERROR` / `RATE_LIMITED` / `TIMEOUT`）
    # 不受本字段影响——那是插件自己的陈述，照旧按 `ErrorType.is_retryable` 重试。
    #
    # 默认 False 是 fail-safe：忘记声明 → 少重试一次，而不是把副作用做两遍。
    idempotent: bool = False


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


# D4/P1-17：日志脱敏 deny-by-default 敏感字段（值绝不入日志）
_REDACTED_KEYS = frozenset(
    k.lower()
    for k in ("token", "key", "password", "secret", "authorization", "cookie",
              "content", "prompt", "path", "base_url", "url", "query", "api_key")
)
# review 复核 #9：deny-by-default——**所有**字符串值默认只记长度，仅显式列入本
# 白名单的 key 才记录原值（截断）。当前为空：任意字符串参数（如 Reminder 的
# message、正文类字段）值一律不落日志，杜绝工具参数内容泄露。
_SAFE_STRING_KEYS: frozenset[str] = frozenset()


def summarize_tool_arguments(raw_params: dict, max_len: int = 60) -> str:
    """工具参数的安全日志摘要（D4/P1-17）。

    默认只记录 key + 值类型/长度，不记录原值；敏感字段（token/key/password/
    secret/authorization/content/prompt/path/base_url/url 等）恒为 ``<redacted>``，
    其余字符串值 deny-by-default 只记 ``<str:长度>``（review 复核 #9——此前
    allowlist 脱敏会放行 Reminder 的 message 等字段原样入日志）。防止日志携带
    API token、聊天正文与完整路径。

    Args:
        raw_params: 原始参数字典。
        max_len: 白名单字符串值的最大展示长度。

    Returns:
        安全摘要字符串（如 "query=<redacted>, delay_seconds=<int>, message=<str:7>"）。
    """
    if not raw_params:
        return ""
    parts = []
    for k, v in raw_params.items():
        key = k.lower()
        if key in _REDACTED_KEYS:
            parts.append(f"{k}=<redacted>")
            continue
        if isinstance(v, str):
            if key in _SAFE_STRING_KEYS:
                s = v if len(v) <= max_len else v[:max_len] + "…"
                parts.append(f"{k}={s!r}")
            else:
                parts.append(f"{k}=<str:{len(v)}>")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
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
    # head_only（含流式强制场景）
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
        # S3-G4：改用出站守卫共用的**非 DNS** 判定。原先只试 `ipaddress.ip_address`，
        # 于是 `http://2130706433/`（十进制）、`http://0177.0.0.1/`（八进制）、
        # `http://metadata.google.internal/` 全部放行——`block_private_ips` 形同虚设。
        # 主机名形态（如 `localhost`）仍需 DNS 才能判定，由 `ssrf.validate_endpoint`
        # 与出站客户端逐跳校验兜住（见该函数的边界说明）。
        from Module._common.ssrf import literal_host_is_blocked
        if literal_host_is_blocked(parsed.hostname):
            return f"禁止访问内网地址: {parsed.hostname}"
    return None


def _expand_session_template(text: str, safe_sid: str) -> str:
    """`{session_id}` 模板的**唯一**替换点（读写共用）。

    S2-G4：读侧此前**不做替换**（只有写路径替换），于是配置里带模板的那一项
    **永不匹配**，真正放行一切的是同一份配置里的**裸根**——写隔离、读不隔离，
    多角色/多会话的文件层隔离被整体打穿。两份实现正是缺陷来源（RC-11：
    同一语义不得有两个真相源），故收口到这一个函数。
    """
    return text.replace("{session_id}", safe_sid)


def _read_dir_paths(profile: SafetyProfile, safe_sid: str) -> list[Path] | None:
    """读白名单展开为路径；`None` = **未配置**读白名单（保持既有"不限制读"语义）。

    返回的路径与模板一一对应，调用方若需要"哪一项是会话作用域的"可对照
    `profile.read_dirs` 判断（见 `_resolve_sandbox_path` 的相对路径分支）。
    """
    if not profile.read_dirs:
        return None
    return [Path(_expand_session_template(d, safe_sid)) for d in profile.read_dirs]


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
    safe_sid = sanitize_session_id(session_id)
    if not raw.is_absolute():
        if is_write:
            raw = Path(_expand_session_template(profile.write_dir, safe_sid)) / raw
        else:
            dirs = _read_dir_paths(profile, safe_sid)
            if dirs:
                # 相对路径落在**会话作用域**的那一项（S2-G4）：配置里若同时存在
                # 公共根，取 `read_dirs[0]` 会把相对读引到公共根，等于没隔离。
                scoped = next(
                    (d for d, tpl in zip(dirs, profile.read_dirs or [], strict=True)
                     if "{session_id}" in tpl),
                    dirs[0],
                )
                raw = scoped / raw
    try:
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return raw, f"路径解析失败: {e}"

    if is_write:
        write_dir = Path(
            _expand_session_template(profile.write_dir, safe_sid)
        ).resolve()
        # D1/P1-14：containment 用 is_relative_to——字符串 startswith 会把
        # /sandbox 误判为 /sandbox_evil 的父级（sibling 逃逸）。
        if not resolved.is_relative_to(write_dir):
            return resolved, f"写入路径必须在 '{write_dir}' 下"

    return resolved, None


def _path_has_symlink(path: Path) -> bool:
    """路径任意组件是否为符号链接（resolve 前判定）。

    D1/P1-14：resolved 已跟随 symlink（.resolve() deref），对 resolved 检查
    is_symlink() 会漏——/sandbox/link 指向外部时 resolved 是外部真实路径，
    但 link 本身是 symlink。必须在 resolve 前沿原始路径链逐组件检查。
    """
    p = path if path.is_absolute() else Path.cwd() / path
    current = Path(p.anchor)
    for part in p.parts[1:] if p.anchor else p.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _validate_path(
    resolved: Path, profile: SafetyProfile, raw: Path | None = None,
    safe_sid: str = "default",
) -> str | None:
    """路径安全检查。

    Args:
        resolved: 已解析的绝对路径。
        profile: 安全配置声明。
        raw: resolve 前的原始路径（symlink 判定用）；None 时跳过 symlink 检查。
        safe_sid: **已 sanitize** 的会话 id——读白名单的 `{session_id}` 模板在此展开
            （S2-G4）。调用方必须传当前调用的真实会话（`_check_safety` 已 sanitize）；
            默认值仅服务于无需会话的直调场景。

    Returns:
        None 表示通过检查，否则返回错误描述字符串。
    """
    path_str = str(resolved)

    read_dirs = _read_dir_paths(profile, safe_sid)
    if read_dirs is not None:
        # D1/P1-14：is_relative_to 替代 startswith（sibling 前缀不再误判）
        # S2-G4：白名单**必须**先替换 `{session_id}` 再比对——否则带模板的那一项
        # 永不匹配，跨会话读会被"配置里的裸根"放行（写隔离、读不隔离）。
        allowed = any(
            resolved.is_relative_to(d.resolve())
            for d in read_dirs
        )
        if not allowed:
            return f"路径 '{path_str}' 不在允许的目录中"

    if not profile.allow_symlinks:
        # D1/P1-14：resolved 本身是 symlink，或 resolve 前的原始路径链含 symlink
        # （resolve 已 deref，只查 resolved 会漏中间组件）→ 都拒绝
        if resolved.is_symlink() or (raw is not None and _path_has_symlink(raw)):
            return f"路径 '{path_str}' 包含符号链接，已被拒绝"

    if resolved.exists() and resolved.is_file():
        if resolved.stat().st_size > profile.max_file_bytes:
            return f"文件过大 ({resolved.stat().st_size} bytes)"

    return None


# ============================================================================
# SafetyPipeline — 执行中间件
# ============================================================================

def _origin_connection_id(ctx: Any) -> str:
    """从 ToolContext 取本次调用的发起 WS 连接 ID（CR-13）。

    `ToolContext.services["_origin_connection_id"]` 由 CognitiveLoop._prepare_turn
    每回合换绑（与 `services["session"]` 同构）。取不到（直接构造 ctx 的测试路径、
    v1/REST 来源）返回 ""——安全管道据此走"无绑定"分支并告警，不假装有归属。
    """
    services = getattr(ctx, "services", None)
    if not isinstance(services, dict):
        return ""
    value = services.get("_origin_connection_id", "")
    return value if isinstance(value, str) else ""


class SafetyPipeline:
    """工具执行中间件管道：validate → safety → hitl → execute → truncate。"""

    def __init__(self) -> None:
        """初始化安全管道。

        初始化 HITL 审批请求/结果字典、审批超时（默认 60s）、执行统计计数器。
        """
        # request_id → (event, tool_name, params, origin_connection_id)
        # CR-13：第 4 项是**发起该工具调用的 WS 连接 ID**（v2 握手分配；v1/REST 为
        # ""）。审批必须来自同一连接，否则多客户端下别人能从广播帧里拿到 request_id
        # 直接批准高危调用（对照：孪生路径 tool.status 早已有 sender 绑定）。
        self._hitl_requests: dict[str, tuple[asyncio.Event, str, dict, str]] = {}
        self._hitl_results: dict[str, tuple[bool, str]] = {}
        self._hitl_timeout: float = 60.0
        self._stats: dict[str, int] = {
            "total": 0, "timeout": 0, "denied": 0, "rate_limited": 0,
        }

    # ---- HITL public API ----

    def submit_approval(self, request_id: str, approved: bool,
                        approver_connection_id: str | None = None) -> bool:
        """前端提交审批结果（CR-13：绑定发起连接）。

        Args:
            request_id: 审批请求 ID。
            approved: True 表示批准，False 表示拒绝。
            approver_connection_id: 提交审批的连接 ID。与请求登记的发起连接不一致
                时**拒绝**（返回 False，不写入结果、不唤醒等待方）。发起连接未知
                （v1/REST 发起，登记为 ""）时不做连接校验——此时无法判定归属，
                记 warning 留痕而不是假装安全。

        Returns:
            True 如果找到了对应的审批请求且提交方有权处理。
        """
        entry = self._hitl_requests.get(request_id)
        if entry is None:
            return False
        origin = entry[3]
        if origin:
            if approver_connection_id != origin:
                logger.warning(
                    "HITL 审批被拒：request=%s 来自连接 %s，但发起连接是 %s",
                    request_id, approver_connection_id or "<未知>", origin)
                return False
        else:
            logger.warning(
                "HITL 审批无连接绑定（request=%s，发起方未提供连接 ID）——"
                "多客户端下无法判定审批归属", request_id)
        self._hitl_results[request_id] = (
            approved, "confirmed" if approved else "denied",
        )
        entry[0].set()
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
            for rid, (event, tool_name, params, _origin) in self._hitl_requests.items()
            if not event.is_set()
        ]

    def set_hitl_broadcast(self, callback: Callable[..., Any]) -> None:
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

        logger.info("工具开始: %s(%s)", name, summarize_tool_arguments(raw_params))

        # 统一日志 + 返回
        def _finish(result: Any):
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
            hitl_ok, hitl_status = await self._run_hitl(
                plugin, name, raw_params, _origin_connection_id(ctx))
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
                # 合成点①：这个 TIMEOUT 是**管道自己**写的（内层预算
                # `plugin.safety.request_timeout` 到期），不是插件的陈述——必须置
                # `synthesised_timeout=True`，否则它以"插件返回的结果"形态到达调度器，
                # 未声明 `SafetyProfile.idempotent` 的工具会被同一个 `call_id` 再
                # invoke 一次（副作用落地两遍）。而这条内层预算几乎总是先于调度器的
                # `_timeout_for` 触发（= max(60, declared+5) > declared）。
                return _finish(ToolResult.fail(
                    name=name, error=f"执行超时 ({timeout}s)",
                    error_type=ErrorType.TIMEOUT, synthesised_timeout=True,
                ))
            except asyncio.CancelledError:
                if cancellation_is_external():
                    # S2-R2-1：**不得**把外部取消归一为业务结果——吞掉它会让外层
                    # `wait_for` 失去超时语义（见 _cancellation_is_external 注释），
                    # 把"超时"伪报成"用户取消"、跳过重试、并把任务标成成功。
                    raise
                # 插件内部自取消（非本任务被取消）才是真正的业务结果
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

        # 预检查
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
            hitl_ok, _ = await self._run_hitl(
                plugin, name, raw_params, _origin_connection_id(ctx))
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

        # 流式执行
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
            if cancellation_is_external():
                # 与 execute() 同一口径（S2-R2-1）：外部取消必须原样上抛。
                # 本方法当前**无生产调用方**（见报告"工具系统的重要正面结论"），
                # 但两条路径的取消语义不得分叉。
                raise
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
        session_id = sanitize_session_id(
            getattr(ctx, "session_id", "default") or "default"
        )

        # 网络能力强制：访问网络的插件必须声明 Capability.NETWORK
        if profile.network_allowed:
            if Capability.NETWORK not in getattr(plugin, "capabilities", set()):
                return "插件声明了 network_allowed 但未声明 Capability.NETWORK"

        # URL 安全：检查名称包含 "url" 的参数（如 url、source_url、api_url）
        if profile.network_allowed:
            for key, value in raw_params.items():
                if "url" in key.lower() and isinstance(value, str):
                    err = _validate_url(value, profile)
                    if err:
                        return err

        # 路径安全：检查名称包含 "path" 的参数（如 path、file_path、output_path）
        if profile.filesystem_allowed:
            is_write = Capability.FILESYSTEM_WRITE in getattr(plugin, "capabilities", set())
            for key, value in raw_params.items():
                if "path" in key.lower() and isinstance(value, str):
                    # D1/P1-14：传 raw（resolve 前）供 symlink 判定——resolve 已 deref
                    resolved, err = _resolve_sandbox_path(
                        value, profile, session_id, is_write,
                    )
                    if err:
                        return err
                    err = _validate_path(
                        resolved, profile, raw=Path(value), safe_sid=session_id)
                    if err:
                        return err
                    # 将验证后的安全路径注入 ctx
                    if key == "path":  # 只对主 "path" 参数注入 _safe_path
                        ctx.services["_safe_path"] = str(resolved)

        return None

    async def _run_hitl(
        self, plugin: Any, name: str, params: dict, origin_connection_id: str = "",
    ) -> tuple[bool, str]:
        """执行 HITL 审批流程。

        Args:
            plugin: ToolPlugin 实例。
            name: 工具名称。
            params: 工具参数。
            origin_connection_id: 发起本次工具调用的 WS 连接 ID（CR-13）。登记后
                审批只接受同一连接提交；"" 表示发起方无连接身份（v1/REST）。

        Returns:
            (是否批准, 状态字符串)，状态为 "confirmed" / "denied" / "timeout" / "expired"。
        """
        request_id = f"msg_{uuid.uuid4().hex[:26]}"
        event = asyncio.Event()
        self._hitl_requests[request_id] = (event, name, params, origin_connection_id)

        # 广播审批请求 — 由 ToolManager 通过 callback 注入。发起连接已知时**只发给
        # 它**：审批弹窗不该出现在无关客户端上（信息面与操作面同时收窄）。
        if hasattr(self, "_broadcast_hitl"):
            await self._broadcast_hitl(
                request_id, name, params, origin_connection_id)

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
