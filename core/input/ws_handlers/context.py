"""WS 请求上下文 —— 连接级状态的单一载体（ARCH-INC-P2-01 / 评估批次 D）。

## 为什么需要它

拆分前，22 个 dispatcher 共享一个 10 参数签名：

    handle_json_message(data, agent, audio_buf, ws, voice_mgr,
                        sent_message_ids, limiter, rate_key)

评估 §4 P2-01 记录的正是这个增长模式：「新增语音、工具、服务消息时**冲突面和参数面
继续增大**」。每加一类消息，要么再塞一个参数，要么把一个不相关的依赖透传下去。

本对象把**连接级**状态收拢为单一载体，handler 签名因此收敛为：

    async def handler(ctx, data, msg, channel)

## 字段归属（为什么是这些）

全部为**连接生命周期内不变或由连接持有**的状态：

| 字段 | 说明 |
|---|---|
| `agent` | 组合根，所有 handler 都要 |
| `ws` | 发送目标；可为 None（音频流回调直调路径） |
| `audio_buf` | 本连接的 VAD 分段缓冲 |
| `voice_mgr` | 本连接的三模式语音状态机 |
| `sent_message_ids` | 本连接已接受 messageId 集合（v2 幂等去重） |
| `limiter` / `rate_key` | 限流器与其 key（连接建立时按认证主体解析） |

**逐消息**的状态（`data` / `msg` / `channel`）**不放进本对象**——它们是每次派发的入参，
放进上下文会让「随消息变化」与「随连接不变」混淆，正是重构要消除的问题。

## 行为不变式

本对象只是参数打包，**不改变任何行为**：字段原样透传，handler 内部逻辑逐字保留
（拆分前 `agent` → 拆分后 `ctx.agent`，仅此而已）。因此 wire 逐字节不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

    from core.agent import Agent
    from core.input.audio_buffer import AudioBuffer
    from core.input.voice_sessions import VoiceSessionManager
    from core.network.rate_limiter import RateLimiter


@dataclass(frozen=True)
class WsRequestContext:
    """单条 WS 连接的处理上下文（连接级状态打包，逐消息状态不入内）。"""

    agent: "Agent"
    """Agent 组合根。"""

    ws: "web.WebSocketResponse | None" = None
    """发送目标；None 表示无连接（音频流回调直调路径，静默降级）。"""

    audio_buf: "AudioBuffer | None" = None
    """本连接的音频分段缓冲（VAD）。"""

    voice_mgr: "VoiceSessionManager | None" = None
    """本连接的三模式语音状态机。"""

    sent_message_ids: set[str] | None = None
    """本连接已接受 messageId 集合（v2 去重；None 表示不追踪）。"""

    limiter: "RateLimiter | None" = None
    """限流器（create_app 挂载时传入；None 不检查）。"""

    rate_key: str | None = None
    """本连接限流 key（认证主体/IP）；None 不检查。"""
