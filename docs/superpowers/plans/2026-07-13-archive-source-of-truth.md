# Archive as Source of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MessageArchive the authoritative data source — debug session view reads from archive, not session snapshot.

**Architecture:** Add `session_id` to archive messages, add query methods to MessageArchive, switch debug session endpoints to read from archive, add one-time migration for existing data.

**Tech Stack:** Python 3.13, motor (async MongoDB), aiohttp

## Global Constraints

- Archive 是权威数据源，Session 快照仅用于 LLM 上下文恢复
- 迁移必须在 `session.load()` 之后执行（session_id 在 load() 中恢复）
- 索引创建失败不阻止启动
- MongoDB 不可用时，session detail 降级读内存 `session.messages_for_client()`

---

### Task 1: MessageArchive — 索引 + query_by_session + count_by_session

**Files:**
- Modify: `core/memory/message_archive.py:33-38` (connect)
- Modify: `core/memory/message_archive.py:142-154` (after count, before EOF)

**Interfaces:**
- Produces: `MessageArchive.query_by_session(session_id: str, *, skip: int = 0, limit: int = 200) -> tuple[list[dict], int]`
- Produces: `MessageArchive.count_by_session(session_id: str) -> int`

- [ ] **Step 1: Add session_id index in connect()**

Open `core/memory/message_archive.py`. In `connect()`, after the existing index comment (line 36), add:

```python
        # session_id 复合索引 — 用于按 session 查询消息
        try:
            await self._db.ensure_index(
                self.COLLECTION, [("session_id", 1), ("_id", -1)]
            )
        except Exception:
            logger.warning(
                "MessageArchive: session_id index creation failed, continuing"
            )
```

- [ ] **Step 2: Add query_by_session() and count_by_session()**

At the end of `message_archive.py` (after `clear_all()` at line 154), append:

```python
    # ---- 按 session 查询 ----

    async def query_by_session(
        self, session_id: str, *, skip: int = 0, limit: int = 200
    ) -> tuple[list[dict], int]:
        """按 session_id 查询消息，_id 降序（最新在前）。
        返回 (messages, total)。"""
        if self._db is None:
            return [], 0
        filter_expr = {"session_id": session_id}
        total = await self._db.count(self.COLLECTION, filter_expr)
        docs = await self._db.find(
            self.COLLECTION, filter_expr,
            sort=[("_id", -1)],
            skip=skip,
            limit=limit,
        )
        return docs, total

    async def count_by_session(self, session_id: str) -> int:
        """按 session_id 统计消息数。"""
        if self._db is None:
            return 0
        return await self._db.count(self.COLLECTION, {"session_id": session_id})
```

- [ ] **Step 3: Verify import works**

```bash
conda run -n Elysia python -c "from core.memory.message_archive import MessageArchive; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/memory/message_archive.py
git commit -m "feat: add session_id index, query_by_session, count_by_session to MessageArchive"
```

---

### Task 2: SessionMemory — archive_append 写 session_id + migration

**Files:**
- Modify: `core/memory/session.py:83-89` (_archive_append)
- Modify: `core/memory/session.py` (add after _archive_append)

**Interfaces:**
- Consumes: `MessageArchive.append()` (accepts dict with `session_id` field)
- Produces: `SessionMemory.migrate_archive_session_id() -> int` (returns count of backfilled docs)

- [ ] **Step 1: Add session_id to _archive_append()**

In `core/memory/session.py`, change `_archive_append()` to inject `session_id`:

```python
    async def _archive_append(self, entry: dict) -> None:
        """fire-and-forget: 将消息追加到永久归档。异常只记日志，不影响主流程。"""
        assert self._archive is not None, "archive not initialized"
        try:
            entry["session_id"] = self._session_id
            await self._archive.append(entry)
        except Exception:
            logger.debug("archive append failed", exc_info=True)
```

The change is adding one line `entry["session_id"] = self._session_id` before `await self._archive.append(entry)`.

- [ ] **Step 2: Add migrate_archive_session_id() method**

After `_archive_append()`, add:

```python
    async def migrate_archive_session_id(self) -> int:
        """一次性迁移：给 message_archive 中缺少 session_id 的旧文档补上。
        必须在 session.load() 之后调用（session_id 已从 MongoDB 恢复）。
        返回迁移的文档数。"""
        if self._archive is None or self._archive._db is None:
            return 0
        try:
            modified = await self._archive._db.update_many(
                self._archive.COLLECTION,
                {"session_id": {"$exists": False}},
                {"$set": {"session_id": self._session_id}},
            )
            if modified > 0:
                logger.info(
                    "SessionMemory: 迁移 %d 条 archive 文档 → session_id=%s",
                    modified, self._session_id,
                )
            return modified
        except Exception:
            logger.debug("archive session_id migration failed", exc_info=True)
            return 0
```

- [ ] **Step 3: Verify import works**

```bash
conda run -n Elysia python -c "from core.memory.session import SessionMemory; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/memory/session.py
git commit -m "feat: inject session_id into archive messages + migration method"
```

---

### Task 3: Server — session list/detail 端点改读 archive

**Files:**
- Modify: `core/input/server.py:341-382` (sessions_list_handler)
- Modify: `core/input/server.py:384-430` (session_detail_handler)
- Modify: `core/input/server.py:638-662` (add _sessions_to_client after _archive_to_client)

**Interfaces:**
- Consumes: `archive.query_by_session()`, `archive.count_by_session()`, `session.messages_for_client()`
- Produces: `_sessions_to_client(msgs: list[dict]) -> list[dict]`

- [ ] **Step 1: Add _sessions_to_client() helper**

After `_archive_to_client()` (server.py line ~662), add:

```python
    def _sessions_to_client(msgs: list[dict]) -> list[dict]:
        """将 archive 原始 dict 转为 session 详情前端兼容格式。"""
        result = []
        for m in msgs:
            entry: dict = {"role": m.get("role", "?"),
                           "timestamp": m.get("timestamp", ""),
                           "content_type": m.get("content_type", {})}
            content = m.get("content", "")
            if isinstance(content, list):
                entry["parts"] = [
                    {"content": p.get("content", "")}
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                entry["content"] = content
            elif isinstance(content, str):
                entry["parts"] = [{"content": content}]
            elif "parts" in m:
                entry["parts"] = m["parts"]
            else:
                entry["parts"] = [{"content": str(content)}]
            result.append(entry)
        return result
```

- [ ] **Step 2: Update sessions_list_handler — count from archive**

In `sessions_list_handler()` (server.py:341-382), change the `message_count` line from:

```python
"message_count": len(d.get("messages", [])),
```

To:

```python
"message_count": await archive.count_by_session(
    d.get("session_id", "")
) if archive else len(d.get("messages", [])),
```

Also update the fallback branch (MongoDB unavailable) to use `await archive.count_by_session(...)` or fall through to `len(session._messages)`.

Full updated handler:

```python
    async def sessions_list_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        session = agent._session
        archive = getattr(agent, "_archive", None)
        if session._db is None:
            return web.json_response({
                "sessions": [{
                    "session_id": session._session_id,
                    "message_count": len(session._messages),
                    "updated_at": "",
                    "token_total": 0,
                }],
            })
        try:
            docs = await session._db.find(
                "sessions", {}, sort=[("updated_at", -1)], limit=500)
            from collections import defaultdict
            token_totals: dict[str, int] = defaultdict(int)
            try:
                token_docs = await session._db.find(
                    "token_usage", {}, limit=2000)
                for td in token_docs:
                    sid = td.get("session_id", "")
                    token_totals[sid] += td.get("total_tokens", 0)
            except Exception:
                pass
            return web.json_response({
                "sessions": [
                    {
                        "session_id": d.get("session_id", "?"),
                        "message_count": (
                            await archive.count_by_session(d.get("session_id", ""))
                            if archive else len(d.get("messages", []))
                        ),
                        "updated_at": d.get("updated_at", ""),
                        "token_total": token_totals.get(d.get("session_id", ""), 0),
                    }
                    for d in docs
                ],
            })
        except Exception as e:
            return web.json_response(
                {"error": f"查询失败: {e}"}, status=500)
```

- [ ] **Step 3: Update session_detail_handler — read from archive**

Replace `session_detail_handler()` (server.py:384-430) with:

```python
    async def session_detail_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        sid = request.match_info.get("session_id", "")
        if not sid:
            return web.json_response({"error": "缺少 session_id"}, status=400)
        session = agent._session
        archive = getattr(agent, "_archive", None)

        # 辅助函数：从 sessions 集合取 params（目标 session，非当前 session）
        async def _get_params(sid: str) -> dict:
            if session._db is not None:
                try:
                    docs = await session._db.find(
                        "sessions", {"session_id": sid}, limit=1)
                    if docs:
                        return docs[0].get("params", {})
                except Exception:
                    pass
            return {}

        if archive and archive._db is not None:
            # 从 archive（权威源）读取消息
            try:
                msgs, total = await archive.query_by_session(sid)
                formatted = _sessions_to_client(msgs)
                params = await _get_params(sid)
                return web.json_response({
                    "session_id": sid,
                    "messages": formatted,
                    "params": params,
                    "total": total,
                })
            except Exception as e:
                logger.warning("archive query_by_session failed: %s", e)
                # archive 查询异常 → 显式降级到 sessions 快照（不依赖隐式 fallthrough）

        # Archive 不可用 — 降级读 sessions 快照（或内存）
        if session._db is None:
            return web.json_response({
                "session_id": sid,
                "messages": session.messages_for_client(),
                "params": session._params,
            })
        try:
            docs = await session._db.find(
                "sessions", {"session_id": sid}, limit=1)
            if not docs:
                return web.json_response({"error": "会话不存在"}, status=404)
            doc = docs[0]
            raw_msgs = doc.get("messages", [])
            formatted = _sessions_to_client(raw_msgs)
            return web.json_response({
                "session_id": doc.get("session_id", sid),
                "messages": formatted,
                "params": doc.get("params", {}),
            })
        except Exception as e:
            return web.json_response(
                {"error": f"查询失败: {e}"}, status=500)
```

- [ ] **Step 4: Verify server code loads**

```bash
conda run -n Elysia python -c "from core.input.server import create_app; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add core/input/server.py
git commit -m "feat: switch session list/detail endpoints to read from archive"
```

---

### Task 4: Agent — wire migration after session.load()

**Files:**
- Modify: `core/agent.py:224-226` (_start_persistence)

- [ ] **Step 1: Add migration call**

In `core/agent.py`, `_start_persistence()`, after `await self._session.load()` (line 226), add:

```python
            await self._session.load()
            # 迁移旧 archive 文档（缺少 session_id 的补上）
            await self._session.migrate_archive_session_id()
```

The relevant section becomes:

```python
        if mongo_ok:
            await self._archive.connect(self._session.db)
            await self._session.load()
            # 迁移旧 archive 文档（缺少 session_id 的补上）
            await self._session.migrate_archive_session_id()
            if self._character_reflector:
```

- [ ] **Step 2: Verify import works**

```bash
conda run -n Elysia python -c "from core.agent import Agent; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core/agent.py
git commit -m "feat: run archive session_id migration after session.load()"
```

---

### Verification

After all tasks, restart the service and verify:

```bash
# 1. 重启服务
./start.sh

# 2. 检查日志 — 应看到迁移日志（如果有旧数据）
tail -20 log/app.log | grep -i migrate

# 3. 打开 debug 页面 → 数据库 → Session 管理
#    - 消息数应反映 archive 中的实际数量
#    - 点进 session 详情 — 应只看到 archive 中存在的消息

# 4. 运行测试
conda run -n Elysia python -m pytest core/tests/ -v -q 2>&1 | tail -5
```
