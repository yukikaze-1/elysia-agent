# Batch 17 — Ollama Provider Adapter（KB-13 / AR-16 / PD-12）执行记录

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> **范围**：canonical 主 scope 仅 **KB-13**（Ollama runtime context authority mismatch）与
> **AR-16**（Ollama Provider Adapter / dedicated backend architecture debt），并顺带裁决
> **PD-12**（`contextWindow` 语义）。**不做**：每个 provider 全拆 adapter、LLM 子系统重写、
> generic HTTP client 重写、PromptAssembler 重构、KB-12 tokenizer/估算器正式修复、
> UI model 配置重做、model discovery、Ollama 安装/管理、GPU 调度、VRAM 自动调优、
> benchmark 框架、retry 框架重写、OpenAI/Gemini 语义重构、前端改版。
>
> **结果**：**KB-13 = GUARDED**、**AR-16 = CLOSED**、**PD-12 = 裁决（A：effective runtime
> window）**、**OBS-2 被本批事实反驳（注释与括号已修）**。协议从 OpenAI-compatible
> **切到 native `/api/chat`**（有 §33 gate 的四条硬证据）。公共 API **零变化**、
> config schema **零变化**、前端**零变化**。真实 Ollama 冷启动证据
> `configured=8192 / requested=8192 / observed=8192`（修前 `8192 / None / 4096`）。
>
> 环境：Ollama **0.24.0**、模型 `qwen2.5:3b`、daemon **未配** `OLLAMA_CONTEXT_LENGTH`。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| KB-13 confirmed? | **是**（修前真实路径复现：configured=8192、requested=∅、observed=**4096**；预算是 6144） |
| AR-16 resolved? | **CLOSED**（Ollama wire/runtime 语义收口到 `Module/llm/ollama.py::OllamaBackend`；上层零 `if provider == "ollama"`） |
| classification? | KB-13 → `GUARDED`（新不变量 OL1–OL6 + 变异全杀）；AR-16 → `CLOSED`；PD-12 → 已裁决（A） |
| contextWindow contract? | **effective runtime window**（预算 ×0.75 的来源 +"能显式申请分配的 provider 必须按它申请"的运行时意图），**不是**理论 capability、**不是**纯预算提示 |
| provider architecture? | `LLMAdapter → LLMFactory → {OpenAICompatBackend, OllamaBackend}`（`gemini` 仍未实现；`custom`/云端共用通用后端） |
| production fix? | **有**：`Module/llm/ollama.py`（新，519 行）+ `registry.py` 的 canonical-provider 选择（+20 行） |
| protocol? | **native `POST /api/chat`**（实测 `/v1/chat/completions` 静默忽略 `options.num_ctx`，无法表达 runtime context） |
| public API? | **零变化**（`LLMBackend` 协议形状不变、REST/WS/契约层不变、前端不变） |
| config schema? | **零变化**（不新增 `runtimeContextWindow`；已有 `contextWindow` 继续用） |
| real Ollama? | **有**：冷启动三分真值 + downshift + warm 4096→8192→4096 + 静默截断 + 诊断失败不影响 generation |
| real CI? | 见 §37（owner push 后核对 backend / real-mongo / frontend / 静态 jobs） |

---

## 2. Original Backlog Reconciliation

### 2.1 KB-13

| canonical 断言 | 本批核对 |
|---|---|
| "Elysia 用静态 `contextWindow` 做 budgeting" | ✅ 成立（`LLMFactory.context_window` → `budget_from_context_window`，唯一生产消费点 `loop.py` + `preview.py`） |
| "Ollama serving context 是 runtime allocation，不由声明决定" | ✅ 成立（冷启动 A/B：无 `num_ctx` → 4096；`num_ctx=8192` → 8192） |
| "authority 缺失" | ✅ 成立且**更强**：不只是"没绑定"，而是**当前所用协议根本无法表达**——`/v1/chat/completions` 带 `options.num_ctx` 也是 4096 |
| "超窗 HTTP 200 + 静默截断" | ✅ 复现（`num_ctx=2048` + ~6000 词 prompt → 200、`prompt_eval_count=2048`、`done_reason=stop`、无 error） |
| "preview 与 normal chat 两者都受影响" | ✅ 成立（两条路径共用同一窗口权威；本批未给任何一条加第二套 authority） |
| "Reach = DEPLOYMENT，但用户可见" | ✅ 保持（词表无 `USER-adjacent`） |
| 原登记的"`providers.yaml` 应改成 4096"倾向 | ❌ **明确证伪**：数值不是问题（模型默认 4096 只是 daemon 默认），绑定 authority 才是 |

### 2.2 AR-16

| 预期职责（原登记） | 本批落点 |
|---|---|
| request / wire translation | ✅ `_build_payload` + `_to_native_message` |
| `num_ctx` | ✅ 只来自显式声明（OL1/OL2） |
| `keep_alive` | ✅ 内部转写钩子（**不新增用户配置**，§26-C） |
| Ollama-specific runtime options | ✅ `num_predict` / `temperature`；无对应语义者**丢弃并警告一次** |
| canonical Elysia context budget → Ollama runtime context | ✅ 同一 `_BACKENDS` 条目 → backend → wire |
| `prompt_eval_count` / `eval_count` → 通用 usage 归一 | ✅ 终帧一次（OL5） |
| silent truncation / runtime diagnostic 语义 | ✅ `probe_runtime_context()`（configured/requested/observed）+ 文档与不变量 |
| 可选 `/api/ps` diagnostics，**不得**进热路径 | ✅ 只在显式诊断/集成测试；热路径 1 generation = 1 request（有守卫） |
| 目标形状 `{OpenAICompatibleBackend, GeminiBackend, OllamaBackend}` | ✅ 前两者现状不变（gemini 仍 `NotImplementedError`），ollama 独立 |
| "不是每个 provider 都要独立 adapter" | ✅ 只有 ollama 拆；`custom`/mimo/云端共用通用后端 |

---

## 3. Current LLM Architecture（before）

```text
core/cognitive/loop.py::_build_turn_prompt
    ├── LLMFactory.context_window(self._llm.model)        ← 预算权威（providers.yaml）
    ├── budget_from_context_window(...) = window × 0.75
    ├── PromptAssembler.build_messages(prompt_budget_tokens=…)
    └── LLMAdapter.stream_think(messages, tools, extra_body={"thinking": …})
              ↓
        LLMAdapter._backend
              ↓
        LLMFactory.create("ollama/<model>")               ← 唯一选择点
              ↓
        OpenAICompatBackend（**所有** provider 共用）
              ↓  openai.AsyncOpenAI(base_url="http://localhost:11434/v1")
        POST /v1/chat/completions   ← 请求体没有任何 runtime context 字段
              ↓
        Ollama runner：**按 daemon 默认分配 context = 4096**
```

```text
providers.yaml::llm.ollama.context_window = 8192
        ↓  LLMFactory.context_window（读）
   prompt 预算 = 6144              ← "Elysia 相信能装 6144"
        ✗ 与运行时无连接
   Ollama serving window = 4096    ← "实际服务 4096"，超窗 200 + 静默截断
```

第二条组装路径（preview，Batch 7）共用同一窗口权威，因此**同病**。

---

## 4. Context Semantics Decision（PD-12 → A）

**裁决**：`providers.yaml::llm.<provider>.context_window`

> = **Elysia 对该 provider 声明的 effective runtime window**：
> 既用于 prompt budgeting（×0.75），也是"该 provider 上实际可服务多少"的承诺；
> 对**能显式申请分配**的 provider（当前仅 Ollama，经 native `options.num_ctx`），
> 必须按同一值**申请**；对不能申请的 provider（云端），它是**声明即信任**，
> 请求侧如实记为 `requested=None`（"runtime context not controlled by Elysia"）。

**判据是消费者语义，不是命名**（§5 的全仓审计）：

| Consumer | 位置 | 当前含义 |
|---|---|---|
| Prompt budget（chat） | `core/cognitive/loop.py::_build_turn_prompt` | **effective ceiling**（预算 = 0.75×，见 `Module/system_prompt/budget.py`） |
| Preview budget + 本地拒绝 | `core/character/preview.py:114-139` | **真实可服务窗口**：`估算输入 >= effective_window` 就是"装不下，不发" |
| Provider backend | `Module/llm/registry.py::create` | 修前**不读**（缺陷）；修后 ollama 读它做 runtime 申请 |
| UI / config | `providers.yaml` + `core/providers/provider_registry.py` | 仅 provider 元数据；**没有**"理论最大"展示面 |
| Diagnostics | `Module/llm/ollama.py::probe_runtime_context` | 新：configured / requested / observed 三分真值 |
| cost / token limit | `core/monitoring/token_tracker.py` | 用 provider 返回的 usage，**不**读窗口 |
| max output 计算 | 无 | 仓内没有 model 级 output cap metadata（Batch 7 已声明） |

**为什么不是 B（理论 capability）**：全仓**零**处把它当理论上限用；若改读成 capability，
则预算立刻变假（8192 预算 vs 4096 服务），且必须新增第二个字段承载运行时分配——
凭空产生两个权威（§45 prefer no new config field）。**为什么不是 C（预算提示）**：那等于
承认"budgeting truth 是假的"，与本批目标（truthfulness）直接冲突。

**为什么不需要新字段（§44-7 = NO）**：新增 `runtimeContextWindow` 会立刻要求"预算用哪个"
的二选一，无论选哪个都让另一个字段变成谎言。当前语义可以用**一个**值无歧义表达。

---

## 5. Context Authority Map

```text
config          providers.yaml::llm.ollama.context_window = 8192
                        ↓（`LLMFactory.context_window()` 与 `LLMFactory.create()` 读**同一条目**）
budgeting       budget_from_context_window(8192) = 6144   → PromptAssembler / preview
                        │
backend request OllamaBackend(context_window=8192)
                        ↓  _build_payload（唯一 wire 构造点）
wire            POST /api/chat { "options": { "num_ctx": 8192 }, … }
                        ↓
runtime         Ollama runner 分配 8192（**冷启动即 8192**；warm 时按请求重载）
                        ↓
observation     GET /api/ps context_length = 8192   ← 只在 probe / 集成测试里读
```

**三分真值**（OL4）：`configured`（config）/ `requested`（wire）/ `observed`（/api/ps）
可区分，且 `observed` **不是**热路径依赖。

**`requested` 的精确语义**：它是**generation**（`chat`/`chat_stream`/`stream_think`）会带的值。
连通性探活 `test()` **不**申请分配；但 `/api/providers/test` 的 **usable** 级会走真实 `chat()`
⇒ 会申请；`--full` 对 ollama 直接跳过（独立复核纠正了本文初稿的说法）。

---

## 6. Explicit vs Fallback Context

| 情形 | 预算侧 | 运行时侧 |
|---|---|---|
| `context_window = 8192`（显式） | 6144 | `num_ctx = 8192` |
| `context_window` 缺失 | `DEFAULT_CONTEXT_WINDOW = 32768` ⇒ 24576（既有兼容回退，**不变**） | **不发 `num_ctx`**（daemon 默认；诊断面 `requested=None`） |
| `context_window = 0 / 负数` | 同"缺失"（`effective_context_window` 既有语义） | **不发 `num_ctx`** |

**32768 绝不下发**（§6/§7/§134 硬边界）。三条独立理由：

1. 语义不同：它是"预算别撑爆"的兼容回退，不是内存分配意图；
2. 实测风险不对称：`num_ctx=0` / `-1` 在真实 daemon 上 → **`context_length=4`**（畸形
   runner），而"不发"至少得到 daemon 的正常默认；32768 实测需 ~4.28GB VRAM；
3. `effective_context_window()` 的两个调用点（预算、预览拒绝）都是**进程内**判定，
   不产生外部副作用——把它接到 wire 上等于改变它的风险等级。

---

## 7. Ollama Protocol Decision

**切 native `/api/chat`**，依据（§32/§33 Gate）：

| Gate 判据 | 证据 |
|---|---|
| native semantics 确实需要 | `options.num_ctx` 是 runtime allocation 的**唯一**表达 |
| generic endpoint **不能**表达 | **实测**：`/v1/chat/completions` 带 `options.num_ctx=8192` → `/api/ps context_length=**4096**`（静默忽略）；顶层 `num_ctx` 同样无效 |
| streaming 可保持 | 实测 NDJSON：`{message:{content}}` 逐帧 + 终帧 `done:true`/`done_reason`/usage 字段 |
| tools 可保持 | 实测 native 返回 `message.tool_calls`（并行调用 `function.index` 0/1；`arguments` 是**对象**） |
| multimodal 可保持 | native `messages[].images` 被接受（当前 `supports_multimodal: false`，**不扩能力**） |
| regression 覆盖充分 | 3 个新 wire 级确定性测试文件 + 真实 daemon 旁证 + 7 条既有用例按新契约校正 |

**必须记录的后果**：`tool_choice` / `stream_options` / `response_format` / `extra_body`
在 native 无对应语义 ⇒ 丢弃（并警告一次）；`prefix: True` **显式拒绝**（静默丢字段会把
"续写"变成"重新回答"，两者都 200）。`max_tokens` / `temperature` 则**忠实映射**到
`options.num_predict` / `options.temperature`（native 的真实字段名）。

---

## 8. Provider Boundary Design（after）

```text
core/cognitive/loop.py / core/character/preview.py / preflight / debug / REST
        │  只看 generic 契约：LLMAdapter.stream_think / LLMFactory.context_window
        │  （零 ollama 分支、零 num_ctx、零 /api/ps）
        ▼
LLMAdapter ──► LLMFactory.create(model_id)
                    │  **唯一** provider→backend 选择点（canonical provider identity）
                    ├── OpenAICompatBackend   （deepseek / openai / mimo / custom）
                    └── OllamaBackend         （ollama：native /api/chat + num_ctx + usage 归一
                                               + runtime 诊断；raw_client = OpenAI 兼容侧车）
                                     │
                                     ▼
                          POST /api/chat { options.num_ctx = configured }
```

---

## 9. Backend Responsibility（`OllamaBackend` 做什么/不做什么）

| 承担 | 不承担 |
|---|---|
| request/wire translation（OpenAI 形状 → native，含 `images` 转写） | 对话策略 / 人格 / 记忆 |
| 运行时选项（`num_ctx` / `num_predict` / `temperature` / `keep_alive` 钩子） | prompt 预算策略（只**接收**已解析的 `context_window`） |
| response/usage 归一（终帧 `prompt_eval_count`/`eval_count` → 通用契约） | 会话生命周期 / 终态收口 |
| runtime diagnostics（`probe_runtime_context`，opt-in、失败不抛） | 任何热路径探活 |

**边界硬约束**：它只看到最终的 `messages[]` 与已解析的 `context_window`——**不读全局
YAML**（§79）、不 import `core`。

## 10. Factory Responsibility

`LLMFactory.create()` 只做三件事：**canonical provider 解析**、**参数解析**（base_url /
api_key / context_window，全部来自 `_BACKENDS` 条目）、**backend 构造**。它不碰 runtime
request 细节（`num_ctx` 怎么进 wire 是 backend 的事）。选择判据是
`model_id.split("/", 1)[0]`，**不是** URL 含 11434、模型名前缀或 localhost 启发式
（有反例守卫 `test_an_ollama_looking_url_does_not_change_the_protocol`）。

## 11. Generic Adapter Responsibility

`LLMAdapter` **零改动**：仍是 `think` / `stream_think` / `switch_model` / `get_raw_client`，
不感知 `num_ctx`，也不感知 provider 差异。`Module/llm/protocol.py` 的 `LLMBackend` 形状不变
（`num_ctx` **不在**通用接口上；`probe_runtime_context` 只存在于 ollama backend，generic
调用方拿不到也不需要）。

---

## 12. Pre-Fix Reproduction

**确定性（wire 级，`Module/llm/tests/test_ollama_context_authority.py`，修前 RED）**：

```text
FAILED test_configured_context_is_requested_on_the_native_wire
       → 实测出站路径 = /v1/chat/completions（期望 /api/chat），且请求体无 options
FAILED test_downshift_is_honored_not_hardcoded
       → 实测 options=None（期望 num_ctx=4096）
FAILED test_every_generation_entry_point_declares_the_context
       → /v1/chat/completions
```

**真实路径（`tmp/batch17/pre_fix_real.py`，走 `LLMAdapter` + 真 daemon，冷启动）**：

```text
configured  LLMFactory.context_window('ollama/qwen2.5:3b') = 8192   （budget 6144）
backend     OpenAICompatBackend
generation  ok（真实回复）
observed    GET /api/ps context_length = 4096      ← KB-13 现场
usage       {'input_tokens': 35, 'output_tokens': 20, 'total_tokens': 55, …}
```

⇒ **X（8192）≠ Y（4096）**：prompt budgeting truth 是假的，且 provider 不会报错。

**PRE_FIX_RED 的第二类（Challenger 发现，比 KB-13 更狠）**：协议切换当日，工具轮的
**第二发**请求在 native 上直接 **HTTP 400**（回放的 `tool_calls[].function.arguments`
是 JSON 字符串，而 native 要求**对象**）⇒ 工具**已经真的执行**（副作用、`tool.completed`
帧、会话写入都发生了），回复永不出现。真实 daemon A/B：修前 compat 第 2 轮 **200 +
"…22 degrees…"**；切换后 native 第 2 轮 **400** `Value looks like object, but can't find
closing '}' symbol`。修复见 §15.1，守卫见 §28 与变异 M8。

---

## 13. Real Ollama Evidence

**环境**：Ollama `0.24.0`；`qwen2.5:3b`；daemon 未配 `OLLAMA_CONTEXT_LENGTH`，service
definition 无 `num_ctx` override；每次冷启动前 `ollama stop` 并确认 `/api/ps` 为空。

### 13.1 冷启动 A/B（修复前后，同一路径）

| 截面 | configured | requested | observed | prompt_eval_count |
|---|---|---|---|---|
| **修前**（`OpenAICompatBackend` → `/v1`） | 8192 | **∅** | **4096** | 35 |
| **修后**（`OllamaBackend` → native，显式 8192） | 8192 | **8192** | **8192** | 35 |
| 修后 downshift（临时声明 4096） | 4096 | 4096 | **4096** | — |
| 修后 missing（删除声明） | ∅ | **∅** | 4096（daemon 默认） | — |

内存旁证（`size_vram`）：ctx=4096 → 2 390 300 672 B（≈2.28GB）；ctx=8192 → 2 541 295 616 B
（≈2.42GB）；ctx=2048 → 2 314 803 200 B；ctx=32768 → 4 281 147 392 B（≈4.28GB，
仅一次上限探针，**未**作为本批 normal path）。

### 13.2 warm runner 的 context 变化（§71/§72）

| 步骤 | 请求 `num_ctx` | `/api/ps context_length` | `load_duration` |
|---|---|---|---|
| 冷 | 4096 | 4096 | 1.88s（真加载） |
| warm | 8192 | **8192** | 2.19s（**重载**） |
| warm | 2048 | **2048** | 2.18s（**重载**） |
| warm（同值） | 8192 | 8192 | **0.11s**（复用） |

⇒ Ollama 0.24.0 **会按请求重载/调整 runner** ⇒ 不需要任何自动 unload/retry（§70），
本批也不实现；代价（context 变更 = 一次 ~2s 重载）如实记录。

### 13.3 silent truncation（重新验证，§18/§19）

`num_ctx=512` + 估算 3763 token 的 prompt ⇒ **HTTP 200**、`prompt_eval_count = 512`、
`done_reason = stop`、无 error；Elysia 侧 usage 归一为
`{'input_tokens': 512, 'output_tokens': 14, …}` ⇒
**"请求被接受" ≠ "prompt 被完整消费"**，且 `prompt_eval_count` 只作诊断（KB-12 边界外）。

### 13.4 opt-in 复现入口

```bash
RUN_EXTERNAL_TESTS=1 conda run -n Elysia python -m pytest \
    Module/llm/tests/test_ollama_real.py -q -s      # 7 passed
```

---

## 14. Root Cause

**双层**，缺一不可：

1. **语义层（KB-13）**：Elysia 把"声明窗口"当成"实际服务窗口"用，而 Ollama 的 serving
   context 是**请求侧运行时分配**；两者之间**没有绑定**，且超窗是 200 + 静默截断 ⇒
   provider error 无法替我们发现 mismatch。
2. **架构层（AR-16）**：provider-specific runtime semantics **没有任何归属地**——
   `num_ctx` 在全仓生产代码里出现 0 次；`if provider == "ollama"` 只散落在 preflight
   与 `api_key or "ollama"` 兜底里；通用 backend 的 `extra_body` 通道**在物理上**表达不了
   runtime context（compat 端点忽略它）。

⇒ 只加一个字段（"最小 patch"）**修不了**：必须先有"谁负责把 Elysia 的 context 意图翻成
该 provider 的 wire 语义"这个边界，才谈得上绑定。

---

## 15. Fix（before → after / why minimal）

| 维度 | before | after |
|---|---|---|
| 选择 | 所有 provider → `OpenAICompatBackend` | `provider == "ollama"` → `OllamaBackend`（唯一选择点，canonical identity） |
| wire | `POST /v1/chat/completions`，无 context 字段 | `POST /api/chat {options:{num_ctx: configured}}` |
| usage | `usage.prompt_tokens/completion_tokens` | `prompt_eval_count`/`eval_count` → 同一通用契约（终帧一次） |
| tools | OpenAI delta 形状（分片） | native 完整调用 → 归一成 OpenAI delta 形状（JSON 字符串 + index） |
| 诊断 | 无 | `probe_runtime_context()`：configured/requested/observed（opt-in） |
| 上层 | —— | **逐字不变**（loop / preview / preflight / debug / REST / 前端） |
| config | —— | **零 schema 变化** |

**为什么最小**：新增 1 个文件 + 1 处 20 行的选择分支；通用 backend、`LLMAdapter`、
PromptAssembler、CognitiveLoop、preview、preflight、provider test、REST、契约层**全部零改动**。
"再加一个字段到 compat 端点"的更大风险是**它根本不生效**（实测）——那才是真正的半成品。

---

### 15.1 P0 修补（Challenger D/C 独立发现同一处）

**形状**：响应方向有归一化（native 对象 → 通用 JSON 字符串），**请求方向没有反向翻译**。
通用契约里 `ToolCall.function.arguments` 是**字符串**（OpenAI 规范），
`core/cognitive/tool_phase.py::_prepare_tool_round` 把它 `model_dump()` 回放进下一轮
messages ⇒ native 400。

**为什么本批自己的测试没抓住**：原 `test_ollama_stream_contract.py` 里那条用例**名字**
叫 `roundtrip`，实际只测了"native → 通用"这一侧；wire 级假服务器对任何请求体都回脚本化
帧，所以回放方向从未被构造过。**教训**：`roundtrip` 这类命名会让人高估覆盖——已改名，
并补了逐层回放守卫（backend 级 + adapter 级 + 真实 daemon 级）。

**修复**（`_native_tool_arguments`，只做忠实转写，不发明语义）：

| 回放形态 | 处理 |
|---|---|
| 已是 dict | 原样 |
| `'{"city": "Paris"}'` / `'{"a": {"b": [1,2]}}'` / `'{}'` | `json.loads` → dict |
| `""` / 空白（无参工具的常态） | `{}` |
| 解不开的串 / JSON 非对象（`'[1,2]'`） | `{}` + **一次 warning**（不打死回合：这条消息只是上下文回放，工具结果在紧随其后的 `role="tool"` 消息里） |
| `None` | `{}` |

同时**透传** `tool_call_id` / `tool_name`（native 认得这两键，丢掉等于删信息）。

### 15.2 声明值的**统一解析**（独立复核发现的新失败模式）

同一个 `context_window` 有两个消费方，此前**各自解析**：预算侧 `int(window)`，运行时侧
拿**原样值**再比较。手写成带引号的 YAML（`context_window: "8192"`，而注释正鼓励用户手改
这个文件）时：预算照算 6144，而**每一次 ollama 生成**都在发请求前抛
`TypeError: '>' not supported between instances of 'str' and 'int'`
（Batch 17 之前该值从不离开 `int()` 化后的预算路径 ⇒ 这是**本批引入的**失败模式）。

修复：`Module/llm/registry.py::declared_context_window()` 成为**唯一解析点**，
两个消费方共用：`int` / 数字字符串 / `float` 接受；`None` / `≤0` / 布尔 / 非数字 ⇒ `None`
（= 未声明 ⇒ 预算走回退、运行时**不申请**）；不再抛 `ValueError`。布尔被显式拒绝
（`int(True) == 1` 会把预算退化成 1 token，且 `num_ctx=1` 被 daemon 拒绝）。
`create(..., context_window=N)` 也不再因显式形参 + `**kwargs` 撞成重复关键字。

## 16. `num_ctx` Semantics

- 来源**唯一**：`providers.yaml::llm.ollama.context_window`（经 `LLMFactory.create` 传入构造器）。
- 只在**显式且 > 0** 时下发；`≤0`/缺失 ⇒ 不发（§17）。
- 语义 = "请为该 runner 分配这么多 context"，**不是**"prompt 上限"、**不是**"模型能力声明"。
- 超大值**不设硬编码上限**（§41）：Ollama 自己夹取（0.24.0 实测 100000000 → 32768），
  分歧由诊断暴露；登记为 follow-up（§41）。
- 变更生效方式：warm runner 会**按请求重载**（§13.2）。

## 17. Missing-Context Semantics

采用 §9 **候选 A**：**不发 `num_ctx`**，并把"runtime 不受 Elysia 控制"如实表达为
`requested = None`（`probe_runtime_context()` 与 `configured_context`/`requested_context`
两个属性）。**不采用** B（provider default 字段：本仓没有可信的"ollama 运行时默认窗口"
来源，daemon 默认随版本/显存/模型变化）与 C（拒绝/警告：一条 metadata 缺失不该让可用配置
不可用，与既有 fail-open 家族冲突）。**绝不**"偷偷用 32768"。

## 18. `/api/ps` Role

**B — 诊断 / 验证**（§16-B）。出现在两处：`OllamaBackend.probe_runtime_context()`
（显式调用、失败不抛、返回 `error` 字段）与 `Module/llm/tests/test_ollama_real.py`
（opt-in 集成测试）。**不在** generation 热路径：`test_no_generation_entry_point_probes_the_runner`
逐入口断言 `paths == ["/api/chat"] * n`（变异 M6 验证过这条守卫真的会红）。

## 19. `keep_alive`

**C — 本批不暴露**（§26-C）。构造器有 `keep_alive` 形参但**默认 None ⇒ 不发该键**，
由 Ollama 自己的默认（本机 5m）管辖；**没有**任何 providers.yaml / UI / REST 面。
`_build_payload` 的转发有单测（显式传入才出现在 wire 上）。真实测试里用 `keep_alive: 0`
的语义（立即卸载）已验证过，但**不用**它做任何产品行为。

**独立复核的补充证据（支持协议切换）**：compat 端点**连 `keep_alive` 也忽略**
（实测 `"10m"` / `0` 都还是 daemon 默认的 ~300s），native 则如实生效
（`"10m"` → 599.8s、`0` → 3s 内卸载）。即 runtime 侧的"常驻控制"同样只在 native 可得。

## 20. Streaming Compatibility

- native NDJSON 逐帧 → 通用 `{"content", "tool_calls"}` chunk，**顺序不变**；
- 空 content 帧被跳过（不产出空 chunk）；
- 终帧 `done=true` 结束流；**没有** `[DONE]` 哨兵，也不需要（消费者只看 chunk 序列）；
- 流内 `{"error": ...}` 帧 → `RuntimeError`（不吞错）；
- HTTP 错误 → `httpx.HTTPStatusError`（`status_code` 可读）⇒ `LLMAdapter` 的重试映射不变；
- 与 OpenAI 兼容路径的差异（无 `finish_reason` 字段、无 usage chunk）**不影响**消费者：
  下游只用 `content` / `tool_calls` / 终帧 `usage`。
- 单测：`TestStreamNormalization`（顺序/终止/tool_calls/并行 index/错误帧）。

## 21. Usage Normalization

```text
prompt_eval_count → input_tokens
eval_count        → output_tokens
total_tokens      = input + output          （native 无 total 字段）
cached_tokens     = None   reasoning_tokens = None   （键恒在，与通用契约同形）
```

- **只从终帧取**（native 只有终帧带计数）⇒ streaming 不会重复计数（OL5；变异 M5 验证）；
- 两字段都缺失 ⇒ `last_usage = None`（**不编造** 0）；
- 实测同一 prompt 在 native 与 compat 上分别为 30/30（`prompt_eval_count` vs
  `usage.prompt_tokens`）⇒ 语义确实匹配。

## 22. Tool Call Compatibility

- `tools`（OpenAI function 形状）**原样透传**（实测 native 接受）；
- native 响应 → 通用 delta：`function.arguments`（**对象**）→ JSON 字符串、
  `function.index` → 顶层 `index`、`id`/`type` 保留；
- 并行调用 index 0/1 正确（实测 + 单测）；
- `tool_choice` **丢弃**（native 无该字段且忽略之）：Elysia 的 `tool_choice="auto"` 语义
  即"由模型决定"，与 native 默认一致 ⇒ 不改变 tool 决策结果；
- 端到端：`core/adapter/tests/test_ollama_stream_contract.py::TestOllamaToolCallsThroughAdapter`
  （经 `merge_stream_tool_calls` + `normalize_tool_calls` 得到合法 `ToolCall`）。

## 23. Multimodal Compatibility

当前 `providers.yaml::llm.ollama.supports_multimodal = false` ⇒ 上层**不会**把图片块发到
ollama（`message_builder` 按该位裁剪），故本批**不扩能力**。native 的 `images[]` 转写
（data URI → base64）已实现并有单测，属**忠实转写**（若将来有人把该位打开，图片不会被
静默丢掉），但**不声称**新增多模态支持。

## 24. Error Normalization

| 真实形态（实测） | 后端行为 | 上层结果 |
|---|---|---|
| 未知模型 → HTTP **404** `{"error":"model 'x' not found"}` | `httpx.HTTPStatusError`（`response.status_code=404`） | 不可重试 ⇒ `GENERATION_PROVIDER_UNAVAILABLE`（与修前同类） |
| 坏 JSON → HTTP **400** | 同上（400） | 不可重试 |
| 5xx / 429 | 同上 | `_extract_status` ∈ `_RETRYABLE_STATUS` ⇒ 既有退避重试 |
| daemon 不可达 | `httpx.ConnectError`（无 status） | 不可重试 ⇒ 与 `openai.APIConnectionError` 同层 |
| 流内 `{"error":…}` 帧 | `RuntimeError` | 回合失败收口（Batch 11 路径不变） |
| 200 但无 `message` 字段 | `RuntimeError`（**不折成空串**，O2 的教训） | 显式失败而非"空白回复" |

错误 taxonomy **未重做**（§61）。**独立复核实测的其它真实形状**（记录，不额外加代码）：

- `messages: []`（或缺 `messages`）⇒ **200** + `done:true` + `done_reason:"load"` +
  `content:""` 且 **usage 两字段缺失**（"只判 200"的客户端会渲染一个空白回合且不记 token）。
  Elysia 恒发 ≥1 条消息，故不可达；本批**不**为它发明语义。
- 坏 `keep_alive` ⇒ 400 `time: invalid duration`；坏 `tools` 形状 ⇒ 200 + **静默忽略**；
  `num_ctx` 传成字符串/布尔 ⇒ **HTTP 500**（不是 200）——这正是本批在 registry 边界
  **统一解析**（§15.2）而不是把原样值发出去的原因之一：否则一个带引号的 YAML 会变成
  可重试类 5xx 的重试风暴。

## 25. Cancellation / Resource Cleanup

- 流式响应由 `async with self._client.stream(...)` 持有 ⇒ 消费者 `aclose()` / 任务取消
  时上下文管理器退出并关闭响应；
- 单测 `TestOllamaStreamCancellation`：中途 `aclose()` 不抛，随后同一 backend 立即完成
  下一次生成（真的再发一次请求），无连接/任务残留 ⇒ Batch 9–15 的取消契约不回归；
- `close()` / `reconnect()` 幂等；`http_client_factory` 形态在 reconnect 时**重建全新**
  client（出站守卫跨重启保留，与通用后端同语义）；
- sidecar（`raw_client`）由 backend 持有并在 `close()`/`reconnect()` 关闭。

## 26. Prompt Budget Alignment

- chat 与 preview **都**继续读 `LLMFactory.context_window(self._llm.model)`（Batch 7 的
  PW1–PW6 权威未动、未新增第二套）；
- 本批只让**运行时侧**向同一值对齐，因此"预算 0.75× W"与"服务窗口 W"现在一致；
- `Module/system_prompt/budget.py` **零改动**（KB-12 的估算误差边界仍在）。

## 27. KB-12 Boundary

**本批没有解决 token estimation 误差**：`estimate_tokens` 仍是字符启发式（CJK 1 字 1 token、
其余 4 字符 1 token），对 CJK 偏高、对 JSON/代码偏低。Batch 17 只保证"**窗口**是多少"
（configured = requested = observed），不保证"**当前 prompt 用了多少**"估得准。
KB-12 **保持 OPEN**，留给 Batch 18。诊断上可把 `prompt_eval_count` 与估算并排打印
（本批在真实测试里这么做），但**不**用它反推截断位置、也不把它接进预算。

---

## 28. Regression Tests

### PRE_FIX_RED（修前真红，行为层）

`Module/llm/tests/test_ollama_context_authority.py` 三条（§12）：出站路径 = `/v1/chat/completions`、
请求体无 `options`、三条生成入口皆无 runtime context。**红点不是"类不存在"**——它只经
`LLMFactory.create` + `LLMBackend` 协议断言，修前即可运行并失败。

### ALREADY_GREEN_GUARD（修前偶然绿，修后成为真守卫）

- 未声明/0/负数 ⇒ 不发 `num_ctx`（OL2；变异 M2/M7 会红）
- 非 Ollama 请求体逐字段零污染（OL6；变异 M4 会红）
- 三条生成入口 1 generation = 1 request（OL4；变异 M6 会红）

### PRODUCT_CONTRACT_GUARD

- 协议与端点：native `/api/chat`（修前 compat → 红）
- 三入口一致；`system` role 与消息顺序不变；prefix **显式拒绝**（不静默丢）；
  探活**不申请分配**（避免一次探活触发大额显存分配）；
  远端 base_url 与 auth 头保持（§84/§86）

### FALSIFICATION_GUARD

- 指向 `localhost:11434` 的 OpenAI 兼容 provider **不得**被劫持成 native（§29/§30）
- 空 `content` 合法 vs 无 `message` 字段必须抛错（O2）
- 诊断失败不影响 generation；观测值不认错模型（`observed=None` 而不是别的模型的值）
- 流在终帧前断开 ⇒ **不编造** usage

### P0 / 独立复核新增的守卫（第二轮）

| 判据 | 位置 |
|---|---|
| 回放方向 `arguments` 必须是**对象**（精确用 `_prepare_tool_round` 的形状构造） | `test_ollama_backend.py::TestToolRoundReplay`（6 例，含形状参数化 + malformed warning） |
| 端到端回环：第 1 轮产出 → 归一化 → 回放 → 第 2 轮出站体 | `core/adapter/tests/test_ollama_stream_contract.py::TestOllamaToolRoundReplayThroughAdapter` |
| 声明值逐形态**两侧同结论**（`"8192"`/`8192.0`/`True`/`"abc"`/`[]`/`0`/`-1`/`0.5`） | `test_ollama_context_authority.py::TestDeclaredValueCoercion`（14 例 + override 1 例） |
| `observed` **不得**认成同族别的 tag（`qwen2.5:14b` ≠ 已加载的 `qwen2.5:3b`） | `test_ollama_backend.py::TestHotPathDiscipline`（2 例） |
| 提前 `aclose()` / 任务取消**当场**关闭 HTTP 流（`aclosing`，不等 asyncgen finalizer） | `test_ollama_backend.py::TestStreamClosure`（2 例，用自定义 `AsyncByteStream` 记录 `aclose`） |
| `close()` 之后 `raw_client` 不再偷偷新建无人持有的连接；`reconnect()` 换新侧车 | `test_ollama_backend.py::TestLifecycleAndHealth`（2 例） |
| in-stream / 200+error 帧必须被归为 **provider 层**（不是 INTERNAL） | `test_ollama_backend.py::TestStreamNormalization`（2 例，断言 `_is_llm_layer_error`） |

### 真实 daemon（opt-in）

`Module/llm/tests/test_ollama_real.py` **8 例**（§13 + 工具轮第二发）。
该模块要求**独占**该模型（`/api/ps` 是 daemon 级最后写入者胜的视图）：冷启动前提拿不到
时**显式 skip 并写明原因**（仓库既有约定），行为判据本身**不削弱**；`observed` 读取对
瞬时串扰有一次重发重试，稳定错值仍然失败。

---

## 29. Challenger Findings

四个独立 subagent（A 语义/消费者审计、B 真实 wire 复核、C 架构/隔离、D 消费者链回归）
+ main agent 亲自复现。**核心 finding 全部由 main agent 独立重放后才采纳**。

### 29.1 confirmed（真问题，已修）

| # | 发现 | 谁发现 | main 复现 | 处置 |
|---|---|---|---|---|
| **P0** | 回放的 `tool_calls[].function.arguments` 是字符串 ⇒ native **400** ⇒ 工具轮第二发必死（工具已执行、回复永不出现、400 不可重试） | C、D（独立同点） | ✅ 真实 daemon：dict/null ⇒ body 通过；**任何字符串（含 `""`）与数组 ⇒ 400** | 修（§15.1）+ 三层守卫 + 真实 daemon 用例 + 变异 **M8** |
| **NEW-1** | 声明值两侧解析不对称 ⇒ 带引号的 YAML 让每次生成抛 `TypeError`（本批**新引入**的失败模式） | A | ✅ 修前 `"8192"` ⇒ TypeError；`True` ⇒ 预算 1 token | 修（§15.2）+ 14 例参数化 + 变异 **M9** |
| **A2** | `_observed_context` 去 tag 比家族名 ⇒ `qwen2.5:14b` 会拿到已加载 `qwen2.5:3b` 的窗口（伪造匹配/mismatch） | C | ✅ 直接调用复现 | 修（精确同名；不带 tag 只接受 `:latest`）+ 2 例 |
| **A4** | `create(..., context_window=N)` 撞成重复关键字 TypeError | C | ✅ | 修（`kwargs.pop` 覆盖模式，与 `base_url`/`api_key` 同形）+ 1 例 |
| **B3** | 提前取消时 HTTP 流要等 asyncgen finalizer 才关（同 compat 旧形态，非新泄漏） | C | ✅ 用自记 `aclose` 的 byte stream 复现 | 修（`aclosing`）+ 2 例（含任务取消） |
| **C4** | `close()` 之后再取 `raw_client` 会新建无人持有的连接 | C | ✅ | 修（closed ⇒ `None`，用既有"不可用"契约）+ 2 例 |
| **M6-pre** | 热路径守卫只测 `stream_think`（变异 M6 存活） | main（变异） | ✅ 变异存活 | 改成逐入口断言（变异 M6 已死） |
| **M9-pre** | 变异脚本用错 parametrize node id ⇒ "节点不存在"被当成"红"（vacuous kill） | main（变异） | ✅ "no tests ran" | 变异脚本加 vacuous 防线 + 重挑敏感形态 |

### 29.2 fix weakness / fake-green（本批自己制造的，已收口）

- **`roundtrip` 命名高估覆盖**：一条叫 roundtrip 的用例只覆盖了响应方向 —— P0 正是从这条
  缝里漏出去的。已改名，并补"逐层回放"守卫（backend/adapter/真实 daemon）。
- **探活分配语义写错**（我自己在 §3.4 初稿里）：`/api/providers/test` 的 **usable** 级**会**
  申请分配、`--full` 对 ollama 直接跳过。已改正措辞并保留精确表述。
- **fake transport 对请求体"照单全收"**：wire 级假服务器对任何 body 都回脚本化帧 ⇒
  "请求体合法"这件事必须由**断言**承担（现在承担了：路径 + 字段集 + arguments 类型）。

### 29.3 采纳但**不**改代码（记录为边界/发现）

| 项 | 结论 |
|---|---|
| **B1** `Module/config/preflight.py` 两处 `if provider == "ollama"`（凭据/连通性，**先于本批存在**） | AR-16 的 closure 口径是"**runtime semantics** 不分散"；这两处不含 `num_ctx`/`keep_alive`/`/api/ps`。已登记 `KB-66`（可用既有 `local: true` 元数据替代），**本批不动** Batch 5 刚钉好的 capability 面 |
| **B-new-1** `raw_client` 侧车会**反复换掉**主路径 runner（compat 默认 4096 ⇒ native 8192 被降档，交替各付 ~2.3s 重载） | 强化 `KB-62`/`AR-18` 的描述（比"不受控"更重）。修法需要动 WorkflowEngine 的 SDK 形状契约 ⇒ 范围外 |
| **B-new-2** `/api/ps` 是 daemon 级视图 + 小值被夹（ps 读 4 而 llama 实分 256） | 写进不变量 §3.4 已知边界与模块 docstring；集成测试加独占前提 + 重试 |
| **B-new-3** Ollama 默认窗口随显存（4k/32k/256k），4096 非普遍事实 | 文档与 docstring 已明确"本机 6GB 的值"，不写成常数 |
| **B-new-4** 超窗截断**丢中间**（保最前 `num_keep=4` + 最近若干）⇒ 最先劣化的是 system prompt | 写进不变量 §3.4 与模块 docstring：这是"budget == num_ctx"**承重**的理由 |
| **B5** `messages: []` ⇒ 200 + `done_reason:"load"` + 无 usage；坏 `tools` ⇒ 200 静默忽略 | 记进 §24（Elysia 恒发 ≥1 条消息 ⇒ 不可达；不为它发明语义） |
| **A-new-2** vision 插件等**直接调 backend** 的路径不经过预算（ollama 上现在会带上 8192） | 登记 `KB-65`（先于本批存在的"无预算消费者"家族，本批让它可见） |
| **A-new-3** provider 级窗口 × 任意模型 id（无 model 级 allowlist） | 登记 `KB-67`（Batch 1.5 F9 的延续；本批让声明值**真的变成分配**，故后果加重） |
| **P1** SDK 层隐式重试（`max_retries=2`）随协议切换消失（流式仍有 adapter 级 3 次；`chat()` 现在 0 次；连接错误从不重试） | 登记 `KB-68`。**不**在 backend 里发明重试（§89：本批不重构 retry policy；仓库文档化的策略一直是 `LLMAdapter` 的那套，SDK 隐式重试不是契约） |
| **A-new-1 / B2** 并发客户端会污染 `/api/ps` 判据 | 集成测试加独占说明 + 冷启动显式 skip + 一次重发重试（判据强度不变） |

### 29.4 accepted risk（明示接受，不修）

- **`observed` 不是 allocation 精确账本**（小值夹取 + daemon 级视图）：它是"旁证"，
  **不是**热路径依赖，也不作为自动判据（除 opt-in 集成测试）。
- **native 与 compat 的关键字差异**（native 无顶层 `type`、`index` 嵌在 `function` 内、
  `arguments` 是对象）：由 backend 双向归一化吸收；**通用契约不变**，故上层不受影响。
- **协议切换的可见差异**（`tool_choice`/`stream_options`/`response_format`/`extra_body`
  丢弃并警告一次；`max_tokens`/`temperature` 映射到 native 字段；prefix 显式拒绝）。

## 30. Mutation Evidence

`tmp/batch17/mutations.py`（脚本把修复逐条打坏并断言**指定用例**变红；任一存活即非零退出）：

| ID | 变异 | 结果 | 被杀的守卫 |
|---|---|---|---|
| **M1** | 移除 `num_ctx`（回到 KB-13） | **KILLED** | OL-T1（wire 上必须有 num_ctx/在 native 端点） |
| **M2** | 用通用回退 32768 当 `num_ctx` | **KILLED** | OL2（missing / 0 / 单源一致性 3 例） |
| **M3** | provider 选择改成 URL 启发式 | **KILLED** | routing guard（canonical identity + 反例） |
| **M4** | Ollama options 泄进通用 backend | **KILLED** | OL6（openai + deepseek 逐字段） |
| **M5** | 每 chunk 发 usage | **KILLED** | OL5（backend 级 + adapter 级各一条） |
| **M6** | `/api/ps` 移进 generation 热路径 | **KILLED**（**首轮存活**，见下） | OL4（三条入口的请求数） |
| **M7** | `num_ctx` 硬编码 8192 | **KILLED** | 4096 downshift + missing/非法值 |
| **M8** | 回放方向 `arguments` 不翻译（保持字符串） | **KILLED** | 工具轮回放（backend 级 + adapter 级） |
| **M9** | 声明值不做统一解析（运行时侧拿原样值） | **KILLED**（**次轮才真杀**，见下） | 带引号 YAML 形态 + 布尔形态 |

**M9 的两轮**：首轮挑的形态是 `8192`（int）——对"原样透传"这个变异**不敏感**，于是存活；
换成 `"8192"`（带引号）与 `True` 后被杀。**同轮还发现变异脚本自身的假绿**：写错的
parametrize node id 让 pytest 报 "no tests ran"，旧判据把它当成"红"⇒ 记为 KILLED。
脚本已加 vacuous 防线（节点不存在/未选中 ⇒ 判失败）。这两件事都是本仓"假绿"家族的实例：
**变异测试自己也会假绿**。

**M6 首轮存活**是一次真实的"守卫没装在改动上"：原守卫只测 `stream_think`，而变异加在
`chat()` ⇒ 测试仍绿。已改成**逐入口**断言（`paths == ["/api/chat"] * n`），重跑后 KILLED。
这条记录保留，因为它正是本仓"假绿"家族的实例（守卫范围 ≠ 改动范围）。

## 31. Non-Ollama Isolation Proof

1. **请求体逐字段**：`openai/gpt-4o` 的 body 字段集恒为 `{model, messages, stream}`；
   `deepseek/deepseek-v4-flash` + extra_body 恒为
   `{model, messages, stream, stream_options, thinking}`——**零** `options`/`num_ctx`/`keep_alive`
   （变异 M4 证明这两条会红）。
2. **路径**：两者仍走 `/chat/completions`。
3. **选择**：`mimo/<model>` + 指向 `http://localhost:11434/v1` 的 base_url ⇒ 仍是
   `OpenAICompatBackend`（URL 启发式反例）。
4. **代码面**：`Module/llm/protocol.py`、`core/adapter/llm_adapter.py`、`Module/llm/openai_compat.py`
   **零改动**；全仓生产代码新增 `ollama` 相关分支**只在** `registry.create`（选择）与
   `Module/llm/ollama.py`。
5. **`custom` provider**：SSRF 守卫、参数解析、构造路径不变（既有用例全绿）。

## 32. Performance / Hot-Path Impact

- **无额外网络往返**：热路径 1 user generation → 1 provider request（守卫 + 变异 M6）。
- **无 `/api/ps`**：诊断是显式调用。
- **请求翻译成本**：一帧 NDJSON 解析 + 一次 dict→dict 转写（微秒级）。
- **首 token 延迟**：修复前后同一 prompt 的 `prompt_eval_count` 均 35；
  真实测试 7 例（含 5 次模型加载/重载）合计 **20.5s** ⇒ 未观察到 adapter 架构带来的
  额外延迟；context 变更的一次性重载代价 ~2.2s 如实记录（warm 同值复用 0.11s）。
- **不做**大 benchmark（§98）、不做 VRAM 自动调优（§42）。

## 33. Public API Impact

**零**：`LLMBackend` 协议（`chat`/`chat_stream`/`stream_think`/`test`/`close`/`raw_client`）
形状不变；`LLMFactory` 的公开方法（`create`/`from_config`/`context_window`/
`supports_multimodal`/`is_implemented`）签名与语义不变；REST/WS 契约层与前端**未触碰**；
`/api/providers/test` 响应结构不变（诊断面留在 backend，未扩 API）。

## 34. Config Schema Impact

**零**：不新增/不删除/不改名任何配置键。`Module/config/providers.yaml` 的 ollama 条目
**只有注释**被修正（原文"qwen2.5:3b 的 num_ctx 默认 8K／只影响截断时机，不影响正确性"
已被本批证据反驳）。已有用户配置（`llm.model: ollama/x`、`OLLAMA_URL`、`context_window`）
**无需任何改动**即获得正确语义（§83）。`Module/llm/config.py`、`core/config/app_config.py`、
`frontend_contract` 均未改。

## 35. Commits

| Hash | 类型 | 内容 |
|---|---|---|
| `11755751` | `refactor(llm)` | 新增 `OllamaBackend`（native `/api/chat`）+ 工厂按 canonical provider 选择 + wire 级 PRE_FIX_RED 转绿 + 3 个新测试文件 + 7 条既有用例按新契约校正 |
| `06c0351d` | `fix(llm)` | **P0**：回放方向的 `tool_calls.function.arguments` 忠实转写成 native 对象（+ `tool_call_id`/`tool_name` 透传）+ 三层守卫 |
| `fcb97693` | `fix(llm)` | 独立复核加固：声明值单点解析、`observed` 精确匹配、`aclosing`、`raw_client` after close、provider 层归因、文档更正、真实测试抗串扰 |
| （docs 提交） | `docs(architecture)` | 不变量 OL1–OL6（+ §3.4 已知边界 / accepted risk #3 / §7 更新）+ provider 文档（README / architecture.md / config README / providers.yaml 注释 / CLAUDE.md） |
| （docs 提交） | `docs(review)` | 本执行记录 |
| （docs 提交） | `docs(todo)` | KB-13 → GUARDED、AR-16 → CLOSED、PD-12 裁决、OBS-2 更正、新发现 KB-61..68 / AR-18 / AR-19 登记 |

## 36. Worktree Verification

`git status --short` 在本批结束时**干净**（无残留变异、无临时文件入库；证据脚本都在
`tmp/batch17/`，该目录被 `.gitignore` 忽略且被 `pytest.ini` 的 `norecursedirs` 排除）。

## 37. Test Results

### 37.1 定向（本批直接相关）

| 套件 | 结果 |
|---|---|
| `Module/llm/tests/`（含 3 个新文件） | **142 passed, 8 skipped**（skip = `external` 未 opt-in） |
| `core/adapter/tests/`（含新回环守卫） | **44 passed** |
| `core/prompt/` + `core/character/tests/test_preview.py`（Batch 7 保护） | 全绿（窗口权威未动） |
| 真实 Ollama（`RUN_EXTERNAL_TESTS=1 Module/llm/tests/test_ollama_real.py`） | **8 passed**（25s；冷启动 8192 / downshift 4096 / missing 不申请 / warm 4096→8192→4096 / 静默截断 512 / 工具轮第二发 / 诊断失败不影响 generation / 连接失败不撒谎） |
| 变异（`tmp/batch17/mutations.py`） | **M1–M9 全 KILLED**（含 M6/M9 的两轮，见 §30） |

### 37.2 档位与门禁

| 档位 | 结果 |
|---|---|
| `make test-quick` | **6391 passed, 2 skipped, 1 xfailed**（81.6s） |
| `make test-normal` | **6391 passed, 2 skipped, 1 xfailed**（81.6s；Normal 与 Quick 仍相等） |
| `make test-remainder` | 0 条（文档化状态：Normal − Quick = ∅） |
| `make test-full` | **6423 passed, 66 skipped, 1 xfailed**（469s） |
| `make test-real-mongo` | **46 passed, 6444 deselected**（48s；真实 MongoDB 8 容器） |
| `make check-test-sets` | 通过（`|Quick ∪ remainder| == |Normal|`） |
| 前端 `npm run build` | ✅（875ms；**零前端改动**，产物不入库） |
| `make lint` / `make pyright` | ✅ / **0 errors, 0 warnings** |
| `check-arch` / `check-contract` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | 全部 ✅ |

### 37.3 真实 MongoDB 层

本批**无 Mongo 改动**；按项目要求仍跑（`docker compose -f docker-compose.test.yml up -d`
→ `make test-real-mongo` → `down`）：**46 passed**，零回归。

### 37.4 真实 CI

真实 CI 需要 owner push（本仓 push-only + self-hosted，PR 侧不跑）。本批在本地跑齐了
与 CI 等价的全部判据（§37.2 + §37.3）；push 后需核对 `backend` / `real-mongo` /
`frontend` / 静态 jobs 全绿——**这是唯一必须在 push 之后才能报告的项**。

### 37.5 前端

**零前端改动**（`frontend/`、`frontend_contract/`、`static/` 均未触碰；构建产物不入库）。
仍按要求跑了 `npm run build`：✅（875ms，退出码 0）。

## 38. Invariant Mapping

| ID | 断言 | 判据 |
|---|---|---|
| **OL1** | 显式声明窗口的 Ollama 模型：预算与 `num_ctx` 取同一值（同一 `_BACKENDS` 条目） | `test_ollama_context_authority.py`（2 例） |
| **OL2** | 通用预算回退**绝不**成为运行时分配（缺失/非法 ⇒ 不发） | 同文件（5 例，含参数化 0/-1/-8192） |
| **OL3** | Ollama 语义只在 provider backend 边界内 | 全模块经 generic 入口断言 + `make check-arch` |
| **OL4** | configured/requested/observed 可区分；observed 不在热路径 | `test_ollama_backend.py::TestHotPathDiscipline`（5 例） |
| **OL5** | usage 恰好一次归一；终帧缺失不编造 | backend 2 例 + adapter 1 例 |
| **OL6** | 非 Ollama 请求体/语义零变化 | 3 例（openai/deepseek/URL 反例） |

（完整表见 [`docs/architecture/invariants.md`](../architecture/invariants.md) §3.4。）

## 39. Todo Update

| ID | before | after |
|---|---|---|
| **KB-13** | `OPEN`（已确证） | **`GUARDED`**（不变量 OL1–OL6 + 变异全杀 + 真实 daemon 旁证；残余边界见 §3.4 已知边界） |
| **AR-16** | `DEFERRED` | **`CLOSED`**（语义收口 + 上层零泄漏 + 选择明确 + 非 Ollama 不受影响 + tests） |
| **PD-12** | `PRODUCT/ARCHITECTURE DECISION REQUIRED` | **已裁决：A（effective runtime window）**，零 schema 变化 |
| **OBS-2** | `OPEN`（"`context_window` 是估算值，只影响截断时机，不影响正确性"） | **注释与结论已更正**（对 ollama，"只影响截断时机"是错的；数值仍是手写声明值 ⇒ 校准意义仍存） |

## 40. Keep As-Is

- `CognitiveLoop` 保持 generic（零 ollama 分支）；
- `PromptAssembler` / `MessageBuilder` / `system_prompt/budget.py` 保持 generic 且零改动；
- Batch 7 的 preview window protection（PW1–PW6）保持不变、不产生第二套 authority；
- 现有 `LLMBackend` 通用接口不变（`num_ctx` 不外露）；
- **不做** per-generation `/api/ps`；
- **不做** GPU/VRAM 自动调优、不做硬件启发式；
- **不做** tokenizer（KB-12 边界）；
- **不做**前端改版、**不做** provider-per-class 机械重写。

## 41. New Findings（只登记）

| ID | 内容 | 类型 |
|---|---|---|
全部登记在 `todo.md`（canonical ID）：

| ID | 内容 | 来源 |
|---|---|---|
| **KB-61** | `num_ctx` 上限夹取在 Ollama 侧（声明高于 32768 时 configured > observed 再分叉，只由诊断暴露）；本批按 §41 不加硬编码上限 | main |
| **KB-62** | 工作流 task LLM 路径仍不受控（`raw_client` 侧车），且会**降档重载**主路径 runner（实测每步 ~2.3s） | main + 独立复核 B |
| **KB-63** | `probe_runtime_context()` 无任何报告面（刻意：不扩 API / 不让 `/debug` 触发昂贵探测） | main + 独立复核 A |
| **KB-64** | 覆盖缺口：`__main__.py` smoke 仍只覆盖 deepseek；daemon 默认窗口的确定规则未测 | main |
| **KB-65** | 直接调 backend 的路径（vision / consolidator / reflector / emotion / awareness）**不经过预算** | 独立复核 A |
| **KB-66→AR-19** | `preflight.py` 两处 `if provider == "ollama"`（凭据/连通性，先于本批存在；可用既有 `local: true` 替代） | 独立复核 C |
| **KB-67** | `context_window` 是 provider 级却作用于任意模型 id（F9 延续；本批让声明值**真的变成分配**） | 独立复核 A |
| **KB-68** | 协议切换后 SDK 隐式重试消失（`chat()` 现在 0 次；连接错误从不重试）；本批不重构 retry policy | 独立复核 D |
| **AR-18** | 双协议并存的架构张力（native 生成 + compat 侧车），"ollama 窗口是多少"必须先问"哪条路径" | main |


## 42. Follow-up

保留 canonical backlog 原样（KB-12 / KB-17 / KB-29 / KB-30 / KB-32-33 / KB-39-45 /
KB-48-60 / KB-18-21 / OBS-6-8 / KB-11 / GR-25 / N-15 等），本批**不改**其状态；
下一批建议：**Batch 18 — Context / Token Estimation Correctness（KB-12）**。
