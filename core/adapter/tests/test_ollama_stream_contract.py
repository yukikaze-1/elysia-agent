"""Ollama native 后端 → `LLMAdapter` 的消费端契约（Batch 17 / OL5 / OL-T7 / OL-T8）。

`Module/llm/tests/test_ollama_backend.py` 钉的是**出站 wire**；本模块钉的是**归一化
之后**这一侧，即 `CognitiveLoop` 真正吃到的形状：

- 流式文本顺序、终帧 usage **恰好一次**（OL5：不得每 chunk 累加——那是真实账单错误）；
- tool_calls 经 `merge_stream_tool_calls` + `normalize_tool_calls` 后成为合法 `ToolCall`
  （native 的 `arguments` 是**对象**，通用契约要 JSON 字符串）；
- 提前取消流不残留连接、后端仍可继续服务（Batch 9–15 的取消契约不回归）。

分层：`service`（MockTransport，无真实网络、无真实 Ollama）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from Module.llm.ollama import OllamaBackend
from core.adapter.llm_adapter import LLMAdapter
from core.adapter.tool_call_parser import merge_stream_tool_calls, normalize_tool_calls

pytestmark = pytest.mark.service

_TIMEOUT = httpx.Timeout(5.0)


def _chunk(content: str) -> dict:
    return {"model": "m", "created_at": "x",
            "message": {"role": "assistant", "content": content}, "done": False}


def _done(prompt: int = 100, completion: int = 25) -> dict:
    return {"model": "m", "created_at": "x", "message": {"role": "assistant", "content": ""},
            "done": True, "done_reason": "stop", "total_duration": 1, "load_duration": 1,
            "prompt_eval_count": prompt, "prompt_eval_duration": 1,
            "eval_count": completion, "eval_duration": 1}


class _Server:
    def __init__(self, frames: list[dict]) -> None:
        self.frames = frames
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode() or "{}"))
        text = "".join(json.dumps(f) + "\n" for f in self.frames)
        return httpx.Response(200, text=text,
                              headers={"content-type": "application/x-ndjson"})


def _adapter(server: _Server) -> LLMAdapter:
    backend = OllamaBackend(
        model="m", base_url="http://localhost:11434/v1", context_window=8192,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler),
                                      timeout=_TIMEOUT))
    return LLMAdapter(_test_backend=backend)


class TestOllamaUsageThroughAdapter:
    @pytest.mark.asyncio
    async def test_final_usage_is_recorded_exactly_once(self):
        """OL-T7：终帧 `prompt_eval_count=100` / `eval_count=25` ⇒ input=100/output=25/
        total=125，**恰好一次**（`core/cognitive/streaming.py` 按 chunk 记账，
        重复一次就是一笔假账）。"""
        server = _Server([_chunk("a"), _chunk("b"), _done(100, 25)])
        adapter = _adapter(server)
        seen: list[dict] = []
        async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
            if "usage" in chunk:
                seen.append(chunk["usage"])
        assert seen == [{"input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
                         "cached_tokens": None, "reasoning_tokens": None}]


class TestOllamaToolCallsThroughAdapter:
    @pytest.mark.asyncio
    async def test_native_tool_call_response_is_consumable_by_the_adapter(self):
        """OL-T8（**响应方向**）：native 的完整 tool_call（`arguments` 是对象）经既有
        合并/归一化后仍是合法 `ToolCall`，且 `arguments` 是 JSON 字符串。

        ⚠️ 本用例**只**覆盖 native → 通用这一侧。反向（把归一化后的 `ToolCall` 回放
        进下一轮请求）由 `Module/llm/tests/test_ollama_backend.py::TestToolRoundReplay`
        钉住——Batch 17 的 Challenger 正是从"名字叫 roundtrip、实际只测了一个方向"
        这条缝隙里找出了 P0（字符串 arguments ⇒ native 400）。改名是为了不再高估覆盖。
        """
        call = {"id": "call_1", "function": {"index": 0, "name": "get_weather",
                                             "arguments": {"city": "Paris"}}}
        server = _Server([{"model": "m", "message": {"role": "assistant", "content": "",
                                                     "tool_calls": [call]}, "done": False},
                          _done()])
        adapter = _adapter(server)
        accumulated: list[dict] = []
        async for chunk in adapter.stream_think(
                [{"role": "user", "content": "weather?"}],
                tools=[{"type": "function", "function": {"name": "get_weather"}}]):
            if chunk.get("tool_calls"):
                merge_stream_tool_calls(accumulated, chunk["tool_calls"])

        parsed = normalize_tool_calls(accumulated)
        assert len(parsed) == 1
        assert parsed[0].id == "call_1"
        assert parsed[0].function.name == "get_weather"
        assert json.loads(parsed[0].function.arguments) == {"city": "Paris"}


class TestOllamaStreamCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_mid_stream_leaves_the_backend_usable(self):
        """§90/§91：提前取消（aclose）后不得留下卡死的连接或半开流。

        判据是**行为**的：取消后同一后端能立刻完成下一次生成（真的再发一个请求），
        且取消本身不抛。
        """
        server = _Server([_chunk("a"), _chunk("b"), _chunk("c"), _done()])
        adapter = _adapter(server)
        stream = adapter.stream_think([{"role": "user", "content": "hi"}])
        first = await anext(stream)
        assert first["content"] == "a"
        await stream.aclose()  # 取消：关掉 HTTP 流、释放连接

        server.frames = [_chunk("z"), _done()]
        got = [c["content"] async for c in adapter.stream_think(
            [{"role": "user", "content": "again"}]) if c.get("content")]
        assert got == ["z"]
        assert len(server.requests) == 2


class TestOllamaToolRoundReplayThroughAdapter:
    @pytest.mark.asyncio
    async def test_round2_replay_from_normalized_calls_is_native_safe(self):
        """端到端回环：第 1 轮的产出 → `_prepare_tool_round` 的 messages → 第 2 轮出站体。

        这是 P0 的消费端判据：Elysia 回放的 `tool_calls[].function.arguments` 必须是
        **对象**，否则真实 daemon 对第 2 轮直接 400（工具已执行、回复永不出现）。
        """
        from core.schema.tool import ToolCall, ToolCallFunction
        from core.schema.types import ToolCallId

        call = {"id": "call_1", "function": {"index": 0, "name": "get_weather",
                                             "arguments": {"city": "Paris"}}}
        server = _Server([{"model": "m", "message": {"role": "assistant", "content": "",
                                                     "tool_calls": [call]}, "done": False},
                          _done()])
        adapter = _adapter(server)
        accumulated: list[dict] = []
        async for chunk in adapter.stream_think(
                [{"role": "user", "content": "weather?"}],
                tools=[{"type": "function", "function": {"name": "get_weather"}}]):
            if chunk.get("tool_calls"):
                merge_stream_tool_calls(accumulated, chunk["tool_calls"])
        normalized = normalize_tool_calls(accumulated)

        # `core/cognitive/tool_phase.py::_prepare_tool_round` 的真实形状
        round2 = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None,
             "tool_calls": [tc.model_dump(mode="json") for tc in normalized]},
            {"role": "tool", "tool_call_id": normalized[0].id, "content": "晴，24°C"},
        ]
        server.frames = [_chunk("巴黎晴，24°C"), _done()]
        async for _ in adapter.stream_think(round2, tools=[]):
            pass

        sent = server.requests[-1]["messages"][1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(sent, dict), (
            f"回放的 arguments 必须是对象（native 对字符串 400）：{sent!r}")
        assert sent == {"city": "Paris"}
        # 反证（防空转）：归一化产出的 ToolCall 本身仍是通用契约（字符串）
        assert isinstance(normalized[0].function.arguments, str)
        assert isinstance(normalized[0], ToolCall)
        assert isinstance(normalized[0].function, ToolCallFunction)
        assert normalized[0].id == ToolCallId("call_1")
