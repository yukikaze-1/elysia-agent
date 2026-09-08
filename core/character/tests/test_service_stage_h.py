"""Stage H 角色 REST 领域语义回归。

这些测试直接走 CharacterService，覆盖 wire 之下的分页、修订 CAS、状态策略以及
reset/preview 的安全边界；路由层只负责解析和错误信封，不在这里伪造数据库或推理器。
"""

import pytest

from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft
from core.character.instance import CharacterInstance
from core.character.revision import CharacterRevision
from core.character.registry import CharacterRuntimeRegistry
from core.character.runtime import CharacterRuntime
from core.character.service import (
    CharacterBusyError,
    CharacterImmutableError,
    CharacterPreviewUnavailableError,
    CharacterQueryError,
    CharacterService,
    CharacterStateConflictError,
    RESET_FIELDS,
    RevisionConflictError,
)
from core.profile.profile_store import UserIdentityStore
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId
from core.storage.character_repo import CharacterRepository
from core.storage.character_instance_repo import CharacterInstanceRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


_BODY = "你是一个用于分页和 CAS 回归的测试角色。"


def _draft(**overrides: object) -> CharacterDraft:
    values: dict[str, object] = {
        "displayName": "Stage H 角色",
        "description": "用于 Stage H 测试。",
        "aliases": [],
        "promptProfile": {
            "corePersonality": _BODY,
            "relationshipStyle": "保持清晰、尊重的关系边界。",
            "emotionStyle": "按上下文自然表达。",
            "speakingStyle": "用简洁自然的方式交流。",
            "conversationInitiative": "在适当时主动推进对话。",
            "examples": [],
        },
        "runtimeDefaults": {
            "voice": {"providerId": None, "voiceId": None},
            "emotion": {"idleExpressions": []},
            "wakeWords": [],
            "proactive": {"enabled": False, "intervalSeconds": 1800, "triggers": []},
        },
        "presentation": {
            "avatarAssetId": None,
            "live2dModelId": None,
            "themeColor": None,
        },
        "openingMessage": None,
    }
    values.update(overrides)
    return CharacterDraft(**values)


def _make_service(
    *,
    instance_repo: CharacterInstanceRepository | None = None,
    runtime_registry: CharacterRuntimeRegistry | None = None,
) -> CharacterService:
    return CharacterService(
        repo=CharacterRepository(db=FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=UserIdentityStore(),
        instance_repo=instance_repo,
        runtime_registry=runtime_registry,
    )


@pytest.mark.asyncio
async def test_list_cursor_search_and_limit_are_deterministic() -> None:
    service = _make_service()
    await service.repo.connect()
    await service.rev_repo.connect()
    created = []
    for index in range(3):
        created.append(
            await service.create(
                _draft(
                    displayName=f"分页角色 {index}",
                    aliases=[f"stage-h-alias-{index}"],
                    description=f"第 {index} 个分页角色。",
                ),
                owner_user_id="usr_local_default",
            )
        )

    first = await service.list(source="custom", limit=2)
    assert len(first.items) == 2
    assert first.has_more is True
    assert first.cursor

    second = await service.list(source="custom", cursor=first.cursor, limit=2)
    assert len(second.items) == 1
    assert second.has_more is False
    assert second.cursor is None
    assert {item.characterId for item in first.items}.isdisjoint(
        item.characterId for item in second.items
    )
    assert {item.characterId for item in first.items + second.items} == {
        item.summary.characterId for item in created
    }

    found = await service.list(source="custom", search="ALIAS-1", limit=100)
    assert [item.characterId for item in found.items] == [created[1].summary.characterId]

    with pytest.raises(CharacterQueryError):
        await service.list(source="custom", limit=0)
    with pytest.raises(CharacterQueryError):
        await service.list(source="custom", cursor="not-a-cursor")


@pytest.mark.asyncio
async def test_revision_page_and_mutation_cas_include_state_policy() -> None:
    service = _make_service()
    await service.repo.connect()
    await service.rev_repo.connect()
    detail = await service.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId

    mutation = await service.update(
        character_id,
        _draft(displayName="Stage H 角色 v2"),
        expected_revision=1,
        state_policy="reset_evolution",
    )
    assert mutation.character.summary.currentRevision == 2
    assert mutation.stateImpact.statePolicy.value == "reset_evolution"
    assert {field.value for field in mutation.stateImpact.resetFields} == {
        field.value for field in RESET_FIELDS
    }

    first = await service.list_revisions(character_id, limit=1)
    assert [item.revision for item in first.items] == [2]
    assert first.has_more is True
    assert first.cursor
    second = await service.list_revisions(character_id, cursor=first.cursor, limit=1)
    assert [item.revision for item in second.items] == [1]
    assert second.has_more is False

    with pytest.raises(RevisionConflictError):
        await service.update(
            character_id,
            _draft(displayName="过期写入"),
            expected_revision=1,
            state_policy="preserve",
        )


@pytest.mark.parametrize(
    ("state_policy", "expected_self_l2"),
    [("preserve", "持续演化的自我"), ("reset_evolution", "")],
)
@pytest.mark.asyncio
async def test_revision_propagates_to_follow_latest_instances_only(
    state_policy: str, expected_self_l2: str,
) -> None:
    instance_repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    registry = CharacterRuntimeRegistry()
    service = _make_service(
        instance_repo=instance_repo, runtime_registry=registry,
    )
    detail = await service.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId
    state = CharacterRuntimeState(
        instance_id="chi_follow",
        session_id=SessionId("session_follow"),
        source_revision=1,
        self_l2="持续演化的自我",
        state_l3="近期状态",
        mood="开心",
        mood_intensity=0.8,
    )
    follow = CharacterInstance(
        instanceId="chi_follow",
        characterId=character_id,
        revision=1,
        revisionPolicy="follow_latest",
        stateVersion=4,
        state=state,
    )
    pinned = CharacterInstance(
        instanceId="chi_pinned",
        characterId=character_id,
        revision=1,
        revisionPolicy="pinned",
        stateVersion=2,
        state=state.model_copy(
            deep=True,
            update={"instance_id": "chi_pinned", "session_id": SessionId("session_pinned")},
        ),
    )
    await instance_repo.save(follow)
    await instance_repo.save(pinned)
    runtime = CharacterRuntime(
        instance=follow.model_copy(deep=True),
        definition=CharacterDefinition(
            characterId=character_id, source="custom", mutable=True,
        ),
        revision=CharacterRevision(
            characterId=character_id, revision=1, displayName="Stage H 角色",
        ),
        user_identity=UserIdentity(
            userId="usr_local_default", displayName="测试用户",
        ),
    )
    registry.get("chi_follow", lambda: runtime)

    result = await service.update(
        character_id,
        _draft(displayName="Stage H 角色 v2"),
        expected_revision=1,
        state_policy=state_policy,
    )

    assert result.stateImpact.affectedInstances == 1
    assert result.stateImpact.pendingInstances == 0
    updated = await instance_repo.get("chi_follow")
    assert updated is not None and updated.state is not None
    assert updated.revision == 2
    assert updated.stateVersion == 5
    assert updated.state.source_revision == 2
    assert updated.state.self_l2 == expected_self_l2
    if state_policy == "reset_evolution":
        assert updated.state.state_l3 == ""
        assert updated.state.mood == "平静"
    else:
        assert updated.state.state_l3 == "近期状态"
        assert updated.state.mood == "开心"
    untouched = await instance_repo.get("chi_pinned")
    assert untouched is not None and untouched.revision == 1
    assert untouched.stateVersion == 2
    assert registry.get_existing("chi_follow") is None


@pytest.mark.asyncio
async def test_reconcile_recovers_unmarked_published_reset_policy() -> None:
    """Definition 已发布但 pending marker 未写入时，runtime load 仍可恢复策略。"""
    instance_repo = CharacterInstanceRepository(db=FakeDocumentDBBackend())
    service = _make_service(instance_repo=instance_repo)
    detail = await service.create(_draft(), owner_user_id="usr_local_default")
    character_id = detail.summary.characterId
    await instance_repo.save(CharacterInstance(
        instanceId="chi_interrupted",
        characterId=character_id,
        revision=1,
        revisionPolicy="follow_latest",
        stateVersion=3,
        state=CharacterRuntimeState(
            instance_id="chi_interrupted",
            session_id=SessionId("session_interrupted"),
            source_revision=1,
            self_l2="必须被清空",
            state_l3="也必须被清空",
        ),
    ))
    revision = service._revision_from_draft(
        character_id,
        _draft(displayName="中断后的 v2"),
        revision=2,
        state_policy="reset_evolution",
    )
    await service.rev_repo.append_revision(revision)
    assert await service.repo.cas_revision_pointer(
        character_id, 1, 2, updated_at="2026-09-08T00:00:00+00:00"
    )
    stale = await instance_repo.get("chi_interrupted")
    assert stale is not None and stale.pendingRevision is None

    recovered = await service.reconcile_instance_revision(stale)

    assert recovered.revision == 2
    assert recovered.stateVersion == 4
    assert recovered.state is not None
    assert recovered.state.source_revision == 2
    assert recovered.state.self_l2 == ""
    assert recovered.state.state_l3 == ""


@pytest.mark.asyncio
async def test_preview_fails_closed_without_stateless_adapter() -> None:
    service = _make_service()
    with pytest.raises(CharacterPreviewUnavailableError):
        await service.preview(_draft(), user_message="请介绍一下自己。")


@pytest.mark.asyncio
async def test_builtin_reads_and_immutable_writes_do_not_require_storage() -> None:
    class _UnavailableRepository:
        async def connect(self) -> bool:
            return False

    service = CharacterService(
        repo=_UnavailableRepository(),  # type: ignore[arg-type]
        rev_repo=_UnavailableRepository(),  # type: ignore[arg-type]
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=UserIdentityStore(),
    )
    detail = await service.get("chr_builtin_elysia")
    assert detail is not None
    listed = await service.list(source="builtin", limit=100)
    assert any(item.characterId == "chr_builtin_elysia" for item in listed.items)
    revisions = await service.list_revisions("chr_builtin_elysia", limit=1)
    assert revisions.items and revisions.items[0].revision == 1
    with pytest.raises(CharacterImmutableError):
        await service.update(
            "chr_builtin_elysia",
            _draft(displayName="不应写入"),
            expected_revision=1,
            state_policy="preserve",
        )


def _runtime_for_reset() -> CharacterRuntime:
    state = CharacterRuntimeState(
        instance_id="chi_stage_h",
        session_id=SessionId("session_stage_h"),
        source_revision=1,
        self_l2="长期自我",
        state_l3="近期状态",
        state_history=[{"timestamp": "2026-09-07T00:00:00Z", "content": "旧状态"}],
        last_awareness_time=123.0,
        approved_awareness_text="旧觉察",
        approved_awareness_at=124.0,
        awareness_show_count=2,
        compression_last_at=125.0,
        compression_last_count=3,
        mood="兴奋",
        mood_intensity=0.9,
        speaking_style_c6="旧风格",
        reflector_buffer=[("u", "a")],
        reflector_context=[{"text": "旧上下文"}],
        self_perception_history=[{"text": "旧自我"}],
        mr_round_count=4,
        mr_new_memory_count=5,
        mr_last_summary={"summary": "旧总结"},
    )
    instance = CharacterInstance(
        instanceId="chi_stage_h",
        characterId="chr_stage_h",
        stateVersion=7,
        state=state,
    )
    return CharacterRuntime(
        instance=instance,
        definition=CharacterDefinition(characterId="chr_stage_h"),
        revision=CharacterRevision(characterId="chr_stage_h", displayName="Stage H"),
        user_identity=UserIdentity(userId="usr_local_default", displayName="测试用户"),
    )


@pytest.mark.asyncio
async def test_reset_evolution_uses_state_cas_and_clears_only_evolution() -> None:
    service = _make_service()
    runtime = _runtime_for_reset()
    persisted: list[tuple[CharacterRuntimeState, int]] = []

    async def persist(state: CharacterRuntimeState, version: int) -> bool:
        persisted.append((state, version))
        return True

    reset = await service.reset_evolution(
        runtime.instance.instanceId,
        expected_state_version=7,
        runtime=runtime,
        persist=persist,
    )
    assert reset.self_l2 == ""
    assert reset.state_l3 == ""
    assert reset.speaking_style_c6 == ""
    assert reset.reflector_buffer == []
    assert reset.mood == "平静"
    assert reset.mood_intensity == 0.5
    assert reset.mr_round_count == 0
    assert reset.mr_last_summary == {}
    assert runtime.stateVersion == 8
    assert runtime.instance.stateVersion == 8
    assert persisted and persisted[0][1] == 8
    assert persisted[0][0].self_l2 == ""

    with pytest.raises(CharacterStateConflictError):
        await service.reset_evolution(
            runtime.instance.instanceId,
            expected_state_version=7,
            runtime=runtime,
            persist=persist,
        )


@pytest.mark.asyncio
async def test_reset_evolution_rejects_busy_runtime_before_mutation() -> None:
    service = _make_service()
    runtime = _runtime_for_reset()
    await runtime.lock.acquire()
    try:
        with pytest.raises(CharacterBusyError):
            await service.reset_evolution(
                runtime.instance.instanceId,
                expected_state_version=7,
                runtime=runtime,
                persist=lambda state, version: _persist_true(state, version),
            )
    finally:
        runtime.lock.release()


async def _persist_true(state: CharacterRuntimeState, version: int) -> bool:
    return True
