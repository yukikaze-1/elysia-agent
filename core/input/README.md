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

- `create_app(agent, *, serve_static, wake_word_enabled, task_system, auth_token, access_token, max_ws_connections, max_request_bytes)` — 创建 aiohttp Application。STT 不再参数注入：建连时 `ws_handler` 读 `agent.stt` 槽位（`agent.stt_transcribe` / `agent.stt_emotion_state`），支持运行时热切换；BINARY 帧经 `agent.stt_enabled` 防御门控。

### 产品认证（批次 1，BE-010）

- `access_token`（`AGENT_ACCESS_TOKEN`，显式参数默认 `""`）非空时，`/ws`、业务 `/api/*`、`/uploads/*` 要求 `Authorization: Bearer <access_token>`；匿名 → 401 `AUTH_REQUIRED`/`AUTH_INVALID` 统一错误信封。无 token 环境全匿名（v1 兼容）。与 `auth_token`（`SERVER_AUTH_TOKEN`，admin/debug）权限域分离。
- `max_ws_connections`：并发 WS 上限，超出拒绝 503 `CONNECTION_LIMITED`；`max_request_bytes`：业务请求体上限（app 级 `client_max_size`），超出 413 `REQUEST_TOO_LARGE`。
- 启动守卫：`warn_if_unsecured(host, access_token)` 非回环绑定 + 无产品认证 → ERROR 显著阻塞告警（`run_server` 每轮调用）。

### 版本协商（批次 1，BE-020）

- WS 握手头 `X-Protocol-Version: 2` → v2 路径：建连发 `server_info`（protocol_version/features/services，取代 capabilities）；缺省/1 → v1 路径（发 capabilities）。`features` 严禁超前声明（批次 1 全 false）。**批次 8c（F15）**：switch_model/set_service 后的 capabilities 重播仅达 v1 通道，跳过 v2（v2 用 server_info 取代，切换感知经自身回显）。

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
