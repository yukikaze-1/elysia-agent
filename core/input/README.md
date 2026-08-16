# Input Module

> 文档导航：[项目文档](../../README.md) → [Core 文档](../README.md)

输入层模块。负责接收浏览器客户端的音频和文本输入，管理 WebSocket 连接和音频缓冲区。

## 文件结构

```
input/
├── __init__.py              # 导出
├── __main__.py              # 集成测试 (python -m core.input)
├── server.py                # 纯装配（create_app + 管理端点；bind 守卫在 core/utils/server_guard.py）
├── middleware.py            # 认证/payload/限流中间件工厂 + _nosniff_uploads（批次 D）
├── health.py                # health/ready 探针（批次 D）
├── ws_handler.py            # WebSocket 协议层（22 消息类型 _WS_HANDLERS 注册表分发，批次 B）
├── ws_v2.py                 # v2 深业务（_route_v2_text/_handle_voice_commit + 幂等/ACK helper，批次 B）
├── _common.py               # 跨业务共享（_send_ack/_find_channel/_tool_error/_archive_*）
├── errors.py                # 统一错误信封（批次 A）
├── provider_routes.py       # Provider 管理 REST（批次 C）
├── tts_routes.py            # TTS 声音 REST（批次 C）
├── upload_routes.py         # 上传/表情 REST（批次 C）
├── tools_routes.py          # Desktop 工具 REST（批次 C）
├── chat_routes.py           # Chat API + 消息归档 REST（批次 C）
├── session_routes.py        # 多会话 REST（/api/sessions CRUD/activate/messages/resources/generations/pending、/api/messages/{id}/context、/api/search/messages，批次 3）
├── attachment_routes.py     # 附件 HMAC 短签下载 + 签发（GET /api/attachments/{id} + /sign，BE-070）
├── voice_sessions.py        # 三模式语音状态机（普通录音/实时语音/输入法转写，BE-060~062）
├── audio_buffer.py          # AudioBuffer — VAD 分帧 + 音频缓冲 + PCM/WAV 转换 + 唤醒词并行检测
├── wakeword.py              # WakeWordDetector — openWakeWord 流式唤醒词检测
├── sticker_registry.py      # 内置表情包注册表（扫描 static/stickers/*.png，BE-071）
├── tts_profile.py           # TTS profile 共享 helper — 活动会话激活 TTS 后端 + 默认音色解析
├── debug_routes/            # Debug API 路由 (package: 7 modules)
├── tests/                   # 单元测试
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
- 启动守卫：`core/utils/server_guard.py` 提供 `assert_secure_bind` / `warn_if_unsecured`（AgentServer 每轮调用）。

### 开发期远程测试（SERVER_HOST）

默认绑定回环地址（`config.yaml server.host: 127.0.0.1` / dataclass 默认）。远程测试机直连：

- `.env` 写 `SERVER_HOST=0.0.0.0`（env 覆盖 config.yaml——`scripts/start.sh` 即可远程启动，无需改 config.yaml 或传 `--host`）。
- 非回环绑定 + 无 `AGENT_ACCESS_TOKEN` → bind 守卫 fail-closed 拒绝启动，须显式 `ALLOW_INSECURE_REMOTE=1`（开发期匿名远程；生产改用 `AGENT_ACCESS_TOKEN`）。
- 浏览器前端 WS 暂不支持携带 Bearer token（WS 握手不能设自定义头）——token 模式仅 REST 可用；匿名远程直连请用防火墙 / 内网 IP 缓解。

### 版本协商（批次 1，BE-020）

- WS 握手头 `X-Protocol-Version: 2` → v2 路径：建连发 `server_info`（protocol_version/features/services，取代 capabilities）；缺省/1 → v1 路径（发 capabilities）。`features` 严禁超前声明（批次 1 全 false）。**批次 8c（F15）**：switch_model/set_service 后的 capabilities 重播仅达 v1 通道，跳过 v2（v2 用 server_info 取代，切换感知经自身回显）。

### 服务模式开关

- `set_service {service, enabled}` WS 消息 → `agent.set_service()` → `service_mode_result` 广播（所有 client）。TTS off → `_tts_mgr` 停用回落逐字文本流；STT off → 前端停麦 + 后端 BINARY 防御。模式与当前模型经 `session.service_state` 持久化（30d TTL），启动时 `_apply_service_state` 应用。

### REST API

**批次 3 多会话 REST**（`session_routes.py`）：`/api/sessions`（CRUD/activate）、`/api/sessions/{id}/messages`（before/after 分页）、`/api/messages/{id}/context`、`/api/search/messages`（category 白名单 8 值 text/image/link/video/audio/file/tool/sticker——阶段 3 联调补 sticker 贴图历史；`date_from/date_to` 严格 `YYYY-MM-DD` 自然日 fail-fast，非格式 400 `CONTRACT_VALIDATION`，未知时区 400 `INVALID_TIMEZONE`）、`/api/sessions/{id}/resources`（sticker 分类 + 附件软删过滤）、`/api/sessions/{id}/generations`、`/api/generations/{gid}`、`/api/sessions/{id}/pending`。

**批次 7 附件/表情**：`POST /api/uploads`（通用附件上传，filetype 魔数校验 + 签名 URL；阶段 3 起逐文件 `kind`——每文件一组 `kind+files` 邻接配对，兼容旧「单 kind + 多文件」整批提示，悬空/邻接歧义/未知 kind → 400；复验批次起 `kind` 确定后 MIME 规范化单源——audio+video/webm→audio/webm、WAV 探测别名→audio/wav，`core/schema/utils.py::normalize_attachment_mime`，写/读/下载三路径同值，存量记录经读路径兼容）、`GET /api/attachments/{id}`（HMAC 短签下载，免 Bearer，Range/206，Content-Type 同样过规范化）、`GET /api/attachments/{id}/sign`（签发，Bearer）、`GET /api/stickers`（内置表情包）。附件元数据存 `attachments` 集合（BE-070 fail-closed：refs/ref_count + 软删 status/deleted_at，TTL 由 AttachmentGC 管理）。

## 测试

```bash
python -m pytest core/input/ -v
python -m core.input
```

## 已迁出模块（CORE-MOD-P1-01）

| 原路径 | 新路径 | 原因 |
|---|---|---|
| `core/input/idempotency.py` | `core/storage/idempotency.py` | 幂等存储是运行时基础设施，agent 生命周期也消费 |
| `core/input/custom_providers.py` | `core/providers/custom_providers.py` | provider 元数据被 agent 生命周期/热切换消费 |
| `core/input/provider_registry.py` | `core/providers/provider_registry.py` | 与 custom provider 同域 |
| `core/input/capabilities.py` | `core/services/capabilities.py` | 服务能力声明 + 广播是运行时能力，agent 与 input 共用 |
| `core/input/rate_limiter.py` | `core/network/rate_limiter.py` | 限流是传输无关基础设施（CORE-MOD-P2-01） |

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Core 文档](../README.md) | Core 层总览 |
| [Module/stt/README.md](../../Module/stt/README.md) | STT（音频输入消费者） |
| [Module/output/README.md](../../Module/output/README.md) | 输出通道（WebSocket 方向） |
| [core/providers/README.md](../providers/README.md) | Provider 元数据与自定义 provider |
| [core/services/README.md](../services/README.md) | 服务能力声明与广播 |
| [docs/architecture.md](../../docs/architecture.md) | 输入输出系统架构 |
