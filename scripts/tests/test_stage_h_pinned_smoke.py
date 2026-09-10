"""BE-H2-03：真实库 smoke 的守卫（marker=unit，不触真实 Mongo）。

smoke 本身是显式 opt-in 的人工/CI gate（`STAGE_H_FIXTURES=1` + 非公网 Mongo）。这里
只锁「它不会偷偷跑」：模块可无副作用导入，且缺 opt-in 时拒绝执行。
"""

import asyncio

import pytest

from scripts.dev import stage_h_pinned_smoke as smoke_mod

pytestmark = pytest.mark.unit


def test_module_import_has_no_db_side_effects():
    """导入 smoke 模块不得建立任何连接（否则默认 pytest 收集就会碰开发库）。"""
    assert hasattr(smoke_mod, "main")
    assert smoke_mod.FIXTURE_CHARACTER_ID == "chr_stage_h_fixture"


def test_main_refuses_without_opt_in(monkeypatch):
    """未设 STAGE_H_FIXTURES 时 main() 立即拒绝，不进入任何 DB 路径。"""
    monkeypatch.delenv("STAGE_H_FIXTURES", raising=False)
    with pytest.raises(SystemExit):
        asyncio.run(smoke_mod.main())


def test_smoke_report_exit_code_follows_failures():
    """任一 FAIL 即退出码 1——否则 gate 会假过。"""
    smoke = smoke_mod._Smoke()
    smoke.check("X.ok", True)
    assert smoke.report()["exit"] == 0

    smoke.check("X.bad", False, "nope")
    report = smoke.report()
    assert report["exit"] == 1
    assert report["failed"] == ["X.bad"]
