"""CharacterVersionRepository 服务测试（需要 MongoDB）。"""
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from core.schema.types import SessionId
from core.schema.compression import CompressionTrigger

load_dotenv()

COLLECTION = "character_versions"


@pytest.mark.service
class TestCharacterVersionRepo:
    @pytest_asyncio.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        # 清理上次运行的残留数据，确保测试隔离
        if r._db is not None:
            try:
                await r._db.delete_many(COLLECTION, {"session_id": {"$regex": "^test"}})
            except Exception:
                pass
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


@pytest.mark.service
class TestCharacterVersionRepoContentType:
    """content_type 过滤和独立计数。"""

    @pytest_asyncio.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        # 清理上次运行的残留数据，确保测试隔离
        if r._db is not None:
            try:
                await r._db.delete_many(COLLECTION, {"session_id": {"$regex": "^test"}})
            except Exception:
                pass
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_version_c6_type(self, repo):
        """c6 类型 append → version=1（独立于 L2）。"""
        v = await repo.append_version(
            session_id="test_ct", text="C6 text",
            timestamp="2026-07-22T00:00:00+08:00",
            trigger=CompressionTrigger.C6_REFRESH,
            change_summary=["调整"], content_type="c6",
        )
        assert v == 1

    @pytest.mark.asyncio
    async def test_l2_c6_independent_versions(self, repo):
        """L2 和 C6 版本号互不干扰。"""
        sid = "test_independent"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [],
                                  content_type="l2")
        v_c6 = await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                         CompressionTrigger.C6_REFRESH, [],
                                         content_type="c6")
        assert v_c6 == 1  # C6 从 1 开始
        v_l2_2 = await repo.append_version(sid, "l2_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.COMPRESSION, [],
                                            content_type="l2")
        assert v_l2_2 == 2  # L2 继续增长
        v_c6_2 = await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.C6_REFRESH, [],
                                            content_type="c6")
        assert v_c6_2 == 2  # C6 也独立增长

    @pytest.mark.asyncio
    async def test_list_versions_filtered_by_type(self, repo):
        """content_type 过滤：只返回匹配类型的版本。"""
        sid = "test_filter_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        l2_versions = await repo.list_versions(sid, content_type="l2")
        assert len(l2_versions) == 1
        assert l2_versions[0]["content_type"] == "l2"

        c6_versions = await repo.list_versions(sid, content_type="c6")
        assert len(c6_versions) == 1
        assert c6_versions[0]["content_type"] == "c6"

    @pytest.mark.asyncio
    async def test_count_versions_by_type(self, repo):
        """count_versions 按 content_type 分别计数。"""
        sid = "test_count_by_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")
        await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        assert await repo.count_versions(sid, content_type="l2") == 1
        assert await repo.count_versions(sid, content_type="c6") == 2

    @pytest.mark.asyncio
    async def test_default_content_type_is_l2(self, repo):
        """默认 content_type="l2" 向后兼容。"""
        sid = "test_default_type"
        v = await repo.append_version(sid, "text", "2026-07-22T00:00:00+08:00",
                                       CompressionTrigger.COMPRESSION, [])
        assert v == 1
        versions = await repo.list_versions(sid)
        assert len(versions) == 1
        assert versions[0].get("content_type", "l2") == "l2"
