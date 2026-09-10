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


def test_run_cleans_up_in_order_even_when_a_phase_raises(monkeypatch):
    """REST/WS/断言任一步抛异常，收尾仍必须完整执行，且先停机再清理。

    回归：收尾写成正常路径的后续语句时，异常会直接跳出，fixture 与其派生记录
    全部留在库里。
    """
    import asyncio

    events: list[str] = []

    class _FakeAgent:
        async def start(self) -> None:
            events.append("start")
            raise RuntimeError("模拟 WS 超时")

        async def stop(self) -> None:
            events.append("stop")

    async def _fake_cleanup(repos: object) -> dict:
        events.append("cleanup")
        return {"removed": {}}

    async def _fake_seed(repos: object) -> dict:
        events.append("seed")
        return {}

    monkeypatch.setattr(smoke_mod, "_make_agent", lambda: _FakeAgent())
    monkeypatch.setattr(smoke_mod, "cleanup_fixture", _fake_cleanup)
    monkeypatch.setattr(smoke_mod, "seed_fixture", _fake_seed)

    smoke = smoke_mod._Smoke()
    with pytest.raises(RuntimeError):
        asyncio.run(smoke_mod._run(smoke, object()))

    # 基线清理 → 启动 → 停机 → 异常路径收尾清理（顺序不可换）
    assert events == ["cleanup", "start", "stop", "cleanup"]


def test_main_reports_partial_checks_when_a_phase_raises(monkeypatch, capsys):
    """_run 抛异常时仍打印已跑完的断言 + 一个 aborted 失败项，并退出 1。"""
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setenv("STAGE_H_FIXTURES", "1")

    async def _boom(smoke: object, repos: object) -> None:
        raise RuntimeError("boom")

    async def _fake_repos() -> object:
        return SimpleNamespace()

    monkeypatch.setattr(smoke_mod, "_assert_non_production", lambda uri: None)
    monkeypatch.setattr(smoke_mod, "_repos", _fake_repos)
    monkeypatch.setattr(smoke_mod, "_run", _boom)

    assert asyncio.run(smoke_mod.main()) == 1
    payload = capsys.readouterr().out
    assert "smoke.aborted" in payload
    assert "RuntimeError" in payload
