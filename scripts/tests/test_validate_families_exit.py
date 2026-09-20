# 测试文件：scripts/tests/test_validate_families_exit.py
"""KB-79 / Batch 19：维护者工具 `validate_families.py` 的**退出合同**（fail-closed）。

它不进 CI（要下载 tokenizer 文件），跑的时机正是"新增/升级 provider 家族"这种
**没人会复核脚本自身行为**的时刻，所以它的假绿代价特别高。修前的实况：全部家族
SKIP（无网络 / 无法解析 revision）时打印几行 SKIP 然后 **exit 0** —— "什么都没检查"
看起来和"检查通过"一样（§136 的三种假绿之三：环境不满足必须**拒答**）。

本组覆盖 §102 要求的五件事，全部**离线**（`huggingface_hub` / `tokenizers` 都是
函数内导入，用假模块注入 sys.modules）：

1. 正常成功 → verdict 行 + exit 0；
2. 有家族未覆盖 → exit 1（不是 0，也不是 2）；
3. **全部 SKIP → exit 2 拒答**（本组核心）；
4. 坏 revision（Hub 返回非 40 位 SHA）→ 该家族 SKIP，不落进下载路径；
5. 合同过滤是**单一权威**：主判定只用合同内类别，`latin_accent` 单独报告（KB-78）；
   且该类的**击穿**仍必须让退出码变红（Batch 20 闭合后由 ⑤b 显式守住"再坏一次"）。
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "token_corpus" / "validate_families.py"
_CORPUS = _REPO / "Module/system_prompt/tests/fixtures/token_corpus"


def _load_module():
    spec = importlib.util.spec_from_file_location("b19_validate_families", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def vf():
    return _load_module()


@pytest.fixture()
def fake_hub(monkeypatch):
    """假 `huggingface_hub`：`HfApi().model_info(repo).sha` 由测试控制。"""
    state = {"sha": "a" * 40, "raise": False}

    class _Info:
        def __init__(self, sha): self.sha = sha

    class _HfApi:
        def model_info(self, repo, revision=None):
            if state["raise"]:
                raise RuntimeError("offline")
            return _Info(state["sha"])

    mod = types.ModuleType("huggingface_hub")
    mod.HfApi = _HfApi  # type: ignore[attr-defined]
    mod.hf_hub_download = lambda **kw: "/tmp/fake-tokenizer.json"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
    return state


@pytest.fixture()
def fake_tokenizer(monkeypatch):
    """假 tokenizer：按"目标 est/actual 比"合成 ids 数（离线、确定性）。

    - 合同内样本：目标 0.90（⇒ OK）或 0.70（⇒ NOT COVERED，由 `contract_ratio` 控制）
    - `latin_accent` 样本：由 `accent_ratio` 控制。**默认 0.90**（KB-78 已于 Batch 20
      闭合 ⇒ 该类不该再影响退出码）；需要验证"缺口仍在时必须红"的用例把它调到 0.60。
    """
    from Module.system_prompt.budget import _TOKEN_WEIGHTS, profile_for_model

    entries = json.loads((_CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]
    latin_bodies = {(_CORPUS / e["path"]).read_text(encoding="utf-8")
                    for e in entries if e["category"] == "latin_accent"}
    state = {"contract_ratio": 0.90, "accent_ratio": 0.90}

    def _est(text: str, model: str | None) -> float:
        from Module.system_prompt.budget import _CLASSES, _classify
        weights = _TOKEN_WEIGHTS[profile_for_model(model)]
        counts = dict.fromkeys(_CLASSES, 0)
        for ch in text:
            counts[_classify(ch)] += 1
        return sum(counts[k] * weights[k] for k in _CLASSES)

    class _Enc:
        def __init__(self, n): self.ids = list(range(max(1, n)))

    class _Tokenizer:
        @classmethod
        def from_file(cls, _path):
            return cls()

        def encode(self, text, add_special_tokens=False):
            ratio = state["contract_ratio"]
            if text in latin_bodies:
                ratio = state["accent_ratio"]
            est = _est(text, "ollama/qwen2.5:3b")
            return _Enc(math.ceil(est / ratio))

    mod = types.ModuleType("tokenizers")
    mod.Tokenizer = _Tokenizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenizers", mod)
    return state


def test_normal_success_exits_zero(vf, fake_hub, fake_tokenizer, capsys):
    """① 合同内类别全部达标 → exit 0，且报告里说明了校验了几个家族。"""
    assert vf.main(["Qwen/Qwen2.5-3B"]) == 0
    out = capsys.readouterr().out
    assert "已校验 1 个家族" in out
    assert "NOT COVERED" not in out
    assert "拒答" not in out


def test_uncovered_family_exits_one(vf, fake_hub, fake_tokenizer, capsys):
    """② 有家族未覆盖 → exit 1（与"拒答"区分开）。"""
    fake_tokenizer["contract_ratio"] = 0.70
    assert vf.main(["Qwen/Qwen2.5-3B"]) == 1
    out = capsys.readouterr().out
    assert "NOT COVERED" in out


def test_all_skip_refuses_to_answer(vf, fake_hub, fake_tokenizer, capsys):
    """③ **核心**：全部家族 SKIP 时拒答（exit 2），不再假绿。"""
    fake_hub["raise"] = True
    assert vf.main(["nonexistent/repo-xyz", "also-missing/foo"]) == 2
    out = capsys.readouterr().out
    assert "拒答" in out
    assert "SKIP" in out


def test_bad_revision_is_skipped_then_refuses(vf, fake_hub, fake_tokenizer, capsys):
    """④ Hub 返回非 40 位 SHA ⇒ 不落进下载路径（浮动引用），单独跑即拒答。"""
    fake_hub["sha"] = "main"          # 分支名：正是 B615 要消除的浮动引用
    assert vf.main(["some/unpinned-repo"]) == 2
    out = capsys.readouterr().out
    assert "不是 40 位 commit SHA" in out
    assert "拒答" in out


def test_partial_skip_still_produces_verdicts(vf, fake_hub, fake_tokenizer, capsys):
    """②b 部分 SKIP 不应抹掉其余家族的结论（exit 仍由真实结论决定）。"""
    fake_hub["sha"] = "main"
    assert vf.main(["some/unpinned-repo", "Qwen/Qwen2.5-3B"]) in (0, 1)
    out = capsys.readouterr().out
    assert "已校验 1 个家族" in out
    assert "拒答" not in out


def test_contract_filter_is_single_authority(vf, fake_hub, fake_tokenizer, capsys):
    """⑤ 主判定只用合同内类别；`latin_accent` 单独报告（对齐过滤 ≠ 藏起缺口）。"""
    from Module.system_prompt.budget import UNCOVERED_CONTENT_CLASSES

    assert vf.main(["Qwen/Qwen2.5-3B"]) == 0
    out = capsys.readouterr().out
    # 未覆盖类来自单一权威常量，而不是脚本自己写的一份
    assert str(sorted(UNCOVERED_CONTENT_CLASSES)) in out
    assert "latin_accent" in out and "└ latin_accent" in out
    # 主判定的 worst 样本不得是 latin_accent（那说明过滤没生效）
    main_line = next(line for line in out.splitlines() if line.startswith("Qwen/Qwen2.5-3B"))
    assert "script_latin2" not in main_line
    # 而 latin_accent 的结论如实打在单独一行里——KB-78 闭合后是"在合同内"，
    # 且脚本必须**同时**说清"未覆盖类只剩越南语"，避免读者把"这一类在合同内"
    # 误读成"整类被覆盖"。
    accent_line = next(line for line in out.splitlines() if "└ latin_accent" in line)
    assert "在合同内" in accent_line
    assert "只剩越南语" in out


def test_accent_floor_breach_still_turns_red(vf, fake_hub, fake_tokenizer, capsys):
    """⑤b `latin_accent` 若**再次**击穿硬下限，退出码必须变红（缺口不许被藏起来）。

    这条是在替 KB-78 的**将来**把关：Batch 20 之前它红是真的红；闭合之后默认不再红，
    但"再坏一次"必须还能报出来——所以这里显式把该类的 ratio 调回击穿区间。
    """
    fake_tokenizer["accent_ratio"] = 0.60
    assert vf.main(["Qwen/Qwen2.5-3B"]) == 1
    out = capsys.readouterr().out
    accent_line = next(line for line in out.splitlines() if "└ latin_accent" in line)
    assert "击穿硬下限" in accent_line
    assert "已知缺口" in out
