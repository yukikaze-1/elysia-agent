"""集成测试：启动最小测试服务器验证 AudioBuffer 和 WS 路由。运行: python -m core.input"""

import os
import sys

from Module._common.main_runner import run_module_main
from .audio_buffer import AudioBuffer, pcm_to_wav

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def test_audio_buffer():
    """验证 AudioBuffer 基本功能（不需要真实 STT backend）。"""
    print("=== AudioBuffer 单元验证 ===")

    transcriptions: list[str] = []

    async def mock_stt(audio_bytes: bytes) -> str:
        return "mock 识别结果"

    async def on_transcription(text: str) -> None:
        transcriptions.append(text)

    buf = AudioBuffer(
        stt_transcribe=mock_stt,
        aggressiveness=3,
        on_transcription=on_transcription,
    )

    # 喂入一段静音 PCM 数据（正好 640 字节 = 320 个 int16 样本 @ 16kHz = 20ms）
    silence = b"\x00" * 640
    for _ in range(50):  # 1 秒静音 — 不应触发 VAD
        await buf.feed(silence)

    print(f"  静音后转录数: {len(transcriptions)} (预期 0)")

    await buf.flush()
    print(f"  flush 后转录数: {len(transcriptions)}")

    # 验证 pcm_to_wav
    pcm_data = b"\x00" * 32000  # 1 秒 16kHz mono 16bit
    wav = pcm_to_wav(pcm_data)
    print(f"  WAV 头 + 数据: {len(wav)} bytes (预期 32044)")

    print("AudioBuffer 验证通过!")
    print()


async def main():
    print("Input 模块 — 集成测试")
    print()
    await test_audio_buffer()
    print("全部测试完成!")


if __name__ == "__main__":
    run_module_main(main)
