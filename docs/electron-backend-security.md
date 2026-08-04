# Electron 后端安全部署指南

**文档版本：** v0.1（批次 1 产出，BE-010）
**关联：** `docs/electron-backend-baseline.md`、`docs/superpowers/specs/2026-08-04-electron-backend-batch1-auth-version-error.md`

本文件说明 Elysia Agent 后端的**远程安全部署**方式：产品认证、TLS 终止、防护范围与已知缺口。远程部署必须遵循本指南；本地开发（回环 + 无 token）维持匿名。

---

## 1. 认证模型

### 1.1 两个权限域分离

| Token | 配置 | 保护范围 | 权限域 |
|---|---|---|---|
| `AGENT_ACCESS_TOKEN` | `ServerConfig.access_token`（env） | `/ws`、业务 `/api/*`、`/uploads/*` | **产品用户** |
| `SERVER_AUTH_TOKEN` | `ServerConfig.auth_token`（env） | `/shutdown` `/restart` `/tools` `/tools/reload` `/debug/*` | **管理/运维** |

两者独立配置、权限域分离——产品 token 不能访问管理端点，管理 token 不能用于产品 API。`AGENT_ACCESS_TOKEN` 标 `secret=True`（不落盘 YAML）。

### 1.2 认证语义

- `access_token` 为空 → **全匿名**（本地开发，v1 前端兼容）；
- 非空 → 以下全部要求 `Authorization: Bearer <access_token>`：
  - `/ws` 握手（拒绝升级返回 401，非 101）；
  - 全部业务 `/api/*`（`/api/chat` `/api/messages` `/api/providers` `/api/images`）；
  - `/uploads/*` GET 资源读取。
- 未带凭据 → 401 `AUTH_REQUIRED`；凭据错误 → 401 `AUTH_INVALID`；均返回统一错误信封。

### 1.3 凭据载体

- 凭据放 `Authorization` Header（REST）或 WS 握手 Header（`Authorization: Bearer`）——**绝不放 URL 查询参数**；
- Electron Main 持有凭据，Renderer 不接触明文 token；
- 日志、错误体、诊断包**不记录 token**（`secret=True` + 日志敏感扫描测试守护）。

### 1.4 启动守卫

服务绑定**非回环地址**（非 `127.0.0.1/localhost/::1`）且未配置 `AGENT_ACCESS_TOKEN` 时，启动输出 **ERROR 级显著阻塞告警**（指引如何配置）。这是 PRD「拒绝启动或输出显著阻塞告警」的"或"分支——不拒绝启动，避免破坏本地开发（config 默认 host=`0.0.0.0`）。

## 2. TLS / HTTPS-WSS 部署

后端**不内建 TLS**——生产由反向代理终止 TLS。推荐部署形态：

```
Electron Main ──(wss/https)──► 反向代理（TLS 终止 + 转发 Authorization）──► 后端 :8765
```

### 2.1 反向代理要求

- **TLS 终止**：Nginx/Caddy/Traefik 终止 TLS，转发到后端 `http://127.0.0.1:8765`（后端只在回环/内网监听）；
- **转发凭据**：代理把客户端 `Authorization` Header 原样转发给后端（认证决策在后端，不信任代理）；
- **跨 Origin 重定向不得转发凭据**：代理对重定向（3xx）剥离 `Authorization`，防凭据泄漏到其他 Origin；
- **WS 升级**：代理需支持 WebSocket Upgrade（`proxy_set_header Upgrade/Connection`）；`/ws` 路径同样要求客户端带 token；
- **示例（Nginx）**：

```nginx
server {
    listen 443 ssl;
    server_name agent.example.com;
    ssl_certificate     /etc/letsencrypt/live/agent.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agent.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 转发客户端 Authorization（认证在后端）
        proxy_set_header Authorization $http_authorization;
        # WebSocket 升级
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_http_version 1.1;
        # 跨域重定向剥离凭据（浏览器自动处理：Authorization 不跨 Origin 发送）
    }
}
```

### 2.2 后端监听建议

- 远程部署时后端 host 建议绑定 `127.0.0.1`（仅代理可达）或内网地址 + `AGENT_ACCESS_TOKEN`；
- 若绑定 `0.0.0.0` 且无 token，启动会告警（见 §1.4）——远程部署必须配置 token。

### 2.3 可重复测试

- `core/input/tests/test_auth.py`：有 token 环境匿名 `/api/*` `/ws` `/uploads/*` 被拒、有效身份可用（即远程部署安全姿势）；
- `core/input/tests/test_limits.py`：请求体超限、并发超限拒绝；
- 反向代理的 TLS 终止本身由代理层保证（后端无 TLS 职责），本仓库测试覆盖后端认证决策——部署后建议用 `curl -k https://…/health` 与 `wss` 连接各做一次冒烟。

## 3. 限制保护

- `max_request_bytes`（默认 8MB）：业务 JSON body 上限，超限 413 `REQUEST_TOO_LARGE`；
- `max_ws_connections`（默认 64）：并发 WS 上限，超限 503 `CONNECTION_LIMITED`；
- 图片上传大小/类型沿用 `image_max_mb` / `image_allowed_types`（415 `INVALID_REQUEST` 映射）。

### 3.1 受保护会话与数据完整性（批次 3 多会话）

- **`default` 会话禁删**：`DELETE /api/sessions/default` → 409 `SESSION_PROTECTED`（`default` 恒存在且受保护，禁删含其 append-only 消息归档与 character_state）；
- **忙时门禁**：生成/TTS 进行中 `activate`（WS/REST）或删除**活动会话** → 409 `AGENT_BUSY`（retryable），防 engine 换绑与异步延续竞态；
- 会话 ID 未登记即被 v2 显式 `conversationId` 路由会 lazy-create——不碰 `default`，仅可能产生幽灵会话文档（Demo 可接受，后续批次可加白名单校验）。

## 4. 已知缺口（后置批次）

| 缺口 | 后置 |
|---|---|
| `/uploads/*` 匿名 GET 已拒（有 token），但**短期签名 URL / 鉴权下载**未实现——当前 v2 客户端需带 `Authorization` 访问资源 | 批次 7（BE-070/071） |
| 速率限制实现（`RATE_LIMITED` 仅入目录） | 批次 2+ |
| 自定义模型密钥 Secret Store（当前 `api_key_env` 仅运维预配置） | 批次 5（BE-050） |
| 日志无 token 已有测试守护；但 Provider 测试密钥不得经匿名明文连接（已由产品认证覆盖） | 批次 5 强化 |

## 5. 敏感信息纪律

- token/API key 一律环境变量（`AGENT_ACCESS_TOKEN`/`SERVER_AUTH_TOKEN`/`*_API_KEY`），不落 YAML/git；
- 错误信封 `details` 默认空 dict——禁止放入密钥、聊天正文、Inner Voice、未脱敏路径；
- 日志使用关联 `requestId`，不记录正文与凭据。
