"""文件系统工具插件 — 读取文件、列出目录、写入文件。"""

import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from paths import ProjectPath

from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter
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
# read_file
# ============================================================================

class ReadFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对或绝对）")
    start_line: int = Field(default=0, ge=0, description="起始行号（1-indexed，1 表示第一行）。设为 0 等效于 1")
    max_lines: int = Field(default=200, ge=1, le=500, description="最大读取行数")


class ReadFilePlugin(ToolPlugin):
    name = "read_file"
    description = "读取指定文件的内容（仅文本文件）"
    param_model = ReadFileParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_file_bytes=10 * 1024 * 1024,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ReadFileParams,
    ) -> str:
        """读取指定文件的内容。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 读取参数，包含路径、起始行和最大行数。

        Returns:
            文件内容字符串，或错误信息。
        """
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_file():
            return f"不是文件: {path}"

        try:
            fsize = resolved.stat().st_size
            if fsize > self.safety.max_file_bytes:
                return f"文件过大 ({fsize} bytes)，最大允许 {self.safety.max_file_bytes} bytes"
        except OSError as e:
            return f"无法读取文件信息: {e}"

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
            return f"无法读取（可能是二进制文件）: {path}"
        except Exception as e:
            return f"读取失败: {e}"

        output = "\n".join(result_lines)
        if not output.strip():
            output = "（文件为空）"
            return output

        last_shown = start - 1 + len(result_lines)
        if last_shown < total:
            output += f"\n\n…[显示 {start}-{last_shown} 行，共 {total} 行]"

        return output


# ============================================================================
# list_directory
# ============================================================================

class ListDirParams(BaseModel):
    path: str = Field(default=".", description="目录路径")
    depth: int = Field(default=1, ge=1, le=3, description="遍历深度，1-3")


class ListDirPlugin(ToolPlugin):
    name = "list_directory"
    description = "列出目录中的文件和子目录"
    param_model = ListDirParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["/home/yomu/data/sandbox"],
        max_dir_depth=3,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ListDirParams,
    ) -> str:
        """列出目录中的文件和子目录。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 列表参数，包含路径和遍历深度。

        Returns:
            目录列表字符串，或错误信息。
        """
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_dir():
            return f"不是目录: {path}"

        depth = max(1, min(params.depth, self.safety.max_dir_depth))
        visited: set[int] = set()

        def _walk(p: Path, d: int) -> list[str]:
            lines: list[str] = []
            inode = p.stat().st_ino if not p.is_symlink() else None
            if inode and inode in visited:
                return [f"{'  ' * (depth - d)}[循环] {p}"]
            if inode:
                visited.add(inode)

            try:
                entries = sorted(
                    p.iterdir(),
                    key=lambda x: (not x.is_dir(), x.name.lower()),
                )
            except PermissionError:
                return [f"{'  ' * (depth - d)}[权限不足] {p}"]

            prefix = "  " * (depth - d)
            for entry in entries:
                try:
                    st = entry.stat()
                except OSError:
                    lines.append(f"{prefix}{entry.name}/ (?)")
                    continue

                mtime = time.strftime(
                    "%m-%d %H:%M", time.localtime(st.st_mtime),
                )
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/ ({mtime})")
                    if d > 1:
                        lines.extend(_walk(entry, d - 1))
                else:
                    size = _format_size(st.st_size)
                    lines.append(f"{prefix}{entry.name}  {size}  {mtime}")
            return lines

        lines = [f"目录: {resolved}\n"]
        lines.extend(_walk(resolved, depth))
        output = "\n".join(lines)
        if len(output) > 6000:
            output = output[:6000] + "\n\n…[输出已截断]"
        return output


# ============================================================================
# write_file
# ============================================================================

class WriteFileParams(BaseModel):
    path: str = Field(..., description="文件路径（相对路径，自动限定在 sandbox 目录下）")
    content: str = Field(..., description="要写入的内容")


class WriteFilePlugin(ToolPlugin):
    name = "write_file"
    description = "在安全目录下写入文件（需用户确认）"
    param_model = WriteFileParams
    capabilities = {Capability.FILESYSTEM_WRITE}
    safety = SafetyProfile(
        filesystem_allowed=True,
        write_dir=ProjectPath.DATA_SANDBOX,
        max_file_bytes=10 * 1024 * 1024,
        requires_human_approval=True,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: WriteFileParams,
    ) -> str:
        """在安全目录下写入文件。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 写入参数，包含路径和内容。

        Returns:
            写入结果字符串。
        """
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        session_id = ctx.session_id or "default"
        safe_sid = session_id.replace("/", "_").replace("\\", "_")
        write_dir = Path(
            self.safety.write_dir.replace("{session_id}", safe_sid)
        ).resolve()

        if not (
            str(resolved) == str(write_dir)
            or str(resolved).startswith(str(write_dir) + os.sep)
        ):
            return f"拒绝写入: 路径 '{params.path}' 越过了 sandbox 边界"

        resolved.parent.mkdir(parents=True, exist_ok=True)

        try:
            resolved.write_text(params.content, encoding="utf-8")
            size = resolved.stat().st_size
            return f"已写入 {resolved.name} ({size} bytes)"
        except Exception as e:
            return f"写入失败: {e}"
