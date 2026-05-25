"""真实 API 集成测试。运行: python -m Module.llm"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from . import LLMFactory


async def main():
    backend = LLMFactory.create("deepseek/deepseek-chat")

    # 1. 普通对话
    print("=== 普通对话 (api.deepseek.com) ===")
    r = await backend.chat(
        [{"role": "user", "content": '你好，请说"测试成功"'}]
    )
    print(f"回复: {r}")

    # 2. prefix continuation — 强制 JSON
    print()
    print("=== Prefix 续写 (api.deepseek.com/beta) ===")
    r = await backend.chat(
        [
            {
                "role": "system",
                "content": "以JSON格式回复，包含name和greeting两个字段",
            },
            {"role": "assistant", "content": "{\n", "prefix": True},
        ]
    )
    print(f"回复: {r}")

    # 3. 流式
    print()
    print("=== 流式输出 ===")
    async for chunk in backend.chat_stream(
        [{"role": "user", "content": "用一句话介绍你自己"}]
    ):
        print(chunk, end="", flush=True)
    print()

    await backend.close()
    print()

    # 4. V4 Flash
    backend2 = LLMFactory.create("deepseek/deepseek-v4-flash")
    print("=== V4 Flash 普通对话 ===")
    r = await backend2.chat(
        [{"role": "user", "content": '你好，请说"测试成功"'}]
    )
    print(f"回复: {r}")
    print()
    print("=== V4 Flash 流式输出 ===")
    async for chunk in backend2.chat_stream(
        [{"role": "user", "content": "用一句话介绍你自己"}]
    ):
        print(chunk, end="", flush=True)
    print()
    await backend2.close()

    print()
    print("全部测试完成!")


if __name__ == "__main__":
    asyncio.run(main())
