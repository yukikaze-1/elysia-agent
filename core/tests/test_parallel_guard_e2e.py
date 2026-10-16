"""并行安全守卫的端到端验证（子进程，marker=slow）。

## 为什么必须是子进程

守卫的失败形态是"**整个 pytest 会话被拒答**"（`pytest.fail` + `shouldstop`），
而"运行被拒"这件事无法在自身会话里自测——就像"assert 会失败"不能在同一个 assert
里证明。同理，`make test-quick -n 8` 必须**不被拒答**这条反向判据，也只能在一个
真正的 `-n` 会话里成立。

## 三件事各证明什么

| 用例 | 证明 |
|------|------|
| `test_parallel_real_mongo_run_is_refused` | `pytest tests/real_mongo -n 2` → 非零退出 + 明确原因（且**没有** INTERNALERROR 噪声） |
| `test_parallel_milvus_slow_run_is_refused` | `-m slow -n 2` 选中真实 Milvus 用例 → 同样拒答 |
| `test_normal_parallel_runs_are_not_refused` | **反向判据**：串行收集、以及"并行但 `-m` 不选这两层"都不得被误伤——本批第一版实现正是在这里把 `make test-quick -n 8` 弄红的 |

## 安全边界

子进程一律把 `ELYSIA_TEST_MONGODB_URI` 指向**不可达端口**（27099）：万一守卫回归、
用例真的跑起来，第一个 fixture 就会 ping 失败，而不是去连（更不会去 drop）任何库。
`RUN_REAL_MONGO_TESTS=1` 是必需的——没有它，根 conftest 会给 `external` 用例加
`skipif`，而 skip 判定（`tryfirst`）先于本守卫，于是 `-n 2` 会静默全 skip，
用例变成"跳过即通过"的假绿。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_ROOT = Path(__file__).resolve().parents[2]
#: 刻意指向不可达端口——见模块 docstring「安全边界」。
_UNREACHABLE_MONGO = "mongodb://127.0.0.1:27099/elysia_guard_test"
_TIMEOUT_S = 300


def _run_pytest(*args: str, opt_in: bool = False) -> subprocess.CompletedProcess:
    """在仓库根跑一次子进程 pytest，捕获输出。"""
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    if opt_in:
        env["RUN_REAL_MONGO_TESTS"] = "1"
        env["ELYSIA_TEST_MONGODB_URI"] = _UNREACHABLE_MONGO
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=_TIMEOUT_S,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


class TestParallelRunRefused:
    """会共用真实资源的两层：并行必须被明确拒答。"""

    def test_parallel_real_mongo_run_is_refused(self):
        proc = _run_pytest("tests/real_mongo", "-n", "2", "-q", "--tb=line", opt_in=True)
        out = _output(proc)
        assert proc.returncode != 0, f"并行跑 real_mongo 必须失败：\n{out}"
        assert "must run serially" in out, f"缺少明确原因：\n{out}"
        assert "INTERNALERROR" not in out, (
            "拒答不能退化成 xdist 的 INTERNALERROR（那会把原因换成一个无关用例的 "
            f"nodeid）：\n{out}")

    def test_parallel_milvus_slow_run_is_refused(self):
        proc = _run_pytest(
            "-m", "slow", "-n", "2", "-q", "--tb=line",
            "core/tests/test_phase3.py::TestDebugDbApi",
            "core/tests/test_phase2.py::TestMemoryIntegration",
        )
        out = _output(proc)
        assert proc.returncode != 0, f"并行跑真实 Milvus 用例必须失败：\n{out}"
        assert "long_term_memory_v2__test" in out, f"缺少明确原因：\n{out}"
        assert "INTERNALERROR" not in out, f"拒答不应触发 INTERNALERROR：\n{out}"


class TestNoFalsePositive:
    """反向判据：正常路径不得被误伤（本批第一版就是在这里翻车的）。"""

    def test_serial_real_mongo_collection_is_not_refused(self):
        """串行（`make test-real-mongo` 的形态）：拒答逻辑必须完全不介入。"""
        proc = _run_pytest("tests/real_mongo", "--collect-only", "-q")
        out = _output(proc)
        assert proc.returncode == 0, f"串行收集不该失败：\n{out}"
        assert "must run serially" not in out, out

    def test_parallel_run_excluding_both_layers_is_not_refused(self):
        """`-m "unit or service" -n 2`：**收集得到**这两层但一条都不选 → 不得拒答。

        这正是 `make test-quick -n 8` 的形态。写在这里是因为第一版守卫把判据挂在
        "conftest 被加载"上，于是这条命令直接报
        `ERROR tests/real_mongo - pytest.UsageError`——整个日常档位变红。
        """
        proc = _run_pytest("-m", "unit or service", "-n", "2", "-q", "tests/")
        out = _output(proc)
        assert proc.returncode == 0, f"unit/service 档位被误伤：\n{out}"
        assert "must run serially" not in out, out
        assert "long_term_memory_v2__test" not in out, out

    def test_parallel_slow_tests_with_fake_storage_are_not_refused(self):
        """反证"判据是精确的"：`test_server.py` 的 slow 用例都带 `fake_storage`
        （Milvus 被换成内存桩）→ `-n 2` 下**必须照常跑**。

        没有这条，守卫可能退化成"slow 层一律串行"——那会白白禁掉 39 条 slow 里的
        18 条（`make test-full` 里本可以并行的那部分）。
        """
        proc = _run_pytest(
            "-m", "slow", "-n", "2", "-q", "core/input/tests/test_server.py")
        out = _output(proc)
        assert proc.returncode == 0, f"fake_storage 的 slow 用例被误伤：\n{out}"
        assert "long_term_memory_v2__test" not in out, out
        assert "passed" in out, out
