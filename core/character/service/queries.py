"""读链：查询 / 列表 / 详情 / 运行时源解析 / 无状态 preview。

自 core/character/service.py 拆出。读链只依赖 `base`（共享管道）与 `helpers`
（纯函数）：它不碰定义写链与实例写链——`preview` 的草稿编译是纯内存构造
（`_compile_preview_profile`），不落库、不建 instance、零持久化副作用。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft
from core.character.revision import CharacterRevision
from core.character.service.base import CharacterServiceBase
from core.character.service.errors import (
    CharacterNotFoundError,
    CharacterPreviewUnavailableError,
    CharacterQueryError,
    CharacterStorageUnavailableError,
)
from core.character.service.helpers import (
    MAX_CHARACTER_LIST_SCAN,
    _decode_cursor,
    _detail,
    _encode_cursor,
    _limit,
    _normalise_revision,
    _revision_wire,
    _summary,
)
from frontend_contract.rest import (
    CharacterDetail,
    CharacterListResponse,
    CharacterPreviewResult,
    CharacterRevisionListResponse,
    CharacterRevisionSummary,
    CharacterSummary,
)

if TYPE_CHECKING:
    from core.character.runtime_profile import RuntimeCharacterProfile

# 日志名固定为拆分前的模块名：`%(name)s` 出现在每行日志里，拆分不改日志分流。
logger = logging.getLogger("core.character.service")


class CharacterQueryChain(CharacterServiceBase):
    """读链混入：读路径 + 无状态 preview。"""

    async def get(self, character_id: str) -> CharacterDetail | None:
        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            return _detail(*builtin_pair)
        await self._ensure_connected()
        pair = await self._get_definition_and_revision(character_id)
        if pair is None:
            return None
        return _detail(*pair)

    async def resolve_runtime_source(
        self, character_id: str, *, revision: int | None = None,
    ) -> tuple[CharacterDefinition, CharacterRevision]:
        """Resolve one exact immutable revision for runtime construction.

        This domain API deliberately returns internal models rather than REST
        DTOs.  A requested historical revision is never replaced with the current
        pointer, and a missing/corrupt revision fails closed.
        """
        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            definition, current = builtin_pair
            if revision is not None and revision != current.revision:
                raise CharacterNotFoundError(character_id)
            return definition, _normalise_revision(current)

        await self._ensure_connected()
        definition = await self.repo.get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        selected_revision = revision or definition.currentRevision
        content = await self.rev_repo.get_revision(character_id, selected_revision)
        if content is None:
            raise CharacterStorageUnavailableError(
                f"角色 revision 缺失: {character_id}@{selected_revision}"
            )
        return definition, _normalise_revision(content)

    async def list(
        self,
        *,
        source: str | None = None,
        status: str = "active",
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CharacterListResponse:
        if source not in {None, "builtin", "custom"}:
            raise CharacterQueryError("source 无效")
        if status not in {"active", "archived"}:
            raise CharacterQueryError("status 无效")
        page_size = _limit(limit)
        search_value = search.strip() if search else None
        if search_value is not None and not search_value:
            search_value = None
        if search_value and len(search_value) > 200:
            raise CharacterQueryError("search 过长")
        marker = _decode_cursor(cursor)
        if marker is not None and (
            not isinstance(marker.get("updatedAt"), str)
            or not isinstance(marker.get("characterId"), str)
        ):
            raise CharacterQueryError("cursor 无效")

        # Built-ins are file-backed and remain available when Mongo is offline.
        # A combined/custom list still needs the custom store and therefore fails
        # closed instead of silently returning an incomplete catalogue.
        if source in {None, "custom"}:
            await self._ensure_connected()

        summaries: list[CharacterSummary] = []
        if source in {None, "custom"}:
            definitions = await self.repo.list_definitions(
                source="custom",
                status=status,
                owner_user_id="usr_local_default",
                search=None,
                cursor=None,
                limit=MAX_CHARACTER_LIST_SCAN,
            )
            for definition in definitions:
                revision = await self.rev_repo.get_revision(
                    definition.characterId, definition.currentRevision
                )
                if revision is None:
                    raise CharacterStorageUnavailableError("角色 revision 缺失")
                summaries.append(_summary(definition, revision))

        if source in {None, "builtin"} and self.catalog is not None:
            if self._builtin_entries is None:
                self._builtin_entries = []
                for definition in self.catalog.list():
                    pair = self.catalog.get_revision(definition.characterId)
                    if pair is None:
                        raise CharacterStorageUnavailableError("内置角色 revision 缺失")
                    self._builtin_entries.append(pair)
            for definition, revision in self._builtin_entries:
                if definition.status != status:
                    continue
                summaries.append(_summary(definition, revision))

        if search_value:
            needle = search_value.casefold()
            summaries = [
                item
                for item in summaries
                if needle
                in " ".join(
                    [item.characterId, item.displayName, item.description, *item.aliases]
                ).casefold()
            ]
        summaries.sort(key=lambda item: (item.updatedAt, item.characterId), reverse=True)
        if marker is not None:
            summaries = [
                item
                for item in summaries
                if (item.updatedAt, item.characterId)
                < (marker["updatedAt"], marker["characterId"])
            ]
        has_more = len(summaries) > page_size
        page = summaries[:page_size]
        next_cursor = (
            _encode_cursor({"updatedAt": page[-1].updatedAt, "characterId": page[-1].characterId})
            if has_more and page
            else None
        )
        return CharacterListResponse(items=page, has_more=has_more, cursor=next_cursor)

    async def list_revisions(
        self, character_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> CharacterRevisionListResponse:
        page_size = _limit(limit)
        marker = _decode_cursor(cursor)
        revision_marker = None
        if marker is not None:
            if not isinstance(marker.get("revision"), int) or marker["revision"] < 1:
                raise CharacterQueryError("cursor 无效")
            revision_marker = marker["revision"]

        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            revisions = [] if revision_marker is not None else [builtin_pair[1]]
            items = [
                CharacterRevisionSummary(
                    revision=wire.revision,
                    checksum=wire.checksum,
                    createdBy=wire.createdBy,
                    createdAt=wire.createdAt,
                )
                for wire in (_revision_wire(revision) for revision in revisions)
            ]
            return CharacterRevisionListResponse(items=items, has_more=False, cursor=None)

        await self._ensure_connected()
        definition = await self._get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        if hasattr(self.rev_repo, "list_revisions_page"):
            revisions, has_more = await self.rev_repo.list_revisions_page(
                character_id, cursor=revision_marker, limit=page_size
            )
        else:
            revisions = await self.rev_repo.list_revisions(
                character_id,
                cursor=str(revision_marker) if revision_marker else None,
                limit=page_size + 1,
            )
            has_more = len(revisions) > page_size
            revisions = revisions[:page_size]
        items = [
            CharacterRevisionSummary(
                revision=wire.revision,
                checksum=wire.checksum,
                createdBy=wire.createdBy,
                createdAt=wire.createdAt,
            )
            for wire in (_revision_wire(revision) for revision in revisions)
        ]
        next_cursor = (
            _encode_cursor({"revision": items[-1].revision})
            if has_more and items
            else None
        )
        return CharacterRevisionListResponse(
            items=items, has_more=has_more, cursor=next_cursor
        )

    async def preview(
        self, draft: CharacterDraft, *, user_message: str
    ) -> CharacterPreviewResult:
        """无状态预览：校验 draft → 一次性组装 prompt → 单次推理，零副作用。

        BE-STAGEH-03：不建 session/instance、不写 archive、不读会话/LTM/演化 state、
        不推进 revision/stateVersion、不触发 reflector/压缩/mood/TTS/工具。用量不并入
        任何会话 token 统计（结果不带 usage）。
        """
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        port = self.preview_port
        if port is None:
            raise CharacterPreviewUnavailableError("角色预览未启用无状态推理适配器")
        identity = self.user_identity_store.get()
        if identity is None:
            # P0-3：权威身份不可用时不得用占位身份预览（fail-closed，稳定公开错误）。
            if (self.user_identity_store.is_authoritative
                    and not self.user_identity_store.is_available):
                raise CharacterStorageUnavailableError("用户身份权威存储不可用")
            raise CharacterPreviewUnavailableError("用户身份未初始化，无法预览")

        profile = self._compile_preview_profile(draft, owner_user_id=identity.userId)
        try:
            reply = await port.generate(
                profile=profile,
                user_identity=identity,
                user_message=user_message,
            )
        except CharacterPreviewUnavailableError:
            raise
        except Exception as exc:
            logger.warning("角色预览推理失败: %s", type(exc).__name__)
            raise CharacterPreviewUnavailableError("角色预览上游失败") from exc

        text = reply.text.strip()
        if not text:
            raise CharacterPreviewUnavailableError("角色预览返回空回复")
        return CharacterPreviewResult(
            ephemeral=True,
            reply=text,
            model=reply.model,
            usage=None,
        )

    def _compile_preview_profile(
        self, draft: CharacterDraft, *, owner_user_id: str
    ) -> "RuntimeCharacterProfile":
        """把 draft 编译成仅存在于内存的 profile（不落库、不建 instance）。"""
        from core.character.runtime_profile import compile_runtime_profile

        character_id = f"chr_preview_{uuid.uuid4().hex[:12]}"
        definition = CharacterDefinition(
            characterId=character_id,
            slug=draft.displayName,
            source="custom",
            mutable=True,
            status="active",
            currentRevision=1,
            ownerUserId=owner_user_id,
        )
        revision = self._revision_from_draft(character_id, draft, revision=1)
        return compile_runtime_profile(definition, revision)
