# 系统不变量（Invariants）

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

## 这是什么

本文记录**已经被测试钉住、且被认为必须长期成立**的系统不变量，并把每一条映射到
「测试 → 门禁 → CI」。

它回答一个具体问题：**某条不变量被破坏了，会有东西变红吗？**
——只写 prose 的不变量清单会退化成愿望清单；只有同时给出可执行判据，
它才是可维护的 contract。

## 怎么读

| 列 | 含义 |
|---|---|
| **Invariant** | 不变量本身（一句话，可证伪） |
| **Test** | 钉住它的测试（文件::用例）。**这是判据所在** |
| **Gate** | 该测试经由哪道门禁执行 |
| **CI** | 哪个 job 会跑到它 |

**CI 列的含义**：`push/backend` = 每次 push 到 main 的 `ci.yml:backend` job
（跑 `make test-normal`）会执行；`manual` = 只在手工触发的 `nightly.yml` 里跑
（**push 不会自动跑到**）；`—` = **当前没有任何自动执行点**。

> ⚠️ **`manual` 与 `—` 是本文最有价值的两列**。一条不变量"有测试、但 CI 不跑"
> 等于**假保障**——它在下一次回归时不会红。这类项集中列在 §7。

---

## 1. Prompt（P）

来源：[2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) §4。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **P1** | prompt 预算降级是 **render-local** 的：一次 `render` 的降级只影响本次，不得永久改写下一次的上下文 | `Module/system_prompt/tests/test_render_local_budget.py`（12 例，含异常路径恢复） | `make test-quick` / `test-normal` | **push/backend** |
| **P2** | 临时上下文**不泄漏**到 system prompt：角色/关系/情绪等运行时片段不得固化进模板正文 | `core/prompt/tests/test_system_prompt_leak.py`（21 例） | `make test-quick` / `test-normal` | **push/backend** |

**P1 的机制**：opt-in 覆盖 + `finally` 逆序恢复；`render()` 不再使用破坏性的
`set_context` 广播。判据点：
`Module/system_prompt/tests/test_render_local_budget.py :: test_a1_next_normal_render_restores_full_memory_injection`（溢出轮结束应恢复为 `None`）与
`:: test_a5_exception_during_ladder_still_restores_override`（**异常路径也必须恢复**）。

**P2 的补充网**：`core/prompt/tests/test_system_prompt_snapshot.py`（快照）与
`Module/system_prompt/sections/authored.py` 的**递归转义**（见 CLAUDE.md
「system prompt 变量转义」）。后者有 AST 守卫防二次转义。

### 1.1 Preview window contract（PW）

来源：[2026-09-18 Batch 7 执行记录](../review/2026-09-18-Batch7-provider-preview-window-执行记录.md)。
缺陷形状（canonical backlog **F8**）：prompt 预算（S2-H9，`076ebee8`）只接进了
**chat** 组装路径（`core/cognitive/loop.py::_build_turn_prompt`）；**预览**是第二条
组装路径（`core/character/preview.py`，比预算机制早 6 天落地），从未迁移 ⇒
`system + userMessage` 原样发出，小窗口 provider 上超窗 → provider 侧失败 →
路由把它折成 503 `CHARACTER_PREVIEW_FAILED`（**retryable，是谎报**：重试不会让它
消失）。

> **Provider preview requests must respect the resolved model's effective context window.**

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PW1** | 预览请求**必须装得下 resolved runtime model 的声明窗口**（估算输入 < 有效窗口）；裁剪沿用 chat 的同一实现与同一常量，不新增预览专用默认值 | `core/input/tests/test_character_preview_window.py::test_preview_fits_small_window_when_trimming_suffices`、`::test_builtin_character_preview_records_window_margin` | `make test-quick` / `test-normal` | **push/backend** |
| **PW2** | 窗口/预算权威 = **resolved runtime model**（`LLMAdapter.model` → `LLMFactory.context_window`）；不得读 `config.yaml`/bootstrap 模型，也不得新建第二份 contextWindow 表 | `::test_preview_budget_follows_resolved_model_not_config`、`::test_preview_undeclared_window_keeps_compat_policy` | 同上 | **push/backend** |
| **PW3** | **能裁进窗口的预览必须能预览**：本地拒绝的阈值是**窗口**而非预算（预算含 25% 输出余量，保护节自身就可能超过它） | `::test_preview_boundary_sends_when_fits_and_validates_when_not`、`::test_small_window_model_can_preview_a_small_request` | 同上 | **push/backend** |
| **PW4** | 估算输入**占满或超过**窗口（`>=`，含"装得下但无输出余量"）→ **本地**拒绝（400 `CHARACTER_VALIDATION`，非重试）且**零 provider 请求**；不得发送已知必失败的请求、不得自动缩预算重试 | `::test_preview_impossible_input_fails_locally_without_provider_call`、`::test_preview_boundary_sends_when_fits_and_validates_when_not` | 同上 | **push/backend** |
| **PW5** | **大窗口行为不回归**；未声明窗口的第三方 provider 维持既有兼容 fail-open（与预算同源的保守默认，不是"无限大"、也不是 0） | `::test_preview_large_window_provider_unchanged`、`::test_preview_undeclared_window_keeps_compat_policy` | 同上 | **push/backend** |
| **PW6** | 预览预算**不改变正常生成策略**：回退规则只有一份（`Module.system_prompt.budget.effective_context_window`）；**预算换算与回退规则逐字不变**（⚠️ 措辞更正：Batch 17 起，`llm.model: ollama/*` 时**出站请求**确有变化——协议、options 形状、tool_call 归一、prefix 拒绝——那是 §3.4 的领域；本条只声称**预算层**不变） | `core/prompt/tests/test_prompt_budget.py::TestEstimation::test_effective_window_is_the_shared_fallback`、`::test_budget_uses_headroom_and_default` | 同上 | **push/backend** |

**为什么拒绝用 400 而不是 503**：产品契约（Stage H §4.5）把 503
`CHARACTER_PREVIEW_FAILED` 定义为"provider/运行时**暂时**不可用"。裁完仍占满窗口是
**确定性**的输入问题（改角色档案或换模型才可解），用可重试的 503 报它等于重复
F8 的原始错误——把真相换成"稍后重试"。400 `CHARACTER_VALIDATION` 是既有错误码，
**不新增契约字段**。

**PW6 的精确边界**：`effective_context_window(None/0/负数)` 一律回退保守默认
（32768）。对 `0` 与 `None` 这与旧行为一致；对**负数**，旧式
`context_window or DEFAULT` 会把它当"已声明"并算出退化预算（`max(1, -5*0.75)=1`），
现在改为回退默认值。仓内**没有任何配置声明 0/负数**（`providers.yaml` 六家均为正数，
`LLMFactory.context_window` 对 falsy 已归 None）⇒ 该分支不可达，属 hardening。

**为什么预览不设 output cap**：chat 与预览**都不**设 `max_tokens`（provider 默认），
输出余量由 `CONTEXT_HEADROOM_RATIO`（0.75）预留；仓库没有 model 级 output cap
metadata，本批不新造（"限制输入长度和输出 token" 那句设计稿从未落成契约字段，
属独立产品决策）。

### 1.2 Token estimation contract（TE）

**Batch 18 / KB-12**：prompt 用量估算的**合同**。它回答的不是"估算多准"，而是
"预算决策在边界上是否如实"。权威实现只有一处：`Module/system_prompt/budget.py`。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **TE1** | **单一估算权威**：同一个 resolved model 下，所有 prompt 预算消费者（chat 组装、system prompt 梯子、历史裁剪、预览窗口判定、工具结果截断）走**同一份**估算实现与**同一套** profile 选择规则；不得出现第二份 `_estimate_tokens` 式公式 | `core/prompt/tests/test_prompt_budget.py::TestEstimation`、`core/prompt/tests/test_prompt_budget_wiring.py::TestAssemblerWiring`、`Module/tool/tests/test_framework.py::TestTokenEstimation` | `make test-quick` / `test-normal` | **push/backend** |
| **TE2** | 估算**确定性、本地、无 provider 网络调用**（热路径不查 token 计数端点、不下载 tokenizer、不引入 tokenizer 依赖） | `Module/system_prompt/tests/test_token_corpus_contract.py::TestEstimatorHygiene::test_determinism_across_repeated_calls`；`Module/llm/tests/test_ollama_token_truth.py`（external，旁证） | `make test-quick` / `test-normal` | **push/backend** |
| **TE3** | 估算器是**有界近似**且合同被显式声明：低估不得越过 chat 侧 25% 余量（`est/actual ≥ 0.75`），高估不得越出 profile 声明的 T；**近似不得被表述为精确计数** | `::TestEstimatorContract::test_no_family_is_underestimated_beyond_the_budget_headroom`、`::test_no_family_is_overestimated_beyond_the_declared_bound`、`::test_declared_bounds_are_the_measured_bounds`、`::test_hard_floor_follows_the_headroom_ratio`（文档即断言） | `make test-quick` | **push/backend** |
| **TE4** | 估算必须**区分实质不同的内容类**（拉丁/CJK/生僻字/假名/emoji/数字/结构化密集/消息框架/工具 schema），不得退回"按字符数线性外推"；按 resolved model 选 profile（**6 个家族**），未知模型回退**保守** profile | `::TestEstimatorContract`（全量语料 × 权威 tokenizer）、`::test_cross_family_spread_is_why_profiles_exist`（同文本跨家族差 >3×）、`::test_same_char_count_different_content_differs` | `make test-quick` | **push/backend** |
| **TE5** | **prompt 的每一段都不可漏算也不可重算**：provider request = `messages + tools`，两者一起进预算；`system` 与当前用户消息永不因裁剪丢失 | `::TestPromptComposition`、`core/prompt/tests/test_prompt_budget_wiring.py::TestMessageBuilderToolAccounting`、`::TestTurnPromptWiring::test_tool_definitions_are_taken_before_the_budget_is_spent` | `make test-quick` | **push/backend** |
| **TE6** | 未知/自定义模型**保持可用**：估算不因缺 metadata 抛错，走确定性的保守回退；配置非法（畸形 schema / 缺字段）不得让 prompt 组装崩掉 | `::TestEstimatorHygiene::test_unknown_model_falls_back_and_stays_usable`、`Module/system_prompt/tests/test_token_corpus_contract.py::TestPreFixRed::test_legacy_formula_ignores_tool_schemas_entirely` | `make test-quick` | **push/backend** |
| **TE7** | **合同外文字只许高估**：婆罗米系/东南亚/希伯来/阿拉伯/希腊/亚美尼亚/西里尔这些跨家族差 3–6× 的文字，由**固定在已验证家族实测上界**的系数覆盖 ⇒ 无静默截断面；代价（某些家族高估最多 **13.9×**：`other`=3.0 × o200k 的阿拉伯文）是显式接受的 | `::TestKnownLimitations::test_out_of_scope_scripts_never_underestimate` | `make test-quick` | **push/backend** |
| **TE9** | **回合内每一轮都要重施同一份预算**（Batch 19 / KB-77）：ReAct 的工具轮会把 assistant `tool_calls` 与 `role="tool"` 结果**原地**追加进同一个 `messages` 数组，而每一轮都整份重发 ⇒ 第 2..N 轮必须在**发下一轮之前**用同一份 canonical 预算再裁一次（先丢历史 → 再整组丢较早的工具往返 → 最后截最新一组的 tool 内容）。**永不丢**：system、当前用户消息、最新一组的配对结构——且"不产生孤儿 `role=\"tool\"`"由**结构性判据**（`turn_tail_start` + `_deletion_is_safe`）保证，不依赖边界算得对（Challenger 反证的"无 user 条目"形状已补守卫）。受保护内容**自身**超预算时如实报 `over_budget`（log ERROR），**不假装成功**——所以本条是"必须被重施 + 残留必须可见"，不是"已被强制满足"。修前实测：首轮 6123 ≤ 6144，追加一条接近单条上限（`max_result_tokens=2000`）的结果后第 2 轮 **8205 > 8192 窗口**，第 5 轮 16533；Ollama 超窗 = 200 + 静默截断（本机复测：~11k token 的 prompt 得 `prompt_eval_count=8192`、HTTP 200）。热路径成本实测 ≈23 ms（15.7k 字符/17 条）～73 ms（55.9k 字符/38k est token），O(n)（逐条可加性有守卫） | `core/cognitive/tests/test_react_round_budget.py`（7，修前 2 红）、`core/prompt/tests/test_round_budget.py`（12，含 Challenger 反证形状） | `make test-quick` / `test-normal` | **push/backend** |
| **TE8** | **CJK 扩展 B–I（生僻字）在合同内且单列一类**：它们比 BMP 汉字贵 3–7×（实测 est/actual 曾 = **0.13**） | `::TestEstimatorContract`（`cjk_ext_b_ideographs` 语料）、`scripts/token_corpus/validate_families.py` | `make test-quick` | **push/backend** |
| **TE10** | **密集变音符号的**普通拉丁**文本在合同内**（Batch 20 / KB-78）：捷克/波兰真实散文（不是合成 pangram）的 `est/actual ≥ 0.775`，逐 profile × 逐家族；证据是签入的**真散文 holdout**（12 篇 Wikipedia 导言，CC BY-SA 4.0，7 个权威 tokenizer 真值），**不参与系数拟合**。`latin_ext` 由封闭形式推导得出（`w_need = (0.775·actual − base)/n_latin_ext`，对 w 仿射 ⇒ 精确解），**只动这一个系数**、其余 10 类逐位不变 | `Module/system_prompt/tests/test_latin_prose_contract.py`（31：真散文 floor ×6 profile、低密度波兰语在场、只有 latin_ext 变了、JSON/CJK/代码无回归、推导可执行复算、fixture 哈希/许可/规模） | `make test-quick` | **push/backend** |
| **TE11** | **合同外的 `latin_accent` 只保留越南语**，且**高估代价被登记**：`latin_ext` 里"普通 Latin-1/Extended-A 变音"（捷克/波兰）与"堆叠变音"（越南语 U+1EA0–1EFF）是两套互斥的 token 经济——同一标量不可能同时覆盖（覆盖捷克/波兰所需系数会让越南语高估 2.5–3.0×；覆盖越南语所需系数是**负数**，即必然击穿硬下限）。故越南语留在 `UNCOVERED_CONTENT_CLASSES`，方向**只许高估**且区间被钉住 | `::TestKnownLimitations::test_dense_accented_latin_is_a_registered_uncovered_class`（语料侧）、`test_latin_prose_contract.py::TestVietnameseStaysOutOfContract`（真散文侧 + 代价区间） | `make test-quick` | **push/backend** |

**TE3 的两个数从哪来**：`budget = window × 0.75`，而超窗在 Ollama 上是
**HTTP 200 + 静默截断**（Batch 17 实测，从不报 400）⇒ 估算低估超过 25% 时，"预算
判装得下"就不再蕴含"provider 完整消费"。高估同样是缺陷（过早裁记忆/历史、预览误拒，
§9），故它是**双侧**合同而不是"越保守越好"。

**profile 为什么必须按家族选（一条被证伪过的假设）**：家族之间在**两条独立轴**上
分叉——①CJK 压缩率（0.58–1.21 token/字）②**数字分组**（Qwen/Mistral 逐位
≈1.0 token/位，DeepSeek/Llama/OpenAI 三位一组 ≈0.33）。两条轴**不共线**：用两个
profile 覆盖时，"数字密集"内容被迫高估 **2.4–2.8×**（Batch 18 线性规划 + 真实
Ollama 实测）。故按族分 6 个 profile；本仓默认的 `deepseek/*` 因此拿到 T=1.46
而不是 2.4。

**已验证范围**：6 个 profile 各自的依据家族（见 `budget.py` docstring 的表），
其中 Qwen3-8B（对 `qwen`）与 OLMo-7B（对 `llama`）经**外部**验证亦在区间内。
验证入口 `scripts/token_corpus/validate_families.py`（改家族时跑一次，
据此决定"纳入拟合"还是"如实登记"）。

**合同外的已知边界（实测登记，不隐藏）**：①`repeat` 类（同字符重复）的 token 率
**完全由词表决定**，字符统计无法区分——实测 `"哈哈"×300` = 0.25 token/字而
`"呀"×300` = 1.00 token/字，两者字符分布同构；②`script` 类（婆罗米系/东南亚/
希伯来/阿拉伯/希腊/亚美尼亚/西里尔）跨家族差 **3–6×**，单一系数不可能覆盖。
①② 都不参与硬合同；①的实测区间钉在
`::TestKnownLimitations::test_degenerate_repeats_stay_within_the_recorded_range`，
②的方向（**只许高估**）钉在 `::test_out_of_scope_scripts_never_underestimate`。

**真值层级（本批定义）**：G1 provider 观测输入计数（Ollama `prompt_eval_count`，
须先证明未截断）> G2 模型权威 tokenizer（HF `tokenizer.json`）> G3 第三方通用
tokenizer（tiktoken，仅在家族匹配时）> G4 估算器本身。签入的真值见
`Module/system_prompt/tests/fixtures/token_corpus/GOLDEN.json`（7 个 tokenizer +
Ollama 实测，含截断污染标记）。

## 已知边界（accepted risk，全部有测试钉住行为）

1. **保护节压过预算**：8K 级窗口上 *保护节*（F1/F2/F3/C1/C2/C6/C7）自身就可能超过
   25% 输出余量 ⇒ 预览只保证"输入 < 窗口"，不保证"输入 ≤ 预算"。不是静默降级：
   `SystemPromptManager.render` 会 warning 报出保护节规模，`build_messages` 会
   warning 报出"裁尽历史后仍超预算"。
2. **判据是有界近似估算，不是 provider tokenizer**（**Batch 18 重写**）：`estimate_tokens`
   按 **11 个字符类**加权、按 resolved model 选 profile（6 个家族：qwen / deepseek /
   llama / openai / mistral / generic，各自声明的 T 见 `budget.py` 的表），合同为
   `est/actual ∈ [0.75, T]`，见 §1.2 TE1–TE11。
   **它仍然不是精确计数**，残余边界必须知道：①**合同外的 `repeat` 类**（同字符
   重复，实测区间 [0.58, 5.34]）——词表信息不在字符统计里，无法用启发式闭合；
   ②**provider 侧固定框架**（默认注入的 system prompt、tool prompt 样板）观测不到，
   实测 ≈22–72 token 的有界常数（Ollama/Qwen）；③**图片 token** 仍是未验证的固定
   1024；④**合同外的 `latin_accent` 类**只剩**越南语**（堆叠变音符号）——它与
   **捷克/波兰**在 `latin_ext` 里是两套互斥的 token 经济：同一标量不可能同时覆盖
   （覆盖捷克/波兰所需系数会把越南语高估 2.5–3.3×；覆盖越南语所需系数是**负数**）。
   **Batch 20 / KB-78 已修**：`latin_ext` 重标定后捷克/波兰**真散文**（12 篇
   Wikipedia 导言 × 权威 tokenizer × 各 profile 的**完整声明覆盖**，含外部队列
   Yi-1.5-6B / Phi-3-mini / OLMo-7B / Qwen3-8B）全部 `≥ 0.775` 进合同（见 §1.2 TE10），
   代价是越南语从 2.47 高估到 **3.31**（方向安全，区间被 TE11 钉住）。
   `validate_families.py` 复跑 6 家族全 OK（`latin_accent` 最坏 0.82 @ Yi-1.5-6B）。
   三份未覆盖清单由 `budget.UNCOVERED_CONTENT_CLASSES` **单一定义**。旧条目所述"CJK 偏高 5% / JSON 偏低 0.6×"
   已作废：那是**退化探针**（`"你" * N`，恰好 1.000 token/字）测出来的数字，
   自然中文实测是 0.60 token/字（高估 67%），见
   [Batch 18 执行记录](../review/2026-09-19-Batch18-context-token-estimation-执行记录.md)。
3. **声明窗口 ≠ 服务窗口（Batch 17 起在 Ollama 上被绑定，云端仍是"声明即信任"）**：
   `providers.yaml` 的 `context_window` 是**手写声明值**。对 **Ollama**，Batch 17 起
   该值会同时成为 native `/api/chat` 的 `options.num_ctx`（见 §3.4 OL1/OL2），
   故"声明多少就请求多少"成立——但仍有两条残余边界：①Ollama 自身对超大值有夹取上限
   （0.24.0 实测 32768），声明高于它会再次分叉，只有诊断（`probe_runtime_context`）
   能看见；②**超窗仍是 HTTP 200 + 静默截断**，而估算器是字符启发式（见下条），
   所以"估算装得下"依然不是"provider 一定完整消费"。对**云端** provider（deepseek /
   openai / mimo / custom）Elysia 无法申请分配，其"声明 = 实际服务窗口"仍是**信任**，
   `requested` 如实记为 None。属既有条目 `OBS-2` 的家族，证据见 `KB-13` /
   [Batch 17 执行记录](../review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md)。
4. **窗口小于保护节下界**：若 `context_window` ≤ 保护节规模（实测 ~2.1K 起），
   任何预览都会被本地拒绝（PPW-3 在该区间退化为"明确拒绝"而非"永不 400"）。
   这是诚实的结果：不具备可预览性时应报"内容超窗"，而不是发一个必然被截断/拒绝的
   请求。

---

## 2. Background Task（T）

来源：[2026-09-17 Batch 1.7 执行记录](../review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) §3。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **T1** | accepted task **必然到达终态**（`COMPLETED`/`FAILED`/`CANCELLED`），绝不永久停在 `queued`/`planning`/`running`，也不静默消失 | `core/agent/tests/test_task_terminal_closure.py`（4 例） | `make test-quick` / `test-normal` | **push/backend** |
| **T2** | 每个任务**恰好一个** logical terminal outcome（first-terminal-wins；安全网与所有 push 点共用同一守卫） | 同上 + `core/agent/tests/test_task_result_durability.py`（26 例） | 同上 | **push/backend** |
| **T3** | 终态**跨进程边界存活**：所有终态事件经 `push_mailbox_event` → 唯一性守卫 → **同步 WAL sink** → 邮箱 → 轮询落 repo，**无 fast-path** | `core/agent/tests/test_task_result_durability.py`、`core/agent/tests/test_task_result_interrupt_delivery.py` | 同上 | **push/backend** |
| **T4** | 失败**归因与实际阶段一致**：规划期任何失败 → `planning_failed`（结构不合法 → `plan_too_large`），**不得**是 `internal_error`/`timeout` | `core/agent/tests/test_task_terminal_closure.py` | 同上 | **push/backend** |
| **T5** | 重启补投**不覆写**更强的终态（补投逐字不改写；`delivered` 状态与库一致） | `core/agent/tests/test_task_terminal_closure.py`（T6 用例） | 同上 | **push/backend** |
| **T6** | durable ack 只在**消费方取得所有权之后**授予（禁止态 `WAL removed ∧ delivered=True ∧ ¬accepted`） | `core/agent/tests/test_task_result_durability.py` | 同上 | **push/backend** |
| **T7** | at-least-once 重投**不改变逻辑结果**（同一终态的 replay 允许，第二次不得产生新语义） | `core/agent/tests/test_task_result_interrupt_delivery.py`、`test_task_outcome_user_semantics.py`（19 例） | 同上 | **push/backend** |

**T1/T2 的最强证据**：Batch 1.7 用 `probe_t2_race.py` 做过并发攻击扫描
（planning 失败 × `cancel()`/`stop()`，10 个时延点 × 2 种攻击 = 20 例），
**全部恰好一个 logical terminal**。该探针在 `/tmp`，**未入库**（见 §7）。

---

## 3. Runtime（R）

来源：[2026-09-17 Batch 1.6 执行记录](../review/2026-09-17-Batch1.6-runtime-authority-lifecycle-执行记录.md)、
[2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) §4、
[2026-09-18 Batch 4 执行记录](../review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md)、
[2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md)、
[2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md)（§3.3）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **R1** | **bootstrap ≠ runtime ≠ persisted**：三处配置来源各自独立，不得互相冒充 | `core/agent/tests/test_runtime_state_persistence.py`、`core/agent/tests/test_state_single_authority.py` | `make test-quick` / `test-normal` | **push/backend** |
| **R2** | runtime reporter **读 runtime 权威**（不是 bootstrap，也不是持久化快照） | `core/agent/tests/test_runtime_authority_end_to_end.py::test_switch_then_restart_keeps_every_reporter_consistent` | `make test-quick` / `test-normal` | **push/backend** |
| **R3** | **显式 CLI 覆盖跨 reload 存活**："没传" 与 "传了等于默认值" 必须可区分（argparse 的 default 不得冒充显式覆盖） | `core/agent/tests/test_cli_overrides.py`（22 例，含 `test_explicit_override_survives_every_reload`） | `make test-quick` / `test-normal` | **push/backend** |
| **R4** | 独立子系统**拥有自己的资源生命周期**（谁创建谁关闭；停机后必须 drain 再关依赖） | `core/agent/tests/test_task_llm_ownership.py`（10 例） | `make test-quick` / `test-normal` | **push/backend** |
| **R5** | **readiness 反映真实可用性**：`/ready` 的 identity 门禁读「身份已装载且权威库连着」的运行时状态，不读代理量（ping 通 / 对象存在 / repo 已构造） | `core/profile/tests/test_ready_identity_truthfulness.py::test_RHT2_ready_503_when_identity_load_failed`、`::test_RHT5c_failed_reload_does_not_serve_stale_identity`、`core/agent/tests/test_identity_readiness_lifecycle.py::test_healthy_start_is_ready_and_does_not_touch_health_check_probe` | `make test-quick` / `test-normal` | **push/backend** |
| **R6** | **关键装载失败留在运行时状态**：失败事实由拥有该 lifecycle 的对象（`UserIdentityStore`）持有并可查（`is_available` = 不可用 + `last_error` = 类型名），不得只以日志形式存在；成功恢复后必须消解，不得钉死判定 | `core/profile/tests/test_ready_identity_truthfulness.py::test_RHT1_load_failure_sets_unavailable_and_queryable_error`、`::test_RHT6_reconnect_clears_failure_and_restores_ready`、`core/agent/tests/test_identity_readiness_lifecycle.py::test_start_with_undecodable_identity_keeps_process_alive_but_not_ready` | `make test-quick` / `test-normal` | **push/backend** |

**R4 的静态面**：`scripts/check_architecture.py` 的 **R6**（裸 `create_task`/
`ensure_future` 必须逐点登记「任务存哪 / 谁取消 / 谁等待 / 异常谁看」，
19 个登记点 / 22 处 spawn）。门禁：`make check-arch`，**push/backend**。
运行期契约见 `Module/_common/task_scope.py`（`AsyncTaskScope` / `cancel_and_drain`）。

### 3.1 Readiness contract（PD-1 落纸）

> **Identity availability is required for chat readiness.**
> **Process liveness is independent from readiness.**

**三种语义，互不冒充**：

| 端点 | 回答的问题 | 判据 | 契约 |
|---|---|---|---|
| `/health` | 进程还活着吗 | 进程内存读取，**零网络探针** | **恒 200** |
| `/ready` | 对外承诺的核心 chat 能力现在可用吗 | core chat 门禁（见下） | `chatReady` → 200，否则 503 |
| `/ready` 的 `fullReady` | 长期记忆真的能持久化吗 | `chatReady` ∧ Milvus ∧ embedding | **不**门禁 HTTP，只可观测 |

**为什么「身份不可用 ⇒ 不就绪」是产品语义而不是新增严格性**：产品自己早已对同一
权威 fail-closed——`UserIdentityStore.is_available`（`_cached is not None` 且
`repo.is_connected`）是这三处的围栏判据：`/api/me`（`core/input/character_routes.py`
→ 503 `STORAGE_UNAVAILABLE`）、对话轮次的身份快照
（`CognitiveLoop._identity_snapshot` → 503）、角色服务（`service/queries.py` 预览、
`service/instances.py` 归档）。**产品不服务，readiness 就不许说就绪**；否则运维/部署
看到的绿与用户拿到的 503 直接矛盾（N-1 的实测形态：同一受控状态下 `/ready` 200
而 `/api/me` 503）。

**范围纪律（不泛化）**：本门禁只覆盖「聊天/用户请求的必要依赖」这一条身份状态，
不把 TTS/STT/Workflow/Memory/可选集成或任意 warning 塞进 `/ready`。允许
`Agent` 进程继续运行而 `readiness=false`——「不就绪」不等于「进程必须退出」。

**为什么不是 `health_check()`**：两个谓词回答两个问题，且**调用它还有副作用**。

| 谓词 | 问题 | 实现 | 副作用 |
|---|---|---|---|
| `is_available` | 身份现在能不能服务？ | `_cached is not None and repo.is_connected`（纯读） | 无 |
| `health_check()` | 权威库现在通不通？ | `repo.health_check()` → 真实 `ping()` | 失败时 `_mark_unavailable()`（`_db = None`）并**重连** |

「ping 通」不蕴含「身份已装载」：库里文档不合域模型时 repo 连得好好的，而 `_cached`
恒为 None。故 `/ready` 读 `health_check()` 会假绿；而读 `is_available` 同时让探针
（`/ready`）、修复器（`_dependency_needs_recovery("identity")`，本就是
`not store.is_available`）与产品围栏三者**同源**，并恢复「探针无副作用」。
`health_check()` 的职责**不变**——它仍是数据库恢复协调器的探针。

**未就绪的判据表**：

| 进程 | 身份状态 | chat 能力 | 期望 `/ready` |
|---|---|---|---|
| 存活 | 已装载可用 | 可用 | ready（200） |
| 存活 | 装载失败/重载失败 | 不可用 | **not ready（503）** |
| 存活 | 从未配置 repo（本地/单测模式） | 可用（本地 bootstrap） | ready（200） |
| 存活 | 失败后恢复（`connect()` 重载成功） | 可用 | ready（200） |
| 停机窗口（`_state ≠ RUNNING`） | 任意 | — | not ready（503），CR-08 |

**本契约**不**包含的判定（经复核后的接受项，防止把它读成更强的承诺）**：

1. `is_available` 只看「repo 句柄仍在 + 缓存非空」，**不**做新鲜度探测——权威库在
   缓存之后挂掉、或权威文档被**外部写入者**改坏/删除时，`is_available` 仍为真
   （缓存即服务），身份**写**路径会 fail-closed（`update_if_revision` 真查库 → 503/409），
   而恢复协调器**不会**因此重载（`_dependency_needs_recovery("identity")` =
   `not is_available` 为假）。⇒ **R5 的强度只到"装载期真值"**：它保证「装载/重载失败 ⇒
   不就绪」，不保证「缓存与权威文档逐轮比对」。要闭合这一段需要 freshness 机制
   （按 revision/updatedAt 轮询比对 + 防抖），属**独立设计**，不在本批（§77 禁止为
   两条 finding 新增 debounce/hysteresis）；已登记为 follow-up。
2. 本地模式（无 repo）`is_available` 恒 True：该模式下 `storage_path` 无生产调用方
   （`Agent` 与 `create_app` 两条装配路径都不传），且本地模式没有异步装载窗口，
   故「loading ⇒ not ready」这一行在本地模式不适用；
3. `/ready` 的 identity 门禁是**进程内状态读取**，不重新加载、不重试、不写状态；
4. `/ready` 的判据是 `is True`（显式布尔）：真值非布尔（duck-typed 替身的
   `is_connected = 1`）会被判 **not ready**。生产 `Repository.is_connected` 是
   `self._db is not None`（真布尔），故这是**有意的保守方向**而非缺陷——宁可拒绝
   一个无法确认的"可用"，也不让替身属性冒充健康（守卫：
   `test_ready_503_when_availability_is_truthy_but_not_bool`）。

### 3.2 Provider capability truth（PC）

来源：[2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md)。
canonical finding：**N-10**（预检说可用、运行期必 `NotImplementedError`）、
**F6**（provider test 测的不是跑的那个）、**F7**（`/debug` 的 `*_ok` 两个权威）。

**五层必须分开，不得互相冒充**（一个 provider 配置能走多远）：

| 层 | 问题 | 权威 |
|---|---|---|
| `CONFIGURED` | 配置/env 里有这么一条吗 | `config.<svc>.model` / `providers.yaml` |
| `RESOLVED` | 最终 provider/model 解析成什么 | 各 Module registry 的 `provider/model` 解析（含 custom 参数解析） |
| `INSTANTIATED` | 后端对象建出来了吗 | `agent.llm` / `agent.stt` / `agent.tts`（运行期句柄） |
| `SUPPORTED` | **本仓这份代码**实现了该 capability 吗 | 各 registry 的 `is_implemented()`（与 `create()` 守卫同常量） |
| `REACHABLE` | 远端此刻真的可用吗 | 只有 `--full` / `POST /api/providers/test` 能回答（需要网络） |

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PC1** | **报告的能力必须对应运行期真正在跑的那个 provider/model**：provider test 的缺省目标解析到 `agent.<svc>.model`（后端未连接才回退 config 默认值），不得测 bootstrap 配置 | `core/input/tests/test_provider_test_target_authority.py`（8 例，含真实 `switch_stt` A→B 与端到端端点） | `make test-normal` | **push/backend** |
| **PC2** | **实例化受 capability 门控**：`providers.yaml` 里声明但本仓未实现的 provider，构造期即 `NotImplementedError`；`is_implemented()` 与工厂守卫读**同一常量**，且两条装配路径还各有一道 1s 连通性探测才挂载 ⇒ "句柄存在 ⇒ 实现支持"。（依据是**工厂守卫 + 挂载期探测**，**不是** ABC——ABC 只保证方法存在，不保证有实现；Challenger 2026-09-18 实测一个把抽象方法实现成 `raise NotImplementedError` 的 `STTBackend` 子类照样能实例化） | `Module/config/tests/test_preflight_provider_capability.py::TestRuntimeFactAnchor`、`core/agent/tests/test_service_availability_truthfulness.py::TestInstantiatedImpliesImplemented`（遍历 providers.yaml） | `make test-normal` | **push/backend** |
| **PC3** | **静态预检 network-free**：`--check` 只判「配置 + resolver + capability 静态有效性」；"实现支持但服务离线"仍 PASS，连通性归 `--full` / provider test | `Module/config/tests/test_preflight_provider_capability.py::TestStaticCheckStaysNetworkFree`（含 socket 哨兵） | `make test-normal` | **push/backend** |
| **PC4** | **`/debug` 与 capabilities 是纯投影**：只报运行期已知状态，**不得**为了"更真实"主动探活（昂贵/有副作用/可能计费/拖慢端点） | `core/agent/tests/test_service_availability_truthfulness.py::TestAvailabilityReportingNeverProbes` | `make test-normal` | **push/backend** |
| **PC5** | **一个上报事实一个权威**：`/debug` 的 `tts_ok`/`stt_ok`、capabilities 的 `available`、provider test 的目标全部由**运行期句柄**得出，不允许第二份缓存标志 | 同上 `TestAvailabilityHasOneAuthority`（失败热切换后三面同答案）、`TestNoHandleMeansNotAvailable` | `make test-normal` | **push/backend** |
| **PC6** | **unsupported 不得先报成功**：未实现的 provider 在预检/`--full`/下拉里都不得报 usable（LLM FAIL、STT WARN、`--full` 不再 `[PASS] 跳过`），且不得伪装成凭据或连通性错误 | `Module/config/tests/test_preflight_provider_capability.py`（N10-1/N10-5）、`core/input/tests/test_provider_registry.py::test_unimplemented_provider_excluded_even_with_models` | `make test-normal` | **push/backend** |

**`*_ok` 的语义（PC4/PC5 的落地口径）**：

```text
*_ok = 运行期句柄存在
     = runtime usable by construction
     ≠ 远端 provider 此刻可达
```

允许这样定义，是因为**构造期已经把 capability 门住了**（PC2：未实现的 provider 建不出来，
STT/TTS 后端又都是 `ABC`），也因为 `capabilities.services.*.available`（下发给前端的
产品契约）本来就用的这个判据。**没有**任何进程内状态能回答"远端此刻可达"——
`/ready` 的教训（§3.1）是"不要拿代理量冒充可用"，而这里的边界是
"不要拿一次网络探测冒充进程状态"。

**范围纪律（不泛化）**：本条只覆盖 LLM/STT/TTS 三个 provider 子系统的
「配置 → 解析 → 实例化 → capability → 上报」这一条链，不把 embedding / vectordb /
documentdb 拉进来（它们有同形的 `NotImplementedError` 守卫，但当前没有"上报假绿"的
报告面），也不建立通用的 ProviderCapability 框架。

### 3.3 Conflict metadata truth（CM）

来源：[2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md)。
canonical finding：**N-3**（`PUT /api/me` 的 409 `details.currentRevision` 读进程内
陈旧缓存 ⇒ 谎报；客户端按它重试必然再冲突）。

**权威归属（一句话）**：乐观并发冲突时，**当前 revision 的唯一权威是权威持久化
存储里的那条记录**（`user_identity_profiles.revision`，经 repository 读取），
不是请求负载、不是进程内缓存、不是 `expected + 1`、也不是路由自己重新算一次。

| 关注点 | 权威 | 说明 |
|---|---|---|
| 请求里的 `expectedRevision` | 请求负载 | 客户端**声称**的状态；冲突后不得回显成"现值" |
| `UserIdentityStore._cached` | 运行时读缓存 | 读路径服务用；多 writer 时按定义可能陈旧 |
| 持久化当前 revision | 权威库文档字段 | **唯一权威**；读出走 `UserIdentityRepository.get()` |
| CAS 冲突判定 | repository 的 `update_one(filter={user_id, revision: expected})` | `modified_count == 0` ⇒ 未命中 |
| 409 的 `currentRevision` | repository 在冲突点回读的权威现值 | 上层只投影；未知则**省略** |

**语义（必须写清，防止被读成更强的承诺）**：`currentRevision` = **构造冲突响应那一刻**
可观察到的权威现值（response-time），**不**保证等于"击败本次写入的那个 writer 的
revision"。Mongo 的 `update_one` 在 filter 未命中时只返回计数（`matched_count=0`），
**拿不到**冲突瞬间的 actual revision；用一次额外 read 去假装拥有这种更强语义正是
§12 禁止的。并发写者 C 在 CAS 与回读之间提交时，上报的是 C 之后的值——那也正是
客户端恢复所需要的值。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **CM1** | 冲突元数据**源自权威持久化状态**：409 的 `details.currentRevision` 等于权威库里那条记录的 `revision` | `core/input/tests/test_me_conflict_metadata_authority.py::test_cmt1_conflict_reports_authoritative_revision`、`tests/real_mongo/test_real_identity_conflict_metadata.py::test_real_conflict_reports_authoritative_revision` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM2** | **陈旧请求/缓存 revision 永不冒充权威现值**：不得返回客户端提交的 expected、store 读缓存、冲突前旧值、`expected + 1`；多次 intervening write 后报的是权威现值 | `::test_cmt2_stale_cache_never_reported_as_current`、`::test_cmt3_multiple_intervening_writes_not_expected_plus_one`、`::test_cmt_repeated_conflict_tracks_authoritative_state`、`core/storage/tests/test_user_identity_repo.py::test_update_if_revision_conflict_reads_authority_not_caller_state`、`tests/real_mongo/…::test_real_multiple_writes_never_expected_plus_one` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM3** | **失败的乐观更新不得改动权威状态，也不得隐式重试**：冲突路径不重写、不 bump revision、不 save、不 repair、不 refresh 后重放；winner 的 payload 与 revision 原样保留 | `::test_cmt4_failed_writer_preserves_winner_state` | `make test-normal` | **push/backend** |
| **CM4** | **同一权威记录上的不同 writer 报同一真相**：两个独立 store/repo 实例、乃至跨进程 writer 写入后，落败方的 409 报的都是权威现值 | `::test_cmt_order_perturbation_both_orders_agree`、`tests/real_mongo/…::test_real_cross_process_writer_is_the_reported_authority` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM5** | **不知道就不说**（fail-closed）：权威现值读不到（记录已不存在）→ **省略** `details.currentRevision`；权威回读失败 → 503 `STORAGE_UNAVAILABLE`。**绝不**回退成缓存值假装权威 | `::test_cmt_missing_record_reports_no_fabricated_revision`、`::test_cmt_read_failure_after_conflict_fails_closed_503`、`core/storage/tests/test_user_identity_repo.py::test_update_if_revision_conflict_without_record_reports_unknown` | `make test-normal` | **push/backend** |

**范围纪律（不泛化）**：本条只覆盖「乐观并发冲突时的**上报值**」，不覆盖 cache
freshness（E1：外部写入者改库后热缓存不自动刷新 ⇒ `GET` 可陈旧、`PUT` 可能持续 409）。
冲突路径读一次权威库**不**构成全局 freshness 机制，也不做 conflict→cache refresh。

**CAS 本身未被削弱**：filter 仍含 `revision: expected`，`$set` 在同一原子操作内
bump 版本；没有去掉 revision filter、没有 last-write-wins、没有"取最新 revision
静默重试"。本批只改**报告真相**，不改并发保护强度。


### 3.4 Ollama runtime context authority（OL）

来源：[2026-09-19 Batch 17 执行记录](../review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md)。
canonical finding：**KB-13**（Ollama runtime context authority mismatch）、
architecture debt：**AR-16**（Ollama Provider Adapter）、product decision：**PD-12**（已裁决）。

**合同（PD-12 → A）**：`providers.yaml::llm.<provider>.context_window` 是 Elysia 对该
provider 的 **effective runtime window** 声明——**既**是 prompt 预算的来源（×0.75），
**也**是"能显式申请分配的 provider 必须按它申请"的运行时意图。它**不是**模型理论
capability（全仓没有任何 consumer 这么读它：没有 model 级 capability 表、没有 UI 展示、
没有 min(capability, config) 逻辑，而预算与预览拒绝两处都把它当**实际可服务窗口**用）。

**三分真值**（不得互相冒充）：

```text
configured  providers.yaml::llm.<provider>.context_window（唯一来源：
            LLMFactory.context_window() 与 LLMFactory.create() 读同一条目）
requested   wire 上的 options.num_ctx（ollama native）；不申请 ⇒ None
observed    GET /api/ps context_length（**只在显式诊断/集成测试里**读）
```

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **OL1** | 对**显式声明**了有效窗口的 Ollama 模型，Elysia 的 prompt 预算与向 Ollama 请求的 `num_ctx` 取**同一个值**；两者不是"两次调用同一个函数"，而是同一个 `_BACKENDS` 条目 | `Module/llm/tests/test_ollama_context_authority.py::TestConfiguredContextReachesTheWire::test_configured_context_is_requested_on_the_native_wire`、`::TestOneAuthorityForBudgetAndRuntime::test_declared_window_is_the_single_source_for_both_sides` | `make test-quick` / `test-normal` | **push/backend** |
| **OL2** | **通用预算回退绝不是运行时分配指令**：`context_window` 缺失或 ≤0 ⇒ 请求体**不含** `num_ctx`（由 daemon 默认管辖），尤其不得把 `DEFAULT_CONTEXT_WINDOW`（32768）当分配发出 | `::TestGenericFallbackIsNeverAllocated::test_missing_context_is_not_promoted_to_generic_fallback`、`::test_zero_or_negative_context_is_never_sent[0/-1/-8192]`、`::TestOneAuthorityForBudgetAndRuntime::test_missing_declaration_yields_no_allocation_on_both_sides` | 同上 | **push/backend** |
| **OL3** | Ollama 特有语义**只在 provider backend 边界内**：`CognitiveLoop` / `PromptAssembler` / preview / preflight / API 路由 / UI 里不得出现 `num_ctx`、`keep_alive`、`/api/ps`（`LLMFactory.create` 的 canonical-provider 选择是唯一例外，且是**选择**不是**语义**） | `Module/llm/tests/test_ollama_context_authority.py`（全模块只经 `LLMFactory.create` + `LLMBackend` 协议断言，不 import ollama 专用类）；`make check-arch` | 同上 | **push/backend** |
| **OL4** | configured / requested / observed **可区分**，且 observed **不是**热路径依赖：正常路径 1 user generation → 1 provider request；`probe_runtime_context()` 失败不影响 generation | `Module/llm/tests/test_ollama_backend.py::TestHotPathDiscipline::test_no_generation_entry_point_probes_the_runner`、`::test_diagnostics_failure_never_breaks_generation` | 同上 | **push/backend** |
| **OL5** | Ollama 原生 usage 字段**恰好一次**归一进通用 usage 契约（`prompt_eval_count`→input、`eval_count`→output），流式不得每 chunk 累加；终帧缺失时**不编造** | `Module/llm/tests/test_ollama_backend.py::TestStreamNormalization::test_usage_is_normalized_exactly_once_on_the_final_frame`、`::test_stream_without_final_frame_fabricates_no_usage`、`core/adapter/tests/test_ollama_stream_contract.py::TestOllamaUsageThroughAdapter::test_final_usage_is_recorded_exactly_once` | 同上 | **push/backend** |
| **OL6** | Ollama 相关改动**不得改变**非 Ollama provider 的请求体与语义（逐字段断言：`options`/`num_ctx`/`keep_alive` 一律不得出现） | `::TestNonOllamaIsolation::test_openai_payload_is_byte_free_of_ollama_fields`、`::test_deepseek_payload_is_byte_free_of_ollama_fields`、`::TestBackendSelection::test_an_ollama_looking_url_does_not_change_the_protocol` | 同上 | **push/backend** |

**协议决策（native vs OpenAI-compatible）**：`bash` 层面实测（Ollama 0.24.0 / `qwen2.5:3b`，
冷启动 A/B）`/v1/chat/completions` **带与不带** `options.num_ctx` 都是 `context_length=4096`
（静默忽略），native `/api/chat` 才生效 ⇒ "在通用后端里加个字段"**物理上不成立**，必须走
native（§33 协议切换门：native 确实需要 / 通用端点不能表达 / streaming·tools·multimodal
实测可保持 / 本批补了 wire 级确定性用例 + 真实 daemon 旁证）。**反过来也钉住**：指向
`localhost:11434` 的 OpenAI 兼容 provider **不会**被 URL 启发式劫持成 native 语义
（`test_an_ollama_looking_url_does_not_change_the_protocol`）。

**silent truncation 的真实形态（独立复核 2026-09-19，load-bearing）**：Ollama 超窗时
**丢中间**——只保留最前 `num_keep` 个 token（**默认 4**）+ 最近 `num_ctx - num_keep`。
而 Elysia 的预算保护的是 **system prompt + 当前消息**、丢的是最老的历史：两边保护的
正好相反 ⇒ 一旦 budget 与 `num_ctx` 分叉，**最先静默劣化的是人设/system prompt**。
这就是"预算 == `num_ctx`"（OL1）为什么是承重条款，而不是整洁偏好。

**warm runner 行为（实测，决定"要不要自动 unload/retry"）**：Ollama 0.24.0 会按请求的
`num_ctx` **重载** runner（4096→8192→4096 观测均跟随；同值请求复用，`load_duration`
0.11s vs ~2.2s）⇒ **不需要**任何自动卸载/重试（§70 禁止的方向），本批也不实现。

**已知边界（诚实列出，不是"已覆盖"）**：

1. **Ollama 自身上限夹取**：`num_ctx=100000000` 在 0.24.0 上实测被夹到 `32768`（
   HTTP 200，无错误）。故若把 `context_window` 声明到高于 Ollama 的夹取上限，
   configured > observed 会**再次**分叉——本批按 §41 **不新增硬编码上限**，
   该分歧只由 `probe_runtime_context()` 的诊断暴露（`observed != configured`），
   已登记为 follow-up。
2. **工作流（task LLM）路径不受控**：`raw_client` 仍是 OpenAI 兼容侧车
   （`WorkflowEngine` Planner/Executor 的 SDK 形状契约，本批范围外）⇒ 后台任务在
   ollama 上仍按 daemon 默认窗口服务。已登记。
3. **探活的分配语义要分清（独立复核 2026-09-19 纠正了本节初稿）**：
   `test()`（`POST /api/providers/test` 的 **connectivity** 级）刻意不带 `num_ctx`
   ——它问的是"远端此刻可用吗"（PC 家族），不是一次预算过的 generation。
   但 **usable** 级会走真实 `chat()` ⇒ **会申请分配**（`core/input/provider_routes.py`
   的 `_run_usable_test`）；而 `--full` 对 ollama 是**直接跳过**连通性检查
   （`Module/config/preflight.py`）。故 `requested_context` 的语义准确表述是
   "**generation 请求会带的值**"，不是"所有出站请求都不带"。
4. **`observed` 不是 allocation 的精确账本**：`/api/ps` 是 **daemon 级、最后写入者胜**
   的视图，且小值会被夹（独立复核实测 `num_ctx=0/1/4` 在 ps 上读作 4，而 llama 实际
   分配 **256** 格；`257` → ps 257 而实际 512）。故它只能作"紧接着我们自己的请求之后"
   的旁证；任何并发客户端（`ollama run`、另一个应用）都能改写它。
5. **Ollama daemon 默认窗口随显存变化**：二进制口径是"按显存取 4k/32k/256k"
   （`OLLAMA_CONTEXT_LENGTH` / `--context-length`），本机 6GB 实测 4096。
   所以"未声明 ⇒ 不申请"这一支的**实际窗口是机器相关的**，本批不把它写成常数。
6. **runtime observation 不进任何自动档位**：真实 daemon 旁证
   (`Module/llm/tests/test_ollama_real.py`) 只标 `external`（`make test-external`），
   CI 不依赖本机 Ollama/GPU（§114）。它属于 §7 的"有测试但自动化跑不到"家族。

---

## 4. User-visible semantics（U）

来源：[2026-09-17 Batch 1.8 执行记录](../review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md)。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **U1** | 用户可见 outcome **等于** logical terminal outcome | `core/agent/tests/test_task_outcome_user_semantics.py`（19 例，端到端：真 mailbox → 真 `drain_mailboxes_once` → 真 EventBus → 真 `_handle_task_result` → 真 `_prepare_think_input`） | `make test-quick` / `test-normal` | **push/backend** |
| **U2** | 内部细节**可以压缩，但不得反转**：细节可压扁，**失败绝不翻成功** | 同上（每条"不得宣称成功"都配一条**正向分类断言**） | 同上 | **push/backend** |
| **U3** | **live 与 replay 语义相等** | 同上 | 同上 | **push/backend** |
| **U4** | `failed ≠ completed`（两者不得互相冒充） | 同上 | 同上 | **push/backend** |
| **U5** | `cancelled` **独立可辨**（不并入 failed/completed） | 同上 | 同上 | **push/backend** |

**U 的单一映射点**：内部终态 → 用户级语义只经**一个**函数（Batch 1.8 §1 第 5 条）。
这样"映射正确"只需在一个地方成立，而不是在每个消费方各自成立。

### 4.1 Pagination completeness（PG）

来源：[2026-09-18 Batch 8 执行记录](../review/2026-09-18-Batch8-pagination-completeness-执行记录.md)。
缺陷形状（canonical backlog **P2-01r**）：会话列表的数据源是
`SessionRepository.list_all()` 的 `find({}, sort=[("updated_at", -1)], limit=200)`
——**单页上限被当成了数据集上限**；`SessionManager.list()` 在这个 ≤200 的窗口里
做游标过滤与搜索，于是第 201+ 条记录既翻不到也搜不到，而最后一页**谎报**
`has_more=false`（客户端据此停止）。设计文档（批次 3 spec §4「列表游标（keyset）」）
从未允许任何数据集上限——这是"实现缺失"，不是产品决策。

> **A client following the published pagination contract can eventually reach every
> visible record in a stable result set.**

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PG1** | **可达性完备**：稳定数据集里，客户端只依据公开合同（`has_more` / `cursor`）继续请求，最终能拿到**全部**可见记录；不得存在"前 N 条可见、其余永久不可达" | `core/input/tests/test_sessions_pagination.py`（21 例：201/225 条 + 边界矩阵 0/1/50/51/199/200/201/225 + search 分页）、`core/memory/tests/test_session_manager_pagination.py`（覆盖层/离线/竞态）、`core/storage/tests/test_session_repo_pagination.py`（存储层原语） | `make test-quick` / `test-normal` | **push/backend** |
| **PG2** | **单页上限 ≠ 数据集上限**：`limit` 只约束一页；取数必须**在存储层分页**（keyset 谓词 + 排序 + `limit` 下推），不得"预取全量再切片"，也不得"取前 N 条再分页" | `core/storage/tests/test_session_repo_pagination.py::TestQueryShape::test_page_read_never_fetches_more_than_the_page`、`::test_sort_and_cursor_predicate_are_pushed_down`、`core/memory/tests/test_session_manager_pagination.py::TestReachabilityBeyondTwoHundred::test_list_does_not_prefetch_the_whole_dataset`（结构性：用户列表路径碰 `list_all` 即红） | 同上 | **push/backend** |
| **PG3** | **不重不漏 + 稳定序**：完整分页的 ID 并集 **等于**权威可见集合、两两交集为空；排序 `updated_at DESC, session_id ASC` 的 tie-break **必须下推到查询**（相同时间戳跨页不重复不遗漏） | `test_sessions_pagination.py::TestBoundaryMatrix`、`::TestReachabilityBeyondTwoHundred::test_two_hundred_and_one_sessions_all_reachable`、`test_session_repo_pagination.py::TestStorageLevelPagination::test_identical_timestamps_tie_break_across_boundaries`、`tests/real_mongo/test_real_session_pagination.py::test_identical_timestamps_use_storage_level_tie_break` | `make test-quick` / `test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **PG4** | **元数据自洽**：`has_more=true ⇔ cursor 存在`；走到末尾是"空页 + has_more=false"（200），不是 404/400；`limit` 被夹取只影响**单页**大小，不改变可达性 | `test_sessions_pagination.py::TestMetadataCoherence`、`::TestPageSizeCapIsNotDatasetCap` | 同上 | **push/backend** |
| **PG5** | **单文档读与列表窗口无关**：`GET /api/sessions/{id}` 的摘要按 ID 直读（不再依赖"最近 N 条"列表），否则第 201+ 条的详情会兜底成 `updatedAt: ""`（前端契约要求非空） | `core/memory/tests/test_session_manager_pagination.py::test_get_summary_is_authoritative_beyond_the_old_cap`、`core/storage/tests/test_session_repo_pagination.py::TestSummaryReads`、`tests/real_mongo/test_real_session_pagination.py::test_single_document_summary_is_window_independent` | `make test-quick` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **PG6** | **失败的真相**：翻页中途的存储读失败**不得**折成"短页 + has_more=false"（那是把故障谎报成"列表到底了"）；离线降级只在首页（或以同一个降级页游标续翻）成立 | `test_session_manager_pagination.py::TestAdversarialRegressions::test_mid_walk_storage_failure_is_not_reported_as_end_of_list`、`::test_offline_repo_falls_back_to_local_summaries_with_pagination` | `make test-quick` / `test-normal` | **push/backend** |

**为什么"下推"而不是"提高 200 上限"**：把 `limit=200` 改成 2000 只是把边界往后挪
（数据集总会超过它），并且每次列表请求都要读全量文档（会话 doc 内嵌消息窗口）。
PG2 因此把"存储层分页"本身当成不变量：修复的验收判据之一是**单页读取量不得随
数据集增长**（`test_page_read_never_fetches_more_than_the_page` + 结构性守卫）。

**为什么 tie-break 必须在查询里**：内存 fake 的排序是 Python Timsort（同值稳定），
真 Mongo 对相等排序键**不作任何顺序保证**——只按 `updated_at` 排序时，相同时间戳
的记录在页边界可能被重复或漏掉。该不变量由真库用例证明（`push/real-mongo`）。

**已知边界（原登记项已关闭）**：Batch 8 登记的"缺 `updated_at` 的历史文档在 keyset
谓词下不可达 / 页边界游标被写成 `""` 导致其后记录被掐断"（canonical backlog
**KB-15**）**已由 [Batch 16](#49-session-data-integritysd) 修复**：排序键改为**全域
定义**（有序桶 ⊕ legacy 桶，见 SD1–SD3），游标编解码对称（`""` 是合法的 legacy 桶
Sentinel）。原 xfail 用例已改写为正式断言 `test_missing_updated_at_row_is_reachable`。

### 4.2 Speaking 生命周期（SP）

来源：[2026-09-18 Batch 9 执行记录](../review/2026-09-18-Batch9-speaking-state-recovery-执行记录.md)。
缺陷形状（canonical backlog **KB-6**）：`TTSStreamManager.wait_all()` 没有超时预算，
而"某一轮讲话真正结束"的唯一信号就是它返回（`FSMBackedPhaseController.finish_turn`）。
一旦有**任何**门控（`_release_events[seq]`）永远不会被 `set`，`wait_all()` 就永不返回，
FSM 永久停在 **SPEAKING**——回合仍持有 `_process_lock` ⇒ 新用户消息只进
`_pending_messages`（只在 `STATE_CHANGE→IDLE` 时出队），**纯文字聊天里 Agent 不再回复**，
且"再说一句"救不回来（新消息不触发 interrupt），只能等一次 VAD/停止按钮或重启。

三条挂起源：
① `_download_and_forward` 的两个**早退分支**在 `try/finally` 之外直接 `return`（S2-G3 已修）；
② `reset_for_turn` 直接 `clear()` 门控 dict，让已持有旧 `Event` 的等待者永久挂起（S2-G3 已修）；
③ **任务在"首个 event-loop step 之前"被取消** ⇒ 协程体一行都不执行、早退分支与 `finally`
全都没进 ⇒ 放行从未发生（Batch 9 修）。③ 的可达路径是用户在本轮播放中点掉 TTS 再打开
（`Agent.set_service("tts", False/True)` → `set_enabled(False)` 的同步段里 `cancel_all()`；
`BaseEventLoop._run_once` 先 poll I/O 再跑本批 ready，所以 WS handler 的 step 能与
"本轮入队句子"的 step 同批，而 TTS 任务的 step 要等下一批）。

**SPEAKING 的语义**（本批核对，未重新定义）："Agent 正在向用户输出本轮的 response"，
**不是**"扬声器正在出声"——首句到达即进入（与 TTS 是否可用无关），退出由**服务端输出工作**
的完成决定，客户端播放不被追踪。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **SP1** | 每次进入 SPEAKING 都有**明确的退出 owner**（正常：`finish_turn`；异常/打断：`handle_interrupt`），不存在"进了没人负责出"的路径 | `core/cognitive/tests/test_speaking_lifecycle.py`（16 例，真实 `process()` → 真实 `AgentFSM.transition`；每条先断言**确实进过 SPEAKING**再断言终态） | `make test-quick` / `test-normal` | **push/backend** |
| **SP2** | success / failure / cancel / interrupt / empty / shutdown **都不能**留下 `state == SPEAKING` 且"当前输出工作 == 无" | 同上逐场景；`assert_no_speaking_work_left()` 同时断言无未落定 TTS 任务、无未释放门控 | 同上 | **push/backend** |
| **SP3** | 旧 speaking epoch 的**迟到完成**不得覆盖新一轮（不得把它切成 IDLE，也不得提前放行它的门控） | `test_speaking_lifecycle.py::test_stale_completion_from_interrupted_turn_cannot_overwrite_next_turn`；`core/adapter/tests/test_tts_adapter.py::test_stale_task_from_previous_turn_does_not_forward` | 同上 | **push/backend** |
| **SP4** | 完成由**生命周期完成**驱动，**不是**"队列空"/"任务已创建"：输出工作还活着时不得提前退出；门控链必须闭合（放行不得依赖"协程体是否运行过"） | `test_speaking_lifecycle.py::test_speaking_does_not_exit_while_output_work_is_alive`；`core/adapter/tests/test_tts_adapter.py::test_prestart_cancel_releases_successor_gate`（PRE_FIX_RED） | 同上 | **push/backend** |

**明确不做**（本批的取舍，避免"看起来更稳"的假修复）：不给 `wait_all()` 加超时、
不加 SPEAKING watchdog / 超时复位、不新增第二个 generation 计数器、不引入说话生命周期协调器。
超时只会把"永久卡死"换成"提前离开 SPEAKING 而音频仍在播"——那违反 SP4 与 SPEAKING 的语义。

### 4.3 Thinking 生命周期与取消边界（TH）

来源：[2026-09-18 Batch 10 执行记录](../review/2026-09-18-Batch10-thinking-state-cancellation-recovery-执行记录.md)。
缺陷形状（canonical backlog **KB-22**）：`CognitiveLoop.process()` 在 `_prepare_turn()`
**之前**就取得回合锁（`async with self._process_lock`），但取消边界（`except
asyncio.CancelledError → _handle_interrupt()`）原本只覆盖 `_think_loop` 那一段。而
`_prepare_turn` 在 `await self.phases.enter_thinking()`（FSM 已是 THINKING）**之后**
还有一个真挂起点——`await self._generation_registry.register(...)`（Mongo upsert），
它只被 `except Exception` 包着，而 Python 3.8+ 起 `asyncio.CancelledError` 是
**`BaseException`**（实测 `__mro__ = (CancelledError, BaseException, object)`），
外层 `try` 又只 `except (CharacterBusyError, StorageUnavailableError)`。

于是落在该窗口的取消**一路逃出 `process()`**：`async with` 退出时**释放锁**，
canonical 收口 `_handle_interrupt()`（唯一做 `THINKING → INTERRUPTED → IDLE` 的地方）
永不执行 ⇒ FSM 永久停在 **THINKING**。`status.is_accepting_input` 恒假 ⇒ 之后**每一条**
用户消息都走 `process_message` 的排队分支、被 ACK `accepted` 后进 `_pending_messages`，
而 pending 只在 `STATE_CHANGE → IDLE` 出队（那个事件再也不会来）⇒ 进程活着、EventBus
活着、消息能进队列，但**没有任何回合再启动**——用户感知为 Agent **永久失聪**。
仓内唯一恢复点是 `Agent.start()` 的 `fsm.reset()`（`core/agent/lifecycle.py :: AgentLifecycle.start`）。

**THINKING 的语义**（本批核对，未重新定义）：回合**已被接受、正在做本回合的准备工作与
认知工作**（选项 C）——`enter_thinking()` 在 generation 建档与 prompt 组装**之前**，
把会话解析/身份快照/角色 runtime 换绑都算在内。它是"本回合已开始"的对外声明，
所以**不**把它挪到 prepare 的尾部（见下方"明确不做"）。

**ownership 判据**（本批的核心决定）：收口只在"**本回合**确实已经开始"时执行，判据是
`_generation_ctx`——它在 `enter_thinking()` **之前**建立、在 `_prepare_turn` 入口清空，
且 `_prepare_turn` 全程在 `_process_lock` 之内（不可能混入第二个回合的 ctx）。
**明确不用** `fsm.state == THINKING`：那分不清"我的 THINKING"与"别人的 THINKING"，
也分不清"本回合还没开始、相位上留着上一回合的 `IDLE_BACKGROUND_TASK`"。
ctx 赋值与 `enter_thinking()` 的 FSM transition 之间**没有 await**，因此"ctx 非 None"
与"FSM 已经/即将是 THINKING"**严格等价**——即使某个 `on_change` 订阅者在
`enter_thinking()` 的 `_notify` 上挂起（`on_change` 是公开订阅点，
签名就是 `Callable[[str], Awaitable[None]]`），判据也不会误判。这条由
`test_cancel_inside_enter_thinking_notify_still_recovers` 钉住（Challenger A3b）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **TH1** | 每次进入 THINKING 都有**turn-scoped 的终态 owner**（含 prepare 窗口）：接受/失败/取消/打断/被取代都必须有明确责任方把它推离 THINKING | `core/agent/tests/test_thinking_cancel_recovery.py`（4 例，真 `Agent.start()`/`process_message`/EventBus/INTERRUPT）；`core/cognitive/tests/test_thinking_cancellation.py`（8 例，真 `process()` → 真 `AgentFSM.transition`） | `make test-quick` / `test-normal` | **push/backend** |
| **TH2** | 本回合开始之后的取消**或普通异常**都**不能**留下"state == THINKING 且本回合已无存活 owner"：收口必须走 canonical 路径（`THINKING → INTERRUPTED → IDLE`），**不得**用 `finally: set_idle()` 之类的补丁（`THINKING → IDLE` 在 `VALID_TRANSITIONS` 里是**非法边**） | `test_thinking_cancellation.py::test_cancel_after_thinking_entry_recovers_via_canonical_interrupt`（PRE_FIX_RED，断言状态序列 `["THINKING","INTERRUPTED","IDLE"]`）、`::test_cancel_during_error_recovery_is_also_covered`（PRE_FIX_RED，取消落在异常收口自身）、`::test_generic_exception_after_turn_started_still_recovers`（PRE_FIX_RED，普通异常同样有 owner）、`::test_cancel_inside_enter_thinking_notify_still_recovers`（PRE_FIX_RED，判据对挂起的 `on_change` 订阅者免疫）、`test_thinking_cancel_recovery.py::test_cancelled_thinking_turn_recovers_and_next_message_is_processed`（PRE_FIX_RED） | 同上 | **push/backend** |
| **TH3** | 旧回合的 cleanup **不能**覆盖新回合：A 的收口与 B 的 `enter_thinking()` 之间由 `_process_lock` 串行化——B 进不来，A 也不会迟到 | `test_thinking_cancellation.py::test_old_turn_cleanup_cannot_overwrite_newer_turn`、`::test_cleanup_scoped_by_ownership_not_by_fsm_value`（用 FSM 取值当 ownership proof 的实现会红） | 同上 | **push/backend** |
| **TH4** | FSM 真相 / 回合任务所有权 / `_process_lock` 所有权三者一致：取消后不得出现"锁已释放 + 无存活 owner + 仍是 THINKING"，也不得让停机语义被"复活成可接受输入的 IDLE"破坏 | `test_thinking_cancel_recovery.py::test_stop_during_thinking_window_does_not_strand_the_agent`（PRE_FIX_RED：断言相位不留在 THINKING、`run_state == STOPPED`、停机窗口拒收新消息）、`test_thinking_cancellation.py::test_cancel_before_thinking_entry_is_safe`、`::test_pre_thinking_cancel_preserves_other_turn_background_phase` | 同上 | **push/backend** |

> **TH4 的成立条件（历史，Batch 11 已闭合）**：TH4 曾附带一条限定——"收口能在停机 drain
> 预算（3s）内走完时才成立"。若收口自身被**第二发** `task.cancel()`
> （`AsyncTaskScope.drain` 超时后的直接取消，绕过 `cancel_current()`）打断，相位会停在
> THINKING/INTERRUPTED 而 `run_state == STOPPED`（Challenger 用"`mark` 卡住 > 3s"
> 确定性复现：`stop elapsed=3.00s fsm=thinking run_state=STOPPED`）。
> **该限定已由 Batch 11（§4.4 CF1–CF6）解除**：终态收口现在对重复取消免疫，
> TH4 不再有前置条件。
| **TH5** | 相位忙时被 ACK 接受的消息（pending）在恢复后必须**经既有 drain 机制**重新获得处理权（`STATE_CHANGE → IDLE` → `_handle_state_change` 出队），不新增调度器/轮询 | `test_thinking_cancel_recovery.py::test_message_queued_during_recovery_window_is_processed`（PRE_FIX_RED，生产取消源 = 模型热切换） | 同上 | **push/backend** |

**明确不做**（本批的取舍，避免"看起来更稳"的假修复）：不把 `enter_thinking()` 挪到
prepare 尾部（会改变 UI/打断/观测时机，而根因是取消边界不是入口时机）；不写
`finally: set_idle()`（A 的迟到收口会打掉 B）；不新增 `_thinking_epoch` / 第二套
生命周期计数器（现有 `_generation_ctx` + `_process_lock` 已足够）；不加 watchdog /
超时复位 / `asyncio.shield` 包整个 `process()`（那会破坏用户打断）；不改 FSM 转换表、
不改 EventBus 优先级、不改 pending 出队机制。

**已知边界（本批登记，未修）**：

- ~~**入口之前的取消**（O1）：本回合已 ACK 的消息静默结束（没有任何 `generation.failed` 终态）。~~
  **已由 Batch 12 关闭**（canonical backlog **KB-24**）：取消仍然不给这条输入一个
  回复（`accepted` 不承诺回复），但它现在**必须**落一个可归因的显式终态
  （`disposition=cancelled_pre_ownership` + 取消来源 + message/session 身份），
  且**未接管输入的回合不得被新输入抢占**（新输入排队）。见 §4.5 AD1–AD6。
- **`_prepare_turn` 的回合级写入不回滚**（O3）：取消传播时（入口之前）`_session` /
  `_prompt` / tool_context / `_tts_mgr.reset_for_turn` / `_clock` 的写入保留；`_clock`
  会残留成非 None（消费者都有"本回合会重设"的前提，当前无害）。
- **`IDLE 已广播 / 锁未释放`窗口**（O2）：若收口/回合在 post IDLE 之后继续挂起（例如某个
  会挂起的 `on_change("idle")` 订阅者），pending 出队 spawn 的回合会卡在锁上，而
  "下一条消息"可能把它当作在途回合取消掉 ⇒ 已 ACK 的排队消息被丢弃且顺序被抢。
  需要"有订阅者挂起"这一前提（仓内今日没有），生产形态还要求收口后仍持锁挂起。

> **原"第二发取消打断收口链"边界（KB-25 / KB-23）已由 Batch 11 修复**，见 §4.4。

### 4.4 取消安全的回合收口（CF）

来源：[2026-09-18 Batch 11 执行记录](../review/2026-09-18-Batch11-cancellation-safe-turn-finalization-执行记录.md)。
缺陷形状（canonical backlog **KB-25**，与 **KB-23** 同 root）：一个回合的终态收口是一条
**长 await 链**——

```text
process() 捕获取消/异常
  → _handle_interrupt() / _handle_turn_exception()
      → output.turn_cancelled(...)         ← await（出站帧，~5s 上界）
      → generation_registry.mark(...)      ← await（Mongo upsert，**无上界**）
      → _cancel_desktop_calls()            ← await（桥 RPC，**无上界**）
      → output.typing(False)               ← await
      → FSMBackedPhaseController.handle_interrupt()
          → fsm.transition(INTERRUPTED) + bus.post   ← 同步
          → _notify("interrupted")                    ← await
          → asyncio.sleep(0.05)                       ← await（UI 可观测延迟）
          → fsm.transition(IDLE) + bus.post(IDLE)     ← ★ 逻辑终态 ★ 同步
          → _notify("idle")                           ← await
```

`asyncio.CancelledError` 是 `BaseException`（实测 `__mro__ = (CancelledError,
BaseException, object)`），既不被沿途的 `except Exception` 接住，也**不被兄弟 `except`
子句接住**（在 `except` 块里抛出的异常只由外层 handler 处理）。于是：

- **第二发** `task.cancel()`（生产唯一形态 = `Agent.stop()` 第 3 步
  `_turns.drain(timeout=3.0)` 预算耗尽后的直接 `task.cancel()`，
  `Module/_common/task_scope.py :: AsyncTaskScope.drain`；`cancel_current()` 本身是 one-shot，**不可能**发第二发）
  只要落在上面任何一个 await 上，CancelledError 就穿过收口链逃出 `process()`；
- 同理，**第一发**取消落在 `_handle_turn_exception` 的 await 上（think 路径的
  `except Exception` 分支不在任何取消边界内——`except` 块里抛出的异常**不被兄弟子句**
  接住，但**会被外层 try** 接住，故 prepare 路径上的同一个 handler 自 Batch 10 起就已覆盖，
  think 路径没有这层包裹）也照样逃出。这一半登记为 **KB-31**（`USER`/`HIGH`：
  `run_state` 仍是 RUNNING ⇒ 用户可见的永久失聪），与 KB-25 **同 root、同 owner、
  同 invariant、同 fix**，但守卫互斥、可达性相反 ⇒ canonical 上**不合并**
  （`SAME_ROOT_DISTINCT_REACHABILITY`，不是 KB-23 那种 `SAME_ROOT_ALIAS`）。

后果是同一个：`_process_lock` 由 `async with` 释放、回合任务以 cancelled 结束，
而相位**留在 THINKING 或 INTERRUPTED**——`status.is_accepting_input` 恒假 ⇒ 之后每条
消息都被 ACK `accepted` 后进 `_pending_messages`，而 pending 只在 `STATE_CHANGE → IDLE`
出队（那个事件再也不会来）⇒ **永久失聪**；仓内唯一恢复点是 `Agent.start()` 的
`fsm.reset()`（`core/agent/lifecycle.py :: AgentLifecycle.start`）。

**修复的形状（owner-side，不 shield 整块）**：把收口显式劈成两段——
**逻辑终态**（同步、无 await：`transition(IDLE)` + `bus.post(STATE_CHANGE IDLE)`）
与 **best-effort 资源清理**（可取消）。前者放进 `finally`：

- `FSMBackedPhaseController.handle_interrupt()`：`_notify("interrupted")` +
  `sleep(0.05)` 包进 `try`，IDLE 落位/广播/通知放进 `finally`；并且对 `IDLE` 早退
  （幂等，同时避开 `IDLE → INTERRUPTED` 这条**非法边**）；
- `CognitiveLoop._handle_interrupt()`：best-effort 段包进 `try`，
  `await self.phases.handle_interrupt()` 放进 `finally`；
- `CognitiveLoop._handle_turn_exception()`：分类 + `turn_failed` + `mark` 包进 `try`，
  `await self._handle_interrupt()` 放进 `finally`。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **CF1** | 一旦某个回合已开始**终态收口**（无论它由取消、异常还是正常收尾触发），**该收口过程本身**必须对后续取消免疫——再发任意多发 `task.cancel()` 都**不能**阻止它达到稳定、逻辑一致的 terminal lifecycle state | `core/cognitive/tests/test_cancellation_safe_finalization.py`（16 例：**7 例**逐 await 注入第二发——`turn_cancelled` / `mark` / `desktop.pending_calls` / `typing(False)` / `_notify("interrupted")` / `sleep(0.05)` + 三连发；**1 例**异常收口被取消 = KB-31 的 loop 级形态；另 8 例是普通异常清理、幂等与 5 条 ALREADY_GREEN 守卫）、`core/agent/tests/test_turn_finalization_cancellation_safety.py`（5 例，真 Agent + 真 WS 停止按钮 + 真 `_turns.drain`，含 **KB-31 的 USER/RUNNING 形态**） | `make test-quick` / `test-normal` | **push/backend** |
| **CF2** | **runtime terminal truth 先于 ownership 释放**：不得出现"锁已释放 / 回合任务已结束，而 FSM 仍宣称 active（THINKING/EXECUTING/SPEAKING/INTERRUPTED）" | 同上的每一条都断言完整状态序列 `["THINKING","INTERRUPTED","IDLE"]`（不重不漏）+ `process_lock.locked() is False`。**KB-31 的专属判别点**：`test_cancel_during_error_finalization_settles`（loop 级）、`test_stop_button_during_error_finalization_does_not_deafen_agent`（Agent 级，`run_state == RUNNING` 下断言相位离场 + 第二条消息被处理）——修前 2 红 | 同上 | **push/backend** |
| **CF3** | 为完成逻辑终态而做的保护**不得吞掉** caller 的取消意图：terminal obligations 完成后取消仍须传播/可观察 | `test_second_cancel_*` 全部断言 `task.cancelled() is True`（第二发取消**不是无效的**）；`test_second_cancel_after_terminal_transition_keeps_terminal_state` 划出临界区右边界 | 同上 | **push/backend** |
| **CF4** | **best-effort 外部清理不得阻塞核心生命周期终态**：TTS/desktop/出站帧/Mongo 清理卡住或抛普通异常，FSM 仍必须落到 IDLE | `test_ordinary_exception_in_cleanup_still_settles_state`（`typing(False)` 抛 RuntimeError，PRE_FIX_RED）、`test_second_cancel_during_desktop_cleanup_settles`（清理被放弃是允许的，相位不许被带走） | 同上 | **push/backend** |
| **CF5** | 收口仍然是 **turn-scoped** 的：旧回合的第二发收口**不得**影响新回合（不 IDLE 新回合、不 mark 新 generation、不与 B 的 `enter_thinking` 交错） | `test_old_turn_finalization_does_not_interleave_with_new_turn`（断言 A 的 `interrupted/idle` 严格早于 B 的 `thinking`）、`test_pending_message_is_processed_after_second_cancel` | 同上 | **push/backend** |
| **CF6** | 为收口做的保护**不得让停机失去上界**，也不得让停机期间**产生新的回合生命周期** | `core/agent/tests/test_turn_finalization_cancellation_safety.py::test_shutdown_second_cancel_keeps_shutdown_bounded`（真实 `Agent.stop()` + 真实 3s drain 预算 + `mark` 卡住 ⇒ elapsed 有界、相位离场、`run_state == STOPPED`、回合派发轴 `["thinking","interrupted","idle"]` 无第二个回合、`_current_task is None`） | 同上 | **push/backend** |

**权威归属（一句话）**：第一次取消之后，**逻辑终态归被取消的那次 `CognitiveLoop.process()`
调用**（持 `_process_lock` 者）；它必须在释放锁之前把相位推到 IDLE。相位写入的**唯一**
owner 仍是 `FSMBackedPhaseController`；`_handle_interrupt` 只是流程编排。

**分类（两类 cleanup）**：

| 收口步骤 | 类别 | 第二发取消可以打断吗 |
|---|---|---|
| `fsm.transition(INTERRUPTED)` + `bus.post` | **MUST_COMPLETE**（逻辑终态第一段） | ❌ 同步，不可打断 |
| `fsm.transition(IDLE)` + `bus.post(IDLE)` | **MUST_COMPLETE**（逻辑终态；pending drain 的唯一触发点） | ❌ 放在 `finally` 的同步段 |
| `_notify("interrupted")` / `sleep(0.05)` / `_notify("idle")` | observability（best-effort） | ✅ 可打断；打断后 IDLE 仍由 `finally` 落位 |
| `output.turn_cancelled` 帧 / `generation_registry.mark` / `_cancel_desktop_calls` / `_tts_mgr.cancel_all` / `output.typing(False)` | **BEST_EFFORT / CANCELLABLE** | ✅ 可打断/可放弃（`_handle_interrupt` 的 `finally` 仍会补做逻辑终态） |

**明确不做**（避免"看起来更稳"的假修复）：不 `asyncio.shield` 整个 `_handle_interrupt`
或 `process()`（会让清理逃出 `_process_lock`，破坏 CF5/TH3；shield 的 inner task 无人
await 时还会变成 detached 清理任务）；不做 retry-until-clean；不引入
`uncancel()`（全仓零使用）也不把 `Task.cancelling()` 变成业务 authority（它在全仓仅
用于 `Module/_common/task_scope.py::cancellation_is_external` 这一个判据）；不改 FSM
转换表、不新增状态、不加 watchdog / 超时复位 / 第二套生命周期 epoch；不把资源清理重排
到逻辑终态**之前**（会改变 wire 帧顺序与 50ms INTERRUPTED 可观测窗口）。

**已知边界（本批登记，未修）**：

- **persisted 终态是 best-effort**：若第二发取消落在 `generation_registry.mark` **之前**
  （例如落在 `output.turn_cancelled` 上），那条 generation 行会停在 `running`。按
  §17/§18 的优先级（runtime truth 优先于 persisted truth）这是**允许**的，且有既有兜底：
  启动期 `GenerationRegistry.mark_stale_interrupted(cutoff)`
  （`core/agent/_lifecycle_persistence.py :: _start_persistence` 与 `:: _recover_dependency` 两处调用点）把它扫成 `interrupted`。
  真库旁证：`tests/real_mongo/test_real_finalization_cancellation.py`（`make test-real-mongo`，
  **push/real-mongo**）。
- **被放弃的资源清理**：第二发取消若恰好落在 `_cancel_desktop_calls()` 上，在途 desktop
  调用可能不被逐条 `cancel_call`（由 `DesktopCallRepository.mark_stale_expired` 启动期
  兜底）；TTS 任务同理可能留到下一次 `cancel_all()`/停机。这些都属于 BEST_EFFORT
  资源清理，不构成 CF1/CF2 违反。
- **`handle_interrupt()` 的 `_notify` 是顺序 best-effort**：逻辑终态走的是 `finally`，
  但 `_notify("interrupted")` 若被某个订阅者永久阻塞，`finally` 也进不去；而 `_notify("idle")`
  若在迭代中途被取消，**其后**的订阅者收不到通知（`_notify` 逐个 `await`，
  `CancelledError` 不是 `Exception`，不会被逐订阅者的 `except Exception` 吞掉）。
  仓内唯一 `on_change` 注册者是 `wait_until_idle`（同步 `event.set()`，其生产调用点只有
  `Module/bench/bench.py`，且自带超时），故当前不可达；与 TH 的 O2 边界同源
  （Challenger B11 的 C 项）。
- **"必然落到 IDLE"是机制保证，但以 `transition`/`bus.post`/`_notify` 不抛为条件**：
  `finally` 体内任一步抛异常都会**顶替**正在传播的 `CancelledError`（任务以 failed 而非
  cancelled 收场），且 `handle_interrupt` 的 `try` 起点在 `transition(INTERRUPTED)` +
  `bus.post(INTERRUPTED)` **之后**。当前实现下这两步都不会抛（`transition(IDLE)` 是合法边、
  `bus.post` 是**无界队列**的 `put_nowait`、`_notify` 逐订阅者吞 `Exception`），故不可达；
  但**将来在 `finally` 里新增任何可能抛异常的语句时必须重新评估**（Challenger B11 的 H 项）。
- **幂等的口径限于"IDLE 入口"**：`handle_interrupt()` 的守卫是
  `state == IDLE → return`，因此"收口完成后再调一次"是无副作用 no-op；
  但两个**并发** owner 同时进入时（第二个进来时相位已是 INTERRUPTED）会重复广播
  `interrupted`（`transition` 同相位 no-op、`bus.post` 照发）。当前唯一调用者是持
  `_process_lock` 的回合任务 ⇒ 不可达；若将来引入第二个调用者（IdleMonitor / Agent 层打断），
  必须先把判据从"IDLE 早退"升级为"完整 terminal 状态判据"。

### 4.5 Accepted input delivery / admission ownership（AD）

来源：[2026-09-18 Batch 12 执行记录](../review/2026-09-18-Batch12-accepted-input-delivery-closure-执行记录.md)。
缺陷形状（canonical backlog **KB-24**，Batch 10 登记为 O1）：一条用户输入从
"已被接受"到"回合真正接管"之间有一段**没有任何 owner** 的窗口——

```text
Agent.process_message() → bus.post(UserInputData) → return True   ← WS 层据此回 accepted=true
  → _handle_user_input()（同步：spawn 回合任务 + 登记 _current_task）
  → CognitiveLoop.process() → await _process_lock
  → _prepare_turn(): _generation_ctx = None
                     await session_resolver(...)      ← 真挂起点
                     await registry.get_or_load(...)  ← 真挂起点
                     _generation_ctx = GenerationContext(...)   ← ★ admission commit point
  → enter_thinking() → _think_loop() → _build_turn_prompt()
                                       └─ 到这一步输入才 session.add_message(...)
```

窗口内这条输入**不在** session/archive、**不在** `_pending_messages`，EventBus 对
USER_INPUT **没有重投**（`_handle_interrupt` 只搬移不复制），`generation.*` 一个都没发。
落在窗口内的取消会把回合任务连同这条已 ACK 的输入一起抹掉——**修前触发它不需要任何
异常条件**：用户在窗口内再发一条消息，`_handle_user_input` 的旧判据
（`prev not in _started_cognitive_tasks`）只认"coroutine 是否 first-step"，于是把
"已 first-step、停在 `_prepare_turn`"的回合当成"真正在跑"而抢占它。两条消息都
`accepted=true`，最终只有第二条有回应；而 v2 幂等表已把第一条落成 ACCEPTED，
客户端拿同一 `clientRequestId` 重试只会被**逐字重放那个 accepted**。

> **`accepted` 的真实合同（本批先确认、后修）**：`accepted` = **Transport Accepted**
> ——"调用已进入 runtime 生命周期"（`bus.post` 成功，或有界 `_pending_messages`
> append 成功）＋ 决策幂等（同 `clientRequestId` 重放同一 ACK）。它**不**承诺
> 持久化、**不**承诺一定被回复、**不**承诺失败一定告知。因此下面的不变量**不是**
> "每条 accepted 都会得到回复"（用户可以取消、系统可以停机），而是
> "**accepted 之后这条输入始终有一个 lifecycle owner 或一个显式终态**"。
> 进程崩溃后的恢复**不在**该保证范围内（`accepted ≠ durable`）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **AD1** | 一旦输入获得 runtime-level `accepted`，它必须**始终**由以下之一拥有：active turn、`_pending_messages`、或显式终态。不得存在"无 owner"窗口 | `core/agent/tests/test_admission_input_delivery.py::test_admission_gap_incoming_input_queues_and_nothing_is_lost`（PRE_FIX_RED）、`::test_third_message_arriving_while_second_is_still_on_the_bus`（三条都不得丢） | `make test-quick` / `test-normal` | **push/backend** |
| **AD2** | admission commit point（`_generation_ctx` 建立，与 `enter_thinking()` 之间无 await）**之前**的取消不得让已接受输入无声消失：必须落一个**可归因**的显式终态 | `::test_admission_gap_stop_button_cancels_with_explicit_disposition`（PRE_FIX_RED：断言 `disposition=cancelled_pre_ownership` + 取消来源）、`::test_stop_button_clearing_pending_is_attributable`（PRE_FIX_RED：pending 清空同样可归因） | 同上 | **push/backend** |
| **AD3** | **未接管输入的在途回合不得被抢占**：新输入排队（FIFO 不抢），只有真正接管了的回合才允许被新输入打断 | `::test_admission_gap_incoming_input_queues_and_nothing_is_lost`（PRE_FIX_RED）、`::test_admission_gap_preserves_fifo_order`（PRE_FIX_RED，M1→M2 原序） | 同上 | **push/backend** |
| **AD4** | admission recovery **不得**让同一条逻辑输入被处理两次（原子交接：owner 要么是回合、要么是 pending，不得同时） | `::test_cancel_after_history_append_does_not_duplicate_processing`（边界之后：断言该输入在 session 中**恰一条**、LLM **恰被喂一次**、pending 为空——不是 `<= 1`，那对"一次都没处理"同样成立） | 同上 | **push/backend** |
| **AD5** | 显式取消与停机**不得复活**本应保持取消的输入；recovery 也不得把 shutdown / 用户 stop 的取消意图偷偷撤销 | `::test_admission_gap_stop_button_cancels_with_explicit_disposition`（不 requeue、之后不得被处理）、`::test_stop_button_clearing_pending_is_attributable` | 同上 | **push/backend** |
| **AD6** | 正常快速路径与"Agent 真忙 ⇒ 排队"两条既有路径**不因本组不变量而改变**（零调度跳变、零新增轮询）；且"已接管输入的回合不被新消息抢占"这一既有语义保持（用户连发两条 = 排队，不是打断） | `::test_normal_fast_path_unchanged`、`::test_busy_agent_still_queues_after_fix`（ALREADY_GREEN_GUARD）、`::test_new_input_does_not_preempt_a_turn_that_owns_its_input`（PRODUCT_CONTRACT_GUARD） | 同上 | **push/backend** |
| **AD7** | ownership 判据**不得陈旧**：新回合一旦开始执行，就不得再冒领上一回合的 `_generation_ctx`（否则停在 `_process_lock` 上的回合会被误判为"已接管"，第三条消息抢占它 ⇒ 已 ACK 输入静默丢失且**零 disposition**） | `::test_ownership_judgement_is_never_stale_for_a_new_turn`（Challenger C 反证的回归守卫） | 同上 | **push/backend** |
| **AD8** | 排队消息不得因为"在途回合从未离开 IDLE"而永久滞留：回合结束（取消/异常/正常）后若已空闲且仍有排队消息，必须重新获得处理权——复用既有出队路径，不新增调度器/轮询 | `::test_turn_ending_without_state_change_still_drains_pending`（PRE_FIX_RED；生产形态 = P0-3 `StorageUnavailableError` 在窗口内 fail-closed） | 同上 | **push/backend** |

**SE1/SE2（Batch 20 补，KB-41）——非用户来源的回合也不得抢占"尚未接管输入"的回合**：

AD3 的判据（`not_owning` → 排队）当初只接在 `_handle_user_input` 上。另有**两条**
非用户来源的回合入口——`_handle_system_tick`（主动搭话）与 `_handle_task_result`
（后台任务终态唤醒）——它们**无条件** `cancel_current()`。于是同一个 admission 窗口
里，一个恰好到达的 tick 或任务终态会把用户回合连那条**已 ACK** 的输入一起取消
（`cancelled_pre_ownership`、不进 session/pending、不重投）。窗口窄（tick 门是相位
IDLE + 1s 轮询；task result 要恰好完成）但真实。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **SE1** | 系统来源的工作**不得**在 ownership 交接窗口里抹掉或搁浅已 accepted 的用户输入：判据与 AD3 **同一份**（`_turn_is_pre_ownership`，读 `current_turn_owns_input` + `_started_cognitive_tasks`）；系统事件的处置按其**可再生性**区分——proactive tick 可再生（`IdleMonitor` 1s 轮询 + `is_completely_idle` 门）故**放弃并留痕**；`TaskResultData` **不可再生**故**推迟**（`_deferred_task_results`）并在用户回合落定后重新入队 | `core/agent/tests/test_system_event_preemption.py::test_se_t1_accepted_input_survives_system_tick_in_window`（PRE_FIX_RED）、`::test_se_t2_accepted_input_survives_task_result_in_window`（PRE_FIX_RED）、`::test_se_t4_multi_input_with_system_event_keeps_fifo`（PRE_FIX_RED）、`::test_se_t6_no_silent_loss_under_event_flood`（PRE_FIX_RED） | `make test-quick` / `test-normal` | **push/backend** |
| **SE2** | 修 SE1 **不得**让系统事件饿死，也不得改变用户取消的优先级：proactive 回合在真正空闲时照常起（liveness）；`INTERRUPT`（停止按钮/语音取消）**仍照常**抢占窗口内的回合 | `::test_se_t3_system_event_first_then_user_input`、`::test_se_t7_system_event_not_starved`、`::test_se_t9_interrupt_source_unchanged` | 同上 | **push/backend** |

**SE1 的推迟语义（与 Issue B 的关系）**：推迟路径**刻意不写** durable ack——
`delivered=True` 的唯一授予点仍是 `_handle_task_result` 里 **spawn 之后**那一步。
重启时清空暂存（权威是库里 `delivered=False` 那一行，由 `task_result_redelivery`
补投）；停机时清空并记 `disposition=deferred_by_shutdown`。

**明确不做**（本批取舍）：不延后 ACK（会把"排队时客户端长时间没有 ACK"引入 wire
契约）、不把 ACK 前移成持久化（本批**不是** durable queue 项目）、不新增 FSM 状态 /
转换边、不新增 wire 枚举、不改 pending 出队机制、不加 watchdog / 轮询 /
`asyncio.shield` 包 `process()`、不把 session history 当工作队列、不对所有
`CancelledError` 一律 requeue（那会复活用户 stop 掉的输入）。

**已知边界（本批登记，未修）**：

- ~~**连发三条时 pending 内的既有乱序**~~ → **Batch 13 已确证并修复**（KB-38），
  见 §4.6。修前：上一条消息还排在 **EventBus 队列**里（事件消费者尚未把它交给
  `_handle_user_input`）时，`process_message` 因 `bus.pending_user_inputs > 0` 把新消息
  **直接** append 进 `_pending_messages`；随后消费者才把旧消息也 append 进去 ⇒ 两条在
  pending 里反序（实测 `['M3','M2']`，处理顺序 `M1 → M3 → M2`）。
- **终态归因是日志级**：显式终态只存在于运行时日志
  （`disposition=cancelled_pre_ownership` / `failed_pre_ownership` /
  `cancelled_by_interrupt` + source + message/session 身份；Batch 14 起另有
  `cancelled_by_shutdown`，见 §4.7），**不**产生 wire 帧、**不**写库。这与"不为满足
  文字不变量增加无用 DB 记录"的取舍一致；客户端因此仍无法区分"这条被取消了"与
  "这条从未被处理"（→ 新登记 KB-40）。
- **"接管之后、进历史之前"这一段没有归因**：`cancelled_pre_ownership` 只在
  `_handle_prepare_cancelled` 里打，而该 handler 只覆盖 `_prepare_turn` 之内的落点；
  取消若落在 `_think_loop` → `_build_turn_prompt`（`core/cognitive/loop.py :: CognitiveLoop._build_turn_prompt`，即 `session.add_message` 之前）这一段，处置只有一句 `<<< 用户打断`。
  **Batch 14 已确证这是可达的**（USER：直投输入或停止按钮落在这个窗口）→ 新登记
  **KB-46**（原 N7），与"该输入是否应进会话历史"这一产品决策绑定，本批**只登记不修**。
- **v1/REST 输入的身份为空**：`_admission_message_id` 取自 v2 预分配的 `messageId`；
  v1/REST 路径没有这个 ID ⇒ 终态日志里 `message_id=-`（只有 conversation_id）。
  身份正是"最需要归因"的低版本路径上最弱的那一环（→ 新登记）。
- **`_drain_pending_after_turn` 只在 `is_completely_idle` 时出队**：它刻意**不**越过
  `IDLE 已广播 / 锁未释放`（KB-28）那条边界——若收口仍持锁，新回合会卡在锁上而被
  下一条消息当作在途回合取消。**Batch 14 实测**：该边界今天不会造成丢失——新输入
  （公开入口与直投）在 Batch 12 的 `not_owning` + Batch 13 的 `_has_older_waiting_input`
  之下都会**排队**而不是取消那个卡在锁上的回合；原始"下一条消息取消它并吞掉已 ACK
  输入"的形态在 HEAD 上**已不成立**（见 §4.7 RO2 与 KB-28 的 reclassify）。

---

### 4.6 Pending input ordering（PO）

KB-38（Batch 13 确证并修复）。缺陷形状：一个用户的连续输入在一个时间段里可能同时
存在于**两个没有共同顺序**的容器里——"已派发等待"的 `_pending_messages` 与"尚未派发"
的 EventBus 队列——而**两个 append 权威按写入时刻排序**：

```text
process_message(M2) → bus.post(UserInputData(M2))        ← 快速路径：M2 的 owner 是总线
process_message(M3) → _pending_messages.append(M3)       ← 判据 bus.pending_user_inputs > 0
   ⋮
消费者派发 M2 → _handle_user_input(M2) → append(M2)      ← M2 到这里才入队 ⇒ ["M3","M2"]
   ⋮
出队 pop(0)（FIFO）⇒ 处理顺序 M1 → M3 → M2               ← 与 admission 顺序相反
```

**根因不在 EventBus**：总线对同优先级事件按 `Event.seq` 严格 FIFO（`post()` 里
`seq += 1` 与 `put_nowait` 之间没有 await）。根因是**顺序仲裁的信息面不完整**，分两层：

1. **入队顺序依据错了**（写入时刻 ≠ admission 时刻）：`process_message` 在 admission
   时刻写队列、`_handle_user_input` 在**派发**时刻写队列，两条路径按写入先后排列。
2. **等待集只数了一半**：真正要回答的是"本条之前还有没有 accepted 的输入在等"，而"在等"
   有**两个容器**（`_pending_messages` ∪ 总线驻留的 USER_INPUT）。只看 `_pending_messages`
   会在"队首已 `pop(0)` 交给总线、还没被 claim"的窗口里失明；更晚的输入在这个窗口里能拿到
   **更小**的总线 `seq`（`ws_handler.py` STT / `ws_v2.py` voice.commit /
   `voice_sessions.py` REALTIME 直投甚至不经 `process_message`），于是先被派发、先成为
   active turn。

```text
T0 已接管、产出中；M2（更早）忙时入队 ⇒ pending=[M2]
V1（更晚）**直投** ⇒ bus=[V1]                     ← 不经 process_message
出队口把 M2 交给总线 ⇒ bus=[V1(seq 小), M2(seq 大)]
派发 V1 时 M2 已不在 pending ⇒ 只看 pending 的判据失明 ⇒ V1 先成为 active turn
⇒ claim / session / LLM 三层证据全反序
```

第 2 层是本批 **Challenger A 独立反证**出来的：第一版修复只做了第 1 层 + "只看 pending"的
越队判据，当时 21 条守卫全绿、顺序仍可被反演。

#### 合同定义（Phase 0，先确认后修）

| 概念 | 定义 |
|---|---|
| **ordering domain** | 单个 **Agent 实例**的 accepted 输入流。一个 FSM / 一个 `_pending_messages` / 一个 `_cognitive.current_task` / 一把 `_process_lock` ⇒ 回合在全 Agent 上串行（与 session 无关）。**不**跨进程承诺（`accepted ≠ durable`），也不承诺跨连接到达顺序——合同是"已 accepted 的输入按 admission 顺序处理"，与它来自哪条连接无关。 |
| **admission order** | `Agent.process_message()` **提交准入决策**的那一刻：`reserve_work`（v2，可能挂起）**之后**、分支判定（直投总线 / 入队）**之前**——两者之间**没有 await**，所以该顺序是全序。`request_id`（`next_request_id()` 的 `{n:03d}` 单调计数器）就是这一时刻的见证，也是排序键——**不新增**第二套 message identity。 |
| **processing order（canonical）** | **active-turn ownership commit**：`_generation_ctx` 建立（`CognitiveLoop.current_turn_owns_input` 变真，`loop.py::_prepare_turn` 内一次无 await 的赋值）。与 Batch 12 的 admission commit / Batch 10 的 ownership 判据同一点。session 历史追加与 LLM 首轮只作旁证。 |
| **既有合同证据** | `process_message`（"Agent 忙碌时消息入队（FIFO），IDLE 时出队处理"）、`pending_messages`（"FIFO 队列中暂存的消息"）、`_dispatch_pending_message`（"队首……（FIFO 出队）"）、AD3（"新输入排队（FIFO 不抢）"）、前端按发送顺序渲染且无 priority/reorder 指示。**本批不新增合同**，只把既有 FIFO 承诺贯通到整条 handoff 链。 |

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PO1** | 在 ordering domain 内，保持可处理的 accepted 输入按 **admission 顺序**获得 active-turn ownership（不论当次走直投总线还是进 pending，也不论两条入队路径谁先写入） | `core/agent/tests/test_pending_input_ordering.py::test_three_message_burst_keeps_admission_order`（PRE_FIX_RED）、`::test_five_message_burst_keeps_admission_order`（PRE_FIX_RED）、`core/agent/tests/test_admission_input_delivery.py::test_third_message_arriving_while_second_is_still_on_the_bus` | `make test-quick` / `test-normal` | **push/backend** |
| **PO2** | pending 只有**一个**入队权威（`_enqueue_pending`），且队列**始终按 admission 顺序有序**；出队仍是 `pop(0)` = 取最老 | `::test_three_message_burst_keeps_admission_order`（层 2 断言 pending 顺序）、`::test_two_message_boundary_unchanged` | 同上 | **push/backend** |
| **PO3** | 存在**更早** accepted 的输入在等（等待集 = `_pending_messages` ∪ **总线驻留的 USER_INPUT**）时，新输入（含不经 `process_message` 的直投输入）**不得**直接成为 active turn 越过它 | `::test_newer_direct_input_cannot_bypass_older_pending_input`、`::test_newer_input_cannot_enter_bus_while_older_pending_waits`（Challenger A 反例的 `process_message` 变体）、`::test_direct_input_cannot_overtake_when_older_pending_is_in_flight`（直投变体）——三条均 PRE_FIX_RED | 同上 | **push/backend** |
| **PO4** | 恢复（出队重投 / admission recovery）**保留原 admission 顺序**：不得给同一条输入分配新顺序，也不得把更老的输入排到更晚的输入之后 | `::test_error_in_admission_window_then_drain_keeps_order`（PRE_FIX_RED：窗口内失败后 M2→M3 原序）、变异 M5（出队重盖 `request_id` → 5 条守卫红） | 同上 | **push/backend** |
| **PO5** | 显式取消只移除被取消的逻辑输入：停止按钮清空 bus + pending（既有合同，可归因），**幸存者保持相对顺序**，被清除者不得复活 | `::test_stop_button_clears_pending_without_reordering_survivors`、`core/agent/tests/test_admission_input_delivery.py::test_stop_button_clearing_pending_is_attributable` | 同上 | **push/backend** |
| **PO6** | 顺序修复不得制造**重复 owner**，也不得在停机时**复活**已取消的输入（连回合都不许起） | `::test_shutdown_does_not_drain_pending`（变异 M6a/M6b 各自红）、`core/agent/tests/test_admission_input_delivery.py::test_cancel_after_history_append_does_not_duplicate_processing`、`_assert_full_order` 的 no-duplicate / no-omission 断言 | 同上 | **push/backend** |

**明确不做**（本批取舍）：**不改 EventBus 的优先级 / 派发顺序 / drain 语义**（总线本来就是
同优先级 FIFO；本批只给它加了一个**只读快照** `pending_user_input_request_ids()`，供顺序仲裁
看见总线驻留者）、不换 pending 容器（`list` + 有界插入足够）、不新建 scheduler / durable
queue / priority queue、不把所有输入一律塞进 pending（保持 fast path 与"真忙才排队"两条既有
路径）、不改输入层三条直投入口的调用形态、不新增 wire 字段或公开 API、不加锁、不用
`sleep` / task 创建顺序 / lock 公平性碰运气（顺序由 admission 键与 barrier 测试决定）。

**快路径判据收敛为一处**：`not status.is_accepting_input or _has_older_waiting_input(rid)`——
`process_message` 与 `_handle_user_input` 共用同一个 helper，"更早的等待者"只有一个定义。

**已知边界（本批登记，未修）**：

- **排序键是 `request_id` 的单调性**：本批复用它作为 admission 顺序的见证（§31
  "优先复用既有 identity"）。它由 `test_admission_order_key_is_monotone` 钉住——将来把
  它改成 UUID / 每会话计数 / 随机值会让顺序静默错乱，而那条守卫会先红。
- **停机 / 显式中断不覆盖**：顺序不变量只约束"保持可处理的 accepted 输入"；停止按钮与
  `Agent.stop()` 清空 bus + pending 是既有终态合同（PO5/PO6 只要求"不复活、不重排幸存者"）。
  **Batch 14 更正**：这句话的**代码**此前只做到 pending 那一半——`Agent.stop()` 不清总线，
  于是总线驻留的 accepted 输入会跨 `stop()→start()` 边界被新 epoch 消费（**KB-47**）。
  现已按本句的合同补齐（见 §4.7 RO1）；顺序不变量本身仍不覆盖停机。
- **停止按钮只支持整队取消**：没有"只取消 pending 里某一条"的接口 ⇒ §38 的
  "cancel 中间项"当前 **N/A**（PO5 只覆盖整队清除后的相对顺序）。
- **直投输入的抢占语义（保留）**：直投输入在**等待集为空**时仍会走 spawn 分支抢占已接管
  输入的回合（既有行为，本批未改；§34 Keep-As-Is）。本批只挡住"等待集里有更早输入"时的越队。
  副作用是"已 claim 但未进历史就被抢占"的丢失形态 → **Batch 14 已确证为 USER 可达并登记
  KB-46**（原 N7；含"停止按钮落在同一窗口"这一更宽的触发面），本批只登记不修（产品决策 +
  KB-30 依赖，见 §4.7 的 RO3 未建立说明）。
- **排序键读不到时的口径是保守的**：本条读不到 ⇒ 只要等待集非空就排队；等待者读不到 ⇒
  视为更早。今天 5 个 `UserInputData` 生产者全都带 `next_request_id()`，所以两支都不可达；
  它们存在的意义是"将来新增入口忘带键时退化成排队，而不是静默越队"（Challenger A/B 各自把
  这条不可达性变成了可执行事实）。

---

### 4.7 Runtime epoch ownership（RO）

来源：[2026-09-19 Batch 14 执行记录](../review/2026-09-19-Batch14-residual-input-ownership-triage-执行记录.md)。
缺陷形状（canonical backlog **KB-47**，Batch 13 登记为 **N8**）：`EventBus` 的生命周期与
`Agent` 实例相同（`EventBus()` 在 `Agent.__init__` 构造），而同一实例的
`stop() → start()` 是**新的 runtime epoch**——

```text
事件消费者被钉在一个真实的挂起点上（如 _handle_state_change → output.notify_state）
  → process_message(M1) → bus.post(UserInputData(M1)) → return True（= 已 accepted）
  → Agent.stop()：_state=STOPPED / _pending_messages 清空 / 消费者取消
       —— 但 M1 仍在**总线队列**里（取消消费者 ≠ 清队列）
  → Agent.start()：新 epoch 的消费者取到的第一条事件就是 M1
  ⇒ 被当成**新输入**处理：进 session 历史、喂 LLM、可能再次触发工具副作用
```

"非 durable 的 accepted 输入不得跨运行被消费"**早已是写下的合同**，只是代码只做了
一半：`docs/architecture/invariants.md` 本节 §4.6 的 PO5/已知边界（"停止按钮与
`Agent.stop()` 清空 bus + pending 是既有终态合同"）、§4.5 的 AD5（"显式取消与**停机**
不得复活本应保持取消的输入"），以及输入层的 S2-R2-4 判据
（`core/input/ws_handlers/chat.py` / `core/input/voice_sessions.py`：
"停机窗口内输入层不再向 bus 投任何事件……**残留事件跨运行被消费本身就是缺陷族**"，
commit `c029110e`）都按"停机即结束 ownership"写；而 `Agent.stop()` 此前只清 pending。

> **术语**：**runtime epoch** = 两次 `start()` 之间的一段运行期。同一实例的
> `stop()→start()` 是**新 epoch**（`fsm.reset()` + `reset_lifecycle_state()` +
> `_started_cognitive_tasks.clear()` + 新消费者）；`clear_work()` 的 docstring 亦写明
> 目的是"避免同一 Agent restart 残留"。本批**不引入** `RuntimeEpoch` 框架/字段——
> epoch 只是"停机边界"的语义说法，机制仍是"stop 时结束 ownership"。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **RO1** | `Agent.stop()` **结束**所有"非 durable、未被任何回合接管"的 accepted 输入的 ownership——**两个容器都算**（总线驻留 ＋ `_pending_messages`）：停机返回后总线**不得**再驻留 `USER_INPUT`（不得跨 epoch 复活）；且**两处的丢弃都必须可归因**（`disposition=cancelled_by_shutdown` + 身份）并释放 v2 session-work 引用 | `core/agent/tests/test_runtime_epoch_input_ownership.py::test_stop_boundary_ends_input_ownership_and_prevents_resurrection`（PRE_FIX_RED：修前 `bus.pending_user_inputs==1`，restart 后该输入进 session/被喂 LLM 且零 disposition）、`::test_stop_boundary_attributes_queued_pending_input`（PRE_FIX_RED：修前 pending 被清空却零日志，而停止按钮路径对同一处置记 `cancelled_by_interrupt`） | `make test-quick` / `test-normal` | **push/backend** |
| **RO2** | 停机清扫**按 event type 分类**，不得退化为全清：`TaskResultData` 等 durable 终态（§2 T3/T6）必须原位保留；幸存者的出队顺序（`priority, seq`）与计数/快照簿记不得被改变 | `::test_stop_boundary_preserves_durable_events`（PRE_FIX_RED；变异 M2"粗暴全清"⇒ 本条红）、`core/monitoring/tests/test_event_bus_ownership.py`（4 例：分类 / 顺序不变 / 空集 no-op / 已派发者不受影响） | 同上 | **push/backend** |
| **RO3** | **合法 transient 不得改变用户可见的 ownership 语义**：`_current_task` 指向已结束任务（**KB-26**）与 `FSM=IDLE + _process_lock 仍持有`（**KB-28**）都可以存在，前提是它们不产生 bypass / duplicate / omission / resurrection。Batch 14 以证据**证伪**了两者的用户可见后果（见下方"合法 transient 的边界"） | Batch 14 用**当次临时探针**逐个触发了 KB-26 的全部 5 个生产读者（5 例）与 KB-28 的 4 例；探针位于 `tmp/`、**未入库**（按纪律不登记为仓库路径），证据见[Batch 14 执行记录](../review/2026-09-19-Batch14-residual-input-ownership-triage-执行记录.md)（稳定来源）。两者当前**只作证伪记录**，未入库为常驻守卫——若要长期钉住，需在后续批次把探针**转正为回归测试**，届时本节改引测试 node id | — | — |

**RO1/RO2 的边界（明确不做）**：不新增 `RuntimeEpoch` 管理框架 / 不新增 epoch 字段、
不做 restart 侧过滤（`start()` 在 spawn 消费者**之前**就已 `_state = RUNNING`，那期间
被接受的输入是**合法**的新 epoch 工作，start 侧清扫会误伤它们）、不改 FSM / 不改
pending 容器 / 不改 `_process_lock` / 不加 watchdog 或轮询、不触碰 wire 与 schema。

**RO4（Batch 20 补，KB-48）——准入边界必须覆盖**每一条**直投入口**：
`stop()` 建立的是**硬**准入边界：在 epoch N 创建的生产者不得在 N 已停止后把用户
输入投进队列（否则同一实例 `start()` 后会被当新输入消费 = 跨 epoch 复活）。判据只有
一处（`status.is_service_running`）；处置与 `process_message` 一致——**拒收而不排队**
（停机窗口没有"稍后再处理"）。

为什么 RO1 的清扫挡不住它：清扫是 `stop()` 的**最后一步**，而输入层的两条生产者
（`do_transcription` 的 STT 回传、`ws_v2._handle_voice_commit`）跑在 aiohttp 的 WS
任务上——`stop()` **不**取消它们（`runner.cleanup()` 在 `agent.stop()` 返回**之后**
才跑）。窗口 = 「RO1 清扫 → `stop()` 返回 → 在途生产者才投递」。修前 E2：`stop()`
返回后真 `do_transcription` 仍投进总线，`start()` 后那条文本进 session / 被喂 LLM。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **RO4** | 直投 `UserInputData` 的**每一条**生产者在入队前都问同一个生命周期权威（`status.is_service_running`）；被拒者**不产生副作用**（不 reserve work、不写幂等 accepted、不发 `accepted` ACK），且**不跨 epoch 复活**。`TaskResultData` 等 durable 事件**不**受这条约束（RO2 的分类边界不变） | `core/input/tests/test_runtime_epoch_input_boundary.py`（7：RE-T1/R2 为 PRE_FIX_RED，修前 `bus.pending_user_inputs==1` 且重启后文本进历史；RE-T3/T4 是运行中／新 epoch 的负向控制；RE-T6 钉 durable 保全；RE-T7 钉 `stop()` 仍有界） | `make test-quick` / `test-normal` | **push/backend** |

**修法边界（Batch 20 实测）**：`voice.commit` 的判据放在**一切副作用之前**
（幂等 lookup / claim / `reserve_work`），故被拒的提交不留下任何预占——与
`test_process_message_rejects_before_v2_work_reservation` 同款收口；它复用既有
rejected 路径与 `_not_accepted_error`（`AGENT_BUSY`），**零新增 wire 枚举**。
`do_transcription` **仍然回显**转写（客户端需要清掉自己的录音态），被拒的只有入队。

**合法 transient 的边界（Batch 14 实测，KB-26 / KB-28）**：

- **`_current_task` 陈旧**（KB-26）：`_current_task` 的权威含义只是"最近 spawn 的回合 /
  取消目标"（写点分散在回合 spawn 与取消路径：`core/cognitive/loop.py :: CognitiveLoop.process`、`:: cancel_current`、`:: mark_shutting_down`、`core/agent/agent.py :: Agent._handle_user_input` / `:: _handle_system_tick`；清点在 `cancel_current`），
  ownership 判据自 Batch 12 起是 `_generation_ctx`（`current_turn_owns_input`）＋
  `_started_cognitive_tasks`。生产读者**恰好 5 处**（`core/cognitive/loop.py :: CognitiveLoop.process` 与 `:: cancel_current`、
  `core/agent/agent.py :: Agent._handle_user_input` 与 `:: _handle_task_result`、`core/debug/info.py :: DebugInfo._collect_system`），每一处都在
  `.done()` 守卫之内 ⇒ 陈旧句柄**不产生**错误取消 / 错误排队 / 误报忙 / 跳过登记。
- **`FSM=IDLE` 而锁未释放**（KB-28）：原始claim（"下一条消息取消卡在锁上的回合，
  吞掉已 ACK 且已出队的消息"）在 HEAD 上**已被 Batch 12/13 的判据覆盖**——卡在
  `_process_lock` 的回合尚未接管输入（`_generation_ctx` 已在 `process()` 入口清空），
  于是新输入（公开入口与直投）走 `not_owning` ⇒ **排队**；实测 claims/session/LLM
  三层顺序 `KA→KB→KC→KV`，无丢失、无重复、队列正常排空。它仍需要"某个
  `on_change` 订阅者在通知上挂起"这一前提，而仓内生产不注册这类订阅者 ⇒
  **当前不可达**（保留为 OPEN/LOW，归入 KB-28 的 reclassify）。

---

### 4.8 Claimed input conversation truth（CT）

来源：[2026-09-19 Batch 15 执行记录](../review/2026-09-19-Batch15-claimed-input-conversation-truth-执行记录.md)。
缺陷形状（canonical backlog **KB-46**，Batch 13 登记为 **N7**、Batch 14 确证为
USER/HIGH）：一条用户输入已完成 accepted admission、且 active-turn claim 已成立
（`_generation_ctx` 非 None），但**还没**被写进 session 历史时，cancellation / failure
会让它在 **session history、`message_archive`、未来 LLM context** 三层**同时消失**：

```text
process()  取锁**之前**清空 _generation_ctx / _admission_* / _turn_message_persisted
  _prepare_turn():
      await session_resolver(...)                        ← 真挂起点（claim 之前）
      await registry.get_or_load(...)                    ← 真挂起点（claim 之前）
      self._generation_ctx = GenerationContext(...)      ← ★ CLAIM commit point（与下一行无 await）
      await self.phases.enter_thinking()                 ← W1 挂起点 A（_notify 订阅者挂起时）
      await generation_registry.register(...)            ← W1 挂起点 B（Mongo upsert）
      runtime.try_begin_generation()                     ← 同步
  _think_loop() → _build_turn_prompt():
      await self._prompt.build_messages(...)             ← W2 挂起点 C（Milvus ANN + 12 段渲染）
      self._session.add_message(UserMessage(...))        ← ★ RECORD commit point（同步）
      self._turn_message_persisted = True
  _start_turn_tracking() → LLM                           ← MODEL_CONSUMED
```

**W1**（取消落在 A/B，走 `_handle_prepare_cancelled` 的 ctx 非 None 分支）与 **W2**
（取消落在 C，走 `process()` 的 `except asyncio.CancelledError` 臂）是**同一个 ownership
状态**，却都让输入三层皆无；另有 **W3** = 同一窗口内的**普通异常**（走
`_handle_turn_exception`，与取消同 root，§33）。

> **会话历史合同（本批 Phase 0 裁定）**：`session history` 记录
> **"用户真的提交过、且被 runtime 接管过的每一条输入"**（**Contract A**，实现上是
> **混合语义**：user 消息在模型调用**之前**写、assistant 消息在生成**之后**写）。
> 证据：`loop.py` 记录点紧邻注释"此后用户至少能在历史里看到'我说过这句'"，唯一豁免是
> `is_task_result`（合成系统唤醒）；反证 B（"只记模型消费过的"）——record 早于 LLM 首轮，
> provider 从未被调用时它已在历史里；反证 C（"只记完成的回合"）——`_archive_partial_reply`
> 会把被打断的**半截** assistant 回复照样归档。客户端把服务端历史当**权威转写稿**
> （`chatStore.ts::addUserMessage` 乐观渲染 → `restoreFromServer` **整体替换** `messages.value`）
> ⇒ 历史里缺一条，用户刷新/重连后自己刚发的消息就**消失**。
>
> **停止按钮语义**：前端只发 `{type:'interrupt'}`（无 payload）且**不回收** user 气泡
> ⇒ 它是"停止 assistant 当前生成"，**不是**"撤销我刚发出的 user message"。因此
> CLAIMED 之后的取消必须保留用户输入。
>
> **本批的保证范围**：**runtime conversation truth**（存活 runtime 生命周期内的
> session 窗口 + 既有归档路径 ⊕ 未来 LLM context）。**不**含进程崩溃后的恢复
> （`accepted ≠ durable`，与 §4.5 同一条既有合同）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **CT1** | 一条 user input 一旦被 active turn **claim**（`_generation_ctx` 非 None），受支持的 cancellation / failure **不得**让它从 runtime conversation truth 中静默消失 | `core/agent/tests/test_claimed_input_conversation_truth.py::test_ct_t1_w1_cancel_in_prepare_after_claim_keeps_input_in_history`（PRE_FIX_RED：修前 `history=['（暖场）']`、archive 同样没有）、`::test_ct_t1_w2_cancel_in_prompt_build_after_claim_keeps_input_in_history`（PRE_FIX_RED，W2 落点）、`::test_ct_t2_ordinary_exception_after_claim_keeps_input_in_history`（PRE_FIX_RED，W3） | `make test-quick` / `test-normal` | **push/backend** |
| **CT2** | claim / record / model-consume / terminal 四个边界**各有独立 owner**，不得混为一谈；user message 进会话历史**只有一个 canonical 权威**（normal path 与 recovery 共用） | `::test_ct_t3_next_turn_llm_context_contains_cancelled_claimed_input`（PRE_FIX_RED：判据落在**真实** `LLMAdapter.stream_think` 收到的 messages）、`tests/real_mongo/test_real_claimed_input_truth.py::test_rm1_recovered_claimed_input_is_really_persisted_once`（真库三处各恰一条） | 同上 + `make test-real-mongo` | **push/backend** |
| **CT3** | conversation-truth 的补写**幂等**：同一条逻辑输入不得在 history / archive / model context 里出现两次。判据落在**既有身份**（`(session, message_id)`）与一次性载荷上，不新增平行布尔 truth source | `::test_ct_t11_record_recovery_is_idempotent_across_repeated_finalization`、`::test_ct_t5_cancel_after_record_does_not_duplicate`、`::test_ct_t13_exception_finalization_cancelled_does_not_double_record`（W3→W1 叠加）、`::test_ct_t4_normal_turn_records_input_exactly_once`（prompt 侧恰一次） | 同上 | **push/backend** |
| **CT4** | 保留 user-input truth **不得伪造 assistant 产出或"回合已完成"**：被取消回合的 history shape 是 `user`（无 assistant 消息），generation 行仍如实落 `cancelled` / `interrupted` | `::test_ct_t1_w1_…`（`roles[-1] == "user"` + `generations.status ∈ {cancelled, interrupted}`）、`::test_ct_t8_cancelled_then_normal_keeps_history_order` | 同上 | **push/backend** |
| **CT5** | **claim 之前**被取消/失败的输入**不得**被 conversation-truth 补写提升进会话历史（Batch 12 的 admission 边界不得被本批破坏） | `::test_ct_t6_pre_claim_cancel_creates_no_phantom_history`（取消路径：`_handle_prepare_cancelled` 见 ctx 为 None 直接返回，**不进**收口）、`::test_ct_t6b_pre_claim_exception_creates_no_phantom_history`（异常路径：`_handle_turn_exception` 的 `finally` **一定**进收口 ⇒ 此处"已 claim"门禁是**唯一**防线） | 同上 | **push/backend** |
| **CT6** | conversation-truth 保护**不得**阻塞取消安全的终态收口与停机：补写必须是**同步零 await**（不新增挂起点、不做 DB 往返），任何异常都不得外抛——否则第二发取消会让相位永停 THINKING（CF1/CF2 回归） | `::test_ct_t10_session_write_failure_does_not_block_terminal_finalization`（§64：写失败仍须回 IDLE）、`::test_ct_t12_shutdown_in_window_stays_bounded_and_starts_no_new_turn`（§67：停机有界且不得在 STOPPED 后开新回合）、`::test_ct_t11_…`（重复收口） | 同上 | **push/backend** |

**实现形状（唯一权威 + 协程局部载荷 + 三条门禁 + 一处消费点）**：

- `CognitiveLoop._record_user_input()` —— user message 进会话历史的**唯一权威**
  （normal path 与 recovery 共用；Batch 14 的 W1/W2"两种会话真相"因此不会再出现）。
- `process()` 取锁**之前**的同步段构造**协程局部量** `record = (content, message_id)`，
  沿终态路径**显式下传**（`_handle_prepare_cancelled(record)` /
  `_handle_turn_exception(e, record)` → `_handle_interrupt(record=…)`）；
  `is_task_result=True` 直接构造为 `None` ⇒ 合成系统唤醒**不可能**被写成用户历史。
  **不放实例属性**：Challenger 反证 + 主 Agent 独立复现（N2）——两个 `process()` 体
  重叠时（实测 in-flight 峰值 = 2），入口的同步前缀会把旧回合的载荷覆盖成新回合的，
  旧回合若随后 claim，其 recovery 就会把**新回合的正文**写进历史（实测
  `A_count=2 B_count=0`）。局部量让"载荷属于哪个回合"在**结构上**不可能搞错。
- `CognitiveLoop._record_claimed_input()` 在 `_handle_interrupt()` 的**零 await prologue**
  消费（在 `is_completely_idle` 早退**之前**）——一处覆盖 W1/W2/W3。三条门禁：
  ① 已 claim（`_generation_ctx is not None`）、② 未 recorded（`_turn_message_persisted`）、
  ③ 载荷非 None；另在拿到稳定身份时核对 `_session_has_message_id()` 作防御纵深。
  "零 await"是**承重约束**（变异 M9/M10 证明：一旦挪到首个 await 之后，被直投输入
  抢占的回合就会因为等待锁的新回合先跑一步、清掉 ctx 而静默丢失 ⇒ CT-T14）。

**CT1–CT6 的边界（明确不做）**：不引入事务 / 回滚 / durable inbox / unit-of-work
（**KB-30 保持独立**——本批只挪动**一个** user-input truth commit point，不建立整个回合的
原子提交）；不新增 schema / migration；不改 wire 契约（不新增 `message.cancelled` /
`turn_status`）；不改 EventBus / FSM / `_process_lock` / admission 与 pending 排序；
不承诺 crash durability；不为持久化新增每回合 read-before-write。

**诚实声明（本批的保证不是"三层恒等"）**：三层 truth 的**写入时机本就不同构**——
`add_message` 同一步写内存 deque（经 fire-and-forget 任务写 `message_archive`），而
`sessions` 文档由既有的 `save()` 在**下一个成功回合的 after_response** / 关机
`save_all()` 落库。因此取消路径上的补写**不**立即进 `sessions` 文档；
`test_rm1` 显式 `save()` 后经真 `SessionRepository.load()` 读回才断言（→ 新登记
**KB-51**）。这是**既有**语义，不是本批引入的。**边界澄清（Challenger 实测）**：
**关机**路径下补写**确实**会被 `save_all()` 落库（实测 `[I3] memory=1
sessions_hits=2 archive_hits=1`，与正常回合的对照同值）——本批"不承诺 durability"
指的是**进程硬杀**（`accepted ≠ durable`），不是"取消路径永远不落 `sessions`"。

### 4.9 Session data integrity（SD）

来源：[2026-09-19 Batch 16 执行记录](../review/2026-09-19-Batch16-session-data-integrity-执行记录.md)。
两个 finding 都是**数据不变量**层面的缺陷，不是"索引看起来不完整"：

- **KB-15（可达性）**：一个**已经持久化**的逻辑会话，可能被正常支持的分页/枚举
  **永久看不到**。修前 `list_page` 把排序/谓词直接压**原始** `updated_at`，而投影层
  （`_summary_from_doc`）把"非字符串 / 缺失 / null"归一成 `""` 再交给游标：

  ```text
  存储侧排序键 ≠ 对外暴露的排序键
     ├── 静默漏项：{updated_at: {$lt: "<str>"}} 对缺失/null 永不匹配（type bracketing）
     │             ⇒ 排在末尾却谁也叫不到它（只有"一页装得下"时才露过面）
     └── 游标毒化：BSON Date 型排在字符串**之前**（BSON 序）而投影归成 ""
                   ⇒ 它若落在页边界，next_cursor = base64("|sid")
                     ⇒ 解码端拒绝空时间戳 ⇒ 下一页 400，此后全部不可达
  ```

- **KB-16（身份唯一性）**：`session_id` 是**逻辑主键**（直读/改名/删除/摘要/级联/缓存键
  全部按它寻址），但存储层从未有唯一约束。三条写路径都是
  `update_one({session_id}, …, upsert=True)`——**不是原子的**：两个并发 upsert 都看不到
  匹配就各自插入。真库实测（Batch 16 / E3）：20 轮 × 两个独立 client 的 `create`
  有 1–3 轮产出重复文档；把存在性检查卡住可以让它**确定性**复现（2 篇同 ID）。

> **会话持久化合同（Batch 16 Phase 0 裁定）**
>
> | 字段 | 地位 |
> |---|---|
> | `session_id` | **REQUIRED / 非空字符串 / 逻辑主键**（唯一约束见 SD4）——所有写者恒写，全仓按它寻址 |
> | `updated_at` | **REQUIRED（写入侧）；读入侧允许缺失**——规范形态是 `now_iso()` 的 UTC ISO 字符串；排序键就是它本身（不做任何格式解析） |
> | `messages` | OPTIONAL（`create()` 写 `[]`；窗口由 `session_max_messages` 界界） |
> | `title` / `character_binding` | OPTIONAL（缺省空串 / 空对象） |
> | `character_state` / `reflector_state` / `service_state` | OPTIONAL（`load()` 缺省构造默认值；`create()` 不写，首次 `save()` 才出现） |
> | `created_at` | **不存在**——任何版本的写者都没写过（真库审计：7/7 缺失），故**不得**作为 fallback 候选 |
> | 非空字符串以外的一切 `updated_at` 形态 | **legacy 桶**（可达，排最后，桶内 `session_id ASC`）——不视为 corruption、也不伪造时间戳 |
> | `session_id` 缺失/null/非字符串/空串 | **corruption**：无身份、不可寻址（唯一索引把它们并入 null 桶，至多一篇） |
>
> **不变量落点**：存储层的两个桶判据 `ORDERED_TS_FILTER` / `LEGACY_TS_FILTER` 与投影层
> 的 Sentinel `""` **必须一致**——"存储顺序 == 投影顺序 == 游标语义"是游标能当位置用的
> 前提（这正是 KB-15 的根因形状）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **SD1** | **可达性完备（含 legacy 元数据）**：每一条结构上被接受的持久会话都必须能被**支持的分页语义**走到——`updated_at` 缺失/null/非字符串/空串/**数组**的记录**不得**静默落在可枚举数据集之外（要么可达，要么被显式归类为 corruption） | `core/storage/tests/test_session_repo_pagination.py::TestLegacyOrderingBucket`（N ∈ {0,1,49,50,51,205} × {missing,null,empty,array}；Date/数字型；search；list_all；legacy-only 数据集）、`::TestArrayValuedOrderingMetadata`（挑战者 A 的数组反证）、`core/input/tests/test_sessions_pagination.py::TestLegacyOrderingRowsAreReachable`（route 层 = 用户入口）、`tests/real_mongo/test_real_session_ordering_truth.py::TestLegacyOrderingReachabilityRealMongo`（真库 BSON 序/类型括号） | `make test-quick` / `test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **SD2** | **排序 fallback 必须确定性**：legacy 元数据不得用"读的时刻 / 当前进程时间 / 首次读取时间"来补——排序键只能是**已持久化的事实**（本批：`session_id`）。同一稳定数据集重复遍历顺序逐项一致 | `test_session_repo_pagination.py::TestLegacyOrderingBucket::test_legacy_bucket_is_deterministic_across_runs`、`core/memory/tests/test_session_manager_pagination.py::TestAdversarialRegressions::test_repeated_traversal_with_legacy_rows_is_stable`、`tests/real_mongo/test_real_session_ordering_truth.py::TestLegacyOrderingReachabilityRealMongo::test_traversal_order_is_stable_across_runs` | `make test-quick` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **SD3** | **游标遍历全域且终止**：任何由 API 产出的合法游标，继续翻页最终**终止**且不重不漏；`has_more=false` 必须真的意味着"当前查询域没有下一项"（含 legacy 桶） | `test_session_manager_pagination.py::TestAdversarialRegressions::test_legacy_row_on_page_boundary_round_trips_through_the_cursor`（PRE_FIX_RED：修前 400）、`::test_sentinel_cursor_round_trips_and_legacy_cursor_still_decodes`、`test_session_repo_pagination.py::TestLegacyOrderingBucket::test_legacy_row_at_page_boundary_does_not_poison_the_cursor`、`tests/real_mongo/test_real_session_ordering_truth.py::TestLegacyOrderingReachabilityRealMongo::test_legacy_row_on_page_boundary_does_not_truncate_the_list` | 同上 | **push/backend** + **push/real-mongo** |
| **SD4** | **逻辑身份唯一**：权威 `sessions` 集合里，至多一篇文档代表一个逻辑 `session_id`（存储层强制，不只靠应用层检查） | `core/storage/tests/test_session_identity_uniqueness.py`（索引引导/升级/幂等/降级）、`tests/real_mongo/test_real_session_identity_truth.py::TestSessionIdentityUniquenessRealMongo::test_connect_creates_the_unique_index`、`::test_find_then_upsert_window_cannot_produce_two_authorities`（**确定性** PRE_FIX_RED：修前 2 篇）、`::test_concurrent_same_session_creation_keeps_one_authority` | `make test-quick` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **SD5** | **身份腐化必须显式**：遗留重复文档不得被"随便挑一篇当权威"静默掩盖——启动日志显著告警 + 运行时 `schema_drift` 的 `option_conflicts` 报 error；系统**不自动删数据、不猜赢家** | `test_session_identity_uniqueness.py::TestSessionIdentityIndexBootstrap::test_duplicates_keep_availability_and_skip_unique_build`（caplog 断言告警）、`tests/real_mongo/test_real_session_identity_truth.py::TestSessionIdentityUniquenessRealMongo::test_duplicates_do_not_destroy_availability`（真库 + `check_schema_drift` 报 option_conflict） | 同上 | **push/backend** + **push/real-mongo** |
| **SD6** | **约束部署是向后安全的**：加/升级唯一约束必须有**定义的**遗留数据行为，且索引引导**不得意外毁掉可用性**——已有重复 ⇒ 保持现状（非唯一）+ 告警 + 应用照常启动；唯一建不出来 ⇒ 退回非唯一，`connect()` 仍返回 True | `test_session_identity_uniqueness.py::…::test_duplicates_keep_availability_and_skip_unique_build`、`::test_unique_build_failure_falls_back_to_non_unique`、`::test_index_listing_failure_skips_all_changes`、`::test_duplicate_probe_failure_counts_as_possible_duplicates`、`tests/real_mongo/test_real_session_identity_truth.py::TestSessionIdentityUniquenessRealMongo::test_existing_non_unique_index_is_upgraded_in_place`、`::test_building_unique_index_with_duplicates_fails_loudly`（真库异常码 11000 留证） | 同上 | **push/backend** + **push/real-mongo** |

**实现形状（唯一权威 + 两个桶 + 受守卫的索引升级）**：

- **排序域**（`SessionRepository.list_page`）：`ORDERED_TS_FILTER =
  {"updated_at": {"$type": "string", "$gt": "", "$not": {"$type": "array"}}}`
  （恰为"非空字符串**标量**"；`$not: {$type: "array"}` 是承重条件——Mongo 的
  `$type`/比较算子会**遍历数组元素**，不排除数组就会出现"有序桶文档投影成 Sentinel"
  与"游标 Sentinel 表示 legacy 桶"撞车，见 §33 挑战者 A）先按
  `updated_at DESC, session_id ASC` 取；取不满一页时再查
  `LEGACY_TS_CLAUSE = {"$nor": [ORDERED_TS_FILTER]}`（**真补集**：缺失/null/非字符串/
  空串/数组/对象…）并按 `session_id ASC` 取剩余。两桶拼接 == 投影层的契约序
  （`_precedes` / `_after_cursor`：`""` 最小）。真库 `explain` 实测：有序桶仍是
  `IXSCAN(updated_at_-1_session_id_1)` + 索引序（`docsExamined == nReturned == limit`，
  与旧谓词逐项相同）；legacy 桶是一次 `session_id` 索引扫描（O(N)、无阻塞排序）——
  两者都**不是**"全表扫描 + 阻塞排序"的正常路径退化，且 legacy 查询只在有序桶取尽后
  发生（一次遍历的最后一页）。
- **游标**（`core/memory/session_manager.py`）：格式不变（`base64url(updated_at|session_id)`），
  只是解码端接受空时间戳（legacy 桶 Sentinel）。旧游标（非空时间戳）**逐字节兼容**；
  无 legacy 文档时**永不**产出空时间戳游标 ⇒ 公开 API 零变化。
- **身份约束**（`SessionRepository._ensure_session_id_index`，S8 同族纪律）：
  先 `aggregate` 探测重复（**检测失败按"可能有重复"处理**）→ `list_indexes()` 比选项
  （查询失败 ⇒ 不做任何变更）→ 已唯一则返回（幂等）→ 有重复则保持非唯一 + 告警 →
  数据干净且选项不同则 `drop_index` 后重建（Mongo **不能原地改选项**，真库实测 code 86）
  → 建失败退回非唯一且**不抛**（`connect()` 仍 True）。
- **写路径**（`SessionRepository._upsert_session`）：`non_fatal=is_duplicate_key`
  保证并发冲突不判死仓储；**经确认**是本集合 `session_id` 约束的 E11000 才收敛
  （重跑一次 upsert ⇒ 走 update 分支，不可能再次冲突）；别的唯一约束冲突原样冒泡。

**"supported 数据集"的边界（SD1 的判据）**：带**非空字符串 `session_id`** 的文档一律
属于 supported（其 `updated_at` 无论是什么形态都可达）；`session_id` 缺失/null/非字符串/
空串的文档没有可寻址身份，属 corruption——本批不隐藏它们（legacy 桶里照样可达，
`sessionId` 为空串），唯一索引把同类并入 null 桶（至多一篇），`schema_drift` 与启动日志
负责让操作者看见。**不支持无限兼容**：不为任意 BSON 形态做类型特判、不做 repair-on-read、
不把 `updated_at` 解析成时间（排序是字符串序）。

**SD1–SD6 的边界（明确不做）**：不做通用迁移框架、不做自动删重复/自动合并 winner
（重复文档可能各自带着不同消息、标题、归档关联，"取最新的那个"是猜）、不做 repair-on-read、
不引入聚合排序（`$ifNull` / 哨兵字段）**也不改文档 schema**（零字段变更）、不改公开分页
API（`sessions[]/has_more/cursor` 与 `SessionSummary` 逐字段不变）、不恢复全量预取
（PG2 仍然成立）、不为 legacy 数据伪造时间戳（禁止 `now()` / 随机 / 首次读取时间）。

### 4.10 Synthetic injection provenance（SY）— **合同只覆盖会话历史**

来源：Batch 1.8 §11.4（明确 deferred）+ Batch 20 的独立复核（F13）。
**这一节的作用是把"已裁决的"与"未裁决的"分开写清楚**——因为 F13 的处置取决于后者。

**已裁决（会话历史侧，唯一一条）**：`session history` 记录"用户真的提交过、且被
runtime 接管过的每一条输入"；唯一豁免是 `is_task_result`（合成系统唤醒，直接构造为
`None` ⇒ **不可能**被写成用户历史）。守卫：`test_ct_t7_synthetic_task_result_turn_is_not_
recorded_as_user_history`（**按条数**钉住）。注意 CT-T7 是**测试级**登记，不在 §4.8 的
CT1–CT6 表内。

**未裁决（Batch 20 复核结论：`PRODUCT_DECISION_REQUIRED`，见 PD-13）**：合成文本进入
`mood` 判定 / 记忆提取 / 性格反思这三条链**没有任何合同依据**——没有 invariant、
没有测试、没有 wire/UI 契约裁决过"合成注入算不算用户 authored"。Batch 1.8 把它**显式
留在范围外**（"系统注入文本被当成用户发言这个更大的问题不在本批范围"），Batch 15 的
记录则把主动搭话那半**钉在现状**（"`_handle_system_tick` 的主动搭话
（`is_task_result=False`）**保持修前行为**（会被记录）…否则就是未经授权的会话真相变更"）。
故本仓**不得**由 AI 自行补一个过滤条件——那是产品语义决定。

**Batch 20 的独立复现（E2，真实 `drain_mailboxes_once` → `_handle_task_result` →
`_build_wake_up_message`）**：一个失败的后台任务终态产生的合成文本被
`_prepare_think_input` 原样当作 `user_message`，于是

```text
[系统通知] 当前时间: … 你此前委派的后台任务（帮我查天气）未能完成。失败信息: …
  → EmotionDetector._detect_mood(...)      = "sad"          （mood 链）
  → consolidator.consolidate(user=…, …)                      （记忆链，且以用户名作前缀渲染）
  → engine.buffer_message(user_msg=…, …)                     （性格反思链，role=用户名）
```

**两条真实入口 + 一条不是**：①后台任务终态唤醒（`is_task_result=True`，有 flag）；
②主动搭话 tick（`agent.py` 传入 `"（系统提示：用户已经N秒没有说话了…）"`，**没有任何
provenance flag**）；③**唤醒词不是文本注入路径**（只发 `wake` 帧，前端闪一下指示器）。
`is_task_result` 在 `_finalize_turn` 边界**被丢弃**（`core/cognitive/finalize.py` /
`streaming.py` / `tool_phase.py` 里零引用），所以下游三个消费者拿到的只是一条普通字符串。

**没有可用的 provenance 载体**：`core/schema/message.py` 只有自由 `metadata`（生产写入者
仅两处，都与来源无关）；`frontend_contract` 的 `Message` / `V2TextMessage` 无 source 字段；
`LongTermMemory.insert` 的 `source` 只标**写入路径**（`"consolidation"`）而非输入来源。
机器可判别的信号在边界上**存在**（`_dispatch_event` 的 `isinstance` 分派 / `event_type`），
只是被压成了一个 bool（或直接丢掉）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **SY1** | 系统产生的控制刺激必须在**整个下游认知处理**中与用户 authored 输入可区分（不得靠文本前缀/字符串内容判断） | **未实现**——正是 PD-13 要裁决的东西 | — | — |
| **SY2** | 合成刺激**不得**在没有产品合同明确授权的情况下变成持久的用户证据（LTM / L2 / 用户档案） | 现状：**不成立**（mood / 记忆 / 反思三条链都receive它） | — | — |
| **SY3** | 过滤合成输入**不得**同时抑制正常用户学习或唤醒/主动搭话本身 | 若 PD-13 裁决为"过滤"，本条是修法的验收条件（负向控制：同内容的真实用户输入必须仍进原路径） | — | — |

**判定口径（Batch 20）**：F13 = `CONFIRMED`（机制真实、E2 可复现）+ `BLOCKED_BY_PD-13`
（语义未裁决）。**本批不动 production 代码**——按"禁止 AI 自行选择产品语义"的纪律。

## 5. 架构与静态面

| Invariant | Test / Gate | Gate 命令 | CI |
|---|---|---|---|
| `Module/` 生产代码与 `frontend_contract/` 不得反向依赖 | `make check-arch`（R1） | `scripts/check_architecture.py` | **push/backend** |
| 领域/基础设施层不得依赖 `core.input` | `make check-arch`（R2） | 同上 | **push/backend** |
| 禁止跨包导入下划线私有符号 | `make check-arch`（R3，allowlist 空） | 同上 | **push/backend** |
| 不得新增 **eager** import 环 | `make check-arch`（R4） | 同上 | **push/backend** |
| 裸 `create_task` 必须逐点登记所有权（逐点记**期望条数**） | `make check-arch`（R6；条数以 `scripts/check_architecture.py::_TASK_SPAWN_ALLOWLIST` 为准） | 同上 | **push/backend** |
| **跨包私有访问（含私有方法）必须公开化或显式标注** | `core/tests/test_cross_package_privacy.py`（5 例） | `make test-normal` | **push/backend** |
| 输入层任何未标注私有穿透即红（比全仓更严） | `core/input/tests/test_layer_discipline.py` | `make test-normal` | **push/backend** |
| **索引选项声明表只许指向真实 schema 实体**：集合键/索引名写错或整条漏登记 ⇒ 该集合选项**一个都不比**却照报健康（B3-CB-2） | `core/storage/tests/test_schema_drift.py::TestExpectedIndexOptionsPinnedToCode`（9 例；判据 = 生产 `ensure_index(...)` 的 **AST**，非第二张手写表） | `make test-quick` / `test-normal` | **push/backend** |
| **上传目录在调用期解析**：import 期固化 ⇒ 测试的环境隔离静默失效、文件写进真实图片目录（B3-CB-7） | `core/input/tests/test_upload_dir_isolation.py`（4 例：真实上传 + 正对照 + 真实目录 delta=0 + fresh 子进程） | `make test-quick` / `test-normal` | **push/backend** |
| 具名超时预算必须表态（外层 ≥ 内层 / 叶子 + 理由） | `make check-timeouts` | `scripts/check_timeout_budget.py` | **push/backend** |
| 契约枚举成员必须有生产者、字段有读必须有写 | `make check-contract` | `scripts/check_contract_reachability.py` | **push/backend** |
| 契约产物无漂移（ts / schema / raw_ts / golden frames） | `make check-types` | `frontend_contract --check` 等 | **push/backend + frames** |
| PR 可达 job 不得 self-hosted、不得引宿主凭据 | `make check-ci-isolation` | `scripts/check_ci_isolation.py` | **push/backend** |
| 每个 workflow 必须声明 `permissions:` | 同上 | 同上 | **push/backend** |
| self-hosted 清理必须密闭 + 双守卫 + fail-closed | 同上（6 处清理点） | 同上 | **push/backend** |
| 测试集合代数：`Quick ∪ remainder == Normal` 且交集空 | `make check-test-sets` | `scripts/check_test_sets.py` | **push/backend** |
| 每个测试必须有分层 marker、快慢层不得交叠 | 根 `conftest.py::pytest_collection_modifyitems` | 任何 pytest 进程内断言 | **push/backend** |
| 依赖锁与声明一致 | `make check-env-lock` | `scripts/check_env_lock.py` | **push/backend** |
| 无凭据形态入库 | `make check-secrets` | `scripts/check_secrets.py` | **push/backend** |
| 依赖无已知漏洞（Medium+） | `make audit` | pip-audit + bandit | **push/backend** |
| **文档里的 `@property` 计数与枚举必须与代码一致** | `core/tests/test_doc_counts.py::test_property_count_matches_code`（四份文档：`CLAUDE.md` / `core/README.md` / `README.md` / `docs/architecture.md`，判据 = `core/agent/agent.py` 的 AST 计数） + `::test_property_list_is_complete`（`core/README.md` 的**枚举**不得漏项） | `make test-quick` / `test-normal` | **push/backend** |

---

## 6. Self-hosted runner 信任边界

| Invariant | 实现 | 强制点 | CI |
|---|---|---|---|
| 不受信代码**永不**落在持久 self-hosted runner 上 | 所有 job `if: github.event_name == 'push'`（或 `workflow_dispatch`）；无 `pull_request` 触发器 | `make check-ci-isolation` | **push/backend** |
| 宿主凭据（`.env`）不跨 job 泄漏 | 每个 self-hosted job 开头密闭清理 workspace + `Load .env` 依赖该清理 | 同上（6 处，`TestWorkspaceWipeRecognition` + `test_every_self_hosted_cleanup_site_is_fail_closed`） | **push/backend** |
| 清理 fail-closed（显式检查退出码，`set -e` 不算） | 6/6 清理点 | 同上 | **push/backend** |
| `runs-on` 无法静态判定时**拒答** | 表达式 `runs-on` → 违规 | 同上 | **push/backend** |
| 无死 job（守卫引用的事件必须在 `on:` 声明过） | `_EVENT_GUARD_RE` 与 `_is_push_only` **共用同一正则** | 同上 | **push/backend** |

安全模型与「为什么本仓不开放 PR」见 [`docs/ci.md`](../ci.md)。

---

## 7. 已知假保障（有测试 / 有不变量，但自动化跑不到）

**这一节是本文存在的核心理由。** 以下项**不是**"已覆盖"，列出来是为了让它们
无法静默退化：

| 项 | 现状 | 风险 | 处置 |
|---|---|---|---|
| **T1/T2 并发攻击扫描**（`probe_t2_race.py`，20 例） | 探针在 `/tmp`，**未入库** | 并发竞态回归不会被自动发现 | 见 [todo.md](../../todo.md) 的 Batch 2.3 follow-up |
| **`make test-full`（含 slow）无自动执行点** | 只在 `nightly.yml` 手工 `workflow_dispatch` 触发 | slow 层回归只在人工注意时才发现 | 若需覆盖，须先有隔离 runner 或接受 push 成本；见 `docs/ci.md` |
| **`external` 层（真实 LLM/DB）无自动执行点** | 同上（`nightly.yml` 的 `external` 输入） | 外部集成回归不自动发现 | 同上；**刻意 opt-in**（真实费用） |
| **Ollama runtime context 的真实 daemon 旁证**（`Module/llm/tests/test_ollama_real.py`，7 例：冷启动 configured=requested=observed、downshift、warm 4096→8192→4096、静默截断、诊断失败不影响 generation） | 只标 `external`：需要本地 Ollama daemon（非凭据） | 换 Ollama 版本/换模型后 runtime 语义漂移不会被自动发现（本批实测版本 = 0.24.0） | **接受**（CI 不依赖本机 Ollama/GPU，§114）；手工 `RUN_EXTERNAL_TESTS=1 make test-external`；见 §3.4 |
| ~~`external` opt-in 门禁只覆盖 `Module/`~~ | **已修**（2026-09-17）：门禁并入根 `conftest.py`，现覆盖全仓；`core/tests/test_external_optin_gate.py` 钉住范围 | 修前 `core/` 的 external 用例**未 opt-in 也真跑**（真实 DeepSeek 调用、计费） | — |
| **`real-mongo` 层** | push 侧有 job（`ci.yml:real-mongo`） | ✅ 有覆盖 | — |
| **私有守卫的"同名遮蔽"**（Challenger 2026-09-17 实测） | 在**访问方包内**定义一个同名 `def _x` 即可让该属性名全仓免检（9 处违规塌成 1 处）；扫描面也只含 `core`+`Module` | 守卫被当成"完备"时会漏掉真实穿透 | **接受**（属"按属性名归属近似"的固有代价，模块 docstring 已记载）；定位仍是**回归网**，见 [todo.md](../../todo.md) |
| **清空形态识别的长尾** | 规范 `find … -exec rm` 与 4 种 `$GITHUB_WORKSPACE` 写法 + 当前目录形态已覆盖；`xargs`/`for` 循环/`git clean` 未覆盖 | 间接清空写法不受三道清理子句约束 | **接受**（本仓 6 处清理点全用规范写法，无实例）；见 todo.md |
| **`if:` 守卫是 token 存在性** | `… \|\| true` 会被判成受信 push-only | 静态判据无法判定布尔语义 | **接受**（保守方向是"当 PR 可达"，此处相反但需构造）；见 todo.md |
| **删除 `__init__.py` 重导出的风险** | F401 已豁免 `__init__.py`；重导出由 `__all__` + 直接导入覆盖；**无自动"公开面冻结"测试**（`Module/workflow_contract` 有，其余没有） | 误删重导出曾真实发生（Batch 2：`SessionId`/`asdict` → 59 模块 ImportError） | 见 todo.md |

---

## 8. 维护纪律

1. **新增不变量必须同时给判据**。只写 prose 的条目一律不收——本文要么指向一个
   真实存在的测试/门禁，要么进 §7。
2. **改判据要同步改本文**。测试改名/删除后，本文的映射会变成谎言；CI 不检查本文
   （它与代码的对应关系靠人工维护），所以改动者负责同步。
3. **§7 只增不删**，除非该项真的获得了自动执行点——那时把它移到 ▲ 表里。
4. **不变量是"必须长期成立"的断言，不是"当前实现细节"**。实现换了、不变量还在，
   就保留条目并换掉指向；实现细节变了、不变量本身不再成立，那是**设计变更**，
   要单独评审。

## 相关文档

| 文档 | 内容 |
|---|---|
| [ci.md](../ci.md) | CI 权威说明：job 布局 / 信任边界 / 为什么全 self-hosted / PR 恢复清单 |
| [testing.md](../testing.md) | 测试分层体系与档位命令 |
| [architecture.md](../architecture.md) | 完整架构详解 |
| [CLAUDE.md](../../CLAUDE.md) | 项目地图 + 各门禁的约定 |
| [todo.md](../../todo.md) | 待办与 follow-up（含本文 §7 的承接项） |
| [2026-09-18 Batch 4 执行记录](../review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md) | R5/R6 的来源 |
| [2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md) | PC1–PC6 的来源（N-10 / F6 / F7） |
| [2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md) | CM1–CM5 的来源（N-3：409 冲突元数据真值） |
| [2026-09-19 Batch 17 执行记录](../review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md) | OL1–OL6 + PD-12 裁决（KB-13 / AR-16）的来源 |
| [2026-09-18 Ollama runtime context authority 记录](../review/2026-09-18-ollama-runtime-context-authority-记录.md) | KB-13 / AR-16 / PD-12 的登记与 E3 证据 |
| [2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) | P1/P2/R 的来源 |
| [2026-09-17 Batch 1.7 执行记录](../review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) | T1–T4 的来源 |
| [2026-09-17 Batch 1.8 执行记录](../review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md) | U1–U5 的来源 |
| [2026-09-18 Batch 7 执行记录](../review/2026-09-18-Batch7-provider-preview-window-执行记录.md) | PW1–PW6 的来源（F8） |
| [2026-09-18 Batch 8 执行记录](../review/2026-09-18-Batch8-pagination-completeness-执行记录.md) | PG1–PG6 的来源（P2-01r） |
| [2026-09-18 Batch 9 执行记录](../review/2026-09-18-Batch9-speaking-state-recovery-执行记录.md) | SP1–SP4 的来源（KB-6） |
| [2026-09-18 Batch 10 执行记录](../review/2026-09-18-Batch10-thinking-state-cancellation-recovery-执行记录.md) | TH1–TH5 的来源（KB-22） |
| [2026-09-18 Batch 11 执行记录](../review/2026-09-18-Batch11-cancellation-safe-turn-finalization-执行记录.md) | CF1–CF6 的来源（KB-25 / KB-23） |
| [2026-09-18 Batch 12 执行记录](../review/2026-09-18-Batch12-accepted-input-delivery-closure-执行记录.md) | AD1–AD8 的来源（KB-24） |
| [2026-09-19 Batch 13 执行记录](../review/2026-09-19-Batch13-pending-input-ordering-执行记录.md) | PO1–PO6 的来源（KB-38） |
| [2026-09-19 Batch 14 执行记录](../review/2026-09-19-Batch14-residual-input-ownership-triage-执行记录.md) | RO1–RO4 的来源（KB-47；KB-26/28/29 裁决） |
| [2026-09-19 Batch 15 执行记录](../review/2026-09-19-Batch15-claimed-input-conversation-truth-执行记录.md) | CT1–CT6 的来源（KB-46） |
| [2026-09-19 Batch 16 执行记录](../review/2026-09-19-Batch16-session-data-integrity-执行记录.md) | SD1–SD6 的来源（KB-15 / KB-16） |
| [2026-09-19 Batch 18 执行记录](../review/2026-09-19-Batch18-context-token-estimation-执行记录.md) | TE1–TE11 的来源（KB-12） |
| [2026-09-20 Batch 19 执行记录](../review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) | TE9 的来源（KB-77）；长期 backlog 裁决 |
| [2026-09-20 Batch 20 执行记录](../review/2026-09-20-Batch20-residual-correctness-closure-执行记录.md) | SY1–SY3 / PD-13（F13）+ TE10–TE11（KB-78）的来源 |
| [2026-09-17 Batch 2 执行记录](../review/2026-09-17-Batch2-guardrails-hardening-执行记录.md) | §5/§6 门禁（R1–R6 / check-ci-isolation）的来源 |
