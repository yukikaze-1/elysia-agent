# Input Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

输入层模块。负责接收浏览器客户端的音频和文本输入，管理 WebSocket 连接和音频缓冲区。

## 文件结构

```
input/
├── __init__.py          # 导出
├── __main__.py          # 集成测试 (python -m Module.input)
├── audio_buffer.py      # AudioBuffer — VAD 分帧 + 音频缓冲 + PCM/WAV 转换
├── server.py            # create_app — WebSocket 服务器 + HTTP 路由
├── wakeword.py          # WakeWordDetector — 唤醒词检测
├── test_audio_buffer.py # 单元测试
├── test_server.py       # WebSocket 服务器测试
├── test_wakeword.py     # 唤醒词测试
└── README.md
```

## 快速开始

```python
from aiohttp import web
from Module.input import create_app, AudioBuffer

async def my_stt(audio_bytes: bytes) -> str:
    return "识别结果"

# 创建 app（需要 agent 实例）
app = create_app(agent, stt_transcribe=my_stt)
web.run_app(app, port=8000)
```

## 接口

### AudioBuffer

- `feed(data: bytes)` — 喂入 PCM 音频数据，触发 VAD + 分帧
- `flush()` — 清空缓冲区，触发最后的转录
- `deafen(seconds: float)` — 暂时关闭麦克风，防止回声
- `set_wake_enabled(enabled: bool)` — 开关唤醒词检测

### create_app

- `create_app(agent, stt_transcribe, *, serve_static, stt_emotion_state, wake_word_enabled)` — 创建 aiohttp Application

## 测试

```bash
python -m pytest Module/input/ -v
python -m Module.input
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/stt/README.md](../stt/README.md) | STT（音频输入消费者） |
| [Module/output/README.md](../output/README.md) | 输出通道（WebSocket 方向） |
| [docs/architecture.md](../../docs/architecture.md) | 输入输出系统架构 |
