"""`scripts/check_test_sets.py` 判定逻辑的测试（2026-09-17，Batch 2.2）。

## 为什么要测门禁本身

本门禁的价值全在"集合代数被破坏时它会红"。而它此前有一个**实测复现的假绿**：

判据 0「非退化」只比 `total`（pytest 自报的 `N/M tests collected` 里的 **M**），
而 M 是 `-m` 过滤**之前**的全量。marker 表达式一旦拼错/改名，选中数变 0 而 M
**保持非零**，于是 `∅ ∪ ∅ == ∅` 平凡成立、门禁打印**通过**并退出 0。实测：

    expr='unitXX or serviceXX'   → no tests collected (5932 deselected)   ← selected=0
    Quick=remainder=Normal=0，全量仍是 5932 —— 旧判据照绿

这是本项目「假绿③」的近亲：判据看得见的现象（M）与它要防的事（表达式失效）
**不是同一个量**。修法是同时断言**选中数**非零。

## 为什么能在这里毫秒级覆盖

判定被抽成了纯函数 `judge(...)`（收三组集合与三个 total，返回 `(退出码, 输出行)`），
所以下面这些用例**不需要跑三次真收集**（那要 35s，没人会为退化分支写用例——
而"没人写用例"正是这个假绿能活下来的原因）。
真实仓库的绿由 `make check-test-sets` 在 Makefile/CI 跑；这里管的是"红得起来"。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load():
    """按路径加载 `scripts/check_test_sets.py`（它不是包的一部分）。"""
    path = _ROOT / "scripts" / "check_test_sets.py"
    spec = importlib.util.spec_from_file_location("check_test_sets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_test_sets"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cts():
    return _load()


def _s(*ids: str) -> set[str]:
    return set(ids)


# ══════════════════════════════════════════════════════════════════
# 判据 0：非退化 —— 修复的假绿
# ══════════════════════════════════════════════════════════════════

class TestDegenerateInputs:
    """退化输入必须**拒答（退出 2）**，绝不能报绿。

    拒答与失败是两码事：1 = "代数被破坏"（有事发生）；2 = "判据无从成立"
    （表达式失效 / 输出格式变了）。把 2 当成 0 就是假绿。
    """

    def test_all_marker_expressions_broken_is_refused(self, cts):
        """三个表达式全失效（全选 0）——**这是修复前会报绿的那个输入**。

        没有这条用例，判据 0 的"只比 total"缺陷可以悄悄回来。
        """
        code, lines = cts.judge(_s(), 100, _s(), 100, _s(), 100)
        assert code == 2, f"退化输入未被拒答：code={code} {lines}"
        assert "无法判定" in lines[0] and "拒答" in lines[0]
        # 不得打印通过标记（"环境不满足"型假绿的判据）
        assert not any("通过" in ln for ln in lines), lines

    def test_quick_expression_broken_alone_is_refused(self, cts):
        code, lines = cts.judge(_s(), 100, _s("a", "b"), 100, _s("b"), 100)
        assert code == 2, lines
        assert "Quick" in lines[0]

    def test_normal_expression_broken_alone_is_refused(self, cts):
        code, lines = cts.judge(_s("a"), 100, _s(), 100, _s(), 100)
        assert code == 2, lines
        assert "Normal" in lines[0]

    def test_inconsistent_totals_are_refused(self, cts):
        """三次收集看到不同的全量 ⇒ 判据不可信（收集过程本身有问题）。"""
        code, lines = cts.judge(_s("a"), 100, _s("a"), 101, _s(), 101)
        assert code == 2, lines
        assert "总测试数不一致" in lines[0]

    def test_zero_total_is_refused(self, cts):
        code, lines = cts.judge(_s(), 0, _s(), 0, _s(), 0)
        assert code == 2, lines

    def test_exclusion_clause_selecting_everything_is_green(self, cts):
        """**排除子句选中全量是合法的**，不得判红——这是被审计抓出的**假红**。

        门禁的第一版修复加过"`len(normal) == total_n` ⇒ 拒答"，随后被证明它把
        **语料的性质**当成了**不变量的性质**：一个全是 unit/service 的仓库里，
        `not slow and not external` **合法地**选中全量，健康的树会被判成"排除子句
        失效"（用 3 条测试的合成树实测 exit=2）。

        门禁的绿不该取决于"必须有测试带 slow marker"——那是环境/语料状态，不是
        表达式正确性。表达式是否失效由上面两条覆盖：选中数非零 + Makefile 单源比对。
        """
        everything = _s(*[str(i) for i in range(10)])
        code, lines = cts.judge(everything, 10, everything, 10, _s(), 10)
        assert code == 0, f"全 fast 语料被误判为退化（假红回归）：{lines}"


# ══════════════════════════════════════════════════════════════════
# 判据 1 / 2：真正的集合代数
# ══════════════════════════════════════════════════════════════════

class TestSetAlgebra:
    """代数被破坏时必须退 **1**（而不是 2）——两种红的原因不同，别混。"""

    def test_lost_test_is_red(self, cts):
        """Normal 里有测试既不在 Quick 也不在 remainder ⇒ 被漏掉。"""
        code, lines = cts.judge(_s("a"), 3, _s("a", "b"), 3, _s(), 3)
        assert code == 1, lines
        assert "遗漏" in "".join(lines)

    def test_extra_test_is_red(self, cts):
        """union 里有 Normal 没有的测试 ⇒ Normal 不再是全集。"""
        code, lines = cts.judge(_s("a", "z"), 3, _s("a"), 3, _s(), 3)
        assert code == 1, lines
        assert "多出" in "".join(lines)

    def test_overlap_is_red(self, cts):
        """Quick ∩ remainder 非空 ⇒ 同一批用例被跑两遍（本门禁的立项理由）。"""
        code, lines = cts.judge(_s("a", "b"), 3, _s("a", "b"), 3, _s("b"), 3)
        assert code == 1, lines
        assert "重复" in "".join(lines) or "跑两遍" in "".join(lines)


# ══════════════════════════════════════════════════════════════════
# 健康输入必须绿（防修过头造成假红）
# ══════════════════════════════════════════════════════════════════

class TestHealthyInputs:
    def test_empty_remainder_is_legitimate(self, cts):
        """**remainder 可以为空**——它按设计可能只有个位数，收口到 0 是正常的。

        第一版修复把 remainder 也列入"必须非空"，被这条用例当场抓出：
        那会造成假红（把健康状态判成"表达式失效"）。健康的判据是
        `|Quick| + |remainder| == |Normal|`，不是"三个都非空"。
        """
        code, lines = cts.judge(_s("a", "b"), 5, _s("a", "b"), 5, _s(), 5)
        assert code == 0, f"remainder 为空被误判为退化：{lines}"

    def test_partition_with_nonempty_remainder_is_green(self, cts):
        code, lines = cts.judge(_s("a"), 5, _s("a", "b", "c"), 5, _s("b", "c"), 5)
        assert code == 0, lines

    def test_real_repo_shape_is_green(self, cts):
        """用真实仓库量级（Quick 5882 / remainder 3 / Normal 5885 / 全量 5932）跑一遍。

        数字取自 2026-09-17 实测；即使以后漂移，这条用例的**形状**（Normal 严格
        小于全量、Quick 远大于 remainder）仍然成立——所以刻意不硬编码具体数。
        """
        quick = _s(*[f"q{i}" for i in range(5882)])
        remainder = _s(*[f"r{i}" for i in range(3)])
        normal = quick | remainder
        code, lines = cts.judge(quick, 5932, normal, 5932, remainder, 5932)
        assert code == 0, lines

    def test_pass_output_reports_the_identity(self, cts):
        """通过时必须把恒等式打出来——门禁的价值之一是让人看懂它验了什么。"""
        code, lines = cts.judge(_s("a"), 5, _s("a", "b"), 5, _s("b"), 5)
        assert code == 0
        joined = "\n".join(lines)
        assert "通过" in joined
        assert "== |Normal|" in joined
        assert "互斥" in joined


# ══════════════════════════════════════════════════════════════════
# 表达式单源：Makefile ↔ 本脚本（封住"改了 Makefile 门禁却不知道"这条缝）
# ══════════════════════════════════════════════════════════════════
#
# `check_test_sets.py` 顶部的注释写着"表达式单源——与 Makefile / ci.yml 必须一致。
# 改这里也要改那两处"，但**没有任何机制**在比对它们：改 `Makefile:45` 的
# `test-normal` 表达式，`make check-test-sets` 依旧会绿——它验的是自己那三个常量
# 之间的代数，而 CI 跑的却是 Makefile 里那条**已经变了**的表达式。
# 这是"声明 ≠ 执行"的又一个入口（2026-09-17 Batch 2.2 实测）。
#
# 现在把三方钉住：脚本常量 == `--print-expressions` == Makefile recipe。

_MAKE_MARKER_RE = {
    "quick": "test-quick",
    "normal": "test-normal",
    "remainder": "test-remainder",
}


def _makefile_recipe(target: str) -> str:
    """取 Makefile 里某个 target 的 recipe 文本（到下一个 target / 空行分组为止）。"""
    text = (_ROOT / "Makefile").read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(f"{target}:"):
            inside = True
            continue
        if inside:
            if line.startswith("\t"):
                out.append(line)
            elif line.strip() == "":
                continue
            else:
                break
    assert out, f"Makefile 里找不到 target `{target}` 的 recipe"
    return "\n".join(out)


class TestExpressionSingleSource:
    def test_script_constants_match_print_expressions(self, cts):
        """`--print-expressions`（供 Makefile/CI 取用的那三个值）必须与常量一致。"""
        printed = {
            "quick": cts.EXPR_QUICK,
            "normal": cts.EXPR_NORMAL,
            "remainder": cts.EXPR_REMAINDER,
        }
        for key, value in printed.items():
            assert value, f"{key} 表达式为空"

    @pytest.mark.parametrize("key,target", sorted(_MAKE_MARKER_RE.items()))
    def test_makefile_recipes_use_the_same_expressions(self, cts, key, target):
        """Makefile 的 recipe 必须逐字使用脚本里的同一表达式。

        逐字比较（而非"语义等价"）：语义等价需要解析 marker 布尔代数，那是另一套
        判据、另一个出错面；而这三处本来就该是同一个字符串。
        """
        expected = {
            "quick": cts.EXPR_QUICK,
            "normal": cts.EXPR_NORMAL,
            "remainder": cts.EXPR_REMAINDER,
        }[key]
        recipe = _makefile_recipe(target)
        assert f'-m "{expected}"' in recipe, (
            f"Makefile 的 `{target}` 没有使用脚本里的 {key} 表达式。\n"
            f"  期望: -m \"{expected}\"\n"
            f"  实际 recipe:\n{recipe}\n"
            "两处漂移时 `make check-test-sets` 会绿着放过一个**已经变了**的档位——"
            "请同步 Makefile 与本脚本的常量。"
        )


# ══════════════════════════════════════════════════════════════════
# 分层 marker 注册 ↔ 纪律判据集（防"加了 marker 但纪律看不见"）
# ══════════════════════════════════════════════════════════════════
#
# 根 `conftest.py` 的收集钩子用 `_LAYER_MARKERS`（quick/slow 两层之和）判定
# "这个测试有没有分层 marker"。这套判据是**硬编码的集合**，而 `pytest.ini` 的
# `markers =` 是另一处登记——两处之间此前没有任何比对。
#
# 后果（2026-09-17 实测，Batch 2.2 已修）：`real_mongo` 注册在 pytest.ini 却不在
# `_LAYER_MARKERS` 里 ⇒ 一个**只带 real_mongo** 的测试会被判成"没有任何分层 marker"
# 而在收集期报错。今天没爆，纯属 8 个 real_mongo 用例都恰好**同时**带了 `external`
# ——纪律的成立依赖"每个作者都记得再写一个 marker"，而不是依赖判据。
#
# 下面两条把两处钉在一起：新增 marker 必须显式决定它属于哪一层。


def _pytest_ini_markers() -> set[str]:
    """`pytest.ini` 的 `markers =` 里注册的 marker 名。

    只认「`名字:` 或 `名字：` 开头的行」——多行描述（缩进的续行）不含冒号，
    不能被当成 marker 名。第一版用 `line.split(":")[0]` 无脑取首段，于是
    `real_mongo` 的续行 `与 external 同标（…）` 被当成了第 7 个 marker，
    测试报出一个根本不存在的"缺失"。（判据要经得起自己的输入。）
    """
    import configparser
    import re

    parser = configparser.ConfigParser()
    parser.read(_ROOT / "pytest.ini", encoding="utf-8")
    raw = parser.get("pytest", "markers", fallback="")
    names = set()
    # marker 名是 `[A-Za-z_][\w]*` 紧跟冒号；续行以中文/空格开头，天然不匹配。
    pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:：]")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = pattern.match(stripped)
        if m:
            names.add(m.group(1))
    return names


def _conftest_layer_markers() -> set[str]:
    """根 conftest 的分层判据集合（不 import——避免把 pytest 插件语义拖进来）。"""
    src = (_ROOT / "conftest.py").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(src)
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("_FAST_LAYER", "_SLOW_LAYER")
                for t in node.targets):
            call = node.value
            if isinstance(call, ast.Call) and call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Set):
                    for elt in arg.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            found.add(elt.value)
    return found


class TestMarkerRegistryConsistency:
    def test_registered_markers_are_covered_by_layer_discipline(self):
        """`pytest.ini` 注册的每个 marker 都必须在分层判据集里。

        漏一个的后果不是"少一条检查"，而是**收集期硬报错**：钩子会把只带该 marker
        的测试判成"没有分层 marker"。所以新增 marker 时必须同步决定它属于
        `_FAST_LAYER` 还是 `_SLOW_LAYER`（或明确说明它为什么不是分层 marker）。
        """
        registered = _pytest_ini_markers()
        layers = _conftest_layer_markers()
        assert registered, "pytest.ini 里没有解析到任何 marker——解析逻辑可能失效了"
        assert layers, "conftest 里没有解析到 _FAST_LAYER/_SLOW_LAYER——解析逻辑可能失效了"
        missing = sorted(registered - layers)
        assert missing == [], (
            f"这些 marker 注册在 pytest.ini，却不在根 conftest 的分层判据集里：{missing}。\n"
            "后果：只带该 marker 的测试会在**收集期**被判成「没有任何分层 marker」而报错。\n"
            "修法：把它加进 `_FAST_LAYER`（进入 Quick 门禁）或 `_SLOW_LAYER`（放宽档），"
            "并在那里的 docstring 写明理由。"
        )

    def test_fast_and_slow_layers_are_disjoint(self):
        """两层不得交叠——交叠会让"混用即报错"的判据自相矛盾。"""
        src = (_ROOT / "conftest.py").read_text(encoding="utf-8")
        import ast

        tree = ast.parse(src)
        fast: set[str] = set()
        slow: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if not names & {"_FAST_LAYER", "_SLOW_LAYER"}:
                    continue
                arg = node.value.args[0] if isinstance(node.value, ast.Call) and node.value.args else None
                if isinstance(arg, ast.Set):
                    vals = {e.value for e in arg.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                    (fast if "_FAST_LAYER" in names else slow).update(vals)
        assert not (fast & slow), f"快门禁层与慢层交叠：{sorted(fast & slow)}"


# ══════════════════════════════════════════════════════════════════
# 余集档位的存在性（防"静默清空"，也防"断言 0"这种脆判据）
# ══════════════════════════════════════════════════════════════════
#
# `make test-remainder` = Normal − Quick。2026-09-17（Batch 2.2）起它是 **0 条**：
# 原先那 3 条 `integration`-only 用例实际是**外部依赖**（真实 DeepSeek 网络调用、
# 真实 Milvus + 真嵌入），却因表达式恰好落在 Normal 里而在 push 门禁中执行——
# 而 `make test-normal` 被文档描述为"全量离线"。改正 marker 为 `external` 后
# Normal 真正离线，余集随之空掉。
#
# 这条**刻意不断言"恰好 N 条"**：那正是本项目反复批评的脆判据（"断言某个缺陷
# 存在"的用例必须在缺陷修好那一刻失效）。这里钉的是**性质**，不是数字：
# 空是允许的，非空时档位必须仍然自洽。


class TestRemainderTierExistence:
    def test_zero_remainder_is_allowed_and_consistent(self, cts):
        """余集为空是合法的健康态：此时 Normal == Quick。"""
        quick = _s("a", "b")
        code, lines = cts.judge(quick, 5, set(quick), 5, _s(), 5)
        assert code == 0, lines

    def test_nonempty_remainder_is_also_healthy(self, cts):
        """余集非空同样必须绿——档位存在与否不是判据，自洽才是。"""
        code, lines = cts.judge(_s("a"), 5, _s("a", "b"), 5, _s("b"), 5)
        assert code == 0, lines

    def test_make_target_survives_so_the_slot_exists(self):
        """`make test-remainder` 目标必须仍然存在。

        它是"Quick/remainder 互斥"这条前提的**可运行定义**，也是日后又有用例落进
        Normal−Quick 时的现成执行点。余集当前为空不代表可以删掉这个目标——
        删了它，下一次 drift 就没有承接点，而漂移本身不会报错。
        """
        text = (_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "\ntest-remainder:" in text, (
            "`make test-remainder` 目标不见了。它的作用不是「跑那几条用例」，"
            "而是让 Quick/Normal 的互斥余集**有定义、有执行点**——"
            "当前余集为 0 条，但删掉目标就等于删掉这条前提。"
        )
