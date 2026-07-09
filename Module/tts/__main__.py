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

    # --- MiniMax TTS (云端) ---
    import os
    if os.getenv("MINIMAX_API_KEY"):
        print("\n=== MiniMax TTS ===")
        try:
            mm = TTSFactory.create("minimax/speech-2.8-turbo")
            result = await mm.test()
            print(f"  test: {result}")
            if result.get("ok"):
                audio = await mm.synthesize(
                    "你好，今天天气真好(sighs)，当然了！",
                    character="default",
                )
                print(f"  synthesize: {len(audio)} bytes")
            await mm.close()
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print("\n=== MiniMax TTS === 跳过（MINIMAX_API_KEY 未设置）")

    print("\nDone.")


if __name__ == "__main__":
    run_module_main(main, load_env=False)
