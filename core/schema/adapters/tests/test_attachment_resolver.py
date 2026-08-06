"""AttachmentFileResolver 单元测试（批次 7 Task 2，marker=unit）。

模块级 register/unregister/load/resolve 纯函数（无 I/O），验证：
- register(att_id, kind, stored_name) 后 resolve 返回
  data/uploads/{kind}/{stored_name} 的绝对 Path（基于 ProjectPath.DATA_UPLOADS）
- unregister 后 resolve 返回 None
- load 从附件记录批量预热
- 未登记返回 None
"""

import pytest

from core.schema.adapters import attachment_resolver as resolver
from paths import ProjectPath

pytestmark = pytest.mark.unit


def _expected(kind: str, stored_name: str):
    return ProjectPath.DATA_UPLOADS.resolve() / kind / stored_name


class TestResolver:
    def test_register_then_resolve_returns_path(self):
        resolver.register("att_abc", "image", "abc123.png")
        p = resolver.resolve("att_abc")
        assert p is not None
        assert p == _expected("image", "abc123.png")
        resolver.unregister("att_abc")

    def test_resolve_unregistered_returns_none(self):
        assert resolver.resolve("att_nope") is None

    def test_unregister_removes_mapping(self):
        resolver.register("att_abc", "audio", "abc123.wav")
        resolver.unregister("att_abc")
        assert resolver.resolve("att_abc") is None

    def test_load_warms_from_records(self):
        records = [
            {"attachment_id": "att_1", "kind": "image", "stored_name": "a.png"},
            {"attachment_id": "att_2", "kind": "video", "stored_name": "b.mp4"},
            {"attachment_id": "att_3"},  # 缺 kind/stored_name，跳过
        ]
        resolver.load(records)
        assert resolver.resolve("att_1") == _expected("image", "a.png")
        assert resolver.resolve("att_2") == _expected("video", "b.mp4")
        assert resolver.resolve("att_3") is None
        resolver.unregister("att_1")
        resolver.unregister("att_2")

    def test_path_is_absolute_under_data_uploads(self):
        resolver.register("att_abc", "file", "c.pdf")
        p = resolver.resolve("att_abc")
        assert p.is_absolute()
        assert ProjectPath.DATA_UPLOADS.resolve() in p.parents
        resolver.unregister("att_abc")
