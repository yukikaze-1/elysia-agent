"""input 层统一错误信封响应构造器。

_protocol_error_response 原寄生于 core/input/server.py，被 api_routes/debug_routes 经
函数级懒导入绕环访问（2026-08-14 批次 A 下沉）。errors.py 仅依赖 frontend_contract +
aiohttp + uuid，不依赖 server.py——调用方改顶层 eager import 即可消除绕环。
"""

import logging
import uuid

from aiohttp import web

from frontend_contract.rest import ProtocolError

logger = logging.getLogger(__name__)


def _protocol_error_response(code: str, message: str, status: int = 401,
                             retryable: bool = False, *,
                             details: dict[str, object] | None = None,
                             retry_after_ms: int | None = None) -> web.Response:
    """构造统一错误信封响应（含公开安全详情与可选重试等待）。

    Args:
        code: 稳定错误码。
        message: 公开说明。
        status: HTTP 状态码。
        retryable: 是否可重试（错误码目录定义；CONNECTION_LIMITED=true，AUTH/REQUEST 等 false）。
        details: 仅允许公开安全字段（不得带正文、密钥或本地路径）。
        retry_after_ms: 服务端建议的下次尝试等待毫秒数。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
        details=details or {}, retryAfterMs=retry_after_ms,
    ).wire_dict()
    return web.json_response(env, status=status)


async def _json_object(request: web.Request) -> dict:
    """读取 JSON body 并要求它是 **object**（S2-F4）。

    为什么需要：`await request.json()` 对 `[1,2]` / `"abc"` / `null` 都能成功返回
    一个**非 dict**，随后 `body.get(...)` 抛 `AttributeError` → 未捕获 → 纯文本 500
    （脱离 54 码错误体系）。实测 5 处中招：`POST /api/providers/test [1,2]`、
    `PATCH /api/sessions/default "abc"`、`PUT /debug/api/ltm/{id} [1]`。

    语义与 `character_routes._json_body` 一致（那里是唯一做对了的地方）。

    Raises:
        _JsonBodyError: 非 JSON 或非 object——由调用方映射成 400 信封。
    """
    try:
        body = await request.json()
    except web.HTTPException:
        # F13：chunked 超限抛 HTTPRequestEntityTooLarge(413)，必须**原样放行**——
        # 那是 aiohttp 的显式响应，有它自己的统一信封；包成 400 会把"请求太大"
        # 说成"格式不对"，客户端据此做出完全相反的处置。
        raise
    except Exception as exc:
        raise _JsonBodyError("请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise _JsonBodyError("请求体必须是 JSON object")
    return body


class _JsonBodyError(ValueError):
    """JSON body 形态错误（调用方映射 400 CONTRACT_VALIDATION）。"""


@web.middleware
async def _error_envelope_middleware(request: web.Request, handler: object) -> web.StreamResponse:
    """最外层兜底：未处理异常 → 统一错误信封（S2-F4）。

    此前没有任何"异常 → 信封"的兜底（只有 payload/nosniff/rate/auth 四个中间件），
    于是任何漏网的 `AttributeError`/`KeyError` 都变成 aiohttp 的**纯文本 500**，
    客户端无法区分"服务器 bug"与"我的请求写错了"。

    `web.HTTPException`（含 400/403/404 等显式响应）与取消异常**原样放行**——
    它们本来就是有意的控制流。
    """
    import asyncio

    try:
        return await handler(request)          # type: ignore[operator]
    except (web.HTTPException, asyncio.CancelledError):
        raise
    except _JsonBodyError as exc:
        # 形态错误是**客户端**的问题 → 400，不是 500。此前它是未捕获的
        # `AttributeError`，客户端只能拿到纯文本 500，无法区分"我写错了"与
        # "服务器炸了"。
        return _protocol_error_response("CONTRACT_VALIDATION", str(exc), 400)
    except Exception:
        logger.error("未处理异常 → 统一错误信封（%s %s）",
                     request.method, request.path, exc_info=True)
        return _protocol_error_response("INTERNAL", "服务器内部错误", 500,
                                        retryable=False)
