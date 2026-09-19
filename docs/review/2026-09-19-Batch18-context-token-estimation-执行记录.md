# Batch 18 — Context / Token Estimation Correctness（KB-12）执行记录

> 范围：canonical 主 scope = **KB-12**（prompt token 估算）。本批**不**改
> `contextWindow` 语义（Batch 17 已定）、`num_ctx`、provider runtime allocation、
> `/api/ps`、通用 LLM 后端架构、PromptAssembler 大重构、memory 架构、system-prompt
> 政策、output-token 政策、budget fraction（0.75）、前端、持久化。
> 其它发现一律 `NEW_FINDING_OUT_OF_SCOPE` 只登记。

---

## 0. 一句话结论

**KB-12 confirmed（且比原登记更严重）**，已修：估算器从"CJK 1 字 1 token + 其余
4 字符 1 token"的单公式，换成**按 resolved model 选 profile 的 9 类有界近似**，
并把此前**完全不可见**的 `tools=[...]` 纳入预算。合同：

```
est / actual ∈ [0.75, T]        cjk_opt(qwen/deepseek) T=1.26   generic T=1.71
```

修前实测：自然中文 **+67% 高估**（误拒面）、minified JSON/numeric array/emoji
**-43% ~ -75% 低估**（静默截断面）、工具 schema **完全不计**
（本仓 12 个内置工具 = **1126 token**，占 6144 预算的 18%）。

---

## 1. Phase 0 — 只读审计

### 1.1 当前公式（逐字，修前）

`Module/system_prompt/budget.py`：

```python
def estimate_tokens(text: str) -> int:
    if not text: return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))          # 7 段 CJK 区间
    return cjk + ((len(text) - cjk) + 3) // 4           # 其余 4 字符 1 token（向上取整）

def estimate_messages_tokens(messages):
    for msg in messages:
        total += 4                                       # 每条固定开销
        total += estimate_tokens(str(msg.get("role","")))
        total += _content_tokens(msg.get("content"))
        if msg.get("tool_calls"): total += ...
    return total                                          # ← 签名里没有 tools
```

来源：S2-H9（Batch 1–1.8）。**第二份估算器**存在于
`Module/tool/safety.py::_estimate_tokens`（`int(latin/4 + cjk)`，
`latin = re.findall(r"[a-zA-Z0-9\s\W]", text)`），用于工具结果截断。

### 1.2 Consumer map（完整版见审计子代理报告）

| Consumer | 输入 | 阈值 | 越界后果 |
|---|---|---|---|
| normal chat 预算 | `LLMFactory.context_window(model) or 32768` | 产出 `budget = window×0.75` | 只是个数，交给下面两处 |
| system prompt 梯子 | `"\n\n".join(sections)` | `system_prompt_budget = budget×0.5` | C4 记忆 3→2→1→0 → 可丢节整节丢（保护节永不丢），**只 warning** |
| 历史裁剪 | 组装后的 `messages[]` | `budget` | 由旧到新丢历史；system 与当前消息**永不丢**；裁尽仍超 → **如实 warning 后照发** |
| **character preview** | `estimate_messages_tokens(messages)` | **`>= effective_window`**（不是预算） | 抛 `PreviewPromptTooLargeError` → 400 `CHARACTER_VALIDATION`，**零 provider 请求** |
| 工具结果截断 | 工具结果文本（**另一个估算器**） | `SafetyProfile.max_result_tokens`（默认 2000） | head/tail 截断 + `truncated=True` |
| debug 面板 | 字符数 / provider usage | 无 | 不裁剪、不拒绝 |

**关键确认**：chat 与预览**共用同一个**估算器（`estimate_tokens`），但
**阈值不同**（预算 vs 窗口）；工具结果截断**不共用**（第二份公式）。

### 1.3 三种阈值（本批区分）

| 阈值 | 值（ollama 8192） | 比较点 |
|---|---|---|
| runtime context window | 8192 | 预览拒绝（`>=`） |
| prompt budget | 6144 | 历史裁剪 |
| system prompt 子预算 | 3072 | section 梯子 |
| tool-result cap | 2000（另一个估算器） | 工具结果截断 |

**0.75 未动**：本批没有证据表明它是 defect root；`test_hard_floor_follows_the_headroom_ratio`
把"合同下限 0.75 由它推出"钉成断言。

---

## 2. 基准语料与真值（§22–§34、§107–§113）

### 2.1 语料（签入）

`Module/system_prompt/tests/fixtures/token_corpus/`：

- **55 条**（49 te / 6 holdout），生成器 `scripts/token_corpus/build_corpus.py`
  （确定性：固定句池 + 轮转索引，无 RNG / 时间 / locale）。
- 覆盖：English（short/1k/10k/标点密集/空白）、CJK（简/繁/日/标点密集/无空格长句）、
  混合中英、JSON（minified/pretty/nested/long-keys/numeric-arrays/escaped/中文）、
  代码（Python/TS/YAML/shell/SQL/长标识符）、Markdown、URL/UUID/hash/path、
  Unicode（emoji/ZWJ/组合符/重音/西里尔/全角）、重复退化输入、chat 形状
  （role 框架 / 100 条小消息 / 1 条大消息 / tool_call 历史 / 空消息）、
  工具 schema（small/medium/many）、**代表性 Elysia prompt**。
- **holdout 只在验证阶段读，不参与任何调参。**

> 生成器放 `scripts/token_corpus/` 而不是 fixtures 目录里：它们不是测试代码
> （不被收集、不在任何档位跑），而 fixtures 里的测试代码受"不得裸写 `os.environ`"
> 守卫管辖——实测把它放进去会让 `test_global_state_isolation` 红。

### 2.2 真值层级（本批定义）

| 级 | 来源 | 用途 | 证据 |
|---|---|---|---|
| **G1** | Ollama `prompt_eval_count`（native `/api/chat`） | 端到端真值 | 必须证明未截断：`num_ctx=8192` 与 `16384` 两次测量相同才算 |
| **G2** | 模型权威 tokenizer（HF `tokenizer.json`） | 文本真值 | Qwen2.5-3B / DeepSeek-V3 / Llama-3-8B / Mistral-7B-v0.3 |
| **G3** | tiktoken（家族匹配时） | 跨家族旁证 | cl100k_base / o200k_base / p50k_base |
| **G4** | 估算器本身 | **不是**真值 | — |

**G1 与 G2 交叉验证**：34 条语料上 Ollama `prompt_eval_count` 与 HF Qwen2.5
tokenizer 的**平均相对误差 0.16%**，最大 5.36%（ZWJ emoji 序列）⇒ 测量链可信。
真值签入 `GOLDEN.json`（含 `contaminated` 标记 + 7 个 tokenizer 的 provenance）。

### 2.3 截断污染纪律（§12–§14、§152）

- 全部 50 条真值样本在 `num_ctx=8192` 与 `16384` 下**计数相同** ⇒ 零污染。
- **显式反例用例**：`num_ctx=1024` 时 `prompt_eval_count == 1024`
  （OLL-E4 钉住"这不能当真值"）。
- E2 初版脚本自己踩了这个坑（`num_ctx` 设成被测窗口 ⇒ 两边都读到 8192），
  已改为"真值在 32768 下测、截断在 8192 下证"。

---

## 3. 修前测量（真实 Ollama / 权威 tokenizer）

`r = est / actual`；合同要求 `r >= 0.75`。

| 类别 | 样本 | est（旧） | actual(qwen) | r（旧） | 判定 |
|---|---|---:|---:|---:|---|
| 中文自然语句 | `cjk_zh_simplified` | 478 | 305 | **1.57** | 高估 57%（误拒面） |
| 中文长句无空格 | `cjk_no_space_long` | 480 | 348 | 1.38 | 高估 |
| 日文 | `cjk_ja` | 315 | 229 | 1.38 | 高估 |
| minified JSON | `json_minified` | 800 | 1220 | **0.66** | 低估（静默截断面） |
| 数字数组 JSON | `json_numeric_arrays` | 890 | 3518 | **0.25** | 低估 75% |
| Python 代码 | `code_python` | 915 | 1031 | 0.89 | 低估 |
| shell 脚本 | `code_shell` | 246 | 384 | **0.64** | 低估 |
| 标点密集英文 | `english_punct_heavy` | 792 | 1489 | **0.53** | 低估 |
| emoji | `unicode_emoji` | 63 | 289 | **0.22** | 低估 78% |
| ZWJ/组合符 | `unicode_zwj_combining` | 188 | 448 | 0.42 | 低估 |
| URL/UUID/路径 | `urls_ids_paths` | 356 | 808 | 0.44 | 低估 |
| 工具 schema（12 个内置） | 真实 `ToolAdapter.definitions()` | **0**（不计） | **1126** | **0.00** | 完全不可见 |

**汇总（自然语料，qwen2.5）**：旧公式 `min=0.22 / median=0.96 / max=1.57`，
**11/34 条击穿 -25% 合同**。

### 3.1 Canonical finding 的**独立复核结论**（不继承）

| 原登记 | 复核结果 |
|---|---|
| "CJK 偏高（est 8329 vs qwen 真值 7921，~5%）" | **方向对、量级严重低估**。原 E2 的探针是 `"你" * N`——**恰好 1.000 token/字**（最有利的一端）；自然中文实测 **0.60 token/字** ⇒ 真实高估是 **+67%**，不是 5%。见 §3.2 |
| "JSON/代码偏低（est 7302 vs 真值 12212，0.6×）" | **confirmed**，且更宽：最坏到 **0.25×**（数字数组） |
| "对纯 CJK 是可靠的保守近似" | **证伪**（同上） |
| "provider 侧计数并非总可得（usage 全 0）" | 本批未复现（50/50 次调用都拿到非零 `prompt_eval_count`），**但仍不作为唯一权威**：真值只用"未截断的"观测计数，且以权威 tokenizer 交叉验证 |

### 3.2 退化探针反例（为什么旧结论错得这么远）

```
"你" × 300   → 300 token   (1.000 token/字)   ← Batch 7 的探针
"你好" × 300 → 300 token   (0.500 token/字)
自然中文      → 0.600 token/字
```

BPE 的合并率完全由**词表**决定；用单字重复当"纯 CJK 代表"等价于用最有利样本
推断全体。这条已钉成断言
（`test_repeated_single_char_is_the_degenerate_case_that_misled_batch7`）。

---

## 4. 修前行为复现（E2，真实 Ollama）

### E2-A — chat **false accept → 静默截断**

构造：真实内置角色 system prompt 前缀（1500 字）+ 真实 12 个工具 schema +
17.6k 字符 JSON 用户消息。

```
真实 prompt tokens（num_ctx=32768 测）= 8543   > 窗口 8192
同一请求在 num_ctx=8192 下 prompt_eval_count = 8192   ← 已截断
旧估算（不看 tools）                      = 5486   <= 预算 6144    ← 判"装得下"
修后估算                                  = 9620   >  预算 6144    ← 如实判超预算
```

**旧行为**：`_apply_prompt_budget` 判 `total <= budget` ⇒ 完全不裁剪、只可能打一条
info ⇒ 请求发出 ⇒ provider **HTTP 200 + 静默截断**（Batch 17 已证 ollama 从不为此
报 400）。用户看不到任何错误，人设/历史被削掉。

### E2-B — preview **false reject（400）**

构造：真实 system prompt 前缀 + 7500 字自然中文用户消息。

```
真实 prompt tokens = 5381 < 窗口 8192        ← provider 完全能接收
旧估算             = 8570 >= 8192            ← 本地拒绝，零 provider 请求
修后估算           = 5625                    ← 放行
```

**旧行为**：`PreviewPromptTooLargeError` → 400 `CHARACTER_VALIDATION`（**不可重试**），
用户明明能发送却被本地拒绝。

两条 E2 已固化为 external 用例 `Module/llm/tests/test_ollama_token_truth.py`
（OLL-E5 / OLL-E6，自校准断言，daemon 不可达则显式 skip）。

---

## 5. Estimator contract 决策（Product Decision Gate §119）

| # | 问题 | 答案 |
|---|---|---|
| 1 | 合同类型 | **有界近似（bounded approximation）**，双侧：`est/actual ∈ [0.75, T]`。不是 exact，也不是"越保守越好" |
| 2 | 真值 | G1 Ollama 观测计数（未截断前提）> G2 权威 tokenizer > G3 tiktoken > G4 估算器（§2.2） |
| 3 | 验证过的 provider/model 家族 | Qwen2.5（HF + Ollama 双证）、DeepSeek-V3、Llama-3、Mistral、cl100k、o200k；p50k **合同外**（无对应模型） |
| 4 | false accept vs false reject 优先级 | **false accept 更严重**（provider 200 + 静默截断，无人报错）⇒ 下限 0.75 是硬约束；但高估被 T 显式约束，不接受"用 2× 高估换安全" |
| 5 | 25% headroom 如何计入 | 它就是下限的来源：`budget = 0.75·W` ⇒ 低估超过 25% 即"预算判装得下而实际超窗"。`test_hard_floor_follows_the_headroom_ratio` 钉住 |
| 6 | preview 与 chat 是否共用 authority | **是**（同一个 `estimate_tokens`/`estimate_messages_tokens` + 同一个 profile 解析）；阈值仍不同且**有意不同**（窗口 vs 预算） |
| 7 | tools 是否纳入 | **纳入**（`estimate_messages_tokens(..., tools=)`），并在 `loop.py` 里把 tool_defs 的获取**提到预算之前** |
| 8 | 是否新增依赖 | **否**。零新依赖、零下载、零网络热路径 |
| 9 | 未知模型如何 fallback | 确定性回退 `generic`（保守 profile，T=1.71），不抛错、不失效 |
| 10 | 能否零网络热路径完成 | **能**（纯函数，无 I/O） |

---

## 6. 候选策略对比（§50–§54、§115–§118）

| 策略 | 结论 | 证据 |
|---|---|---|
| A 更好的通用启发式（单公式） | **不足**。7 家族同文本 token 数相差 3.6×；线性规划求"同时不低估任何一家"的最优单系数，最坏高估被顶到 **1.72–1.83×** | `fit_M*.json` 记录；`test_cross_family_spread_is_why_profiles_exist` |
| B provider/model tokenizer（离线） | **否决**。需下载 vocab（§61 禁）、Windows/Ubuntu wheel、冷启动、版本漂移；且 §63 明确"已有能力才复用"——仓内没有 tokenizer 资产 | `requirements.txt` 无 tokenizer 依赖（`transformers` 仅作为 `sentence-transformers` 传递依赖存在，且其词表与生成模型无关） |
| C exact-when-available + fallback | **不成立**：没有"available"的一侧 | 同上 |
| D 字节保守界 | **否决**。`tokens <= bytes` 不成立：跨家族实测 bytes/token ∈ [1.53, 5.43] | 语料 × 7 tokenizer 实测 |
| **E 混合标定（9 字符类 × 2 profile）** | **采纳** | 见 §7 |

---

## 7. 选定设计

### 7.1 9 个字符类

`word`(ASCII 字母 + `_`) / `digits` / `punct`(其余 ASCII) / `space` / `cjk`(表意+谚文)
/ `kana` / `cjksym`(CJK 标点 + 全角) / `emoji`(含 ZWJ 与 VS16) / `other`(其余非 ASCII)。

- 代码里**不含 `re`**：单趟 `for char in text` + 区间判定，纯函数、无 locale 依赖、
  不做 Unicode 归一化、不修改输入。
- `_` 归入 `word`（蛇形标识符整体成词），修正了第二估算器"每下划线 1 token"的 4× 膨胀。

### 7.2 两个 profile（唯一判定点 `profile_for_model`）

| profile | 覆盖 | 依据 | 实测区间 | 声明 T |
|---|---|---|---|---|
| `cjk_opt` | 模型名含 `qwen` / `deepseek` | Qwen2.5-3B、DeepSeek-V3 tokenizer | **[0.79, 1.26]** | 1.26 |
| `generic` | 其余一切（含未知/`custom`） | Llama-3-8B、Mistral-7B、o200k、cl100k | [0.80, 1.71] | 1.71 |

系数由**签入语料 × 7 个权威 tokenizer** 做 min-max 线性规划求得
（先最大化安全裕度、再最小化最坏高估），并在 holdout 上验证。改系数必须重跑该流程，
否则 `test_token_corpus_contract.py` 会红。

### 7.3 工具 schema：结构化估算，不是 `json.dumps` 后跑文本模型

工具 schema 的骨架（`"type":"function"` / `"parameters"` / `"properties"` …）
逐工具重复，真实 BPE 第一次之后几乎不再为它付费，而线性文本模型每次都全额计——
实测差 **1.55×**（12 个工具：线性 1748 / 真值 1126）。故改为：

```
tools ≈ 100(工具段框架) + Σ[10(工具骨架) + 12 × 属性个数(含嵌套) + 文本估算(变化内容)]
```

受控探针实测（qwen2.5:3b）：工具段框架 ≈77、同构工具边际 ≈45/个、参数边际 ≈15/个；
4 组真实/合成 schema 上复核 `est/actual ∈ [0.97, 1.21]`。

### 7.4 消息框架

实测边际 ≈5.9 token/条（含 role 与 1-token 内容）⇒ 扣掉内容与 role ≈4。
`_PER_MESSAGE_OVERHEAD_TOKENS` 取 **4**（上界取向，为 OpenAI 风格 ≈3–4/条留余量）；
role 名由文本估算另计。external 用例 OLL-E8 钉住"实测 5.74 / 估算 6.53 token/条"。

---

## 8. 修后结果

### 8.1 误差表（自然语料，`cjk_opt` profile，qwen2.5 + deepseek_v3）

| 类别 | est（旧） | est（新） | actual | r（旧） | r（新） |
|---|---:|---:|---:|---:|---:|
| 中文自然语句 | 478 | 306 | 305 | 1.57 | **1.00** |
| 中文长句 | 480 | 279 | 348 | 1.38 | 0.80 |
| 日文 | 315 | 188 | 229 | 1.38 | 0.82 |
| minified JSON | 800 | 1409 | 1220 | 0.66 | **1.15** |
| 数字数组 JSON | 890 | 2914 | 3518 | 0.25 | **0.83** |
| Python 代码 | 915 | 1112 | 1031 | 0.89 | 1.08 |
| shell | 246 | 321 | 384 | 0.64 | 0.84 |
| 标点密集英文 | 792 | 1257 | 1489 | 0.53 | 0.84 |
| emoji | 63 | 232 | 289 | 0.22 | **0.80** |
| ZWJ/组合符 | 188 | 417 | 448 | 0.42 | 0.93 |
| URL/UUID/路径 | 356 | 647 | 808 | 0.44 | 0.80 |
| 工具 schema(12) | 0 | 1104 | 1126 | 0.00 | **0.98** |
| **代表性 Elysia prompt** | — | — | 见 §8.3 | — | — |

**汇总**：

| 指标 | 旧 | 新 |
|---|---|---|
| min r | 0.22 | **0.80** |
| median r | 0.96 | 0.94 |
| max r | 1.57 | **1.26** |
| 击穿 -25%（34 条自然语料） | **11** | **0** |
| 越出 T=1.26 | 12 | **0** |

**holdout（6 条，从未参与调参）**：最小 0.80 / 最大 1.22，全部落在合同内。

### 8.2 分类矩阵（before → after）

判据：`r = est/actual`；**false accept 可能** ⟺ `r < 0.75`；
**false reject 可能** ⟺ `r > 1`（且真实值落在 `[W/r, W)`）。

| 类别 | 旧 r | 旧风险 | 新 r | 新风险 |
|---|---:|---|---:|---|
| 自然中文 | 1.57 | **误拒**（真实 ≤ W < est） | 1.00 | 无 |
| minified JSON | 0.66 | **漏放**（静默截断） | 1.15 | 无 |
| 数字数组 | 0.25 | **漏放** | 0.83 | 无 |
| emoji | 0.22 | **漏放** | 0.80 | 无 |
| 工具 schema | 0.00 | **漏放** | 0.98 | 无 |
| 标点密集 | 0.53 | **漏放** | 0.84 | 无 |

### 8.3 真实 Ollama 旁证（OLL-E4–E8）

| 用例 | 观测 |
|---|---|
| E4 截断污染可见性 | `num_ctx=1024 → 1024`；`num_ctx=32768 → 6029` |
| E5 修前 false accept | truth=8543 / at_window=8192 / legacy=5486 ≤ budget 6144 / new=9620 > budget |
| E6 修前 false reject | truth=5381 / legacy=8570 ≥ 8192 / new=5625 |
| E7 工具 schema | 12 个工具 `actual_delta=1126`，`est=1104`（ratio 0.98） |
| E8 消息框架 | 实测边际 5.74/条，估算 6.53/条（保守） |

环境：Ollama 0.24.0 / qwen2.5:3b / native `/api/chat` / `num_ctx=8192`（`/api/ps`
实测 `context_length=8192`，与 Batch 17 的 configured=requested=observed 一致）。

### 8.4 KB-13 边界（未触碰）

本批**没有**改 `contextWindow` 语义、`num_ctx` 下发、`/api/ps` 诊断。Ollama 路径上
Batch 17 的 `configured == requested == observed` 仍由
`Module/llm/tests/test_ollama_context_authority.py` 独立守着。估算器**不知道**
`num_ctx`/显存/provider 运行时（§70）。

---

## 9. 性能与内存

| 输入 | 新实现 | 旧实现 | 比 |
|---|---:|---:|---:|
| 200 字符 | 0.13 ms | 0.16 ms | 0.78× |
| 10k 字符 | 6.29 ms | 8.00 ms | 0.79× |
| 100k 字符 | 65.60 ms | 79.88 ms | 0.82× |

新实现**更快**（ASCII 快路径在区间判定之前短路）。O(n) 单趟、无正则、无分配
（只有一个 9 元计数 dict）；不缓存（prompt 每轮不同，§96）；冷启动无额外成本
（**没有** tokenizer 资源要加载）。纯函数 ⇒ 线程/async 安全（§97）。

---

## 10. 依赖影响

**零新增**。`tiktoken` / `tokenizers` / `transformers` 只用于**基准真值生成**
（`scripts/token_corpus/build_golden.py`，需 HF + 真实 Ollama，**不进 CI**），
生产估算器不 import 它们中的任何一个。词表文件不签入仓库（§124），只在 `/tmp`
缓存中用于一次性标定。

---

## 11. 回归测试分层

| 层 | 文件 | 作用 |
|---|---|---|
| **PRE_FIX_RED** | `test_token_corpus_contract.py::TestPreFixRed`（4 例） | 旧公式的**逐字复刻**放回签入语料：≥8 条越界、自然中文高估 >T、tools 权重差 ≥2000、确定性版 TE-T1 |
| **合同** | `::TestEstimatorContract`（10 例） | 逐 profile × 逐家族 × 逐语料的双向区间 + holdout + 声明值一致性 + 离散度前提 |
| **ALREADY_GREEN_GUARD** | `core/prompt/tests/test_prompt_budget.py`（26 例，含窗口回退/预算换算/截断优先级） | 预算/窗口语义逐字不变 |
| **PRODUCT_CONTRACT_GUARD** | `core/input/tests/test_character_preview_window.py`（8 例） | 预览阈值 `>=` 窗口、零 provider 请求、单一 authority |
| **接线守卫** | `core/prompt/tests/test_prompt_budget_wiring.py`（6 例，新增） | loop→assembler→builder→estimator 每一跳都不得丢 `model_id`/`tools`；行为面（tools 必须能改变裁剪结果） |
| **FALSIFICATION_GUARD** | `::TestFailureModeGuards`（9 例） | 同字符数不同分布、同字节数不同分布、minified JSON / 数字数组 / emoji / 中文、退化探针反例、空白、下划线 |
| **已知边界** | `::TestKnownLimitations`（2 例） | `repeat` 类实测区间（合同外，如实登记） |
| **真实旁证** | `Module/llm/tests/test_ollama_token_truth.py`（5 例，external） | OLL-E4–E8，需真实 daemon，**不进任何默认档位** |

---

## 12. Mutation 证据（12/12 KILLED，**非 vacuous**）

harness 先验证"目标节点存在 + 模块可导入 + 至少 1 条用例真的跑了"，再判 kill
（Batch 17 §156 的教训）。

| ID | 变异 | 结果 |
|---|---|---|
| M1 | 恢复旧 CJK 逐字 1-token 公式 | KILLED（7 failed） |
| M2 | 恢复旧"非 CJK 一律 4 字符 1 token" | KILLED（4 failed） |
| M3 | tools 不纳入 messages 总量 | KILLED（4 failed） |
| M4 | tool 结构化估算退化为整段文本估 | KILLED（2 failed） |
| M5 | unknown model 不回退（抛异常） | KILLED（1 failed） |
| M6 | profile 判定失效（全部 generic） | KILLED（5 failed） |
| M7 | 第二估算器复活（safety 走旧公式） | KILLED（1 failed） |
| M8 | 删 message 框架开销 | KILLED（1 failed） |
| M9 | preview 不把 model 传给估算 | KILLED（4 failed） |
| M10 | chat 不把 tools 传给 build_messages | KILLED（1 failed） |
| M11 | MessageBuilder 丢掉 tools 参数 | KILLED（1 failed） |
| M12 | 估算不再向上取整 | KILLED（2 failed） |

M10/M11 起初 **SURVIVED**（harness 节点写错 + 真的没有接线测试）——补了
`test_prompt_budget_wiring.py` 之后才被杀死。这正是"变异测试的价值不在绿，而在
暴露测试的空洞"。

---

## 13. 已知边界与残余风险（如实登记）

1. **`repeat` 类（合同外）**：同字符重复的 token 率由**词表**决定，字符统计无法
   区分。实测 `"哈哈"×300`=0.25 token/字 vs `"呀"×300`=1.00 token/字（字符分布同构）。
   实测区间 `cjk_opt [0.55, 5.40] / generic [0.35, 3.60]`，由
   `TestKnownLimitations` 钉住。**可达性论证**：要让这一项造成实质 false accept，
   需要在 6144 预算与 8192 窗口之间填进 ~2048 token 的低估差额 ⇒ 约 **4900 个
   同字重复汉字**（一次性粘贴），届时该轮 prompt 被静默截断（消息本身仍在历史里）。
2. **provider 侧固定框架**不可观测：默认注入的 system prompt、tool prompt 样板，
   实测 ≈22–72 token 的**有界常数**（Ollama/Qwen），由
   `test_provider_side_framing_stays_a_bounded_constant` 钉住上界 96。
3. **图片 token** 仍是固定 1024（未验证）；本批只保证**文本**估算的合同。
   `NEW_FINDING_OUT_OF_SCOPE`。
4. **profile 判定基于模型名子串**（`qwen`/`deepseek`）：只登记**有权威 tokenizer
   证据**的家族。名字里含 `deepseek` 但实际是别的 tokenizer 的社区模型会拿到
   `cjk_opt`（例如 Llama 架构的 DeepSeek-Coder-V1 系）。`mimo` 等**未验证**的中国
   模型走 `generic`（保守）。
5. **`cjk_opt` 的 `cjksym` 系数（1.33）> 1 token/字**：由共线性导致（标点只在少数
   样本里出现），实测合同成立，但它吸收了一部分其他类的方差。属"数据拟合值"，
   改语料必须重跑标定。
6. **未覆盖的 tokenizer 家族（已实测，已登记 `KB-74`）**：合同只在拟合验证过的家族上
   成立。Batch 18 用外部 tokenizer 验证（`scripts/token_corpus/validate_families.py`）：

   | 家族 | profile | min est/actual | 判定 |
   |---|---|---|---|
   | Qwen2.5-3B / DeepSeek-V3（拟合集） | cjk_opt | 0.79–0.80 | ✅ |
   | Qwen3-8B（外部验证） | cjk_opt | 0.80 | ✅ |
   | Llama-3 / Mistral / o200k / cl100k（拟合集） | generic | 0.80 | ✅ |
   | OLMo-7B（外部验证） | generic | 0.80 | ✅ |
   | **Yi-1.5-6B** | generic | **0.66**（emoji） | ⚠️ 未覆盖 |
   | **Phi-3-mini-4k** | generic | **0.63**（emoji / 繁体中文） | ⚠️ 未覆盖 |

   **为什么不用"更保守"敷衍过去**：把 Yi/Phi-3 纳入拟合会把 `generic` 的最坏高估从
   **1.71 顶到 2.19**（实测），而 `generic` 服务**所有** custom/未知使用者——
   那是拿全局上下文质量换两个家族的极端输入。**可达性**：越界样本是 emoji 类，
   要造成实质 false accept 需要上千个 emoji 的极端输入。**残余风险如实登记**，
   并把"纳入/排除"做成可执行入口而不是一句注释。
7. **workflow 子智能体路径**（`Module/workflow_engine/core/executor.py`）自带
   `tool_defs` 且**完全没有预算/估算**（`NEW_FINDING_OUT_OF_SCOPE`）。

---

## 14. 保持不变（Keep as is）

- Batch 17 的 `contextWindow` = effective runtime window（权威未动）
- Ollama `options.num_ctx` 下发规则（显式声明才发；32768 回退绝不当分配指令）
- `/api/ps` 仅诊断
- `CONTEXT_HEADROOM_RATIO = 0.75`（本批未证伪，并把它与合同下限的推导关系钉成断言）
- `SYSTEM_PROMPT_BUDGET_SHARE = 0.5`、`DEFAULT_CONTEXT_WINDOW = 32768`
- PromptAssembler / MessageBuilder 架构（只加可选关键字参数）
- 预览 API 契约与错误码（零变化）
- 公开 API / 配置 schema / 持久化 / 前端（**零变化**）
- 无 provider 网络计数调用、无自动 tokenizer 下载
