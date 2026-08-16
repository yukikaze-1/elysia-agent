# core/network/ — 网络运行时基础设施

> 文档导航：[项目文档](../../README.md) → [Core 文档](../README.md)

传输无关的网络组件（当前为公开入口限流）。CORE-MOD-P2-01 自 `core/input` 迁入，避免传输包承载 agent/middleware 共用的基础设施。

## 文件结构

```
network/
├── __init__.py              # 重导出 RateLimiter / rate_key_for_request
└── rate_limiter.py          # 单进程 token-bucket + 认证主体/IP 分桶
```

## 接口

- `RateLimiter(rules, ...)` — 按 (category, key) 的令牌桶集合；`check()` / `retry_after_ms()` / `reset()`。
- `rate_key_for_request(request, valid_tokens)` — `auth:<sha256>` 优先，无效 token 回退 `ip:<remote>`。

## 依赖方向

`core.network → aiohttp`；不 import `core.agent` / `core.input`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/input/README.md](../input/README.md) | 限流消费方（middleware + WS handler） |
| [docs/architecture.md](../../docs/architecture.md) | 输入/认证架构 |
