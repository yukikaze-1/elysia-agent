#!/usr/bin/env python3
"""Secret scanning 门禁（CR-01 步骤 4）。

**为什么需要它**：2026-06 一份 review 文档在正文里**抄录了当时的 `.env` 明文**
（`DEEPSEEK_API_KEY="sk-d6cdb4…"` + `MILVUS_TOKEN="root:Milvus"`），commit
`f1dcc803` 已推送到公开 GitHub remote，三个月无人发现——因为**六个 CI job 里没有
一个做 secret scanning**：`.gitignore` 只防未来提交、`ConfigField(secret=True)` 只防
写进 YAML、`bandit.yaml` 反而**主动豁免** `B105/B106/B107`（硬编码密码）恰好放行这类问题。

本脚本按"**能被 CI 拦住**"的标准设计，纯 stdlib（可在无依赖的 PR job 里跑）：

1. **已知泄露指纹**：直接匹配本次事故的 key 前缀，防止它以后又出现在新文件里；
2. **形态匹配**：`sk-` / `tvly-` / `ghp_` / `AKIA` / PEM 私钥 / `Bearer <长串>` /
   `*_TOKEN=`/`*_KEY=`/`*_SECRET=` 后跟**看起来像真值**的字面量；
3. **禁用载体**：被 git 跟踪的 `.env*`（`.env.example` 除外）；
4. **文档抄录**（本次事故的直接成因）：markdown/文本里出现 `VAR="真值"` 形态。

**判定"像真值"**：长度达标 + 不是占位符（`your-key` / `xxx` / `test` / `changeme` /
`<...>` / `${...}` / 纯星号掩码 / 全同字符 …）。占位符一律放行——门禁的第一要求
是**不误报**，否则会被加白到失效。

**浅克隆下一票否决（退出码 2）**：CI 的 checkout 是 `--depth 1`——self-hosted job 用
`git fetch --depth 1`，GitHub-hosted 的 `actions/checkout@v4` 默认也是。那里
`git rev-list --all` 只看得到最后一个提交，"无命中"不是结论、而是**没扫**：实测在
depth-1 克隆里本脚本对 2026-06 那次泄露报"✓ 通过"。这与开头说的假绿是同一件事，
所以 `--all-refs` 在历史不完整时**拒绝给结论**，而不是给一个看起来干净的绿灯。

用法：
    python scripts/check_secrets.py            # 扫工作树（被跟踪文件）
    python scripts/check_secrets.py --all-refs # 扫全历史（须完整 clone，本机约 30s）
    python scripts/check_secrets.py --list     # 打印规则清单
退出码：0 通过 / 1 命中 / 2 **无法判定**（浅克隆等历史不完整的情形）。
"""

from __future__ import annotations

import re
import subprocess
import sys

# ── 已知泄露指纹（2026-06 事故，CR-01）────────────────────────────────────────
# 只放前缀：既足以命中，又不在本文件里复现完整凭据（正是本次要禁止的行为）。
KNOWN_LEAK_PREFIXES = ("sk-d6cdb4",)

# ── 形态规则 ────────────────────────────────────────────────────────────────
# (名称, 正则, 说明)
PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("deepseek/openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
     "OpenAI 兼容 API key"),
    ("tavily-key", re.compile(r"\btvly-[A-Za-z0-9]{20,}"), "Tavily API key"),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "GitHub personal access token"),
    ("aws-akid", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥"),
    ("bearer-literal", re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "硬编码 Bearer token"),
    # 赋值形态：VAR = "看起来像真值的字面量"。
    # 后缀刻意**不含裸 `_KEY`**——`yaml_key` / `cache_key` / `sort_key` 是配置键，
    # 不是凭据；把它们判成泄露是典型的"狼来了"，会让门禁被加白到失效。
    ("env-assignment", re.compile(
        r"""(?ix)\b([A-Z][A-Z0-9_]*(?:_API_KEY|_SECRET_KEY|_ACCESS_KEY|_PRIVATE_KEY|"""
        r"""_TOKEN|_SECRET|_PASSWORD|_PASSWD|_CREDENTIALS?))\s*"""
        r"""(?:=|:)\s*["']([^"'\s]{8,})["']"""), "凭据类环境变量的字面量赋值"),
)

# ── 放行判定 ────────────────────────────────────────────────────────────────
_PLACEHOLDER_HINTS = (
    "your", "xxx", "yyy", "zzz", "placeholder", "example", "sample", "dummy",
    "fake", "test", "dummy", "changeme", "change-me", "redacted", "todo",
    "none", "null", "empty", "secret", "token", "key", "password", "passwd",
    "minioadmin", "localhost", "127.0.0.1", "root",
)


def _looks_like_placeholder(value: str) -> bool:
    """占位符 / 掩码 / 模板变量 → 不是真凭据。

    门禁的第一要求是**不误报**：一个会喊狼来了的门禁会被加白到失效，比没有更糟。
    """
    v = value.strip()
    if len(v) < 8:
        return True
    if v.startswith(("$", "{", "<", "%", "os.environ", "env.")) or "${" in v:
        return True
    if set(v) <= set("*•.#-"):          # 纯掩码 sk-****
        return True
    if len(set(v)) <= 2:                # aaaaaaaa / 11111111
        return True
    low = v.lower()
    if any(h in low for h in _PLACEHOLDER_HINTS):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):   # 另一个变量名
        return True
    return False


# 本文件自身与文档里的**规则示例**天然包含这些模式 → 显式豁免并记录理由。
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("scripts/check_secrets.py", "扫描器自身的规则定义"),
    ("docs/credential-handling.md", "凭据规范文档中的掩码示例"),
    ("CODE_REVIEW_REPORT.md", "审查报告以掩码形式引用泄露指纹（事故记录）"),
    ("docs/review/2026-09-12-code-review-ledger.md", "台账同上"),
)

_SCAN_SUFFIXES = (".py", ".md", ".yml", ".yaml", ".json", ".toml", ".sh", ".env",
                  ".example", ".txt", ".cfg", ".ini", ".ts", ".vue", ".js")
_SKIP_DIR_PARTS = ("/.git/", "node_modules/", "__pycache__/", "/data/", "/log/",
                   "/.model_cache/", "/static/assets/")


def _history_state() -> str:
    """历史完整度：`complete` / `shallow` / `unknown`。

    `--all-refs` 的绿灯只在前者可信——浅克隆里 `rev-list --all` 只覆盖最后一个
    提交，扫描器"什么都没找到"与"什么都没扫"不可区分。
    """
    try:
        proc = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                              capture_output=True, text=True)
    except OSError:
        return "unknown"
    if proc.returncode != 0:          # 不是 git 仓库 / git 不可用
        return "unknown"
    return "shallow" if proc.stdout.strip() == "true" else "complete"


def _tracked_files(all_refs: bool) -> list[tuple[str, str]]:
    """待扫清单 `[(读取标识, 路径)]`。

    - 工作树模式：标识 = 路径（经 `git show :<path>` 读被跟踪内容）；
    - `--all-refs`：标识 = blob sha（经 `git cat-file -p` 读），路径 = 该 blob 的
      历史路径——**两者必须分开**：曾把 sha 当路径喂给后缀过滤，于是历史模式
      "扫了但什么都没扫"（全绿）；而这条门禁存在的唯一理由就是历史里有东西。
    """
    if all_refs:
        out = subprocess.run(["git", "rev-list", "--objects", "--all"],
                             capture_output=True, text=True, check=True).stdout
        pairs: list[tuple[str, str]] = []
        for ln in out.splitlines():
            if " " not in ln:
                continue
            sha, path = ln.split(" ", 1)
            pairs.append((sha, path))
        return pairs
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [(ln, ln) for ln in out.splitlines() if ln]


def scan_text(path: str, text: str) -> list[tuple[str, int, str]]:
    """扫描一段文本，返回 [(path, 行号, 说明)]。"""
    findings: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for prefix in KNOWN_LEAK_PREFIXES:
            if prefix in line:
                findings.append((path, lineno, f"已知泄露指纹 {prefix}"))
        for name, pattern, desc in PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            candidate = m.group(2) if (m.lastindex or 0) >= 2 else m.group(0)
            if name == "env-assignment" and _looks_like_placeholder(candidate):
                continue
            if name != "env-assignment" and name != "private-key" and _looks_like_placeholder(
                    candidate):
                continue
            findings.append((path, lineno, f"{desc}（{name}）"))
    return findings


def _is_allowlisted(path: str) -> bool:
    return any(path == a or path.endswith("/" + a) for a, _ in _ALLOWLIST)


def _scannable(path: str) -> bool:
    if any(part in f"/{path}" for part in _SKIP_DIR_PARTS):
        return False
    if path.endswith(_SCAN_SUFFIXES):
        return True
    return path.rsplit("/", 1)[-1].startswith(".env")


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for name, _, desc in PATTERNS:
            print(f"{name}: {desc}")
        return 0
    all_refs = "--all-refs" in argv

    if all_refs:
        state = _history_state()
        if state != "complete":
            why = ("当前是浅克隆（shallow）" if state == "shallow"
                   else "无法确定历史是否完整（git rev-parse 失败，非 git 仓库？）")
            print(f"✗ 无法判定：{why}——`--all-refs` 只看得到部分历史，"
                  "「无命中」不代表干净。\n"
                  "  处理：先 `git fetch --unshallow`（或改用完整 clone）再重跑。")
            return 2

    findings: list[tuple[str, int, str]] = []
    seen: set[tuple[str, str]] = set()
    for ident, path in _tracked_files(all_refs):
        if not _scannable(path) or _is_allowlisted(path):
            continue
        # 被跟踪的 .env（非 example）本身就是违规
        base = path.rsplit("/", 1)[-1]
        if base.startswith(".env") and base != ".env.example":
            key = (path, "env-tracked")
            if key not in seen:
                seen.add(key)
                findings.append((path, 1, "被 git 跟踪的 .env（凭据文件不得入库）"))
            continue
        try:
            text = subprocess.run(["git", "show", f":{path}"] if not all_refs
                                  else ["git", "cat-file", "-p", ident],
                                  capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        for finding in scan_text(path, text):
            key = (finding[0], finding[2])
            if key in seen:          # 历史模式下同一违规会在多个 commit 重复出现
                continue
            seen.add(key)
            findings.append(finding)

    if findings:
        print("✗ secret scanning 命中（疑似凭据入库）：")
        for path, lineno, desc in findings:
            print(f"  {path}:{lineno}  {desc}")
        print("\n处理：轮换该凭据 → 从工作树移除 → 清历史 → 更新 "
              "docs/credential-handling.md 的豁免理由（如确为占位符）")
        return 1
    print("✓ secret scanning 通过：无凭据形态命中")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
