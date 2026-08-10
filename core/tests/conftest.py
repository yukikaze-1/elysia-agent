"""core/tests 共享 fixtures。"""

import pytest


@pytest.fixture(autouse=True)
def _no_network_embedding(monkeypatch):
    """测试统一用内存 fake embedding，不加载真实 HuggingFace 模型。

    CI 干净检出环境无模型缓存且联网失败/挂起（BAAI/bge-large-zh-v1.5 需下载），
    真实 embedding 会让依赖 insert 的测试失败（synced=0 / embedding 空）或挂起，
    导致 CI quick job 超时。
    """
    from core.memory.embedding_provider import EmbeddingProvider

    class _FakeEmbedding:
        dimension = 1024

        async def embed_query(self, text: str) -> list[float]:
            return [0.1] * 1024

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(EmbeddingProvider, "_try_load_sync", lambda self: _FakeEmbedding())


@pytest.fixture(autouse=True)
def _isolate_pending_syncs(tmp_path, monkeypatch):
    """review 复核 #6：pending-syncs journal 隔离到 tmp 目录。

    LTM 离线 insert（repo=None / 返回空 PK）每次都会写 journal——若不隔离会污染
    真实 data/ 目录并跨测试串扰（同一进程内下一测试构造 LongTermMemory 时
    _load_pending_syncs 读到上一测试的脏条目，count 虚高）。
    """
    from core.memory.long_term import LongTermMemory

    monkeypatch.setattr(
        LongTermMemory, "_pending_syncs_file",
        lambda self: str(tmp_path / "memory_pending_syncs.json"),
    )
