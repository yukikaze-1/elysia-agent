"""vectordb 测试 conftest — review 复核 #8：干净 CI 环境无 .env（P0-02 信任边界），
而 VectorDBFactory.create 在缺 MILVUS_URI 时抛 ValueError——此前 PR Gate 在
GitHub-hosted 干净环境 31 errors + 1 failed 全部源于此。

本 autouse fixture 提供测试默认 URI（已有真实 `MILVUS_URI` 时保留）。测试中
MilvusClient 被 mock / 懒连接，URI 仅用于通过工厂构造后端，不产生真实连接。

## 为什么是 function scope（2026-09-16 修正：Batch 1.5 全局状态审计）

原实现是 `scope="session"` + **裸 `os.environ.setdefault`**——一条**永不还原**的进程级
环境写入，且写在**目录级** conftest 里。后果有两层，都实测过（无 .env 的 worktree）：

1. **越界泄漏**：同一进程里**该目录之后**的测试都能看到 `MILVUS_URI`，哪怕本 conftest
   对它们根本不生效。
2. **结果依赖调度**：`--dist loadfile -n 8` 下哪个 worker 拿到
   `Module/database/vectordb/tests/` 是不确定的，于是「后一个测试看不看得到这个变量」
   也不确定。实测：

   | 命令（同一 commit、无 .env） | 结果 |
   |---|---|
   | `pytest core/tests/test_clean_db.py` | **2 failed**, 39 passed |
   | `pytest Module/database/vectordb/tests core/tests/test_clean_db.py` | 95 passed |
   | `pytest core/tests/test_clean_db.py Module/database/vectordb/tests` | **2 failed**, 93 passed |

   `core/tests/test_clean_db.py::TestMilvusCleaner` 的两条用例**只**因这个变量而绿。

**为什么不是「session scope + teardown 还原」**：那只是把窗口缩到 session 结束，
同一次运行内**先后顺序**造成的差异原样保留（上表 B→A 仍能借到变量）。要消除的是
「跨目录借用」，只有把变量的生命周期收在本目录的每个用例内才做得到——
故改 function scope + `monkeypatch`（pytest 自带，自动还原；缺失时还原为**不存在**）。

代价：若将来在本目录新增 `scope="module"/"session"` 的 fixture 并调用
`VectorDBFactory.create`，它拿不到这个默认值——届时请显式 `monkeypatch` 或在
该 fixture 内自备 URI。
"""
import os

import pytest

_DEFAULT_MILVUS_URI = "http://localhost:19530"


@pytest.fixture(autouse=True)
def _default_milvus_uri(monkeypatch) -> None:
    """本目录用例可见的 MILVUS_URI 兜底；用例结束即还原（无则删除）。"""
    if "MILVUS_URI" not in os.environ:
        monkeypatch.setenv("MILVUS_URI", _DEFAULT_MILVUS_URI)
