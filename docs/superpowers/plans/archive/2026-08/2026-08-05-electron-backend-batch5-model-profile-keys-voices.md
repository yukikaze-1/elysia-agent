# 批次 5：模型 Profile 与密钥所有权 / TTS 声音（Implementation Plan）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把模型配置升级为可管理 Profile（Secret Store 密钥 + SSRF 防护 + runtime 执行位置 + TTS 声音列表/试听/覆盖），实现 BE-050/BE-051。

**Architecture:** 服务器 Secret Store（0600 文件）承接用户密钥；SSRFGuard 静态+出站校验自定义 endpoint（保存/切换/测试三入口 + guarded transport 每跳）；Profile 加 runtime/能力字段衔接批次 4；TTS 声音走 `list_voices()` 协议（静态声明 + MiniMax 动态拉取）+ 固定文本试听 + `service_state.tts_voice` 运行时覆盖（每 turn 注入）。契约扩展纯增量，v1 既有键读取兼容。

**Tech Stack:** Python 3.12 + aiohttp + httpx + openai client + Pydantic 契约。

## Global Constraints

- 绝对导入（ruff TID252，`__init__.py`/`__main__.py` 豁免）。
- v1 wire **读取兼容**：既有键不删、值语义不变；新增字段非 None 缺省必然上 wire（接受键新增，既有契约测试补断言、全等断言同步更新）。
- **API/WS/日志永不打印密钥**；Secret Store 目录 0700 + 文件 0600；`data/secrets/` 进 `.gitignore`。
- 新错误码（SSRF_BLOCKED/PROFILE_IN_USE/PROFILE_INVALID_RUNTIME/PROFILE_RUNTIME_NOT_BACKEND）走 `ProtocolError` 信封（`error`=message 别名）；既有参数错误（未知 service/缺 name/缺 base_url/无效请求体）维持 v1 ApiFail 不动（其中**缺 base_url 仅 backend runtime 触发**，desktop/local 允许空）。
- SSRF 测试只用固定样例（127.0.0.1/169.254.169.254/10.0.0.1/example.com），不触网（mock `socket.getaddrinfo`）；**`validate_endpoint` 对空 base_url 直接跳过**（内置 provider 无 body base_url 时是空串，不校验）。
- **内置 provider 守卫容错（review 修正）**：`resolve_model_params` 对内置保持现状返回 `{}`（`test_builtin_returns_empty` 断言不动）；所有守卫读 runtime 一律 `params.get("runtime", "backend")`（内置缺省 backend）。
- 时间戳一律 `now_iso()`（UTC）；注释中文；测试加 marker。

---

### Task 1: Secret Store 基建

**Files:**
- Create: `core/storage/secret_store.py`
- Modify: `paths.py`（`ProjectPath.DATA_SECRETS = "data/secrets"`）、`.gitignore`（`data/secrets/`）
- Test: `core/storage/tests/test_secret_store.py`（新建目录）

**Interfaces:**
- Produces:
  - `get_secret(service, name, path=None) -> str | None`
  - `set_secret(service, name, value, path=None) -> None`（原子写 tmp + os.replace；目录 0700 文件 0600）
  - `delete_secret(service, name, path=None) -> None`
  - `has_secret(service, name, path=None) -> bool`
  - `all_configured(service, path=None) -> set[str]`
  - 默认路径 `ProjectPath.DATA_SECRETS.resolve() / "profiles.json"`；**所有函数都接受 `path`（文件路径或目录均可，测试注入 tmp）**
- Consumes: `ProjectPath`（本 Task 新增）

- [ ] **Step 1: RED（unit）** — `test_secret_store.py`（tmp_path 注入 `path`）：
  - `set→get` round-trip；`set 同名覆盖`；`delete 后 get None`；`has` 三态；`all_configured` 只返回有密钥的 name；
  - **权限**：set 后目录 mode & 0o777 == 0o700、文件 mode & 0o777 == 0o600；
  - **原子写**：set 后旧值被替换、无 tmp 残留文件；
  - **损坏容错**：预写非法 JSON → 返回空 dict（不抛）+ `logger.warning`（mock 断言）；
  - 不存在的文件 → 空 dict。

- [ ] **Step 2: 验证 RED** — `pytest core/storage/tests/test_secret_store.py -v` FAIL（模块不存在）。

- [ ] **Step 3: 实现** — `paths.py` 加 `DATA_SECRETS = "data/secrets"`；`.gitignore` 加 `data/secrets/`；`secret_store.py` 按 Interfaces（`_DEFAULT_DIR`、`os.makedirs(mode=0o700, exist_ok=True)`、tmp+`os.replace`、写前 chmod 文件 0o600）。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(storage): 批次 5——Secret Store（0600 文件 + 原子写 + 损坏容错，永不读回）`

---

### Task 2: SSRFGuard 模块

**Files:**
- Create: `core/input/ssrf.py`
- Test: `core/input/tests/test_ssrf.py`

**Interfaces:**
- Produces:
  - `class SSRFError(ValueError)`（`message` 统一「目标地址被拒绝或无法解析」，不泄露 host/IP）
  - `validate_endpoint(url: str) -> None`（非法 → 抛 `SSRFError`；scheme 缺省视为 https；字面量黑名单 + getaddrinfo 解析双保险；**空串/空白 → 直接放行**——调用方仅对非空 base_url 校验，内置无 body base_url 不误伤）
  - `build_guarded_http_client(base_url: str | None = None, headers: dict | None = None, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient`（校验传输层 + 跨源剥头 + follow_redirects=True）
  - 私有：`_is_blocked_ip(ip)`（ipaddress 全地址族）、`_BLOCKED_HOSTS`（169.254.169.254/metadata.google.internal/100.100.100.200/metadata）、`_ALLOWED_HEADERS` = {Authorization, Content-Type, Accept, User-Agent}
- Consumes: 无

- [ ] **Step 1: RED（unit）** — `test_ssrf.py`（固定样例，mock `socket.getaddrinfo`）：
  - 拒绝：`http://127.0.0.1`、`http://10.0.0.1`、`http://192.168.1.1`、`http://172.16.0.1`、`http://169.254.169.254`、`http://[::1]`、`http://fe80::1`、`http://0.0.0.0`、`http://255.255.255.255`、`http://224.0.0.1`、`http://100.64.0.1`、无 scheme `169.254.169.254`、host=metadata 字面量；
  - 通过：`https://example.com`（解析公网）、无 scheme `example.com` → https、`http://example.com`（显式 http 公网）、**空串 `""` 与空白 → 放行**；
  - **NXDOMAIN**：getaddrinfo 抛 `socket.gaierror` → `SSRFError`（消息含「无法解析」）；
  - `build_guarded_http_client`：transport 是校验子类；**跨源剥头**（mock transport：带 `X-API-Key` 的请求跨 host → 该头被剥、Authorization 同剥；同 host 保留）；**每跳校验**（拦截到内网 IP 的请求 → SSRFError）。

- [ ] **Step 2: 验证 RED** — FAIL。

- [ ] **Step 3: 实现** — `ssrf.py`：`validate_endpoint`（urllib.parse → scheme 缺省 https → host 字面量黑名单 → `socket.getaddrinfo` → 逐 IP `ipaddress.ip_address` 判类 → 抛 SSRFError）；`_SSRFTransport(httpx.AsyncHTTPTransport)` 覆写 `handle_async_request`：解析目标 host 校验 → 跨源剥非允许名单头 → `super().handle_async_request`。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(input): 批次 5——SSRFGuard（validate_endpoint + guarded transport 每跳 + 跨源剥头）`

---

### Task 3: 契约层（rest.py + ws_message.py + gen_ts）

**Files:**
- Modify: `frontend_contract/rest.py`、`frontend_contract/ws_message.py`
- Test: `frontend_contract/tests/`

**Interfaces:**
- Consumes: 无（独立契约层）。
- Produces（rest.py）：
  - `ProviderEntry` + `credentialConfigured: bool = False`、`runtime: str = "backend"`、`capabilities: dict[str, Any] = {}`
  - `CustomProvider` + `runtime: str = "backend"`、`credentialConfigured: bool = False`、`capabilities: dict[str, Any] = {}`
  - 新 `TtsVoiceEntry{id, name?, languages, capabilities}`、`TtsVoicesResponse{voices, current?, source, error?}`、`TtsPreviewResult{ok, mime?, duration_ms?, audio_base64?, error?}`
- Produces（ws_message.py）：
  - Client→Server `SetTtsVoiceMessage{type: "set_tts_voice", voice: str}`（加入 Client 判别联合）
  - Server→Client `SetTtsVoiceResultMessage{type: "set_tts_voice_result", ok: bool, voice?: str, error?: str}`（**加入 ServerMessage 判别联合**——与 switch_model_result/service_mode_result 同列，否则 gen_ts 不生成 TS 判别）
  - `GenerationFailedMessage` + `model: Optional[str] = None`

- [ ] **Step 1: RED（unit）** — `frontend_contract/tests/`：
  - 新字段默认值上 wire：`ProviderEntry(models=["a"]).wire_dict()` 含 `credentialConfigured/runtime/capabilities`；`CustomProvider(name="x", base_url="")` 同；
  - `TtsVoicesResponse(voices=[TtsVoiceEntry(id="v1")])` round-trip；
  - `SetTtsVoiceMessage`/`SetTtsVoiceResultMessage` round-trip；`SetTtsVoiceResultMessage` 是 `ServerMessage` 联合成员（`ServerMessage.model_validate({...})` 可判别）；
  - `GenerationFailedMessage` 带 model round-trip、缺省 model 省略；
  - 既有 `ProviderTestResult`/`ProvidersResponse` round-trip 不破坏。

- [ ] **Step 2: 验证 RED** — `pytest frontend_contract/tests/ -v` FAIL。

- [ ] **Step 3: 实现** — 按 Produces 改两文件；`SetTtsVoiceMessage` 加 Client 联合、`SetTtsVoiceResultMessage` 加 Server 联合。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: gen_ts** — `make gen-types` 重生成；`python -m frontend_contract --check` 通过；`git diff contract.generated.ts` 确认**未修改类型字节不变**（新增字段/类型为增量，修改过字段的类型字节必然变，spec §9 已接受）。

- [ ] **Step 6: Commit** — `feat(contract): 批次 5 契约——providers 新字段 + tts voices/preview + set_tts_voice + generation.failed.model`

---

### Task 4: custom_providers 解析统一 + runtime/能力字段 + D18 清理 + secret_path 穿线

**Files:**
- Modify: `core/input/custom_providers.py`
- Test: `core/input/tests/test_custom_providers.py`（**更新 3 个既有 `assert params == {...}` 全等断言为含 runtime 键**，见 Step 2）

**Interfaces:**
- Consumes: Task 1 `secret_store`（经 `secret_path` 注入，不依赖全局默认目录）。
- Produces（**所有函数新增 `secret_path: Path | None = None` 参数**，测试注入 tmp、生产 None 用默认）：
  - `add_provider(service, entry, path=None, secret_path=None)`：条目支持 `runtime`/`capabilities`（缺省 backend/{}）；`api_key` 非空 → `secret_store.set_secret(service, name, api_key, path=secret_path)`（**不入 YAML**）；`api_key` 空 + 同名 → 保留 secret；runtime backend→desktop/local → `delete_secret`（D18）
  - `remove_provider(service, name, path=None, secret_path=None)`：删条目 + `delete_secret`
  - `resolve_model_params(service, model_id, secret_path=None) -> dict`：**custom 条目**返回 `{base_url, api_key?, runtime}`（api_key = `get_secret` 直存 > api_key_env env，D3）；**内置条目保持现状返回 `{}`**（不破坏 `test_builtin_returns_empty`；守卫用 `.get("runtime", "backend")` 容错）

- [ ] **Step 1: RED（service）** — `test_custom_providers.py`（tmp_path 注入 `path` + `secret_path`，**不碰真实 data/secrets/**）：
  - `add_provider` 带 `api_key` → YAML 条目**无** api_key 字段、`secret_store.get_secret(..., path=secret_path)` 有值；
  - 同名 upsert 不带 api_key → 原 secret 保留；
  - `resolve_model_params("llm", "custom/x", secret_path=...)`：secret 存在 → 返回该 key；secret 无 + api_key_env 设 env → env key；两者都无 → 无 api_key 键；**返回含 `runtime`**；
  - runtime 变更：add backend→desktop（带 secret）→ secret 被清（D18）；
  - `remove_provider` → 条目删 + secret 删；
  - **既有 3 个全等断言更新**：`:31`/`:49`/`:66` `assert params == {...}` → 补 `"runtime"` 键（条目 runtime 缺省 backend）。

- [ ] **Step 2: 验证 RED** — FAIL（含既有全等断言缺 runtime 键失败）。

- [ ] **Step 3: 实现** — 按 Produces 改 `custom_providers.py`；`load/save` 透传新增键；`resolve_model_params` 集成 `secret_store` + 返回 runtime + `secret_path` 穿线。

- [ ] **Step 4: 验证 GREEN** — `pytest core/input/tests/test_custom_providers.py -v` PASS。

- [ ] **Step 5: Commit** — `feat(input): 批次 5——custom_providers 解析统一（secret store 优先）+ runtime/能力 + D18 清理 + secret_path 穿线`

---

### Task 5: 出站守卫注入（custom backend http_client）

**Files:**
- Modify: `Module/llm/openai_compat.py`、`Module/stt/openai_stt.py`、`Module/tts/openai_tts.py`、`Module/llm/registry.py`、`Module/stt/registry.py`、`Module/tts/registry.py`
- Test: `Module/llm/tests/`、`Module/stt/tests/`、`Module/tts/tests/`

**Interfaces:**
- Consumes: Task 2 `build_guarded_http_client`、Task 4 `resolve_model_params`（runtime）。
- Produces:
  - `OpenAICompatBackend.__init__(..., http_client: httpx.AsyncClient | None = None)`——**存实例** `self._injected_client`，`_create_clients()` 中非 None 时 `AsyncOpenAI(http_client=self._injected_client, ...)`；**`reconnect()` 复用同一注入 client**（不丢失守卫，review 修正）
  - `OpenAICompatSTTBackend`/`OpenAICompatTTSBackend.__init__(..., http_client=None)` 同理（openai_tts 的 httpx.AsyncClient 直连路径复用注入 client）
  - Factory `create()` 透传 `http_client` 给后端构造函数

- [ ] **Step 1: RED（unit/service）** — `Module/{llm,stt,tts}/tests/`：
  - 传 `http_client=<mock>` → 后端客户端使用该 client；不传 → 自建（现状不破坏）；
  - **reconnect 保留**：构造后调 `reconnect()`（或 `_create_clients` 二次）→ 注入 client 仍被使用（mock 断言客户端数/引用不变）；
  - Factory `create("custom/x", base_url=..., api_key=..., http_client=guard)` → 后端收到 http_client。

- [ ] **Step 2: 验证 RED** — FAIL（参数不存在）。

- [ ] **Step 3: 实现** — 三后端构造函数加可选 `http_client` + 存实例复用；Factory 透传未识别 kwargs。

- [ ] **Step 4: 验证 GREEN** — PASS（三模块 test 全绿）。

- [ ] **Step 5: Commit** — `feat(backends): 批次 5——LLM/STT/TTS 后端 http_client 注入（custom 出站守卫挂载点 + reconnect 保留）`

---

### Task 6: providers REST handlers（save/delete/test + 错误信封双轨）

**Files:**
- Modify: `core/input/api_routes.py`、`core/input/provider_registry.py`
- Test: `core/input/tests/test_providers_api.py`（**更新既有 `test_test_endpoint_custom_resolves_from_yaml`**，见 Step 2）、`core/input/tests/test_provider_registry.py`

**Interfaces:**
- Consumes: Task 1-5（secret_store/SSRF/custom_providers/backend 注入）+ Task 3 契约。
- Produces：
  - `providers_save_handler`：`runtime` 解析（非法 → 400 PROFILE_INVALID_RUNTIME）；**base_url 门控按 runtime**——backend 缺 → v1 ApiFail（既有检查）、desktop/local 缺 → 放行；backend → `validate_endpoint`；非 backend 带 api_key → 400 PROFILE_INVALID_RUNTIME；api_key → secret store（经 `secret_path=None` 生产默认）；D18；响应 credentialConfigured
  - `providers_delete_handler`：激活判定（活动会话 service_state / config / vision）→ 409 PROFILE_IN_USE；非激活 → remove + secret 清理
  - `providers_test_handler`：body `base_url` **非空时无论 provider 一律 `validate_endpoint`**（含 NXDOMAIN → 400 SSRF_BLOCKED；空 base_url 跳过，内置无 body base_url 不破坏既有 `test_test_endpoint_builtin_connectivity`）；`resolve_model_params` 返回 `runtime = params.get("runtime", "backend")` != backend → 400 PROFILE_RUNTIME_NOT_BACKEND；custom+backend → `build_guarded_http_client` 作 `http_client` 传 Factory
  - `build_providers_response(custom_data, secret_path=None)`：ProviderEntry 带 `credentialConfigured`/`runtime`/`capabilities`；custom 条目 enrich `credentialConfigured`（`all_configured(service, path=secret_path)` ∪ env）

- [ ] **Step 1: RED（service）** — `test_providers_api.py` 新增（mock `socket.getaddrinfo`；**secret 隔离**：`mock.patch.object(secret_store, "_DEFAULT_DIR", tmp_path)`——仿现有 `patch.object(custom_providers, "_DEFAULT_PATH", ...)` 模式，handler 走默认目录但被测目录是 tmp，**不碰真实 data/secrets/**）：
  - 保存 backend 带 api_key → 200 + 该条目 `credentialConfigured: True` + 响应/存储无密钥 + `secret_store.get_secret(service, name)`（patch 后默认目录）有值；
  - 保存 desktop + api_key → 400 `PROFILE_INVALID_RUNTIME`（ProtocolError 信封）；
  - 保存 desktop 缺 base_url → 200（放行，D13）；
  - 保存 backend base_url=127.0.0.1 → 400 `SSRF_BLOCKED`（ProtocolError）；
  - 保存缺 base_url（无 runtime → backend）→ 400 v1 ApiFail（`{ok:false,error}`）；
  - 删除激活（service_state 指向 custom/x）→ 409 `PROFILE_IN_USE`；非激活 → 200 + secret 清理；
  - test：body base_url=127.0.0.1 → 400 SSRF_BLOCKED（**无论 provider**）；desktop 条目 → 400 PROFILE_RUNTIME_NOT_BACKEND；内置 provider **无 body base_url** → 既有 connectivity 测试保持（空串跳过校验）；
  - registry：内置条目含新字段；custom 条目含新字段（secret_path 注入）。

- [ ] **Step 2: 验证 RED** — FAIL + **既有 `test_test_endpoint_custom_resolves_from_yaml` 更新**：原用 `base_url: http://myllm.local`（不可解析 host）→ 新 SSRF 逻辑会 NXDOMAIN 400；改为 mock getaddrinfo 返回公网 IP 或改固定样例 URL（spec 要求固定最小样例），断言同步调（custom backend 经 guarded client）。**另补**：本地内置 provider（gpt-sovits `localhost:9880`）经 test 带 body base_url 被 SSRF 拦截的用例（spec 授权，plan 显式声明）。

- [ ] **Step 3: 实现** — 三 handler + `build_providers_response` 按 Produces；错误信封：新码 `ProtocolError`（`error=message`），老参数错误原样 ApiFail。

- [ ] **Step 4: 验证 GREEN** — `pytest core/input/tests/test_providers_api.py core/input/tests/test_provider_registry.py -v` PASS（既有断言保留、受影响断言按 Step 2 更新）。

- [ ] **Step 5: Commit** — `feat(input): 批次 5——providers REST（保存密钥入 Store + 删除激活约束 + test SSRF/runtime 守卫 + base_url 门控按 runtime + 信封双轨）`

---

### Task 7: switch 路径守卫（runtime + SSRF + 出站注入）

**Files:**
- Modify: `core/agent/server.py`（`switch_llm`/`switch_stt`/`switch_tts`）、`core/adapter/llm_adapter.py`（`switch_model` 若需透传 http_client）
- Test: `core/agent/tests/`（或 `core/input/tests/test_v2_ws.py` 的 switch_model 路径）

**Interfaces:**
- Consumes: Task 2 `validate_endpoint`/`build_guarded_http_client`、Task 4 `resolve_model_params`（runtime）、Task 5 后端注入。
- Produces：
  - 三个 switch 方法：`params = resolve_model_params(svc, model_id)`；`runtime = params.get("runtime", "backend")`（内置返回 `{}` 缺省 backend，不 KeyError）`!= "backend"` → 返回 `{"ok": False, "error": "PROFILE_RUNTIME_NOT_BACKEND: 该 Profile 由 Electron 执行"}`（WS switch_model_result 带 error）；custom+backend → `base_url 非空时 validate_endpoint(base_url)` 失败 → `{"ok": False, "error": ...}`；`build_guarded_http_client(base_url)` 作 `http_client` 传 Factory.create（内置不含 http_client，走现状）

- [ ] **Step 1: RED（service）** — `test_v2_ws.py` 或 `core/agent/tests/`：
  - `switch_llm("custom/desktop_prof")`（resolve 返回 runtime=desktop）→ result ok=False + error 含 PROFILE_RUNTIME_NOT_BACKEND；service_state 未改；
  - `switch_llm("custom/bad")`（base_url=127.0.0.1，mock 解析）→ ok=False（SSRF）；
  - `switch_llm("custom/good")`（公网）→ Factory.create 收到 `http_client`（mock Factory 断言）+ service_state 更新 + broadcast_capabilities 调；
  - **内置 switch 保持**：`resolve_model_params` 返回 `{}`（mock）→ `runtime` 缺省 backend、无 base_url 校验、Factory 无 http_client → ok=True（既有 `test_switch_stt_writes_service_state_and_ok` 保持）；
  - **出站守卫生效**：mock 后端的构造断言注入的 client 是 guarded 类型。

- [ ] **Step 2: 验证 RED** — FAIL。

- [ ] **Step 3: 实现** — 三 switch 方法按 Produces；`llm_adapter.switch_model` 若需把 http_client 传给 `LLMFactory.create` 则加。

- [ ] **Step 4: 验证 GREEN** — PASS（既有 switch 成功/失败路径测试保持）。

- [ ] **Step 5: Commit** — `feat(agent): 批次 5——switch 路径守卫（runtime 拦截 + SSRF 校验 + 出站注入）`

---

### Task 8: Generation model 捕获（D8）

**Files:**
- Modify: `core/cognitive/generation.py`（`GenerationContext.model_id`）、`core/cognitive/loop.py`（构造 ctx 传 model_id；turn_started/turn_completed 传 model_id）、`Module/output/manager.py`（turn_started 用 ctx.model_id）
- Test: `Module/output/tests/`、`core/cognitive/tests/`（如存在）

**Interfaces:**
- Consumes: Task 3 契约（Failed.model，若 Task 3 未加则补）。
- Produces:
  - `GenerationContext.model_id: str`——`loop.py` turn 启动构造 ctx 时传 `self._llm.model`（**捕获快照**，替代 completed 即时读取）
  - `turn_started` 填 `model=ctx.model_id`（Started.model）
  - failed 帧带 `model=ctx.model_id`
  - `GenerationRegistry` **零改动**（已存 model，回归确认）

- [ ] **Step 1: RED（service）** —
  - `loop.py` turn 流程：`turn_started` → v2 收到 `generation.started` 且 `model == self._llm.model`（fake channel 记录）；`turn_completed` → completed.model 与 Started 一致（同一次快照，非即时读取）；
  - failed 事件 → `generation.failed` 帧含 `model`；
  - completed 帧 model 必填保持（既有断言不破坏）。

- [ ] **Step 2: 验证 RED** — FAIL（Started 无 model / loop 未传）。

- [ ] **Step 3: 实现** — `GenerationContext` 加 `model_id`（缺省 ""）；`loop.py` 构造处传 `self._llm.model`；`turn_started`/failed 广播用 `ctx.model_id`。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(cognitive): 批次 5——generation.model 快照捕获（loop 传 model_id + Started 填充 + Failed 补字段）`

---

### Task 9: TTS 协议扩展（list_voices + synthesize voice_id）+ 声音列表路由

**Files:**
- Modify: `Module/tts/protocol.py`、`Module/tts/minimax_tts.py`、`Module/tts/gpt_sovits.py`、`Module/tts/openai_tts.py`、`core/input/api_routes.py`
- Test: `Module/tts/tests/`、`core/input/tests/test_tts_voices.py`（新建）

**Interfaces:**
- Consumes: Task 3 `TtsVoiceEntry/TtsVoicesResponse`、Task 6 活动会话 profile 解析。
- Produces:
  - `TTSBackend.list_voices() -> list[dict]`（协议默认 `[]`；条目 `{id, name?, languages?, capabilities?}`）
  - `TTSBackend.synthesize(..., voice_id: str | None = None)`（协议签名加可选参数，**本 Task 三后端全部实现**——Task 10 试听/覆盖直接可用）
  - `MiniMaxTTSBackend.list_voices()`：`POST /v1/get_voice {voice_type:"system"}` 解析 `data.voice_items`（`{voice_id, voice_name}`）→ 列表；失败 → `[]`；实例内进程缓存（惰性，重复调不重复 POST）。`synthesize` 的 `voice_id` 非空 → payload `voice_setting.voice_id = voice_id`（跳过 `_resolve_voice`）
  - `GPTSoVITSBackend.list_voices()`：静态集 = **`self._voices`（已加载 `gpt_sovits_voices.yaml`）的 keys**（与可合成集锚定，防 voice_id 查 `_call_api` 抛 KeyError）；`synthesize` 的 `voice_id` 非空 → 直接按 voice_id 查 `_voices`（跳过 character 默认）
  - `OpenAICompatTTSBackend.list_voices()`：静态集 `alloy/echo/fable/onyx/nova/shimmer`；`synthesize` 的 `voice_id` 非空 → **流进 `synthesize_stream` 的 `voice` 参数**（review 修正：`synthesize` 委托 `synthesize_stream`，`voice=character` 处改 `voice=voice_id or character`）
  - 共享 helper `_active_tts_profile(agent) -> TTSBackend | None`（活动会话激活 TTS 后端；未启用返回 None，Task 9/10 共用防分叉）
  - 路由 `GET /api/tts/voices`：helper → None → `{voices: [], current: None, source: "static", error: "TTS 未启用"}`（200）；否则 `list_voices()`；`current` = 活动会话 `service_state.tts_voice` 或后端默认

- [ ] **Step 1: RED（service）** — `Module/tts/tests/`：
  - 协议缺省 `list_voices()` == `[]`；
  - MiniMax：mock POST 返回 `{data: {voice_items: [{voice_id:"v1", voice_name:"音色一"}]}}` → `[{id:"v1", name:"音色一"}]`；二次调用不重复 POST（缓存）；API 错 → `[]`；`synthesize(character="Elysia", voice_id="v2")` → payload voice_id=="v2"（跳过 map）；None → 走 map（现状）；
  - gpt-sovits：`list_voices()` 的 id 集 == `_voices` keys；`synthesize(voice_id="v2")` → `_call_api` 用 voice_id 查（mock）；
  - openai_tts：静态集非空 id 唯一；`synthesize(voice_id="echo")` → stream 的 voice=="echo"；
  - `test_tts_voices.py`：`GET /api/tts/voices` → 200 含 voices；TTS 未启用 → 200 空 + error；`current` 随 service_state.tts_voice。

- [ ] **Step 2: 验证 RED** — FAIL。

- [ ] **Step 3: 实现** — 按 Produces；helper 放 `api_routes.py` 或 `core/input/tts_profile.py`（小模块，避免路由文件膨胀）。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(tts): 批次 5——TTS 协议（list_voices + synthesize voice_id）+ 三后端实现 + GET /api/tts/voices`

---

### Task 10: TTS 试听（POST /api/tts/preview）

**Files:**
- Modify: `core/input/api_routes.py`
- Test: `core/input/tests/test_tts_voices.py`

**Interfaces:**
- Consumes: Task 3 `TtsPreviewResult`、Task 9 `_active_tts_profile` + `synthesize(voice_id)`。
- Produces:
  - `POST /api/tts/preview` body `{voice_id?: str}`：模块级 `_PREVIEW_TEXT = "你好，我是声音测试。"` → `_active_tts_profile` → None → `{ok: False, error: "TTS 未启用"}`；否则 `synthesize(_PREVIEW_TEXT, character=<角色>, voice_id=voice_id)` → `{ok, mime, audio_base64}`；异常 → `{ok: False, error}`

- [ ] **Step 1: RED（service）** — `test_tts_voices.py`：
  - mock 激活后端 synthesize 返回 bytes → 200 `{ok: True, mime, audio_base64}`；断言 synthesize 收到固定文本（非聊天）与 voice_id；
  - voice_id 缺省 → synthesize 不带 voice_id；
  - 后端抛错 → `{ok: False, error}`；TTS 未启用 → `{ok: False, error: "TTS 未启用"}`。

- [ ] **Step 2: 验证 RED** — FAIL。

- [ ] **Step 3: 实现** — 路由 + 固定文本常量 + `_active_tts_profile`。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(input): 批次 5——POST /api/tts/preview（固定短文本试听，不读聊天）`

---

### Task 11: 声音覆盖（service_state.tts_voice + TTSStreamManager + WS set_tts_voice + 每 turn 注入）

**Files:**
- Modify: `core/schema/service_state.py`（`tts_voice: str | None = None`——**字段定义处**；session.py save/load 已泛化遍历 `model_fields` 自动同步，无需改）、`core/adapter/tts_adapter.py`、`core/cognitive/loop.py`（每 turn 注入点）、`core/input/ws_handler.py`（set_tts_voice 分支）
- Test: `core/input/tests/test_v2_ws.py`、`core/cognitive/tests/`、`Module/tts/tests/`

**Interfaces:**
- Consumes: Task 3 `SetTtsVoiceMessage`/`SetTtsVoiceResultMessage`、Task 9 后端（list_voices 校验 + synthesize voice_id）。
- Produces:
  - `ServiceState.tts_voice: str | None = None`（save/load 经现泛化逻辑自动 round-trip）
  - `TTSStreamManager.voice: str | None` 属性 + `set_voice(v)`；`_download_and_forward`（L312/L418 两处）调 `synthesize(..., voice_id=self._voice)`
  - **每 turn 注入（review 修正，核心接线）**：`core/cognitive/loop.py` 在 turn 启动（TTS 合成开始前）解析**发起会话**的 `service_state.tts_voice` → `tts_manager.set_voice(voice)`；发起会话 = `resolve_session` 目标会话（非全局活动会话）
  - WS `set_tts_voice`：经 `_active_tts_profile`（活动会话）`list_voices()` 校验 voice 存在 → 写活动会话 `service_state.tts_voice` + save → 回 `set_tts_voice_result {ok: True, voice}`；未知 voice/后端不可用 → `{ok: False, error}` 不改 state

- [ ] **Step 1: RED（service）** —
  - `ServiceState` round-trip：save/load 保留 `tts_voice`（SessionMemory 泛化字段遍历断言）；
  - MiniMax/gpt-sovits/openai_tts `synthesize(voice_id=...)` 已由 Task 9 覆盖（回归）；
  - TTSStreamManager：`set_voice("v2")` 后合成 → 后端 synthesize 收到 `voice_id="v2"`；未 set → 无 voice_id；
  - **每 turn 注入**：fake 会话 A service_state.tts_voice="vA"、活动会话 B="vB"；发起会话 A 的 turn → manager.set_voice("vA")（loop 接线断言）；
  - WS：`set_tts_voice {voice:"v1"}`（mock 后端 list_voices 含 v1）→ `set_tts_voice_result {ok: True, voice:"v1"}` + 活动会话 `tts_voice=="v1"`；`{voice:"nope"}` → `{ok: False, error}` + state 不变；TTS 未启用 → `{ok: False, error}`。

- [ ] **Step 2: 验证 RED** — FAIL。

- [ ] **Step 3: 实现** — 按 Produces；`service_state.py` 加字段；`tts_adapter.py` 加 voice；`loop.py` 每 turn set_voice；`ws_handler.py` 加 `msg_type == "set_tts_voice"` 分支。

- [ ] **Step 4: 验证 GREEN** — PASS。

- [ ] **Step 5: Commit** — `feat(tts): 批次 5——声音覆盖（service_state.tts_voice + TTSStreamManager voice 每 turn 注入 + WS set_tts_voice，失败保留原选择）`

---

### Task 12: 错误码目录 + 集成收口 + 文档

**Files:**
- Modify: `docs/electron-backend-protocol-v2.md`（§9.1 + 新端点/消息契约）、`docs/electron-backend-security.md`、`docs/electron-backend-progress.md`（批次 5 节）、`README.md`、`docs/README.md`、`docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-050/051 标 ✅，验收后）、`paths.py` 成员表注释
- Test: 回归

**Interfaces:**
- Consumes: Task 1-11 全部。
- Produces:
  - 错误码目录 §9.1：+`SSRF_BLOCKED`/`PROFILE_IN_USE`/`PROFILE_INVALID_RUNTIME`/`PROFILE_RUNTIME_NOT_BACKEND`（4 码；PROFILE_MISSING_BASE_URL 不设）
  - 文档：progress 批次 5（决策表 + 实现 + 测试数）、security（Secret Store + SSRF + Header 允许名单 + TOCTOU 取舍 + 密钥纪律）、README/docs 批次 0-5、docs/14 BE-050/051 ✅

- [ ] **Step 1: 集成验证** — `make test-quick` 全绿（Task 1-11 后）；`make check-types` 通过；`python -m frontend_contract --check` 无漂移。
- [ ] **Step 2: 文档** — 按 Produces 更新；错误码表加 4 码；`paths.py` 注释同步 `DATA_SECRETS`；docs/14 标记（验收子 agent 通过后）。
- [ ] **Step 3: Commit** — `docs: 批次 5——错误码目录 + 安全/进度/README/paths/任务清单文档收口`
