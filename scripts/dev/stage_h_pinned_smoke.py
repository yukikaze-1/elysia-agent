#!/usr/bin/env python3
"""Stage H pinned fixture 真实开发库 smoke（BE-H2-03）。

定位：把 `seed_stage_h_pinned.py` 造出的夹具放进**真实开发库 + 真实路由 + 真实角色
运行时解析**里走一遍，证明 BE-R1~R5——脚本单测只断言「四个 repository 收到调用」，
挡不住「数据能写进去但公开响应序列化不了」这类缺口。

用法（必须显式 opt-in；Mongo 主机须回环/内网）：

    STAGE_H_FIXTURES=1 conda run -n Elysia python scripts/dev/stage_h_pinned_smoke.py

约束：
- 只动固定 fixture ID；从不扫描或改写其他角色/会话；
- 不启动也不需要用户正在跑的后端进程——本脚本进程内 `create_app` + 真实 Mongo；
- 推理用 stub LLM（本 smoke 验的是版本解析与序列化，不是模型质量），
  因此不产生外部 provider 调用；
- 结束前把 fixture 会话的归档消息一并清掉（真实生成会写 message_archive，
  而夹具 cleanup 只管 sessions/instances/revisions/definition）；
- 输出脱敏 JSON 摘要 + 非零退出码，任一项 FAIL 即退出 1。

BE-R2 说明：pinned / follow_latest 各跑一个**完整真实回合**（进程内 app + WS v2），
断言的是回合结束后 registry 中该 instance 的 runtime 解析版本：
pinned 恒 revision 1，follow_latest 被 reconcile 到定义当前的 revision 2。

**执行顺序（重要）**：必须先 `agent.stop()` 再 `cleanup_fixture`。跑过回合的 agent
会在关机时把内存里的 session 与 character runtime 状态 flush 回 Mongo，把刚删掉的
fixture 行**复活**；因此本 smoke 分两段：①带 agent 跑 seed/回合，关机；②cleanup 后
用**冷启动**的第二个 agent 做公开读取。真机验收同理——cleanup 必须在服务停止后执行，
或在 cleanup 后重启服务再复查。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.dev.seed_stage_h_pinned import (  # noqa: E402
    FIXTURE_CHARACTER_ID,
    FIXTURE_FOLLOW_CONVERSATION_ID,
    FIXTURE_FOLLOW_INSTANCE_ID,
    FIXTURE_PINNED_CONVERSATION_ID,
    FIXTURE_PINNED_INSTANCE_ID,
    FixtureRepos,
    _assert_non_production,
    _repos,
    _require_opt_in,
    cleanup_fixture,
    seed_fixture,
)

_WS_HEADERS = {"X-Protocol-Version": "2"}
_TURN_TIMEOUT = 60.0


class _Smoke:
    """收集 BE-R* 断言结果；任一 FAIL 决定退出码。"""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, rid: str, cond: bool, detail: str = "") -> None:
        self.checks.append({"id": rid, "pass": bool(cond), "detail": detail[:200]})

    def report(self) -> dict[str, Any]:
        failed = [c["id"] for c in self.checks if not c["pass"]]
        return {"checks": self.checks, "failed": failed, "exit": 1 if failed else 0}


def _make_agent() -> Any:
    """真 Mongo/Milvus 装配 + stub LLM（不注入 fake 存储——本 smoke 要真库）。"""
    from core.agent import Agent
    from core.config import AppConfig
    from core.cognitive.tests.test_v2_generation import _UsageLLM

    config = AppConfig()
    config.tts.enabled = False
    config.stt.enabled = False
    agent = Agent(config=config)
    agent.llm._backend = _UsageLLM()  # type: ignore[reportAttributeAccessIssue]  # noqa: SLF001
    return agent


async def _send_turn(client: Any, conversation_id: str, content: str) -> list[dict]:
    """在指定会话发一条 v2 文本，收帧到 generation.completed（或 idle）。"""
    frames: list[dict] = []
    async with client.session.ws_connect(client.make_url("/ws"), headers=_WS_HEADERS) as ws:
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
            except asyncio.TimeoutError:
                break
            if msg.type.name != "TEXT":
                continue
            if json.loads(msg.data).get("type") in ("server_info", "capabilities"):
                break
        await ws.send_json({
            "type": "text",
            "content": content,
            "clientRequestId": f"req_{time.monotonic_ns()}",
            "conversationId": conversation_id,
        })
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=_TURN_TIMEOUT)
            if msg.type.name != "TEXT":
                continue
            data = json.loads(msg.data)
            frames.append(data)
            if data.get("type") == "generation.completed":
                break
            if data.get("type") == "error":
                break
    return frames


def _instance_revision(agent: Any, instance_id: str) -> int | None:
    """回合结束后 registry 中该 instance 的 runtime 解析版本（None=未加载）。"""
    registry = getattr(agent, "character_runtime_registry", None)
    runtime = registry.get_existing(instance_id) if registry is not None else None
    return None if runtime is None else runtime.revision.revision


async def _be_r1(smoke: _Smoke, client: Any) -> None:
    """BE-R1：seed 后公开 catalog/detail 不再 INTERNAL_ERROR。"""
    resp = await client.get("/api/characters?status=active")
    body = await resp.json()
    smoke.check("BE-R1.list.status", resp.status == 200, f"HTTP {resp.status} {body}")
    ids = [item.get("characterId") for item in (body.get("items") or [])]
    smoke.check("BE-R1.list.contains_fixture",
                FIXTURE_CHARACTER_ID in ids, f"items={ids}")

    detail_resp = await client.get(f"/api/characters/{FIXTURE_CHARACTER_ID}")
    detail = await detail_resp.json()
    smoke.check("BE-R1.detail.status", detail_resp.status == 200,
                f"HTTP {detail_resp.status}")
    smoke.check("BE-R1.detail.created_by",
                (detail.get("current") or {}).get("createdBy") == "migration",
                str((detail.get("current") or {}).get("createdBy")))


async def _bump_to_revision_2(smoke: _Smoke, client: Any) -> None:
    """经公开 REST 把 Definition 推进到 revision 2（H-44 的必要步骤）。"""
    detail = await (await client.get(f"/api/characters/{FIXTURE_CHARACTER_ID}")).json()
    current = detail["current"]
    body = {
        "displayName": current["displayName"],
        "description": current["description"],
        "aliases": current["aliases"],
        "promptProfile": current["promptProfile"],
        "runtimeDefaults": current["runtimeDefaults"],
        "presentation": current["presentation"],
        "openingMessage": current.get("openingMessage"),
        "expectedRevision": detail["summary"]["currentRevision"],
        "statePolicy": "preserve",
    }
    resp = await client.put(f"/api/characters/{FIXTURE_CHARACTER_ID}", json=body)
    payload = await resp.json()
    smoke.check("BE-R2.bump_to_revision_2", resp.status == 200,
                f"HTTP {resp.status} {payload}")
    smoke.check("BE-R2.definition_at_revision_2",
                (payload.get("character") or {}).get("summary", {}).get("currentRevision") == 2,
                str((payload.get("character") or {}).get("summary")))


async def _be_r2(smoke: _Smoke, agent: Any, client: Any) -> None:
    """BE-R2：pinned 用 revision 1，follow_latest 用 revision 2。"""
    pinned_frames = await _send_turn(client, FIXTURE_PINNED_CONVERSATION_ID, "你好")
    smoke.check("BE-R2.pinned.turn_completed",
                any(f.get("type") == "generation.completed" for f in pinned_frames),
                str([f.get("type") for f in pinned_frames])[:160])
    smoke.check("BE-R2.pinned.uses_revision_1",
                _instance_revision(agent, FIXTURE_PINNED_INSTANCE_ID) == 1,
                f"resolved={_instance_revision(agent, FIXTURE_PINNED_INSTANCE_ID)}")

    follow_frames = await _send_turn(client, FIXTURE_FOLLOW_CONVERSATION_ID, "你好")
    smoke.check("BE-R2.follow.turn_completed",
                any(f.get("type") == "generation.completed" for f in follow_frames),
                str([f.get("type") for f in follow_frames])[:160])
    smoke.check("BE-R2.follow.uses_revision_2",
                _instance_revision(agent, FIXTURE_FOLLOW_INSTANCE_ID) == 2,
                f"resolved={_instance_revision(agent, FIXTURE_FOLLOW_INSTANCE_ID)}")


async def _be_r3(smoke: _Smoke, client: Any) -> None:
    """BE-R3：清理后公开读取（冷启动服务，缓存里没有已删角色）。"""
    resp = await client.get("/api/characters?status=active")
    body = await resp.json()
    ids = [item.get("characterId") for item in (body.get("items") or [])]
    smoke.check("BE-R3.list.excludes_fixture",
                resp.status == 200 and FIXTURE_CHARACTER_ID not in ids,
                f"HTTP {resp.status} items={ids}")

    detail_resp = await client.get(f"/api/characters/{FIXTURE_CHARACTER_ID}")
    smoke.check("BE-R3.detail.not_found", detail_resp.status == 404,
                f"HTTP {detail_resp.status} {(await detail_resp.text())[:160]}")


async def _be_r4(smoke: _Smoke, repos: Any, *, prefix: str) -> None:
    """BE-R4：repository 复查——夹具本体 + 真实回合的三类派生记录一个都不剩。"""
    from core.schema.types import SessionId

    smoke.check(f"{prefix}.definition_gone",
                await repos.characters.get_definition(FIXTURE_CHARACTER_ID) is None)
    smoke.check(f"{prefix}.revisions_gone",
                await repos.revisions.list_revisions(FIXTURE_CHARACTER_ID) == [])
    for instance_id in (FIXTURE_PINNED_INSTANCE_ID, FIXTURE_FOLLOW_INSTANCE_ID):
        smoke.check(f"{prefix}.instance_gone:{instance_id}",
                    await repos.instances.get(instance_id) is None)
    for conversation_id in (
        FIXTURE_PINNED_CONVERSATION_ID, FIXTURE_FOLLOW_CONVERSATION_ID):
        sid = SessionId(conversation_id)
        smoke.check(f"{prefix}.session_gone:{conversation_id}",
                    await repos.sessions.load(sid) is None)
        _, archive_total = await repos.archive.query_by_session(sid, limit=1)
        smoke.check(f"{prefix}.archive_gone:{conversation_id}", archive_total == 0)
        smoke.check(f"{prefix}.tokens_gone:{conversation_id}",
                    await repos.tokens.query_by_session(conversation_id, limit=1) == [])
        gens, _, _ = await repos.generations.find_by_session(sid, limit=1)
        smoke.check(f"{prefix}.generations_gone:{conversation_id}", gens == [])


async def _run(smoke: _Smoke, repos: FixtureRepos) -> None:
    """按固定顺序跑完整套：先停机再收尾——异常路径也必须走完收尾。

    顺序（见模块 docstring）：
        停止 agent → 清理 archive/tokens/generations → 清理 session/instance/revision/
        definition → 回读复查
    因此两段 agent 都各自套 `try/finally`，收尾不依赖正常路径。
    """
    from aiohttp.test_utils import TestClient, TestServer

    from core.input.server import create_app

    # 干净基线：上次中断留下的 fixture 先清掉（幂等）
    await cleanup_fixture(repos)

    # ---- 第一段：带 agent 跑 seed 与两轮真实回合 ----
    agent = _make_agent()
    removed: dict[str, int] = {}
    try:
        await agent.start()
        app = create_app(agent, serve_static=False)
        async with TestClient(TestServer(app)) as client:
            await seed_fixture(repos)
            await _be_r1(smoke, client)
            await _bump_to_revision_2(smoke, client)
            await _be_r2(smoke, agent, client)
    finally:
        # 顺序不可换：关机 flush 会把内存里的 session/runtime 状态写回 Mongo，
        # 先 cleanup 会被复活。REST/WS/断言任一步抛异常，这里同样执行。
        await agent.stop()
        removed = (await cleanup_fixture(repos))["removed"]

    # 三类派生记录必须真被删掉（>0）：为 0 说明两轮根本没产生记录，不是「干净」。
    smoke.check("BE-R5.cleanup_counts",
                removed.get("sessions") == 2
                and removed.get("instances") == 2
                and removed.get("characters") == 1
                and removed.get("revisions", 0) >= 1
                and removed.get("archive", 0) > 0
                and removed.get("tokens", 0) > 0
                and removed.get("generations", 0) > 0,
                str(removed))
    await _be_r4(smoke, repos, prefix="BE-R4")

    # ---- 第二段：冷启动服务做清理后的公开读取 ----
    cold_agent = _make_agent()
    try:
        await cold_agent.start()
        cold_app = create_app(cold_agent, serve_static=False)
        async with TestClient(TestServer(cold_app)) as client:
            await _be_r3(smoke, client)
    finally:
        await cold_agent.stop()

    # 关机回写复查：冷服务从未装载过 fixture 会话，关机也不得把它造回来。
    await _be_r4(smoke, repos, prefix="BE-R4.no_resurrection")

    second = await cleanup_fixture(repos)
    smoke.check("BE-R5.second_cleanup_all_zero",
                second["removed"] == {
                    "sessions": 0, "instances": 0, "revisions": 0, "characters": 0,
                    "archive": 0, "tokens": 0, "generations": 0},
                str(second["removed"]))


async def main() -> int:
    _require_opt_in()
    smoke = _Smoke()

    from Module.config.loader import EnvManager

    # 真 agent 装配要读 .env（GenerationRepository / DelegationPreparer 直接看 os.environ）。
    EnvManager.ensure_dotenv()

    from core.config import AppConfig
    _assert_non_production(AppConfig().database.mongodb_uri)

    repos = await _repos()
    try:
        await _run(smoke, repos)
    except SystemExit as exc:
        # 收尾/复查的硬拒绝（例如残留）——记成失败项，报告照常输出。
        smoke.check("smoke.aborted", False, f"SystemExit: {exc}")
    except Exception as exc:  # noqa: BLE001  中断也要输出已跑完的断言
        smoke.check("smoke.aborted", False, f"{type(exc).__name__}: {exc}")
    finally:
        for repo in vars(repos).values():
            try:
                await repo.close()
            except Exception:  # noqa: BLE001  收尾失败不掩盖主结果
                pass

    report = smoke.report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["exit"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
