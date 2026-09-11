"""WS handler 注册表 —— **单一装配点**（ARCH-INC-P2-01 / 评估批次 D）。

按领域拆分的 handler 在此汇总为唯一注册表；`core/input/ws_handler.py` 只从本模块
取表并派发，不直接认识任何领域模块。

## 为什么集中在此

评估 §5 批次 D 第 4 条要求「统一装配点和旧 import re-export」——注册表若散在各领域
模块，新增消息类型就得改 ws_handler，拆分等于白做。此处是**唯一的**「消息类型 →
handler」映射，新增一类消息 = 在本文件加一行 + 在对应领域模块写 handler。

## 与拆分前的一致性

键集合与拆分前**逐项相同**（22 个）；映射目标为等价函数（仅签名收口为
`(ctx, data, msg, channel)`）。因此路由行为逐字节不变。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from core.input.ws_handlers.chat import (
    _dispatch_interrupt,
    _dispatch_text,
    _dispatch_wake_toggle,
)
from core.input.ws_handlers.session import (
    _dispatch_activate_session,
    _dispatch_set_service,
    _dispatch_set_tts_voice,
    _dispatch_switch_model,
)
from core.input.ws_handlers.tool import (
    _dispatch_tool_approval,
    _dispatch_tool_status,
)
from core.input.ws_handlers.voice import (
    _dispatch_audio_cancel,
    _dispatch_dictation_cancel,
    _dispatch_dictation_start,
    _dispatch_dictation_stop,
    _dispatch_voice_commit,
    _dispatch_voice_discard,
    _dispatch_voice_record_start,
    _dispatch_voice_record_stop,
    _dispatch_voice_session_cancel,
    _dispatch_voice_session_pause,
    _dispatch_voice_session_resume,
    _dispatch_voice_session_start,
    _dispatch_voice_session_stop,
)

if TYPE_CHECKING:
    from Module.output import WebSocketChannel
    from core.input.ws_handlers.context import WsRequestContext

WsHandler = Callable[
    ["WsRequestContext", dict, Any, "WebSocketChannel | None"], Awaitable[None],
]

_WS_HANDLERS: dict[str, WsHandler] = {
    # chat
    "wake_toggle": _dispatch_wake_toggle,
    "text": _dispatch_text,
    "interrupt": _dispatch_interrupt,
    # session
    "switch_model": _dispatch_switch_model,
    "set_tts_voice": _dispatch_set_tts_voice,
    "set_service": _dispatch_set_service,
    "activate_session": _dispatch_activate_session,
    # tool
    "tool_approval": _dispatch_tool_approval,
    "tool.status": _dispatch_tool_status,
    # voice
    "voice.record.start": _dispatch_voice_record_start,
    "voice.record.stop": _dispatch_voice_record_stop,
    "voice.commit": _dispatch_voice_commit,
    "voice.discard": _dispatch_voice_discard,
    "voice_session.start": _dispatch_voice_session_start,
    "voice_session.pause": _dispatch_voice_session_pause,
    "voice_session.resume": _dispatch_voice_session_resume,
    "voice_session.stop": _dispatch_voice_session_stop,
    "voice_session.cancel": _dispatch_voice_session_cancel,
    "dictation.start": _dispatch_dictation_start,
    "dictation.stop": _dispatch_dictation_stop,
    "dictation.cancel": _dispatch_dictation_cancel,
    "audio.cancel": _dispatch_audio_cancel,
}
"""消息类型 → handler（22 项，与拆分前逐一对应）。"""

__all__ = ["WsHandler", "_WS_HANDLERS"]
