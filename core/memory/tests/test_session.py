"""SessionMemory 测试 — connect_db / health_check / save / load。"""
import asyncio
import logging
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.memory.session import SessionMemory
from core.schema.types import MessageId, SessionId


@pytest.mark.unit
def test_collection_constant_in_repo():
    """COLLECTION 常量已迁移到 SessionRepository。"""
    from core.storage.session_repo import COLLECTION as REPO_COLLECTION
    assert REPO_COLLECTION == "sessions"


@pytest.mark.unit
def test_archive_done_logs_warning_on_failure(caplog):
    """归档 fire-and-forget 失败不能只记 debug——必须 warning 可见（S1）。"""
    from core.memory.session import _archive_done

    class _FailingTask:
        def result(self):
            raise RuntimeError("mongo write failed")

    with caplog.at_level(logging.WARNING, logger="core.memory.session"):
        _archive_done(_FailingTask())
    assert any("消息归档失败" in r.message for r in caplog.records)


@pytest.mark.service
class TestSessionMemoryConnect:
    """connect_db() 创建 SessionRepository 并委托连接。"""

    @pytest.mark.asyncio
    async def test_connect_db_creates_repo_and_connects(self):
        sm = SessionMemory(session_id=SessionId("test-connect"))
        mock_repo = MagicMock()
        mock_repo.connect = AsyncMock(return_value=True)
        with patch(
            "core.memory.session.SessionRepository",
            return_value=mock_repo,
        ):
            result = await sm.connect_db()
        assert result is True
        assert sm._repo is mock_repo
        mock_repo.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_db_with_explicit_repo(self):
        """传入 repo 参数时直接使用，不创建新的。"""
        mock_repo = MagicMock()
        mock_repo.connect = AsyncMock(return_value=True)
        sm = SessionMemory(session_id=SessionId("test-explicit"), repo=mock_repo)
        result = await sm.connect_db()
        assert result is True
        assert sm._repo is mock_repo
        mock_repo.connect.assert_called_once()


@pytest.mark.service
class TestSessionMemoryConnectLogLevel:
    """connect_db() 失败时必须使用 logger.warning，不是 info。"""

    @pytest.mark.asyncio
    async def test_connect_failure_uses_warning(self, caplog):
        sm = SessionMemory(session_id=SessionId("test-log"))
        with patch(
            "core.storage.session_repo.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await sm.connect_db()

        assert result is False
        assert sm._repo is not None
        # 必须有 WARNING 级别的日志
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "MongoDB unavailable" in warnings[0].message


@pytest.mark.service
class TestSessionMemoryHealthCheck:
    """health_check() 委托给 SessionRepository。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_repo_connected(self):
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=True)
        sm = SessionMemory(session_id=SessionId("test-hc"), repo=mock_repo)
        result = await sm.health_check()
        assert result is True
        mock_repo.health_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_repo_disconnected(self):
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=False)
        sm = SessionMemory(session_id=SessionId("test-hc"), repo=mock_repo)
        result = await sm.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_repo(self):
        sm = SessionMemory(session_id=SessionId("test-hc"))
        result = await sm.health_check()
        assert result is False


@pytest.mark.service
class TestSessionMemorySaveLoad:
    """save() / load() 委托给 SessionRepository，保留 JSONL 降级。"""

    @pytest.mark.asyncio
    async def test_save_delegates_to_repo(self):
        from core.schema.message import UserMessage
        from core.schema.part import TextPart

        mock_repo = MagicMock()
        mock_repo._db = object()  # 模拟已连接
        mock_repo.save = AsyncMock()
        sm = SessionMemory(session_id=SessionId("test-save"), repo=mock_repo)
        sm.add_message(UserMessage(message_id=MessageId("m1"),
                                   content=[TextPart(content="hi")]))
        result = await sm.save()
        assert result is True
        mock_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_falls_back_to_jsonl_when_no_repo(self, tmp_path):
        pending = os.path.join(str(tmp_path), "session_sid.json")
        sm = SessionMemory(session_id=SessionId("sid"))
        sm._pending_file = pending
        result = await sm.save()
        assert result is True
        assert os.path.exists(pending)

    @pytest.mark.asyncio
    async def test_load_delegates_to_repo(self):
        from core.schema.message import UserMessage
        from core.schema.part import TextPart
        from core.schema.character import CharacterState
        from core.schema.memory_reflector import MemoryReflectorState
        from core.schema.service_state import ServiceState
        msg = UserMessage(
            message_id=MessageId("m1"),
            content=[TextPart(content="hello")],
        )

        cs = CharacterState(session_id=SessionId("test-load"), mood="happy")
        mock_repo = MagicMock()
        mock_repo._db = object()  # 模拟已连接
        mock_repo.load = AsyncMock(return_value=([msg], cs, MemoryReflectorState(), ServiceState()))
        sm = SessionMemory(session_id=SessionId("test-load"), repo=mock_repo)
        result = await sm.load(SessionId("test-load"))
        assert result is True
        assert len(sm._messages) == 1
        assert sm._messages[0].message_id == "m1"
        assert sm.character_state.mood == "happy"

    @pytest.mark.asyncio
    async def test_load_returns_false_when_not_found(self):
        mock_repo = MagicMock()
        mock_repo._db = object()
        mock_repo.load = AsyncMock(return_value=None)
        sm = SessionMemory(session_id=SessionId("test-miss"), repo=mock_repo)
        result = await sm.load(SessionId("test-miss"))
        assert result is False

    @pytest.mark.asyncio
    async def test_load_falls_back_to_jsonl_when_no_repo(self, tmp_path):
        pending = os.path.join(str(tmp_path), "session_sid.json")
        # 写入一个最小 JSON 文档
        with open(pending, "w") as f:
            json.dump({"messages": [], "params": {}}, f)
        sm = SessionMemory(session_id=SessionId("sid"))
        sm._pending_file = pending
        result = await sm.load()
        assert result is True
        # 清理
        os.remove(pending)


class TestSessionDurablePersistence:
    """B2/P1-01：离线不假成功、Mongo durable 后删 pending、空未加载不覆盖。"""

    @pytest.mark.asyncio
    async def test_save_falls_back_to_jsonl_when_repo_offline(self, tmp_path):
        """repo 存在但未连接（_db=None）→ save() 不能假成功，必须落 JSONL（P1-01 复现）。

        修复前 repo.save 静默 return → save() 返回 True 但 Mongo 无记录也无 JSONL。
        """
        from core.schema.message import UserMessage
        from core.schema.part import TextPart
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=None)  # 未连接——repo.save 抛 StorageUnavailableError
        pending = os.path.join(str(tmp_path), "session_offline.json")
        sm = SessionMemory(session_id=SessionId("offline"), repo=repo)
        sm._pending_file = pending
        sm.add_message(UserMessage(message_id=MessageId("m1"),
                                   content=[TextPart(content="hi")]))

        result = await sm.save()

        assert result is True
        assert os.path.exists(pending)  # JSONL fallback 落盘（修复前不存在 → 数据双失）

    @pytest.mark.asyncio
    async def test_save_deletes_pending_after_mongo_success(self, tmp_path):
        """Mongo durable 成功后删除 pending JSONL（只有 durable success 才删）。"""
        from core.schema.message import UserMessage
        from core.schema.part import TextPart

        mock_repo = MagicMock()
        mock_repo._db = object()
        mock_repo.save = AsyncMock()
        pending = os.path.join(str(tmp_path), "session_p.json")
        sm = SessionMemory(session_id=SessionId("sid"), repo=mock_repo)
        sm._pending_file = pending
        sm.add_message(UserMessage(message_id=MessageId("m1"),
                                   content=[TextPart(content="hi")]))
        with open(pending, "w") as f:
            f.write("{}")

        result = await sm.save()

        assert result is True
        assert not os.path.exists(pending)  # Mongo durable → pending 已删

    @pytest.mark.asyncio
    async def test_load_sets_storage_unavailable_flag(self):
        """repo 未连接 → load 标记 _storage_unavailable（ensure_loaded 据此不标 loaded）。"""
        from core.storage.session_repo import SessionRepository

        repo = SessionRepository(db=None)
        sm = SessionMemory(session_id=SessionId("sid"), repo=repo)
        result = await sm.load()
        assert result is False
        assert sm._storage_unavailable is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_load_new_session_not_storage_unavailable(self):
        """存储可达但 not found → 不标记 _storage_unavailable（合法新会话）。"""
        mock_repo = MagicMock()
        mock_repo._db = object()
        mock_repo.load = AsyncMock(return_value=None)
        sm = SessionMemory(session_id=SessionId("sid"), repo=mock_repo)
        result = await sm.load()
        assert result is False
        assert sm._storage_unavailable is False  # noqa: SLF001


class TestArchiveOwnership:
    """B3/P1-02：归档任务按会话登记、可 drain、关闭后不再新增。"""

    @staticmethod
    def _msg(mid: str):
        from core.schema.message import UserMessage
        from core.schema.part import TextPart
        return UserMessage(message_id=MessageId(mid), content=[TextPart(content="hi")])

    @pytest.mark.asyncio
    async def test_add_message_tracks_and_cleans_archive_task(self):
        """add_message 创建的归档 task 登记入 _archive_tasks，完成后移除。"""
        msg_repo = MagicMock()
        msg_repo.append = AsyncMock()
        sm = SessionMemory(session_id=SessionId("s1"), msg_repo=msg_repo)
        sm.add_message(self._msg("m1"))
        assert len(sm._archive_tasks) == 1  # noqa: SLF001 — 在途
        await asyncio.sleep(0.05)  # 让 task 完成并触发 done callback
        assert len(sm._archive_tasks) == 0  # noqa: SLF001 — 完成移除
        msg_repo.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_archive_drains_and_blocks_new(self):
        """close_archive 等待在途任务；关闭后 add_message 不再创建归档任务。"""
        msg_repo = MagicMock()
        msg_repo.append = AsyncMock()
        sm = SessionMemory(session_id=SessionId("s1"), msg_repo=msg_repo)
        sm.add_message(self._msg("m1"))
        await sm.close_archive(timeout=1.0)

        sm.add_message(self._msg("m2"))
        await asyncio.sleep(0.05)

        assert len(sm._archive_tasks) == 0  # noqa: SLF001
        assert msg_repo.append.call_count == 1  # 关闭后第二条未归档


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_state_roundtrip():
    """ServiceState 经 save/load 4 元组回显，重启后仍可恢复；默认实例三态为 None。"""
    repo = MagicMock()
    doc: dict = {}
    async def fake_save(sid, msgs, *, character_state=None, reflector_state=None, service_state=None, **kw):
        doc.update(messages=list(msgs), character_state=character_state,
                   reflector_state=reflector_state, service_state=service_state)
    async def fake_load(sid):
        return (doc.get("messages", []), doc.get("character_state"),
                doc.get("reflector_state"), doc.get("service_state"))   # 回显 4 元组
    repo.save = fake_save
    repo.load = fake_load

    s = SessionMemory(session_id="t", repo=repo)
    s.service_state.tts_enabled = False
    s.service_state.llm_model = "deepseek/deepseek-v4-pro"
    # 批次 5（Task 11）：tts_voice 声音覆盖经泛化字段遍历自动 round-trip
    s.service_state.tts_voice = "vA"
    await s.save()
    loaded = SessionMemory(session_id="t", repo=repo)
    await loaded.load("t")
    assert loaded.service_state.tts_enabled is False
    assert loaded.service_state.llm_model == "deepseek/deepseek-v4-pro"
    assert loaded.service_state.tts_voice == "vA"
    fresh = SessionMemory(session_id="fresh")
    assert fresh.service_state.stt_enabled is None   # 默认实例三态 None，非 True
    assert fresh.service_state.tts_voice is None      # 默认实例无声音覆盖
