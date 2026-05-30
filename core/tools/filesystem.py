"""文件系统工具 — 读文件、浏览目录。"""

import os
import stat
import time
from pathlib import Path

from Module.tool import ToolContext, tool


# 按路径前缀拒绝的模式（子串匹配完整路径）
_DENIED_PATHS: list[str] = [
    "/etc/shadow",
    "/etc/passwd",
    "/proc",
    "/sys",
]

# 按文件名/目录名拒绝的 dotfile 模式（逐级路径组件匹配）
_DENIED_NAMES: list[str] = [
    ".env",
    ".ssh",
    ".gnupg",
]


def _resolve(path: str) -> Path:
    """解析用户路径（~，相对路径）为绝对 Path。"""
    return Path(path).expanduser().resolve()


def _validate(path: str, ctx: ToolContext | None = None) -> str | None:
    """校验路径安全性。返回错误消息或 None（通过）。"""
    resolved = _resolve(path)
    path_str = str(resolved)

    # 从 ctx 读取自定义黑名单
    extra_denied = ctx.get("filesystem_denied", []) if ctx else []
    denied_paths = list(_DENIED_PATHS) + [p for p in extra_denied if p.startswith("/")]
    denied_names = list(_DENIED_NAMES) + [p for p in extra_denied if p.startswith(".")]

    for pattern in denied_paths:
        if pattern in path_str:
            return f"安全限制: 禁止访问 '{pattern}' 相关路径"

    for pattern in denied_names:
        for part in resolved.parts:
            if part == pattern or part.startswith(pattern + "."):
                return f"安全限制: 禁止访问 '{pattern}' 文件/目录"

    if not resolved.exists():
        return f"路径不存在: {path}"

    return None


@tool(description="读取指定文件的内容（仅文本文件）", requires=["filesystem:read"])
async def read_file(
    path: str,
    start_line: int = 0,
    max_lines: int = 200,
    ctx: ToolContext = None,
) -> str:
    """读取文本文件，返回带行号的内容。

    :param path: 文件路径
    :param start_line: 起始行号（从 0 开始）
    :param max_lines: 最多读取行数
    """
    err = _validate(path, ctx)
    if err:
        return err

    resolved = _resolve(path)
    if not resolved.is_file():
        return f"不是文件: {path}"

    # 拒绝二进制文件
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            f.read(1024)
    except UnicodeDecodeError:
        return f"无法读取（可能是二进制文件）: {path}"

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取失败: {e}"

    total = len(lines)
    end = min(start_line + max_lines, total)
    selected = lines[start_line:end]

    result: list[str] = []
    for i, line in enumerate(selected, start=start_line + 1):
        result.append(f"{i:4d} | {line.rstrip()}")

    output = "\n".join(result)
    if end < total:
        output += f"\n\n…[第 {end+1}-{total} 行未显示，共 {total} 行]"
    if not output.strip():
        output = "（文件为空）"
    return output


@tool(description="列出目录中的文件和子目录", requires=["filesystem:read"])
async def list_directory(
    path: str = ".",
    depth: int = 1,
    ctx: ToolContext = None,
) -> str:
    """列出目录内容，含文件大小和修改时间。

    :param path: 目录路径（默认当前目录）
    :param depth: 递归深度（1=仅当前层，最大 3）
    """
    err = _validate(path, ctx)
    if err:
        return err

    resolved = _resolve(path)
    if not resolved.is_dir():
        return f"不是目录: {path}"

    depth = max(1, min(depth, 3))

    def _walk(p: Path, d: int) -> list[str]:
        lines: list[str] = []
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return [f"{'  ' * (depth - d)}[权限不足] {p}"]

        prefix = "  " * (depth - d)
        for entry in entries:
            try:
                st = entry.stat()
            except OSError:
                lines.append(f"{prefix}{entry.name}/ (?)")
                continue

            mtime = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
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


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024*1024):.1f}M"
    else:
        return f"{size / (1024*1024*1024):.1f}G"
