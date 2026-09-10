# 测试文件：core/schema/tests/test_memory.py
"""core/schema/memory.py 测试。"""
import pytest
from pydantic import TypeAdapter, ValidationError
from typing import Literal, get_args, get_origin

from core.schema.memory import (
    CompareAction,
    CompareActionType,
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


@pytest.mark.unit
class TestCompareActionType:
    """判别字段引用 CompareActionType 成员（防裸字符串漂移）。"""

    def test_members_match(self):
        assert {m.value for m in CompareActionType} == {"skip", "insert", "update", "merge"}

    def test_action_discriminator_references_enum_member(self):
        """判别字段必须是 Literal[CompareActionType.X]（防裸字符串漂移）。"""
        ann = SkipAction.model_fields["action"].annotation
        assert get_origin(ann) is Literal
        assert get_args(ann) == (CompareActionType.SKIP,)

    def test_discriminator_rejects_bad(self):
        # 必须用 pytest.raises 精确断言「抛 ValidationError」：旧写法 try/except Exception
        # 会把兜底的 AssertionError 一起吞掉，导致「拒绝」与「放行」两种情况都通过，
        # 测试永远不可能红（判别值放宽成裸 str 也照样绿）。
        ta = TypeAdapter(CompareAction)
        with pytest.raises(ValidationError):
            ta.validate_python({"action": "nope"})


@pytest.mark.unit
class TestMemoryTypeCoerce:
    """MemoryType.try_coerce / coerce — 不可信字符串入口的归一化。"""

    def test_try_coerce_passthrough_member(self):
        assert MemoryType.try_coerce(MemoryType.FACT) is MemoryType.FACT

    def test_try_coerce_exact_str(self):
        assert MemoryType.try_coerce("Fact") is MemoryType.FACT

    def test_try_coerce_case_insensitive(self):
        # lowercase / 全大写 / mixed → title 归一
        assert MemoryType.try_coerce("fact") is MemoryType.FACT
        assert MemoryType.try_coerce("FACT") is MemoryType.FACT
        assert MemoryType.try_coerce("preference") is MemoryType.PREFERENCE
        assert MemoryType.try_coerce("NOTE") is MemoryType.NOTE
        assert MemoryType.try_coerce("Note") is MemoryType.NOTE

    def test_try_coerce_strips_whitespace(self):
        assert MemoryType.try_coerce("  Fact  ") is MemoryType.FACT

    def test_try_coerce_unknown_returns_none(self):
        assert MemoryType.try_coerce("bogus") is None
        assert MemoryType.try_coerce("fact-1") is None
        assert MemoryType.try_coerce("") is None
        assert MemoryType.try_coerce(None) is None
        assert MemoryType.try_coerce(123) is None

    def test_coerce_unknown_falls_back_to_note(self):
        assert MemoryType.coerce("bogus") is MemoryType.NOTE
        assert MemoryType.coerce(None) is MemoryType.NOTE
        assert MemoryType.coerce(123) is MemoryType.NOTE
        assert MemoryType.coerce("") is MemoryType.NOTE

    def test_coerce_valid_unchanged(self):
        assert MemoryType.coerce("fact") is MemoryType.FACT
        assert MemoryType.coerce(MemoryType.PLAN) is MemoryType.PLAN
