"""验证估算器在**指定 tokenizer 家族**上的 est/actual 区间。

    用法：conda run -n Elysia python scripts/token_corpus/validate_families.py [repo_id ...]

退出码：**0 = 至少校验了一个家族且无未覆盖家族**；**1 = 有家族未覆盖**（min < 0.75）；
**2 = 拒答**（没有任何家族产出结论，例如全部 SKIP / 无网络）。三者都打印人可读原因。

## 为什么需要这个入口

`Module/system_prompt/budget.py` 的合同是**按 profile 声明**的，而每个 profile 的
声明值只在**一组已验证的家族**上成立：

    qwen     ←  Qwen2.5（HF + 真实 Ollama 双证）、Qwen3
    deepseek ←  DeepSeek-V3
    llama    ←  Llama-3-8B、OLMo-7B
    mistral  ←  Mistral-7B-v0.3
    openai   ←  o200k_base / cl100k_base（GOLDEN，不在本脚本的默认集里）
    generic  ←  上述全部（未知模型的**保守并集**回退）

`generic` 是**未知模型的回退**，而"未知"意味着它迟早会遇到没验证过的家族。本脚本就是
那条"把新家族纳入/排除验证范围"的可执行入口：给定 HF repo，直接报出该家族在每个
profile 下的 `min/median/max` 比值与越界样本。

**它不进 CI**（要下载 tokenizer 文件，§61/§124 禁止把词表签入仓库）。它是维护者工具：
新增或升级 provider/model 家族时跑一次，把结果写进执行记录，并据此决定是"纳入拟合"
还是"如实登记为未覆盖"。

## 与 CI 合同的关系（Batch 19 / KB-78 起：单一权威）

"哪些内容类不参与硬合同"由 `budget.UNCOVERED_CONTENT_CLASSES` **单一定义**，本脚本
不再自己维护一份过滤条件（此前是 `!= "repeat"`，与合同测试的 `{"repeat","script"}`
不一致 ⇒ 会对合同**故意排除**的样本报 `NOT COVERED`，属误导性的假红；见 KB-79）。
本脚本现在：
  * **主判定**只用合同内类别（与 CI 合同同一份集合）；
  * `latin_accent`（密集变音符号的拉丁文本，实测击穿 0.75）**单独一行**报告——
    对齐过滤条件**不得**变成把已知缺口藏起来（KB-78）。

## 已知结论（Batch 18 实测，2026-09-19；Batch 19 复核）

| 家族 | profile | min est/actual | 判定 |
|---|---|---|---|
| Qwen2.5-3B / DeepSeek-V3 / Llama-3 / Mistral（拟合集） | 各自 profile | ≥0.78 | ✅ |
| OLMo-7B / Qwen3-8B | `llama` / `qwen` | ≥0.78 | ✅ |
| Yi-1.5-6B / Phi-3-mini-4k | `generic`（名字不匹配任何家族标记） | ≥0.78 | ✅ `generic` 是全部已验证家族的**保守并集** |

**未覆盖的是"内容"而不是"家族"**：`script`（婆罗米系/东南亚/希伯来/阿拉伯/希腊/
亚美尼亚/西里尔）由固定系数覆盖（只高估，TE7）；`repeat` 只登记区间；而
`latin_accent` **会低估**（捷克 0.65 / 波兰 0.71，qwen profile）——它是**真实缺口**，
修法（重标定 `latin_ext`）已验证可行但属独立的估算器标定批次（KB-78）。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = _REPO / "Module/system_prompt/tests/fixtures/token_corpus"
sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# 远端 tokenizer 的**单一权威来源表**（repo → 不可变 revision）
# ---------------------------------------------------------------------------
#
# 为什么必须有这张表：`hf_hub_download` 不传 `revision` 就是**浮动引用**——今天拉到
# 的 `tokenizer.json` 与三个月后的可能不是同一份，而本脚本存在的意义正是"对一个
# **可复现**的 tokenizer 家族验证估算合同"（bandit B615 / CWE-494）。
#
# revision 全部是 **40 位 commit SHA**（不可变）。来源与证明：
#   * 直接取自 Hugging Face repository metadata（`HfApi().model_info(repo).sha`，
#     并回验 `model_info(repo, revision=sha).sha == sha`）；
#   * 其中 5/6 与本机 HF cache 的 `snapshots/<sha>/` 目录**逐字相同** ⇒ 就是
#     Batch 18 标定/验证实际拉到的那一份（`~/.cache/huggingface/hub/`）。
#   * `allenai/OLMo-7B-0724-hf` 无本地 cache 记录（当时下载到 /tmp，已清理），
#     故只以 API metadata 为准，并已单独回验该 revision 下存在 `tokenizer.json`。
#
# 换 revision 的纪律：这是一次**基准重标定**，不是顺手升级——换了之后
# `GOLDEN.json` 的对应列与文档里的 est/actual 区间都要重测。
TOKENIZER_SOURCES: dict[str, dict[str, str]] = {
    "01-ai/Yi-1.5-6B": {
        "revision": "157a3d77ab5a8a8f079510eaa25169b547481f06",
        "family": "Yi（合同外家族，用于演示 conservative fallback）",
    },
    "microsoft/Phi-3-mini-4k-instruct": {
        "revision": "f39ac1d28e925b323eae81227eaba4464caced4e",
        "family": "Phi-3（同上）",
    },
    "allenai/OLMo-7B-0724-hf": {
        "revision": "1ee306df318ee15bfe4a76ebd5c002b0105b1ab6",
        "family": "OLMo（llama profile 的外部验证）",
    },
    "Qwen/Qwen3-8B": {
        "revision": "b968826d9c46dd6066d109eabc6255188de91218",
        "family": "Qwen3（qwen profile 的外部验证）",
    },
    "Qwen/Qwen2.5-3B": {
        "revision": "3aab1f1954e9cc14eb9509a215f9e5ca08227a9b",
        "family": "Qwen2.5（qwen profile 的拟合家族之一）",
    },
    "deepseek-ai/DeepSeek-V3": {
        "revision": "e815299b0bcbac849fa540c768ef21845365c9eb",
        "family": "DeepSeek-V3（deepseek profile 的拟合家族）",
    },
}

# 默认验证集 = 上表的键（**单一权威**：repo 列表与 revision 不可能分叉）
_DEFAULT = list(TOKENIZER_SOURCES)

# 该家族按 **tokenizer 内容**归属哪个 profile。
# ⚠️ 与生产 `profile_for_model` **不完全一致**：后者按**模型名子串**判定，所以
# `allenai/OLMo-7B-0724-hf` 会落到 `generic`（名字里没有家族标记），而它用的
# 其实是 Llama tokenizer ⇒ 这里标 `llama`。两者都是"标签"，不参与生产决策；
# 有分歧时以本表（按内容）为准，差异本身登记为 `KB-73/KB-75` 的同族问题。
_PROFILE_BY_NAME = (("qwen", "qwen"), ("deepseek", "deepseek"),
                    ("llama", "llama"), ("olmo", "llama"),
                    ("mistral", "mistral"))


def _profile_for(repo: str) -> str:
    name = repo.rpartition("/")[2].lower()
    for marker, profile in _PROFILE_BY_NAME:
        if marker in name:
            return profile
    return "generic"


def _resolve_revision(repo: str) -> str | None:
    """把 repo 解析成**不可变 commit SHA**；解析不出来就返回 None（调用方跳过）。

    两条路径都只产出 SHA：
      1. `TOKENIZER_SOURCES` 里签入的 pin（**首选**：跨运行、跨机器都指向同一份内容）；
      2. 未登记的 repo（命令行临时传入，或大小写变体）→ 向 Hub 问**当前** main 的
         commit SHA，再用该 SHA 下载。

    **诚实的边界**：路径 ② 只保证"**单次运行内**按不可变 SHA 取内容"（同一次运行里
    不会中途换版本），**不保证跨运行可复现**——两次运行可能落在不同的 commit 上。
    要可复现就把该 repo 登记进 `TOKENIZER_SOURCES`。这才是本表存在的意义。

    绝不在失败时退化成浮动引用（tag/branch/None）：解析不出来就返回 None，调用方跳过。
    """
    pinned = TOKENIZER_SOURCES.get(repo, {}).get("revision")
    if pinned:
        return pinned
    try:
        from huggingface_hub import HfApi

        sha = HfApi().model_info(repo).sha
    except Exception as exc:  # noqa: BLE001 — 维护者工具：失败必须看得见
        print(f"{repo:34} SKIP: 无法解析不可变 revision（{type(exc).__name__}: "
              f"{str(exc)[:50]}）—— 不回退到浮动引用")
        return None
    if not sha:
        print(f"{repo:34} SKIP: Hub 未返回 commit SHA —— 不回退到浮动引用")
        return None
    # 形状校验：只接受 40 位十六进制 commit SHA。不校验的话，一个被中间层改写过的
    # 响应（分支名 / 短 SHA / 空串）会原样进到 `hf_hub_download` —— 那正是本脚本要
    # 消除的"浮动引用"（B615 / CWE-494），所以宁可跳过也不要放过去。
    if len(sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        print(f"{repo:34} SKIP: Hub 返回的 revision 不是 40 位 commit SHA"
              f"（{sha[:16]!r}…）—— 不回退到浮动引用")
        return None
    return sha


def main(argv: list[str]) -> int:
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    from Module.system_prompt.budget import (  # noqa: SLF001
        _CLASSES,
        _TOKEN_WEIGHTS,
        _classify,
        UNCOVERED_CONTENT_CLASSES,
    )

    manifest = json.loads((_CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]

    def _bodies(categories: set[str] | None) -> dict[str, str]:
        """取样本：`categories=None` ⇒ **合同内**类别（排除未覆盖类）。"""
        out: dict[str, str] = {}
        for e in manifest:
            if e["kind"] != "text":
                continue
            if categories is None:
                if e["category"] in UNCOVERED_CONTENT_CLASSES:
                    continue
            elif e["category"] not in categories:
                continue
            body = (_CORPUS / e["path"]).read_text(encoding="utf-8")
            if len(body) >= 8:
                out[e["id"]] = body
        return out

    # **主判定**只用合同内类别（与 CI 合同同一份集合）；`latin_accent` 单独报告——
    # "对齐过滤条件"不得变成把已知缺口藏起来（KB-78）。
    bodies = _bodies(None)
    accent_bodies = _bodies({"latin_accent"})

    def _feats(items: dict[str, str]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for eid, body in items.items():
            counts = dict.fromkeys(_CLASSES, 0)
            for ch in body:
                counts[_classify(ch)] += 1
            out[eid] = counts
        return out

    feats = _feats(bodies)
    accent_feats = _feats(accent_bodies)

    print(f"合同内样本 {len(bodies)} 条；未覆盖类 {sorted(UNCOVERED_CONTENT_CLASSES)}"
          f"（其中 latin_accent {len(accent_bodies)} 条单独报告）")
    print(f"{'family':34} {'profile':9} {'min':>6} {'median':>7} {'max':>6}  worst-min"
          f"   [revision]")
    worst_overall: dict[str, float] = {}
    accent_worst: dict[str, float] = {}
    for repo in (argv or _DEFAULT):
        revision = _resolve_revision(repo)
        if revision is None:
            continue
        try:
            # revision= 必须是**不可变 commit SHA**：它既是可复现性的保证，
            # 也是 bandit B615（CWE-494）要求的那条控制。
            path = hf_hub_download(repo_id=repo, filename="tokenizer.json",
                                   revision=revision,
                                   cache_dir="/tmp/elysia_tokenizer_validation")
            tok = Tokenizer.from_file(path)
        except Exception as exc:  # noqa: BLE001 — 维护者工具，失败要看得见原因
            print(f"{repo:34} SKIP: {type(exc).__name__} {str(exc)[:60]}")
            continue

        def _ratios(items: dict[str, str], item_feats: dict[str, dict[str, int]],
                    weights: dict[str, float]) -> list[tuple[float, str]]:
            out: list[tuple[float, str]] = []
            for eid, body in items.items():
                actual = len(tok.encode(body, add_special_tokens=False).ids)
                if not actual:
                    continue
                est = sum(item_feats[eid][k] * weights[k] for k in _CLASSES)
                out.append((est / actual, eid))
            return out

        profile = _profile_for(repo)
        weights = _TOKEN_WEIGHTS[profile]
        ratios = _ratios(bodies, feats, weights)
        if not ratios:
            print(f"{repo:34} SKIP: 主判定集上无可测样本")
            continue
        lo = min(ratios)
        hi = max(ratios)
        med = statistics.median(r for r, _ in ratios)
        verdict = "OK" if lo[0] >= 0.75 else "NOT COVERED"
        print(f"{repo:34} {profile:9} {lo[0]:6.2f} {med:7.2f} {hi[0]:6.2f}  "
              f"{lo[1]} ({verdict})   [{revision[:12]}]")
        worst_overall[repo] = lo[0]

        accent = _ratios(accent_bodies, accent_feats, weights)
        if accent:
            alo = min(accent)
            accent_worst[repo] = alo[0]
            flag = "⚠️ 击穿硬下限（已知缺口 KB-78）" if alo[0] < 0.75 else "在合同内"
            print(f"{'  └ latin_accent':34} {profile:9} {alo[0]:6.2f} "
                  f"{statistics.median(r for r, _ in accent):7.2f} "
                  f"{max(r for r, _ in accent):6.2f}  {alo[1]} ({flag})")

    if not worst_overall:
        # **拒答**：一个家族都没校验成功（无网络 / 全部 SKIP / revision 解析失败）时
        # 绝不 exit 0——那会让"什么都没检查"看起来像"检查通过"（Batch 19 / KB-79）。
        print("\n❌ 拒答：没有任何家族产出结论（全部 SKIP / 无法解析不可变 revision）"
              "——这不代表通过；修好环境后重跑。")
        return 2

    print(f"\n已校验 {len(worst_overall)} 个家族（合同内类别）。")
    uncovered = {r: v for r, v in worst_overall.items() if v < 0.75}
    if uncovered:
        print("\n⚠️  以下家族**未覆盖**（min < 0.75 硬下限）——要么把它们纳入拟合"
              "（会把 generic 的 T 顶高，对所有未知使用者生效），要么如实登记：")
        for repo, v in sorted(uncovered.items(), key=lambda kv: kv[1]):
            print(f"   {repo}: {v:.2f}")
    if accent_worst:
        repo_w, val_w = min(accent_worst.items(), key=lambda kv: kv[1])
        print(f"\nℹ️  已知缺口（`latin_accent`，KB-78）：最坏 {val_w:.2f}（{repo_w}）"
              "—— 该类**不在**合同内，修法是重标定 `latin_ext`（估算器标定批次）。")
    return 1 if uncovered else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
