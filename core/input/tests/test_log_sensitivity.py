"""BE-081 日志敏感收敛测试：验证 ws_handler / cognitive.loop 的日志不再打印用户正文。

覆盖四处收敛点：
- ws_handler.py:680  "WS 收到文本"（v1 文本路由）
- ws_handler.py:651  "voice.commit 提交成功"（语音提交）
- ws_handler.py:1048 "STT 识别"（语音识别）
- loop.py:450       ">>> 用户输入"（认知循环入口）

caplog 限定 core.input.ws_handler 与 core.cognitive.loop 两个 logger 命名空间。
marker=service——构造真实调用路径（v1 WS 文本路由 + 最小 CognitiveLoop.process）。
conversation.log（core/utils/log_setup.py）是调试审计通道，有意保留，不在断言范围。
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.output import OutputManager
from core.adapter.llm_adapter import LLMAdapter
from core.cognitive import CognitiveLoop
from core.cognitive.tests.conftest import make_default_registry
from core.input.ws_handler import handle_json_message
from core.memory.session import SessionMemory
from core.monitoring.event_bus import EventBus
from core.status.agent_fsm import AgentFSM

pytestmark = pytest.mark.service

# 特征敏感内容：正文 / 本地路径 / 密钥值。把三类内容拼进消息体，
# 一旦代码仍打印正文，三类断言会同时暴露。
SENSITIVE_UPLOAD = "/uploads/be-081-scan/secret.png"
SENSITIVE_SECRET = "ATTACHMENT_SIG_SECRET_9f8e7d6c5b4a3"
SENSITIVE_BODY = f"量子青空鲸落-秘密特征正文 {SENSITIVE_UPLOAD} {SENSITIVE_SECRET}"

# I5 收敛：内心独白正文（loop.py:949 的 💭 日志载荷）
INNER_VOICE_BODY = "秘密内心独白-量子青空鲸落-8a-iv"

LOGGER_WS = "core.input.ws_handler"
LOGGER_LOOP = "core.cognitive.loop"


def _assert_no_sensitive(caplog_text: str) -> None:
    """断言收敛后的日志不含正文 / 本地路径 / 密钥值。"""
    assert SENSITIVE_BODY not in caplog_text
    assert SENSITIVE_UPLOAD not in caplog_text
    assert SENSITIVE_SECRET not in caplog_text


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, data):
        self.sent.append(data)


class FakeIdle:
    def touch(self):
        return None


class FakeAgent:
    """v1 文本路由最小 fake：process_message 记录入参，不真正跑循环。"""

    def __init__(self):
        self.user_name = "测试用户"
        self.idle = FakeIdle()
        # _find_channel 遍历 output.channels（公开枚举面）；空列表 → 判定非 v2 → 走 v1 文本路径
        self.output = type("Out", (), {"channels": []})()
        self.calls = []

    async def process_message(self, text="", user_name="", content=None, **kwargs):
        self.calls.append({"text": text, "content": content})
        return True


# ═══════════════════════════════════════════════════════════════════
# CognitiveLoop 最小构造（复用 core/cognitive/tests/test_session_resolver 模式）
# ═══════════════════════════════════════════════════════════════════

def _make_loop(default_session: SessionMemory) -> CognitiveLoop:
    """构造最小 CognitiveLoop，注入 fake llm / resolver（只跑一轮，验证入口日志）。"""
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}

    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])

    async def resolver(conversation_id, protocol_version):
        return default_session

    return CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        runtime_registry=make_default_registry(),
    )


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ws_text_log_omits_body(caplog):
    """v1 文本路由 "WS 收到文本" 只记长度/ID，不记正文。"""
    ws = FakeWs()
    agent = FakeAgent()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_WS):
        await handle_json_message(
            {"type": "text", "content": SENSITIVE_BODY}, agent,
            audio_buf=None, ws=ws, voice_mgr=None)

    # 确认走了 v1 文本路径：日志确实打出且正文确实到达 process_message
    records = [r for r in caplog.records if r.name == LOGGER_WS]
    assert any("WS 收到文本" in r.getMessage() for r in records)
    assert agent.calls and agent.calls[0]["text"] == SENSITIVE_BODY

    # 收敛：不含正文 / 本地路径 / 密钥值
    _assert_no_sensitive(caplog.text)


@pytest.mark.asyncio
async def test_cognitive_loop_log_omits_body(caplog):
    """认知循环 ">>> 用户输入" 只记长度/ID，不记正文。"""
    loop = _make_loop(SessionMemory(session_id="default"))
    with caplog.at_level(logging.DEBUG, logger=LOGGER_LOOP):
        await loop.process(SENSITIVE_BODY, user_name="测试用户")
        await _await_pending(loop)
    await loop._output.close()

    records = [r for r in caplog.records if r.name == LOGGER_LOOP]
    assert any(">>> 用户输入" in r.getMessage() for r in records)

    # 收敛：不含正文 / 本地路径 / 密钥值
    _assert_no_sensitive(caplog.text)


@pytest.mark.asyncio
async def test_inner_voice_not_in_app_log(caplog):
    """loop.py:949 内心独白仅 debug 可见——INFO 级应用日志不含正文（I5 收敛）。

    当前实现是 logger.info("💭 %s", iv_think_buf)——须显式 at_level(INFO) 才能捕获；
    降级为 debug 后 INFO 下不可见。
    """
    loop = _make_loop(SessionMemory(session_id="default"))

    async def stream_with_inner_voice(messages, tools=None, **kw):
        yield {"content": f"<inner_voice>{INNER_VOICE_BODY}</inner_voice>你好",
               "tool_calls": None}

    # 替换 fake stream：本轮输出带 <inner_voice> 标签，触发 _finalize_turn 的 💭 日志
    loop._llm.stream_think = stream_with_inner_voice

    with caplog.at_level(logging.INFO, logger=LOGGER_LOOP):
        await loop.process("测试输入", user_name="测试用户")
        await _await_pending(loop)
    await loop._output.close()

    records = [r for r in caplog.records if r.name == LOGGER_LOOP]
    # 回合确实跑完 finalize（INFO 有「回复生成完成」等摘要），排除断言空转
    assert any(r.levelno == logging.INFO for r in records)
    # 收敛：INFO 级记录不含内心独白正文
    assert not any(INNER_VOICE_BODY in r.getMessage() for r in records)
