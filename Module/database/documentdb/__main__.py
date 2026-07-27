"""真实 API 集成测试。运行: python -m Module.database.documentdb"""

import asyncio
import time
from dotenv import load_dotenv

load_dotenv()

from . import DocumentDBFactory

TEST_DB = "test_elysia"
TEST_COLLECTION = "chat_history"


async def main():
    print("MongoDB 集成测试")
    print()

    backend = DocumentDBFactory.create(f"mongodb/{TEST_DB}")

    # 1. Index
    print("=== 创建索引 ===")
    t0 = time.perf_counter()
    idx = await backend.ensure_index(
        TEST_COLLECTION,
        [("session_id", 1), ("timestamp", -1)],
    )
    print(f"  索引: {idx}  耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")

    idx2 = await backend.ensure_index(
        TEST_COLLECTION,
        [("message_id", 1)],
        unique=True,
    )
    print(f"  唯一索引: {idx2}")

    # 2. Insert one
    print()
    print("=== 插入单条 ===")
    from core.utils import new_message_id
    msg_id = new_message_id()
    t0 = time.perf_counter()
    doc_id = await backend.insert_one(TEST_COLLECTION, {
        "message_id": msg_id,
        "session_id": "test_session",
        "role": "user",
        "content": "你好，爱莉希雅",
        "timestamp": time.time(),
        "type": "text",
    })
    print(f"  单条 _id={doc_id}  耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 3. Insert many
    print()
    print("=== 批量插入 100 条 ===")
    docs = []
    for i in range(100):
        docs.append({
            "message_id": new_message_id(),
            "session_id": "test_session",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"消息内容 {i}",
            "timestamp": time.time() + i,
            "type": "text",
        })
    t0 = time.perf_counter()
    ids = await backend.insert_many(TEST_COLLECTION, docs)
    print(f"  插入 {len(ids)} 条  耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")

    # 4. Count
    print()
    print("=== 计数 ===")
    n = await backend.count(TEST_COLLECTION, {"session_id": "test_session"})
    print(f"  test_session 共 {n} 条记录")

    # 5. Find (sorted + paginated)
    print()
    print("=== 分页查询 (最近 3 条) ===")
    t0 = time.perf_counter()
    results = await backend.find(
        TEST_COLLECTION,
        {"session_id": "test_session"},
        sort=[("timestamp", -1)],
        limit=3,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  查询耗时: {elapsed:.0f}ms")
    for r in results:
        print(f"    role={r['role']}  content={r['content'][:20]}...  ts={r['timestamp']:.0f}")

    # 6. Find by message_id
    print()
    print("=== 按 message_id 查找 ===")
    results = await backend.find(
        TEST_COLLECTION,
        {"message_id": msg_id},
        limit=1,
    )
    if results:
        print(f"  找到: role={results[0]['role']}  content={results[0]['content']}")

    # 7. Cleanup
    print()
    print("=== 清理 ===")
    deleted = await backend.delete_many(TEST_COLLECTION, {"session_id": "test_session"})
    print(f"  删除 {deleted} 条")

    await backend.close()
    print()
    print("全部测试完成!")


if __name__ == "__main__":
    asyncio.run(main())
