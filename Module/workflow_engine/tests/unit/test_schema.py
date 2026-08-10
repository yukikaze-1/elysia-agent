"""ExperienceStatus 枚举化测试（schema 内部类型）。

覆盖:
  - ExperienceStatus 成员值与原 status Literal 对齐（unverified/verified/deprecated）
  - ExperienceRecord.status 默认值仍为 "unverified"
"""

import pytest

from Module.workflow_contract import SessionId, WorkflowId, WorkflowStatus
from Module.workflow_engine.schema.internal import ExperienceRecord, ExperienceStatus, WorkflowState
from Module.workflow_engine.schema.types import ResumeErrorCode

pytestmark = pytest.mark.unit


class TestExperienceStatus:
    def test_members_match_old_literal(self):
        assert {m.value for m in ExperienceStatus} == {"unverified", "verified", "deprecated"}

    def test_experience_record_status_default(self):
        # 既有默认值 "unverified"（tool_name/error_pattern 为必填字段，需显式提供）
        assert ExperienceRecord(tool_name="x", error_pattern="y").status == "unverified"


def test_resume_error_has_queue_full():
    assert ResumeErrorCode.QUEUE_FULL.value == "QUEUE_FULL"


def test_workflow_state_resume_pending_default_false():
    st = WorkflowState(
        task_id=WorkflowId("t1"),
        status=WorkflowStatus.QUEUED,
        tools=[],
        session_id=SessionId("s1"),
    )
    assert st.resume_pending is False
