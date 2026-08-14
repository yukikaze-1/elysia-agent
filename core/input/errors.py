"""input 层统一错误信封响应构造器。

_protocol_error_response 原寄生于 core/input/server.py，被 api_routes/debug_routes 经
函数级懒导入绕环访问（2026-08-14 批次 A 下沉）。errors.py 仅依赖 frontend_contract +
aiohttp + uuid，不依赖 server.py——调用方改顶层 eager import 即可消除绕环。
"""

import uuid

from aiohttp import web

from frontend_contract.rest import ProtocolError


def _protocol_error_response(code: str, message: str, status: int = 401,
                             retryable: bool = False) -> web.Response:
    """构造统一错误信封响应（code/message/retryable/requestId/error/details）。

    Args:
        code: 稳定错误码。
        message: 公开说明。
        status: HTTP 状态码。
        retryable: 是否可重试（错误码目录定义；CONNECTION_LIMITED=true，AUTH/REQUEST 等 false）。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)
