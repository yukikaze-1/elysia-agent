# TTS Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

文本转语音模块。提供统一的 `TTSBackend` 接口，通过不同的后端实现对接 GPT-SoVits（本地）、MiniMax（云端）、OpenAI 兼容（云端）等 TTS 服务。

## 文件结构

```
tts/
├── __init__.py               # 导出 TTSBackend, TTSFactory, TTSConfig
├── protocol.py               # TTSBackend ABC
├── registry.py               # 后端注册表 + TTSFactory.create() / from_config()
├── gpt_sovits.py             # GPTSoVitsBackend — 本地 HTTP API
├── gpt_sovits_voices.yaml    # GPT-SoVits 角色音色配置（ref_audio + prompt_text）
├── openai_tts.py             # OpenAICompatTTSBackend — OpenAI 兼容协议
├── minimax_tts.py            # MiniMaxTTSBackend — MiniMax 云端 HTTP API
├── minimax_tts_api.md        # MiniMax 官方 API 参考文档
├── minimax_voices.yaml       # MiniMax 音色映射（character → voice_id）
├── text_preprocess.py        # 文本预处理函数
├── config.py                 # TTSConfig dataclass
├── __main__.py               # 冒烟测试
└── tests/                    # 单元测试
```

## 快速开始

```python
from Module.tts import TTSFactory

# MiniMax 云端（需设置 MINIMAX_API_KEY 环境变量）
tts = TTSFactory.create("minimax/speech-2.8-hd")

# GPT-SoVits 本地
tts = TTSFactory.create("gpt-sovits/default", base_url="http://192.168.1.18:9880")

# 非流式 — 返回完整音频 bytes
wav = await tts.synthesize("你好呀。", character="Elysia")

# 流式 — 分块 yield 音频
async for chunk in tts.synthesize_stream("你好呀。", character="Elysia"):
    send_audio_chunk(chunk)

await tts.close()
```

## 支持的后端

| Provider | 后端类 | 协议 | 部署 | 环境变量 |
|----------|--------|------|------|----------|
| `gpt-sovits` | `GPTSoVitsBackend` | 专有 HTTP API | 本地 | `GPT_SOVITS_URL` |
| `minimax` | `MiniMaxTTSBackend` | MiniMax HTTP API | 云端 | `MINIMAX_API_KEY` |
| `custom` | `OpenAICompatTTSBackend` | OpenAI `/v1/audio/speech` | 云端 | `CUSTOM_TTS_API_KEY` |

## 接口

```python
class TTSBackend(ABC):
    @property
    def provider_name(self) -> str: ...       # 'gpt-sovits' / 'minimax' / 'custom'
    @property
    def stream_format(self) -> str: ...       # 'wav' / 'mp3'
    def get_preprocess(self): ...             # 返回预处理函数

    async def synthesize(self, text, *, character, emotion=None) -> bytes: ...
    def synthesize_stream(self, text, *, character, emotion=None) -> AsyncGenerator[bytes, None]: ...
    async def test(self) -> dict: ...
    async def close(self) -> None: ...
```

- `character` — 角色名/音色名。MiniMax 通过 `minimax_voices.yaml` 映射到 `voice_id`
- `emotion` — 情绪标签，可选。GPT-SoVits 忽略；MiniMax 传至 `voice_setting.emotion`

## 音色配置

**GPT-SoVits** (`gpt_sovits_voices.yaml`)：
```yaml
Elysia:
  ref_audio: "/path/to/reference.wav"
  prompt_text: "参考音频对应的文本"
```

**MiniMax** (`minimax_voices.yaml`)：
```yaml
Elysia:
  voice_id: "elysia_test_001"
```

## 文本预处理

LLM 输出的文本可能包含动作/表情标记。不同 TTS 引擎对标记的支持不同，预处理策略由各后端内部实现，`TTSStreamManager` 通过 `get_preprocess()` 获取。

| 函数 | 后端 | 删除 | 保留 |
|------|------|------|------|
| `strip_all_markup` | GPT-SoVits, OpenAI | `[]` `<>` `<<>>` `()` `（）` emoji | — |
| `strip_for_minimax` | MiniMax | `[]` `<<>>` emoji | `()`（语气词）`<>`（停顿控制） |

### 显示层过滤

`core/adapter/tts_adapter.py` 对发送到前端的文本做额外过滤：

| 标签 | 示例 | TTS | 显示 |
|------|------|-----|------|
| 停顿控制 | `<#0.5#>` `<#1#>` | 送 TTS | 隐藏 |
| 英文语气 | `(laughs)` `(sighs)` 等——单源是 `core/text/tags.py::STAGE_EN_WORDS`（24 个条目，含 `clears throat`/`clear throat`、`lip smacking`/`lipsmacking` 两对变体） | 送 TTS | 显示 |
| 中文舞台指示 | `(清了清嗓子)` `(愣了两秒)` | 剥离 | 单独一行 |

## Prompt TTS 感知

`f2_output_format.j2` 根据当前 TTS provider 输出不同的标记指令：
- MiniMax: 教 LLM 使用 `(laughs)` `<#x#>` 等原生标记
- GPT-SoVits / OpenAI: 教 LLM 输出纯文本

`CognitiveLoop` 将 `self._tts.provider_name` 传入 `PromptAssembler`。

## 配置

`config.yaml`：
```yaml
tts:
  enabled: true
  model: minimax/speech-2.8-hd     # 或 gpt-sovits/default
```

环境变量（`.env`）：
```bash
MINIMAX_API_KEY=sk-your-key-here
GPT_SOVITS_URL=http://localhost:9880
CUSTOM_TTS_API_KEY=sk-your-key-here
MINIMAX_TTS_URL=https://api.minimaxi.com     # 可选
```

## 添加新后端

1. 新建 `Module/tts/your_backend.py`，继承 `TTSBackend`
2. 实现所有抽象方法 + `provider_name` + `get_preprocess()`
3. 在 `registry.py` `_BACKENDS` 注册，`TTSFactory.create()` 添加分支
4. 在 `text_preprocess.py` 中添加对应预处理函数（如需要）
5. 在 `Module/config/providers.yaml` 的 `tts:` 段登记 provider（base_url/env_key/models——前端设置页与 `/api/providers` 由此读取）

## 测试

```bash
# 单元测试
python -m pytest Module/tts/tests/ -v

# 冒烟测试（需对应服务端运行）
python -m Module.tts

# 全量
python -m pytest Module/tts/ Module/system_prompt/ Module/output/ -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/output/README.md](../output/README.md) | 输出通道（TTS 输出目标） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + TTS 绑定发送 |
