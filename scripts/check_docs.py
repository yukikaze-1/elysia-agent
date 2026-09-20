#!/usr/bin/env python3
"""文档漂移守卫（DG1–DG5）—— canonical / supporting 文档的防回归网。

**为什么存在**：2026-09-20 的文档整理把 authority surface 收口了一次，但"整理过一次"
不等于"以后不会再漂"。本脚本把当时手工发现的那几类漂移变成可执行判据：

| 判据 | 防的是什么 |
|---|---|
| **DG1** 内部 Markdown 链接可解析 | canonical 文档链到不存在的文件（"读者点进去 404"） |
| **DG2** 禁止引用**已知不存在**的符号/路径（`frontend_contract/ws_message.py`、`core/task/`、`TaskScheduler`） | 文档停留在某次重构之前（这三条都是真实发生过的：ws_message 已拆包、core/task 已改名） |
| **DG3** 协议错误码目录 ↔ `frontend_contract/errors.py` 双向一致 | **委托**给既有 `scripts/check_error_codes.py`（不建第二套 checker） |
| **DG4** `core/` 新子系统必须有 README | 约定写在文档里、实际没人执行（"要求"退化成愿望） |
| **DG6** 代码围栏成对（标题不被吞进代码块） | 漏写闭合围栏 ⇒ 章节标题被渲染成代码、锚点失效，而链接检查看不见 |
| **DG5** 文档里的**机器可导出的**事实与代码一致 | 手抄的清单/计数静默过期（AppConfig 环境变量索引） |

**刻意不做的事**（否则它会变成一个没人维护的巨型 linter）：
- **不扫历史语料**（`docs/review/**`、`docs/archive/**`、`docs/superpowers/**`、
  `docs/progress/**`、`docs/completed/**`、`docs/legacy/**`）——历史文档允许保留
  **当时的**路径（见 `docs/README.md` 的 Historical Records 节）。对 500+ 份历史正文
  强制当代链接，等于逼人改写历史。**两个例外**：各历史目录的 `README.md`（它们是
  **current** 导航）+ `docs/archive/frontend-handoff-v2/traces/README.md`
  （**live** 契约产物的清单）在扫描面内。
- **不做 Markdown 风格检查**、**不查拼写**、**不引入第三方依赖**（纯 stdlib）。

用法：

    python scripts/check_docs.py            # 全量判据
    python scripts/check_docs.py --list     # 打印扫描范围与登记表（不改判据）

退出码：0 = 全部通过；1 = 有违规；2 = **拒答**（扫描面本身不成立，例如关键文件缺失——
按本仓"假绿③：环境不满足时必须拒答"的纪律，此时**不打印通过标记**）。
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 扫描面：CANONICAL + SUPPORTING（当前文档）
# ---------------------------------------------------------------------------

CANONICAL: tuple[str, ...] = (
    "README.md",
    "CLAUDE.md",
    "todo.md",
    "docs/README.md",
    "docs/architecture.md",
    "docs/architecture/README.md",
    "docs/architecture/invariants.md",
    "docs/architecture/dead-code-review-checklist.md",
    "docs/testing.md",
    "docs/ci.md",
    "docs/operations.md",
    "docs/decisions.md",
    "docs/hardening-program.md",
    "docs/database-storage.md",
    "docs/electron-backend-protocol-v2.md",
    "docs/electron-backend-security.md",
    "docs/credential-handling.md",
    "docs/code-style-guide.md",
    "docs/readme-conventions.md",
    "docs/completed.md",
    "frontend_contract/README.md",
    # live 契约产物的**清单**（docs/README.md 指定它为 traces 的权威说明）。
    # 不扫它 = 跨仓消费的那批帧的说明无人守（Challenger 2026-09-20 指出的洞：
    # 它的帧数已经与文件实际不符过一次）。
    "docs/archive/frontend-handoff-v2/traces/README.md",
)

# 历史目录的**索引**（它们自身是 current 的导航，故纳入；其正文不纳入）。
CANONICAL_INDEXES: tuple[str, ...] = (
    "docs/review/README.md",
    "docs/archive/README.md",
    "docs/superpowers/README.md",
)

# SUPPORTING = 包/子系统 README（当前文档，跟随代码演进）。
_SUPPORTING_GLOBS: tuple[str, ...] = (
    "core/README.md",
    "core/*/README.md",
    "core/tools/*/README.md",
    "Module/README.md",
    "Module/*/README.md",
    "Module/*/*/README.md",
    "Module/*/docs/*.md",
    "Module/system_prompt/templates/README.md",
    "scripts/README.md",
    "character/README.md",
    "frontend/README.md",
    "log/README.md",
    "static/debug.md",
)

# 明确排除（历史语料 / 生成产物 / 台账），附理由——**登记陈旧也会红**（见 _check_stale）。
_SCAN_EXCLUSIONS: dict[str, str] = {
    "CODE_REVIEW_REPORT.md": "历史审计台账（460KB，含 owner 决策），正文保留当时的路径；见 docs/README.md 的登记",
    "docs/2026-08-29-task-system-overall-architecture-design.md": "⚠️ UNIMPLEMENTED DESIGN DRAFT，与 elysia-frontend 跨仓镜像，禁止改写正文",
    "docs/2026-08-29-task-system-backend-architecture-design.md": "⚠️ UNIMPLEMENTED DESIGN DRAFT，同上",
    "docs/2026-08-30-provider-model-runtime-backend-architecture-design.md": "⚠️ UNIMPLEMENTED DESIGN DRAFT",
    "docs/2026-08-30-character-prompt-customization-backend-architecture-design.md": "设计稿（Stage A–H 已实施，正文保留设计时口径）",
    "Module/workflow_engine/docs/implementation.md": "历史实现记录（2026-07 重构时的实现说明，正文保留当时的 `TaskScheduler` 命名）；当前 API 权威是 docs/api.md",
}

# ---------------------------------------------------------------------------
# DG2：已知不存在的符号 / 路径
# ---------------------------------------------------------------------------

# 每条 = (正则, 一句话说明它为什么不存在、现在该写什么)
_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"frontend_contract/ws_message\.py"),
        "单文件已拆成 `frontend_contract/ws_message/` 包（ARCH-INC-P2-02，9 模块）",
    ),
    (
        re.compile(r"\bcore/task/"),
        "已改名为 `core/tool_runtime/`（ARCH-INC-P1-01）",
    ),
    (
        re.compile(r"\bTaskScheduler\b"),
        "已改名为 `ToolCallDispatcher`（ARCH-INC-P1-01）",
    ),
)

# 行内出现这些标记即视为**显式历史语境**（讲"以前叫什么/已归档/当时"），允许保留旧名。
_HISTORICAL_MARKERS: tuple[str, ...] = (
    "历史", "当时", "已归档", "归档", "改名", "重命名", "原 ", "原名", "原单文件",
    "原路径", "已废弃", "废弃", "旧 |", "不再", "拆分", "split",
    "historical", "superseded", "legacy", "LEGACY",
    "→",  # 改名映射（`core/task/` → `core/tool_runtime/`）
)

#: 判定历史语境时一并看**上文两行**：改名映射表的数据行本身通常不含标记词，
#: 标记在它的表头（`| 旧 | 新 |`）或引导句里。只看本行会把这些行误判为"失效引用"。
_MARKER_LOOKBACK = 6

# 文件级豁免：整个文件都在讲"旧名与新名的关系"。**陈旧也会红**。
_DG2_FILE_EXEMPTIONS: dict[str, str] = {
    "core/tool_runtime/README.md": "本包 README 含「改名映射」表（`core/task/` → `core/tool_runtime/`）",
}

# ---------------------------------------------------------------------------
# DG4：core/ 子系统 README 覆盖
# ---------------------------------------------------------------------------

# `core/<name>/` 里**有生产 .py** 者视为子系统，必须有 README。
# 豁免必须写理由；**豁免项已经有 README 时会红**（防止豁免单变成永久垃圾场）。
_DG4_EXEMPTIONS: dict[str, str] = {
    "core/tests": "测试套件（仅测试代码，不是 runtime 子系统）；测试组织见 docs/testing.md",
}

# ---------------------------------------------------------------------------
# DG5：机器可导出的事实
# ---------------------------------------------------------------------------


def _link_scan_targets() -> list[str]:
    """返回 DG1/DG2 要扫的文件（相对路径，去重且存在）。"""
    out: list[str] = []
    seen: set[str] = set()
    for rel in (*CANONICAL, *CANONICAL_INDEXES):
        if rel in _SCAN_EXCLUSIONS:
            continue
        if (_ROOT / rel).is_file() and rel not in seen:
            seen.add(rel)
            out.append(rel)
    for pattern in _SUPPORTING_GLOBS:
        for path in sorted(_ROOT.glob(pattern)):
            rel = str(path.relative_to(_ROOT))
            if not path.is_file() or rel in seen or rel in _SCAN_EXCLUSIONS:
                continue
            seen.add(rel)
            out.append(rel)
    return out


_FENCE_LINE_RE = re.compile(r"^\s{0,3}```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _mask_fences(text: str) -> str:
    """只遮蔽**围栏代码块**（保留行内 code span，供 DG7 提取路径）。"""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            out.append(_blank(line))
            continue
        out.append(_blank(line) if in_fence else line)
    return "".join(out)


def _blank(s: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in s)


def _mask_non_link_regions(text: str) -> str:
    """把**代码区**（围栏块 + 行内 code span）涂成等长空白，保留换行与偏移。

    两处都不是链接，但都会被朴素的 link 正则命中：
      - 围栏代码块：复制即用的示例片段（里面的相对路径属于**示例**）；
      - 行内 `` `[项目文档](../../README.md)` ``：文法是**示例**——读者点不动它，
        它是"面包屑长什么样"的展示（`docs/readme-conventions.md` 的深度对照表）。
    不涂会把示例判成坏链（假红），而假红会逼人删掉有价值的内容。
    **等长替换**：这样报告的行号仍是原文行号。

    逐行 toggle 而不是 ```` ```.*?``` ```` 配对：Markdown 允许围栏缩进最多 3 格
    （`docs/architecture.md` 里就有一对缩进围栏），正则配对会错位一格，
    把真实的标题/链接整段遮掉 ⇒ 判据看不见它该看的东西（假绿②）。
    """
    return _INLINE_CODE_RE.sub(lambda m: _blank(m.group(0)), _mask_fences(text))


def _resolve(base_dir: Path, target: str) -> Path | None:
    raw = target.split("#", 1)[0].strip()
    if not raw or raw.startswith(("http://", "https://", "mailto:", "#")):
        return None
    return (base_dir / raw).resolve()


_ANCHOR_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _heading_slugs(path: Path) -> set[str]:
    """把 Markdown 标题转成 GitHub 风格 anchor slug（够用即可，不求完备）。

    规则（GitHub 实际行为）：小写 → 去掉标点（保留字母数字、空格、连字符、下划线、
    CJK）→ 空格换 `-`。**不处理重复标题的 `-1` 后缀**：本仓没有重复标题，
    真出现时本判据会误报一次，那时再补。
    """
    text = _mask_fences(path.read_text(encoding="utf-8"))
    slugs: set[str] = set()
    for _, title in _ANCHOR_RE.findall(text):
        # 标题里的链接语法只保留链接文字，code span 去掉反引号
        title = _MD_LINK_RE.sub(lambda m: m.group(0).split("]")[0].lstrip("["), title)
        title = _CODE_SPAN_RE.sub(lambda m: m.group(0).strip("`"), title)
        slug = title.strip().lower()
        slug = re.sub(r"[^\w\s\-\u4e00-\u9fff]", "", slug)
        slug = slug.strip().replace(" ", "-")
        if slug:
            slugs.add(slug)
    return slugs


def check_dg1_anchors(files: list[str]) -> list[str]:
    """DG1b：文内/跨文 `#anchor` 必须落在目标文件真实存在的标题上。

    为什么单独一条：`_resolve()` 会把 `#anchor` 剥掉只验文件存在，于是
    "文件在、锚点拼错"这种断链**完全看不见**（Challenger 2026-09-20 反证：
    `docs/architecture.md` 里指向 4.10 的锚点在标题加了" + "之后失效，
    而守卫照绿）。点击无效与 404 对读者是同一件事。
    """
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}
    for rel in files:
        path = _ROOT / rel
        text = _mask_non_link_regions(path.read_text(encoding="utf-8"))
        own_slugs: set[str] | None = None
        for m in _LINK_RE.finditer(text):
            raw = m.group(1).strip()
            if "#" not in raw or raw.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, anchor = raw.partition("#")
            if not anchor:
                continue
            anchor = anchor.strip()
            if target:
                resolved = _resolve(path.parent, target)
                if resolved is None or not resolved.is_file() or resolved.suffix != ".md":
                    continue  # 文件本身的问题由 DG1 报，这里只管锚点
                slugs = cache.setdefault(resolved, _heading_slugs(resolved))
            else:
                if own_slugs is None:
                    own_slugs = _heading_slugs(path)
                slugs = own_slugs
            if anchor.lower() not in slugs:
                line = text[: m.start()].count("\n") + 1
                problems.append(
                    f"{rel}:{line} 锚点不存在：`{raw}`（目标里没有该标题的 slug）"
                )
    return problems


def check_dg1(files: list[str]) -> list[str]:
    """DG1：canonical / supporting 文档的内部 Markdown 链接必须可解析。"""
    problems: list[str] = []
    for rel in files:
        path = _ROOT / rel
        text = _mask_non_link_regions(path.read_text(encoding="utf-8"))
        for m in _LINK_RE.finditer(text):
            target = m.group(1)
            resolved = _resolve(path.parent, target)
            if resolved is None:
                continue
            if not resolved.exists():
                line = text[: m.start()].count("\n") + 1
                problems.append(f"{rel}:{line} 链接指向不存在的路径：{target}")
    return problems


def check_dg2(files: list[str]) -> list[str]:
    """DG2：不得引用已知不存在的符号/路径，除非在显式历史语境里。"""
    problems: list[str] = []
    for rel in files:
        exempt = rel in _DG2_FILE_EXEMPTIONS
        lines = (_ROOT / rel).read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate((_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            context = "\n".join(lines[max(0, lineno - 1 - _MARKER_LOOKBACK):lineno])
            for pattern, why in _FORBIDDEN:
                if not pattern.search(line):
                    continue
                if exempt or any(marker in context for marker in _HISTORICAL_MARKERS):
                    continue
                problems.append(
                    f"{rel}:{lineno} 引用了不存在的东西 `{pattern.pattern}`（{why}）：{line.strip()[:120]}"
                )
    return problems


def check_dg6(files: list[str]) -> list[str]:
    """DG6：代码围栏必须成对，且**闭合围栏不得带 info string**。

    为什么这条值得单独存在（2026-09-20 Challenger 顺带挖出来的真缺陷）：
    `docs/architecture.md` 的 ```` ```yaml ```` 块**漏写闭合围栏**，而紧随其后的是
    ```` ```python ````（带 info string ⇒ 按 CommonMark 它**不能**充当闭合围栏）。
    后果是 `## 8` / `## 9` / `## 10` 三节的标题**被当成代码渲染**——章节在正文里
    消失、锚点全部失效，而**任何链接检查都看不见**（链接指向的文件确实存在）。
    这类缺陷只有"围栏配平"能发现，且它同时解释了"锚点为何不存在"。
    """
    problems: list[str] = []
    for rel in files:
        state = False
        opener = 0
        for lineno, line in enumerate((_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if not _FENCE_LINE_RE.match(line):
                continue
            info = line.strip()[3:].strip()
            if not state:
                state, opener = True, lineno
            else:
                if info:
                    problems.append(
                        f"{rel}:{lineno} 闭合围栏带了 info string（`{line.strip()}`）——"
                        f"按 CommonMark 它不能闭合第 {opener} 行打开的块；"
                        f"这通常意味着缺一个闭合围栏，或此处本应是起始围栏"
                    )
                state = False
        if state:
            problems.append(
                f"{rel}:{opener} 的代码围栏没有闭合——其后内容会被当成代码渲染"
                f"（标题消失、锚点失效），而链接检查看不见这类缺陷"
            )
    return problems


# 已知的**非仓库内**路径（运行时产物 / 外部仓 / 生成物），逐条写理由。**陈旧也会红**。
_PATH_ALLOWLIST: dict[str, str] = {
    "data/task_result_wal.jsonl": "运行时产物（gitignore），由 ProjectPath 生成",
    "data/session_pending.jsonl": "运行时产物（gitignore）",
    "data/token_usage_pending.jsonl": "运行时产物（gitignore）",
    "data/scheduler_checkpoint.json": "运行时产物（gitignore，P3-01-RESERVED 预留）",
    "data/memory_pending_syncs.json": "运行时产物（gitignore）",
    "data/memory_pending_deletes.json": "运行时产物（gitignore）",
    "data/session_pending_cleanup.json": "运行时产物（gitignore）",
    "data/pending_attachment_release.json": "运行时产物（gitignore）",
    "data/secrets/profiles.json": "运行时密钥存储（0600 + gitignore）",
    "data/secrets/": "运行时密钥目录（gitignore；SecretStore 落盘处）",
    "data/live2d-assets": "运行时资源目录（gitignore；ProjectPath.LIVE2D_ASSETS）",
    "Module/llm/errors.py": "**KB-14 描述的正是这个文件不存在**（`core/tests/fake_llm.py` import 它但从未被调用）——todo 条目按原样保留该路径是判据本身",
    "elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs": "**外部仓**（姊妹前端仓）的路径；本仓内不存在是正常的",
}

# 形如路径的 inline code：带已知顶层目录前缀，或以**已知扩展名**结尾且含 `/`。
_PATH_PREFIXES = ("core/", "Module/", "frontend_contract/", "frontend/", "scripts/",
                  "docs/", "tests/", "character/", "static/", "log/", "data/",
                  ".github/", "elysia-frontend/")
_PATH_EXT_RE = re.compile(
    r"^[\w./\-]+\.(py|md|json|jsonl|ya?ml|ts|vue|toml|sh|cfg|ini|j2|css|html|txt)$"
)
_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")
#: 文档**示例**里的占位路径（不是真实引用）：`Module/tts/your_backend.py`、`path/to/file.py`…
_PATH_PLACEHOLDER_RE = re.compile(r"(your_|path/to/|/to/|foo/|xxx|示例)")


def check_dg7(files: list[str]) -> list[str]:
    """DG7：行内 code 里**像仓库路径**的引用必须能解析到真实文件/目录。

    为什么需要它（Challenger 2026-09-20 的反证）：DG2 只认三条硬编码模式，于是
    `docs/testing.md` 的 `task/tests/`（目录早改名 `tool_runtime/`）与 `CLAUDE.md` 的
    `core/tests/test_session_cascade.py`（真实位置在 `core/memory/tests/`）**双双通过**——
    两条都会让读者按图索骥 404，而 DG1/DG2 都看不见。同一轮还查出了
    `core/input/README.md` 里 5 处**已经搬到别的包**的文件路径。

    **判据刻意收窄**（第一版按"看起来像路径"实现，271 条命中里绝大多数是假阳性——
    假红会逼人删掉有价值的内容，与不做守卫同样糟）：

    - 只查**去掉 `::symbol` 后的路径部分**，且必须含 `/`；
    - 以**已知顶层目录**开头，或以**已知扩展名**结尾；
    - 含占位符（`<` `>` `*` `{` `}` `…` `$`）的一律跳过；
    - 解析候选 = 本文档所在目录 + 其每一级祖先（覆盖"根相对"与"包相对"两种写法）；
    - 带历史标记（`原` / `改名` / `→` …）的行整行跳过；
    - 运行时产物与外部仓路径在 `_PATH_ALLOWLIST` 逐条登记理由（**陈旧也会红**）。
    """
    problems: list[str] = []
    for rel in files:
        doc_dir = (_ROOT / rel).parent
        candidates_dirs = [doc_dir, *doc_dir.parents]
        text = _mask_fences((_ROOT / rel).read_text(encoding="utf-8"))
        doc_lines = text.splitlines()
        for lineno, line in enumerate(doc_lines, 1):
            context = "\n".join(doc_lines[max(0, lineno - 1 - _MARKER_LOOKBACK):lineno])
            if any(marker in context for marker in _HISTORICAL_MARKERS):
                continue
            for span in _INLINE_SPAN_RE.findall(line):
                tok = span.strip().strip("`").strip()
                tok = re.split(r"\s*::\s*", tok, maxsplit=1)[0].strip()
                tok = tok.rstrip("：:，,。；;)）】」")
                if not tok or tok.startswith(("./", "/", "~")) or ".." in tok:
                    continue
                if any(ch in tok for ch in "<>*{}…$|=`"):
                    continue
                if _PATH_PLACEHOLDER_RE.search(tok):
                    continue  # 示例 / 模板占位路径
                if "/" not in tok:
                    continue
                if not (tok.startswith(_PATH_PREFIXES) or _PATH_EXT_RE.match(tok)):
                    continue
                if tok in _PATH_ALLOWLIST:
                    continue
                bare = tok.rstrip("/")
                if any((d / bare).exists() for d in candidates_dirs):
                    continue
                problems.append(
                    f"{rel}:{lineno} 引用了解析不到的仓库路径 `{tok}`（已按文档目录及其各级祖先解析）—— "
                    f"读者按它去找会 404；若它本就是运行时/外部路径，"
                    f"请登记进 scripts/check_docs.py 的 _PATH_ALLOWLIST 并写明理由"
                )
    return problems


def check_dg3() -> list[str]:
    """DG3：错误码目录 ↔ errors.py 双向一致——**委托**既有 checker，不建第二套。"""
    script = _ROOT / "scripts" / "check_error_codes.py"
    if not script.is_file():
        return ["scripts/check_error_codes.py 不存在——DG3 无判据（拒答，不是通过）"]
    proc = subprocess.run(
        [sys.executable, str(script)], cwd=_ROOT, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return [f"错误码双向比对失败（check_error_codes.py 退出 {proc.returncode}）："
                f"{(proc.stdout + proc.stderr).strip()[:400]}"]
    return []


def check_dg4() -> list[str]:
    """DG4：`core/` 下每个子系统必须有 README.md（豁免须写理由，且豁免项不得已有 README）。"""
    problems: list[str] = []
    for child in sorted((_ROOT / "core").iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        rel = f"core/{child.name}"
        has_readme = (child / "README.md").is_file()
        has_prod_py = any(
            p.suffix == ".py" and p.name != "__init__.py"
            for p in child.rglob("*.py")
            if "__pycache__" not in p.parts and "tests" not in p.parts
        )
        if not has_prod_py:
            continue  # 纯测试/纯资源目录不算子系统
        if rel in _DG4_EXEMPTIONS:
            if has_readme:
                problems.append(
                    f"{rel} 已在 _DG4_EXEMPTIONS 登记豁免（{_DG4_EXEMPTIONS[rel]}），"
                    f"但它**已经有 README** —— 请删除该豁免条目（登记陈旧 = 豁免理由在冒充事实）"
                )
            continue
        if not has_readme:
            problems.append(
                f"{rel}/ 是 core 子系统（含生产代码）但没有 README.md —— "
                f"按 docs/readme-conventions.md「core/ 下新子系统必须」；"
                f"若确不该有，请在 scripts/check_docs.py 的 _DG4_EXEMPTIONS 登记**理由**"
            )
    return problems


def _appconfig_env_names() -> list[str]:
    """从 `core/config/app_config.py` 的 AST 导出 env 名（不 import，避免拉起运行时依赖）。

    规则与 `Module/config/loader.py` 一致：`ConfigField(env="X")` 显式优先，
    否则 `(父字段名_ + 字段名).upper()`。
    """
    src = (_ROOT / "core" / "config" / "app_config.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def env_of(node: ast.expr | None, fallback: str) -> str:
        """取 `ConfigField(env="X")` 的显式名；没有则回退到派生名。"""
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "env" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
        return fallback.upper()

    def walk(name: str, prefix: str = "") -> list[str]:
        cls = classes.get(name)
        if cls is None:
            return []
        out: list[str] = []
        for stmt in cls.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            field = stmt.target.id
            value = stmt.value
            nested: str | None = None
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "ConfigField":
                for kw in value.keywords:  # 嵌套 dataclass：`default_factory=AgentConfig`
                    if kw.arg == "default_factory" and isinstance(kw.value, ast.Name):
                        nested = kw.value.id
            elif isinstance(value, ast.Name) and value.id in classes:
                nested = value.id
            if nested is not None:
                # **容器字段本身没有 env**：loader 走 `_is_dataclass_instance` 分支时
                # 只递归、`continue`（不 `os.getenv`）。这里若把它当成 env 名，
                # 会凭空要求文档登记 `AGENT`/`SERVER` 这类不存在的变量。
                out.extend(walk(nested, prefix + field + "_"))
            else:
                out.append(env_of(value, prefix + field))
        return out

    return sorted({n for n in walk("AppConfig") if n})


def check_dg5() -> list[str]:
    """DG5：`docs/operations.md` 的环境变量索引必须覆盖 AppConfig 的全部 env 名。

    canonical 文档里"机器可导出的事实"不该是手抄的——手抄的那份不会被任何门禁发现。
    这里只覆盖**从代码机械导出**的这一类（property 计数由 `core/tests/test_doc_counts.py` 管）。
    """
    problems: list[str] = []
    doc = _ROOT / "docs" / "operations.md"
    if not doc.is_file():
        return ["docs/operations.md 不存在——DG5 无判据（拒答，不是通过）"]
    text = doc.read_text(encoding="utf-8")
    names = _appconfig_env_names()
    if not names:
        return ["无法从 core/config/app_config.py 导出任何 env 名——判据不成立（拒答）"]
    # **词边界匹配**，不是子串：`TTS_RATE_LIMIT_RPM` 是 `TTS_RATE_LIMIT_RPM_X` 的子串，
    # 用 `in` 会让"名字被改坏只剩近似串"这种情况静默通过（守卫假绿②：判据判断的不是它声称的东西）。
    missing = [n for n in names if not re.search(rf'\b{re.escape(n)}\b', text)]
    if missing:
        problems.append(
            "docs/operations.md 的「环境变量索引」缺少 AppConfig 里真实存在的 env 名："
            + ", ".join(missing)
            + "（新增/改名配置字段后请同步该表；该表由本判据钉住，见 docs/operations.md §3.1）"
        )
    return problems


def check_stale_registrations() -> list[str]:
    """登记陈旧也会红：豁免/排除项若已无必要，说明理由在冒充事实。"""
    problems: list[str] = []
    for rel, why in _DG2_FILE_EXEMPTIONS.items():
        path = _ROOT / rel
        if not path.is_file():
            problems.append(f"_DG2_FILE_EXEMPTIONS 登记了 {rel}（{why}），但文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        if not any(p.search(text) for p, _ in _FORBIDDEN):
            problems.append(
                f"_DG2_FILE_EXEMPTIONS 登记了 {rel}（{why}），但该文件已不含任何被禁模式 —— 请删除登记"
            )
    for rel, why in _SCAN_EXCLUSIONS.items():
        if not (_ROOT / rel).is_file():
            problems.append(f"_SCAN_EXCLUSIONS 登记了 {rel}（{why}），但文件不存在")
    for tok, why in _PATH_ALLOWLIST.items():
        if (_ROOT / tok).exists():
            problems.append(
                f"_PATH_ALLOWLIST 登记了 `{tok}`（{why}），但它在仓库里**存在** —— "
                f"请删除该条目（登记陈旧 = 理由在冒充事实）"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="文档漂移守卫（DG1–DG5）")
    parser.add_argument("--list", action="store_true", help="打印扫描范围与登记表后退出")
    args = parser.parse_args()

    files = _link_scan_targets()
    if args.list:
        print(f"扫描面（DG1/DG2）：{len(files)} 份 current 文档")
        for rel in files:
            print(f"  {rel}")
        print("\n不扫（历史语料）的目录前缀：docs/review/ docs/archive/ docs/superpowers/ "
              "docs/progress/ docs/completed/ docs/legacy/")
        print("\n_SCAN_EXCLUSIONS：")
        for rel, why in _SCAN_EXCLUSIONS.items():
            print(f"  {rel} — {why}")
        print("\n_DG2_FILE_EXEMPTIONS：")
        for rel, why in _DG2_FILE_EXEMPTIONS.items():
            print(f"  {rel} — {why}")
        print("\n_DG4_EXEMPTIONS：")
        for rel, why in _DG4_EXEMPTIONS.items():
            print(f"  {rel} — {why}")
        print(f"\nDG5：AppConfig env 名 {len(_appconfig_env_names())} 个，须全部出现在 docs/operations.md")
        return 0

    # 拒答条件：扫描面不成立时非零退出且**不打印通过标记**（假绿③）。
    if not files:
        print("FATAL: 扫描面为空——脚本或仓库布局已变，判据不成立（拒答）", file=sys.stderr)
        return 2

    failures: list[tuple[str, list[str]]] = [
        ("DG1 canonical/supporting 内部链接", check_dg1(files)),
        ("DG1b 文内锚点（#heading）可解析", check_dg1_anchors(files)),
        ("DG2 禁止引用已知不存在的符号/路径", check_dg2(files)),
        ("DG3 协议错误码目录 ↔ errors.py", check_dg3()),
        ("DG6 代码围栏成对", check_dg6(files)),
        ("DG7 行内 code 里的仓库路径存在", check_dg7(files)),
        ("DG4 core/ 子系统 README 覆盖", check_dg4()),
        ("DG5 AppConfig 环境变量索引一致", check_dg5()),
        ("DG* 登记陈旧", check_stale_registrations()),
    ]

    bad = [(name, probs) for name, probs in failures if probs]
    if bad:
        for name, probs in bad:
            print(f"[FAIL] {name}", file=sys.stderr)
            for p in probs:
                print(f"       {p}", file=sys.stderr)
        print(f"\n文档守卫失败：{sum(len(p) for _, p in bad)} 项", file=sys.stderr)
        return 1

    print(
        f"文档守卫通过：DG1（{len(files)} 份文档内部链接与锚点可解析）/ "
        f"DG2（无失效引用）/ DG3（错误码双向一致）/ "
        f"DG4（core 子系统 README 覆盖）/ DG5（{len(_appconfig_env_names())} 个 AppConfig env 名在册）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
