"""共享 pytest fixtures — 所有 Module/ 子模块测试自动继承。"""
import os
import pytest
from unittest.mock import MagicMock, patch


# ============================================================
# LLM（大语言模型）
# ============================================================

@pytest.fixture
def llm_backend():
    from Module.llm.registry import LLMFactory
    return LLMFactory.create("deepseek/deepseek-v4-pro", api_key="sk-test")


# ============================================================
# STT（语音识别）
# ============================================================

@pytest.fixture
def stt_backend():
    from Module.stt.registry import STTFactory
    return STTFactory.create("sensevoice/sense-voice")


# ============================================================
# TTS（语音合成）
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
# Embedding（嵌入）
# ============================================================

@pytest.fixture(autouse=True)
def _clear_embedding_cache():
    """每个测试前清空 EmbeddingFactory 缓存。"""
    from Module.embedding.registry import EmbeddingFactory
    EmbeddingFactory._instances.clear()
    yield


@pytest.fixture
def huggingface_backend():
    # _verified_local_path 是供应链校验收口点：测试中打桩绕过真实 hash 校验
    # （真实校验逻辑单独在 scripts/tests/test_download_model_manifest.py 覆盖）
    with patch("Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()), \
         patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False), \
         patch("Module.embedding.huggingface._verified_local_path", return_value="/fake/model"):
        from Module.embedding.registry import EmbeddingFactory
        yield EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")


@pytest.fixture
def openai_embedding_backend():
    with patch("Module._common.registry_helpers.os.getenv", return_value="sk-test"):
        from Module.embedding.registry import EmbeddingFactory
        return EmbeddingFactory.create("openai/text-embedding-3-large")


# ============================================================
# VectorDB（向量数据库）
# ============================================================

@pytest.fixture
def vectordb_backend():
    with patch("Module.database.vectordb.milvus.MilvusClient", MagicMock()):
        from Module.database.vectordb.registry import VectorDBFactory
        yield VectorDBFactory.create("milvus/test")


# ============================================================
# DocumentDB（文档数据库）
# ============================================================

@pytest.fixture
def documentdb_backend(monkeypatch):
    """DocumentDB 内存替身——**绝不构造真 `AsyncIOMotorClient`**。

    **patch 目标必须打在模块属性上**（S3-R6-2）：`mongodb.py:6` 是
    `from motor.motor_asyncio import AsyncIOMotorClient`——名字在**导入期**就绑进了
    `Module.database.documentdb.mongodb` 的命名空间，patch 源模块
    （`motor.motor_asyncio.AsyncIOMotorClient`）对已存在的绑定无效。
    原实现正是打错了目标：`test_documentdb.py` 里 25 个用例请求本 fixture，
    于是**每个用例都真的构造了一个 `AsyncIOMotorClient`**（motor 构造是惰性的，
    不产生 I/O，随后唯一引用又被 `b._client = MagicMock()` 顶掉，所以不是硬泄漏
    ——但守卫的意图完全落空，而且 `close()` 用例断言的是 fixture 塞进去的 mock）。
    下面两道自检把"patch 是否真的生效"变成 fixture 自己会报错的事。

    **同时把 `MONGODB_URI` 显式钉住**（同一次重核发现的第二个问题）：
    `DocumentDBFactory.create` 在连接串缺失时抛 ValueError，而此前没有任何测试基建
    保证它存在——本文件**单跑必错**（实测改动前 `1 failed / 11 passed / 25 errors`），
    在全量里能过只是因为**同一 worker 里恰好有别的测试先加载了 `.env`**——靠顺序偶然，
    不是靠事实。这里显式设一个**指向非生产端口**的测试串（client 已被替身、不会有 I/O；
    万一替身失效，也不会真的连到 27017 的生产库）。
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:27099/elysia_unit_test")
    with patch("Module.database.documentdb.mongodb.AsyncIOMotorClient") as client_cls:
        from Module.database.documentdb.registry import DocumentDBFactory
        b = DocumentDBFactory.create("mongodb/elysia")
        client_cls.assert_called_once()      # 构造确实经过了替身（打错目标即红）
        assert isinstance(b._client, MagicMock), (  # type: ignore[attr-defined]
            "patch 目标失效：真的 AsyncIOMotorClient 被构造了（S3-R6-2）")
        b._db = {}  # type: ignore[attr-defined]
        return b


# ============================================================
# External 测试门禁（F1/E4）
# ============================================================

def pytest_collection_modifyitems(config, items):
    """外部测试门禁：未显式 RUN_EXTERNAL_TESTS=1 → external 测试整体 skip。
    用 item.add_marker(skipif)——裸 pytest.skip() 在 collection 阶段会中断收集。

    S2-R6-1 例外：`real_mongo` 层自己的开关是 `RUN_REAL_MONGO_TESTS`（真实 MongoDB
    走容器，不需要任何**凭据**——把它挂到 `RUN_EXTERNAL_TESTS` 下会让"我要跑真库
    测试"这句命令同时要求一个它与无关的开关，而漏设的表现是**整体 skip**：
    看起来跑过了、其实一条没跑）。故带 `real_mongo` 的条目由该层自己的守卫把关
    （连不上即 fail，见 `tests/real_mongo/conftest.py`）。"""
    external_ok = os.getenv("RUN_EXTERNAL_TESTS") == "1"
    real_mongo_ok = os.getenv("RUN_REAL_MONGO_TESTS") == "1"
    skip = pytest.mark.skipif(True, reason="需要 RUN_EXTERNAL_TESTS=1 显式 opt-in（真实 LLM/DB E2E）")
    for item in items:
        if item.get_closest_marker("external") is None:   # 精确判 marker（keyword 是超集，防误 skip）
            continue
        if item.get_closest_marker("real_mongo") is not None and real_mongo_ok:
            continue      # 本层自带 opt-in 与"连不上即 fail"的守卫
        if not external_ok:
            item.add_marker(skip)
