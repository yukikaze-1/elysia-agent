"""确定性 web_search mock —— 集成测试用，返回固定结果，不发起网络请求。"""

from typing import Any

from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter
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
        self, *, ctx: ToolContext, emit: Any = None, params: WebSearchParams,
    ) -> str:
        """返回基于 query 关键词的预设搜索结果，不发起网络请求。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 搜索参数，包含查询关键词和结果数量。

        Returns:
            预设的搜索结果字符串。
        """
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
        "title": "相关领域研究综述（2024-2025）— 学术期刊",
        "url": "https://scholar.example.com/survey-2025",
        "content": "近年来该领域取得了显著进展，多项研究在方法论和应用层面实现突破。2025 年发布的几项关键研究进一步推动了技术成熟度的提升，主要贡献集中在性能优化、安全增强和可扩展性方面。学术界和产业界均对此保持高度关注。",
    },
    {
        "title": "最新技术发展报告 — 行业研究机构",
        "url": "https://research.example.com/tech-report-2025",
        "content": "本报告梳理了 2024-2025 年间该方向的核心进展，包括：新算法框架的提出、基准测试成绩的提升、以及落地场景的扩展。报告指出，当前的主要挑战已从理论可行性转向工程化部署和大规模应用。",
    },
    {
        "title": "前沿技术进展与趋势分析 — 科技媒体",
        "url": "https://techreview.example.com/trends-2025",
        "content": "多位行业专家在近期访谈中一致认为，该技术正处于从实验室走向产业化的关键阶段。跨学科融合（计算机科学、认知科学、伦理学）成为新的研究范式，预计未来 2-3 年内将出现更多可商用成果。",
    },
    {
        "title": "相关领域权威论文汇编 — 学术数据库",
        "url": "https://papers.example.com/collection/2025",
        "content": "收录 2024-2025 年发表的高引用论文，涵盖理论分析、算法改进、系统设计和应用案例四大类别。其中多篇论文提出了创新性的解决方案，已被主要国际会议接收。",
    },
    {
        "title": "综合研究报告：方法、挑战与展望 — 研究基金会",
        "url": "https://foundation.example.com/comprehensive-study",
        "content": "本研究汇总了全球 50+ 研究团队的成果，系统评估了当前技术水平、主要瓶颈和未来方向。报告建议优先关注数据质量、评估标准的统一性以及跨团队协作机制的建立。",
    },
]
