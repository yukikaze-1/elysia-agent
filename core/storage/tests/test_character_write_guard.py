"""写入期角色完整性兜底测试（P3 剩余第 3 项，marker=unit）。

问题单 §5.2 ★★☆：`character_profiles=true` 时 assistant 消息必须带角色身份快照；
缺失若**静默落库**，缺口只会在前端整页拒绝时暴露。本兜底在**两个落库边界**加显著告警：

- `MessageRepository.append()` —— 永久归档 message_archive；
- `SessionRepository.save()` —— sessions 内嵌 messages（detail 响应来源）。

设计取舍（有意）：**告警但照常写入**。归档是 append-only 永久日志，丢弃比留下缺口更糟
（历史不可恢复）；兜底目的是可见性，不是拒写——真正的拒写在源头（P3-1 已修工具轮漏传）。
`assistant + tool_calls 但正文为空`是合法形态，故空正文不告警，只有缺角色快照才告警。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from core.schema.message import AssistantMessage, ToolResultMessage, UserMessage
from core.schema.part import TextPart
from core.schema.types import MessageId, ToolCallId
from core.storage.message_repo import COLLECTION as MSG_COLLECTION
from core.storage.session_repo import COLLECTION as SESSION_COLLECTION

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


def _fake():
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
    return FakeDocumentDBBackend()


def _snapshot():
    from core.schema.message import CharacterMessageSnapshot
    return CharacterMessageSnapshot(
        character_id="chr_builtin_elysia",
        character_instance_id="chi_builtin_elysia",
        character_revision=1,
        display_name="Elysia",
    )


def _assistant(character=None, content="hi") -> AssistantMessage:
    return AssistantMessage(
        content=[TextPart(content=content)] if content else [],
        message_id=MessageId("msg_" + "a" * 26),
        character=character,
    )


def _guard_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# message_archive 边界
# ---------------------------------------------------------------------------

class TestArchiveWriteGuard:
    def test_warns_when_assistant_missing_character(self, caplog):
        """开启兜底 + assistant 缺快照 → 显著告警（但照常归档，不丢历史）。"""
        from core.storage.message_repo import MessageRepository

        fake = _fake()
        repo = MessageRepository(db=fake, require_assistant_character=True)
        with caplog.at_level(logging.WARNING):
            _run(repo.append("conv_1", _assistant()))

        msgs = _guard_warnings(caplog)
        assert msgs, "缺角色快照必须告警，不得静默落库"
        assert "缺快照的 assistant" in msgs[-1]
        # 告警但写入：历史不因兜底丢失
        assert len(_run(fake.find(MSG_COLLECTION, {}))) == 1

    def test_silent_when_flag_disabled(self, caplog):
        """未开启（character_profiles=false / 未注入能力）→ 不告警（legacy 合法状态）。"""
        from core.storage.message_repo import MessageRepository

        repo = MessageRepository(db=_fake())
        with caplog.at_level(logging.WARNING):
            _run(repo.append("conv_1", _assistant()))
        assert not _guard_warnings(caplog)

    def test_silent_when_character_present(self, caplog):
        from core.storage.message_repo import MessageRepository

        repo = MessageRepository(db=_fake(), require_assistant_character=True)
        with caplog.at_level(logging.WARNING):
            _run(repo.append("conv_1", _assistant(character=_snapshot())))
        assert not _guard_warnings(caplog)

    def test_silent_for_user_and_tool_roles(self, caplog):
        """user/tool 消息本无角色快照要求 → 不告警。"""
        from core.storage.message_repo import MessageRepository

        repo = MessageRepository(db=_fake(), require_assistant_character=True)
        user = UserMessage(content=[TextPart(content="hi")],
                           message_id=MessageId("msg_" + "u" * 26))
        tool = ToolResultMessage(content=[TextPart(content="ok")],
                                 tool_call_id=ToolCallId("call_1"), tool_name="get_time",
                                 message_id=MessageId("msg_" + "t" * 26))
        with caplog.at_level(logging.WARNING):
            _run(repo.append("conv_1", user))
            _run(repo.append("conv_1", tool))
        assert not _guard_warnings(caplog)

    def test_empty_content_with_character_is_silent(self, caplog):
        """工具轮常态：正文为空但带角色快照 → 合法，不告警。"""
        from core.storage.message_repo import MessageRepository

        repo = MessageRepository(db=_fake(), require_assistant_character=True)
        with caplog.at_level(logging.WARNING):
            _run(repo.append("conv_1", _assistant(character=_snapshot(), content="")))
        assert not _guard_warnings(caplog)


# ---------------------------------------------------------------------------
# sessions 内嵌边界
# ---------------------------------------------------------------------------

class TestSessionWriteGuard:
    def _save(self, repo, messages, session_id="conv_1"):
        from core.schema.character import CharacterState
        from core.schema.types import SessionId

        return repo.save(session_id, messages, CharacterState(session_id=SessionId(session_id)))

    def test_warns_when_embedded_assistant_missing_character(self, caplog):
        from core.storage.session_repo import SessionRepository

        fake = _fake()
        repo = SessionRepository(db=fake, require_assistant_character=True)
        with caplog.at_level(logging.WARNING):
            _run(self._save(repo, [_assistant()]))

        msgs = _guard_warnings(caplog)
        assert msgs, "内嵌 assistant 缺角色快照必须告警"
        assert "缺快照的 assistant" in msgs[-1]
        assert len(_run(fake.find(SESSION_COLLECTION, {}))) == 1  # 照常写入

    def test_silent_when_flag_disabled(self, caplog):
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=_fake())
        with caplog.at_level(logging.WARNING):
            _run(self._save(repo, [_assistant()]))
        assert not _guard_warnings(caplog)

    def test_silent_when_all_assistants_stamped(self, caplog):
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=_fake(), require_assistant_character=True)
        with caplog.at_level(logging.WARNING):
            _run(self._save(repo, [_assistant(character=_snapshot())]))
        assert not _guard_warnings(caplog)

    def test_counts_multiple_gaps_in_one_warning(self, caplog):
        """一次 save 多条缺口 → 合并为一条告警并报数量（避免刷日志）。"""
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=_fake(), require_assistant_character=True)
        msgs = [_assistant(), _assistant(), _assistant(character=_snapshot())]
        for i, m in enumerate(msgs):
            m.message_id = MessageId(f"msg_{i:026d}")
        with caplog.at_level(logging.WARNING):
            _run(self._save(repo, msgs))

        warnings = _guard_warnings(caplog)
        assert len(warnings) == 1, "多条缺口应合并为单条告警"
        assert "2 / 3 条" in warnings[0], warnings[0]
