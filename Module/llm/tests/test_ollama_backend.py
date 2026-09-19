"""`OllamaBackend`（native `/api/chat`）的确定性用例 —— Batch 17 / AR-16。

判据全部落在**出站 wire 与归一化输出**上（`httpx.MockTransport`，无真实网络、无真实
Ollama）。协议/端点的选择理由见 `Module/llm/ollama.py` 模块 docstring 与本批执行记录；
真实 daemon 的冷启动旁证见 `Module/llm/tests/test_ollama_real.py`（`external`，opt-in）。

分层：`service`。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from Module.llm.ollama import OllamaBackend, _native_base_url

pytestmark = pytest.mark.service

_BASE = "http://localhost:11434/v1"
_TIMEOUT = httpx.Timeout(5.0)


# ============================================================
# 测试替身：一个记录请求、按脚本回响的 native 服务器
# ============================================================

class _FakeOllama:
    """记录 (path, body)；按 `frames`（流式）或 `payload`（非流式）回响。"""

    def __init__(self, *, frames: list[dict] | None = None,
                 payload: dict | None = None,
                 status: int = 200, ps: dict | None = None,
                 ps_status: int = 200) -> None:
        self.frames = frames
        self.payload = payload
        self.status = status
        self.ps = ps
        self.ps_status = ps_status
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        self.calls.append((request.url.path, body))
        if request.url.path.endswith("/api/ps"):
            if self.ps_status != 200:
                return httpx.Response(self.ps_status, json={"error": "boom"})
            return httpx.Response(200, json=self.ps or {"models": []})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "model 'x' not found"})
        if body.get("stream"):
            text = "".join(json.dumps(f) + "\n" for f in (self.frames or []))
            return httpx.Response(200, text=text,
                                  headers={"content-type": "application/x-ndjson"})
        return httpx.Response(200, json=self.payload or _full_response("ok"))

    @property
    def last_body(self) -> dict:
        assert self.calls, "没有捕获到任何 /api/chat 请求"
        return self.calls[-1][1]

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


def _full_response(content: str, *, prompt: int = 7, completion: int = 3) -> dict:
    return {
        "model": "qwen2.5:3b",
        "created_at": "2026-09-19T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 10,
        "load_duration": 5,
        "prompt_eval_count": prompt,
        "prompt_eval_duration": 2,
        "eval_count": completion,
        "eval_duration": 3,
    }


def _chunk(content: str) -> dict:
    return {"model": "qwen2.5:3b", "created_at": "x",
            "message": {"role": "assistant", "content": content}, "done": False}


def _done(**extra: Any) -> dict:
    frame = _full_response("", **extra)
    frame["message"] = {"role": "assistant", "content": ""}
    return frame


def _backend(server: _FakeOllama, **kwargs: Any) -> OllamaBackend:
    kwargs.setdefault("context_window", 8192)
    return OllamaBackend(model="qwen2.5:3b", base_url=_BASE,
                         http_client=httpx.AsyncClient(
                             transport=httpx.MockTransport(server.handler),
                             timeout=_TIMEOUT),
                         **kwargs)


# ============================================================
# 1. base_url 归一（自定义远端不能硬化成 localhost:11434）
# ============================================================

class TestNativeBaseUrl:
    @pytest.mark.parametrize("given, expected", [
        ("http://localhost:11434/v1", "http://localhost:11434"),
        ("http://localhost:11434/v1/", "http://localhost:11434"),
        ("http://localhost:11434", "http://localhost:11434"),
        ("http://localhost:11434/api", "http://localhost:11434"),
        ("https://ollama.example.com:8443/v1", "https://ollama.example.com:8443"),
        ("https://gw.example.com/ollama/v1", "https://gw.example.com/ollama"),
        ("", "http://localhost:11434"),
    ])
    def test_normalization(self, given, expected):
        assert _native_base_url(given) == expected

    @pytest.mark.asyncio
    async def test_custom_remote_host_is_used_verbatim(self):
        """§84/§109：不得把 `http://localhost:11434` 写死——用 resolved base_url。"""
        server = _FakeOllama(payload=_full_response("hi"))
        backend = OllamaBackend(model="m", base_url="https://ollama.example.com/v1",
                                context_window=4096,
                                api_key="secret-token",
                                http_client=httpx.AsyncClient(
                                    transport=httpx.MockTransport(server.handler),
                                    timeout=_TIMEOUT))
        await backend.chat([{"role": "user", "content": "hi"}])
        assert server.paths == ["/api/chat"]

    @pytest.mark.asyncio
    async def test_remote_gateway_auth_header_is_kept(self):
        """§86：远端 gateway 带 auth 时不得丢认证头。"""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json=_full_response("hi"))

        backend = OllamaBackend(model="m", base_url="https://gw.example.com/v1",
                                api_key="secret-token",
                                http_client=httpx.AsyncClient(
                                    transport=httpx.MockTransport(handler),
                                    timeout=_TIMEOUT))
        await backend.chat([{"role": "user", "content": "hi"}])
        assert seen.get("authorization") == "Bearer secret-token"


# ============================================================
# 2. wire 构造：num_ctx / 参数映射 / keep_alive / 丢弃
# ============================================================

class TestPayloadTranslation:
    @pytest.mark.asyncio
    async def test_declared_context_becomes_num_ctx(self):
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server, context_window=8192)
        await backend.chat([{"role": "user", "content": "hi"}])
        assert server.last_body["options"]["num_ctx"] == 8192

    @pytest.mark.asyncio
    async def test_missing_or_invalid_context_sends_no_num_ctx(self):
        for bad in (None, 0, -1):
            server = _FakeOllama(payload=_full_response("ok"))
            backend = _backend(server, context_window=bad)
            await backend.chat([{"role": "user", "content": "hi"}])
            assert "options" not in server.last_body, (bad, server.last_body)
            assert backend.requested_context is None

    @pytest.mark.asyncio
    async def test_max_tokens_and_temperature_map_to_native_options(self):
        """`max_tokens`/`temperature` 在 native 的真实对应是 `options.num_predict/.temperature`。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([{"role": "user", "content": "hi"}],
                           max_tokens=64, temperature=0.2)
        options = server.last_body["options"]
        assert options["num_predict"] == 64
        assert options["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_tools_are_passed_through_in_openai_shape(self):
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        tools = [{"type": "function",
                  "function": {"name": "f", "parameters": {"type": "object"}}}]
        await backend.chat([{"role": "user", "content": "hi"}], tools=tools)
        assert server.last_body["tools"] == tools

    @pytest.mark.asyncio
    async def test_keep_alive_is_a_wire_hook_not_a_config_surface(self):
        """§26-C：默认**不发**该键；显式传入才转写（内部/测试钩子，无用户配置项）。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([{"role": "user", "content": "hi"}])
        assert "keep_alive" not in server.last_body

        server2 = _FakeOllama(payload=_full_response("ok"))
        backend2 = _backend(server2, keep_alive="30s")
        await backend2.chat([{"role": "user", "content": "hi"}])
        assert server2.last_body["keep_alive"] == "30s"

    @pytest.mark.asyncio
    async def test_non_native_params_are_dropped_and_warned_once(self, caplog):
        """native 无对应语义的参数一律丢弃（不发明语义），并**只警告一次**。

        等价于通用后端的 `thinking` 门禁（`supports_thinking=False`）——但 Ollama 的
        丢弃面是"所有非 native 参数"，故单独钉住。
        """
        import logging

        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await backend.chat(
                    [{"role": "user", "content": "hi"}],
                    tool_choice="auto",
                    extra_body={"thinking": {"type": "disabled"}},
                    stream_options={"include_usage": True})

        body = server.last_body
        for forbidden in ("tool_choice", "extra_body", "stream_options", "thinking"):
            assert forbidden not in body, body
        hits = [r for r in caplog.records if "Ollama native 协议无对应字段" in r.message]
        assert len(hits) == 1, f"应恰好警告一次，实际 {len(hits)} 次"

    @pytest.mark.asyncio
    async def test_prefix_continuation_is_rejected_not_silently_dropped(self):
        """prefix 只能显式拒绝——静默丢字段会把"续写"变成"重新回答"，两者都 200。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        with pytest.raises(ValueError, match="prefix"):
            await backend.chat([{"role": "assistant", "content": "{\\n", "prefix": True}])
        assert server.calls == [], "被拒绝的请求不得发出"

    @pytest.mark.asyncio
    async def test_system_message_order_and_roles_are_preserved(self):
        """§25：system role 与 messages 顺序不得被后端改写。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ])
        assert server.last_body["messages"] == [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]

    @pytest.mark.asyncio
    async def test_image_blocks_are_translated_not_dropped(self):
        """块列表 content 忠实转写（当前 `supports_multimodal: false`，能力不扩）。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([{
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这个"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        }])
        message = server.last_body["messages"][0]
        assert message["content"] == "看看这个"
        assert message["images"] == ["QUJD"]


# ============================================================
# 3. 流式归一化：文本顺序 / tool_calls / usage 恰好一次
# ============================================================

class TestStreamNormalization:
    @pytest.mark.asyncio
    async def test_text_chunks_keep_order_and_done_terminates(self):
        server = _FakeOllama(frames=[_chunk("你"), _chunk("好"), _done()])
        backend = _backend(server)
        got = [c async for c in backend.chat_stream([{"role": "user", "content": "hi"}])]
        assert got == ["你", "好"]

    @pytest.mark.asyncio
    async def test_usage_is_normalized_exactly_once_on_the_final_frame(self):
        server = _FakeOllama(frames=[_chunk("a"), _chunk("b"),
                                     _done(prompt=100, completion=25)])
        backend = _backend(server)
        chunks = [c async for c in backend.stream_think([{"role": "user", "content": "hi"}])]
        usages = [c["usage"] for c in chunks if "usage" in c]
        assert len(usages) == 1, f"usage 必须恰好一次，实际 {len(usages)} 次"
        assert usages[0] == {
            "input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
            "cached_tokens": None, "reasoning_tokens": None,
        }
        assert backend.last_usage == usages[0]

    @pytest.mark.asyncio
    async def test_stream_without_final_frame_fabricates_no_usage(self):
        """流在终帧前断开 ⇒ 不编造 usage（宁可没有，也不要一笔假账）。"""
        server = _FakeOllama(frames=[_chunk("a")])
        backend = _backend(server)
        chunks = [c async for c in backend.stream_think([{"role": "user", "content": "hi"}])]
        assert [c for c in chunks if "usage" in c] == []
        assert backend.last_usage is None

    @pytest.mark.asyncio
    async def test_tool_calls_are_translated_to_the_generic_delta_contract(self):
        """native 的 `arguments` 是对象、带 `function.index`；通用契约要 JSON 字符串。"""
        call = {"id": "call_1", "function": {"index": 0, "name": "get_weather",
                                             "arguments": {"city": "Paris"}}}
        frames = [{"model": "m", "message": {"role": "assistant", "content": "",
                                             "tool_calls": [call]}, "done": False},
                  _done()]
        server = _FakeOllama(frames=frames)
        backend = _backend(server)
        chunks = [c async for c in backend.stream_think([{"role": "user", "content": "hi"}])]
        deltas = [tc for c in chunks if c.get("tool_calls") for tc in c["tool_calls"]]
        assert len(deltas) == 1
        assert deltas[0] == {
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_keep_distinct_indices(self):
        calls = [
            {"id": "call_1", "function": {"index": 0, "name": "get_weather",
                                          "arguments": {"city": "Paris"}}},
            {"id": "call_2", "function": {"index": 1, "name": "get_time",
                                          "arguments": {"city": "Tokyo"}}},
        ]
        frames = [{"model": "m", "message": {"role": "assistant", "content": "",
                                             "tool_calls": calls}, "done": False},
                  _done()]
        server = _FakeOllama(frames=frames)
        backend = _backend(server)
        chunks = [c async for c in backend.stream_think([{"role": "user", "content": "hi"}])]
        deltas = [tc for c in chunks if c.get("tool_calls") for tc in c["tool_calls"]]
        assert [d["index"] for d in deltas] == [0, 1]
        assert [d["function"]["name"] for d in deltas] == ["get_weather", "get_time"]

    @pytest.mark.asyncio
    async def test_stream_error_frame_is_raised_not_swallowed(self):
        frames = [{"error": "model requires more system memory"}]
        server = _FakeOllama(frames=frames)
        backend = _backend(server)
        with pytest.raises(RuntimeError, match="model requires more system memory"):
            async for _ in backend.stream_think([{"role": "user", "content": "hi"}]):
                pass


# ============================================================
# 4. 非流式 + 错误归一化（重试映射不吃回归）
# ============================================================

class TestNonStreamAndErrors:
    @pytest.mark.asyncio
    async def test_chat_returns_content_and_normalized_usage(self):
        server = _FakeOllama(payload=_full_response("你好", prompt=11, completion=4))
        backend = _backend(server)
        text = await backend.chat([{"role": "user", "content": "hi"}])
        assert text == "你好"
        assert backend.last_usage == {
            "input_tokens": 11, "output_tokens": 4, "total_tokens": 15,
            "cached_tokens": None, "reasoning_tokens": None}

    @pytest.mark.asyncio
    async def test_model_not_found_is_a_404_without_retry_semantics(self):
        """§62：未知模型 → 404（`_extract_status` 可读），**不在**可重试集合里。"""
        from core.adapter.llm_adapter import _RETRYABLE_STATUS, LLMAdapter

        server = _FakeOllama(status=404)
        backend = _backend(server)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await backend.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.response.status_code == 404
        assert LLMAdapter._extract_status(exc_info.value) == 404
        assert 404 not in _RETRYABLE_STATUS

    @pytest.mark.asyncio
    async def test_server_error_is_retryable_by_the_existing_mapping(self):
        from core.adapter.llm_adapter import _RETRYABLE_STATUS, LLMAdapter

        server = _FakeOllama(status=503)
        backend = _backend(server)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await backend.chat([{"role": "user", "content": "hi"}])
        assert LLMAdapter._extract_status(exc_info.value) in _RETRYABLE_STATUS

    @pytest.mark.asyncio
    async def test_connection_failure_has_no_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        backend = OllamaBackend(model="m", base_url=_BASE,
                                http_client=httpx.AsyncClient(
                                    transport=httpx.MockTransport(handler),
                                    timeout=_TIMEOUT))
        with pytest.raises(httpx.ConnectError):
            await backend.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_unexpected_200_shape_raises_instead_of_blank_reply(self):
        """O2 的教训：形状意外的 200 不得折成"空白回复"（调用方无法感知）。"""
        server = _FakeOllama(payload={"done": True, "eval_count": 1})
        backend = _backend(server)
        with pytest.raises(RuntimeError, match="无 message 字段"):
            await backend.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_content_is_legal(self):
        """反证（防空转）：`content: ""` 是合法的（tool-only 响应），不得被上面那条误伤。"""
        server = _FakeOllama(payload=_full_response(""))
        backend = _backend(server)
        assert await backend.chat([{"role": "user", "content": "hi"}]) == ""

    @pytest.mark.asyncio
    async def test_malformed_json_response_is_an_error_not_empty_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="{not json")

        backend = OllamaBackend(model="m", base_url=_BASE,
                                http_client=httpx.AsyncClient(
                                    transport=httpx.MockTransport(handler),
                                    timeout=_TIMEOUT))
        with pytest.raises(json.JSONDecodeError):
            await backend.chat([{"role": "user", "content": "hi"}])


# ============================================================
# 5. 热路径纪律：1 generation = 1 provider request；/api/ps 仅诊断
# ============================================================

class TestHotPathDiscipline:
    @pytest.mark.asyncio
    async def test_no_generation_entry_point_probes_the_runner(self):
        """M6 杀手：**三条**生成入口都不得出现 `/api/ps` 或任何额外往返。

        只测 `stream_think` 是不够的——一条"在 `chat()` 里顺手探一下"的改动
        （2026-09-19 变异 M6）会让那种窄守卫**存活**。所以这里逐入口断言
        `paths == ["/api/chat"] * n`。
        """
        server = _FakeOllama(frames=[_chunk("a"), _done()],
                             payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([{"role": "user", "content": "hi"}])
        assert server.paths == ["/api/chat"], server.paths
        async for _ in backend.chat_stream([{"role": "user", "content": "hi"}]):
            pass
        assert server.paths == ["/api/chat"] * 2, server.paths
        async for _ in backend.stream_think([{"role": "user", "content": "hi"}]):
            pass
        assert server.paths == ["/api/chat"] * 3, server.paths
        assert not any("/api/ps" in p for p in server.paths), server.paths

    @pytest.mark.asyncio
    async def test_diagnostics_probe_is_opt_in_and_reports_three_truths(self):
        server = _FakeOllama(ps={"models": [{"name": "qwen2.5:3b",
                                             "context_length": 8192}]})
        backend = _backend(server, context_window=8192)
        info = await backend.probe_runtime_context()
        assert info["configured"] == 8192
        assert info["requested"] == 8192
        assert info["observed"] == 8192
        assert info["error"] is None

    @pytest.mark.asyncio
    async def test_diagnostics_reports_mismatch_without_raising(self):
        server = _FakeOllama(ps={"models": [{"name": "qwen2.5:3b",
                                             "context_length": 4096}]})
        backend = _backend(server, context_window=8192)
        info = await backend.probe_runtime_context()
        assert (info["configured"], info["requested"], info["observed"]) == (8192, 8192, 4096)

    @pytest.mark.asyncio
    async def test_diagnostics_failure_never_breaks_generation(self):
        """§68：`/api/ps` 不可用不能影响正常 generation。"""
        server = _FakeOllama(payload=_full_response("ok"), ps_status=500)
        backend = _backend(server)
        info = await backend.probe_runtime_context()
        assert info["observed"] is None and info["error"]
        assert await backend.chat([{"role": "user", "content": "hi"}]) == "ok"

    @pytest.mark.asyncio
    async def test_diagnostics_missing_model_is_none_not_a_wrong_model(self):
        server = _FakeOllama(ps={"models": [{"name": "other:7b", "context_length": 4096}]})
        backend = _backend(server)
        info = await backend.probe_runtime_context()
        assert info["observed"] is None


# ============================================================
# 6. 生命周期 + 连通性探活（不得触发大额分配）
# ============================================================

class TestLifecycleAndHealth:
    @pytest.mark.asyncio
    async def test_health_check_requests_no_runtime_allocation(self):
        """探活是"远端此刻可用吗"，**不是**一次预算过的 generation：
        带上 `num_ctx` 会让 `/api/providers/test` 触发大额显存分配。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server, context_window=32768)
        result = await backend.test()
        assert result["ok"] is True
        assert "num_ctx" not in server.last_body.get("options", {})
        assert server.last_body["options"]["num_predict"] == 1

    @pytest.mark.asyncio
    async def test_health_check_reports_failure_instead_of_raising(self):
        server = _FakeOllama(status=500)
        backend = _backend(server)
        result = await backend.test()
        assert result["ok"] is False and result["error"]

    @pytest.mark.asyncio
    async def test_raw_client_is_a_lazy_openai_compat_sidecar(self):
        """`raw_client` 的消费方是 WorkflowEngine（OpenAI SDK 形状）——形状必须保持。"""
        server = _FakeOllama(payload=_full_response("ok"))
        backend = _backend(server)
        await backend.chat([{"role": "user", "content": "hi"}])
        assert backend._compat_client is None, "正常 generation 不得创建侧车"  # noqa: SLF001
        client = backend.raw_client
        assert client is not None
        assert hasattr(client.chat.completions, "create")
        assert str(client.base_url) == "http://localhost:11434/v1/"
        await backend.close()
        assert backend._compat_client is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_close_and_reconnect_are_idempotent(self):
        """`http_client_factory` 形态（生产里出站守卫就用它）：reconnect 后是**全新** client。

        静态注入的 `http_client` 与 `OpenAICompatBackend` 同语义——只在构造时使用，
        reconnect 不重建（那是单测/一次性场景的注入点，工厂才是生产形态）。
        """
        server = _FakeOllama(payload=_full_response("ok"))
        built: list[httpx.AsyncClient] = []

        def factory() -> httpx.AsyncClient:
            client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler),
                                       timeout=_TIMEOUT)
            built.append(client)
            return client

        backend = OllamaBackend(model="m", base_url=_BASE, context_window=8192,
                                http_client_factory=factory)
        assert len(built) == 1
        await backend.reconnect()
        assert len(built) == 2, "reconnect 必须从工厂重建全新 client（不复用已关闭的）"
        assert await backend.chat([{"role": "user", "content": "hi"}]) == "ok"
        await backend.close()
        await backend.close()  # 幂等
