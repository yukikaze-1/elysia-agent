"""并行安全守卫的单元测试（marker=unit）。

覆盖 `core/tests/_pytest_parallel.py` 的三个可测构件：

- `xdist_parallelism`：并行度判据（worker / controller / `-n auto` / 串行）；
- `uses_shared_test_milvus`：判据必须与 `_guard_real_database` 的换桩决策同源；
- `fail_fast_on_parallel`：违规即 fail-fast（`pytest.fail` + `session.shouldstop`），
  且**串行时是 no-op**——这一条是"别把正常路径弄红"的反向判据。

端到端（`pytest tests/real_mongo -n 2` 真的被拒答、且不碰任何库）由
`core/tests/test_parallel_guard_e2e.py` 以子进程验证：守卫的失败形态是
"整个 pytest 会话被拒"，无法在自身会话里自测。
"""

from __future__ import annotations

import pytest

from core.tests._pytest_parallel import (
    PARALLEL_AT_LEAST,
    fail_fast_on_parallel,
    parallel_violation_for_item,
    uses_shared_test_milvus,
    xdist_parallelism,
)

pytestmark = pytest.mark.unit


class _FakeConfig:
    """`xdist_parallelism` 只读三个标准信号，用最小假对象即可。"""

    def __init__(self, *, dist="no", numprocesses=None, workercount=None):
        self._options = {"dist": dist, "numprocesses": numprocesses}
        if workercount is not None:
            self.workerinput = {"workerid": "gw0", "workercount": workercount}

    def getoption(self, name, default=None):
        return self._options.get(name, default)


class TestXdistParallelism:
    """并行度判据：只用 xdist 的标准信号。"""

    def test_no_xdist_is_serial(self):
        assert xdist_parallelism(_FakeConfig()) == 1

    def test_dist_no_is_serial_even_with_numprocesses(self):
        """`--dist no` → 不分布（`-n` 与 `--dist` 同时给时的边界）。"""
        assert xdist_parallelism(_FakeConfig(dist="no", numprocesses=8)) == 1

    def test_n_zero_is_serial(self):
        """`-n 0` 是 xdist 的"关闭分布"语义——不起子进程。"""
        assert xdist_parallelism(_FakeConfig(dist="load", numprocesses=0)) == 1

    def test_n_one_is_serial(self):
        """`-n 1`：有子进程，但用例仍逐条执行 → 对"共用资源"没有风险。"""
        assert xdist_parallelism(_FakeConfig(dist="load", numprocesses=1)) == 1

    def test_n_two_is_parallel(self):
        assert xdist_parallelism(_FakeConfig(dist="load", numprocesses=2)) == 2

    def test_n_auto_is_at_least_two(self):
        """`-n auto` / `-n logical` 解析不出确切数，但必然是并行的。"""
        assert xdist_parallelism(_FakeConfig(dist="load", numprocesses="auto")) \
            == PARALLEL_AT_LEAST

    def test_worker_uses_injected_workercount(self):
        """worker 进程以 xdist 注入的 `workercount` 为准（controller 侧选项不可见）。"""
        assert xdist_parallelism(_FakeConfig(workercount=8)) == 8

    def test_worker_workercount_beats_controller_options(self):
        cfg = _FakeConfig(dist="no", numprocesses=None, workercount=4)
        assert xdist_parallelism(cfg) == 4

    def test_missing_options_are_serial(self):
        """xdist 未安装时 `getoption` 抛 ValueError → 必须判为串行而不是炸掉。"""

        class _NoOptions:
            def getoption(self, name, default=None):
                raise ValueError(name)

        assert xdist_parallelism(_NoOptions()) == 1


class TestUsesSharedTestMilvus:
    """判据与 `_guard_real_database` 的换桩决策同源：没换桩 = 会碰真实集合。"""

    def test_unit_layer_is_isolated(self):
        """unit/service：防线把 Milvus 客户端换成"操作即抛"的桩。"""
        assert uses_shared_test_milvus(["unit"], []) is False
        assert uses_shared_test_milvus(["service"], []) is False

    def test_slow_layer_reaches_real_collection(self):
        """slow：防线不换桩 → 会读写共享集合（如 TestDebugDbApi 的 drop）。"""
        assert uses_shared_test_milvus(["slow"], []) is True

    def test_external_and_integration_also_count(self):
        assert uses_shared_test_milvus(["external"], []) is True
        assert uses_shared_test_milvus(["integration"], []) is True

    def test_fake_storage_is_isolated(self):
        """`fake_storage` 自己换桩 Milvus → 并行安全（本仓 18 条 slow 属此类）。"""
        assert uses_shared_test_milvus(["slow"], ["fake_storage"]) is False

    def test_marker_and_fixture_names_accept_any_iterable(self):
        assert uses_shared_test_milvus(iter(["slow"]), iter(["tmp_path"])) is True


class _Mark:
    """最小 marker 替身（`iter_markers` 只取 `.name`）。"""

    def __init__(self, name):
        self.name = name


class _FakeItem:
    """`fail_fast_on_parallel` / `parallel_violation_for_item` 需要的最小 Item 形状。"""

    def __init__(self, *, config, markers=(), fixtures=(), nodeid="test_x.py::t"):
        self.config = config
        self._marker_names = list(markers)
        self.fixturenames = list(fixtures)
        self.nodeid = nodeid
        self.session = type("S", (), {"shouldstop": False})()

    def iter_markers(self):
        return iter([_Mark(name) for name in self._marker_names])

    def get_closest_marker(self, name):
        return _Mark(name) if name in self._marker_names else None


def _item(*, parallel: bool, markers=(), fixtures=(), nodeid="test_x.py::t"):
    cfg = _FakeConfig(dist="load", numprocesses=2) if parallel else _FakeConfig()
    return _FakeItem(config=cfg, markers=markers, fixtures=fixtures, nodeid=nodeid)


class TestParallelViolationForItem:
    def test_serial_never_violates(self):
        assert parallel_violation_for_item(_item(parallel=False, markers=["slow"])) is None

    def test_parallel_real_mongo_violates(self):
        reason = parallel_violation_for_item(
            _item(parallel=True, markers=["real_mongo", "external"]))
        assert reason is not None
        assert "must run serially" in reason

    def test_parallel_slow_milvus_violates(self):
        reason = parallel_violation_for_item(_item(parallel=True, markers=["slow"]))
        assert reason is not None
        assert "Milvus" in reason

    def test_parallel_slow_with_fake_storage_is_fine(self):
        """反证：`fake_storage` 的 slow 用例不该被误伤（否则 test-full 里 18 条被禁）。"""
        assert parallel_violation_for_item(
            _item(parallel=True, markers=["slow"], fixtures=["fake_storage"])) is None

    def test_parallel_unit_is_fine(self):
        """`make test-quick`（`-n 8` + unit/service）必须零误伤——这是本守卫的底线。"""
        assert parallel_violation_for_item(_item(parallel=True, markers=["unit"])) is None
        assert parallel_violation_for_item(_item(parallel=True, markers=["service"])) is None

    def test_real_mongo_message_names_the_layer_first(self):
        """real_mongo 用例同时"像"Milvus 使用者，但报的必须是更具体的那条原因。"""
        reason = parallel_violation_for_item(
            _item(parallel=True, markers=["real_mongo", "external"]))
        assert "real_mongo" in reason
        assert "Milvus" not in reason


class TestFailFast:
    """违规 → `pytest.fail` + `shouldstop`（一条即停，不跑完几百条）。"""

    def test_serial_is_noop(self):
        item = _item(parallel=False, markers=["slow"])
        fail_fast_on_parallel(item)          # 不抛
        assert item.session.shouldstop is False

    def test_isolation_is_noop_under_parallel(self):
        item = _item(parallel=True, markers=["unit"])
        fail_fast_on_parallel(item)
        assert item.session.shouldstop is False

    def test_violation_fails_and_stops(self):
        item = _item(parallel=True, markers=["slow"])
        with pytest.raises(pytest.fail.Exception) as excinfo:
            fail_fast_on_parallel(item)
        assert "Milvus" in str(excinfo.value)
        assert item.session.shouldstop is True, "首条即停——不然每条违规用例各报一遍"
