"""文件系统工具插件 — 读取文件、列出目录、写入文件。"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from paths import ProjectPath

from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter, tool_failure, ToolResult
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


def _format_size(size: int) -> str:
    """将字节数格式化为人类可读的大小字符串。

    Args:
        size: 字节数。

    Returns:
        格式化后的大小字符串（如 "1.5M"）。
    """
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024*1024):.1f}M"
    else:
        return f"{size / (1024*1024*1024):.1f}G"


# ============================================================================
# read_file（读取文件）
# ============================================================================

class ReadFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对或绝对）")
    start_line: int = Field(default=0, ge=0, description="起始行号（1-indexed，1 表示第一行）。设为 0 等效于 1")
    max_lines: int = Field(default=200, ge=1, le=500, description="最大读取行数")


def _sandbox_roots(profile: SafetyProfile, safe_sid: str, *,
                   write: bool) -> list[Path]:
    """沙盒边界（write → 仅 write_dir；read → write_dir + read_dirs）。"""
    roots: list[str] = []
    if write:
        roots.append(profile.write_dir)
    else:
        if profile.read_dirs:
            roots.extend(profile.read_dirs)
        roots.append(profile.write_dir)
    out: list[Path] = []
    for r in roots:
        try:
            out.append(Path(r.replace("{session_id}", safe_sid)).resolve())
        except (OSError, RuntimeError):
            continue
    return out


def resolve_in_sandbox(raw: str, *, profile: SafetyProfile, session_id: str,
                       write: bool) -> tuple[Path | None, str | None]:
    """解析路径并做沙盒 containment 检查（S2-6）。

    为什么插件自己要查：`_safe_path` 只在 `profile.filesystem_allowed` 时由
    `SafetyPipeline` 注入；插件被直接调用（测试 / 直接执行路径）时拿到的是
    **原始用户输入**。而原先的插件内检查是 `Path(path)`（**未 resolve**）与已
    `resolve()` 的 `write_dir` 比较——`PurePath.is_relative_to` 只比路径部件：

    - `<sandbox>/../../etc/x` 的部件以 `('/', 'sandbox')` 开头 → **判为"在沙盒内"**，
      而实际写入由内核按 `..` 解析，落到沙盒外（实测逃逸成功）；
    - 反过来，经 symlink 到达沙盒的**合法**路径（`/tmp/link → sandbox`）因为未解析
      而与被解析的 write_dir 不匹配 → **误拒**。

    先 `resolve()` 再比较，两个方向同时修好。

    Args:
        raw: 原始路径（可能相对、可能含 `..`）。
        profile: 该插件的安全配置。
        session_id: 会话 ID（`{session_id}` 模板替换）。
        write: True=写路径（只允许 write_dir），False=读路径（read_dirs + write_dir）。

    Returns:
        (resolved_path, None) 或 (None, 拒绝原因)。
    """
    from Module.tool.safety import sanitize_session_id

    safe_sid = sanitize_session_id(session_id or "default")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        base = _sandbox_roots(profile, safe_sid, write=write)
        if not base:
            return None, "拒绝访问: 未配置沙盒边界"
        p = base[0] / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as e:
        return None, f"路径解析失败: {e}"

    roots = _sandbox_roots(profile, safe_sid, write=write)
    if not roots or not any(resolved.is_relative_to(r) for r in roots):
        return None, f"拒绝访问: 路径 '{raw}' 越过了 sandbox 边界"
    return resolved, None


class ReadFilePlugin(ToolPlugin):
    name = "read_file"
    description = "读取指定文件的内容（仅文本文件）"
    param_model = ReadFileParams
    capabilities = {Capability.FILESYSTEM_READ}
    risk_level = "safe"  # 沙盒只读（§3.3）
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_file_bytes=10 * 1024 * 1024,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ReadFileParams,
    ) -> str | ToolResult:
        """读取指定文件的内容。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 读取参数，包含路径、起始行和最大行数。

        Returns:
            文件内容字符串，或错误信息。
        """
        path = ctx.get("_safe_path") or params.path
        # S2-6：插件自己也做 containment（`_safe_path` 只在 filesystem_allowed 时注入）
        resolved, err = resolve_in_sandbox(
            path, profile=self.safety, session_id=ctx.session_id or "default",
            write=False)
        if err or resolved is None:
            return tool_failure(err or "路径不可用",
                                error_type=ErrorType.PERMISSION_DENIED)

        if not resolved.exists():
            return tool_failure(f"路径不存在: {path}",
                                error_type=ErrorType.INVALID_ARGUMENT)
        if not resolved.is_file():
            return tool_failure(f"不是文件: {path}",
                                error_type=ErrorType.INVALID_ARGUMENT)

        try:
            fsize = resolved.stat().st_size
            if fsize > self.safety.max_file_bytes:
                return tool_failure(
                    f"文件过大 ({fsize} bytes)，最大允许 {self.safety.max_file_bytes} bytes",
                    error_type=ErrorType.INVALID_ARGUMENT)
        except OSError as e:
            return tool_failure(f"无法读取文件信息: {e}", error_type=ErrorType.TOOL_ERROR)

        try:
            with open(resolved, "r", encoding="utf-8") as f:
                probe = f.read(1024)
                f.seek(0)

                result_lines: list[str] = []
                i = 0
                start = max(params.start_line, 1)  # 归一化到 1-indexed
                for i, line in enumerate(f):
                    if i + 1 < start:  # i 是 0-indexed，start 是 1-indexed
                        continue
                    if len(result_lines) >= params.max_lines:
                        break
                    result_lines.append(f"{i+1:4d} | {line.rstrip()}")
                total = i + 1
        except UnicodeDecodeError:
            return tool_failure(f"无法读取（可能是二进制文件）: {path}", error_type=ErrorType.INVALID_ARGUMENT)
        except Exception as e:
            return tool_failure(f"读取失败: {e}", error_type=ErrorType.TOOL_ERROR)

        output = "\n".join(result_lines)
        if not output.strip():
            output = "（文件为空）"
            return output

        last_shown = start - 1 + len(result_lines)
        if last_shown < total:
            output += f"\n\n…[显示 {start}-{last_shown} 行，共 {total} 行]"

        return output


# ============================================================================
# list_directory（列目录）
# ============================================================================

class ListDirParams(BaseModel):
    path: str = Field(default=".", description="目录路径")
    depth: int = Field(default=1, ge=1, le=3, description="遍历深度，1-3")


class ListDirPlugin(ToolPlugin):
    name = "list_directory"
    description = "列出目录中的文件和子目录"
    param_model = ListDirParams
    capabilities = {Capability.FILESYSTEM_READ}
    risk_level = "safe"  # 沙盒只读（§3.3）
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_dir_depth=3,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ListDirParams,
    ) -> str | ToolResult:
        """列出目录中的文件和子目录。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 列表参数，包含路径和遍历深度。

        Returns:
            目录列表字符串，或错误信息。
        """
        path = ctx.get("_safe_path") or params.path
        resolved, err = resolve_in_sandbox(
            path, profile=self.safety, session_id=ctx.session_id or "default",
            write=False)
        if err or resolved is None:
            return tool_failure(err or "路径不可用",
                                error_type=ErrorType.PERMISSION_DENIED)

        if not resolved.exists():
            return tool_failure(f"路径不存在: {path}",
                                error_type=ErrorType.INVALID_ARGUMENT)
        if not resolved.is_dir():
            return tool_failure(f"不是目录: {path}",
                                error_type=ErrorType.INVALID_ARGUMENT)

        depth = max(1, min(params.depth, self.safety.max_dir_depth))
        allow_symlinks = self.safety.allow_symlinks
        visited: set[int] = set()
        # D1/P1-14 + P2-05：遍历过程限制节点数/输出字节（不能先收集整棵树再截断），
        # 且 allow_symlinks=False 时跳过全部 symlink entry（不进入 symlink 目录）。
        node_budget = getattr(self.safety, "max_list_nodes", 500)
        output_budget = 6000
        collected: list[str] = []
        exhausted = False

        def _walk(p: Path, d: int) -> None:
            nonlocal exhausted, node_budget
            if exhausted or node_budget <= 0:
                return
            if p.is_symlink() and not allow_symlinks:
                return  # D1：不进入 symlink 目录
            inode: int | None = None
            try:
                if not p.is_symlink():
                    inode = p.stat().st_ino
            except OSError:
                pass
            if inode and inode in visited:
                collected.append(f"{'  ' * (depth - d)}[循环] {p}")
                return
            if inode:
                visited.add(inode)

            try:
                entries = sorted(
                    p.iterdir(),
                    key=lambda x: (not x.is_dir(), x.name.lower()),
                )
            except PermissionError:
                collected.append(f"{'  ' * (depth - d)}[权限不足] {p}")
                return

            prefix = "  " * (depth - d)
            for entry in entries:
                node_budget -= 1
                if node_budget <= 0:
                    exhausted = True
                    collected.append("…[目录节点数已达上限，已停止遍历]")
                    return
                if entry.is_symlink() and not allow_symlinks:
                    continue  # D1：不列出 symlink（含文件与目录）
                try:
                    st = entry.stat()
                except OSError:
                    collected.append(f"{prefix}{entry.name}/ (?)")
                    continue

                mtime = time.strftime(
                    "%m-%d %H:%M", time.localtime(st.st_mtime),
                )
                if entry.is_dir() and not entry.is_symlink():
                    collected.append(f"{prefix}{entry.name}/ ({mtime})")
                    if d > 1:
                        _walk(entry, d - 1)
                else:
                    size = _format_size(st.st_size)
                    collected.append(f"{prefix}{entry.name}  {size}  {mtime}")
                if sum(len(x) for x in collected) > output_budget:
                    exhausted = True
                    collected.append("…[输出已截断]")
                    return

        # D1/P1-14：不泄露允许根之外的绝对路径——只回显调用方提供的路径参数
        collected.append(f"目录: {params.path}")
        _walk(resolved, depth)
        return "\n".join(collected)


# ============================================================================
# write_file（写文件）
# ============================================================================

class WriteFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对路径，自动限定在 sandbox 目录下）")
    content: str = Field(..., description="要写入的内容")


class WriteFilePlugin(ToolPlugin):
    name = "write_file"
    description = "在安全目录下写入文件（需用户确认）"
    param_model = WriteFileParams
    capabilities = {Capability.FILESYSTEM_WRITE}
    risk_level = "high"  # 写沙盒 + requires_human_approval=True（§3.3）
    safety = SafetyProfile(
        filesystem_allowed=True,
        write_dir=ProjectPath.DATA_SANDBOX,
        max_file_bytes=10 * 1024 * 1024,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: WriteFileParams,
    ) -> str | ToolResult:
        """在安全目录下写入文件。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 写入参数，包含路径和内容。

        Returns:
            写入结果字符串。
        """
        path = ctx.get("_safe_path") or params.path
        # S2-6：**先 resolve 再 containment**。原先 `Path(path)` 未解析却与已解析的
        # write_dir 比 `is_relative_to`——词法比较让 `<sandbox>/../../etc/x` 通过
        # （部件前缀相同），实际写入落到沙盒外；symlink 到达沙盒的合法路径则被误拒。
        resolved, err = resolve_in_sandbox(
            path, profile=self.safety, session_id=ctx.session_id or "default",
            write=True)
        if err or resolved is None:
            return tool_failure(err or "路径不可用",
                                error_type=ErrorType.PERMISSION_DENIED)
        try:
            content_bytes = params.content.encode("utf-8")
        except UnicodeEncodeError:
            return tool_failure("写入失败: 内容无法编码为 UTF-8", error_type=ErrorType.INVALID_ARGUMENT)
        if len(content_bytes) > self.safety.max_file_bytes:
            return tool_failure(
                f"内容过大 ({len(content_bytes)} bytes)，"
                f"最大允许 {self.safety.max_file_bytes} bytes",
                error_type=ErrorType.INVALID_ARGUMENT)

        resolved.parent.mkdir(parents=True, exist_ok=True)

        try:
            # P2-05：阻塞文件 I/O 放线程池，避免阻塞 event loop
            await asyncio.to_thread(resolved.write_text, params.content, "utf-8")
            size = resolved.stat().st_size
            return f"已写入 {resolved.name} ({size} bytes)"
        except Exception as e:
            return tool_failure(f"写入失败: {e}", error_type=ErrorType.TOOL_ERROR)
