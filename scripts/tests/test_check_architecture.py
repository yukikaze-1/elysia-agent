"""架构边界门禁（`scripts/check_architecture.py`）测试。

重点覆盖**两个已经踩过的坑**——本脚本初版两处都错过：

1. **假阳性**：把函数内懒导入当作 eager → 误报 2 个伪环
   （`long_term ↔ scoped_long_term`、`schema.types ↔ schema.utils`，
   两者都是刻意用函数内 import 打断的）；
2. **假阴性**：R1/R3 只统计 eager → 漏掉全部 5 处真实泄漏
   （`bench.py`/`documentdb/__main__.py` 的 core 依赖等都在函数内）。

因此测试用**合成 fixture 仓库**分别验证 eager/lazy/type 三分在
R1/R3（用运行时依赖）与 R4（仅 eager）下的行为差异。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "check_architecture.py"

pytestmark = pytest.mark.unit


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """按 {相对路径: 源码} 造一个合成仓库。"""
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return tmp_path


def _run(repo: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo)],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------- 真实仓库


def test_real_repository_passes():
    """当前仓库零硬违规（存量泄漏已在 allowlist 登记）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=str(ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"仓库存在未登记违规:\n{proc.stdout}\n{proc.stderr}"


def test_repository_has_no_real_eager_cycle():
    """R4 基线为 0——两个"环"都是函数内懒导入打断的伪环，不得报出。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"], cwd=str(ROOT),
        capture_output=True, text=True, timeout=180,
    )
    import json

    payload = json.loads(proc.stdout)
    r4 = [v for v in payload["violations"] if v["rule"] == "R4"]
    assert r4 == [], f"出现 eager 环: {r4}"


def test_allowlist_has_no_stale_entries():
    """allowlist 不得含**失效**条目——符号改名/收口后必须同步删除。

    失效条目会让门禁「看起来很严」，实际豁免了一个已不存在的违规——
    比没有豁免更危险（掩盖真实回归）。

    判定按**声明 key 是否真的被命中**，而非比较条目数：一条
    `_MODULE_TO_CORE_ALLOWLIST` 条目可按文件豁免多处（bench.py 同时依赖
    core.config 与 core.agent），条目数与命中数本就不等。
    """
    from scripts.check_architecture import (
        _MODULE_TO_CORE_ALLOWLIST,
        _PRIVATE_SYMBOL_ALLOWLIST,
        check_repository,
    )

    accepted = [v for v in check_repository(ROOT) if v.accepted]
    locations = "\n".join(v.location for v in accepted)

    stale_module = [path for path in _MODULE_TO_CORE_ALLOWLIST if path not in locations]
    stale_symbol = [
        f"{mod}.{sym}"
        for (mod, sym) in _PRIVATE_SYMBOL_ALLOWLIST
        if f"{mod}.{sym}" not in locations
    ]

    assert not stale_module, (
        f"以下 Module→core 豁免条目已失效（违规已不存在，请删除）：{stale_module}"
    )
    assert not stale_symbol, (
        f"以下私有符号豁免条目已失效（符号已公开或改名，请删除）：{stale_symbol}"
    )


# ---------------------------------------------------------------- R1 依赖方向


def test_r1_flags_module_to_core_even_when_lazy(tmp_path):
    """R1 必须抓函数内懒导入——包级依赖不因延迟执行而消失（初版假阴性）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/agent.py": "class Agent: ...\n",
        "Module/__init__.py": "",
        "Module/bench.py": "def run():\n    from core.agent import Agent\n    return Agent\n",
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R1]" in out and "Module/bench.py" in out


def test_r1_allows_allowlisted_module_to_core(tmp_path):
    """allowlist 内的 Module→core 不导致失败。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/agent.py": "class Agent: ...\n",
        "Module/__init__.py": "",
        "Module/bench/bench.py": "def run():\n    from core.agent import Agent\n",
    })
    code, out = _run(repo)
    assert code == 0, out


def test_zero_dependency_layer_reverse_import_fails(tmp_path):
    """frontend_contract 不得反向依赖 Module/core（CLAUDE.md 硬约定，此前无门禁）。"""
    repo = _make_repo(tmp_path, {
        "Module/__init__.py": "",
        "Module/output.py": "X = 1\n",
        "frontend_contract/__init__.py": "",
        "frontend_contract/rest.py": "from Module.output import X\n",
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R1]" in out


# ---------------------------------------------------------------- R2 反向依赖


def test_r2_flags_domain_depending_on_input(tmp_path):
    """领域层不得反向依赖 core.input。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/input/__init__.py": "",
        "core/input/routes.py": "Y = 1\n",
        "core/storage/__init__.py": "",
        "core/storage/repo.py": "from core.input.routes import Y\n",
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R2]" in out


# ---------------------------------------------------------------- R3 私有符号


def test_r3_flags_cross_package_private_import(tmp_path):
    """跨包导入下划线私有符号 → 违规。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/storage/__init__.py": "",
        "core/storage/repo.py": "_secret = 1\n",
        "core/memory/__init__.py": "",
        "core/memory/ltm.py": "from core.storage.repo import _secret\n",
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R3]" in out and "_secret" in out


def test_r3_allows_same_subpackage_private_import(tmp_path):
    """同一子包内的私有导入放行（评估 §4 规则 3 的 _common/_utils 豁免）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/input/__init__.py": "",
        "core/input/_common.py": "_helper = 1\n",
        "core/input/routes.py": "from core.input._common import _helper\n",
    })
    code, out = _run(repo)
    assert code == 0, out


def test_r3_ignores_type_checking_private_import(tmp_path):
    """TYPE_CHECKING 内的私有符号是类型边，不算运行时泄漏（R5 仅报告）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/storage/__init__.py": "",
        "core/storage/repo.py": "_T = 1\n",
        "core/memory/__init__.py": "",
        "core/memory/ltm.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from core.storage.repo import _T\n"
        ),
    })
    code, out = _run(repo)
    assert code == 0, out


# ---------------------------------------------------------------- R4 环


def test_r4_flags_eager_cycle(tmp_path):
    """模块级互相 import → eager 环 → 违规。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/a.py": "from core.b import B\nB = 1\n",
        "core/b.py": "from core.a import A\nA = 1\n",
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R4]" in out


def test_r4_ignores_lazy_cycle(tmp_path):
    """函数内懒导入打断的环**不得**报出（初版假阳性）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/a.py": "from core.b import B\n\ndef use():\n    return B\n",
        "core/b.py": (
            "A = 1\n\n"
            "def use():\n"
            "    from core.a import A\n"
            "    return A\n"
        ),
    })
    code, out = _run(repo)
    assert code == 0, f"懒导入打断的环被误报:\n{out}"


def test_r4_ignores_type_checking_cycle(tmp_path):
    """TYPE_CHECKING 类型边不构成运行时环。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/a.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from core.b import B\n"
        ),
        "core/b.py": "A = 1\n",
    })
    code, out = _run(repo)
    assert code == 0, out


# ---------------------------------------------------------------- R6 裸后台任务登记


def test_r6_flags_unregistered_bare_create_task(tmp_path):
    """未登记的裸 create_task 必须报出（ARCH-INC-P2-03 的回归防线）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/thing.py": (
            "import asyncio\n"
            "class Thing:\n"
            "    def go(self):\n"
            "        return asyncio.create_task(asyncio.sleep(0))\n"
        ),
    })
    code, out = _run(repo)
    assert code == 1
    assert "[R6]" in out and "core/thing.py::Thing.go" in out


def test_r6_flags_ensure_future_and_loop_create_task(tmp_path):
    """三条等价写法都要抓到——ensure_future 与 loop.create_task 同样绕过收口。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/a.py": (
            "import asyncio\n"
            "def f():\n"
            "    asyncio.ensure_future(asyncio.sleep(0))\n"
            "async def g():\n"
            "    asyncio.get_running_loop().create_task(asyncio.sleep(0))\n"
        ),
    })
    code, out = _run(repo)
    assert code == 1
    assert "core/a.py::f" in out
    assert "core/a.py::g" in out
    assert out.count("[R6]") == 2


def test_r6_reports_count_drift_within_same_function(tmp_path):
    """同一函数内**追加**第二个裸 spawn 也要报——布尔式判断会漏掉这种。

    登记表按 (文件, 限定名) 记期望**条数**，故 1 → 2 是漂移。
    """
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/thing.py": (
            "import asyncio\n"
            "def f():\n"
            "    asyncio.create_task(asyncio.sleep(0))\n"
        ),
    })
    code, out = _run(repo)
    assert code == 1 and "1 处" in out

    # 再塞一个：限定名不变，但条数变了
    (repo / "core" / "thing.py").write_text(
        "import asyncio\n"
        "def f():\n"
        "    asyncio.create_task(asyncio.sleep(0))\n"
        "    asyncio.create_task(asyncio.sleep(0))\n",
        encoding="utf-8",
    )
    code2, out2 = _run(repo)
    assert code2 == 1
    assert "2 处" in out2, out2


def test_r6_accepts_async_task_scope(tmp_path):
    """走 AsyncTaskScope 的调度不算裸 spawn——它就是收口点。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/thing.py": (
            "from Module._common.task_scope import AsyncTaskScope\n"
            "SCOPE = AsyncTaskScope('x')\n"
            "def f():\n"
            "    return SCOPE.spawn(_work())\n"
            "async def _work():\n"
            "    return None\n"
        ),
    })
    code, out = _run(repo)
    assert code == 0, out


def test_r6_ignores_tests(tmp_path):
    """测试代码不受 R6 约束。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/tests/test_x.py": (
            "import asyncio\n"
            "def test_a():\n"
            "    asyncio.create_task(asyncio.sleep(0))\n"
        ),
    })
    code, out = _run(repo)
    assert code == 0, out


def test_r6_allowlist_matches_real_repository_counts():
    """真实仓库的登记条数必须与实际完全一致（含陈旧登记）。

    R6 的「陈旧登记」是**独立**报出的违规（按 (文件, 限定名) 反查），
    因此这里只需断言真实仓库零 R6 硬违规——若某处被改成 AsyncTaskScope
    而忘了删登记条目，`test_real_repository_passes` 与本测试都会红。
    """
    from scripts.check_architecture import check_repository

    r6 = [v for v in check_repository(ROOT) if v.rule == "R6"]
    assert r6 == [], "R6 违规（含陈旧登记）：\n" + "\n".join(v.render() for v in r6)


# ---------------------------------------------------------------- 忽略范围


def test_tests_are_not_scanned(tmp_path):
    """tests/ 与 test_*.py 不参与边界检查（测试可以自由依赖）。"""
    repo = _make_repo(tmp_path, {
        "core/__init__.py": "",
        "core/agent.py": "X = 1\n",
        "Module/__init__.py": "",
        "Module/tests/test_x.py": "from core.agent import X\n",
    })
    code, out = _run(repo)
    assert code == 0, out
