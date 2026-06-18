"""确定性 web_search mock —— 集成测试用，返回固定结果，不发起网络请求。"""

from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile
from .plugin import WebSearchParams


class MockWebSearchPlugin(ToolPlugin):
    """Mock web_search —— 返回预设的搜索结果，不发起网络请求。

    与真实 WebSearchPlugin 保持相同的 name、param_model、capabilities、safety，
    通过 ToolManager.load_all([TimePlugin, MockWebSearchPlugin], ctx) 无缝替换。
    真实 WebSearchPlugin 不被加载。
    """

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
        self, *, ctx: ToolContext, emit=None, params: WebSearchParams,
    ) -> str:
        """返回基于 query 关键词的预设搜索结果，不发起网络请求。"""
        query = params.query
        limit = min(params.limit, 10)

        # 根据 query 关键词返回不同领域的固定结果
        results = _pick_results(query)
        return _format_results(results[:limit])


def _pick_results(query: str) -> list[dict[str, str]]:
    """根据 query 关键词选择预设结果集。"""
    q = query.lower()

    if any(kw in q for kw in ("天气", "weather", "气温")):
        return _WEATHER_RESULTS
    if any(kw in q for kw in ("新闻", "news", "头条")):
        return _NEWS_RESULTS
    if any(kw in q for kw in ("代码", "code", "编程", "python", "rust", "golang")):
        return _CODE_RESULTS
    if any(kw in q for kw in ("电影", "movie", "音乐", "music")):
        return _ENTERTAINMENT_RESULTS
    return _GENERAL_RESULTS


def _format_results(results: list[dict[str, str]]) -> str:
    """格式化为与 _tavily_search() 一致的输出格式。"""
    if not results:
        return "未找到相关结果。"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")
    return "\n\n".join(lines)


# ── 预设结果集 ──────────────────────────────────────────────────────────────────

_WEATHER_RESULTS = [
    {
        "title": "北京天气预报一周 — 中国天气网",
        "url": "https://weather.example.com/beijing",
        "content": "北京今日晴转多云，气温 22°C ~ 32°C，东南风 3-4 级，空气质量良。未来三天以晴为主，气温稳定。",
    },
    {
        "title": "上海天气预报 — 中央气象台",
        "url": "https://weather.example.com/shanghai",
        "content": "上海今日阴有阵雨，气温 25°C ~ 30°C，东风 2-3 级，相对湿度 75%。",
    },
    {
        "title": "全国降水量预报图 — 国家气象局",
        "url": "https://weather.example.com/rainfall",
        "content": "未来 24 小时全国降水主要集中在华南和华东地区，华北晴好天气持续。",
    },
]

_NEWS_RESULTS = [
    {
        "title": "科技巨头发布新一代 AI 芯片 — 科技日报",
        "url": "https://news.example.com/ai-chip-2026",
        "content": "该芯片采用 3nm 制程，算力较上一代提升 3 倍，功耗降低 40%，预计下半年量产。",
    },
    {
        "title": "量子计算取得新突破 — 自然科学杂志",
        "url": "https://news.example.com/quantum-2026",
        "content": "研究团队成功实现 1000+ 量子比特的稳定纠缠，纠错率达到实用化门槛。",
    },
    {
        "title": "全球可再生能源装机创新高 — 能源周刊",
        "url": "https://news.example.com/renewable-energy",
        "content": "2026 年上半年全球光伏和风电新增装机突破 500GW，中国占比超 40%。",
    },
]

_CODE_RESULTS = [
    {
        "title": "Python 3.13 新特性详解 — Python 官方文档",
        "url": "https://docs.python.org/3.13/whatsnew/",
        "content": "Python 3.13 引入改进的 asyncio 性能、新的类型注解语法、更好的异常信息等特性。",
    },
    {
        "title": "Rust 异步编程最佳实践 — Rust 社区博客",
        "url": "https://blog.rust-lang.org/async-best-practices",
        "content": "使用 tokio 进行异步编程时应注意避免 blocking 操作、合理使用 select! 宏、正确管理取消安全。",
    },
    {
        "title": "如何设计可测试的分布式系统 — 架构博客",
        "url": "https://example.com/distributed-testing",
        "content": "关键原则：外部依赖可替换、状态可注入、超时可配置、日志结构化。使用 test double 替代真实服务。",
    },
]

_ENTERTAINMENT_RESULTS = [
    {
        "title": "热映电影票房排行榜 — 豆瓣电影",
        "url": "https://movie.example.com/boxoffice",
        "content": "本周票房前三：《星际探索 2》（3.2 亿）、《重返地球》（1.8 亿）、《盛夏光年》（1.2 亿）。",
    },
    {
        "title": "2026 年度音乐榜单发布 — QQ 音乐",
        "url": "https://music.example.com/annual-2026",
        "content": "年度最受欢迎歌手包括周杰伦、Taylor Swift、BTS，流媒体播放量超百亿次。",
    },
]

_GENERAL_RESULTS = [
    {
        "title": "维基百科 — 自由的百科全书",
        "url": "https://zh.wikipedia.org/wiki/Main_Page",
        "content": "维基百科是一个多语言的网络百科全书，由全球志愿者共同维护，现有超过 140 万篇中文条目。",
    },
    {
        "title": "如何高效获取信息 — 研究方法论",
        "url": "https://example.com/research-methods",
        "content": "信息获取应遵循交叉验证原则：使用多个独立来源互相印证，优先选择权威和一手来源。",
    },
    {
        "title": "搜索引擎使用技巧 — 数字素养指南",
        "url": "https://example.com/search-tips",
        "content": "使用精确关键词、引号匹配、site: 限定域名、filetype: 限定文件类型等技巧可显著提高搜索效率。",
    },
    {
        "title": "信息检索技术综述 — 学术论文",
        "url": "https://example.com/ir-survey",
        "content": "现代信息检索系统结合倒排索引、BM25 排序、语义向量检索等技术，提供高效准确的结果。",
    },
    {
        "title": "知识图谱在搜索中的应用 — 技术博客",
        "url": "https://example.com/knowledge-graph-search",
        "content": "知识图谱通过实体链接、关系推理提升搜索结果的相关性和完整性，Google、Bing 已广泛应用。",
    },
]
