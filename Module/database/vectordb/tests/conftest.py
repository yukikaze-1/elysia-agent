"""vectordb 测试 conftest — review 复核 #8：干净 CI 环境无 .env（P0-02 信任边界），
而 VectorDBFactory.create 在缺 MILVUS_URI 时抛 ValueError——此前 PR Gate 在
GitHub-hosted 干净环境 31 errors + 1 failed 全部源于此。

本 session 级 autouse fixture 提供测试默认 URI（`setdefault`——真实 env 已有
MILVUS_URI 时保留）。测试中 MilvusClient 被 mock / 懒连接，URI 仅用于通过工厂
构造后端，不产生真实连接。
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _default_milvus_uri() -> None:
    os.environ.setdefault("MILVUS_URI", "http://localhost:19530")
