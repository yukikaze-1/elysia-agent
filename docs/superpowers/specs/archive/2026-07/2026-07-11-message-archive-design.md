# Message Archive — 对话消息永久归档

**日期:** 2026-07-11
**状态:** 待 Review

---

## 背景

当前项目只有 `SessionMemory` 保留对话消息，受 `max_messages=20` 硬截断和 MongoDB 30天 TTL 双重限制。一个多月的测试产生了 1202 次 LLM 调用（350 万 tokens），但原始对话消息只剩 20 条。

`token_usage` 集合已是只增不删的日志模式，消息本身需要同样的永久归档。

## 目标

- 所有对话消息（user / assistant / tool）**永久保留**，不受 `max_messages` 或 TTL 影响
- 前端通过**分页懒加载**获取全量历史
- Session 和 Archive **职责分离**：session 管上下文窗口，archive 管持久化
- debug 页面可浏览、搜索、清空归档

## 非目标

- 不修改 session 的 `max_messages` 或 TTL
- 不修改 `token_usage` 集合
- 不提供归档编辑/单条删除 API
- 不做跨 session 迁移或历史数据导入

---

## 架构

```
add_message (user/assistant/tool)
    │
    ├─→ SessionMemory._messages (deque, maxlen=20)  [现有，不改]
    │       └─→ save() → MongoDB.sessions (30天 TTL)
    │
    └─→ MessageArchive.append()                      [新增]
            └─→ MongoDB.message_archive (永久，只增不删)

前端加载:
    WS 连接 → server 推 session 最近 20 条              [现有，不改]
    滚动到顶 → GET /api/messages?before=&limit=50       [新增]
```

## 组件

### 1. `core/memory/message_archive.py`（新建）

```
class MessageArchive:
    COLLECTION = "message_archive"
    _db: DocumentDBBackend | None

    async def connect(db: DocumentDBBackend) -> bool
        # db: 由外部传入的 MongoDB backend（复用 session 的连接）
        # 确保索引: _id (降序)

    async def append(msg: dict) -> None
        # insert_one，fire-and-forget
        # 消息格式与 session 完全一致，透传 dict

    async def query(*, before: str | None = None,
                   limit: int = 50,
                   session_id: str = "default")
        → (messages: list[dict], has_more: bool)
        # 按 _id 降序分页
        # before 为上一页最后一条的 _id（ObjectId 字符串）
        # 返回: (messages, has_more)

    async def count(session_id: str = "default") → int

    async def clear_all(session_id: str = "default") → int
```

**索引:**

```
collection: message_archive
  - {_id: -1}  ← 分页查询（按插入时间降序）
```

> session_id 字段预留但不建索引——当前只有 `"default"` 一个 session，无需过滤。

**消息格式:** 与现有 `SessionMemory._messages` 完全一致，三种 role 的 dict 结构直接写入，不做转换。

**错误处理:** `append()` 抛异常时不阻塞主流程（由 `add_message` 中的 `create_task` 兜底，异常只记日志）。

### 2. `core/memory/session.py`（修改）

在 `add_message()` 末尾新增两行：

```python
if self._archive:
    asyncio.create_task(self._archive.append(msg))
```

新增属性 `_archive: MessageArchive | None`，通过构造函数注入：

```python
def __init__(self, ..., archive: MessageArchive | None = None):
    self._archive = archive
```

不传 archive 时行为不变（向后兼容，测试不受影响）。

### 3. `core/agent.py`（修改）

初始化链路：

```python
# Agent.__init__()
self._archive = MessageArchive()
self._session = SessionMemory(
    max_messages=config.memory.session_max_messages,
    archive=self._archive,
)

# _start_persistence() — session 先连接，archive 复用同一个 MongoDB backend
await self._session.connect_db()
await self._archive.connect(self._session.db)
```

`SessionMemory.db` 是已有的 public property（`session.py:36`），返回 `DocumentDBBackend | None`。archive 不直接访问 `_db` 私有属性。

### 4. API 端点（`Module/input/server.py` 修改）

| 方法 | 路径 | 用途 | 受众 |
|------|------|------|------|
| `GET` | `/api/messages?before=&limit=50` | 分页获取归档消息 | 前端聊天页 |
| `GET` | `/debug/api/archive?before=&limit=100&session=default` | 分页浏览归档 | debug 页面 |
| `DELETE` | `/debug/api/archive?session=default` | 清空归档 | debug 页面 |

**GET /api/messages 响应:**
```json
{
  "messages": [{...}, {...}],
  "has_more": true,
  "total": 1234
}
```

**GET /debug/api/db-status 响应扩展:**
```json
{
  "ltm": {...},
  "session": {...},
  "archive": {
    "count": 1234,
    "mongodb_ok": true
  }
}
```

消息格式与 `SessionMemory.messages_for_client()` 输出一致，保证前端渲染逻辑复用。

### 5. 前端 — 聊天页（`static/js/`）

**改动文件:** `chat.js`, `main.js`

**行为:**

1. WebSocket 连接后，`restoreFromServer()` 照常渲染最近 20 条（不增加 loading 时间）
2. 聊天区滚动到顶部时触发 `loadMoreMessages()`
3. `loadMoreMessages()` 调用 `GET /api/messages?before=<最早消息_id>`，渲染到 DOM 顶部
4. 滚动位置修正：插入新消息后保持当前视口位置不跳动
5. has_more=false 时停止监听滚动

**不新增 JS 文件。** 逻辑放在现有 `main.js` 和 `chat.js` 中。

### 6. 前端 — debug 页面（`static/debug.html` 修改）

数据库 Tab 新增 "Message Archive (MongoDB)" 面板：

```
┌──────────────────────────────────────────────────┐
│ Message Archive (MongoDB)                        │
│ 条目: 1,234  当前 session: default               │
│ ┌──────────────────────────────────────────┐     │
│ │ [搜索内容...]          [角色过滤▾]        │     │
│ └──────────────────────────────────────────┘     │
│ ┌──────────────────────────────────────────┐     │
│ │ [user]  07-11 00:27  我之前跟你说过...     │     │
│ │ [asst]  07-11 00:27  好，记住了，下周东京   │     │
│ │ [tool]  07-11 00:27  search_memory → 5条  │     │
│ │ ... (100 条/页)                           │     │
│ └──────────────────────────────────────────┘     │
│ [加载更多]                    [清空全部]         │
└──────────────────────────────────────────────────┘
```

- 搜索：前端 substring 过滤
- 角色过滤：user / assistant / tool / 全部
- 分页：100 条/页
- 展开：点击消息展开完整 JSON
- 清空全部：确认弹窗后调 `DELETE /debug/api/archive`

## 数据库全景（更新后）

| 集合 | 用途 | 保留策略 | 索引 |
|------|------|----------|------|
| `sessions` | LLM 上下文窗口 | 20 条, 30天 TTL | `session_id` |
| `message_archive` | **所有消息永久归档** | **永久, 只增不删** | `_id` |
| `token_usage` | LLM token 统计 | 永久, 只增不删 | 无 |
| `long_term_memory` (Milvus) | 记忆摘要 | 90天/1万条(内存), Milvus 不删除 | Milvus auto index |

## 错误处理

| 场景 | 行为 |
|------|------|
| MongoDB 不可达（append） | `create_task` 内 catch 异常，logger.warning，不影响主流程 |
| MongoDB 不可达（query） | 返回空列表 + has_more=false，前端显示"加载失败，点击重试" |
| archive 未初始化 | `add_message` 检查 `self._archive` 是否为 None，跳过写入，行为等同旧版 |

## 验证

```bash
# 1. 单元测试：MessageArchive CRUD
python -m pytest core/memory/tests/test_message_archive.py -v

# 2. 集成测试：add_message 写入 archive
python -m pytest core/tests/ -v -k "archive"

# 3. API 测试
curl http://localhost:8765/api/messages?limit=10
curl http://localhost:8765/debug/api/archive?limit=10

# 4. 手动验收
# - 发送几条消息，刷新页面，滚动到顶应能看到历史
# - debug 页面数据库 Tab 应有 Message Archive 面板
```
