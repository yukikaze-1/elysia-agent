# TTS Module

文本转语音模块。提供统一的 `TTSBackend` 接口，通过不同的后端实现对接不同的 TTS 服务端（GPT-SoVITS、IndexTTS2 等）。

## 文件结构

```
tts/
├── __init__.py               # 导出 TTSBackend, GPTSoVitsBackend, 预处理函数
├── protocol.py               # TTSBackend ABC
├── registry.py               # 后端注册表 + TTSFactory.create() / from_config()
├── gpt_sovits.py             # GPTSoVitsBackend — HTTP 调用 GPT-SoVITS 服务端
├── gpt_sovits_voices.yaml    # GPT-SoVITS 角色音色配置
├── text_preprocess.py        # 文本预处理（去除 LLM 输出的动作/表情标记）
├── config.py                 # TTSConfig — dataclass 配置
├── __main__.py               # 冒烟测试（需连接 GPT-SoVITS 服务端）
├── test_tts.py               # 单元测试
├── test_config.py            # Config 测试
└── tmp/                      # 测试生成的音频（手动检查用）
```

## 快速开始

```python
from Module.tts import TTSFactory

tts = TTSFactory.create("gpt-sovits/default", base_url="http://192.168.1.18:9880")

# 非流式 — 返回完整 WAV bytes
wav = await tts.synthesize("你好呀。", character="Elysia")

# 流式 — 边生成边产出 WAV chunks
async for chunk in tts.synthesize_stream("你好呀。", character="Elysia"):
    websocket.send_bytes(chunk)

await tts.close()
```

## 接口

```python
class TTSBackend(ABC):
    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes: ...

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncGenerator[bytes, None]: ...
```

- `character` — 角色名，必传，对应 voices.yaml 中的 key
- `emotion` — 情绪名，可选。GPT-SoVITS 不支持情绪控制，传了会被忽略；后续的后端（如 IndexTTS2）可以利用

## 音频格式

`media_type="wav"`，服务端返回：
- 首 chunk: WAV header (44 bytes, 32000Hz / mono / int16)
- 后续 chunk: raw PCM int16

播放时长计算：`len(bytes) / 64000`（64000 = 32000Hz × 1 声道 × 2 字节/采样）

## 音色配置

`gpt_sovits_voices.yaml`：

```yaml
Elysia:
  ref_audio: "/path/to/reference.wav"   # 参考音频路径（服务端文件系统路径）
  prompt_text: "参考音频对应的文本"        # 影响音色克隆效果
```

新增角色在 YAML 中添加即可，调用时传 `character="新角色名"`。

## 文本预处理

LLM 输出的文本可能包含动作/表情标记。不同 TTS 引擎对标记的支持不同，预处理策略由各后端内部实现：

| 函数 | 适用后端 | 行为 |
|---|---|---|
| `strip_all_markup` | GPTSoVits | 移除 `[]` `<>` `<<>>` `()` `（）` |
| `strip_action_only` | IndexTTS2 | 移除 `[]` `()` `（）`，保留 `<表情>` `<<心情>>` |

## 支持的后端

| Provider | 后端类 | 默认 URL | API Key |
|----------|--------|---------|---------|
| `gpt-sovits` | `GPTSoVitsBackend` | `http://localhost:9880` | 不需要 |

## 添加新后端

1. 在 tts/ 下新建 `your_backend.py`
2. 继承 `TTSBackend`，实现 `synthesize` 和 `synthesize_stream`
3. 在 `__init__.py` 中导出

```python
from .protocol import TTSBackend

class YourBackend(TTSBackend):
    def __init__(self, ...):
        self._preprocess = strip_action_only  # 选合适的预处理策略

    async def synthesize(self, text, *, character, emotion=None) -> bytes:
        ...

    async def synthesize_stream(self, text, *, character, emotion=None):
        ...
```

## 测试

```bash
# 单元测试
python -m pytest Module/tts/test_tts.py -v

# 冒烟测试（需确保 GPT-SoVITS 服务端已启动）
python -m Module.tts

# 统一延迟基准测试（含 LLM/TTS/STT 流式和非流式）
python -m Module.bench
```

`__main__.py` 会依次验证流式合成、非流式合成、提前中断等场景，生成的音频写入 `tmp/` 目录以便手动检查音质。

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
tts = TTSFactory.create("gpt-sovits/default", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger，调用方可通过 `Module/logger/LoggerFactory` 注入。

## Config

`TTSConfig` dataclass 支持通过 YAML + 环境变量加载配置：

```python
from Module.config import ConfigLoader
from Module.tts import TTSFactory
from Module.tts.config import TTSConfig

cfg = ConfigLoader(TTSConfig, yaml_path="config.yaml", yaml_key="TTS").load()
tts = TTSFactory.from_config("gpt-sovits/default", cfg)
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `http://localhost:9880` | `GPT_SOVITS_URL` | TTS 服务端点 |
| `voices_config` | `""` | — | 音色配置 YAML 路径，为空则使用内置文件 |
