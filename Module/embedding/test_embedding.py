from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from typing import cast

from .huggingface import HuggingFaceBackend
from .openai_compat import OpenAICompatEmbeddingBackend
from .protocol import EmbeddingBackend
from .registry import EmbeddingFactory


@pytest.fixture(autouse=True)
def _clear_factory_cache():
    """每个测试前清空 Factory 缓存，确保测试独立性。"""
    EmbeddingFactory._instances.clear()
    yield


# ============================================================
# Protocol — ABC constraints
# ============================================================

class TestEmbeddingBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            EmbeddingBackend()  # type: ignore[abstract]

    def test_missing_embed_documents_raises(self):
        with pytest.raises(TypeError):

            class BadBackend1(EmbeddingBackend):
                @property
                def dimension(self) -> int:
                    return 128

                async def embed_query(self, text, **kwargs) -> list[float]:
                    return []

            BadBackend1()  # type: ignore[abstract]

    def test_missing_embed_query_raises(self):
        with pytest.raises(TypeError):

            class BadBackend2(EmbeddingBackend):
                @property
                def dimension(self) -> int:
                    return 128

                async def embed_documents(self, texts, **kwargs) -> list[list[float]]:
                    return []

            BadBackend2()  # type: ignore[abstract]

    def test_missing_dimension_raises(self):
        with pytest.raises(TypeError):

            class BadBackend3(EmbeddingBackend):
                async def embed_documents(self, texts, **kwargs) -> list[list[float]]:
                    return []

                async def embed_query(self, text, **kwargs) -> list[float]:
                    return []

            BadBackend3()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class GoodBackend(EmbeddingBackend):
            @property
            def dimension(self) -> int:
                return 128

            async def embed_documents(self, texts, **kwargs) -> list[list[float]]:
                return [[0.1] * 128] * len(texts)

            async def embed_query(self, text, **kwargs) -> list[float]:
                return [0.1] * 128

            async def close(self) -> None:
                pass

        backend = GoodBackend()
        assert backend.dimension == 128


# ============================================================
# EmbeddingFactory
# ============================================================

class TestEmbeddingFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid model_id"):
            EmbeddingFactory.create("huggingface")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            EmbeddingFactory.create("unknown/model")

    def test_huggingface_model_parsing(self):
        with patch(
            "Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()
        ), patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False):
            backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
        backend = cast(HuggingFaceBackend, backend)
        assert backend._model_name == "BAAI/bge-large-zh-v1.5"
        assert backend.dimension == 1024

    def test_huggingface_unknown_model_dimension(self):
        with patch(
            "Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()
        ), patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False):
            backend = EmbeddingFactory.create(
                "huggingface/unknown-model", dimension=512
            )
        assert backend.dimension == 512

    def test_openai_model_parsing(self):
        with patch("Module.embedding.registry.os.getenv", return_value="sk-test"):
            backend = EmbeddingFactory.create("openai/text-embedding-3-large")
        backend = cast(OpenAICompatEmbeddingBackend, backend)
        assert backend._model == "text-embedding-3-large"
        assert backend.dimension == 0  # not resolved until first API call


# ============================================================
# HuggingFaceBackend
# ============================================================

@pytest.fixture
def hf_backend():
    with patch(
        "Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()
    ), patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False):
        yield EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")


class TestHuggingFaceBackend:
    @pytest.mark.asyncio
    async def test_embed_documents(self, hf_backend):
        hf_backend._embeddings.embed_documents.return_value = [
            [0.1, 0.2], [0.3, 0.4]
        ]
        result = await hf_backend.embed_documents(["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]
        hf_backend._embeddings.embed_documents.assert_called_once_with(
            ["hello", "world"]
        )

    @pytest.mark.asyncio
    async def test_embed_query(self, hf_backend):
        hf_backend._embeddings.embed_query.return_value = [0.5, 0.6]
        result = await hf_backend.embed_query("hello")
        assert result == [0.5, 0.6]
        hf_backend._embeddings.embed_query.assert_called_once_with("hello")

    def test_dimension_known_model(self, hf_backend):
        assert hf_backend.dimension == 1024

    def test_dimension_unknown_raises(self):
        with patch(
            "Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()
        ), patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False):
            backend = EmbeddingFactory.create("huggingface/unknown-model")
        with pytest.raises(RuntimeError, match="Unknown dimension"):
            _ = backend.dimension

    @pytest.mark.asyncio
    async def test_close_noop(self, hf_backend):
        await hf_backend.close()  # should not raise


# ============================================================
# OpenAICompatEmbeddingBackend
# ============================================================

def _make_embedding_response(embeddings: list[list[float]], model: str = "text-embedding-3-large"):
    data = []
    for i, emb in enumerate(embeddings):
        d = MagicMock()
        d.embedding = emb
        d.index = i
        data.append(d)
    resp = MagicMock()
    resp.data = data
    resp.model = model
    return resp


@pytest.fixture
def openai_backend():
    with patch("Module.embedding.registry.os.getenv", return_value="sk-test"):
        return EmbeddingFactory.create("openai/text-embedding-3-large")


class TestOpenAICompatEmbedding:
    @pytest.mark.asyncio
    async def test_embed_documents(self, openai_backend):
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1, 0.2], [0.3, 0.4]])
        )
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            result = await openai_backend.embed_documents(["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    @pytest.mark.asyncio
    async def test_embed_query(self, openai_backend):
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.5, 0.6, 0.7]])
        )
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            result = await openai_backend.embed_query("hello")
        assert result == [0.5, 0.6, 0.7]
        # Verify it calls embed_documents with single-element list
        call_input = mock_create.call_args.kwargs["input"]
        assert call_input == ["hello"]

    @pytest.mark.asyncio
    async def test_dimension_resolved_after_first_call(self, openai_backend):
        assert openai_backend.dimension == 0
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1] * 3072])
        )
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            await openai_backend.embed_query("hello")
        assert openai_backend.dimension == 3072

    @pytest.mark.asyncio
    async def test_passes_model_and_kwargs(self, openai_backend):
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1, 0.2]])
        )
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            await openai_backend.embed_documents(["hello"], dimensions=512)
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "text-embedding-3-large"
        assert call_kwargs["dimensions"] == 512

    @pytest.mark.asyncio
    async def test_close(self, openai_backend):
        mock_close = AsyncMock()
        with patch.object(openai_backend._client, "close", mock_close):
            await openai_backend.close()
        mock_close.assert_called_once()
