"""生成 KB-78 的**真散文 holdout**（`latin_prose_holdout.json` + `text/holdout_latin_*.txt`）。

    用法：conda run -n Elysia python scripts/token_corpus/build_latin_prose_holdout.py

与 `build_golden.py` 同一性质：**不是 CI 的一部分**（需要网络 + 权威 tokenizer 缓存），
存在的意义是让"真值从哪来"**可审计、可重放**；签入的产物才是
`Module/system_prompt/tests/test_latin_prose_contract.py` 的输入。

## 为什么需要它

`latin_ext` 系数原来由签入语料里的 `latin1_accented` 标定，而那条样本的变音密度是
**5.0%**——真捷克散文是 **12–14%**、真波兰语 2–8%。于是**只跑合成语料测不出缺口**：
合成样本比真散文更"重音密集"（czech 19.9% / polish 10.5%），恰好压在能过的那一侧。
KB-78 的缺口（est/actual 0.64–0.75 < 0.75 硬下限）**只有真散文能暴露**。

## 内容纪律（§37：不搬运大段版权文本）

- 来源是 **Wikipedia REST 导言**（`/api/rest_v1/page/summary/{title}` 的 `extract`），
  许可 **CC BY-SA 4.0**（允许在署名下再分发）；
- 每篇只取**导言**（本批 12 篇，单篇 ≤ 730 字符，合计 ≈5.7k 字符）；
- fixture 里逐条记录 `lang` / `chars` / `sha256` / `titles`（署名 + 可核对）；
- **用途**：标定/验证 token 估算，不进入产品数据。

## 真值口径

与 `build_golden.py` **完全同源同法**：4 个 HF `tokenizer.json`
（Qwen2.5-3B / DeepSeek-V3 / Meta-Llama-3-8B / Mistral-7B-v0.3，取
`encode(add_special_tokens=False).ids`）+ 3 个 tiktoken 编码
（cl100k_base / o200k_base / p50k_base，取 `encode()`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = (Path(__file__).resolve().parents[2]
         / "Module/system_prompt/tests/fixtures/token_corpus")

#: 取样标题（lang, 条目标题）。刻意跨题材，让变音密度落在自然范围而不是挑极值：
#: 捷克 9.7–14.2%（含低密度的 Karel Čapek 9.9%）、波兰 **2.1**–8.3%
#: （`Adam Mickiewicz` 是低密度样本——Batch 19 明确要求它在场）。
_TITLES: dict[str, str] = {
    "cs_Praha": "Praha",
    "cs_Brno": "Brno",
    "cs_Vltava": "Vltava",
    "cs_Jan_Amos_Komensky": "Jan Ámos Komenský",
    "cs_Narodni_divadlo": "Národní divadlo",
    "cs_Karel_Capek": "Karel Čapek",
    "pl_Krakow": "Kraków",
    "pl_Wisla": "Wisła",
    "pl_Mickiewicz": "Adam Mickiewicz",
    "pl_Torun": "Toruń",
    "pl_Gdansk": "Gdańsk",
    "pl_Lodz": "Łódź",
}

_HF_TOKENIZERS: dict[str, str] = {
    "qwen2.5": "Qwen/Qwen2.5-3B",
    "deepseek_v3": "deepseek-ai/DeepSeek-V3",
    "llama3": "NousResearch/Meta-Llama-3-8B",
    "mistral": "mistralai/Mistral-7B-v0.3",
}
_TIKTOKEN_ENCODINGS = ("cl100k_base", "o200k_base", "p50k_base")
#: 单篇样本的字符上限（保持"小样本"性质）
_MAX_CHARS = 1000


def _fetch_extract(lang: str, title: str) -> str:
    url = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
           f"{urllib.parse.quote(title)}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "elysia-agent-token-calibration/1.0"})
    # 固定 https 主机（urlopen 的 S310 在此不适用：URL 由本文件的常量表拼出）
    with urllib.request.urlopen(req, timeout=40) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8")).get("extract", "")


def main() -> int:
    os.environ.pop("ALL_PROXY", None)
    os.environ.pop("all_proxy", None)

    import tiktoken
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    hf = {name: Tokenizer.from_file(hf_hub_download(repo_id=repo, filename="tokenizer.json"))
          for name, repo in _HF_TOKENIZERS.items()}
    encs = {name: tiktoken.get_encoding(name) for name in _TIKTOKEN_ENCODINGS}

    samples: list[dict] = []
    for sid, title in _TITLES.items():
        lang = sid.split("_")[0]
        body = _fetch_extract(lang, title)
        if not body:
            print(f"SKIP {sid}: 抓取为空", file=sys.stderr)
            continue
        if len(body) > _MAX_CHARS:
            print(f"SKIP {sid}: {len(body)} 字符超出小样本上限", file=sys.stderr)
            continue
        out_id = f"holdout_latin_{sid}"
        (_HERE / f"text/{out_id}.txt").write_text(body, encoding="utf-8")
        counts = {n: len(tk.encode(body, add_special_tokens=False).ids)
                  for n, tk in hf.items()}
        counts.update({n: len(ex.encode(body)) for n, ex in encs.items()})
        samples.append({
            "id": out_id,
            "lang": lang,
            "path": f"text/{out_id}.txt",
            "chars": len(body),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "tokens": counts,
        })
        print(f"{out_id:<40} {len(body):>4} chars  {counts}")

    if not samples:
        # 一条都没抓到 ⇒ **拒答**（不写出一份看起来正常的空 fixture）
        print("FATAL: 没有取到任何样本——拒绝写出空 fixture", file=sys.stderr)
        return 2

    doc = {
        "note": "KB-78 真散文 holdout：**只用于验证，不参与系数拟合**。真值 = 权威 tokenizer。",
        "generated_by": "scripts/token_corpus/build_latin_prose_holdout.py",
        "regenerate": "conda run -n Elysia python scripts/token_corpus/build_latin_prose_holdout.py",
        "source": {
            "kind": "Wikipedia REST summary extract（导言散文）",
            "license": "CC BY-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "attribution": "Wikipedia contributors",
            "endpoint": "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}",
            "why": "CC BY-SA 4.0 允许在署名下再分发；每篇只取导言（≤ 1000 字符）作标定样本。",
            "titles": _TITLES,
        },
        "tokenizers": {
            **{n: {"source": "huggingface", "repo": r, "file": "tokenizer.json",
                   "usage": "encode(add_special_tokens=False)"}
               for n, r in _HF_TOKENIZERS.items()},
            **{n: {"source": "tiktoken", "usage": "encode()"} for n in _TIKTOKEN_ENCODINGS},
        },
        "samples": samples,
    }
    out = _HERE / "latin_prose_holdout.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(samples)} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
