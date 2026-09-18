"""Batch 8 / P2-01r：会话列表分页完整性（route 层 = 用户入口）。

**canonical 条目**：`todo.md` §1.2 `P2-01r`
——「会话列表 `list_all` 仍预取 200 ⇒ 第 201+ 条不可分页/搜索」。

**契约（设计文档承诺，非本批发明）**：
`docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch3-multi-session.md`
§4 表格与「列表游标（keyset）」条目：`GET /api/sessions` 请求 `cursor/limit/search`，
响应 `SessionListResult{sessions[], has_more, cursor}`；游标 = `base64("updated_at|session_id")`；
排序 `updated_at DESC, session_id ASC`；下一页谓词
`(updated_at < U) OR (updated_at = U AND session_id > S)`；`has_more` 用 limit+1 探测；
`search` 对 title `$regex` 大小写不敏感（**下推到存储**）。

**本文件测的是"用户按公开合同翻页能否走到每一条"**：
driver 只读响应里的 `has_more` / `cursor`（见 `_session_pagination_support`），
不读库、不预先知道数据集大小——否则 metadata 撒谎时测试会假绿。
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.input.tests._session_pagination_support import (
    PAGE_LIMIT,
    SessionTestAgent,
    make_app,
    paginate_sessions,
    seed_session_docs,
)

pytestmark = pytest.mark.service

#: 用户可见全集 = 播种的持久会话 + clean-start 本地 default 摘要（BE-P1-05）。
DEFAULT_SESSION = "default"


async def _client_for(agent: SessionTestAgent) -> TestClient:
    client = TestClient(TestServer(make_app(agent)))
    await client.start_server()
    return client


def _expected_ids(seeded: list[str]) -> set[str]:
    """权威可见 ID 集合：播种记录 + default（内存本地摘要，恒可见）。"""
    return set(seeded) | {DEFAULT_SESSION}


# ── 核心：越过 200 之后仍然可达（PRE_FIX_RED） ──────────────────────────────


class TestReachabilityBeyondTwoHundred:
    @pytest.mark.asyncio
    async def test_two_hundred_and_one_sessions_all_reachable(self):
        """PAG-T1：201 条持久记录 → 按公开合同翻页必须拿到全部 201 条（+default）。

        修前：仓储 `list_all(limit=200)` 预取 ⇒ 最旧 1 条永久不可达
        （`has_more=False` 谎报"没有更多"）。
        """
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 201, prefix="conv_p201")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert len(result["ids"]) == len(set(result["ids"])), "分页出现重复记录"
        assert set(result["ids"]) == _expected_ids(seeded), (
            f"缺失 {sorted(_expected_ids(seeded) - set(result['ids']))}")

    @pytest.mark.asyncio
    async def test_two_hundred_and_twenty_five_sessions_all_reachable(self):
        """PAG-T2 / Challenger Attack B：225 条（> 2×page_size）完整分页。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 225, prefix="conv_p225")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert len(result["ids"]) == len(set(result["ids"]))
        assert set(result["ids"]) == _expected_ids(seeded)
        assert len(result["pages"]) >= 5, "225 条 / 每页 50 至少 5 页"

    @pytest.mark.asyncio
    async def test_two_hundred_and_one_with_search_all_reachable(self):
        """搜索也要能越过 200（canonical 原文：第 201+ 条不可分页/**搜索**）。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 201, prefix="conv_s201")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client, search="会话")
        finally:
            await client.close()

        # 播种标题均为「会话NNN」；default 标题为空 → 不参与搜索命中
        assert set(result["ids"]) == set(seeded)


# ── 边界矩阵 ───────────────────────────────────────────────────────────────


class TestBoundaryMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [0, 1, PAGE_LIMIT, PAGE_LIMIT + 1, 199, 200, 201, 225])
    async def test_every_boundary_is_complete(self, count: int):
        """PAG-T5/PAG-T6/PAG-T7：0 / 1 / page_size / page_size+1 / 199 / 200 / 201 / >201。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, count, prefix=f"conv_b{count}")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        expected = _expected_ids(seeded)
        assert set(result["ids"]) == expected, (
            f"N={count} 缺失 {sorted(expected - set(result['ids']))}")
        assert len(result["ids"]) == len(set(result["ids"]))

    @pytest.mark.asyncio
    async def test_exact_200_ends_without_next_page(self):
        """N=200（+default=201 可见记录）：分页正常结束，不产生空尾页。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 200, prefix="conv_x200")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
            last = result["last_page"]
        finally:
            await client.close()

        assert last["has_more"] is False
        assert "cursor" not in last or last["cursor"] is None
        assert len(result["ids"]) == 201

    @pytest.mark.asyncio
    async def test_two_hundred_and_one_has_one_more_page(self):
        """N=201：必须出现下一页（而非在第一页之后谎报结束）。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 201, prefix="conv_x201")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert len(result["pages"]) == 5, "201 播种 + default 需 5 页（50/50/50/50/2）"
        for page in result["pages"][:-1]:
            assert page["has_more"] is True and page["cursor"]


# ── 元数据一致性 / 客户端提前停止（PAG-6 / §36） ─────────────────────────────


class TestMetadataCoherence:
    @pytest.mark.asyncio
    async def test_has_more_matches_cursor_presence_every_page(self):
        """PAG-6：`has_more=True ⇔ cursor 存在`（否则客户端要么死循环要么提前停）。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 205, prefix="conv_meta")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        for page in result["pages"]:
            assert page["has_more"] is bool(page.get("cursor"))

    @pytest.mark.asyncio
    async def test_client_that_stops_when_has_more_false_sees_everything(self):
        """§36：严格依据 metadata 停下的客户端 == 全集（不依赖"测试知道该继续"）。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 205, prefix="conv_stop")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert set(result["ids"]) == _expected_ids(seeded)

    @pytest.mark.asyncio
    async def test_page_after_the_end_is_empty_not_error(self):
        """§41：走到末尾/无命中时是**正常空页 + has_more=false**，不是 404/400。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 3, prefix="conv_end")
        client = await _client_for(agent)
        try:
            first = await (await client.get("/api/sessions?limit=2")).json()
            assert first["has_more"] is True and first["cursor"]
            second = await (await client.get(
                f"/api/sessions?limit=2&cursor={first['cursor']}")).json()
            assert second["has_more"] is False and not second.get("cursor")
            # 末页之后：无命中 → 空页 200，而不是错误码
            empty = await client.get("/api/sessions?search=绝无此标题的关键词")
            assert empty.status == 200
            data = await empty.json()
            assert data["sessions"] == [] and data["has_more"] is False
        finally:
            await client.close()


# ── 单页上限 ≠ 数据集上限（PAG-2 / §8 / §39） ───────────────────────────────


class TestPageSizeCapIsNotDatasetCap:
    @pytest.mark.asyncio
    async def test_oversized_limit_is_clamped_per_page_but_walk_is_complete(self):
        """limit=500 → 单页被夹到 100（现有 contract 保持），但全程仍能拿到全部记录。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 205, prefix="conv_cap")
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client, limit=500)
        finally:
            await client.close()

        for page in result["pages"]:
            assert len(page["sessions"]) <= 100, "单页上限 100 是既有合同"
        assert set(result["ids"]) == _expected_ids(seeded)

    @pytest.mark.asyncio
    async def test_invalid_and_zero_limit_keep_contract(self):
        """§40：limit=0 / 非数字 → 既有 contract（夹到 [1,100]，非法回退默认）不变。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 3, prefix="conv_bad")
        client = await _client_for(agent)
        try:
            zero = await (await client.get("/api/sessions?limit=0")).json()
            assert 1 <= len(zero["sessions"]) <= 100
            junk = await (await client.get("/api/sessions?limit=abc")).json()
            assert 1 <= len(junk["sessions"]) <= 100
            bad_cursor = await client.get("/api/sessions?cursor=!!!not-base64!!!")
            assert bad_cursor.status == 400
            assert (await bad_cursor.json())["code"] == "CONTRACT_VALIDATION"
        finally:
            await client.close()


# ── 过滤/删除不因分页而泄漏（§50 / §51 / §46） ──────────────────────────────


class TestScopeAndFilterStability:
    @pytest.mark.asyncio
    async def test_search_filter_holds_on_every_page(self):
        """§51：下一页不得丢 filter——搜索只命中标题含关键词的记录。"""
        agent = SessionTestAgent()
        await seed_session_docs(agent, 120, prefix="conv_hit")
        await seed_session_docs(agent, 120, prefix="conv_miss")
        # 命中组标题：会话N；未命中组标题另起一套
        for i in range(120):
            await agent.backend.update_one(
                "sessions", {"session_id": f"conv_miss_{i:03d}"},
                {"$set": {"title": f"别的{i:03d}"}})
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client, search="会话")
        finally:
            await client.close()

        assert all(sid.startswith("conv_hit") for sid in result["ids"])
        assert len(result["ids"]) == 120
        assert all(page["has_more"] is bool(page.get("cursor")) for page in result["pages"])

    @pytest.mark.asyncio
    async def test_deleted_session_does_not_reappear_later_page(self):
        """§50（会话为硬删 + tombstone）：删除的会话不得在后续页复活。"""
        agent = SessionTestAgent()
        seeded = await seed_session_docs(agent, 205, prefix="conv_del")
        await agent.session_manager.delete(seeded[0])
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert seeded[0] not in result["ids"]
        assert set(result["ids"]) == (_expected_ids(seeded) - {seeded[0]})

    @pytest.mark.asyncio
    async def test_response_fields_do_not_drift_across_pages(self):
        """§46/§48：同一类记录在**每一页**都带同样的字段与同样的角色绑定。

        （可选字段本身可缺省——无标题/无绑定是合法形态；这里比的是同类记录的
        字段集与绑定值不随页码漂移。）
        """
        agent = SessionTestAgent()
        for i in range(205):
            await agent.backend.insert_one("sessions", {
                "session_id": f"conv_cb_{i:03d}",
                "title": f"绑定{i:03d}",
                "messages": [],
                "character_binding": {
                    "character_id": "chr_builtin_cyrene",
                    "instance_id": f"chi_{i:03d}",
                },
                "updated_at": f"2026-08-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
            })
        client = await _client_for(agent)
        try:
            result = await paginate_sessions(client)
        finally:
            await client.close()

        assert len(result["ids"]) == 206
        shapes: set[tuple[str, ...]] = set()
        seen = 0
        for page in result["pages"]:
            for item in page["sessions"]:
                if not str(item["conversationId"]).startswith("conv_cb_"):
                    continue
                seen += 1
                shapes.add(tuple(sorted(item.keys())))
                assert item["character"]["characterId"] == "chr_builtin_cyrene"
                assert item["character"]["characterInstanceId"].startswith("chi_")
        assert seen == 205, "跨页读取丢了记录"
        assert len(shapes) == 1, f"同类记录逐页字段集不一致：{shapes}"
