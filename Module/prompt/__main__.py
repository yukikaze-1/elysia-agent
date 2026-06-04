"""集成测试：加载 prompt/ 目录并渲染模板。运行: python -m Module.prompt"""

import os

from dotenv import load_dotenv

load_dotenv()

from .manager import PromptManager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROMPT_DIR = os.path.join(PROJECT_ROOT, "prompt")


def main():
    print("Prompt 模块 — 模板渲染测试")
    print(f"  模板目录: {PROMPT_DIR}")
    print()

    if not os.path.isdir(PROMPT_DIR):
        print("prompt/ 目录不存在，跳过")
        return

    try:
        pm = PromptManager(PROMPT_DIR)
    except Exception as e:
        print(f"初始化失败: {e}")
        return

    # 列出所有模板
    templates = []
    for f in os.listdir(PROMPT_DIR):
        if f.endswith(".j2"):
            templates.append(f)
    print(f"  找到 {len(templates)} 个模板: {templates}")
    print()

    # 尝试渲染第一个模板
    if templates:
        name = templates[0]
        print(f"=== 渲染 {name} ===")
        try:
            result = pm.render(name, name="Elysia")
            print(f"  渲染成功 ({len(result)} 字符)")
            print(f"  前 120 字符: {result[:120]}...")
        except Exception as e:
            print(f"  渲染失败: {e}")

    print()
    print("全部测试完成!")


if __name__ == "__main__":
    main()
