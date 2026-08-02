# Provider 可用性机制 — env 过滤 + 测试功能 设计

## Goal

让前端设置面板只展示**实际可配置/可用的 provider**，并提供连通性 + 可使用性测试，支持前端添加自定义模型并真正切换。

## 背景与问题

1. **两套注册表不一致**：`/api/providers`（core/input/api_routes.py）硬编码 provider 列表，工厂实际用 `Module/config/providers.yaml`（含 `env_key`/`url_key`）。两者已不同步（providers.yaml 里 stt 有 openai/deepgram，api_routes 没有；gemini 后端 NotImplemented 但被硬编码列出）。
2. **下拉框显示不可用 provider**：`openai/gpt-4o` 即使没配 `OPENAI_API_KEY` 也显示可选，选了才在 switch 时报错。
3. **测试功能残缺**：`/api/providers/test` 只支持 custom provider 的连通性测试，不支持内置 provider、不可使用性（真实产出）验证。
4. **custom 切换是坏的**：`switch_llm`/`switch_tts` 只传 model_id，`Factory.create("custom/xxx")` 缺 `base_url` 抛错 → 前端加了自定义模型也切不过去。

## 架构

```
providers.yaml (单一来源: env_key/url_key/local/models)
        │ load_provider_defaults()
        ▼
core/input/provider_registry.py   ← 新增：env 过滤 + 组装 /api/providers 响应
        │
        ▼
/api/providers (GET)  → 过滤后的 {llm,stt,tts,custom,unconfigured}
/api/providers/test (POST) → 任意 provider + level(connectivity|usable)
        │
        ▼
core/input/custom_providers.py   ← 新增 resolve_model_params：custom 参数解析（switch + test 共用）
        │
        ├──────────────┐
        ▼              ▼
switch_llm/switch_tts   /api/providers/test
   → Factory.create(model_id, **resolve_model_params(...))
```

分层约束：`Module/` 工厂不依赖 core（providers.yaml 在 Module 层被读，env 过滤逻辑放 core/input）。

## 数据模型与注册表

### providers.yaml 扩展

为每个 provider 加 `models` 列表；本地服务（内置 localhost 默认、无需配置即可用）加 `local: true`：

```yaml
llm:
  deepseek:
    url_key: "DEEPSEEK_URL"
    beta_url: ""
    beta_url_key: "DEEPSEEK_BETA_URL"
    env_key: "DEEPSEEK_API_KEY"
    supports_prefix: true
    supports_multimodal: false
    models: ["deepseek-v4-flash", "deepseek-v4-pro"]
  openai:
    url_key: "OPENAI_URL"
    env_key: "OPENAI_API_KEY"
    supports_prefix: false
    supports_multimodal: false
    models: ["gpt-4", "gpt-4o", "gpt-5"]
  ollama:
    url_key: "OLLAMA_URL"
    env_key: ""
    local: true
    models: ["qwen2.5:3b"]
  mimo:
    url_key: "MIMO_URL"
    env_key: "MIMO_API_KEY"
    supports_multimodal: true
    models: ["mimo-v2.5", "mimo-v2.5-pro"]
  gemini:
    url_key: "GEMINI_URL"
    env_key: "GEMINI_API_KEY"
    models: []            # 后端 NotImplemented → 空列表，前端自动跳过
  custom:
    url_key: ""
    env_key: "CUSTOM_LLM_API_KEY"
    models: []            # 由 custom_providers.yaml 填充

tts:
  gpt-sovits:
    url_key: "GPT_SOVITS_URL"
    env_key: ""
    local: true
    models: ["default"]
  minimax:
    url_key: "MINIMAX_TTS_URL"
    env_key: "MINIMAX_API_KEY"
    models: ["speech-2.8-hd"]
  custom:
    url_key: ""
    env_key: "CUSTOM_TTS_API_KEY"
    models: []

stt:
  sensevoice:
    url_key: "SENSEVOICE_URL"
    env_key: ""
    local: true
    models: ["sense-voice"]
  openai:
    url_key: "OPENAI_STT_URL"
    env_key: "OPENAI_API_KEY"
    models: []            # 后端 NotImplemented → 空列表
  deepgram:
    url_key: "DEEPGRAM_URL"
    env_key: "DEEPGRAM_API_KEY"
    models: []            # 后端 NotImplemented → 空列表
  custom:
    url_key: ""
    env_key: "CUSTOM_STT_API_KEY"
    models: []
```

> **NotImplemented 后端（gemini / stt-openai / stt-deepgram）直接跳过**：models 为空 → 不进入 `data[service]`、也不进入 `unconfigured` 引导（配了 env 也选不了，属死胡同）。即便 env 已配也不出现在下拉。

## env 过滤规则

新增 `core/input/provider_registry.py`，per provider 判定：

1. **跳过**：`custom`（meta-provider，不指向真实后端）与 models 为空的 provider——不进 `data[service]`、不进 `unconfigured`。
2. `local: true` → **恒显示**（无需配置即显示）。
3. 否则 `env_key` 非空且 `os.getenv(env_key)` 已设 → 已配置。
4. 否则 `url_key` 非空且 `os.getenv(url_key)` 已设 → 已配置。
5. 否则 → 未配置：进 `unconfigured[service]`，`env_vars` 列出非空的 `env_key`/`url_key` 项。

> **local: true 的运行时可用性兜底**：STT/TTS 有 `capabilities.stt/tts` → `sttAvailable`/`ttsAvailable` 门控下拉（a810bc7）。**LLM 无可用性信号**（capabilities 只有 `llm_model` 字符串）——ollama 等本地 LLM 恒显示。注意**切换是惰性的**：`LLMFactory.create` → OpenAICompatBackend 构造无网络探测，切到挂掉的本地 LLM 时 `switch_llm` 仍返回 ok:true，失败要到首条 chat 才暴露——"切换时报错兜底"不成立，**测试按钮是切换前主动验证的唯一途径**。若后续要 ollama 掉线即隐藏，需连通性探测（本次范围外）。

> **minimax 需要 URL + key 双变量**：过滤用单变量 OR 会高估（只配一个也显示）。接受此近似——不完整配置由测试按钮的 usable 检查暴露。如需精确需 per-provider `requires` 覆盖（本次不做）。

**实时判定**：过滤在每次 `/api/providers` 请求时执行（`os.getenv` 实时读、providers.yaml 模块级缓存）——用户配好环境变量后**刷新页面即生效**，无需重启服务。

## /api/providers 响应结构

```json
{
  "llm": {"deepseek": {"models": ["..."]}, "ollama": {"models": ["..."]}},
  "stt": {"sensevoice": {"models": ["sense-voice"]}},
  "tts": {"gpt-sovits": {"models": ["default"]}, "minimax": {"models": ["..."]}},
  "custom": {"llm": [{"name": "...", "base_url": "...", "models": [...]}], "stt": [], "tts": []},
  "unconfigured": {
    "llm": [{"name": "openai", "env_vars": ["OPENAI_API_KEY"]}],
    "stt": [],
    "tts": []
  }
}
```

- `data[service]`（llm/stt/tts）只含已配置 + `local: true` 的 provider（含其 models）。**不含 `custom`**——它不在 `data[service]`，始终在顶层 `custom` 键（env 过滤跳过它，永不移入 `unconfigured`）。
- `unconfigured[service]`：未配置 provider + 需要的 env var 名（**NotImplemented 且 models 为空的 provider 不在此列**）。
- `custom`：不变（custom_providers.yaml 全量）。
- **前端合并**：ModelSelector 把 `data.custom[service]` 每个条目的 `name` 合并为 `custom/<name>` 选项（替代现有 `provider === 'custom'` 分支）。

## /api/providers/test 扩展

支持任意 provider（内置 + 自定义）+ `level`：

```json
POST /api/providers/test
{
  "service": "llm",        // "llm" | "stt" | "tts"
  "provider": "deepseek",  // 内置 provider 名，或 "custom"
  "model": "deepseek-v4-flash",
  "level": "usable",       // "connectivity" | "usable"
  "base_url": "...",       // 内置可省（env 解析）；custom 必填
  "api_key": "..."         // 内置可省（env 解析）
}
```

**参数解析顺序（避免组合歧义）**：
- `provider` 有、`model` 有 → 测该模型。
- `provider` 无、`model` 有 → `provider` 视为 `"custom"`（向后兼容现有调用方 `{service, base_url, api_key, model}`）。
- `provider`、`model` 均无 → **回退到该服务当前运行/配置的模型 id（完整 `provider/model`，split 后使用，不再套用 provider→"custom" 默认）**——否则会把 `sensevoice/sense-voice` 错拼成 `custom/sensevoice/sense-voice`。
- `level` 缺省 → `"connectivity"`。

回退模型来源：LLM → `agent.llm.model`；TTS → `agent.tts.model`（实际运行后端，热切换后不读陈旧 config）；STT → `agent.config.stt.model`。

创建 backend：`Factory.create(f"{provider}/{model}", base_url=..., api_key=...)`。

**base_url/api_key 解析优先级**（请求体 > custom_providers.yaml > env）：
1. 请求体 base_url/api_key **非空** → 直接传（custom"保存前测试"场景）。
2. 请求体未传且 `provider == "custom"` → **复用 `resolve_model_params`** 从 custom_providers.yaml 解析 base_url + api_key（api_key_env → os.getenv）——否则下拉里选中的 custom provider 测试必抛 `ValueError("custom ... requires base_url")`（三个 Factory 都对缺 base_url 的 custom 抛错）。
3. 内置 provider 请求体未传 → **不传**，Factory 自行 `os.getenv` 解析。注意 `resolve_base_url` 用 `is not None`（而非 truthiness）判断，传空串 `""` 会覆盖 env 解析。

**TTS usable 测试的 `character` 参数**：请求体可选 `character` 字段；缺省时用该 provider 的默认音色（gpt-sovits: voices 配置首个，minimax: 默认 voice）。若后端抛"音色不存在"，错误信息在 `detail` 里提示。

### connectivity 级

`backend.test()` → `{ok, latency_ms, error}`。**超时 5s**（原 custom 测试无显式超时，统一加）。

### usable 级（真实产出验证）

| 服务 | 调用 | 成功标准 |
|------|------|----------|
| LLM | `backend.chat([{"role": "user", "content": "你好，请回一句话"}])` | 返回非空文本 |
| TTS | `backend.synthesize("你好，测试", character=<默认音色>)` | 返回非空音频字节 |
| STT | `backend.transcribe(内置样例.wav)` | 返回非空文本 |

**超时 30s**。usable 逻辑是**协调器层纯函数**（按 service 分派，不新增后端方法——chat/transcribe/synthesize 已存在）。

**响应**：`{ok, latency_ms, error, detail?}`。usable 级 `detail` 带产出预览（LLM 前 20 字符、TTS 字节数、STT 前 20 字符）。

**STT 样例音频**：仓库内置一个极小 WAV（~1s 短语音），放 `Module/stt/tests/fixtures/sample.wav`（供测试端点 + 单测共用）。

## custom 切换修复

新增 `core/input/custom_providers.py:resolve_model_params(service, model_id) -> dict`：

```python
def resolve_model_params(service: str, model_id: str) -> dict:
    """解析切换模型所需参数。
    "custom/name" → 查 custom_providers.yaml[name]，返回 {"base_url": ..., "api_key": ...}
    api_key 由 api_key_env 从 os.getenv 取；api_key_env 缺失/为空时跳过（不传 api_key，避免 os.getenv(None) 抛 TypeError）。
    内置 provider → 返回 {}（Factory 自行 env 解析）。
    """
```

`switch_llm` / `switch_tts` 改为：

```python
params = resolve_model_params("llm", model_id)
self._llm.switch_model(model_id, **params)  # switch_model 透传 base_url/api_key 给 Factory.create
```

`LLMAdapter.switch_model` 增加 `**kwargs` 透传（当前签名 `switch_model(self, new_model_id: str)`）。TTS 的 `switch_tts` 直接 `TTSFactory.create(model_id, **params)`。

> 范围外：STT 运行时切换不生效（`switch_stt` 只改 config，transcribe wrapper 是启动时建的）——既有 gap，已记录 todo.md，本次不修。

## 前端改动

### ModelSelector.vue

- 选项来自过滤后的 `/api/providers` 响应：`data[service]`（已配置 + 本地服务）**合并顶层 `data.custom[service]` 的 name → `custom/<name>`**（替代现有 `provider === 'custom'` 分支——该分支在 custom 移出 `data[service]` 后不再触发）。
- 下拉下方若 `unconfigured[service]` 非空，显示引导行：`还有 N 个 provider 未配置（openai → OPENAI_API_KEY）[查看配置方式]`。

### SettingsPanel.vue

- **测试按钮是下拉的兄弟行，不受 disabled 态影响**（解决 a810bc7 冲突：disabled 的下拉恰好要禁掉最需要测试的"已配置但未运行"场景）。
  - 下拉启用时：测当前选中模型。
  - 下拉禁用（服务未连接）时：**发 `{service, level}`（不带 provider/model）**，后端回退到该服务当前配置的模型——正是诊断"为什么连不上"的主场景。
  - 请求期间按钮 disabled 防重入。
- 测试按钮组：`[测试连通性] [测试可使用性]`，结果内联 `✓ 连通 (123ms)` / `✓ 可用 (LLM: "你好，我在…")` / `✗ 失败: 原因`，复用 `test-result` 样式。
- 配置入口弹窗（新，面板内）：列出该服务未配置 provider + 所需 env var，每行可复制 `export FOO=xxx`（或 `.env` 写法 `FOO=xxx`），带复制按钮；说明文字"设置环境变量后刷新页面生效"。

### 前端数据流

ModelSelector 挂载 → fetch `/api/providers` → 过滤后 options（含 custom 合并）+ unconfigured → 测试按钮 → `POST /api/providers/test {service, provider, model, level}`（disabled 态省略 provider/model）。

## 错误处理与 guardrails

- `test` 端点：未知 service/provider → 400；`Factory.create` 抛错 → `{ok:false, error}`；超时 → `{ok:false, error:"测试超时"}`。
- **超时：connectivity 5s / usable 30s。**
- LLM usable prompt 极短省 token；TTS 合成短句；STT 内置小 WAV。
- 前端测试按钮请求期间 disabled（防重入）。

## 测试策略

后端 unit/service：
- env 过滤函数：mock `os.environ`（各分支：local 恒显示 / env_key 命中 / url_key 命中 / 未配置进 unconfigured / **custom 与空 models provider 跳过**）。
- `resolve_model_params`：custom → 查 yaml + os.getenv；api_key_env 缺失不抛错；内置 → {}。
- test 端点分派：mock backend 的 `test`/`chat`/`synthesize`/`transcribe`，断言 level 分派 + 超时 + 错误路径 + **provider/model 缺省回退到配置模型（不套 custom 默认）** + **空 base_url 不覆盖 env 解析** + **custom 缺 body base_url 时经 resolve_model_params 解析**。

前端：无单测基建 → `vue-tsc --noEmit` + `npm run build` 验证。

回归：`make test-quick` 全绿。

## 范围外

- STT 运行时切换不生效（todo.md 已记录）。
- gemini / stt-openai / stt-deepgram 后端实现（models 空，前端不显示）。
- 角色切换 / 多角色（独立 spec）。
