# 阶段 4 联调前整改：SenseVoice 默认地址单源 + 阶段 4 机器契约样例 + 确定性 smoke + 能力说明更新

**文档版本：** v0.1
**文档状态：** 待评审
**日期：** 2026-08-15
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** `docs/review/archive/2026-08/2026-08-15-阶段4联调前后端整改清单.md`（BE-S4-INT-P0-01 / P0-02 / P1-01 / P2-01）
**前置决策：** 会话讨论定案 D0-D6（全部按推荐定案）

---

## 1. 背景

阶段 4（Profile 管理、模型切换、TTS voices/preview、三种语音模式、TTS 播放控制）主体能力已
实现且 261 个定向测试通过，但清单指出后端仍有 2 项 P0、1 项 P1、1 项 P2 需在真实联调准入前闭合：

1. **BE-S4-INT-P0-01 内置 SenseVoice Profile 测试丢失默认地址**：`/api/providers/test` 对
   `sensevoice/sense-voice`（无请求体 `base_url`、无 `SENSEVOICE_URL` 环境）返回
   `error: "Request URL is missing an 'http://' or 'https://' protocol."`——请求根本没用到项目
   公开的默认地址 `http://localhost:20042`，是"URL 构造错误"而非"服务离线"。
2. **BE-S4-INT-P0-02 阶段 4 WS/REST 机器契约样例不完整**：`contract.schema.json` 已含全部阶段 4
   模型，但 `scripts/gen_contract_frames.py` 只对 7 类实时语音帧生成 golden 样例；`voice.transcript`、
   `dictation.partial/final/cancelled`、`audio`、`audio_cancelled`、`switch_model_result`、
   `service_mode_result`、`set_tts_voice_result` 共 9 类帧与 Profile/TTS REST 响应无机器样例——
   前端 checker 只能告警或跳过，跨仓漂移不能阻断 CI。
3. **BE-S4-INT-P1-01 缺少可重复的阶段 4 真实 smoke/runbook**：三种语音模式及 TTS/Profile 的
   副作用边界只能靠人工临场判断，无文档化命令可重复证明。
4. **BE-S4-INT-P2-01 `todo.md` 能力现状过期**：仍写"单 session / 无 session CRUD"、"契约只存在于
   手写 TS / 无版本化协议"、"依赖无上限约束"，与多会话 API、`frontend_contract` 生成门禁、
   `environment.lock.yml` 精确锁现状不符。

## 2. 目标

- **P0-01**：内置 `sensevoice/sense-voice` 在请求体无 `base_url` 且环境无 `SENSEVOICE_URL` 时
  稳定解析为 `http://localhost:20042`；非空请求体覆盖、环境变量覆盖、配置默认值、内置默认值
  的优先级只有一处权威实现，Profile 测试与正常 Agent 启动不得走出不同结果；自定义 STT 仍
  必须显式提供合法 `base_url`；SSRF/runtime 守卫保留。
- **P0-02**：9 类缺失服务端帧 + 阶段 4 REST 响应各有确定性生成的机器样例（成功/失败正反例），
  全部进入 `scripts/gen_contract_frames.py` 漂移门禁（`make check-types`），每条样例可被对应
  Pydantic 模型解析，仓库产物无真实音频/转写/密钥/绝对路径。
- **P1-01**：新增 `scripts/stage4_smoke.py` 确定性 smoke——默认本地 stub/固定 fixture，真实
  付费 provider 仅显式 opt-in；覆盖清单 §5.2 五类最小场景；失败返回非零；报告记录后端 SHA、
  protocol version、场景结果与首个失败点。
- **P2-01**：`todo.md` 遗留风险与多前端评估表的过期事实行更新为当前状态，尚存风险拆清。
- **零契约形状变更**：不新增/修改任何 `frontend_contract` 模型（9 类帧模型与 REST 模型已存在），
  TS/JSON Schema/raw 产物无漂移。

## 3. 现状（事实基线）

### 3.1 P0-01 SenseVoice Profile 测试默认地址

| 项 | 现状 |
|---|---|
| 端点 | `core/input/provider_routes.py:212-319` `providers_test_handler()`：请求体 `base_url` 空 → `create_kwargs = {}`（`:283-287`），不注入地址 |
| 工厂 | `Module/stt/registry.py:18-72` `STTFactory.create()`：`base_url = resolve_base_url(cfg["url_key"], cfg, kwargs.pop("base_url", None))`（`:50`）；非 custom 内置分支 `:67-72` 直接 `SenseVoiceBackend(model=..., base_url=base_url)` |
| URL 解析 | `Module/_common/registry_helpers.py:30-55` `resolve_base_url()`：`explicit(非None) > env > cfg["base_url"]`（缺省 `""`） |
| 默认源不一致 | **无 `from_config()` 生产调用**（`registry.py:74-82` 仅测试/工具引用）。真实启动路径 `core/agent/server.py:422`：`stt_params = resolve_model_params("stt", stt_model)` → 内置 provider 返回 `{}`（`core/input/custom_providers.py:154-155`）→ `STTFactory.create(stt_model, **{})` 无显式 base_url → `providers.yaml:72` `stt.sensevoice.base_url: ""` 非空字符串传入构造函数，**覆盖** `SenseVoiceBackend.__init__` 默认（`sensevoice.py:78`）。**全仓生产调用点 4 处同源同 bug**：`server.py:221`（切换）/`server.py:422`（启动）/`voice_sessions.py:457`（听写 profile 快照）/`provider_routes.py:303`（Profile 测试）；`server.py:424` 启动还做 1s 连通性 `test()`——无 `SENSEVOICE_URL` 时 `InvalidURL` → `raise RuntimeError` 启动失败（现实部署须在 `.env` 填默认地址，正是清单 §3.3.3 禁止的）。清单 §3.2 根因第 1 条"from_config 提供默认"与实际不符（`STTConfig` 默认当前无生产消费方） |
| 错误路径 | `sensevoice.py:102` `self._test_fn = make_test_method(_health_check)`；`Module/_common/test_helpers.py:16-24` `test()` 捕获所有异常 → `{"ok": False, "latency_ms", "error": str(e)}`——`base_url=""` 时 `_health_check` 的 `GET /health` 抛 httpx `InvalidURL`（"Request URL is missing…"），被捕获为 `error` 原样返回（`ProviderTestResult(ok=False, error="Request URL is missing…")`，`provider_routes.py:315`） |
| 相关 | `providers.yaml:70-76` `stt.sensevoice`：`base_url: ""`、`url_key: "SENSEVOICE_URL"`、`local: true`；`registry.py:46-47` `openai`/`deepgram` `NotImplementedError`（STT 唯一可实例化内置 = sensevoice）；`registry.py:56-62` custom 分支无地址直接 `raise ValueError` |
| 现有测试 | `core/input/tests/test_providers_api.py:53-76` `test_test_endpoint_builtin_connectivity`（patch `Module.llm.registry.LLMFactory.create` → fake backend，LLM 路径）；SSRF 用例 `:304-336`（body base_url 任意 provider 均 400 `SSRF_BLOCKED`） |

### 3.2 P0-02 阶段 4 机器契约样例缺口

| 项 | 现状 |
|---|---|
| 生成器 | `scripts/gen_contract_frames.py`：`_voice()`（`:249-267`）只产 7 帧（`voice_session.started/partial/turn/ack` + `audio_start/audio_end` + `voice_session.ended`）；`_ARTIFACTS`（`:321-331`）8 个文件；`_check()`（`:334-350`）生成到 tempfile 后 `git diff --no-index` 比对 |
| 门禁 | `Makefile:77-80` `make check-types` = `frontend_contract --check` + `scripts/gen_contract_frames.py --check`——**新产物进 `_ARTIFACTS` 即自动进漂移门禁，无需改 Makefile** |
| 缺帧模型 | 9 类全部已存在：`VoiceTranscriptMessage`（`ws_message.py:598`）、`DictationPartialMessage`（`:646`）、`DictationFinalMessage`（`:652`）、`DictationCancelledMessage`（`:659`）、`AudioMessage`（`:482`）、`AudioCancelledMessage`（`:676`）、`SwitchModelResultMessage`（`:538`）、`ServiceModeResultMessage`（`:548`）、`SetTtsVoiceResultMessage`（`:558`）——均为 `ServerMessage` 判别联合成员 |
| 帧形状（成功/失败） | `SwitchModelResultMessage`：`error` 仅失败带（成功缺省省略）；`ServiceModeResultMessage`：`error` 恒存在（成功为 `null`，`TsNull`）；`SetTtsVoiceResultMessage`：成功带 `voice` 回显、失败带 `error`（均缺省省略）；`AudioMessage`：`stream_id/seq/data(base64)/is_final`；`DictationFinal/Cancelled`：带 `clientRequestId` |
| REST 模型 | `ProvidersResponse`（`rest.py:127`）、`ProviderTestResult`（`:193`，error/detail/latency_ms 可缺省）、`TtsVoicesResponse`（`:222`）、`TtsPreviewResult`（`:235`）、`ApiOk`（`:158`）、`ApiFail`（`:165`）、`ProtocolError`（`:175`）全部存在 |
| 现有测试 | `scripts/tests/test_gen_contract_frames.py`：`test_all_artifacts_are_standard_jsonl`（遍历 `_ARTIFACTS`，新文件自动纳入）、`test_voice_first_turn_id_is_one`（读 `ws-v2-voice.jsonl` 现有 7 帧）——**新文件不触碰既有帧即可保持绿** |
| 文档 | `docs/archive/frontend-handoff-v2/traces/README.md`：`ws-v2-voice.jsonl` 行（`:36`）+ 轨迹清单表（`:28-43`）+ 再生成/漂移检测（`:45-50`）需同步 |

### 3.3 P1-01 阶段 4 确定性 smoke 基建

| 项 | 现状 |
|---|---|
| 现有先例 | `scripts/tests/test_backend_only_smoke.sh`（F3 shell smoke）；`scripts/start.sh` 有 preflight |
| 测试桩模式 | `core/input/tests/test_voice_sessions.py`：`_ProfileSTTBackend`（固定文本 + `transcribed`/`closed` 记录）+ `patch("core.input.voice_sessions.STTFactory.create", return_value=fake)`；`test_voice_ws.py`：`FakeAgent`（stub `session_manager/output/status/stt/stt_transcribe/resolve_session/next_request_id`）+ `FakeWs`（记录 `send_json`） |
| fixture | `core/input/tests/fixtures/sample_16k_mono.pcm`（32000B，16kHz mono s16le）、`Module/stt/tests/fixtures/sample.wav`（32044B） |
| 服务启动 | `core/input/server.py::create_app(agent)`；input 测试经 `aiohttp_client` 起 app |

### 3.4 P2-01 todo.md 过期事实

| 行 | 现文案 | 过时原因 |
|---|---|---|
| `todo.md:255` | 单 Session 多客户端共享 | `session_id="default"` 硬编码 | 批次 3 多会话：`SessionManager` 缓存池 + `resolve_session` + `/api/sessions` CRUD/activate；`default` 仅缺省活动会话 |
| `todo.md:256` | 会话管理 | 无法新建/切换/列出对话 | `/api/sessions` CRUD + `activate_session` WS 消息 |
| `todo.md:259` | 依赖无版本上限 | `requirements.txt` 无上限约束 | `environment.lock.yml`（conda 30 + pip 133，全部 `==` 精确锁定；未启用逐包 hash）锁定实际部署边界 + `make lock-env`/`make audit`；但 `requirements*.txt` 声明文件仍允许宽范围——需拆清"声明文件"与"实际锁定边界" |
| `todo.md:269` | 协议契约语言中立化 | 契约只存在于手写 TS | `frontend_contract` 生成 `contract.schema.json` + `contract.raw.ts`（transportVersion "2"/protocolVersion "2.0"/schemaHash）+ `make check-types` 漂移门禁 |
| `todo.md:270` | 协议版本化 | WS/HTTP 加 protocol_version | 协议 v2：`TRANSPORT_VERSION="2"`/`PROTOCOL_VERSION="2.0"`（`frontend_contract/version.py` 单源） |
| `todo.md:271` | 多 session / 身份 | 关联单 Session | 批次 3 多会话 + v2 显式 `conversationId` |
| `todo.md:272` | 公开契约文档 | 20+ WS 消息写为 protocol.md | `docs/electron-backend-protocol-v2.md` 已存在 |

## 4. 方案

### 4.1 P0-01：内置 SenseVoice 默认地址单源（D1=B，工厂层兜底 + 共享常量）

**D1 定案：B 工厂层回填共享常量**——在 `Module/stt/sensevoice.py` 定义内置默认地址常量，构造函数
默认、`STTConfig` 默认、`STTFactory` 空值兜底三处引用同一常量（真单源，消灭"多副本同值"与
"构造函数默认碰巧兜底"）。

```python
# Module/stt/sensevoice.py —— 内置 SenseVoice 默认端点（阶段4 P0-01 D1=B：唯一权威默认）
DEFAULT_BASE_URL = "http://localhost:20042"

class SenseVoiceBackend(STTBackend):
    def __init__(self, model: str = "sense-voice", *, api_key: str = "",
                 base_url: str = DEFAULT_BASE_URL, logger=None):
        ...
```

```python
# Module/stt/config.py —— STTConfig 默认值引用同一常量（消灭第三处副本）
from Module.stt.sensevoice import DEFAULT_BASE_URL

@dataclass
class STTConfig:
    base_url: str = ConfigField(
        default=DEFAULT_BASE_URL, env="SENSEVOICE_URL", ...)
```

```python
# Module/stt/registry.py —— 兜底必须位于 custom 分支**之后**（否则 custom 会拿到默认地址
# 而非 `raise ValueError`，放宽 §3.3.4 自定义 STT 必须显式 base_url 的要求）
        if provider == "custom":
            ...
            if not base_url:
                raise ValueError("custom STT provider requires base_url")
            ...
            return OpenAICompatSTTBackend(...)

        if not base_url:
            base_url = DEFAULT_BASE_URL   # D1=B：内置分支空值兜底（sensevoice；openai/deepgram
                                          # 已 NotImplementedError，此处仅剩 sensevoice）
        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)
        return SenseVoiceBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
```

**优先级语义（唯一权威实现，§3.3.2）**：`body base_url（非空，经 handler create_kwargs → explicit） >
env SENSEVOICE_URL（非空） > providers.yaml stt.sensevoice.base_url（用户可改，非空才生效） >
DEFAULT_BASE_URL 常量（内置兜底）`。

**一处修复、四点收敛（review 修正）**：兜底在工厂层（`STTFactory.create` 内置分支，位于 custom 分支
之后），全仓 4 个生产调用点（`server.py:221` 切换 / `server.py:422` 启动 / `voice_sessions.py:457`
听写快照 / `provider_routes.py:303` Profile 测试）全部经过同一工厂 → 无 env/无 explicit 时统一解析为
`DEFAULT_BASE_URL`；`STTConfig.base_url` 默认引用常量（`config.py:14-18`）保持单源一致——其当前无生产
消费方（启动路径不经 `from_config`），属装饰性一致性，可防未来某路径真用 STTConfig 时漂移，但不宣称
其驱动启动收敛。修复后 `server.py:424` 启动连通性 `test()` 从 `InvalidURL` 变为真实连接（干净配置可启动）。

**不改 / 保留**：

1. `providers.yaml` **不动**（D1=B 不填 yaml 默认；yaml `""` 恰是"未配置"信号触发工厂兜底；用户显式填值仍生效）；
2. custom STT：`registry.py:56-62` 缺地址 `raise ValueError` **保持**——修复不得放宽（§3.3.4）；
3. SSRF/runtime 守卫：默认 loopback 是项目受控本地内置，不经 SSRF（`provider_routes.py:276-281` 只在 body `base_url` 非空时校验）；用户覆盖地址仍走 `validate_endpoint` + `build_guarded_http_client`（`provider_routes.py:289-295`）——**不因内置默认修复而改变**（§3.3.5）；
4. LLM/TTS 工厂**不动**（review §2 确认 LLM/TTS Profile 测试当前通过；§7 不扩范围）；
5. `make_test_method` 结构化失败保持——修的是"URL 缺协议"，不是错误信封（§3.3.6）。

**必须补充的测试**（§3.4，review 逐条）：

| # | 用例 | 落点 |
|---|---|---|
| T1 | 无 env/无 explicit：`STTFactory.create("sensevoice/sense-voice")` → `backend._client.base_url == DEFAULT_BASE_URL`（`patch.dict(os.environ, {}, clear=True)` 隔离；`httpx.AsyncClient` 构造不发网络） | `Module/stt/tests/` 工厂级 |
| T2 | `SENSEVOICE_URL` 设值 → `backend._client.base_url == env 值`（env 覆盖默认） | 同上 |
| T3 | 显式 `create(..., base_url=...)` → explicit 胜出 | 同上 |
| T4 | custom 缺地址 `create("custom/x")` → `ValueError`（保持） | 同上 |
| T5 | 端点级成功：`patch("Module.stt.registry.STTFactory.create")` → fake backend `test()` 返回 `{"ok": True, "latency_ms": 12}` → `/api/providers/test` 返回 `ProviderTestResult(ok=true, latency_ms=12)`；断言 create 以 `"sensevoice/sense-voice"` 且**无 base_url kwarg** 调用（默认注入在工厂层） | `core/input/tests/test_providers_api.py` |
| T6 | 端点级失败可诊断：fake backend `test()` 返回 `{"ok": False, "error": "<connect: localhost:20042>"}` → 断言响应 error **不含** `"Request URL is missing"` | 同上 |
| T7 | 后端级：patch `_health_check` 抛 `httpx.ConnectError` → `SenseVoiceBackend.test()` 返回 `{"ok": False, "error": 含 connect 描述}`（transport 错误结构化，非 InvalidURL） | `Module/stt/tests/` |
| T8 | 端点级 body base_url 覆盖（review LOW-4）：`patch("Module.stt.registry.STTFactory.create")` + POST 带合法 `base_url: "http://1.2.3.4:20042"` → 断言 create 以该 base_url kwarg 调用（复用 T5 mock 模式，锁 body → create_kwargs → 工厂 explicit 整链） | `core/input/tests/test_providers_api.py` |
| T9 | LLM/TTS 既有 Profile 测试（`test_test_endpoint_builtin_connectivity` 等）**不回归** | 回归 |
| T10 | 真实验收（§3.5，runbook）：干净配置无 `SENSEVOICE_URL` + 本机 SenseVoice 启动 → 正式端点 `sensevoice/sense-voice` 真实连通；停服务 → 不可达类结果（非 URL 构造错误） | 手动/文档 |

### 4.2 P0-02：补齐阶段 4 WS/REST 契约样例（D2=B 拆文件，D3=A 裸 wire）

**D2 定案：B 按流程拆独立 JSONL**。9 类缺帧 + REST 按流程归入新文件；既有 `ws-v2-voice.jsonl`
**原样保留**（避免破坏 `test_voice_first_turn_id_is_one`）。全部帧用既有契约模型构造，确定性
（固定测试 ID/时间戳，不取 `datetime.now`）。

新增 `_ARTIFACTS` 条目（文件名 → 帧序列 → 覆盖流程）：

| 文件 | 帧（Pydantic 模型构造） | 覆盖 |
|---|---|---|
| `ws-v2-recording.jsonl` | `VoiceTranscriptMessage(recordingId="rec_frame_1", text="你好…", emotion="开心")` ×2（字幕演进） | 普通录音 stop 后待确认字幕 |
| `ws-v2-dictation.jsonl` | `DictationPartialMessage(text="你")` → `DictationPartialMessage(text="你好")` → `DictationFinalMessage(clientRequestId="req_frame_dict1", text="你好")`（成功序列）；`DictationPartialMessage(text="取消…")` → `DictationCancelledMessage(clientRequestId="req_frame_dict2")`（取消序列） | 听写 partial/final/cancelled |
| `ws-v2-audio.jsonl` | 播放完成序列：`AudioStartMessage(stream_id=2, …)` → `AudioMessage(stream_id=2, seq=0, data=…, is_final=False)` → `AudioMessage(stream_id=2, seq=1, data=…, is_final=True)` → `AudioEndMessage(stream_id=2)`；播放取消序列：`AudioStartMessage(stream_id=3, …)` → `AudioMessage(stream_id=3, seq=0, …)` → `AudioCancelledMessage(stream_id=3)` | TTS 播放控制 audio/audio_cancelled |
| `ws-v2-service.jsonl` | `SwitchModelResultMessage(service="llm", model="deepseek/deepseek-v4-flash", ok=True)`（成功无 error 键）+ `SwitchModelResultMessage(service="stt", model="sensevoice/sense-voice", ok=False, error="模型不可用")`；`ServiceModeResultMessage(service="stt", enabled=True, ok=True, error=None)`（error 键存在为 null）+ `ServiceModeResultMessage(service="tts", enabled=False, ok=False, error="后端不可用")`；`SetTtsVoiceResultMessage(ok=True, voice="elysia_test_001")`（成功带 voice 无 error）+ `SetTtsVoiceResultMessage(ok=False, error="声音不存在")` | 模型切换/服务模式/TTS 声音结果成功+失败 |
| `rest-providers.jsonl` | `ProvidersResponse`（llm/stt/tts 各含条目 + custom + unconfigured） | GET /api/providers |
| `rest-provider-test.jsonl` | `ProviderTestResult(ok=True, latency_ms=12)` + `ProviderTestResult(ok=False, error="无法连接 SenseVoice 服务 (…)")` | POST /api/providers/test 成功+失败 |
| `rest-tts-voices.jsonl` | `TtsVoicesResponse(voices=[…], current="elysia_test_001", source="api")` + `TtsVoicesResponse(voices=[], source="static", error="TTS 未启用")` | GET /api/tts/voices 正常+未启用 |
| `rest-tts-preview.jsonl` | `TtsPreviewResult(ok=True, mime="audio/wav", duration_ms=1200, audio_base64=…固定短值)` + `TtsPreviewResult(ok=False, error="voice 不存在")` | POST /api/tts/preview 成功+失败 |
| `rest-envelopes.jsonl` | `ApiOk(ok=True, data=…)`（save/delete 成功）+ `ApiFail(ok=False, error="删除失败，请稍后重试")` + `ProtocolError(code="SSRF_BLOCKED", message=…, retryable=False, requestId="req_frame_ssrf", error=…, details={})` | 通用成功/失败信封（行→模型映射在生成器 docstring + README 表文档化） |

要点：

1. **audio base64（§4.2.3）**：固定短占位数据（如 16 字节合成 PCM，非真实音频），`stream_id/seq/is_final`
   稳定；`rest-tts-preview` 的 `audio_base64` 同样固定短值；**不写真实用户音频/转写/密钥/签名 URL/绝对路径**；
2. **正反例（§4.2.2）**：结果帧（switch_model/service_mode/set_tts_voice/ProviderTestResult/TtsPreview/
   TtsVoicesResponse）成功+失败都覆盖；失败字段的**键存在性**严格对齐模型注释（switch_model 成功无
   `error` 键、service_mode 成功 `error` 键存在为 `null`、set_tts_voice 成功带 `voice` 无 `error`）——
   与生成 TS 的 optional/nullable/缺省形状一致（§4.3「optional、nullable、缺省字段的形状与生成的
   TypeScript 一致」）；
3. **文件内多序列**：recording/dictation/audio/service 文件内含"成功 + 取消/失败"两个子序列，以生成器
   docstring + README 表说明（§4.2.6"写明每个文件覆盖的流程、方向和成功/失败语义"）；
4. **门禁（§4.2.5）**：所有新文件进 `_ARTIFACTS` → `--check` 自动覆盖；手改产物而未更新权威模型 → CI 红
   （既有机制，不改 Makefile）。

**必须补充的测试**（§4.3，review 逐条）：

| # | 用例 |
|---|---|
| P1 | `_ARTIFACTS` 全部文件标准 JSONL（既有 `test_all_artifacts_are_standard_jsonl` 遍历自动纳入，新增文件非空） |
| P2 | 每条 WS 帧行可被 `wm.ServerMessage` 判别联合解析（`ServerMessage.model_validate_json`）——9 类缺帧全覆盖 |
| P3 | 每条 REST 行按文档化模型映射解析（`ProvidersResponse`/`ProviderTestResult`/`TtsVoicesResponse`/`TtsPreviewResult`/`ApiOk`/`ApiFail`/`ProtocolError`） |
| P4 | 结果帧正反例键形状断言：switch_model 成功无 `error` 键/失败有；service_mode 成功 `error` 为 `null`；set_tts_voice 成功带 `voice`；ProviderTestResult 成功无 `error`、失败带 `error`；TtsPreview 成功带 `mime`/`audio_base64`、失败带 `error` |
| P5 | audio 帧 `stream_id/seq/is_final` 稳定 + `data` 为固定短 base64；`seq` 从 0 递增 |
| P6 | 敏感信息 grep 门禁：新样例文件无 API key、签名 URL、真实音频、真实转写、机器绝对路径（`/home/…`） |
| P7 | 漂移：`_check()` 机制既有；新增 test 对任一新文件内容做篡改后生成比对 → 应失败（模拟 `--check` 拦截） |

### 4.3 P1-01：确定性 Stage 4 smoke（D4=A，独立 Python 脚本）

**D4 定案：A 可执行 Python 脚本 `scripts/stage4_smoke.py`**（进程内 `create_app` + 桩 agent/后端，
默认确定性；`STAGE4_SMOKE_LIVE=1` 显式 opt-in 接真实 provider，不进 CI）。定位：**契约级确定性
smoke**——证明阶段 4 关键语义（副作用边界），非真实 provider/Electron 联调（§5.4 区分）。

- **驱动方式**：`create_app(agent)` 进程内启动 + `aiohttp.test_utils.TestClient`（REST）+ WS 客户端
  （`ws_connect`）；agent 用最小桩（复用 `test_voice_ws.py` `FakeAgent` 模式：stub `session_manager`/
  `output`/`status`/`stt`/`stt_transcribe`/`resolve_session`/`next_request_id`/`bus`/`config`；
  场景 1 的 PROFILE_IN_USE 与 provider-test 缺省回退还要 stub `session`（含
  `service_state.{llm,stt,tts}_model`）、`llm`（`model`）、`tts`（`model` 或 None），review LOW-1），
  `patch("Module.stt.registry.STTFactory.create", return_value=_FakeSTTBackend(fixed_text))`、
  `patch("Module.tts.registry.TTSFactory.create", …)`、`patch("Module.llm.registry.LLMFactory.create", …)`
  ——不连 Mongo/Milvus/外部网络，完全内存态；
- **场景**（§5.2 五类，每类独立 PASS/FAIL）：
  1. **Profile 与模型切换**：GET `/api/providers`（不回显 api_key）；POST `/api/providers/test` 成功/
     离线/非法配置；save/delete 自定义 Profile；**正在使用的 Profile 不可删除**（409 `PROFILE_IN_USE`）；
     WS `switch_model` 成功只影响新请求 / 失败保留原活动模型；
  2. **普通录音**：`voice.record.start` → BINARY PCM（fixture `sample_16k_mono.pcm`）→ `voice.record.stop`
     → `voice.transcript` 待确认字幕（**不自动发聊天消息**）；`voice.commit` 才创建用户消息并触发
     generation；`voice.discard` 不创建消息/不触发 generation；取消/空录音/STT 失败可清理并再次录音；
  3. **实时语音**：`voice_session.start` → BINARY → `voice_session.partial/turn/ack` → `voice_session.stop`
     → `voice_session.ended`；每 turn 自动创建且只创建一次用户消息；`voice_session.ack.seq` 随 BINARY
     帧序递增（背压）；TTS `audio_start` 的 generation/conversation 归属与当前 turn 一致；异常结束后
     不残留到下一会话（review LOW-3 补背压与 TTS 归属断言）；
  4. **听写**：`dictation.start` → BINARY → `dictation.partial`（仅预览）→ `dictation.stop` →
     `dictation.final`（只回文字，零消息副作用）；`dictation.cancel` → `dictation.cancelled` 清理缓存；
     同 `clientRequestId` 结束/取消不重复 settle；
  5. **TTS 与播放控制**：GET `/api/tts/voices` 可读；POST `/api/tts/preview` 成功/离线/非法 voice 确定响应；
     `audio_start → audio* → audio_end` stream/seq 连续；`audio.cancel` → `audio_cancelled` 后无孤儿 chunk；
     `set_tts_voice` → `set_tts_voice_result` 成功/失败关联实际状态；
- **报告与退出（§5.3）**：每场景 PASS/FAIL 行；记录后端 SHA（`git rev-parse HEAD`）、protocol version
  （`frontend_contract.version.PROTOCOL_VERSION`）、场景结果、首个失败点；**任一失败 → 非零退出**
  （不打印错误后继续"通过"）；默认无外部服务环境可重复运行；
- **fixture 复用**：`sample_16k_mono.pcm`（录音 PCM 流，描述统一写 16kHz/mono/s16le）、`sample.wav`
  （TTS preview 返回）；
- **不写**：不记录密钥/完整音频 payload/真实转写正文/带签名完整 URL（§5.3）；真实 sensevoice 连通
  属于 §3.5 真实验收（runbook 单列，不进 smoke 默认路径）。

### 4.4 P2-01：todo.md 过期事实更新（D5=A，最小改动）

**D5 定案：A 只修 `todo.md` 指出的 7 行**（`3.4` 表），不扩其它文档。逐行改为当前事实：

- `:255` 单 Session 多客户端共享 → ✅ 批次 3 多会话（`SessionManager` + `/api/sessions` CRUD/activate）；
  `"default"` 仅作缺省活动会话；
- `:256` 会话管理 → ✅ 已实现（会话新建/切换/列出/历史/搜索/删除，`session_routes.py`）；
- `:259` 依赖无版本上限 → **拆清两件事**：`environment.lock.yml` 精确锁（conda 30 + pip 133，全部
  `==` 锁定、未启用逐包 hash，`make lock-env` 重生成 / `make audit` 漏洞扫描）已收敛实际部署边界；
  但 `requirements*.txt` 声明文件仍允许宽范围——风险措辞改为"声明文件允许宽范围，实际锁定由 lock
  文件保证"，不再写"无上限约束"；
- `:269` 协议契约语言中立化 → ✅ `frontend_contract` 生成 `contract.schema.json` + `contract.raw.ts`，
  语言中立（零依赖 Pydantic 模型源）；
- `:270` 协议版本化 → ✅ 协议 v2（`TRANSPORT_VERSION="2"`/`PROTOCOL_VERSION="2.0"` 单源，握手头携带）；
- `:271` 多 session / 身份 → ✅ 批次 3 多会话 + v2 显式 `conversationId`；
- `:272` 公开契约文档 → ✅ `docs/electron-backend-protocol-v2.md`（WS/REST 消息 + 错误码 + 错误信封）。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D0 批组织 | **A 合并单批** | 4 项同挡阶段 4 联调门、共处一份整改清单、共用 §9 完成定义；一次 9 阶段流程成本更低；P0-02 与 P1-01 都依赖同一契约/桩基建，同批共享 |
| D1 P0-01 默认地址归属 | **B 工厂层回填共享常量** | `sensevoice.py` 定义 `DEFAULT_BASE_URL`，构造函数默认 + `STTConfig` 默认 + `STTFactory` 空值兜底三处引用同一常量 → 真单源；清单 §3.3 要求"只有一套可测试的优先级语义"+"不能再依赖构造函数默认碰巧兜底"；比 `providers.yaml` 填默认（A）更彻底（A 仍留 STTConfig/构造函数两处同值副本）。**范围只限 sensevoice**，不碰 ollama/gpt-sovits（§7 不扩） |
| D2 P0-02 产物组织 | **B 按流程拆独立 JSONL** | 9 类缺帧 + REST 归 9 个新文件，每文件单一流程族 + 正反例；既有 `ws-v2-voice.jsonl` 原样保留（不破坏既有语义测试）；清单 §4.2.1 两方式均可，拆文件命名清晰 |
| D3 P0-02 REST 样例形态 | **A 裸 wire 响应对象** | 每行即真实响应 wire 形状（忠实、可被对应模型直接解析）；模型映射靠"每文件单模型"（`rest-providers/rest-provider-test/rest-tts-voices/rest-tts-preview`）+ `rest-envelopes.jsonl` 文档化行→模型映射（README + 生成器 docstring）；拒绝自描述 manifest 包装（`{endpoint, model, response}` 需解包，非原始形状，前端不可直接 replay） |
| D4 P1-01 smoke 形态 | **A 独立 Python 脚本** | `scripts/stage4_smoke.py` 进程内 `create_app` + 桩，默认确定性、`STAGE4_SMOKE_LIVE=1` 才接真实 provider；§9 要求"可执行 smoke，失败返回非零"；脚本为主 + runbook 简述命令 |
| D5 P2-01 文档范围 | **A 只修 todo.md 7 行** | 文档债不扩；只改清单指出的过期事实，别处不动 |
| D6 跨仓边界 | **A 本批只做后端产物** | 后端提交 + 记录新产物 SHA；前端 checker（elysia-frontend `check-backend-contract.mjs`）提升 voice/dictation/audio/Profile/TTS 为 error 由前端仓推进（§4.4 跨仓闭环需双方 SHA 固定后做）；延续 BE-S3-RUN/BE-INT2 先例 |

## 6. 波及面

- 修改：
  - `Module/stt/sensevoice.py` — 新增 `DEFAULT_BASE_URL` 常量 + 构造函数默认引用（`:78`）
  - `Module/stt/config.py` — `STTConfig.base_url` 默认引用常量（`:14-18`）
  - `Module/stt/registry.py` — 内置分支空值兜底（`:67` 前一行）
  - `scripts/gen_contract_frames.py` — 新增 `_recording()/_dictation()/_audio()/_service()/_rest_profile()/_rest_tts()/_rest_envelopes()` 生成器 + `_ARTIFACTS` 9 条目（`:321-331`；映射：`_rest_profile` 产 `rest-providers`+`rest-provider-test` 两文件、`_rest_tts` 产 `rest-tts-voices`+`rest-tts-preview` 两文件）
  - `docs/archive/frontend-handoff-v2/traces/README.md` — 轨迹清单表 + 帧数 + 再生成说明（`:28-50`）
  - `todo.md` — §4.4 7 行过期事实更新（`:255/256/259/269/270/271/272`）
- 测试：
  - `Module/stt/tests/` — **新建**工厂/后端级用例（T1/T2/T3/T4/T7）
  - `core/input/tests/test_providers_api.py` — 端点级用例（T5/T6/T8）
  - `scripts/tests/test_gen_contract_frames.py` — 新增帧语义断言（P2/P3/P4/P5/P6/P7）
  - `scripts/tests/` — smoke 包装（P1-01 可选，slow marker，plan 定）
- **无**：`frontend_contract/` 模型变更（9 类帧 + REST 模型已存在）、TS/JSON Schema/raw 产物漂移、
  前端代码、`Makefile`、`providers.yaml`

## 7. 验收标准

1. `make test-quick` 不低于基线 **3600 passed** 且无新增失败；`make test-normal` 全绿；
2. `make lint`（ruff）全绿、`make pyright`（Elysia env）0 errors、`make check-types` 全绿
   （含新 golden 帧漂移门禁）；
3. **P0-01 自动化**：T1-T9 全过；`grep -rn "Request URL is missing" Module/stt/ core/input/ --include="*.py"` 无命中
   （默认路径不再产生 URL 构造错误）；custom STT 缺地址仍 `ValueError`；
4. **P0-01 真实验收（§3.5，runbook，非 CI）**：干净配置（无 `SENSEVOICE_URL`）+ 默认 SenseVoice 启动 →
   正式 `/api/providers/test` `sensevoice/sense-voice` 真实连通；停服务 → 不可达类结果（非 URL 构造错误）；
5. **P0-02**：9 类缺帧 + 4 类 REST 响应 + 通用信封全部有样例；每个样例可被对应模型解析；
   结果帧正反例键形状与生成 TS 一致；`python scripts/gen_contract_frames.py` 两次输出一致、
   `--check` 未漂移为 0、篡改任一样例后 `--check` 失败；样例文件无 API key/签名 URL/真实音频/真实
   转写/`/home/…` 绝对路径；
6. **P1-01**：一条文档化命令（`python scripts/stage4_smoke.py`）在无付费外部服务环境可重复运行，
   默认走 stub 全 PASS、失败非零退出；报告含后端 SHA + protocol version + 场景结果 + 首个失败点；
   `STAGE4_SMOKE_LIVE=1` 文档化为真实 provider opt-in（不进 CI）；
7. **P2-01**：`todo.md` 7 行过期事实更新完毕；`sed -n '250,275p' todo.md | grep -nE 'session_id = "default" 硬编码|无法新建/切换/列出对话|frontend/src/types/protocol.ts|写为 protocol.md'` 无命中
   （限定整改表格区；grep 目标是**旧事实措辞**——新行标题若保留「单 Session 多客户端共享」等合法问题名不误伤；`:31` 历史基线的旧前提引用不属整改范围）。

## 8. 风险

- **P0-01 优先级语义回归（低）**：工厂兜底只触发"解析结果为空串"，一处修复覆盖 4 个生产调用点
  （`server.py:221/422`、`voice_sessions.py:457`、`provider_routes.py:303`）；`providers.yaml` 用户
  显式填值仍生效（4 层优先级写进 spec 冻结）；LLM/TTS 工厂不动，不引入新默认；SSRF 守卫（body
  base_url 非空才校验）与 guarded client 路径不变；`server.py:424` 启动连通性 `test()` 由 `InvalidURL`
  变真实连接（干净配置可启动）；
- **P0-01 导入环（低）**：`Module/stt/config.py` 引 `Module/stt/sensevoice.py` 常量——`Module/stt/__init__.py`
  顺序 `protocol → registry → config`，registry 已引 sensevoice，config 引 sensevoice 无环（plan 验证）；
- **P0-02 文件数量（低）**：新增 9 个 JSONL（WS 4 + REST 5），README 表同步；样例只覆盖关键正反例，
  非全状态空间——已覆盖清单 §4.2.2 要求的"有意义的正反例"；
- **P0-02 帧键形状漂移（低）**：`rest-envelopes.jsonl` 混合 3 模型靠文档化映射；正反例键存在性由 P4 断言
  锁定，与模型注释/TsNull 注解一致；
- **P1-01 桩 agent 边界（中）**：桩 agent 非真实 Agent——不覆盖生命周期/持久化/Mongo/Milvus 副作用；
  smoke 定位"契约级确定性 smoke"，真实 provider/Electron 联调由 `STAGE4_SMOKE_LIVE` + §5.4 runbook 承担，
  两者在报告与验收中明确区分；
- **P1-01 WS 驱动成本（中）**：三语音模式经真实 ws_handler 走 `VoiceSessionManager`，需桩 agent 提供
  `session_manager/output/status/stt/stt_transcribe/resolve_session` 等完整最小面（复用 `test_voice_ws.py`
  模式）；plan 内 Task 拆解为"先 REST/Profile → 再 WS 语音"减小单任务风险；
- **P2-01 文档行漂移（低）**：改的是事实描述，`grep` 验收（§7.7）可机检。

## 9. 后续衔接

- 本批后端提交 + 记录新产物 SHA → **前端（elysia-frontend）**：消费同一契约产物，将
  voice/dictation/audio/Profile/TTS 从 warning/未检查提升为 error，对真实 Adapter 跑解析测试，
  故意改错字段/帧名证明 `check:contract` 失败（清单 §4.4 跨仓闭环，属前端所有权，D6=A 本批不实施）；
- 双方 SHA 固定后 → Windows Electron + Ubuntu 后端阶段 4 真实联调（§5.4/§9）；
- **标记纪律（§9）**：本批完成仅可标记「阶段 4 功能已开发/自动化已验证」；「阶段 4 已完整联调」
  须待真实联调报告（前后端 SHA、运行时版本、三语音模式、Profile/TTS 测试结果、失败诊断）落定。

## 10. Review 记录（2026-08-15）

- code-review 第 1 轮（general-purpose agent，逐行核对源码）：**总体结论 yes（无阻断）**——4 项
  P0/P1/P2 修复设计与测试计划均成立，D0-D6 决策无需变更。无 HIGH。**MED-1** §3.1/§4.1「启动走
  `from_config` → STTConfig 默认 → explicit 胜出」失实——`from_config` 零生产调用；真实启动路径
  `server.py:422` 同样走 `STTFactory.create`（`resolve_model_params` 内置返回 `{}`），与 Profile 同源
  同 bug → §3.1 改写为"4 生产调用点同源"、§4.1 改为"一处修复四点收敛"（工厂兜底实际修 4 处），
  §8 同步；**MED-2** `environment.lock.yml`「115 pip 包版本+hash」数字错——实为 conda 30 + pip 133
  全 `==` 锁定、未启用逐包 hash → §3.4/§4.4 改为准确表述；**MED-3** §7.7 grep 与 D5「只修 7 行」
  矛盾（`todo.md:31` 历史基线引用含"契约只存在于手写 TS"字样）→ §7.7 grep 限定整改表格区
  （`sed -n '250,275p'`），`:31` 不动。**LOW-1** 桩 agent 最小面缺 `session`（`service_state.*`）/
  `llm`/`tts`（PROFILE_IN_USE 与 provider-test 缺省回退要用）→ §4.3 补；**LOW-2** 生成器命名（7）与
  文件数（9）映射未写明 → §6 补 `_rest_profile`/`_rest_tts` 各产两文件；**LOW-3** 场景 3 缺背压
  `ack.seq` 与 TTS 归属断言 → §4.3 补；**LOW-4** 缺端点级"合法 body base_url 覆盖"正向用例 → 测试表
  补 T8（T9/T10 顺延）。
- **总体结论（agent）**：yes（无阻断）——3 MED + 4 LOW 已在上述修复中消化，D0-D6 不变。
