# Batch 7 — Provider Preview Window Correctness（F8）执行记录

> 范围：canonical backlog 仅 **F8**。本批**不**处理 P2-01r / KB-6 / OBS-6 / KB-11 / GR-25 /
> N-15 / E1 cache freshness 等其它条目。
>
> 结果：**F8 = GUARDED**（生产修复 + 路由级 PRE_FIX_RED + 不变量 PW1–PW6）。
> 同时**修正了 F8 原始证据的两处事实错误**（见 §2.3），并登记 3 条 `NEW_FINDING_OUT_OF_SCOPE`。

---

## 0. 结论摘要

| 问题 | 回答 |
|---|---|
| F8 确认？ | **确认**（结构缺陷：预览是第二条 prompt 组装路径，从未接上 canonical 预算） |
| 复现证据级？ | **E1 + E2（路由级）+ E3（真实 provider）**；E3 **改写了症状描述**（§2.3） |
| Root cause？ | **cross-path omission**（不是硬编码 output budget、不是错 metadata、不是算术错误） |
| 修好了？ | **是**：预览按 resolved runtime model 的窗口取 canonical 预算；裁完仍占满窗口 → 本地 400（零 provider 请求） |
| 公开 API 变化？ | **无**（请求/响应 schema、错误码集合都不变；复用既有 400 `CHARACTER_VALIDATION`） |
| 网络/计费变化？ | **无**（一次预览仍最多一次 generation 请求；无 retry、无 probe；E3 用的是本机 ollama，免费） |
| 真实 CI？ | 见 §20（push 后核对 backend / real-mongo / frontend 全绿） |

---

## 1. Scope Lock

**允许面**（全部只读/只改这些）：`core/character/preview.py`（预览推理口）、
`core/character/service/queries.py`（错误映射）、`Module/system_prompt/budget.py`
（共享回退点）、`core/input/tests/test_character_preview_window.py`（新守卫）、
`core/prompt/tests/test_prompt_budget.py`（+1 单元用例）、`docs/architecture/invariants.md`、
`todo.md`、本文。

**刻意未碰**：`PromptAssembler`、`SystemPromptManager`、`MessageBuilder` 的截断算法、
`core/cognitive/loop.py`（chat 路径）、`providers.yaml`（见 §21/KB-13）、
全局 token 预算、契约层（`frontend_contract/`）、前端仓（`/home/yomu/elysia-frontend`）、
provider registry/adapter。

---

## 2. F8 原始证据与 HEAD 复核

### 2.1 canonical 登记（`todo.md` §1.2，Batch 4 triage 改写后）

> prompt 预算 cross-path：`core/character/preview.py` 调 `build_messages` **不传**
> `prompt_budget_tokens`（chat 路径传）⇒ 预览路径可超 context → provider 400。
> **实测（Challenger）：内置角色预览 system prompt ≈9283 est tokens，ollama 窗口
> 8192/预算 6144 ⇒ 小窗口 provider 上 100% 复现**（USER / MEDIUM / E1+E2）。

### 2.2 HEAD 复核：代码事实成立

```
core/character/preview.py:75-80   build_messages(user_message=…, user_name=…,
                                                user_pronoun_override=…,
                                                user_timezone_override=…)   ← 无 prompt_budget_tokens
core/cognitive/loop.py:793-802    prompt_budget = budget_from_context_window(
                                      LLMFactory.context_window(self._llm.model))
                                  build_messages(…, prompt_budget_tokens=prompt_budget)
```

历史（`git log -S`）：预算机制 `076ebee8`（2026-09-16）晚于预览落地 `21c6d194`
（2026-09-10）6 天，且只接了 chat 一处调用点 ⇒ 预览**从未迁移**。
**这不是** "preview 硬编码了过大的 `max_tokens`"：预览**根本不发** `max_tokens`
（`think(messages)` → `backend.chat(messages)`，`kwargs={}`，实测）。

### 2.3 原始证据的两处事实错误（本批现测修正）

| 原记 | 实测（HEAD，仓内 estimator） | 判定 |
|---|---|---|
| 内置角色预览 system prompt ≈ **9283 est tokens** | 9235 **字符** = **7597** est tokens | 数字量级对、**单位错**（报的是字符数） |
| 小窗口 provider 上 **100% 复现** | 内置 elysia + 默认 11 字消息 = 7614 ≤ 8192 ⇒ **200**；+703 字 = 8305 > 8192 ⇒ 超窗 | **不成立**：真实边界是"预览消息 > ~575 CJK 字"（内置 elysia 的余量） |
| provider **400** → HTTP 503 | **真实 ollama 0.24.0 不返回 400**：超窗输入 → **HTTP 200 + 静默截断**（§2.4） | **症状改写**：400 属"强制窗口"类 provider（严格 OpenAI 风格），不是本地 ollama |

> 结论：**缺陷真实且 USER 可达**（小窗口 provider 上稍长的预览消息就超声明窗口），
> 但原登记的**频率与症状描述都不准确**。本批以现测为准确认，不沿用旧数字。

### 2.4 E3：真实 provider 实测（本机 ollama 0.24.0 / qwen2.5:3b）

真实路由 `POST /api/characters/preview` + 真实 `LLMAdapter`（非 stub）：

| 用例（HEAD，修前） | 声明窗口 | 估算输入 | provider 观测 | 路由 |
|---|---|---|---|---|
| 内置 elysia + 默认消息 | 8192 | 7614 | 200，reply 31 字 | **200** |
| 内置 elysia + 703 CJK 字 | 8192 | **8305 > 8192** | 200，reply 15 字（prompt 被 provider 截断） | **200** |
| 裸 `/api/chat`（本仓代码之外）1000 字 | — | 1005 | 200，`prompt_eval_count=1029` | — |

### 2.5 E3 补充复测（主 Agent 独立于 Challenger，同一 provider）

探针：`tmp/batch7/ollama_probe_now.py` + `tmp/batch7/ollama_default_ctx.py`（均在本机新进程内跑，
`httpx(trust_env=False)` 直连 `127.0.0.1:11434`，或走项目自己的 `LLMAdapter`）。

| # | 请求 | 观测 | 说明 |
|---|---|---|---|
| 1 | 项目 `LLMAdapter`（**预览实际走的路径**）1000 CJK 字，无 `max_tokens` | 200；provider `input_tokens=1029`（仓内 estimator=1005，**低 2.4%**） | estimator 对纯 CJK 是可靠的保守近似 |
| 2 | 同上 9000 字 | 200，reply 3513 字；**provider usage 全 0** | 异常（provider 未给计数），未追；见 §24 KB-12 |
| 3 | native `/api/chat` 6000 字，**不传 `num_ctx`** | 200，`prompt_eval_count=**4096**` | **默认服务窗口 = 4096**（声明是 8192）← KB-13 的关键数，主 Agent 亲测 |
| 4 | native `/api/chat` 6000 字，显式 `num_ctx=4096` | 200，`prompt_eval_count=4096` | **超窗 = 截断，绝不报错** |
| 5 | native `/api/chat` 9000 字，显式 `num_ctx=8192` | 200，`prompt_eval_count=8192` | 同上，第二个截面 |
| 6 | native `/api/chat` 6000 字，显式 `num_ctx=8192` | 200，`prompt_eval_count=6029` | **对照成立**：未超窗时不截断 |
| 7 | native `/api/chat` 3000 字，显式 `num_ctx=8192` | 200，`prompt_eval_count=3029` | 同上 |
| 8 | 裸 `/v1/chat/completions` 500 字 | 200，`prompt_tokens=529` | OpenAI 兼容端点小请求可用 |
| 9 | 裸 `/v1/chat/completions` 3000 / 8000 / 20000 字 | **ReadTimeout**（多次） | 见下"局限" |

**结论（三个截面一致）**：ollama 0.24.0 在超窗时**截断并返回 200**，从不 400；因此
F8 原始登记里的 "provider 400" 在**该 provider 上复现不出来**，属"强制窗口"类 provider
的行为。同时 **声明窗口 8192 ≠ 实际默认服务窗口 4096**（第 3 行，主 Agent 亲测；
Challenger 亦独立测得同一数值）。

**局限（必须说清）**：
1. 本机裸 httpx 的 `/v1/chat/completions` 在 ≥3000 字时**稳定 ReadTimeout**，
   故 Challenger 报的 `/v1 prompt_tokens=4096` **主 Agent 未独立复现**；改用 native
   端点的显式 `num_ctx` 截面（第 4/5 行）得到同向且更强的结论。
2. 第 2 行那个 "usage 全 0" 未诊断——它既是 KB-12"改用 provider tokenizer"的现实障碍
   （provider 有时不给计数），也说明 provider usage 不能当唯一权威。
3. E3 属本地旁证，**不进 CI**（本机 ollama 不在任何测试档位内）。

因此 F8 的**真实用户可见症状**分两类，本批两条都处理：
- **强制窗口类 provider**（严格 OpenAI 风格 / 云端）：超窗 → provider 400 → 路由折成
  **503 `CHARACTER_PREVIEW_FAILED` + retryable**（"稍后重试"是谎报）。E2 用窗口约束
  stub 复现（§11），本机无此类 provider 可做 E3。
- **截断类 provider**（本机 ollama）：超窗 → 200 但 prompt 被截断 ⇒ 预览人设被悄悄削掉。

---

## 3. Preview Contract（产品语义）

证据：Stage H 设计稿 §4（`docs/review/2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md`）、
`frontend_contract/rest.py:1078-1102`、前端 `CyCharacterPreview.vue`（在**另一仓**
`/home/yomu/elysia-frontend`）。

- **目标**：对**未保存的 draft** 做**一次**"示例回复"推理 —— `{draft, userMessage}` →
  `{ephemeral:true, reply, model, usage:null}`；**零副作用**（不建 session/instance、
  不写 archive、不读历史/LTM、不挂工具、不触发 TTS/mood）。
- **不承诺**：模型可用性；也**不暴露任何生成参数**（无 max tokens / temperature /
  model 选择器；`userMessage` wire 上限 20000，UI 上限 4000）。
- 由此确定"窗口不足时该怎么办"：**不能悄悄改小输出预算来蒙混**（没有这个字段，
  也不该发明），只能 ①按 canonical 预算裁剪；②裁完仍不可能 → 明确报错。
- 错误契约（设计稿 §4.5）：draft 无效 → 400 `CHARACTER_VALIDATION`；provider/运行时
  **暂时**不可用 → 502/503 `CHARACTER_PREVIEW_FAILED`（可重试）。

---

## 4. 修前请求链（数值）

```
前端「生成预览」→ POST /api/characters/preview  {draft, userMessage}
  → character_routes._characters_preview（frontend_contract 严格校验）
  → CharacterService.preview（校验 draft / 编译内存 profile / 身份快照）
  → PromptCharacterPreviewAdapter.generate（core/character/preview.py）
      ├─ model_id = llm.model = "ollama/qwen2.5:3b"     （resolved runtime model）
      ├─ PromptAssembler(session=None, long_term=None).enable_preview_mode()
      └─ build_messages(...)            ← **不传 prompt_budget_tokens（缺陷）**
  → LLMAdapter.think(messages)  → backend.chat(messages)   kwargs = {}（无 max_tokens）
  → provider
```

| 量 | 值（内置 elysia + 703 CJK 字，修前） |
|---|---|
| configured/bootstrap model | `deepseek/deepseek-chat`（`config.yaml`） |
| resolved runtime model | `ollama/qwen2.5:3b` |
| declared contextWindow | 8192（`providers.yaml::llm.ollama`） |
| canonical budget | 6144（= 8192 × 0.75） |
| system prompt 子预算 | 3072（= 6144 × 0.5） |
| 实际 system prompt | **7597** est tokens（9235 字符） |
| preview input（system+user） | **8305** est tokens |
| requested output tokens | **未设置**（provider 默认） |
| effective total | 8305 + provider 默认输出 |
| provider 结果 | 强制窗口类：**400** → 路由 **503 retryable**；ollama：200 + 截断 |

---

## 5. 约束模型（哪些真的参与）

| 约束 | 参与？ | 依据 |
|---|---|---|
| context window（声明值） | **是**（唯一可用权威） | `providers.yaml::llm.<provider>.context_window` → `LLMFactory.context_window`（provider 级，无 model 级粒度） |
| max input tokens | 同上（本仓无独立字段） | — |
| max output tokens | **否**（本仓 chat/预览都不设；无 model 级 output cap metadata） | `grep max_output\|maxOutputTokens\|max_completion_tokens\|num_predict` 生产代码 0 命中 |
| provider 全局限制 | 未建模 | — |
| preview 专属 requested output | **不存在**（无字段、无硬编码） | 契约 + `think()` kwargs 实测 `{}` |
| API 字符限制 | 否 | — |
| image/token 预算 | 否（预览不带图） | `supports_multimodal=False` 路径 |
| provider 私有限制 | 未建模（KB-13 记 ollama 的 num_ctx 截断） | — |

---

## 6. Authority Map（before → after）

| Concern | Before（HEAD） | After（本批） |
|---|---|---|
| preview provider | `LLMAdapter`（构造期注入，未变） | 同左 |
| preview model | `LLMAdapter.model`（resolved runtime，未用于预算） | `LLMAdapter.model` → **预算权威** |
| context window | 预览**不读**（chat 读 `LLMFactory.context_window`） | 预览与 chat 同源：`LLMFactory.context_window(model)` |
| output token cap | 无（chat 与预览皆无） | **无变化**（刻意不新造） |
| input token count | 预览**不计算** | `Module.system_prompt.budget.estimate_messages_tokens`（既有唯一估算点） |
| preview default output | 无（provider 默认） | 无变化 |
| 窗口回退（未声明/非法） | 仅 chat 侧隐式（`x or DEFAULT`，负数会算出退化预算） | `budget.effective_context_window()` 单点定义（None/0/负数 → 32768） |

---

## 7. Root Cause

**H. 其它 → 精确说是 cross-path omission（跨路径漏接）**：
预览是**第二条 prompt 组装路径**，在 canonical 预算机制（S2-H9 / `076ebee8`）落地
**之前**写成，之后没有任何一处把它接上；chat 路径接了。判据：
- 不是 A（硬编码过大 output budget）：预览**不发** `max_tokens`（实测 kwargs `{}`）。
- 不是 B（用了错 metadata）：预览**完全没用**任何窗口。
- 不是 C（只考虑 output）：预览既没算 input 也没算 output。
- 不是 D（contextWindow metadata 错）：即便用正确的 8192，修前也不裁剪。
  （metadata 与真实 served 窗口的偏差是**另一条**既有问题 → KB-13，不在本批修。）
- 不是 E（provider API 参数语义错）。
- 不是 F（复制了主聊天默认参数）：**恰恰相反，它没复制那份守卫**。

---

## 8. Fix（before → after → 为什么最小）

| 文件 | before | after |
|---|---|---|
| `core/character/preview.py` | `build_messages(...)` 无预算；直接 `think()` | 取 `model_id = self._llm.model` → `window = LLMFactory.context_window(model_id)` → `prompt_budget_tokens=budget_from_context_window(window)`；组装后 `estimate_messages_tokens(messages)`，`>= effective_context_window(window)` ⇒ 抛 `PreviewPromptTooLargeError`（**不发** provider） |
| `core/character/service/queries.py` | 任何 port 异常 → 503 `CHARACTER_PREVIEW_FAILED`（retryable） | 新增 `except PreviewPromptTooLargeError` → 400 `CHARACTER_VALIDATION`（非重试）；其余异常语义不变 |
| `Module/system_prompt/budget.py` | 回退规则内联在 `budget_from_context_window` | 抽出 `effective_context_window()`（None/0/负数 → 32768），预算改用它 —— 让"按窗口判定"的调用方与预算**取同一个回退值** |

**为什么最小**：只改一条组装路径 + 一处错误映射 + 抽出一个已存在的回退规则；
复用既有 estimator / 既有 assembler / 既有错误码 / 既有常量。**没有**新增
preview 专属默认值、没有第二份 contextWindow 表、没有新契约字段、没有 retry、
没有额外 provider 请求、没有动 chat 路径。

**Decision record（为什么不做那些"看起来更彻底"的改法）**：
- **不设 preview 专属 `max_tokens`**：契约无此字段；chat 也不设；输出余量由
  0.75 headroom 承担。§24 明确"仓库没有该 metadata 就不要为完整性新造"。
- **不改为"永远先发再降级重试"**：§36 禁止（多计费、隐藏错误、语义不可预测）。
- **不修 `providers.yaml` 的 ollama 窗口**：它会**全局**改变 chat 的截断时机
  （§0 禁止全局 token 预算重构），且会让内置角色在 ollama 上从"被截断"变成
  "本地直接拒绝预览"——更糟。→ KB-13 登记，交由 owner 决策。
- **不把本地判定说成精确**：判据是启发式 estimator（见 §9 与 KB-12）。

---

## 9. Boundary Semantics

| 情形 | 处理 | 测试 |
|---|---|---|
| 估算输入 < 窗口 | 发（provider 1 次） | `::test_preview_boundary_sends_when_fits_and_validates_when_not`（`fits == window-1`） |
| 估算输入 **== 窗口** | **本地 400**（"装得下但没有输出余量"） | 同上（`over == window`，provider 调用数仍为 1 ⇒ **未发**） |
| 估算输入 > 窗口 | 本地 400，零 provider 请求 | `::test_preview_impossible_input_fails_locally_without_provider_call` |
| 未声明窗口（第三方 provider） | 兼容 fail-open：与预算同源的保守默认 32768 | `::test_preview_undeclared_window_keeps_compat_policy` |
| 窗口 0 / 负数 | 同"未声明"（回退 32768），不产生退化预算 | `core/prompt/tests/test_prompt_budget.py::TestEstimation::test_effective_window_is_the_shared_fallback` |
| 窗口 < 保护节下界（~2.1K） | 任何预览都本地 400（诚实拒绝；不发必然被截断/拒绝的请求） | 同 PPW-4 用例（Challenger A 记录该退化区间） |
| 估算器误差带 | **accepted risk**：CJK 偏高 / JSON·代码偏低 ⇒ 双向误判可能 | 无（需真 tokenizer；KB-12） |

---

## 10. Normal Chat Impact

- **行为**：现有配置下 `core/cognitive/loop.py` 的 prompt 组装**逐字不变** —— 预算公式、
  常量、回退值（None → 32768）都相同；唯一变化是 `effective_context_window()` 抽成函数。
- **唯一语义变化**：`context_window <= 0`（含负数）从"已声明"改为"回退默认"。
  仓内**没有任何配置**声明 0/负数（`providers.yaml` 六家均为正），且
  `LLMFactory.context_window` 对 falsy 值已返回 None ⇒ 该分支**不可达**。
  这是 hardening，不是策略变更；已在 `invariants.md` PW6 写明（Challenger G 提出）。
- **未触碰**：`PromptAssembler` / `SystemPromptManager` / `MessageBuilder` /
  `CognitiveLoop` / 工具定义 / 流式生成 / `PromptRecorder`。

---

## 11. Regression Tests（分类）

| 测试（`core/input/tests/test_character_preview_window.py`，全部走真实 HTTP 路由） | 分类 | 修前 |
|---|---|---|
| `test_preview_fits_small_window_when_trimming_suffices` | **PRE_FIX_RED** | 503（provider 收到 8429 > 8192） |
| `test_preview_boundary_sends_when_fits_and_validates_when_not` | **PRE_FIX_RED** | 503（"fits"那半也超窗） |
| `test_preview_impossible_input_fails_locally_without_provider_call` | **PRE_FIX_RED** | 503（provider 收到 12229） |
| `test_preview_budget_follows_resolved_model_not_config` | **PRE_FIX_RED** | 503 |
| `test_builtin_character_preview_records_window_margin` | **PRE_FIX_RED** | 503（provider 收到 8305） |
| `test_preview_large_window_provider_unchanged` | ALREADY_GREEN_GUARD | 200 → 200（请求逐字一致：8429） |
| `test_small_window_model_can_preview_a_small_request` | ALREADY_GREEN_GUARD / 正控制（§54） | 200 → 200 |
| `test_preview_undeclared_window_keeps_compat_policy` | PRODUCT_CONTRACT_GUARD（§20 兼容策略） | 200 → 200 |
| `core/prompt/tests/test_prompt_budget.py::...::test_effective_window_is_the_shared_fallback` | PRODUCT_CONTRACT_GUARD（共享回退点） | 单元（新） |

修前汇总：**5 failed / 3 passed**（红的全是 PRE_FIX_RED）；修后 **8 passed**。

**防假绿（§53）**：每条成功用例都断言 `len(backend.calls) == 1` **且**记录到 provider
实收 `input_tokens`；本地拒绝用例断言 `backend.calls == []`。前提断言**现算**
（未加预算 8429 > 8192；加预算 7867 < 8192；边界 `fits == window-1` / `over == window`）。

---

## 12. Small/Large Window Matrix（路由级，实测）

| provider（声明窗口） | 请求 | 修前 | 修后 | provider 实收输入 |
|---|---|---|---|---|
| ollama（8192） | 合成 draft + 1303 字 | 503 | **200** | 8429 → **7867** |
| ollama（8192） | 大档案（不可行） | 503 | **400 CHARACTER_VALIDATION** | 12229 → **0（未发）** |
| ollama（8192） | 内置 elysia + 703 字 | 503 | **200** | 8305 → **8131** |
| ollama（8192） | 内置 elysia + 11 字 | 200 | 200 | 7614 → 7440 |
| deepseek（131072） | 合成 draft + 1303 字 | 200 | 200 | **8429 → 8429（逐字不变）** |
| 未声明 provider | 合成 draft（小） | 200 | 200 | ≤ 32768 |

**E3（真实 ollama，非 stub）**：修前内置 elysia 两个用例都是 **HTTP 200**（见 §2.4）——
再次确认"provider 400"不发生在本地 ollama，而发生在强制窗口类 provider。

---

## 13. Challenger Findings（独立子代理，独立复现）

| 攻击 | 结论 | 处置 |
|---|---|---|
| A tiny window（patch 到 4096/2048/512） | 每个超窗请求都被本地拒绝、0 provider 调用；但 W ≤ 2048 时保护节下界(~2.1K)让**任何**预览都 400 | **accepted risk**（§9、invariants §1.1 边界 4） |
| B large window | 修前/修后 messages **逐字一致**（8429/8305/27605/107605），kwargs 仍 `{}`，无新参数 | **no issue**（PPW-5） |
| C input-heavy | 确定性；边界由 estimator 决定 | no issue |
| D output cap | 仓内无 model 级 output cap metadata；预览不发该参数 | **accepted risk**（§8 decision record） |
| E wrong authority | 修后跟 resolved runtime model（7867）而非 config（8429）；`switch_model` 后仍跟手 | **no issue**；但权威是 **provider 级**（`ollama/llama3.1:70b` → 同一 8192）→ KB-13 |
| F unknown metadata | 无 metadata → 32768 默认，不崩/不除零/不虚构巨大窗口；`context_window(None)` 的 TypeError 不可达 | no issue |
| G zero/invalid window | 0/负数 → 32768；**但**：旧式回退下 `budget_from_context_window(-5)` 曾是 1，现为 24576 ⇒ PW6 的"逐字不变"需限定 | **已处置**：invariants PW6 写明精确边界（仓内不可达） |
| H off-by-one | `== 窗口` 原被**发送**（0 输出余量） | **fix weakness → 已修**：改为 `>=` 拒绝 + 边界用例钉死 |
| I tool/system overhead | 预览 `tools=None`，无额外 overhead；system 计入估算 | no issue |
| 变异 m1–m5 | 4F/4P、5F/3P、2F/6P、4F/4P、2F/6P（均无假绿） | 与主 Agent 的 M1–M11 一致（§14） |
| **W1 漏拒**（JSON/代码偏离） | estimator 对 JSON 低估：est 7302 → qwen 真值 12212 ⇒ 强制窗口 provider 上仍会 400 → 503 | **confirmed fix weakness**（根因是 estimator，非本层可修）→ **KB-12** |
| **W2 误拒**（CJK 偏高） | est 8329 → 真值 7921 ⇒ 一个勉强装得下的预览被本地 400 | **confirmed fix weakness** → **KB-12** |
| **W3 F8 症状在原 provider 上不可复现** | 真实 ollama 超窗 → 200 + 静默截断，从不 400 | **reclassified**（§2.3/§2.4）→ 症状描述已改写，KB-13 |

主 Agent 独立复核：E3 真实 provider 预览（2 例，均 200）、真实 `/api/chat` 超窗行为、
以及 m1–m11 变异矩阵（§14）。

---

## 14. Mutation Evidence

| 变异 | 结果 | 杀死它的用例 |
|---|---|---|
| M1 恢复旧行为（不传预算） | 4F/4P | T1/T3/T5/T8 |
| M2 窗口权威改回 config/bootstrap 模型 | 5F/3P | +T4 |
| M3 删掉本地 fit check | 2F/6P | T3/T4 |
| M4 判据改用预算而非窗口 | 4F/4P | T1/T3/T5/T8 |
| M5 本地拒绝映射回 503 | 2F/6P | T3/T4 |
| M6 unknown 回退改 8192 | 8P（本文件）/ **1F**（`core/prompt/tests/test_prompt_budget.py`） | `test_budget_uses_headroom_and_default` + `test_effective_window_is_the_shared_fallback` |
| M7 丢掉当前用户消息 | 1F/7P | 边界用例 |
| M8 预算硬编码 6144 | 1F/7P | T2（大窗口请求被无谓裁剪） |
| M9 窗口写死 32768（provider-wide 假设） | 5F/3P | T1/T3/T4/T5/T8 |
| M10 回退不覆盖负数 | 8P（本文件）/ **1F**（单元） | `test_effective_window_is_the_shared_fallback` |
| M11（还原） | 8P | — |

---

## 15. Public API / Schema Impact

**无**。改动只涉及后端错误映射与请求组装：
- 请求/响应模型（`CharacterPreviewRequest/Result/Usage`）**未动**，`make check-types` 通过；
- 错误码**未新增**：复用既有 400 `CHARACTER_VALIDATION`（非重试）。原本的 503
  `CHARACTER_PREVIEW_FAILED` 语义**收窄**为"真正的 provider/运行时不可用"；
- 无 `details` 结构变化；无 generated types 漂移。

> 语义变化如实记录：对"裁完仍超窗"这一类，客户端从 `503 + retryable + retryAfterMs`
> 变为 `400 + 非重试`。前端既有映射（`CHARACTER_VALIDATION` → "角色校验未通过…"）
> 无需改动即可显示；这属**语义纠正**（原来那条 503 谎报可恢复）。

---

## 16. Network / Billing Impact

| 问题 | 答案 |
|---|---|
| 新增 provider 请求？ | **No**（一次预览仍 ≤ 1 次 generation） |
| 新增 retry？ | **No** |
| 新增付费调用？ | **No**（测试全用 stub；E3 用本机 ollama，免费本地） |
| 新增健康探测/模型自省？ | **No** |
| 变的请求？ | 超窗案例从"发出必失败请求"变为 **0 请求**；可行案例请求参数（messages）按 canonical 预算裁剪 |

---

## 17. Performance Impact

- 每次预览多一次 `estimate_messages_tokens(messages)`：对 messages 的一次线性遍历
  （CJK 逐字符比较 + 少量算术），与 `PromptAssembler` 内部已有的估算同阶；
  无重复 tokenization 循环、无网络、无缓存需求。
- 拒绝了"必失败请求"反而省下一次 provider 往返（ollama 上实测 12–14s）。
- 无新超时常量（`check-timeouts` 通过）。

---

## 18. Commits

| # | commit | 内容 |
|---|---|---|
| 1 | `558503be` | `fix(preview): fit preview prompt to the resolved model window`（生产修复 + 守卫测试同 commit） |
| 2 | 本文所在 commit | `docs(architecture): record preview window invariants (F8)`（invariants §1.1 + 本文 + todo.md F8 状态与 KB-12/13/14 登记） |

---

## 19. Worktree Verification

| 工作树 | 用途 | 结果 |
|---|---|---|
| `/tmp/b7-preview` | 生产修复 + 守卫（本批主工作树） | 修后 8 passed；quick/normal/full 绿 |
| `/tmp/b7-mut` | 变异矩阵（M1–M11） | 见 §14 |
| `/tmp/b7-clean` | 干净 HEAD：修前红证据 + 真实 ollama E3 基线 | 修前 5F/3P；E3 两例 200 |

**Fresh process（§56）**：E3 探针与全部 pytest 运行都在**新起进程**里读取
`providers.yaml` / registry / config（`providers.yaml` 在 import 期缓存，故这一步必要）；
修前/修后分别在**两个不同工作树**的独立进程中测得，排除 module cache 假绿。

**Order perturbation（§57）**：小窗口/大窗口/未声明窗口用例在同一文件内交替执行，
另有 `-n 8 --dist loadfile`（quick/normal/full）与单文件串行两种顺序，结果一致；
无全局状态需要恢复（stub/服务/身份 store 都是 per-test 构造）。

---

## 20. Test Results

| 档位 | 命令 | 结果 |
|---|---|---|
| targeted | `pytest core/input/tests/test_character_preview_window.py` | 修前 **5F/3P** → 修后 **8P** |
| targeted（相关既有） | `core/character/tests/` + `core/input/tests/test_character_routes.py` + `core/prompt/tests/` + `Module/system_prompt/tests/` | **352 passed** |
| fresh subprocess（E3） | 真实 ollama + 真实路由（HEAD 工作树） | 2 例 **HTTP 200**（见 §2.4） |
| quick | `make test-quick` | **6077 passed, 2 skipped, 1 xfailed** |
| normal | `make test-normal` | **6077 passed, 2 skipped, 1 xfailed** |
| full | `make test-full` | **6109 passed, 27 skipped, 1 xfailed** |
| static gates | `lint` / `pyright` / `check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | **全通过**（pyright 0 errors；test-sets `|Quick ∪ remainder| = 6080 == |Normal|`） |
| frontend/build | 本批**零前端改动**，契约未变 | `make check-types`（含 golden frames --check）通过；前端仓未改 |
| real CI | push 后 | 见 §26（本机无 GitHub 凭据 ⇒ 由 owner push 后核对） |

> 环境说明：worktree 不带被 gitignore 的 `.env`，`Module/config/tests/test_preflight.py::TestCheckTTSEnv::test_minimax_key_present_passes`
> 会因缺 `MINIMAX_TTS_URL` 而红（既有弱用例 `GR-2`）。把仓内 `.env` 复制进 worktree 后
> 与主 checkout 一致，quick/normal/full 全绿；CI 侧同源。

---

## 21. Invariant Mapping

| Invariant | Test | Suite | CI |
|---|---|---|---|
| PW1 预览装得下 resolved 窗口 | `test_preview_fits_small_window_when_trimming_suffices`、`test_builtin_character_preview_records_window_margin` | `core/input/tests/test_character_preview_window.py`（service） | push/backend（`make test-normal`） |
| PW2 权威 = resolved runtime model | `test_preview_budget_follows_resolved_model_not_config`、`test_preview_undeclared_window_keeps_compat_policy` | 同上 | push/backend |
| PW3 能裁进窗口就能预览 | `test_preview_boundary_sends_when_fits_and_validates_when_not`、`test_small_window_model_can_preview_a_small_request` | 同上 | push/backend |
| PW4 不可能请求本地拒绝、零 provider 调用 | `test_preview_impossible_input_fails_locally_without_provider_call`、边界用例的 `== window` 半 | 同上 | push/backend |
| PW5 大窗口不回归 / 未声明窗口兼容 | `test_preview_large_window_provider_unchanged`、`test_preview_undeclared_window_keeps_compat_policy` | 同上 | push/backend |
| PW6 不改正常生成策略 / 回退单源 | `core/prompt/tests/test_prompt_budget.py::TestEstimation::test_effective_window_is_the_shared_fallback`、`::test_budget_uses_headroom_and_default` | `core/prompt/tests/`（unit） | push/backend |

---

## 22. Todo Update

`todo.md` 只改 **F8**：
- §1.2：`OPEN` → **`GUARDED`**（+ 频率主张修正说明）；
- §3 Truthfulness：新增 F8 关闭行（谎报 retryable 503 → 本地确定性 400）；
- §5：新增 F8 一行索引；
- 新增 `KB-12` / `KB-13` / `KB-14`（`NEW_FINDING_OUT_OF_SCOPE`，**只登记不修**）。

---

## 23. Keep As-Is（本批刻意不动）

1. **Normal chat generation policy**：预算公式/常量/截断优先级一律不变（§10）。
2. **Provider capability truth model**（Batch 5）：capability 真相仍在 registry；
   本批只是**消费**它（`LLMFactory.context_window`），没有新增第二份能力表。
3. **No automatic retry loop**：不加"400 → 减半 max_tokens → 重发"。
4. **No new external health probe**：预览路径不发任何探测请求。
5. **No new contextWindow table**：窗口真相仍只有 `providers.yaml` 一份。
6. **`providers.yaml` 的估算窗口值**：不改（改了会全局影响 chat；KB-13）。
7. **未知 provider 的兼容 fail-open**（Batch 3.5/5 策略）：保持。
8. **合法无-key provider 守卫**：保持（预览不因本批新增要求凭据）。

---

## 24. New Findings（只登记，不修）

| ID | 标题 | Reach | Sev | Ev | 为什么不在本批 |
|---|---|---|---|---|---|
| **KB-12** | prompt 估算器双向误差带：CJK 偏高（est 8329 vs qwen 真值 7921）、JSON/代码偏低（est 7302 vs 真值 12212）⇒ 预览的本地窗口判定可能误拒或漏放 | USER | LOW | E2(7) | 修它需要真 tokenizer（§15/§16 明示不在本批范围），且 estimator 是 chat 与预览的共享底座 |
| **KB-13** | `providers.yaml::llm.ollama.context_window = 8192` 与实测 ollama 0.24.0 默认 `num_ctx = 4096` 不一致，且 ollama **超窗时静默截断（200）** 而非报错 ⇒ "装得下声明窗口"是必要非充分条件；影响 chat 与预览**两者** | DEPLOYMENT | MEDIUM | **E3(7)** | 改它 = 全局改变 chat 截断时机（§0 禁止），且会让内置角色在 ollama 上从"被截断"变成"本地直接拒绝"（更糟）；属 owner 决策 |
| **KB-14** | `core/tests/fake_llm.py` 的故障注入路径 `from Module.llm.errors import RateLimitError/ServiceUnavailableError` —— **该模块不存在**（全仓无 `Module/llm/errors.py`）⇒ `fault="http_429"/"http_503"` 会以 ModuleNotFoundError 结束而不是注入 429/503（当前无调用点，故未暴露） | INTERNAL | LOW | E1 | 测试基础设施缺陷，与 F8 无关；本批不动测试替身 |

---

## 25. Follow-up（保留 canonical backlog 其它项）

不做：`P2-01r`（会话分页）、`KB-6`（FSM SPEAKING）、`OBS-6`、`KB-11`、`GR-25`、`N-15`、
`E1 cache freshness`、`F5`/`F13`/`KB-8`、以及所有 `AR-*`/`GR-*`/`FEAT-*`/`PD-*`。

推荐下一步（owner 决策）：
1. **KB-13**：ollama 的窗口真相（声明 vs served）——是校正 `providers.yaml`，
   还是让 provider 层显式声明/探测 `num_ctx`；
2. **KB-12**：是否为本地窗口判定引入 provider 侧 tokenizer（或降级为"仅告警不拒绝"）；
3. **OBS-2** 的括号（"只影响截断时机，不影响正确性"）已被 KB-13 的 E3 证据反驳，
   建议一并复核。

---

## 26. CI 验证状态（本机无 GitHub 凭据 ⇒ owner push）

`git ls-remote origin main` 在本机失败（`could not read Username for 'https://github.com'`；
无 `gh`、无 `~/.git-credentials`、无 SSH key、无 `credential.helper`）——**主 Agent 无法 push**，
与 Batch 6 同一处境。为把"push 后才发现红"的概率压到最低，按 `ci.yml` 的三个 job
逐条跑**等价命令**并记录：

| CI job | 等价命令 | 结果 |
|---|---|---|
| `backend`（self-hosted，push-only） | 12 道门禁：`lint` / `pyright` / `check-types` / `check-arch` / `check-timeouts` / `check-contract` / `check-debug-auth` / `check-ci-isolation` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` | **全通过**（pyright 0 errors；`|Quick ∪ remainder| = 6080 == |Normal|`） |
| 同上 | `source .env && make test-normal` | **6077 passed, 2 skipped, 1 xfailed** |
| `Real MongoDB`（push-only，compose） | `docker compose -f docker-compose.test.yml up -d` → healthy → `RUN_REAL_MONGO_TESTS=1 ELYSIA_TEST_MONGODB_URI=mongodb://127.0.0.1:27018/elysia_test make test-real-mongo` → teardown | **15 passed, 6122 deselected**；容器已 `down` 且断言消失 |
| `Frontend (vue-tsc + build, self-hosted)` | `cd frontend && npm ci && npm run build`（= `vue-tsc --noEmit && vite build`） | **`npm ci` 0；`✓ built in 993ms`，BUILD=0** |

**未由主 Agent 独立见证的唯一一条**：§78 完成标准里的「真实 CI 全绿」——需要 owner
push 到 `main` 后由 self-hosted runner 跑 `ci.yml`。本批的两个 commit 已落在 `main`
线性的 fast-forward 位置上（`ccfc5227` → `558503be` → docs commit），push 后即可触发。
