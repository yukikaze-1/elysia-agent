#!/usr/bin/env python3
"""下载 sentence-transformers 嵌入模型到 .model_cache/。

用法:
    python scripts/download_model.py                        # 官方 HuggingFace（默认，固定 revision）
    python scripts/download_model.py --model BAAI/bge-m3    # 指定模型
    python scripts/download_model.py --mirror               # 显式 opt-in 镜像（默认 hf-mirror.com）
    HF_MIRROR=https://hf-mirror.com python scripts/download_model.py  # 镜像经环境变量显式 opt-in

供应链安全约束（P1-18）：
- 默认走官方 HuggingFace endpoint；默认 BGE 模型固定不可变 commit revision。
- 第三方 mirror 只能显式 opt-in（``--mirror`` CLI 或 ``HF_MIRROR`` 环境变量），
  且镜像内容仍按同一 manifest 校验 sha256，内容不同会被拒绝。
- 下载先落 ``.model_cache/.tmp`` 临时目录，全部文件 sha256 校验通过后原子切换
  到最终快照；失败保留旧可用版本。
- 默认 ``trust_remote_code=False``；未登记 manifest 的模型打印未固定警告，
  不执行远程代码。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# 使脚本可独立运行（python scripts/download_model.py）：把项目根加入 sys.path
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Module.embedding.model_manifest import (  # noqa: E402
    DEFAULT_MODEL_ID,
    HF_MIRROR_ENV,
    HF_OFFICIAL_ENDPOINT,
    get_manifest,
    is_mirror_enabled,
    resolve_endpoint,
    verify_manifest_dir,
)

from sentence_transformers import SentenceTransformer  # noqa: E402

# 默认第三方镜像（仅 --mirror / HF_MIRROR 显式启用时使用）
_DEFAULT_MIRROR_URL = "https://hf-mirror.com"


def find_project_root() -> Path:
    """向上查找项目根目录（包含 requirements.txt 的目录）。"""
    here = Path(__file__).resolve().parent
    for d in [here] + list(here.parents):
        if (d / "requirements.txt").exists():
            return d
    return here.parent  # fallback


def _iter_existing_snapshots(model_name: str, cache_dir: Path):
    """按优先级遍历缓存中已有的模型快照目录。

    兼容两种布局：
    HF Hub 格式: models--ORG--MODEL/snapshots/{revision}
    直接格式:   ORG/MODEL
    """
    hub_name = "models--" + model_name.replace("/", "--")
    snapshots_dir = cache_dir / hub_name / "snapshots"
    if snapshots_dir.is_dir():
        for snap in sorted(snapshots_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if snap.is_dir():
                yield snap
    direct_path = cache_dir / model_name
    if direct_path.is_dir():
        yield direct_path


def cached_model_valid(model_name: str, cache_dir: Path) -> bool:
    """判断缓存中的模型是否可用（manifest 模型需全部文件 hash 匹配）。

    不以目录/单文件存在作为成功：已登记 manifest 的模型必须全部文件存在且
    sha256 匹配才视为已缓存；被篡改或缺失时返回 False，触发重新下载。
    """
    manifest = get_manifest(model_name)
    candidates = list(_iter_existing_snapshots(model_name, cache_dir))
    if not candidates:
        return False
    if manifest is None:
        # 未登记模型：无法做 hash 校验，目录存在即视为已缓存
        return True
    for snap in candidates:
        ok, _failures = verify_manifest_dir(snap, manifest)
        if ok:
            return True
    return False


def _atomic_switch_model_dir(src: Path, dst: Path) -> None:
    """把临时目录 src 原子切换到最终目录 dst。

    同文件系统内：先把旧目录 rename 到备份名，再把新目录 rename 到目标；
    任一步失败回滚旧目录，保证失败时旧可用版本仍在。
    """
    backup = dst.with_name(dst.name + ".old")
    # 清理上次中断残留的备份
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if dst.exists():
        os.replace(dst, backup)
    try:
        os.replace(src, dst)
    except Exception:
        # 切换失败：恢复旧目录
        if backup.exists():
            os.replace(backup, dst)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _write_refs_main(model_dir: Path, revision: str) -> None:
    """写 refs/main 指向固定 revision，便于 hub 默认分支解析。"""
    refs_dir = model_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / "main").write_text(revision, encoding="utf-8")


def download_model(model_name: str, cache_dir: Path) -> bool:
    """下载并校验模型到 cache_dir。返回 True 表示成功。

    已登记 manifest 的模型固定 revision 下载、临时目录 sha256 全量校验通过后
    原子切换到最终快照；未登记模型下载时打印未固定警告（trust_remote_code=False）。
    """
    manifest = get_manifest(model_name)
    endpoint = resolve_endpoint()
    if is_mirror_enabled():
        source_desc = f"{endpoint}（镜像，显式 opt-in）"
    elif endpoint != HF_OFFICIAL_ENDPOINT:
        source_desc = f"{endpoint}（HF_ENDPOINT 显式配置）"
    else:
        source_desc = f"{endpoint}（官方）"
    print(f"正在下载模型 {model_name} 到 {cache_dir} ...")
    print(f"来源: {source_desc}")
    if manifest is not None:
        print(f"固定 revision: {manifest.revision}")
    else:
        print("⚠ 该模型未登记固定 manifest，将不做 hash 校验（trust_remote_code=False）")

    # 让 huggingface_hub 使用本次解析出的 endpoint
    os.environ["HF_ENDPOINT"] = endpoint

    hub_name = "models--" + model_name.replace("/", "--")
    tmp_root = cache_dir / ".tmp"
    tmp_hub = tmp_root / hub_name
    target_hub = cache_dir / hub_name

    try:
        SentenceTransformer(
            model_name,
            cache_folder=str(tmp_root),
            revision=manifest.revision if manifest is not None else None,
            trust_remote_code=False,
        )

        # 定位临时快照并全量校验 manifest
        if manifest is not None:
            snapshots_dir = tmp_hub / "snapshots"
            verified_snapshot: Path | None = None
            if snapshots_dir.is_dir():
                for snap in snapshots_dir.iterdir():
                    if not snap.is_dir():
                        continue
                    ok, failures = verify_manifest_dir(snap, manifest)
                    if ok:
                        verified_snapshot = snap
                        break
                    print(f"✗ 快照 {snap.name} 校验失败:")
                    for fail in failures:
                        print(f"    {fail}")
            if verified_snapshot is None:
                print("✗ 下载内容校验失败（缺文件或 hash 不匹配），保留旧缓存不动")
                return False
            print("✓ 下载内容 sha256 校验通过")

        # 全部校验通过后原子切换到最终位置
        _atomic_switch_model_dir(tmp_hub, target_hub)
        if manifest is not None:
            _write_refs_main(target_hub, manifest.revision)
        print(f"✓ 模型已下载: {target_hub}")
        return True
    except Exception as exc:  # noqa: BLE001 — CLI 脚本统一兜底
        print(f"✗ 下载失败: {exc}")
        return False
    finally:
        # 清理临时目录（切换成功后 tmp_hub 已不存在）
        if tmp_hub.exists():
            shutil.rmtree(tmp_hub, ignore_errors=True)
        try:
            if tmp_root.exists() and not any(tmp_root.iterdir()):
                tmp_root.rmdir()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="下载 sentence-transformers 嵌入模型到 .model_cache/（固定 revision + hash 校验）"
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL_ID,
        help=f"模型名称 (默认: {DEFAULT_MODEL_ID})",
    )
    parser.add_argument(
        "--mirror", nargs="?", const=_DEFAULT_MIRROR_URL, default=None,
        help="使用第三方镜像（显式 opt-in，默认官方 HuggingFace）。"
             f"不带值时使用 {_DEFAULT_MIRROR_URL}；也可用 HF_MIRROR 环境变量。"
             "镜像内容仍按相同 manifest 校验 hash。",
    )
    args = parser.parse_args()

    if args.mirror:
        os.environ[HF_MIRROR_ENV] = args.mirror

    root = find_project_root()
    cache_dir = root / ".model_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if cached_model_valid(args.model, cache_dir):
        print(f"✓ 模型已缓存且校验通过: {cache_dir}")
        return

    ok = download_model(args.model, cache_dir)
    if not ok:
        # review 复核 #11：下载/校验失败必须以非零退出码结束——setup.sh 据此
        # 判定失败；此前返回 0 让「下载失败」被当成功（目录半成品残留）。
        print("✗ 模型下载/校验失败，请重试或检查网络/镜像配置", file=sys.stderr)
        sys.exit(1)
    print()
    print(f"提示: 确保 MODEL_CACHE_DIR 指向 {cache_dir}")
    print("  或在项目根创建软链接: ln -s /path/to/model_cache .model_cache")


if __name__ == "__main__":
    main()
