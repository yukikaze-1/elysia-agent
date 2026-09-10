"""BE-STAGEH-05：pinned 联调夹具的幂等与清理边界（marker=service，内存假后端）。"""

import pytest

from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.schema.types import SessionId
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from scripts.dev.seed_stage_h_pinned import (
    FIXTURE_CHARACTER_ID,
    FIXTURE_FOLLOW_INSTANCE_ID,
    FIXTURE_PINNED_INSTANCE_ID,
    _assert_non_production,
    _assert_public_serialization,
    _fixture_revision,
    _require_opt_in,
    cleanup_fixture,
    seed_fixture,
)

pytestmark = pytest.mark.service


def _repos() -> tuple[
    CharacterRepository,
    CharacterRevisionRepository,
    CharacterInstanceRepository,
    SessionRepository,
]:
    return (
        CharacterRepository(db=FakeDocumentDBBackend()),
        CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        CharacterInstanceRepository(db=FakeDocumentDBBackend()),
        SessionRepository(db=FakeDocumentDBBackend()),
    )


@pytest.mark.asyncio
async def test_seed_creates_fixture_graph_and_is_idempotent():
    characters, revisions, instances, sessions = _repos()

    first = await seed_fixture(characters, revisions, instances, sessions)
    second = await seed_fixture(characters, revisions, instances, sessions)

    assert first == second
    assert first["characterId"] == FIXTURE_CHARACTER_ID
    assert [i["revisionPolicy"] for i in first["instances"]] == [
        "pinned", "follow_latest"]
    assert all(i["revision"] == 1 for i in first["instances"])

    # 幂等：重复执行不产生副本
    assert len(characters._db.collections["characters"]) == 1  # noqa: SLF001
    assert len(revisions._db.collections["character_revisions"]) == 1  # noqa: SLF001
    assert len(instances._db.collections["character_instances"]) == 2  # noqa: SLF001
    assert len(sessions._db.collections["sessions"]) == 2  # noqa: SLF001

    pinned = await instances.get(FIXTURE_PINNED_INSTANCE_ID)
    follow = await instances.get(FIXTURE_FOLLOW_INSTANCE_ID)
    assert pinned is not None and pinned.revisionPolicy == "pinned"
    assert pinned.canonical is False
    assert follow is not None and follow.revisionPolicy == "follow_latest"


@pytest.mark.asyncio
async def test_seed_output_is_sanitized():
    payload = await seed_fixture(*_repos())
    assert set(payload) == {"fixture", "characterId", "revision", "instances"}
    for entry in payload["instances"]:
        assert set(entry) == {
            "instanceId", "conversationId", "revisionPolicy", "revision"}
    assert "mongo" not in repr(payload).lower()


@pytest.mark.asyncio
async def test_cleanup_removes_only_fixture_data():
    characters, revisions, instances, sessions = _repos()
    # 无关记录（模拟真实用户数据）必须幸存
    await characters.insert_definition(CharacterDefinition(
        characterId="chr_unrelated", slug="无关角色", source="custom"))
    await instances.save(CharacterInstance(
        instanceId="chi_unrelated", characterId="chr_unrelated"))
    await sessions.create(SessionId("conv_unrelated"), title="无关会话")

    await seed_fixture(characters, revisions, instances, sessions)
    removed = await cleanup_fixture(characters, revisions, instances, sessions)

    assert removed["removed"] == {
        "sessions": 2, "instances": 2, "revisions": 1, "characters": 1}
    assert await characters.get_definition(FIXTURE_CHARACTER_ID) is None
    assert await instances.get(FIXTURE_PINNED_INSTANCE_ID) is None
    # 无关数据未被触碰
    assert await characters.get_definition("chr_unrelated") is not None
    assert await instances.get("chi_unrelated") is not None


def test_require_opt_in(monkeypatch):
    monkeypatch.delenv("STAGE_H_FIXTURES", raising=False)
    with pytest.raises(SystemExit):
        _require_opt_in()
    monkeypatch.setenv("STAGE_H_FIXTURES", "1")
    _require_opt_in()   # 不抛


def test_assert_non_production_allows_private_and_rejects_public():
    _assert_non_production(None)
    _assert_non_production("mongodb://localhost:27017")
    _assert_non_production("mongodb://127.0.0.1:27017")
    _assert_non_production("mongodb://192.168.1.18:27017")
    _assert_non_production("mongodb://10.0.0.5:27017")
    with pytest.raises(SystemExit):
        _assert_non_production("mongodb://8.8.8.8:27017")
    with pytest.raises(SystemExit):
        _assert_non_production("mongodb://db.prod.example.com:27017")


# ─────────────── BE-H2-01：fixture revision 必须通过公开契约序列化 ───────────────


def test_fixture_revision_created_by_is_contract_valid():
    """createdBy 必须落在公开 CharacterCreatedBy（system|user|migration）内。

    回归：真实联调里 fixture 写 createdBy="fixture"，整页 GET /api/characters 因
    响应模型校验失败返回 INTERNAL_ERROR。
    """
    assert _fixture_revision().createdBy == "migration"


def test_fixture_revision_passes_public_serialization_guard():
    """fixture revision 必须能走公开 _revision_wire（CharacterRevisionContent）路径。"""
    revision = _fixture_revision()
    _assert_public_serialization(
        CharacterDefinition(
            characterId=FIXTURE_CHARACTER_ID, slug="x", source="custom", mutable=True),
        revision,
    )


def test_public_serialization_guard_rejects_unknown_created_by():
    """守卫本身必须真的会拦——否则它只是装饰。"""
    revision = _fixture_revision().model_copy(update={"createdBy": "fixture"})
    with pytest.raises(SystemExit):
        _assert_public_serialization(
            CharacterDefinition(
                characterId=FIXTURE_CHARACTER_ID, slug="x", source="custom", mutable=True),
            revision,
        )


@pytest.mark.asyncio
async def test_seeded_fixture_passes_public_catalog_list():
    """seed 后的数据必须能整页通过公开 list 序列化（不再 INTERNAL_ERROR）。"""
    from core.character.service import CharacterService
    from core.profile.profile_store import UserIdentityStore

    characters, revisions, instances, sessions = _repos()
    await seed_fixture(characters, revisions, instances, sessions)

    service = CharacterService(
        repo=characters, rev_repo=revisions, catalog=None,
        user_identity_store=UserIdentityStore(),
    )
    listed = await service.list(source="custom", limit=50)
    assert [item.characterId for item in listed.items] == [FIXTURE_CHARACTER_ID]

    detail = await service.get(FIXTURE_CHARACTER_ID)
    assert detail is not None
    assert detail.current.createdBy == "migration"
