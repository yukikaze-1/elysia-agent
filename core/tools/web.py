"""网络工具 — 搜索互联网、抓取网页内容。"""

import re
from html.parser import HTMLParser

import httpx

from Module.tool import ToolContext, tool


# ---------------------------------------------------------------------------
# web_search — DuckDuckGo (默认) + Brave API (可选)
# ---------------------------------------------------------------------------

@tool(description="搜索互联网，返回相关网页的标题、URL 和摘要", requires=["web_search"])
async def web_search(query: str, limit: int = 5, ctx: ToolContext = None) -> str:
    """搜索互联网。

    :param query: 搜索关键词
    :param limit: 返回结果数量（最多 10）
    """
    limit = min(limit, 10)

    # Brave Search API（可选）
    backend = (ctx.services.get("web_search") if ctx else None) or ""
    if backend == "brave":
        brave_key = ctx.services.get("brave_api_key") if ctx else None
        if brave_key:
            return await _brave_search(query, limit, brave_key)

    # DuckDuckGo（默认）
    return await _ddg_search(query, limit)


async def _ddg_search(query: str, limit: int) -> str:
    try:
        from ddgs import DDGS  # 新包名
    except ImportError:
        from duckduckgo_search import DDGS  # 旧包名 fallback
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
    except Exception as e:
        return f"搜索失败: {e}"

    if not results:
        return f"未找到与 '{query}' 相关的结果。"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        href = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"{i}. {title}\n   {href}\n   {body}")
    return "\n\n".join(lines)


async def _brave_search(query: str, limit: int, api_key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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
    except Exception as e:
        return f"Brave 搜索失败: {e}"

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


# ---------------------------------------------------------------------------
# web_fetch — 抓取网页纯文本
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """提取 HTML 中的可见文本，跳过 script/style/不可见元素。"""

    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self._skip = False
        self._skip_tags = {"script", "style", "noscript", "head", "meta", "link"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip = False
        if tag in ("br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.text.append(text)


def _extract_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    raw = " ".join(parser.text)
    # 压缩连续空白
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return raw.strip()


@tool(description="抓取并解析指定网页的文本内容", requires=["network"], timeout=20.0)
async def web_fetch(url: str, ctx: ToolContext = None) -> str:
    """抓取网页内容，提取纯文本。

    :param url: 要抓取的网页 URL
    """
    if not url.startswith(("http://", "https://")):
        return f"无效的 URL: {url}"

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Elysia-Agent/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                return f"不支持的内容类型: {content_type}"

            text = _extract_text(resp.text)
            if len(text) > 8000:
                text = text[:8000] + "\n\n…[内容已截断]"
            return text if text else "（页面无文本内容）"
    except httpx.HTTPStatusError as e:
        return f"HTTP 错误: {e.response.status_code}"
    except Exception as e:
        return f"抓取失败: {e}"
