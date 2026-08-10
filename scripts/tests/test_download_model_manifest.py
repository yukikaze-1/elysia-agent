"""download_model.py 供应链修复测试（P1-18，marker=unit，不真正下载）。

覆盖：
- manifest 结构：默认 BGE 模型固定不可变 commit revision + 全文件 sha256。
- 校验语义：篡改文件 / 缺文件 / 镜像内容不同 → verify_manifest_dir 拒绝。
- endpoint 策略：默认官方；第三方 mirror 只能显式 opt-in（HF_MIRROR）。
- remote-code 策略：默认 False；仅 manifest + allowlist 模型可显式开启。
- 下载流程：固定 revision、hash 校验通过后原子切换、失败保留旧缓存。
- 加载侧：HuggingFaceBackend 构造时 trust_remote_code 默认 False；
  manifest 模型本地缺失/校验失败拒绝加载。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from Module.embedding.model_manifest import (
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    HF_MIRROR_ENV,
    HF_OFFICIAL_ENDPOINT,
    ManifestFile,
    ModelManifest,
    get_manifest,
    is_known_model,
    is_mirror_enabled,
    resolve_endpoint,
    trust_remote_code_for,
    verify_manifest_dir,
)

pytestmark = pytest.mark.unit


# ============================================================
# 工具 —— 合成 manifest / 快照构造（文件内容已知，便于验证 hash 语义）
# ============================================================

_CONTENTS: dict[str, bytes] = {
    "config.json": b"config",
    "model.bin": b"weights",
}


def _sha256_bytes(data: bytes) -> str:
    """计算 bytes 的 sha256（hex）。"""
    return hashlib.sha256(data).hexdigest()


def _make_manifest(model_id: str = "test/org-model") -> ModelManifest:
    """构造一个文件内容已知的合成 manifest。"""
    return ModelManifest(
        model_id=model_id,
        revision="a" * 40,
        files=tuple(
            ManifestFile(path, _sha256_bytes(data)) for path, data in _CONTENTS.items()
        ),
    )


def _write_snapshot(root: Path, manifest: ModelManifest) -> Path:
    """按 manifest 写入一个 hash 全部正确的快照目录，返回快照路径。"""
    snap = root / "snapshots" / manifest.revision
    for mf in manifest.files:
        p = snap / mf.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_CONTENTS[mf.path])
    return snap


@contextmanager
def _hf_backend_patch():
    """创建 HuggingFaceBackend 所需的 patch 组合（绕过真实 hash 校验）。"""
    with (
        patch("Module.embedding.huggingface.HuggingFaceEmbeddings", MagicMock()),
        patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False),
        patch("Module.embedding.huggingface._verified_local_path", return_value="/fake/model"),
    ):
        yield


# ============================================================
# manifest 结构
# ============================================================

class TestManifestStructure:
    def test_default_model_has_fixed_revision(self):
        manifest = get_manifest(DEFAULT_MODEL_ID)
        assert manifest is not None
        assert manifest.revision == DEFAULT_MODEL_REVISION
        # 不可变 commit hash：40 位十六进制，而非可变 tag/branch 名
        assert len(manifest.revision) == 40
        assert set(manifest.revision) <= set("0123456789abcdef")
        assert manifest.revision != "main"

    def test_default_manifest_files_have_sha256(self):
        manifest = get_manifest(DEFAULT_MODEL_ID)
        assert manifest is not None
        assert len(manifest.files) >= 5
        paths = [mf.path for mf in manifest.files]
        for mf in manifest.files:
            assert not mf.path.startswith("/")  # 相对路径
            assert len(mf.sha256) == 64
            assert set(mf.sha256) <= set("0123456789abcdef")
        # 加载必需的几个文件必须在 manifest 中
        for required in ("config.json", "pytorch_model.bin", "tokenizer.json", "vocab.txt"):
            assert required in paths

    def test_unknown_model_not_manifested(self):
        assert get_manifest("org/nonexistent") is None
        assert is_known_model("org/nonexistent") is False


# ============================================================
# verify_manifest_dir —— 全量 hash 校验语义
# ============================================================

class TestVerifyManifestDir:
    def test_all_files_pass(self, tmp_path):
        manifest = _make_manifest()
        snap = _write_snapshot(tmp_path, manifest)
        ok, failures = verify_manifest_dir(snap, manifest)
        assert ok is True
        assert failures == []

    def test_tampered_file_rejected(self, tmp_path):
        """篡改文件内容（hash 不匹配）→ 拒绝。"""
        manifest = _make_manifest()
        snap = _write_snapshot(tmp_path, manifest)
        (snap / "config.json").write_bytes(b"EVIL")
        ok, failures = verify_manifest_dir(snap, manifest)
        assert ok is False
        assert any("config.json" in f for f in failures)
        assert any("hash mismatch" in f for f in failures)

    def test_missing_file_rejected(self, tmp_path):
        """缺文件 → 拒绝。"""
        manifest = _make_manifest()
        snap = _write_snapshot(tmp_path, manifest)
        (snap / "model.bin").unlink()
        ok, failures = verify_manifest_dir(snap, manifest)
        assert ok is False
        assert any("missing" in f and "model.bin" in f for f in failures)

    def test_directory_existence_not_enough(self, tmp_path):
        """目录存在但 hash 不匹配 → 仍拒绝（不以目录/单文件存在作为成功）。"""
        manifest = _make_manifest()
        snap = _write_snapshot(tmp_path, manifest)
        (snap / "config.json").write_bytes(b"tampered")
        (snap / "extra.bin").write_bytes(b"extra")  # 多余文件不能掩盖损坏
        ok, failures = verify_manifest_dir(snap, manifest)
        assert ok is False

    def test_mirror_content_different_rejected(self, tmp_path):
        """镜像内容不同（同 revision 但文件 hash 不同）→ 拒绝。"""
        manifest = _make_manifest()
        snap = _write_snapshot(tmp_path, manifest)
        (snap / "model.bin").write_bytes(b"different-weights")
        ok, failures = verify_manifest_dir(snap, manifest)
        assert ok is False
        assert any("model.bin" in f for f in failures)


# ============================================================
# endpoint 策略 —— 官方默认，mirror 显式 opt-in
# ============================================================

class TestEndpointPolicy:
    def test_default_official(self, monkeypatch):
        monkeypatch.delenv(HF_MIRROR_ENV, raising=False)
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        assert resolve_endpoint() == HF_OFFICIAL_ENDPOINT
        assert is_mirror_enabled() is False

    def test_mirror_explicit_opt_in(self, monkeypatch):
        monkeypatch.setenv(HF_MIRROR_ENV, "https://hf-mirror.com")
        assert is_mirror_enabled() is True
        assert resolve_endpoint() == "https://hf-mirror.com"

    def test_user_hf_endpoint_respected(self, monkeypatch):
        monkeypatch.delenv(HF_MIRROR_ENV, raising=False)
        monkeypatch.setenv("HF_ENDPOINT", "https://internal.mirror.example")
        assert resolve_endpoint() == "https://internal.mirror.example"
        assert is_mirror_enabled() is False


# ============================================================
# remote-code 策略 —— 默认 False，allowlist 显式开启
# ============================================================

class TestRemoteCodePolicy:
    def test_default_model_remote_code_false(self):
        assert trust_remote_code_for(DEFAULT_MODEL_ID) is False

    def test_unknown_model_remote_code_false(self):
        assert trust_remote_code_for("org/unknown") is False

    def test_allowlisted_manifest_model_enabled(self, monkeypatch):
        import Module.embedding.model_manifest as mm
        manifest = _make_manifest()
        monkeypatch.setattr(mm, "_MANIFESTS", {manifest.model_id: manifest})
        monkeypatch.setattr(mm, "_REMOTE_CODE_ALLOWLIST", frozenset({manifest.model_id}))
        assert trust_remote_code_for(manifest.model_id) is True

    def test_allowlisted_without_manifest_disabled(self, monkeypatch):
        """allowlist 但没有固定 manifest（revision/hash）→ 仍不开 remote code。"""
        import Module.embedding.model_manifest as mm
        monkeypatch.setattr(mm, "_MANIFESTS", {})
        monkeypatch.setattr(mm, "_REMOTE_CODE_ALLOWLIST", frozenset({"test/org-model"}))
        assert trust_remote_code_for("test/org-model") is False


# ============================================================
# 下载流程 —— 固定 revision + 校验后原子切换 + 失败保留旧缓存
# ============================================================

def _register_manifest(monkeypatch, manifest: ModelManifest) -> None:
    """把合成 manifest 注册进模型注册表（monkeypatch 恢复）。"""
    import Module.embedding.model_manifest as mm
    monkeypatch.setattr(mm, "_MANIFESTS", {manifest.model_id: manifest})


class TestDownloadFlow:
    def test_download_pins_revision_and_atomic_switch(self, tmp_path, monkeypatch):
        from scripts.download_model import download_model

        manifest = _make_manifest()
        _register_manifest(monkeypatch, manifest)
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        monkeypatch.delenv(HF_MIRROR_ENV, raising=False)
        cache = tmp_path / "cache"
        cache.mkdir()

        def _fake_download(model, *, cache_folder, revision, trust_remote_code):
            # 固定 revision 下发；默认 remote code False
            assert revision == manifest.revision
            assert trust_remote_code is False
            hub = Path(cache_folder) / ("models--" + model.replace("/", "--"))
            _write_snapshot(hub, manifest)
            return MagicMock()

        with patch("scripts.download_model.SentenceTransformer", side_effect=_fake_download):
            ok = download_model(manifest.model_id, cache)

        assert ok is True
        target = cache / "models--test--org-model" / "snapshots" / manifest.revision
        assert target.is_dir()
        assert (target / "config.json").read_bytes() == b"config"
        # refs/main 指向固定 revision
        refs = cache / "models--test--org-model" / "refs" / "main"
        assert refs.read_text(encoding="utf-8") == manifest.revision
        # 临时目录已清理
        assert not (cache / ".tmp").exists()

    def test_tampered_download_keeps_old_cache(self, tmp_path, monkeypatch):
        from scripts.download_model import download_model

        manifest = _make_manifest()
        _register_manifest(monkeypatch, manifest)
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        monkeypatch.delenv(HF_MIRROR_ENV, raising=False)
        cache = tmp_path / "cache"
        cache.mkdir()
        # 旧缓存：一个校验通过的快照
        old_snap = _write_snapshot(cache / "models--test--org-model", manifest)
        old_config = (old_snap / "config.json").read_bytes()

        def _bad_download(model, *, cache_folder, revision, trust_remote_code):
            hub = Path(cache_folder) / ("models--" + model.replace("/", "--"))
            snap = _write_snapshot(hub, manifest)
            (snap / "model.bin").write_bytes(b"EVIL-WEIGHTS")  # 篡改 → hash 不匹配
            return MagicMock()

        with patch("scripts.download_model.SentenceTransformer", side_effect=_bad_download):
            ok = download_model(manifest.model_id, cache)

        assert ok is False
        # 旧缓存保留且未被污染
        assert old_snap.is_dir()
        assert (old_snap / "config.json").read_bytes() == old_config
        assert not (cache / ".tmp").exists()

    def test_missing_file_download_fails(self, tmp_path, monkeypatch):
        from scripts.download_model import download_model

        manifest = _make_manifest()
        _register_manifest(monkeypatch, manifest)
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        monkeypatch.delenv(HF_MIRROR_ENV, raising=False)
        cache = tmp_path / "cache"
        cache.mkdir()

        def _incomplete_download(model, *, cache_folder, revision, trust_remote_code):
            hub = Path(cache_folder) / ("models--" + model.replace("/", "--"))
            snap = _write_snapshot(hub, manifest)
            (snap / "model.bin").unlink()  # 缺文件
            return MagicMock()

        with patch("scripts.download_model.SentenceTransformer", side_effect=_incomplete_download):
            ok = download_model(manifest.model_id, cache)

        assert ok is False
        assert not (cache / "models--test--org-model").exists()

    def test_mirror_content_different_rejected_and_keeps_old(self, tmp_path, monkeypatch):
        """镜像显式 opt-in 但内容与 manifest 不同 → 拒绝，保留旧缓存。"""
        from scripts.download_model import download_model

        monkeypatch.setenv(HF_MIRROR_ENV, "https://hf-mirror.com")
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        manifest = _make_manifest()
        _register_manifest(monkeypatch, manifest)
        cache = tmp_path / "cache"
        cache.mkdir()
        old_snap = _write_snapshot(cache / "models--test--org-model", manifest)
        old_config = (old_snap / "config.json").read_bytes()

        def _mirror_download(model, *, cache_folder, revision, trust_remote_code):
            hub = Path(cache_folder) / ("models--" + model.replace("/", "--"))
            snap = _write_snapshot(hub, manifest)
            (snap / "model.bin").write_bytes(b"mirror-different")  # 镜像内容不同
            return MagicMock()

        with patch("scripts.download_model.SentenceTransformer", side_effect=_mirror_download):
            ok = download_model(manifest.model_id, cache)

        assert ok is False
        # 下载过程确实使用了镜像 endpoint
        assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"
        assert (old_snap / "config.json").read_bytes() == old_config

    def test_cached_model_valid_requires_full_manifest(self, tmp_path, monkeypatch):
        from scripts.download_model import cached_model_valid

        manifest = _make_manifest()
        _register_manifest(monkeypatch, manifest)
        cache = tmp_path / "cache"
        cache.mkdir()
        # 空目录 → 无效
        assert cached_model_valid(manifest.model_id, cache) is False
        # 只写一个文件 → 无效（不以单文件/目录存在作为成功）
        snap = _write_snapshot(cache / "models--test--org-model", manifest)
        (snap / "model.bin").unlink()
        assert cached_model_valid(manifest.model_id, cache) is False
        # 完整正确 → 有效
        _write_snapshot(cache / "models--test--org-model", manifest)
        assert cached_model_valid(manifest.model_id, cache) is True
        # 篡改 → 再次无效
        snap2 = cache / "models--test--org-model" / "snapshots" / manifest.revision
        (snap2 / "config.json").write_bytes(b"evil")
        assert cached_model_valid(manifest.model_id, cache) is False


# ============================================================
# 加载侧 —— HuggingFaceBackend 供应链闸门
# ============================================================

class TestHuggingFaceLoadSide:
    def test_backend_trust_remote_code_default_false(self):
        """HuggingFaceBackend 构造时 model_kwargs.trust_remote_code 默认 False。"""
        from Module.embedding.huggingface import HuggingFaceBackend

        with patch("Module.embedding.huggingface.HuggingFaceEmbeddings") as emb_cls, \
             patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False), \
             patch("Module.embedding.huggingface._verified_local_path", return_value="/fake/model"):
            HuggingFaceBackend(model_name=DEFAULT_MODEL_ID, cache_folder="/tmp/none")
        kwargs = emb_cls.call_args.kwargs
        assert kwargs["model_kwargs"]["trust_remote_code"] is False
        assert kwargs["model_kwargs"]["local_files_only"] is True

    def test_backend_trust_remote_code_allowlisted(self, monkeypatch):
        """allowlist 内且已登记 manifest 的模型才显式开启 remote code。"""
        import Module.embedding.model_manifest as mm
        monkeypatch.setattr(mm, "_REMOTE_CODE_ALLOWLIST", frozenset({DEFAULT_MODEL_ID}))
        from Module.embedding.huggingface import HuggingFaceBackend

        with patch("Module.embedding.huggingface.HuggingFaceEmbeddings") as emb_cls, \
             patch("Module.embedding.huggingface.torch.cuda.is_available", return_value=False), \
             patch("Module.embedding.huggingface._verified_local_path", return_value="/fake/model"):
            HuggingFaceBackend(model_name=DEFAULT_MODEL_ID, cache_folder="/tmp/none")
        kwargs = emb_cls.call_args.kwargs
        assert kwargs["model_kwargs"]["trust_remote_code"] is True

    def test_manifest_model_missing_locally_raises(self):
        """manifest 模型本地缺失 → 拒绝加载并提示先下载。"""
        from Module.embedding.huggingface import _verified_local_path

        with pytest.raises(RuntimeError, match="本地缓存缺失"):
            _verified_local_path(DEFAULT_MODEL_ID, None)

    def test_manifest_model_tampered_raises(self, tmp_path):
        """manifest 模型本地缓存被篡改/缺文件 → 拒绝加载。"""
        from Module.embedding.huggingface import _verified_local_path

        snap = tmp_path / "model"
        snap.mkdir()
        (snap / "config.json").write_text("tampered")  # 其余 manifest 文件缺失
        with pytest.raises(RuntimeError, match="缓存校验失败"):
            _verified_local_path(DEFAULT_MODEL_ID, str(snap))
