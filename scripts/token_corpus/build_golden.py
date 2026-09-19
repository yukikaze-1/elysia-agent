"""生成 `GOLDEN.json`：语料 × 权威 tokenizer 的 token 真值（Batch 18）。

    用法：conda run -n Elysia python scripts/token_corpus/build_golden.py

**这不是 CI 的一部分**：它需要 (a) 一次性拉取权威 tokenizer 文件（HF）与
(b) 本机真实 Ollama。两者都不可作为 required CI 依赖（§59/§61/§111/§158）。
它存在的意义是让"真值从哪来"可审计、可重放——签入的 `GOLDEN.json` 才是
`test_token_corpus_contract.py` 的输入。

前置：
  1. `/tmp/te/tokenizers.json`（HF tokenizer.json 本地路径）——由基准脚本拉取；
  2. `/tmp/te/results2.json`（真实 Ollama 的 `prompt_eval_count`，含 8192/16384
     两次测量与 truncation 判定）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

_HERE = (Path(__file__).resolve().parents[2]
         / "Module/system_prompt/tests/fixtures/token_corpus")
_TIKTOKEN_ENCODINGS = ("cl100k_base", "o200k_base", "p50k_base")
_HF_TOKENIZERS = {
    "qwen2.5": "Qwen/Qwen2.5-3B",
    "deepseek_v3": "deepseek-ai/DeepSeek-V3",
    "llama3": "NousResearch/Meta-Llama-3-8B",
    "mistral": "mistralai/Mistral-7B-v0.3",
}


def main() -> None:
    import tiktoken
    from tokenizers import Tokenizer

    hf = {n: Tokenizer.from_file(p)
          for n, p in json.load(open("/tmp/te/tokenizers.json")).items()}
    encs = {n: tiktoken.get_encoding(n) for n in _TIKTOKEN_ENCODINGS}
    ollama = json.loads(Path("/tmp/te/results2.json").read_text())

    text: dict[str, dict[str, int]] = {}
    chat: dict[str, dict] = {}
    for entry in json.loads((_HERE / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]:
        if entry["kind"] == "text":
            body = (_HERE / entry["path"]).read_text(encoding="utf-8")
            counts = {n: len(tk.encode(body, add_special_tokens=False).ids)
                      for n, tk in hf.items()}
            counts.update({n: len(ex.encode(body)) for n, ex in encs.items()})
            text[entry["id"]] = counts
        else:
            rec = next(c for c in ollama["cases"] if c["id"] == entry["id"])
            chat[entry["id"]] = {
                "prompt_eval_count": rec["count_ctx8192"],
                "count_at_16384": rec.get("count_ctx16384"),
                "contaminated": rec.get("contaminated", None),
                "category": entry["category"],
            }

    golden = {
        "provenance": {
            "note": "Batch 18 / KB-12 估算合同的真值来源；只由 _build_golden.py 生成",
            "text_tokenizers": {
                **{n: {"source": "huggingface", "repo": r,
                       "file": "tokenizer.json", "usage": "encode(add_special_tokens=False)"}
                   for n, r in _HF_TOKENIZERS.items()},
                **{n: {"source": "tiktoken", "usage": "encode()"}
                   for n in _TIKTOKEN_ENCODINGS},
            },
            "ollama": {
                "model": ollama["model"],
                "endpoint": "native /api/chat",
                "metric": "prompt_eval_count",
                "num_ctx_primary": 8192,
                "num_ctx_contamination_check": 16384,
                "framing_baseline_single_user_message": 29,
                "note": "count 在 8192 与 16384 下相同才视为未截断；contaminated 的"
                        "样本不得当真值",
            },
            "framing": ollama["framing"],
        },
        "text": text,
        "chat_ollama": chat,
    }
    out = _HERE / "GOLDEN.json"
    out.write_text(json.dumps(golden, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}  ({len(text)} text cases, {len(chat)} chat cases, "
          f"{len(text[list(text)[0]])} families)")


if __name__ == "__main__":
    sys.exit(main())
