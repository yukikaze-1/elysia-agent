"""WriteFilePlugin 单元测试。"""

import pytest

from Module.tool.protocol import ToolContext
from Module.tool.safety import SafetyProfile

from core.tools.filesystem.plugin import WriteFilePlugin, WriteFileParams

pytestmark = pytest.mark.service


class TestWriteFilePlugin:
    """WriteFilePlugin — 在 sandbox 目录下安全写入文件。"""

    @staticmethod
    def _make_plugin(sandbox_dir: str) -> WriteFilePlugin:
        """创建注入了自定义 sandbox 目录的 WriteFilePlugin。"""
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=sandbox_dir,
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )
        return plugin

    @staticmethod
    def _make_ctx(safe_path: str, session_id: str = "test_session") -> ToolContext:
        """创建注入了 _safe_path 的 ToolContext。"""
        ctx = ToolContext(session_id=session_id)
        ctx.services["_safe_path"] = safe_path
        return ctx

    # ── 基本写入 ──

    @pytest.mark.asyncio
    async def test_writes_file_inside_sandbox(self, tmp_path):
        """在 sandbox 内的路径写入文件，文件内容正确且结果包含 '已写入'。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "hello.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="hello.txt", content="hello world"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "hello world"

    @pytest.mark.asyncio
    async def test_writes_file_with_unicode_content(self, tmp_path):
        """写入包含中文和特殊字符的内容。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "unicode.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        content = "你好世界\nこんにちは\n🌟 emoji test"
        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="unicode.txt", content=content),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == content

    # ── 路径越界 ──

    @pytest.mark.asyncio
    async def test_rejects_path_outside_sandbox(self, tmp_path):
        """写入 sandbox 外的路径返回 '拒绝写入'。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        outside_path = tmp_path / "escape.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(outside_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="escape.txt", content="bad"),
        )

        assert "拒绝写入" in result
        assert not outside_path.exists()

    @pytest.mark.asyncio
    async def test_rejects_path_that_traverses_up(self, tmp_path):
        """路径中包含 .. 导致的越界被拒绝。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        # _safe_path 指向 sandbox 外的路径（已由安全管线解析后注入）
        outside_path = tmp_path / "escaped.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(outside_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="../escaped.txt", content="bad"),
        )

        assert "拒绝写入" in result

    # ── 父目录创建 ──

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path):
        """写入嵌套路径时自动创建不存在的父目录。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "nested/deep/file.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="nested/deep/file.txt", content="deep content"),
        )

        assert "已写入" in result
        assert safe_path.exists()
        assert safe_path.read_text(encoding="utf-8") == "deep content"
        # 验证父目录存在
        assert safe_path.parent.is_dir()

    @pytest.mark.asyncio
    async def test_creates_multiple_nested_parent_dirs(self, tmp_path):
        """多层级嵌套路径，各级父目录均被创建。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "a/b/c/d/e/deep.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="a/b/c/d/e/deep.txt", content="nested"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "nested"

    # ── session_id 模板替换 ──

    @pytest.mark.asyncio
    async def test_session_id_replaced_in_write_dir_template(self, tmp_path):
        """write_dir 中的 {session_id} 被替换为实际的 session_id。"""
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        expected_write_dir = sandbox_base / "my_session_123"
        safe_path = expected_write_dir / "output.txt"
        ctx = self._make_ctx(str(safe_path), session_id="my_session_123")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="output.txt", content="session data"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "session data"

    @pytest.mark.asyncio
    async def test_session_id_with_slashes_is_sanitized(self, tmp_path):
        """session_id 中的 / 和 \\ 被替换为 _ 防止路径穿越。"""
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        # session_id 含斜杠 → 应被替换为下划线
        expected_write_dir = sandbox_base / "a_b_c"
        safe_path = expected_write_dir / "file.txt"
        ctx = self._make_ctx(str(safe_path), session_id="a/b\\c")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="file.txt", content="sanitized"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "sanitized"

    # ── 默认 session_id ──

    @pytest.mark.asyncio
    async def test_uses_default_session_id_when_empty(self, tmp_path):
        """session_id 为空时使用 'default' 作为 fallback。"""
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        expected_write_dir = sandbox_base / "default"
        safe_path = expected_write_dir / "file.txt"
        ctx = ToolContext(session_id="")
        ctx.services["_safe_path"] = str(safe_path)

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="file.txt", content="default session"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "default session"

    # ── 边界情况 ──

    @pytest.mark.asyncio
    async def test_writes_empty_content(self, tmp_path):
        """写入空内容 — 创建空文件。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "empty.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="empty.txt", content=""),
        )

        assert "已写入" in result
        assert safe_path.exists()
        assert safe_path.read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_result_includes_file_size(self, tmp_path):
        """返回结果中包含写入的字节数。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "sized.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        content = "hello world"  # 11 bytes
        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="sized.txt", content=content),
        )

        assert "已写入" in result
        assert "11 bytes" in result


class TestWriteFileHardening:
    """D1/P2-05：待写内容 UTF-8 字节上限 + sibling 边界。"""

    @staticmethod
    def _plugin(sandbox_dir: str, max_bytes: int) -> WriteFilePlugin:
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=sandbox_dir,
            max_file_bytes=max_bytes,
            requires_human_approval=True,
        )
        return plugin

    @pytest.mark.asyncio
    async def test_content_bytes_exceeding_limit_rejected(self, tmp_path):
        """新建文件当前大小 0 会绕过旧上限——按待写 UTF-8 字节拒绝。"""
        sandbox = tmp_path / "sandbox"
        plugin = self._plugin(str(sandbox), max_bytes=8)
        ctx = ToolContext(session_id="test_session")
        ctx.services["_safe_path"] = str(sandbox / "new.txt")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="new.txt", content="九个字节的内容"),
        )

        assert "过大" in result  # 内容 UTF-8 超 8 bytes → 拒绝且不落盘
        assert not (sandbox / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_sibling_prefix_write_rejected(self, tmp_path):
        """write_dir=/sandbox，请求 /sandbox_evil/secret → 拒绝（is_relative_to 修复）。"""
        sandbox = tmp_path / "sandbox"
        evil = tmp_path / "sandbox_evil"
        evil.mkdir(parents=True)
        plugin = self._plugin(str(sandbox), max_bytes=1024)
        ctx = ToolContext(session_id="test_session")
        ctx.services["_safe_path"] = str(evil / "secret.txt")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="secret.txt", content="x"),
        )

        assert "越过了 sandbox" in result
        assert not (evil / "secret.txt").exists()
