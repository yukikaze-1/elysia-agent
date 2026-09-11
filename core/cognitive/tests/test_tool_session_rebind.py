"""BE-S3-RUN-P0-01：describe_image 必须读每轮目标会话的当前图片（marker=service）。

覆盖复验清单 §3.4 六用例（Agent/loop 级）：default 后新会话发图可取 1 图 / A·B 不串 /
v2 显式非 active / 图文交替不残留 / 失败后再发图绑定仍正确 / 多模态直看图分支不回归。

使用真实 DescribeImagePlugin + stub 视觉后端（LLMFactory.create patch，不碰真实 LLM）
+ 最小 CognitiveLoop（镜像 test_tts_voice_injection.py::_build_loop）。
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from Module.output.websocket import WebSocketChannel
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.generation_registry import GenerationRegistry
from core.cognitive.timing import TimingClock
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.schema.message import UserMessage
from core.schema.types import MessageId
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity

pytestmark = pytest.mark.service

# 两张不同图片的 data URI（describe_image 断言按内容区分，防串会话）
IMG_A = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
IMG_B = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

_IMG_CONTENT_A = [{"type": "image", "src": IMG_A, "mime": "image/png"},
                  {"type": "text", "content": "这张图里有什么？"}]
_IMG_CONTENT_B = [{"type": "image", "src": IMG_B, "mime": "image/png"},
                  {"type": "text", "content": "这张图里是什么？"}]


class _FakeVisionBackend:
    """视觉后端替身：记录收到的 messages，返回固定描述（不碰真实 LLM）。"""

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.messages.append(messages)
        return "测试描述：图中是一只猫。"

    async def close(self) -> None:
        pass


class _ToolCallingLLM:
    """首个响应发 describe_image tool_call，后续响应返回纯文本（驱动 ReAct 工具轮）。"""

    def __init__(self, response: str = "好的，已查看图片。") -> None:
        import re
        self.response = response
        self.calls = 0
        self.model = "fake/test"  # CognitiveLoop 访问 self._llm.model（loop.py:504/615/689/723）
        self._tok = re.compile(r"([一-鿿]|[\w]+|[^\w\s])")

    async def chat(self, messages: list[dict], **kwargs) -> str:
        return self.response

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        pass

    async def stream_think(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"content": None, "tool_calls": [{
                "index": 0, "id": "call_describe",
                "function": {"name": "describe_image", "arguments": "{}"},
            }]}
            return
        for tok in self._tok.findall(self.response):
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None}


def _default_rt() -> CharacterRuntime:
    """构造 elysia default runtime（registry 预置用，测试不关心角色值）。"""
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId=DEFAULT_INSTANCE_ID, characterId="chr_builtin_elysia"),
        definition=CharacterDefinition(characterId="chr_builtin_elysia"),
        revision=CharacterRevision(characterId="chr_builtin_elysia", revision=1, displayName="Elysia"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="妖梦"),
    )
    from core.cognitive.tests.conftest import mock_engine_with_state
    rt.ensure_engine(lambda: mock_engine_with_state(instance_id=DEFAULT_INSTANCE_ID))   # 预置 engine：_prepare_turn 不再触发 engine_factory
    return rt


async def _build_loop(session_a: SessionMemory, session_b: SessionMemory,
                      tmp_path, monkeypatch):
    """最小 CognitiveLoop + 真实 ToolAdapter（describe_image + stub 视觉后端）。

    resolver：显式 conversationId → 该会话；None → 活动会话 A（v2 缺省语义）。
    monkeypatch 注入 stub 视觉后端 + 缓存目录重定向（防写真实 data/cache/vision）。
    注意：adapter.start() **不注入** startup session（H1/L3 定案）——pre-fix 时
    services 无 "session" 键 → describe_image 报「session 不可用」，用例先红；
    post-fix process() 换绑后 key 存在且指向目标会话 → 用例转绿。
    """
    from core.adapter.tool_adapter import ToolAdapter
    from core.tools.vision.plugin import DescribeImagePlugin

    vision = _FakeVisionBackend()
    llm = _ToolCallingLLM()
    ws = AsyncMock()
    output = OutputManager()
    output.add_channel(WebSocketChannel(ws, protocol_version=2))
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    registry = GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))
    rt_reg = CharacterRuntimeRegistry()
    rt_reg.get(DEFAULT_INSTANCE_ID, lambda: _default_rt())

    async def resolver(conversation_id, protocol_version):
        if conversation_id == "A":
            return session_a
        if conversation_id == "B":
            return session_b
        return session_a  # v2 缺省 → 活动会话 A

    monkeypatch.setattr(
        "core.tools.vision.plugin.LLMFactory.create", lambda *a, **k: vision)
    monkeypatch.setattr(
        "core.tools.vision.plugin._CACHE_DIR", tmp_path / "vision_cache")
    adapter = ToolAdapter([DescribeImagePlugin], session_id="default")
    await adapter.start()

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        generation_registry=registry,
        runtime_registry=rt_reg,
    )
    loop.configure(tool_adapter=adapter)
    return loop, ws, adapter, vision


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]  # noqa: SLF001
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _first_image_url(vision: _FakeVisionBackend) -> str | None:
    """从视觉后端收到的首条消息里取 image_url.url（无图返回 None）。"""
    if not vision.messages:
        return None
    content = vision.messages[0][0]["content"]
    for c in content:
        if c.get("type") == "image_url":
            return c["image_url"]["url"]
    return None


class TestToolSessionRebind:
    @pytest.mark.asyncio
    async def test_image_in_new_session_visible_to_tool(self, tmp_path, monkeypatch):
        """default 启动后新建会话 A 发图 → describe_image 取得当前用户消息的 1 张图。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")  # 启动会话（模拟 default）
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("这张图里有什么？", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_A, "describe_image 未读到 A 会话当前图片"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_session_switch_no_cross_talk(self, tmp_path, monkeypatch):
        """连续在 A、B 各发不同图：B 的工具只看到 B 的图，不读到 A。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("A图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            vision.messages.clear()
            loop._llm = _ToolCallingLLM()  # noqa: SLF001 — 换新 LLM：calls 计数器逐 turn 重置（首个响应发 tool_call）
            await loop.process("B图", content=_IMG_CONTENT_B,
                               conversation_id="B", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_B, "B 的 turn 读到 A 的图（串会话）"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_explicit_non_active_session(self, tmp_path, monkeypatch):
        """v2 显式 conversationId 指向非活动会话 B → 工具读取显式目标 B。"""
        session_a = SessionMemory(session_id="A")  # 活动会话
        session_b = SessionMemory(session_id="B")  # 显式非活动
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("B图", content=_IMG_CONTENT_B,
                               conversation_id="B", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_B, "显式 B 未读 B 的图"
            # 换绑服务值也应指向 B（不残留 A）
            assert adapter.tool_context.services["session"] is session_b
            # spec §4.1 要点 2 记录的 off-by-one：换绑先于用户消息入队，快照=turn 前计数。
            # 全新 B 会话 turn 前计数为 0（process 后 session_b.messages 已有 4 条，不相等）。
            assert adapter.tool_context.services["_session_msg_count"] == 0
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_text_turn_no_residual_image(self, tmp_path, monkeypatch):
        """图 turn 后接纯文本 turn → describe_image 0 图，不残留上一轮图片。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("看图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert vision.messages  # 首轮确实读了图
            vision.messages.clear()
            # 纯文本 turn：LLM 仍发 describe_image tool_call，但当前用户消息无图
            loop._llm = _ToolCallingLLM()  # noqa: SLF001 — 换新 LLM（首个响应发 tool_call）
            await loop.process("你好", conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert vision.messages == [], "纯文本 turn 仍看到上一轮图片（残留）"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_failed_turn_then_retry_binding_correct(self, tmp_path, monkeypatch):
        """generation 失败后再发图 → session 绑定仍正确。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            # 第一轮：LLM 首轮抛错 → generation 失败（不产生工具轮）
            async def _boom_stream(messages, tools=None, **kwargs):
                if False:
                    yield None  # L2：async generator（async def 直接 raise 只返回 coroutine，非可迭代）
                raise ConnectionError("fake 断连")
            loop._llm.stream_think = _boom_stream  # noqa: SLF001
            await loop.process("失败图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            # 第二轮：恢复 LLM → describe_image 仍读到 A 的图
            loop._llm = _ToolCallingLLM()  # noqa: SLF001 — 换回正常 LLM（重试 turn）
            await loop.process("重试图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_A, "失败后再发图 session 绑定不正确"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_multimodal_direct_view_keeps_describe_image_excluded(self, tmp_path, monkeypatch):
        """多模态模型直看图分支：describe_image 从 tool_defs 排除（既有行为不回归）。"""
        from Module.llm.registry import LLMFactory
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            loop._clock = TimingClock()  # noqa: SLF001
            loop._session = session_a  # noqa: SLF001 — _build_turn_prompt 直测需 _session（loop.py:627 add_message）
            monkeypatch.setattr(LLMFactory, "supports_multimodal",
                                classmethod(lambda cls, m: True))
            _, tool_defs = await loop._build_turn_prompt("你好", "u", None, "", None, False)
            assert "describe_image" not in [t["function"]["name"] for t in tool_defs]
            monkeypatch.setattr(LLMFactory, "supports_multimodal",
                                classmethod(lambda cls, m: False))
            _, tool_defs2 = await loop._build_turn_prompt("你好", "u", None, "", None, False)
            assert "describe_image" in [t["function"]["name"] for t in tool_defs2]
        finally:
            await loop._output.close()


async def _drain_archive(session: SessionMemory) -> None:
    """等待会话 fire-and-forget 归档 task 落库。"""
    tasks = [t for t in session._archive_tasks if not t.done()]  # noqa: SLF001
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestWriteGuardNoFalsePositive:
    """端到端组合验证（P3-1 + P3-3）：真实工具回合归档全部带角色戳，且兜底不误报。

    这条链路只有组合起来才成立：工具轮归档点补 character（P3-1）→ 兜底开关打开
    （P3-3）→ 真实回合并**不**产生告警。若日后有人再摘掉某个归档点的 character，
    本用例会红（兜底告警会被 caplog 捕获），而单点单测不会。
    """

    @pytest.mark.asyncio
    async def test_real_tool_turn_stamps_every_assistant_and_no_warning(
        self, tmp_path, monkeypatch, caplog,
    ):
        import logging

        from core.storage.message_repo import COLLECTION as MSG_COLLECTION
        from core.storage.message_repo import MessageRepository
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake, require_assistant_character=True)
        session_a = SessionMemory(session_id="A", msg_repo=repo)
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(
            session_a, session_b, tmp_path, monkeypatch)
        try:
            with caplog.at_level(logging.WARNING):
                await loop.process("这张图里有什么？", content=_IMG_CONTENT_A,
                                   conversation_id="A", protocol_version=2)
                await _await_pending(loop)
                await _drain_archive(session_a)

            archived = await fake.find(MSG_COLLECTION, {})
            assistant = [d for d in archived if d.get("role") == "assistant"]
            assert len(assistant) >= 2, (
                f"工具回合应归档工具轮 + 终轮共 ≥2 条 assistant，实得 {len(assistant)}")
            missing = [d.get("message_id") for d in assistant if not d.get("character")]
            assert not missing, f"这些归档 assistant 缺角色快照: {missing}"

            guard_warnings = [
                r.getMessage() for r in caplog.records
                if "角色完整性告警" in r.getMessage()
            ]
            assert not guard_warnings, (
                f"正常工具回合不应触发写入期兜底告警（说明有归档点漏传 character）: "
                f"{guard_warnings}")
        finally:
            await loop._output.close()
