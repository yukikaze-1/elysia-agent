"""`RUN_EXTERNAL_TESTS` opt-in 门禁的**范围**守卫（2026-09-17 Batch 2.3）。

## 被固化的缺陷

这条门禁此前**只**写在 `Module/conftest.py` 里。pytest 的 conftest 按目录层级
生效，所以 `core/` 下的用例**根本走不到它**——`core/` 里标了 `external` 的用例
在**没有** `RUN_EXTERNAL_TESTS=1` 时照常执行。

实测：`core/tests/test_phase1.py::TestStreamingIntegration` 两个用例在未设该变量时
**2 passed**（真实 DeepSeek 调用、计费），而 `pytest.ini` 把 `external` 定义为
「离线门禁排除、独立 opt-in」；`Module/` 侧同类用例则正确 skip。
**同一条纪律只装在了一半的目录上**——判据存在 ≠ 判据在范围里。

## 本文件测什么

用**子进程**真跑一次 pytest 收集+执行，断言：
① 未设 `RUN_EXTERNAL_TESTS` 时有 `external` 用例被 skip（而不是 pass/run）；
② 该 skip 覆盖 `core/`（不只是 `Module/`）——这正是缺陷所在。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent

# 一个确定带 external marker 的 core/ 用例（真实 LLM，见其 docstring）
_CORE_EXTERNAL_NODE = (
    "core/tests/test_phase2.py::TestLongTermMemory::test_insert_and_search_async")


def _run(nodeid: str, *, opt_in: bool) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "RUN_EXTERNAL_TESTS"}
    if opt_in:
        env["RUN_EXTERNAL_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "no:cacheprovider",
         "--no-header", "-rs"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300, env=env,
    )


def test_core_external_test_is_skipped_without_optin():
    """**核心断言**：未 opt-in 时 `core/` 的 external 用例必须 skip。

    修复前这里是 **1 passed**（真跑、真调用、真计费）——本用例若变红，
    说明 `core/` 又脱离了 opt-in 门禁的范围。
    """
    proc = _run(_CORE_EXTERNAL_NODE, opt_in=False)
    out = proc.stdout + proc.stderr
    assert "skipped" in out, (
        f"core/ 的 external 用例在未设 RUN_EXTERNAL_TESTS=1 时没有被 skip。\n"
        f"输出：\n{out[-1500:]}"
    )
    assert "1 passed" not in out, (
        f"core/ 的 external 用例**真的跑了**（真实外部调用、计费）：\n{out[-1500:]}"
    )


def test_optin_still_runs_them():
    """反向对照：显式 opt-in 后不该被 skip（别修成"永远 skip"）。

    只收集不执行（`--collect-only`）——我们验的是**门禁不拦**，
    不需要为此真的付一次外部调用。
    """
    env = {**os.environ, "RUN_EXTERNAL_TESTS": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _CORE_EXTERNAL_NODE, "--collect-only",
         "-q", "-p", "no:cacheprovider"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300, env=env,
    )
    out = proc.stdout + proc.stderr
    assert "1 test collected" in out or "tests collected" in out, out[-1200:]
