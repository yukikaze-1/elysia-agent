"""网络工具插件 — 搜索和网页抓取。"""

from .plugin import WebSearchPlugin, WebFetchPlugin
from .mock_plugin import MockWebSearchPlugin

__all__ = ["WebSearchPlugin", "WebFetchPlugin", "MockWebSearchPlugin"]
