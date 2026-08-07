"""ToolResultType/ToolStatus 枚举化测试。

覆盖:
  - ToolResultType 成员值与原 ResultType Literal 对齐（text/image/audio/file/mixed）
  - ToolStatus 成员值与原 status Literal 对齐（pending/processing/done/failed）
"""

import pytest

from Module.tool.protocol import ToolResultType, ToolStatus

pytestmark = pytest.mark.unit


class TestToolResultType:
    def test_members_match_old_literal(self):
        assert {m.value for m in ToolResultType} == {"text", "image", "audio", "file", "mixed"}

    def test_is_str_enum(self):
        assert ToolResultType.TEXT == "text"


class TestToolStatus:
    def test_members_match_old_literal(self):
        assert {m.value for m in ToolStatus} == {"pending", "processing", "done", "failed"}

    def test_default_is_processing(self):
        # 既有默认值 "processing"
        assert ToolStatus.PROCESSING == "processing"
