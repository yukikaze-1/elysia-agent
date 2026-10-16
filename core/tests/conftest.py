"""core/tests 共享 fixtures。"""

import pytest
import pytest_asyncio


# `_no_network_embedding` 已上提到根 conftest 的 `_no_real_embedding`
# （2026-09-14 CI 性能专项）：目录作用域只覆盖 490/4985 条，
# 漏掉 core/agent、core/input、core/cognitive 等全部 teardown 热点。


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


@pytest_asyncio.fixture
async def fresh_ltm_collection():
    """把**测试用** LTM 集合清空（drop + recreate）——**先清后跑**，不做收尾清理。

    ## 为什么需要（2026-09-28 实测，不是推断）

    `TestMemoryIntegration` 的两条用例都往真实 Milvus 的
    ``long_term_memory_v2__test`` 写条目且从不删除，于是集合随**每次** targeted run
    单调增长：实测 ``pytest core/tests/test_phase2.py::TestMemoryIntegration``
    跑 1 次 → 2 行、连跑 4 次 → 8 行（直接查集合，不信 ``get_collection_stats``
    ——那个 ``row_count`` 在 flush 前恒为 0，会把残留读成"干净的"）。后果有二：

    ① `test_save_memory_tool` 用 ``top_k=20`` 召回，而嵌入被根 conftest 的
       ``_no_real_embedding`` 换成了常量 ``[0.1]*1024``（所有条目向量相同 ⇒ 排序
       无意义、返回哪 20 条是任意的）：历史条目越多，本次新写的那条越可能被挤出
       top-20 → 反复跑本文件会**越来越 flaky**；
    ② 更隐蔽的一条：``any("测试保存" in r.text …)`` 与 ``"香山"`` 断言都能被**上一轮
       的残留**满足 ⇒ 真 insert 断了也照绿（假绿）。

    ## 为什么清在**用例之前**，而不是 session 收尾

    - 被 kill 的 run（CI 取消、超时）不留"清理没跑"的窗口——本 run 开始时已经清过，
      集合内容只属于本 run；
    - 不引入 session 级 finalizer 的时序问题：Milvus 不可达时收尾清理会把 teardown
      变成随机失败，而"先清"失败只是本用例的正常 setup error（它本来就需要真库）。

    ## fail-closed：只允许 drop 测试集合

    清理前断言 ``MemoryRepositoryV2.COLLECTION`` 以 ``__test`` 结尾（根 autouse 防线
    ``install_test_milvus_collection`` 的产物）。**断言在前、drop 在后**——防线若失效，
    这里直接红，绝不 drop 生产集合。

    调用方：`core/tests/test_phase2.py::TestMemoryIntegration`（``slow`` 层，共用真实
    Milvus 的用例，已由 `core/tests/_pytest_parallel.py` 保证串行）。
    """
    from core.storage.memory_repo_v2 import MemoryRepositoryV2

    collection = MemoryRepositoryV2.COLLECTION
    assert collection.endswith("__test"), (
        f"拒绝清理：COLLECTION={collection!r} 不是测试集合（autouse 防线未生效？）——"
        "drop 生产长期记忆不可逆"
    )

    repo = MemoryRepositoryV2()
    connected = await repo.connect()
    assert connected, "Milvus 不可达——本 fixture 的调用方都需要真实向量库"

    await repo.clear_all(confirm=repo.CONFIRM_TOKEN)
    assert repo.COLLECTION == collection, "clear_all 不得改动集合名（重定向必须保持）"
