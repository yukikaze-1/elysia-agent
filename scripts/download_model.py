#!/usr/bin/env python3
"""下载 sentence-transformers 嵌入模型到 .model_cache/。

用法:
    python scripts/download_model.py                        # 下载默认模型
    python scripts/download_model.py --model BAAI/bge-m3   # 指定模型

已存在则跳过。需要先 pip install -r requirements.txt。
"""

import argparse
from pathlib import Path


def find_project_root() -> Path:
    """向上查找项目根目录（包含 requirements.txt 的目录）。"""
    here = Path(__file__).resolve().parent
    for d in [here] + list(here.parents):
        if (d / "requirements.txt").exists():
            return d
    return here.parent  # fallback


def model_exists(model_name: str, cache_dir: Path) -> bool:
    """检查模型是否已在 HuggingFace Hub 缓存中。

    SentenceTransformer 使用 HuggingFace Hub 缓存结构:
    {cache_dir}/models--ORG--MODEL/snapshots/{hash}/config.json
    """
    hub_name = "models--" + model_name.replace("/", "--")
    snapshots_dir = cache_dir / hub_name / "snapshots"
    if snapshots_dir.exists() and any(snapshots_dir.iterdir()):
        return True
    return False


def download_model(model_name: str, cache_dir: Path) -> bool:
    """下载 sentence-transformers 模型。返回 True 表示成功。"""
    print(f"正在下载模型 {model_name} 到 {cache_dir} ...")
    print("（约 1.3GB，可能需要几分钟）")
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(model_name, cache_folder=str(cache_dir))
        print(f"✓ 模型已下载: {cache_dir}")
        return True
    except Exception as e:
        print(f"✗ 下载失败: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="下载 sentence-transformers 嵌入模型到 .model_cache/"
    )
    parser.add_argument(
        "--model", type=str, default="BAAI/bge-large-zh-v1.5",
        help="模型名称 (默认: BAAI/bge-large-zh-v1.5)",
    )
    args = parser.parse_args()

    root = find_project_root()
    cache_dir = root / ".model_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if model_exists(args.model, cache_dir):
        print(f"✓ 模型已缓存: {cache_dir}")
        return

    ok = download_model(args.model, cache_dir)
    if ok:
        print()
        print(f"提示: 确保 MODEL_CACHE_DIR 指向 {cache_dir}")
        print("  或在项目根创建软链接: ln -s /path/to/model_cache .model_cache")


if __name__ == "__main__":
    main()
