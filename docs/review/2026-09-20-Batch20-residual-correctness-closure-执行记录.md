# Batch 20 — Residual Correctness Closure 执行记录（KB-82 / KB-56 / KB-78 / F13 / KB-41 / KB-48）

> 批次定位：**不是** Batch 20 意义上的新一轮 hardening。Batch 19 已正式结束系统性
> correctness hardening（`Systematic correctness hardening program = CLOSED`），
> 本批只处理它最后留下的 **6 个真实 finding**，并给每一项一个**不留模糊**的终态：
> `FIXED + GUARDED` / 重新证伪或降级 / `PRODUCT_DECISION_REQUIRED`。
>
> **不重新打开**：lifecycle、admission 架构、EventBus 架构、token estimator 架构、
> provider 架构、session 架构。**不扩大** backlog（新发现只登记，不立即修）。
>
> 基线 = `2a87c521`（Batch 19 最终记录）。行号除注明外均为该 commit。

## 0. 处理顺序与终态一览

| 顺序 | ID | Reach / Sev | 处理 | 终态 |
|---|---|---|---|---|
| 1 | **KB-82** | USER / MEDIUM | production fix（协议配对） | **FIXED / GUARDED** |
| 2 | **KB-56** | USER / MEDIUM | production fix（前端会话归属） | **FIXED / GUARDED** |
| 3 | **KB-78** | USER / MEDIUM | production fix（`latin_ext` 重标定 + 真散文 holdout；含外部队列家族） | **FIXED / GUARDED** |
| 4 | **F13** | USER / LOW-MED | **不动 production** | **CONFIRMED / PRODUCT_DECISION_REQUIRED / BLOCKED_BY_PD-13** |
| 5 | **KB-41** | INTERNAL / LOW | production fix（ownership 判据 + 推迟） | **FIXED / GUARDED** |
| 6 | **KB-48** | DEPLOYMENT / LOW | production fix（准入判据） | **FIXED / GUARDED** |

**零 public API 变化、零 config、零 schema、零 migration。** 唯一新增依赖是前端的
`vitest`（devDependency；KB-56 的 store 不变量在 `npm run build` 的类型检查下不可见）。

## 1. 方法与纪律

| 角色 | 范围 | 产物 |
|---|---|---|
| Sub-agent A | KB-56 前端/session 隔离 | 逐行复核 + 两条**修正**（见 §2） |
| Sub-agent B | KB-78 估算器合同 | 系数表 / 语料清单 / 合同常量 / 环境（tokenizer 缓存、Ollama）逐项实测 |
| Sub-agent C | KB-41 + KB-48 lifecycle | 生产者全量清点 + 可复用屏障 + 既有守卫清单 |
| Sub-agent D | F13 合成语义 | 完整消费者地图 + 合同裁决边界 |
| Main agent | 六项裁决、production fix、E2/E3 复现、变异、全局回归、文档 | 本记录 |

**纪律**：子代理结论**不直接触发** production patch——每一项的 PRE_FIX_RED、关键
E2、变异与「修后仍绿」都由主 Agent 独立复现。所有引用 `path:line` 的证据都在基线
HEAD 上核对过。

## 2. KB-82 — Gate Rejection Tool Pairing

### 2.1 结论修正（子代理带来的两条精度更正）

原 finding 的机制描述**基本正确但偏轻**：

1. 游标**不是"恒为空"**，而是**只有首页为空**。会话域端点（`/api/sessions/{id}/messages`）
   的响应项**带 `_id`**（`rest.py::Message.mongo_id` 的 wire 别名），旧代码在
   `chatStore.ts:299` 把它 latch 成 `earliestMessageId` ⇒ 之后持续沿**跨会话归档**
   往回翻。**污染是无界的**，不是"漏一页"。
2. 回放的会话身份**已经送达并被丢掉**：`useWebSocket.ts:52` 收到 `s.conversationId`
   却只传 `s.messages, s.token_stats`。即"零 wire 变化"是可行的——身份本来就在帧里。

### 2.2 根因与修法（KB-82）

`_gate_tool_calls` 把 `submit_task` 从校验里拆出来（它有自己的 `_handle_submit_task`
校验），却**没把它算进"连坐回执"的集合**。而 `_execute_tool_round` 在 gate 返回
False 时**直接 return**（`tool_phase.py:284-286`）⇒ submit 一个都不执行，回执也不生成
⇒ assistant 声明的 id 永久缺配对。

修法（`core/cognitive/tools.py`）：连坐时也为每个 submit id 补一条**终态失败**回执
（`"本轮其他工具校验失败，整轮取消（未提交后台任务）"`），并把它写进 `rejected_ids`
——刻意避免"被拒 id 集合"与"已回执 id 集合"两个概念漂移成不同集合。

**配对 ≠ 执行**（§10）：被拒的工具仍然一个都不执行；回执是 `ToolResult.fail`，不含
成功文案；被拒轮次不发任何 `tool.started/tool.completed` 帧。

### 2.3 协议不变量与守卫

| 不变量 | 内容 |
|---|---|
| **TP1** | 每个已出现在 assistant 消息里的 tool call，在进入下一次 provider generation 前必须**恰好一个**终态 tool-result 配对 |
| **TP2** | 被拒绝/被 gate 的调用**不得执行** |
| **TP3** | 配对按 `tool_call_id` **身份**匹配，与位置/文本无关 |
| **TP4** | 补拒绝回执不得伪造成功，也不得改变审批/执行语义 |

**断言口径**（§12）：`declared_call_ids` 与 `terminal_reply_call_ids` 的**集合与重数**
都必须相等——只比 `len(...)` 会漏掉"一个 id 发两次、另一个缺失"。

**守卫**：`core/cognitive/tests/test_tool_pairing.py`（14 条，含 TP-T1..T8：
单拒 / 执行+拒 / 拒+执行（**反序**，证明与位置无关）/ 多拒 / 并行 / 工具抛异常 /
黑名单 / 下一轮 strict-provider 形状）+ `test_tool_gate.py` 中一条**语义更正**的用例
（原 `test_unknown_tool_does_not_affect_submit_task` 断言的正是缺陷行为）。

**PRE_FIX_RED**：修前 **5 红**（全部红在配对断言上，无 import/symbol 错误）。

**变异**（每次 14 条用例真跑）：

| 变异 | 结果 |
|---|---|
| M82-1 去掉 rejected reply | 红（配对守卫） |
| M82-2 reply 用错 call_id | 红（身份守卫） |
| M82-3 rejected tool 仍执行 | 红（non-execution 守卫；`ctx.executed == []`） |
| M82-4 同一 id 发两次 terminal reply | 红（exactly-once 守卫） |

**明确不做**：不执行被拒工具、不重复 reply、不把 rejection 写成 success、不改工具顺序、
不改 `MAX_TOOL_ROUNDS`、不改 TE9 预算、不改审批产品语义。

**同族残余（Challenger P7，未并入本批）**：被 gate 拒绝的那一轮在预算裁剪里**不是一组
可丢的往返** ⇒ 预算由截断**更早的合法往返**来支付。它与 KB-82 **同族但不同根**
（budget vs gate 配对），且本批的配对修复**不影响**它（拒绝轮的 assistant 载荷仍不是
"可丢的往返组"）。**登记为 `NEW_FINDING`**（见 §7）。

## 3. KB-56 — Cross-Session "Load More" Contamination

### 3.1 E2 复现（真实 store，真 Pinia + fetch 替身）

前端**此前完全没有测试设施**（`package.json` 无 test script、无 vitest/jest/cypress、
无 `frontend/src/api/`）。本批新增**最小**测试设施：`vitest`（唯一新增依赖）+
`frontend/src/stores/chatStore.test.ts`。**不建 E2E 框架**（§29：仓内无 Playwright，
不为这一项造一套）。

替身保真度是这一项的关键：回放帧按**真实契约** `SessionMessageItem` 构造（**没有** `_id`），
历史页按 `MessagesPageResponse` 构造（`cursor` 是**顶层字段**、页内项带 `_id`）。
第一版替身给回放项也加了 `_id`，掩盖了"游标必须来自会话域响应"这一事实——已修正。

**PRE_FIX_RED**：修前 **11 条全红**（含 A→B、A→B→C 乱序、A→B→A、同会话多页、
失败路径、身份未知、去重、live+history 合并）。

### 3.2 修法与不变量

| 不变量 | 内容 |
|---|---|
| **SI1** | 会话域异步响应只能改它**发起时**捕获的那个会话 |
| **SI2** | 切会话不会重定向已在途的请求 |
| **SI3** | 陈旧响应可以丢弃或存进原会话，但**绝不能**污染别的会话 |
| **SI4** | 结论与响应到达顺序无关 |

修法（**identity check at mutation boundary**，不是取消旧请求——§26）：
`restoreFromServer(…, conversationId)` 绑定归属并 `sessionEpoch += 1`；
`loadMoreMessages` 在发起时冻结 `(sessionId, epoch)`，**在任何 store 变更之前**核对，
改打 `/api/sessions/{id}/messages`，游标取响应的 `cursor`；归属未知时**拒绝加载**
（绝不退回全库端点）。

**为什么需要世代票而不只是 session id**：`activeSessionId !== captured` 在
**A→B→A** 的瞬时形状下会漏（请求回来时会话又变回 A 了），故用递增票。

### 3.3 变异

| 变异 | 结果 |
|---|---|
| M56-1 删除 identity guard | 3 红（陈旧成功 / 陈旧错误 / A→B→C） |
| M56-2 guard 用当前 active id 而非捕获值 | 1 红（**A→B→A**——正是世代票存在的理由） |
| M56-3 只 guard error path | **改写了判据**：原样的 M56-3 是**空的**（错误响应在 `!resp.ok` 就 return，根本到不了 store 变更）⇒ 改成"成功路径不核对身份"后 **3 红** |

### 3.4 前端影响与 CI

- `frontend/src/stores/chatStore.ts`（归属 + 闸门 + 会话域端点）、
  `frontend/src/composables/useWebSocket.ts`（传 `conversationId`）；
- `frontend/package.json`：`"test": "vitest run src"`；
- `.github/workflows/ci.yml` 的 frontend job 增一步 `npm test`（`npm run build` 只做
  类型检查，零行为覆盖——这是这些不变量此前**完全不可见**的原因）。
- `make check-ci-isolation` 复跑通过（未新增 job、未改 runs-on）。
- `npm run build`（`vue-tsc --noEmit && vite build`）通过。

**已知盲区（新登记）**：golden contract frames 的 `ws-v2-session.jsonl` 回放
`"messages": []`，且 17 个 frame 里**没有任何 `_id`** ⇒ `make check-types` 在结构上
**不可能**发现这一类（非空回放项的形状未被 any golden 覆盖）。

## 4. KB-78 — Dense-Diacritic Latin Recalibration

### 4.1 合同的两个数（先分清，不混用）

| 数 | 含义 | 出处 |
|---|---|---|
| **0.75** | **硬安全下限**：`budget = window × 0.75`，低估 >25% ⇒ provider 200 + 静默截断（Ollama 从不报 400） | `budget.CONTEXT_HEADROOM_RATIO`；`test_token_corpus_contract.py::_HARD_FLOOR` |
| **0.775** | **合同下限 / 守卫退役判据**：比硬下限高 3pt，让"系数被悄悄改坏"在击穿真合同**之前**变红 | 同一测试的 `_CONTRACT_MIN_RATIO` |

### 4.2 修前实测（E3：真散文 × 权威 tokenizer）

真 Wikipedia 导言（cs 6 篇 / pl 6 篇）× 本地权威 tokenizer：

| profile | 捷克 | 波兰 |
|---|---|---|
| qwen | **0.637–0.698** | **0.682–0.746** |
| generic | 0.89–1.02（6 族并集） | 0.87–1.16 |

17 条 < 0.75（含越南语条目的高估侧不计）。**根因**：`latin_ext` 由
`latin1_accented` 标定，而那条样本的变音密度是 **5.0%**，真捷克语是 **9.7–14.2%**、
真波兰语 **2.1–8.3%**。签入语料比真散文**更**重音密集（czech 19.9% / polish 10.5%），
恰好压在能过的一侧 ⇒ **只跑合成语料测不出缺口**。

### 4.3 为什么没有重跑 LP（纪律，不是省事）

`budget.py` 的 `### 系数怎么来的` 要求"改系数必须重跑线性规划流程"。**仓内没有那个
脚本**（LP 是过程、不是可运行产物；`fit_M*.json` 在盘上也不存在）。因此：

- 主 Agent **重建**了 LP 并验证了它**不能**复现现有系数 ⇒ 诚实裁决"流程无法从仓内
  产物重建"，**不用一个自己编的目标函数**去替换原作者的目标函数；
- 改为**封闭形式推导**（可复核、不需要重建 LP）：`ratio(w)` 对 `latin_ext` 是**仿射**的
  ⇒ `w_need = (0.775·actual − base)/n_latin_ext` 是**精确解**；取 `max(现值, 全部 floor
  样本的 w_need)` 并向上留 2% 余量；
- **只动这一个系数**，其余 10 类逐位不变（可逐位核对）。

### 4.3b 第一版漏了外部队列（**留痕**，第二版修正）

第一版只按 `GOLDEN.json` 的 6 个家族拟合，得 `generic = 0.560`，并让仓库自己的
`validate_families.py` 在**外部队列**上报出仍击穿：

```text
01-ai/Yi-1.5-6B   generic   0.66  script_latin2_czech (⚠️ 击穿硬下限（已知缺口 KB-78）)
```

**根因**：`generic` 的**声明覆盖**包含 Yi-1.5-6B 与 Phi-3-mini（外部队列），
而"拟合集合"只有 GOLDEN 的 6 族 ⇒ profile 在它**自称覆盖**的家族上失守。
同理 `llama` 的覆盖含 OLMo-7B、`qwen` 含 Qwen3-8B（这两族本来就够，系数未变）。

**第二版**把外部队列的真值（Yi / Phi-3 / OLMo / Qwen3，**逐仓钉死 40 位 revision**）
并入 holdout fixture，并把 floor 集合改为**该 profile 的完整声明覆盖**：

| profile | floor 集合（完整覆盖） | 修前 | 需求 | 修后 |
|---|---|---|---|---|
| **generic** | GOLDEN 6 族 + Yi-1.5-6B + Phi-3-mini | 0.2000 | **1.126**（pl_Wisla/Yi） | **1.1490** |

复跑 `validate_families.py`（E3 旁证，6 个家族 × 权威 tokenizer）：

```text
01-ai/Yi-1.5-6B   generic  in-contract  └ latin_accent 0.82  (在合同内)
microsoft/Phi-3   generic  in-contract  └ latin_accent 1.04  (在合同内)
allenai/OLMo-7B   llama    in-contract  └ latin_accent 0.91  (在合同内)
Qwen/Qwen3-8B     qwen     in-contract  └ latin_accent 0.99  (在合同内)
Qwen/Qwen2.5-3B   qwen     in-contract  └ latin_accent 0.99  (在合同内)
deepseek-ai/…V3   deepseek in-contract  └ latin_accent 0.98  (在合同内)
EXIT=0
```

为此新增变异 **M78-5**（generic 只按 GOLDEN 6 族拟合到 0.800）⇒ **6 红**，
证明"覆盖声明与拟合集合必须逐族对齐"这一条现在**有守卫**。
`validate_families.py` 的收尾文案也同步更正：它在缺口闭合后仍打印"已知缺口…
修法是重标定"，会让读者把"这一类整体未覆盖"误读成"仍在击穿"——现在分两句说清
（普通拉丁成员在合同内 / 未覆盖类只剩越南语）。

### 4.4 修法与证据

新增 **真散文 holdout**（12 篇 Wikipedia 导言，CC BY-SA 4.0，单篇 ≤ 730 字符，
逐条记 `lang`/`chars`/`sha256`/署名标题）+ 7 个权威 tokenizer 真值
（与 `GOLDEN.json` 同源同法）+ 生成脚本 `scripts/token_corpus/build_latin_prose_holdout.py`
（可重放、全 SKIP 时**拒答** exit 2）。

**低密度波兰语在场**（Batch 19 的硬要求）：`pl_Mickiewicz` 变音密度 **2.1%**——
正是它把 qwen 的需求推到 1.864（若只按语料配方取 1.181，它在真散文上仍 0.778 贴线）。

系数变化（**只改实际未达标的 4 个 profile**）：

| profile | 修前 | 修后 | 需求（floor 样本最大值） |
|---|---|---|---|
| qwen | 0.8601 | **1.8700** | 1.864 |
| llama | 1.6723 | **1.6800** | 1.676 |
| mistral | 3.4031 | **3.4100** | 3.406 |
| generic | 0.2000 | **1.1490** | 1.126（见 §4.3b：必须含外部队列） |
| deepseek | 1.0079 | 1.0079（本来就过，需求 0.873） | — |
| openai | 1.1090 | 1.1090（本来就过，需求 1.004） | — |

**结果**：

- 合同内语料 band **不变**（qwen 仍 **[0.779, 1.191]**；12 个 profile×family 组合里
  11 个**逐位一致**，mistral 最高值 1.303→1.306）；
- 捷克/波兰真散文 → **0.778–1.640**（含外部队列家族），全部 ≥0.775 进合同；
- **代价（登记，不隐藏）**：越南语（`latin_accent`，合同外）2.47 → **3.31**（方向安全）。

### 4.5 为什么 `latin_accent` 不整类转正（真实的残余）

`latin_ext` 里混着**两套互斥的 token 经济**：普通 Latin-1/Extended-A 变音（捷克/波兰）
与**堆叠变音**（越南语 U+1EA0–1EFF）。单系数扫描（12 条真散文 + 3 条语料）：

- 让捷克/波兰达到 0.775 所需：qwen **1.042–1.864**；
- 让越南语达到 0.775 所需：qwen **-2.506**（**负数**）⇒ 覆盖越南语必然击穿硬下限；
- 抬到 1.87 后越南语高估 **2.97**，超出 qwen 的合同 T=1.25 ⇒ 两者**不可能**同时进合同。

故裁决：**捷克/波兰进合同**（`UNCOVERED_CONTENT_CLASSES` 里的 `latin_accent` 语义收窄为
"越南语"），越南语保留在未覆盖类 + **代价区间被钉住**。这不是"还没调到"，是**当前
11 类 × 6 profile 体系内的结构性互斥**（要闭合需**新增字符类**或**改 profile 结构**——
两者都在本任务 scope lock 之外）。

### 4.6 守卫、变异与不回归

**守卫**：`Module/system_prompt/tests/test_latin_prose_contract.py`（31 条）——
真散文 floor ×6 profile（LT-T1/T2/T3）、**低密度波兰语必须在场**、只有 `latin_ext` 变了、
JSON/CJK/代码**零影响**（LT-T5/T6）、generic/未知/custom 回退（LT-T7/T8）、
**推导可执行复算**（读 fixture 真值 + `_classify` 独立算出所需系数，再断言 production
表满足它——不是注释里的数字）、越南语代价区间、fixture 哈希/许可/规模完整性。

**变异**（每次 system_prompt 全量 173+ 条真跑）：

| 变异 | 结果 |
|---|---|
| M78-1 恢复旧 `latin_ext` | **7 红**（真散文 floor + 语料守卫 + 代价区间） |
| M78-2 只按语料配方闭合（qwen 1.181） | **5 红**（低密度波兰语 holdout 红——正是它存在的意义） |
| M78-3 generic 不更新 | **8 红** |
| M78-4 过度校准（qwen 4.0） | **6 红**（高估侧 + 越南语代价区间 + 语料 T） |
| M78-5 generic 只按 GOLDEN 6 族拟合（0.800） | **6 红**（外部队列 floor——第一版的盲区现在有守卫） |

**跨家族回归**：`core/prompt`、`Module/system_prompt`、`core/cognitive`、
`Module/tool`、`core/input/test_character_preview_window.py` 共 **841 passed**。

**作者工具**：`scripts/token_corpus/validate_families.py` 在本机可跑（6 个 tokenizer
缓存齐全、Ollama 在跑）；本批的验证走 `test_latin_prose_contract.py`（离线、
确定、进 CI），validate_families 仍是 external 档的人工复核工具。

## 5. F13 — 合成注入的 provenance（**不动 production**）

### 5.1 独立复现（E2）

真实 `drain_mailboxes_once` → `_handle_task_result` → `_build_wake_up_message`，
一个**失败**的后台任务终态产生：

```text
[系统通知] 当前时间: … 你此前委派的后台任务（帮我查天气）未能完成。失败信息: …
```

这条字符串被 `_prepare_think_input` **原样**当作 `user_message`，于是：

```text
EmotionDetector._detect_mood(...)              = "sad"        （mood 链）
consolidator.consolidate(user=…, …)                            （记忆链；以用户名作前缀渲染）
engine.buffer_message(user_msg=…, …)                           （性格反思链；role=用户名）
```

### 5.2 为什么这不是"照着现有合同补一个判据"

完整消费者地图（子代理 + 主 Agent 复核）给出的是**边界**，不是授权：

- **已裁决的只有会话历史侧**：`session history` 记录"用户真的提交过、且被 runtime
  接管过的每一条输入"，唯一豁免是 `is_task_result`（`invariants.md` §4.8）。
  CT-T7 是**测试级**登记（不在 CT1–CT6 表内）。
- **三条链没有任何合同依据**：Batch 1.8 §11.4 **显式**把它留在范围外
  （"系统注入文本被当成用户发言这个更大的问题不在本批范围"）。
- **主动搭话那半被钉在现状**：Batch 15 记录写明 `_handle_system_tick` 的主动搭话
  "**保持修前行为**（会被记录）…否则就是**未经授权的会话真相变更**"。
- **provenance 载体不存在**：`is_task_result` 在 `_finalize_turn` 边界**被丢弃**
  （`finalize.py` / `streaming.py` / `tool_phase.py` 零引用）；tick 路径**没有任何
  flag**；`core/schema/message.py` 只有自由 `metadata`（生产写入者两处，均与来源无关）；
  wire 契约无 source 字段。

### 5.3 裁决

**F13 = CONFIRMED / USER / LOW-MED / E2 + `PRODUCT_DECISION_REQUIRED` + `BLOCKED_BY_PD-13`。**

**不动 production 代码**——按"禁止 AI 自行选择产品语义"的纪律。修法成本已量化并记录
（`is_task_result` 跨 `_finalize_turn` 转发 + 给 tick 引入 provenance 参数），
PD-13 裁决后是一个小批。合同落纸在 `invariants.md` §4.10（SY1–SY3），
其中 SY1/SY2 明确标注**未实现**。

**顺带修正 finding 的前提**：**唤醒词不是文本注入路径**（只发 `wake` 帧，前端闪一下
指示器），真实入口只有两条。

## 6. KB-41 — System Event Preemption vs Input Ownership

### 6.1 窗口与 E2

非用户来源的两条回合入口**无条件** `cancel_current()`（`_handle_system_tick`
`agent.py:1209`、`_handle_task_result` `:1248`），而用户输入路径自 KB-24 起有 ownership
判据。窗口 = 回合已 first-step 但停在 `_prepare_turn` 挂起点
（`current_turn_owns_input is False`，相位仍 IDLE）。

**E2 复现（修前）**：窗口内投一个 tick ⇒ 用户那条已 ACK 的输入
`cancelled_pre_ownership`、session 里查不到，而**tick 回合的文本进了历史**：

```text
["（暖场）", "（系统提示：用户已经120秒没有说话了，请自然地开启一个话题）"]
```

### 6.2 修法与两类处置

复用**同一份**判据（`_turn_is_pre_ownership`，读 `current_turn_owns_input` +
`_started_cognitive_tasks`），按**可再生性**分处置：

- **SYSTEM_TICK**（可再生：`IdleMonitor` 1s 轮询 + `is_completely_idle` 门）⇒
  **放弃并留痕**（INFO）。liveness 由 IdleMonitor 在真正空闲时重发保证。
- **TASK_RESULT**（不可再生）⇒ **推迟**到 `_deferred_task_results`，用户回合落定后
  `_redeliver_deferred_task_results` 重新入队；**刻意不写 durable ack**（唯一授予点
  仍是 `_handle_task_result` 里 spawn 之后那一步）；重启时清空暂存（权威是库里
  `delivered=False` 那一行，由 `task_result_redelivery` 补投）；停机时清空并记
  `disposition=deferred_by_shutdown`。

**为什么不排队进 `_pending_messages`**：系统事件不是用户输入，不参与 FIFO 越队判据、
不进 pending 出队口，也不能被停机清扫按 USER_INPUT 处理。

### 6.3 守卫、变异

**守卫**：`core/agent/tests/test_system_event_preemption.py`（9 条）。
**PRE_FIX_RED**：修前 **5 红，全部红在断言上**（无 `AttributeError`——推迟队列的读取
刻意用 `getattr` 容错，与 Batch 12 的 `_UNKNOWN` 同纪律）；4 条负向控制（SE-T3/T5/T7/T9）
修前修后都绿。

| 变异 | 结果 |
|---|---|
| M41-1 恢复旧抢占 | 3 红（SE-T1/T4/T6） |
| M41-2 推迟结果永不补投（饿死） | 2 红（SE-T2/T6） |
| M41-3 绕过推迟结果的补投兜底 | 2 红（SE-T2/T6） |

**不回归**：`core/agent/tests/` 全量 **514 passed**（含 AD1–AD8、PO1–PO6、
`test_shutdown_steps_matrix.py` 的 20 步矩阵——后者的 `object.__new__(Agent)` 替身
正是"`stop()` 里的新字段要用 `getattr` 兜底"的证据链）。

**明确不做**：不改 EventBus priority、不全局锁死 system events、不做 durable queue、
不重设调度器。INTERRUPT（用户停止按钮）仍**照常**抢占（SE-T9 钉住）。

**测试方法学的一条留痕**：`_pump_until`（每次只 `sleep(0)`）**不足以**等一个自然产生的
回合走完（要跨 `_process_lock` → 会话解析 → registry → 流式调用；实测 12 次里偶发 2 次
在预算内还没到流式调用）。已改为**结构化信号**：等"出现可等待的回合任务"→ `_settle` 它。
8 连跑稳定。

## 7. KB-48 — Late Input Across Stop / Runtime Epoch

### 7.1 窗口与 E2

两条直投入口（`do_transcription`、`ws_v2._handle_voice_commit`）不查生命周期；它们跑在
aiohttp 的 WS 任务上，而 `stop()` **不**取消它们（`runner.cleanup()` 在 `agent.stop()`
返回**之后**才跑）。窗口 = 「RO1 清扫 → `stop()` 返回 → 在途生产者才投递」。

**E2 复现（修前）**：`stop()` 返回后真 `do_transcription` 仍把事件投进总线
（`bus.pending_user_inputs == 1`），`start()` 后那条文本进 session 被喂 LLM：

```text
["[TextPart(... content='旧 epoch 的转写内容')]"]
```

**生产者的完整清点**（不是只修 `do_transcription` 一个函数）：5 条 `UserInputData`
生产者——`process_message`（有判据）、pending 出队（n/a）、本批两条（**无**）、
REALTIME 语音段（有判据）+ STT epoch 守卫。

### 7.2 修法与不变量

两条入口补**同一** `status.is_service_running` 判据（与 `process_message`、
REALTIME、`do_interrupt` 同源）；处置与 `process_message` 一致：**拒收而不排队**。

- `voice.commit`：判据放在**一切副作用之前**（幂等 lookup / claim / `reserve_work`）——
  被拒的提交不留下任何预占（与 `test_process_message_rejects_before_v2_work_reservation`
  同款收口）；复用既有 rejected 路径 + `_not_accepted_error` → `AGENT_BUSY`，
  **零新增 wire 枚举**。
- `do_transcription`：**仍然回显**转写（客户端需要清掉录音态），被拒的只有入队。

| 不变量 | 内容 |
|---|---|
| **RE1** | epoch N 创建的生产者不得在 N 已停止后把用户输入投进 N+1 |
| **RE2** | `stop()` 建立的是**硬准入边界**（拒收而不排队） |
| **RE3** | 拒收陈旧生产者输出**不得**误伤 durable 的 `TaskResultData`（RO2 的分类边界不变） |

### 7.3 守卫与变异

**守卫**：`core/input/tests/test_runtime_epoch_input_boundary.py`（7 条：
RE-T1 停机后迟到转写 / RE-T2 跨 restart 不复活 / RE-T3 运行中照常 / RE-T4 新 epoch 照常 /
RE-T5 停机后 voice.commit / RE-T6 durable 保全 / RE-T7 `stop()` 仍有界）。
**PRE_FIX_RED**：修前 **3 红，全部红在断言上**。

第一版 RE-T5 的替身传了 `voice_mgr=object()` ⇒ 修前红在 `AttributeError`
（一个不存在的符号）而不是断言上，**违反 PRE_FIX_RED 纪律**——已换成能正常返回的
`_StubVoiceMgr`，使修前红在"仍投进了总线"这一真实行为上。

| 变异 | 结果 |
|---|---|
| M48-1 删除 epoch check（两条入口） | 3 红 |
| M48-2 只看一次性 bool（restart 后误放行） | 7 passed —— **未杀**（见下） |
| M48-3 停机清空一切（误杀 durable） | 红（既有 RO2 守卫 `test_stop_boundary_preserves_durable_events` 杀死，非本批用例） |

**M48-2 的诚实结论**：本批修的是**行为**（两条入口拒收），不是"state shape"；
"只看一次性 bool"这一**在代码里从未存在**的替代表述无法被行为用例区分——因为生产
代码里没有这个 bool。**不声称它被杀**。

**不回归**：`core/input/tests/` 全量 **932 passed**（含
`test_shutdown_admission.py` 的 STOPPING/STOPPED/RESTARTING × `process_message`、
REALTIME 的 `TestShutdownAdmissionGate`、`test_ws_close_budget.py`）。
`test_idempotency_durable.py::_VoiceFakeAgent` 的 Status 替身按仓库既有做法
（`test_voice_ws.py::FakeAgent`）镜像了 `is_service_running` 轴。

**明确不做**：不靠"`stop()` 再清一次 queue"糊过去（那挡不住清扫**之后**的投递）；
不新增 `RuntimeEpoch` 框架/字段（RO1 的边界仍然有效）；不动 RO2 的分类。

## 8. 本批新增的登记（只登记，不修；`todo.md` §8 = KB-83/84/85）

| ID | Reach | Sev | Ev | State | Reopen trigger |
|---|---|---|---|---|---|
| **KB-83**（budget×gate 同族残余，Challenger P7） | USER | LOW | E1+E2(19) | **OPEN（登记）** | gate 拒绝轮的 assistant 载荷不是"可丢的往返组" ⇒ 预算由截断更早的合法往返支付。触发：真实出现"合法工具结果被过早截断"的报告，或做预算/工具轮小批 |
| **KB-84**（golden frames 盲区） | CI_ONLY | LOW | E1(20) | **OPEN（登记）** | `ws-v2-session.jsonl` 回放 `messages: []` 且 17 个 frame 无任何 `_id` ⇒ `make check-types` 结构上看不见"非空回放项形状"。触发：前端契约批次给回放加非空 golden |
| **KB-85**（估算器文档/工具陈旧表述） | INTERNAL | INFO | E2(20) | **OPEN（登记）** | ①`budget.py` 的 qwen 声明 T=1.20 vs 契约测试 `_CONTRACT_MAX_RATIO["qwen"]=1.25`；②`scripts/README.md` 的"55 条语料" vs MANIFEST 71 条；③Batch 18 记录 §7.1/§7.2 仍描述已被 §16.1 取代的 9 类/2 profile 设计。触发：下次改这些文件的批次 |

## 9. 六项终态对照

| 判据（§110–115） | KB-82 | KB-56 | KB-78 | F13 | KB-41 | KB-48 |
|---|---|---|---|---|---|---|
| current HEAD E2 confirmed | ✅ | ✅ | ✅(E3) | ✅ | ✅ | ✅ |
| PRE_FIX_RED（红在真实行为） | ✅ 5 | ✅ 11 | ✅ 见 §4.6 | — (blocked) | ✅ 5 | ✅ 3 |
| production fix | ✅ | ✅ | ✅ | ❌（刻意不动） | ✅ | ✅ |
| guards | 14 | 11 | 31 | 0（合同落纸 SY1–SY3） | 9 | 7 |
| mutation | 4/4 杀 | 3/3 杀 | 4/4 杀 | 不做（无 production 改） | 3/3 杀 | 2/3 杀（M48-2 诚实未杀） |
| 不变量 | TP1–TP4 | SI1–SI4 | TE10/TE11 | SY1–SY3（未实现） | SE1/SE2 | RO4 |
| 最终状态 | **GUARDED** | **GUARDED** | **GUARDED** | **BLOCKED_BY_PD-13** | **GUARDED** | **GUARDED** |

## 10. 提交划分

```text
fix(cognitive): preserve tool-call pairing for rejected gated calls
fix(frontend/session): reject stale cross-session history responses
fix(prompt): recalibrate dense-diacritic Latin token estimates
fix(cognitive): preserve input ownership across system-event preemption
fix(agent): reject late input crossing the stopped runtime epoch
docs(architecture): record the residual correctness invariants
docs(review): record the residual correctness closure
docs(todo): close or reclassify residual correctness findings
```

F13 **没有** production commit（按 §107：不为 commit 数量硬改）。
`022ac01a` 是 KB-78 的**第二版**（外部队列覆盖修正，见 §4.3b）——它同时带上了
文档同步；`scripts/tests/test_validate_families_exit.py` 的夹具改为
`accent_ratio` 可控（缺口闭合后默认不再红，但"再坏一次"由新增的 ⑤b 守住）。

## 11. 最终验证电池（本批实测）

| 项 | 结果 |
|---|---|
| `make lint` | ✅ All checks passed（ruff） |
| `make pyright` | ✅ **0 errors, 0 warnings, 0 informations** |
| `make audit` | ✅ pip-audit：`No known vulnerabilities found, 2 ignored`；**bandit（`-ll`）：Medium = 0 / High = 0**（`-f json` 实测 `results` 为空；本批新增的 `build_latin_prose_holdout.py` 曾引入 B310 + B615 两条 Medium，已按仓内既有处置收口：钉死 40 位 revision + 运行时 https 断言 + 带理由的 `# nosec B310`） |
| `check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | ✅ 10/10 全绿 |
| `make test-quick` | ✅ **6538 passed** / 3 skipped / 1 xfailed（82s） |
| `make test-normal` | ✅ **6538 passed** / 3 skipped / 1 xfailed（81s） |
| `make test-remainder` | ✅ 0 条（Quick ≡ Normal 的互斥性保持） |
| `make test-full` | ✅ **6570 passed** / 72 skipped / 1 xfailed（490s） |
| `make test-real-mongo` | ✅ **46 passed**（50s） |
| 跨不变量回归（agent+cognitive+prompt+input+monitoring+system_prompt） | ✅ **2092 passed** / 1 skipped |
| 估算器影响面回归（prompt+system_prompt+cognitive+tool+preview） | ✅ **842 passed** / 1 skipped |
| frontend `npm test` | ✅ **11 passed** |
| frontend `npm run build` | ✅（`vue-tsc --noEmit` + vite build） |
| `validate_families.py`（E3 旁证，6 家族 × 权威 tokenizer） | ✅ EXIT=0，6/6 家族 OK，`latin_accent` 最坏 **0.82**（Yi-1.5-6B） |
| git status | ✅ clean（8 个提交，见 §10） |
| real CI | ⏳ **READY_FOR_CI**（本记录不宣称 CLOSED——owner push 后由 CI 复验） |

## 12. 程序状态

```text
KB-82 GUARDED   KB-56 GUARDED   KB-78 GUARDED
KB-41 GUARDED   KB-48 GUARDED
F13  BLOCKED_BY_PD-13（唯一未关闭项，且是产品决策而非工程缺口）
```

required CI 全绿、电池全绿 ⇒ **Residual correctness closure complete**；
余下的是**一个产品决策（PD-13）**与三条已登记的低风险新发现（KB-83/84/85）——
按 §123，**不再开新的 correctness campaign**，恢复正常产品开发。
