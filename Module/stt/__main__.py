"""真实 API 集成测试。运行: python -m Module.stt"""

import os
import sys

from Module._common.main_runner import run_module_main
from . import STTFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_AUDIO = os.path.join(PROJECT_ROOT, "ref", "Elysia", "Elysia.wav")


async def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:20042"
    audio_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_AUDIO

    backend = STTFactory.create("sensevoice/sense-voice", base_url=base_url)

    with open(audio_path, "rb") as f:
        test_audio = f.read()

    print(f"SenseVoice URL: {base_url}")
    print(f"Audio file: {audio_path} ({len(test_audio)} bytes)")
    print()

    # 1. 非流式识别
    print("=== 非流式识别 ===")
    try:
        text = await backend.transcribe(test_audio)
        print(f"识别结果: {text}")
    except Exception as e:
        print(f"识别失败: {e}")

    # 2. 流式识别
    print()
    print("=== 流式识别 ===")
    try:
        async for chunk in backend.transcribe_stream(test_audio):  # type: ignore[misc]
            print(f"流式chunk: {chunk}")
    except Exception as e:
        print(f"流式识别失败: {e}")

    await backend.close()
    print()
    print("全部测试完成!")


if __name__ == "__main__":
    run_module_main(main)
