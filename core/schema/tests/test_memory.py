# core/schema/tests/test_memory.py
"""core/schema/memory.py 测试。"""
import pytest
from pydantic import TypeAdapter, ValidationError

from core.schema.memory import (
    CompareAction,
    ExtractedMemory,
    InsertAction,
    MemoryType,
    MergeAction,
    SkipAction,
    UpdateAction,
)


@pytest.mark.unit
class TestExtractedMemory:
    """ExtractedMemory 校验规则。"""

    def test_valid_construction(self):
        m = ExtractedMemory.model_validate(
            {"type": "Fact", "content": "妖梦喜欢蓝色", "poignancy": 7}
        )
        assert m.type == MemoryType.FACT
        assert m.content == "妖梦喜欢蓝色"
        assert m.poignancy == 7

    def test_poignancy_defaults_to_5(self):
        """缺失 poignancy 时默认 5（与旧 .get("poignancy", 5) 行为一致）。"""
        m = ExtractedMemory.model_validate({"type": "Plan", "content": "明天爬山"})
        assert m.poignancy == 5

    def test_rejects_empty_content(self):
        with pytest.raises(ValidationError):
            ExtractedMemory.model_validate({"type": "Fact", "content": ""})
        with pytest.raises(ValidationError):
            ExtractedMemory.model_validate({"type": "Fact", "content": "   "})

    def test_rejects_lowercase_type(self):
        """type 大小写敏感——词表为大写开头（Fact/Preference/...）。"""
        with pytest.raises(ValidationError):
            ExtractedMemory.model_validate({"type": "fact", "content": "ok"})


@pytest.mark.unit
class TestCompareActionDispatch:
    """CompareAction discriminated union — 按 action 字段分派到具体模型。"""

    def test_dispatch_all_four_actions(self):
        adapter = TypeAdapter(list[CompareAction])
        actions = adapter.validate_python([
            {"action": "skip"},
            {"action": "insert", "content": "新记忆", "type": "Event", "poignancy": 6},
            {"action": "update", "target_id": "id-1", "new_content": "更新后"},
            {"action": "merge", "target_id": "id-2", "merged_content": "合并后"},
        ])
        assert isinstance(actions[0], SkipAction)
        assert isinstance(actions[1], InsertAction)
        assert isinstance(actions[2], UpdateAction)
        assert isinstance(actions[3], MergeAction)

    def test_unknown_action_rejected(self):
        adapter = TypeAdapter(CompareAction)
        with pytest.raises(ValidationError):
            adapter.validate_python({"action": "destroy"})

    def test_update_optional_fields_default_none(self):
        """UpdateAction 缺失 type/poignancy → None（降级兼容）。"""
        a = UpdateAction.model_validate({"action": "update", "target_id": "id-1",
                                         "new_content": "新内容"})
        assert a.type is None
        assert a.poignancy is None

    def test_merge_optional_fields_default_none(self):
        """MergeAction 缺失 type/poignancy → None（保留 LTM 的 None-语义）。"""
        a = MergeAction.model_validate({"action": "merge", "target_id": "id-2",
                                        "merged_content": "合并内容"})
        assert a.type is None
        assert a.poignancy is None
