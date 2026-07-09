# Pre-flight 环境预检

## 动机

当前首次部署体验差：

1. `.env` 不存在时静默使用默认值，用户直到 API 报 401 才发现没配 key
2. TTS/STT/DB 服务不可达时静默降级，排查困难
3. 缺少依赖检查——pip 包缺失只能靠 import 报错发现
4. `.env.example` 含废弃项（`DEEPSEEK_API_BASE`、`DEEPSEEK_API_BETA`），代码从不读取

目标：`app.py --check` 一键预检，首次部署时快速发现配置问题。

## 设计

### 触发方式

```
python app.py --check          # 快速预检
python app.py --check --full   # 含连通性探活
```

`--check` 执行预检后**始终 exit**（不启动应用）。`--server`/`--tts`/`--stt` 等 flag 只影响预检用的配置（CLI 覆盖 YAML）。如需"预检通过后启动"：

```bash
python app.py --check && python app.py --server
```

### 执行位置

```
load_dotenv() → parse args → ConfigLoader.load() → AppConfig
    │
    ├── --check? → preflight(config, full=--full) → exit(0|1)
    │
    └── 无 --check → setup_logging() → Agent(config) → start()
```

`ConfigLoader.load()` 之后运行，拥有完整 `AppConfig`，能根据实际配置的 provider 精准检查。

### 预检项目

#### 默认（`--check`）

| # | 检查 | 级别 | 方法 |
|---|------|------|------|
| 1 | Python >= 3.11 | FAIL | `sys.version_info` |
| 2 | `.env` 存在 | FAIL | `Path(".env").exists()`，不存在则打印 `请先运行: cp .env.example .env，然后编辑填入 API key` |
| 3 | 根据 LLM provider 检查对应 env var | FAIL | 从 `config.llm.model` 解析 provider（如 `deepseek/deepseek-v4-flash` → `deepseek`），按表检查对应 env var。ollama 跳过（无需 API key）。支持 provider 清单见下方 |
| 4 | 关键 pip 包可 import | FAIL | `aiohttp, httpx, openai, pydantic, jinja2, yaml, dotenv`，列出缺失的包 |
| 5 | character 文件存在 | FAIL | 读 `config.agent.character`，检查路径 |
| 6 | 根据 TTS provider 检查对应 env var | WARN | 按表检查：minimax→`MINIMAX_API_KEY`，gpt-sovits→`GPT_SOVITS_URL`，custom→`CUSTOM_TTS_API_KEY` |

**检查 #6 的 TTS provider 映射表**（从 `config.tts.model` 解析 provider）：

| Provider | 检查的 env var | 备注 |
|----------|---------------|------|
| minimax | `MINIMAX_API_KEY` | 云端，当前默认 |
| gpt-sovits | `GPT_SOVITS_URL` | 本地部署 |
| custom | `CUSTOM_TTS_API_KEY` | OpenAI 兼容协议，需用户自行配置 endpoint |
| 7 | 根据 STT provider 检查对应 env var | WARN | 按表检查：sensevoice→`STT_API_URL`，custom→`CUSTOM_STT_API_KEY`，openai→`OPENAI_API_KEY`（暂未实现），deepgram→`DEEPGRAM_API_KEY`（暂未实现） |

**检查 #7 的 STT provider 映射表**（从 `config.stt.model` 解析 provider）：

| Provider | 检查的 env var | 备注 |
|----------|---------------|------|
| sensevoice | `STT_API_URL` | 默认 STT，本地部署 |
| custom | `CUSTOM_STT_API_KEY` | 需用户自行配置 endpoint |
| openai | `OPENAI_API_KEY` | 暂未实现（factory 抛出 NotImplementedError） |
| deepgram | `DEEPGRAM_API_KEY` | 暂未实现（factory 抛出 NotImplementedError） |
| 8 | `log/`、`data/` 目录可写 | WARN | `Path.mkdir(exist_ok=True)` + `touch` 测试 |

**检查 #3 的 LLM provider 映射表**（从 `config.llm.model` 的 `provider/model` 解析 provider 部分）：

| Provider | 检查的 env var | 备注 |
|----------|---------------|------|
| deepseek | `DEEPSEEK_API_KEY` | 默认 LLM |
| openai | `OPENAI_API_KEY` | |
| ollama | — | 本地部署，跳过 |
| custom | `CUSTOM_LLM_API_KEY` | 需用户自行配置 endpoint |
| mimo | `MIMO_API_KEY` | |
| gemini | `GEMINI_API_KEY` | 暂未实现，但 env var 已预留 |

#### 扩展（`--check --full`）

| # | 检查 | 级别 | 方法 |
|---|------|------|------|
| 9 | LLM 服务连通 | WARN | `LLMFactory.create(model)` → `backend.test()`，发送真实对话请求（"hi", max_tokens=1），超时 10s |
| 10 | TTS 服务连通 | WARN | `TTSFactory.create(model)` → `backend.test()`，超时 5s。MiniMax→POST /v1/get_voice，GPT-SoVits→GET /health，验证 API key 有效性 + 服务功能 |
| 11 | STT 服务连通 | WARN | `STTFactory.create(model)` → `backend.test()`，超时 5s。SenseVoice→GET /health |

### 预检逻辑位置

新增 `Module/config/preflight.py`，暴露 `run(config: AppConfig, full: bool = False) -> bool`。纯逻辑，不污染 `app.py`。

`app.py` 调用：

```python
if args.check:
    from Module.config.preflight import run as preflight
    ok = preflight(config, full=args.full)
    sys.exit(0 if ok else 1)
```

### 配套变更

- **`.env.example`** — 移除废弃的 `DEEPSEEK_API_BASE`、`DEEPSEEK_API_BETA`
- **`Makefile`** — 添加 `check`、`check-full` 目标，调用 `conda run python app.py --check`
- **`README.md`** — 快速开始中加入 `python app.py --check` 步骤

## 成功标准

1. 无 `.env` 时：`app.py --check` → FAIL，打印 `cp .env.example .env` 提示
2. 当前 LLM provider 需要的 env var 缺失时：FAIL，提示缺少哪个
3. pip 包缺失时：FAIL，列出缺失的包名
4. 正常配置时：PASS，exit 0
5. 可选 env var（TTS/STT）缺失时：WARN，不阻止 exit 0
6. `--check --full` 含轻量连通性探活（不实际合成音频或调用 LLM chat）
