"""core/network/ — 传输无关的网络运行时基础设施。

CORE-MOD-P2-01：限流器自 core/input 迁入——不依赖 WebSocket/REST 路由，
middleware 与 ws_handler 均可安全消费。
"""

from core.network.rate_limiter import RateLimiter, rate_key_for_request

__all__ = ["RateLimiter", "rate_key_for_request"]
