"""D1/D4：`core.schema` 导入隔离不变量 + 冷启动耗时报告数据（Issue D）。

**不变量**：`core.schema` 是轻量数据模型层，不 import 任何 provider SDK。
即 `import core.schema` ⇒ `"openai" not in sys.modules`（httpx 随 openai 一并消失）。

为什么必须开**新子进程**：同进程断言会被同一次 pytest 会话里其它测试的 import 污染
（`core/tests/` 大量用例会 import 到 LLM 适配层 → openai），断言将恒为「已污染」而
永远红，或被 `importorskip` 糊成静默绿。子进程探针是唯一能证明「这条链本身干净」
的方式——它检查的是**导入图**，不是当前进程的残留状态。

背景链路（修复前，见 Issue D 报告）：
  core/schema/__init__.py:17 → core/schema/types.py:7
  → Module/workflow_contract.schema（触发父包 __init__）
  → Module/workflow_contract/__init__.py:32 → infra.py:17 `from openai import AsyncOpenAI`

D4 只**记录**耗时（print + 本模块 docstring 的实测值），不对墙钟阈值断言——那种断言
在负载波动的 CI 上必假红。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # 子进程确定性门禁，秒级（同 scripts/tests/test_check_architecture.py 惯例）

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "conftest.py").is_file() and (p / "core").is_dir() and (p / "Module").is_dir()
)
"""仓库根——显式 cwd，使 `python -c` 的 sys.path[0] 含 `core` / `Module`。

**不能猜层级**（`parents[2]` 在本文件是 `core/`）：探针若因路径错而失败，
报出来的是 `ModuleNotFoundError` 而非隔离破坏——假红掩盖真判据。"""

_PROBE_TIMEOUT_S = 120
"""单次子进程探针上限（冷 import 实测 ≈1.4s，120s 只防挂死）。"""

_PROBE = r"""
import json, sys, time

t0 = time.perf_counter()
import core.schema  # noqa: F401
elapsed_ms = (time.perf_counter() - t0) * 1000.0

# provider SDK 及其运行时传递依赖：出现任一根即说明隔离被破坏
_forbidden = ("openai", "httpx", "httpcore", "anthropic", "google.genai")
_roots = sorted({m.split(".")[0] for m in sys.modules} & set(_forbidden))

print(json.dumps({
    "elapsed_ms": elapsed_ms,
    "forbidden_roots": _roots,
    "n_modules": len(sys.modules),
}))
"""


def _run_probe(source: str) -> dict:
    """在全新解释器里跑探针，返回其 JSON 报告。

    非零退出即失败——不 skip、不放行：探针自身崩掉时「没看到 openai」不构成证据。
    """
    proc = subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )
    assert proc.returncode == 0, (
        f"子进程探针失败（exit {proc.returncode}）\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_d1_import_core_schema_pulls_no_provider_sdk():
    """D1：新解释器里 `import core.schema` 后，provider SDK 不得出现在 sys.modules。"""
    report = _run_probe(_PROBE)
    assert report["forbidden_roots"] == [], (
        "`import core.schema` 拖入了 provider SDK（不变量：core.schema 是轻量数据模型层，"
        f"不 import 任何 provider SDK）：{report['forbidden_roots']}"
        f"（共 {report['n_modules']} 个模块）"
    )


def test_d4_import_timing_is_report_data():
    """D4：记录冷启动耗时——**报告数据，不做墙钟断言**（`pytest -s` 可见打印）。

    修复前实测（本机，3 次取最优，含 ≈16ms 解释器启动）：
      bare interpreter   ≈ 16 ms
      import pydantic    ≈ 69 ms
      import openai      ≈ 993 ms
      import core.schema ≈ 1313 ms（≈ openai 全额税，即本 Issue 修复对象）
    """
    report = _run_probe(_PROBE)
    print(
        f"\n[D4] cold `import core.schema` = {report['elapsed_ms']:.0f} ms"
        f"（{report['n_modules']} modules loaded）"
    )
    # 只断言「测到了」——耗时不设阈值（CI 负载波动会让阈值断言假红）。
    assert report["elapsed_ms"] > 0.0
    assert report["n_modules"] > 0
