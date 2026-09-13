# 测试文件：scripts/tests/test_check_secrets.py
"""Secret scanning 门禁（CR-01 步骤 4）—— 先证明它抓得到，再相信它的绿灯。

门禁类脚本最大的风险不是"漏"而是"**假绿**"：本次实现过程中就出现过一次——
`--all-refs` 把 blob sha 当路径喂给后缀过滤，于是"历史模式"扫了 0 个文件却报通过。
`test_history_mode_actually_reads_blobs` 就是那条缺陷的回归网。

**第二次假绿（2026-09-14，CI 报错暴露）**：CI 的 checkout 是 `--depth 1`，
历史模式只扫得到最后一个提交却照样打印"✓ 通过"。旧测试把"本仓全历史必须命中"
写成断言，在浅克隆里**不可能成立**（且清历史后还会反过来永远红）。现在三层收口：

1. 脚本对浅克隆**拒答**（退出码 2，不报绿）——`test_shallow_checkout_refuses_to_report_green`
2. 合成仓库给出与环境无关的端到端证据——`test_end_to_end_history_mode_catches_leak_deleted_from_the_tree`
3. 真实仓库那条用 `git log -S` 现算期望值，浅克隆下显式 skip（原因写进 skip 消息）
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from check_secrets import (  # noqa: E402
    KNOWN_LEAK_PREFIXES,
    _looks_like_placeholder,
    _scannable,
    _tracked_files,
    scan_text,
)

pytestmark = pytest.mark.unit

# 形态逼真但**不是**真凭据的测试串（由片段拼接，避免本文件自己被扫描器命中）
_FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
_FAKE_TAVILY = "tvly-" + "0123456789abcdefghij"
_FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
# 不直接下标：一旦指纹表被清空（正是要反证的回归），测试模块会在 import 期
# 抛 IndexError → 报"collection error"，测不到真正的行为差异。
_LEAK_PREFIX = KNOWN_LEAK_PREFIXES[0] if KNOWN_LEAK_PREFIXES else "sk-" + "d6cdb4"
_LEAK = _LEAK_PREFIX + "deadbeefdeadbeefdeadbeefdeadbeef"


# ═══════════════════════════════════════════════════════════════════
# 能抓到（正向）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("line, expect", [
    (f'DEEPSEEK_API_KEY="{_FAKE_OPENAI}"', "env-assignment"),
    (f'key = "{_FAKE_OPENAI}"', "deepseek/openai-key"),
    (f'T=  "{_FAKE_TAVILY}"', "tavily-key"),
    (f'token: "{_FAKE_PEM}"', "private-key"),
    (f'Authorization: Bearer {_FAKE_OPENAI}', "bearer-literal"),
    (f'KEY="{_LEAK}"', "已知泄露指纹"),
])
def test_patterns_are_detected(line, expect):
    hits = scan_text("x.py", line)
    assert any(expect in desc for _, _, desc in hits), (line, hits)


def test_known_leak_fingerprint_alone_is_enough():
    """已知指纹单独出现也要命中——它是本次事故的 key，不该有任何"看上下文"的余地。"""
    hits = scan_text("x.md", f"当时的值是 {_LEAK} 这样")
    assert any("已知泄露指纹" in d for _, _, d in hits)


def test_tracked_env_file_is_flagged():
    """被跟踪的 `.env` 本身就是违规（哪怕内容全是占位符）。"""
    assert _scannable(".env")
    assert _scannable("config/.env.production")
    assert not _scannable("node_modules/x/.env")


def test_scan_reports_path_and_line():
    findings = scan_text("docs/a.md", "无关行\n" + f'X_API_KEY="{_FAKE_OPENAI}"\n')
    assert findings and findings[0][0] == "docs/a.md" and findings[0][1] == 2


# ═══════════════════════════════════════════════════════════════════
# 不误报（负向）——门禁的第一要求：不喊狼来了
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value", [
    "", "   ", "short", "your-key-here", "xxxxxxxxxxxx", "changeme",
    "${DEEPSEEK_API_KEY}", "<your-api-key>", "sk-****", "****************",
    "aaaaaaaaaaaa", "test", "redacted", "os.environ[KEY]", "MY_OTHER_VAR",
])
def test_placeholders_are_not_flagged(value):
    assert _looks_like_placeholder(value) is True
    assert scan_text("x.py", f'DEEPSEEK_API_KEY="{value}"') == []


def test_non_secret_config_keys_are_not_flagged():
    """`yaml_key` / `cache_key` 这类配置键后缀不是 `_API_KEY`，不得被判成凭据。

    这是实现期真实踩到的误报：`yaml_key="Module.MyModule"` 曾被拦下。
    """
    assert scan_text("x.py", 'yaml_key="Module.MyModule"') == []
    assert scan_text("x.py", 'cache_key="user:profile:v2"') == []
    assert scan_text("x.py", 'sort_key="created_at"') == []


def test_ordinary_prose_is_clean():
    text = ("记忆、心情、觉察、L2、C6、user_name 六处都没用转义；"
            "HITL 审批不绑定审批连接——多客户端下可被绕过。")
    assert scan_text("docs/a.md", text) == []


# ═══════════════════════════════════════════════════════════════════
# 历史模式：必须真的读到 blob（实现期缺陷的回归网）
# ═══════════════════════════════════════════════════════════════════

def test_history_mode_actually_reads_blobs():
    """`--all-refs` 返回的是 (blob sha, 路径) —— 曾把 sha 当路径过滤，扫了 0 个文件。"""
    pairs = _tracked_files(all_refs=True)
    assert pairs, "历史模式必须至少列出一个对象"
    # rev-list --objects 同时列出 commit（无路径）/ tree（目录路径）/ blob ——
    # 取第一个**可扫的 blob** 做形状断言，而不是 pairs[0]
    scannable = [(i, p) for i, p in pairs if _scannable(p)]
    assert scannable, "历史模式必须至少列出可扫的 blob（否则等于没扫）"
    ident, path = scannable[0]
    assert len(ident) == 40 and all(c in "0123456789abcdef" for c in ident), ident
    assert path and not path.startswith(ident), "路径与标识必须分开"
    assert path != ident, "历史模式标识是 blob sha，不是路径"


def test_tree_mode_scans_tracked_files():
    pairs = _tracked_files(all_refs=False)
    assert ("core/agent/agent.py", "core/agent/agent.py") in pairs
    assert all(i == p for i, p in pairs), "工作树模式：标识即路径"


# 泄露所在的文件（与 2026-06 事故同名，便于人读时对上号）
_LEAK_DOC = "docs/review-2026-06-01.md"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """在合成仓库里跑一条 git 命令（显式钉住签名/身份，不读用户全局配置）。

    合成仓库：tmp 下 `git init` + 两个提交，**不联网、不碰本仓**，所以能留在
    quick 门禁（`-m "unit or service"`）里跑——CI 的报错正是由这类断言引起的，
    把它挪出 CI 等于让回归网失效。
    """
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=gate-test",
         "-c", "user.email=gate-test@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


def _repo_with_leak_only_in_history(tmp_path: Path) -> Path:
    """造一个真仓库：提交 1 抄录凭据，提交 2 把文件删掉——泄露只存在于历史里。"""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q")
    doc = repo / _LEAK_DOC
    doc.parent.mkdir(parents=True)
    doc.write_text(f"当时的值是 {_LEAK} 这样\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "抄录凭据")
    doc.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "移除抄录")
    return repo


def _run_gate(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """在 `repo` 里跑门禁脚本（cwd 决定它扫哪个仓库）。"""
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_secrets.py"), *args],
        capture_output=True, text=True, cwd=repo, timeout=timeout,
    )


def _git_here(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout


def test_end_to_end_history_mode_catches_leak_deleted_from_the_tree(tmp_path):
    """**核心证据**：能抓到"只存在于历史提交里"的凭据。

    左侧对照（同一仓库的工作树模式必须绿）让断言有鉴别力——证明它不是因为
    "扫什么都报红"才命中，而是真的读到了已被删除的历史 blob。
    """
    repo = _repo_with_leak_only_in_history(tmp_path)

    tree = _run_gate(repo)
    assert tree.returncode == 0, f"文件已删除，工作树模式不该命中\n{tree.stdout}"

    hist = _run_gate(repo, "--all-refs")
    assert hist.returncode == 1, f"历史里的泄露必须命中\n{hist.stdout[:500]}"
    assert _LEAK_DOC in hist.stdout
    assert "已知泄露指纹" in hist.stdout


def test_shallow_checkout_refuses_to_report_green(tmp_path):
    """浅克隆下 `--all-refs` 必须**拒答**（退出码 2），不得打印"✓ 通过"。

    这正是 CI 的情境：self-hosted job `git fetch --depth 1`、GitHub-hosted
    `actions/checkout@v4` 默认同样 depth 1。修前本用例是绿的假象——脚本对
    2026-06 那次泄露报"通过"（当时 CI 的失败就出在这里）。
    """
    origin = _repo_with_leak_only_in_history(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-tags",
                    f"file://{origin}", str(clone)], check=True, capture_output=True)

    is_shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                                cwd=clone, capture_output=True, text=True,
                                check=True).stdout.strip()
    assert is_shallow == "true", "前置条件：这个 clone 必须是浅克隆（否则本用例没在测该情形）"

    proc = _run_gate(clone, "--all-refs")
    assert proc.returncode == 2, f"历史不完整时必须拒答而不是报绿\n{proc.stdout[:500]}"
    assert "无法判定" in proc.stdout
    assert "✓" not in proc.stdout, "拒答时不得出现通过标记"


def test_real_repo_full_history_verdict_is_faithful():
    """真实仓库：全历史判定必须与 git 自己的 pickaxe（`log -S`）一致。

    期望值**现算**而不是写死"预期为红"：CR-01 步骤 3 清完历史后
    `docs/review-2026-06-01.md` 里的泄露就该消失，写死会让这条用例在 owner
    做对事情的那一刻变成一条永远修不好的红灯。

    浅克隆下无从判定 → 显式 skip（CI 即此类情形，skip 原因会打在摘要里，
    不会静默转绿）；脚本自身在那种环境下也已经拒答（退出码 2）。
    """
    if _git_here("rev-parse", "--is-shallow-repository").strip() == "true":
        pytest.skip("浅克隆（CI 的 checkout 是 --depth 1）：看不到完整历史，"
                    "全历史判据请在完整 clone 上跑 `make check-secrets-history`")

    # pickaxe 只看内容是否出现在某个提交的 diff 里，不依赖本扫描器的实现
    leaked = _git_here("log", "--all", "-S", _LEAK_PREFIX, "--format=%H",
                       "--", _LEAK_DOC).strip()

    proc = _run_gate(ROOT, "--all-refs", timeout=900)
    if leaked:
        assert proc.returncode == 1, f"历史里仍有泄露，必须命中\n{proc.stdout[:500]}"
        assert _LEAK_DOC in proc.stdout
        assert "已知泄露指纹" in proc.stdout
    else:
        assert proc.returncode == 0, f"历史已清理，必须转绿\n{proc.stdout[:500]}"


def test_working_tree_is_clean():
    """工作树必须干净——这是进 CI 的那条。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_secrets.py")],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    assert proc.returncode == 0, f"工作树不得有凭据形态命中\n{proc.stdout}"
