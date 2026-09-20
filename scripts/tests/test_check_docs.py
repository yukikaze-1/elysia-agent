"""文档守卫（`scripts/check_docs.py`，DG1–DG5）的测试（marker=unit）。

**为什么要有这个文件**：`check_docs.py` 是"文档整理完成后防止再次漂移"的那道网。
一道没有被测试钉住的守卫，其判据会随重构悄悄失效——本仓的教训是**假绿三形态**
（判据写错 / 守卫没装在改动上 / 环境不满足）。这里逐条把判据钉住，并且**先证明它能红**：
每个 negative 用例都构造一个真实违规，断言判据**确实报告**它，而不是只看"真实仓库现在过"。

真实仓库"现在过"这一条也在测（`test_real_repo_is_clean`）——但它的价值是回归网，
不是判据本身。
"""

from __future__ import annotations

import os
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
# DG1b / DG6 / DG7 —— Challenger 2026-09-20 反证后新增的三条判据
# ---------------------------------------------------------------------------


def test_dg1b_flags_a_broken_anchor(tmp_path: Path):
    """`#anchor` 必须落在真实标题上——文件在、锚点拼错，对读者与 404 是同一件事。"""
    from scripts.check_docs import _heading_slugs

    doc = tmp_path / "t.md"
    doc.write_text("# 标题\n\n## 10. 日志系统\n\n[坏](../t.md#10-日志系统)\n",
                   encoding="utf-8")
    slugs = _heading_slugs(doc)
    assert "10-日志系统" in slugs, slugs
    assert "10-日志系" not in slugs


def test_dg6_detects_unbalanced_fence_and_tagged_closer(tmp_path: Path):
    """缺闭合围栏 ⇒ 后续标题被当成代码渲染；带 info string 的"闭合"围栏不算闭合。"""
    from scripts.check_docs import check_dg6

    good = tmp_path / "good.md"
    good.write_text("# t\n\n```yaml\na: 1\n```\n\n## s\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("# t\n\n```yaml\na: 1\n\n```python\n## s\n", encoding="utf-8")

    import scripts.check_docs as cd
    orig = cd._ROOT
    cd._ROOT = tmp_path
    try:
        assert check_dg6(["good.md"]) == []
        problems = check_dg6(["bad.md"])
    finally:
        cd._ROOT = orig
    assert problems, "缺闭合围栏必须报"


def test_dg7_resolves_package_relative_and_flags_missing(tmp_path: Path):
    """DG7 要能同时处理"根相对"与"包相对"（后者相对文档所在目录的各级祖先），
    并对真正不存在的仓库路径报错——判据是 **git 跟踪清单**，不是磁盘。"""
    doc = "见 `memory/tests/test_x.py`（包相对）与 `core/agent/agent.py`（根相对）；坏引用 `core/tests/test_missing.py`。\n"
    problems = _dg7_in_sandbox(
        tmp_path, doc,
        tracked=["core/memory/tests/test_x.py", "core/agent/agent.py"],
    )
    assert len(problems) == 1, problems
    assert "core/tests/test_missing.py" in problems[0]


# ---------------------------------------------------------------------------
# DG7 —— 仓库真值 vs 工作区文件系统（2026-09-21 clean-checkout 跟进）
# ---------------------------------------------------------------------------
#
# 真实 CI 在干净 checkout 里报出 8 条 DG7 失败，而本机全绿。根因：DG7 曾用
# `Path.exists()` 判定引用有效，于是**只存在于开发者工作区的 untracked/gitignored
# 文件**（tmp/ 探针、npm build 产物、运行时 data/ 目录）让引用看起来成立。
#
# 下面这组用例把语义钉死：**仓库引用必须对仓库真值成立**，
# 非仓库路径必须**显式分类**（带理由），而不是"碰巧存在"。


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dg7_in_sandbox(tmp_path: pathlib.Path, doc_text: str, *, tracked=(), untracked=(),
                    extra_files=()) -> list[str]:
    """在沙箱里跑 DG7：`tracked` 是**仓库真值**，`untracked` 只存在于文件系统。

    模拟的关键点：本函数把"git 说存在的"和"磁盘上碰巧存在的"分开喂进去——
    旧实现只看后者，所以 `untracked` 能让引用假绿。
    """
    import scripts.check_docs as cd

    _write(tmp_path / "core" / "README.md", doc_text)
    for rel in tracked:
        _write(tmp_path / rel, "x")
    for rel in untracked:
        _write(tmp_path / rel, "x")
    for rel in extra_files:
        _write(tmp_path / rel, "x")

    orig_root = cd._ROOT
    orig_inv = cd._tracked_inventory
    cd._ROOT = tmp_path
    cd._tracked_inventory = lambda root=None: tuple(sorted(set(tracked) | {"core/README.md"}))
    try:
        return cd.check_dg7(["core/README.md"])
    finally:
        cd._ROOT = orig_root
        cd._tracked_inventory = orig_inv


def test_dg7_t1_untracked_local_file_cannot_validate_a_reference(tmp_path: Path):
    """**DG7-T1（核心回归）**：untracked 的本地文件不能让仓库引用成立。

    这正是真实 CI 的失败形状：`tmp/b14/subA/probe_kb26.py` 在开发者机器上存在，
    于是本机判绿；干净 checkout 里没有它，CI 判红。同一次提交必须给出同一个结论。
    """
    doc = "见 `docs/architecture/invariants.md` 与 `tmp/scratch/probe.py`。\n"
    problems = _dg7_in_sandbox(
        tmp_path, doc,
        tracked=["docs/architecture/invariants.md"],
        untracked=["tmp/scratch/probe.py"],      # 只在磁盘上，git 里没有
    )
    assert len(problems) == 1, problems
    assert "tmp/scratch/probe.py" in problems[0]


def test_dg7_t2_tracked_file_resolves_without_touching_the_filesystem(tmp_path: Path):
    """**DG7-T2**：仓库真值里的路径必须判过——**即使磁盘上没有该文件**。

    这条把方向也钉住了：判据是"git 里有"，不是"磁盘上有"。
    """
    doc = "见 `core/agent/agent.py` 与 `docs/operations.md`。\n"
    problems = _dg7_in_sandbox(
        tmp_path, doc,
        tracked=["core/agent/agent.py", "docs/operations.md"],
        untracked=[],                       # 磁盘上不创建它们
    )
    assert problems == [], problems


def test_dg7_t3_exact_reason_bearing_runtime_exemption_passes(tmp_path: Path):
    """**DG7-T3**：被**显式分类**的运行时路径判过（且不要求它在磁盘上存在）。"""
    doc = "上传落到 `data/uploads/`，视觉缓存落 `data/cache/vision/`。\n"
    problems = _dg7_in_sandbox(tmp_path, doc, tracked=[])
    assert problems == [], problems


def test_dg7_t4_unknown_missing_path_fails(tmp_path: Path):
    """**DG7-T4**：既不在仓库真值里、也不在分类表里的路径必须红。"""
    doc = "见 `core/agent/does_not_exist.py`。\n"
    problems = _dg7_in_sandbox(tmp_path, doc, tracked=["core/agent/agent.py"])
    assert len(problems) == 1, problems
    assert "core/agent/does_not_exist.py" in problems[0]


def test_dg7_t5_stale_exemption_fails(tmp_path: Path):
    """**DG7-T5**：登记陈旧必须红——两类陈旧都要报。

    ① **没有任何文档再引用它**（登记表不该变成永久垃圾场）；
    ② **它其实是仓库路径**（那就不需要豁免，留着会让真判据被绕过）。
    注意：**"干净 checkout 里不存在"不算陈旧**——运行时路径本就该缺席，
    否则就形成"豁免项必须物理存在"的悖论。
    """
    import scripts.check_docs as cd

    orig_root = cd._ROOT
    try:
        # ① 未被引用
        cd._ROOT = tmp_path
        p1 = cd.check_stale_registrations(scan_texts={}, tracked=())
        assert any("data/uploads/" in x for x in p1), p1

        # ② 已被 git 跟踪（下面有被跟踪内容）⇒ 不需要豁免
        p2 = cd.check_stale_registrations(
            scan_texts={"a.md": "`data/uploads/`"}, tracked=("data/uploads/keep.txt",)
        )
        assert any("已被 git 跟踪" in x for x in p2), p2

        # ③ 被引用 + 未被跟踪 ⇒ **不陈旧**（这是 §14 的悖论修复）
        p3 = cd.check_stale_registrations(
            scan_texts={"a.md": "`data/uploads/`"}, tracked=()
        )
        assert not any("data/uploads/" in x for x in p3), p3
    finally:
        cd._ROOT = orig_root


def test_dg7_declared_roots_are_never_tracked():
    """**分类表不得覆盖任何被跟踪的树**——这是"分类 ≠ 消音"的机械证明。

    豁免之所以成立，是因为登记项**有意不在仓库里**（运行期创建 / 构建期生成 / 外部仓）。
    一旦某项（或它下面的任何路径）被 git 跟踪，豁免就从"分类"退化成"绕过真判据"，
    必须红（这条同时是 §11 要求的"证明预期边界的测试"）。
    """
    from scripts.check_docs import _NON_REPOSITORY_PATHS, _tracked_inventory

    tracked = set(_tracked_inventory())
    for declared, (kind, reason) in _NON_REPOSITORY_PATHS.items():
        base = declared.rstrip("/")
        covered = sorted(t for t in tracked if t == base or t.startswith(base + "/"))
        assert not covered, (
            f"`{declared}`（{kind}）被登记为**非仓库**路径，但下面这些路径被 git 跟踪："
            f"{covered[:5]} —— 豁免覆盖了仓库内容，请删除该条目或收窄它"
        )
        assert len(reason.strip()) >= 10, f"{declared} 的理由太短：{reason!r}"


def test_dg7_tmp_is_not_exempt():
    """`tmp/` **不得**出现在分类表里。

    临时探针是"当次调查"的产物，canonical 文档引用它等于把易腐的 scratch 当成证据；
    正确做法是改引稳定来源（测试 node id / 符号 / 执行记录）。这条把纪律钉死，
    防止"下次 CI 红了就把 tmp/ 加进豁免表"。
    """
    from scripts.check_docs import _NON_REPOSITORY_PATHS

    offenders = [d for d in _NON_REPOSITORY_PATHS if d.rstrip("/") == "tmp" or d.startswith("tmp/")]
    assert not offenders, f"`tmp/` 不得被豁免（应改引稳定来源）：{offenders}"


def test_dg7_exemptions_are_narrow_not_whole_trees(tmp_path: Path):
    """**豁免必须窄**：不得出现 `data/`、`static/`、`tmp/` 这类整棵树豁免。

    真实反证（Challenger Q5 的形状）：若豁免了 `data/`，那么 `data/cache/visoin/`
    这种**拼错**的路径也会通过。本用例把"拼错仍未跟踪的路径必须红"钉住——
    它是豁免宽度是否可接受的唯一可执行判据。
    """
    from scripts.check_docs import _NON_REPOSITORY_PATHS

    for forbidden in ("data", "static", "tmp", "log"):
        assert forbidden not in {d.rstrip("/") for d in _NON_REPOSITORY_PATHS}, (
            f"`{forbidden}/` 不允许整棵树豁免（会产生 `{forbidden}/typo/` 也通过的盲区）"
        )

    # 拼错的运行期路径 + 拼错的仓库路径都必须红
    doc = "写 `data/cache/visoin/`、`static/index.htmll`、`core/agent/agent_x.py`。\n"
    problems = _dg7_in_sandbox(tmp_path, doc, tracked=["core/agent/agent.py"])
    flagged = " ".join(problems)
    assert "data/cache/visoin/" in flagged, problems
    assert "static/index.htmll" in flagged, problems
    assert "core/agent/agent_x.py" in flagged, problems
    assert len(problems) == 3, problems


def test_dg7_h1_prose_marker_must_not_blind_the_next_lines(tmp_path: Path):
    """**洞 #1 回归**（Challenger 2026-09-21 反证）：散文里的普通标记词不得让**后面几行**免检。

    曾经用"本行 + 上文 6 行"判历史语境，于是捏造一个路径、把它放在含 `归档`/`历史`/`不再`
    这类**散文词**的行后面 1 行，整道守卫就报绿（实测 `EXIT=0`）。这等于给文档开了
    "在这些词附近随便写路径"的后门。现在只认**同一行**或**同一张表**的标记。

    表格特例仍必须保留：改名映射表的标记在表头（`| 旧 | 新 |`），数据行本身干净。
    """
    marker_line = "- 会话删除后由归档清理重试负责。\n"
    bogus = "core/tool_runtime/checkpoint_typo.py"

    problems = _dg7_in_sandbox(tmp_path, marker_line + f"- 检查点见 `{bogus}`。\n")
    assert len(problems) == 1, f"标记词后的下一行必须仍被检查：{problems}"
    assert bogus in problems[0]

    # 同一张表：表头带标记 ⇒ 表内数据行放行（改名映射表的正当形态）
    table = (
        "> | 旧 | 新 |\n"
        "> |---|---|\n"
        "> | `core/task/` | `core/tool_runtime/` |\n"
    )
    assert _dg7_in_sandbox(tmp_path, table) == [], "改名映射表应被放行"


def test_dg7_h2_explicit_inventory_cannot_widen_git_truth(tmp_path: Path):
    """**洞 #2 回归**：手工喂入的清单**不得**放宽仓库真值（不得制造假绿）。

    Challenger 反证：`git ls-files` + 一条 untracked 路径作为 `--tracked-from`，
    旧实现直接采信 ⇒ `EXIT=0` + 通过标记，恢复了"本机绿 / CI 红"的同一形状。
    现在的语义：**git 永远优先**；同时给了显式清单就只做子集校验，冲突即拒答。
    """
    import scripts.check_docs as cd

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x", encoding="utf-8")
    listing = tmp_path / "listing.txt"
    listing.write_text("docs/x.md\ntmp/fake_probe.py\n", encoding="utf-8")

    orig_root, orig_git, orig_env = cd._ROOT, cd._git_tracked, os.environ.get(cd._TRACKED_FROM_ENV)
    cd._TRACKED_CACHE.clear()
    cd._ROOT = tmp_path
    cd._git_tracked = lambda root=None: cd._inventory_from_listing({"docs/x.md"})
    try:
        try:
            cd._tracked_inventory(tracked_from=listing)
        except RuntimeError as exc:
            assert "冲突" in str(exc)
        else:
            raise AssertionError("显式清单放宽 git 真值时必须拒答，而不是采信")
    finally:
        cd._ROOT, cd._git_tracked = orig_root, orig_git
        if orig_env is None:
            os.environ.pop(cd._TRACKED_FROM_ENV, None)
        cd._TRACKED_CACHE.clear()


def test_dg7_h3_explicit_listing_handles_cquoted_cjk(tmp_path: Path):
    """**洞 #3 回归**：显式清单两种格式都要能读对（`-z` 与 C-quoted 行）。

    `git ls-files`（无 `-z`）会把非 ASCII 路径 C-quote 成 `"docs/\346\211\247..."`，
    按行直读会**丢掉真实路径**并造出 `"docs` 之类的假条目——实测差 63 条，
    于是同一份清单在"有 .git"与"没 .git"两种环境下给出**两个结论**。
    """
    import scripts.check_docs as cd

    cjk = "docs/review/2026-09-16-Batch1-correctness-durability-\u6267\u884c\u8bb0\u5f55.md"
    quoted = '"' + "".join(
        ch if ord(ch) < 128 else "".join(f"\\{o:03o}" for o in ch.encode("utf-8"))
        for ch in cjk
    ) + '"'
    lines = tmp_path / "q.txt"
    lines.write_text(f'README.md\n{quoted}\n', encoding="utf-8")
    assert cd._parse_explicit_listing(lines) == {"README.md", cjk}

    nul = tmp_path / "q.bin"
    nul.write_bytes(f"README.md\0{cjk}\0".encode("utf-8"))
    assert cd._parse_explicit_listing(nul) == {"README.md", cjk}


def test_dg7_low_whole_tree_exemption_is_rejected_by_the_guard():
    """**LOW 回归**：整棵顶层目录豁免必须**在代码层**被拒（不只靠一个硬编码单测名单）。

    Challenger 反证：`covers_tracked` 只抓得到"覆盖了被跟踪内容"的树；一棵**当前未被跟踪**
    的整树（例如 `.model_cache/`）画不出任何机械反对票——于是 `<树>/<拼错>/` 会通过。
    """
    import scripts.check_docs as cd

    orig = dict(cd._NON_REPOSITORY_PATHS)
    cd._NON_REPOSITORY_PATHS["model_cache"] = (cd.PATH_KIND_RUNTIME, "MUTANT：整棵树豁免（应被拒）")
    try:
        problems = cd.check_stale_registrations(scan_texts={"a.md": "`model_cache/`"}, tracked=())
    finally:
        cd._NON_REPOSITORY_PATHS.clear()
        cd._NON_REPOSITORY_PATHS.update(orig)
    assert any("整棵顶层目录" in p for p in problems), problems


def test_dg7_refuses_when_tracked_inventory_unavailable(tmp_path: Path):
    """**拒答契约**：取不到仓库真值时 exit 2 且**不打印通过标记**。

    绝不允许退回文件系统判据——那正是 2026-09-21 clean-checkout 假绿的成因。
    """
    import scripts.check_docs as cd

    orig = cd._ROOT
    cd._ROOT = tmp_path          # 不是 git 仓库
    cd._TRACKED_CACHE.clear()
    try:
        try:
            cd._tracked_inventory()
        except RuntimeError as exc:
            assert "git ls-files" in str(exc)
        else:
            raise AssertionError("非 git 目录必须拒答，而不是给出清单")
    finally:
        cd._ROOT = orig
        cd._TRACKED_CACHE.clear()


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
