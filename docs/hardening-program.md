# Correctness Hardening Program（Batch 1 → 20）— 历史 roll-up

> 文档导航：[项目文档](../README.md) → [docs 索引](README.md) → 本文

> ⚠️ **HISTORICAL_SUMMARY。本文不是当前架构/行为权威。**
> 它回答的是"那轮加固做了什么、现在还剩什么"，**不是**"系统现在是什么样"。
> 当前事实看：[architecture.md](architecture.md) ·
> [architecture/invariants.md](architecture/invariants.md) · [testing.md](testing.md) ·
> [ci.md](ci.md) · [todo.md](../todo.md)。
>
> **本文的"最终状态"不是抄某一份执行记录的自我声明**，而是按 §31 的要求从
> **HEAD + todo.md + invariants.md + 测试/门禁实况 + CI 配置 + Batch 19/20 记录**
> 交叉重建的（重建方法与差异见文末「与执行记录的差异」）。

## 为什么会有这个 program

2026-09-16 的
[架构职责分离与分层重构审计](review/2026-09-16-架构职责分离与分层重构审计.md)
与 [CODE_REVIEW_REPORT.md](../CODE_REVIEW_REPORT.md) 的整改清单暴露了同一类问题的**共同形状**：

> **reported truth ≠ actual truth。**
> 系统/文档/门禁**声称**的与**实际**发生的不一致——而且不一致时**没有任何东西变红**。

这不是"某个 bug"，而是一种会自我复制的缺陷类型：
prompt 截断污染下一次 render、`/ready` 探 ping 却报 ready、409 报陈旧 revision、
`accepted` 的输入静默丢失、门禁声明了却没接线、文档写着不存在的路径……

于是 program 的判据被定为 **unknown correctness risk = minimal**（不是"todo = 0"），
并要求每一批都产出**可执行的守卫**（测试 / 门禁 / 不变量落纸），而不是"改完就算"。

## Batch 1 → 20 一览

每批 1–3 行。**证据一律在对应执行记录里，本表不复制。**

| Batch | 日期 | 主题 | 主要不变量 / finding | 结果 | 记录 |
|---|---|---|---|---|---|
| 1 | 09-16 | correctness / durability | P1/P2 · R（prompt 截断状态污染、中断丢 `TaskResultData`） | 4 条缺陷修复 + 不变量落纸 | [记录](review/2026-09-16-Batch1-correctness-durability-执行记录.md) |
| 1.5 | 09-16 | 配置/测试真值性 | 测试与配置不得自证 | 修 3 类假绿 | [记录](review/2026-09-16-Batch1.5-config-truthfulness-执行记录.md) |
| 1.6 | 09-17 | runtime 资源所有权 | resource lifecycle ownership | 生命周期归属收口 | [记录](review/2026-09-17-Batch1.6-runtime-authority-lifecycle-执行记录.md) |
| 1.7 | 09-17 | 后台任务终态 | T1–T4（终态必须持久且唯一） | 新增 `background_task_results` + 本地 WAL | [记录](review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) |
| 1.8 | 09-17 | 用户可见终态语义 | U1–U5（内部真值 → 用户可见） | 语义映射落纸 | [记录](review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md) |
| 2 | 09-17 | 门禁加固 | 「假绿」三形态 + R1–R6 | `check-arch` 五规则；删掉假门禁 R5 | [记录](review/2026-09-17-Batch2-guardrails-hardening-执行记录.md) |
| 3 | 09-17 | 删除 / 简化 | false-dead 审计 | 删死代码 + `P3-01-RESERVED` 标记纪律 | [记录](review/2026-09-17-Batch3-deletion-simplification-执行记录.md) |
| 3.5 | 09-17 | B3-CB 分诊 | 7 条 → 3 修 4 分类 | 分诊闭环 | [记录](review/2026-09-17-Batch3.5-correctness-triage-执行记录.md) |
| 3.6 | 09-18 | 回归守卫补全 | N-4 / N-8 | 两道表级/路径级守卫 | [记录](review/2026-09-18-Batch3.6-regression-guard-completion-执行记录.md) |
| 4（分诊） | 09-18 | backlog 重建 | — | 重建 `todo.md` 为 single source of truth | [记录](review/2026-09-18-Batch4-backlog-triage.md) |
| 4 | 09-18 | runtime health 真值 | N-1 / N-11 · **PD-1** | `/ready` 分层探针；`/ready` 假绿消除 | [记录](review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md) |
| 5 | 09-18 | provider 能力真值 | N-10 / F6 / F7 · PC1–PC6 | `is_implemented()` 成为唯一权威 | [记录](review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md) |
| 6 | 09-18 | 冲突元数据真值 | N-3 · CM1–CM5 · **PD-2** | 409 报权威库现值 | [记录](review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md) |
| 7 | 09-18 | provider 预览窗口 | F8 · PW1–PW6 | 预览遵守 resolved 窗口；不可能输入本地 400 | [记录](review/2026-09-18-Batch7-provider-preview-window-执行记录.md) |
| 8 | 09-18 | 分页完备性 | P2-01r · PG1–PG6 | 第 201+ 条可达 | [记录](review/2026-09-18-Batch8-pagination-completeness-执行记录.md) |
| 9 | 09-18 | SPEAKING 恢复 | KB-6 · SP1–SP4 | TTS 取消不再卡相位 | [记录](review/2026-09-18-Batch9-speaking-state-recovery-执行记录.md) |
| 10 | 09-18 | THINKING 取消恢复 | KB-22 · TH1–TH5 | 取消不留永停相位 | [记录](review/2026-09-18-Batch10-thinking-state-cancellation-recovery-执行记录.md) |
| 11 | 09-18 | 取消安全的回合收口 | KB-25 / KB-23 · CF1–CF6 | 重复取消免疫 | [记录](review/2026-09-18-Batch11-cancellation-safe-turn-finalization-执行记录.md) |
| 12 | 09-18 | 已 ACK 输入投递 | KB-24 · AD1–AD8 | admission 窗口所有权 | [记录](review/2026-09-18-Batch12-accepted-input-delivery-closure-执行记录.md) |
| 13 | 09-19 | pending 输入定序 | KB-38 · PO1–PO6 | 重投保序 | [记录](review/2026-09-19-Batch13-pending-input-ordering-执行记录.md) |
| 14 | 09-19 | 残余输入所有权分诊 | KB-47 · RO1–RO4 | epoch 清扫后不得复活 | [记录](review/2026-09-19-Batch14-residual-input-ownership-triage-执行记录.md) |
| 15 | 09-19 | 已接管输入的会话真值 | KB-46 · CT1–CT6 | claimed input 必须进会话历史 | [记录](review/2026-09-19-Batch15-claimed-input-conversation-truth-执行记录.md) |
| 16 | 09-19 | 会话数据完整性 | KB-15 / KB-16 · SD1–SD6 | legacy 可达 + `session_id` 唯一 | [记录](review/2026-09-19-Batch16-session-data-integrity-执行记录.md) |
| 17 | 09-19 | Ollama provider adapter | KB-13 / AR-16 · **PD-12** · OL1–OL6 | native `/api/chat`；`num_ctx` 与预算同源 | [记录](review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md) |
| 18 | 09-19 | 上下文 / token 估算合同 | KB-12 · TE1–TE8 | 6 profile × 字符类 + 语料合同 | [记录](review/2026-09-19-Batch18-context-token-estimation-执行记录.md) |
| 19 | 09-20 | 长尾 backlog 收口 | KB-77 · **TE9** | 回合内每轮重施预算；backlog 判例化 | [记录](review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) |
| 20 / Residual Closure | 09-20 | 残余 correctness 收口 | KB-82 · KB-41 · KB-48 · KB-78 · KB-56 · F13 · SY1–SY3 | 5 条修复 + 1 条转产品决策（PD-13） | [记录](review/2026-09-20-Batch20-residual-correctness-closure-执行记录.md) |

> **Batch 4 有两个文件**（分诊稿 + 执行记录）；`Batch 2.1/2.2/2.3` **没有独立文件**，
> 是 Batch 2 记录内部的子章节。编号消歧见 [review/README.md](review/README.md)。

## 不变量家族（program 的产出形态）

program 的主要产出不是"修了 N 个 bug"，而是**把每类缺陷固化成不变量家族**——
每条都有 ID、判据（测试）、门禁与 CI 归属：

| 家族 | 主题 | 权威 |
|---|---|---|
| `P` | Prompt 预算降级是 render-local 的 | [invariants.md](architecture/invariants.md) §1 |
| `PW` | Provider preview window contract | 同上 §1.1 |
| `TE` | Token estimation contract（含 TE9 回合内预算） | 同上 §1.2 |
| `T` | Background Task 终态 | 同上 §2 |
| `R` / `PC` / `CM` / `OL` | Runtime readiness / provider 能力真值 / 冲突元数据真值 / Ollama runtime context | 同上 §3–§3.4 |
| `PG` / `SP` / `TH` / `CF` / `AD` / `PO` / `RO` / `CT` / `SD` | 用户可见语义：分页 / 说话 / 思考 / 收口 / 准入 / 定序 / epoch / 会话真值 / 数据完整性 | 同上 §4.1–§4.9 |
| `SY` | 合成注入 provenance（**未裁决**，见 PD-13） | 同上 §4.10 |
| `SE` | 系统事件抢占 | Batch 20 记录 + 守卫 |

## 最终状态（从 HEAD 交叉重建）

### 已关闭

- **Systematic correctness hardening：closed。** 20 个批次全部有执行记录、守卫与不变量，
  没有"改完没守卫"的批次。
- **Residual correctness closure：complete。** Batch 20 的 5 条 finding
  （KB-82 / KB-41 / KB-48 / KB-78 / KB-56）全部 FIXED，且各自带守卫与变异测试
  （M82/M41/M48/M78/M56 系列全杀）。
- **18 份不变量家族**落纸在 [invariants.md](architecture/invariants.md)，
  每条都能追到测试与 CI job（该文件 §7 单独列出**跑不到的**假保障，不混进"已覆盖"）。

### 尚未关闭（**如实登记，不写成"全修完了"**）

| 项 | 状态 | 为什么还开着 |
|---|---|---|
| **F13** 合成注入被当作"用户消息"参与 mood / 记忆 / 反思三条链 | **BLOCKED_BY_PD-13** | 会话历史侧已由 `is_task_result` 豁免并测试级钉住（CT-T7）；其余三条链**没有任何合同依据**，过滤与否是**产品语义**决定。纪律：裁决前**禁止** AI 自行补过滤。修法成本已知（`is_task_result` 跨 `_finalize_turn` 转发 + 给 tick 引入 provenance 参数）。见 [invariants.md](architecture/invariants.md) §4.10 SY1–SY3、[todo.md](../todo.md) §3 PD-13 |
| **GR-24** 部分索引语义不参与 schema drift 比对 | ARCHITECTURE_DEBT | 无 required CI 门禁对它变红；需要给"部分索引"建独立登记语义 |
| **GR-4 / GR-16** 两个**已复现的假绿** | PARTIALLY_GUARDED | R4 eager 环判据漏报相对导入/包节点；desktop golden 未从 runtime 单源 |
| **KB-27 / KB-32** 超时预算门禁的两个盲区 | PARTIALLY_GUARDED / ARCHITECTURE_DEBT | `*timeout*` 形参名与跨模块预算对门禁不可见 |
| **10 条** observability / tooling 债 | OBSERVABILITY_DEBT 等 | 不影响 runtime correctness，见 [todo.md](../todo.md) §1.4 |
| **16 条**待 owner 拍板的产品决策 | PRODUCT_DECISION_REQUIRED | 见 [decisions.md](decisions.md) §Pending |

> **正确说法**：*"系统性/残余 correctness hardening 已收口；剩下的是 1 条产品语义阻塞项
> （F13/PD-13）、2 条已复现的假绿、2 条门禁盲区、若干架构债与可观测性债。"*
> **错误说法**："所有可能的 bug 都修完了。"

### 这一轮之后新增的守卫（program 的真正遗产）

| 守卫 | 管什么 |
|---|---|
| `make check-arch`（R1–R4 / R6） | 依赖方向、跨包私有符号、eager 环、裸后台任务登记（**逐点记期望条数**） |
| `make check-timeouts` | 具名超时预算必须表态（外层 ≥ 内层 / 叶子 + 理由） |
| `make check-contract` | 契约枚举成员必须有生产者、字段有读必须有写（预留须带解除条件） |
| `make check-ci-isolation` | 信任边界：PR 可达 job 禁 self-hosted / 禁死 job / 清理必须 fail-closed |
| `make check-test-sets` | `Quick ∪ remainder == Normal` 且交集为空 |
| `core/tests/test_cross_package_privacy.py` + `core/input/tests/test_layer_discipline.py` | 跨包私有穿透（含**方法**） |
| `core/tests/test_doc_counts.py` | 文档里的 `@property` 计数与枚举与代码一致（四份文档） |
| `scripts/check_docs.py` | 文档漂移守卫（canonical 链接可解析、禁止引用已不存在的路径、env 索引与会话/错误码一致等）——见 [readme-conventions.md](readme-conventions.md) |
| 根 `conftest.py` 的 marker 钩子 | 每个测试必须有分层 marker，快慢层不得交叠 |

## 与执行记录的差异（透明说明）

§31 要求最终状态**不**来自执行记录的自我声明。逐条核对结果：

| 执行记录的说法 | 核对结果 |
|---|---|
| Batch 19 记录 §217 自陈"**本记录不宣称 CLOSED**" | ✅ 一致——本文也不把"hardening 收口"归给 Batch 19 |
| Batch 20 记录引用"Batch 19 宣称 `Systematic correctness hardening program = CLOSED`" | ⚠️ **该引文在 Batch 19 记录里查不到**（grep "systematic"/"program" 零命中）。本文因此**不引用**这句，改按 HEAD + todo 事实陈述："20 批全部有记录与守卫，程序收口" |
| 各批次的"全部闭合 / 无残留"表述 | ⚠️ 只对**该批范围内**成立。全仓范围内仍有 1 条阻塞项 + 2 条假绿 + 2 条门禁盲区（见上表），本文按全仓口径写 |
| "hardening 结束" | ⚠️ 收口 ≠ 无债。[todo.md](../todo.md) §2/§4 仍挂着 deferred 与架构债，那是**设计选择**不是漏项 |

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [review/README.md](review/README.md) | 全部执行记录的索引（含两套 Batch 编号的消歧） |
| [architecture/invariants.md](architecture/invariants.md) | 不变量权威；每条 → 测试 → 门禁 → CI，另有「跑不到的假保障」清单 |
| [todo.md](../todo.md) | **当前** backlog（本文的"尚未关闭"以它为准，不以历史记录为准） |
| [decisions.md](decisions.md) | 已裁定决策索引；`PD-13` 的阻塞关系 |
| [../CODE_REVIEW_REPORT.md](../CODE_REVIEW_REPORT.md) | 另一套编号（报告 Roadmap Batch 1–9）的审计台账 |
