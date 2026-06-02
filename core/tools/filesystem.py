"""文件系统工具插件 — 读取文件和列出目录。"""

import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


def _format_size(size: int) -> str:
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
    start_line: int = Field(default=0, ge=0, description="起始行号（0-indexed）")
    max_lines: int = Field(default=200, ge=1, le=500, description="最大读取行数")


class ReadFilePlugin(ToolPlugin):
    name = "read_file"
    description = "读取指定文件的内容（仅文本文件）"
    param_model = ReadFileParams
    capabilities = {Capability.FILESYSTEM_READ}
    safety = SafetyProfile(
        filesystem_allowed=True,
        read_dirs=["data/sandbox"],
        max_file_bytes=10 * 1024 * 1024,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ReadFileParams,
    ) -> str:
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_file():
            return f"不是文件: {path}"

        # 文件大小预检
        try:
            fsize = resolved.stat().st_size
            if fsize > self.safety.max_file_bytes:
                return f"文件过大 ({fsize} bytes)，最大允许 {self.safety.max_file_bytes} bytes"
        except OSError as e:
            return f"无法读取文件信息: {e}"

        # 二进制检测 + 逐行读取（不预加载全部到内存）
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                # 二进制探针：读取首个 1KB 触发 UnicodeDecodeError
                probe = f.read(1024)
                # 回到文件开头
                f.seek(0)

                result_lines: list[str] = []
                i = 0
                for i, line in enumerate(f):
                    if i < params.start_line:
                        continue
                    if len(result_lines) >= params.max_lines:
                        break
                    result_lines.append(f"{i+1:4d} | {line.rstrip()}")
                total = i + 1  # 文件总行数
        except UnicodeDecodeError:
            return f"无法读取（可能是二进制文件）: {path}"
        except Exception as e:
            return f"读取失败: {e}"

        output = "\n".join(result_lines)
        if not output.strip():
            output = "（文件为空）"
            return output

        last_shown = params.start_line + len(result_lines)
        if last_shown < total:
            output += f"\n\n…[显示 {params.start_line+1}-{last_shown} 行，共 {total} 行]"

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
        read_dirs=["data/sandbox"],
        max_dir_depth=3,
        allow_symlinks=False,
    )

    async def execute(
        self, *, ctx: ToolContext, emit=None, params: ListDirParams,
    ) -> str:
        path = ctx.get("_safe_path") or params.path
        resolved = Path(path)

        if not resolved.exists():
            return f"路径不存在: {path}"
        if not resolved.is_dir():
            return f"不是目录: {path}"

        depth = max(1, min(params.depth, self.safety.max_dir_depth))
        visited: set[int] = set()  # inode set — 防符号链接循环

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
