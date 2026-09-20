"""KB-78 / Batch 20：**真散文**拉丁文本的 token 估算合同（LT-T1..LT-T8）。

## 为什么需要这个文件（而不是只靠 `test_token_corpus_contract.py`）

`latin_ext` 系数原来由签入语料里的 `latin1_accented` 标定，而那条样本的变音密度是
**5.0%**——真捷克散文是 **12–14%**、真波兰语 2–8%。于是签入语料上的合同全绿，而
真实用户文本被低估到 **0.64–0.75**（< 0.75 硬下限 ⇒ 预算判"装得下"而 provider
静默截断）。**只跑语料测不出这个缺口**：合成样本比真散文更"重音密集"（czech 19.9% /
polish 10.5%），于是它恰好压在能过的那一侧。

本文件把**真散文**（Wikipedia 导言，CC BY-SA 4.0）作为 **holdout** 签入并钉住：
它**不参与拟合**，只做验证。真值来自 7 个权威 tokenizer，与 `GOLDEN.json` 同源同法。

## 真值怎么重新生成

```bash
conda run -n Elysia python scripts/token_corpus/build_latin_prose_holdout.py
```

该脚本（与 `build_golden.py` 同性质，**不在 CI 里**）重新抓取导言并用权威 tokenizer
（4 × HF `tokenizer.json` + 3 × tiktoken 编码，口径与 `build_golden.py` 逐字相同）
写回 `latin_prose_holdout.json` 与 `text/holdout_latin_*.txt`。

**本测试本身不需要 tokenizer**：真值已签入，只用 `estimate_tokens` 与它比对
（`_classify` 是纯字符统计）。故它是 `unit` 档、离线、确定性。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from Module.system_prompt.budget import (
    CONTEXT_HEADROOM_RATIO,
    UNCOVERED_CONTENT_CLASSES,
    _CLASSES,
    _TOKEN_WEIGHTS,
    estimate_tokens,
    profile_for_model,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures/token_corpus"
_HOLDOUT = json.loads((_FIXTURES / "latin_prose_holdout.json").read_text(encoding="utf-8"))

# 与 `test_token_corpus_contract.py` **同一份**口径（那两个常量是该测试的私有量，
# 这里按同值重述并在 `test_floors_match_the_contract_module` 里对 `budget.py` 复核）。
_CONTRACT_MIN_RATIO = 0.775
_HARD_FLOOR = 0.75

# profile → 该 profile 的验收家族（与合同测试同义）
_PROFILE_FAMILIES: dict[str, tuple[str, ...]] = {
    "qwen": ("qwen2.5",),
    "deepseek": ("deepseek_v3",),
    "llama": ("llama3",),
    "openai": ("cl100k_base", "o200k_base"),
    "mistral": ("mistral",),
    "generic": ("qwen2.5", "deepseek_v3", "llama3", "cl100k_base", "o200k_base", "mistral"),
}
_PROFILE_MODEL: dict[str, str | None] = {
    "qwen": "ollama/qwen2.5:3b",
    "deepseek": "deepseek/deepseek-v4-flash",
    "llama": "ollama/llama3:8b",
    "openai": "openai/gpt-4o",
    "mistral": "ollama/mistral:7b",
    "generic": None,
}
#: KB-78 重标定**之前**的 `latin_ext`（回归证据：抬升必须真的发生）
_LATIN_EXT_BEFORE = {
    "qwen": 0.8601, "deepseek": 1.0079, "llama": 1.6723,
    "openai": 1.1090, "mistral": 3.4031, "generic": 0.2000,
}
#: KB-78 重标定之后（= production 系数）
_LATIN_EXT_AFTER = {
    "qwen": 1.8700, "deepseek": 1.0079, "llama": 1.6800,
    "openai": 1.1090, "mistral": 3.4100, "generic": 0.5600,
}
#: 语料里三个 `latin_accent` 样本的重标定后实测区间（含越南语高估代价）
_LATIN_ACCENT_POST_CALIBRATION = {
    # sample → (min ratio, max ratio) over 6 profiles × 各自家族（实测，Batch 20）
    "script_latin2_czech": (0.779, 1.840),     # min @ generic/cl100k_base
    "script_latin2_polish": (0.837, 1.297),    # min @ generic/mistral
    "script_vietnamese": (1.182, 3.237),       # min @ generic/mistral，max @ llama/llama3
}
#: 真散文在**重标定后**的实测高估上界（逐 profile）。`mistral` 的 1.58 **超出**它自己的
#: 合同 T=1.31——那是**既有**行为（捷克语在 mistral tokenizer 下就是贵 1.5×），
#: 与本批的重标定无关（mistral 的 `latin_ext` 只从 3.4031 抬到 3.4100）。如实登记：
#: 高估方向是**安全侧**（只会过早裁预算 / 预览误拒，不会静默截断）。
_PROSE_MAX_RATIO = {
    "qwen": 0.950, "deepseek": 1.024, "llama": 1.640,
    "openai": 1.106, "mistral": 1.580, "generic": 1.338,
}
_LATIN_EXT_INDEX = _CLASSES.index("latin_ext")


def _samples():
    for rec in _HOLDOUT["samples"]:
        yield rec, (_FIXTURES / rec["path"]).read_text(encoding="utf-8")


def _class_counts(text: str) -> dict[str, int]:
    from Module.system_prompt.budget import _classify

    counts = dict.fromkeys(_CLASSES, 0)
    for ch in text:
        counts[_classify(ch)] += 1
    return counts


def _latin_need(body: str, profile: str, actual: int, target: float) -> float:
    """为达到 target ratio 所需的 `latin_ext`（其余系数不动）——**精确**仿射解。

    `ratio(w) = (Σ_{c≠latin_ext} n_c·w_c + n_latin_ext·w) / actual` 对 w 仿射，
    故 `w_need = (target·actual − base) / n_latin_ext`。
    """
    counts = _class_counts(body)
    n_li = counts["latin_ext"]
    assert n_li > 0, "该样本没有 latin_ext 字符，本推导不适用"
    weights = _TOKEN_WEIGHTS[profile]
    base = sum(counts[c] * weights[c] for c in _CLASSES if c != "latin_ext")
    return (target * actual - base) / n_li


# ---- LT-T1/T2/T3：真散文 floor（核心） ----


class TestRealProseLatinFloor:
    """捷克 + 波兰真散文（含**低密度波兰语**）必须进合同。"""

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_real_czech_and_polish_prose_respects_the_contract_floor(self, profile):
        """**KB-78 的核心断言**：真散文 est/actual ≥ 0.775，逐 profile × 逐家族。

        修前这里是最低 **0.637**（qwen/捷克）——17 条 < 0.75。
        """
        model = _PROFILE_MODEL[profile]
        assert profile_for_model(model) == profile
        violations = []
        for rec, body in _samples():
            assert rec["lang"] in ("cs", "pl")
            est = estimate_tokens(body, model=model)
            for family in _PROFILE_FAMILIES[profile]:
                actual = rec["tokens"][family]
                ratio = est / actual
                if ratio < _CONTRACT_MIN_RATIO:
                    violations.append(f"{rec['id']}/{family}: est={est} actual={actual} "
                                      f"ratio={ratio:.3f}")
        assert not violations, (
            f"真散文低估越界（{profile}）：\n" + "\n".join(violations))

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_real_prose_overestimation_is_bounded_and_registered(self, profile):
        """另一侧：真散文的高估必须**有界且已登记**（重标定的代价必须可见）。

        ⚠️ 这里**不**断言 ≤ 该 profile 的合同 T：`mistral` 真散文实测 1.58 > T=1.31，
        而那是**既有**行为——合同 T 是在**签入语料**上测的，真捷克/波兰散文在
        mistral tokenizer 下本来就贵 1.5×。本批的 `latin_ext` 对 mistral 只动了
        3.4031→3.4100，既不制造也不掩盖它。于是本测试钉的是"登记值本身"：
        超界必须**被写下来**，不许悄悄漂。
        """
        worst = max(
            (estimate_tokens(body, model=_PROFILE_MODEL[profile]) / rec["tokens"][family],
             rec["id"], family)
            for rec, body in _samples()
            for family in _PROFILE_FAMILIES[profile]
        )
        registered = _PROSE_MAX_RATIO[profile]
        assert worst[0] == pytest.approx(registered, abs=0.01), (
            f"真散文高估实测 {worst[0]:.3f}（{worst[1]}/{worst[2]}）≠ 登记 {registered}——"
            f"若确属改进请同步 _PROSE_MAX_RATIO 与 budget.py 的说明")

    def test_low_density_polish_prose_is_in_the_holdout(self):
        """**低密度波兰语真散文必须在场**（Batch 19 明确要求）。

        判据是实测变音密度，不是文件名：至少一条 pl 样本的 `latin_ext` 占比 < 4%。
        只用高密度样本调参正是原缺口的成因。
        """
        densities = []
        for rec, body in _samples():
            if rec["lang"] != "pl":
                continue
            counts = _class_counts(body)
            densities.append((counts["latin_ext"] / max(1, sum(counts.values())), rec["id"]))
        assert densities, "holdout 里没有波兰语样本"
        assert min(densities)[0] < 0.04, (
            f"缺少低密度波兰语真散文（最低 {min(densities)[0]*100:.1f}% @ {min(densities)[1]}）"
            "——高密度样本会掩盖低密度样本的低估面")


# ---- LT-T4/T5/T6/T7/T8：不回归 ----


class TestNoRegressionAcrossFamilies:
    """重标定只动 `latin_ext`：其它文本类与其它 profile 必须逐位不变。"""

    def test_only_latin_ext_changed(self):
        """系数表里**只有** `latin_ext` 与登记值不同（防"顺手调其它系数"）。"""
        for profile, weights in _TOKEN_WEIGHTS.items():
            assert weights["latin_ext"] == pytest.approx(_LATIN_EXT_AFTER[profile]), profile
            assert weights["latin_ext"] >= _LATIN_EXT_BEFORE[profile], (
                f"{profile}: latin_ext 被调低了（{_LATIN_EXT_BEFORE[profile]} → "
                f"{weights['latin_ext']}）——KB-78 的方向是**抬升**")
        # 只有"实际未达标"的 profile 才允许改（openai/deepseek 本来就过）
        changed = {p for p in _TOKEN_WEIGHTS
                   if _TOKEN_WEIGHTS[p]["latin_ext"] != _LATIN_EXT_BEFORE[p]}
        assert changed == {"qwen", "llama", "mistral", "generic"}, (
            f"本批只该改实际未达标的 4 个 profile，实际改了 {sorted(changed)}")

    def test_json_code_cjk_samples_are_unaffected(self):
        """JSON / 代码 / CJK 样本的估算值完全不受影响。

        用"同一文本在重标定前后的估算值之差"表达：只有含 `latin_ext` 的样本允许变，
        且变化量必须**正好**等于 `Δw × n_latin_ext`（可逐位核对，不是"大致不变"）。
        """
        corpus = _FIXTURES
        manifest = json.loads((corpus / "MANIFEST.json").read_text(encoding="utf-8"))
        for entry in manifest["entries"]:
            if entry["kind"] != "text":
                continue
            body = (corpus / entry["path"]).read_text(encoding="utf-8")
            counts = _class_counts(body)
            n_li = counts["latin_ext"]
            for profile, model in (("qwen", "ollama/qwen2.5:3b"), ("generic", None)):
                after = estimate_tokens(body, model=model)
                weights = _TOKEN_WEIGHTS[profile]
                after_total = sum(counts[c] * weights[c] for c in _CLASSES if counts[c])
                # 与 `estimate_tokens` 同款收口：空串 → 0；非空 → ceil 且至少 1
                expected_after = 0 if not body else max(1, math.ceil(after_total))
                assert after == expected_after, (
                    f"{entry['id']}/{profile}: 估算值 {after} ≠ 复算 {expected_after}")
                if n_li == 0:
                    before_total = sum(counts[c] * _LATIN_EXT_BEFORE[profile] if c == "latin_ext"
                                       else counts[c] * weights[c]
                                       for c in _CLASSES if counts[c])
                    expected_before = 0 if not body else max(1, math.ceil(before_total))
                    assert after == expected_before, (
                        f"{entry['id']}/{profile}: 无 latin_ext 的样本估算被改动"
                        f"（{expected_before} → {after}）")
                    continue
                # 含 latin_ext 的样本：变化量必须正好是 Δw × n_li
                before_total = sum(counts[c] * _LATIN_EXT_BEFORE[profile] if c == "latin_ext"
                                   else counts[c] * weights[c] for c in _CLASSES if counts[c])
                assert after_total - before_total == pytest.approx(
                    (weights["latin_ext"] - _LATIN_EXT_BEFORE[profile]) * n_li), (
                    f"{entry['id']}/{profile}: 变化量不是纯粹的 latin_ext 抬升")

    def test_unknown_and_custom_models_use_the_generic_profile(self):
        """LT-T8：未知 / `custom` 模型落到 `generic`，且 generic 的真散文也进合同。"""
        for model in (None, "custom/my-local-model", "mimo/mimo-v2.5", "ollama/unknown:1b"):
            assert profile_for_model(model) == "generic", model
        violations = [
            (rec["id"], family, estimate_tokens(body, model=None) / rec["tokens"][family])
            for rec, body in _samples()
            for family in _PROFILE_FAMILIES["generic"]
            if estimate_tokens(body, model=None) / rec["tokens"][family] < _CONTRACT_MIN_RATIO
        ]
        assert not violations, f"generic profile 的真散文低估越界：{violations}"


# ---- 重标定推导：可执行、可复核 ----


class TestCoefficientDerivation:
    """把"系数从哪来"变成**可执行推导**，而不是注释里的数字。

    这不是同义反复：推导只读签入 fixture 的真值与 `_classify`，独立算出所需系数，
    再断言 production 表里的值**确实满足**它。任何一侧被改动（fixture / 公式 /
    系数）都会红。
    """

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_latin_ext_covers_every_holdout_sample(self, profile):
        """逐样本：现系数必须 ≥ 该样本达到合同下限所需的系数。"""
        for rec, body in _samples():
            need = max(_latin_need(body, profile, rec["tokens"][f], _CONTRACT_MIN_RATIO)
                       for f in _PROFILE_FAMILIES[profile])
            have = _TOKEN_WEIGHTS[profile]["latin_ext"]
            assert have >= need - 1e-9, (
                f"{profile}/{rec['id']}: latin_ext={have} < 所需 {need:.4f}")

    def test_corpus_floor_also_binds(self):
        """语料侧的 floor 也必须被覆盖（否则"只拟合真散文"会漏掉已签入证据）。"""
        manifest = json.loads((_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
        golden = json.loads((_FIXTURES / "GOLDEN.json").read_text(encoding="utf-8"))
        for entry in manifest["entries"]:
            if entry["kind"] != "text" or entry["category"] != "latin_accent":
                continue
            body = (_FIXTURES / entry["path"]).read_text(encoding="utf-8")
            for profile, families in _PROFILE_FAMILIES.items():
                for family in families:
                    actual = golden["text"][entry["id"]][family]
                    need = _latin_need(body, profile, actual, _CONTRACT_MIN_RATIO)
                    have = _TOKEN_WEIGHTS[profile]["latin_ext"]
                    if actual and need > 0:      # need ≤ 0 ⇒ 该样本本来就达标
                        assert have >= need - 1e-9, (
                            f"{profile}/{entry['id']}/{family}: latin_ext={have} < 所需 {need:.4f}")

    def test_floors_match_the_contract_module(self):
        """本文件重述的两个下限必须与 `budget.py` 的余量比例一致（防漂移）。"""
        # 硬下限**就是**余量比例：`budget = window × 0.75` ⇒ 低估 > 25% 即 false accept
        assert CONTEXT_HEADROOM_RATIO == _HARD_FLOOR
        assert _CONTRACT_MIN_RATIO > _HARD_FLOOR, "合同下限必须严格高于硬下限"


# ---- 越南语：合同外的代价被登记，不许藏 ----


class TestVietnameseStaysOutOfContract:
    """`latin_accent` 剩下的唯一成员是越南语——它与捷克/波兰**互斥**，如实登记。"""

    def test_latin_accent_is_still_a_registered_uncovered_class(self):
        assert "latin_accent" in UNCOVERED_CONTENT_CLASSES, (
            "latin_accent 被移出未覆盖类——越南语并不达标（见下），"
            "移出会让它的高估/低估失去登记")

    def test_vietnamese_overestimation_cost_is_registered(self):
        """重标定的**代价**必须可见：越南语在 qwen/generic 上被高估 ~3×，且方向安全。"""
        corpus = _FIXTURES
        golden = json.loads((corpus / "GOLDEN.json").read_text(encoding="utf-8"))
        body = (corpus / "text/script_vietnamese.txt").read_text(encoding="utf-8")
        assert estimate_tokens(body, model="ollama/qwen2.5:3b") > 0
        ratios = []
        for profile, families in _PROFILE_FAMILIES.items():
            est = estimate_tokens(body, model=_PROFILE_MODEL[profile])
            for family in families:
                actual = golden["text"]["script_vietnamese"][family]
                if actual:
                    ratios.append((est / actual, f"{profile}/{family}"))
        low = min(ratios)
        high = max(ratios)
        # 方向必须是**安全侧**：不再低估（≥1.0）
        assert low[0] >= 1.0, (
            f"越南语被低估了（{low[1]}={low[0]:.3f}）——latin_ext 抬升本该只往安全侧走")
        assert 1.5 <= high[0] <= 4.0, (
            f"越南语高估代价越出登记区间（{high[1]}={high[0]:.3f}）")

    def test_corpus_latin_accent_ranges_are_pinned(self):
        """语料三条 `latin_accent` 样本的实测区间被钉住（任何一侧明显恶化即红）。"""
        corpus = _FIXTURES
        golden = json.loads((corpus / "GOLDEN.json").read_text(encoding="utf-8"))
        for entry_id, (low_exp, high_exp) in _LATIN_ACCENT_POST_CALIBRATION.items():
            body = (corpus / f"text/{entry_id}.txt").read_text(encoding="utf-8")
            ratios = []
            for profile, families in _PROFILE_FAMILIES.items():
                est = estimate_tokens(body, model=_PROFILE_MODEL[profile])
                for family in families:
                    actual = golden["text"][entry_id][family]
                    if actual:
                        ratios.append(est / actual)
            low, high = min(ratios), max(ratios)
            assert low == pytest.approx(low_exp, abs=0.02), (
                f"{entry_id}: 最低 ratio {low:.3f} ≠ 登记 {low_exp}")
            assert high == pytest.approx(high_exp, abs=0.02), (
                f"{entry_id}: 最高 ratio {high:.3f} ≠ 登记 {high_exp}")


# ---- fixture 完整性 ----


class TestHoldoutIntegrity:
    def test_text_matches_the_recorded_hash(self):
        """签入的文本必须与 `sha256` 一致——否则真值就与文本脱钩了。"""
        for rec, body in _samples():
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            assert digest == rec["sha256"], f"{rec['id']}: 文本与登记哈希不一致"
            assert len(body) == rec["chars"], f"{rec['id']}: 字符数与登记不一致"

    def test_holdout_covers_both_languages_and_seven_tokenizers(self):
        langs = {rec["lang"] for rec, _ in _samples()}
        assert langs == {"cs", "pl"}, f"holdout 语言覆盖不足：{langs}"
        families = set(_HOLDOUT["tokenizers"])
        assert families == {"qwen2.5", "deepseek_v3", "llama3", "mistral",
                            "cl100k_base", "o200k_base", "p50k_base"}, families
        for rec, _ in _samples():
            assert set(rec["tokens"]) == families, rec["id"]
            assert all(v > 0 for v in rec["tokens"].values()), rec["id"]

    def test_source_is_licensed_for_redistribution(self):
        """来源必须是可再分发的许可（CC BY-SA 4.0），并带署名与许可链接。"""
        src = _HOLDOUT["source"]
        assert "CC BY-SA" in src["license"]
        assert src["license_url"].startswith("https://creativecommons.org/")
        assert src["attribution"]
        assert src["titles"], "缺每篇样本的条目标题 ⇒ 无法核对署名"

    def test_no_prose_sample_exceeds_the_excerpt_budget(self):
        """单篇样本不得超长（保持"小样本"性质，不做大段搬运）。"""
        for rec, body in _samples():
            assert len(body) <= 1000, f"{rec['id']}: {len(body)} 字符，超出小样本上限"
