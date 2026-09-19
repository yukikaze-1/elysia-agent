"""验证 `generic` profile 在**指定 tokenizer 家族**上的 est/actual 区间。

    用法：conda run -n Elysia python scripts/token_corpus/validate_families.py [repo_id ...]

## 为什么需要这个入口

`Module/system_prompt/budget.py` 的合同是**按 profile 声明**的，而每个 profile 的
声明值只在**一组已验证的家族**上成立：

    cjk_opt  ←  Qwen2.5（HF + 真实 Ollama 双证）、DeepSeek-V3
    generic  ←  Llama-3-8B、Mistral-7B-v0.3、o200k_base、cl100k_base

`generic` 是**未知模型的回退**，而"未知"意味着它迟早会遇到没验证过的家族。本脚本就是
那条"把新家族纳入/排除验证范围"的可执行入口：给定 HF repo，直接报出该家族在每个
profile 下的 `min/median/max` 比值与越界样本。

**它不进 CI**（要下载 tokenizer 文件，§61/§124 禁止把词表签入仓库）。它是维护者工具：
新增或升级 provider/model 家族时跑一次，把结果写进执行记录，并据此决定是"纳入拟合"
还是"如实登记为未覆盖"。

## 已知结论（Batch 18 实测，2026-09-19）

| 家族 | profile | min est/actual | 判定 |
|---|---|---|---|
| Llama-3 / Mistral / o200k / cl100k | generic | 0.80 | ✅ 已验证（拟合集） |
| OLMo-7B | generic | 0.80 | ✅ 区间内 |
| Qwen3-8B | cjk_opt | 0.80 | ✅ 区间内 |
| Yi-1.5-6B | generic | **0.66** | ⚠️ **未覆盖**（emoji 类） |
| Phi-3-mini-4k | generic | **0.63** | ⚠️ **未覆盖**（emoji / 繁体中文） |

**未覆盖的含义**：在这些家族上，估算仍可能低估到 0.63–0.66（低于 0.75 硬下限），
即"预算判装得下而实际超窗"仍可能发生。修它的代价已实测：把这两个家族纳入拟合会把
`generic` 的最坏高估从 **1.71 顶到 2.19**（对所有 custom/未知使用者生效）——Batch 18
判定"不划算"，改为**如实登记**（`todo.md` KB-74）而不是悄悄放宽或悄悄忽略。
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

_DEFAULT = ["01-ai/Yi-1.5-6B", "microsoft/Phi-3-mini-4k-instruct",
            "allenai/OLMo-7B-0724-hf", "Qwen/Qwen3-8B", "Qwen/Qwen2.5-3B",
            "deepseek-ai/DeepSeek-V3"]

# 该家族在**生产**里会落到哪个 profile（按 `profile_for_model` 的判定）
_PROFILE_BY_NAME = (("qwen", "cjk_opt"), ("deepseek", "cjk_opt"))


def _profile_for(repo: str) -> str:
    name = repo.rpartition("/")[2].lower()
    for marker, profile in _PROFILE_BY_NAME:
        if marker in name:
            return profile
    return "generic"


def main(argv: list[str]) -> int:
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    from Module.system_prompt.budget import _TOKEN_WEIGHTS, _CLASSES  # noqa: SLF001

    manifest = json.loads((_CORPUS / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]
    bodies = {e["id"]: (_CORPUS / e["path"]).read_text(encoding="utf-8")
              for e in manifest if e["kind"] == "text"
              and e["category"] != "repeat" and len(
                  (_CORPUS / e["path"]).read_text(encoding="utf-8")) >= 8}
    feats = {}
    for eid, body in bodies.items():
        from Module.system_prompt.budget import _classify  # noqa: SLF001
        counts = dict.fromkeys(_CLASSES, 0)
        for ch in body:
            counts[_classify(ch)] += 1
        feats[eid] = counts

    print(f"{'family':34} {'profile':9} {'min':>6} {'median':>7} {'max':>6}  worst-min")
    worst_overall = {}
    for repo in (argv or _DEFAULT):
        try:
            path = hf_hub_download(repo_id=repo, filename="tokenizer.json",
                                   cache_dir="/tmp/elysia_tokenizer_validation")
            tok = Tokenizer.from_file(path)
        except Exception as exc:  # noqa: BLE001 — 维护者工具，失败要看得见原因
            print(f"{repo:34} SKIP: {type(exc).__name__} {str(exc)[:60]}")
            continue
        profile = _profile_for(repo)
        weights = _TOKEN_WEIGHTS[profile]
        ratios = []
        for eid, body in bodies.items():
            actual = len(tok.encode(body, add_special_tokens=False).ids)
            if not actual:
                continue
            est = sum(feats[eid][k] * weights[k] for k in _CLASSES)
            ratios.append((est / actual, eid))
        lo = min(ratios)
        hi = max(ratios)
        med = statistics.median(r for r, _ in ratios)
        verdict = "OK" if lo[0] >= 0.75 else "NOT COVERED"
        print(f"{repo:34} {profile:9} {lo[0]:6.2f} {med:7.2f} {hi[0]:6.2f}  "
              f"{lo[1]} ({verdict})")
        worst_overall[repo] = lo[0]
    uncovered = {r: v for r, v in worst_overall.items() if v < 0.75}
    if uncovered:
        print("\n⚠️  以下家族**未覆盖**（min < 0.75 硬下限）——要么把它们纳入拟合"
              "（会把 generic 的 T 顶高，对所有未知使用者生效），要么如实登记：")
        for repo, v in sorted(uncovered.items(), key=lambda kv: kv[1]):
            print(f"   {repo}: {v:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
