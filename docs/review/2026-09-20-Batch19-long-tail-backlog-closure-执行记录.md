# Batch 19 — Long-tail Backlog Closure 执行记录

> 批次定位：**不是**"把 todo 剩下所有项修完"，也**不是**继续沿每条 finding 无限深挖，而是
> 用当前 HEAD、真实生产调用链、既有 invariants、真实测试与独立 Challenger 把剩余 backlog
> **系统性压缩**成明确状态。成功判据不是"修了多少条"，而是"剩下每一条都知道：它是什么、
> 是否真实、谁能触发、风险多大、是否已有 guard、何时值得做、是否应该继续存在"。
>
> HEAD = `e11619f1`（Batch 18 CI follow-up）。本记录中的行号除注明外均为该 commit。

## 1. 方法与组织

| 角色 | 范围 | 产物 |
|---|---|---|
| Sub-agent A | lifecycle / cancellation / ownership（KB-26..KB-55 子集） | 18 项裁决 + 11 个新探针（pristine HEAD 副本，29 passed） |
| Sub-agent B | persistence / session / observability / deployment（KB-17/56..60、OBS-*、DEP-*） | 逐项裁决（见 §4） |
| Sub-agent C | provider / Ollama / workflow / architecture（KB-61..68/72、AR-3..19、F3/F17） | 24 项裁决 + `check_arch/check_contract/check_error_codes` 复跑 |
| Sub-agent D | estimator（KB-69..77）+ Batch 18 audit follow-up A–D | 逐项裁决 + 真实 tokenizer 交叉验证 |
| Sub-agent E | guardrail / test debt（GR-1..25、KB-14、KB-27） | 27 项裁决 + 合成仓复现 R4 漏报 |
| Sub-agent F | misc correctness long tail（KB-2/4/8/10/11/18..21/39..44/53、F1） | 16 项裁决 + 6 个探针 |
| **Challenger** | 独立副本（`git archive HEAD`）× 4 组 claim 攻击 | 见 §4b（confirmed / falsified / fix weakness / new finding） |
| Main agent | 决策表、production fix、mutation、全局回归、todo 重写 | 本记录 |

**主 Agent 的独立复核**（不依赖子代理结论）：KB-77 的 pre-fix E2 与真实 tokenizer 交叉验证、Latin-2 的真散文测量（本地权威 tokenizer × Wikipedia REST 散文）、验证脚本的真实运行、`enforce_round_budget` 的记账不变式（60 组随机形状：`outcome.after == estimate_messages_tokens(...)` 零偏差、零孤儿 `role="tool"`、system 恒在 `[0]`）+ 边界形状（空表 / len 1·2 / 无 user 条目 / 孤立 tool 条目）。

**纪律**：Sub-agent 结论**不直接触发** production patch；任何 FIX_NOW 由主 Agent 独立 E2/E3
复现。所有裁决基于**当前 HEAD**（多个子代理在 pristine 副本上重跑，因为共享工作树在批次中
被本批 fix 并发修改）。

## 2. Production Fix 1（本批唯一 production 改动）：KB-77 ReAct 轮次再预算

### 2.1 Pre-fix 证据（E2，确定性、真实路径）

驱动**真实** `CognitiveLoop._think_loop` + 真实 `_execute_tool_round`/`_dispatch_tool_calls`
（工具结果真的进入 `messages`）+ 真实 `MessageBuilder` 首轮裁剪 + canonical 估算权威：

| 配置 | 首轮 | 第 2 轮 | 第 5 轮 |
|---|---|---|---|
| `ollama/qwen2.5:3b`（窗口 8192 / 预算 6144）/ 每轮 1 条接近单条上限（`max_result_tokens=2000`，实测 est 1993）的工具结果 | **6123**（≤ 预算） | **8205**（> **窗口**） | **16533** |
| 12 工具集 / 每轮 3 条并行近上限结果 | 4566 | **27558** | — |

`messages` 数组跨轮**同一个对象**（原地追加），全程零再预算调用。Sub-agent D 用真实
Qwen2.5 tokenizer 交叉验证：K=1 第 6 轮 **9056**、K=2 第 4 轮 **10231**、K=3 第 3 轮
**10209**（均 > 8192）。而 Ollama 超窗是 **HTTP 200 + 静默截断**，且截断保留"头部 +
最近 `num_keep`、丢掉**中间**"（`Module/llm/ollama.py:48-53`）⇒ 静默劣化的正是
system prompt / 人设区。

### 2.1b Ollama 旁证（E3，本机复测）

`qwen2.5:3b` + native `/api/chat` + `options.num_ctx = 8192`，prompt 用 20000 字普通中文
（≈11k 真实 qwen token，远超窗口）：

    HTTP 200 | done_reason=stop | prompt_eval_count=8192 | /api/ps context_length=8192

即 provider **不报错、不拒绝**，只是把 prompt 静默截到窗口——这正是 KB-77 的严重度所依赖的
前提（Batch 17 的 E3 结论在本批环境上复现）。对照：12000 字（≈6.7k token）时
`prompt_eval_count=6684`（完整消费），说明上面那次确实是"被截"而不是"本来就小"。

### 2.2 根因

预算只在**首轮**派生一次并施加一次（`_apply_prompt_budget`，`message_builder.py:240-242`），
而 ReAct 每轮把整个数组重发（`streaming.py:134`），工具轮把 assistant `tool_calls` +
`role="tool"` 结果 + `[TOOL_EXECUTED]` 标记**原地**追加（`tool_phase.py:113/144-148/229-233`、
`loop.py:1352`）；`core/cognitive/` 内 `budget`/`estimate_*` 零引用。单条结果上限（2000）
与 `MAX_TOOL_ROUNDS=5` 只约束**单项**，不约束**总量**。

### 2.3 修法（最小、无 ReAct 重写）

- `core/prompt/message_builder.py::enforce_round_budget(messages, budget, *, turn_start, model_id, tools)`
  ——与首轮**同一份**估算权威、同一"由旧到新"优先级，三档：
  1. 丢历史（`messages[1:turn_start]`，与 `_apply_prompt_budget` 完全一致）；
  2. **整组**丢较早的工具往返（assistant tool_calls + 其 tool 结果 + 轮次标记）——
     整组丢是为了不产生**孤儿** `role="tool"`（严格 provider 会 400）；
  3. 截**最新一组**的 tool 结果内容（保留配对结构，附截断标记；
     标记按"完整说明 → 短标记 → 空内容"三级回退）。
  永不丢：`messages[0]`（system）、当前用户消息、最新工具往返组的**配对结构**；
  三档用尽仍超预算时**如实**报 `over_budget`（不假装成功）。
- `core/cognitive/loop.py`：预算在 `_think_loop` **派生一次**并传给 `_build_turn_prompt`
  （消除"两处各自派生"的漂移面）；工具轮之后、下一轮之前调用再预算并留痕。
- 性能：利用 `estimate_messages_tokens` **逐条可加**把裁剪循环从 O(n²) 次全量估算降到
  O(n) 次逐条估算（该前提由 `test_message_costs_are_additive` 钉住）。

**不做**：不改 ReAct 结构、不动 FSM/`_process_lock`/wire/schema/config、不改 tool 执行与
会话归档（裁剪只作用于 **prompt 副本**）、不加 watchdog/新常量。

### 2.4 守卫与变异（非空验证）

| 守卫 | 修前 | 修后 |
|---|---|---|
| `core/cognitive/tests/test_react_round_budget.py`（7） | **2 红**（第 2 轮 6808 > 6144；并行 27558） | 7 绿 |
| `core/prompt/tests/test_round_budget.py`（9） | 函数不存在（PRE_FIX_RED 走 loop 级） | 9 绿 |

变异（每次 16 条用例全跑，"被杀"均为**行为断言**而非导入错误）：

| 变异 | 结果 |
|---|---|
| M1 去掉每轮再预算调用 | 2 failed / 14 passed（轮次预算 ×2 杀死） |
| M2 不丢较早的工具往返组 | 3 failed / 13 passed |
| M3 不丢历史 | 1 failed / 15 passed |
| M4 不截断最新一组 | 2 failed / 14 passed |
| M5 允许丢**最新**一组（破坏配对/可见性） | 2 failed / 14 passed |
| 基线（无变异） | 16 passed |

**保持的既有不变量**：tool 执行与帧集合不变（`tool_started/completed` 仍 3 帧）、
session 里存**完整**结果（裁剪只改 prompt 副本）、max rounds 正常、取消仍按既有语义
结算、CT/OL/TE 不变量保持（TE9 新增）。

## 3. 裁决：Latin-2 / 估算器合同（AUDIT-B → **KB-78**）

**问题**：`script_latin2_czech` 实测 qwen profile ≈0.65，低于 0.75 硬下限，却因
`category="script"` 被合同排除；而 `budget.py` 说"拉丁（含重音）**在合同内**"。

**独立测量（E3）**：真 Wikipedia REST 散文（cs/pl/vi）× **本地缓存的权威 tokenizer**
（Qwen2.5-3B / DeepSeek-V3 / Llama-3-8B / Mistral-7B / Yi-1.5 / Phi-3）：

| 文本 | 变音密度 | qwen profile | generic |
|---|---|---|---|
| cs_Praha / Brno / Česko / Vltava（真实散文） | 12.3–14.2% | **0.65–0.68** | 0.67–0.69 |
| pl_Kraków / Warszawa / Wisła | 3.7–8.3% | 0.73–0.80 | 0.70–0.83 |
| vi_Hà_Nội / Việt_Nam | ~11% | 2.13–2.14 | 0.78–0.79 |
| 签入语料三条（pangram+词表 ×3） | 10.5–19.9% | 0.65–0.74 | 0.57–0.67 |

**结论（§140 要求的明确裁决）= `IN_CONTRACT`**：这是**普通拉丁文字**（`other` 占比 ≈0），
走**拟合**的 `latin_ext` 类 ⇒ 可以低估——与 `script`（跨家族 3–6×、方向被钉在"只许高估"）
**性质不同**，混在一起会让方向保证被误读成覆盖。**真实自然文本同样击穿下限**，故
"misclassified corpus（只是样本退化）"**不成立**：样本是合成的（pangram+词表重复 3 次），
但 12% 密度的真散文一样是 0.65–0.68。

**本批处置（§141：IN_CONTRACT + 下限被击穿 ⇒ 至少 CONFIRMED；若不修必须给明确 deferred 原因）**：

- **估算器未改**（deferred）：`latin_ext` 是**线性规划产物**，模块 docstring 明令
  "改系数必须重跑该流程"，而 hand-tune 一个 magic number 会绕开那条纪律；§82 明确
  这类重标定属**独立的估算器标定批次**。可行性已实测（qwen `latin_ext` 0.86→1.18 /
  generic 0.20→0.56 即可闭合，且**合同内 band 不变**：qwen [0.775, 1.19]）⇒ 下一批
  的起点很小。
- **合同/守卫已修**（本批，非 production）：
  1. 三条样本从 `script` 单列为 **`latin_accent`**（生成器 `build_corpus.py` 为源，
     MANIFEST 重新生成，文本逐字节不变）；
  2. `budget.UNCOVERED_CONTENT_CLASSES = ("repeat","script","latin_accent")` 成为
     **单一权威**（合同测试 + 验证脚本共用；此前三处各写一份，正是 KB-80 的根因）；
  3. `TestKnownLimitations::test_dense_accented_latin_is_a_registered_uncovered_class`
     把实测区间钉住（worst ∈ [0.60,0.70]、best ∈ [3.00,3.50]、36 组）；**修好后显式
     `skip`**（避免"断言缺陷存在"的用例在 owner 修好那一刻永久变红）；
  4. `test_latin_accent_samples_are_not_silently_promoted` 逐一点名三条证据，
     防止有人把它们静默挪进合同内类别或删掉；
  5. `budget.py` 合同范围节改写：普通重音密度（≤8%）在合同内，密集变音符号拉丁
     **未覆盖**且击穿下限（含实测数字与修法）。

## 4. 裁决：维护者工具与文档（KB-79 / KB-80 / KB-81）

| ID | 结论 | 证据 | 处置 |
|---|---|---|---|
| **KB-79** | `validate_families.py` **全部家族 SKIP 仍 exit 0** ⇒ "什么都没检查"看起来像"通过" | E2：两个不存在的 repo → 2 行 SKIP + 0 个 verdict + EXIT=0 | **FIXED**：0 家族产出结论 ⇒ **exit 2 拒答**；有未覆盖家族 ⇒ exit 1；新增 6 条离线退出合同用例（正常成功/部分 SKIP/**全 SKIP**/坏 revision/合同过滤/单一权威） |
| **KB-80** | 验证脚本过滤条件（`!= "repeat"`）与 CI 合同（`{"repeat","script"}`）不一致 ⇒ 对合同**故意排除**的样本报 `NOT COVERED`（误导性假红） | E2：修前 Qwen2.5-3B 打印 min 0.65 NOT COVERED；同批（KB-78）的 `latin_accent` 真实缺口又靠 `script` 大类躲过硬下限 | **FIXED**：单一权威常量 + `latin_accent` **单独一行**报告（对齐过滤 ≠ 藏缺口）。真实运行：合同集 38 样本 qwen **0.78 OK** / Phi-3(generic) **0.80 OK**，同时打印 `└ latin_accent 0.65 ⚠️ 击穿硬下限（已知缺口 KB-78）` |
| **KB-81** | 旧 profile 名（`cjk_opt`）残留：**可执行 0 处**，全在文档 | E2：穷举 grep | **FIXED**（docs）：`invariants.md` 改为 6 profile / 11 字符类 / T 指向代码表，并更正两个陈旧数字（高估上界 **13.9×**、`repeat` 区间 **[0.58, 5.34]**）；验证脚本 docstring 同步 |

本批同时把两处**已复现的门禁盲点**转成 Active（未在本批修，需独立设计）：

- **GR-1**（本批顺手 FIXED）：文档数字门禁只硬编码三份 → `docs/architecture.md` 的
  「43 个同名 `@property`」（真值 46）永不被检查。**已修**：两处 43→46 + 把该文档加入
  parametrize（PRE_FIX_RED：把 43 放回去 → `test_property_count_matches_code` 红）。
- **GR-15**（本批顺手 FIXED）：`pytest.ini` 未启用 `--strict-markers` ⇒ "合法分层 marker
  + 拼错的附加 marker"会静默改变选择。**已修**：`addopts = --strict-markers`（全仓
  collect-only 实测 6576 tests 通过）。
- **GR-4 / GR-16 / KB-27 / KB-32** 保留为 Active（§1.3）——都需要改判据/契约，不属
  "小 hardening"。
- **观察（无 canonical ID，归 GR-9 家族）**：`make audit` 的 bandit 输出有 4 条
  `nosec encountered (B608), but no failed test` ⇒ Batch 18 follow-up 加的抑制**当前是惰性的**
  （与 `# noqa` 陈旧同族：仓库没有"抑制是否仍生效"的门禁）。

## 4b. Challenger 复核（独立副本 + 4 组 claim 攻击）

| 类别 | 内容 |
|---|---|
| **confirmed** | KB-77 是真缺陷（独立探针：baseline 第 2 轮 **8279 > 8192**、第 5 轮 **17066 = 2.08× 窗口**；并纠正"单条结果每轮必然超窗"的措辞——单条时 5 轮内只超**预算** 7361，超**窗口**需 ≥2 条并行结果或估算合同内的 1.27× 低估）；tree 版修复在真实路径上 569/569 全绿、每轮 ≤ 预算；`outcome.after` 与真值**逐位相等**（200 组 fuzz，|Δ|=0）；验证脚本退出合同非空（M1 回退 `return 0` → 3 红）；Latin-2 真散文确实击穿（捷克 0.639–0.664 / 波兰 0.709–0.734）。 |
| **falsified（我方）** | 交给 Challenger 的 **patch 文件本身是坏的**：我在 mutation harness 运行**期间**生成它，捕到了 M2 变异体（`if len(spans) <= 1 or True:`）⇒ patch 的 step ② 是死代码且 3 条自家用例红。**已查明、已重生成干净 patch**（`git diff e11619f1..HEAD`，0 处 `or True`）；**tree/提交件始终是正确的**（`git show HEAD:core/prompt/message_builder.py` 为 `if len(spans) <= 1:`，quick/normal/full 全绿）。 |
| **witness 的"两次更正"（留痕，§19）** | **原登记**：签入语料闭合需 `latin_ext` qwen 0.86→1.18 / generic 0.20→0.56。**Challenger 第一轮**：判其被证伪，称需 qwen ≈1.48–2.65 / generic ≈0.85–0.87（我据此改了 budget.py / todo / 本记录）。**Challenger 第二轮自我更正**：第一轮的换算模型错了（把 est/actual 当成了"变音密度的仿射函数"，正确斜率是 `n_latin_ext / actual_tokens`，差 ≈1.9×）；直接代入系数实测 = **qwen 0.860→1.181 / generic 0.200→0.503 闭合签入语料**（代入后该类最坏 ≈0.775–0.778，贴线）。**最终准确表述**：原登记正确；本记录与 todo 已恢复；并补上真实散文所需的更高量级（qwen ≈**1.54** / generic ≈**0.34**）。 |
| **该轮唯一真实残余（已处理）** | 语料样本比真散文更重音密集（czech 19.9% / polish 10.5% vs 真 cs 12.3–14.2% / pl 3.7–8.3%）⇒ **只按语料配方重标定**会在真散文仍 **0.723–0.740（9 篇中 4 篇 < 0.75）**时让守卫退役。已处理：①退役判据从硬下限 0.75 抬到**合同下限 0.775**，并新增"落在 [0.75, 0.775) 之间 ⇒ **fail**（既没达合同、又会被 covered 判据放行）"分支；②budget.py / todo / 本记录写明"下一批必须纳入低密度波兰语**真散文**样本"。 |
| **fix weakness（已修）** | 无 user 条目形状下 `turn_tail_start` 的退化分支可让 step ① 删掉 assistant(`tool_calls`) 而留下 `role="tool"` ⇒ **孤儿 tool（严格 provider 400）**。修法：退化时保护"**第一组**工具往返"起点 + 新增结构性判据 `_deletion_is_safe`（tool 条目/带 tool_calls 的 assistant 一律不可单独删）。5 条对抗用例（无 user 单组/双组、配对安全、**最后一条 user 消息存活**）已补；并新增**结构性兜底**：
"最后一条 user 消息永不丢"不再依赖调用方把边界算对（Challenger P3：`turn_start=998` 曾能删掉它）。 |
| **fix weakness（已修）** | 契约测试的"单一权威"断言是同义反复（`set(EXCLUDED) == set(UNCOVERED)`，后者就是前者构造出来的）⇒ 换成有内容的检查（无重复 / 类别真实存在 / **每类都在 budget.py docstring 写明理由**），并让 `latin_accent` 守卫从"只钉 min/max"改为**钉住击穿下限的配对集合**（部分重标定会红）。 |
| **fix weakness（接受）** | ① 尽最大努力仍超预算时只 log ERROR、回合继续——已把 TE9 措辞改成"必须重施 + 残留必须可见"，不再声称"已被强制满足"；② 热路径成本实测 ≈23–73 ms/轮（O(n)，同步在事件循环上）：对 30k 字符级 prompt 属可接受但非免费；③ **gate 拒绝轮在预算裁剪里不是一组可丢的往返** ⇒ 预算由截断**更早的合法往返**支付（较早结果 1512→736 token，被拒绝的 assistant 载荷 1257 token 原样留下）——与 KB-82 同根，已并入其小批。 |
| **new finding** | **KB-82**（USER/MEDIUM/E2）：gate 拒绝轮里 `submit_task` 的 tool_call 无配对回复 ⇒ 严格 provider 下一轮 400。**pre-existing、非本批引入**；主 Agent 已独立复现（`assistant declares ['call_submit','call_bad'] / tool replies ['call_bad']`）。已登记进 §1.1 Active 并给出 8 行修法；**按 §5 的 production fix budget（1 MEDIUM cluster）本批不修**。 |
| **severity/reachability correction（采纳）** | F17 必须写成"**未关闭的 wrong user-visible truth**（USER/MEDIUM）"而不是纯决策项；KB-67 的实测后果（`ollama/llama3.1:8b` 原生 128K 也被套 8192）写进 §4。两者都已在 todo 里保留 Active 归属。 |
| **could not verify** | 真实 Ollama 的"超窗静默截断"由 Challenger 侧未复测（不允许加载模型）——**主 Agent 已补做**（§2.1b：HTTP 200 / `prompt_eval_count=8192`）；KB-67 的数值探针 Challenger 只做了代码阅读（主 Agent 侧沿用 subC 的 E2 payload 证据）。 |

**第二组变异（针对 Challenger 驱动的新加固）**：

| 变异 | 结果 |
|---|---|
| M6+M7 同时去掉两处配对防线（边界退化 + 无结构判据） | **1 failed / 11 passed**（对抗用例杀死） |
| M6 或 M7 **单独**去掉 | 12 passed ⇒ **冗余防线**：任一单独存在即可阻止孤儿（已在 docstring 写明是 defense-in-depth） |
| M8 假 witness：只把 generic `latin_ext` 抬到 0.56 | **1 failed / 47 passed**（配对集合守卫杀死——改进前会静默通过） |
| M9 子串式改名（`latin_accent` → `latin_accent_X`） | 48 passed ⇒ **未被杀**（我的 M9"已杀"claim 被 Challenger 证伪）——已把检查改成**反引号精确形态** |
| M9b 删掉未覆盖类的 docstring 理由 | **1 failed / 47 passed** |
| M9c 反引号式改名（精确检查） | **1 failed / 47 passed** |
| M10 去掉"最后一条 user 消息永不丢"兜底 | **1 failed / 12 passed**（首版用例因"预算太大、根本没裁"而假通过，已修形状 + 小预算） |
| M11 代入记录配方（qwen 1.18 / generic 0.56） | `47 passed, 1 skipped` ⇒ 守卫**按设计退役**（该类最坏 ≈0.775–0.778，贴线）；真实散文残余见上表 |
| 基线 | 61 → 67 passed（含新增对抗用例） |

## 5. 完成判据对照（§204）

| 判据 | 结果 |
|---|---|
| canonical backlog 全量遍历 | ✅ 6 域 + 主 Agent，覆盖 `todo.md` 全部 active/deferred 项 |
| 每项明确 state / 每 deferred 项有 reopen trigger | ✅ `todo.md` §1–§5 |
| duplicate / alias / superseded 压缩 | ✅ `todo.md` §9 ID 漂移表（AUDIT-A..D → KB-78..81；KB-5 → GR-19） |
| USER/HIGH、DEPLOYMENT/HIGH 全部处理或升级 | ✅ 本批**无**未处理的 USER/HIGH（最高为 KB-78 MEDIUM / GR-24 MEDIUM） |
| production fix 未超预算 | ✅ 1 个 MEDIUM cluster（KB-77）；其余为 guardrail/docs（不计 production 预算） |
| PRE_FIX_RED 真实 | ✅ KB-77 两条 loop 级用例修前红；GR-1 把 43 放回去即红 |
| Challenger 独立 | ✅ 独立 `git archive HEAD` 副本 + 4 组 claim 攻击 |
| mutations non-vacuous | ✅ M1–M5 全杀（16 条用例每次真跑） |
| `make lint` / `pyright` / `audit` | ✅ 全绿（audit：pip-audit 0 未豁免漏洞；bandit Medium/High = 0） |
| 所有 required `check-*` | ✅ 12 项全绿 |
| quick / normal / full / real-mongo | ✅（见 §6） |
| frontend build | ✅ |
| git status clean（提交后） | ✅（见 §7 提交划分） |
| owner push 后真实 CI green | ⏳ **READY_FOR_CI**（本记录不宣称 CLOSED） |

## 6. 最终 Active Correctness（只列真正未解决、有实际影响者）

| 顺序 | ID | 一句话 | Reach / Sev | Evidence |
|---|---|---|---|---|
| 1 | **KB-56** | 「加载更多」用全库端点 + 恒空游标 ⇒ 可把**别会话**的消息注入当前聊天（会话真相污染）。E2 机制已复现（`repo.query(before=None)` 返回 A+B 两会话）；修复点在**前端 store**（改打已存在的 `/api/sessions/{id}/messages`）或后端加会话过滤 | USER / MEDIUM | E2(机制)+E1 |
| 2 | **KB-78** | 密集变音符号拉丁文本（捷克/波兰）击穿估算硬下限（真散文 qwen 0.65–0.68）⇒ 6144 预算可放进 ~9450 真实 token（>8192）而在 Ollama 上静默截断 | USER / MEDIUM | **E3** |
| 3 | **F13** | 合成唤醒文本参与 mood/记忆提取/反射（历史侧已豁免）| USER / LOW-MED | E2 |
| 4 | **KB-41** | 系统事件（任务终态/主动搭话）可抢占**尚未接管输入**的用户回合 ⇒ 已 ACK 输入静默丢（窗口窄）| INTERNAL / LOW | E2 |
| 5 | **KB-48** | 停机窗口内两条直投入口仍投事件 ⇒ 跨 epoch 复活 | DEPLOYMENT / LOW | E2 |

**本批未修但已登记的 Deployment 面**：GR-24（部分索引盲区：旧库会整域 503 而自检报健康，E3）、
DEP-5（YAML 那行永不生效，已文档化）。

## 7. 测试与门禁结果（Batch 19 最终状态）

| 项 | 结果 |
|---|---|
| `make lint` / `make pyright` | ✅（0 error） |
| `make audit` | ✅ pip-audit：`No known vulnerabilities found, 2 ignored`；bandit（`-ll`）：Medium/High = 0（19 Low 为登记基线） |
| `check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | ✅ 全绿 |
| `make test-quick` | ✅ 6476 passed / 2 skipped / 1 xfailed（82s） |
| `make test-normal` | ✅ 6476 passed / 2 skipped / 1 xfailed（81s） |
| `make test-full` | ✅ 6508 passed / 71 skipped / 1 xfailed（486s） |
| `make test-real-mongo` | ✅ 46 passed（45s） |
| 目标回归（`core/prompt core/cognitive Module/system_prompt scripts/tests`） | ✅ 883 passed |
| frontend `npm run build` | ✅ |
| 新增守卫 | 28（KB-77 19 + 验证脚本退出合同 6 + 契约/未覆盖类守卫 2 + doc-count 新参数化 1） |
| 变异 | M1–M5、M6+M7、M8、M8b、M9b、M9c、M10 全杀；M6/M7 单独为**冗余防线**（已在 docstring 写明）；M11（代入记录配方）按设计退役 |

