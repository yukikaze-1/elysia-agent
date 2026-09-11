from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from typing import cast

from Module.embedding.huggingface import HuggingFaceBackend
from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
from Module.embedding.protocol import EmbeddingBackend
from Module.embedding.registry import EmbeddingFactory


# ============================================================
# 工具 —— HuggingFace 后端创建（打桩供应链校验收口）
# ============================================================

@contextmanager
def _hf_backend_patch():
    """创建 HuggingFace 后端所需的 patch 组合。

    _verified_local_path 是供应链校验收口点：打桩绕过真实 hash 校验，
    使测试不依赖本机模型缓存（真实校验逻辑在 scripts/tests/ 单独覆盖）。
    """
    with (
        patch("Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()),
        patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False),
        patch("Module.embedding.huggingface._verified_local_path", return_value="/fake/model"),
    ):
        yield


# ============================================================
# 协议 —— ABC 抽象约束
# ============================================================

@pytest.mark.service
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
# EmbeddingFactory（嵌入工厂）
# ============================================================

@pytest.mark.service
class TestEmbeddingFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid model_id"):
            EmbeddingFactory.create("huggingface")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            EmbeddingFactory.create("unknown/model")

    def test_huggingface_model_parsing(self):
        with _hf_backend_patch():
            backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
        backend = cast(HuggingFaceBackend, backend)
        assert backend._model_name == "BAAI/bge-large-zh-v1.5"
        assert backend.dimension == 1024

    def test_huggingface_unknown_model_dimension(self):
        with _hf_backend_patch():
            backend = EmbeddingFactory.create(
                "huggingface/unknown-model", dimension=512
            )
        assert backend.dimension == 512

    def test_openai_model_parsing(self):
        with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
            backend = EmbeddingFactory.create("openai/text-embedding-3-large")
        backend = cast(OpenAICompatEmbeddingBackend, backend)
        assert backend._model == "text-embedding-3-large"
        assert backend.dimension == 0  # not resolved until first API call


# ============================================================
# HuggingFaceBackend（HuggingFace 后端）
# ============================================================

@pytest.fixture
def hf_backend(huggingface_backend):
    return huggingface_backend


@pytest.mark.service
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
        with _hf_backend_patch():
            backend = EmbeddingFactory.create("huggingface/unknown-model")
        with pytest.raises(RuntimeError, match="Unknown dimension"):
            _ = backend.dimension

    @pytest.mark.asyncio
    async def test_close_noop(self, hf_backend):
        await hf_backend.close()  # should not raise


# ============================================================
# OpenAICompatEmbeddingBackend（OpenAI 兼容嵌入后端）
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
def openai_backend(openai_embedding_backend):
    return openai_embedding_backend


@pytest.mark.service
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
        # 验证它以单元素列表调用 embed_documents
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


# ============================================================
# OpenAICompatEmbeddingBackend —— 显式 dimensions（P2-1 维度对齐）
# ============================================================

@pytest.mark.service
class TestOpenAICompatDimensions:
    """构造时显式指定 dimensions，使向量维度与向量库集合固定维度对齐。

    背景：Milvus 集合固定 1024 维，而 text-embedding-3-small 原生 1536 维——
    不显式限定则写入被维度校验拒绝。
    """

    def test_dimension_known_before_first_call(self):
        """显式声明维度后，首次 API 调用前 dimension 即为真值（而非 0）。"""
        from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
        with patch("Module.embedding.openai_compat.os.getenv", return_value="sk-test"):
            backend = OpenAICompatEmbeddingBackend(
                model="text-embedding-3-small", dimensions=1024,
            )
        assert backend.dimension == 1024

    def test_dimension_zero_without_explicit(self):
        """未显式声明 → 首次调用前仍为 0（保持既有语义）。"""
        from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
        with patch("Module.embedding.openai_compat.os.getenv", return_value="sk-test"):
            backend = OpenAICompatEmbeddingBackend(model="text-embedding-3-small")
        assert backend.dimension == 0

    @pytest.mark.asyncio
    async def test_explicit_dimensions_sent_to_api(self):
        """构造时声明的 dimensions 透传给 embeddings.create。"""
        from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
        with patch("Module.embedding.openai_compat.os.getenv", return_value="sk-test"):
            backend = OpenAICompatEmbeddingBackend(
                model="text-embedding-3-small", dimensions=1024,
            )
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1] * 1024])
        )
        with patch.object(backend._client.embeddings, "create", mock_create):
            await backend.embed_query("hello")
        assert mock_create.call_args.kwargs["dimensions"] == 1024

    @pytest.mark.asyncio
    async def test_caller_dimensions_wins_over_constructor(self):
        """调用方显式传 dimensions 时优先于构造默认值。"""
        from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
        with patch("Module.embedding.openai_compat.os.getenv", return_value="sk-test"):
            backend = OpenAICompatEmbeddingBackend(
                model="text-embedding-3-small", dimensions=1024,
            )
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1] * 512])
        )
        with patch.object(backend._client.embeddings, "create", mock_create):
            await backend.embed_documents(["hello"], dimensions=512)
        assert mock_create.call_args.kwargs["dimensions"] == 512

    @pytest.mark.asyncio
    async def test_no_dimensions_kwarg_when_unset(self):
        """未声明维度 → 不向 API 传 dimensions（避免不支持该参数的兼容端点报错）。"""
        from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend
        with patch("Module.embedding.openai_compat.os.getenv", return_value="sk-test"):
            backend = OpenAICompatEmbeddingBackend(model="text-embedding-3-small")
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.1, 0.2]])
        )
        with patch.object(backend._client.embeddings, "create", mock_create):
            await backend.embed_documents(["hello"])
        assert "dimensions" not in mock_create.call_args.kwargs


# ============================================================
# EmbeddingFactory —— shutdown（关闭）
# ============================================================

@pytest.mark.service
class TestEmbeddingFactoryShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_closes_all_instances(self):
        """shutdown() 关闭所有缓存的实例。"""
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            b2 = EmbeddingFactory.create("huggingface/bge-large-zh-v1.5")

        mock_close1 = AsyncMock()
        mock_close2 = AsyncMock()
        b1.close = mock_close1
        b2.close = mock_close2

        await EmbeddingFactory.shutdown()
        mock_close1.assert_called_once()
        mock_close2.assert_called_once()
        assert len(EmbeddingFactory._instances) == 0

    @pytest.mark.asyncio
    async def test_shutdown_handles_close_exception(self):
        """某个实例 close() 异常不影响其他实例关闭和清理。"""
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            b2 = EmbeddingFactory.create("huggingface/bge-large-zh-v1.5")

        b1.close = AsyncMock(side_effect=RuntimeError("close failed"))
        mock_close2 = AsyncMock()
        b2.close = mock_close2

        await EmbeddingFactory.shutdown()
        mock_close2.assert_called_once()
        assert len(EmbeddingFactory._instances) == 0

    @pytest.mark.asyncio
    async def test_create_after_shutdown_returns_new_instance(self):
        """shutdown 后 create() 返回新实例而非旧缓存。"""
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            await EmbeddingFactory.shutdown()
            b2 = EmbeddingFactory.create("huggingface/bge-m3")
        assert b1 is not b2

    @pytest.mark.asyncio
    async def test_shutdown_on_empty_is_noop(self):
        """空实例池 shutdown 不崩溃。"""
        EmbeddingFactory._instances.clear()
        await EmbeddingFactory.shutdown()


# ============================================================
# EmbeddingFactory —— 单例缓存
# ============================================================

@pytest.mark.service
class TestEmbeddingFactorySingleton:
    def test_same_model_returns_same_instance(self):
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            b2 = EmbeddingFactory.create("huggingface/bge-m3")
        assert b1 is b2

    def test_different_model_returns_different_instance(self):
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            b2 = EmbeddingFactory.create("huggingface/bge-large-zh-v1.5")
        assert b1 is not b2

    def test_instances_dict_tracks_correctly(self):
        with _hf_backend_patch():
            b1 = EmbeddingFactory.create("huggingface/bge-m3")
            b2 = EmbeddingFactory.create("huggingface/bge-large-zh-v1.5")
        assert EmbeddingFactory._instances["huggingface/bge-m3|{}"] is b1
        assert EmbeddingFactory._instances["huggingface/bge-large-zh-v1.5|{}"] is b2


# ============================================================
# HuggingFaceBackend —— from_config（配置加载）
# ============================================================

@pytest.mark.service
class TestHuggingFaceFromConfig:
    def test_from_config_creates_backend(self):
        from Module.embedding.config import EmbeddingConfig

        cfg = EmbeddingConfig(api_key="")
        with _hf_backend_patch():
            backend = EmbeddingFactory.from_config("huggingface/BAAI/bge-large-zh-v1.5", cfg)
        assert backend.dimension == 1024


# ============================================================
# API 错误 / 空输入
# ============================================================

@pytest.mark.service
class TestEmbeddingErrors:
    @pytest.mark.asyncio
    async def test_openai_api_error_propagates(self, openai_backend):
        from openai import APIStatusError
        mock_create = AsyncMock(side_effect=APIStatusError(
            "rate limit", response=MagicMock(status_code=429), body=None
        ))
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            with pytest.raises(APIStatusError):
                await openai_backend.embed_query("hello")

    @pytest.mark.asyncio
    async def test_embed_documents_empty_list(self, openai_backend):
        """空列表输入行为。"""
        mock_create = AsyncMock(return_value=_make_embedding_response([]))
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            result = await openai_backend.embed_documents([])
        assert result == []

    @pytest.mark.asyncio
    async def test_embed_query_empty_string(self, openai_backend):
        """空字符串查询行为。"""
        mock_create = AsyncMock(
            return_value=_make_embedding_response([[0.0] * 3])
        )
        with patch.object(openai_backend._client.embeddings, "create", mock_create):
            result = await openai_backend.embed_query("")
        assert isinstance(result, list)
        assert len(result) > 0
