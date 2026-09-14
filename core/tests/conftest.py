"""core/tests 共享 fixtures。"""

import pytest


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
