"""统一延迟基准测试。覆盖 LLM、TTS、STT 三个模块的流式和非流式路径。

>> 备注：此模块后续将移动至 tools/bench/，不作为 Module 的子模块。"""

import asyncio
import os
import time
import wave

import httpx
from dotenv import load_dotenv

load_dotenv()

from Module.llm import LLMFactory
from Module.stt import STTFactory
from Module.tts import TTSFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_AUDIO = os.path.join(PROJECT_ROOT, "ref", "Elysia", "Elysia.wav")


class LatencyBench:
    TTS_BYTES_PER_SECOND = 64000  # 32000 Hz * 1 channel * 2 bytes/sample

    def __init__(
        self,
        *,
        audio_file: str = DEFAULT_AUDIO,
        tts_url: str = "http://192.168.1.18:9880",
        stt_url: str = "",
    ):
        self.audio_file = audio_file
        self.tts_url = tts_url
        self.stt_url = stt_url or os.getenv("SENSEVOICE_API_URL", "http://localhost:20042")

    # ---- helpers ----

    @staticmethod
    def _fmt(seconds: float) -> str:
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        return f"{seconds:.3f}s"

    @staticmethod
    def _rtf(audio_duration: float, elapsed: float) -> str:
        if elapsed <= 0:
            return "-"
        factor = audio_duration / elapsed
        return f"{factor:.2f}x{' (realtime)' if factor >= 1 else ''}"

    @staticmethod
    async def _probe_http(url: str, label: str, timeout: float = 3.0) -> bool:
        """短超时探测 HTTP 服务是否可达。不可达打印跳过信息并返回 False。"""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.get(f"{url}/")
        except Exception:
            print(f"  [跳过] 无法连接 {label} ({url})，请确认服务已启动")
            return False
        return True

    # ---- public ----

    async def run_all(self) -> None:
        print("Module 延迟基准测试")
        print()
        await self.bench_llm("deepseek/deepseek-v4-pro", "V4 Pro")
        await self.bench_llm("deepseek/deepseek-v4-flash", "V4 Flash")
        await self.bench_tts()
        await self.bench_stt()
        await self.bench_e2e()
        print()
        print("全部测试完成!")

    # ---- 端到端管线 ----

    async def bench_e2e(self) -> None:
        """端到端管线延迟: start → first delta → complete (不含 TTS/STT)."""
        print("=" * 50)
        print("端到端管线 (Agent process_message)")
        print("=" * 50)

        if not os.getenv("DEEPSEEK_API_KEY"):
            print("  [跳过] 未设置 DEEPSEEK_API_KEY")
            return

        try:
            from core.app_config import AppConfig
            from Module.config.loader import ConfigLoader
            config = ConfigLoader(AppConfig, yaml_path="config.yaml").load()
            # 纯文本模式，不依赖 TTS/STT
            config.tts.enabled = False
            config.stt.enabled = False
        except Exception as e:
            print(f"  [跳过] 配置加载失败: {e}")
            return

        from core.agent import Agent
        agent = Agent(config=config)

        try:
            t_setup = time.perf_counter()
            await agent.start()
            setup_ms = (time.perf_counter() - t_setup) * 1000
            print(f"  启动耗时: {int(setup_ms)} ms")
            print(f"  记忆条目: {agent._long_term.count}")
            print()

            messages = [
                ("简单问候", "你好，简单打个招呼"),
                ("记忆搜索", "我叫妖梦，你还记得我最喜欢的颜色吗？"),
                ("Web 搜索", "帮我查一下今天北京的天气"),
            ]

            for label, msg in messages:
                t0 = time.perf_counter()
                await agent.process_message(msg)
                # 等离开 IDLE（处理已开始），再等回到 IDLE（处理完成）
                for _ in range(500):
                    if agent._fsm.state != "idle":
                        break
                    await asyncio.sleep(0.01)
                for _ in range(5000):
                    if agent._fsm.state == "idle":
                        break
                    await asyncio.sleep(0.01)
                total_ms = (time.perf_counter() - t0) * 1000
                print(f"  {label}: {self._fmt(total_ms / 1000)}  "
                      f"({int(total_ms)} ms)  — \"{msg[:30]}...\"")
                await asyncio.sleep(0.5)  # 间隔，避免 API 限流

        except Exception as e:
            print(f"  [错误] {e}")
        finally:
            await agent.stop()

    # ---- LLM ----

    async def bench_llm(self, model_id: str, label: str) -> None:
        print("=" * 50)
        print(f"LLM ({label})")
        print("=" * 50)

        env_key = "DEEPSEEK_API_KEY"
        if not os.getenv(env_key):
            print(f"  [跳过] 未设置 {env_key}，请在 .env 中配置")
            return

        try:
            backend = LLMFactory.create(model_id)
        except Exception as e:
            print(f"  [跳过] 创建后端失败: {e}")
            return

        try:
            # Non-streaming
            t0 = time.perf_counter()
            reply = await backend.chat(
                [{"role": "user", "content": "用一句话介绍你自己"}]
            )
            elapsed = time.perf_counter() - t0
            print(f"  chat()              total={self._fmt(elapsed):>10}  reply={len(reply)} chars")

            # Streaming
            t0 = time.perf_counter()
            ttft = None
            num_tokens = 0
            async for chunk in backend.chat_stream(
                [{"role": "user", "content": "用一句话介绍你自己"}]
            ):
                if ttft is None:
                    ttft = time.perf_counter() - t0
                num_tokens += len(chunk)
            elapsed = time.perf_counter() - t0
            tps = num_tokens / elapsed if elapsed > 0 else 0
            print(f"  chat_stream()       TTFT={self._fmt(ttft or 0):>10}  "
                  f"total={self._fmt(elapsed):>10}  "
                  f"chars={num_tokens}  rate={tps:.0f} chars/s")

            # Prefix (non-streaming)
            t0 = time.perf_counter()
            reply = await backend.chat([
                {"role": "system", "content": "以JSON格式回复，包含name和greeting两个字段"},
                {"role": "assistant", "content": "{\n", "prefix": True},
            ])
            elapsed = time.perf_counter() - t0
            print(f"  chat(prefix)        total={self._fmt(elapsed):>10}  reply={len(reply)} chars")

            # No thinking — non-streaming
            t0 = time.perf_counter()
            reply = await backend.chat(
                [{"role": "user", "content": "用一句话介绍你自己"}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            elapsed = time.perf_counter() - t0
            print(f"  chat(no-thinking)   total={self._fmt(elapsed):>10}  reply={len(reply)} chars")

            # No thinking — streaming
            t0 = time.perf_counter()
            ttft = None
            num_tokens = 0
            async for chunk in backend.chat_stream(
                [{"role": "user", "content": "用一句话介绍你自己"}],
                extra_body={"thinking": {"type": "disabled"}},
            ):
                if ttft is None:
                    ttft = time.perf_counter() - t0
                num_tokens += len(chunk)
            elapsed = time.perf_counter() - t0
            tps = num_tokens / elapsed if elapsed > 0 else 0
            print(f"  chat_stream(no-think) TTFT={self._fmt(ttft or 0):>10}  "
                  f"total={self._fmt(elapsed):>10}  "
                  f"chars={num_tokens}  rate={tps:.0f} chars/s")

        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "connect" in msg:
                print(f"  [跳过] 无法连接 DeepSeek API，请检查网络或 DEEPSEEK_API_BASE")
            elif "unauthorized" in msg or "api key" in msg:
                print(f"  [跳过] API Key 无效，请检查 .env 中的 DEEPSEEK_API_KEY")
            else:
                print(f"  [错误] {e}")
        finally:
            await backend.close()

    # ---- TTS ----

    async def bench_tts(self) -> None:
        print()
        print("=" * 50)
        print("TTS (GPT-SoVits)")
        print("=" * 50)

        try:
            tts = TTSFactory.create("gpt-sovits/default", base_url=self.tts_url)
        except Exception as e:
            print(f"  [跳过] 创建 TTS 后端失败: {e}")
            return

        if not await self._probe_http(self.tts_url, "GPT-SoVits"):
            await tts.close()
            return

        text = "你好，今天天气真不错呢。"

        try:
            # Non-streaming
            t0 = time.perf_counter()
            wav = await tts.synthesize(text, character="Elysia")
            elapsed = time.perf_counter() - t0
            audio_dur = len(wav) / self.TTS_BYTES_PER_SECOND
            print(f"  synthesize()        total={self._fmt(elapsed):>10}  "
                  f"audio={self._fmt(audio_dur)}  RTF={self._rtf(audio_dur, elapsed)}")

            # Streaming
            t0 = time.perf_counter()
            ttfc = None
            chunks: list[bytes] = []
            async for chunk in tts.synthesize_stream(text, character="Elysia"):
                if ttfc is None:
                    ttfc = time.perf_counter() - t0
                chunks.append(chunk)
            elapsed = time.perf_counter() - t0
            total_bytes = sum(len(c) for c in chunks)
            audio_dur = total_bytes / self.TTS_BYTES_PER_SECOND
            print(f"  synthesize_stream() TTFC={self._fmt(ttfc or 0):>10}  "
                  f"total={self._fmt(elapsed):>10}  "
                  f"audio={self._fmt(audio_dur)}  RTF={self._rtf(audio_dur, elapsed)}")

        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "connect" in msg:
                print(f"  [跳过] 无法连接 GPT-SoVits ({self.tts_url})，请确认 TTS 服务已启动")
            else:
                print(f"  [错误] {e}")
        finally:
            await tts.close()

    # ---- STT ----

    async def bench_stt(self) -> None:
        print()
        print("=" * 50)
        print("STT (SenseVoice)")
        print("=" * 50)

        try:
            backend = STTFactory.create("sensevoice/sense-voice", base_url=self.stt_url)
        except Exception as e:
            print(f"  [跳过] 创建 STT 后端失败: {e}")
            return

        if not await self._probe_http(self.stt_url, "SenseVoice"):
            await backend.close()
            return

        if not os.path.exists(self.audio_file):
            print(f"  [跳过] 测试音频不存在: {self.audio_file}")
            await backend.close()
            return

        with wave.open(self.audio_file, "rb") as wf:
            audio_dur = wf.getnframes() / wf.getframerate()
        with open(self.audio_file, "rb") as f:
            audio_bytes = f.read()

        try:
            # Non-streaming
            t0 = time.perf_counter()
            text = await backend.transcribe(audio_bytes)
            elapsed = time.perf_counter() - t0
            print(f"  transcribe()        total={self._fmt(elapsed):>10}  "
                  f"audio={self._fmt(audio_dur)}  RTF={self._rtf(audio_dur, elapsed)}  "
                  f"result={len(text)} chars")

            # Streaming
            t0 = time.perf_counter()
            first = None
            num_chunks = 0
            async for chunk in backend.transcribe_stream(audio_bytes):
                if first is None:
                    first = time.perf_counter() - t0
                num_chunks += 1
            elapsed = time.perf_counter() - t0
            print(f"  transcribe_stream() first={self._fmt(first or 0):>10}  "
                  f"total={self._fmt(elapsed):>10}  "
                  f"audio={self._fmt(audio_dur)}  RTF={self._rtf(audio_dur, elapsed)}  "
                  f"chunks={num_chunks}")

        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "connect" in msg:
                print(f"  [跳过] 无法连接 SenseVoice ({self.stt_url})，请确认 STT 服务已启动")
            else:
                print(f"  [错误] {e}")
        finally:
            await backend.close()
