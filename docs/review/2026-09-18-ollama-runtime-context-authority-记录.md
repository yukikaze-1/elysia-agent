# Ollama Runtime Context Authority（KB-13）与 Ollama Provider Adapter（AR-16）backlog 记录

> **本文性质**：backlog / planning 记录，**不是**批次执行记录。本次**只改 `todo.md` 与本文**：
> 零生产代码、零测试、零 CI、零 `providers.yaml` 改动；**不**实现 Ollama adapter、**不**修 KB-13、
> **不**重做全仓 provider 审计、**不**改动其它 backlog 条目状态。
>
> 记录时间：2026-09-18。上游证据：[Batch 7 执行记录](2026-09-18-Batch7-provider-preview-window-执行记录.md) §2.5。

---

## 0. 这次新增的是什么

Batch 7 用 `prompt_eval_count` **间接推出**「默认服务窗口 = 4096」。本次补的是**对 runtime
allocation 本身的直接观测**（`ollama ps` 的 `CONTEXT` 列 + `GET /api/ps` 的 `context_length`），
并用 **A/B 冷启动对照**证明该 allocation 由**请求侧的 `num_ctx`** 决定，而不是由 model
metadata 或 Elysia 的声明窗口决定。

因此 KB-13 的结论从「两个数字不一致」上升为「**两边没有绑定（authority 缺失）**」——
canonical 描述已按此改写（§2）。

---

## 1. E3 证据

### 1.1 环境（本次记录方只读复核）

| 项 | 值 | 来源 |
|---|---|---|
| Ollama 版本 | **0.24.0** | `ollama --version` |
| 模型 | `qwen2.5:3b` | `ollama ps` / `providers.yaml::llm.ollama.models` |
| daemon 级 context env | **未配置** `OLLAMA_CONTEXT_LENGTH` | `systemctl cat ollama` + `systemctl show ollama -p Environment` |
| service definition 的 `num_ctx` override | **无**（drop-in 只有 `OLLAMA_HOST` / `OLLAMA_HOME`） | `/etc/systemd/system/ollama.service.d/override.conf` |
| 记录时 runner 状态 | **cold**（`ollama ps` 为空表） | `ollama ps` |

> 复核方式是**只读**的（版本/服务定义/当前 `ps`）；下述 A/B 实验本身由 owner 亲测，
> 本记录方未重复执行（重跑需要 `ollama stop` + 真实 generation，属运行时副作用）。

### 1.2 A/B 冷启动实验（owner 亲测）

| 截面 | 前置 | 请求 | `ollama ps` `CONTEXT` | `GET /api/ps` `context_length` |
|---|---|---|---|---|
| **A** 不传 `num_ctx` | `ollama stop qwen2.5:3b` → 确认模型未加载 | native `/api/chat`，**不含** `options.num_ctx` | **4096** | **4096** |
| **B** 显式 `num_ctx=8192` | 再次彻底 stop → 确认未加载 | native `/api/chat`，`options.num_ctx = 8192` | **8192** | **8192** |

**结论**：Ollama 的实际 serving context 是 **runtime allocation**——不传 `num_ctx` 时取
daemon 默认（本机冷启动实测 4096），显式传 `num_ctx=8192` 时才分配 8192。
`providers.yaml` 声明 `context_window = 8192` **不会**让它变成 8192。

### 1.3 与 Batch 7 旧测的关系（同向，不是矛盾）

- Batch 7 §2.5：6000 字**不传** `num_ctx` → `prompt_eval_count=4096`（推默认窗口 4096）；
  显式 `num_ctx=8192` 的 9000 字 → 8192；未超窗对照（6000 字 / 显式 8192）→ 6029。**同向且被本次加强**。
- Batch 7 已确认超窗行为是 **HTTP 200 + 静默截断**（三个截面一致，**从不 400**）。

**证据边界（不许越读）**：本次只测「一次冷启动 + 一个请求」的 allocation。同一 runner
生命周期内 allocation 是否会被后续请求改写、`keep_alive` 与它的交互、以及 daemon 默认值
的确定规则（是否随模型/显存变化）**均未测**，留待未来 adapter batch 的 Phase 2 审计。

### 1.4 Elysia 侧现状（E1）

- prompt 预算 = `providers.yaml::llm.<provider>.context_window` × 0.75，唯一执行点
  `Module/system_prompt/budget.py`（S2-H9）——即 **budgeting 使用静态声明值**。
- 当前 Ollama 走**通用 OpenAI-compatible backend**（`Module/llm/openai_compat.py`），
  请求路径**没有任何地方保证** `num_ctx = 8192`：全仓生产代码里 `num_ctx` 出现 0 次。
- ⇒ 存在稳定可发生的组合：**Elysia budgeting window = 8192 ／ Ollama serving context = 4096**；
  且因为超窗是 200 + 静默截断，**provider error 不会替我们发现这个 mismatch**。

---

## 2. KB-13（canonical 条目，已改写）

**canonical ID**：`KB-13`（不变，未新造 ID）
**新 canonical 标题**：`KB-13 — Ollama runtime context authority mismatch`
**分类**：§1.3 Runtime / Observability（`TRUTHFULNESS` 家族；同一事实亦进 §3 主题表）

| 维度 | 值 |
|---|---|
| Status | `OPEN`（**已确证**；`CONFIRMED` 不是 §1.1 词表值，故不新造词表项） |
| Reachability | `DEPLOYMENT`（词表无 `USER-adjacent`；**备注：用户可见**——人设被静默削掉） |
| Severity | `MEDIUM` |
| Evidence | `E3(7)` + **`E3`（本机 A/B 冷启动复测）** |

**为什么改写**（原描述的问题）：原文写「`providers.yaml` 声明 8192 与实测默认 `num_ctx = 4096`
不一致」，容易被读成**「窗口数值写错了，改个数就好」**。A/B 冷启动证明真相不是数值差：
serving context 是**运行时分配**，声明值 ↔ 运行时分配之间**没有绑定**——改 `providers.yaml`
既不能让 Ollama 用 8192，也不能表达「Elysia 要求 provider 用多少」。因此：

- 保留的旧事实：超窗 **200 + 静默截断**（⇒「装得下声明窗口」是**必要非充分**条件；
  不能靠 provider error 发现 mismatch）；
- 新增的事实：**declared window ≠ runtime allocation**，且 allocation 由 `num_ctx` 控制
  （A/B 两截面直接观测）；
- 影响面：**preview 与 normal chat 两者**；
- 附带修正：`providers.yaml` 中 ollama 的注释（「qwen2.5:3b 的 num_ctx 默认 8K」+
  「只影响截断时机，不影响正确性」）与 `OBS-2` 括号里的同一说法**已被本证据反驳**；
  但按本次范围约束**不动 `providers.yaml`、不改 `OBS-2` 状态**，只登记。

**没有被标成 `FALSE_POSITIVE`**：本条有 E3 直接观测 + 可复现的 A/B，不是假阳性。

---

## 3. AR-16 — `Ollama Provider Adapter`（Architecture Debt，新条目）

**canonical ID**：`AR-16`（`AR-15` 之后的第一个空号；Architecture Debt 表现有 `AR-1..AR-15`）
**category**：Architecture Debt（§1.6）
**status**：`DEFERRED`（时序决策见 §4）

### 3.1 为什么不是复用 `AR-4` / `AR-5`

已按要求先查既有条目，两条都**不同轴**，不能承载：

| 条目 | 它管什么 | 与本条的关系 |
|---|---|---|
| `AR-4` | **provider portability（F10）**：*切换* provider 的运行时能力未成体系（跨 provider 模型名 + `thinking`/`prefix` 能力门控贯通） | 关注「哪个子系统用哪个 provider」；本条关注「**一个 provider 的 runtime semantics 在哪里收口**」 |
| `AR-5` | `AR-4` 的实例：`TASK_SYSTEM_MODEL` 是裸模型名 ⇒ 工作流不能用另一个 provider | 同上，属 task LLM 归属 |

既有设计稿 [`docs/2026-08-30-provider-model-runtime-backend-architecture-design.md`](../2026-08-30-provider-model-runtime-backend-architecture-design.md)
§5 已提出 `ProtocolAdapter` registry（并把 `ollama_native` 列为未来协议）——本条目与该方向一致，
但那是**协议适配**层面的设计稿，不是 backlog 条目；backlog 侧此前**没有任何** provider-specific
adapter 条目，故新增 `AR-16`。

### 3.2 Motivation

> 当 provider 的 **runtime semantics 明显偏离通用 OpenAI-compatible 行为**时，应由
> provider-specific adapter 收口，而不是把 `if provider == "ollama"` 分散到
> preview、CognitiveLoop、preflight、debug 等上层。

现状佐证（E1）：`Module/config/preflight.py` 已有 `if provider == "ollama"` 分支（无需 key /
跳过连通性检查）；`openai_compat.py` 用 `api_key or "ollama"` 兜底；`KB-13` 的 runtime context
authority 在**任何一层都没有归属地**。

### 3.3 目标架构概念

```text
LLMAdapter
    ↓
LLMFactory
    ├─ OpenAICompatibleBackend
    ├─ GeminiBackend
    └─ OllamaBackend
```

**非目标（写进条目的约束）**：这**不是**「每个 provider 都要独立 adapter」。只有
provider-specific runtime semantics 足够明显时才拆；`custom`/云端 OpenAI 兼容 provider
继续共用通用 backend。

### 3.4 预期职责（只记范围，不实现）

- Ollama request / wire translation；
- `num_ctx`；
- `keep_alive`；
- Ollama-specific runtime options；
- **canonical Elysia context budget → Ollama runtime context 的落实**；
- `prompt_eval_count` / `eval_count` → 通用 usage normalization；
- Ollama 特有的 silent truncation / runtime diagnostic 语义；
- **可选** `/api/ps` diagnostics / integration verification。

**性能约束（设计原则，不是实现决定）**：`/api/ps` **不得**成为每次 generation 前的强制网络
probe；正常请求路径仍应保持 **1 user generation → 1 provider generation request**，
不引入额外 health probe。

### 3.5 Deferred reason

不在当前 correctness batch 中途插入完整 adapter 重构。理由（blast radius 大于单点 correctness fix）：

- provider factory / backend boundary；
- context authority；
- request options；
- usage normalization；
- diagnostics；
- 现有 OpenAI-compatible 路径。

⇒ **DEFERRED UNTIL CURRENT CORRECTNESS RUN COMPLETES**（**不是**无限延期）。

### 3.6 Trigger condition

- **正常启动**：当前高优先级 correctness backlog **再清理一轮后**，且在**继续增加新的
  Ollama-specific runtime feature / patch 之前**，单独开启 Ollama Adapter batch；
- **提前触发**：若接下来某个 correctness item 本身需要修改 —— ①Ollama request parameters、
  ②context window runtime semantics、③`num_ctx`、④provider-specific truncation、
  ⑤Ollama runtime model state —— 则**暂停该项的局部 Ollama patch**，优先提前执行 Adapter batch。

---

## 4. 未裁决决策点：`PD-12`（PRODUCT/ARCHITECTURE DECISION REQUIRED）

`contextWindow` 的语义到底是哪一种，**当前未裁决**，必须在 TODO 中保留：

| 语义 | 含义 | 后果 |
|---|---|---|
| **A** | Elysia **实际允许/要求 provider 使用的 effective runtime context** | `contextWindow = 8192` 可由 Ollama adapter 落实为 `num_ctx = 8192` |
| **B** | **模型理论 capability / maximum context** | **不能**直接等价于 `num_ctx`：模型理论支持 32768，但本地 6GB GPU 可能只想分配 8192 ⇒ 未来可能需要区分 **model context capability** 与 **effective/runtime context allocation** |

**本次明确不决定**：字段名与 schema（是否新增 `runtimeContextWindow` 之类）——留给未来
adapter batch 的 Phase 1/3（全仓语义审计 + authority model）后由 owner 拍板。

---

## 5. KB-13 与 AR-16 的关系（**不得合并成同一概念**）

```text
KB-13（已确认的具体 correctness / runtime authority mismatch）
   │  motivates / likely addressed by
   ▼
AR-16 Ollama Provider Adapter（长期收口 provider-specific semantics 的 architecture work）
```

- `KB-13` = **已确认的具体缺陷**（E3 可复现的 authority mismatch）；
- `AR-16` = **架构工作**，用于长期收口 provider-specific semantics；
- **`KB-13 != AR-16`**：KB-13 未来也可能通过**更小的配置 / authority fix** 解决，而 adapter
  仍有独立的架构价值；两条在 backlog 中**保持独立条目**（依赖关系只写在两边描述里）。

---

## 6. 未来 Batch 候选：`Future Batch — Ollama Provider Adapter`

**建议执行顺序**（本次只记录，不启动）：

| Phase | 内容 |
|---|---|
| 1 | 确认 `contextWindow` 在**全仓**的真实语义（= `PD-12` 的 A/B 裁决输入） |
| 2 | 审计当前 Ollama 在通用 OpenAI-compatible backend 中的**真实路径**（含 `/v1` vs native 端点、`num_ctx` 传递链） |
| 3 | 确定 **model capability vs runtime allocation** 的 authority model |
| 4 | 建立**最小** Ollama-specific backend/adapter |
| 5 | 把 runtime context **正确落实**到 Ollama |
| 6 | 真实 Ollama **E3 验证** |

**未来 acceptance evidence 至少应包括**（前提：最终设计选择由 Elysia 显式控制 runtime allocation）：

```text
cold Ollama runner
        ↓
Elysia effective budget/window = X
        ↓
真实 Elysia generation request
        ↓
Ollama /api/ps context_length = X
```

> 若 Phase 1/3 的结论是「窗口语义 = B（capability）」或「不由 Elysia 控制 allocation」，
> 则这条 acceptance 需相应改写——**现在不预设**。

---

## 7. 本次明确不决定 / 不做的事

- 不决定 `contextWindow` **一定**直接传 `num_ctx`；
- 不决定**一定**新增 `runtimeContextWindow` 字段；
- 不决定**每次请求前查 `/api/ps`**；
- 不改 `providers.yaml`、不给 Ollama 请求加 `num_ctx`、不创建 `OllamaBackend`；
- 不改任何测试 / CI / 生产代码；不修 KB-13、不重做 provider 审计、不动其它 backlog 状态。

> 以上候选方案与决策点留给未来 Adapter batch 的 **Phase 1 architecture/semantics audit**。

---

## 8. Backlog 落点（本次 `todo.md` 改了哪些行）

| 位置 | 改动 |
|---|---|
| §1.3 Runtime / Observability | `KB-13` 行**改写**（新标题 + 新语义 + A/B 证据 + Reach 备注）；Status 仍 `OPEN` |
| §1.6 Architecture Debt | **新增 `AR-16`**（Ollama Provider Adapter，`DEFERRED` + 触发条件） |
| §1.8 Product Decisions | **新增 `PD-12`**（`contextWindow` 语义 A/B，`PRODUCT/ARCHITECTURE DECISION REQUIRED`） |
| §3 Truthfulness Backlog | **新增 `KB-13` 行**（reported≠actual 主题表补记；不改其它行） |
| §4 Deferred / Keep-As-Is | **新增 `AR-16` 行**（deferred 理由 + 重开/提前触发条件） |

**未动**：`OBS-2`（其「不影响正确性」括号已被反驳，但状态不变）、
`docs/architecture/invariants.md`（§1.1 accepted risk #3「声明窗口 ≠ 服务窗口」已覆盖同一
边界，未来 adapter batch 再刷新）、`providers.yaml` 及其注释。
