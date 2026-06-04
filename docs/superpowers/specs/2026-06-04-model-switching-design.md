# 模型切换与自定义接入

## 背景

当前 LLM/TTS/STT 的模型选择存在以下问题：

- TTS provider 硬编码 `"gpt-sovits/default"`（agent.py:90），不可配置
- STT provider 硬编码 `"sensevoice/sense-voice"`（agent.py:373），不可配置
- LLM 虽然可通过 config.yaml 配置，但 `LLMAdapter` 硬编码检查 `DEEPSEEK_API_KEY`，换其他 provider 时报错
- 无法接入任意 OpenAI 兼容的第三方服务（如硅基流动、阿里百炼等）
- 没有运行时切换模型的能力，必须改配置+重启

## 目标

1. 所有模块（LLM/TTS/STT）的 provider/model 均可通过配置文件驱动
2. 支持 `custom` provider，用户填写 base_url + api_key 即可接入任意 OpenAI 兼容服务
3. 运行时热切换模型，无需重启
4. 前端设置面板提供统一的管理界面（选择器、自定义表单、测试连接）
5. 协议层提供 `test()` 方法用于连通性检查

---

## 阶段一：配置驱动

### AppConfig 扩展

`core/app_config.py` 新增：

```python
# TTSConfig
tts_model: str = ConfigField(default="gpt-sovits/default")

# STTConfig
stt_model: str = ConfigField(default="sensevoice/sense-voice")
```

### config.yaml 对应

```yaml
tts:
  enabled: true
  model: gpt-sovits/default    # 新增

stt:
  enabled: true
  model: sensevoice/sense-voice # 新增
```

### LLMAdapter 修复

移除 `llm_adapter.py:49-52` 硬编码的 `DEEPSEEK_API_KEY` 检查。
`LLMFactory.create()` 已在 registry 中按 provider 查找对应的 env var，不需要 adapter 层重复检查。

### Agent 改动

```python
# agent.py — TTS 创建改用 config
self._tts = TTSFactory.create(config.tts.model)

# agent.py — STT 创建改用 config
stt_backend = STTFactory.create(config.stt.model)
```

---

## 阶段二：自定义接入（custom provider）

### 注册表扩展

三个模块的 `_BACKENDS` 各新增 `custom` 条目：

| 模块 | provider key | base_url | env_key |
|------|-------------|----------|---------|
| LLM | `custom` | 空（从 config 读） | `CUSTOM_LLM_API_KEY` |
| STT | `custom` | 空（从 config 读） | `CUSTOM_STT_API_KEY` |
| TTS | `custom` | 空（从 config 读） | `CUSTOM_TTS_API_KEY` |

### 配置结构

各模块 Config 新增 `api_key`、`base_url` 字段（已有则扩展）：

```python
@dataclass
class LLMConfig:
    model: str = ConfigField(default="deepseek/deepseek-chat")
    base_url: str = ConfigField(default="https://api.deepseek.com", env="LLM_BASE_URL")
    api_key: str = ConfigField(default="", secret=True, env="LLM_API_KEY")

@dataclass
class STTConfig:
    model: str = ConfigField(default="sensevoice/sense-voice")
    base_url: str = ConfigField(default="http://localhost:20042", env="STT_BASE_URL")
    api_key: str = ConfigField(default="", secret=True, env="STT_API_KEY")

@dataclass
class TTSConfig:
    model: str = ConfigField(default="gpt-sovits/default")
    base_url: str = ConfigField(default="http://localhost:9880", env="TTS_BASE_URL")
    api_key: str = ConfigField(default="", secret=True, env="TTS_API_KEY")
    voices_config: str = ConfigField(default="")
```

### Factory 改动

`LLMFactory.create("custom/Qwen/Qwen3-235B")` 时：

1. 从 Config（而非注册表）读取 `base_url` 和 `api_key`
2. 仍然创建 `OpenAICompatBackend`（custom 和 openai 走同一个类）
3. STT/TTS 同理

### custom_providers.yaml

自定义端点独立存储，与 config.yaml 分离：

```yaml
# 通过前端设置面板管理，不手动编辑
llm:
  - name: siliconflow
    base_url: https://api.siliconflow.cn/v1
    api_key_env: SILICONFLOW_API_KEY
    models:
      - Qwen/Qwen3-235B
      - deepseek-ai/DeepSeek-V3

stt:
  - name: my-whisper
    base_url: https://whisper.internal/v1
    api_key_env: WHISPER_API_KEY
    models:
      - whisper-large-v3

tts:
  - name: my-tts
    base_url: https://tts.internal/v1
    api_key_env: TTS_API_KEY
    models:
      - vits-v2
```

`api_key_env` 存环境变量名而非 key 值本身（安全）。

---

## 阶段三：运行时热切换 + 前端

### 协议层扩展：test() 方法

三个 protocol ABC 各新增抽象方法：

```python
@abstractmethod
async def test(self) -> dict:
    """测试连通性。返回 {"ok": bool, "latency_ms": int, "error": str}"""
    ...
```

实现：

| 模块 | test() 逻辑 | 超时 |
|------|-----------|------|
| LLM | `chat.completions.create`，`max_tokens=1`，测 RTT | 5s |
| STT | 发送 1 秒静音 PCM，检查 API 正常响应 | 5s |
| TTS | 合成 "测试" 二字，检查返回 bytes 非空 | 5s |

### Agent 层 switch 方法

```python
# core/agent.py
async def switch_llm(self, model_id: str) -> dict:
    """热切换 LLM。返回 {"ok": bool, "error": str}"""
```

`LLMAdapter.switch_model(new_model_id)`：
1. 关闭当前 `_backend` 客户端
2. 调用 `LLMFactory.create(new_model_id)` 创建新后端
3. 更新 `_requested_model`

TTS/STT 同理。注意：`CognitiveLoop` 持有 `self._tts` 引用，切换后需更新。

### WebSocket 消息

```json
// 前端 → 后端
{"type": "switch_model", "service": "llm", "model": "openai/gpt-4"}

// 后端 → 前端
{"type": "switch_model_result", "service": "llm", "model": "openai/gpt-4", "ok": true}
```

### REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/providers` | 返回可用 provider 列表（注册表 + custom_providers.yaml） |
| POST | `/api/providers` | 保存自定义 provider 到 custom_providers.yaml |
| DELETE | `/api/providers/{service}/{name}` | 删除自定义 provider |
| POST | `/api/providers/test` | 测试连接 `{service, provider, model, base_url, api_key}` |

### 前端设置面板

在现有 `static/js/settings.js` 面板中新增 "模型" 区块：

```
┌─────────────────────────────────────────┐
│  设置                            [✕]    │
├─────────────────────────────────────────┤
│  🔊 TTS  [■ ON]  音量 [===----]         │
│                                         │
│  ── 模型配置 ──                         │
│  LLM                                     │
│  ┌────────────────────────────────┐     │
│  │ deepseek/deepseek-v4-flash  ▾ │     │
│  └────────────────────────────────┘     │
│  预设: deepseek | openai | ollama        │
│                                         │
│  STT                                     │
│  ┌────────────────────────────────┐     │
│  │ sensevoice/sense-voice      ▾ │     │
│  └────────────────────────────────┘     │
│                                         │
│  TTS                                     │
│  ┌────────────────────────────────┐     │
│  │ gpt-sovits/default          ▾ │     │
│  └────────────────────────────────┘     │
│                                         │
│  ── 自定义端点 ──                        │
│  [+ 添加]                               │
│  ┌ siliconflow ────────────────────┐    │
│  │ base_url: api.siliconflow.cn/v1 │    │
│  │ models: Qwen3-235B, DeepSeek-V3 │    │
│  │ [测试连接] [编辑] [删除]         │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

交互逻辑：

1. 模型下拉框：选中后通过 WS 发送 `switch_model`，切换成功更新 localStorage
2. "添加自定义"：弹出表单（name、base_url、api_key_env、models 列表）
3. "测试连接"：POST `/api/providers/test`，spin 后显示 ✓ 延迟 / ✕ 错误
4. 保存写 `custom_providers.yaml`，刷新 provider 列表

### 持久化策略

| 内容 | 存储 |
|------|------|
| 当前选中 provider/model | `config.yaml`（服务端写回） |
| 自定义 provider 定义 | `custom_providers.yaml`（服务端写回） |
| 前端 UI 状态 | `localStorage` |

---

## 非目标

- OpenAI Whisper / Deepgram STT 后端实现：暂不实现，可通过 custom provider 接入
- 角色切换：后续独立设计，已写入 todo.md
- Embedding 模型切换：Milvus 向量维度与 Embedding 模型绑定，暂不做

---

## 修改范围

| 文件 | 改动 |
|------|------|
| `core/app_config.py` | TTS/STT 新增 model 字段；各 Config 新增 api_key/base_url |
| `core/agent.py` | TTS/STT 创建改用 config；新增 switch_llm/switch_stt/switch_tts |
| `core/llm_adapter.py` | 移除 DEEPSEEK_API_KEY 硬编码检查；新增 switch_model() |
| `Module/llm/protocol.py` | 新增 test() 抽象方法 |
| `Module/stt/protocol.py` | 新增 test() 抽象方法 |
| `Module/tts/protocol.py` | 新增 test() 抽象方法 |
| `Module/llm/registry.py` | 新增 custom 条目；Factory 支持从 config 读取 base_url/api_key |
| `Module/stt/registry.py` | 同上 |
| `Module/tts/registry.py` | 同上 |
| `Module/llm/config.py` | 扩展 api_key、base_url 字段 |
| `Module/stt/config.py` | 扩展 api_key、base_url 字段 |
| `Module/tts/config.py` | 扩展 api_key、base_url 字段 |
| `Module/input/server.py` | 新增 /api/providers* 端点 + switch_model WS 消息 |
| `static/js/settings.js` | 新增模型配置 UI、自定义端点管理、测试连接 |
| `static/css/chat.css` | 新增模型配置相关样式 |
| `config.yaml` | 新增 tts.model、stt.model 字段 |

## 验证

```bash
# 模块层 test() 单元测试
python -m pytest Module/llm/ -v -k test
python -m pytest Module/stt/ -v -k test
python -m pytest Module/tts/ -v -k test

# 配置加载测试
python -m pytest Module/config/ -v

# API 端点测试
python -m pytest core/tests/test_phase3.py -v

# 手动验证
# 1. 启动 agent，打开 /debug
# 2. 设置面板 → 切换 LLM 模型 → 确认切换成功
# 3. 添加自定义端点 → 测试连接 → 确认可用
# 4. 重启 agent → 确认配置持久化
```

## 实现记录

实现过程中超出原始 spec 的变更：

- **OpenAICompatTTSBackend** — custom TTS provider 走 OpenAI TTS 协议而非 GPT-SoVits 专有协议
- **TTSFactory model 验证** — `gpt-sovits` 验证 model 为 "default" 或已知 voice 名
- **ConfigLoader None 修复** — YAML `model:` 空值不覆盖默认值
- **WS capabilities 消息** — 前端获取 STT/TTS 实际可用状态，禁用不可用的控件
- **TTS 创建移至 start()** — 支持 `/restart` 后重建 TTS
- **启动横幅** — 显示实际使用的 model 名
