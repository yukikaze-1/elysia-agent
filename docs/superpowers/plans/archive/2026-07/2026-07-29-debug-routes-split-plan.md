# Debug Routes 拆分 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `core/input/debug_routes.py`（802 行，23 handler，全部嵌套在 `register_debug_routes()` 闭包内）拆分为 `core/input/debug_routes/` package（7 个模块），handler 从闭包提升为顶层 async 函数，业务逻辑不变。

**Architecture:** 纯结构重构。原 `register_debug_routes()` 内的闭包 handler 按资源分组提升为各子模块的顶层函数，通过 `request.app["agent"]` 获取 Agent。`debug_routes.py` → `debug_routes/__init__.py` 作为入口，调用各子模块的 `register_*` 函数。Python import 机制自动解析 `from .debug_routes import register_debug_routes`（`.py` → package `__init__`），调用方零改动。

**Tech Stack:** Python 3.11+, aiohttp

## Global Constraints

- 不变更任何 handler 的业务逻辑、参数校验、错误处理
- 不改变路由路径或 HTTP 方法
- 不新增测试（纯结构重构）
- Handler 通过 `request.app["agent"]` 获取 Agent（替代闭包捕获 `app`）
- `server.py` 的 import 路径 `from .debug_routes import register_debug_routes` 保持不变

---

### Task 1: 创建 `_common.py` — 共享 helper

**Files:**
- Create: `core/input/debug_routes/_common.py`
- Create: `core/input/debug_routes/__init__.py`（空文件，package marker）

**Interfaces:**
- Produces: `sessions_to_client(msgs: list[Message] | list[dict]) -> list[dict]`

- [ ] **Step 1: 创建 `debug_routes/` 目录结构**

```bash
mkdir -p core/input/debug_routes
```

- [ ] **Step 2: 创建 `core/input/debug_routes/__init__.py`**（空 package marker）

```python
# debug_routes/ package
```

- [ ] **Step 3: 创建 `core/input/debug_routes/_common.py`**

从原 `debug_routes.py:29-63` 提取 `_sessions_to_client()`，去掉前导下划线改为 public helper。需要添加原文件顶部的 import：

```python
"""Debug routes 共享 helper。"""

from core.schema.message import Message


def sessions_to_client(msgs: list[Message] | list[dict]) -> list[dict]:
    """将消息列表转为 session 详情前端兼容格式。接受 Message 或原始 dict。

    Args:
        msgs: Message 对象列表或原始 dict 列表。

    Returns:
        list[dict]: 前端兼容的消息字典列表，每项包含 role、timestamp、parts 等字段。
    """
    result = []
    for m in msgs:
        if isinstance(m, Message):
            entry = m.model_dump(mode="json")
        else:
            entry = dict(m)
        item: dict = {"role": entry.get("role", "?"),
                      "timestamp": entry.get("timestamp", ""),
                      "content_type": entry.get("content_type", {})}
        content = entry.get("content", "")
        if isinstance(content, list):
            item["parts"] = [
                {"content": p.get("content", "") if isinstance(p, dict) else getattr(p, "content", "")}
                for p in content
                if (isinstance(p, dict) and p.get("type") == "text")
                   or (not isinstance(p, dict) and getattr(p, "type", None) == "text")
            ]
            item["content"] = content
        elif isinstance(content, str):
            item["parts"] = [{"content": content}]
        elif "parts" in entry:
            item["parts"] = entry["parts"]
        else:
            item["parts"] = [{"content": str(content)}]
        result.append(item)
    return result
```

- [ ] **Step 4: 验证 import**

```bash
python -c "from core.input.debug_routes._common import sessions_to_client; print('OK')"
```

- [ ] **Step 5: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 6: Commit**

```bash
git add core/input/debug_routes/__init__.py core/input/debug_routes/_common.py
git commit -m "refactor: extract _common.py from debug_routes — sessions_to_client helper"
```

---

### Task 2: 创建 `overview.py` — Overview + DB 状态

**Files:**
- Create: `core/input/debug_routes/overview.py`

**Interfaces:**
- Produces: `register_overview_routes(app: web.Application) -> None`

- [ ] **Step 1: 创建 `core/input/debug_routes/overview.py`**

从原 `debug_routes.py:65-121` 提取 `debug_api_handler` + `db_status_handler`。Handler 从闭包提升为顶层函数，通过 `request.app["agent"]` 获取 Agent。

```python
"""Debug overview routes: aggregated info + database status."""

import logging

from aiohttp import web

from core.debug.info import collect, asdict

logger = logging.getLogger(__name__)


def register_overview_routes(app: web.Application) -> None:
    """注册 overview 路由。"""
    app.router.add_get("/debug/api", debug_api_handler)
    app.router.add_get("/debug/api/db-status", db_status_handler)


async def debug_api_handler(request: web.Request) -> web.Response:
    """Return aggregated debug info for the overview dashboard.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: JSON with all debug panels (system, memory, scheduler, etc.).
    """
    info = await collect(request.app["agent"])
    return web.json_response(asdict(info))


async def db_status_handler(request: web.Request) -> web.Response:
    """Return database status (LTM count, session count, archive count, health checks).

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: JSON with ltm/session/archive status and health_check results.
    """
    agent = request.app["agent"]
    ltm = agent._long_term
    return web.json_response({
        "ltm": {
            "count": ltm.count,
            "milvus_ok": await agent._memory_repo.health_check(),
            "dirty_count": len(ltm._dirty_ids),
        },
        "session": {
            "count": 1,  # 当前只有单个 session
            "current_id": str(agent._session.session_id),
            "mongodb_ok": await agent._session_repo.health_check(),
        },
        "archive": {
            "count": await agent._message_repo.count(),
            "mongodb_ok": await agent._message_repo.health_check(),
        },
    })
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes.overview import register_overview_routes; print('OK')"
```

- [ ] **Step 3: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/overview.py
git commit -m "refactor: extract overview.py — /debug/api + /debug/api/db-status"
```

---

### Task 3: 创建 `ltm.py` — LTM CRUD

**Files:**
- Create: `core/input/debug_routes/ltm.py`

**Interfaces:**
- Produces: `register_ltm_routes(app: web.Application) -> None`

- [ ] **Step 1: 创建 `core/input/debug_routes/ltm.py`**

从原 `debug_routes.py:92-245` 提取 5 个 LTM handler（list/insert/delete_single/update/clear）。

```python
"""Debug LTM routes: CRUD for long-term memory entries."""

import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


def register_ltm_routes(app: web.Application) -> None:
    """注册 LTM CRUD 路由。"""
    app.router.add_get("/debug/api/ltm", ltm_list_handler)
    app.router.add_post("/debug/api/ltm", ltm_insert_handler)
    app.router.add_delete("/debug/api/ltm/{entry_id}", ltm_delete_single_handler)
    app.router.add_put("/debug/api/ltm/{entry_id}", ltm_update_handler)
    app.router.add_delete("/debug/api/ltm", ltm_clear_handler)


async def ltm_list_handler(request: web.Request) -> web.Response:
    """List LTM entries with optional search and type filter.

    Args:
        request: aiohttp Request, query params: search, type, limit, offset。

    Returns:
        web.Response: {"total": ..., "entries": [...]}。
    """
    agent = request.app["agent"]
    search = request.query.get("search", "").strip()
    mem_type = request.query.get("type", "").strip()
    try:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return web.json_response({"error": "无效的分页参数"}, status=400)

    if agent._long_term is None:
        return web.json_response({"error": "长期记忆未初始化"}, status=503)
    entries = agent._long_term._entries
    if search:
        entries = [e for e in entries if search in e.text]
    if mem_type:
        entries = [e for e in entries if e.memory_type == mem_type]

    total = len(entries)
    page = entries[offset:offset + limit]
    return web.json_response({
        "total": total,
        "entries": [
            {
                "id": e.id,
                "text": e.text,
                "memory_type": e.memory_type,
                "poignancy": e.poignancy,
                "timestamp": e.timestamp,
                "metadata": e.metadata,
            }
            for e in page
        ],
    })


async def ltm_insert_handler(request: web.Request) -> web.Response:
    """Insert a new LTM entry.

    Args:
        request: aiohttp Request, JSON body: text, memory_type, poignancy。

    Returns:
        web.Response: {"ok": True} or error.
    """
    agent = request.app["agent"]
    try:
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"error": "text 不能为空"}, status=400)
        mem_type = body.get("memory_type", "note")
        poignancy = int(body.get("poignancy", 5))
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "无效的请求体"}, status=400)

    await agent._long_term.insert(text, memory_type=mem_type, poignancy=poignancy)
    logger.info("DEBUG API: ltm_insert type=%s, text_len=%d, poignancy=%d", mem_type, len(text), poignancy)
    return web.json_response({"ok": True})


async def ltm_delete_single_handler(request: web.Request) -> web.Response:
    """Delete a single LTM entry by ID.

    Args:
        request: aiohttp Request, URL path: /{entry_id}。

    Returns:
        web.Response: {"ok": True/False}。
    """
    agent = request.app["agent"]
    entry_id = request.match_info.get("entry_id", "")
    if not entry_id:
        return web.json_response({"error": "缺少 entry_id"}, status=400)
    ok = await agent._long_term.delete(entry_id)
    logger.warning("DEBUG API: ltm_delete entry_id=%s, ok=%s", entry_id, ok)
    return web.json_response({"ok": ok})


async def ltm_update_handler(request: web.Request) -> web.Response:
    """Update an LTM entry's text by ID.

    Args:
        request: aiohttp Request, URL path: /{entry_id}, JSON body: text。

    Returns:
        web.Response: {"ok": True/False}。
    """
    agent = request.app["agent"]
    entry_id = request.match_info.get("entry_id", "")
    if not entry_id:
        return web.json_response({"error": "缺少 entry_id"}, status=400)
    body = await request.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        return web.json_response({"error": "text 不能为空"}, status=400)
    ok = await agent._long_term.update_text(entry_id, new_text)
    logger.info("DEBUG API: ltm_update entry_id=%s, text_len=%d, ok=%s", entry_id, len(new_text), ok)
    return web.json_response({"ok": ok})


async def ltm_clear_handler(request: web.Request) -> web.Response:
    """Clear all LTM entries.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"ok": True, "deleted": count}。
    """
    agent = request.app["agent"]
    count = await agent._long_term.clear_all()
    logger.warning("DEBUG API: ltm_clear deleted=%d", count)
    return web.json_response({"ok": True, "deleted": count})
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes.ltm import register_ltm_routes; print('OK')"
```

- [ ] **Step 3: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/ltm.py
git commit -m "refactor: extract ltm.py — LTM CRUD (5 handlers)"
```

---

### Task 4: 创建 `archive.py` — Archive + Sessions CRUD

**Files:**
- Create: `core/input/debug_routes/archive.py`

**Interfaces:**
- Consumes: `sessions_to_client` from `._common`（模块内直接 import）
- Produces: `register_archive_routes(app: web.Application) -> None`

- [ ] **Step 1: 创建 `core/input/debug_routes/archive.py`**

从原 `debug_routes.py:247-501` 提取 8 个 handler：4 个 archive + 4 个 sessions。通过 `from ._common import sessions_to_client` 直接导入（与 `session_detail_handler` 同模块）。保留 `session_detail_handler` 内部的 `_get_character_state` 闭包（不提升）。

```python
"""Debug archive + sessions routes: message archive CRUD + session management."""

import logging

from aiohttp import web

from core.schema.message import MessageRole
from core.schema.types import SessionId
from ..api_routes import _archive_to_client
from ._common import sessions_to_client

logger = logging.getLogger(__name__)


def register_archive_routes(app: web.Application) -> None:
    """注册 Archive + Sessions 路由。"""
    app.router.add_get("/debug/api/archive", archive_list_handler)
    app.router.add_delete("/debug/api/archive/{msg_id}", archive_delete_handler)
    app.router.add_put("/debug/api/archive/{msg_id}", archive_update_handler)
    app.router.add_delete("/debug/api/archive", archive_clear_handler)

    app.router.add_get("/debug/api/sessions", sessions_list_handler)
    app.router.add_get("/debug/api/sessions/{session_id}", session_detail_handler)
    app.router.add_delete("/debug/api/sessions/{session_id}", session_delete_handler)
    app.router.add_delete("/debug/api/sessions", sessions_clear_handler)


async def archive_list_handler(request: web.Request) -> web.Response:
    """分页浏览归档消息（debug 专用，offset 分页 + 搜索）。

    Args:
        request: aiohttp Request, query params: limit, skip, search, role。

    Returns:
        web.Response: {"messages": [...], "total": ..., "limit": ..., "skip": ...}。
    """
    agent = request.app["agent"]
    repo = agent._message_repo

    try:
        limit = int(request.query.get("limit", "100"))
        limit = max(1, min(limit, 500))
        skip = int(request.query.get("skip", "0"))
        skip = max(0, skip)
    except ValueError:
        limit = 100; skip = 0
    search = request.query.get("search", "").strip()
    role_str = request.query.get("role", "").strip()
    role = MessageRole(role_str) if role_str else None

    try:
        messages, total = await repo.query_offset(skip=skip, limit=limit, search=search, role=role)
        client_msgs = _archive_to_client(messages)
        return web.json_response({
            "messages": client_msgs,
            "total": total,
            "limit": limit,
            "skip": skip,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def archive_delete_handler(request: web.Request) -> web.Response:
    """Delete a single archived message by ID，同时从当前 session 中移除。

    Args:
        request: aiohttp Request, URL path: /{msg_id}。

    Returns:
        web.Response: {"ok": True/False, "session_purged": count}。
    """
    agent = request.app["agent"]
    repo = agent._message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return web.json_response({"error": "缺少 msg_id"}, status=400)
    ok = await repo.delete_one(msg_id)
    # 同步清理当前 session —— archive 和 session 是独立存储，
    # 只删 archive 不改 session 会导致 LLM prompt 仍包含已删消息
    session_purged = agent._purge_message_from_session(msg_id)
    if session_purged:
        await agent._session.save()
    logger.info("DEBUG API: archive_delete msg_id=%s, ok=%s, session_purged=%d", msg_id, ok, session_purged)
    return web.json_response({"ok": ok, "session_purged": session_purged})


async def archive_update_handler(request: web.Request) -> web.Response:
    """Update an archived message's text by ID.

    Args:
        request: aiohttp Request, URL path: /{msg_id}, JSON body: text。

    Returns:
        web.Response: {"ok": True/False}。
    """
    agent = request.app["agent"]
    repo = agent._message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return web.json_response({"error": "缺少 msg_id"}, status=400)
    body = await request.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        return web.json_response({"error": "text 不能为空"}, status=400)
    ok = await repo.update_text(msg_id, new_text)
    logger.info("DEBUG API: archive_update msg_id=%s, text_len=%d, ok=%s", msg_id, len(new_text), ok)
    return web.json_response({"ok": ok})


async def archive_clear_handler(request: web.Request) -> web.Response:
    """清空全部归档并同步清空当前 session（debug 专用）。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"ok": True, "deleted": count}。
    """
    agent = request.app["agent"]
    deleted = await agent._message_repo.clear_all()
    # 同步清空当前 session：archive 删光后 session 也要保持一致
    session = agent._session
    n_session = len(session.messages)
    if n_session:
        session.clear()
        await session.save()
    logger.warning("DEBUG API: archive_clear deleted=%d, session_cleared=%d", deleted, n_session)
    return web.json_response({"ok": True, "deleted": deleted})


async def sessions_list_handler(request: web.Request) -> web.Response:
    """List all sessions with message counts and token totals.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"sessions": [{session_id, message_count, updated_at, token_total}, ...]}。
    """
    agent = request.app["agent"]
    summaries = await agent._session_repo.list_all()
    if not summaries:
        # MongoDB 不可用，返回内存中的当前 session
        return web.json_response({
            "sessions": [{
                "session_id": str(agent._session.session_id),
                "message_count": len(agent._session.messages),
                "updated_at": "",
                "token_total": 0,
            }],
        })
    try:
        # 聚合 token 用量（MongoDB $group，单次查询）
        token_totals = await agent._token_repo.total_tokens_by_session()
        return web.json_response({
            "sessions": [
                {
                    "session_id": str(s.session_id),
                    "message_count": s.message_count,
                    "updated_at": s.updated_at,
                    "token_total": token_totals.get(str(s.session_id), 0),
                }
                for s in summaries
            ],
        })
    except Exception as e:
        return web.json_response(
            {"error": f"查询失败: {e}"}, status=500)


async def session_detail_handler(request: web.Request) -> web.Response:
    """Get session detail: messages, character state params, and total count.

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: {"session_id": ..., "messages": [...], "params": {...}, "total": ...}。
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return web.json_response({"error": "缺少 session_id"}, status=400)

    # 辅助函数：从 session repo 取 character_state（目标 session，非当前 session）
    async def _get_character_state(sid: SessionId) -> dict:
        result = await agent._session_repo.load(sid)
        if result is not None:
            _, character_state, _ = result  # (messages, CharacterState, MemoryReflectorState)
            return character_state.model_dump(mode="json")
        return {}

    if await agent._message_repo.health_check():
        # 从 message archive（权威源）读取消息
        try:
            msgs, total = await agent._message_repo.query_by_session(sid, limit=20)
            formatted = sessions_to_client(msgs)
            character_state = await _get_character_state(SessionId(sid))
            return web.json_response({
                "session_id": sid,
                "messages": formatted,
                "params": character_state,
                "total": total,
            })
        except Exception as e:
            logger.warning("message_repo query_by_session failed: %s", e)

    # MessageRepo 不可用 — 降级读 sessions 快照（或内存）
    result = await agent._session_repo.load(sid)
    if result is None:
        # MongoDB 不可用，返回当前内存 session（仅当 sid 匹配时）
        if sid == str(agent._session.session_id):
            return web.json_response({
                "session_id": sid,
                "messages": agent._session.messages_for_client(),
                "params": agent._session.character_state.model_dump(mode="json"),
            })
        return web.json_response({"error": "会话不存在"}, status=404)
    raw_msgs, character_state, _ = result
    formatted = sessions_to_client(raw_msgs)
    return web.json_response({
        "session_id": sid,
        "messages": formatted,
        "params": character_state.model_dump(mode="json"),
    })


async def session_delete_handler(request: web.Request) -> web.Response:
    """Delete a session by ID. Clears in-memory session if it matches the current one.

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: {"ok": True, "deleted": count} or error.
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return web.json_response({"error": "缺少 session_id"}, status=400)
    if not await agent._session_repo.health_check():
        return web.json_response(
            {"error": "MongoDB 不可用"}, status=503)
    try:
        deleted = await agent._session_repo.delete(sid)
        logger.warning("DEBUG API: session_delete sid=%s, current=%s, deleted=%d",
                      sid, str(agent._session.session_id), deleted)
        if sid == str(agent._session.session_id):
            agent._session.clear()
        return web.json_response({"ok": True, "deleted": deleted})
    except Exception as e:
        return web.json_response(
            {"error": f"删除失败: {e}"}, status=500)


async def sessions_clear_handler(request: web.Request) -> web.Response:
    """Clear all sessions and in-memory session state.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"ok": True, "deleted": count} or error.
    """
    agent = request.app["agent"]
    if not await agent._session_repo.health_check():
        return web.json_response(
            {"error": "MongoDB 不可用"}, status=503)
    try:
        deleted = await agent._session_repo.clear_all()
        logger.warning("DEBUG API: sessions_clear deleted=%d", deleted)
        agent._session.clear()
        return web.json_response({"ok": True, "deleted": deleted})
    except Exception as e:
        return web.json_response(
            {"error": f"清空失败: {e}"}, status=500)
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes.archive import register_archive_routes; print('OK')"
```

- [ ] **Step 3: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/archive.py
git commit -m "refactor: extract archive.py — archive + sessions CRUD (8 handlers)"
```

---

### Task 5: 创建 `recording.py` — Prompts + Responses

**Files:**
- Create: `core/input/debug_routes/recording.py`

**Interfaces:**
- Produces: `register_recording_routes(app: web.Application) -> None`

- [ ] **Step 1: 创建 `core/input/debug_routes/recording.py`**

从原 `debug_routes.py:503-606` 提取 4 个 handler（prompts list/detail + responses list/detail）。

```python
"""Debug recording routes: prompt + response recorder inspection."""

import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)


def register_recording_routes(app: web.Application) -> None:
    """注册 Prompts + Responses 路由。"""
    app.router.add_get("/debug/api/prompts", prompts_list_handler)
    app.router.add_get(r"/debug/api/prompts/{index:\d+}", prompt_detail_handler)
    app.router.add_get("/debug/api/responses", responses_list_handler)
    app.router.add_get(r"/debug/api/responses/{index:\d+}", response_detail_handler)


async def prompts_list_handler(request: web.Request) -> web.Response:
    """返回最近 N 次 LLM 调用的 prompt 摘要列表。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"records": [...], "total": ..., "max": ...}。
    """
    agent = request.app["agent"]
    recorder = getattr(agent, "_prompt_recorder", None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())


async def prompt_detail_handler(request: web.Request) -> web.Response:
    """返回指定记录的完整 prompt 内容 + 模板源码。

    Args:
        request: aiohttp Request, URL path: /{index}。

    Returns:
        web.Response: {"index": ..., "timestamp": ..., "messages": [...], "tools": [...], ...}。
    """
    index = int(request.match_info["index"])
    agent = request.app["agent"]
    recorder = getattr(agent, "_prompt_recorder", None)
    if recorder is None:
        raise web.HTTPNotFound(text="PromptRecorder 未初始化")
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound(text=f"记录 #{index} 不存在")

    # 读取模板源码（从 Module/system_prompt/templates/）
    template_source = None
    templates_dir = "Module/system_prompt/templates"
    try:
        sources = {}
        for fname in sorted(os.listdir(templates_dir)):
            if fname.endswith(".j2") and not fname.startswith("_"):
                fpath = os.path.join(templates_dir, fname)
                with open(fpath) as f:
                    sources[fname] = f.read()
        if sources:
            template_source = {"path": templates_dir, "sections": sources}
    except Exception:
        pass

    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "model": record.model,
        "messages": record.messages,
        "tools": record.tools,
        "template_source": template_source,
    })


async def responses_list_handler(request: web.Request) -> web.Response:
    """返回最近 N 次 LLM 调用的原始回复摘要列表。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: {"records": [...], "total": ..., "max": ...}。
    """
    agent = request.app["agent"]
    recorder = getattr(agent, "_response_recorder", None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())


async def response_detail_handler(request: web.Request) -> web.Response:
    """返回指定记录的完整原始回复。

    Args:
        request: aiohttp Request, URL path: /{index}。

    Returns:
        web.Response: {"index": ..., "timestamp": ..., "raw_text": ..., "inner_voice": ...}。
    """
    index = int(request.match_info["index"])
    agent = request.app["agent"]
    recorder = getattr(agent, "_response_recorder", None)
    if recorder is None:
        raise web.HTTPNotFound(text="ResponseRecorder 未初始化")
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound(text=f"记录 #{index} 不存在")
    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "raw_text": record.raw_text,
        "inner_voice": record.inner_voice,
        "text_len": record.text_len,
        "inner_voice_len": record.inner_voice_len,
    })
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes.recording import register_recording_routes; print('OK')"
```

- [ ] **Step 3: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/recording.py
git commit -m "refactor: extract recording.py — prompts + responses (4 handlers)"
```

---

### Task 6: 创建 `character.py` — Character 版本历史

**Files:**
- Create: `core/input/debug_routes/character.py`

**Interfaces:**
- Produces: `register_character_routes(app: web.Application) -> None`

- [ ] **Step 1: 创建 `core/input/debug_routes/character.py`**

从原 `debug_routes.py:608-778` 提取 4 个 handler + 2 个 private helper（`_list_character_versions`, `_character_rollback`）。Helper 从闭包提升为模块级私有函数。

```python
"""Debug character routes: L2 / C6 version history + rollback."""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from core.schema._utils import now_iso
from core.schema.compression import CompressionTrigger

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def register_character_routes(app: web.Application) -> None:
    """注册 Character 版本历史路由。"""
    app.router.add_get("/debug/api/l2-versions", l2_versions_handler)
    app.router.add_post("/debug/api/l2-rollback", l2_rollback_handler)
    app.router.add_get("/debug/api/character-versions", character_versions_handler)
    app.router.add_post("/debug/api/character-rollback", character_rollback_handler)


async def _list_character_versions(agent: "Agent", content_type: str,
                                    limit: int, before_version: int | None):
    """List character version history (L2 or C6) for the current session.

    Args:
        agent: Agent instance with character engine.
        content_type: "l2" or "c6".
        limit: Maximum number of versions to return.
        before_version: Optional upper bound version number (exclusive).

    Returns:
        tuple: (result_dict | None, error_str | None, http_status_code)
    """
    engine = getattr(agent, "_character_engine", None)
    if not engine or not getattr(engine, "_version_repo", None):
        return None, "版本历史不可用", 503
    sid = agent._session.session_id
    versions = await engine._version_repo.list_versions(
        sid, limit=limit, before_version=before_version,
        content_type=content_type,
    )
    total = await engine._version_repo.count_versions(sid, content_type=content_type)
    return {
        "versions": versions,
        "total": total,
        "has_more": len(versions) == limit and versions[-1]["version"] > 1,
    }, None, 200


async def _character_rollback(agent: "Agent", content_type: str, target_version: int):
    """Rollback character content (L2 or C6) to a specific version.

    Args:
        agent: Agent instance with character engine.
        content_type: "l2" or "c6".
        target_version: Version number to rollback to.

    Returns:
        tuple: (result_dict | None, error_str | None, http_status_code)
    """
    engine = getattr(agent, "_character_engine", None)
    if not engine or not getattr(engine, "_version_repo", None):
        return None, "版本历史不可用", 503
    sid = agent._session.session_id
    entry = await engine._version_repo.get_version(sid, target_version,
                                                    content_type=content_type)
    if entry is None:
        return None, f"版本 {target_version} 不存在", 404

    # 恢复文本
    if content_type == "l2":
        engine.state.self_l2 = entry["text"]
    elif content_type == "c6":
        engine.state.speaking_style_c6 = entry["text"]

    new_version = await engine._version_repo.append_version(
        session_id=sid, text=entry["text"], timestamp=now_iso(),
        trigger=CompressionTrigger.MANUAL,
        change_summary=[f"回滚到版本 {target_version}"],
        content_type=content_type,
    )

    # L2 rollback 后触发 C6 刷新
    if content_type == "l2":
        try:
            await engine._refresh_c6()
        except Exception:
            pass

    return {"status": "ok", "new_version": new_version, "text": entry["text"]}, None, 200


async def l2_versions_handler(request: web.Request) -> web.Response:
    """List L2 self-image version history (legacy route).

    Args:
        request: aiohttp Request, query params: limit, before。

    Returns:
        web.Response: {"versions": [...], "total": ..., "has_more": ...}。
    """
    agent = request.app["agent"]
    limit = int(request.query.get("limit", "20"))
    before = request.query.get("before")
    before_version = int(before) if before else None
    result, error, status = await _list_character_versions(
        agent, "l2", limit, before_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)


async def l2_rollback_handler(request: web.Request) -> web.Response:
    """Rollback L2 self-image to a specific version (legacy route).

    Args:
        request: aiohttp Request, JSON body: version。

    Returns:
        web.Response: {"status": "ok", "new_version": ..., "text": ...} or error.
    """
    agent = request.app["agent"]
    try:
        body = await request.json()
        target_version = int(body["version"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "缺少 version 或格式错误"}, status=400)
    result, error, status = await _character_rollback(
        agent, "l2", target_version)
    if error:
        return web.json_response({"error": error}, status=status)
    logger.warning("DEBUG API: l2_rollback version=%d, new_version=%s",
                  target_version, result.get("new_version"))
    return web.json_response(result)


async def character_versions_handler(request: web.Request) -> web.Response:
    """List character version history (L2 or C6).

    Args:
        request: aiohttp Request, query params: type (l2/c6), limit, before。

    Returns:
        web.Response: {"versions": [...], "total": ..., "has_more": ...}。
    """
    agent = request.app["agent"]
    content_type = request.query.get("type", "l2")
    if content_type not in ("l2", "c6"):
        return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
    limit = int(request.query.get("limit", "20"))
    before = request.query.get("before")
    before_version = int(before) if before else None
    result, error, status = await _list_character_versions(
        agent, content_type, limit, before_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)


async def character_rollback_handler(request: web.Request) -> web.Response:
    """Rollback character content (L2 or C6) to a specific version.

    Args:
        request: aiohttp Request, JSON body: type (l2/c6), version。

    Returns:
        web.Response: {"status": "ok", "new_version": ..., "text": ...} or error.
    """
    agent = request.app["agent"]
    try:
        body = await request.json()
        content_type = body["type"]
        target_version = int(body["version"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "缺少 type/version 或格式错误"}, status=400)
    if content_type not in ("l2", "c6"):
        return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
    result, error, status = await _character_rollback(
        agent, content_type, target_version)
    if error:
        return web.json_response({"error": error}, status=status)
    logger.warning("DEBUG API: character_rollback type=%s, version=%d, new_version=%s",
                  content_type, target_version, result.get("new_version"))
    return web.json_response(result)
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes.character import register_character_routes; print('OK')"
```

- [ ] **Step 3: 验证现有测试不受影响**

```bash
pytest core/input/tests/test_server.py -v -q
```

- [ ] **Step 4: Commit**

```bash
git add core/input/debug_routes/character.py
git commit -m "refactor: extract character.py — version history + rollback (4 handlers)"
```

---

### Task 7: 创建 `__init__.py` — 入口 + TaskSystem SSE

**Files:**
- Modify: `core/input/debug_routes/__init__.py`

**Interfaces:**
- Consumes: `register_*_routes` from all sibling modules
- Produces: `register_debug_routes(app: web.Application) -> None`

- [ ] **Step 1: 重写 `core/input/debug_routes/__init__.py`**

替换 Task 1 的空 package marker，写入完整入口：

```python
"""Debug API 路由 — 数据库状态, LTM CRUD, Session 管理, 消息归档, LLM 录制。

所有增删改操作均记录日志：info（资源级修改）/ warning（批量删除、回滚）。
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


def register_debug_routes(app: web.Application) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。"""

    from .overview import register_overview_routes
    from .ltm import register_ltm_routes
    from .archive import register_archive_routes
    from .recording import register_recording_routes
    from .character import register_character_routes

    register_overview_routes(app)
    register_ltm_routes(app)
    register_archive_routes(app)
    register_recording_routes(app)
    register_character_routes(app)

    # TaskSystem debug API — SSE 实时推送任务状态
    from Module.task_system.debug_api import TaskDebugAPI

    ts = app.get('task_system')
    if ts is not None:
        task_debug_api = TaskDebugAPI(ts)
        app['task_debug_api'] = task_debug_api
        app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
        app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
    else:
        app['task_debug_api'] = None

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)


# TaskDebugAPI 生命周期管理

async def _on_startup(app: web.Application) -> None:
    """Start TaskDebugAPI on application startup.

    Args:
        app: aiohttp Application instance.
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.start()


async def _on_shutdown(app: web.Application) -> None:
    """Stop TaskDebugAPI on application shutdown.

    Args:
        app: aiohttp Application instance.
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.stop()
```

- [ ] **Step 2: 验证 import**

```bash
python -c "from core.input.debug_routes import register_debug_routes; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/input/debug_routes/__init__.py
git commit -m "refactor: wire debug_routes/__init__.py — entry point + TaskSystem SSE"
```

---

### Task 8: 删除旧文件 + 最终验证

**Files:**
- Delete: `core/input/debug_routes.py`

- [ ] **Step 1: 删除 `core/input/debug_routes.py` 并暂存**

```bash
git rm core/input/debug_routes.py
```

- [ ] **Step 2: 验证 server.py import 自动解析到 package `__init__`**

```bash
python -c "from core.input.server import create_app; print('OK')"
```

- [ ] **Step 3: 运行全量 input 测试**

```bash
pytest core/input/tests/ -v -q
```

- [ ] **Step 4: 运行全量 quick 测试（防回归）**

```bash
make test-quick
```

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: remove old debug_routes.py — replaced by debug_routes/ package"
```

---

### 最终文件布局

```
core/input/
├── debug_routes.py           → 已删除
├── debug_routes/
│   ├── __init__.py           → register_debug_routes() 入口 + TaskSystem SSE + startup/shutdown
│   ├── _common.py            → sessions_to_client() 共享 helper
│   ├── overview.py           → GET /debug/api, GET /debug/api/db-status (2 handlers)
│   ├── ltm.py                → LTM CRUD (5 handlers)
│   ├── archive.py            → Archive + Sessions CRUD (8 handlers)
│   ├── recording.py          → Prompts + Responses (4 handlers)
│   └── character.py          → Character 版本历史 (4 handlers + 2 helpers)
└── server.py                 → import 路径不变
```
