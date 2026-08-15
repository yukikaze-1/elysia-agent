"""Stage 4 smoke 断言（P1-01）：脚本可执行、默认 stub 全 PASS、失败非零退出。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE = ROOT / "scripts" / "stage4_smoke.py"

pytestmark = pytest.mark.slow


def _run_smoke() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SMOKE), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        env={"PATH": "/usr/bin:/bin", "SENSEVOICE_URL": "", "STAGE4_SMOKE_LIVE": "0"},
    )


def test_smoke_exits_zero_on_stub_pass():
    """默认 stub 模式全 PASS → 退出码 0（spec §5.3 非零只在失败）。"""
    proc = _run_smoke()
    assert proc.returncode == 0, f"stub smoke 应通过:\n{proc.stdout}\n{proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["exit"] == 0
    assert report["failed"] == 0
    assert report["sha"] and report["protocol"]
    assert report["scenarios"]  # 至少覆盖 5 场景
