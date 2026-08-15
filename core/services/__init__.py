"""core/services/ — 服务运行时能力声明与广播。

capabilities 构建依赖 Agent 公开接口 + frontend_contract，广播经 Module.output
WebSocketChannel；不 import core.input。
"""

from core.services.capabilities import (
    build_capabilities,
    build_server_info,
    broadcast_capabilities,
)

__all__ = [
    "build_capabilities",
    "build_server_info",
    "broadcast_capabilities",
]
