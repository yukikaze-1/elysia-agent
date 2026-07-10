# Message Archive 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有对话消息永久归档到 MongoDB `message_archive` 集合，前端分页懒加载全量历史，debug 页面可浏览/搜索/清空。

**Architecture:** `SessionMemory.add_message()` 中 fire-and-forget 写入 `MessageArchive`，与现有 session 逻辑完全解耦。前端 WebSocket 连接后渲染最近 20 条（不变），滚动到顶时触发 `GET /api/messages` 分页加载更多。

**Tech Stack:** Python 3.13, motor (MongoDB async), vanilla JS (ES modules), pytest + pytest-asyncio

## Global Constraints

- 所有 API key 通过环境变量传递
- 代码注释用中文
- 使用 conda 环境 `conda activate Elysia`
- 消息格式与现有 `SessionMemory._messages` 完全一致
- 不修改 session 的 max_messages 或 TTL
- archive 为 None 时行为等同旧版（向后兼容）

---

### Task 1: MessageArchive 核心类 + 单元测试

**Files:**
- Create: `core/memory/message_archive.py`
- Create: `core/memory/tests/test_message_archive.py`

**Interfaces:**
- Consumes: `Module.database.documentdb.protocol.DocumentDBBackend` (insert_one, find, count, delete_many, ensure_index)
- Produces:
  - `class MessageArchive` with `COLLECTION = "message_archive"`
  - `async connect(db: DocumentDBBackend) -> bool`
  - `async append(msg: dict) -> str` (returns _id)
  - `async query(*, before: str | None = None, limit: int = 50) -> tuple[list[dict], bool]`
  - `async count() -> int`
  - `async clear_all() -> int`

- [ ] **Step 1: 写 MessageArchive 接口测试（TDD）**

```python
# core/memory/tests/test_message_archive.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.memory.message_archive import MessageArchive


class TestMessageArchiveConnect:
    @pytest.mark.asyncio
    async def test_connect_creates_index(self):
        db = AsyncMock()
        db.ensure_index = AsyncMock(return_value="_id_-1")
        archive = MessageArchive()
        result = await archive.connect(db)
        assert result is True
        db.ensure_index.assert_called_once_with("message_archive", [("_id", -1)])

    @pytest.mark.asyncio
    async def test_connect_returns_false_on_error(self):
        db = AsyncMock()
        db.ensure_index = AsyncMock(side_effect=Exception("no connection"))
        archive = MessageArchive()
        result = await archive.connect(db)
        assert result is False


class TestMessageArchiveAppend:
    @pytest.mark.asyncio
    async def test_append_inserts_and_returns_id(self):
        db = AsyncMock()
        db.insert_one = AsyncMock(return_value="abc123")
        archive = MessageArchive()
        await archive.connect(db)
        msg = {"role": "user", "content": [{"type": "text", "content": "hello"}]}
        _id = await archive.append(msg)
        assert _id == "abc123"
        db.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_raises_when_not_connected(self):
        archive = MessageArchive()
        with pytest.raises(RuntimeError, match="not connected"):
            await archive.append({"role": "user"})


class TestMessageArchiveQuery:
    @pytest.mark.asyncio
    async def test_query_returns_messages_and_has_more(self):
        db = AsyncMock()
        msgs = [{"_id": str(i), "role": "user", "text": f"msg{i}"} for i in range(51)]
        db.find = AsyncMock(return_value=msgs[:50])
        archive = MessageArchive()
        await archive.connect(db)
        messages, has_more = await archive.query(limit=50)
        assert len(messages) == 50
        assert has_more is True
        db.find.assert_called_once()

    @pytest.mark.asyncio
    async def test_query_no_more_pages(self):
        db = AsyncMock()
        db.find = AsyncMock(return_value=[{"_id": "1", "role": "user"}])
        archive = MessageArchive()
        await archive.connect(db)
        messages, has_more = await archive.query(limit=50)
        assert has_more is False

    @pytest.mark.asyncio
    async def test_query_with_before_cursor(self):
        db = AsyncMock()
        db.find = AsyncMock(return_value=[])
        archive = MessageArchive()
        await archive.connect(db)
        await archive.query(before="abc123", limit=30)
        # 验证 filter 包含 _id < "abc123"
        call_args = db.find.call_args
        assert "_id" in call_args[0][1]
        assert call_args[1]["limit"] == 31  # limit + 1 用于判断 has_more


class TestMessageArchiveCount:
    @pytest.mark.asyncio
    async def test_count_returns_zero_when_empty(self):
        db = AsyncMock()
        db.count = AsyncMock(return_value=0)
        archive = MessageArchive()
        await archive.connect(db)
        c = await archive.count()
        assert c == 0

    @pytest.mark.asyncio
    async def test_count_returns_total(self):
        db = AsyncMock()
        db.count = AsyncMock(return_value=42)
        archive = MessageArchive()
        await archive.connect(db)
        c = await archive.count()
        assert c == 42


class TestMessageArchiveClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_deletes_and_returns_count(self):
        db = AsyncMock()
        db.delete_many = AsyncMock(return_value=10)
        archive = MessageArchive()
        await archive.connect(db)
        deleted = await archive.clear_all()
        assert deleted == 10
        db.delete_many.assert_called_once_with("message_archive", {})
```

- [ ] **Step 2: 运行测试，验证全部 FAIL**

```bash
conda run -n Elysia python -m pytest core/memory/tests/test_message_archive.py -v
```
预期: 7 failed (class/module not found)

- [ ] **Step 3: 实现 MessageArchive**

```python
# core/memory/message_archive.py
"""MessageArchive — 对话消息永久归档（MongoDB，只增不删）。"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.database.documentdb.protocol import DocumentDBBackend

logger = logging.getLogger(__name__)


class MessageArchive:
    """所有对话消息的永久归档存储。

    与 SessionMemory 职责分离:
    - SessionMemory: LLM 上下文窗口，截断保留最近 N 条
    - MessageArchive: 永久归档，只增不删，分页查询

    使用方式:
        archive = MessageArchive()
        await archive.connect(db_backend)       # 复用 session 的 MongoDB 连接
        await archive.append(msg)                # 每条消息 fire-and-forget
        messages, has_more = await archive.query(limit=50)  # 分页查询
    """

    COLLECTION = "message_archive"

    def __init__(self):
        self._db: "DocumentDBBackend | None" = None

    # ---- 连接 ----

    async def connect(self, db: "DocumentDBBackend") -> bool:
        """接入已有的 MongoDB backend。创建索引。返回是否成功。"""
        self._db = db
        try:
            await self._db.ensure_index(self.COLLECTION, [("_id", -1)])
            logger.info("MessageArchive: connected, collection='%s'", self.COLLECTION)
            return True
        except Exception as e:
            logger.warning("MessageArchive: 索引创建失败 (%s)", e)
            return False

    # ---- 写入 ----

    async def append(self, msg: dict) -> str:
        """追加一条消息。fire-and-forget — 调用方用 create_task 包装。返回 _id。"""
        if self._db is None:
            raise RuntimeError("MessageArchive not connected")
        _id = await self._db.insert_one(self.COLLECTION, msg)
        return _id

    # ---- 查询 ----

    async def query(
        self,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict], bool]:
        """分页查询消息，按 _id 降序（最新的在前）。

        before: 上一页最后一条的 _id（ObjectId 字符串），用于游标分页
        limit: 每页数量
        返回: (messages, has_more)
        """
        if self._db is None:
            return [], False

        filter_expr: dict = {}
        if before:
            from bson import ObjectId
            filter_expr["_id"] = {"$lt": ObjectId(before)}

        docs = await self._db.find(
            self.COLLECTION,
            filter_expr,
            sort=[("_id", -1)],
            limit=limit + 1,  # 多取一条判断 has_more
        )

        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]

        return docs, has_more

    # ---- 管理 ----

    async def count(self) -> int:
        """返回归档消息总数。"""
        if self._db is None:
            return 0
        return await self._db.count(self.COLLECTION, {})

    async def clear_all(self) -> int:
        """清空全部归档。返回删除数量。"""
        if self._db is None:
            return 0
        deleted = await self._db.delete_many(self.COLLECTION, {})
        logger.info("MessageArchive: 清空 %d 条消息", deleted)
        return deleted
```

- [ ] **Step 4: 运行测试，验证全部 PASS**

```bash
conda run -n Elysia python -m pytest core/memory/tests/test_message_archive.py -v
```
预期: 7 passed

- [ ] **Step 5: Commit**

```bash
git add core/memory/message_archive.py core/memory/tests/test_message_archive.py
git commit -m "feat: add MessageArchive — persistent append-only message storage"
```

---

### Task 2: SessionMemory + Agent 集成

**Files:**
- Modify: `core/memory/session.py:21-27` (构造函数签名)
- Modify: `core/memory/session.py:49-69` (add_message 末尾)
- Modify: `core/agent.py:65` (SessionMemory 构造)
- Modify: `core/agent.py:200-218` (_start_persistence 中 connect archive)
- Create: `core/memory/tests/test_session_archive_integration.py`

**Interfaces:**
- Consumes: `MessageArchive` (from Task 1)
- Produces: `SessionMemory(..., archive: MessageArchive | None = None)`
- `add_message()` 末尾 fire-and-forget 写入 archive

- [ ] **Step 1: 写集成测试**

```python
# core/memory/tests/test_session_archive_integration.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.memory.session import SessionMemory
from core.memory.message_archive import MessageArchive


class TestSessionArchiveIntegration:
    @pytest.mark.asyncio
    async def test_add_message_writes_to_archive(self):
        """add_message 应触发 archive.append（fire-and-forget）。"""
        archive = MagicMock(spec=MessageArchive)
        archive.append = AsyncMock(return_value="abc123")
        session = SessionMemory(max_messages=20, archive=archive)

        session.add_message("user", "hello")
        # fire-and-forget — 等待一小段时间让 task 执行
        await asyncio.sleep(0.05)
        archive.append.assert_called_once()
        # 验证传入的消息格式
        called_msg = archive.append.call_args[0][0]
        assert called_msg["role"] == "user"
        assert called_msg["content"] == [{"type": "text", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_add_message_skips_when_no_archive(self):
        """archive=None 时 add_message 不应报错。"""
        session = SessionMemory(max_messages=20, archive=None)
        # 不应抛出异常
        session.add_message("user", "hello")
        assert len(session.messages) == 1

    @pytest.mark.asyncio
    async def test_add_message_does_not_block_on_archive_error(self):
        """archive.append 失败时不应阻塞 add_message 返回。"""
        archive = MagicMock(spec=MessageArchive)
        archive.append = AsyncMock(side_effect=Exception("MongoDB down"))
        session = SessionMemory(max_messages=20, archive=archive)

        # 不应抛出异常
        session.add_message("user", "hello")
        await asyncio.sleep(0.05)
        # 消息仍然成功添加到内存
        assert len(session.messages) == 1
```

- [ ] **Step 2: 运行测试，验证 FAIL**

```bash
conda run -n Elysia python -m pytest core/memory/tests/test_session_archive_integration.py -v
```
预期: 3 failed (SessionMemory 没有 archive 参数)

- [ ] **Step 3: 修改 SessionMemory 构造函数**

```python
# core/memory/session.py:21-27 → 改为:
def __init__(self, max_messages: int = 20, session_id: str = "default",
             archive: "MessageArchive | None" = None):
    self._messages: deque[dict[str, Any]] = deque(maxlen=max_messages)
    self._params: dict[str, Any] = {}
    self._max_messages = max_messages
    self._session_id = session_id
    self._db: Any = None
    self._archive = archive
    self._pending_file = os.path.join("data", f"session_{session_id}.json")
```

文件顶部添加 import:
```python
# core/memory/session.py 顶部，在现有 import 之后:
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.memory.message_archive import MessageArchive
```

- [ ] **Step 4: 修改 SessionMemory.add_message() 末尾**

在 `add_message()` 方法的 `self._messages.append(entry)` 之后、方法 return 之前，添加:

```python
        # 写入永久归档（fire-and-forget，不阻塞主流程）
        if self._archive:
            import asyncio
            asyncio.create_task(self._archive_append(entry))
```

在类中添加辅助方法:
```python
    async def _archive_append(self, entry: dict) -> None:
        try:
            await self._archive.append(entry)
        except Exception:
            logger.debug("archive append failed", exc_info=True)
```

- [ ] **Step 5: 修改 Agent 初始化**

```python
# core/agent.py:65 → 改为:
from core.memory.message_archive import MessageArchive

# Agent.__init__() 中，SessionMemory 构造之前加一行:
self._archive = MessageArchive()

# SessionMemory 构造改为:
self._session = SessionMemory(
    max_messages=config.memory.session_max_messages,
    archive=self._archive,
)
```

- [ ] **Step 6: 修改 Agent._start_persistence()**

```python
# core/agent.py:200-203 → 改为:
mongo_ok, milvus_ok = await asyncio.gather(
    self._session.connect_db(),
    self._long_term.connect(),
)
if mongo_ok:
    await self._archive.connect(self._session.db)
    await self._session.load()
    # ... 其余不变 ...
```

- [ ] **Step 7: 运行测试**

```bash
conda run -n Elysia python -m pytest core/memory/tests/test_session_archive_integration.py -v
```
预期: 3 passed

```bash
conda run -n Elysia python -m pytest core/memory/tests/test_long_term.py core/memory/tests/test_message_archive.py core/memory/tests/test_session_archive_integration.py -v
```
预期: 18 passed (8 + 7 + 3)

- [ ] **Step 8: Commit**

```bash
git add core/memory/session.py core/agent.py core/memory/tests/test_session_archive_integration.py
git commit -m "feat: integrate MessageArchive into SessionMemory and Agent"
```

---

### Task 3: GET /api/messages 端点

**Files:**
- Modify: `Module/input/server.py` (新增 handler + 注册路由)

**Interfaces:**
- Consumes: `request.app["agent"]._archive` (MessageArchive from Task 2)
- Produces: `GET /api/messages?before=&limit=50` → `{"messages": [...], "has_more": bool, "total": int}`

- [ ] **Step 1: 写 handler 并注册路由**

在 `server.py` 的 `create_app()` 中，`/api/images` 路由附近添加:

```python
    # ---- Message Archive API ----

    async def messages_handler(request: web.Request) -> web.Response:
        """分页获取归档消息。供前端聊天页滚动加载历史。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"messages": [], "has_more": False, "total": 0})

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
            limit = max(1, min(limit, 200))  # 限制 1-200
        except ValueError:
            limit = 50

        try:
            messages, has_more = await archive.query(before=before, limit=limit)
            total = await archive.count()
            # 消息格式转换：archive 存的是原始 dict（content=[TextPart]）
            # 前端渲染需要 parts 字段 —— 与 messages_for_client() 对齐
            client_msgs = _archive_to_client(messages)
            return web.json_response({
                "messages": client_msgs,
                "has_more": has_more,
                "total": total,
            })
        except Exception as e:
            logger.warning("Failed to query message archive: %s", e)
            return web.json_response(
                {"messages": [], "has_more": False, "total": 0, "error": str(e)},
                status=500,
            )

    app.router.add_get("/api/messages", messages_handler)

    def _archive_to_client(msgs: list[dict]) -> list[dict]:
        """将 archive 原始 dict 转为前端兼容格式（补 parts 字段）。"""
        result = []
        for m in msgs:
            entry = dict(m)
            # 提取 _id 供前端分页用
            if "_id" in entry:
                entry["_id"] = str(entry["_id"])
            # 如果已有 parts，直接透传
            if "parts" in entry:
                result.append(entry)
                continue
            # 否则从 content (TextPart 数组) 提取 parts
            content = entry.get("content", [])
            if isinstance(content, list):
                entry["parts"] = [
                    {"content": p["content"]}
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
            elif isinstance(content, str):
                entry["parts"] = [{"content": content}]
            else:
                entry["parts"] = [{"content": ""}]
            result.append(entry)
        return result
```

- [ ] **Step 2: 验证端点**

启动服务后:
```bash
# 发送几条消息到 agent（通过 WS 或聊天界面）
# 然后:
curl http://localhost:8765/api/messages?limit=5
# 预期: {"messages": [...], "has_more": ..., "total": ...}
```

- [ ] **Step 3: Commit**

```bash
git add Module/input/server.py
git commit -m "feat: add GET /api/messages endpoint for paginated archive query"
```

---

### Task 4: Debug API 端点（archive 浏览 + db-status 扩展）

**Files:**
- Modify: `Module/input/server.py` (新增 2 个 handler + 扩展 db_status_handler)

**Interfaces:**
- Consumes: MessageArchive (from Task 1), `/debug/api/` route pattern
- Produces:
  - `GET /debug/api/archive?before=&limit=100` → `{"messages": [...], "has_more": bool, "total": int}`
  - `DELETE /debug/api/archive` → `{"ok": true, "deleted": int}`
  - `GET /debug/api/db-status` → 扩展 `"archive": {"count": int, "mongodb_ok": bool}`

- [ ] **Step 1: 扩展 db_status_handler**

找到 `db_status_handler`（`server.py:166-182`），在返回 dict 中加 `"archive"`:

```python
    async def db_status_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        ltm = agent._long_term
        session = agent._session
        archive = getattr(agent, "_archive", None)
        return web.json_response({
            "ltm": {
                "count": ltm.count,
                "milvus_ok": ltm._connected,
                "dirty_count": len(ltm._dirty_ids),
            },
            "session": {
                "count": 1,
                "current_id": getattr(session, "_session_id", "default"),
                "mongodb_ok": session._db is not None,
            },
            "archive": {
                "count": await archive.count() if archive else 0,
                "mongodb_ok": archive is not None and archive._db is not None,
            },
        })
```

- [ ] **Step 2: 新增 archive 浏览和清空 handler**

在 `create_app()` 中，`/debug/api/db-status` 路由附近添加:

```python
    async def archive_list_handler(request: web.Request) -> web.Response:
        """分页浏览归档消息（debug 专用）。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"messages": [], "has_more": False, "total": 0})

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "100"))
            limit = max(1, min(limit, 500))
        except ValueError:
            limit = 100

        try:
            messages, has_more = await archive.query(before=before, limit=limit)
            total = await archive.count()
            client_msgs = _archive_to_client(messages)
            return web.json_response({
                "messages": client_msgs,
                "has_more": has_more,
                "total": total,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def archive_clear_handler(request: web.Request) -> web.Response:
        """清空全部归档（debug 专用）。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"ok": False, "error": "archive not initialized"})
        deleted = await archive.clear_all()
        return web.json_response({"ok": True, "deleted": deleted})

    app.router.add_get("/debug/api/archive", archive_list_handler)
    app.router.add_delete("/debug/api/archive", archive_clear_handler)
```

- [ ] **Step 3: 验证**

```bash
curl http://localhost:8765/debug/api/db-status | python3 -m json.tool
# 预期: 包含 "archive": {"count": ..., "mongodb_ok": true}

curl http://localhost:8765/debug/api/archive?limit=5
# 预期: {"messages": [...], "has_more": ..., "total": ...}

curl -X DELETE http://localhost:8765/debug/api/archive
# 预期: {"ok": true, "deleted": N}
```

- [ ] **Step 4: Commit**

```bash
git add Module/input/server.py
git commit -m "feat: add debug API endpoints for message archive browse/clear"
```

---

### Task 5: 前端聊天页 — 滚动加载历史

**Files:**
- Modify: `static/js/main.js` (loadMoreMessages + handleMessage 分发)
- Modify: `static/js/chat.js` (滚动监听 + 位置修正)
- Modify: `static/js/state.js` (可能需要 earliest_id 追踪)

**Interfaces:**
- Consumes: `GET /api/messages?before=&limit=50` (from Task 3)
- Produces: 聊天区顶部滚动时自动加载历史消息

- [ ] **Step 1: 在 chat.js 中添加滚动监听**

在 `static/js/chat.js` 末尾添加:

```js
// ---- 滚动加载历史 ----
let isLoadingMore = false;
let hasMoreMessages = true;
let earliestMessageId = null;

export function setupScrollLoader() {
  const wrap = document.getElementById('chat-wrap');
  if (!wrap) return;

  wrap.addEventListener('scroll', () => {
    if (wrap.scrollTop <= 80 && !isLoadingMore && hasMoreMessages) {
      loadMoreMessages();
    }
  });
}

export function resetScrollLoader() {
  isLoadingMore = false;
  hasMoreMessages = true;
  earliestMessageId = null;
}

async function loadMoreMessages() {
  isLoadingMore = true;
  const indicator = showLoadingIndicator();

  try {
    let url = '/api/messages?limit=50';
    if (earliestMessageId) {
      url += '&before=' + encodeURIComponent(earliestMessageId);
    }
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();

    hasMoreMessages = data.has_more;
    const messages = data.messages || [];

    if (messages.length > 0) {
      // 记录当前滚动高度用于位置修正
      const wrap = document.getElementById('chat-wrap');
      const oldHeight = wrap.scrollHeight;
      const oldScrollTop = wrap.scrollTop;

      // 渲染到 DOM 顶部
      prependMessages(messages.reverse());

      // 修正滚动位置：保持当前视口不动
      const newHeight = wrap.scrollHeight;
      wrap.scrollTop = newHeight - oldHeight + oldScrollTop;

      // 更新最早消息 ID
      if (messages.length > 0) {
        earliestMessageId = messages[0]._id;
      }
    }
  } catch (e) {
    showLoadingError(e);
  } finally {
    isLoadingMore = false;
    removeLoadingIndicator(indicator);
  }
}

function showLoadingIndicator() {
  const wrap = document.getElementById('chat-wrap');
  const el = document.createElement('div');
  el.id = 'load-more-indicator';
  el.style.cssText = 'text-align:center;padding:8px;color:#888;font-size:12px;';
  el.textContent = '加载历史消息...';
  wrap.insertBefore(el, wrap.firstChild);
  return el;
}

function removeLoadingIndicator(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

function showLoadingError(e) {
  const wrap = document.getElementById('chat-wrap');
  const el = document.createElement('div');
  el.style.cssText = 'text-align:center;padding:8px;color:#f44336;font-size:12px;cursor:pointer;';
  el.textContent = '加载失败，点击重试';
  el.onclick = () => { el.remove(); loadMoreMessages(); };
  wrap.insertBefore(el, wrap.firstChild);
}
```

- [ ] **Step 2: 在 chat.js 中添加 prependMessages()**

```js
// 向 DOM 顶部插入历史消息（从旧到新排列）
function prependMessages(messages) {
  const chat = document.getElementById('chat');
  for (const m of messages) {
    const div = renderMessage(m);
    if (div) {
      chat.insertBefore(div, chat.firstChild);
    }
  }
}

function renderMessage(m) {
  const role = m.role;
  if (role === 'tool') {
    // tool 消息：简述
    const div = document.createElement('div');
    div.className = 'msg-row tool';
    const bubble = document.createElement('div');
    bubble.className = 'bubble tool';
    bubble.innerHTML = `<span class="tool-name">${esc(m.name || 'tool')}</span>
      ${esc((m.parts || []).map(p => p.content).join('').slice(0, 80))}`;
    div.appendChild(bubble);
    return div;
  }

  const div = document.createElement('div');
  div.className = 'msg-row ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble ' + role;

  // inner_voice
  if (m.inner_voice) {
    const iv = document.createElement('div');
    iv.className = 'inner-voice';
    iv.textContent = m.inner_voice;
    bubble.appendChild(iv);
  }

  // parts（API 已统一转换为 parts 格式）
  const parts = m.parts || [];
  for (const p of parts) {
    const span = document.createElement('span');
    span.textContent = p.content || '';
    bubble.appendChild(span);
  }

  // 图片
  if (m.content && Array.isArray(m.content)) {
    const imgHtml = renderImages(m.content);
    if (imgHtml) {
      const imgDiv = document.createElement('div');
      imgDiv.innerHTML = imgHtml;
      bubble.appendChild(imgDiv);
    }
  }

  div.appendChild(bubble);
  return div;
}
```

- [ ] **Step 3: 在 main.js 中调用 setupScrollLoader**

在 `init()` 函数末尾（`ws.js` 的 `connect()` 调用之后）:

```js
import { setupScrollLoader, resetScrollLoader } from './chat.js';

// 在 init() 中，connect(handleMessage) 之后:
setupScrollLoader();
```

在 `handleMessage` 中，`case 'session'` 分支重置 loader:

```js
case 'session':
  restoreFromServer(msg.messages);
  resetScrollLoader();
  if (msg.token_stats) { ... }
  break;
```

- [ ] **Step 4: 验证**

手动测试:
1. 确保 MongoDB 运行中，服务启动
2. 发送 5+ 条消息（超过 session 20 条需多轮，简单验证用 5 条即可）
3. 刷新页面 → 应看到最近 20 条（或全部）
4. 滚动到顶部 → 应出现 "加载历史消息..." → 如果数据库有旧消息，应渲染出来

- [ ] **Step 5: Commit**

```bash
git add static/js/chat.js static/js/main.js
git commit -m "feat: add scroll-to-load history in chat page"
```

---

### Task 6: Debug 页面 — Message Archive 面板

**Files:**
- Modify: `static/debug.html`

**Interfaces:**
- Consumes: `GET /debug/api/db-status` (扩展后, from Task 4), `GET /debug/api/archive`, `DELETE /debug/api/archive`
- Produces: 数据库 Tab 第三个面板 "Message Archive (MongoDB)"

- [ ] **Step 1: 修改 loadDbPanel 加载 archive 数据**

在 `loadDbPanel()` 函数中，`Promise.all` 增加 archive 请求:

```js
async function loadDbPanel() {
  document.getElementById('db-updated').textContent = '加载中...';
  try {
    const [status, ltm, sessions, archive] = await Promise.all([
      fetch('/debug/api/db-status').then(r => r.json()),
      fetch('/debug/api/ltm?limit=200').then(r => r.json()),
      fetch('/debug/api/sessions').then(r => r.json()),
      fetch('/debug/api/archive?limit=100').then(r => r.json()),
    ]);
    document.getElementById('db-updated').textContent = '最后刷新: ' + new Date().toLocaleTimeString();
    renderDbPanels(status, ltm, sessions, archive);
  } catch (e) {
    document.getElementById('db-updated').textContent = '加载失败: ' + e.message;
  }
}
```

- [ ] **Step 2: 修改 renderDbPanels 和新增 renderArchivePanel**

修改函数签名:

```js
function renderDbPanels(status, ltm, sessions, archive) {
  _ltmData = ltm;
  _sessionData = sessions.sessions || [];
  _archiveData = archive;

  const ltmPanel = renderLtmPanel(status);
  const sessionPanel = renderSessionPanel(status);
  const archivePanel = renderArchivePanel(status);
  document.getElementById('db-panels').innerHTML = ltmPanel + sessionPanel + archivePanel;
  // ... 绑定搜索事件 ...
}
```

添加 `_archiveData` 变量:

```js
let _ltmData = { entries: [], total: 0 };
let _sessionData = [];
let _archiveData = { messages: [], has_more: false, total: 0 };
```

新增 `renderArchivePanel()`:

```js
function renderArchivePanel(status) {
  const s = status.archive || {};
  const data = _archiveData;
  const rows = (data.messages || []).map(m => {
    const role = m.role || '?';
    const ts = m.timestamp ? m.timestamp.slice(0, 16).replace('T', ' ') : '';
    const parts = m.parts || [];
    let text = parts.map(p => p.content || '').join('');
    // tool 消息特殊显示
    if (role === 'tool') {
      text = `[${esc(m.name || 'tool')}] ${text.slice(0, 60)}`;
    }
    return `<div class="db-item">
      <span class="text">
        <span class="role-tag ${role}">${esc(role)}</span>
        <span style="font-size:10px;color:var(--muted)">${esc(ts)}</span>
        ${expandable(text, 80)}
      </span>
    </div>`;
  }).join('');

  return `<div class="card">
    <h2>Message Archive (MongoDB)</h2>
    <div class="db-stat">
      条目: <b>${data.total}</b> &nbsp; MongoDB: ${ok(s.mongodb_ok)} &nbsp; 当前 session: <b>${esc(status.session?.current_id || 'default')}</b>
    </div>
    <div class="db-search">
      <input id="archive-search" placeholder="搜索消息内容...">
      <select id="archive-role-filter">
        <option value="">全部</option>
        <option value="user">user</option>
        <option value="assistant">assistant</option>
        <option value="tool">tool</option>
      </select>
    </div>
    <div style="max-height:400px;overflow-y:auto;margin-bottom:8px;" id="archive-list">
      ${rows || '<div class="empty">暂无消息</div>'}
    </div>
    <div style="display:flex;justify-content:space-between;">
      <button class="db-btn" id="archive-load-more-btn" ${data.has_more ? '' : 'disabled'}>加载更多</button>
      <button class="db-btn danger" onclick="confirmClearArchive()">清空全部</button>
    </div>
  </div>`;
}
```

- [ ] **Step 3: 添加搜索/过滤 + 加载更多 handler**

```js
// 搜索和过滤绑定（在 renderDbPanels 末尾）
document.getElementById('archive-search')?.addEventListener('input', filterArchive);
document.getElementById('archive-role-filter')?.addEventListener('change', filterArchive);
document.getElementById('archive-load-more-btn')?.addEventListener('click', loadMoreArchive);

function filterArchive() {
  const search = (document.getElementById('archive-search')?.value || '').trim().toLowerCase();
  const role = document.getElementById('archive-role-filter')?.value || '';
  const items = document.querySelectorAll('#archive-list .db-item');
  items.forEach(item => {
    const text = item.textContent.toLowerCase();
    const hasRole = role ? text.includes('>' + role + '<') || item.querySelector('.role-tag.' + role) : true;
    const matches = (!search || text.includes(search)) && hasRole;
    item.style.display = matches ? '' : 'none';
  });
}

async function loadMoreArchive() {
  const btn = document.getElementById('archive-load-more-btn');
  btn.disabled = true;
  btn.textContent = '加载中...';
  const messages = _archiveData.messages || [];
  const lastId = messages.length > 0 ? messages[messages.length - 1]._id : null;
  try {
    const r = await fetch('/debug/api/archive?limit=100' + (lastId ? '&before=' + lastId : ''));
    const data = await r.json();
    _archiveData.messages = messages.concat(data.messages || []);
    _archiveData.has_more = data.has_more;
    _archiveData.total = data.total;
    // 重新渲染面板
    const status = { archive: { count: data.total, mongodb_ok: true } };
    // 简易重渲染：直接更新 archive panel
    loadDbPanel();
  } catch (e) {
    btn.textContent = '加载失败，重试';
    btn.disabled = false;
  }
}
```

- [ ] **Step 4: 添加清空 handler**

```js
function confirmClearArchive() {
  document.getElementById('confirm-title').textContent = '清空全部消息归档';
  document.getElementById('confirm-msg').textContent = '将删除 MongoDB message_archive 中的所有消息。此操作不可撤销。';
  document.getElementById('confirm-yes-btn').onclick = async () => {
    closeConfirmModal();
    try {
      await fetch('/debug/api/archive', { method: 'DELETE' });
      loadDbPanel();
    } catch (e) { alert('清空失败: ' + e.message); }
  };
  document.getElementById('confirm-modal').classList.add('show');
}
```

- [ ] **Step 5: 修改 CSS 布局 — 三栏**

debug.html 的 `.db-panel` 改为三栏布局:

```css
.db-panel {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
}
@media (max-width: 1200px) {
  .db-panel { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 900px) {
  .db-panel { grid-template-columns: 1fr; }
}
```

- [ ] **Step 6: 验证**

手动测试:
1. 打开 debug 页面 → 数据库 Tab
2. 应看到三个面板: Long-Term Memory | Session 管理 | Message Archive
3. Archive 面板显示消息总数，支持搜索、角色过滤
4. 点击 "加载更多" 应分页加载
5. 点击 "清空全部" → 确认 → 数据清空

- [ ] **Step 7: Commit**

```bash
git add static/debug.html
git commit -m "feat: add Message Archive panel to debug page"
```

---

## 自检

| 检查项 | 状态 |
|--------|------|
| Spec 每条需求都有对应 Task | ✓ Component 1→Task1, 2→Task2, 3→Task2, 4→Task3/4, 5→Task5, 6→Task6 |
| 无 TBD/TODO/placeholder | ✓ |
| 所有接口签名一致 | ✓ `MessageArchive.connect(db: DocumentDBBackend)` → agent 传 `self._session.db` |
| 已有测试覆盖 | ✓ 单元测试 (Task1) + 集成测试 (Task2)，共 10 个 |
| 向后兼容 | ✓ archive=None 时 add_message/query 行为与旧版一致 |
| 所有 import 路径正确 | ✓ `core.memory.message_archive.MessageArchive` |
