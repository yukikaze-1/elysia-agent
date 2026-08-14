"""契约层枚举测试：SearchCategory / UploadKind（marker=unit，阶段 3 联调整改 D3-B）。

新增枚举不被任何 ContractModel 字段引用 → gen_ts/gen_schema 不收集 → TS/schema/raw_ts
快照不变（Task 1 Step 5 用 make check-types 验证）；本测试锁定枚举值集合（wire 单源）。
"""
from enum import StrEnum

import pytest

from frontend_contract.rest import SearchCategory, UploadKind

pytestmark = pytest.mark.unit


class TestSearchCategory:
    def test_value_set_8(self):
        assert {m.value for m in SearchCategory} == {
            "text", "image", "link", "video", "audio", "file", "tool", "sticker",
        }

    def test_is_str_enum(self):
        assert issubclass(SearchCategory, StrEnum)

    def test_sticker_member(self):
        assert SearchCategory.STICKER == "sticker"
        assert SearchCategory("sticker") is SearchCategory.STICKER


class TestUploadKind:
    def test_value_set_4(self):
        assert {m.value for m in UploadKind} == {"image", "video", "audio", "file"}

    def test_sticker_not_included(self):
        assert "sticker" not in {m.value for m in UploadKind}
