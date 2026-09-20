# Batch 2 — Architecture & CI Guardrails Hardening 执行记录

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → [review](.) → 本文

**范围**：`4203198f..0da53486`（12 个 commit，172 文件 +2193/−453）
**目标**：把前几批已验证的架构不变量、测试可信度要求、资源所有权规则与 CI 约束
转成**长期可执行的自动门禁**——不是加更多规则，而是让已有规则**真的拦得住它自称拦的东西**。
**不做**：LLMAdapter 迁移 / TTSStreamManager 拆分 / WorkflowEngine 重写 / 任何业务 feature。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| 现有 guardrails 是否可信？ | **部分不可信**。找到 **5 处"判据比声称的弱"**（4 处假阴性/1 处假红）+ **1 条死规则**。全部已修，且每处都配了变异验证的回归测试。 |
| 修了哪些假绿？ | ① 私有守卫**只收字段不收方法**（跨包私有方法调用整类静默放行，实测漏 9 处）；② `check_test_sets` 的"非退化"判据**比的是全量而非选中数**（三个表达式全拼错时仍报绿）；③ CI 清空形态识别**漏 `rm -rf "$WS"/*` 等真清空写法**（三道清理子句整条不适用）；④ `RUN_EXTERNAL_TESTS` 门禁**只覆盖 `Module/`**（`core/` 的 external 用例未 opt-in 也真跑、真计费）。 |
| 删除了哪些 dead rule？ | **R5**（`_type_only_edges()` 定义了却从未被 `main()` 调用）——连同无消费者的 `_Module.type_edges` 桶整条删除，并加 3 条回归用例防复活。 |
| CI 是否覆盖 PR？ | **仍为 push-only，且这是刻意决定**（owner 2026-09-17）。理由、三种触发方式的信任差异、以及恢复 PR 门禁的**唯一安全前提**（与常驻 runner label 不相交的一次性隔离 runner）写进 `docs/ci.md`。 |
| F401 如何纳入？ | `ruff.toml` 的 `select` **从来没有 F401**（全仓 476 处无人管）。先分类再动手：生产 198 + 测试 140 **删除**（现为 0）、`__init__.py` 137 处**保留**（逐项核实全是有意重导出，`--fix` 曾误删 2 处导致 59 个模块 ImportError）。**新增代码不再扩大债务**。 |
| SLF001 如何处理？ | **没有一刀切**。全仓 750 处中 **709 在测试、41 在生产**，且 **ruff 根本没启用 SLF001**——那些标注对 lint 是惰性的。真正执行这条纪律的是两道守卫；本批把其中一道的覆盖缺口补上，并把"惰性"这一事实写进 CLAUDE.md。 |

---

## 2. Guardrail Inventory

| Gate | Entry Command | Implementation | Intended Invariant | Actual Coverage | 判定 |
|---|---|---|---|---|---|
| 架构边界 | `make check-arch` | `scripts/check_architecture.py` | 依赖方向 / 跨包私有符号 / eager 环 / 裸 spawn 登记 | R1/R2/R3/R4/R6 硬失败 + 3 项 R1 豁免 | **VALID_HARD_GATE**（R4 有独立专项缺陷，见 §3） |
| 私有穿透（全仓） | `make test-normal` | `core/tests/test_cross_package_privacy.py` | 跨包私有访问必须公开化或标注 | 字段**与方法**（本批修复）；扫描面 `core`+`Module` | **VALID_HARD_GATE**（同名遮蔽为已知代价） |
| 私有穿透（输入层） | `make test-normal` | `core/input/tests/test_layer_discipline.py` | 输入层任何未标注穿透即红 | 本来就认方法，比全仓更严 | **KEEP_AS_IS** |
| 测试集合代数 | `make check-test-sets` | `scripts/check_test_sets.py` | `Quick ∪ remainder == Normal` 且交集空 | 三条恒等式 + **选中数非零**（本批修） | **VALID_HARD_GATE** |
| CI 信任边界 | `make check-ci-isolation` | `scripts/check_ci_isolation.py` | PR 不可达 self-hosted / 清理纪律 / 最小权限 | 6 条规则，5 处本批加固 | **VALID_HARD_GATE** |
| 超时预算 | `make check-timeouts` | `scripts/check_timeout_budget.py` | 外层 ≥ 内层；具名预算必须表态 | R1–R4 | **KEEP_AS_IS** |
| 契约可达性 | `make check-contract` | `scripts/check_contract_reachability.py` | 枚举有生产者 / 字段有读必有写 | 含陈旧登记检查 | **KEEP_AS_IS** |
| 契约漂移 | `make check-types` | `frontend_contract --check` + frames | ts/schema/raw_ts/frames 无漂移 | 17 golden frames | **KEEP_AS_IS** |
| Debug 鉴权 | `make check-debug-auth` | `scripts/check_debug_auth.py` | debug 前端必须带凭据；禁 EventSource | 21 文件 | **KEEP_AS_IS** |
| 密钥扫描 | `make check-secrets` | `scripts/check_secrets.py` | 无凭据形态入库 | 被跟踪文件；`--all-refs` 在浅克隆拒答 | **KEEP_AS_IS** |
| Compose 安全 | `make check-compose` | `scripts/check_compose_security.py` | 数据服务仅 loopback / 无默认凭据 | 2 个 compose 文件 | **KEEP_AS_IS** |
| 环境锁 | `make check-env-lock` | `scripts/check_env_lock.py` | requirements ↔ lock 版本满足 | 20 个顶层包 | **KEEP_AS_IS** |
| 依赖/静态安全 | `make audit` | pip-audit + bandit | 0 未豁免漏洞 / 0 Medium+ | Low 19 项为已知基线 | **KEEP_AS_IS** |
| 导入风格 | `make lint` | `ruff` | 禁相对导入 / 参数注解 / **unused import** | F401 为本批新增 | **VALID_HARD_GATE** |
| 类型 | `make pyright` | pyright | 生产代码类型正确 | 0 error / 0 warning | **KEEP_AS_IS** |

---

## 3. False-Negative Findings（规则声称检查、实际漏掉）

| # | 位置 | 声称 | 实际 | 证据 |
|---|---|---|---|---|
| FN1 | `core/tests/test_cross_package_privacy.py` | 跨包私有穿透守卫 | **归属表只收赋值目标**（`ast.Assign`/`AnnAssign`），`def _m` 一个不收 ⇒ `if defining and …` 恒假 ⇒ 跨包私有**方法**调用全量放行 | 修复前 0 处 → 修复后 **9 处**（`tts_adapter.py`×8 + `streaming.py`×1 调 `Module/output/manager.py::OutputManager._broadcast`） |
| FN2 | `scripts/check_test_sets.py` 判据 0 | "三次收集都因拼错 marker 返回空集"被防 | 比的是 **`total`（`-m` 过滤前的全量）**，marker 拼错时选中数变 0 而 total 保持非零 ⇒ `∅ ∪ ∅ == ∅` 平凡成立、**退出 0 报绿** | `expr='unitXX'` → `no tests collected (5932 deselected)`，旧判据照绿 |
| FN3 | `scripts/check_ci_isolation.py` `_wipes_workspace_root()` | "清空 workspace 的步骤必须密闭+双守卫+fail-closed" | rm 分支要求整个 token **就是**工作区根 ⇒ `rm -rf "$WS"/*`、`rm -rf "$GITHUB_WORKSPACE"/*`、`find … -delete` 均**不被认作 wipe** ⇒ 三道子句一条都不适用 | 端到端：这些写法在 self-hosted push job 里**0 违规** |
| FN4 | `Module/conftest.py` external 门禁 | `RUN_EXTERNAL_TESTS=1` 才跑 external | conftest 按**目录**生效 ⇒ `core/` 下的 external 用例走不到 ⇒ **未 opt-in 也真跑**（真实 DeepSeek 调用 + 真实 Milvus） | `pytest core/tests/test_phase2.py -m external` → `3 passed in 13.01s`（未设该变量） |
| FN5 | `scripts/check_ci_isolation.py` permissions 规则 | "pin least privilege" | 只查**存在性** ⇒ `permissions: write-all` 放行（比不写更危险） | Challenger 探针：`write-all` → 0 违规 |
| FN6 | `scripts/check_architecture.py` R5 | "`TYPE_CHECKING` 类型边仅报告" | `_type_only_edges()` **从未被 `main()` 调用** ⇒ 不打印、不参与退出码、无消费者 | 纯 `TYPE_CHECKING` 的 `Module → core.agent` 边在 `make check-arch` 下完全静默 |
| FN7 | `scripts/check_ci_isolation.py` `_is_push_only()` | 受信守卫识别 | 与死 job 判定**各自**判 guard，此处要求字面量 `"== 'push'"` ⇒ `github.event_name=='push'`（无空格）被判非受信 ⇒ **假红**（逼人删 guard） | 8 种写法参数化实测 |

---

## 4. False-Positive Findings（规则会错误阻止合法代码）

| # | 位置 | 误判 | 处置 |
|---|---|---|---|
| FP1 | `check_test_sets.py`（本批第一版修复**自己引入**） | `len(normal) == total_n ⇒ 拒答`：把**语料的性质**当**不变量的性质**——全 fast 的仓库里 `not slow and not external` 合法地选中全量 | **删除**，并留一条**反向回归用例**（`test_exclusion_clause_selecting_everything_is_green`） |
| FP2 | `check_ci_isolation.py` 清空识别（第一版修复引入） | 可选后缀组可回溯 ⇒ `rm -rf "$WS"/static/assets`（删构建产物）被判成清空 | 改为**纯字符串判定**（§6 说明四轮演进） |
| FP3 | 同上 | `rm -rf ./*.log`、`rm -rf ./.cache` 被判成清空 | 同上；两条已进 `_NEGATIVE` 参数化用例 |
| FP4 | `_is_push_only()` | 见 FN7 | 与死 job 判定**共用同一正则** |
| FP5 | `check_ci_isolation.py` 对 PR **不可达** job 的 `runs-on: ${{ … }}` | 审计指出"trusted push-only 时表达式 runs-on 不被检查，清理纪律也整段跳过" | **接受风险**（登记 todo.md）：受信 ref 上的表达式 runs-on 不构成不受信代码执行路径 |

---

## 5. Dead / Redundant Rules

| 规则 | 状态 | 判据 | 处置 |
|---|---|---|---|
| **R5**（`TYPE_CHECKING` 类型边仅报告） | **DEAD_RULE** | `_type_only_edges()` 全仓 grep 只命中自己的 `def`；`main()` 调用图无它 | **整条删除**（函数 + `_Module.type_edges` 桶）。刻意**不留** `--report` 开关——无消费者的代码正是 R5 当初退化的方式 |
| R1/R2/R3 allowlist | **REDUNDANT（有意）** | R2/R3 allowlist 为空且期望为空；R1 3 项（bench×2 + documentdb `__main__`）逐项附理由 | **KEEP_AS_IS** |
| `# noqa: SLF001` ×750 | **惰性但非死** | `ruff.toml` 的 `select` 无 SLF001 ⇒ 对 lint 无作用；真正执行纪律的是两道守卫 | 写进 CLAUDE.md（避免"看到标注就以为 lint 在管"） |
| R6 的"条数漂移"分支 | **零测试覆盖** | 变异实测：`elif entry[0] != count` → `elif False` 仍 20 passed（该分支被 `is_real_repo` 门控，合成 fixture 走不到） | 登记 todo.md（P3） |

---

## 6. Private-Access Analysis

### 全仓 `noqa: SLF001` 分类（750 处，实测）

| 维度 | 计数 |
|---|---|
| **测试 vs 生产** | **测试 709 / 生产 41** |
| 按包 | `core` 656（29/627）、`Module` 61（0/61）、`scripts` 11（7/4）、`frontend_contract` 3（0/3） |
| 场景桶 | (A) 同包内部 303、(B) 单测白盒断言 220、(C) 测试驱动私有方法 165、(D) 生产跨模块 23、(E) 跨层 9 |
| 生产跨包未标注（修复前） | **9**（`OutputManager._broadcast` 的 9 个调用点） |

> 前序审计提到的"~599"是**测试单侧、更早提交**的数字；当前测试侧是 709。

### 关键判定

- **"只收字段不收方法"的精确机制**：断的不是检测器（`_penetration()` 匹配任意 `_` 前缀
  `ast.Attribute`，`ast.walk` 会访问 `obj._m()` 的内层），而是**归属表**——
  `owners.get("_private_method")` 恒空 ⇒ `if defining and …` 恒假。
- **真实泄漏只有 9 处，全部是 `core/` 调 `Module`**：`core/adapter/tts_adapter.py` 8 处
  （`flush_emojis` + 句前/句后 emoji + 句首舞台指示 + 句子正文 + 取消兜底）与
  `core/cognitive/streaming.py:243`（无 ctx 的 v1 纯文本广播）。
- **处置**：8 处收成一个 `_broadcast_text_delta()`——**唯一**带理由的豁免点；理由是
  `OutputManager` 的公开面（`text_delta`/`inner_voice`/`segment_*`）**都要求
  `GenerationContext`**（承担 generation 生命周期契约），而 TTS 这几类文本按设计
  不属于任何 generation、且要带 `stream_id`（TTS 侧播放 ID），语义上无法用公开面表达。
  长期方向（已写进 docstring）：补 `text_delta_unbound()`，届时豁免点删除。
- **行为保持**：Challenger 用 AST 逐点比对确认 8↔8 个调用点、实参一致、
  `stream_id=sid` 出现在同样的 7 处、`send("")` 8↔8、两处 `try` 未动、顺序不变。

### 已知局限（未修，登记 todo.md）

在**访问方包内**定义一个同名 `def _x` 即可让该属性名全仓免检
（实测 9 处违规塌成 1 处）。这是"按属性名归属近似判跨包"的固有代价
（同名属性本来就会漏报，模块 docstring 早已记载），本批让私有方法也进归属表后**更容易触发**。
**守卫的定位仍是回归网，不是完备审计工具。**

---

## 7. CI Trigger Map

| Workflow | push | pull_request | workflow_dispatch | schedule | 备注 |
|---|---|---|---|---|---|
| `ci.yml` | ✅ `branches:[main]`，`paths-ignore: docs/**` | ❌ | ❌ | ❌ | `backend` / `real-mongo` / `frontend` 三 job，全 self-hosted |
| `contract-frames.yml` | ✅ `branches:[main]`，`paths: traces/**, frontend_contract/**` | ❌ | ❌ | ❌ | `frames` job |
| `nightly.yml` | ❌ | ❌ | ✅ 输入 `suite`（full/external/both） | ❌ | **文件名叫 nightly 但没有定时器**——`external` 因此 0 次自动执行 |

**无 `pull_request` / `pull_request_target` / `workflow_run` / `repository_dispatch`**（实测 0 命中）。
本批为 `contract-frames.yml` 与 `nightly.yml` 补上 `permissions: contents: read`
（此前只有 `ci.yml` 有），并把"必须显式声明且**只读**"变成门禁规则。

---

## 8. Self-hosted Security Model

**能跑在 self-hosted 上的代码**：只有 push 到 `main`（受信 ref）与受信管理员
`workflow_dispatch` 的**仓库自身**代码。6/6 job 都带
`if: github.event_name == 'push'`（或 `workflow_dispatch`），由
`make check-ci-isolation` 静态强制。

**不能跑在 self-hosted 上的代码**：任何不受信 PR 的代码——包括同仓 PR。
理由（`docs/ci.md` 新增章节）：`pull_request` 的工作流取自 PR merge commit
（**作者可控**），`make`/`pytest`/`npm ci` 全是任意代码执行；宿主机有真实 `.env`
（14 个凭据名）、持久 workspace 与真实用户身份。

**三条被明确拒绝的"替代方案"**：
1. `pull_request_target` —— 更危险而非更安全（拿 secrets + 写 token；"只跑 base 代码"
   的保护只要有一个步骤 checkout PR head 就整体失效）；
2. environment + required reviewers —— 管的是"何时批准"，批准**之后**不受信代码照样
   在持久机器上跑（GitHub 官方明确说这类工作流 not in an isolated environment）；
3. 手工 `workflow_dispatch` 跑 PR —— 正是 pwn request 的形状。

**恢复 PR 门禁的唯一安全前提**：一次性的 ephemeral / 容器 / VM 隔离 runner，
且**打上与常驻 runner（`self-hosted, Linux, X64`）不相交的 label**。
账单约束仍然成立：GitHub-hosted job 在额度被卡后**根本不会启动**，
所以"用托管 runner 兜底"在本仓当前状态下不成立。

---

## 9. Test-Set Truth Table

实测（`scripts/print_test_stats.py`，2026-09-17）：

| 集合 | 表达式 | 数量 |
|---|---|---|
| full | （无 `-m`） | **6002** |
| unit | `unit` | 3389 |
| service | `service` | 2585 |
| integration | `integration` | 7（**全部与 slow/external 同标**；`integration and not slow and not external` = 0） |
| slow | `slow` | 39 |
| external | `external` | 18 |
| real_mongo | `real_mongo` | 8 |
| **Quick** | `unit or service` | **5952** |
| **Normal** | `not slow and not external` | **5952** |
| **remainder** | `not slow and not external and not unit and not service` | **0** |
| 未标注（必须 0） | — | 0 |
| 快慢交叠（必须 0） | — | 0 |

**恒等式**：`Quick ∪ remainder == Normal`（5952 == 5952）、`Quick ∩ remainder == ∅`。

### 本批改变的东西

`Normal == Quick` 是**新状态**：此前 remainder 有 3 条 `integration`-only 用例，
而它们**实际打真实 DeepSeek（计费）与真实 Milvus**——却在 push 门禁里跑，
而文档三处称该档位"全量离线"。已按 `pytest.ini` 对 `external` 的定义改正 marker。
**证明"Normal 现在真离线"不是靠读代码**：Challenger 用插件拦掉所有非 loopback
DNS/connect（并在 xdist worker 内验证拦截生效）后重跑整个 Normal 档位 → 与常规运行
**逐字相同的计数**（5928 passed / 2 skipped / 1 xfailed）。

`make test-remainder` **保留**（当前 0 条）：它是"Quick/remainder 互斥"这条前提的
可运行定义，也是日后 drift 的承接点。

`norecursedirs` 追加 `tmp`：此前本地收集面**比 CI 宽**（CI 是干净 checkout，
本地把 gitignored 的 `tmp/` 草稿区里叫 `test_*.py` 的文件也收进来）。
实测同一 commit 上一份报 5931、另一份报 5951；草稿文件还能让
**`make test-quick` 本地红而 CI 全绿**。修后本地与干净 worktree 逐字相同。

---

## 10. Static Hygiene Strategy

### F401（本批启用）

`select` 里**从来没有** F401。分类后处置：

| 类别 | 数量 | 处置 |
|---|---|---|
| 生产代码（非 `__init__`） | 198 | **删除** → 现为 **0** |
| 测试文件 | 140 | **删除** → 现为 **0** |
| `__init__.py` | 137 | **保留**（逐项核实全是有意重导出）+ per-file-ignore |
| `__main__.py` | 1 | 删除 |

**为什么 `__init__.py` 必须豁免**：`ruff --fix` 首轮就**误删两处真重导出**
（`core/schema/types.py` 的 `SessionId` → **59 个模块 ImportError**；
`core/debug/info.py` 的 `asdict` → 在 `core/debug/__init__.py` 的 `__all__` 里）。
后续又发现**四处跨模块重导出**（`core/input/server.py` 的 `warn_if_unsecured`/
`assert_secure_bind`/`is_loopback_host`、`core/personality/engine.py` 的 `_extract_tag`
——原注释还写着"保持向后兼容"，注释留着名字没了、`core/input/ws_handler.py` 的
`_handle_activate_session`）被删，实测 15 条用例 ImportError。已用**显式别名**
（`X as X`）恢复——别名本身就是「我有意重导出」的语义。

**为了不再漏这类问题**，引入一个可复用的检测手段：AST 扫全仓 `from X import Y`，
再逐个在 import 期 `hasattr` 解析——它一次报出全部 4 处回归。

### SLF001（本批不动）

**没有清零，也不该清零**：709/750 在测试侧，是正常的白盒断言与私有方法驱动。
本批做的事是**让生产侧的跨包穿透有牙**（补上方法覆盖），而不是消灭 `_xxx`。
另外明确记录一个事实：`ruff.toml` **未启用 SLF001**，所以全仓
`# noqa: SLF001` 对 lint 是**惰性的**——不是"被豁免"，是"没人读"。

### Ratchet

- F401：**`__init__.py` 之外一律为 0**（新建即红），本批即达到；无需 baseline 计数
  （比"≤ N"更强且不受环境漂移影响）。
- SLF001：**不加 ratchet**。理由：真正的判据是"跨包 + 生产 + 未标注"，
  而那正是私有守卫在做的事（本批已修好）。对一个**未被 lint 启用**的规则做计数
  ratchet 只会制造维护负担。

---

## 11. Architecture Invariant Map

完整版（含每条不变量的来源、机制说明与「有测试但 CI 跑不到」的假保障清单）
见 **[docs/architecture/invariants.md](../architecture/invariants.md)**。

摘要：

| 组 | 条数 | 测试 | CI |
|---|---|---|---|
| Prompt（P1–P2） | 2 | `test_render_local_budget.py`(12) / `test_system_prompt_leak.py`(21) | push/backend |
| Background Task（T1–T7） | 7 | `test_task_terminal_closure.py`(4) / `test_task_result_durability.py`(26) / `test_task_result_interrupt_delivery.py`(8) / `test_task_outcome_user_semantics.py`(19) | push/backend |
| Runtime（R1–R4） | 4 | `test_runtime_state_persistence.py`(4) / `test_state_single_authority.py`(11) / `test_runtime_authority_end_to_end.py`(1) / `test_cli_overrides.py`(22) / `test_task_llm_ownership.py`(10) | push/backend |
| User-visible（U1–U5） | 5 | `test_task_outcome_user_semantics.py`(19) | push/backend |
| 架构/静态 | 19 | 各 `scripts/check_*.py` + 守卫测试 | push/backend |

**CI 列每条都实测过**（按 `-m "not slow and not external"` 收集一遍确认 > 0），
不是照抄文档。

---

## 12. Mutations

全部在临时改动后 `git restore`，最终工作区干净。

| # | 变异 | 预期 | 实际 |
|---|---|---|---|
| M1 | 真实仓库 `Module/output/manager.py` 注入 `from core.agent.agent import Agent` | `check-arch` 红 | ✅ `[R1] ✘ 违规 Module/output/manager.py → core.agent.agent`，退出 1 |
| M2 | 注入跨包**私有方法**调用（`output._split_versions()`，不带 noqa） | 守卫红 | ✅ 1 failed（修复前**静默通过**——这就是 FN1） |
| M2b | 删掉 `_private_attr_defs` 的方法收集分支 | 3 条新用例红 | ✅ 3 failed |
| M3 | 三个 marker 表达式全改成不存在的名字 | 集合门禁拒答（2） | ✅ 修复前 **退出 0 报绿**；修复后退出 2「不能据此报绿」 |
| M3b | 单独改坏 Quick 表达式 | 拒答 | ✅ exit 2 |
| M3c | Makefile 的 `test-normal` 表达式漂移 | 单源比对红 | ✅ 1 failed |
| M3d | 弱化"选中数非零"判据（模拟原缺陷） | 3 条新用例红 | ✅ 3 failed |
| M3e | 从 `_SLOW_LAYER` 删 `real_mongo` | marker 登记比对红 | ✅ 1 failed |
| M3f | 往 `pytest.ini` 独加新 marker | marker 登记比对红 | ✅ 1 failed |
| M4 | 生产文件注入 unused import | `make lint` 红 | ✅ 报 F401（修复前完全看不见） |
| M5 | 回退清空识别到旧判据 | 6 条用例红 | ✅ 6 failed（含端到端"缺守卫必须报违规"） |
| M5b | 删掉裸 `$GITHUB_WORKSPACE` 写法 | 参数化用例红 | ✅ 3 failed |
| M5c | permissions 退回"只查存在性" | 权限用例红 | ✅ 3 failed |
| M6 | 复活 R5（重新加函数 + 字段） | 死规则用例红 | ✅ 2 failed |
| M7 | 把 `--report` 接回默认输出 | 回归用例红 | ✅ 1 failed |
| M8 | docstring 宣传不存在的 `--verbose` | phantom flag 用例红 | ✅ 1 failed |
| M9 | 删掉仓库级 external 门禁（模拟修前状态） | `core/` external 用例红 | ✅ 1 failed（该用例断言"未 opt-in 时**不得**出现 `1 passed`"） |

---

## 13. Commits

```
62e02f71  fix(arch): make private-boundary guard cover method access
69e736ec  chore(arch): remove dead architecture rule R5
ae134e07  ci: close two trust-boundary judgment holes and pin least privilege
6b4e19e2  test(ci): make the test-set gate refuse on broken expressions, and make Normal offline
51931e16  test(ci): exclude the gitignored tmp/ scratch area from pytest collection
e2003080  chore(lint): enable F401 and clear the production/test backlog
24e22a23  docs(architecture): record enforced system invariants with test/gate/CI mapping
2ca3a7e3  fix(tests): make the RUN_EXTERNAL_TESTS opt-in gate cover core/, not just Module/
2e9ca74e  docs(architecture): record the external opt-in scope fix in the false-assurance table
ec71d3c1  fix(ci): close three holes the Challenger found in this batch's own fixes
f81d3bbc  docs: correct stale test counts and record the Challenger's accepted risks
0da53486  docs(architecture): list the Challenger-confirmed accepted risks in the false-assurance table
```

**每个 commit 都在独立 worktree 里验证过**（`git worktree add` 到 `/tmp`，跑该 commit
涉及的门禁与测试，验证后移除）。

---

## 14. Test Results

| 项 | 结果 |
|---|---|
| `make test-quick` | **5949 passed, 2 skipped, 1 xfailed**（74.6s） |
| `make test-normal`（CI 档位） | **5949 passed, 2 skipped, 1 xfailed**（72.8s） |
| `make test-full`（含 slow） | **5981 passed, 20 skipped, 1 xfailed**（419.0s） |
| `make lint` | All checks passed（F401 现为 0） |
| `make pyright` | 0 errors, 0 warnings, 0 informations |
| `make check-arch` | 通过（R1 3 项豁免） |
| `make check-types` | 通过（含 17 golden frames） |
| `make check-contract` | 通过（23 预留 / 4 已知缺口） |
| `make check-timeouts` | 通过（R2 4 条 / R3 叶子 9 条 / R4） |
| `make check-debug-auth` | 通过（21 文件） |
| `make check-secrets` | 通过 |
| `make check-compose` | 通过（2 文件） |
| `make check-env-lock` | 通过（20 包） |
| `make check-ci-isolation` | 通过（3 workflow，6 清理点） |
| `make check-test-sets` | 通过（Quick 5952 / remainder 0 / Normal 5952 / 全量 6002） |
| `scripts/tests/` | **304 passed** |
| 全仓导入烟测 | 767+ 模块 0 失败 |
| 本地 vs 干净 worktree 收集数 | **逐字相同**（5952） |

### ⚠️ 真实 CI：**未验证**

**当前环境无法推送**——`git push --dry-run` 报
`could not read Username for 'https://github.com'`；本机无 `gh`、无 credential helper、
`~/.ssh` 下无私钥（只有 `authorized_keys` / `known_hosts`）。
本地领先 `origin/main` **12 个 commit**。

因此**本文不能宣称"真实 CI 最终绿"**。代码侧能做的替代验证已全部做到：
逐个 commit 在干净 worktree 里复跑、与本地 `git fetch --depth 1` 等价的收集面比对
（`norecursedirs` 修复后逐字相同）、以及 CI 所用的**同一条命令**
（`make test-normal` / `make test-lint` / 各 `check-*`）在干净树上的复跑。

推上去之后请确认：
```
make lint && make pyright && make check-types && make check-arch \
  && make check-timeouts && make check-contract && make check-debug-auth \
  && make check-ci-isolation && make check-secrets && make check-test-sets \
  && make check-compose && make check-env-lock && make audit && make test-normal
```
以及 `real-mongo` / `frontend` / `frames` 三个 job。

---

## 15. Challenger Findings

独立 Challenger 对 `4203198f..e2003080` 做对抗式复核（两个 `/tmp` worktree +
探针脚本），**在本批自己新加/改动的判据里**又找出三处问题：

| # | 分类 | 内容 |
|---|---|---|
| C-1 | **confirmed → fixed** | permissions 门禁接受 `permissions: write-all`（判据比声称的弱）→ 改为真判只读（含 `contents: write`、混合、显式 null） |
| C-2 | **confirmed → fixed** | `rm -rf "$GITHUB_WORKSPACE"/*`（裸变量）仍漏检 → 识别面扩到 4 种写法；端到端复现 0 违规 → 现为违规 |
| C-3 | **confirmed → fixed** | docstring 宣传不存在的 `--report` 开关——**删 R5 的同一次提交造了个同类的假开关** → 改正 + 加判据 |
| C-4 | **confirmed → accepted risk** | 私有守卫的**同名遮蔽**：访问方包内一行 `def _broadcast` 即让 9 处违规塌成 1 处。固有代价，守卫定位是回归网 |
| C-5 | **confirmed → accepted risk** | 守卫扫描面只含 `core`+`Module`；`scripts/`、`frontend_contract/` 不在内（有真实标注实例） |
| C-6 | **confirmed → accepted risk** | 清空识别长尾（`xargs`、`for` 循环、`git clean -ffdx`）；本仓 6 处清理点全用规范写法，无实例 |
| C-7 | **confirmed → accepted risk** | `if:` 守卫是 token 存在性而非布尔求值（`… \|\| true` 会被判受信） |
| C-8 | **confirmed → fixed** | `docs/testing.md` 计数整体漂移一轮 + `CLAUDE.md` 的 `integration` 条数（10 → 实为 7）与执行点说法有误导 |
| C-9 | **confirmed → fixed（本批已提前修）** | `RUN_EXTERNAL_TESTS` 门禁不覆盖 `core/`（Challenger 独立复现，与本批 `2ca3a7e3` 结论一致） |
| C-10 | **false positive（Challenger 自纠）** | 它最初怀疑"半个拼错的 `EXPR_QUICK` 会被放过"——实跑证明会被并集恒等式抓住（exit 1，2563 遗漏）；又报 `__all__` 有缺口，实为 gitignored `tmp/` 探针所致 |
| C-11 | **fixed** | `__init__.py` 里同一段 5 行注释被重复粘贴多次（两版还互相矛盾）→ 清理为单一版本 |

Challenger 自己也声明了两条初始攻击假设**被实跑证伪**（如实报告而非凑数）。

---

## 16. Keep As-Is

明确**保持现状**（不做改动，且记录了理由）：

- **`make test-full` / `external` 无自动执行点** —— `external` 是真实费用，刻意 opt-in；
  `slow` 层要覆盖需先有隔离 runner。见 invariants §7。
- **R4 扩到子包粒度** —— 2026-09-16 审计已裁定：第一红是伪环
  （`Module._common ⇄ Module.config`，由 `config_test_helpers.py` + `__main__.py` 构成）
  ⇒ 会立刻污染 allowlist，**反而削弱门禁**。本批不动 R4。
- **SLF001 不做计数 ratchet** —— 见 §10。
- **`scripts/print_test_stats.py` 的弱点**（不检查子进程返回码）—— 它是统计打印器、
  非门禁；修它不改变任何判据。
- **`_MODULE_TO_CORE_ALLOWLIST` 按文件 key** —— bench/documentdb CLI 两处刻意豁免，
  理由已逐项写清；"新增即绿"（该文件内新增 core 依赖不被拦）是已知取舍。
- **`real_mongo` 层不挂 `RUN_EXTERNAL_TESTS`** —— 它走容器、不需凭据，
  由该层自己的 fail-closed 守卫把关。
- **前端 job 的 Node 版本断言、compose 的 `down --remove-orphans`、nightly 的
  `suite` 输入设计** —— 审计确认正确，不动。

---

## 17. Follow-up

未做项已逐条登记进 **[todo.md](../../todo.md)**（不明说的"以后再说"等于没记）：

**P2**
- R4 的 eager 环判据有**可复现漏报**（包节点 `X` 与 `X.__init__` 分裂；相对导入整类跳过
  ⇒ 两个运行期真 ImportError 的硬环被判绿）。真实仓库当前 0 环。
- 私有守卫的**同名遮蔽**（C-4）。

**P3**
- R6 的"条数漂移"分支零测试覆盖（变异实测全绿）。
- 守卫扫描面不含 `scripts`/`frontend_contract`（C-5）。
- `real_mongo` 未列入 `_SLOW_LAYER` 之外的任何强制（本批已补进 `_SLOW_LAYER`）——
  剩余项：`pytest.ini` 未启用 `--strict-markers`。
- 清空识别长尾（C-6）、`if:` 守卫非布尔求值（C-7）。
- RUF100 未启用 ⇒ 863 处 `# noqa` 无门禁检查陈旧（含 38 处 F401 标注）。
- CI 未扫描 `env:` / `with:` / `working-directory:` / `container:` 里的宿主路径。
- `deepseek` 外部测试被移出 push 门禁后**只剩手工入口**（`nightly.yml` 是
  `workflow_dispatch`，文件名叫 nightly 但**没有 `schedule:`**）——若需定期覆盖，
  要么加 `schedule:`，要么接受手工。

---

## 18. 结论

本批**没有**让门禁变得更多，而是让已有的门禁**真的拦得住它自称拦的东西**：

- 5 处"判据比声称的弱"（4 假阴性 + 1 假红）+ 1 条死规则，全部修复并配变异验证的
  回归测试；**其中 3 处（permissions / `$GITHUB_WORKSPACE` / phantom flag）
  是本批自己的修复引入或遗漏的，由独立 Challenger 找出后修复**——这本身就是
  "对抗式复核有效"的证据。
- F401 从"从未启用"到"`__init__.py` 之外为 0"，且**没有盲删任何公开 re-export**
  （两次误删都被发现并恢复，并留下了可复用的检测手段）。
- SLF001 **没有机械清零**（709/750 在测试侧，属正常白盒），改为让生产侧跨包穿透有牙。
- CI 保持 push-only 并把安全模型写成文档 + 可执行门禁；`permissions` 现在真的是只读。
- 不变量从"散落在各批执行记录里的 prose"变成 **[可维护的 contract](../architecture/invariants.md)**，
  含 CI 覆盖实测与**假保障清单**。
- **未验证项只有一项：真实 CI**（本机无推送凭据，见 §14）。
