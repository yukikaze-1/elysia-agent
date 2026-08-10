"""Embedding 模型供应链单源 manifest（P1-18）。

固定默认 BGE 模型的不可变 commit revision 与所需文件的 sha256 hash，
供下载脚本（scripts/download_model.py）与加载后端（huggingface.py）共同消费。

供应链约束：
- 官方 HuggingFace endpoint 为默认；第三方 mirror 只能显式 opt-in
  （HF_MIRROR 环境变量 / --mirror CLI）。
- 默认 ``trust_remote_code=False``；仅 allowlist 内同时具备固定 revision/hash
  的特殊模型可显式开启（当前无启用项）。
- 缓存校验以完整 manifest 为准，不以目录/单文件存在作为成功。

本模块零依赖（仅标准库），不 import core/ Module/ 其他模块，可被独立脚本导入。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# 官方 HuggingFace endpoint（默认）
HF_OFFICIAL_ENDPOINT = "https://huggingface.co"

# 第三方 mirror 显式 opt-in 环境变量
HF_MIRROR_ENV = "HF_MIRROR"

# 默认嵌入模型
DEFAULT_MODEL_ID = "BAAI/bge-large-zh-v1.5"

# 默认模型固定的不可变 commit revision。
# 验证：与 HuggingFace Hub API 该仓库当前 sha 一致（2026-08-10）；
# 下载/加载一律使用该 commit，不使用可变 tag/branch。
DEFAULT_MODEL_REVISION = "79e7739b6ab944e86d6171e44d24c997fc1e0116"


@dataclass(frozen=True)
class ManifestFile:
    """manifest 中的一个文件条目。"""

    path: str  # 相对模型快照根的路径（POSIX 分隔）
    sha256: str  # 该文件 sha256（hex，64 字符）


@dataclass(frozen=True)
class ModelManifest:
    """一个模型的供应链 manifest：固定 revision + 所需文件的 sha256。"""

    model_id: str
    revision: str  # 不可变 commit hash
    files: tuple[ManifestFile, ...]

    def file_hash_map(self) -> dict[str, str]:
        """返回 path → sha256 映射。"""
        return {f.path: f.sha256 for f in self.files}


# 默认 BGE 模型 manifest（sha256 于 2026-08-10 从固定 revision 快照计算并
# 与官方 HuggingFace endpoint 抽查比对一致；不含 .gitattributes/README.md
# 等非加载必需文件）。
_DEFAULT_MANIFEST = ModelManifest(
    model_id=DEFAULT_MODEL_ID,
    revision=DEFAULT_MODEL_REVISION,
    files=(
        ManifestFile("1_Pooling/config.json",
                     "e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352"),
        ManifestFile("config.json",
                     "2d4f53504b43a856f982dad647835c0ae98f91a73c8ff0db95ab89e3e4806fa9"),
        ManifestFile("config_sentence_transformers.json",
                     "940d5f50db195fa6e5e6a4f122c095f77880de259d74b14a65779ed48bdd7c56"),
        ManifestFile("modules.json",
                     "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf"),
        ManifestFile("pytorch_model.bin",
                     "bf84a56fb045c24e090495195584a3922c3e4204107f0bba8b79c11f67a207f2"),
        ManifestFile("sentence_bert_config.json",
                     "84e39fda68ccbff05bfa723ae9c0e70e23e2ec373b76e0f8c6e71af72a693cbf"),
        ManifestFile("special_tokens_map.json",
                     "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
        ManifestFile("tokenizer.json",
                     "7dfbf1966ebf99d471c3796e9b457329d2b2182b817e144f1e904b957745c839"),
        ManifestFile("tokenizer_config.json",
                     "e1790949631401af1bfb6c9c7aeec7fcf612e274d73579d99f704faea40c8ba7"),
        ManifestFile("vocab.txt",
                     "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"),
    ),
)

# model_id → manifest 注册表（单源，可被测试 monkeypatch 扩展）
_MANIFESTS: dict[str, ModelManifest] = {
    _DEFAULT_MANIFEST.model_id: _DEFAULT_MANIFEST,
}

# remote-code allowlist：只有同时满足「已登记固定 manifest（revision + hash）」
# 且在以下集合内的模型才允许 trust_remote_code=True。当前无启用项。
_REMOTE_CODE_ALLOWLIST: frozenset[str] = frozenset()


def get_manifest(model_id: str) -> ModelManifest | None:
    """返回 model_id 对应的 manifest；未登记返回 None。"""
    return _MANIFESTS.get(model_id)


def is_known_model(model_id: str) -> bool:
    """是否已登记固定 manifest 的模型。"""
    return model_id in _MANIFESTS


def trust_remote_code_for(model_id: str) -> bool:
    """返回模型是否允许 ``trust_remote_code=True``。

    仅当模型同时在 manifest（固定 revision + hash）与 remote-code allowlist
    中才返回 True；否则一律 False（默认关闭，防供应链代码执行）。
    """
    if model_id not in _REMOTE_CODE_ALLOWLIST:
        return False
    return get_manifest(model_id) is not None


def is_mirror_enabled() -> bool:
    """是否显式启用了第三方 mirror（HF_MIRROR 环境变量）。"""
    return bool(os.environ.get(HF_MIRROR_ENV, "").strip())


def resolve_endpoint() -> str:
    """解析模型下载/加载 endpoint。

    优先级：``HF_MIRROR``（显式 opt-in mirror）> ``HF_ENDPOINT``（用户显式配置）
    > 官方 HuggingFace。未做任何显式配置时默认官方。
    """
    mirror = os.environ.get(HF_MIRROR_ENV, "").strip().rstrip("/")
    if mirror:
        return mirror
    user_endpoint = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    if user_endpoint:
        return user_endpoint
    return HF_OFFICIAL_ENDPOINT


def sha256_file(path: Path) -> str:
    """计算单个文件的 sha256（hex）。

    分块读取，避免大权重文件（~1.3GB）占用过多内存。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest_dir(root: Path, manifest: ModelManifest) -> tuple[bool, list[str]]:
    """校验目录内 manifest 全部文件存在且 sha256 匹配。

    Args:
        root: 模型快照根目录。
        manifest: 目标模型 manifest。

    Returns:
        (是否全部通过, 失败明细列表)。失败明细逐条列出缺失/不可读/不匹配的文件。
        只有全部文件通过才返回 True——不以目录或单文件存在作为成功。
    """
    failures: list[str] = []
    for mf in manifest.files:
        file_path = root / mf.path
        if not file_path.is_file():
            failures.append(f"missing: {mf.path}")
            continue
        try:
            actual = sha256_file(file_path)
        except OSError as exc:  # 不可读视为失败（fail-closed）
            failures.append(f"unreadable: {mf.path} ({exc})")
            continue
        if actual != mf.sha256:
            failures.append(
                f"hash mismatch: {mf.path} "
                f"(expected {mf.sha256[:16]}..., got {actual[:16]}...)"
            )
    return not failures, failures
