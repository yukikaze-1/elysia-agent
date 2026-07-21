"""CharacterVersionRepository 服务测试（需要 MongoDB）。"""
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger

load_dotenv()


@pytest.mark.service
class TestCharacterVersionRepo:
    @pytest_asyncio.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_and_get(self, repo):
        sid = SessionId("test-append")
        v = await repo.append_version(
            session_id=sid, text="我是一个测试",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=["新增: 测试条目"],
        )
        assert v == 1
        entry = await repo.get_version(sid, 1)
        assert entry is not None
        assert entry["text"] == "我是一个测试"
        assert entry["trigger"] == "compression"
        assert entry["change_summary"] == ["新增: 测试条目"]

    @pytest.mark.asyncio
    async def test_append_auto_increment(self, repo):
        sid = SessionId("test-increment")
        v1 = await repo.append_version(
            session_id=sid, text="v1",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=[],
        )
        v2 = await repo.append_version(
            session_id=sid, text="v2",
            timestamp="2026-07-21T01:00:00.000Z",
            trigger=CompressionTrigger.FLUSH,
            change_summary=["新增: v2"],
        )
        assert v1 == 1
        assert v2 == 2

    @pytest.mark.asyncio
    async def test_list_versions_pagination(self, repo):
        sid = SessionId("test-list")
        for i in range(5):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[f"改动{i}"],
            )
        # 默认返回最近 20 条
        versions = await repo.list_versions(sid)
        assert len(versions) == 5
        assert versions[0]["version"] == 5  # 最新在前
        assert versions[4]["version"] == 1

        # before_version 分页
        page2 = await repo.list_versions(sid, before_version=3)
        assert len(page2) == 2
        assert page2[0]["version"] == 2
        assert page2[1]["version"] == 1

    @pytest.mark.asyncio
    async def test_count(self, repo):
        sid = SessionId("test-count")
        for i in range(3):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[],
            )
        assert await repo.count_versions(sid) == 3

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        result = await repo.get_version(SessionId("no-such"), 999)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_list(self, repo):
        versions = await repo.list_versions(SessionId("no-such"))
        assert versions == []
