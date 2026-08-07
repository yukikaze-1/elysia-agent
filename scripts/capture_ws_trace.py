#!/usr/bin/env python3
"""真实 WS 事件轨迹抓取脚本（批次 0，BE-001）。

用途：产出脱敏的真实 WebSocket JSONL 轨迹，供 Electron 前端联调参考。
模式：
  python scripts/capture_ws_trace.py chat       # 完整聊天：建连→消息→inner_voice→多句→token_usage→心情→idle
  python scripts/capture_ws_trace.py cancel     # 生成中打断
  python scripts/capture_ws_trace.py tool       # 工具审批往返（save_memory HITL）
  python scripts/capture_ws_trace.py v2chat     # v2 聊天（X-Protocol-Version: 2 + clientRequestId）
  python scripts/capture_ws_trace.py v2cancel   # v2 生成中打断
  python scripts/capture_ws_trace.py v2fail     # v2 故障（503 → generation.failed）
  python scripts/capture_ws_trace.py v2session  # v2 多会话：activate_session 切隔离会话 + text 带 conversationId

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
# 环境变量值脱敏：key 名保留，可剥值（KEY=value → KEY=***）
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=(?:[^\s\"',}]+|\"[^\"]*\"|'[^']*')")

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
                 space_after_first_dot: bool = False, fault: str | None = None) -> None:
        super().__init__(response=response)
        self._token_delay = token_delay
        self._space_after_first_dot = space_after_first_dot
        self._space_inserted = False
        self._fault = fault
        self._fault_call = 0

    async def stream_think(self, messages, **kwargs):
        # 故障注入（复用 FakeLLMBackend._maybe_inject_fault 语义：http_503 → httpx 异常）
        if self._fault:
            self._fault_call += 1
            if self._fault == "http_503":
                import httpx
                req = object()
                resp = type("R", (), {"status_code": 503})()
                raise httpx.HTTPStatusError("unavailable", request=req, response=resp)
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
    """逐行去敏：绝对路径 + 环境变量值 → 占位符。"""
    line = _PATH_RE.sub("$SANITIZED_PATH", line)
    line = _ENV_RE.sub(lambda m: f"{m.group(1)}=***", line)
    line = line.replace("test-key-placeholder", "***")
    return line


def _inject_fake_storage(agent):
    """把 agent 的全部 Mongo repo 换成内存 fake（生产数据保护，review 修复 I1）。

    无论 Mongo 是否在线，轨迹运行全程不连/不写真实 Mongo：start() 的 connect 落到
    内存 FakeDocumentDBBackend（各 repo connect 对已注入 db 直接返回 True），stop()
    的 save_all() 也只写内存 fake。generation registry 换为同一 fake db 实例
    （agent 与 loop 同一引用）。同时换掉 eager default 与已构造 SessionMemory 的
    repo 引用，保证任何路径都不触碰生产 default。
    """
    from core.cognitive.generation_registry import GenerationRegistry
    from core.storage.character_version_repo import CharacterVersionRepository
    from core.storage.generation_repo import GenerationRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

    backend = FakeDocumentDBBackend()
    session_repo = SessionRepository(db=backend)
    message_repo = MessageRepository(db=backend)
    token_repo = TokenRepository(db=backend)
    version_repo = CharacterVersionRepository(db=backend)
    agent._session_repo = session_repo
    agent._message_repo = message_repo
    agent._token_repo = token_repo
    agent._version_repo = version_repo
    agent.session_manager._repo = session_repo
    agent.session_manager._msg_repo = message_repo
    for s in agent.session_manager._sessions.values():
        s._repo = session_repo
        s._msg_repo = message_repo
    reg = GenerationRegistry(repo=GenerationRepository(db=backend))
    agent._generation_registry = reg
    agent.cognitive._generation_registry = reg
    return agent


async def _make_agent(fake: FakeLLMBackend):
    """构造真实 Agent（镜像 core/input/tests/test_server.py _make_agent）。

    start() 后清空 session 消息——轨迹必须可重放，不得携带真实历史会话。

    注意（生产数据保护，review 修复 I1）：注入内存 fake 存储——清空的是内存 deque，
    绝不连真 Mongo。若用真 repo 且 Mongo 在线，agent.stop() 的 save_all() 会把清空后
    的 deque 回写 Mongo，覆盖生产 default。全部模式（chat/cancel/tool/server_info/
    v2chat/v2cancel/v2fail/v2session）现统一走此隔离工厂。
    """
    from core.config import AppConfig
    from core.agent import Agent

    config = AppConfig()
    # 强制 TTS/STT 关闭：句子边界事件 + 无外部调用
    config.tts.enabled = False
    config.stt.enabled = False
    agent = Agent(config=config)
    _inject_fake_storage(agent)
    agent.llm._backend = fake
    await agent.start()
    # 清空会话历史（SLF001 豁免：抓包工具需空会话保证轨迹稳定）
    agent.session._messages.clear()  # noqa: SLF001
    return agent


async def _make_agent_isolated(fake: FakeLLMBackend):
    """v2session 轨迹用 agent 工厂：不清 default，切到隔离测试会话。

    生产数据保护（Task 13 强制 + review 修复 I1）：基座 `_make_agent` 已注入内存
    fake 存储（不连真 Mongo）；此处再切 active 到隔离测试会话 `conv_trace_x`——
    v2 建连回放取 active（resolve(None, 2)），显示空会话，不泄露 default 历史。
    conv_trace_x 为测试会话：清空其内存 deque（轨迹空会话可重放），绝不触碰 default。
    """
    agent = await _make_agent(fake)
    # 预注册隔离测试会话（内存缓存构造，不触发 load/不落库）
    agent.session_manager.get("conv_trace_x")  # noqa: SLF001
    # 把活动指针切到隔离会话——v2 建连回放取 active 即显示 conv_trace_x（空）
    await agent.session_manager.switch("conv_trace_x")
    # 清空隔离会话内存 deque——轨迹从空会话开始可重放
    agent.session_manager.get("conv_trace_x")._messages.clear()  # noqa: SLF001
    return agent


async def _drain_connect_frames(ws, frames: list[str], *, max_connect: int = 5
                                ) -> tuple[list[str], bool]:
    """排空建连帧；异常（连接被 reset/EOF）→ 返回 (frames, truncated=True) 不静默。

    返回 (frames, truncated)：truncated 表示连接异常截断（非正常 timeout 结束）。
    """
    truncated = False
    for _ in range(max_connect):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            truncated = True
            print(f"[capture] 连接/握手失败，轨迹可能截断: {e}", file=sys.stderr)
            break
        frames.append(json.dumps(msg, ensure_ascii=False))
        if msg.get("type") == "capabilities":
            break
    return frames, truncated


async def _capture(fake: FakeLLMBackend, send_then, max_frames: int = 200, timeout: float = 30.0,
                   headers: dict | None = None,
                   make_agent=None) -> list[str]:
    """连接 WS，执行 send_then，逐帧记录直到 state idle 或超时。

    on_incoming 可选回调：收到某帧后可向 ws 发送应答（如 tool_approval 回传）。
    headers: WS 握手 HTTP 头（如 X-Protocol-Version: 2）。
    make_agent: agent 工厂（默认 _make_agent；v2session 用 _make_agent_isolated）。
    """
    from core.input.server import create_app

    make_agent = make_agent or _make_agent
    agent = await make_agent(fake)
    app = create_app(agent, serve_static=False)

    from aiohttp.test_utils import TestClient, TestServer

    frames: list[str] = []
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async with client.session.ws_connect(client.make_url("/ws"), headers=headers) as ws:
            # 排空建连帧（session/capabilities/expression）；异常截断打警示不静默
            frames, truncated = await _drain_connect_frames(ws, frames)
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
            if truncated:
                print("[capture] 警告：建连阶段连接异常，轨迹可能截断", file=sys.stderr)
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

    elif mode in ("v2chat", "v2cancel", "v2fail"):
        # v2 轨迹（批次 2 交付物）：X-Protocol-Version: 2 + clientRequestId
        rid = f"req_cap_{os.urandom(4).hex()}"
        headers = {"X-Protocol-Version": "2"}
        if mode == "v2chat":
            fake = FakeLLMWithUsage(response=CHAT_RESPONSE)
            out = TRACES_DIR / "ws-v2-chat.jsonl"

            async def _send(ws):
                await ws.send_json({"type": "text", "content": "你好，请介绍一下你自己",
                                    "clientRequestId": rid})
        elif mode == "v2cancel":
            fake = FakeLLMWithUsage(response=CANCEL_RESPONSE, token_delay=0.05,
                                    space_after_first_dot=True)
            out = TRACES_DIR / "ws-v2-cancel.jsonl"

            async def _send(ws):
                await ws.send_json({"type": "text", "content": "请详细回答（将被打断）",
                                    "clientRequestId": rid})
                await asyncio.sleep(1.6)
                await ws.send_json({"type": "interrupt"})
        else:  # v2fail
            fake = FakeLLMWithUsage(response=CHAT_RESPONSE, fault="http_503")
            out = TRACES_DIR / "ws-v2-fail.jsonl"

            async def _send(ws):
                await ws.send_json({"type": "text", "content": "hi",
                                    "clientRequestId": rid})

        frames = await _capture(fake, _send, max_frames=80, timeout=20, headers=headers)
        lines = [_sanitize(f) for f in frames]
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for i, line in enumerate(lines):
            json.loads(line)
        print(f"[ok] 写入 {out}（{len(lines)} 行，全部可解析）")
        return 0

    elif mode == "v2session":
        # 批次 3（Task 13）多会话轨迹：activate_session 切隔离会话 + text 带
        # conversationId + clientRequestId。用 _make_agent_isolated（不清 default，
        # 预注册 conv_trace_x 内存会话）——生产数据保护：绝不删/清 default。
        fake = FakeLLMWithUsage(response=CHAT_RESPONSE)
        out = TRACES_DIR / "ws-v2-session.jsonl"
        rid = f"req_cap_{os.urandom(4).hex()}"
        headers = {"X-Protocol-Version": "2"}

        async def _send(ws):
            # 1) activate_session：切到隔离测试会话（服务端重发该会话回放）
            await ws.send_json({"type": "activate_session", "conversationId": "conv_trace_x"})
            await asyncio.sleep(0.6)  # 等 activate 回放帧落盘再发文本
            # 2) text 带 conversationId → 生成落到 conv_trace_x
            await ws.send_json({"type": "text", "content": "你好，请介绍一下你自己",
                                "conversationId": "conv_trace_x", "clientRequestId": rid})

        frames = await _capture(fake, _send, max_frames=120, timeout=30, headers=headers,
                                make_agent=_make_agent_isolated)
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
        print(f"未知模式: {mode}；用 chat|cancel|tool|server_info|v2chat|v2cancel|v2fail|v2session",
              file=sys.stderr)
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
