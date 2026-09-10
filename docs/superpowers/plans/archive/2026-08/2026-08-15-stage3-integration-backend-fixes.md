# 阶段 3 联调整改实现计划：sticker 搜索 + 日期 fail-fast + 逐文件 kind

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端补齐三处阶段 3 真实联调契约：`/api/search/messages` 支持 `category=sticker`、日期参数 handler 层 fail-fast（`YYYY-MM-DD`）、`/api/uploads` 逐文件 `kind` 配对（兼容旧单 kind 形态）。

**Architecture:** 在 `frontend_contract/rest.py` 新增 `SearchCategory`（8 值）/`UploadKind`（4 值）StrEnum 作共享白名单单源（不被 ContractModel 字段引用 → TS 快照零漂移）；`session_routes.py`/`message_repo.py`/`upload_routes.py` 三处字符串集合改为枚举值集合；`upload_routes.py` 收集段从标量 `kind` 重构为「邻接配对 + 单 kind 特判」逐文件 hint。

**Tech Stack:** Python 3.11+ / aiohttp / pydantic / pytest（pytest-aiohttp）/ StrEnum / frontend_contract 契约单源。

**Spec:** `docs/superpowers/specs/archive/2026-08/2026-08-15-stage3-integration-backend-fixes-design.md`（上游需求锚点：`docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端整改清单.md`）

## Global Constraints

- **契约零响应形状变更**：`SearchMessageItem`/`AttachmentInfo` 字段不变（spec D9）；新增枚举**不被任何 ContractModel 字段引用**——`gen_ts._referenced_enums`（gen_ts.py:111-126）只收集被字段引用的枚举，故 `make check-types` 必须无漂移。
- **白名单统一用值集合** `frozenset(m.value for m in SearchCategory)`，**不用类成员 `in`**（`"image" in SearchCategory` 在 Python 3.11 为 False / 3.12 为 True，版本差异）；分支相等比较可用 `category == SearchCategory.LINK`（StrEnum 继承 str，与裸 str `==` 跨版本安全）。
- **sticker 不进 UploadKind**（保持 4 值 image/video/audio/file——贴图走 `/api/stickers`，spec D7）。
- **P1-01 时序**：后端日期 fail-fast 与前端 FE-S3-INT-P0-01 同步放行（本 plan 只做后端；前端当前发 ISO 时间戳，后端先行会使其日期查询 400）。
- **禁相对导入**（ruff TID252），一律绝对导入。
- **测试 marker**：契约枚举测试 `unit`；REST/upload 测试 `service`（现有 `pytestmark = pytest.mark.service`）。

---

### Task 1: 契约层枚举 SearchCategory + UploadKind（frontend_contract）

**Files:**
- Modify: `frontend_contract/rest.py`（StrEnum 区，`FinishReason`/`Role` 附近）
- Create: `frontend_contract/tests/test_rest_enums.py`

**Interfaces:**
- Produces: `SearchCategory`（StrEnum，8 值 text/image/link/video/audio/file/tool/sticker）、`UploadKind`（StrEnum，4 值 image/video/audio/file）——Task 2 消费 `SearchCategory`，Task 4 消费 `UploadKind`。

- [ ] **Step 1: Write the failing test**

```python
"""契约层枚举测试：SearchCategory / UploadKind（marker=unit，阶段 3 联调整改 D3-B）。

新增枚举不被任何 ContractModel 字段引用 → gen_ts/gen_schema 不收集 → TS/schema/raw_ts
快照不变（Task 1 Step 5 用 make check-types 验证）；本测试锁定枚举值集合（wire 单源）。
"""
from enum import StrEnum

import pytest

from frontend_contract.rest import SearchCategory, UploadKind

pytestmark = pytest.mark.unit


class TestSearchCategory:
    def test_value_set_8(self):
        assert {m.value for m in SearchCategory} == {
            "text", "image", "link", "video", "audio", "file", "tool", "sticker",
        }

    def test_is_str_enum(self):
        assert issubclass(SearchCategory, StrEnum)

    def test_sticker_member(self):
        assert SearchCategory.STICKER == "sticker"
        assert SearchCategory("sticker") is SearchCategory.STICKER


class TestUploadKind:
    def test_value_set_4(self):
        assert {m.value for m in UploadKind} == {"image", "video", "audio", "file"}

    def test_sticker_not_included(self):
        assert "sticker" not in {m.value for m in UploadKind}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_rest_enums.py -v`
Expected: FAIL（`ImportError: cannot import name 'SearchCategory' from 'frontend_contract.rest'`）

- [ ] **Step 3: Write minimal implementation**

在 `frontend_contract/rest.py` 的 StrEnum 区（`class Role(StrEnum)` 之前或之后均可，保持 StrEnum 定义聚簇）新增：

```python
class SearchCategory(StrEnum):
    """GET /api/search/messages category 白名单（wire 值，8 取值）。"""
    TEXT = "text"
    IMAGE = "image"
    LINK = "link"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    TOOL = "tool"
    STICKER = "sticker"


class UploadKind(StrEnum):
    """POST /api/uploads kind 字段合法值（4 取值；sticker 贴图走 /api/stickers，不落附件）。"""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest frontend_contract/tests/test_rest_enums.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Verify contract gate no drift**

Run: `make check-types`
Expected: 全绿——新增枚举未被字段引用，`contract.generated.ts`/`contract.schema.json`/`contract.raw.ts` 及 golden frames 无漂移。（若漂移：说明枚举被某字段引用，回退。）

- [ ] **Step 6: Commit**

```bash
git add frontend_contract/rest.py frontend_contract/tests/test_rest_enums.py
git commit -m "feat(contract): SearchCategory/UploadKind 契约枚举（阶段 3 联调整改 D3-B）"
```

---

### Task 2: P0-01 搜索支持 category=sticker（枚举化 + repo）

**Files:**
- Modify: `core/input/session_routes.py`（`_RESOURCE_CATEGORIES` :43、search 白名单 :653-655、import）
- Modify: `core/storage/message_repo.py`（`_CATEGORY_PART_TYPES` :23、`query_search` :407-423、docstring :383、import）
- Test: `core/input/tests/test_session_routes.py`（新增 `TestSearchSticker`，import 补 `StickerPart`）

**Interfaces:**
- Consumes: `SearchCategory`（Task 1）
- Produces: `_SEARCH_CATEGORY_VALUES = frozenset(m.value for m in SearchCategory)`（session_routes 模块级）、`_RESOURCE_CATEGORIES = _SEARCH_CATEGORY_VALUES - {"text", "tool"}`、repo `_CATEGORY_PART_TYPES = frozenset(m.value for m in SearchCategory) - {"tool"}`

- [ ] **Step 1: Write the failing test**

在 `core/input/tests/test_session_routes.py` 顶部 import 增加 `StickerPart`（现有 `from core.schema.part import TextPart, ImagePart, VideoPart, FilePart, AudioPart` → 加 `StickerPart`），文件末尾新增：

```python
@pytest.mark.service
class TestSearchSticker:
    """P0-01：/api/search/messages?category=sticker（spec 阶段 3 联调整改）。

    前端 HistoryCategory.sticker 已冻结 → category=sticker；后端白名单 + repo 集合补
    sticker，走 {"type": "sticker"} $elemMatch。纯贴图消息 snippet 空串合法。
    """

    @staticmethod
    async def _seed_stickers(agent, sid: str, sticker_ids: list[str]) -> None:
        """按序播种纯贴图 user 消息（StickerPart）。"""
        for i, st_id in enumerate(sticker_ids):
            await _append(agent, sid, UserMessage(
                content=[StickerPart(sticker_id=st_id)],
                message_id=MessageId(f"msg_stk_{sid}_{i}"),
                timestamp=f"2026-08-01T09:{i:02d}:00+00:00",
            ))

    @pytest.mark.asyncio
    async def test_sticker_category_200(self, aiohttp_client):
        """category=sticker 返回 200（不再 400）且命中贴图消息。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk1")
        await self._seed_stickers(agent, "conv_stk1", ["st_001", "st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={"category": "sticker"})
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_sticker_snippet_empty(self, aiohttp_client):
        """纯贴图消息 snippet 空串（前端按文本行 + category chip 展示，spec 2.2.5）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk2")
        await self._seed_stickers(agent, "conv_stk2", ["st_001"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        data = await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json()
        assert data["items"][0]["snippet"] == ""

    @pytest.mark.asyncio
    async def test_sticker_conversation_scope(self, aiohttp_client):
        """当前会话范围只返回该会话贴图消息。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_a")
        await _create_session(agent, "conv_stk_b")
        await self._seed_stickers(agent, "conv_stk_a", ["st_001"])
        await self._seed_stickers(agent, "conv_stk_b", ["st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        data = await (await client.get("/api/search/messages", params={
            "category": "sticker", "conversationId": "conv_stk_a"})).json()
        assert [i["conversationId"] for i in data["items"]] == ["conv_stk_a"]

    @pytest.mark.asyncio
    async def test_sticker_cross_session_paginate(self, aiohttp_client):
        """全部会话跨会话 + cursor 第二页无重复无遗漏。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_c1")
        await _create_session(agent, "conv_stk_c2")
        await self._seed_stickers(agent, "conv_stk_c1", ["st_001", "st_002"])
        await self._seed_stickers(agent, "conv_stk_c2", ["st_003", "st_004"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        p1 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "limit": "2"})).json()
        assert len(p1["items"]) == 2 and p1["has_more"] is True
        p2 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "limit": "2", "cursor": p1["cursor"]})).json()
        ids = [i["messageId"] for i in p1["items"]] + [i["messageId"] for i in p2["items"]]
        assert len(set(ids)) == 4, f"cursor 分页重复或遗漏: {ids}"

    @pytest.mark.asyncio
    async def test_sticker_text_plus_sticker_matches(self, aiohttp_client):
        """文字+贴图消息命中；纯贴图 + 关键词 query 不命中（无 text part）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_mix")
        await _append(agent, "conv_stk_mix", UserMessage(
            content=[TextPart(content="看看这个"), StickerPart(sticker_id="st_001")],
            message_id=MessageId("msg_stk_mix0"),
            timestamp="2026-08-01T09:00:00+00:00",
        ))
        await self._seed_stickers(agent, "conv_stk_mix", ["st_002"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        # 无 query：文字+贴图 与 纯贴图 都命中
        data = await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json()
        assert len(data["items"]) == 2
        # 有 query：纯贴图不命中，文字+贴图命中（text part 命中关键词）
        data2 = await (await client.get("/api/search/messages", params={
            "category": "sticker", "query": "看看"})).json()
        assert [i["messageId"] for i in data2["items"]] == ["msg_stk_mix0"]

    @pytest.mark.asyncio
    async def test_sticker_items_locatable(self, aiohttp_client):
        """返回项非空 conversationId/messageId/timestamp（可跳转来源/context）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_loc")
        await self._seed_stickers(agent, "conv_stk_loc", ["st_001"])
        app = _make_app(agent)
        client = await aiohttp_client(app)
        item = (await (await client.get(
            "/api/search/messages", params={"category": "sticker"})).json())["items"][0]
        assert item["conversationId"] and item["messageId"] and item["timestamp"]

    @pytest.mark.asyncio
    async def test_sticker_unknown_category_still_400(self, aiohttp_client):
        """未知 category 仍 400——加入 sticker 不放宽为任意字符串。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_stk_unk")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={"category": "bogus"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -k TestSearchSticker -v`
Expected: FAIL（`test_sticker_category_200` 400 `CONTRACT_VALIDATION`——白名单无 sticker）

- [ ] **Step 3: Write minimal implementation**

`core/input/session_routes.py`：

1. 顶部 import 增加（现有 `from frontend_contract.rest import FinishReason, ProtocolError, Role`）：
```python
from frontend_contract.rest import FinishReason, ProtocolError, Role, SearchCategory
```
2. 模块级常量（`:43` `_RESOURCE_CATEGORIES` 行处）替换：
```python
# search/资源 category 白名单单源（D3-B）：值集合（wire str 成员判断，跨 3.11/3.12 无差异）。
_SEARCH_CATEGORY_VALUES = frozenset(m.value for m in SearchCategory)
# 资源索引类别 = search 白名单 - {text（走搜索）, tool（role 特判）}
_RESOURCE_CATEGORIES = _SEARCH_CATEGORY_VALUES - {"text", "tool"}
```
（删除原 `_RESOURCE_CATEGORIES = {"image", "link", "video", "audio", "file", "sticker"}`）
3. search handler 白名单（`:653-655`）替换：
```python
if category is not None and category not in _SEARCH_CATEGORY_VALUES:
    return _error_response("CONTRACT_VALIDATION", f"不支持的 category: {category}")
```

`core/storage/message_repo.py`：

1. 顶部 import 增加（文件已有 `import re`、`from zoneinfo import ZoneInfo` 等）：
```python
from frontend_contract.rest import SearchCategory
```
2. `_CATEGORY_PART_TYPES`（`:23`）替换：
```python
# query_search 支持的 category 取值 → content part 类型 / role 映射（D3-B 枚举派生单源）。
# tool 特殊：无 part type，走 role=tool；link 特殊：archive 无独立 link part，URL 正则特判。
_CATEGORY_PART_TYPES = frozenset(m.value for m in SearchCategory) - {"tool"}
```
3. `query_search` 分支（`:407-412`）改枚举比较（`category` 仍为 `str | None`，StrEnum `==` 与裸 str 安全）：
```python
if category == SearchCategory.LINK:
    elem_type = {"type": "text",
                 "content": {"$regex": r"(https?://|www\.)", "$options": "i"}}
elif category in _CATEGORY_PART_TYPES - {"text", "link"}:
    elem_type = {"type": category}
elif category == SearchCategory.TEXT and not query:
    elem_type = {"type": "text"}
```
（`:423` 的 `if category == "tool":` 改为 `if category == SearchCategory.TOOL:`）
4. docstring `:383` `category: text|image|link|video|audio|file|tool` → `category: text|image|link|video|audio|file|tool|sticker`。

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -k TestSearchSticker -v`
Expected: PASS（7 passed）
随后回归（MED-2：repo 行为锁定网 + handler 层）：
`conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py core/storage/tests/test_message_repo.py -q`
Expected: 全绿（既有搜索/资源/日期测试 + repo 的 image/tool/link 分类、日期、keyword+category 组合用例不回归）

- [ ] **Step 5: Commit**

```bash
git add core/input/session_routes.py core/storage/message_repo.py core/input/tests/test_session_routes.py
git commit -m "feat(search): /api/search/messages 支持 category=sticker（P0-01，D3-B 枚举化）"
```

---

### Task 3: P1-01 日期参数 handler 层 fail-fast

**Files:**
- Modify: `core/input/session_routes.py`（新增 `_validate_date_param` + handler 接线 :659-663 附近 + `from datetime import date`）
- Test: `core/input/tests/test_session_routes.py`（新增 `TestSearchDateFailFast`）

**Interfaces:**
- Consumes: `_error_response`（session_routes 既有）
- Produces: `_validate_date_param(name: str, value: str) -> str | None`（非法返回公开错误消息，合法返回 None）

- [ ] **Step 1: Write the failing test**

`core/input/tests/test_session_routes.py` 文件末尾新增：

```python
@pytest.mark.service
class TestSearchDateFailFast:
    """P1-01：日期参数 handler 层 fail-fast（spec 阶段 3 联调整改）。

    非 YYYY-MM-DD → 400 CONTRACT_VALIDATION（不再吞 200 空结果）；未知时区仍
    INVALID_TIMEZONE。与前端 FE-S3-INT-P0-01（改发自然日）同步放行。
    """

    @pytest.mark.asyncio
    async def test_date_iso_datetime_400(self, aiohttp_client):
        """ISO datetime 负例（前端当前错误形态）→ 400 CONTRACT_VALIDATION。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_iso")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-08-13T16:00:00.000Z"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_compact_400(self, aiohttp_client):
        """紧凑日期 20260814 → 400（fromisoformat 接受但契约要求 YYYY-MM-DD）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_cpt")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_to": "20260814"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_week_calendar_400(self, aiohttp_client):
        """ISO 周历 2026-W33-4 → 400。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_wk")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-W33-4"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_invalid_calendar_400(self, aiohttp_client):
        """非法日历 2026-13-99 → 400（正则过、fromisoformat 拒）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_bad")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "query": "x", "date_from": "2026-13-99"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_date_valid_range_still_200(self, aiohttp_client):
        """合法自然日 range 仍 200 且命中（UTC 当天）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_ok")
        await _seed_text_messages(agent, "conv_date_ok", 2, prefix="day")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "date_to": "2026-08-01",
            "timezone": "UTC"})
        assert resp.status == 200
        assert len((await resp.json())["items"]) == 2

    @pytest.mark.asyncio
    async def test_date_from_only_and_to_only(self, aiohttp_client):
        """仅 from / 仅 to 均 200 且半开区间命中。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_ft")
        await _seed_text_messages(agent, "conv_date_ft", 1, prefix="edge")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        r1 = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "timezone": "UTC"})
        r2 = await client.get("/api/search/messages", params={
            "date_to": "2026-08-01", "timezone": "UTC"})
        assert r1.status == r2.status == 200

    @pytest.mark.asyncio
    async def test_date_unknown_timezone_still_400(self, aiohttp_client):
        """未知时区保持 400 INVALID_TIMEZONE（不与日期格式校验混淆）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_tz")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-01", "timezone": "Mars/Olympus"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "INVALID_TIMEZONE"

    @pytest.mark.asyncio
    async def test_date_dst_boundary(self, aiohttp_client):
        """DST 切换日自然日 → 23h 半开 UTC 区间命中（America/New_York 2026-03-08）。

        3/8 美东 = [2026-03-08T05:00:00Z, 2026-03-09T04:00:00Z)（凌晨跳 1 小时）：
        当天 12:00Z 命中；3/7 美东 23:30（= 3/8 04:30Z）不命中。
        """
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_dst")
        await _append(agent, "conv_date_dst", UserMessage(
            content=[TextPart(content="dst")],
            message_id=MessageId("msg_dst1"),
            timestamp="2026-03-08T12:00:00+00:00",  # 区间内
        ))
        await _append(agent, "conv_date_dst", UserMessage(
            content=[TextPart(content="dst-eve")],
            message_id=MessageId("msg_dst0"),
            timestamp="2026-03-08T04:30:00+00:00",  # 3/7 美东 23:30，区间外
        ))
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-03-08", "date_to": "2026-03-08",
            "timezone": "America/New_York"})
        assert resp.status == 200
        items = (await resp.json())["items"]
        assert [i["messageId"] for i in items] == ["msg_dst1"]

    @pytest.mark.asyncio
    async def test_date_from_gt_to_returns_empty_200(self, aiohttp_client):
        """date_from > date_to（倒置）保持 200 空结果（D2，不扩大范围，回归保险）。"""
        agent = _FakeAgent()
        await _create_session(agent, "conv_date_inv")
        await _seed_text_messages(agent, "conv_date_inv", 1, prefix="inv")
        app = _make_app(agent)
        client = await aiohttp_client(app)
        resp = await client.get("/api/search/messages", params={
            "date_from": "2026-08-02", "date_to": "2026-08-01", "timezone": "UTC"})
        assert resp.status == 200
        assert (await resp.json())["items"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -k TestSearchDateFailFast -v`
Expected: FAIL（ISO datetime 当前返回 200 空页而非 400）

- [ ] **Step 3: Write minimal implementation**

`core/input/session_routes.py`：

1. 顶部 import 增加（现有 `import re`、`from zoneinfo import ZoneInfo`）：
```python
from datetime import date
```
2. 在参数解析 helpers 区（`_parse_limit` 附近）新增：
```python
def _validate_date_param(name: str, value: str) -> str | None:
    """严格校验 date_from/date_to。合法返回 None，非法返回公开错误消息。

    双重校验：正则拒非 YYYY-MM-DD 形态（紧凑 20260814 / ISO 周历 2026-W33-4 /
    ISO datetime），fromisoformat 拒非法日历（2026-13-99）。放于 handler 层（进入
    repository 前）fail-fast，不再吞 200 空结果（spec P1-01）。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{name} 必须为 YYYY-MM-DD（收到 {value!r}）"
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{name} 不是有效日期（收到 {value!r}）"
    return None
```
3. search handler 中，在 `ZoneInfo(tz_name)` 校验（`:659-663`）**之前**插入：
```python
for _name, _value in (("date_from", date_from), ("date_to", date_to)):
    if _value:
        _err = _validate_date_param(_name, _value)
        if _err:
            return _error_response("CONTRACT_VALIDATION", _err, 400)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -k TestSearchDateFailFast -v`
Expected: PASS（9 passed）
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_session_routes.py -q`
Expected: 全绿（既有 `test_search_invalid_timezone_400`/`test_search_date_range` 不回归——均用合法严格格式）

- [ ] **Step 5: Commit**

```bash
git add core/input/session_routes.py core/input/tests/test_session_routes.py
git commit -m "feat(search): 日期参数 handler 层 fail-fast（P1-01，非 YYYY-MM-DD → 400）"
```

---

### Task 4: P0-02 /api/uploads 逐文件 kind 配对

**Files:**
- Modify: `core/input/upload_routes.py`（收集段 :142-200、阶段 A :205-242、`_ALLOWED_KINDS` :380 → `_UPLOAD_KIND_VALUES`）
- Test: `core/input/tests/test_upload.py`（新增 `_post_kinds` helper + `TestPerFileKind` 类）

**Interfaces:**
- Consumes: `UploadKind`（Task 1）
- Produces: `_UPLOAD_KIND_VALUES = frozenset(m.value for m in UploadKind)`（upload_routes 模块级，替换 `_ALLOWED_KINDS`）；收集结果 `files: list[tuple[str, bytes, str | None]]`（filename, bytes, kind_hint）
- 测试魔数（WEBM/MP4/PDF）落位见 Step 1（test_upload.py 模块级，Elysia env 实测 filetype 可识别）

- [ ] **Step 1: Write the failing test**

先在 `core/input/tests/test_upload.py` 模块级（`PNG`/`TEXT` 常量旁）定义魔数常量（LOW-4 落位明确）：

```python
# P0-02 新增魔数（Elysia env 实测 filetype 可识别）
WEBM = bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282847765626d4287810442858102")  # video/webm
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + b"mp42isom"  # video/mp4
PDF = b"%PDF-1.4\n" + b"1 0 obj\n<<>>\nendobj\n"  # application/pdf
```

在 `core/input/tests/test_upload.py` 的 `_post` helper 之后新增 `_post_kinds`：

```python
async def _post_kinds(client, parts):
    """构造逐文件 kind 的 multipart（前端 real-backend.ts 同款：每文件一组 kind+files）。

    Args:
        parts: list[(kind, filename, content)]，kind 为 None 表示该文件无 kind（魔数判定）。
    """
    form = FormData()
    for kind, name, content in parts:
        if kind is not None:
            form.add_field("kind", kind)
        form.add_field("files[]", content, filename=name,
                       content_type="application/octet-stream")
    return await client.post("/api/uploads", data=form)
```

文件末尾新增：

```python
class TestPerFileKind:
    """P0-02：/api/uploads 逐文件 kind（spec 阶段 3 联调整改；前端每文件一组 kind+files）。

    覆盖清单 3.3：混合批次逐项 kind、webm 消歧、files/files[] 兼容、单 kind 旧形态、
    悬空/邻接/未知 400、逐 hint 大小预检、魔数权威、整批回滚。
    """

    @pytest.mark.asyncio
    async def test_mixed_kinds_each_correct(self, make_app, aiohttp_client):
        """image+video+file 混合批次成功且逐项 kind 正确。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("video", "b.mp4", MP4), ("file", "c.pdf", PDF),
        ])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert [a["kind"] for a in data["attachments"]] == ["image", "video", "file"]
        assert [a["mime"] for a in data["attachments"]] == [
            "image/png", "video/mp4", "application/pdf"]

    @pytest.mark.asyncio
    async def test_webm_audio_video_disambiguated(self, make_app, aiohttp_client):
        """audio/webm + video/webm 同批分别归 audio/video（hint tiebreaker）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("audio", "voice.webm", WEBM), ("video", "clip.webm", WEBM),
        ])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["audio", "video"]

    @pytest.mark.asyncio
    async def test_files_and_files_array_both_work(self, make_app, aiohttp_client):
        """files 与旧 files[] 字段均可用（现有 _post 用 files[]；本 helper 亦 files[]）。
        files 单数形态：显式 FormData 用 name='files'。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("files", PNG, filename="single.png",
                       content_type="application/octet-stream")
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 200
        assert (await resp.json())["attachments"][0]["kind"] == "image"

    @pytest.mark.asyncio
    async def test_single_kind_multi_files_legacy(self, make_app, aiohttp_client):
        """旧形态：单 kind + 多文件 → 全部文件按该 kind 预检/归位（整批统一）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="a.png", content=PNG, kind="image",
                           extra_files=[("b.png", PNG)])
        assert resp.status == 200
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["image", "image"]

    @pytest.mark.asyncio
    async def test_dangling_kind_400(self, make_app, aiohttp_client):
        """尾部悬空 kind（kind 后无文件）→ 400 CONTRACT_VALIDATION。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("files[]", PNG, filename="a.png",
                       content_type="application/octet-stream")
        form.add_field("kind", "video")  # 悬空
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_adjacent_kind_400(self, make_app, aiohttp_client):
        """kind 未紧邻文件（kind,kind,files 连续）→ 400（顺序歧义，清单 3.3）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("kind", "video")  # 前一 kind 未被文件消费即来新 kind
        form.add_field("files[]", PNG, filename="a.png",
                       content_type="application/octet-stream")
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_unknown_kind_mixed_400(self, make_app, aiohttp_client):
        """混合批次含非法 kind → 400（sticker 非附件白名单保持）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("sticker", "st.png", PNG)])
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_big_video_not_blocked_by_last_image_limit(self, make_app, aiohttp_client):
        """逐 hint 预检：前大视频（>10MB 但 <50MB，hint=video）不被最后 image 10MB 误拒。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        big_mp4 = MP4 + b"\x00" * (12 * 1024 * 1024)  # 12MB video/mp4
        resp = await _post_kinds(client, [
            ("video", "big.mp4", big_mp4), ("image", "a.png", PNG)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["video", "image"]

    @pytest.mark.asyncio
    async def test_magic_beats_hint(self, make_app, aiohttp_client):
        """魔数与提示冲突时以服务端魔数归位为准（hint=video 的 PNG → image）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("video", "a.png", PNG)])
        assert resp.status == 200
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "image"
        assert att["mime"] == "image/png"

    @pytest.mark.asyncio
    async def test_no_kind_all_files_magic_detect(self, make_app, aiohttp_client):
        """全批无 kind（hint=None 合法路径）：全部文件按魔数判定（LOW-3 补洞）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            (None, "a.png", PNG), (None, "b.pdf", PDF)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["image", "file"]

    @pytest.mark.asyncio
    async def test_mixed_batch_failure_rolls_back(self, make_app, aiohttp_client, tmp_path):
        """混合批次中途失败完整回滚：第一文件成功写盘后第二文件记录写入失败。"""
        agent, app = make_app(fail_on_create=2)  # 第 2 个 create 抛 StorageUnavailableError
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("video", "b.mp4", MP4)])
        assert resp.status == 503
        assert (await resp.json())["code"] == "STORAGE_UNAVAILABLE"
        assert not agent._attachment_repo.records  # noqa: SLF001
        # 已写盘文件全部清理（不留孤儿）
        assert not list((tmp_path / "uploads" / "image").glob("*.png"))
        assert not list((tmp_path / "uploads" / "video").glob("*.mp4"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_upload.py -k TestPerFileKind -v`
Expected: 部分 FAIL——**真正先失败的四例**：`test_webm_audio_video_disambiguated`（标量 kind 覆盖 → 同批两 webm 同归一类，断言 `["audio","video"]` 不满足）、`test_dangling_kind_400`（现状悬空 kind 被吞 → 断言 400 不满足）、`test_adjacent_kind_400`（现状 kind,kind,files 静默配对 → 断言 400 不满足）、`test_big_video_not_blocked_by_last_image_limit`（标量 kind 用最后 image 10MB 预检大视频 → 误拒 413，断言 200 不满足）。
其余（混合 kind 归位、魔数权威、全批无 kind、files/files[]、单 kind 旧形态）在当前代码下即 PASS——这些是**行为锁定回归**，非本批新行为，验证其保持即通过。
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_upload.py -q`
Expected: 全绿（既有单 kind 用例不回归）

- [ ] **Step 3: Write minimal implementation**

`core/input/upload_routes.py`：

1. 顶部 `_ALLOWED_KINDS`（`:380`）替换为：
```python
_UPLOAD_KIND_VALUES = frozenset(m.value for m in UploadKind)
```
（import 增加 `from frontend_contract.rest import UploadKind`）
2. 收集段（`:142-192`）重构：
```python
    pending_kinds: list[str] = []   # 未消费 kind 栈（邻接配对：最近一个 kind 作用于其后首个文件）
    kind_count = 0                  # 批内 kind part 总数（单 kind 特判判定）
    files: list[tuple[str, bytes, str | None]] = []   # (原始文件名, 内容, kind_hint | None)
    max_read = max(_KIND_LIMITS.values())
    total_bytes = 0
    try:
        reader = await request.multipart()
    except web.HTTPRequestEntityTooLarge:
        raise
    except Exception:
        return _protocol_error_response("INVALID_REQUEST", "无效的 multipart 请求", 400)
    while True:
        try:
            field = await reader.next()
        except web.HTTPRequestEntityTooLarge:
            raise
        except Exception:
            return _protocol_error_response("INVALID_REQUEST", "multipart 解析失败", 400)
        if field is None:
            break
        field = cast(BodyPartReader, field)
        name = field.name or ""
        if name == "kind":
            raw = await field.read_chunk(65)
            if len(raw) > 64:
                return _protocol_error_response("INVALID_REQUEST", "kind 字段超长", 400)
            if isinstance(raw, (bytes, bytearray)):
                decoded = bytes(raw).decode("utf-8", errors="replace").strip()
            else:
                decoded = str(raw or "").strip()
            # 邻接判定（MED-2）：前一 kind 未被文件消费即来新 kind → 顺序歧义 400
            if pending_kinds:
                return _protocol_error_response(
                    "CONTRACT_VALIDATION", "kind 未紧邻文件", 400)
            pending_kinds.append(decoded)
            kind_count += 1
        elif name.startswith("files"):
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_read + 1))
                if len(data) > max_read:
                    return _protocol_error_response(
                        "REQUEST_TOO_LARGE", "单个文件过大", 413)
            total_bytes += len(data)
            if cms and total_bytes > cms:
                return _protocol_error_response(
                    "REQUEST_TOO_LARGE", "整体上传超过聚合上限", 413)
            hint = pending_kinds.pop() if pending_kinds else None
            files.append((getattr(field, "filename", "") or "attachment",
                          bytes(data), hint))
        # 其他字段忽略

    # 步骤 0：数量 ≤10（保留现有，MED-1）
    if len(files) > 10:
        return _protocol_error_response("TOO_MANY_FILES", "单次上传文件数超限（≤10）", 400)
    # 步骤 0.5：kind part 总数防御上限（D6，defense-in-depth）。
    # 实际不可达：邻接判定（kind 未紧邻文件即 400）+ 悬空 400 + ≤10 文件上限使
    # kind_count ≤ 11；保留兜底防未来放宽邻接判定时回归，不单测（LOW-2）。
    if kind_count > 100:
        return _protocol_error_response("CONTRACT_VALIDATION", "kind 字段数量超限", 400)
    # 步骤 0.6：尾部悬空 kind（kind 后无文件被消费）→ 400（D5）
    if pending_kinds:
        return _protocol_error_response(
            "CONTRACT_VALIDATION", "kind 后缺少对应文件", 400)
    # 步骤 0.7：单 kind 特判（D1-A）——旧形态「单 kind + 多文件」整批统一；
    # 此分支下 hint=None 不可达（单 kind 一律视为整批共同提示）
    if kind_count == 1 and files:
        single_kind = next(h for h in (f[2] for f in files) if h is not None)
        files = [(name, data, single_kind) for name, data, _ in files]
    # 步骤 0.8：每 hint 枚举校验（未知 kind → 400；sticker 非附件白名单）
    for _, _, hint in files:
        if hint is not None and hint not in _UPLOAD_KIND_VALUES:
            return _protocol_error_response("CONTRACT_VALIDATION", f"未知 kind: {hint}", 400)
```
3. 阶段 A 逐文件校验（`:205-242`）改造：`for filename_raw, data in files:` → `for filename_raw, data, hint in files:`，且：
   - `:207` `if kind and len(data) > _KIND_LIMITS[kind]:` → `if hint and len(data) > _KIND_LIMITS[hint]:`
   - `:236-237` `if mime == "video/webm": final_kind = "audio" if kind == "audio" else "video"` → `final_kind = "audio" if hint == "audio" else "video"`
   - 其余不变（`:239` 归位、`:240` 复核、`validated.append` 中 `final_kind` 等字段名不变）。

**注意**：删除原 `:194-197` 的 `if kind and kind not in _ALLOWED_KINDS` 块与 `_ALLOWED_KINDS` 定义（已被步骤 0.5-0.8 取代）；`hint == "audio"` 为 str 比较（hint 是 wire str），无需枚举比较。

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_upload.py -k TestPerFileKind -v`
Expected: PASS（11 passed）
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_upload.py -q`
Expected: 全绿（既有单 kind 用例 `test_kind_sticker_400`/`test_too_many_files_400`/`test_upload_kind_field_size_capped` 不回归）

- [ ] **Step 5: Verify lint**

Run: `conda run -n Elysia ruff check core/input/upload_routes.py core/input/tests/test_upload.py`
Expected: All passed（`hint` 逐文件解包无未使用变量——`validated` dict 构造仍用 `data`；确认 `_ALLOWED_KINDS` 已删除无残留引用）

- [ ] **Step 6: Commit**

```bash
git add core/input/upload_routes.py core/input/tests/test_upload.py
git commit -m "feat(upload): /api/uploads 逐文件 kind 配对 + 旧单 kind 兼容（P0-02）"
```

---

### Task 5: 全量门禁验证（收尾，spec §7.1/7.2）

**Files:** 无新文件（纯验证步骤）

- [ ] **Step 1: 全量快速测试（unit + service）**

Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short`
Expected: 全绿（新增 sticker/日期/逐文件用例 + 既有全部回归；`make test-quick` 等价）

- [ ] **Step 2: ruff 全量**

Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`
Expected: All passed

- [ ] **Step 3: pyright 全量（Elysia env）**

Run: `conda run -n Elysia pyright`
Expected: 0 errors（`make pyright` 等价；base env 会漏报，须 Elysia）

- [ ] **Step 4: 契约门禁复跑**

Run: `make check-types`
Expected: 全绿（Task 1 Step 5 已验，全量收尾复跑确认无漂移）

- [ ] **Step 5: 验收 grep（spec §7.5 可机检）**

Run: `grep -rn "_ALLOWED_KINDS" core/input/upload_routes.py`
Expected: 无输出（已替换为 `_UPLOAD_KIND_VALUES`）
Run: `grep -rn '"text", "image", "video", "audio", "file", "link", "tool"' core/input/session_routes.py`
Expected: 无输出（白名单已枚举化）

Task 5 不产生 commit（纯验证；执行完成后由批次流程的 Phase 4-5 跑 `make test-normal` 全量再确认）。

---

## Self-Review

- **Spec 覆盖**：D3-B（枚举）→ Task 1 + Task 2/4 消费；P0-01（白名单 + repo + sticker 测试 7 条）→ Task 2；P1-01（fail-fast + 日期测试）→ Task 3；P0-02（逐文件 kind + 测试 8 条 + 邻接/悬空/单 kind 特判/魔数权威/回滚）→ Task 4；验收 §7.5 grep（`_ALLOWED_KINDS` 删除）→ Task 4 Step 5；`make check-types` 无漂移 → Task 1 Step 5。无缺口。
- **占位符**：无 TBD/TODO；所有步骤含具体代码与预期。
- **类型一致性**：`SearchCategory`/`UploadKind`（Task 1）在 Task 2/4 消费；`_SEARCH_CATEGORY_VALUES`/`_RESOURCE_CATEGORIES`（Task 2）与 `_UPLOAD_KIND_VALUES`（Task 4）命名一致；`files` 三元组 `(filename, bytes, hint)` 在 Task 4 全流程一致；`_validate_date_param` 签名 `(str, str) -> str | None` 在 Task 3 唯一。

## Plan Review 记录（2026-08-15）

- code-review 第 1 轮（general-purpose agent）：结论 yes（无阻断项），7 条 issue 全部消化——
  **MED-1** 缺 DST 边界测试 → Task 3 补 `test_date_dst_boundary`（America/New_York 2026-03-08 23h 半开区间）；**MED-2** Task 2 回归漏 repo 测试 + 无全量门禁 → Task 2 Step 4 并入 `test_message_repo.py` + 新增 Task 5（test-quick/ruff/pyright/check-types/验收 grep）；**LOW-1** Task 4 Step 2 失败预期与现状不符（标量 kind 不影响非 webm 归位）→ 修正为四例先失败 + 其余为行为锁定回归；**LOW-2** step 0.5 `kind_count>100` 不可达 → 标注 defense-in-depth（邻接+悬空+≤10 已限 kind_count≤11，不单测）；**LOW-3** hint=None 路径无测试 → 补 `test_no_kind_all_files_magic_detect`；**LOW-4** 魔数常量落位未明 → Step 1 明确模块级落位；**LOW-5** D2 倒置无回归 → 补 `test_date_from_gt_to_returns_empty_200`。agent 验证事实基线（行号/魔数 filetype 识别/fromisoformat 行为/pyright 类型/`_ALLOWED_KINDS` 引用/gen_ts 收集逻辑/依赖方向）全部核对通过，无残留问题。
