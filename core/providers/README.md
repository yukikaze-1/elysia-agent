# Providers — Provider 元数据与自定义 provider 持久化

> 文档导航：[项目文档](../../README.md) → [Core 文档](../README.md)

Provider 配置与解析的共享包，供 `core.agent`（生命周期/热切换）与 `core.input`（REST 管理）共同消费，不依赖传输层。

## 文件结构

```
providers/
├── __init__.py              # 重导出公共 API
├── custom_providers.py      # custom_providers.yaml 读写 + Secret Store 密钥 + resolve_model_params
└── provider_registry.py     # providers.yaml 过滤 + /api/providers 响应组装
```

## 接口

- `load(path=None)` / `save(data, path=None)` — 自定义 provider 配置读写。
- `add_provider(service, entry, ...)` / `remove_provider(service, name, ...)` — 自定义 provider 增删，api_key 只进 Secret Store。
- `resolve_model_params(service, model_id, ...)` — 解析 custom model 的 base_url/api_key/runtime；内置 provider 返回 `{}`。
- `build_providers_response(custom_data, ...)` — 组装 `/api/providers` 响应（已配置/未配置/能力字段）。

## 依赖方向

`core.providers → core.storage.secret_store + Module.config.providers`；禁止 import `core.agent` / `core.input`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/input/README.md](../input/README.md) | Provider 管理 REST 路由 |
| [core/agent/README.md](../agent/README.md) | Agent 生命周期与热切换消费方 |
| [Module/config/README.md](../../Module/config/README.md) | 内置 provider 默认值 |
