"""网络工具插件 — 网页搜索和抓取。"""

import asyncio
import os
import re
from html.parser import HTMLParser
from typing import Any

import httpx
from pydantic import BaseModel, Field

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


# ============================================================================
# 文本提取器
# ============================================================================

class _TextExtractor(HTMLParser):
    """提取 HTML 中的可见文本，跳过 script/style/不可见元素。"""

    def __init__(self) -> None:
        """初始化文本提取器。"""
        super().__init__()
        self.text: list[str] = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "head", "meta", "link"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """处理开始标签，跳过不可见元素。

        Args:
            tag: HTML 标签名。
            attrs: 标签属性列表。
        """
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        """处理结束标签，恢复可见状态并在块级元素后插入换行。

        Args:
            tag: HTML 标签名。
        """
        if tag in self._skip_tags:
            self._skip = False
        if tag in ("br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        """处理文本数据，提取可见文本。

        Args:
            data: HTML 文本数据。
        """
        if not self._skip:
            text = data.strip()
            if text:
                self.text.append(text)


def _extract_text(html: str) -> str:
    """从 HTML 中提取可见文本。

    Args:
        html: HTML 字符串。

    Returns:
        提取并清理后的纯文本。
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    raw = " ".join(parser.text)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return raw.strip()


def _get_proxy() -> str | None:
    """从环境变量读取代理 URL。

    优先级: DDGS_PROXY > HTTPS_PROXY > https_proxy > HTTP_PROXY > http_proxy > all_proxy
    返回 None 表示不使用代理。
    """
    return (
        os.environ.get("DDGS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
        or None
    )


# ============================================================================
# web_search — Tavily (默认) + Brave API (可选)
# ============================================================================

class WebSearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=10, description="结果数量，最大10")


class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网，返回相关网页的标题、URL 和摘要"
    param_model = WebSearchParams
    capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_concurrency=3,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: Any = None, params: WebSearchParams,
    ) -> str:
        """搜索互联网，返回相关网页的标题、URL 和摘要。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 搜索参数，包含查询关键词和结果数量。

        Returns:
            搜索结果字符串。
        """
        backend = ctx.get("web_search", "")
        if backend == "brave":
            brave_key = ctx.get("brave_api_key")
            if brave_key:
                return await _brave_search(params.query, params.limit, brave_key)

        return await _tavily_search(params.query, params.limit)


async def _tavily_search(query: str, limit: int) -> str:
    """Tavily Search API — 默认搜索后端。"""
    from tavily import AsyncTavilyClient

    api_key = os.environ.get("TAVILY_API_KEY", "")
    proxy_url = _get_proxy()

    client_kwargs: dict = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if proxy_url:
        client_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

    async with AsyncTavilyClient(**client_kwargs) as client:
        response = await client.search(
            query=query,
            search_depth="basic",
            max_results=limit,
            include_answer=False,
        )

    results = response.get("results") or []
    if not results:
        return f"未找到与 '{query}' 相关的结果。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")
    return "\n\n".join(lines)


async def _brave_search(query: str, limit: int, api_key: str) -> str:
    """Brave Search API — 备选搜索后端。

    Args:
        query: 搜索关键词。
        limit: 结果数量上限。
        api_key: Brave API 密钥。

    Returns:
        搜索结果字符串。
    """
    async with httpx.AsyncClient(timeout=15.0, proxy=_get_proxy()) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"未找到与 '{query}' 相关的结果。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        desc = r.get("description", "")
        lines.append(f"{i}. {title}\n   {url}\n   {desc}")
    return "\n\n".join(lines)


# ============================================================================
# web_fetch — 抓取网页纯文本
# ============================================================================

class WebFetchParams(BaseModel):
    url: str = Field(..., description="要抓取的网页 URL")


class WebFetchPlugin(ToolPlugin):
    name = "web_fetch"
    description = "抓取并解析指定网页的文本内容"
    param_model = WebFetchParams
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(
        network_allowed=True,
        block_private_ips=True,
        max_redirects=5,
        max_response_bytes=5 * 1024 * 1024,
        request_timeout=30.0,
    )

    async def execute(
        self, *, ctx: ToolContext, emit: Any = None, params: WebFetchParams,
    ) -> str:
        """抓取并解析指定网页的文本内容。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 抓取参数，包含目标 URL。

        Returns:
            网页纯文本内容，或错误信息。
        """
        url = params.url
        if not url.startswith(("http://", "https://")):
            return f"无效的 URL: {url}"

        try:
            async with httpx.AsyncClient(
                timeout=self.safety.request_timeout,
                follow_redirects=True,
                max_redirects=self.safety.max_redirects,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Elysia-Agent/1.0)"},
                proxy=_get_proxy(),
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return f"不支持的内容类型: {content_type}"

                text = _extract_text(resp.text)
                if not text:
                    return "（页面无文本内容）"
                return text
        except httpx.HTTPStatusError as e:
            return f"HTTP 错误: {e.response.status_code}"
        except Exception as e:
            return f"抓取失败: {e}"
