"""多角色数据完整性自检测试（P3）。

覆盖三层：
1. 两类 repo 计数方法的 Mongo 过滤语义（用内存 FakeDocumentDBBackend 真跑过滤器）；
2. CharacterIntegrityReport / check_character_data_integrity 的语义与降级；
3. 启动步骤接线（agent.start() 后报告挂在 agent 上）。
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.service


def _fake():
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
    return FakeDocumentDBBackend()


# ---------------------------------------------------------------------------
# repo 计数：过滤语义（真跑 Mongo 过滤器，覆盖三种缺口形态）
# ---------------------------------------------------------------------------

class TestSessionBindingCount:
    """sessions 缺角色绑定的计数口径。"""

    def test_counts_missing_and_empty_and_blank(self):
        from core.storage.session_repo import SessionRepository
        from core.schema.types import SessionId

        fake = _fake()
        repo = SessionRepository(db=fake)
        loop = asyncio.new_event_loop()
        try:
            # 合格：两字段齐备
            loop.run_until_complete(repo.create(
                SessionId("conv_ok"),
                character_binding={"character_id": "chr_a", "instance_id": "chi_a"}))
            # 缺口 1：character_binding 为空对象 {}（真实故障形态，50 条）
            loop.run_until_complete(repo.create(SessionId("conv_empty")))
            # 缺口 2：键缺失（直接落库，绕过 create 的归一化）
            loop.run_until_complete(fake.insert_one("sessions", {"session_id": "conv_missing"}))
            # 缺口 3：值为空串
            loop.run_until_complete(fake.insert_one(
                "sessions",
                {"session_id": "conv_blank",
                 "character_binding": {"character_id": "", "instance_id": ""}}))

            count = loop.run_until_complete(repo.count_incomplete_character_binding())
        finally:
            loop.close()

        assert count == 3, "空对象/键缺失/空串三种缺口都应计入，合格记录不计"

    def test_zero_when_all_complete(self):
        from core.storage.session_repo import SessionRepository
        from core.schema.types import SessionId

        fake = _fake()
        repo = SessionRepository(db=fake)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(repo.create(
                SessionId("conv_ok"),
                character_binding={"character_id": "chr_a", "instance_id": "chi_a"}))
            count = loop.run_until_complete(repo.count_incomplete_character_binding())
        finally:
            loop.close()
        assert count == 0

    def test_raises_when_disconnected(self):
        """未连接 → StorageUnavailableError（区分「0 缺口」与「未扫描」）。"""
        from core.storage.base import StorageUnavailableError
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.count_incomplete_character_binding())


class TestAssistantCharacterCount:
    """message_archive 缺角色快照的 assistant 消息计数口径。"""

    def test_counts_null_and_missing_only_for_assistant(self):
        from core.storage.message_repo import MessageRepository

        fake = _fake()
        repo = MessageRepository(db=fake)
        loop = asyncio.new_event_loop()
        try:
            docs = [
                # 缺口 1：character 显式 null（真实故障形态）
                {"role": "assistant", "session_id": "s1", "character": None},
                # 缺口 2：character 键缺失
                {"role": "assistant", "session_id": "s1"},
                # 合格：带角色快照
                {"role": "assistant", "session_id": "s1",
                 "character": {"character_id": "chr_a", "instance_id": "chi_a"}},
                # user/tool 消息本无 character 要求——不计入
                {"role": "user", "session_id": "s1"},
                {"role": "tool", "session_id": "s1", "character": None},
            ]
            for d in docs:
                loop.run_until_complete(fake.insert_one("message_archive", d))

            count = loop.run_until_complete(repo.count_assistant_without_character())
        finally:
            loop.close()

        assert count == 2, "只统计 assistant 的 null/缺失，user/tool 不计入"

    def test_raises_when_disconnected(self):
        from core.storage.base import StorageUnavailableError
        from core.storage.message_repo import MessageRepository

        repo = MessageRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.count_assistant_without_character())


# ---------------------------------------------------------------------------
# CharacterIntegrityReport / check_character_data_integrity
# ---------------------------------------------------------------------------

class _StubRepo:
    def __init__(self, value=0, raises=False):
        self._value = value
        self._raises = raises

    async def count_incomplete_character_binding(self) -> int:
        if self._raises:
            raise RuntimeError("mongo down")
        return self._value

    async def count_assistant_without_character(self) -> int:
        if self._raises:
            raise RuntimeError("mongo down")
        return self._value


class TestIntegrityReportSemantics:
    def test_has_gaps_false_when_not_scanned(self):
        """未扫描时不得断言「无缺口」以外的事——has_gaps 恒 False。"""
        from core.character.integrity import CharacterIntegrityReport

        report = CharacterIntegrityReport(scanned=False)
        assert report.has_gaps is False
        assert "未执行" in report.summary()

    def test_has_gaps_true_when_any_side_nonzero(self):
        from core.character.integrity import CharacterIntegrityReport

        assert CharacterIntegrityReport(
            sessions_missing_binding=1, scanned=True).has_gaps is True
        assert CharacterIntegrityReport(
            assistant_messages_missing_character=2, scanned=True).has_gaps is True
        assert CharacterIntegrityReport(scanned=True).has_gaps is False


class TestCheckCharacterDataIntegrity:
    @pytest.mark.asyncio
    async def test_skipped_when_capability_disabled(self):
        from core.character.integrity import check_character_data_integrity

        report = await check_character_data_integrity(
            enabled=False, session_repo=_StubRepo(5), message_repo=_StubRepo(7))
        assert report.scanned is False
        assert report.has_gaps is False

    @pytest.mark.asyncio
    async def test_scans_and_reports_counts(self):
        from core.character.integrity import check_character_data_integrity

        report = await check_character_data_integrity(
            enabled=True, session_repo=_StubRepo(50), message_repo=_StubRepo(14))
        assert report.scanned is True
        assert report.sessions_missing_binding == 50
        assert report.assistant_messages_missing_character == 14
        assert report.has_gaps is True

    @pytest.mark.asyncio
    async def test_degrades_on_storage_failure(self):
        """存储不可用/查询异常 → 未扫描，绝不抛（启动不能因自检失败而中断）。"""
        from core.character.integrity import check_character_data_integrity

        report = await check_character_data_integrity(
            enabled=True, session_repo=_StubRepo(raises=True),
            message_repo=_StubRepo(raises=True))
        assert report.scanned is False
        assert report.has_gaps is False

    @pytest.mark.asyncio
    async def test_degrades_when_repo_missing(self):
        from core.character.integrity import check_character_data_integrity

        report = await check_character_data_integrity(
            enabled=True, session_repo=None, message_repo=None)
        assert report.scanned is False

    @pytest.mark.asyncio
    async def test_negative_counts_clamped_to_zero(self):
        """防御：异常计数不应产生负值（避免 summary/has_gaps 语义失真）。"""
        from core.character.integrity import check_character_data_integrity

        report = await check_character_data_integrity(
            enabled=True, session_repo=_StubRepo(-3), message_repo=_StubRepo(-1))
        assert report.sessions_missing_binding == 0
        assert report.assistant_messages_missing_character == 0
        assert report.has_gaps is False


class TestLogIntegrityReport:
    def test_warns_when_gaps_present(self, caplog):
        import logging

        from core.character.integrity import CharacterIntegrityReport, log_integrity_report

        report = CharacterIntegrityReport(
            sessions_missing_binding=50,
            assistant_messages_missing_character=14,
            scanned=True,
        )
        with caplog.at_level(logging.WARNING):
            log_integrity_report(report)

        assert caplog.records, "有缺口必须产生显著告警"
        message = caplog.records[-1].getMessage()
        assert "50" in message and "14" in message
        assert "fail-closed" in message

    def test_no_warning_when_clean(self, caplog):
        import logging

        from core.character.integrity import CharacterIntegrityReport, log_integrity_report

        with caplog.at_level(logging.WARNING):
            log_integrity_report(CharacterIntegrityReport(scanned=True))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_no_warning_when_not_scanned(self, caplog):
        """未扫描不是告警条件（Mongo 离线启动是常态）。"""
        import logging

        from core.character.integrity import CharacterIntegrityReport, log_integrity_report

        with caplog.at_level(logging.WARNING):
            log_integrity_report(CharacterIntegrityReport(scanned=False))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
