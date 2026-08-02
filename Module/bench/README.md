# Bench Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一延迟基准测试。覆盖 LLM、TTS、STT 三个模块的流式和非流式路径。

## 使用

```python
from Module.bench import LatencyBench

bench = LatencyBench(
    tts_url="http://localhost:9880",     # 或设置环境变量 TTS_API_URL
    stt_url="http://localhost:20042",    # 或设置环境变量 STT_API_URL
    audio_file="data/ref/Elysia.wav",
)
await bench.run_all()
```

或直接运行：

```bash
python -m Module.bench
```

运行前会自动检测各服务可用性：
- **LLM** — 检查 `DEEPSEEK_API_KEY` 是否配置，首条 API 调用是否成功
- **TTS** — 依次测试 GPT-SoVits、MiniMax、Custom (OpenAI 兼容)，无对应 env var 时跳过
- **STT** — 依次测试 SenseVoice、Custom (OpenAI 兼容)，无对应 env var 时跳过

不可用的服务会打印 `[跳过]` 并给出修复建议（如 "请确认 TTS 服务已启动"）。

## 测试内容

| 模块 | 方法 | 指标 |
|------|------|------|
| 全链路 | `bench_e2e()` | 依次跑通 LLM+TTS+STT 端到端 |
| LLM | `chat()` | total elapsed, reply chars |
| LLM | `chat_stream()` | TTFT, total elapsed, chars/sec |
| LLM | `chat(prefix)` | total elapsed |
| LLM | `chat(no-thinking)` | total elapsed |
| LLM | `chat_stream(no-think)` | TTFT, total elapsed, chars/sec |
| TTS | `synthesize()` | total elapsed, RTF |
| TTS | `synthesize_stream()` | TTFC, total elapsed, RTF |
| STT | `transcribe()` | total elapsed, RTF |
| STT | `transcribe_stream()` | first chunk, total elapsed, RTF |

LLM 默认测试 V4 Pro 和 V4 Flash 两个模型，各含默认/关闭思考两组对比。

## 指标说明

- **TTFT** — Time To First Token，首 token 延迟
- **TTFC** — Time To First Chunk，首个音频 chunk 延迟
- **RTF** — Real-Time Factor，实时率 = 音频时长 / 处理耗时。> 1x 表示比实时快
- **rate** — chars/sec，流式产出速率

## 备注

此模块本质是工具而非 Module 子模块，后续将移动到 `tools/bench/`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/llm/README.md](../llm/README.md) | LLM 模块（bench 测试目标之一） |
| [Module/tts/README.md](../tts/README.md) | TTS 模块（bench 测试目标之一） |
