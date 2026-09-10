#!/usr/bin/env python3
"""Stage H pinned instance 联调夹具（BE-STAGEH-05）。

产品 API 没有创建 `revisionPolicy=pinned` instance 的入口，开发库又按决策不保留
legacy migration 数据，因此 Windows 真实联调无法构造 H-44（pinned vs follow_latest
对照）。本脚本用**正式 repository** 建一条幂等的、显式标记为 fixture 的 pinned
instance + 同角色 follow_latest instance + 各自一条持久化 session binding。

用法（必须显式 opt-in）：

    STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/seed_stage_h_pinned.py
    STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/seed_stage_h_pinned.py --cleanup

约束：
- 仅在 `STAGE_H_FIXTURES=1` 且 Mongo 主机非公网（回环/内网/链路本地）时运行；
- 幂等：固定 character/instance/session ID + setOnInsert，重复执行不产生副本；
- 只写/只删本脚本自己固定 ID 的 fixture 数据，不扫描/修改/迁移任何旧会话；
- 输出脱敏 JSON（只有 characterId/instanceId/conversationId/revisionPolicy/revision）。

**清理范围（--cleanup）**：两条 fixture session 的**全部派生记录**——`message_archive`、
`token_usage`、`generations`，随后是 session / instance / 该 characterId 的全部 revision /
definition；删完回读复查，有残留即非零退出。

**执行顺序（重要）**：cleanup 必须在**服务停止之后**运行。跑过 fixture 会话的 agent 会在
`stop()` 时把内存里的 session 与 character runtime 状态 flush 回 Mongo——先 cleanup 再关机，
刚删掉的行会被关机回写重新创建。

验收（§6.3）：seed 后从 REST 把 Definition 更新到 revision 2——follow_latest 下一轮
用 revision 2，pinned 仍用 revision 1；清理后正式 active catalog 不残留测试角色。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if TYPE_CHECKING:
    from core.character.definition import CharacterDefinition
    from core.character.revision import CharacterRevision
    from core.storage.character_instance_repo import CharacterInstanceRepository
    from core.storage.character_repo import CharacterRepository
    from core.storage.character_revision_repo import CharacterRevisionRepository
    from core.storage.generation_repo import GenerationRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository

FIXTURE_CHARACTER_ID = "chr_stage_h_fixture"
FIXTURE_PINNED_INSTANCE_ID = "chi_stage_h_fixture_pinned"
FIXTURE_FOLLOW_INSTANCE_ID = "chi_stage_h_fixture_follow"
FIXTURE_PINNED_CONVERSATION_ID = "conv_stage_h_fixture_pinned"
FIXTURE_FOLLOW_CONVERSATION_ID = "conv_stage_h_fixture_follow"
FIXTURE_OWNER_USER_ID = "usr_local_default"
FIXTURE_SLUG = "[stage-h-fixture] pinned 对照角色"
FIXTURE_REVISION = 1


@dataclass(frozen=True)
class FixtureRepos:
    """夹具用到的全部正式 repository。

    四类是夹具本体（definition/revision/instance/session）；后三类是**真实回合派生**的
    副作用记录——清理时必须一起删，否则会留下指向已删会话的孤儿行（generations 还有
    30 天 TTL）。
    """

    characters: "CharacterRepository"
    revisions: "CharacterRevisionRepository"
    instances: "CharacterInstanceRepository"
    sessions: "SessionRepository"
    archive: "MessageRepository"
    tokens: "TokenRepository"
    generations: "GenerationRepository"


def _require_opt_in() -> None:
    if os.getenv("STAGE_H_FIXTURES") != "1":
        raise SystemExit(
            "拒绝执行：需显式设置 STAGE_H_FIXTURES=1（开发夹具，禁止默认运行）")


def _assert_non_production(mongo_uri: str | None) -> None:
    """公网 Mongo 主机视为生产——夹具只允许打在本地/内网库上。"""
    if not mongo_uri:
        return
    host = urlparse(mongo_uri).hostname
    if not host:
        return
    if host == "localhost":
        return
    try:
        addr = ip_address(host)
    except ValueError:
        raise SystemExit(f"拒绝执行：Mongo 主机 {host!r} 不是本地/内网地址") from None
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return
    raise SystemExit(f"拒绝执行：Mongo 主机 {host!r} 不是本地/内网地址")


def _fixture_revision() -> "CharacterRevision":
    """复用生产 draft→revision 构建器，保证与真实保存的 revision 同形。"""
    from core.character.draft import CharacterDraft
    from core.character.service import CharacterService
    from frontend_contract.rest import CharacterCreatedBy

    draft = CharacterDraft.model_validate({
        "displayName": "Stage H 夹具角色",
        "description": "pinned/follow_latest 对照用开发夹具（非产品数据）。",
        "promptProfile": {
            "corePersonality": "你是 Stage H 联调夹具角色，回复极简。",
            "relationshipStyle": "保持清晰边界。",
            "emotionStyle": "自然表达。",
            "speakingStyle": "一句话内。",
            "conversationInitiative": "不主动。",
        },
    })
    revision: CharacterRevision = CharacterService._revision_from_draft(  # noqa: SLF001
        FIXTURE_CHARACTER_ID,
        draft,
        revision=FIXTURE_REVISION,
        # BE-H2-01：领域 CharacterRevision.createdBy 是宽松 str，写库不校验；公开
        # CharacterRevisionContent 只接受 system|user|migration。fixture 用它表达
        # 「受控脚本注入」，不污染公开枚举。
        created_by=CharacterCreatedBy.MIGRATION,
    )
    return revision


def _assert_public_serialization(
    definition: "CharacterDefinition", revision: "CharacterRevision",
) -> None:
    """fixture 数据必须能走公开 REST 序列化路径。

    回归 BE-H2-01：非法 createdBy 在领域层无感，却会让 GET /api/characters 整页
    ValidationError → INTERNAL_ERROR（目录里所有角色一起挂掉，不是单条坏数据）。
    """
    from pydantic import ValidationError

    from core.character.service import _summary  # noqa: PLC2701

    try:
        _summary(definition, revision)
    except ValidationError as exc:
        raise SystemExit(
            f"拒绝继续：fixture revision 无法通过公开契约序列化（{exc.error_count()} 处校验失败）"
        ) from None


async def seed_fixture(repos: FixtureRepos) -> dict:
    """幂等创建 fixture definition/revision/两个 instance/两条 session binding。"""
    from core.character.definition import CharacterDefinition
    from core.character.instance import CharacterInstance
    from core.schema.types import SessionId

    characters, revisions = repos.characters, repos.revisions
    instances, sessions = repos.instances, repos.sessions

    if await characters.get_definition(FIXTURE_CHARACTER_ID) is None:
        await characters.insert_definition(CharacterDefinition(
            characterId=FIXTURE_CHARACTER_ID,
            slug=FIXTURE_SLUG,
            source="custom",
            mutable=True,
            status="active",
            currentRevision=FIXTURE_REVISION,
            ownerUserId=FIXTURE_OWNER_USER_ID,
        ))
    if await revisions.get_revision(FIXTURE_CHARACTER_ID, FIXTURE_REVISION) is None:
        await revisions.append_revision(_fixture_revision())

    pairs = (
        (FIXTURE_PINNED_INSTANCE_ID, FIXTURE_PINNED_CONVERSATION_ID, "pinned"),
        (FIXTURE_FOLLOW_INSTANCE_ID, FIXTURE_FOLLOW_CONVERSATION_ID, "follow_latest"),
    )
    for instance_id, conversation_id, policy in pairs:
        await instances.save_if_absent(CharacterInstance(
            instanceId=instance_id,
            characterId=FIXTURE_CHARACTER_ID,
            revision=FIXTURE_REVISION,
            revisionPolicy=policy,          # type: ignore[arg-type]
            ownerUserId=FIXTURE_OWNER_USER_ID,
            canonical=False,                # 不占用 (user, character) canonical 槽位
        ))
        await sessions.ensure_exists(
            SessionId(conversation_id),
            {"character_id": FIXTURE_CHARACTER_ID, "instance_id": instance_id},
        )

    # BE-H2-01：回读并过一遍公开序列化路径——坏 createdBy 到此即失败，不会等到
    # 前端打开角色目录时才暴露成整页 INTERNAL_ERROR。
    definition = await characters.get_definition(FIXTURE_CHARACTER_ID)
    revision = await revisions.get_revision(FIXTURE_CHARACTER_ID, FIXTURE_REVISION)
    if definition is None or revision is None:
        raise SystemExit("拒绝宣告 seed 成功：fixture 数据回读失败")
    _assert_public_serialization(definition, revision)

    return {
        "fixture": "stage-h-pinned",
        "characterId": FIXTURE_CHARACTER_ID,
        "revision": FIXTURE_REVISION,
        "instances": [
            {
                "instanceId": instance_id,
                "conversationId": conversation_id,
                "revisionPolicy": policy,
                "revision": FIXTURE_REVISION,
            }
            for instance_id, conversation_id, policy in pairs
        ],
    }


async def _all_revision_numbers(
    revisions: "CharacterRevisionRepository", character_id: str,
) -> list[int]:
    """分页取该 characterId 的全部 revision 号——不假设只有 1/2，也不扫描其他角色。"""
    numbers: list[int] = []
    cursor: int | None = None
    while True:
        page, has_more = await revisions.list_revisions_page(
            character_id, cursor=cursor, limit=100)
        numbers.extend(rev.revision for rev in page)
        if not has_more:
            return numbers
        cursor = numbers[-1]


async def _verify_no_residue(repos: FixtureRepos) -> None:
    """删完回读复查——仍有残留就非零退出，不谎报干净。"""
    from core.schema.types import SessionId

    residue: list[str] = []
    if await repos.characters.get_definition(FIXTURE_CHARACTER_ID) is not None:
        residue.append("definition")
    for instance_id in (FIXTURE_PINNED_INSTANCE_ID, FIXTURE_FOLLOW_INSTANCE_ID):
        if await repos.instances.get(instance_id) is not None:
            residue.append(f"instance:{instance_id}")
    if await _all_revision_numbers(repos.revisions, FIXTURE_CHARACTER_ID):
        residue.append("revisions")
    for conversation_id in (
        FIXTURE_PINNED_CONVERSATION_ID, FIXTURE_FOLLOW_CONVERSATION_ID):
        sid = SessionId(conversation_id)
        if await repos.sessions.load(sid) is not None:
            residue.append(f"session:{conversation_id}")
        # 真实回合派生记录：三张表的会话字段都叫 session_id（不是 conversation_id）
        _, archive_total = await repos.archive.query_by_session(sid, limit=1)
        if archive_total:
            residue.append(f"archive:{conversation_id}")
        if await repos.tokens.query_by_session(conversation_id, limit=1):
            residue.append(f"tokens:{conversation_id}")
        gens, _, _ = await repos.generations.find_by_session(sid, limit=1)
        if gens:
            residue.append(f"generations:{conversation_id}")
    if residue:
        raise SystemExit("拒绝宣告清理成功：fixture 仍有残留 " + ", ".join(residue))


async def cleanup_fixture(repos: FixtureRepos) -> dict:
    """删除本夹具固定 ID 的全部数据（含真实回合派生记录）——不触碰任何其他记录。

    顺序：先删**派生记录**（archive / tokens / generations），再删本体
    （session → instance → 全部 revision → definition）。派生记录必须删——否则留下
    指向已删会话的孤儿行（generations 有 30 天 TTL，期间一直可见）。最后回读复查。
    """
    from core.schema.types import SessionId

    removed = {"sessions": 0, "instances": 0, "revisions": 0, "characters": 0,
               "archive": 0, "tokens": 0, "generations": 0}
    for conversation_id in (
        FIXTURE_PINNED_CONVERSATION_ID, FIXTURE_FOLLOW_CONVERSATION_ID):
        sid = SessionId(conversation_id)
        removed["archive"] += await repos.archive.delete_by_session(sid)
        removed["tokens"] += await repos.tokens.delete_by_session(sid)
        removed["generations"] += await repos.generations.delete_by_session(sid)
        removed["sessions"] += await repos.sessions.delete(sid)
    for instance_id in (FIXTURE_PINNED_INSTANCE_ID, FIXTURE_FOLLOW_INSTANCE_ID):
        removed["instances"] += int(await repos.instances.delete(instance_id))
    # BE-H2-02：H-44 会把同一 Definition 从 revision 1 推到 2，清理必须删净全部
    # revision（只删固定 R1 会留孤儿，且计数是假的）。
    for number in await _all_revision_numbers(repos.revisions, FIXTURE_CHARACTER_ID):
        await repos.revisions.delete_revision(FIXTURE_CHARACTER_ID, number)
        removed["revisions"] += 1
    removed["characters"] = int(
        await repos.characters.delete_definition(FIXTURE_CHARACTER_ID))

    await _verify_no_residue(repos)
    return {"fixture": "stage-h-pinned", "removed": removed}


async def _repos() -> FixtureRepos:
    """按 Agent 的生产装配方式建连夹具用到的全部正式 repository。"""
    from core.config import AppConfig
    from core.storage.character_instance_repo import CharacterInstanceRepository
    from core.storage.character_repo import CharacterRepository
    from core.storage.character_revision_repo import CharacterRevisionRepository
    from core.storage.generation_repo import GenerationRepository
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository
    from core.storage.token_repo import TokenRepository

    cfg = AppConfig()
    uri = cfg.database.mongodb_uri
    db_name = cfg.database.db_name
    _assert_non_production(uri)
    bundle = FixtureRepos(
        characters=CharacterRepository(base_url=uri),
        revisions=CharacterRevisionRepository(base_url=uri),
        instances=CharacterInstanceRepository(base_url=uri),
        sessions=SessionRepository(database_name=db_name, base_url=uri),
        archive=MessageRepository(database_name=db_name, base_url=uri),
        tokens=TokenRepository(database_name=db_name, base_url=uri),
        generations=GenerationRepository(database_name=db_name, base_url=uri),
    )
    for repo in vars(bundle).values():
        if not await repo.connect():
            raise SystemExit(
                f"MongoDB 不可用（{type(repo).__name__}）——夹具未创建")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage H pinned 联调夹具")
    parser.add_argument("--cleanup", action="store_true",
                        help="只删除本夹具新建的数据（不动其他任何记录）")
    args = parser.parse_args()

    _require_opt_in()

    async def _run() -> dict:
        repos = await _repos()
        return await (cleanup_fixture(repos) if args.cleanup else seed_fixture(repos))

    try:
        payload = asyncio.run(_run())
    except SystemExit:
        raise
    except Exception as exc:
        # 存储/连接失败：脱敏 + 非零退出——绝不打印看似成功的汇总（不含 URI/路径）。
        print(json.dumps({
            "fixture": "stage-h-pinned",
            "ok": False,
            "error": type(exc).__name__,
        }, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
