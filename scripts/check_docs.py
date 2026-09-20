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
import os
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


#: 引用的四种身份（§12）：只有第一类需要"仓库真值"成立，其余三类是**显式分类**。
PATH_KIND_REPOSITORY = "REPOSITORY_PATH"
PATH_KIND_RUNTIME = "RUNTIME_PATH"
PATH_KIND_GENERATED = "GENERATED_PATH"
PATH_KIND_EXTERNAL = "EXTERNAL_PATH"

#: **非仓库路径分类表**（exact 或目录前缀，逐条带理由与证据）。
#:
#: 判定语义（**2026-09-21 clean-checkout 修复**）：DG7 对仓库引用必须问**仓库真值**
#: （`git ls-files`），**不再**用 `Path.exists()`——否则只存在于开发者工作区的
#: untracked/gitignored 文件会让引用假绿（真实 CI 在干净 checkout 里报了 8 条）。
#: 本表登记的是"**有意不在仓库里**"的路径：它们由运行期创建或构建期生成，
#: 干净 checkout 中**本就应该缺席**，所以豁免不要求它们存在。
#:
#: 纪律：exact / 精确目录前缀，逐条写理由与证据，**且不得覆盖任何被跟踪的树**。
#: 禁止出现 `tmp/`：临时探针路径不该被 canonical 文档引用（应改引稳定来源）。
#: 陈旧判定见 `check_stale_registrations`——**"干净 checkout 里不存在"不算陈旧**
#: （否则会形成"运行时路径必须物理存在"的悖论）。
_NON_REPOSITORY_PATHS: dict[str, tuple[str, str]] = {
    # --- 外部仓（姊妹前端仓）：本仓内不存在是正常的 ---
    "elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs": (
        PATH_KIND_EXTERNAL,
        "**外部仓**（elysia-frontend）路径；本仓不存在是正常的",
    ),
    # --- 运行期数据根（ProjectPath.DATA 下的运行期产物；见 .gitignore 第 19-24 行） ---
    "data/uploads/": (
        PATH_KIND_RUNTIME,
        "运行期上传目录（`ProjectPath.DATA_UPLOADS`；core/input/upload_routes.py 写入）",
    ),
    "data/cache/vision/": (
        PATH_KIND_RUNTIME,
        "运行期视觉缓存（`ProjectPath.DATA_CACHE_VISION`；core/tools/vision/plugin.py 写入）",
    ),
    "data/secrets/": (
        PATH_KIND_RUNTIME,
        "运行期密钥存储目录（SecretStore 落盘处，0600）",
    ),
    "data/live2d-assets": (
        PATH_KIND_RUNTIME,
        "运行期资源目录（`ProjectPath.LIVE2D_ASSETS`）",
    ),
    "data/token_usage_pending.jsonl": (PATH_KIND_RUNTIME, "运行期 journal（TokenTracker 落盘）"),
    "data/scheduler_checkpoint.json": (PATH_KIND_RUNTIME, "运行期 journal（P3-01-RESERVED 预留）"),
    "data/memory_pending_syncs.json": (PATH_KIND_RUNTIME, "运行期 journal（Milvus 离线待写）"),
    "data/memory_pending_deletes.json": (PATH_KIND_RUNTIME, "运行期 journal（Milvus 离线待删）"),
    "data/session_pending_cleanup.json": (PATH_KIND_RUNTIME, "运行期 journal（会话清理重试）"),
    "data/pending_attachment_release.json": (PATH_KIND_RUNTIME, "运行期 journal（附件引用释放重试）"),
    "data/consolidator_pending.json": (PATH_KIND_RUNTIME, "运行期 journal（未整合对话缓冲）"),
    "data/task_result_wal.jsonl": (PATH_KIND_RUNTIME, "运行期 WAL（后台任务终态，fsync）"),
    "data/secrets/profiles.json": (PATH_KIND_RUNTIME, "运行期密钥文件（0600 + gitignore）"),
    # --- 构建产物（Vite 输出到 static/；见 frontend/vite.config.ts 的 outDir） ---
    "static/index.html": (PATH_KIND_GENERATED, "Vite 构建产物（`npm run build` 写出；.gitignore 第 10 行）"),
    "static/debug.html": (PATH_KIND_GENERATED, "Vite 构建产物（多入口第二页；.gitignore 第 11 行）"),
    "static/assets/": (PATH_KIND_GENERATED, "Vite 构建产物目录（.gitignore 第 12 行）"),
    # --- 描述"某个文件不存在"的判据本身 ---
    "Module/llm/errors.py": (
        PATH_KIND_RUNTIME,
        "**KB-14 描述的正是这个文件不存在**（`core/tests/fake_llm.py` import 它但从未被调用）",
    ),
}

#: 被跟踪路径的祖先目录也算"在仓库里"（`core/agent/` 没有独立文件但确实存在）。
_TRACKED_CACHE: dict[str, frozenset[str]] = {}


#: 显式跟踪清单的输入通道（`git ls-files` 的输出，每行一条）。
#: 用于**没有 `.git` 的树**（`git archive` 解出的目录、源码 tarball）——
#: 那些环境里 git 判据无从取得，但**仍然不允许退回文件系统判据**，
#: 所以要求调用方**显式喂入仓库真值**（`--tracked-from <file>` 或本环境变量）。
#: 不提供 ⇒ 拒答（exit 2）。
_TRACKED_FROM_ENV = "ELYSIA_TRACKED_PATHS"


def _inventory_from_listing(entries: set[str]) -> frozenset[str]:
    """由**文件清单**导出"文件 + 其各级祖先目录"（目录也算在仓库里）。"""
    dirs: set[str] = set()
    for path in entries:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return frozenset(entries | dirs)


def _tracked_inventory(
    root: Path | None = None, tracked_from: Path | None = None
) -> tuple[str, ...]:
    """返回**仓库真值**里的路径（文件 + 其各级祖先目录）。

    权威来源，按优先级：
    1. `--tracked-from <file>` / `ELYSIA_TRACKED_PATHS`（显式清单，供无 `.git` 的树）；
    2. `git ls-files`（开发者工作区 / 干净 clone / CI checkout —— 正常路径）。

    这是 DG7 唯一的"存在性"权威：工作区文件系统可以被 untracked/gitignored 文件污染，
    `git ls-files` 不会。两者都取不到就抛 `RuntimeError`——调用方必须**拒答**，
    **绝不**退回文件系统判据（那正是 2026-09-21 clean-checkout 假绿的成因）。
    """
    root = root or _ROOT
    key = f"{root}|{tracked_from}|{os.environ.get(_TRACKED_FROM_ENV, '')}"
    if key in _TRACKED_CACHE:
        return tuple(sorted(_TRACKED_CACHE[key]))

    explicit = tracked_from
    if explicit is None:
        env_value = os.environ.get(_TRACKED_FROM_ENV, "").strip()
        explicit = Path(env_value) if env_value else None
    if explicit is not None:
        if not explicit.is_file():
            raise RuntimeError(
                f"显式跟踪清单 {explicit} 不存在（{_TRACKED_FROM_ENV} / --tracked-from）——拒答"
            )
        entries = {ln.strip() for ln in explicit.read_text(encoding="utf-8").splitlines() if ln.strip()}
        if not entries:
            raise RuntimeError(f"显式跟踪清单 {explicit} 为空——拒答（不是通过）")
        inventory = _inventory_from_listing(entries)
        _TRACKED_CACHE[key] = inventory
        return tuple(sorted(inventory))

    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            f"无法从 {root} 读取 git 跟踪清单（git ls-files 失败：{exc}）——"
            f"DG7 的判据是**仓库真值**，取不到时必须拒答，不得退回文件系统判据。"
            f"若这是一棵没有 `.git` 的树（archive / tarball），请显式喂入清单："
            f"`--tracked-from <git ls-files 输出>` 或设 {_TRACKED_FROM_ENV}"
        ) from exc
    entries = {p for p in proc.stdout.split("\0") if p}
    if not entries:
        raise RuntimeError(
            f"{root} 的 git 跟踪清单为空——这不是一个可判定的 checkout（拒答）"
        )
    inventory = _inventory_from_listing(entries)
    _TRACKED_CACHE[key] = inventory
    return tuple(sorted(inventory))


def _non_repository_kind(tok: str) -> str | None:
    """`tok` 命中分类表则返回其 KIND（exact 或目录前缀），否则 None。"""
    entry = tok.rstrip("/")
    for declared, (kind, _reason) in _NON_REPOSITORY_PATHS.items():
        base = declared.rstrip("/")
        if entry == base or entry.startswith(base + "/"):
            return kind
    return None


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


def check_dg7(files: list[str], tracked: tuple[str, ...] | None = None) -> list[str]:
    """DG7：行内 code 里**像仓库路径**的引用必须对**仓库真值**成立。

    **2026-09-21 语义修复**：真实 CI 在干净 checkout 里报了 8 条，而本机全绿。根因是
    本判据曾用 `Path.exists()`——只存在于开发者工作区的 untracked/gitignored 文件
    （`tmp/` 探针、`npm run build` 产物、运行期 `data/`）让引用"看起来成立"。
    **同一次提交必须在开发者工作区、干净 clone、CI checkout 里给出同一个结论**，
    所以"存在性"只问 `git ls-files`：

    | 引用身份 | 判定 |
    |---|---|
    | `REPOSITORY_PATH`（默认） | 必须在 `git ls-files` 清单里（文件或其祖先目录） |
    | `RUNTIME_PATH` / `GENERATED_PATH` / `EXTERNAL_PATH` | 必须在 `_NON_REPOSITORY_PATHS` 里**显式分类**（带理由）——**不要求存在**，因为它们本就该在干净 checkout 里缺席 |

    **分类 ≠ 消音**：豁免只对"有意不在仓库里"的路径开放，且**不得覆盖任何被跟踪的树**
    （`test_dg7_declared_roots_are_never_tracked` 机械证明）。`tmp/` **不在**表里——
    临时探针路径不该被 canonical 文档引用，应改引稳定来源（测试 / 符号 / 执行记录）。

    解析候选目录为"文档所在目录 + 其各级祖先"（覆盖"根相对"与"包相对"两种写法）；
    含占位符/示例的 code span、带历史标记的行照旧跳过。
    """
    problems: list[str] = []
    tracked_set = set(tracked if tracked is not None else _tracked_inventory())
    for rel in files:
        doc_dir = (_ROOT / rel).parent
        candidates_dirs = [doc_dir, *doc_dir.parents]
        doc_lines = _mask_fences((_ROOT / rel).read_text(encoding="utf-8")).splitlines()
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
                if _non_repository_kind(tok) is not None:
                    continue  # 显式分类的非仓库路径（运行期 / 构建产物 / 外部仓）
                for base in candidates_dirs:
                    try:
                        candidate = str((base / tok.rstrip("/")).relative_to(_ROOT))
                    except ValueError:
                        continue
                    if candidate.replace(os.sep, "/") in tracked_set:
                        break
                else:
                    problems.append(
                        f"{rel}:{lineno} 引用了解析不到的仓库路径 `{tok}`（判据 = `git ls-files`，"
                        f"按文档目录及其各级祖先解析）——读者按它去找会 404。"
                        f"若它本就是运行期/构建产物/外部仓路径，请登记进 scripts/check_docs.py 的 "
                        f"_NON_REPOSITORY_PATHS 并写明理由；**临时/scratch 路径不要登记**，"
                        f"改引稳定来源（测试 node id / 符号 / 执行记录）"
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


def check_stale_registrations(
    scan_texts: dict[str, str] | None = None,
    tracked: tuple[str, ...] | None = None,
) -> list[str]:
    """登记陈旧也会红——但**"干净 checkout 里不存在"不算陈旧**。

    这是 §14 明确要求的语义修正：运行时/构建产物类登记项**本就该在干净 checkout 里缺席**，
    若把"物理不存在"当陈旧，就会形成"豁免项必须存在"的悖论（等于要求把 runtime 产物签进库）。
    陈旧的三条正当判据：

    1. **没有任何在扫文档再引用它**（表不该退化成永久垃圾场）；
    2. **理由缺失或过短**（等于没写）；
    3. **它其实已被 git 跟踪**（那就不需要豁免，留着会让真判据被绕过）。

    `scan_texts` 由 `main()` 传入（`{相对路径: 文本}`，只含在扫文档）；
    不传时自行读取在扫文档（测试可注入沙箱内容）。
    """
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

    if scan_texts is None:
        scan_texts = {
            rel: (_ROOT / rel).read_text(encoding="utf-8") for rel in _link_scan_targets()
        }
    corpus = "\n".join(scan_texts.values())
    # 仓库真值由 `main()` 解析一次后传入——**不要**在这里各自去问 git：没有 `.git` 的树
    # （archive / tarball）会因缺清单而抛错，而调用方本已用 `--tracked-from` 提供了真值。
    tracked_set = set(tracked if tracked is not None else _tracked_inventory())
    for declared, (kind, reason) in _NON_REPOSITORY_PATHS.items():
        base = declared.rstrip("/")
        if len(reason.strip()) < 10:
            problems.append(
                f"_NON_REPOSITORY_PATHS 的 `{declared}`（{kind}）理由太短，等于没写：{reason!r}"
            )
        # "已被 git 跟踪" = 声明项本身被跟踪 **或它下面有被跟踪内容**（目录前缀形态）。
        # 只看精确相等会漏掉"声明了 data/uploads/ 而 data/uploads/x.txt 被跟踪"这种
        # 豁免盖住仓库内容的情形——那正是豁免最该被拒绝的时候。
        covers_tracked = base in tracked_set or any(
            t == base or t.startswith(base + "/") for t in tracked_set
        )
        if covers_tracked:
            problems.append(
                f"_NON_REPOSITORY_PATHS 登记了 `{declared}`（{kind}），但它**已被 git 跟踪** —— "
                f"仓库路径不需要豁免，请删除该条目（留着会让真判据被绕过）"
            )
            continue
        referenced = base in corpus or any(
            tok in corpus for tok in (base, base + "/", declared)
        )
        if not referenced:
            problems.append(
                f"_NON_REPOSITORY_PATHS 登记了 `{declared}`（{kind}：{reason}），"
                f"但**没有任何在扫文档引用它** —— 请删除该条目（登记陈旧 = 理由在冒充事实）"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="文档漂移守卫（DG1–DG5）")
    parser.add_argument("--list", action="store_true", help="打印扫描范围与登记表后退出")
    parser.add_argument(
        "--tracked-from",
        metavar="FILE",
        help="显式跟踪清单（git ls-files 输出，每行一条）——供没有 .git 的树"
             f"（git archive / tarball）；也可用环境变量 {_TRACKED_FROM_ENV}",
    )
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
        print("\n_NON_REPOSITORY_PATHS（DG7 的**有意不在仓库里**分类表；不要求物理存在）：")
        for rel, (kind, why) in _NON_REPOSITORY_PATHS.items():
            print(f"  [{kind}] {rel} — {why}")
        print("\nDG7 仓库路径判据 = `git ls-files`（**不是** Path.exists()）——"
              "同一次提交在开发者工作区 / 干净 clone / CI 里结论一致")
        print(f"\nDG5：AppConfig env 名 {len(_appconfig_env_names())} 个，须全部出现在 docs/operations.md")
        return 0

    # 拒答条件：扫描面不成立时非零退出且**不打印通过标记**（假绿③）。
    if not files:
        print("FATAL: 扫描面为空——脚本或仓库布局已变，判据不成立（拒答）", file=sys.stderr)
        return 2

    # **仓库真值**：DG7 的存在性判据只有它。取不到 ⇒ 拒答（exit 2，**不打印通过标记**）——
    # 退回文件系统判据正是 2026-09-21 clean-checkout 假绿的成因。
    try:
        tracked = _tracked_inventory(tracked_from=args.tracked_from and Path(args.tracked_from))
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    scan_texts = {rel: (_ROOT / rel).read_text(encoding="utf-8") for rel in files}
    failures: list[tuple[str, list[str]]] = [
        ("DG1 canonical/supporting 内部链接", check_dg1(files)),
        ("DG1b 文内锚点（#heading）可解析", check_dg1_anchors(files)),
        ("DG2 禁止引用已知不存在的符号/路径", check_dg2(files)),
        ("DG3 协议错误码目录 ↔ errors.py", check_dg3()),
        ("DG6 代码围栏成对", check_dg6(files)),
        ("DG7 行内 code 里的仓库路径对仓库真值成立", check_dg7(files, tracked)),
        ("DG4 core/ 子系统 README 覆盖", check_dg4()),
        ("DG5 AppConfig 环境变量索引一致", check_dg5()),
        ("DG* 登记陈旧", check_stale_registrations(scan_texts, tracked)),
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
        f"DG4（core 子系统 README 覆盖）/ DG5（{len(_appconfig_env_names())} 个 AppConfig env 名在册）/ "
        f"DG6（围栏成对）/ DG7（{len(tracked)} 条仓库真值路径；非仓库分类 "
        f"{len(_NON_REPOSITORY_PATHS)} 条）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
