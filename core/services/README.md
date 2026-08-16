# Services — 服务运行时能力声明与广播

> 文档导航：[项目文档](../../README.md) → [Core 文档](../README.md)

服务运行时能力（LLM/STT/TTS）的契约模型构建与 v1 通道重播。原 `core/input/capabilities.py` 迁入（CORE-MOD-P1-01）。

## 文件结构

```
services/
├── __init__.py              # 重导出公共 API
└── capabilities.py          # build_capabilities / build_server_info / broadcast_capabilities
```

## 接口

- `build_capabilities(agent)` — v1 `CapabilitiesMessage`（嵌套 per-service 能力）。
- `build_server_info(agent, connection_id=None)` — v2 `ServerInfoMessage`（features/services + durable）。
- `broadcast_capabilities(agent)` — switch/set_service 后向 v1 WebSocketChannel 重播；v2 通道跳过。

## 依赖方向

`core.services → frontend_contract + Module.output + core.version + core.storage.idempotency`；只经 `TYPE_CHECKING` 引用 `core.agent`，运行时无反向依赖。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/input/README.md](../input/README.md) | WS 建连消费 capabilities/server_info |
| [frontend_contract/README.md](../../frontend_contract/README.md) | 契约模型单源 |
| [docs/electron-backend-protocol-v2.md](../../docs/electron-backend-protocol-v2.md) | v2 server_info 语义 |
