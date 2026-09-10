# Electron 后端改造 — 批次 5：模型 Profile 与密钥所有权 / TTS 声音（Spec）

**Spec 版本：** v0.3（修订：首轮 review 吸收——D8 契约现状纠正 + desktop/local test/switch 语义 + SSRF runtime-aware 注入判定与 test 端点 body base_url 防线 + v1 wire 兼容表述修正 + credentialConfigured 死字段语义 + TTSStreamManager voice 注入机制 + voices/preview/set_tts_voice 会话归属锁定 + 跨会话删除残差声明 + 错误信封双轨 + custom TTS 静态声音 + Header 允许名单 + 挂空引用修正；**二轮 re-review 吸收**——移除 PROFILE_MISSING_BASE_URL 新码（缺 base_url 沿用 v1 ApiFail，消 runtime 缺省重叠）、set_tts_voice 锁死活动会话（消校验/写入跨会话错位）、内置 provider test body base_url 并入 TOCTOU 取舍声明、SSRF 拒绝消息统一）
**文档日期：** 2026-08-05
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（§10 BE-050~051）与 `docs/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md`（§六批次 5）
**前置：** 批次 0 基线 + 批次 1（认证/错误信封）+ 批次 2（结构化聊天 v2 + service_state 运行时状态先例）+ 批次 3（多会话 + GenerationRegistry model 持久化先例）+ 批次 4（Runtime 感知 + `runtime` 枚举先例）

---

## 1. 目的

把模型配置从"运维预配置（`api_key_env` 环境变量名）"升级为**用户可管理的三类 Profile**：LLM/STT/TTS Profile 可安全新增、同名更新、测试、激活、删除，并带能力字段与执行位置（Runtime）；引入**服务器 Secret Store**，让 Electron 设置页能一次性提交密钥、后端只回 `credentialConfigured=true` 而永不读回；自定义 Endpoint 落实 SSRF 防护；新增 TTS 声音列表与固定文本试听，支持运行时声音选择覆盖。

**兼容硬约束**：既有客户端 wire **读取兼容**——现有键位不删、值语义不变（REST `/api/providers` 新增字段为纯增量，见 §9 修正说明）；既有错误路径（v1 ApiFail）对老参数错误**不变**；`CustomProvider.api_key_env` 保留兼容；现有测试全绿（契约测试同步补新字段断言）。

---

## 2. 已定决策

| 决策 | 结论 |
|---|---|
| D1 | **密钥所有权：服务器 Secret Store（docs/14 §10 推荐路径 1）。** Electron 一次性提交密钥 → 后端写受控 Secret Store → 只回 `credentialConfigured=true`，**永不读回**。 |
| D2 | **Secret Store 形态：文件 0600 + gitignore，明文（磁盘权限即边界），不落 MongoDB**；加密留 batch 7+ 评估。 |
| D3 | **旧 `api_key_env` 兼容：并存不迁移。** 老条目继续 env 变量解析，新条目 `api_key` 直存 Secret Store；`resolve_model_params` 统一两路径（secret store 优先，api_key_env 兜底）。 |
| D4 | **Profile 加 `runtime` 字段**（`backend`/`desktop`/`local_worker`，默认 `backend`；枚举复用批次 4）。**Backend 类后端管密钥**（Secret Store）；**desktop/local_worker 类仅声明占位、密钥归 Electron 管**——后端提交 `api_key` 到此类条目**拒绝**（400）。**此类条目的 test/switch 后端不代理**（返回稳定错误，见 D16）。 |
| D5 | **SSRF 防护：`validate_endpoint`（静态 + DNS 解析校验）** 用于保存/切换；**出站再校验**（custom backend 的 HTTP 客户端注入校验传输层，重定向每跳过校验）。作用于 **custom + runtime=backend** 的 base_url + **`/api/providers/test` 请求体携带的任何 base_url**（不设 provider 白名单——body 任意 URL 都需过校验）。内置 provider 自身配置的 base_url（含 `local: true` 如 ollama/gpt-sovits）为运维托管、豁免。 |
| D6 | **TTS 声音数据源：静态声明优先 + MiniMax 动态拉取。** `TTSBackend.list_voices()` 协议方法；gpt-sovits 返回静态声明，MiniMax 经现有 `POST /v1/get_voice`（`voice_type=system`）动态拉取 + 进程内缓存；custom TTS（OpenAI 兼容）返回模块内固定静态声音集。 |
| D7 | **试听接口：新 `POST /api/tts/preview`**（激活 TTS profile + 可选 voice_id + 固定短文本 → 音频 base64）。**不塞进 `/api/providers/test`**。试听不读取真实聊天。 |
| D8 | **「本条回复实际模型」：契约补全 + 捕获校正。** `GenerationCompletedMessage.model` **已是必填 `str`**（构造点已填实际模型）——本批**不动其必填性**，仅校正捕获来源为 generation 启动时快照；`GenerationStartedMessage.model`（已声明 `Optional`）本批**填充**；`GenerationFailedMessage` **补 `model: Optional[str]`**。`GenerationRegistry` **已持久化 model（批次 3 先例）**——本批零改动。 |
| D9 | **删除约束：当前激活 profile 拒删（409 + 明确错误）**；非激活直接删 + 清除对应 Secret Store 密钥。激活判定 = 活动会话 `service_state.{svc}_model` 或 `config.{svc}.model` 指向 `custom/{name}`（LLM 另含 `vision.model`）。**跨会话引用残差显式声明**（见 §3.6）。 |
| D10 | **声音选择覆盖：`service_state` 加可选 `tts_voice`**（不动 character 映射默认）；设置后 TTS 合成以该 voice 为准，未设置回退 character 映射；删除/切换失败保留原选择。 |
| D11 | **能力字段：`capabilities: dict` 自由键**（`supports_prefix`/`supports_multimodal` 等，与 providers.yaml 命名对齐）；custom 条目声明存 YAML，内置 ProviderEntry 从 providers.yaml 带出已知键。 |
| D12 | **声音试听固定文本**：常量（如「你好，我是声音测试。」），永不从会话/聊天读取；试听端点不写任何历史。 |
| D13 | **SSRF 范围**：仅 custom + runtime=backend 的 base_url 与 `/api/providers/test` 的 body base_url 走 SSRF；desktop/local 的 base_url 后端不拨号、跳过 SSRF 且**允许为空**（save 时 base_url 仅 backend 必填）。 |
| D14 | **切换失败保留原选择**：沿用批次 2 switch 语义（先建后换/先测后换，失败不替换 + 关闭新建未挂载后端）；本批在 switch 路径接入 SSRF 校验与 runtime 守卫，行为不变。 |
| D15 | **`/api/tts/voices` + `/api/tts/preview` + WS `set_tts_voice` 均走认证**（产品 token，批次 1 的 AGENT_ACCESS_TOKEN 域）。 |
| D16 | **非 backend runtime 的 test/switch**：返回 400 `PROFILE_RUNTIME_NOT_BACKEND`（"该 Profile 由 Electron 执行，后端不代理"）；前端凭 `runtime` 字段路由到 Electron 侧，不调用后端。 |
| D17 | **错误信封双轨**：新批次 5 错误码（SSRF_BLOCKED/PROFILE_IN_USE/PROFILE_INVALID_RUNTIME/PROFILE_RUNTIME_NOT_BACKEND）走 `ProtocolError` 信封；既有参数错误（未知 service/缺 name/缺 base_url/无效请求体）**维持 v1 ApiFail 不动**——其中**缺 base_url 沿用现 handler 的 v1 ApiFail 检查**（不设新码，避免与 runtime 缺省 backend 的语义重叠）。兼容：`ProtocolError.error` = `message` 别名，v1 前端失败路径读 `err.error` 仍成立。 |
| D18 | **runtime 变更的密钥清理**：条目 runtime 从 backend 改为 desktop/local → 后端**清除**该条目 Secret Store 密钥；从 desktop/local 改为 backend → 要求**重新提交** api_key（缺省则 credentialConfigured=false）。 |
| D19 | **REST 新增字段默认非 None 上 wire（接受键新增）**：`credentialConfigured: bool=False`、`runtime: str="backend"`、`capabilities: dict={}` 均序列化（wire_dict 只省略 None）；不追求 providers REST 字节级不变，改为"既有键读取兼容"（§9）。 |

---

## 3. BE-050 模型 Profile 与密钥

### 3.1 Secret Store（新 `core/storage/secret_store.py`）

文件持久化，单一 JSON 文件 `data/secrets/profiles.json`：

```json
{"llm": {"myprovider": "sk-...", "siliconflow": "sk-..."}, "stt": {}, "tts": {}}
```

```python
# core/storage/secret_store.py
def get_secret(service: str, name: str, path: Path | None = None) -> str | None
def set_secret(service: str, name: str, value: str, path: Path | None = None) -> None
def delete_secret(service: str, name: str, path: Path | None = None) -> None
def has_secret(service: str, name: str, path: Path | None = None) -> bool
def all_configured(service: str, path: Path | None = None) -> set[str]   # 该 service 有密钥的 name 集合
```

- **路径**：`ProjectPath.DATA_SECRETS = "data/secrets"`（新成员），文件 `profiles.json`。
- **权限**：目录 `0o700` + 文件 `0o600`；`.gitignore` 加 `data/secrets/`。
- **原子写**：tmp 文件 + `os.replace`；读失败（文件损坏）返回空并 `logger.warning`（不崩溃）。
- **永不读回**：所有响应/日志路径只出 `credentialConfigured` 布尔；`details`/日志禁止打印密钥。
- 单例访问：模块级函数 + `_DEFAULT_DIR`（与 `custom_providers.py` 同风格），`path` 参数供测试注入 tmp 目录。

### 3.2 契约扩展（`frontend_contract/rest.py`）

```python
class ProviderEntry(ContractModel):
    models: list[str]
    credentialConfigured: bool = False   # env_key/url_key 已配置 或 local（D19：序列化上 wire）
    runtime: str = "backend"             # 内置 provider 恒 backend
    capabilities: dict[str, Any] = {}    # 从 providers.yaml 带出（supports_prefix/supports_multimodal 等）

class CustomProvider(ContractModel):
    name: str
    base_url: str                        # desktop/local 可为 ""
    api_key_env: Optional[str] = None    # 兼容保留（运维预配置）
    models: Optional[list[str]] = None
    runtime: str = "backend"             # backend|desktop|local_worker（D4）
    credentialConfigured: bool = False   # backend: secret store 或 env 有密钥；desktop/local: 恒 False（后端不管理，Electron 侧判定）
    capabilities: dict[str, Any] = {}    # D11

class ProvidersResponse(ContractModel):   # 结构不变，仅成员类型扩展
    llm/stt/tts: dict[str, ProviderEntry]
    custom: dict[str, list[CustomProvider]]
    unconfigured: dict[str, list[UnconfiguredEntry]]
```

- `custom_providers.yaml` 条目新增 `runtime`/`capabilities` 键（api_key 永不入 YAML）。
- **读响应永不含密钥**：`credentialConfigured=true` 只表"有可用密钥"。
- **`credentialConfigured` 语义（三处一致声明）**：
  - 内置 `ProviderEntry`：`local: true` 恒 true；否则 env_key/url_key 已设则 true。
  - custom + backend：secret store 有密钥 **或** api_key_env 对应 env 已设 → true。
  - custom + desktop/local：**恒 false**（后端不管理本机密钥）；Electron 前端看到 `runtime=desktop/local` 时**自行**合并本机密钥状态展示，不以后端 false 当作"未配置"。
- 保存/测试/切换读路径全部经 `resolve_model_params` 统一（D3）。

### 3.3 保存流程（`POST /api/providers` 增强）

请求体 `{service, entry: {name, base_url?, api_key?, api_key_env?, models?, runtime?, capabilities?}}`：

1. `service` ∈ {llm, stt, tts}，`name` 非空；`runtime` ∈ 枚举（缺省 `backend`）。
2. `runtime=backend`：`base_url` 必填（缺 → 400，**沿用现 handler 的 v1 ApiFail 检查**，D17——不设新码）+ **SSRF 校验**（D5/D13）。
   `runtime=desktop/local_worker`：`base_url` 可空、跳过 SSRF；携带 `api_key` → **400 `PROFILE_INVALID_RUNTIME`**（D4）。
3. `api_key` 非空（仅 backend）：写入 Secret Store；**不入 YAML**。
4. `api_key` 空（backend 同名更新）：**保留已有 secret**（upsert 不丢密钥，D3）。
5. `api_key_env` 保留兼容（两者可并存：secret store 优先）。
6. **runtime 变更清理（D18）**：backend→desktop/local 删除该条目 secret；desktop/local→backend 无新 key → credentialConfigured=false。
7. 写入 custom_providers.yaml（`runtime`/`capabilities`/`models`/`api_key_env`，无密钥）。
8. 响应返回更新后的 custom 列表（`credentialConfigured` 反映实际密钥状态，含 D18 清理后）。

**错误**：SSRF 拒绝 → 400 `SSRF_BLOCKED`（消息不泄露 host/IP）；非 backend runtime 带 api_key → 400 `PROFILE_INVALID_RUNTIME`；以上均 `ProtocolError` 信封（D17）。缺 base_url/未知 service/无效请求体 → v1 ApiFail 既有路径不动。

### 3.4 解析统一（`resolve_model_params` 扩展）

`core/input/custom_providers.py::resolve_model_params(service, model_id)`：

- custom 条目：`base_url` 从 YAML；`api_key` 解析顺序 = **Secret Store 直存 > `api_key_env` env 变量**（D3）；**返回同时携带 `runtime`**（供 switch/test 做 D16 守卫）。
- 返回参数给 `switch_llm/stt/tts` 与 `/api/providers/test`（现路径，行为不变，仅 key 来源扩展 + runtime 随附）。

### 3.5 SSRF 防护（新 `core/input/ssrf.py`）

```python
class SSRFError(ValueError): ...

def validate_endpoint(url: str) -> None:
    """校验 endpoint。非法 → 抛 SSRFError（调用方转 400 SSRF_BLOCKED）。"""

def build_guarded_http_client(base_url: str, headers: dict | None = None) -> httpx.AsyncClient:
    """返回带校验传输层的 httpx client——每次连接（含重定向每跳）解析目标 IP 校验 + 跨源重定向剥敏感头。"""
```

**规则**（D5）：
- scheme 缺省视为 `https`；仅允许 `http`/`https`。
- 拒绝云元数据与特殊主机：`169.254.169.254`、`metadata.google.internal`、`100.100.100.200`、`metadata` 等（字面量 + 解析后 IP 双保险）。
- `socket.getaddrinfo` 解析 host；对每个候选 IP 拒绝：回环（`127/8`、`::1`）、链路本地（`169.254/16`、`fe80::/10`）、私网（`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`）、保留/多播/受限（`0.0.0.0`、`255.255.255.255`、`224/4`、`240/4`、`100.64/10` CGNAT）。
- **Header 允许名单（docs/14 §10 显式要求）**：自定义 backend 出站仅允许 `Authorization`/`Content-Type`/`Accept`/`User-Agent`；**跨源重定向时剥除允许名单外全部 Header（含 Authorization）**——防 `X-API-Key` 等敏感头泄漏到内网重定向目标。httpx 默认只剥 Authorization，需在守卫传输层显式强化。
- **出站再校验**：`build_guarded_http_client` 内自建 `httpx.AsyncHTTPTransport` 子类，`handle_async_request` 在连接前对目标 host 解析校验（覆盖每跳），`follow_redirects=True` 保留。
- **注入点**（custom + backend runtime 的 base_url）：openai 兼容后端（LLM/STT）以 `AsyncOpenAI(http_client=guarded)` 注入；httpx 直连后端（TTS minimax/openai_tts）构造时替换 client。内置 provider 不注入（豁免）。
- **`/api/providers/test` 防线（D5）**：body 携带的 `base_url` **无论 provider** 一律 `validate_endpoint`（test 是客户端任意拨号内网的唯一现成通道，必须关死）。
- **调用时机**：`providers_save_handler`（保存）、`switch_llm/stt/tts`（切换，custom+backend）、`/api/providers/test`（body base_url + custom backend 解析）、custom backend 构造（出站）。
- **不可解析行为**：保存/切换时 `validate_endpoint` 无法解析 host（NXDOMAIN/临时失败）→ 400 `SSRF_BLOCKED`（消息"无法解析目标主机"），合法 URL 需可解析才能保存。
- **已知取舍（显式声明）**：守卫在连接前校验、随后由 httpx 二次解析建连，存在 TOCTOU 窗口（DNS rebinding 完全闭合需 IP pinning，批次 5 范围外，记录为已知缺口；docs/15 高风险专项已在 D5 锁定的"出站再校验"层面覆盖单次切换，未承诺防活跃 rebinding）。**内置 provider 经 `/api/providers/test` 传 body base_url 时同样处于该窗口**（内置后端不注入守卫；test 为一次性诊断路径，防护同 custom+backend 出站再校验的取舍一致，不额外扩展）。
- **测试只使用固定最小样例**（docs/14 §10）：测试集用 `http://127.0.0.1`、`http://169.254.169.254`、`http://10.0.0.1`、`https://example.com` 等固定 URL，**不触网**（mock 解析）。

### 3.6 删除约束（`DELETE /api/providers/{service}/{name}`）

- **激活判定**：活动会话 `service_state.{svc}_model`（若有）或 `config.{svc}.model` 以 `custom/{name}` 开头 → 激活；LLM 另含 `vision.model`。
- 激活 → **409 `PROFILE_IN_USE`**（消息"请先切换到其他模型"）；非激活 → 删除 YAML 条目 + **清除 Secret Store 对应密钥**（D9）。
- **跨会话引用残差（显式声明）**：仅检查活动会话 + config；**非活动持久化会话**的 service_state 若引用被删 profile，本批不扫描（Mongo 全量扫描成本高）。残差行为 = 切换/重启到该会话时后端重建失败并返回明确错误（不静默损坏历史消息文本）。该取舍记录待 batch 7+ 评估会话级引用索引。

### 3.7 激活 / 切换（runtime 守卫）

- 复用 WS `switch_model` + `switch_llm/stt/tts`（现路径，服务端已"先测后换/先建后换"）。
- 本批增量：
  1. `resolve_model_params` 返回 runtime；`runtime != "backend"` → **400 `PROFILE_RUNTIME_NOT_BACKEND`**（D16，后端不代理本机 Profile 的切换）。
  2. switch 到 custom+backend 前 `validate_endpoint(base_url)`（D14，行为不变仅加校验）。
  3. `broadcast_capabilities` 携带当前模型（service_state 已写入，确认接线）。
- `capabilities` 广播的 model 反映"当前选择"；"本条回复实际模型"走 §3.8。

### 3.8 本条回复实际模型（D8）

- **捕获校正**：turn 启动时把实际 LLM 模型 ID（`agent.llm.model`，含热切换）快照进 `GenerationContext`，并作为 `model` 传给 generation 启动/完成/失败事件——替代现状 `Completed` 直接读 `self._llm.model` 的即时读取（避免 turn 中切换导致完成帧模型与启动时不一致）。
- **契约**：
  - `GenerationCompletedMessage.model`：**保持必填 `str`**（现状已填，零改动）。
  - `GenerationStartedMessage.model`（已声明 `Optional[str]=None`）：本批填充（turn_started 传实际模型）。
  - `GenerationFailedMessage`：**补 `model: Optional[str] = None`**（缺省省略，不破坏现有 failed 帧）。
- **`GenerationRegistry`：零改动**（register 已存 model，批次 3 先例）。
- 前端用 `generation.model` 区分"本条回复实际模型"与 capabilities/switch 回显的"当前选择"。

---

## 4. BE-051 TTS 声音

### 4.1 声音列表（`GET /api/tts/voices`）

新契约：

```python
class TtsVoiceEntry(ContractModel):
    id: str
    name: Optional[str] = None
    languages: list[str] = []
    capabilities: dict[str, Any] = {}

class TtsVoicesResponse(ContractModel):
    voices: list[TtsVoiceEntry]
    current: Optional[str] = None        # 活动会话 service_state.tts_voice 或后端默认
    source: str = "static"               # static|api（数据源）
    error: Optional[str] = None
```

- **数据源**（D6）：`TTSBackend` 协议增 `list_voices() -> list[TtsVoiceEntry]`：
  - MiniMax：经现有 `POST /v1/get_voice`（`voice_type=system`）拉取 + 进程内缓存。
  - gpt-sovits 等本地：返回静态声明（模块内常量）。
  - **custom TTS（OpenAI 兼容 `openai_tts`）**：返回模块内固定静态声音集（alloy/echo/fable/onyx/nova/shimmer 等 OpenAI 固定 voice 集）——避免激活自定义 TTS 后声音面板为空。
  - 协议缺省：未实现 `list_voices()` 的旧后端 → 空列表（`source="static"`）。
- **会话归属（锁定）**：`/api/tts/voices` 恒作用于**活动会话**的激活 TTS profile（`agent.session`，批次 3 全局活动指针）。多会话下 profile 与 voice 状态 per-session——列表按活动会话的激活 profile 拉取，避免 A 会话切 minimax、B 会话列表取 gpt-sovits 的错位。
- 端点逻辑：取激活 TTS profile 后端 `list_voices()`；后端不可用/未激活 → `{voices: [], current: None, source: "static", error: "TTS 未启用"}`（200，不炸）。
- `current`：活动会话 `service_state.tts_voice` 优先，否则后端默认 voice。

### 4.2 试听（`POST /api/tts/preview`）

```python
class TtsPreviewResult(ContractModel):
    ok: bool
    mime: Optional[str] = None
    duration_ms: Optional[int] = None
    audio_base64: Optional[str] = None
    error: Optional[str] = None
```

- 请求体 `{voice_id?: str}`（缺省 → 当前 voice）；使用**固定试听文本常量**（D12），永不读聊天。
- **会话归属（锁定）**：恒作用于**活动会话**的激活 TTS profile。
- 合成走激活后端 `synthesize(text, character=..., voice_id=...)`（voice 覆盖经 §4.3 链路）。
- 失败 → `{ok: False, error}`；成功 → `{ok: True, mime, audio_base64, duration_ms?}`。
- 音频体积：固定短文本（≤几秒），base64 JSON 可接受（D7）。

### 4.3 声音选择覆盖（D10）

- `service_state` 增可选 `tts_voice: str | None`（随 session.save/load 同步，同批次 2 专用类型字段先例）。
- **协议扩展**：`TTSBackend.synthesize(text, *, character, emotion=None, voice_id: str | None = None)`——`voice_id` 非空时跳过 character→voice_id 映射，直接用该 voice；否则现行为（向后兼容，旧后端缺省参数）。
- **注入机制（review 修正）**：`TTSStreamManager` 增 `voice: str | None` 属性 + `set_voice(voice)`；**每 turn 由发起会话**（`resolve_session` 目标会话，而非全局活动会话——防止 A 会话改 voice 影响 B 会话 in-flight 合成）的 `service_state.tts_voice` 解析后注入；合成路径 `_download_and_forward` 调 `self._tts.synthesize(..., voice_id=self._voice)`。manager 持 tts/output/character_name（现构造不变），voice 作为 per-turn 可变状态传入。
- **设置接口**：新 WS Client→Server `set_tts_voice {voice?}`——**恒作用于活动会话**（与 §4.1/§4.2 对齐，避免"校验用激活后端、写入目标会话"的跨会话错位；多会话下前端先 `activate_session` 再设置）。流程：经活动会话激活 TTS 后端 `list_voices()` 校验该 voice 存在 → 写活动会话 `service_state.tts_voice` + save → 回 `set_tts_voice_result {ok, voice?, error?}`。
- **失败保留原选择**：校验失败/后端不可用 → 不改 state、回 `{ok: False, error}`（当前 voice 不变）。
- **删除/切换**：切换 TTS 后端不清 `tts_voice`（voice_id 是 provider 声音；前端在列表渲染时对当前 voice 不在新后端列表的情况提示，不做自动清除）。

---

## 5. 统一错误码增补（`docs/electron-backend-protocol-v2.md` §9.1）

| code | HTTP | 场景 |
|---|---|---|
| `SSRF_BLOCKED` | 400 | 自定义 endpoint 命中私网/元数据/链路本地等 SSRF 规则，或无法解析目标主机（消息统一"目标地址被拒绝或无法解析"，不泄露 host/IP） |
| `PROFILE_IN_USE` | 409 | 删除当前激活 profile |
| `PROFILE_INVALID_RUNTIME` | 400 | runtime 枚举非法，或非 backend runtime 携带 api_key |
| `PROFILE_RUNTIME_NOT_BACKEND` | 400 | 对 desktop/local 条目执行后端 test/switch（D16） |

新错误码走 `ProtocolError` 信封（D17）；`details` 含 `service`/`name`，**不含密钥与地址细节**（SSRF 拒绝消息统一"目标地址被拒绝或无法解析"，不带 host/IP——防内网拓扑泄漏）。既有参数错误（未知 service/缺 name/缺 base_url/无效请求体）维持 v1 ApiFail 不动。

---

## 6. 安全与敏感纪律

- 密钥只进 Secret Store（0600 + gitignore + 不落 DB）；API/WS 响应、错误信封、日志**永不打印密钥**（`logger` 记 `service`/`name`/布尔状态）。
- SSRF：custom backend 出站全连接校验 + test 端点 body base_url 防线；跨源重定向剥敏感头（Header 允许名单）；错误信息不暴露内网拓扑。
- 试听/列表不读聊天、不写历史；固定文本（D12）。
- Desktop/Local 类 Profile 后端不存密钥、不代理 test/switch（D4/D16）；Electron 本地密钥不在本批后端范围。
- `data/secrets/` 加入 `.gitignore`；README/安全文档声明"磁盘权限即边界、明文待 batch 7+ 加密"。

---

## 7. 测试计划

| 层 | 内容 |
|---|---|
| unit（secret_store） | get/set/delete/has/原子写/损坏文件容错/**目录 0700 + 文件 0600**（tmp 目录） |
| unit（ssrf） | 固定样例矩阵：回环/私网/链路本地/元数据/多播/保留/CGNAT/合法公网/无 scheme→https/localhost 拒绝/NXDOMAIN；`build_guarded_http_client` 重定向每跳校验 + 跨源剥头（mock transport）；测试不触网 |
| service（custom_providers） | 解析统一：secret store 优先 > api_key_env；同名 upsert 保 secret；删除清 secret；runtime 变更清理（D18） |
| service（providers_api） | 保存（backend 带 key 入 store 不入 YAML / desktop 带 key 400 / SSRF 400 / 缺 base_url 沿用 v1 ApiFail / runtime 变更清理）；删除（激活 409 / 非激活成功+清 secret）；test（body base_url SSRF 拦截 + 非 backend runtime 400） |
| service（voices/preview） | list_voices 静态/MiniMax mock 动态/custom TTS 静态集；preview 固定文本 + voice 覆盖 + 失败保留原选择；voices 会话归属=活动会话 |
| service（generation model） | Started 填充 model / Failed 带 model / Completed 必填保持；registry 已存 model 回归 |
| service（WS set_tts_voice） | 合法设置成功（活动会话）/ 未知 voice 失败不改 state / result 回显 |
| contract | 契约模型新增字段 gen_ts 漂移门禁；`make check-types` 通过 |
| 回归 | `make test-quick` 全绿；providers REST 新增键读取兼容（既有键断言保持，新增字段断言补充）；v2 WS generation 帧增量不破坏 v1 |

**契约测试更新**：`test_provider_registry.py`/`test_providers_api.py` 现有断言按新字段补（`credentialConfigured`/`runtime`/`capabilities`），**既有关键字与值语义断言不变**。

---

## 8. 文档交付

- `docs/electron-backend-progress.md`：批次 5 节（决策 + 实现 + 验收结果）。
- `docs/electron-backend-protocol-v2.md`：§9.1 增 5 错误码；`set_tts_voice`/`generation.model`（Started/Failed 补全、Completed 现状确认）/`/api/tts/voices`/`/api/tts/preview` 契约描述。
- `docs/electron-backend-security.md`：Secret Store 模型 + SSRF 防护（含 TOCTOU 取舍）+ 密钥纪律 + Header 允许名单。
- `README.md`/`docs/README.md`：批次 0-4 → 0-5；protocol-v2/security 行标批次 5。
- `paths.py` 成员表（ProjectPath 增 `DATA_SECRETS`）随变更处更新。
- `docs/14` 任务表：BE-050/051 标 ✅ 完成（验收后）。

---

## 9. 兼容性（review 修正）

- **既有键读取兼容（REST）**：`ProvidersResponse` 既有键（llm/stt/tts/custom/unconfigured、ProviderEntry.models、CustomProvider 现有字段）不删、值语义不变；新增字段（`credentialConfigured`/`runtime`/`capabilities`）因非 None 缺省**必然上 wire（D19）**——v1 前端按已知键读取不受影响，契约测试补新字段断言。**不追求字节级一致**（wire_dict `exclude_none` 只省略 None）。
- **既有 custom 条目**：无 `runtime`/`capabilities` → 默认 `backend`/`{}`；`api_key_env` 条目照常 env 解析（D3）。
- **TTS 后端协议**：`synthesize` 增可选 `voice_id` 缺省参数、`list_voices()` 新方法——均向后兼容（现有调用不变，未实现后端返回空列表）。
- **Generation 帧**：Completed.model 必填**不动**；Started.model 从"声明未填"变"填充"（v2-only 帧，v1 不收）；Failed.model 新字段缺省省略——v1/v2 现有消费不破坏。
- **providers_save 增强**：老客户端不带 `runtime`/`api_key` 的行为与现状一致（缺省 backend、无密钥条目照旧）。
- **错误信封**：老参数错误维持 ApiFail；新错误码 ProtocolError（`error`=message 别名，v1 `err.error` 仍读得到）。
