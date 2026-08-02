# Input Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

输入层模块。负责接收浏览器客户端的音频和文本输入，管理 WebSocket 连接和音频缓冲区。

## 文件结构

```
input/
├── __init__.py          # 导出
├── __main__.py          # 集成测试 (python -m core.input)
├── audio_buffer.py      # AudioBuffer — VAD 分帧 + 音频缓冲 + PCM/WAV 转换
├── server.py            # 胶水组装 + 基础路由
├── ws_handler.py        # WebSocket 处理（set_service 路由 + BINARY 防御）
├── capabilities.py      # build_capabilities — 嵌套 per-service 能力声明 + 全量重播
├── api_routes.py        # 业务 API 路由
├── debug_routes/         # Debug API 路由 (package: 7 modules)
├── custom_providers.py  # 自定义 provider 持久化（JSON 文件读写）
├── provider_registry.py # /api/providers 响应组装（已配置/未配置区分）
├── wakeword.py          # WakeWordDetector — 唤醒词检测
├── tests/               # 单元测试
└── README.md
```

## 快速开始

```python
from aiohttp import web
from core.input import create_app, AudioBuffer

# create_app 不再参数注入 STT——ws_handler 运行时读 agent.stt 槽位（支持热切换）
app = create_app(agent)
web.run_app(app, port=8000)
```

## 接口

### AudioBuffer

- `feed(data: bytes)` — 喂入 PCM 音频数据，触发 VAD + 分帧
- `flush()` — 清空缓冲区，触发最后的转录
- `deafen(seconds: float)` — 暂时关闭麦克风，防止回声
- `set_wake_enabled(enabled: bool)` — 开关唤醒词检测

### create_app

- `create_app(agent, *, serve_static, wake_word_enabled, task_system, auth_token)` — 创建 aiohttp Application。STT 不再参数注入：建连时 `ws_handler` 读 `agent.stt` 槽位（`agent.stt_transcribe` / `agent.stt_emotion_state`），支持运行时热切换；BINARY 帧经 `agent.stt_enabled` 防御门控。

### 服务模式开关

- `set_service {service, enabled}` WS 消息 → `agent.set_service()` → `service_mode_result` 广播（所有 client）。TTS off → `_tts_mgr` 停用回落逐字文本流；STT off → 前端停麦 + 后端 BINARY 防御。模式与当前模型经 `session.service_state` 持久化（30d TTL），启动时 `_apply_service_state` 应用。

## 测试

```bash
python -m pytest core/input/ -v
python -m core.input
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/stt/README.md](../../Module/stt/README.md) | STT（音频输入消费者） |
| [Module/output/README.md](../../Module/output/README.md) | 输出通道（WebSocket 方向） |
| [docs/architecture.md](../../docs/architecture.md) | 输入输出系统架构 |
