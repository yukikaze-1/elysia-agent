"""ReadFilePlugin 和 ListDirPlugin 单元测试。"""

import pytest

from Module.tool.protocol import ToolContext

from core.tools.filesystem.plugin import (
    _format_size,
    ReadFilePlugin,
    ReadFileParams,
    ListDirPlugin,
    ListDirParams,
)

pytestmark = pytest.mark.service

# ============================================================================
# _format_size
# ============================================================================

class TestFormatSize:
    """_format_size 将字节数转换为人类可读的字符串格式。"""

    def test_bytes_under_1024(self):
        """小于 1024 字节时返回 'NB' 格式。"""
        assert _format_size(500) == "500B"

    def test_kilobytes(self):
        """1024 到 1MB 之间返回 'N.NK' 格式。"""
        # 1500 / 1024 = 1.464... → 1.5K
        assert _format_size(1500) == "1.5K"

    def test_megabytes(self):
        """1MB 到 1GB 之间返回 'N.NM' 格式。"""
        size = 5 * 1024 * 1024  # 5 MiB
        assert _format_size(size) == "5.0M"

    def test_gigabytes(self):
        """1GB 及以上返回 'N.NG' 格式。"""
        size = 3 * 1024 * 1024 * 1024  # 3 GiB
        assert _format_size(size) == "3.0G"


# ============================================================================
# ReadFilePlugin
# ============================================================================

class TestReadFilePlugin:
    """ReadFilePlugin — 读取文件内容并返回带行号的文本。"""

    @staticmethod
    def _make_ctx(path: str) -> ToolContext:
        """创建注入了 _safe_path 的 ToolContext。"""
        ctx = ToolContext()
        ctx.services["_safe_path"] = path
        return ctx

    # ── 基本功能 ──

    @pytest.mark.asyncio
    async def test_reads_file_content_with_line_numbers(self, tmp_path):
        """读取文件内容，每行带有 4 位右对齐行号。"""
        file = tmp_path / "hello.txt"
        file.write_text("hello\nworld\n")

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ReadFileParams(path=str(file)),
        )

        assert "   1 | hello" in result
        assert "   2 | world" in result

    # ── 路径校验 ──

    @pytest.mark.asyncio
    async def test_nonexistent_path(self, tmp_path):
        """不存在的路径返回 '路径不存在'。"""
        path = str(tmp_path / "nonexistent.txt")

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(path),
            emit=None,
            params=ReadFileParams(path=path),
        )

        assert "路径不存在" in result

    @pytest.mark.asyncio
    async def test_not_a_file_when_path_is_directory(self, tmp_path):
        """路径是目录时返回 '不是文件'。"""
        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(tmp_path)),
            emit=None,
            params=ReadFileParams(path=str(tmp_path)),
        )

        assert "不是文件" in result

    # ── 边界情况 ──

    @pytest.mark.asyncio
    async def test_binary_file(self, tmp_path):
        """二进制文件（UTF-8 解码失败）返回 '二进制文件' 提示。"""
        file = tmp_path / "data.bin"
        # 写入无效 UTF-8 字节以触发 UnicodeDecodeError
        file.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ReadFileParams(path=str(file)),
        )

        assert "二进制文件" in result

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path):
        """空文件返回 '文件为空'。"""
        file = tmp_path / "empty.txt"
        file.write_text("")

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ReadFileParams(path=str(file)),
        )

        assert "文件为空" in result

    # ── 分页（start_line / max_lines）──

    @pytest.mark.asyncio
    async def test_start_line_skips_lines(self, tmp_path):
        """start_line 参数跳过指定行数后开始输出，行号从 start_line+1 开始。"""
        file = tmp_path / "lines.txt"
        lines = [f"line{i}" for i in range(10)]
        file.write_text("\n".join(lines))

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ReadFileParams(path=str(file), start_line=5),
        )

        # start_line=5 应从第 6 行开始（0-indexed 跳过前 5 行）
        assert "   6 | line5" in result
        assert "line0" not in result

    @pytest.mark.asyncio
    async def test_max_lines_limits_output(self, tmp_path):
        """max_lines 参数限制输出行数。"""
        file = tmp_path / "many.txt"
        file.write_text("\n".join(f"line{i}" for i in range(20)))

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ReadFileParams(path=str(file), max_lines=5),
        )

        # 只应输出 5 行内容（带行号和 '|' 分隔符）
        content_lines = [l for l in result.split("\n") if "|" in l]
        assert len(content_lines) == 5

    @pytest.mark.asyncio
    async def test_truncation_notice_when_file_has_more_lines(self, tmp_path):
        """文件行数超出 max_lines 时，末尾显示 '…' 截断提示。"""
        file = tmp_path / "large.txt"
        file.write_text("\n".join(f"line{i}" for i in range(300)))

        plugin = ReadFilePlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            # 默认 max_lines=200，文件 300 行 → 触发截断
            params=ReadFileParams(path=str(file)),
        )

        assert "…" in result
        assert "行" in result  # 截断提示包含行数统计


# ============================================================================
# ListDirPlugin
# ============================================================================

class TestListDirPlugin:
    """ListDirPlugin — 列出目录内容，含大小和时间戳。"""

    @staticmethod
    def _make_ctx(path: str) -> ToolContext:
        ctx = ToolContext()
        ctx.services["_safe_path"] = path
        return ctx

    # ── 基本功能 ──

    @pytest.mark.asyncio
    async def test_lists_files_and_dirs_with_size_and_timestamp(self, tmp_path):
        """列出文件（含大小和时间戳）和子目录（含 / 后缀）。"""
        file = tmp_path / "readme.txt"
        file.write_text("hello")
        subdir = tmp_path / "sub"
        subdir.mkdir()

        plugin = ListDirPlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(tmp_path)),
            emit=None,
            params=ListDirParams(path=str(tmp_path)),
        )

        assert "目录:" in result
        assert "readme.txt" in result
        assert "sub/" in result
        # 文件应显示大小
        assert "B" in result or "K" in result or "M" in result

    # ── 路径校验 ──

    @pytest.mark.asyncio
    async def test_nonexistent_path(self, tmp_path):
        """不存在的路径返回 '路径不存在'。"""
        path = str(tmp_path / "ghost")

        plugin = ListDirPlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(path),
            emit=None,
            params=ListDirParams(path=path),
        )

        assert "路径不存在" in result

    @pytest.mark.asyncio
    async def test_not_a_directory_when_path_is_file(self, tmp_path):
        """路径是文件时返回 '不是目录'。"""
        file = tmp_path / "plain.txt"
        file.write_text("hello")

        plugin = ListDirPlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(file)),
            emit=None,
            params=ListDirParams(path=str(file)),
        )

        assert "不是目录" in result

    # ── 递归深度 ──

    @pytest.mark.asyncio
    async def test_depth_1_does_not_show_nested_files(self, tmp_path):
        """depth=1 时仅列出顶层条目，不展开子目录内容。"""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        nested = subdir / "deep.txt"
        nested.write_text("hidden")

        plugin = ListDirPlugin()
        result = await plugin.execute(
            ctx=self._make_ctx(str(tmp_path)),
            emit=None,
            params=ListDirParams(path=str(tmp_path), depth=1),
        )

        # depth=1 时不应出现深层文件
        assert "deep.txt" not in result
        # 但子目录本身应该出现
        assert "sub/" in result
