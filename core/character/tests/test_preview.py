"""BE-STAGEH-03：无状态角色预览（零副作用 + 冻结响应）。

真实联调前 preview 在校验后固定抛 CharacterPreviewUnavailableError（503）。本文件
锁定成功路径、错误映射与「一次 preview 不产生任何持久化/事件副作用」。
"""

import pytest

from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.character.draft import CharacterDraft
from core.character.preview import PreviewReply
from core.character.service import (
    CharacterPreviewUnavailableError,
    CharacterService,
    CharacterValidationError,
)
from core.profile.profile_store import UserIdentityStore
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.tests.fake_llm import FakeLLMBackend

pytestmark = pytest.mark.service

_BODY = "你是预览用角色，说话简短。"


def _draft(**overrides: object) -> CharacterDraft:
    values: dict[str, object] = {
        "displayName": "预览角色",
        "description": "预览用。",
        "aliases": [],
        "promptProfile": {
            "corePersonality": _BODY,
            "relationshipStyle": "保持清晰边界。",
            "emotionStyle": "自然表达。",
            "speakingStyle": "简洁。",
            "conversationInitiative": "适度主动。",
            "examples": [],
        },
    }
    values.update(overrides)
    return CharacterDraft(**values)


class _FakePort:
    def __init__(self, reply: str = "你好呀♪", model: str | None = "fake/model"):
        self.calls: list[tuple[object, object, str]] = []
        self._reply = reply
        self._model = model

    async def generate(self, *, profile, user_identity, user_message):  # noqa: ANN001
        self.calls.append((profile, user_identity, user_message))
        return PreviewReply(text=self._reply, model=self._model)


class _BoomPort:
    async def generate(self, *, profile, user_identity, user_message):  # noqa: ANN001
        raise RuntimeError("upstream down")


def _store() -> UserIdentityStore:
    store = UserIdentityStore()
    store.bootstrap("妖梦")
    return store


def _service(port=None, *, store=None, repo_db=None, rev_db=None) -> CharacterService:
    return CharacterService(
        repo=CharacterRepository(db=repo_db or FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=rev_db or FakeDocumentDBBackend()),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=store or _store(),
        preview_port=port,
    )


@pytest.mark.asyncio
async def test_preview_returns_ephemeral_result() -> None:
    port = _FakePort()
    service = _service(port)

    result = await service.preview(_draft(), user_message="在吗？")

    assert result.ephemeral is True
    assert result.reply == "你好呀♪"
    assert result.model == "fake/model"
    assert result.usage is None          # 预览用量不并入任何会话统计
    assert len(port.calls) == 1
    profile, identity, message = port.calls[0]
    assert message == "在吗？"
    assert identity.displayName == "妖梦"
    assert profile.config.base_personality == _BODY   # authored C1 来自 draft
    assert profile.config.name == "预览角色"


@pytest.mark.asyncio
async def test_preview_has_zero_persistence_side_effects() -> None:
    repo_db = FakeDocumentDBBackend()
    rev_db = FakeDocumentDBBackend()
    service = _service(_FakePort(), repo_db=repo_db, rev_db=rev_db)

    await service.preview(_draft(), user_message="在吗？")

    assert repo_db.collections == {}    # 不建 definition
    assert rev_db.collections == {}     # 不建 revision


@pytest.mark.asyncio
async def test_preview_invalid_draft_does_not_call_port() -> None:
    port = _FakePort()
    service = _service(port)
    bad = _draft(promptProfile={
        "corePersonality": "<output_format>偷渡</output_format>",
        "relationshipStyle": "x", "emotionStyle": "", "speakingStyle": "y",
        "conversationInitiative": "", "examples": [],
    })

    with pytest.raises(CharacterValidationError) as excinfo:
        await service.preview(bad, user_message="hi")

    assert excinfo.value.issues
    assert port.calls == []


@pytest.mark.asyncio
async def test_preview_without_port_fails_closed() -> None:
    service = _service(None)
    with pytest.raises(CharacterPreviewUnavailableError):
        await service.preview(_draft(), user_message="hi")


@pytest.mark.asyncio
async def test_preview_upstream_failure_maps_to_stable_error() -> None:
    service = _service(_BoomPort())
    with pytest.raises(CharacterPreviewUnavailableError):
        await service.preview(_draft(), user_message="hi")


@pytest.mark.asyncio
async def test_preview_empty_reply_is_not_echoed_as_success() -> None:
    service = _service(_FakePort(reply="   "))
    with pytest.raises(CharacterPreviewUnavailableError):
        await service.preview(_draft(), user_message="hi")


@pytest.mark.asyncio
async def test_preview_requires_initialized_user_identity() -> None:
    service = _service(_FakePort(), store=UserIdentityStore())
    with pytest.raises(CharacterPreviewUnavailableError):
        await service.preview(_draft(), user_message="hi")


# ── 真实适配器：无会话/无记忆的 prompt 组装 ──


class _RecordingFakeLLM(FakeLLMBackend):
    """记录 chat() 入参的假后端。"""

    def __init__(self, response: str = "预览回复♪") -> None:
        super().__init__(response=response)
        self.messages: list[dict] | None = None
        self.kwargs: dict | None = None

    async def chat(self, messages: list[dict], **kwargs) -> str:  # noqa: ANN003
        self.messages = messages
        self.kwargs = kwargs
        return self.response


@pytest.mark.asyncio
async def test_prompt_adapter_builds_stateless_prompt_without_tools() -> None:
    from core.adapter.llm_adapter import LLMAdapter
    from core.character.preview import PromptCharacterPreviewAdapter
    from core.profile.user_identity import UserIdentity

    backend = _RecordingFakeLLM()
    llm = LLMAdapter(_test_backend=backend)
    adapter = PromptCharacterPreviewAdapter(llm)
    profile = _service(_FakePort())._compile_preview_profile(  # noqa: SLF001
        _draft(), owner_user_id="usr_local_default")

    reply = await adapter.generate(
        profile=profile,
        user_identity=UserIdentity(
            userId="usr_local_default", displayName="妖梦", pronouns="她"),
        user_message="在吗？",
    )

    assert reply.text == "预览回复♪"
    assert reply.model == llm.model
    assert backend.messages is not None
    # 只有 system + 当前用户消息——没有会话历史
    assert [m["role"] for m in backend.messages] == ["system", "user"]
    assert backend.messages[-1]["content"] == "在吗？"
    system = backend.messages[0]["content"]
    assert _BODY in system          # authored C1 已注入
    assert "预览角色" in system
    assert backend.kwargs == {}     # 不挂任何工具
