"""CI 信任边界静态门禁（P0-02，2026-08-10 全项目 review）。

防止不受信 pull_request 代码在持久化 self-hosted runner 上接触宿主凭据。
拒绝两类组合：

1. 可在 pull_request 上触发的 job 使用 `runs-on: self-hosted` —— 恶意 PR 的
   Makefile/pytest/Python/`npm ci` 会在持久 runner 上以宿主权限执行任意代码。
2. 任何 PR 可达 job 的步骤引用宿主绝对路径凭据（`/home/.../.env`），或
   `cp`/`source` 宿主 `.env` —— 直接把宿主密钥暴露给 PR 代码。

第三条（2026-09-14 CI 性能专项补）：**死 job**。job 的 `if` 守卫引用的事件必须
在 workflow 顶层 `on:` 里声明过，否则该守卫的分支永远不会成立。此前 ci.yml
的 `external` job 就是这种形态：`if: github.event_name == 'workflow_dispatch'`，
而 ci.yml 的 `on:` 只有 push / pull_request —— 45 分钟的真实 LLM/DB E2E 配置
从未执行过一次（runner 侧 57 份 worker 日志里只有 Quick / Normal）。

**为什么这条属于信任边界门禁**：`_is_push_only` 把「守卫里提到
workflow_dispatch」的 job 直接当作受信 job 并**跳过全部检查**（第 79-83 行）。
于是「写一个永不触发的守卫」成了绕过本门禁的静默通道——把受信判定反过来
当作免检声明。加了第三条之后，这种守卫必须在 `on:` 里真实声明才成立。

第四条（2026-09-14 Frontend 迁移批次补）：**PR 可达 job 的 `runs-on` 必须可
静态判定为 GitHub-hosted**。此前只查「`self-hosted` 这个子串在不在 `runs-on`
里」——那么 `runs-on: ${{ vars.CI_RUNNER }}`（仓库变量指向自建机器）**一个子串
都不命中，直接判绿**，而 PR 代码实际落在哪类 runner 上完全取决于仓库设置。
这正是「门禁的三种假绿」之③（环境不满足时判据无从成立）：无法判定时必须**拒答**
（非零退出），不能默认它安全。

第五条（同一批次）：**受信 self-hosted job 的 workspace 清理必须密闭、受守卫、且
fail-closed**。三个子句：
  a. 禁止裸 glob 清理（`rm -rf ./*`、`rm -rf *`）——shell glob 不匹配前导点，
     实测在 `/home/yomu/actions-runner/_work/elysia-agent/elysia-agent` 留下
     `.pytest_cache` / `.ruff_cache` / `.env` 等点文件跨 job 残留；
  b. 凡是清空 workspace 根目录的步骤，必须同时带两道守卫——`${GITHUB_WORKSPACE:?`
     （未设置即失败）与拒绝 `/`（`= "/"`）。
  c. **清理的退出码必须被显式检查**（`if ! find …; then … exit 1; fi`）。清理失败
     却继续，会在**未清空的持久 workspace** 上 fetch+checkout——而 checkout 那段
     脚本刻意不用 `set -e`（它必须容忍 retry loop 里「允许失败」的命令），所以
     失败默认是静默忽略的。实测：`find … -exec rm -rf -- {} +` 会把 `rm` 的失败
     传播成自己的非零退出（rm 返回 1 → find 退出 1），所以检查退出码足够。
     **`set -e` 不算满足本子句**：那会改变 retry loop 的控制流（要求写显式检查）。
**为什么这属于信任边界**：清理是**承重**的，不是卫生问题。backend job 的
`Load .env` 步骤明确依赖「上一步已把 workspace 清空，所以这里的 .env 一定是本
job 从宿主重新取的」；清理一旦失败或漏掉点文件，上一个 job（例如 nightly 的
external job，它 `cp` 真的宿主 .env 进 workspace）留下的凭据就会跨 job 泄漏给
下一个 job。

判定规则：
- job 的 `if` 若显式限定 push-only（字符串含 `github.event_name == 'push'`
  且不含 `pull_request`），视为受信 push job，允许 self-hosted + 宿主 .env；
  否则视为 PR 可达，必须 GitHub-hosted、不得触碰宿主路径。
- 步骤 `run`/`name` 中出现 `/home/<...>/.env`、或以 `/home/` 开头的绝对路径、
  `cp <宿主绝对路径> .env`、`source <宿主绝对路径>.env` 均视为访问宿主凭据。
- job `if` 中出现的每个 `github.event_name == '<event>'` 事件名都必须出现在顶层
  `on:` —— 否则记「不可达 job」违规。
- PR 可达 job 的 `runs-on` 若含 `${{`（表达式）或既不是已知 GitHub-hosted 镜像名
  （`ubuntu-*` / `windows-*` / `macos-*`）也不是显式 `self-hosted`，记违规（拒答）。
- 受信 self-hosted job 中，清空 workspace 根目录的步骤若做了裸 glob 清理、缺少
  `${GITHUB_WORKSPACE:?` 或 `= "/"` 守卫、或没把清理命令放进显式检查退出码的
  反条件里（见子句 c），记违规。

用法：
    python scripts/check_ci_isolation.py            # 检查 .github/workflows/*.yml 全部
    python scripts/check_ci_isolation.py <file...>  # 只检查指定文件
                                                          # 违规打印并退出码 1
    python -c "from scripts.check_ci_isolation import check_workflow_file; ..."
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_WORKFLOWS_DIR = ".github/workflows"

# 宿主绝对路径凭据特征（仓库不应引用任何 /home/... 或宿主 .env）
_HOST_PATH_MARKERS = (
    "/home/",
)
_HOST_ENV_MARKERS = (
    "cp /home/",
    "source /home/",
    ". /home/",
)

_EVENT_GUARD_RE = re.compile(r"github\.event_name\s*==\s*['\"]([a-z_]+)['\"]")

_TRUSTED_EVENTS = frozenset({"push", "workflow_dispatch", "schedule"})
"""受信触发（不受信 pull_request 永不触发它们）——`_is_push_only` 的判据词汇表。

与 `_EVENT_GUARD_RE` 配对使用：**同一个正则**既供死 job 判定、也供这里的守卫识别，
避免两处对空格/引号的要求不一致而互相打架（曾经就因此产生假红，见 `_is_push_only`）。
"""

_EVENT_INHERITING = frozenset({"workflow_call"})
"""`on:` 里出现它时，`github.event_name` 取自调用方——死 job 判定对这类文件不适用。"""

# GitHub 托管镜像名（`runs-on: ubuntu-latest` / `ubuntu-24.04` / `windows-2022` /
# `macos-14`）。PR 可达 job 的 runs-on **必须**命中它——否则一律拒答，见 docstring
# 第四条。刻意做成**形状**判定而不是"已知镜像名白名单"：白名单会在 GitHub 上线新
# 镜像（如 `ubuntu-26.04`）时假红，而假红会逼人去放宽规则。三种形状：
#   `latest` / 两位（macos-14）/ 四位（windows-2022）/ `两位.两位`（ubuntu-24.04）
_HOSTED_IMAGE_RE = re.compile(
    r"^(?:ubuntu|windows|macos)-(?:latest|\d{2}\.\d{2}|\d{2}|\d{4})$")

# 裸 glob 清理（`rm -rf ./*`、`rm -rf *`）——点文件逃逸，见 docstring 第五条 a。
_BARE_GLOB_RM_RE = re.compile(r"\brm\s+(?:(?:-[A-Za-z]+|--)\s+)*(?:\./)?\*(?=\s|$)")

# 「清空了 workspace 根目录」的判定：规范写法（find 顶层逐项删）或对根/通配符 rm。
# 注意 `rm -rf node_modules` **不算**——它的目标不是 workspace 根，套守卫只会造成假红。
#
# 2026-09-17（Batch 2.2）修复的**假阴性**：此前 rm 分支要求整个 token **就是**
# 工作区根（`$WS` / `${GITHUB_WORKSPACE}` / `.` / `*`），于是下面这些**同样清空
# workspace** 的写法全部不被认作 wipe ⇒ 三道清理子句（守卫标记 ×2 + fail-closed）
# **一条都不适用**，门禁照绿。实测（audit F1，已被本修复覆盖）：
#
#     rm -rf "$WS"/*                          ← 最像真实写法的那个，漏
#     rm -rf $WS/*         rm -rf "$WS"/.* "$WS"/*      rm -rf "${GITHUB_WORKSPACE}"/*
#     find "$WS" -mindepth 1 -maxdepth 1 -delete        ← 同样是真清空，漏
#
# 为什么这条重要：`frontend/vite.config.ts` 用 `outDir: ../static` +
# `emptyOutDir: false`，而 `static/assets|index.html|debug.html` 是 gitignore 的——
# **前端 job 的绿依赖"上一次的构建产物被清掉"**。清理纪律漏检等于该依赖无人守。
_WS_WIPE_FIND_MARKER = "-mindepth 1 -maxdepth 1 -exec rm"
# `find … -delete` 形态：同一条命令里出现 `-mindepth 1` 与 `-delete`，即视为清空。
# 刻意不要求 `-maxdepth 1`、也不要求路径正好是 `$WS`——把整个工作区删光、或删光它
# 某个子树的顶层，都是"清空 workspace"的同类形态；漏检的代价（清理纪律整条不适用）
# 远大于多要求一处守卫的成本（守卫本身无害，只是要求在删除前先校验 `$WS`）。
# 真实仓库的 6 处清理全是 `find … -exec rm` 规范写法，故本形态今天零命中。
_WS_WIPE_FIND_DELETE_RE = re.compile(
    r"\bfind\b[^\n]*\s-mindepth\s+1\b[^\n]*\s-delete\b")
# 「工作区根」的写法枚举。**顺序要紧**：长的必须在短的前面——
# `\./\*` 必须排在 `\.` 之前、`\*` 之前也要有 `\./\*`。否则正则在首个候选处就
# 匹配成功（如 `rm -rf ./*` 用 `\.` 只吃掉 `.`），随后 `(?![\w.*/-])` 看到 `/`
# 而失败，且**不会回头重试更长的候选**——整条命令就此漏检（实测：
# `rm -rf ./*`、`rm -rf "$WS"/*`、`rm -rf "${GITHUB_WORKSPACE}"/*` 三条全 miss）。
# ── 「清空 workspace 根」的 rm 形态识别 ──────────────────────────────────────
#
# 2026-09-17（Batch 2.2 + Challenger 复核）：这里经历过**四轮**修正，每一轮都被
# 实测教过一遍。记下来，避免下次又走回正则里：
#
#  ① 可变 token + 负向断言：可选后缀组可整体不匹配 ⇒ `"$WS"/static/assets` 被误判
#     成清空（假红）；把 `/` 放进断言后又漏掉 `"$WS"/*`（漏检）。
#  ② 候选按长到短 + 复杂断言：仍靠断言"猜边界"，脆弱难读；且 alternation 会
#     **回溯到更短的候选**——`(?:\./\*|\./|\.)` 在 `rm -rf ./*.log` 上先用 `\./\*`
#     失败、回退用 `\./` 成功 ⇒ 删日志被误判成清空（假红）。
#  ③ 完整目标枚举 + 断言：能过用例，但断言仍在承担"区分根与子路径"的语义。
#  ④ **纯字符串判定**（现在这版）：不再用正则描述"边界"，而是把 `rm` 的每个
#     **目标实参**切出来，与一组**写死的合法写法**逐一比对。可读、可加、
#     假红/漏检都能被参数化用例直接钉住——正则的边界语义不再参与。
#
# 为什么值得这么改：这三道清理子句（两道守卫 + fail-closed）**全部**以
# `_wipes_workspace_root()` 为唯一入口。认不出一个字符差异的写法，
# 等于那三道对它是关闭的——所以"识别面"本身就是安全属性。

# 工作区根的**字面写法**（含通配后缀）——`rm` 的目标必须**恰好**命中其一。
_WS_ROOT_TARGETS = frozenset({
    # 变量形式（`$WS` 是本仓清理样板里的局部变量，`GITHUB_WORKSPACE` 是 CI 提供的）
    "$WS", '"$WS"', "'$WS'",
    "${WS}", '"${WS}"', "'${WS}'",
    "${GITHUB_WORKSPACE}", '"${GITHUB_WORKSPACE}"', "'${GITHUB_WORKSPACE}'",
    "$GITHUB_WORKSPACE", '"$GITHUB_WORKSPACE"', "'$GITHUB_WORKSPACE'",
    # `${WS:?...}` / `${GITHUB_WORKSPACE:?...}`（带"未设置即失败"的写法也能被清空）
    "${WS:?}", '"${WS:?}"', "${GITHUB_WORKSPACE:?}", '"${GITHUB_WORKSPACE:?}"',
})
# 允许的**直接内容**后缀：`/*`、`/.*`（连点文件）、`/`。刻意不含任意路径，
# 那样会把 `"$WS"/static/assets`（删构建产物）也算成清空 ⇒ 假红。
_WS_ROOT_SUFFIXES = ("", "/*", "/.*", "/")

# 当前目录形态：只认 `.` / `./` / `./*` 三种**完整目标**。
_WS_DOT_TARGETS = frozenset({".", "./", "./*", "./.*", "*", ".[!.]*"})

# `rm` 的目标实参切分：匹配 `rm` 及其选项，捕获后续所有非选项实参。
_RM_INVOCATION_RE = re.compile(r"\brm\s+((?:(?:-[A-Za-z]+|--)\s+)*)(.*)")


def _rm_targets(text: str) -> list[list[str]]:
    """把每一条 `rm` 调用的目标实参切出来（返回每条调用的目标列表）。

    刻意做得**笨**：按空白切分、去掉选项与引号外的干扰。判据不依赖 shell 语义，
    只依赖"目标字符串长什么样"——这正是我们要比对的东西。
    """
    out: list[list[str]] = []
    for line in text.splitlines():
        for m in _RM_INVOCATION_RE.finditer(line):
            rest = m.group(2)
            args = rest.replace("\\\n", " ").split()
            # 只把**真选项**（`-x` / `--x` / 裸 `--`）滤掉；裸 `*`、`-` 之外的
            # 单字符不是选项——`*` 恰恰是最常见的清空目标之一。
            targets = [a for a in args
                       if not (a.startswith("-") and len(a) > 1)]
            if targets:
                out.append(targets)
    return out


def _is_root_target(target: str) -> bool:
    """单个 `rm` 目标是否指向工作区根（或其直接内容）。"""
    if target in _WS_DOT_TARGETS:
        return True
    for base in _WS_ROOT_TARGETS:
        for suffix in _WS_ROOT_SUFFIXES:
            if target == base + suffix:
                return True
    # `${WS:?<msg>}` 这类带消息的写法：按前缀匹配到 `}` 为止再套后缀
    m = re.fullmatch(r"(?:\"|')?\$\{(?:WS|GITHUB_WORKSPACE):\?[^}]*\}(?:\"|')?(.*)", target)
    if m and m.group(1) in _WS_ROOT_SUFFIXES:
        return True
    return False


def _rm_wipes_workspace_root(text: str) -> bool:
    """`rm` 调用里是否有目标指向工作区根。"""
    return any(_is_root_target(t) for targets in _rm_targets(text) for t in targets)

_WS_GUARD_MARKERS = (
    ("${GITHUB_WORKSPACE:?", "未设置即失败（`${GITHUB_WORKSPACE:?…}`）"),
    ('= "/"', "拒绝把 `/` 当工作区（`[ \"$WS\" = \"/\" ]`）"),
)

# 「清理 fail-closed」判据（docstring 第五条 c）：清理命令必须被**显式检查退出码**，
# 即 `if ! <cleanup …>; then … exit <非零> … fi`。
#
# 为什么不在门禁里直接判 `set -e`：那段 checkout 脚本**刻意**不用 `set -e`——
# retry loop 里 `git remote add`（撞已有 origin 时失败）与 `git fetch`（网络抖动）
# 都是「允许失败」的命令，`set -e` 会让它们直接终止脚本、改变控制流。所以
# `set -e` 既不是本仓的写法，也不该被算作满足本子句。
#
# 判据形状（三段，缺一不可）：
#   ① `if ! <find|rm …>`          —— 反条件包住清理命令本身
#   ② 其后若干行内出现 `exit <非零>`
#   ③ 这个 `exit` 必须还在**同一个 if 块内**——用逐行负向断言禁掉中途的 `fi`，
#      否则「`if ! …; then :; fi` + 后面别处一个 `exit 1`」会被误判成合规。
_CLEANUP_FAIL_CLOSED_RE = re.compile(
    r"if\s+!\s*(?:find|rm)\b[^\n]*\n"
    r"(?:(?!\bfi\b)[^\n]*\n){0,4}?"
    r"[^\n]*\bexit\s+[1-9]\d*\b")


def _declared_events(data: dict[str, Any]) -> set[str]:
    """workflow 顶层 `on:` 声明的事件名集合。

    `on:` 可以是字符串（`on: push`）、列表（`on: [push, pull_request]`）或映射
    （`on: {push: ..., workflow_dispatch: ...}`）——三种形态都要认。

    另有一个 YAML 1.1 的坑必须处理：**裸 `on:` 会被解析成布尔 `True`**（PyYAML
    按 YAML 1.1 把 `on`/`off`/`yes`/`no` 当布尔）。所以键要按 `"on"` 与 `True`
    两种形态都找一遍，不能只 `data["on"]`。
    """
    raw: Any = None
    for key in ("on", True):          # type: ignore[list-item]  # 见 docstring：两种键形态
        if key in data:
            raw = data[key]
            break
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(e) for e in raw}
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    return set()


def _guarded_events(job: dict[str, Any]) -> set[str]:
    """job `if` 里用 `github.event_name == '<event>'` 比较过的事件名集合。"""
    return set(_EVENT_GUARD_RE.findall(str(job.get("if", ""))))


def _is_push_only(job: dict[str, Any]) -> bool:
    """job 是否显式限定只跑受信触发（push / workflow_dispatch / schedule）。

    非 PR 触发（push 到受信 ref、受信管理员手工 workflow_dispatch、定时 schedule）
    视为受信——不受信 pull_request 永不触发它们。if 表达式含任一受信 guard 且不含
    pull_request → 受信 job（允许 self-hosted + 宿主凭据，F1 external E2E 用）。

    引号先归一：`== 'push'` 与 `== "push"` 语义完全相同（YAML 里两种都合法），
    只认单引号会把一个**正确加了 guard** 的受信 job 误判成不受信（假红），
    而那会逼人去删 guard——正好是反方向。
    """
    # 判据与死 job 判定**共用同一个正则**，而不是各自做子串匹配——两者此前对
    # "`==` 两侧的空格"要求不同：死 job 判定允许 `github.event_name=='push'`
    # （正则含 `\s*`），而这里要求字面量 `"== 'push'"` ⇒ 一个**正确加了 guard** 的
    # self-hosted push-only job 会被判成非受信而**假红**，那会逼人去删 guard——
    # 正好是反方向（2026-09-17 Batch 2.2 实测复现：`if: github.event_name=='push'`）。
    # 引号先归一：`== 'push'` 与 `== "push"` 语义相同（YAML 里两种都合法）。
    cond = str(job.get("if", "")).replace('"', "'")
    guarded = set(_EVENT_GUARD_RE.findall(cond))
    trusted_guard = bool(guarded & _TRUSTED_EVENTS)
    has_pr_ref = "pull_request" in cond
    return trusted_guard and not has_pr_ref


def _step_text(step: dict[str, Any]) -> str:
    """步骤的 run + name 拼接文本（用于路径/凭据特征扫描）。"""
    return f"{step.get('name', '')}\n{step.get('run', '')}"


def _runs_on_labels(job: dict[str, Any]) -> list[str]:
    """`runs-on` 归一成 label 列表。

    两种形态都要认：字符串（`runs-on: ubuntu-latest`）与列表
    （`runs-on: [self-hosted, Linux, X64]`，PyYAML 已解成 Python list）。字符串
    形态按 `[`/`]`/`,` 切开再逐项去引号；表达式（`${{ … }}`）会被原样保留成一项，
    由调用方判定「无法静态解析」。
    """
    raw = job.get("runs-on", "")
    items = [str(i) for i in raw] if isinstance(raw, list) else re.split(r"[,\[\]]", str(raw))
    return [i.strip().strip("'\"") for i in items if i.strip()]


def _unresolvable_runs_on(labels: list[str]) -> str:
    """PR 可达 job 的 runs-on 无法静态判定为 GitHub-hosted 时返回原因，否则空串。

    拒答（而非判绿）是刻意的：`runs-on: ${{ vars.CI_RUNNER }}` 一个特征子串都不含，
    按「self-hosted 在不在字符串里」判会直接放行，而 job 实际落在哪类 runner 上
    取决于仓库设置——判据本身无从成立，属「门禁的三种假绿」之③。
    """
    if not labels:
        return "runs-on 为空"
    if any("${{" in label for label in labels):
        return "runs-on 含表达式，无法静态判定 job 落在哪类 runner 上"
    unknown = [label for label in labels if not _HOSTED_IMAGE_RE.match(label)]
    if unknown:
        return (f"runs-on 里的 {unknown} 不是已知 GitHub-hosted 镜像名"
                "（ubuntu-* / windows-* / macos-*）")
    return ""


def _shell_code_lines(text: str) -> str:
    """剥掉**整行注释**后的 shell 代码文本。

    只有会被执行的行才算数：backend job 的注释里**引用**了旧写法
    `rm -rf ./* ./.git` 当反面教材，不剥注释的话这条规则会在「记住了教训」的地方
    判红——而假红会逼人删掉那段说明，正好是反方向。

    局限（明确记录，不假装完备）：行尾内联注释不剥。宁可内联注释里提一次旧写法
    时假红一次让人看见，也不去解析引号里的 `#`（那会漏掉真命令，属更坏的失败）。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _wipes_workspace_root(text: str) -> bool:
    """步骤是否清空 workspace **根目录**（见 docstring 第五条 b 的判定口径）。"""
    return (
        _WS_WIPE_FIND_MARKER in text
        or bool(_WS_WIPE_FIND_DELETE_RE.search(text))
        or _rm_wipes_workspace_root(text)
    )


def _check_self_hosted_cleanup(job_name: str, steps: list[dict[str, Any]]) -> list[str]:
    """受信 self-hosted job 的 workspace 清理纪律（docstring 第五条）。"""
    violations: list[str] = []
    for step in steps:
        # 只看 `run` 的**代码行**——注释（含 name）里提到旧写法不该判红。
        text = _shell_code_lines(str(step.get("run", "")))
        step_name = step.get("name", "<unnamed>")
        glob_hits = _BARE_GLOB_RM_RE.findall(text)
        if glob_hits:
            violations.append(
                f"job '{job_name}' 步骤 '{step_name}'（self-hosted）用裸 glob 清理 "
                f"（{sorted(set(glob_hits))}）——shell glob 不匹配前导点，"
                "`.pytest_cache`/`.env`/`.ruff_cache` 会跨 job 残留；"
                "改用 `find \"$WS\" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +`")
        if not _wipes_workspace_root(text):
            continue
        for marker, why in _WS_GUARD_MARKERS:
            if marker not in text:
                violations.append(
                    f"job '{job_name}' 步骤 '{step_name}'（self-hosted）清空 workspace "
                    f"根目录却缺少守卫 {marker} —— {why}；"
                    "清理逻辑必须对「未设置」与「等于 /」两种情形都拒绝执行")
        # 子句 c：清理失败必须终止 job（fail-closed）
        if not _CLEANUP_FAIL_CLOSED_RE.search(text):
            violations.append(
                f"job '{job_name}' 步骤 '{step_name}'（self-hosted）清空了 workspace "
                "根目录，但**没有显式检查清理的退出码**——清理失败会被静默忽略，"
                "脚本继续在未清空的持久 workspace 上 fetch+checkout（残留的 .env "
                "会跨 job 泄漏，而 backend 的 `Load .env` 正依赖 workspace 已清空）。"
                "要求写法：`if ! find \"$WS\" -mindepth 1 -maxdepth 1 "
                "-exec rm -rf -- {} +; then` … `exit 1` … `fi`。"
                "`set -e` 不算满足——它会改变 retry loop 的控制流")
    return violations


def _check_permissions(data: dict[str, Any], violations: list[str]) -> None:
    """workflow 顶层 `permissions:` 必须存在**且确实是只读**。

    2026-09-17（Batch 2.2）加的是"**存在性**"判据；随后 Challenger 复核指出
    它把 `permissions: write-all` 放行了——而规则自己的提示语写着
    「本仓 job 只需读代码，请显式声明 `contents: read`」，commit 标题也写着
    "pin least privilege"。**判据比声称的弱，就是假绿**：一个写了 `write-all`
    的 workflow 会拿到"已声明 permissions"的绿灯，而它比不写更危险
    （不写只继承仓库默认，写 `write-all` 是**显式**要求全写）。

    故这里把判据补到与声称一致：
    - 缺失 / 显式 `null` → 违规（继承仓库默认，不可判定）；
    - 顶层 `write-all` → 违规（显式全写，与"只读"直接矛盾）；
    - 值必须是 mapping，且**每个** scope 的值都在只读白名单内
      （`read` / `none`）；出现 `write` / `write-all` → 违规。

    刻意**不**要求恰好等于 `{contents: read}`：有些 workflow 未来可能需要
    `pull-requests: read` 之类——只读是本仓的纪律，具体 scope 集合不是。
    """
    perms = data.get("permissions")
    if "permissions" not in data or perms is None:
        violations.append(
            "workflow 顶层缺少 `permissions:` 块——会继承仓库默认（可能是 read/write），"
            "静态判据无从成立。本仓 job 只需读代码，请显式声明 `permissions:` + `contents: read`")
        return
    if isinstance(perms, str):
        if perms == "read-all":
            return          # 显式全读，符合"只读"纪律
        violations.append(
            f"workflow 顶层 `permissions: {perms}` 不是只读——本仓 job 只需读代码，"
            "`write-all` 比不写更危险（那是**显式**要求全写）。请改为 `contents: read`")
        return
    if not isinstance(perms, dict):
        violations.append(
            f"workflow 顶层 `permissions:` 形态无法静态判定（{type(perms).__name__}）"
            "——按本门禁一贯纪律：无法判定时**拒答**，不默认它安全")
        return
    writable = sorted(
        scope for scope, value in perms.items()
        if isinstance(value, str) and value not in ("read", "none"))
    if writable:
        violations.append(
            f"workflow 顶层 `permissions:` 授予了写权限：{writable}——"
            "本仓 job 只需读代码（凭据来自宿主 `.env`，不来自 `GITHUB_TOKEN`；"
            "而 token 会嵌进 git fetch URL 并落进 workspace 的 `.git/config`）。"
            "请改为 `read`/`none`")


def check_workflow_file(path: str | Path) -> list[str]:
    """检查一个 workflow 文件的 PR 信任边界，返回违规列表（空 = 通过）。

    Args:
        path: workflow YAML 文件路径。

    Returns:
        违规描述列表；为空表示无违规。
    """
    violations: list[str] = []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    jobs: dict[str, Any] = (data or {}).get("jobs", {}) or {}
    declared = _declared_events(data or {})
    # 最小 token 权限（2026-09-17 Batch 2.2）：workflow 必须显式声明 `permissions`。
    # 不声明就继承**仓库默认**（可能是 read/write），而本仓每个 job 都只需读代码
    # ——凭据来自宿主 `.env`（仅受信 ref 加载），不来自 `GITHUB_TOKEN`；而 token 会
    # 嵌进 git fetch URL 并落进 workspace 的 `.git/config`，给写权限纯属放大 blast
    # radius。ci.yml 早就为此写了注释与块，但另外两个 workflow 一直没跟上——
    # 这正是「同一条纪律只装在改动过的那个文件上」。故改成**门禁强制**，
    # 新增 workflow 忘记声明会直接红。
    _check_permissions(data or {}, violations)
    # 可复用 workflow（`on: workflow_call`）里 `github.event_name` 取自**调用方**，
    # 所以它的 job 用 `if: github.event_name == 'push'` 是正当写法、不是死守卫。
    # 不豁免的话，本仓加第一个 reusable workflow 就会让 CI 假红。
    # 注意这只关掉"死 job"判定：`_is_push_only` 仍照常分类（无 guard 的
    # workflow_call job 依旧算 PR 可达，必须 GitHub-hosted）。
    event_inherited = bool(declared & _EVENT_INHERITING)
    for name, job in jobs.items():
        labels = _runs_on_labels(job)
        runs_on = str(job.get("runs-on", ""))
        steps: list[dict[str, Any]] = job.get("steps", []) or []
        # 死 job 判定（先于 `_is_push_only` 的免检 continue——否则「写一个永不触发的
        # 守卫」即可绕过下面全部检查，2026-09-14）
        unreachable = set() if event_inherited else _guarded_events(job) - declared
        if unreachable:
            violations.append(
                f"job '{name}' 的 if 守卫引用了未在 on: 声明的事件 "
                f"{sorted(unreachable)}（本 workflow 声明的是 {sorted(declared)}）"
                "——该分支永不触发，是死 job；要么在 on: 声明该事件，要么删掉死配置")
        if _is_push_only(job):
            # 受信 push job：允许 self-hosted + 宿主 .env（受信 ref，仅维护者可触发）。
            # 这里不扫描宿主路径——push-only guard 本身即信任边界（门禁脚本负责
            # 验证 PR 可达 job 永不 self-hosted、永不引用宿主路径）。
            # 但**清理纪律照查**：清理是承重的（backend job 的 .env 依赖它），
            # 不是"受信 job 就可以随便删"（2026-09-14 Frontend 迁移批次，第五条）。
            if "self-hosted" in labels:
                violations.extend(_check_self_hosted_cleanup(name, steps))
            continue
        # PR 可达 job：禁止 self-hosted、禁止宿主凭据路径
        if "self-hosted" in labels:
            violations.append(
                f"job '{name}' 可在 pull_request 上运行却使用 runs-on: {runs_on} —— "
                "不受信 PR 会在持久化 self-hosted runner 上执行任意代码")
        else:
            reason = _unresolvable_runs_on(labels)
            if reason:
                violations.append(
                    f"job '{name}'（PR 可达）的 runs-on 无法静态判定为 GitHub-hosted："
                    f"{reason} —— 无法判定就必须拒答，不能默认它安全")
        for step in steps:
            text = _step_text(step)
            for marker in _HOST_PATH_MARKERS:
                if marker in text:
                    violations.append(
                        f"job '{name}'（PR 可达）步骤引用宿主绝对路径 '{marker}'")
            for marker in _HOST_ENV_MARKERS:
                if marker in text:
                    violations.append(
                        f"job '{name}'（PR 可达）步骤 source/cp 宿主 .env '{marker}'")
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：检查 workflow 文件，违规退出码 1。

    Args:
        argv: 命令行参数（可选 workflow 路径列表）。

    Returns:
        0 表示通过，1 表示违规，2 表示无法判定（拒答）。
    """
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        paths = [Path(p) for p in argv]
    else:
        # 默认**扫描全部** workflow 文件——此前调用方手写三个文件名，新增第四个
        # workflow 不会被任何门禁看见（CLAUDE.md「守卫没装在改动上」）。
        paths = sorted(Path(_WORKFLOWS_DIR).glob("*.yml"))
    if not paths:
        print(f"CI 信任边界检查无法判定：{_WORKFLOWS_DIR} 下没有任何 *.yml")
        return 2

    failed = False
    for path in paths:
        violations = check_workflow_file(path)
        if violations:
            failed = True
            print(f"CI 信任边界检查失败：{path}")
            for v in violations:
                print(f"  - {v}")
        else:
            print(f"CI 信任边界检查通过：{path}"
                  "（无 PR 可达 self-hosted job，无宿主凭据路径，无死 job）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
