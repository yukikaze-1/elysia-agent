#!/usr/bin/env python3
"""真实 WS 事件轨迹抓取脚本（批次 0，BE-001）。

用途：产出脱敏的真实 WebSocket JSONL 轨迹，供 Electron 前端联调参考。
三个模式：
  python scripts/capture_ws_trace.py chat     # 完整聊天：建连→消息→inner_voice→多句→token_usage→心情→idle
  python scripts/capture_ws_trace.py cancel   # 生成中打断
  python scripts/capture_ws_trace.py tool     # 工具审批往返（save_memory HITL）

原理：构造真实 Agent + FakeLLM（带 <inner_voice>/<mood> 标签 + 多句标点 + 可选 usage / tool_calls），
起 aiohttp app，用 WS 客户端逐帧记录到 frontend-handoff-v2/traces/。

约束：
- TTS 必须禁用（默认 AppConfig tts.enabled=False），否则句子边界事件不发且真调 TTS。
- 工具轨迹需要带 stream_think 的 fake（参照 core/tests/test_phase3.py 模式），
  FakeLLMBackend 本身无 stream_think，经 LLMAdapter 走非流式路径 tool_calls 恒为 None。
- 去敏：环境变量值/绝对路径/真实内容替换为占位符。

运行（Elysia conda）：
  conda activate Elysia && python scripts/capture_ws_trace.py [chat|cancel|tool]
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import os

# 保证项目根在 sys.path（与 app.py 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 禁用 CUDA（与 app.py 一致，避免 torch 噪音）
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

from core.tests.fake_llm import FakeLLMBackend  # noqa: E402

TRACES_DIR = Path(__file__).resolve().parent.parent / "frontend-handoff-v2" / "traces"

# 去敏正则：绝对路径 /home/... 与 /tmp/...
_PATH_RE = re.compile(r"/(?:home|tmp)/[^\s\",}]+")
# 环境变量值脱敏：key 名保留，值替换
_ENV_RE = re.compile(r"([A-Z][A-Z0-9_]{2,})")

CHAT_RESPONSE = (
    "<inner_voice>我觉得这个话题很有意思，想认真回应一下。</inner_voice>"
    "<mood>开心|0.8</mood>"
    "你好！这是第一句话，回答你的问题。"
    "第二句话进一步补充说明。"
    "第三句话作为收尾，希望对你有所帮助。"
)

CANCEL_RESPONSE = (
    "<inner_voice>准备回答。</inner_voice>"
    "这是第一句会被打断的话。\n"
    "这是第二句，正常生成的话会继续。"
    "这是第三句，但生成到一半就会被 interrupt 打断。"
    "这是第四句，正常情况下不会出现。"
    "这是第五句，正常情况下也不会出现。"
)


class FakeLLMWithUsage(FakeLLMBackend):
    """FakeLLMBackend 扩展：实现 stream_think 走真实流式路径，收尾带 usage chunk。

    关键：LLMAdapter.stream_think 对无 stream_think 的 backend 走 `chat()` 逐字符分支，
    永不发出 usage chunk（loop.py:502 `"usage" in chunk` 只在 stream_think 路径触发）。
    因此必须实现 stream_think：逐 token yield dict + 收尾 usage。

    Args:
        token_delay: 每个 token 间 sleep 秒数（>0 拉长生成，供 cancel 模式打断）。
        space_after_first_dot: 第一句 `。` 后插入空格 token（强制 tokenizer flush `_pending`
            产出 delta——中文无空格文本默认不 flush，cancel 轨迹需要"先有内容再被打断"）。
    """

    def __init__(self, response: str = "test response", token_delay: float = 0.0,
                 space_after_first_dot: bool = False) -> None:
        super().__init__(response=response)
        self._token_delay = token_delay
        self._space_after_first_dot = space_after_first_dot
        self._space_inserted = False

    async def stream_think(self, messages, **kwargs):
        gt_count = 0  # 统计 `>`——第 2 个 `>` 即 `</inner_voice>` 闭合
        for tok in self._tokenize(self.response):
            if self._token_delay:
                await asyncio.sleep(self._token_delay)
            yield {"content": tok, "tool_calls": None}
            if tok == ">":
                gt_count += 1
            # 在 inner_voice 闭合后的第一个 `。` 插空格，强制 tokenizer flush 语音 _pending
            if (self._space_after_first_dot and not self._space_inserted
                    and gt_count >= 2 and tok == "。"):
                self._space_inserted = True
                yield {"content": " ", "tool_calls": None}
        yield {
            "content": None,
            "tool_calls": None,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 18,
                "total_tokens": 30,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
            },
        }


class FakeLLMToolCall(FakeLLMBackend):
    """工具审批轨迹用 fake：带 stream_think，第一轮 yield 指向需审批工具的 tool_calls，
    后续轮返回正常文本（有状态——避免 ReAct 循环每轮都触发同一工具调用）。"""

    def __init__(self, response: str = "tool") -> None:
        super().__init__(response=response)
        self._round = 0

    async def stream_think(self, messages, **kwargs):
        self._round += 1
        if self._round == 1:
            # 先流一段文本，再抛出工具调用
            for tok in self._tokenize("<inner_voice>让我先查一下记忆。</inner_voice>我正在帮你处理这个请求。\n"):
                yield {"content": tok, "tool_calls": None}
            yield {
                "content": None,
                "tool_calls": [{
                    "id": "call_save_memory_1",
                    "function": {
                        "name": "save_memory",
                        "arguments": json.dumps(
                            {"content": "这是一条需要审批后保存的记忆"}, ensure_ascii=False),
                    },
                }],
            }
        else:
            # 后续轮：正常回复文本（保存完成后的收尾）
            for tok in self._tokenize("好的，我已经把这条记忆保存下来了。\n"):
                yield {"content": tok, "tool_calls": None}
            yield {
                "content": None,
                "tool_calls": None,
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 15,
                    "total_tokens": 35,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                },
            }


def _sanitize(line: str) -> str:
    """逐行去敏：绝对路径 + 长环境变量值 → 占位符。"""
    line = _PATH_RE.sub("$SANITIZED_PATH", line)
    # 脱敏形如 KEY=longvalue 中的值（保守：只替换已知敏感模式）
    line = line.replace("test-key-placeholder", "***")
    return line


async def _make_agent(fake: FakeLLMBackend):
    """构造真实 Agent（镜像 core/input/tests/test_server.py _make_agent）。

    start() 后清空 session 消息——轨迹必须可重放，不得携带 MongoDB 真实历史会话。
    """
    from core.config import AppConfig
    from core.agent import Agent

    config = AppConfig()
    # 强制 TTS/STT 关闭：句子边界事件 + 无外部调用
    config.tts.enabled = False
    config.stt.enabled = False
    agent = Agent(config=config)
    agent.llm._backend = fake
    await agent.start()
    # 清空会话历史（SLF001 豁免：抓包工具需空会话保证轨迹稳定）
    agent.session._messages.clear()  # noqa: SLF001
    return agent


async def _capture(fake: FakeLLMBackend, send_then, max_frames: int = 200, timeout: float = 30.0,
                   headers: dict | None = None) -> list[str]:
    """连接 WS，执行 send_then，逐帧记录直到 state idle 或超时。

    on_incoming 可选回调：收到某帧后可向 ws 发送应答（如 tool_approval 回传）。
    headers: WS 握手 HTTP 头（如 X-Protocol-Version: 2）。
    """
    from core.input.server import create_app

    agent = await _make_agent(fake)
    app = create_app(agent, serve_static=False)

    from aiohttp.test_utils import TestClient, TestServer

    frames: list[str] = []
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async with client.session.ws_connect(client.make_url("/ws"), headers=headers) as ws:
            # 排空建连帧（session/capabilities/expression）
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                    frames.append(json.dumps(msg, ensure_ascii=False))
                    if msg.get("type") == "capabilities":
                        break
                except (asyncio.TimeoutError, Exception):
                    break
            await send_then(ws)
            seen_idle = False
            for _ in range(max_frames):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                frames.append(json.dumps(msg, ensure_ascii=False))
                # tool 模式：收到 tool_approval → 自动回传 approved（request_id 从帧内提取）
                if (msg.get("type") == "command"
                        and msg.get("command") == "tool_approval"):
                    rid = (msg.get("params") or {}).get("request_id")
                    if rid:
                        await ws.send_json(
                            {"type": "tool_approval", "request_id": rid, "approved": True})
                        print(f"[tool] 回传审批 approved for {rid}")
                if msg.get("type") == "state" and msg.get("state") == "idle":
                    seen_idle = True
                    break
            print(f"[capture] frames={len(frames)} idle_seen={seen_idle}")
    finally:
        await client.close()
        await agent.stop()
    return frames


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "chat"
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    if mode == "chat":
        fake = FakeLLMWithUsage(response=CHAT_RESPONSE)
        out = TRACES_DIR / "ws-chat.jsonl"

        async def _send(ws):
            await ws.send_json({"type": "text", "content": "你好，请介绍一下你自己"})

    elif mode == "server_info":
        # v2 握手：X-Protocol-Version: 2 → server_info（批次 1 交付物）
        fake = FakeLLMWithUsage(response=CHAT_RESPONSE)
        out = TRACES_DIR / "ws-server-info.jsonl"

        async def _send(ws):
            # 纯握手捕获：不发送任何消息，只记录建连帧（session/server_info/expression）
            await asyncio.sleep(1.0)

        frames = await _capture(fake, _send, max_frames=10,
                                headers={"X-Protocol-Version": "2"})
        lines = [_sanitize(f) for f in frames]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for i, line in enumerate(lines):
            json.loads(line)
        print(f"[ok] 写入 {out}（{len(lines)} 行，全部可解析）")
        return 0

    elif mode == "cancel":
        # token_delay 拉长生成 + 第一句后插入空格 flush delta，interrupt 落在生成中段
        fake = FakeLLMWithUsage(response=CANCEL_RESPONSE, token_delay=0.05,
                                space_after_first_dot=True)
        out = TRACES_DIR / "ws-cancel.jsonl"

        async def _send(ws):
            await ws.send_json({"type": "text", "content": "请详细回答（将被打断）"})
            # 等第一句 delta 流式出来（inner_voice ≈0.6s + 第一句+空格 ≈1.2s flush）再打断
            await asyncio.sleep(1.6)
            await ws.send_json({"type": "interrupt"})

    elif mode == "tool":
        fake = FakeLLMToolCall(response="tool")
        out = TRACES_DIR / "ws-tool-approval.jsonl"

        async def _send(ws):
            await ws.send_json({"type": "text", "content": "请帮我保存一条记忆"})

    else:
        print(f"未知模式: {mode}；用 chat|cancel|tool", file=sys.stderr)
        return 1

    frames = await _capture(fake, _send)
    lines = [_sanitize(f) for f in frames]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 校验：每行可解析
    for i, line in enumerate(lines):
        json.loads(line)
    print(f"[ok] 写入 {out}（{len(lines)} 行，全部可解析）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
