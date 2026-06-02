"""共享 pytest fixtures — 所有 Module/ 子模块测试自动继承。"""
import pytest
from unittest.mock import MagicMock, patch


# ============================================================
# LLM
# ============================================================

@pytest.fixture
def llm_backend():
    from Module.llm.registry import LLMFactory
    return LLMFactory.create("deepseek/deepseek-v4-pro", api_key="sk-test")


# ============================================================
# STT
# ============================================================

@pytest.fixture
def stt_backend():
    from Module.stt.registry import STTFactory
    return STTFactory.create("sensevoice/sense-voice")


# ============================================================
# TTS
# ============================================================

_FAKE_VOICES_YAML = """
Elysia:
  ref_audio: "/fake/ref.wav"
  prompt_text: "测试文本"
"""


@pytest.fixture
def tts_backend():
    with patch("builtins.open", MagicMock(side_effect=FileNotFoundError)), \
         patch("pathlib.Path.read_text", return_value=_FAKE_VOICES_YAML):
        from Module.tts.gpt_sovits import GPTSoVitsBackend
        yield GPTSoVitsBackend(base_url="http://fake:9880")


# ============================================================
# Embedding
# ============================================================

@pytest.fixture(autouse=True)
def _clear_embedding_cache():
    """每个测试前清空 EmbeddingFactory 缓存。"""
    from Module.embedding.registry import EmbeddingFactory
    EmbeddingFactory._instances.clear()
    yield


@pytest.fixture
def huggingface_backend():
    with patch("Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()), \
         patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False):
        from Module.embedding.registry import EmbeddingFactory
        yield EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")


@pytest.fixture
def openai_embedding_backend():
    with patch("Module.embedding.registry.os.getenv", return_value="sk-test"):
        from Module.embedding.registry import EmbeddingFactory
        return EmbeddingFactory.create("openai/text-embedding-3-large")


# ============================================================
# VectorDB
# ============================================================

@pytest.fixture
def vectordb_backend():
    with patch("Module.database.vectordb.milvus.MilvusClient", MagicMock()):
        from Module.database.vectordb.registry import VectorDBFactory
        yield VectorDBFactory.create("milvus/test")


# ============================================================
# DocumentDB
# ============================================================

@pytest.fixture
def documentdb_backend():
    with patch("motor.motor_asyncio.AsyncIOMotorClient", MagicMock()):
        from Module.database.documentdb.registry import DocumentDBFactory
        b = DocumentDBFactory.create("mongodb/elysia")
        b._client = MagicMock()
        b._db = {}
        return b
