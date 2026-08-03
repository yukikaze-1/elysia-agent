# 数据库清理脚本设计

## 概述

为测试方便，提供一个 `scripts/clean_db.py` 脚本，一键清理 Elysia Agent 的所有数据库内容（MongoDB + Milvus + 本地持久化文件）。

## 背景

Elysia Agent 使用三个存储后端：

| 存储 | 连接方式 | 数据库/集合 | 用途 |
|------|---------|-------------|------|
| MongoDB (`default`) | `MONGODB_URI` | `sessions` | Session 持久化 |
| MongoDB (`default`) | `MONGODB_URI` | `message_archive` | 消息归档（append-only） |
| MongoDB (`default`) | `MONGODB_URI` | `token_usage` | Token 统计 |
| Milvus | `MILVUS_URI` | `long_term_memory` | 长期记忆向量存储 |
| 本地文件 | `data/` | `session_default.json`, `token_usage_pending.jsonl`, `scheduler_checkpoint.json`, `data/cache/vision/*.json` | 离线 fallback / 状态 / 视觉缓存 |

## CLI 接口

```
usage: clean_db.py [-h] [-f] [--mongo-only] [--milvus-only] [--files-only] [--dry-run]

清理 Elysia Agent 所有数据库内容

options:
  -h             显示帮助
  -f, --force    跳过确认提示，直接执行
  --mongo-only   只清理 MongoDB（sessions, message_archive, token_usage）
  --milvus-only  只清理 Milvus（long_term_memory）
  --files-only   只清理本地 data/ 文件
  --dry-run      仅打印将要清理的内容，不实际执行
```

- `--mongo-only`、`--milvus-only`、`--files-only` 可组合使用
- 都不传则清理全部
- 无 `-f` 时打印目标列表并要求输入 `yes` 确认

## 清理逻辑

### 设计原则：复用组件级 `clear_all()` 方法

避免在脚本中 hardcode collection 名和维度常量，由各组件维护自己的清理逻辑作为唯一真实源。为此需要三个前置变更：

**前置变更 A — SessionMemory 添加 `COLLECTION` 常量**

`core/memory/session.py` 中 `"sessions"` 字符串散落在 6 处，添加类常量：

```python
class SessionMemory:
    COLLECTION = "sessions"
```

**前置变更 B — TokenTracker 添加 `COLLECTION` 常量 + `clear_all()`**

`core/token_tracker.py` 中 `"token_usage"` 是 hardcode 字符串，添加：

```python
class TokenTracker:
    COLLECTION = "token_usage"

    @staticmethod
    async def clear_all(db) -> int:
        """清空所有 token_usage 记录。返回删除数量。"""
        return await db.delete_many(TokenTracker.COLLECTION, {})
```

**前置变更 C — LongTermMemory.clear_all() 自包含化**

当前的 `clear_all()` 依赖先调用 `connect()` 设置 `self._vector_db`，否则 `if self._vector_db:` 分支跳过，Milvus 实际未被清理。同时 `connect()` 会调用 `_load_from_milvus()` 加载全部条目，清理场景下纯属浪费。

修改为：`clear_all()` 无需先 `connect()`，内部自行处理连接生命周期：

```python
async def clear_all(self) -> int:
    """清空所有记忆（内存 + Milvus 重建 collection）。可独立调用，无需先 connect()。"""
    async with self._write_lock:
        count = len(self._entries)
        self._entries.clear()
        self._absorbed_ids.clear()
        self._dirty_ids.clear()

        if self._vector_db is not None:
            # 已通过 connect() 持有连接 → 直接操作
            await self._vector_db.drop_collection(self.COLLECTION)
            await self._vector_db.create_collection(
                self.COLLECTION, dimension=self.DIMENSION)
        else:
            # 无已有连接 → 自行创建临时连接
            from Module.database.vectordb import VectorDBFactory
            vector_db = VectorDBFactory.create("milvus/memory")
            try:
                if await vector_db.collection_exists(self.COLLECTION):
                    await vector_db.drop_collection(self.COLLECTION)
                await vector_db.create_collection(
                    self.COLLECTION, dimension=self.DIMENSION)
            finally:
                await vector_db.close()
    return count
```

- 有 `_vector_db`（Agent 正常流程）→ 复用已有连接，行为不变
- 无 `_vector_db`（脚本直接调用）→ 创建临时连接，清理后 `finally` 关闭
- 异常直接传播，不静默吞掉（解决了 `connect()` 失败返回 False → `clear_all()` 静默跳过的问题）

### MongoDB

通过 `DocumentDBFactory.create("mongodb/default")` 获取 backend，然后：

| 目标 | 方法 | 来源 |
|------|------|------|
| `sessions` | `db.delete_many(SessionMemory.COLLECTION, {})` | SessionMemory.COLLECTION 常量 |
| `message_archive` | `MessageArchive()` → `connect(db)` → `clear_all()` | 复用已有 clear_all() |
| `token_usage` | `TokenTracker.clear_all(db)` | 新增的静态方法 |

关闭：`finally` 块中调用 `db.close()` 关闭 AsyncIOMotorClient 连接池。

### Milvus

直接调用 `LongTermMemory.clear_all()`（前置变更 C 已使其自包含，无需先 `connect()`）：

```python
ltm = LongTermMemory()
await ltm.clear_all()
```

- `clear_all()` 内部自行创建 VectorDBFactory 连接、drop + recreate collection、关闭连接
- collection 名、维度、索引参数全部由组件内部维护
- 异常直接传播到脚本的错误处理层，不会被静默吞掉
- `finally` 块中调用 `ltm.close()` 清理 embed task 等资源

### 本地文件

以脚本自身目录为基准解析 `data/` 路径（`Path(__file__).resolve().parent.parent / "data"`），不依赖 CWD：

| 路径 | 说明 |
|------|------|
| `data/session_default.json` | session fallback |
| `data/token_usage_pending.jsonl` | token 统计缓冲区 |
| `data/scheduler_checkpoint.json` | 任务调度器状态 |
| `data/cache/vision/*.json` | 视觉 LLM 缓存文件（glob 匹配，逐个删除） |

## 错误处理

- **连接失败**：打印错误信息，exit code 1
- **部分成功**：`asyncio.gather(*tasks, return_exceptions=True)`，每个任务独立完成，不因一个失败而取消其余。最后汇总报告每个目标的成功/失败状态
- **`--dry-run`**：检查连接可用性，让用户在真正执行前发现问题
- **清理收尾**：无论成功与否，在 `finally` 中调用 `db.close()`（MongoDB）和 `ltm.close()`（Milvus）释放连接和资源
- **exit code**：全部成功为 0，任一失败为 1

## 代码结构

`scripts/clean_db.py`（~120 行）：

```python
# 三个 Cleaner，各自封装一个存储后端的清理逻辑
class MongoCleaner:
    # DocumentDBFactory.create("mongodb/default")
    # → MessageArchive.clear_all() + TokenTracker.clear_all() + delete_many for sessions
    # → db.close() in finally

class MilvusCleaner:
    # LongTermMemory() → clear_all()（自包含，无需 connect()）
    # finally: ltm.close()

class FileCleaner:
    # Path(__file__).resolve().parent.parent / "data" 为基准
    # 删除 4 类文件（含 vision cache glob）

async def main():
    args = parse_args()
    # 1. 确定清理目标
    # 2. 确认（无 -f 时）
    # 3. asyncio.gather(*tasks, return_exceptions=True)
    # 4. 汇总报告
    # 5. cleanup (close connections)
```

- `.env` 通过 `python-dotenv` 加载，从 `Path(__file__).resolve().parent.parent / ".env"` 读取（不依赖 CWD）
- `argparse` 参数解析

## 排除项

- 不做 Docker volume 清理（`docker compose down -v`），这属于基础设施操作
- 不做 Makefile target（用户选择独立脚本即可）
- 不做 app.py CLI 集成
