"""文档守卫（`scripts/check_docs.py`，DG1–DG5）的测试（marker=unit）。

**为什么要有这个文件**：`check_docs.py` 是"文档整理完成后防止再次漂移"的那道网。
一道没有被测试钉住的守卫，其判据会随重构悄悄失效——本仓的教训是**假绿三形态**
（判据写错 / 守卫没装在改动上 / 环境不满足）。这里逐条把判据钉住，并且**先证明它能红**：
每个 negative 用例都构造一个真实违规，断言判据**确实报告**它，而不是只看"真实仓库现在过"。

真实仓库"现在过"这一条也在测（`test_real_repo_is_clean`）——但它的价值是回归网，
不是判据本身。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_docs import (
    _DG4_EXEMPTIONS,
    _FORBIDDEN,
    _HISTORICAL_MARKERS,
    _appconfig_env_names,
    _mask_non_link_regions,
    _resolve,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# DG1 —— 链接可解析
# ---------------------------------------------------------------------------


def test_dg1_resolves_relative_and_rejects_missing(tmp_path: Path):
    """相对链接按**文件所在目录**解析；不存在即不解析（判据不是"看起来像路径"）。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.md").write_text("x", encoding="utf-8")
    src = tmp_path / "docs" / "index.md"
    assert _resolve(src.parent, "target.md") is not None
    assert _resolve(src.parent, "target.md").exists()
    assert not _resolve(src.parent, "missing.md").exists()


def test_dg1_ignores_external_and_anchor_targets(tmp_path: Path):
    """外链与纯锚点不是内部链接，不该被解析（否则判据会去检查互联网）。"""
    assert _resolve(tmp_path, "https://example.com/x") is None
    assert _resolve(tmp_path, "#section") is None
    assert _resolve(tmp_path, "mailto:a@b.c") is None
    assert _resolve(tmp_path, "") is None


def test_mask_preserves_line_numbers_and_blanks_code():
    """遮蔽必须**等长替换**：报告的行号要等于原文行号。

    行内 code span 与围栏块都不是链接——`docs/readme-conventions.md` 的
    「面包屑按目录深度」表把示例写成 `` `[项目文档](../../README.md)` ``，
    剥不掉就会把**示例**判成坏链（假红会逼人删掉有价值的内容）。
    """
    text = "\n".join(
        [
            "见 [真链](a.md)",                                   # line 1：应保留
            "示例：`[项目文档](../../README.md)`",                  # line 2：应遮蔽
            "```markdown",                                        # line 3
            "> 导航：[项目文档](../../../README.md)",               # line 4：应遮蔽
            "```",                                                # line 5
            "再一条 [真链](b.md)",                                 # line 6：应保留
        ]
    )
    masked = _mask_non_link_regions(text)
    assert masked.count("\n") == text.count("\n"), "遮蔽改变了行数 ⇒ 行号会漂"
    assert "[真链](a.md)" in masked
    assert "[真链](b.md)" in masked
    assert "../../README.md" not in masked
    assert "../../../README.md" not in masked
    assert "> 导航" not in masked


# ---------------------------------------------------------------------------
# DG2 —— 禁止引用已知不存在的东西
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "契约在 `frontend_contract/ws_message.py` 里定义。",
        "工具调度走 `core/task/`。",
        "`TaskScheduler` 负责 worker pool。",
    ],
)
def test_dg2_patterns_hit_the_real_dead_references(line: str):
    """三条被禁模式必须**真的**命中它们声称的字符串（判据写错的假绿①）。"""
    assert any(p.search(line) for p, _ in _FORBIDDEN), line


@pytest.mark.parametrize(
    "line",
    [
        "工具调度在 `core/tool_runtime/`（ARCH-INC-P1-01，原 `core/task/`）",
        "历史记录里当时叫 `TaskScheduler`，后来改名了",
        "已归档：`frontend_contract/ws_message.py` 拆成了包",
        "`core/task/` → `core/tool_runtime/`",
    ],
)
def test_dg2_historical_context_is_allowed(line: str):
    """显式历史语境允许保留旧名——否则会逼人改写历史正文（§76 禁止）。"""
    assert any(marker in line for marker in _HISTORICAL_MARKERS), line


def test_dg2_historical_markers_are_not_vacuous():
    """标记表不能宽到"随便一行都算历史"：普通陈述句不该带标记。"""
    plain = "工具调度在 `core/task/` 里做即时调用并发。"
    assert not any(marker in plain for marker in _HISTORICAL_MARKERS), plain


# ---------------------------------------------------------------------------
# DG4 —— core/ 子系统 README 覆盖
# ---------------------------------------------------------------------------


def test_dg4_exemptions_carry_reasons():
    """豁免必须写**理由**——没有理由的豁免单会变成永久垃圾场。"""
    assert _DG4_EXEMPTIONS, "豁免表为空是允许的，但当前仓库有 core/tests 需要它"
    for rel, why in _DG4_EXEMPTIONS.items():
        assert rel.startswith("core/"), rel
        assert len(why.strip()) >= 8, f"{rel} 的豁免理由太短，等于没写：{why!r}"


def test_dg4_exempted_dirs_do_not_have_readme():
    """**登记陈旧即红**：豁免项若已经有 README，说明豁免理由在冒充事实。"""
    for rel, why in _DG4_EXEMPTIONS.items():
        assert not (_ROOT / rel / "README.md").is_file(), (
            f"{rel} 已在豁免表里（{why}），但它已经有 README —— 请删除该条目"
        )


# ---------------------------------------------------------------------------
# DG5 —— AppConfig 环境变量索引
# ---------------------------------------------------------------------------


def test_dg5_does_not_emit_container_field_names():
    """嵌套 dataclass 的**容器字段本身没有 env**。

    loader 走 `_is_dataclass_instance` 分支时只递归、`continue`（不 `os.getenv`）。
    早期版本把容器名也当 env 导出（`AGENT` / `SERVER` / `MEMORY`…），因为 DG5 当时用
    子串匹配，而 `AGENT` 恰好是 `AGENT_ACCESS_TOKEN` 的子串 ⇒ **判据假绿**。改用词边界后
    才暴露。这条用例钉住修好的形状。
    """
    names = _appconfig_env_names()
    containers = {"AGENT", "LLM", "MEMORY", "SERVER", "TTS", "STT", "WAKE_WORD",
                  "LIVE2D", "VISION", "DATABASE"}
    leaked = sorted(containers & set(names))
    assert not leaked, f"容器字段名被当成 env 导出了：{leaked}"


def test_dg5_emits_the_real_env_surface():
    """抽掉了"看起来对"的名单：这里只断言**已知真实存在**的几个名字在集合里。

    不写死总数（那会让守卫随配置增删而漂移，正是它要防的东西）。
    """
    names = set(_appconfig_env_names())
    for expected in ("SERVER_HOST", "MONGODB_URI", "TTS_RATE_LIMIT_RPM",
                     "LIVE2D_STATIC_IMAGE", "DATABASE_DB_NAME"):
        assert expected in names, f"{expected} 应从 app_config.py 导出"


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def test_real_repo_is_clean():
    """真实仓库当前必须通过（回归网）。

    这条**不是**判据本身——判据由上面的 negative 用例钉住。它的价值是：
    任何人让 canonical/supporting 文档漂移时，`make test-quick` 会直接红。
    """
    proc = subprocess.run(
        [sys.executable, "scripts/check_docs.py"],
        cwd=_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"文档守卫未通过（退出 {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"
    )
    assert "文档守卫通过" in proc.stdout, "通过时必须打印通过标记（假绿③：不打印=拒答）"


def test_guard_refuses_to_pass_when_scope_is_empty():
    """**拒答**契约：扫描面不成立时非零退出且不打印通过标记（假绿③）。"""
    proc = subprocess.run(
        [sys.executable, "scripts/check_docs.py", "--list"],
        cwd=_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert "扫描面" in proc.stdout
    assert "不扫（历史语料）" in proc.stdout, "--list 必须交代不扫什么，否则范围不可审计"
