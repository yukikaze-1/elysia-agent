"""core/schema/memory_reflector.py 的 ReflectorActions 模型测试。"""
import pytest
from pydantic import ValidationError

from core.schema.memory import MemoryType
from core.schema.memory_reflector import (
    DeleteItem,
    MergeItem,
    ReflectorActions,
    UpdateItem,
)


@pytest.mark.unit
class TestReflectorActions:
    """ReflectorActions 整体校验规则。"""

    def test_empty_object_valid(self):
        """{} → 三个空列表（干净组的合法输出）。"""
        a = ReflectorActions.model_validate({})
        assert a.merges == [] and a.updates == [] and a.deletes == []

    def test_partial_keys_valid(self):
        """部分键 → 缺失键取默认空列表。"""
        a = ReflectorActions.model_validate({"merges": []})
        assert a.updates == [] and a.deletes == []

    def test_full_valid_input(self):
        a = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并后内容",
                        "type": "Fact", "poignancy": 7}],
            "updates": [{"id": "m3", "new_content": "更新后内容"}],
            "deletes": [{"id": "m4"}],
        })
        assert a.merges[0].type == MemoryType.FACT
        assert a.merges[0].ids == ["m1", "m2"]
        assert a.updates[0].new_content == "更新后内容"
        assert a.deletes[0].id == "m4"

    def test_caps_exceeded_rejected(self):
        """超限拒绝：6 merges / 6 updates / 4 deletes。"""
        merge = {"ids": ["a", "b"], "merged_content": "x"}
        update = {"id": "a", "new_content": "x"}
        delete = {"id": "a"}
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"merges": [merge] * 6})
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"updates": [update] * 6})
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"deletes": [delete] * 4})

    def test_reason_extra_field_ignored(self):
        """updates/deletes 带 reason（prompt 要求 LLM 输出）→ 被忽略不报错。"""
        a = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "新", "reason": "过时"}],
            "deletes": [{"id": "m2", "reason": "纯噪音"}],
        })
        assert not hasattr(a.updates[0], "reason")


@pytest.mark.unit
class TestMergeItem:
    def test_single_id_rejected(self):
        """ids 少于 2 个 → 拒绝（原 len(ids) >= 2 守卫）。"""
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["only-one"], "merged_content": "x"})

    def test_empty_merged_content_rejected(self):
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["a", "b"], "merged_content": ""})
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["a", "b"], "merged_content": "   "})

    def test_type_lowercase_rejected(self):
        """严格枚举——小写 "fact" 拒绝（词表统一后全库 TitleCase）。"""
        with pytest.raises(ValidationError):
            MergeItem.model_validate(
                {"ids": ["a", "b"], "merged_content": "x", "type": "fact"})

    def test_type_defaults_none(self):
        """缺省 type/poignancy → None（update_text 保留原值语义）。"""
        m = MergeItem.model_validate({"ids": ["a", "b"], "merged_content": "x"})
        assert m.type is None and m.poignancy is None

    def test_poignancy_out_of_range_rejected(self):
        for bad in (0, 11):
            with pytest.raises(ValidationError):
                MergeItem.model_validate(
                    {"ids": ["a", "b"], "merged_content": "x", "poignancy": bad})


@pytest.mark.unit
class TestUpdateDeleteItem:
    def test_update_empty_fields_rejected(self):
        with pytest.raises(ValidationError):
            UpdateItem.model_validate({"id": "", "new_content": "x"})
        with pytest.raises(ValidationError):
            UpdateItem.model_validate({"id": "a", "new_content": "  "})

    def test_delete_empty_id_rejected(self):
        with pytest.raises(ValidationError):
            DeleteItem.model_validate({"id": ""})
