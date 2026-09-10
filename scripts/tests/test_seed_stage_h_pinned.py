"""BE-STAGEH-05：pinned 联调夹具的幂等与清理边界（marker=service，内存假后端）。"""

import pytest

from core.character.definition import CharacterDefinition
from core.character.instance import CharacterInstance
from core.schema.message import UserMessage
from core.schema.part import TextPart
from core.schema.token import TokenUsage
from core.schema.types import GenerationId, MessageId, SessionId
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.generation_repo import GenerationRepository
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.storage.token_repo import TokenRepository
from scripts.dev.seed_stage_h_pinned import (
    FIXTURE_CHARACTER_ID,
    FIXTURE_FOLLOW_CONVERSATION_ID,
    FIXTURE_FOLLOW_INSTANCE_ID,
    FIXTURE_PINNED_CONVERSATION_ID,
    FIXTURE_PINNED_INSTANCE_ID,
    FixtureRepos,
    _assert_non_production,
    _assert_public_serialization,
    _fixture_revision,
    _require_opt_in,
    cleanup_fixture,
    seed_fixture,
)

pytestmark = pytest.mark.service


def _repos() -> FixtureRepos:
    """每个 repository 一个独立内存假后端——互不串库。"""
    return FixtureRepos(
        characters=CharacterRepository(db=FakeDocumentDBBackend()),
        revisions=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        instances=CharacterInstanceRepository(db=FakeDocumentDBBackend()),
        sessions=SessionRepository(db=FakeDocumentDBBackend()),
        archive=MessageRepository(db=FakeDocumentDBBackend()),
        tokens=TokenRepository(db=FakeDocumentDBBackend()),
        generations=GenerationRepository(db=FakeDocumentDBBackend()),
    )


async def _seed_side_effects(repos: FixtureRepos, conversation_id: str) -> None:
    """模拟真实回合派生出的三类记录：归档消息 / token 统计 / generation 生命周期。"""
    sid = SessionId(conversation_id)
    await repos.archive.append(sid, UserMessage(
        content=[TextPart(content="你好")],
        message_id=MessageId(f"msg_{conversation_id}"),
        session_id=sid,
    ))
    await repos.tokens.record(TokenUsage(
        session_id=sid, model="m", input_tokens=12, output_tokens=18,
        total_tokens=30, round_num=0,
    ))
    await repos.generations.upsert({
        "generation_id": GenerationId(f"gen_{conversation_id}"),
        "session_id": conversation_id,
        "status": "completed",
    })


async def _side_effect_counts(repos: FixtureRepos, conversation_id: str) -> tuple[int, int, int]:
    sid = SessionId(conversation_id)
    _, archive_total = await repos.archive.query_by_session(sid, limit=50)
    tokens = await repos.tokens.query_by_session(conversation_id)
    gens, _, _ = await repos.generations.find_by_session(sid, limit=50)
    return archive_total, len(tokens), len(gens)


@pytest.mark.asyncio
async def test_seed_creates_fixture_graph_and_is_idempotent():
    repos = _repos()

    first = await seed_fixture(repos)
    second = await seed_fixture(repos)

    assert first == second
    assert first["characterId"] == FIXTURE_CHARACTER_ID
    assert [i["revisionPolicy"] for i in first["instances"]] == [
        "pinned", "follow_latest"]
    assert all(i["revision"] == 1 for i in first["instances"])

    # 幂等：重复执行不产生副本
    assert len(repos.characters._db.collections["characters"]) == 1  # noqa: SLF001
    assert len(repos.revisions._db.collections["character_revisions"]) == 1  # noqa: SLF001
    assert len(repos.instances._db.collections["character_instances"]) == 2  # noqa: SLF001
    assert len(repos.sessions._db.collections["sessions"]) == 2  # noqa: SLF001

    pinned = await repos.instances.get(FIXTURE_PINNED_INSTANCE_ID)
    follow = await repos.instances.get(FIXTURE_FOLLOW_INSTANCE_ID)
    assert pinned is not None and pinned.revisionPolicy == "pinned"
    assert pinned.canonical is False
    assert follow is not None and follow.revisionPolicy == "follow_latest"


@pytest.mark.asyncio
async def test_seed_output_is_sanitized():
    payload = await seed_fixture(_repos())
    assert set(payload) == {"fixture", "characterId", "revision", "instances"}
    for entry in payload["instances"]:
        assert set(entry) == {
            "instanceId", "conversationId", "revisionPolicy", "revision"}
    assert "mongo" not in repr(payload).lower()


@pytest.mark.asyncio
async def test_cleanup_removes_only_fixture_data():
    repos = _repos()
    # 无关记录（模拟真实用户数据）必须幸存
    await repos.characters.insert_definition(CharacterDefinition(
        characterId="chr_unrelated", slug="无关角色", source="custom"))
    await repos.instances.save(CharacterInstance(
        instanceId="chi_unrelated", characterId="chr_unrelated"))
    await repos.sessions.create(SessionId("conv_unrelated"), title="无关会话")
    await _seed_side_effects(repos, "conv_unrelated")

    await seed_fixture(repos)
    await _seed_side_effects(repos, FIXTURE_PINNED_CONVERSATION_ID)
    await _seed_side_effects(repos, FIXTURE_FOLLOW_CONVERSATION_ID)
    removed = await cleanup_fixture(repos)

    assert removed["removed"] == {
        "sessions": 2, "instances": 2, "revisions": 1, "characters": 1,
        "archive": 2, "tokens": 2, "generations": 2}
    assert await repos.characters.get_definition(FIXTURE_CHARACTER_ID) is None
    assert await repos.instances.get(FIXTURE_PINNED_INSTANCE_ID) is None
    # 无关数据未被触碰
    assert await repos.characters.get_definition("chr_unrelated") is not None
    assert await repos.instances.get("chi_unrelated") is not None
    assert await _side_effect_counts(repos, "conv_unrelated") == (1, 1, 1)


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

    repos = _repos()
    await seed_fixture(repos)

    service = CharacterService(
        repo=repos.characters, rev_repo=repos.revisions, catalog=None,
        user_identity_store=UserIdentityStore(),
    )
    listed = await service.list(source="custom", limit=50)
    assert [item.characterId for item in listed.items] == [FIXTURE_CHARACTER_ID]

    detail = await service.get(FIXTURE_CHARACTER_ID)
    assert detail is not None
    assert detail.current.createdBy == "migration"


# ─────────────── BE-H2-02：cleanup 必须删净该 fixture 的全部 revision ───────────────


async def _append_revision(repos: FixtureRepos, number: int) -> None:
    revision = _fixture_revision().model_copy(
        update={"revision": number, "displayName": f"夹具 r{number}"})
    await repos.revisions.append_revision(revision)


@pytest.mark.asyncio
async def test_cleanup_removes_every_revision_of_the_fixture():
    """H-44 会把 Definition 推到 revision 2；只删 revision 1 会留孤儿。"""
    repos = _repos()
    await seed_fixture(repos)
    await _append_revision(repos, 2)
    await _append_revision(repos, 3)

    removed = await cleanup_fixture(repos)

    assert removed["removed"]["revisions"] == 3
    assert await repos.revisions.list_revisions(FIXTURE_CHARACTER_ID) == []


@pytest.mark.asyncio
async def test_cleanup_second_run_reports_zeros():
    """幂等：第二次执行成功且全零。"""
    repos = _repos()
    await seed_fixture(repos)
    await _append_revision(repos, 2)
    await _seed_side_effects(repos, FIXTURE_PINNED_CONVERSATION_ID)

    await cleanup_fixture(repos)
    second = await cleanup_fixture(repos)

    assert second["removed"] == {
        "sessions": 0, "instances": 0, "revisions": 0, "characters": 0,
        "archive": 0, "tokens": 0, "generations": 0}


@pytest.mark.asyncio
async def test_cleanup_leaves_other_character_revisions_intact():
    """只删固定 characterId 的 revision——绝不扫描/误删其他角色。"""
    repos = _repos()
    other = _fixture_revision().model_copy(
        update={"characterId": "chr_unrelated_rev"})
    await repos.revisions.append_revision(other)
    await seed_fixture(repos)
    await _append_revision(repos, 2)

    await cleanup_fixture(repos)

    survivors = await repos.revisions.list_revisions("chr_unrelated_rev")
    assert [rev.characterId for rev in survivors] == ["chr_unrelated_rev"]


@pytest.mark.asyncio
async def test_cleanup_raises_when_revision_storage_fails():
    """存储失败必须冒出非零退出——不得打印看似成功的汇总。"""
    from core.storage.base import StorageUnavailableError

    repos = _repos()
    await seed_fixture(repos)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise StorageUnavailableError("模拟 Mongo 抖动")

    repos.revisions.delete_revision = _boom  # type: ignore[method-assign]
    with pytest.raises(StorageUnavailableError):
        await cleanup_fixture(repos)


@pytest.mark.asyncio
async def test_cleanup_raises_when_residue_survives():
    """删完回读复查：仍有残留 → 非零退出，不谎报干净。"""
    repos = _repos()
    await seed_fixture(repos)

    async def _noop(*args: object, **kwargs: object) -> None:
        return None

    repos.revisions.delete_revision = _noop  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        await cleanup_fixture(repos)


# ─────────────── 真实回合的三类副作用也必须纳入正式 cleanup ───────────────


@pytest.mark.asyncio
async def test_cleanup_removes_side_effects_of_both_fixture_conversations():
    """message_archive / token_usage / generations 都必须删净。

    回归：清理只覆盖 session/instance/revision/definition 时，真实回合派生出的三类
    记录会留下指向已删会话的孤儿行（generations 还有 30 天 TTL）。
    """
    repos = _repos()
    await seed_fixture(repos)
    for conversation_id in (
        FIXTURE_PINNED_CONVERSATION_ID, FIXTURE_FOLLOW_CONVERSATION_ID):
        await _seed_side_effects(repos, conversation_id)

    removed = await cleanup_fixture(repos)

    assert removed["removed"]["archive"] == 2
    assert removed["removed"]["tokens"] == 2
    assert removed["removed"]["generations"] == 2
    for conversation_id in (
        FIXTURE_PINNED_CONVERSATION_ID, FIXTURE_FOLLOW_CONVERSATION_ID):
        assert await _side_effect_counts(repos, conversation_id) == (0, 0, 0)


@pytest.mark.asyncio
async def test_cleanup_raises_when_generation_residue_survives():
    """generations 删不掉时必须非零退出——这一层最容易漏（字段名是 session_id）。"""
    repos = _repos()
    await seed_fixture(repos)
    await _seed_side_effects(repos, FIXTURE_PINNED_CONVERSATION_ID)

    async def _noop(*args: object, **kwargs: object) -> int:
        return 0

    repos.generations.delete_by_session = _noop  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        await cleanup_fixture(repos)
