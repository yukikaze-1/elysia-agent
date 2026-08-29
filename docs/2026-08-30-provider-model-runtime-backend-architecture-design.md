# Cyrene Provider、模型发现、协议适配与运行状态后端架构设计

> 文档导航：[项目文档](../README.md)
>
> **文档状态：** 架构讨论稿（目标设计，未实现）  
> **日期：** 2026-08-30  
> **事实基线：** `192.168.1.18:~/elysia-agent@1bdd97e3e98e6c7fce4ff5e8c4811a16453e7ee9`  
> **权威范围：** 后端 Provider 连接、模型 Profile、在线模型发现、LLM API 协议适配、凭据、运行状态与兼容迁移  
> **验证口径：** 本文基于代码静态核对，不代表下述目标能力已经实现或完成真机验收  
> **明确排除：** 任务中心继续暂缓；本文不改变任务系统设计，也不实施前端 UI

---

## 0. 结论

当前后端已经具备 Provider 列表、自定义端点保存、Secret Store、SSRF 防护、连通/可用测试和运行时模型切换，但现有模型把“Provider 连接”和“具体模型”压在了同一个 `custom/{name}` 标识中。这个结构只适合“一个连接对应一个手工命名模型”的早期用法，无法可靠承载以下产品需求：

- 从 Provider 的 API 在线获取模型列表；
- 一个 Provider 连接下选择和管理多个模型；
- 明确选择 Chat Completions 或 Responses 等调用协议；
- 确保用户选择的模型 ID 就是实际发给上游的模型 ID；
- 在前端安全展示 Embedding、MongoDB、Milvus 和幂等存储的运行状态。

本设计采用六项核心决策：

1. **拆分 `ProviderConnection` 与 `ModelProfile`。** 前者拥有端点、协议和凭据，后者拥有具体上游 `modelId` 与模型能力；前端不得再解析复合字符串推导二者。
2. **协议通过 Adapter Registry 选择。** 第一批实现 `openai_chat_completions` 和 `openai_responses`；不得在路由、工厂和业务层散落 `if protocol == ...`。
3. **模型发现只由后端发起。** Electron Renderer 不直连 Provider、不持有已保存的明文密钥；发现失败不修改持久化配置。
4. **保留现有 `/api/providers` 和 `switch_model` 作为迁移兼容层。** 新契约上线并稳定前，不直接改变旧 wire 语义。
5. **新增经过认证的 `/api/runtime/status`。** 它面向设置页和诊断页，不改变 `/health` 的 liveness 与 `/ready` 的 readiness 语义。
6. **Embedding 与存储第一阶段只读展示。** 不把数据库连接串、密钥或内部异常暴露给前端，也不在这一批实现前端修改数据库配置。

推荐实施顺序为：领域模型与兼容迁移 → 模型发现 → Responses Adapter → 运行状态 → 收口旧接口。不能先做前端在线下拉框，再让后端继续把 Provider 名当模型名。

---

## 1. 背景、目标与范围

### 1.1 产品侧问题

当前设置页允许用户手工填写自定义模型，但存在三个结构性问题：

- 用户需要逐个输入模型，无法从兼容端点获取 Provider 已开放的模型；
- 表单没有正式的协议选择，后端默认把所有 LLM 当作 OpenAI Chat Completions；
- Embedding 和数据库依赖对前端不可见，用户无法区分“聊天可用”“语义记忆降级”和“数据库不可用”。

### 1.2 本设计目标

- 建立稳定、可迁移、可扩展的 Provider 与模型领域模型；
- 支持保存前和保存后的在线模型发现；
- 支持 Chat Completions 与 Responses 两种真实调用路径；
- 保持内置 LLM/STT/TTS 与现有自定义配置可继续使用；
- 通过生成契约向前端提供类型化 API；
- 提供安全、有限、可轮询的运行状态快照；
- 明确凭据所有权、SSRF、日志脱敏、限流与错误码；
- 给出分批实现、迁移、测试和验收清单。

### 1.3 本设计非目标

- 本批不恢复或改造任务中心；
- 不让 Renderer 直接访问第三方 Provider；
- 不在第一批覆盖 Anthropic、Gemini、Ollama Native 等全部厂商原生协议；
- 不仅凭 `/models` 返回值自动断定模型支持 Chat Completions、Responses、工具或多模态；
- 不在第一批允许前端切换 Embedding 模型；
- 不允许前端编辑 MongoDB、Milvus、幂等存储连接信息；
- 不向前端暴露数据库 URI、密钥、文件路径、堆栈或原始异常；
- 不为了支持内网自定义端点而默认放宽现有 SSRF 策略。

---

## 2. 当前实现事实基线

本节只描述基线代码已经存在的行为。

### 2.1 Provider REST API

`core/input/provider_routes.py` 当前注册四个端点：

| 方法 | 路径 | 当前作用 |
|------|------|----------|
| GET | `/api/providers` | 返回已配置内置 Provider、自定义条目和未配置引导 |
| POST | `/api/providers` | 保存或按同名覆盖一个自定义条目 |
| DELETE | `/api/providers/{service}/{name}` | 删除自定义条目及其 Secret Store 密钥 |
| POST | `/api/providers/test` | 执行 connectivity 或 usable 测试 |

当前没有模型发现端点，也没有独立的 ProviderConnection、ModelProfile CRUD。

`frontend_contract/rest.py` 中的 `ProviderEntry` 和 `CustomProvider` 暴露 `models`、`runtime`、`credentialConfigured`、`capabilities`，但 `models` 仍只是条目中的列表，没有独立 Profile 身份、启停状态、来源、协议或测试状态。

### 2.2 Provider 与模型被同一个标识混用

当前自定义条目按 `service + name` 存储，活动模型使用 `custom/{name}`：

1. `resolve_model_params(service, "custom/{name}")` 只解析 `base_url`、`api_key` 和 `runtime`；
2. `LLMFactory.create("custom/{name}")` 将字符串拆成 `provider="custom"`、`model="{name}"`；
3. `OpenAICompatBackend` 随后把 `{name}` 作为上游请求的模型名。

因此，自定义条目的 `models: [...]` 即使包含多个真实模型，也没有被模型切换链路可靠选中。若只在前端增加一个在线模型下拉框，用户选择的模型仍可能没有真正传给上游。

这是本次改造必须先解决的核心问题。

### 2.3 LLM 协议现状

- `Module/llm/registry.py` 当前把已实现的 LLM Provider 统一创建为 `OpenAICompatBackend`；
- `Module/llm/openai_compat.py` 的健康测试、非流式、流式和工具调用均使用 `client.chat.completions.create(...)`；
- 当前没有 Responses API Backend；
- `gemini` 在注册表中仍明确为未实现；
- `LLMBackend` 已抽象 `chat`、`chat_stream`、`stream_think`、`test` 和 `close`，可作为协议 Adapter 的归一化输出边界。

### 2.4 配置与凭据现状

- 内置 Provider 静态信息来自 `Module/config/providers.yaml`；
- 自定义 Provider 非密钥字段写入 `custom_providers.yaml`；
- Backend Runtime 的 `api_key` 写入 Secret Store，不进入 YAML；
- 更新时未提交新 Key 会保留旧 Key；删除条目会删除对应 Secret；
- Desktop/Local Worker Runtime 不由后端保存凭据；
- 自定义 YAML 当前没有 `schemaVersion`，写入不是显式的临时文件原子替换，也没有多写者锁。

### 2.5 SSRF 与测试现状

- Backend Runtime 自定义端点在保存、测试、切换/工厂创建路径复用 SSRF 校验；
- 测试请求会使用受保护的 HTTP Client；
- 内置本地服务通过受控配置接入，不应等同于任意用户输入 URL；
- `/api/providers/test` 已进入认证和限流体系；
- 部分测试失败路径仍直接返回 `str(e)`，新接口需要统一改成安全错误码和脱敏文案。

### 2.6 Embedding 与存储状态现状

`core/memory/embedding_provider.py` 当前懒加载顺序为：

1. 存在 `OPENAI_API_KEY` 时尝试 `openai/text-embedding-3-small`；
2. 失败或未配置时尝试 `huggingface/BAAI/bge-large-zh-v1.5`；
3. 均不可用时返回空向量，系统按纯文本能力降级。

当前对象只保存 `_backend`、`_tried`、`_task` 和超时等内部状态，没有公开的类型化快照，路由也无法安全知道最终选中的 Provider、模型、向量维度或降级原因。

当前探针语义为：

- `/health`：liveness，零外部网络探测，进程存活即 200；
- `/ready`：readiness，返回 `chatReady`、`fullReady` 和 `mongo/milvus/idempotency` 依赖状态；
- `server_info.services`：只覆盖 LLM/STT/TTS 的建连快照。

前端需要的“Embedding 当前使用什么”“数据库是否正常”不适合继续塞进 `/health`，也不应依赖解析 `/ready` 的内部探针形状。

### 2.7 当前问题归纳

| 编号 | 问题 | 直接后果 |
|------|------|----------|
| BE-MODEL-P0-01 | Provider 身份与模型身份混用 | 多模型选择无法保证真实生效 |
| BE-MODEL-P0-02 | 所有 LLM 固定走 Chat Completions | 无法真实支持 Responses API |
| BE-MODEL-P1-01 | 没有受保护的模型发现 API | 前端只能手输，或被迫不安全直连 |
| BE-MODEL-P1-02 | 自定义配置无 schemaVersion/原子迁移 | 后续结构升级存在损坏和回滚风险 |
| BE-MODEL-P1-03 | Embedding 无公开状态接口 | 前端无法区分语义可用与纯文本降级 |
| BE-MODEL-P1-04 | 业务诊断状态与探针边界不清 | 容易错误扩张 `/health`、`/ready` 契约 |
| BE-MODEL-P1-05 | 新错误尚无完整稳定码 | 前端只能展示不稳定的异常字符串 |

---

## 3. 目标架构

```text
Electron Renderer
  │  只调用 Cyrene Backend typed API
  ▼
Provider / Model REST Routes       Runtime Status Route
  │                                │
  ▼                                ▼
ModelProviderService          RuntimeStatusService
  │                                │
  ├── ProviderRepository            ├── Agent service snapshot
  ├── ModelProfileRepository        ├── EmbeddingProvider.snapshot()
  ├── CredentialStore              └── cached storage probes
  ├── ModelDiscoveryService
  │       │
  │       ▼
  │   ProtocolAdapterRegistry
  │       ├── OpenAI Chat Completions Adapter
  │       └── OpenAI Responses Adapter
  │
  ▼
Service switch / LLMFactory
  │
  ▼
具体 LLMBackend（协议差异在 Adapter 内归一化）
```

职责边界：

- **Route**：认证后的 HTTP 解析、契约校验、状态码映射；不直接操作 YAML 或 SDK。
- **ModelProviderService**：用例编排、唯一性、运行时约束、删除保护、迁移兼容。
- **Repository**：版本化非密钥配置、原子写入、锁和迁移。
- **CredentialStore**：仅管理 Backend Runtime 密钥，永不返回明文。
- **ModelDiscoveryService**：超时、缓存、结果归一化和错误分类。
- **ProtocolAdapterRegistry**：协议能力的唯一注册点。
- **LLMBackend**：把不同 SDK 响应归一成当前 Cognitive Loop 能消费的文本、工具调用和 usage。
- **RuntimeStatusService**：只读状态聚合，不改变服务状态，也不承担 readiness 门禁。

---

## 4. 领域模型

### 4.1 ProviderConnection

ProviderConnection 表示“一组可复用的连接与凭据”，不是具体模型。

| 字段 | 类型 | 说明 |
|------|------|------|
| `providerId` | string | 稳定、不透明 ID；前端不得解析 |
| `service` | enum | 第一批为 `llm`；保留 `stt/tts` |
| `displayName` | string | 用户可编辑显示名 |
| `kind` | enum | `builtin` 或 `custom` |
| `runtime` | enum | `backend/desktop/local_worker` |
| `protocol` | enum | 当前连接实际使用的 API 协议 |
| `baseUrl` | string | 规范化 API 根地址；不是具体 action URL |
| `credentialConfigured` | bool | 只表示密钥是否存在，不返回密钥 |
| `mutable` | bool | 内置配置通常不可删除，自定义可编辑 |
| `enabled` | bool | 是否允许创建/使用其 Profile |
| `discoverySupported` | bool | Adapter 与 Runtime 是否支持后端发现 |
| `createdAt/updatedAt` | timestamp | 配置审计时间 |

ID 约定：

- 内置连接可使用确定性保留 ID，例如 `builtin_llm_deepseek`；
- 自定义连接使用 `prv_<uuid>`；
- ID 只用于引用，不携带 Provider 名、模型名或 URL 语义；
- 重命名 `displayName` 不改变 `providerId`。

### 4.2 ModelProfile

ModelProfile 表示“某个连接下可实际调用的具体模型”。

| 字段 | 类型 | 说明 |
|------|------|------|
| `profileId` | string | 稳定、不透明 ID，例如 `mdl_<uuid>` |
| `providerId` | string | 指向 ProviderConnection |
| `service` | enum | 必须与连接一致 |
| `modelId` | string | 原样发送给上游 API 的模型 ID |
| `displayName` | string | UI 名称，默认等于 `modelId` |
| `source` | enum | `static/api/manual/legacy` |
| `enabled` | bool | 是否可被选择 |
| `capabilities` | object | 已确认的能力；未知不得冒充 false/true |
| `protocolOverride` | null | 第一版恒为空，暂不允许逐模型覆盖协议 |
| `createdAt/updatedAt` | timestamp | 配置审计时间 |

第一版将协议放在 ProviderConnection 上，而不是每个 Profile 上。理由是同一端点和凭据通常对应一个 API 形状，可减少错误组合。若未来同一端点确实需要混用协议，再通过显式 `protocolOverride` 扩展，不能由模型名猜测。

`capabilities` 只保存后端真正确认或用户显式声明的能力，例如：

```json
{
  "multimodal": "unknown",
  "tools": "unknown",
  "streaming": "supported",
  "reasoning": "unknown"
}
```

不要把 `/models` 未返回某字段解释为“不支持”。

### 4.3 连接、模型与活动状态的关系

```text
ProviderConnection 1 ─── N ModelProfile
                                │
                                ├── 会话 A 当前激活
                                ├── 会话 B 当前激活
                                └── 应用默认 Profile
```

- 活动状态仍由现有会话 `service_state` 和应用默认配置拥有；
- Provider/Profile 配置本身不保存“全局唯一 current”；
- REST 列表可以返回由服务层计算的 `activeInCurrentSession`，但它不是配置字段；
- 删除连接前必须检查其全部 Profile 是否被任一受保护配置或活动会话引用；
- 删除 Profile 不得连带删除同连接下其他 Profile 或凭据。

### 4.4 Runtime 边界

- `runtime=backend`：后端保存凭据、执行发现、测试和真实调用；
- `runtime=desktop/local_worker`：后端只保存非敏感元数据，不接收凭据，也不代表后端可发现模型；
- 第一批模型发现仅支持 Backend Runtime；其他 Runtime 返回稳定的 `PROVIDER_RUNTIME_UNSUPPORTED`；
- 未来若 Desktop Runtime 需要发现模型，应通过 Desktop Bridge 的专用能力和权限流程实现，不能让后端假装已执行。

---

## 5. 协议 Adapter 设计

### 5.1 协议枚举

第一批正式接受：

| 枚举 | 含义 | 实现要求 |
|------|------|----------|
| `openai_chat_completions` | OpenAI 兼容 Chat Completions | 复用并收口当前 `OpenAICompatBackend` |
| `openai_responses` | OpenAI Responses | 新增独立 Backend/Adapter |

未来可以增加 `anthropic_messages`、`gemini_generate_content`、`ollama_native`，但在 Adapter、测试和能力声明全部完成前不得出现在后端可选协议列表中。

迁移规则：所有既有内置及自定义 LLM 默认映射到 `openai_chat_completions`，从而保持当前行为。

### 5.2 Adapter Registry

接口示意（非最终可执行代码）：

```text
ProtocolAdapter
  protocol
  supportsDiscovery(runtime) -> bool
  discoverModels(connection, credential, page) -> DiscoveryResult
  createBackend(connection, profile, credential, options) -> LLMBackend
  testConnection(connection, credential) -> ProviderTestResult
  normalizeError(exception) -> SafeProviderError
```

注册表是以下判断的单一来源：

- 协议是否已经实现；
- 是否支持模型发现；
- 如何创建 LLM Backend；
- 如何执行连接测试；
- 如何把 SDK/HTTP 异常转换为公开安全错误。

路由、Provider Service、LLMFactory 不应各自维护协议分支。

### 5.3 Chat Completions Adapter

改造目标：

- 保持现有文本流、多模态输入、prefix、工具调用和 usage 行为；
- 从 `ModelProfile.modelId` 获取上游模型名，不再从 `custom/{name}` 推导；
- 把当前 `OpenAICompatBackend` 纳入 Adapter Registry；
- 连接测试与可用测试都使用同一连接解析和 SSRF Client；
- 保持 built-in Provider 的 beta URL/prefix 等现有特殊配置；
- 禁止因引入协议枚举而改变现有 DeepSeek、OpenAI、Ollama、Mimo 的默认路径。

### 5.4 Responses Adapter

Responses 不是把 URL 从 `/chat/completions` 改成 `/responses` 就完成。新 Adapter 至少要处理：

- 当前 OpenAI 格式 messages 到 Responses input 的转换；
- system/developer/user/assistant 角色与多段内容映射；
- 流式文本事件归一到 `chat_stream`；
- reasoning 与普通输出的安全分离；
- function/tool schema 转换；
- 工具调用 ID、名称和增量参数的组装；
- usage 的 input/output/total/cached/reasoning token 归一化；
- 取消、超时、连接关闭和客户端复用；
- Provider 返回的内部 response ID 是否保存的明确策略。

第一版建议保持无状态调用：除非后续设计明确需要续接，不把 Provider response ID 暴露给前端，也不把它当作 Cyrene 对话 ID。

Responses Adapter 必须实现现有 Cognitive Loop 实际使用的 `LLMBackend` 行为，不能只让“测试连接”通过而聊天、流式工具调用不可用。

### 5.5 模型发现 Adapter

对于 OpenAI 兼容协议，发现逻辑通常读取 API 根地址下的模型集合并归一化 `data[].id`。实现要求：

- URL 路由由 Adapter 和安全 URL 库组合，不做字符串拼接；
- `baseUrl` 表示 API 根，例如 `https://example.com/v1`，不是 `/chat/completions` action URL；
- 去重、稳定排序、限制最大返回数量；
- 只采信 Provider 实际返回的字段；
- `/models` 不可用时返回“发现不支持/失败”，并保留手工输入能力；
- 发现模型不等于模型可用，保存或启用前仍可执行 Profile usable test；
- 不根据模型名称自动切换协议；
- 不把一次发现结果直接写入配置，必须由用户选择并提交 Profile。

---

## 6. REST 与 WebSocket 契约

### 6.1 兼容策略

新语义通过新端点并行上线：

- 旧 `/api/providers` 四端点保留，标记为兼容层；
- 旧 `switch_model {service, model}` 保留；
- 新前端完成迁移且至少经过一个稳定发布周期后，再讨论废弃；
- 兼容期由服务层把旧 `custom/{name}` 映射到 legacy Profile；
- 不允许新端点继续复用“name 同时是模型名”的规则。

### 6.2 推荐新端点

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/model-providers` | 列出内置和自定义 ProviderConnection |
| POST | `/api/model-providers` | 创建自定义连接并可一次性提交凭据 |
| PATCH | `/api/model-providers/{providerId}` | 修改显示名、端点、协议、启停或凭据 |
| DELETE | `/api/model-providers/{providerId}` | 删除连接、从属 Profile 和凭据，受引用保护 |
| POST | `/api/model-providers/discover` | 对已保存连接或未保存草稿执行模型发现 |
| GET | `/api/model-profiles?service=llm&providerId=...` | 列出具体模型 Profile |
| POST | `/api/model-profiles` | 从发现结果或手工输入创建 Profile |
| PATCH | `/api/model-profiles/{profileId}` | 修改显示名、能力声明或启停 |
| DELETE | `/api/model-profiles/{profileId}` | 删除单个 Profile，受引用保护 |
| POST | `/api/model-profiles/{profileId}/test` | 使用真实协议执行 connectivity/usable 测试 |
| GET | `/api/runtime/status` | 返回面向 UI 的安全运行状态 |

所有请求/响应都必须先建模到 `frontend_contract/rest.py`，再生成前端类型；不得在 Python 路由和 TypeScript 中各维护一份手写 DTO。

### 6.3 创建 ProviderConnection

请求示例：

```json
{
  "service": "llm",
  "displayName": "my-openai-compatible",
  "runtime": "backend",
  "protocol": "openai_chat_completions",
  "baseUrl": "https://example.com/v1",
  "apiKey": "one-time-secret"
}
```

响应不得回显 `apiKey`，只返回 `credentialConfigured: true/false`。

PATCH 语义：

- 缺省 `apiKey`：保留旧密钥；
- 非空 `apiKey`：替换密钥；
- 显式 `clearCredential: true`：删除密钥；
- `apiKey` 与 `clearCredential` 同时出现：400；
- 从 Backend Runtime 改为其他 Runtime：清除后端密钥；
- 修改 `baseUrl` 或 `protocol` 不自动改写已有 `modelId`，但将相关 Profile 标记为“需重新测试”。

### 6.4 模型发现请求

模型发现同时支持两种模式，且必须二选一：

1. 已保存连接：提交 `providerId`，后端读取 Secret Store；
2. 未保存草稿：提交 `draft` 和一次性 `apiKey`，只用于本次请求。

请求示例：

```json
{
  "providerId": "prv_8b4f...",
  "forceRefresh": false
}
```

```json
{
  "draft": {
    "service": "llm",
    "runtime": "backend",
    "protocol": "openai_responses",
    "baseUrl": "https://example.com/v1",
    "apiKey": "one-time-secret"
  },
  "forceRefresh": false
}
```

响应示例：

```json
{
  "models": [
    {
      "id": "model-a",
      "displayName": "model-a",
      "ownedBy": "provider",
      "capabilities": {},
      "selectable": true
    }
  ],
  "source": "api",
  "fetchedAt": "2026-08-30T12:00:00Z",
  "cached": false,
  "warnings": []
}
```

约束：

- 请求中的一次性 Key 不写 YAML、不写日志、不进入错误 details；
- 发现失败不创建 Provider、不创建 Profile、不替换凭据；
- 返回 0 个模型可以是成功结果，但必须携带可理解 warning；
- Provider 不支持发现时保留手工 `modelId` 输入；
- 建议缓存成功结果 5 分钟，Key 不参与可逆缓存键；
- `forceRefresh` 仍受限流约束，不能绕过限流。

### 6.5 Profile 可用性测试

`POST /api/model-profiles/{profileId}/test` 建议保留两级：

- `connectivity`：连接、认证和最小协议握手；
- `usable`：用该 Profile 的真实 `modelId` 和真实协议产生最小输出。

测试必须返回稳定错误码，公开响应不得包含上游完整响应体、请求头、Key、内部 URL 参数或堆栈。

### 6.6 模型切换

推荐新增 WS 消息而不是改变旧字段含义：

```json
{
  "type": "switch_model_profile",
  "service": "llm",
  "profileId": "mdl_1234...",
  "clientRequestId": "..."
}
```

后端处理顺序：

1. 查 Profile；
2. 校验 enabled、service 与 Runtime；
3. 查 ProviderConnection；
4. 解析凭据；
5. 用 `protocol + modelId` 创建真实 Backend；
6. 执行现有热切换；
7. 成功后更新当前会话 `service_state`；
8. 返回 `profileId/providerId/modelId/protocol` 的安全摘要。

旧 `switch_model` 在兼容期映射到 built-in 或 legacy Profile，不直接删除。

### 6.7 运行状态契约

`GET /api/runtime/status` 是经过认证的业务诊断接口，建议响应：

```json
{
  "status": "ok",
  "observedAt": "2026-08-30T12:00:00Z",
  "backend": {
    "version": "0.1.0+...",
    "buildSha": "...",
    "protocolVersion": "2.0",
    "uptimeSeconds": 123.4,
    "fsm": "IDLE"
  },
  "services": {
    "llm": {
      "status": "ready",
      "profileId": "...",
      "provider": "deepseek",
      "model": "deepseek-v4-flash",
      "protocol": "openai_chat_completions"
    },
    "stt": {"status": "ready", "model": "sensevoice/sense-voice"},
    "tts": {"status": "disabled"},
    "embedding": {
      "status": "ready",
      "provider": "huggingface",
      "model": "BAAI/bge-large-zh-v1.5",
      "dimension": 1024,
      "fallbackMode": "semantic",
      "lastErrorCode": null
    }
  },
  "storage": {
    "mongodb": {"status": "ready"},
    "milvus": {"status": "ready"},
    "idempotency": {
      "status": "ready",
      "durable": true,
      "needsRecovery": false
    }
  },
  "memory": {
    "sessionCount": 12,
    "longTermCount": 345
  }
}
```

具体字段以生成契约为准，但安全边界不可改变：数据库连接串、数据库名、用户名、密钥、文件路径和原始异常不在响应中。

### 6.8 新错误码

至少增加以下稳定错误码，并登记到协议错误码目录：

| 错误码 | HTTP | 含义 |
|--------|------|------|
| `PROVIDER_NOT_FOUND` | 404 | ProviderConnection 不存在 |
| `MODEL_PROFILE_NOT_FOUND` | 404 | ModelProfile 不存在 |
| `PROVIDER_PROTOCOL_UNSUPPORTED` | 400 | 协议未实现或不适用于 service |
| `PROVIDER_RUNTIME_UNSUPPORTED` | 400 | 当前 Runtime 不支持该后端操作 |
| `PROVIDER_DISCOVERY_UNSUPPORTED` | 422 | Provider/协议无在线发现能力 |
| `PROVIDER_DISCOVERY_FAILED` | 502 | 上游发现请求失败 |
| `PROVIDER_DISCOVERY_TIMEOUT` | 504 | 上游发现超时 |
| `MODEL_NOT_AVAILABLE` | 422 | Profile 模型未通过可用性校验 |
| `PROFILE_IN_USE` | 409 | 沿用现有码，配置仍被引用 |
| `SSRF_BLOCKED` | 400 | 沿用现有码，目标被安全策略阻止 |
| `RATE_LIMITED` | 429 | 沿用现有码，发现/测试超限 |

公开 `details` 只允许稳定、安全、前端确实需要的字段，例如 `providerId/profileId/retryAfterSeconds`。

---

## 7. 持久化与兼容迁移

### 7.1 目标持久化形状

第一版继续使用后端本地配置文件，避免把低频配置过早迁入 MongoDB，但必须升级为版本化、原子写入：

```yaml
schemaVersion: 2
providers:
  - providerId: prv_xxx
    service: llm
    displayName: example
    kind: custom
    runtime: backend
    protocol: openai_chat_completions
    baseUrl: https://example.com/v1
    enabled: true
profiles:
  - profileId: mdl_xxx
    providerId: prv_xxx
    service: llm
    modelId: model-a
    displayName: model-a
    source: api
    enabled: true
```

密钥仍只进入 Secret Store，建议从旧 `(service, name)` 键迁移到 `(service, providerId)`。

仓库层必须提供：

- 进程内异步锁或等价单写者保护；
- 临时文件写入、flush/fsync、原子 replace；
- schemaVersion 校验；
- 迁移前备份；
- 幂等迁移；
- 损坏文件 fail-safe，不以空骨架静默覆盖原文件；
- 明确的未来多实例限制。若后端将多实例运行，再迁移到 MongoDB 或集中配置存储。

### 7.2 内置 Provider

内置 Provider 继续来自 `Module/config/providers.yaml`，不需要复制到用户配置：

- Provider Service 在读取时把内置定义投影为只读 ProviderConnection 和静态 Profile；
- 使用确定性 ID；
- `mutable=false`；
- 协议必须由配置显式声明，未声明的既有 LLM 在迁移期默认 Chat Completions；
- `models: []` 的未实现 Provider 不得被宣布为可选。

### 7.3 `custom_providers.yaml` v1 迁移

每个旧条目的迁移步骤：

1. 生成一个稳定 `providerId`；
2. 拷贝 `service/name/base_url/runtime/capabilities`；
3. 协议设置为 `openai_chat_completions`；
4. `models[]` 中每个非空唯一值生成一个正常 ModelProfile；
5. 额外生成一个隐藏的 `source=legacy` Profile，其 `modelId=name`，用于保持旧 `custom/{name}` 的真实行为；
6. 旧活动状态 `custom/{name}` 映射到该 legacy Profile；
7. 将旧 Secret 复制到 `providerId` 新键，验证后再清理旧键；
8. 原文件保留只读备份；
9. 原子写入 v2 文件；
10. 重复执行迁移不得产生重复 Provider/Profile。

之所以保留 legacy Profile，是因为当前代码实际把 `name` 作为上游模型名。直接把 `models[0]` 设为活动模型会悄悄改变用户行为。

legacy Profile 的退出策略：

- 默认不出现在新建选择列表；
- 如果当前会话正在使用，则显示为“兼容配置”；
- 用户成功切换到真实 Profile 后，不再作为默认项；
- 删除 legacy Profile 前确认所有会话与默认配置均已迁移。

### 7.4 回滚

- v2 首次写入前生成带时间戳的 v1 备份；
- 迁移失败时继续使用 v1 兼容读取，不写半成品；
- 新代码不得在无法理解更高 schemaVersion 时降级覆盖文件；
- 回滚到旧代码前必须确认 Secret 的旧键仍在，或提供反向恢复脚本；
- 迁移完成和旧键清理应分两个发布批次，避免一次提交不可逆。

---

## 8. Runtime Status 实现设计

### 8.1 不改变探针语义

- `/health` 继续是零网络 liveness；
- `/ready` 继续决定 HTTP readiness，`chatReady/fullReady` 口径不变；
- `/api/runtime/status` 只服务 UI 和诊断，不被编排器当健康探针；
- 新接口失败不得反向修改 FSM、服务开关或 readiness 状态。

### 8.2 Embedding 公共快照

为 `EmbeddingProvider` 增加公开、不可变的状态快照，禁止路由读取 `_backend/_task/_tried` 私有字段。建议状态：

| 状态 | 含义 |
|------|------|
| `not_started` | 尚未触发懒加载 |
| `loading` | 后台加载中 |
| `ready` | 语义嵌入可用 |
| `degraded` | 发生过可识别降级但仍有受限能力 |
| `unavailable` | 仅能纯文本回退 |

快照字段至少包括：Provider、模型、dimension、`semantic/text_only`、安全错误码、最近状态变化时间。

加载线程应返回“Backend + 描述信息”的结果对象，由事件循环一次性提交状态，避免超时线程晚到后写入不一致字段。

### 8.3 存储状态

- MongoDB 状态复用 session/message/attachment 三 Repository 的健康语义；
- Milvus 复用 long-term health check；
- 幂等状态复用当前 store 的 `durable/needs_recovery`；
- 聚合探测并行执行，每项有超时；
- 为 UI 轮询增加 2–5 秒短缓存，避免每次打开设置都产生重复数据库 ping；
- 缓存只保存布尔/枚举结果和时间，不缓存凭据或异常正文；
- UI 推荐 10–30 秒轮询，不应高频刷新。

### 8.4 `server_info` 与 capability

- `server_info` 继续承担建连时的轻量能力协商；
- 只有当新端点真实可用时，才增加类似 `model_provider_v2`、`model_discovery`、`runtime_status`、`openai_responses` 的 feature flag；
- 动态运行状态不塞进一次性的 `server_info`；
- 前端根据 feature flag 决定是否显示在线发现和状态面板，不靠版本号猜测。

---

## 9. 安全设计

### 9.1 凭据

- Backend Runtime 的 Key 归 ProviderConnection，不重复存到每个 Profile；
- 列表、创建、更新、发现、测试、错误和日志均不得回显 Key；
- 编辑时不回填旧 Key，只返回 `credentialConfigured`；
- 一次性草稿 Key 只存在于请求作用域；
- 日志过滤 `Authorization/api-key/x-api-key` 等敏感头；
- 删除连接时，配置和 Secret 的处理必须具有明确顺序与补偿；
- Desktop/Local Worker 密钥不进入后端 Secret Store。

### 9.2 SSRF

模型发现、连接测试、Profile 测试和真实调用必须统一使用当前受保护的 URL 校验与 HTTP Client：

- 校验 scheme、host、解析后的 IP；
- 每次重定向都重新校验；
- 防 DNS rebinding；
- 限制重定向次数；
- 限制连接和总超时；
- 限制响应体大小和模型条目数；
- 产品模式不因“模型发现”新增私网/loopback 豁免；
- 如未来确需访问私网 Provider，应另行设计管理员 allowlist，而不是添加全局关闭 SSRF 的开关。

建议初始预算：连接超时 3 秒、总超时 10 秒、最多 3 次重定向、响应体最多 1 MiB、最多归一化 1000 个模型。最终常量应集中配置并覆盖边界测试。

### 9.3 认证、限流和审计

- 所有新端点接入现有业务 API 认证；
- discover/test 使用独立的高成本限流桶；
- 限流键继续按当前认证主体/远端策略，不能让不同用户互相耗尽额度；
- 记录 requestId、providerId、profileId、协议、结果类别、耗时；
- 不记录 Key、完整上游响应、用户消息正文或带查询参数的敏感 URL；
- `forceRefresh` 不能绕过缓存之外的限流。

### 9.4 错误脱敏

- Adapter 把 SDK/HTTP 异常映射为稳定公开错误；
- 完整异常只进受控后端日志；
- 前端响应使用通用中文文案和安全 details；
- TLS、认证、超时、协议不兼容、模型不存在分别分类；
- 不再把未审查的 `str(e)` 直接作为新接口响应。

---

## 10. 模块与分层落点

建议文件布局，最终可按实现规模微调：

```text
core/providers/
  models.py                    # ProviderConnection / ModelProfile 领域模型
  repository.py                # schema v2、锁、原子写、迁移
  service.py                   # CRUD、引用保护、兼容映射
  credentials.py               # Secret Store 适配
  discovery.py                 # 发现用例、缓存、归一化
  protocol_registry.py         # 协议 Adapter 注册
  adapters/
    openai_chat.py
    openai_responses.py

core/input/
  model_provider_routes.py     # 新 typed REST 路由
  runtime_status_routes.py     # UI 诊断状态

core/services/
  runtime_status.py            # 状态聚合服务

Module/llm/
  protocol.py                  # 保持统一 LLMBackend 边界
  openai_compat.py             # Chat Completions
  openai_responses.py          # Responses 新实现
  registry.py                  # 通过协议 Adapter 创建

frontend_contract/
  rest.py                      # 新请求/响应 DTO
  ws_message.py                # switch_model_profile + capability

core/memory/
  embedding_provider.py        # 公共 snapshot 与状态机
```

分层约束：

- `core/input` 不读写 YAML、不直接创建 OpenAI Client；
- `core/providers` 不依赖 aiohttp Request/Response；
- `Module/llm` 不知道前端 DTO；
- `frontend_contract` 不导入运行时 Service；
- Embedding 状态通过公开接口读取，不跨模块摸私有属性；
- 旧 `custom_providers.py` 在兼容期只作为 v1 Adapter，不能继续扩张为新模型中心。

---

## 11. 分批实施计划

### 批次 0：冻结契约与决策

- 确认 ID 格式、协议枚举和 v1 兼容周期；
- 在 `frontend_contract` 建模新 REST/WS 类型；
- 更新协议文档、错误码目录和 capability；
- 生成前端契约并通过 schema/golden 门禁；
- 不宣告尚未实现的 feature flag。

### 批次 1：Provider/Profile 拆分

- 实现领域模型、Repository、Service；
- 投影内置 Provider/Profile；
- 实现 schema v2 原子持久化；
- 实现 v1 幂等迁移和 legacy Profile；
- 实现新 CRUD；
- 新增 `switch_model_profile`；
- 保留旧 API/WS 兼容映射；
- 确认选中 `modelId` 原样发送到上游。

### 批次 2：在线模型发现

- 实现 DiscoveryService 和 Chat Completions Adapter；
- 接入 Secret Store 和草稿一次性 Key；
- 接入 SSRF、逐跳重定向检查、超时、响应限制、缓存和限流；
- 实现手工输入 fallback；
- 接入 typed contract 和 feature flag；
- 完成发现失败不产生持久化副作用的测试。

### 批次 3：Responses API

- 新增 Responses Backend；
- 完成消息、流式文本、工具调用、usage、取消和错误归一化；
- 注册协议并接入 Profile 测试/切换；
- 做真实兼容端点联调；
- 全链路通过后才把 `openai_responses` 宣告给前端。

### 批次 4：Runtime Status

- 为 EmbeddingProvider 增加公共快照；
- 实现 RuntimeStatusService 与短缓存；
- 新增 authenticated `/api/runtime/status`；
- 输出 LLM/STT/TTS/Embedding 与 Mongo/Milvus/Idempotency 安全状态；
- 保持 `/health`、`/ready` 语义和已有测试不变。

### 批次 5：加固与收口

- 完成故障注入、日志脱敏、并发写和迁移回滚测试；
- 更新 runbook、安全、架构和设置文档；
- 统计旧接口使用情况；
- 前后端稳定一个发布周期后再提出旧 API 废弃计划；
- legacy Profile 全部迁移前不删除兼容代码和旧 Secret 键。

---

## 12. 后端实施任务清单

### 12.1 领域与持久化

- [ ] BE-MODEL-001：新增 ProviderConnection 与 ModelProfile 类型及约束。
- [ ] BE-MODEL-002：定义内置和自定义稳定 ID，保证前端无需解析。
- [ ] BE-MODEL-003：实现 schema v2 Repository、原子写、锁和损坏保护。
- [ ] BE-MODEL-004：实现 v1→v2 幂等迁移、备份和 legacy Profile。
- [ ] BE-MODEL-005：迁移 Secret 键，并提供跨版本回滚窗口。
- [ ] BE-MODEL-006：实现 Provider/Profile 引用保护和级联删除规则。
- [ ] BE-MODEL-007：避免显示名重命名导致活动配置失效。

### 12.2 协议与执行

- [ ] BE-MODEL-010：定义协议枚举与 Adapter Registry。
- [ ] BE-MODEL-011：将现有 OpenAICompatBackend 接入 Chat Completions Adapter。
- [ ] BE-MODEL-012：LLMFactory 改为由 connection.protocol + profile.modelId 创建。
- [ ] BE-MODEL-013：实现 Responses Backend 的非流式聊天。
- [ ] BE-MODEL-014：实现 Responses 流式文本和取消。
- [ ] BE-MODEL-015：实现 Responses 工具调用增量组装。
- [ ] BE-MODEL-016：实现 Responses usage 归一化。
- [ ] BE-MODEL-017：实现统一安全错误映射和 Client 关闭。

### 12.3 API 与契约

- [ ] BE-MODEL-020：在 `frontend_contract/rest.py` 增加 Provider/Profile DTO。
- [ ] BE-MODEL-021：实现 ProviderConnection CRUD。
- [ ] BE-MODEL-022：实现 ModelProfile CRUD 和可用性测试。
- [ ] BE-MODEL-023：实现 `switch_model_profile` typed WS 消息。
- [ ] BE-MODEL-024：实现旧 `/api/providers` 与 `switch_model` 兼容映射。
- [ ] BE-MODEL-025：生成前端 TS、schema/raw/golden，并通过一致性检查。
- [ ] BE-MODEL-026：新增 capability，但只在真实能力完成后置 true。

### 12.4 模型发现与安全

- [ ] BE-MODEL-030：实现已保存连接和未保存草稿两种发现模式。
- [ ] BE-MODEL-031：实现模型结果归一化、去重、排序和数量上限。
- [ ] BE-MODEL-032：接入逐跳 SSRF 校验和 guarded client。
- [ ] BE-MODEL-033：实现超时、响应体、重定向和并发限制。
- [ ] BE-MODEL-034：实现成功缓存与 `forceRefresh` 语义。
- [ ] BE-MODEL-035：新增 discover/test 高成本限流桶。
- [ ] BE-MODEL-036：保证一次性 Key、Header 和上游响应不进入日志。
- [ ] BE-MODEL-037：登记并测试新增错误码。

### 12.5 Runtime Status

- [ ] BE-MODEL-040：实现 Embedding 不可变状态快照。
- [ ] BE-MODEL-041：跟踪实际 Provider、模型、dimension 和 fallbackMode。
- [ ] BE-MODEL-042：实现并行、超时、短缓存的存储状态聚合。
- [ ] BE-MODEL-043：实现 `/api/runtime/status` typed route。
- [ ] BE-MODEL-044：验证响应中不存在 URI、凭据、路径和原始异常。
- [ ] BE-MODEL-045：保持 `/health`、`/ready` wire 与 HTTP 语义不变。

### 12.6 文档与运维

- [ ] BE-MODEL-050：更新 `electron-backend-protocol-v2.md`。
- [ ] BE-MODEL-051：更新 `electron-backend-security.md`。
- [ ] BE-MODEL-052：更新 `architecture.md` 和相关模块 README。
- [ ] BE-MODEL-053：补充迁移、回滚、私网 Provider allowlist 的运维说明。
- [ ] BE-MODEL-054：在联调 runbook 增加发现、协议和运行状态检查。

---

## 13. 测试与验证矩阵

### 13.1 单元测试

- Provider/Profile 校验、ID 稳定性和 service 一致性；
- Repository 原子写、并发写、损坏文件和未知 schemaVersion；
- v1 迁移幂等、legacy Profile、Secret 双键窗口；
- Adapter 注册、未知协议、未实现协议不被宣告；
- 模型发现归一化、空列表、重复 ID、超量截断；
- Responses 消息/工具/usage 事件归一化；
- Embedding 状态机和超时晚到竞态；
- Runtime Status 脱敏序列化。

### 13.2 Service/路由测试

- 新 CRUD 的认证、校验、引用保护和错误信封；
- 编辑不带 Key 保留 Secret，清除和 Runtime 切换正确删除；
- 发现成功不自动保存，失败零副作用；
- Provider ID 与 draft 二选一；
- discover/test 限流隔离；
- 旧 API 到 legacy Profile 的兼容映射；
- `switch_model_profile` 发送真实 `modelId`；
- `/api/runtime/status` 缓存、超时与依赖降级。

### 13.3 安全测试

- loopback、私网、IPv6、混淆地址、DNS rebinding；
- 每跳重定向重新校验；
- 超大响应体、超多模型、慢响应和重定向循环；
- Key 不出现在响应、日志、缓存键、YAML 和异常；
- 原始 Provider 错误体不透传；
- 未认证访问全部拒绝；
- `forceRefresh` 不能绕过限流。

### 13.4 集成测试

- 内置 Chat Completions Profile 行为不变；
- 自定义 v1 配置迁移后仍能调用旧 `custom/{name}`；
- 同一连接下发现、保存并切换两个不同模型，抓取上游请求确认模型 ID 精确匹配；
- Chat Completions 和 Responses 分别完成文本流、工具调用、usage 和取消；
- 后端重启后 Provider/Profile/Secret 引用稳定；
- OpenAI 不可用时 Embedding 正确落到 HuggingFace；两者不可用时报告 text-only；
- Mongo/Milvus/幂等分别故障时，Runtime Status 与 `/ready` 各自保持既定语义。

### 13.5 契约门禁

- Python contract model 校验；
- TypeScript 生成产物无手工漂移；
- schemaHash/raw/golden 更新；
- 新字段 optional/required/null/omitted 语义明确；
- feature flag 与实际实现一致；
- 旧前端连接新后端仍能使用兼容 API。

---

## 14. 完成验收标准

只有同时满足以下条件，才可认为本设计完成实施：

1. ProviderConnection 与 ModelProfile 已独立持久化并具有稳定 ID；
2. 旧自定义配置可幂等迁移，活动选择和 Secret 不丢失；
3. 在线发现由后端发起，Renderer 不直连 Provider；
4. 发现失败不会创建或修改任何配置；
5. 用户选择的 `modelId` 与发送给上游的模型 ID 完全一致；
6. Chat Completions 与 Responses 选择会创建不同的真实 Adapter；
7. 两种协议均通过文本流、工具调用、usage、取消和错误测试；
8. 新端点均有认证、限流、SSRF、超时、大小限制和日志脱敏；
9. Secret 不进入 YAML、响应、日志、缓存或生成契约；
10. `/api/runtime/status` 能报告真实 Embedding 和存储状态；
11. Runtime Status 不暴露数据库 URI、名称、凭据、路径或原始异常；
12. `/health` 与 `/ready` 的现有语义、状态码和契约没有被改变；
13. 前端只使用生成契约和 capability，不靠版本号或硬编码猜测；
14. 旧 `/api/providers` 与 `switch_model` 在约定兼容期继续工作；
15. 协议、安全、架构、模块 README 与联调 runbook 已同步更新。

---

## 15. 待确认决策与推荐默认值

| 决策 | 推荐默认值 | 原因 |
|------|------------|------|
| 自定义 ID | `prv_<uuid>` / `mdl_<uuid>` | 稳定、不携带业务语义 |
| 协议归属 | 第一版 Provider 级 | 减少无效组合；未来可显式 override |
| 既有自定义协议 | Chat Completions | 保持当前行为 |
| 配置存储 | schema v2 本地 YAML + 原子写 | 当前低频单进程配置足够，迁移成本较低 |
| 发现缓存 | 成功结果 5 分钟 | 降低上游压力，仍允许显式刷新 |
| 私网自定义端点 | 默认继续阻止 | 不以便利性换取 SSRF 回退 |
| Responses 范围 | 先支持经过验证的 OpenAI/兼容实现 | 避免宣称所有兼容端点都相同 |
| Embedding 配置 | 第一版只读状态 | 先解决可观测性，不扩大配置风险 |
| 数据库信息 | 只返回状态枚举 | 前端无需且不应知道连接细节 |
| 旧接口兼容期 | 至少一个稳定发布周期 | 支持前后端错峰升级和回滚 |

上述默认值可以在实施前调整；一旦进入契约实现，应写入协议文档并避免在代码中隐式决定。

---

## 16. 主要风险

| 风险 | 影响 | 缓解方式 |
|------|------|----------|
| v1 `name` 实际被当模型使用 | 迁移后模型悄然变化 | 隐藏 legacy Profile 保持原行为 |
| Responses 兼容实现差异 | 测试通过但工具流失败 | Adapter 级完整集成测试，不只测连通 |
| 模型发现被利用为 SSRF | 访问内网或元数据服务 | 共用 guarded client、逐跳校验、限流与大小限制 |
| 配置与 Secret 非事务更新 | 产生孤儿配置或孤儿密钥 | 明确写入顺序、补偿、双键迁移窗口 |
| Runtime Status 高频探测 | 给数据库增加额外压力 | 并行超时、短缓存、前端低频轮询 |
| capability 超前声明 | 前端展示不可用功能 | 实现、测试、文档完成后才置 true |
| 新旧接口长期双轨 | 维护成本上升 | 使用统计、明确废弃条件和期限 |

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目文档索引](README.md) | 后端当前有效文档入口 |
| [Electron 后端协议 v2](electron-backend-protocol-v2.md) | REST/WS 契约、能力协商与错误码的正式落点 |
| [Electron 后端安全](electron-backend-security.md) | 认证、Secret Store、SSRF、限流和探针安全边界 |
| [完整架构详解](architecture.md) | Core、Module、输入层和数据流的现有总体架构 |
| [后端架构分层与模块化增量评估](review/2026-08-30-后端架构分层与模块化增量评估.md) | 当前后端分层与模块化风险背景 |

