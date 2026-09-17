import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from typing import cast

from Module.llm.openai_compat import OpenAICompatBackend
from Module.llm.protocol import LLMBackend
from Module.llm.registry import LLMFactory

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
def _set_llm_urls(monkeypatch):
    """设置 LLM URL 环境变量（所有 LLM 测试共用）。"""
    monkeypatch.setenv("DEEPSEEK_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_BETA_URL", "https://api.deepseek.com/beta")


# ============================================================
# 协议 —— ABC 抽象约束
# ============================================================

class TestLLMBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            LLMBackend()  # type: ignore[abstract]

    def test_missing_chat_raises(self):
        with pytest.raises(TypeError):

            class BadBackend1(LLMBackend):
                async def chat_stream(self, messages, **kwargs):
                    yield ""

            BadBackend1()  # type: ignore[abstract]

    def test_missing_chat_stream_raises(self):
        with pytest.raises(TypeError):

            class BadBackend2(LLMBackend):
                async def chat(self, messages, **kwargs) -> str:
                    return ""

            BadBackend2()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class GoodBackend(LLMBackend):
            async def chat(self, messages, **kwargs) -> str:
                return "ok"

            async def chat_stream(self, messages, **kwargs):
                yield "ok"

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

        backend = GoodBackend()
        assert isinstance(backend, LLMBackend)


# ============================================================
# _extract_prefix
# ============================================================

class TestExtractPrefix:
    def test_strips_prefix_from_messages(self):
        from Module.llm.openai_compat import _extract_prefix

        messages = [
            {"role": "system", "content": "hello"},
            {"role": "assistant", "content": "{\n", "prefix": True},
        ]
        cleaned, has_prefix = _extract_prefix(messages)
        assert has_prefix is True
        assert "prefix" not in cleaned[1]
        assert cleaned[1]["content"] == "{\n"
        assert cleaned[0] == {"role": "system", "content": "hello"}

    def test_no_prefix(self):
        from Module.llm.openai_compat import _extract_prefix

        messages = [{"role": "user", "content": "hi"}]
        cleaned, has_prefix = _extract_prefix(messages)
        assert has_prefix is False
        assert cleaned == messages

    def test_empty_messages(self):
        from Module.llm.openai_compat import _extract_prefix

        cleaned, has_prefix = _extract_prefix([])
        assert has_prefix is False
        assert cleaned == []


# ============================================================
# LLMFactory（LLM 工厂）
# ============================================================

class TestLLMFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid model_id"):
            LLMFactory.create("deepseek-v4-pro")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            LLMFactory.create("unknown/model")

    def test_gemini_placeholder_raises(self):
        with pytest.raises(NotImplementedError):
            LLMFactory.create("gemini/gemini-2.5-pro")

    def test_deepseek_model_parsing(self):
        with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("deepseek/deepseek-v4-pro")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "deepseek-v4-pro"
        assert backend._supports_prefix is True

    def test_ollama_no_env_key(self):
        backend = cast(OpenAICompatBackend, LLMFactory.create("ollama/qwen3:latest"))
        assert backend._model == "qwen3:latest"
        assert backend._supports_prefix is False

    def test_custom_base_url(self):
        with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create(
                "openai/gpt-5", base_url="https://custom.openai.com/v1"
            )
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "gpt-5"
        assert str(backend._client.base_url) == "https://custom.openai.com/v1/"


# ============================================================
# OpenAICompatBackend —— chat / chat_stream（对话/流式）
# ============================================================

def _make_nonstream_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message = MagicMock()
    resp.choices[0].message.content = content
    return resp


def _make_stream_chunks(*chunks: str):
    async def _gen():
        for c in chunks:
            ch = MagicMock()
            ch.choices = [MagicMock()]
            ch.choices[0].delta = MagicMock()
            ch.choices[0].delta.content = c
            yield ch

    return _gen()


@pytest.fixture
def backend(llm_backend):
    return llm_backend


class TestOpenAICompatChat:
    @pytest.mark.asyncio
    async def test_chat_returns_content(self, backend):
        mock_create = AsyncMock(
            return_value=_make_nonstream_response("hello world")
        )
        with patch.object(backend._client.chat.completions, "create", mock_create):
            result = await backend.chat([{"role": "user", "content": "hi"}])
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_chat_empty_choices_raises(self, backend):
        """非流式空 choices → 抛异常让上层决策，而非静默返回空串（O2）。

        原实现 error 后 return——调用方收到空回复产生「空白回复」，
        无法感知并重试；显式抛错才能让 CognitiveLoop 走空响应守卫。
        """
        resp = MagicMock()
        resp.choices = []
        mock_create = AsyncMock(return_value=resp)
        with patch.object(backend._client.chat.completions, "create", mock_create):
            with pytest.raises(RuntimeError):
                await backend.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_collects_stream_chunks(self, backend):
        """chat() 使用非流式 API 调用（stream=False）。"""
        mock_create = AsyncMock(
            return_value=_make_nonstream_response("collected")
        )
        with patch.object(backend._client.chat.completions, "create", mock_create):
            result = await backend.chat([{"role": "user", "content": "hi"}])
        assert result == "collected"
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["stream"] is False

    @pytest.mark.asyncio
    async def test_chat_passes_kwargs(self, backend):
        mock_create = AsyncMock(
            return_value=_make_nonstream_response("ok")
        )
        with patch.object(backend._client.chat.completions, "create", mock_create):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                temperature=1.5,
                max_tokens=200,
            )
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["temperature"] == 1.5
        assert call_kwargs["max_tokens"] == 200

    @pytest.mark.asyncio
    async def test_chat_stream_yields_chunks(self, backend):
        mock_create = AsyncMock(
            return_value=_make_stream_chunks("a", "b", "c")
        )
        with patch.object(backend._client.chat.completions, "create", mock_create):
            chunks = []
            async for c in backend.chat_stream([{"role": "user", "content": "hi"}]):
                chunks.append(c)
        assert chunks == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_chat_stream_passes_stream_true(self, backend):
        mock_create = AsyncMock(
            return_value=_make_stream_chunks("x")
        )
        with patch.object(backend._client.chat.completions, "create", mock_create):
            async for _ in backend.chat_stream(
                [{"role": "user", "content": "hi"}],
                temperature=0.5,
            ):
                pass
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["stream"] is True
        assert call_kwargs["temperature"] == 0.5


# ============================================================
# prefix 续写
# ============================================================

class TestPrefixContinuation:
    @pytest.mark.asyncio
    async def test_deepseek_prefix_injects_extra_body(self, backend):
        mock_create = AsyncMock(
            return_value=_make_nonstream_response('"public_reply": "hi"}')
        )
        with patch.object(backend._beta_client.chat.completions, "create", mock_create):
            result = await backend.chat([
                {"role": "system", "content": "be helpful"},
                {"role": "assistant", "content": "{\n", "prefix": True},
            ])
        assert '"public_reply"' in result
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["extra_body"] == {"prefix": True}
        # 发给 API 的消息中已移除 prefix 字段
        sent_messages = call_kwargs["messages"]
        assert "prefix" not in sent_messages[1]

    @pytest.mark.asyncio
    async def test_ollama_prefix_raises(self):
        backend = LLMFactory.create("ollama/qwen3")
        with pytest.raises(ValueError, match="prefix"):
            await backend.chat([
                {"role": "assistant", "content": "{\n", "prefix": True},
            ])


# ============================================================
# close（关闭）
# ============================================================

class TestClose:
    @pytest.mark.asyncio
    async def test_close_closes_client(self, backend):
        mock_close = AsyncMock()
        with patch.object(backend._client, "close", mock_close):
            await backend.close()
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_closes_beta_client(self):
        backend = LLMFactory.create("deepseek/deepseek-v4-pro", api_key="sk-test")
        mock_close = AsyncMock()
        mock_beta_close = AsyncMock()
        with (  # type: ignore[attr-defined]
            patch.object(backend._client, "close", mock_close),
            patch.object(backend._beta_client, "close", mock_beta_close),  # type: ignore[attr-defined]
        ):
            await backend.close()
        mock_close.assert_called_once()
        mock_beta_close.assert_called_once()


# ============================================================
# stream_think（思维流）
# ============================================================

class TestStreamThink:
    @pytest.mark.asyncio
    async def test_protocol_default_falls_back_to_chat(self):
        """LLMBackend.stream_think() 默认实现逐字符产出纯文本。"""
        class DefaultBackend(LLMBackend):
            async def chat(self, messages, **kwargs) -> str:
                return "ab"
            async def chat_stream(self, messages, **kwargs):
                yield ""
            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}
            async def close(self) -> None:
                pass

        backend = DefaultBackend()
        chunks = []
        async for c in backend.stream_think([{"role": "user", "content": "hi"}]):
            chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0] == {"content": "a", "tool_calls": None}
        assert chunks[1] == {"content": "b", "tool_calls": None}

    @pytest.mark.asyncio
    async def test_openai_stream_think_yields_structured_chunks(self, backend):
        """OpenAICompatBackend.stream_think() 调用 _stream_structured。"""
        async def _gen():
            for content in ("a", "b"):
                ch = MagicMock()
                ch.choices = [MagicMock()]
                ch.choices[0].delta = MagicMock()
                ch.choices[0].delta.content = content
                ch.choices[0].delta.tool_calls = None
                ch.usage = None
                yield ch

        mock_create = AsyncMock(return_value=_gen())
        with patch.object(backend._client.chat.completions, "create", mock_create):
            chunks = []
            async for c in backend.stream_think([{"role": "user", "content": "hi"}]):
                chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0]["content"] == "a"
        assert chunks[0]["tool_calls"] is None

    @pytest.mark.asyncio
    async def test_stream_structured_with_tool_calls(self, backend):
        """_stream_structured 检测 tool_calls 并构建结构化 dict。"""
        async def _gen():
            ch = MagicMock()
            ch.choices = [MagicMock()]
            ch.choices[0].delta = MagicMock()
            ch.choices[0].delta.content = None
            tc = MagicMock()
            tc.index = 0
            tc.id = "call_1"
            tc.type = "function"
            tc.function = MagicMock()
            tc.function.name = "search"
            tc.function.arguments = '{"q":"test"}'
            ch.choices[0].delta.tool_calls = [tc]
            ch.usage = None
            yield ch

        mock_create = AsyncMock(return_value=_gen())
        with patch.object(backend._client.chat.completions, "create", mock_create):
            chunks = []
            async for c in backend.stream_think([{"role": "user", "content": "hi"}]):
                chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0]["content"] is None
        assert len(chunks[0]["tool_calls"]) == 1
        assert chunks[0]["tool_calls"][0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_stream_think_api_error_propagates(self, backend):
        """API 错误从 _stream_structured 向上传播。"""
        from openai import APIStatusError
        mock_create = AsyncMock(side_effect=APIStatusError(
            "error", response=MagicMock(), body=None
        ))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            with pytest.raises(APIStatusError):
                async for _ in backend.stream_think([{"role": "user", "content": "hi"}]):
                    pass


# ============================================================
# API 错误处理
# ============================================================

class TestAPIErrors:
    @pytest.mark.asyncio
    async def test_chat_content_none_returns_empty(self, backend):
        """API 返回 content=None 时 chat() 返回空字符串。"""
        mock_create = AsyncMock(return_value=_make_nonstream_response(""))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            result = await backend.chat([{"role": "user", "content": "hi"}])
        assert result == ""

    @pytest.mark.asyncio
    async def test_chat_stream_skips_empty_delta(self, backend):
        """delta.content=None/空时跳过该 chunk。"""
        async def _gen():
            for content in ("a", None, "", "b"):
                ch = MagicMock()
                ch.choices = [MagicMock()]
                ch.choices[0].delta = MagicMock()
                ch.choices[0].delta.content = content
                yield ch

        mock_create = AsyncMock(return_value=_gen())
        with patch.object(backend._client.chat.completions, "create", mock_create):
            chunks = [c async for c in backend.chat_stream([{"role": "user", "content": "hi"}])]
        assert chunks == ["a", "b"]

    @pytest.mark.asyncio
    async def test_chat_http_error_propagates(self, backend):
        """HTTP 错误从 API 向上传播。"""
        from openai import APIStatusError
        mock_create = AsyncMock(side_effect=APIStatusError(
            "bad request", response=MagicMock(status_code=400), body=None
        ))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            with pytest.raises(APIStatusError):
                await backend.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_timeout_propagates(self, backend):
        """网络超时异常向上传播。"""
        import httpx
        mock_create = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            with pytest.raises(httpx.ReadTimeout):
                await backend.chat([{"role": "user", "content": "hi"}])


# ============================================================
# prefix 续写边界情况
# ============================================================

class TestPrefixContinuationEdges:
    @pytest.mark.asyncio
    async def test_prefix_with_chat_stream(self, backend):
        """chat_stream 中 prefix 消息通过 _beta_client + extra_body 传递。"""
        async def _gen():
            ch = MagicMock()
            ch.choices = [MagicMock()]
            ch.choices[0].delta = MagicMock()
            ch.choices[0].delta.content = "streamed"
            yield ch

        mock_create = AsyncMock(return_value=_gen())
        with patch.object(backend._beta_client.chat.completions, "create", mock_create):
            chunks = []
            async for c in backend.chat_stream([
                {"role": "assistant", "content": "{\n", "prefix": True},
            ]):
                chunks.append(c)
        assert "streamed" in chunks
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["extra_body"] == {"prefix": True}

    @pytest.mark.asyncio
    async def test_prefix_merges_caller_extra_body(self, backend):
        """调用者 extra_body 与 prefix 合并，调用者优先。"""
        mock_create = AsyncMock(return_value=_make_nonstream_response("ok"))
        with patch.object(backend._beta_client.chat.completions, "create", mock_create):
            await backend.chat(
                [{"role": "assistant", "content": "{\n", "prefix": True}],
                extra_body={"custom": "val"},
            )
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["extra_body"]["prefix"] is True
        assert call_kwargs["extra_body"]["custom"] == "val"

    @pytest.mark.asyncio
    async def test_unsupported_prefix_in_chat_stream_raises(self):
        """非 prefix 支持 provider 的 chat_stream 中 prefix 抛异常。"""
        backend = LLMFactory.create("ollama/qwen3")
        with pytest.raises(ValueError, match="prefix"):
            async for _ in backend.chat_stream([
                {"role": "assistant", "content": "{\n", "prefix": True},
            ]):
                pass


# ============================================================
# 工厂边界情况
# ============================================================

class TestFactoryEdges:
    def test_model_id_with_multiple_slashes(self):
        """model_id "provider/model/variant" 解析为 provider="provider"。"""
        with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("openai/gpt-5/variant")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "gpt-5/variant"

    def test_factory_injects_logger(self):
        """LLMFactory.create() 注入 caller-aware logger。"""
        with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("deepseek/deepseek-v4-pro")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._logger is not None
        assert backend._logger.name == "Module.llm.registry"


# ============================================================
# Token 用量统计
# ============================================================

class TestUsageTracking:
    """测试 API usage 捕获。"""

    @pytest.mark.asyncio
    async def test_stream_structured_yields_usage_chunk(self, llm_backend):
        """流式 streaming 中检测到 usage 时产出 usage chunk。"""
        from unittest.mock import MagicMock, AsyncMock

        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 100
        usage_obj.completion_tokens = 50
        usage_obj.total_tokens = 150
        usage_obj.prompt_tokens_details = None
        usage_obj.completion_tokens_details = None

        delta = MagicMock()
        delta.content = "hello"
        delta.tool_calls = None

        chunk_no_usage = MagicMock()
        chunk_no_usage.choices = [MagicMock(delta=delta)]
        chunk_no_usage.usage = None

        chunk_with_usage = MagicMock()
        chunk_with_usage.choices = []  # usage chunk 的 choices 为空（真实 API 行为）
        chunk_with_usage.usage = usage_obj

        async def async_iter():
            for item in [chunk_no_usage, chunk_with_usage]:
                yield item

        llm_backend._client.chat.completions.create = AsyncMock(return_value=async_iter())

        chunks = []
        async for chunk in llm_backend._stream_structured([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0] == {"content": "hello", "tool_calls": None}
        assert chunks[1] == {
            "content": None,
            "tool_calls": None,
            "usage": {
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                "cached_tokens": None, "reasoning_tokens": None,
            },
        }
        assert llm_backend.last_usage == {
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cached_tokens": None, "reasoning_tokens": None,
        }

    @pytest.mark.asyncio
    async def test_stream_structured_no_usage_when_none(self, llm_backend):
        """无 usage 时不产出额外 chunk。"""
        from unittest.mock import MagicMock, AsyncMock

        delta = MagicMock()
        delta.content = "ok"
        delta.tool_calls = None

        chunk = MagicMock()
        chunk.choices = [MagicMock(delta=delta)]
        chunk.usage = None

        async def async_iter():
            yield chunk

        llm_backend._client.chat.completions.create = AsyncMock(return_value=async_iter())

        chunks = []
        async for chunk in llm_backend._stream_structured([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert "usage" not in chunks[0]

    @pytest.mark.asyncio
    async def test_call_api_nonstreaming_captures_usage(self, llm_backend):
        """非流式路径捕获 usage。"""
        from unittest.mock import MagicMock, AsyncMock

        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 10
        usage_obj.completion_tokens = 5
        usage_obj.total_tokens = 15
        usage_obj.prompt_tokens_details = None
        usage_obj.completion_tokens_details = None

        choice = MagicMock()
        choice.message.content = "resp"

        mock_response = MagicMock()
        mock_response.choices = [choice]
        mock_response.usage = usage_obj

        llm_backend._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await llm_backend.chat([{"role": "user", "content": "hi"}])
        assert result == "resp"
        assert llm_backend.last_usage == {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "cached_tokens": None, "reasoning_tokens": None,
        }

    @pytest.mark.asyncio
    async def test_call_api_streaming_empty_choices_usage_chunk(self, llm_backend):
        """_call_api 流式路径：空 choices + usage chunk 不抛 IndexError、捕获 usage、跳过 delta。

        M6 回归：不能先访问 chunk.choices[0]（usage chunk 的 choices 恒空，会 IndexError）。
        """
        from unittest.mock import MagicMock, AsyncMock

        usage_obj = MagicMock()
        usage_obj.prompt_tokens = 100
        usage_obj.completion_tokens = 50
        usage_obj.total_tokens = 150
        usage_obj.prompt_tokens_details = None
        usage_obj.completion_tokens_details = None

        delta = MagicMock()
        delta.content = "hello"

        chunk_usage = MagicMock()
        chunk_usage.choices = []  # 空 choices —— 若先访问 choices[0] 会 IndexError（M6）
        chunk_usage.usage = usage_obj

        chunk_content = MagicMock()
        chunk_content.choices = [MagicMock(delta=delta)]
        chunk_content.usage = None

        async def async_iter():
            yield chunk_usage
            yield chunk_content

        llm_backend._client.chat.completions.create = AsyncMock(return_value=async_iter())

        chunks = []
        async for chunk in llm_backend.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert chunks == ["hello"]  # usage chunk 无内容跳过
        assert llm_backend.last_usage == {
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cached_tokens": None, "reasoning_tokens": None,
        }


# ============================================================
# http_client 注入（Task 5 批次 5 — custom 出站守卫挂载点）
# ============================================================

class TestHTTPClientInjection:
    """OpenAICompatBackend http_client 注入挂载点。

    后续 Task 6/7 会把 build_guarded_http_client 产出的守卫 client 注入进来；
    本测试锁定：注入即用、缺省自建、reconnect 复用同一注入 client。
    注：AsyncOpenAI 构造时校验 http_client 必须是真实 httpx.AsyncClient，
    因此测试用真实 AsyncClient 实例作注入对象（守卫 client 同为 AsyncClient）。
    """

    def test_injected_http_client_used_by_clients(self):
        """注入 http_client → 常规 + beta client 都用该 client。"""
        guard = httpx.AsyncClient()
        try:
            backend = OpenAICompatBackend(
                model="guard-test",
                api_key="sk-test",
                base_url="https://guard.example.com/v1",
                beta_url="https://guard.example.com/beta",
                http_client=guard,
            )
            assert backend._injected_client is guard
            assert backend._client._client is guard
            assert backend._beta_client._client is guard  # type: ignore[union-attr]
        finally:
            asyncio.run(backend.close())
            asyncio.run(guard.aclose())

    def test_no_http_client_self_builds(self):
        """不传 http_client → 后端自建（现状不破坏，不把 http_client 传给 AsyncOpenAI）。"""
        with patch("Module.llm.openai_compat.AsyncOpenAI", return_value=MagicMock()) as mock_cls:
            OpenAICompatBackend(
                model="guard-test",
                api_key="sk-test",
                base_url="https://guard.example.com/v1",
                beta_url="https://guard.example.com/beta",
            )
        assert mock_cls.call_args_list, "应至少创建常规 client"
        for call in mock_cls.call_args_list:
            assert "http_client" not in call.kwargs

    @pytest.mark.asyncio
    async def test_reconnect_factory_rebuilds_fresh_guard(self):
        """review 修正：http_client_factory 路径下 reconnect 重建**全新**守卫 client——
        AsyncOpenAI wrapper.close() 连带关闭旧守卫，若复用旧实例则 custom LLM 重启即死；
        工厂保证新守卫可用（守卫跨重启保留）。"""
        calls: list = []

        def factory():
            c = httpx.AsyncClient()
            calls.append(c)
            return c

        backend = OpenAICompatBackend(
            model="guard-test",
            api_key="sk-test",
            base_url="https://guard.example.com/v1",
            http_client_factory=factory,
        )
        try:
            first_sdk = backend._client
            first_guard = calls[0]
            assert backend._client._client is first_guard
            await backend.reconnect()
            # AsyncOpenAI 重建 + 工厂再次调用 → 全新守卫 client
            assert backend._client is not first_sdk
            assert len(calls) == 2
            assert backend._client._client is calls[1]
            assert calls[1] is not first_guard
            # 旧守卫随旧 wrapper 关闭（不复用已关闭实例），新守卫可用
            assert first_guard.is_closed
            assert not calls[1].is_closed
        finally:
            await backend.close()
            for c in calls:
                try:
                    await c.aclose()
                except Exception:
                    pass

    def test_factory_passes_http_client_to_custom_backend(self):
        """LLMFactory.create("custom/x", ..., http_client=guard) → 后端收到。

        批次 8a（T12）：custom 初始配置走 SSRF 守卫——base_url 用公网字面量 IP
        （8.8.8.8）确定性通过校验（伪造域名会解析失败被守卫拦截，不用于此测试）。
        """
        guard = httpx.AsyncClient()
        try:
            backend = LLMFactory.create(
                "custom/guard-test",
                base_url="https://8.8.8.8/v1",
                api_key="sk-test",
                http_client=guard,
            )
            backend = cast(OpenAICompatBackend, backend)
            assert backend._injected_client is guard
            assert backend._client._client is guard
        finally:
            asyncio.run(backend.close())
            asyncio.run(guard.aclose())


# ============================================================
# provider 能力门控：thinking 键（S2-H3）
# ============================================================

class TestThinkingCapabilityGate:
    """未声明 `supports_thinking` 的 provider 上丢弃 `thinking` 键。

    缺陷形状：主链与另外 ~10 个调用点硬编码 `extra_body={"thinking": ...}`，后端原样
    合并进请求体；严格 OpenAI 风格 API 对未知顶层参数 400 ⇒ 该 provider 上 100% 回合
    失败，而日志只显示 GENERATION_PROVIDER_UNAVAILABLE（误诊成本最高的一条）。

    两层都要测：**能力位读取**（registry 把 providers.yaml 的值传进后端）与
    **门控行为**（后端真的丢/不丢），只测一层都看不出缺口。
    """

    def test_registry_passes_capability_flag(self):
        """registry 从 providers.yaml 读到能力位并注入后端。"""
        assert LLMFactory.create("deepseek/deepseek-v4-pro",
                                 api_key="x")._supports_thinking is True
        assert LLMFactory.create("ollama/qwen3")._supports_thinking is False
        assert LLMFactory.create("openai/gpt-4o", api_key="x")._supports_thinking is False

    @pytest.mark.asyncio
    async def test_undeclared_provider_drops_thinking_nonstream(self):
        """未声明 → 请求体里没有 thinking（`extra_body` 整体为空 → None）。"""
        backend = LLMFactory.create("openai/gpt-4o", api_key="x")
        mock_create = AsyncMock(return_value=_make_nonstream_response("hi"))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                extra_body={"thinking": {"type": "disabled"}})

        assert mock_create.call_args.kwargs["extra_body"] is None, (
            "未声明支持的 provider 不得收到 thinking 键")

    @pytest.mark.asyncio
    async def test_undeclared_provider_drops_thinking_stream(self):
        """流式路径（`_call_api`）同样丢——两条 extra_body 合并点必须一致。"""
        backend = LLMFactory.create("ollama/qwen3")
        mock_create = AsyncMock(return_value=_make_stream_chunks("x"))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            async for _ in backend.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    extra_body={"thinking": {"type": "disabled"}}):
                pass

        assert mock_create.call_args.kwargs["extra_body"] is None

    @pytest.mark.asyncio
    async def test_declared_provider_keeps_thinking(self, backend):
        """反证（防空转）：声明支持的 provider（deepseek）照常收到该键。"""
        mock_create = AsyncMock(return_value=_make_nonstream_response("hi"))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                extra_body={"thinking": {"type": "disabled"}})

        assert mock_create.call_args.kwargs["extra_body"] == {
            "thinking": {"type": "disabled"}}

    @pytest.mark.asyncio
    async def test_gate_is_narrow_other_keys_pass_through(self):
        """门禁只丢 `thinking` 一个键——调用方的其它 extra_body 键必须原样保留。"""
        backend = LLMFactory.create("ollama/qwen3")
        mock_create = AsyncMock(return_value=_make_nonstream_response("hi"))
        with patch.object(backend._client.chat.completions, "create", mock_create):
            await backend.chat(
                [{"role": "user", "content": "hi"}],
                extra_body={"thinking": {"type": "disabled"}, "top_k": 7})

        assert mock_create.call_args.kwargs["extra_body"] == {"top_k": 7}

    @pytest.mark.asyncio
    async def test_drop_warning_once_per_backend(self, caplog):
        """丢弃只警告一次（每后端实例）——否则每轮刷一条把日志淹没，且要说清怎么改。"""
        import logging

        backend = LLMFactory.create("ollama/qwen3")
        mock_create = AsyncMock(return_value=_make_nonstream_response("hi"))
        with caplog.at_level(logging.WARNING), \
                patch.object(backend._client.chat.completions, "create", mock_create):
            for _ in range(3):
                await backend.chat([{"role": "user", "content": "hi"}],
                                   extra_body={"thinking": {"type": "disabled"}})

        hits = [r for r in caplog.records if "supports_thinking" in r.message]
        assert len(hits) == 1, f"应恰好警告一次，实际 {len(hits)} 次"
        assert "providers.yaml" in hits[0].message  # 给出可操作的修法
