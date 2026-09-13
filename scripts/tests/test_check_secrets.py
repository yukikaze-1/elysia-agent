# 测试文件：scripts/tests/test_check_secrets.py
"""Secret scanning 门禁（CR-01 步骤 4）—— 先证明它抓得到，再相信它的绿灯。

门禁类脚本最大的风险不是"漏"而是"**假绿**"：本次实现过程中就出现过一次——
`--all-refs` 把 blob sha 当路径喂给后缀过滤，于是"历史模式"扫了 0 个文件却报通过。
`test_history_mode_actually_reads_blobs` 就是那条缺陷的回归网。
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


def test_end_to_end_catches_the_real_historical_leak():
    """**核心证据**：全历史模式必须命中 2026-06 那次真实泄露。

    这条用真实仓库历史验证，而不是合成 fixture——一个"在真实数据上什么都没发现"
    的扫描器，与一个坏掉的扫描器无法区分。
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_secrets.py"), "--all-refs"],
        capture_output=True, text=True, cwd=ROOT, timeout=900,
    )
    assert proc.returncode == 1, f"全历史必须命中（清历史前预期为红）\n{proc.stdout[:500]}"
    assert "docs/review-2026-06-01.md" in proc.stdout
    assert "已知泄露指纹" in proc.stdout


def test_working_tree_is_clean():
    """工作树必须干净——这是进 CI 的那条。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_secrets.py")],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    assert proc.returncode == 0, f"工作树不得有凭据形态命中\n{proc.stdout}"
