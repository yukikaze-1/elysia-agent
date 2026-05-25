"""真实 API 集成测试。运行: python -m Module.database.vectordb"""

import asyncio
import random
import time

from dotenv import load_dotenv

load_dotenv()

from . import VectorDBFactory

TEST_COLLECTION = "test_bench"


async def main():
    print("Milvus 集成测试")
    print()

    backend = VectorDBFactory.create("milvus/test_bench")

    # 1. Create collection (replace if exists from previous run)
    print(f"=== 创建 collection: {TEST_COLLECTION} (dim=128) ===")
    t0 = time.perf_counter()
    await backend.create_collection(TEST_COLLECTION, dimension=128, replace=True)
    print(f"  耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")

    exists = await backend.collection_exists(TEST_COLLECTION)
    print(f"  collection_exists: {exists}")

    # 2. Insert
    print()
    print("=== 插入 100 条向量 ===")
    vectors = [[random.random() for _ in range(128)] for _ in range(100)]
    metadata = [{"text": f"doc {i}", "category": "a" if i % 2 == 0 else "b"} for i in range(100)]

    t0 = time.perf_counter()
    ids = await backend.insert(TEST_COLLECTION, vectors, metadata)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  插入 {len(ids)} 条, 耗时: {elapsed:.0f}ms")

    # 3. Search
    print()
    print("=== ANN 搜索 ===")
    query_vec = [random.random() for _ in range(128)]
    t0 = time.perf_counter()
    results = await backend.search(
        TEST_COLLECTION, query_vec, top_k=3, output_fields=["text", "category"]
    )
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  搜索耗时: {elapsed:.0f}ms")
    for r in results:
        print(f"    id={r['id']}, text={r['entity'].get('text')}, dist={r['distance']:.4f}")

    # 4. Search with scalar filter
    print()
    print("=== 标量过滤搜索 ===")
    t0 = time.perf_counter()
    results = await backend.search(
        TEST_COLLECTION, query_vec, top_k=5,
        filter_expr='category == "b"',
        output_fields=["text", "category"],
    )
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  过滤搜索耗时: {elapsed:.0f}ms, 返回 {len(results)} 条")

    # 5. Scalar query
    print()
    print("=== 标量查询 ===")
    t0 = time.perf_counter()
    results = await backend.query(
        TEST_COLLECTION, filter_expr='category == "a"', limit=5, output_fields=["text"]
    )
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"  查询耗时: {elapsed:.0f}ms, 返回 {len(results)} 条")
    for r in results[:3]:
        print(f"    id={r['id']}, text={r.get('text')}")

    # 6. Cleanup
    print()
    print("=== 清理 ===")
    t0 = time.perf_counter()
    await backend.drop_collection(TEST_COLLECTION)
    print(f"  drop 耗时: {(time.perf_counter() - t0) * 1000:.0f}ms")

    await backend.close()
    print()
    print("全部测试完成!")


if __name__ == "__main__":
    asyncio.run(main())
