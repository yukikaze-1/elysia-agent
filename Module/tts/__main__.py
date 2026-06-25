from pathlib import Path

from Module._common.main_runner import run_module_main
from . import TTSFactory

TMP = Path(__file__).parent / "tmp"


async def main():
    TMP.mkdir(exist_ok=True)
    tts = TTSFactory.create("gpt-sovits/default", base_url="http://192.168.1.18:9880")

    # Test 1: stream with emotion
    print("=== Test 1: synthesize_stream ===")
    chunks: list[bytes] = []
    async for chunk in tts.synthesize_stream(  # type: ignore[misc]
        "你好，今天天气真不错呢。[微笑] 要一起出去走走吗？",
        character="Elysia",
        emotion="happy",
    ):
        chunks.append(chunk)
        print(f"  chunk: {len(chunk)} bytes")
    total = sum(len(c) for c in chunks)
    (TMP / "test1_stream.wav").write_bytes(b"".join(chunks))
    print(f"  -> tmp/test1_stream.wav  {total} bytes, ~{total / 64000:.1f}s\n")

    # Test 2: non-stream
    print("=== Test 2: synthesize ===")
    wav = await tts.synthesize("好的，我在这里等你。", character="Elysia")
    (TMP / "test2_synthesize.wav").write_bytes(wav)
    print(f"  -> tmp/test2_synthesize.wav  {len(wav)} bytes, ~{len(wav) / 64000:.1f}s\n")

    # Test 3: early break (async generator cleanup)
    print("=== Test 3: early break ===")
    async for chunk in tts.synthesize_stream(  # type: ignore[misc]
        "测试提前中断。", character="Elysia"
    ):
        print(f"  chunk: {len(chunk)} bytes")
        break  # just first chunk

    await tts.close()
    print("\nDone.")


if __name__ == "__main__":
    run_module_main(main, load_env=False)
