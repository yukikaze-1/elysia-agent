"""SessionRepository 状态持久化 save → load 往返测试。

CLAUDE.md 强制约定：会话运行时状态用 SessionMemory 的**专用类型字段**
（`character_state` / `reflector_state` / `service_state`），经 `session.save()`
同步、`load()` 恢复。既有 `test_session_repo.py` 对 `save` 只断言
`update_one` 被调用一次、对 `load()` 零正向断言——把生产端字段写成 `{}`
也无人察觉。本文件用 `FakeDocumentDBBackend` 做真实往返，断言**非默认值**
原样回读（不是只断言 not None）。
"""

import asyncio

import pytest

from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import UserMessage
from core.schema.part import TextPart
from core.schema.service_state import ServiceState
from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


def _user_message(content: str = "这是一条用于往返测试的用户消息", msg_id: str = "msg-rt") -> UserMessage:
    """构造最小 UserMessage（用于验证 messages 字段也参与往返）。"""
    return UserMessage(
        content=[TextPart(content=content)],
        message_id=msg_id,
        session_id=SessionId("sess-rt"),
    )


def _character_state() -> CharacterState:
    """带可观察非默认值的 CharacterState（每个字段都刻意偏离默认）。"""
    return CharacterState(
        session_id=SessionId("sess-rt"),
        self_l2="我是长期演化的自我",
        state_l3="今天被夸了很开心",
        state_history=[{"timestamp": "2026-09-01T00:00:00+00:00", "content": "旧状态"}],
        last_awareness_time=111.5,
        approved_awareness_text="被批准的觉察文本",
        approved_awareness_at=112.5,
        awareness_show_count=3,
        compression_last_at=113.5,
        compression_last_count=4,
        mood="雀跃",
        mood_intensity=0.87,
        speaking_style_c6="说话活泼一点",
        reflector_buffer=[("用户说了什么", "助手回答了")],
        reflector_context=[{"note": "反射上下文"}],
        self_perception_history=[{"entry": "自我认知条目"}],
        updated_at="2026-09-01T00:00:00+00:00",
    )


def _reflector_state() -> MemoryReflectorState:
    """带非默认计数的 MemoryReflectorState。"""
    return MemoryReflectorState(
        mr_round_count=7,
        mr_new_memory_count=2,
        mr_last_summary={"summary": "上一轮总结"},
    )


def _service_state() -> ServiceState:
    """带非默认值的 ServiceState（含三态 stt_enabled=False 这一非默认末态）。"""
    return ServiceState(
        llm_model="deepseek/deepseek-v4-pro",
        stt_model="sensevoice/sense-voice",
        tts_model="minimax/speech-2.8-hd",
        stt_enabled=False,
        tts_enabled=True,
        tts_voice="voice-yomu",
    )


def test_character_state_roundtrips_non_default_values():
    """save 写入的非默认 CharacterState 必须原样回读（逐字段级，不是 not None）。"""
    fake = FakeDocumentDBBackend()
    repo = SessionRepository(db=fake)
    original = _character_state()

    asyncio.run(repo.save(SessionId("sess-rt"), [_user_message()], original))
    loaded = asyncio.run(repo.load(SessionId("sess-rt")))
    assert loaded is not None
    _, got, _, _, _ = loaded

    # 全字段等价 + 若干具名断言（让失败信息可读）
    assert got.model_dump() == original.model_dump()
    assert got.mood == "雀跃"
    assert got.mood_intensity == 0.87
    assert got.self_l2 == "我是长期演化的自我"
    assert got.state_l3 == "今天被夸了很开心"
    assert got.awareness_show_count == 3
    assert got.reflector_buffer == [("用户说了什么", "助手回答了")]


def test_reflector_state_roundtrips_non_default_values():
    """save 写入的非默认 MemoryReflectorState 必须原样回读。"""
    fake = FakeDocumentDBBackend()
    repo = SessionRepository(db=fake)
    original = _reflector_state()

    asyncio.run(repo.save(SessionId("sess-rt"), [], _character_state(), original))
    loaded = asyncio.run(repo.load(SessionId("sess-rt")))
    assert loaded is not None
    _, _, got, _, _ = loaded

    assert got.model_dump() == original.model_dump()
    assert got.mr_round_count == 7
    assert got.mr_new_memory_count == 2
    assert got.mr_last_summary == {"summary": "上一轮总结"}


def test_service_state_roundtrips_non_default_values():
    """save 写入的非默认 ServiceState 必须原样回读（含 stt_enabled=False 三态）。"""
    fake = FakeDocumentDBBackend()
    repo = SessionRepository(db=fake)
    original = _service_state()

    asyncio.run(repo.save(SessionId("sess-rt"), [], _character_state(), None, original))
    loaded = asyncio.run(repo.load(SessionId("sess-rt")))
    assert loaded is not None
    _, _, _, got, _ = loaded

    assert got.model_dump() == original.model_dump()
    assert got.stt_enabled is False
    assert got.tts_enabled is True
    assert got.tts_voice == "voice-yomu"
    assert got.llm_model == "deepseek/deepseek-v4-pro"


def test_all_state_fields_roundtrip_together():
    """三个专用字段同时写入 → 同一次 load 全部原样回读（两侧同步约定整体验证）。"""
    fake = FakeDocumentDBBackend()
    repo = SessionRepository(db=fake)
    character_state = _character_state()
    reflector_state = _reflector_state()
    service_state = _service_state()

    asyncio.run(repo.save(
        SessionId("sess-rt"),
        [_user_message()],
        character_state,
        reflector_state,
        service_state,
    ))
    loaded = asyncio.run(repo.load(SessionId("sess-rt")))
    assert loaded is not None
    messages, cs, rs, ss, _ = loaded

    assert cs.model_dump() == character_state.model_dump()
    assert rs.model_dump() == reflector_state.model_dump()
    assert ss.model_dump() == service_state.model_dump()
    # messages 也必须往返（同一次 save 的一致性）
    assert len(messages) == 1
    assert messages[0].message_id == "msg-rt"


def test_load_defaults_when_state_never_persisted():
    """未写入状态字段时 load 返回默认模型（不是 None / 不崩）——反向基线。"""
    fake = FakeDocumentDBBackend()
    repo = SessionRepository(db=fake)
    asyncio.run(repo.create(SessionId("sess-empty")))

    loaded = asyncio.run(repo.load(SessionId("sess-empty")))
    assert loaded is not None
    _, cs, rs, ss, _ = loaded

    assert cs.mood == "平静"
    assert cs.mood_intensity == 0.5
    assert rs.mr_round_count == 0
    assert ss.stt_enabled is None
