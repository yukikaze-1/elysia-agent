"""真实 API 集成测试。运行: python -m Module.embedding"""

import time

from Module._common.main_runner import run_module_main
from . import EmbeddingFactory


async def main():
    # 1. HuggingFace local
    print("=" * 50)
    print("HuggingFace (BAAI/bge-large-zh-v1.5)")
    print("=" * 50)
    try:
        backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
        print(f"  dimension = {backend.dimension}")

        t0 = time.perf_counter()
        vec = await backend.embed_query("你好，世界")
        elapsed = time.perf_counter() - t0
        print(f"  embed_query('你好，世界')  elapsed={elapsed * 1000:.0f}ms  dims={len(vec)}  "
              f"sample={vec[:3]}...")

        t0 = time.perf_counter()
        vecs = await backend.embed_documents(["第一段文本", "第二段文本", "第三段文本"])
        elapsed = time.perf_counter() - t0
        print(f"  embed_documents(3 texts)  elapsed={elapsed * 1000:.0f}ms  count={len(vecs)}  "
              f"dims={len(vecs[0])}")

        await backend.close()
    except Exception as e:
        print(f"  [跳过] {e}")

    print()

    # 2. OpenAI compatible
    print("=" * 50)
    print("OpenAI (text-embedding-3-large)")
    print("=" * 50)
    try:
        backend = EmbeddingFactory.create("openai/text-embedding-3-large")

        t0 = time.perf_counter()
        vec = await backend.embed_query("你好，世界")
        elapsed = time.perf_counter() - t0
        print(f"  embed_query('你好，世界')  elapsed={elapsed * 1000:.0f}ms  dims={len(vec)}  "
              f"sample={vec[:3]}...")
        print(f"  dimension = {backend.dimension}")

        t0 = time.perf_counter()
        vecs = await backend.embed_documents(["第一段文本", "第二段文本", "第三段文本"])
        elapsed = time.perf_counter() - t0
        print(f"  embed_documents(3 texts)  elapsed={elapsed * 1000:.0f}ms  count={len(vecs)}  "
              f"dims={len(vecs[0])}")

        await backend.close()
    except Exception as e:
        print(f"  [跳过] {e}")

    print()
    print("全部测试完成!")


if __name__ == "__main__":
    run_module_main(main)
