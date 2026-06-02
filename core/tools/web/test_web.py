"""WebSearchPlugin, WebFetchPlugin, _TextExtractor 单元测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from Module.tool.protocol import ToolContext


# ============================================================================
# _TextExtractor / _extract_text
# ============================================================================

class TestTextExtractor:
    """测试 _TextExtractor 和 _extract_text 文本提取功能。"""

    def test_extracts_visible_text_from_html(self):
        """从 HTML 中提取可见文本（段落、正文）。"""
        from core.tools.web.plugin import _extract_text

        html = (
            "<html><head><title>Test</title></head>"
            "<body><h1>标题</h1><p>第一段内容。</p><p>第二段内容。</p></body></html>"
        )
        result = _extract_text(html)
        assert "标题" in result
        assert "第一段内容" in result
        assert "第二段内容" in result

    def test_skips_script_tag_content(self):
        """跳过 <script> 标签内的内容。"""
        from core.tools.web.plugin import _extract_text

        html = (
            "<html><body>"
            "<p>可见文本</p>"
            "<script>console.log('应该被跳过'); var x = 1;</script>"
            "<p>另一段</p>"
            "</body></html>"
        )
        result = _extract_text(html)
        assert "可见文本" in result
        assert "另一段" in result
        assert "console.log" not in result
        assert "var x" not in result
        assert "被跳过" not in result

    def test_skips_style_tag_content(self):
        """跳过 <style> 标签内的内容。"""
        from core.tools.web.plugin import _extract_text

        html = (
            "<html><body>"
            "<p>可见文本</p>"
            "<style>body { color: red; } .hidden { display: none; }</style>"
            "<p>另一段</p>"
            "</body></html>"
        )
        result = _extract_text(html)
        assert "可见文本" in result
        assert "另一段" in result
        assert "color: red" not in result
        assert "display: none" not in result

    def test_block_tags_produce_line_breaks(self):
        """br/p/div 等块级标签在输出中产生换行。"""
        from core.tools.web.plugin import _extract_text

        html = "<p>第一行</p><p>第二行</p><div>第三行</div>"
        result = _extract_text(html)
        assert "\n" in result

    def test_empty_html_returns_empty_string(self):
        """空 HTML 返回 ''。"""
        from core.tools.web.plugin import _extract_text

        result = _extract_text("")
        assert result == ""

    def test_malformed_html_does_not_crash(self):
        """畸形 HTML 不抛出异常。"""
        from core.tools.web.plugin import _extract_text

        html = "<html><body><p>未闭合段落<span>嵌套<span>混乱</body>"
        result = _extract_text(html)
        assert isinstance(result, str)
        assert "未闭合段落" in result

    def test_noscript_tag_skipped(self):
        """跳过 <noscript> 标签内容。"""
        from core.tools.web.plugin import _extract_text

        html = (
            "<html><body>"
            "<p>可见</p>"
            "<noscript>您的浏览器不支持 JavaScript</noscript>"
            "<p>继续</p>"
            "</body></html>"
        )
        result = _extract_text(html)
        assert "可见" in result
        assert "继续" in result
        assert "JavaScript" not in result

    def test_head_tag_metadata_skipped(self):
        """<head> 内的元数据被跳过。"""
        from core.tools.web.plugin import _extract_text

        html = (
            "<html><head><title>页面标题</title><meta charset='utf-8'></head>"
            "<body><p>正文内容</p></body></html>"
        )
        result = _extract_text(html)
        assert "正文内容" in result
        assert "页面标题" not in result


# ============================================================================
# WebSearchPlugin
# ============================================================================

class TestWebSearchPlugin:
    """测试 WebSearchPlugin — 搜索后端路由。"""

    def test_default_backend_uses_ddg(self):
        """默认后端（无 backend 配置）使用 DDG。"""
        import asyncio
        from core.tools.web.plugin import WebSearchPlugin, WebSearchParams

        plugin = WebSearchPlugin()
        ctx = ToolContext(session_id="test")

        with patch(
            "core.tools.web.plugin._ddg_search",
            AsyncMock(return_value="DDG 结果"),
        ) as mock_ddg:
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebSearchParams(query="test"))
            )
            mock_ddg.assert_called_once_with("test", 5)
            assert result == "DDG 结果"

    def test_brave_without_api_key_falls_back_to_ddg(self):
        """brave 后端但无 api_key → 回退到 DDG。"""
        import asyncio
        from core.tools.web.plugin import WebSearchPlugin, WebSearchParams

        plugin = WebSearchPlugin()
        ctx = ToolContext(services={"web_search": "brave"})

        with patch(
            "core.tools.web.plugin._ddg_search",
            AsyncMock(return_value="DDG 回退结果"),
        ) as mock_ddg, patch(
            "core.tools.web.plugin._brave_search",
            AsyncMock(),
        ) as mock_brave:
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebSearchParams(query="test"))
            )
            mock_ddg.assert_called_once()
            mock_brave.assert_not_called()
            assert result == "DDG 回退结果"

    def test_brave_with_api_key_uses_brave(self):
        """brave 后端且有 api_key → 使用 Brave。"""
        import asyncio
        from core.tools.web.plugin import WebSearchPlugin, WebSearchParams

        plugin = WebSearchPlugin()
        ctx = ToolContext(services={
            "web_search": "brave",
            "brave_api_key": "sk-test-key-123",
        })

        with patch(
            "core.tools.web.plugin._brave_search",
            AsyncMock(return_value="Brave 结果"),
        ) as mock_brave, patch(
            "core.tools.web.plugin._ddg_search",
            AsyncMock(),
        ) as mock_ddg:
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebSearchParams(query="test"))
            )
            mock_brave.assert_called_once_with("test", 5, "sk-test-key-123")
            mock_ddg.assert_not_called()
            assert result == "Brave 结果"

    def test_ddg_no_results_returns_not_found_message(self):
        """DDG 无结果时返回包含"未找到"的消息。"""
        import asyncio
        from core.tools.web.plugin import WebSearchPlugin, WebSearchParams

        plugin = WebSearchPlugin()
        ctx = ToolContext(session_id="test")

        with patch(
            "core.tools.web.plugin._ddg_search",
            AsyncMock(return_value="未找到与 'xyz' 相关的结果。"),
        ) as mock_ddg:
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebSearchParams(query="xyz"))
            )
            assert "未找到" in result

    def test_ddg_with_custom_limit(self):
        """自定义 limit 参数传递给 DDG。"""
        import asyncio
        from core.tools.web.plugin import WebSearchPlugin, WebSearchParams

        plugin = WebSearchPlugin()
        ctx = ToolContext(session_id="test")

        with patch(
            "core.tools.web.plugin._ddg_search",
            AsyncMock(return_value="DDG 结果"),
        ) as mock_ddg:
            asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebSearchParams(query="test", limit=8))
            )
            mock_ddg.assert_called_once_with("test", 8)


# ============================================================================
# WebFetchPlugin
# ============================================================================

class TestWebFetchPlugin:
    """测试 WebFetchPlugin — 网页抓取。"""

    def _make_client_mock(self, text="", content_type="text/html; charset=utf-8",
                          status_code=200, raise_for_status_error=None):
        """创建一个 httpx.AsyncClient mock，模拟 HTTP 响应。"""
        mock_response = MagicMock()
        mock_response.text = text
        mock_response.headers = {"content-type": content_type}
        mock_response.status_code = status_code

        if raise_for_status_error is not None:
            mock_response.raise_for_status = MagicMock(side_effect=raise_for_status_error)
        else:
            mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        return mock_client

    def test_rejects_non_http_url(self):
        """非 http/https URL 返回"无效的 URL"。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")
        result = asyncio.run(
            plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="ftp://example.com/file"))
        )
        assert "无效的 URL" in result

    def test_rejects_plain_string_not_url(self):
        """非 URL 字符串返回"无效的 URL"。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")
        result = asyncio.run(
            plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="not-a-url"))
        )
        assert "无效的 URL" in result

    def test_successful_html_fetch_returns_extracted_text(self):
        """成功抓取 HTML 页面返回提取后的纯文本。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")

        html = "<html><body><h1>标题</h1><p>正文内容。</p></body></html>"
        mock_client = self._make_client_mock(text=html)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="https://example.com"))
            )
            assert "标题" in result
            assert "正文内容" in result

    def test_http_404_returns_http_error(self):
        """HTTP 404 返回"HTTP 错误"。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams
        import httpx

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=mock_response
            )
        )
        mock_response.text = "Not Found"

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="https://example.com/404"))
            )
            assert "HTTP 错误" in result

    def test_non_html_content_type_returns_unsupported_message(self):
        """非 HTML 内容类型返回"不支持的内容类型"。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")

        mock_client = self._make_client_mock(
            text='{"key": "value"}',
            content_type="application/json",
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="https://example.com/api/data"))
            )
            assert "不支持的内容类型" in result

    def test_empty_page_returns_no_content_message(self):
        """空页面或无文本内容返回提示信息。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")

        html = "<html><head></head><body></body></html>"
        mock_client = self._make_client_mock(text=html)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="https://example.com/empty"))
            )
            assert "页面无文本内容" in result

    def test_text_plain_content_type_accepted(self):
        """text/plain 内容类型也被接受。"""
        import asyncio
        from core.tools.web.plugin import WebFetchPlugin, WebFetchParams

        plugin = WebFetchPlugin()
        ctx = ToolContext(session_id="test")

        mock_client = self._make_client_mock(
            text="Hello World\nLine 2\nLine 3",
            content_type="text/plain",
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                plugin.execute(ctx=ctx, emit=None, params=WebFetchParams(url="https://example.com/data.txt"))
            )
            assert "Hello World" in result
            assert "Line 2" in result
