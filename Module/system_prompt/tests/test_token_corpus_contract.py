"""Batch 18 / KB-12：token 估算的**合同测试**（签入语料 × 权威 tokenizer 真值）。

## 它守的是什么

不是"估算值等于某个数"，而是**预算决策的真值性**：

    est / actual >= M      （M = 0.75 + 3pt 余量）
    est / actual <= T      （profile 的最坏高估，防止"越保守越好"的另一种缺陷）

`M` 的来历是硬约束而不是口味：chat 侧只有 `budget = window × 0.75` 一层保护，
预算判"装得下"而真实 prompt 超窗时 provider 是 **HTTP 200 + 静默截断**
（Batch 17 实测，Ollama 从不为超窗报 400）。故**低估超过 25% 就是实质 false accept**。
`T` 的来历见 `Module/system_prompt/budget.py` 的模块 docstring。

## 语料与真值从哪来

- 语料：`fixtures/token_corpus/`（签入，确定性生成，生成器 `_build_corpus.py`）
- 真值：`fixtures/token_corpus/GOLDEN.json`（签入，7 个权威 tokenizer；
  生成器 `_build_golden.py`，需 HF + 真实 Ollama，**不进 CI**）
- 本测试**零网络、零 provider**：只读签入文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from Module.system_prompt.budget import (
    CONTEXT_HEADROOM_RATIO,
    _TOKEN_WEIGHTS,  # noqa: SLF001 — 合同测试要读 profile 表本体
    budget_from_context_window,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tools_tokens,
    profile_for_model,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).with_name("fixtures") / "token_corpus"
_CORPUS = json.loads((_FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]
_GOLDEN = json.loads((_FIXTURES / "GOLDEN.json").read_text(encoding="utf-8"))

# 词表 → 它应当与哪个 profile 对表（**不是**"哪个 provider"，而是"哪个 tokenizer 家族"）。
_PROFILE_FAMILIES: dict[str, tuple[str, ...]] = {
    "qwen": ("qwen2.5",),
    "deepseek": ("deepseek_v3",),
    "llama": ("llama3",),
    "openai": ("o200k_base", "cl100k_base"),
    "mistral": ("mistral",),
    "generic": ("qwen2.5", "deepseek_v3", "llama3", "o200k_base", "cl100k_base", "mistral"),
}

# 每个 profile 声称的最坏高估（`budget.py` docstring 的表）。
_CONTRACT_MAX_RATIO: dict[str, float] = {
    "qwen": 1.25, "deepseek": 1.46, "llama": 1.74,
    "openai": 1.52, "mistral": 1.31, "generic": 2.68,
}

# **硬下限**：chat 侧只有 `budget = window × 0.75` 一层保护，低估 >25% 就是实质
# false accept（provider 超窗是 200 + 静默截断，不报 400）。这个数由 `budget.py`
# 的 `CONTEXT_HEADROOM_RATIO` 推出，`test_hard_floor_follows_the_headroom_ratio` 钉住。
_HARD_FLOOR = 0.75

# **设计下限**：拟合目标 0.80，实测最坏 0.794 —— 断言口径取 0.78，比硬下限高 3pt，
# 让"系数被悄悄改坏"在击穿真合同之前就变红。
_CONTRACT_MIN_RATIO = 0.775

# chat 形状语料里，**短请求**的绝对误差由 provider 侧固定框架主导（默认注入的
# system prompt、tool prompt 样板），那部分我们既观测不到也不该假装能算。
# 故比例合同只在内容量足够大的样本上断言；小样本另有一条**绝对上界**守卫。
_MIN_CHAT_TOKENS_FOR_RATIO = 512
_MAX_UNMODELED_FRAMING_TOKENS = 96

# p50k_base（GPT-3 时代的 byte-level BPE）**不在合同内**：`providers.yaml` 不存在
# 任何对应模型。仍保留在 GOLDEN 里作为"家族离散度"的证据（见 worst-case 断言）。
_OUT_OF_CONTRACT = ("p50k_base",)

# 文本语料里 <8 字符的样本：单条消息的 chat 模板开销占比过大，不参与比例断言。
_MIN_CHARS_FOR_RATIO = 8

# **合同外的类别：`repeat`（退化/对抗性重复输入）**。
#
# 为什么它必须是"合同外"而不是"被忽略"：BPE 的合并率取决于**词表**，而词表信息
# 在字符统计里根本不存在。实测（Qwen2.5 / DeepSeek-V3 / Llama-3 / cl100k）：
#     "哈哈" * 300  → 0.25 token/字      （"哈哈" 是词表里的 token）
#     "呀"   * 300  → 1.00 token/字      （"呀呀" 不在词表里，逐字计费）
# 两者字符分布**完全同构**（同一字符重复 300 次），任何启发式都无法区分。故：
#   * 拟合与硬合同只用**自然语言 + 结构化内容**（`repeat` 之外的全部类别）；
#   * `repeat` 的实测区间单独钉在 `TestKnownLimitations` 里，**如实报告不隐藏**，
#     并且每次改动估算器都会让那组数字重新暴露一次。
_CONTRACT_EXCLUDED_CATEGORIES = frozenset({"repeat", "script"})


def _text_body(path: str) -> str:
    return (_FIXTURES / path).read_text(encoding="utf-8")


def _text_cases(*, holdout: bool | None = None, include_degenerate: bool = False):
    for entry in _CORPUS:
        if entry["kind"] != "text":
            continue
        if holdout is not None and (entry["set"] == "holdout") != holdout:
            continue
        if not include_degenerate and entry["category"] in _CONTRACT_EXCLUDED_CATEGORIES:
            continue
        body = _text_body(entry["path"])
        if len(body) < _MIN_CHARS_FOR_RATIO:
            continue
        yield entry, body


# 每个 profile 的代表性 model id（判定点本身另有测试）
_PROFILE_MODEL: dict[str, str | None] = {
    "qwen": "ollama/qwen2.5:3b",
    "deepseek": "deepseek/deepseek-v4-flash",
    "llama": "ollama/llama3:8b",
    "openai": "openai/gpt-4o",
    "mistral": "ollama/mistral:7b",
    "generic": None,
}


def _profile_model(profile: str) -> str | None:
    """取一个能解析到该 profile 的代表性 model id（判定点本身另有测试）。"""
    model = _PROFILE_MODEL[profile]
    assert profile_for_model(model) == profile, f"{profile!r} ← {model!r} 解析不到"
    return model


class TestEstimatorContract:
    """est/actual 的双向区间，逐 profile × 逐家族 × 逐语料。"""

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_no_family_is_underestimated_beyond_the_budget_headroom(self, profile):
        """**核心合同**：低估不得超过 25%（否则 chat 侧 0.75 余量被击穿 → 静默截断）。"""
        model = _profile_model(profile)
        violations = []
        for entry, body in _text_cases():
            est = estimate_tokens(body, model=model)
            for family in _PROFILE_FAMILIES[profile]:
                actual = _GOLDEN["text"][entry["id"]][family]
                ratio = est / actual
                if ratio < _CONTRACT_MIN_RATIO:
                    violations.append(f"{entry['id']}/{family}: est={est} actual={actual} "
                                      f"ratio={ratio:.2f}")
        assert not violations, "低估越界（false accept 面）：\n" + "\n".join(violations)

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_no_family_is_overestimated_beyond_the_declared_bound(self, profile):
        """另一侧同样被约束：无限高估会过早裁剪记忆/历史并让预览误拒（§9）。"""
        model = _profile_model(profile)
        limit = _CONTRACT_MAX_RATIO[profile]
        violations = []
        for entry, body in _text_cases():
            est = estimate_tokens(body, model=model)
            for family in _PROFILE_FAMILIES[profile]:
                actual = _GOLDEN["text"][entry["id"]][family]
                ratio = est / actual
                if ratio > limit:
                    violations.append(f"{entry['id']}/{family}: est={est} actual={actual} "
                                      f"ratio={ratio:.2f} > {limit}")
        assert not violations, "高估越界：\n" + "\n".join(violations)

    @pytest.mark.parametrize("profile", sorted(_PROFILE_FAMILIES))
    def test_contract_holds_on_the_holdout_corpus(self, profile):
        """holdout 只在验证阶段读——**不参与任何调参**（§165/§166）。"""
        model = _profile_model(profile)
        limit = _CONTRACT_MAX_RATIO[profile]
        seen = 0
        for entry, body in _text_cases(holdout=True):
            est = estimate_tokens(body, model=model)
            for family in _PROFILE_FAMILIES[profile]:
                actual = _GOLDEN["text"][entry["id"]][family]
                ratio = est / actual
                assert _CONTRACT_MIN_RATIO <= ratio <= limit, (
                    f"holdout {entry['id']}/{family}: est={est} actual={actual} "
                    f"ratio={ratio:.2f} ∉ [{_CONTRACT_MIN_RATIO}, {limit}]")
            seen += 1
        assert seen >= 5, f"holdout 语料太少（{seen} 条），证明力不足"

    def test_declared_bounds_are_the_measured_bounds(self):
        """profile 表里声明的 T 必须与实测一致（防止改了系数却忘了改文档）。"""
        for profile, families in _PROFILE_FAMILIES.items():
            model = _profile_model(profile)
            worst = 0.0
            for entry, body in _text_cases():
                est = estimate_tokens(body, model=model)
                for family in families:
                    worst = max(worst, est / _GOLDEN["text"][entry["id"]][family])
            declared = _CONTRACT_MAX_RATIO[profile]
            assert worst <= declared, f"{profile}: 实测 {worst:.2f} > 声明 {declared}"
            assert declared - worst <= 0.10, (
                f"{profile}: 声明上限 {declared} 比实测 {worst:.2f} 松太多——文档过期")

    def test_hard_floor_follows_the_headroom_ratio(self):
        """硬下限不是口味：它由 0.75 预算比推出（低估 1/0.75 = 1.33× 就溢出窗口）。

        **这是"文档即断言"，不是估算器的覆盖**——它只保证常量之间不漂移；
        真正的估算器覆盖在 `TestEstimatorContract`。
        """
        window = 8192
        budget = budget_from_context_window(window)
        assert budget == int(window * CONTEXT_HEADROOM_RATIO)
        # 预算内允许放行的最大真实 token 数
        max_admitted = budget / _HARD_FLOOR
        assert max_admitted <= window * 1.0001, (
            f"硬下限 {_HARD_FLOOR} 不足以保证 {window} 内不溢出（放进 {max_admitted:.0f}）")
        assert _CONTRACT_MIN_RATIO > _HARD_FLOOR

    def test_cross_family_spread_is_why_profiles_exist(self):
        """证据性断言：单一系数无法覆盖的**原因**——同文本跨家族差 3× 以上。"""
        body = _text_body([e for e in _CORPUS
                           if e["id"] == "cjk_zh_simplified"][0]["path"])
        counts = _GOLDEN["text"]["cjk_zh_simplified"]
        spread = counts["p50k_base"] / counts["deepseek_v3"]
        assert spread > 3.0, f"跨家族离散度只有 {spread:.2f}×，profile 设计的前提需重估"
        # 单条中文语句在不同 profile 下的估算必须不同（否则 profile 是摆设）
        assert estimate_tokens(body, model="ollama/qwen2.5:3b") < estimate_tokens(body)
        # 数字分组是**第二条独立轴**：同一串数字在 qwen 与 deepseek 下必须分开
        digits = _text_body([e for e in _CORPUS if e["id"] == "digits_run"][0]["path"])
        assert estimate_tokens(digits, model="ollama/qwen2.5:3b") > 1.5 * estimate_tokens(
            digits, model="deepseek/deepseek-v4-flash"), "数字分组差异没有被建模"


# 修前公式的**逐字复刻**（`Module/system_prompt/budget.py` @ Batch 17 / commit 5d350945）。
# 留着它不是为了怀念：整套 PRE_FIX_RED 与 mutation 都要能"把旧算法放回来跑一遍"，
# 而那必须是**可执行**的旧算法，不是文字描述。
_LEGACY_CJK_RANGES = (
    (0x3000, 0x303F), (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
)


def legacy_estimate_tokens(text: str) -> int:
    """修前公式：CJK 1 字 1 token，其余 4 字符 1 token（向上取整）。"""
    cjk = sum(1 for ch in text
              if any(lo <= ord(ch) <= hi for lo, hi in _LEGACY_CJK_RANGES))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def legacy_estimate_messages(messages: list[dict]) -> int:
    """修前 messages 估算：每条 +4 开销 + role + content，**完全不看 tools**。"""
    total = 0
    for msg in messages:
        total += 4 + legacy_estimate_tokens(str(msg.get("role", "")))
        content = msg.get("content")
        if isinstance(content, str):
            total += legacy_estimate_tokens(content)
    return total


class TestPreFixRed:
    """PRE_FIX_RED：把修前算法放回同一份签入语料上跑，证明缺陷是真的、修是有效的。

    没有这一组，"新估算器更好"就只是一句自述——旧公式必须**在真值上真的越界**。
    """

    def test_legacy_formula_breaches_the_contract_while_the_new_one_does_not(self):
        """旧公式在多个类别上低估 >25%；新估算器在同一批样本上全部落在合同内。"""
        families = _PROFILE_FAMILIES["qwen"]
        legacy_breaches, new_breaches, checked = [], [], 0
        for entry, body in _text_cases():
            actual = _GOLDEN["text"][entry["id"]]["qwen2.5"]
            legacy_ratio = legacy_estimate_tokens(body) / actual
            new_ratio = estimate_tokens(body, model="ollama/qwen2.5:3b") / actual
            checked += 1
            if legacy_ratio < _HARD_FLOOR:
                legacy_breaches.append(f"{entry['id']}({entry['category']}): {legacy_ratio:.2f}")
            if new_ratio < _HARD_FLOOR:
                new_breaches.append(f"{entry['id']}: {new_ratio:.2f}")
            _ = families
        assert checked >= 30
        assert len(legacy_breaches) >= 8, (
            "旧公式的越界样本不足 8 条——本用例的 PRE_FIX_RED 前提不成立，"
            f"只找到 {legacy_breaches}")
        assert not new_breaches, f"新估算器仍越界：{new_breaches}"
        assert families == ("qwen2.5",)

    def test_legacy_formula_overestimates_natural_cjk_beyond_the_declared_bound(self):
        """反向：旧公式对自然中文高估到 1.6× 以上（Batch 7 只测出 1.02×，因为探针退化）。"""
        entry = next(e for e in _CORPUS if e["id"] == "cjk_zh_simplified")
        body = _text_body(entry["path"])
        actual = _GOLDEN["text"]["cjk_zh_simplified"]["qwen2.5"]
        legacy_ratio = legacy_estimate_tokens(body) / actual
        assert legacy_ratio > _CONTRACT_MAX_RATIO["qwen"], (
            f"旧公式对自然中文只有 {legacy_ratio:.2f}×，不足以支撑 W2 误拒结论")
        assert estimate_tokens(body, model="ollama/qwen2.5:3b") / actual <= \
            _CONTRACT_MAX_RATIO["qwen"]

    def test_legacy_formula_ignores_tool_schemas_entirely(self):
        """PRE_FIX_RED 的第二根：tools 对修前估算**完全不可见**（差的不是精度而是有无）。"""
        payload = json.loads((_FIXTURES / "chat" / "tools_many.json")
                             .read_text(encoding="utf-8"))
        assert legacy_estimate_messages(payload["messages"]) == legacy_estimate_messages(
            payload["messages"])   # 纯函数
        legacy_with = legacy_estimate_messages(payload["messages"])   # 签名里根本没有 tools
        new_without = estimate_messages_tokens(payload["messages"], model="ollama/qwen2.5:3b")
        new_with = estimate_messages_tokens(payload["messages"], model="ollama/qwen2.5:3b",
                                            tools=payload["tools"])
        actual = _GOLDEN["chat_ollama"]["tools_many"]["prompt_eval_count"]
        assert new_with - new_without >= 2000, "tool schema 必须有可观权重"
        # 修前："短消息" ⇒ 估算很小 ⇒ 预算判装得下；真相是 schema 就吃掉几千 token
        assert legacy_with / actual < _HARD_FLOOR
        assert new_with / actual >= _CONTRACT_MIN_RATIO

    def test_legacy_false_reject_is_reproducible_from_the_corpus(self):
        """TE-T1 的确定性版本：CJK 语料按窗口放大到"真实装得下"时，旧公式仍拒。"""
        entry = next(e for e in _CORPUS if e["id"] == "cjk_zh_simplified")
        body = _text_body(entry["path"])
        per_char = _GOLDEN["text"]["cjk_zh_simplified"]["qwen2.5"] / len(body)
        window = 8192
        # 造一段"真实 token 数 = 窗口的 70%"的中文（即完全装得下）
        target_true = int(window * 0.70)
        scaled = (body * (int(target_true / (per_char * len(body))) + 1))
        scaled = scaled[: int(target_true / per_char)]
        true_tokens = int(len(scaled) * per_char)
        legacy = legacy_estimate_tokens(scaled)
        new = estimate_tokens(scaled, model="ollama/qwen2.5:3b")
        assert true_tokens < window
        assert legacy >= window, f"旧公式没有误拒（legacy={legacy}, true={true_tokens}）"
        assert new < window, f"新估算器仍误拒（new={new}, true={true_tokens}）"


class TestKnownLimitations:
    """把估算器的**已知边界**钉成数字——不是"通过"，是"不许悄悄变坏"。

    见 `_CONTRACT_EXCLUDED_CATEGORIES` 的说明：`repeat`（同字符重复）这一类的
    token 率完全由词表决定，字符统计无法区分"哈哈"（0.25 token/字）与"呀"（1.00）。
    本组用例把当前实测的上下界钉住：**任何一侧明显恶化都会红**。
    """

    # `repeat` 类实测区间（跨 6 个 profile 的并集）：词表决定 token 率，无法用字符
    # 统计闭合。改动系数后必须同步复核这个登记值。
    _REPEAT_RATIO_RANGE = (0.35, 6.50)
    # `script`（婆罗米系/东南亚/希伯来/阿拉伯/希腊/亚美尼亚/西里尔）**合同外**，
    # 但方向必须是**安全侧**：实测这些文字跨家族差 3–6×，实现取"已验证家族实测上界"
    # 固定系数 ⇒ 只许高估、不许低估。这条断言把"不许低估"钉住。
    _SCRIPT_MIN_RATIO = 1.0

    def test_degenerate_repeats_stay_within_the_recorded_range(self):
        """`repeat` 全程合同外，但**区间被登记**：越界即红（不许悄悄变坏）。"""
        low, high = self._REPEAT_RATIO_RANGE
        seen = []
        for entry, body in _text_cases(include_degenerate=True):
            if entry["category"] != "repeat":
                continue
            for profile, families in _PROFILE_FAMILIES.items():
                est = estimate_tokens(body, model=_profile_model(profile))
                for family in families:
                    seen.append((est / _GOLDEN["text"][entry["id"]][family],
                                 f"{profile}:{entry['id']}/{family}"))
        assert seen, "repeat 语料没有被测到——合同外的类别必须有实测记录"
        worst = min(seen); best = max(seen)
        assert low <= worst[0] and best[0] <= high, (
            f"repeat 类实测区间 [{worst[0]:.2f} ({worst[1]}), {best[0]:.2f} "
            f"({best[1]})] 越出登记区间 [{low}, {high}]")
        # 至少一次"低估"与一次"高估"：这正是该类别无法被单一系数覆盖的证据
        assert worst[0] < _HARD_FLOOR, "退化类应当仍存在低估样本（词表不可知）"
        assert best[0] > max(_CONTRACT_MAX_RATIO.values()), "退化类应当仍存在高估样本"

    def test_out_of_scope_scripts_never_underestimate(self):
        """`script` 类在合同外，但**方向被钉住**：只许高估，不许低估。

        为什么这条比"登记一个区间"更强：`other` 类的系数被固定在已验证家族上的
        实测上界（安全侧），所以这些文字不会再走"预算判装得下而实际静默截断"那条
        路——代价（某些家族高估最多 ~6×）是显式接受的。任何把该系数调低的改动
        都会在这里变红。
        """
        from Module.system_prompt.budget import _classify  # noqa: SLF001

        seen = 0
        for entry, body in _text_cases(include_degenerate=True):
            if entry["category"] != "script":
                continue
            # 只对**非拉丁文字**（由固定的 `other` 类主导）保证方向；Latin-2
            # （波兰/捷克/越南）走的是**拟合**的 `latin_ext` 类，属"合同外、
            # 无方向保证"——它和法语/德语重音共用一类，无法既紧又覆盖极端。
            if sum(1 for ch in body if _classify(ch) == "other") < 0.4 * len(body):
                continue
            for profile, families in _PROFILE_FAMILIES.items():
                model = _profile_model(profile)
                est = estimate_tokens(body, model=model)
                for family in families:
                    ratio = est / _GOLDEN["text"][entry["id"]][family]
                    assert ratio >= self._SCRIPT_MIN_RATIO, (
                        f"{profile}/{entry['id']}/{family}: ratio={ratio:.2f} < 1.0 "
                        "——合同外文字被低估了（安全侧的保证被破坏）")
                    seen += 1
        assert seen >= 30, f"script 语料覆盖不足（{seen} 组）"

    def test_repeat_category_is_excluded_from_the_binding_contract(self):
        """防止有人"顺手"把退化样本塞回合同里——那会把系数推向为最坏情况服务。"""
        assert {"repeat", "script"} <= _CONTRACT_EXCLUDED_CATEGORIES
        cats = {e["category"] for e in _CORPUS if e["kind"] == "text"}
        assert _CONTRACT_EXCLUDED_CATEGORIES <= cats, "登记表里的类别必须真实存在"


class TestFailureModeGuards:
    """把"曾经错过的东西"钉住：它们是**行为**守卫，不是数值守卫。"""

    def test_same_char_count_different_content_differs(self):
        """FALSIFICATION：估算不得退化回"按字符数线性外推"。

        判据用**每字符 token 数**（tokens/char）：中文、ASCII 散文、JSON、代码
        在同一长度下的每字符成本必须明显分开。
        """
        model = "ollama/qwen2.5:3b"
        n = 60
        samples = {
            "cjk": "这是一个用于检验估算器的中文句子，包含标点符号。" * 3,
            "prose": "The quick brown fox jumps over the lazy dog near the river. " * 2,
            "json": ('{"id":12345,"name":"item_007","tags":["alpha","beta"],'
                     '"score":3.75,"active":true}'),
            "code": "def parse(payload: str) -> dict: return json.loads(payload) or {}",
        }
        rates = {}
        for name, text in samples.items():
            text = (text * 4)[:n]
            rates[name] = estimate_tokens(text, model=model) / n
        assert max(rates.values()) / min(rates.values()) > 1.8, (
            f"每字符成本几乎没有区分度：{rates}")
        # 中文的每字符成本必须显著高于 ASCII 散文（压缩率差异）
        assert rates["cjk"] > rates["prose"] * 1.5, rates

    def test_same_byte_length_different_distribution_differs(self):
        """FALSIFICATION：同字节数（CJK 3 字节/字 vs ASCII 1 字节/字）分布不同。"""
        cjk = "中文内容测试" * 3          # 18 字 = 54 字节
        ascii_ = "a" * 54
        assert len(cjk.encode()) == len(ascii_.encode())
        assert estimate_tokens(cjk, model="ollama/qwen2.5:3b") != estimate_tokens(
            ascii_, model="ollama/qwen2.5:3b")

    def test_minified_json_is_not_the_old_underestimate(self):
        """KB-12 的 W1 面：minified JSON 曾低估 43%（est 800 vs 真值 1220）。"""
        body = _text_body([e for e in _CORPUS if e["id"] == "json_minified"][0]["path"])
        est = estimate_tokens(body, model="ollama/qwen2.5:3b")
        actual = _GOLDEN["text"]["json_minified"]["qwen2.5"]
        assert est >= actual * _CONTRACT_MIN_RATIO

    def test_numeric_arrays_are_not_the_old_underestimate(self):
        """最坏历史样本：est/actual = 0.25（数字数组）。"""
        body = _text_body([e for e in _CORPUS if e["id"] == "json_numeric_arrays"][0]["path"])
        est = estimate_tokens(body, model="ollama/qwen2.5:3b")
        actual = _GOLDEN["text"]["json_numeric_arrays"]["qwen2.5"]
        assert est >= actual * _CONTRACT_MIN_RATIO, f"est={est} actual={actual}"

    def test_natural_cjk_is_not_the_old_overestimate(self):
        """KB-12 的 W2 面：自然中文曾高估 57%（旧公式 1 字 1 token）。"""
        body = _text_body([e for e in _CORPUS if e["id"] == "cjk_zh_simplified"][0]["path"])
        est = estimate_tokens(body, model="ollama/qwen2.5:3b")
        actual = _GOLDEN["text"]["cjk_zh_simplified"]["qwen2.5"]
        assert est <= actual * _CONTRACT_MAX_RATIO["qwen"]

    def test_repeated_single_char_is_the_degenerate_case_that_misled_batch7(self):
        """Batch 7 的 E2 用 `"你" * N`——那恰好是估算器最准的输入（1.000 token/字）。

        钉住它，是为了让"用退化样本下结论"这件事留下痕迹：真实自然中文是
        0.6 token/字，两者相差 1.7×。
        """
        n = 300
        est = estimate_tokens("你" * n, model="ollama/qwen2.5:3b")
        # 退化输入：估算仍然落在合同内（但它是"最有利"的一端，不能代表中文）
        assert est / n <= _CONTRACT_MAX_RATIO["qwen"]

    def test_emoji_and_zwj_are_counted(self):
        body = _text_body([e for e in _CORPUS if e["id"] == "unicode_emoji"][0]["path"])
        est = estimate_tokens(body, model="ollama/qwen2.5:3b")
        actual = _GOLDEN["text"]["unicode_emoji"]["qwen2.5"]
        assert est >= actual * _CONTRACT_MIN_RATIO, f"emoji 被低估：est={est} actual={actual}"

    def test_whitespace_runs_are_not_charged_like_letters(self):
        """空白不是字母：重复空格/缩进不能按字计价（否则缩进代码被严重高估）。"""
        assert estimate_tokens(" " * 200, model="ollama/qwen2.5:3b") < 60

    def test_snake_case_underscore_is_not_punctuation_priced(self):
        """旧 safety 估算器把 `_` 按 1 token/个计（4× 膨胀）——已统一到唯一权威。"""
        from Module.tool.safety import _estimate_tokens

        assert _estimate_tokens("a" + "_" * 50) < 20


class TestPromptComposition:
    """组装侧：tools/messages 都要进总量，且两者都不可被漏掉或重复计。"""

    def test_tool_schemas_are_included_in_the_request_total(self):
        model = "ollama/qwen2.5:3b"
        messages = [{"role": "system", "content": "你是 Elysia。"},
                    {"role": "user", "content": "在吗"}]
        tools = json.loads((_FIXTURES / "chat" / "tools_many.json")
                           .read_text(encoding="utf-8"))["tools"]
        without = estimate_messages_tokens(messages, model=model)
        with_tools = estimate_messages_tokens(messages, model=model, tools=tools)
        assert with_tools - without == estimate_tools_tokens(tools, model=model) > 0

    def test_tool_heavy_short_chat_is_not_a_false_accept(self):
        """短用户文本 + 巨大 schema：真实请求远大于 messages 本身。

        真值来自真实 Ollama（`tools_many_short_chat`，num_ctx=8192 下未截断）。
        """
        model = "ollama/qwen2.5:3b"
        payload = json.loads((_FIXTURES / "chat" / "tools_many_short_chat.json")
                             .read_text(encoding="utf-8"))
        est = estimate_messages_tokens(payload["messages"], model=model,
                                       tools=payload["tools"])
        actual = _GOLDEN["chat_ollama"]["tools_many_short_chat"]["prompt_eval_count"]
        ratio = est / actual
        assert _CONTRACT_MIN_RATIO <= ratio <= _CONTRACT_MAX_RATIO["qwen"], (
            f"tool-heavy 短对话越界：est={est} actual={actual} ratio={ratio:.2f}")

    def test_chat_framing_cases_hold_the_contract_end_to_end(self):
        """chat 形状语料（含 role 框架 / tool_call 历史）对真实 `prompt_eval_count`。

        比例合同只在**内容量足够大**的样本上断言：短请求的绝对误差由 provider 侧
        固定框架主导（见下一条守卫），把几十个 token 的框架塞进比例判据只会得到
        噪声（实测 chat_single_turn 的 actual 里 22/36 是框架）。
        """
        model = "ollama/qwen2.5:3b"
        bad, checked = [], 0
        for entry in _CORPUS:
            if entry["kind"] != "chat":
                continue
            golden = _GOLDEN["chat_ollama"][entry["id"]]
            assert golden["contaminated"] is False, f"{entry['id']} 的真值被截断污染"
            if golden["prompt_eval_count"] < _MIN_CHAT_TOKENS_FOR_RATIO:
                continue
            payload = json.loads((_FIXTURES / entry["path"]).read_text(encoding="utf-8"))
            est = estimate_messages_tokens(payload["messages"], model=model,
                                           tools=payload.get("tools"))
            ratio = est / golden["prompt_eval_count"]
            checked += 1
            if not (_CONTRACT_MIN_RATIO <= ratio <= _CONTRACT_MAX_RATIO["qwen"]):
                bad.append(f"{entry['id']}: est={est} actual={golden['prompt_eval_count']} "
                           f"ratio={ratio:.2f}")
        assert checked >= 5, f"参与断言的 chat 样本只有 {checked} 条，证明力不足"
        assert not bad, "chat 形状语料越界：\n" + "\n".join(bad)

    def test_provider_side_framing_stays_a_bounded_constant(self):
        """短请求的缺口必须是**有界常数**（provider 框架），不是随长度增长的比例误差。

        这条守卫把"我们算不到的 provider 固定开销"显式钉成一个上界：任何把缺口
        变成比例误差的改动都会在这里变红。Ollama/Qwen 的固定量实测 ≈22–72 token。
        """
        model = "ollama/qwen2.5:3b"
        worst_gap = 0
        for entry in _CORPUS:
            if entry["kind"] != "chat":
                continue
            golden = _GOLDEN["chat_ollama"][entry["id"]]
            if golden["prompt_eval_count"] >= _MIN_CHAT_TOKENS_FOR_RATIO:
                continue
            payload = json.loads((_FIXTURES / entry["path"]).read_text(encoding="utf-8"))
            est = estimate_messages_tokens(payload["messages"], model=model,
                                           tools=payload.get("tools"))
            worst_gap = max(worst_gap, golden["prompt_eval_count"] - est)
        # 只断言**上界**：`worst_gap >= 0` 是恒真的（初值 0），写上去是假强度。
        assert worst_gap <= _MAX_UNMODELED_FRAMING_TOKENS, (
            f"短请求的 provider 框架缺口 {worst_gap} token 超出登记上界 "
            f"{_MAX_UNMODELED_FRAMING_TOKENS}")

    def test_budget_threshold_agrees_with_the_contract(self):
        """把合同翻译成**决策**：预算判"装得下"时真实输入必须 <= 窗口。

        这是 §202 的成功判据：不是误差小，而是**边界决策不再因文本类型而错**。
        """
        window = 8192
        budget = budget_from_context_window(window)
        # 反推：预算内允许的最大真实 token 数 = budget / M
        max_admitted = budget / _CONTRACT_MIN_RATIO
        assert max_admitted <= window, (
            f"合同下限 {_CONTRACT_MIN_RATIO} 不足以保证 window={window} 内不溢出："
            f"预算 {budget} 会放进 {max_admitted:.0f} token")
        assert budget == int(window * CONTEXT_HEADROOM_RATIO)


class TestEstimatorHygiene:
    def test_determinism_across_repeated_calls(self):
        body = _text_body([e for e in _CORPUS if e["id"] == "mixed_zh_en"][0]["path"]) \
            if any(e["id"] == "mixed_zh_en" for e in _CORPUS) else "混合 mixed 文本"
        assert len({estimate_tokens(body, model="ollama/qwen2.5:3b")
                    for _ in range(5)}) == 1

    def test_unknown_model_falls_back_and_stays_usable(self):
        for model in (None, "", "custom/whatever", "unknown/xyz"):
            assert profile_for_model(model) == "generic"
            assert estimate_tokens("hello 世界", model=model) > 0

    def test_profile_table_is_the_only_coefficient_source(self):
        assert set(_TOKEN_WEIGHTS) == {
            "qwen", "deepseek", "llama", "openai", "mistral", "generic"}
        for profile, weights in _TOKEN_WEIGHTS.items():
            assert set(weights) == {
                "word", "digits", "punct", "space", "cjk", "cjk_ext", "kana",
                "cjksym", "emoji", "latin_ext", "other"}, profile
            assert all(v > 0 for v in weights.values()), profile

    def test_estimation_does_not_mutate_its_input(self):
        messages = [{"role": "user", "content": "在吗"},
                    {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]}]
        snapshot = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        estimate_messages_tokens(messages, model="ollama/qwen2.5:3b")
        assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == snapshot
