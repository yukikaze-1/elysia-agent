# 测试文件：core/tools/tests/test_vision_cache.py
"""S3-G5：describe_image 的磁盘缓存必须有 **TTL 与上限**。

原状：写侧记 `created_at`、读侧**从不看它**，没有任何淘汰 ⇒ 只增不减
（重核时实测磁盘 3 文件/17K，但那是"还没用起来"而不是"有界"）。本文件此前
**零覆盖**（`core/tools/tests/` 下没有任何 vision 用例）。
"""

import json
import time
from pathlib import Path

import pytest

import core.tools.vision.plugin as vision
from core.tools.vision.plugin import (
    CACHE_TTL_SECONDS,
    _load_cache,
    _save_cache,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """把缓存目录指到 tmp（不碰真实 data/cache/vision）。"""
    d = tmp_path / "vision"
    monkeypatch.setattr(vision, "_CACHE_DIR", d)
    return d


def _write_raw(cache_dir: Path, key: str, *, age_seconds: float, description: str = "旧描述"):
    cache_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - age_seconds))
    (cache_dir / f"{key}.json").write_text(json.dumps({
        "hash": key, "description": description, "model": "mimo/x", "created_at": stamp,
    }, ensure_ascii=False), encoding="utf-8")


class TestVisionCacheTtl:
    def test_fresh_entry_hits(self, cache_dir):
        _save_cache("k1", "描述", "mimo/x")
        assert _load_cache("k1") == "描述"

    def test_expired_entry_is_a_miss_and_is_deleted(self, cache_dir):
        """过 TTL → 未命中，且文件被删（否则"过期"只是逻辑上的，磁盘照旧只增）。"""
        _write_raw(cache_dir, "old", age_seconds=CACHE_TTL_SECONDS + 60)
        assert _load_cache("old") is None
        assert not (cache_dir / "old.json").exists(), "过期条目没被清掉"

    def test_entry_just_inside_ttl_still_hits(self, cache_dir):
        """边界对照：刚好在 TTL 内的条目必须命中（别把 TTL 做成"永远未命中"）。"""
        _write_raw(cache_dir, "fresh", age_seconds=CACHE_TTL_SECONDS - 60)
        assert _load_cache("fresh") == "旧描述"

    def test_missing_timestamp_is_treated_as_unknown_not_expired(self, cache_dir):
        """时间戳缺失/不可解析 → 不当作过期（**不能**因为读不懂就删用户缓存）。"""
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "nots.json").write_text(
            json.dumps({"hash": "nots", "description": "无时间戳"}), encoding="utf-8")
        assert _load_cache("nots") == "无时间戳"

    def test_corrupt_file_is_a_miss_without_raising(self, cache_dir):
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "bad.json").write_text("{ not json", encoding="utf-8")
        assert _load_cache("bad") is None


class TestVisionCacheCap:
    def test_write_evicts_oldest_beyond_cap(self, cache_dir, monkeypatch):
        """超过上限 → 淘汰**最旧**的，且总量收敛到上限。"""
        monkeypatch.setattr(vision, "CACHE_MAX_ENTRIES", 5)
        for i in range(5):
            # i 越大越旧：old0 最新、old4 最旧
            _write_raw(cache_dir, f"old{i}", age_seconds=10_000 + i)
        _save_cache("newest", "新描述", "mimo/x")     # 写入后触发淘汰 → 6 → 5

        files = sorted(p.stem for p in cache_dir.glob("*.json"))
        assert len(files) == 5, f"淘汰后仍超上限：{files}"
        assert "newest" in files, "把刚写的条目淘汰掉了"
        assert "old4" not in files, f"最旧的条目（old4）没被淘汰：{files}"
        assert "old0" in files, f"淘汰的不是最旧的那条：{files}"

    def test_under_cap_evicts_nothing(self, cache_dir, monkeypatch):
        """回归网：没超上限时不得删任何东西。"""
        monkeypatch.setattr(vision, "CACHE_MAX_ENTRIES", 5)
        for i in range(3):
            _save_cache(f"k{i}", f"描述{i}", "mimo/x")
        assert sorted(p.stem for p in cache_dir.glob("*.json")) == ["k0", "k1", "k2"]
