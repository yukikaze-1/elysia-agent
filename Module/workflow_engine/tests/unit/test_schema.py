"""ExperienceStatus 枚举化测试（schema 内部类型）。

覆盖:
  - ExperienceStatus 成员值与原 status Literal 对齐（unverified/verified/deprecated）
  - ExperienceRecord.status 默认值仍为 "unverified"
"""

import pytest

from Module.workflow_engine.schema.internal import ExperienceRecord, ExperienceStatus

pytestmark = pytest.mark.unit


class TestExperienceStatus:
    def test_members_match_old_literal(self):
        assert {m.value for m in ExperienceStatus} == {"unverified", "verified", "deprecated"}

    def test_experience_record_status_default(self):
        # 既有默认值 "unverified"（tool_name/error_pattern 为必填字段，需显式提供）
        assert ExperienceRecord(tool_name="x", error_pattern="y").status == "unverified"
