import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from typing import cast

from .openai_compat import OpenAICompatBackend
from .protocol import LLMBackend
from .registry import LLMFactory


# ============================================================
# Protocol — ABC constraints
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

            async def close(self) -> None:
                pass

        backend = GoodBackend()
        assert isinstance(backend, LLMBackend)


# ============================================================
# _extract_prefix
# ============================================================

class TestExtractPrefix:
    def test_strips_prefix_from_messages(self):
        from .openai_compat import _extract_prefix

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
        from .openai_compat import _extract_prefix

        messages = [{"role": "user", "content": "hi"}]
        cleaned, has_prefix = _extract_prefix(messages)
        assert has_prefix is False
        assert cleaned == messages

    def test_empty_messages(self):
        from .openai_compat import _extract_prefix

        cleaned, has_prefix = _extract_prefix([])
        assert has_prefix is False
        assert cleaned == []


# ============================================================
# LLMFactory
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
        with patch("Module.llm.registry.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("deepseek/deepseek-v4-pro")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "deepseek-v4-pro"
        assert backend._supports_prefix is True

    def test_ollama_no_env_key(self):
        backend = cast(OpenAICompatBackend, LLMFactory.create("ollama/qwen3:latest"))
        assert backend._model == "qwen3:latest"
        assert backend._supports_prefix is False

    def test_custom_base_url(self):
        with patch("Module.llm.registry.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create(
                "openai/gpt-5", base_url="https://custom.openai.com/v1"
            )
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "gpt-5"
        assert str(backend._client.base_url) == "https://custom.openai.com/v1/"


# ============================================================
# OpenAICompatBackend — chat / chat_stream
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
def backend():
    return LLMFactory.create("deepseek/deepseek-v4-pro", api_key="sk-test")


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
    async def test_chat_collects_stream_chunks(self, backend):
        """chat() uses non-streaming API call (stream=False)."""
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
# prefix continuation
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
        # prefix field removed from messages sent to API
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
# close
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
        with patch.object(backend._client, "close", mock_close), \
             patch.object(backend._beta_client, "close", mock_beta_close):
            await backend.close()
        mock_close.assert_called_once()
        mock_beta_close.assert_called_once()


# ============================================================
# stream_think
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
# API error handling
# ============================================================

class TestAPIErrors:
    @pytest.mark.asyncio
    async def test_chat_content_none_returns_empty(self, backend):
        """API 返回 content=None 时 chat() 返回空字符串。"""
        mock_create = AsyncMock(return_value=_make_nonstream_response(None))
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
# prefix continuation edges
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
# Factory edge cases
# ============================================================

class TestFactoryEdges:
    def test_model_id_with_multiple_slashes(self):
        """model_id "provider/model/variant" 解析为 provider="provider"。"""
        with patch("Module.llm.registry.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("openai/gpt-5/variant")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._model == "gpt-5/variant"

    def test_factory_injects_logger(self):
        """LLMFactory.create() 注入 caller-aware logger。"""
        with patch("Module.llm.registry.os.getenv", return_value="sk-test"):
            backend = LLMFactory.create("deepseek/deepseek-v4-pro")
        backend = cast(OpenAICompatBackend, backend)
        assert backend._logger is not None
        assert backend._logger.name.endswith("test_llm")
