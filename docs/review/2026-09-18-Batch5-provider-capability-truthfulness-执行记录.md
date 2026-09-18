# Batch 5 — Provider Capability Truthfulness 执行记录

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文
>
> 前置：[Batch 4 Backlog Triage & Planning](2026-09-18-Batch4-backlog-triage.md) §10 备选 **B**
> （`N-10` + `F6` + `F7`，9+7+7 = 23 分）与 [Batch 3.5 正确性分诊](2026-09-17-Batch3.5-correctness-triage-执行记录.md) §12。
>
> Scope **冻结**为 canonical backlog 的三条：**N-10**（预检说可用 / 运行期必
> `NotImplementedError`）、**F6**（provider test 测的不是跑的那个）、**F7**
> （`/debug` 的 `tts_ok`/`stt_ok` 混用两种语义）。本批**没有**重新扫描全仓，
> 也**没有**碰 N-3 / N-1 / N-11 / GR-3 / GR-24 / N-15 / WAITING_APPROVAL /
> reminder persistence / task LLM provider H3 / workflow reporter /
> provider portability / prompt budget cross-path / ToolAdapter cleanup。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| **N-10?** | ✅ **CONFIRMED_DEPLOYMENT_REACHABLE**，**FIXED** —— `c70312de`。修前 E2 实测：预检 `13/13 passed, 0 failed` → `True`，而同一配置下 `LLMFactory.create` 与**启动路径** `LLMAdapter(model=…)` 都抛 `NotImplementedError` |
| **F6?** | ✅ **CONFIRMED_ADMIN_REACHABLE**，**FIXED** —— `7ef3202b`。修前 E2 实测：`runtime active = sensevoice/runtime-model`、`probe target = sensevoice/config-model`（两个实际值，§53） |
| **F7?** | ⚠️ **半 CONFIRMED / 半 FALSE_POSITIVE** —— **FIXED（确认的那半）** `87cc7e38`；**重分类（不成立的那半）**见 §8 |
| 多少 confirmed? | **3 条 finding 全部重新 triage**；其中 **2 条整体确认**（N-10、F6），**1 条部分确认**（F7：语义混用确认，前置假设「存在性冒充可用」不成立） |
| 多少 fixed? | **3 个 production commit**（N-10 / F6 / F7-确认半） |
| 多少 reclassified? | **1**（F7 的「`tts_ok` 存在性太弱」→ 不是缺陷；依据见 §8） |
| Public API changed? | **否**。无 wire/schema 变更：`DebugData` 字段逐字不变（`tts_ok`/`stt_ok`）、`/api/providers` 响应形状不变、`/api/providers/test` 请求/响应不变。`Agent.stt_ok` 保留（公开只读属性，改的是**取值来源**）。`make check-types` 无漂移 |
| Network behavior changed? | **否（普通路径零新增网络调用）**。`--check` 仍 network-free；`/debug` 无网络；provider test 行为不变（仍真发探活请求，本批只改"测谁"）。**未新增任何付费调用** |
| Persistence changed? | **否**。无集合/索引/字段变更，无迁移 |
| Production diff 规模 | **8 个生产文件**、`+329/−33` 行（含大量 docstring/注释）；实际行为改动 = 3 处判据替换 + 1 处判据新增 + 1 处槽位删除 |
| 回归网 | 新增 **41** 条（`test_preflight_provider_capability.py` 21 + `test_provider_test_target_authority.py` 10 + `test_service_availability_truthfulness.py` 9 + `test_provider_registry.py` 1）；其中 **24 条 PRE_FIX_RED**（19 条对 `3452d987` 红；5 条对 `c70312de` 红——Challenger 命中后补的守卫），其余 17 条是守卫 |
| Mutation | M1（capability 判据退回假绿）/ M2（F6 退回 config authority）/ M3（F7 退回粘性标志）/ M4（下拉排他退回 `models` 空判据）——**四条全部被杀**，见 §15 |
| Challenger | 独立子代理 + 独立 worktree（`/tmp/b5-challenger`），A–G 七个攻击 + 强制突变。**3 处命中并当场修/改口径**（A 的理由错误、F2、F3）；**3 处记为既有 out-of-scope 新 finding**（C2 残留、F1、G）；2 处记为接受风险；见 §14 |
| 真实 CI | 见 §21.7（push 后核验） |

**Challenger 之后的一句话**：本批最难的对手不是那三条 finding，而是**我自己写下的理由**。
独立子代理用反例推翻了其中一条（`ABC ⇒ 实现存在`，§14-A），又用两个"只改 registry、
忘了改第二张手抄表"的漂移场景证明修复还留着一扇后门（§14-F2/F3）——两者都已修/已改口径。
**报告里说得比事实强，和代码里报得比事实强，是同一种病。**

**一句话**：三条 finding 共享的不是一个"共同的 capability 框架"，而是**同一条链上
报告层各自猜了一次**：

```text
配置存在 → provider 已解析 → 对象已实例化 → 实现支持该操作 → 外部服务此刻可连通
   ↑            ↑                ↑                 ↑                  ↑
  config      resolver       runtime handle    registry/ABC      网络探测
```

N-10 的错在**第 4 层没人问**（预检只能看第 1 层：env 齐不齐）；F6 的错在**第 2/3 层
被跳过**（probe 直接读 config）；F7 的错在**第 3 层有两个答案**（一个句柄、一个粘性
标志）。修法因此是同一句话的三次落地：**每一层只由它的权威回答，报告层只投影**——
不为它新建 service、不为它扩 schema、更不让 `/debug` 开始联网。

---

## 2. Fixed Input Set（原始描述与 canonical 状态）

| ID | 原始描述（todo.md §1，Batch 4 triage 后） | Reach | Sev | 修前 Status |
|---|---|---|---|---|
| **N-10** | `gemini/<model>` 预检全绿，运行期 `LLMFactory.create` 无条件 `NotImplementedError`（LLM 侧无 stub 冻结表，STT 侧有 `_STT_STUB_PROVIDERS`） | DEPLOYMENT | MEDIUM | `OPEN` `TRUTHFULNESS` |
| **F6** | `/api/providers/test` 的 **STT 缺省模型读 config**，而 llm/tts 读 runtime ⇒ 测的不是跑的那个 | ADMIN | MEDIUM | `OPEN` `TRUTHFULNESS` |
| **F7** | `/debug` 的 `tts_ok = agent.tts is not None`（存在性），而 `stt_ok` 是真实连通标志 ⇒ 面板混用两种语义 | ADMIN | LOW | `OPEN` `TRUTHFULNESS` |

> 编号漂移提醒（见 todo.md §2）：Batch 3.5 报告 §12 里的 `N-9` 就是今天的 **N-10**；
> Batch 1.6 表格里的 `F5` 就是今天的 **F6**。本批一律用 canonical 编号。

---

## 3. Triage Matrix

| ID | Reproduced | Reachability | Evidence | Classification | Action |
|---|---:|---|---|---|---|
| **N-10** | ✅ 是（fresh subprocess，修前） | DEPLOYMENT（`python app.py --check` → `scripts/start.sh` → 启动期崩） | **E2**（子进程跑真 `preflight.run` + 真 `LLMFactory.create` + 真 `LLMAdapter` 构造） | `CONFIRMED_DEPLOYMENT_REACHABLE` | **FIXED** `c70312de` |
| **F6** | ✅ 是（真 Agent + 真 `switch_stt` + 真端点） | ADMIN（管理面"测试当前 STT"） | **E2**（两值实测：runtime `sensevoice/runtime-model` vs probe `sensevoice/config-model`；端到端观察工厂入参） | `CONFIRMED_ADMIN_REACHABLE` | **FIXED** `7ef3202b` |
| **F7** | ✅ 是（确认的那半）；❌ 否（重分类的那半） | ADMIN（`/debug` 面板 + WS capabilities 同时可见） | **E2**（真 `Agent` + 真 `switch_stt` 失败路径 + 真 `_collect_runtime_stats` + 真 `build_capabilities`，三面取值实测） | 确认半 `CONFIRMED_ADMIN_REACHABLE`；重分类半 `FALSE_POSITIVE` | **FIXED** `87cc7e38`（确认半）；重分类依据 §8 |

**没有**任何一条被标为 `ALREADY_FIXED` / `INSUFFICIENT_EVIDENCE`；F7 的**一半**被标为
`FALSE_POSITIVE`，理由必须写在 §8（而不是含糊地"部分修复"）。

---

## 4. Provider Capability Model（五层，各自的权威）

| 层 | 问题 | 权威（修后） | 修前状况 |
|---|---|---|---|
| **1. CONFIGURED** | 配置/env 里有这么一条吗 | `config.<svc>.model`（`ConfigLoader`：CLI > env > YAML > 默认）+ `Module/config/providers.yaml`（凭据/地址/模型清单） | 同（未改） |
| **2. RESOLVED** | 最终 provider/model 解析成什么 | 各 Module registry 的 `provider/model` 解析 + `core/providers/custom_providers.resolve_model_params` | 同（未改） |
| **3. INSTANTIATED** | 后端对象建出来了吗 | `agent.llm`（`LLMAdapter.model`）/ `agent.stt` / `agent.tts`（运行期句柄） | **F6 越过它**（STT probe 直读 config） |
| **4. SUPPORTED** | **本仓这份代码**实现了该 capability 吗 | 各 registry 的 `is_implemented()`，与 `create()` 的守卫**读同一常量** `_UNIMPLEMENTED_PROVIDERS` | **N-10：无人问**（LLM 侧根本没有这个概念） |
| **5. REACHABLE** | 远端此刻真的可用吗 | 只有 `--full`（`_check_*_connectivity`）与 `POST /api/providers/test` 能回答——它们需要网络 | 同（未改，且**刻意**不让普通路径获得它） |

**共享 authority 的实现形态（§35/§36：不抽公共 service）**：

```text
providers.yaml ─┐
                ├─→ Module/{llm,stt,tts}/registry.py ──┬─→ create()（建连守卫，行为权威）
env / config ───┘        _UNIMPLEMENTED_PROVIDERS      ├─→ is_implemented()（能力查询，报告权威）
                                                       │
                                                       ├─→ Module/config/preflight.py（--check / --full）
                                                       ├─→ core/providers/provider_registry.py（/api/providers）
                                                       └─→ core/input/provider_routes.py（provider test 目标）
运行期句柄 agent.{llm,stt,tts} ─┬─→ core/services/capabilities.py（available/model，WS 下发）
                                 ├─→ core/debug/info.py（tts_ok/stt_ok，/debug 面板）
                                 └─→ core/input/provider_routes.py（provider test 缺省目标）
```

`is_implemented` 与 `create()` 守卫**共用一个 `frozenset` 常量**——这是"判据与行为
不可能漂移"的机制，而不是一句承诺。

**三层语义边界（§12/§13，写进代码 docstring）**：`_UNIMPLEMENTED_PROVIDERS` 描述
**当前这份代码**（provider 级），**不**描述厂商产品能力，**不**描述 model 级能力
（model 级见 `supports_multimodal` / `context_window` / `providers.yaml::models` /
gpt-sovits 音色白名单）。声明在 `providers.yaml` 却不在集合中的 provider 一律按
"已实现"对待（**fail-open**，不误拒第三方/插件 provider）。

---

## 5. Pre-Fix Truth Mismatches

### 5.1 N-10（DEPLOYMENT）

```text
reported（python app.py --check）：
    13/13 passed, 0 warned, 0 failed — 通过        → exit 0 → scripts/start.sh 放行

actual runtime：
    LLMFactory.create("gemini/gemini-1.5-pro")
        → NotImplementedError: Gemini backend is not yet implemented
    Agent.__init__ → LLMAdapter(model=config.llm.model) → LLMFactory.create(...)
        → NotImplementedError（**构造期**，进程根本起不来）
```

**实测输出（修前，fresh subprocess，见 §21.3）**：

```text
=== config.llm.model = gemini/gemini-1.5-pro
  [PASS] LLM env ok (GEMINI_API_KEY 已设置)
  [PASS] LLM URL ok (GEMINI_URL 已设置 → https://generativelanguage.googleapis.com)
  ...
结果: 13/13 passed, 0 warned, 0 failed — 通过
=== preflight(full=False) -> True
=== runtime: LLMFactory.create('gemini/gemini-1.5-pro')
    RAISED NotImplementedError: Gemini backend is not yet implemented
=== runtime: LLMAdapter(model='gemini/gemini-1.5-pro')  —— 启动路径
    RAISED NotImplementedError: Gemini backend is not yet implemented
```

### 5.2 F6（ADMIN）

```text
config.stt.model      = sensevoice/config-model      ← bootstrap（A）
agent.stt.model       = sensevoice/runtime-model     ← 运行期在跑（B）
probe target [stt]    = sensevoice/config-model      ← 端点在测（A）✗
probe target [llm]    = agent.llm.model              ← 本来就对
probe target [tts]    = runtime，未连接才回退 config  ← 本来就对
```

端到端观察（修前，工厂真实入参）：`seen == ["sensevoice/config-model"]`，
而 `agent.stt.model == "sensevoice/runtime-model"`。

### 5.3 F7（ADMIN，确认的那半）

同一个受控状态（热切换失败，旧后端仍可用）下的三面取值：

```text
/debug  info.stt_ok                              = False   ✗
/debug  面板渲染（OverviewTab.vue:104）            = ✗       ✗
capabilities.services.stt.available              = True    ✓
真实能力（agent.stt is not None，能 transcribe）  = True    ✓
```

反方向也成立（停机窗口）：`_stop_llm_and_tts` 置 `_stt = None` 却**不复位** `_stt_ok`
⇒ `/debug` 显示 ✓ 而后端已经没了。

---

## 6. N-10 Analysis

**为什么 preflight 会 pass**：`_check_llm_env` 的判据全部落在**第 1 层（配置）**：
`_LLM_PROVIDER_ENV["gemini"] = "GEMINI_API_KEY"` 存在 + `resolve_base_url` 能从
`GEMINI_URL` 或 providers.yaml 解出地址 ⇒ 两条 `[PASS]`。它没有、也无法从自己的
静态表里知道"本仓实现了没有"——LLM 侧**根本没有这个概念**（STT 侧有一个
`_STT_STUB_PROVIDERS`，但那是 STT registry 内联元组的**第二份副本**）。

**为什么 runtime 会 NotImplemented**：`Module/llm/registry.py::create` 里
`if provider == "gemini": raise NotImplementedError(...)`——**权威在这里**，
而它不可查询（只能"撞上去"才知道）。

**canonical capability truth 在哪里（修后）**：
`Module/llm/registry.py::_UNIMPLEMENTED_PROVIDERS`（唯一常量）→ `create()` 守卫 +
`LLMFactory.is_implemented()`；报告层（preflight、`/api/providers`）只查询，不自建表。

**Root cause 分类（§8 的五选，可多选）**：

| 类别 | 是否命中 | 说明 |
|---|---|---|
| A. registry 宣称支持但 implementation 没实现 | **命中**（根因） | `providers.yaml` 声明 gemini（含 `env_key`/`url_key`），`_BACKENDS` 里有它，但工厂抛 |
| B. preflight 只看配置存在 | **命中**（放大器） | 无 capability 判据，只能退化成"env 齐不齐" |
| C. provider/model alias 解析到错误 implementation | 否 | 解析链本身是对的 |
| D. capability 与 provider 两个维度混淆 | **命中（同类）** | STT 有 stub 概念、LLM 没有；`/api/providers` 用"`models` 为空"这个**副产品**代理 capability |
| E. 基类默认 `raise NotImplementedError` 但没有 capability metadata | **命中（形态）** | LLM/STT registry 用内联 `if`/元组 + raise，**没有可查询的 metadata** |

**没有**用 `try/except NotImplementedError` 伪装修复（§9）：能力判定走
`is_implemented()` 的**静态**回答，任何时候都不需要"调用一次看看抛不抛"。

---

## 7. F6 Analysis

**probe 测谁（修前）**：`core/input/provider_routes.py::_resolve_test_model`——
body 不带 `provider`/`model` 时按 service 取"当前"：

```python
if service == "llm":
    model_id = agent.llm.model                                  # 运行期 ✓
elif service == "tts":
    model_id = tts.model if tts is not None else config.tts.model  # 运行期 ✓
else:
    model_id = agent.config.stt.model                           # 配置 ✗
```

**runtime 用谁**：`agent.stt.model` —— `STTBackend.model` 由 `provider_name` +
`_model` 派生的 `provider/model`（`Module/stt/protocol.py`）。它同时是
`capabilities.services.stt.model`、启动横幅、`_custom_profile_in_use` 的权威。

**为什么发生分叉**：`switch_stt` 成功时**有意只写 `ServiceState.stt_model`、不改
config**（config 是"默认"，ServiceState 是"当前"，保住"恢复默认"语义）。
于是 bootstrap=A → switch_stt(B) 之后，`config.stt.model` 与 `agent.stt.model`
**长期不同**，而端点读的是前者。同一文件里的 `_custom_profile_in_use` 早就用
运行期值（"以运行期值为第一真相源"，注释原文），**本文件内部自相矛盾**。

**修法为什么最小**：只把 stt 分支换成与 tts 分支**同一个 helper**
（`_runtime_model_or_default`：句柄不存在或自述模型为空才回退 config），
不复制字符串、不改端点副作用、不改 wire。`llm`/`tts` 两个分支的判据保持不变。

---

## 8. F7 Analysis

### 8.1 字段真正承诺什么（证据）

| 来源 | 原文 | 读法 |
|---|---|---|
| `frontend_contract/debug.py:238-239` | `tts_ok: bool = Field(description="TTS 后端可用")` / `stt_ok` 同 | **只有两个字："可用"**，从未被定义为 object-exists / configured / initialized / supported / reachable / last-probe |
| 生成产物（ts / raw.ts / schema.json） | 逐字继承同一句描述 | 同上 |
| `frontend/src/debug/components/OverviewTab.vue:103-104` | `<span :class="d.tts_ok ? 'ok' : 'fail'">{{ d.tts_ok ? '✓' : '✗' }}</span>` | 裸 ✓/✗，**不含"在线/可达"字样** |
| `frontend_contract/ws_message/_types.py:255` | `available: bool = Field(description="后端连接可用")` | capabilities 的**产品契约**字段 |
| `core/services/capabilities.py:55-58/112-115` | `available = agent.stt is not None` / `agent.tts is not None` | **产品对"可用"的既有判据就是运行期句柄** |
| `docs/` 全量检索 | 没有任何 spec/plan 裁定 `_ok` 是 health 还是 enabled（Batch 1.6 `:438` 与 `tmp/batch16` 各自记录"没有任何文档裁定"） | 语义**悬空**，只能按既有契约（capabilities）倒推 |

结论：**既有契约把"可用"定义为"运行期后端句柄存在"**，而不是"远端此刻可达"
（后者需要网络，且 §26/§74 明确禁止 `/debug` 主动探活、禁止新增健康循环）。

### 8.2 确认的那半：一个事实两个权威

`tts_ok = agent.tts is not None`（运行期句柄），`stt_ok = agent.stt_ok` → 粘性槽位
`_stt_ok`（只在 `__init__` / bootstrap / `switch_stt` 写入）。两者在**同一个受控状态**下
给出相反答案（§5.3）：

- `switch_stt` 失败 → `_stt_ok = False`，但**失败路径有意保留仍然可用的旧后端**
  （先建后关）⇒ `/debug` 说"STT 不可用"，而 STT 还能转写、capabilities 说 available。
- 停机 → `_stt = None` 但 `_stt_ok` 不复位 ⇒ `/debug` 说"可用"，而后端已经没了。

**修法**：让报告读同一个权威（运行期句柄），**删掉**这个缓存投影（`_stt_ok` + 5 个
写入点），`Agent.stt_ok` 变成派生属性。字段名、schema、`/debug` 读取点、`tts_ok`
判据全部不动。

### 8.3 重分类的那半：「`tts_ok` 只是对象存在，不代表可用」

**不是缺陷（`FALSE_POSITIVE`）**，依据三条，且都是可执行的：

1. **"对象存在 ⇒ 实现支持"在本仓是结构性成立的**，不是巧合：所有工厂对未实现的
   provider 在**构造期**拒绝（N-10 起由 `is_implemented` 与守卫共用一个常量），
   STT/TTS 后端又都是 `ABC`（抽象方法没实现根本实例化不出来）。
   这条依据被 `TestInstantiatedImpliesImplemented` 钉住——遍历 `providers.yaml`
   断言 `is_implemented ⇔ create() 抛 NotImplementedError`，**削弱它就会红**。
2. **"远端此刻可达"不属于进程内状态**，`/debug` 拿不到也不该去拿：§26 列了四条
   理由（昂贵、有副作用、可能计费、污染状态/拖慢端点），§74 禁止后台健康循环。
3. **`available` 的既有产品判据就是句柄存在**（`capabilities.py`，下发给前端并驱动
   麦克风门控 `InputBar.vue:32`、设置面板"（未连接）"徽标）。把 `/debug` 改成更强的
   语义，会让它与 capabilities **重新分叉**——正是本批要消除的病。

因此 §32 推荐的默认语义（`*_ok = active runtime handle exists AND current
implementation supports required capability`）在本仓落地后**前半即后半**：

```text
*_ok = 运行期句柄存在  =  runtime usable by construction
     ≠  远端 provider 此刻可达（无进程内权威，不猜）
```

**没有**做 M3 那句"把 `_ok` 改回 `obj is not None`"式的突变测试——因为修复方向恰恰
是**让两者同源**；对应的突变是"把 `stt_ok` 改回粘性标志"（M3，见 §15）。

**留下的产品决策（记录，不扩 schema）**：`_ok` **不区分"关掉了"与"坏了"**
（`set_service('tts', False)` 之后后端仍在槽位里 ⇒ 面板仍 ✓）。修它需要给
`DebugData` 增 `stt/tts` 的 `model`/`enabled` 字段（**OBS-5** 已单独跟踪），
属 schema 扩展，本批按 §28/§81 明确不做。

---

## 9. Authority Map

| Report | Before Authority | After Authority |
|---|---|---|
| `--check` 的 LLM capability 结论 | **无**（只有 env 齐不齐） | `LLMFactory.is_implemented()`（← `_UNIMPLEMENTED_PROVIDERS`，与 `create()` 守卫同常量） |
| `--check` 的 STT capability 结论 | preflight 自带的 `_STT_STUB_PROVIDERS`（registry 内联元组的副本） | `STTFactory.is_implemented()`（同一常量，副本已删） |
| `--full` 的 unsupported 结论 | STT 报 `[PASS] 跳过`；LLM 报 `连通异常` | 都报"未实现"（STT WARN / LLM WARN，且不再伪装成网络错误） |
| `/api/providers` 下拉排他 | `not cfg.get("models")`（**副产品**，恰好等于未实现） | `_provider_is_implemented(service, name)` → 各 registry `is_implemented()` |
| provider test 的缺省 provider/model（stt） | `agent.config.stt.model` | `agent.stt.model`（运行期句柄），未连接才回退 config |
| provider test 的缺省 provider/model（llm/tts） | 运行期（本来就对） | 同（未改）；tts 与 stt 现在共用同一 helper |
| `/debug` 的 `stt_ok` | 粘性槽位 `_stt_ok` | `agent.stt is not None`（运行期句柄） |
| `/debug` 的 `tts_ok` | `agent.tts is not None`（本来就对） | 同（未改） |
| `Agent.stt_ok`（公开只读属性） | `self._stt_ok` | `self._stt is not None` |
| capabilities `services.*.available` | `agent.<svc> is not None`（本来就对） | 同（未改）——现在 `/debug` 与它同源 |

**"一个事实一个权威"的对照**：修前 `stt 可用吗` 有三份答案（`_stt_ok`、`_stt is not
None`、`_stt_enabled`）；修后"可用"只剩**一份**（运行期句柄），"启用"仍是
`stt_enabled`（三态派生，语义不同，**没有**合并——`enabled` 与 `available` 是两个问题）。

---

## 10. Fixes

### 10.1 `c70312de` — fix(config): reject unsupported runtime provider capability（N-10）

- **before**：LLM 侧无 capability 概念；STT 侧有一份副本表；`--full` 对 unsupported
  `[PASS] 跳过`；`/api/providers` 靠"`models` 为空"排他。
- **after**：三个 registry 各增 `_UNIMPLEMENTED_PROVIDERS` + `is_implemented()`（与
  `create()` 守卫同常量）；preflight 删副本表、新增 LLM capability 门（FAIL）、
  `--full` 不再报 PASS/不再伪装网络错误；下拉按 capability 排他；
  `_check_pip_deps()` 提前（新 import 的失败模式保持可读）。
- **为什么最小**：改动集中在"判定来源"这一处；没有新框架、没有新字段、没有新网络
  调用；`_UNIMPLEMENTED_PROVIDERS` 只是把**已经存在**的两处内联知识收成一个常量。
- 规模：7 文件、`+576/−13`（其中 357 行是新增测试）。

### 10.2 `7ef3202b` — fix(stt): test the active runtime provider（F6）

- **before**：stt 分支读 `agent.config.stt.model`。
- **after**：读 `agent.stt.model`，与 tts 分支共用 `_runtime_model_or_default()`
  （句柄不存在或自述模型为空才回退 config）。
- **为什么最小**：一个分支 + 一个 3 行 helper；不改端点副作用、不改 wire、不改
  测试功能本身（§37）。
- 规模：2 文件、`+245/−5`（其中 223 行是新增测试）。

### 10.2b `b3862338` — fix(config): gate provider capability before the credential env table（Challenger F2/F3/C2）

- **before**：capability 门排在 `_LLM_PROVIDER_ENV` / `_STT_PROVIDER_ENV` 的 early-return
  **之后**（那张表一漂移，假绿就回来）；TTS 报告层**完全**不问 capability；
  静态预检 import `Module.llm` 在缺 SDK 时抛裸 `ImportError`。
- **after**：三态 `_capability_verdict()` 三个子系统共用，判据只看 `providers.yaml` +
  `_UNIMPLEMENTED_PROVIDERS`；三个 `_check_*_connectivity` 统一（`unknown` /
  `unimplemented` 分开报）；capability 查询对 SDK 缺失 fail-open（理由写进 docstring：
  总判定由 `_check_pip_deps` 兜住），并**回退**了 `c70312de` 里那次买不到承诺的
  `_check_pip_deps()` 提前。
- **为什么最小**：只增 2 个 helper + 3 处调用顺序调整；`_*_PROVIDER_ENV` 表本身保留
  （它仍提供"凭据 env 映射"，只是**不再**兼任 capability 判据）。

### 10.2c `ac3005d0` — test(stt): pin probe-target order-insensitivity（§63）

F6 的结论在 single / A→B / B→A / 交错失败四种顺序下都必须成立（2 条，对 `3452d987` 红）。

### 10.3 `87cc7e38` — fix(debug): report STT availability from the runtime handle（F7）

- **before**：`Agent.stt_ok` 读粘性槽位 `_stt_ok`（6 个写入/初始化点）。
- **after**：派生属性 `self._stt is not None`；槽位与全部写入点**删除**；
  `core/debug/info.py` 一行不改（它本来就读 `agent.stt_ok`）。
- **为什么最小**：删掉的是一个**缓存投影**，不是任何信息；字段名/wire/面板/契约
  全部不动；`tts_ok` 判据不动。
- 规模：7 文件、`+320/−15`（其中 279 行是新增测试）。

### 10.4 `70425fdd` — docs(debug): correct F7's availability justification（Challenger A）

- **before**：docstring 用「后端都是 ABC ⇒ 抽象方法没实现就实例化不出来」论证
  "句柄存在 ⇒ 实现支持"。
- **after**：改成两条**可核查**的事实（工厂守卫 + 挂载期 1s 探测），并显式写下
  "不要再用 ABC 论证"与反例；新增 `test_mount_paths_require_a_passing_live_probe`
  钉住第二条依据。
- **为什么最小**：只改理由与一处测试，不改任何行为。

---

## 11. Static vs Connectivity Boundary

| 入口 | 回答什么 | 是否联网 | 判据 |
|---|---|---|---|
| `python app.py --check` | 配置 + resolver + **capability 静态有效性** | **否**（socket 哨兵守卫） | Python/`.env`/pip/角色文件/嵌入模型/Mongo URI/Milvus URI + **LLM capability**（FAIL）+ TTS/STT 地址与凭据（WARN） |
| `python app.py --check --full` | 上述 + **外部依赖此刻可连通** | **是**（真实探活） | LLM/TTS/STT `backend.test()`、Milvus/Mongo 真连；**unsupported 不再被跳过成 PASS**，且不伪装成网络错误 |
| `GET /debug/api` | 运行期**已知**状态 | **否**（§26） | `tts_ok`/`stt_ok` = 运行期句柄存在；**不**做任何探测 |
| WS `capabilities` / `server_info` | 当前服务能力 | **否** | `available` = 运行期句柄存在；`enabled` = 三态派生开关 |
| `POST /api/providers/test`（`connectivity`） | 指定/缺省 provider 此刻可达吗 | **是**（5s 超时） | `backend.test()`；缺省目标 = **运行期句柄** |
| `POST /api/providers/test`（`usable`） | 真实产出一份结果 | **是**（30s 超时，可能计费） | LLM 发文本 / TTS 合成音频 / STT 转写样例 |

**边界测试**（§15 要求钉住）：

- `test_supported_provider_with_unreachable_endpoint_still_passes_check`：实现支持 +
  端点黑洞（`127.0.0.1:1`）⇒ `--check` 仍 PASS。
- `test_no_network_calls_in_static_checks`：静态检查期间 `socket.connect` /
  `socket.create_connection` 被调用即失败。
- `test_reporting_does_no_network_io`：`/debug` 投影 + capabilities 构建期间同上。
- `test_unimplemented_stt_provider_is_not_reported_pass`：`--full` + unsupported ⇒
  WARN，**不是** PASS，且 `run()` 的整体结论不被它抬绿。

---

## 12. Regression Tests

| 类别 | 用例 | 文件 |
|---|---|---|
| **PRE_FIX_RED（对 `3452d987`）19 条** | N10-1 ×2、N10-2 ×3、N10-5 ×2、N10-6 ×2、下拉 capability 排他 ×1、F6-1/F6-2/F6-3/F6-5 ×5、F6-6（顺序扰动）×2、F7-B ×2 | `test_preflight_provider_capability.py`、`test_provider_registry.py`、`test_provider_test_target_authority.py`、`test_service_availability_truthfulness.py` |
| **PRE_FIX_RED（对 `c70312de`，Challenger 命中后补）5 条** | N10-7：LLM/STT 的"只改 registry、漏改 env 表"漂移 ×2、TTS 报告层必须问 registry ×1、TTS `--full` 不得伪装连通性 ×1、unknown/unimplemented 不得互相冒充 ×1（+ `missing_sdk` 守卫 ×1） | `test_preflight_provider_capability.py` |
| **ALREADY_GREEN_GUARD**（12 条） | N10-3 ×3（deepseek / ollama / sensevoice 无凭据）、F6-4 ×2（未连接回退 config）、F6 llm/tts 分支 ×1、F7-3 ×2、`TestInstantiatedImpliesImplemented` ×3（工厂守卫 + 遍历 providers.yaml + 挂载期探测）、`missing_sdk` 在 `3452d987` 上本就绿 ×1 | 同上 |
| **PRODUCT_CONTRACT_GUARD**（5 条） | N10-4 ×2（network-free 边界）、F7 网络哨兵 ×1、`tts_ok` 判据守卫 ×1、`test_successful_switch_reports_available` ×1 | 同上 |

新测试**没有**把已有绿用例当成修复证据：上表 **24 条 PRE_FIX_RED** 全部在独立 worktree
的对应**修复前**提交上实跑为红（§20），其余 17 条明确标为守卫。

---

## 13. Hot-switch / Runtime Consistency

STT **支持**热切换（`Agent.switch_stt`），故按 §19/§57 给出结果：

| 场景 | 结果 |
|---|---|
| bootstrap=A → `switch_stt(B)` → provider test 缺省目标 | ✅ 跟 **B**（`test_after_switch_stt_probe_targets_the_new_backend`，真实生命周期 + 真 1s 连通性探测） |
| 同上 → capabilities 报的 stt model | ✅ 与 probe 目标同源（`test_switch_then_probe_target_matches_capabilities_model`） |
| 同上 → `/debug` 的 `stt_ok` | ✅ True（句柄存在），与 capabilities `available` 一致 |
| `switch_stt` **失败** → 旧后端保留 | ✅ `/debug` = capabilities = 真实能力 = True（三面同答案）；修前 `/debug` = False（矛盾） |
| 同 provider、不同 model | ✅ probe 跟运行期 model（`test_stt_probe_targets_runtime_model_within_same_provider`） |

**Restart consistency（§58）**：provider/model 只在 startup 解析的场景由 fresh
subprocess 覆盖一次（§21.3 的 N-10 验证脚本从零进程跑 `preflight.run` + 工厂 +
`LLMAdapter` 构造）。**Config precedence（§59）未改**：`_resolve_test_model` 只在
"body 完全不带 provider/model"时生效，显式传参路径一字未动。

---

## 14. Challenger Findings

独立子代理（自建独立 worktree `/tmp/b5-challenger`，**不接触主工作区**），七个攻击 A–G
+ 强制突变 M1–M4。它的定位是**证伪**，不是确认，故下表按"它推翻了什么"来记。

| 攻击 | 结论 | 本批处置 |
|---|---|---|
| **A** 对象存在 / 方法存在 / 但实现必抛 `NotImplementedError` | **survived（结论成立），但我的理由被推翻** | ✅ **改口径** `70425fdd`：ABC 只保证方法**存在**不保证**有实现**；真实依据改为"工厂守卫 + 挂载期 1s 探测"，并新增守卫用例 |
| **B** config provider A / runtime provider B | **survived** | 无（`/api/providers/test` 三服务、`/debug`、capabilities、banner、`_custom_profile_in_use` 全部跟运行期；`config.<svc>.model` 的其余读者逐个分类为合法"默认值"用法） |
| **C** 静态预检 network-free | **partial**：C1（离线仍 PASS + 零网络系统调用）✅ survived；**C2 被推翻** | ✅ **修** `b3862338`（capability 查询对缺 SDK fail-open）＋**记录**残留（`_check_embedding_model` 自身的既有崩溃，见 §25 DEP-9） |
| **D** 未实现 vs 远端可达 | **survived** | 无（`--full` 不再 PASS/不再伪装网络错误；反向陷阱 = 本地 OpenAI 兼容桩实测 `[PASS] LLM 连通 ok`，无假阴性） |
| **E** 句柄存在但凭据缺失 | **survived** | 无（报告层不读凭据；两条装配路径都拒绝建不出/探不通的后端 ⇒ "句柄存在"实际比 docstring 原论证更强） |
| **F** supported provider 不被误拒 | **partial**：全量扫描无假阴性；但发现 1 个假 WARN + 2 个潜在洞 | ✅ **修**潜在洞（F2/F3，`b3862338` + 5 条守卫）；**记录**假 WARN（F1，见 §25 DEP-7） |
| **G** provider 级 vs model 级 capability | **survived**（`is_implemented` 的 provider 级语义正确）；发现 1 个被误导的消费者 | **记录**（model 级假绿，见 §25 DEP-8） |
| **H** 突变 M1–M4 | **四条全部被杀** | 见 §15 |

**逐条要点（保留证据强度差别）**

- **A1（fake-green 可复现）**：用**打补丁的工厂**塞一个每个抽象方法都 `raise
  NotImplementedError` 的替身，`switch_stt` 返回 `{"ok": True}`、`stt_ok=True`、
  capabilities `available=true`，而 `agent.stt_transcribe()` 抛异常。
  **接受风险**：工厂就是那道门——绕过它等于绕过整条不变量。生产路径无任何动态
  `setattr(agent, "_stt", …)`（Challenger AST 扫描确认），两条装配路径还各有一道
  1s 实探。这条边界已写进 docstring。
- **A3（反例）**：`STTBackend` 子类把抽象方法实现成 `raise NotImplementedError` 时
  `__abstractmethods__ == frozenset()`，**实例化成功**。→ 我的原论证是 non-sequitur。
- **C2（裸 ImportError）**：`c70312de` 引入的 `from Module.llm import LLMFactory` 让
  `run()` 在缺 openai SDK 时以裸 ImportError 死掉（没有 `结果:` 汇总）。已修；
  **残留**（`_check_embedding_model` → `Module.embedding` → openai）是**既有**行为，
  在 `3452d987` 上逐字复现 ⇒ 记为 DEP-9，本批不修（§2 不扩 scope）。
- **F2/F3（潜在洞，实测可复现）**：见 §10.2b。这是本批**最重要的一次攻击**——
  前一个 commit 让"capability 只有一处权威"对**文案**成立，对**控制流**还不成立。
- **F1（假 WARN）**：`sensevoice` 报 `[WARN] … 将不可用` 而工厂建得出来（代码内置
  `DEFAULT_BASE_URL`，`providers.yaml` 的 `base_url` 是空串）⇒ DEP-7。
- **G（model 级假绿）**：`gpt-sovits/<不存在的音色>` 预检 `[PASS] TTS 可用`，
  而 `_start_tts` 对该 `ValueError` **fail-fast** ⇒ `--check` 绿 + 启动崩，
  与 N-10 同形但在 model 轴上 ⇒ DEP-8。
- **被 Challenger 点名的"类别互相冒充"**：同一次运行里 `_check_llm_env` 说"未知 provider"
  而 `_check_llm_connectivity` 说"未实现"。已修（三态统一）。
- **它明确说测不到的**（原文照录）：真实 HTTP surface（只驱动到 handler 的第一步函数，
  未起 aiohttp TestClient）；无真实云 provider（只用本地回环桩）；未跑全量套件；
  desktop/Electron 的 `available=false, enabled=true` 是代码阅读 + 修复前 diff 推断；
  M3 是它对粘性标志的**重建**而非逐字回退；A2 的不可达性依赖 AST 扫描（未发现动态
  `setattr`，但插件理论上可以）。

---

## 15. Mutation Evidence

**主 Agent 自己的突变**（在每个 production commit 的**修复前**提交上做，见 §20）：

| 突变 | 内容 | 期望 | 实测 |
|---|---|---|---|
| M1 | `LLMFactory.is_implemented` 恒 `True`（capability 判据退回假绿） | 新测试红 | ❌ **红** |
| M2 | `_resolve_test_model` 的 STT 分支退回 `agent.config.stt.model` | 顺序/一致性测试红 | ❌ **红** |
| M3 | `Agent.stt_ok` 退回粘性标志 | F7-B 两条红 | ❌ **红** |
| M4 | `_provider_is_implemented` 恒 `True`（下拉排他退回 `models` 空判据） | 下拉 capability 用例红 | ❌ **红** |

**Challenger 独立重跑（自己的变异器 `_challenger/mutate.py`，带唯一锚点断言，
改完必回退并复验绿）**：

| 突变 | 被杀情况 |
|---|---|
| **M1** `LLMFactory.is_implemented → return True` | `test_preflight_provider_capability.py` → **4 failed, 11 passed**（gemini 不再 usable 的两条、registry 判据一致、`--full` 不伪装网络错误） |
| **M2** STT 分支退回 config | `test_provider_test_target_authority.py` → **5 failed, 3 passed** |
| **M3** 重建粘性标志（失败路径写、`stt_ok` 读） | `test_service_availability_truthfulness.py` → **2 failed, 6 passed**（恰好是两条 PRE_FIX_RED） |
| **M4** `_provider_is_implemented → return True` | `test_provider_registry.py` → **1 failed, 8 passed**（恰好 `test_unimplemented_provider_excluded_even_with_models`） |

回退后四个文件 **40 passed**，工作区干净（除它的 scratch）。

**§72 要求的 M3 形态说明**：F7 的修复方向是"让两个字段同源"，所以"把 `_ok` 改回
`obj is not None`"不是有效的突变（那正是修后的实现）。有效的突变是"把 `stt_ok` 改回
**粘性标志**"——即恢复被删掉的那套并行权威。两条独立变异（主 Agent 与 Challenger）
都确认它能被杀。

---

## 16. Public API / Schema Impact

| 面 | 变更 |
|---|---|
| `DebugData`（`frontend_contract/debug.py`） | **无**。`tts_ok`/`stt_ok` 字段名、类型、默认值、描述逐字不变 |
| 生成产物（`contract.generated.ts` / `contract.schema.json` / `contract.raw.ts`） | **无漂移**（`make check-types` 通过；未跑 `make gen-types`，因为没有 wire 变更——§82） |
| WS 消息（`capabilities` / `server_info`） | **无**。沿用既有字段与判据 |
| `GET /api/providers` | 形状不变（`llm`/`stt`/`tts`/`custom`/`unconfigured` + `ProviderEntry` 字段不变）；**值更真实**（未实现 provider 不再出现） |
| `POST /api/providers/test` | 请求/响应不变；**缺省目标**更真实 |
| `python app.py --check` 输出 | 文案有变（新增 capability FAIL 行、`--full` 的 STT 从 `[PASS]` 改 `[WARN]`），**退出码语义不变**（`failed == 0`） |
| `Agent.stt_ok` | 公开只读属性**保留**（名/类型/语义不变），取值来源改为运行期句柄；`_stt_ok` 私有槽位删除 |
| `LLMFactory` / `STTFactory` / `TTSFactory` | **新增** classmethod `is_implemented(model_id)`（纯新增，无签名变更） |

---

## 17. Network / Billing Impact

| 问题 | 答案 |
|---|---|
| 普通 `--check` 是否新增网络调用？ | **否**。新增的 `is_implemented()` 只查内存中的 `_UNIMPLEMENTED_PROVIDERS`；socket 哨兵用例钉住 |
| `/debug` 是否新增网络调用？ | **否**。改动只是换了一个判据来源（句柄）；哨兵用例钉住 |
| `/api/providers` 是否新增网络调用？ | **否** |
| 是否新增付费调用？ | **否**。provider test 的副作用面一字未改（仍按调用方 `level` 决定，`usable` 级本来就可能计费——**保持现状**，§37/§76） |
| `--full` 是否新增付费调用？ | **否**。反而**减少**了一次无意义尝试：unsupported provider 不再走 `LLMFactory.create` |
| 是否有新的后台探活/健康循环？ | **否**（§74 明确禁止） |

---

## 18. Persistence Impact

**无**。无新集合/索引/字段，无迁移，无 `ensure_index` 变更。
`ServiceState.stt_model` 的写入时机与内容一字未改（`switch_stt` 仍只写它、不写 config）。
capability 状态**不持久化**（`_UNIMPLEMENTED_PROVIDERS` 是代码常量）。

---

## 19. Commits

| hash | message |
|---|---|
| `c70312de` | `fix(config): reject unsupported runtime provider capability` |
| `7ef3202b` | `fix(stt): test the active runtime provider` |
| `87cc7e38` | `fix(debug): report STT availability from the runtime handle` |
| `ac3005d0` | `test(stt): pin probe-target order-insensitivity`（§63 顺序扰动，同属 F6 回归网） |
| `b3862338` | `fix(config): gate provider capability before the credential env table`（Challenger F2/F3/C2） |
| `70425fdd` | `docs(debug): correct F7's availability justification`（Challenger A） |
| （本 commit） | `docs(architecture): record provider capability truth invariants`（本文 + invariants.md §3.2 + todo）——刻意不写自身 hash，避免 amend 后自指失效 |

**刻意没有**制造第四个 production commit：F7 的重分类半不是缺陷（§8.3），
按 §85 的要求不写"fix"。

---

## 20. Worktree Verification

每个 production commit 都在**独立 worktree** 上验证"修复前 + 新测试 → 红；修复后 → 绿"
（§87）：

| worktree | 基线 | 放入 | 结果 |
|---|---|---|---|
| `/tmp/b5-w0` | `3452d987`（纯 HEAD） | — | 对照：`test_minimax_key_present_passes` **也红** ⇒ 该失败是 GR-2 已知的环境依赖（`MINIMAX_TTS_URL` 来自未跟踪的 `.env`），与本批无关；放入 F6 最终测试文件后 **7 failed / 3 passed**（含两条顺序扰动用例） |
| `/tmp/b5-w1` | `3452d987` | commit 1 的新测试 | ❌ 红：`test_preflight_provider_capability.py` **9 failed / 6 passed**；`test_provider_registry.py::test_unimplemented_provider_excluded_even_with_models` **1 failed**（实测 gemini 带 `credentialConfigured: true` 出现在下拉里）→ 放入生产改动后 **362 passed** |
| `/tmp/b5-w2` | `c70312de` | commit 2 的新测试 | ❌ 红：**5 failed / 3 passed**（含端到端 `seen == ["sensevoice/config-model"]`）→ 放入生产改动后 **32 passed** |
| `/tmp/b5-w3` | `7ef3202b` | commit 3 的新测试 | ❌ 红：**2 failed / 6 passed** → 放入生产改动后 **136 passed** |
| `/tmp/b5-w2`（复用） | `c70312de` | N10-7 的 5 条守卫 | ❌ 红：**5 failed / 16 deselected**（"只改 registry、漏改 env 表"的漂移当场复现假绿；TTS 占位被报成 `[PASS] TTS 可用`）→ 放入生产改动后 **21 passed** |
| `/tmp/b5-challenger` | `87cc7e38` | Challenger 独立攻击 + 独立变异 | 见 §14 / §15 |

---

## 21. Test Results

### 21.1 Targeted

```text
Module/config/tests/ Module/llm/tests/ Module/stt/tests/ Module/tts/tests/
core/input/tests/ core/agent/tests/ core/debug/           → 全绿（见 §21.4/§21.5 汇总）
```

### 21.2 Order perturbation（§63）

已**入库**为正式用例（`TestProbeTargetIsOrderInsensitive`，2 条，对 `3452d987` 红），
不是一次性探针：

| 顺序 | 期望 | 实测 |
|---|---|---|
| single（只切 A） | probe = A | ✅ |
| A→B | probe = B | ✅ |
| B→A（反向） | probe = A（不是"只跟最后一个不同的值"） | ✅ |
| 交错失败（成功 A → **失败** B） | probe 仍 = A（不漂回 config、不漂到失败模型），且 `stt_ok` 与句柄同源 | ✅ |

预检侧的"顺序"由 N10-6 / N10-7 覆盖：每个 provider 的判定在逐项重置 `_COUNTS` 后
互不影响，且判据不依赖 `_*_PROVIDER_ENV` 的遍历/命中顺序。

### 21.3 Fresh subprocess（§62）

`tmp/batch5/verify_n10_subprocess.py`（**本地一次性脚本，未入库**——`tmp/` 在 `.gitignore` 里；
它只做 `AppConfig()` + 改 `llm.model` + 跑 `preflight.run` + 调工厂，输出如下）：

```text
### A) gemini 配置（key+URL 齐全）
  [FAIL] LLM provider 'gemini' 在本仓尚未实现 — 运行期 LLMFactory.create('gemini/gemini-1.5-pro')
         会抛 NotImplementedError，进程将在启动期失败。请改用已实现的 provider，…
结果: 11/12 passed, 0 warned, 1 failed — 存在失败项
    preflight(full=False) -> False
    runtime create        -> NotImplementedError

### B) deepseek 配置（合法，端点 http://127.0.0.1:1 不可达）
  [PASS] LLM env ok (DEEPSEEK_API_KEY 已设置)
  [PASS] LLM URL ok (DEEPSEEK_URL 已设置 → http://127.0.0.1:1)
结果: 13/13 passed, 0 warned, 0 failed — 通过
    preflight(full=False) -> True （静态预检不得因离线变红）
```

两端**首次一致**：静态预检的结论与运行期真实行为同向；连通性不参与静态结论。

### 21.4 Quick / Normal

```text
make test-quick   → 6047 passed, 2 skipped, 1 xfailed   （Normal 与之同集，见 docs/testing.md）
make test-normal  → 6047 passed, 2 skipped, 1 xfailed in 76.49s
make check-test-sets → |Quick ∪ remainder| = 6050 == |Normal|；全量收集 = 6104
```

### 21.5 Full

```text
make test-full    → 6079 passed, 24 skipped, 1 xfailed in 441.41s (0:07:21)
```

### 21.6 Static gates

```text
make lint              → All checks passed!
make pyright           → 0 errors, 0 warnings, 0 informations
make check-arch        → 架构边界检查通过（R1/R2/R3/R4/R6）
make check-contract    → 契约可达性门禁通过（23 个预留 / 4 个已知缺口在册）
make check-timeouts    → 超时预算门禁通过（R1–R4）
make check-types       → 无漂移（ts / schema / raw_ts / 17 golden frames / 54 错误码单源）
make check-test-sets   → Quick ∪ remainder == Normal 且交集为空
make check-ci-isolation→ 三个 workflow 全部通过
```

### 21.7 Real CI

⚠️ **未完成（本机无 GitHub 凭据）**：`git fetch origin main` →
`fatal: could not read Username for 'https://github.com'`（与 Batch 3.5 / 3.6 / 4 同因，
本机没有可用的 GitHub 凭据）。故**无法从这台机器 push，也无法观察真实 CI**。

**替代证据（§21.6 已列）**：本机按 `.github/workflows/ci.yml::backend` 的**逐步骤**
跑了一遍等价集合（不是"跑了几个 make"）：

| CI step | 本地等价 | 结果 |
|---|---|---|
| Lint | `make lint` | ✅ |
| Pyright | `make pyright` | ✅ 0 errors |
| Contract drift | `make check-types` | ✅ |
| Architecture boundary | `make check-arch` | ✅ |
| Timeout budget | `make check-timeouts` | ✅ |
| Contract reachability | `make check-contract` | ✅ |
| Debug frontend auth | `make check-debug-auth` | ✅（21 文件） |
| CI trust boundary | `make check-ci-isolation` | ✅（3 个 workflow） |
| Secret scanning | `make check-secrets` | ✅ |
| Test set algebra | `make check-test-sets` | ✅ |
| Compose policy | `make check-compose` | ✅ |
| Env lock | `make check-env-lock` | ✅ |
| Dependency & static scan | `make audit` | ✅（`No known vulnerabilities found, 2 ignored`） |
| Offline suite | `make test-normal` | ✅ 6047 passed |

**push 后必须核验（清单，给有凭据的机器）**：

1. `git push origin main`——7 个 commit：`c70312de`（N-10）/ `7ef3202b`（F6）/
   `87cc7e38`（F7）/ `ac3005d0`（§63 顺序扰动）/ `b3862338`（Challenger F2/F3/C2）/
   `70425fdd`（Challenger A）/ 本文所在 commit（docs）。前六个 hash 已固定，
   最后一个每 amend 一次都会变，故不写死。
2. 确认 `ci.yml` 的三个 **required** job 全绿：`Backend (static gates + offline suite)`、
   `Frontend (vue-tsc + build)`、`Real MongoDB (storage layer)`。
   `real-mongo` 与本批理论上无关（无 schema/持久化改动），但它是 **required job**，
   必须等它跑完而不是跳过（§96）。
3. `contract-frames.yml` 也应绿——本批**没有** wire/schema 变更，若有红则说明
   golden frames 与本批声明不符，必须查清而不是重生成。
4. 若任何 job 因为"账户额度/账单"根本没启动（注解 `The job was not started because
   recent account payments have failed`），按 `docs/ci.md` 的处置走——那**不是**本批的红。

### 21.8 External

**未运行**（§95：真实 LLM 凭据为 opt-in，普通 CI 不依赖收费 provider）。
本批的 capability 判据全部可用 fake/local adapter 证明（§60）——新增用例**零**真实
API 调用、**零**真实凭据读取。

---

## 22. Invariant Mapping

| Invariant | Test | Gate/Suite | CI |
|---|---|---|---|
| **PC1** 报告对应运行期 provider/model | `core/input/tests/test_provider_test_target_authority.py`（10） | `make test-normal` | **push/backend** |
| **PC2** 实例化受 capability 门控 | `...::TestRuntimeFactAnchor`、`...::TestInstantiatedImpliesImplemented`（含 `test_mount_paths_require_a_passing_live_probe`） | `make test-normal` | **push/backend** |
| **PC3** 静态预检 network-free | `...::TestStaticCheckStaysNetworkFree` | `make test-normal` | **push/backend** |
| **PC4** `/debug` 是纯投影 | `...::TestAvailabilityReportingNeverProbes` | `make test-normal` | **push/backend** |
| **PC5** 一个上报事实一个权威 | `...::TestAvailabilityHasOneAuthority`、`...::TestNoHandleMeansNotAvailable` | `make test-normal` | **push/backend** |
| **PC6** unsupported 不得先报成功 | `...::TestUnimplementedProviderIsNotReportedUsable`、`...::TestFullProbeDoesNotPassUnsupported`、`...::TestCapabilityGateDoesNotDependOnTheEnvTable`（5 条，Challenger 补强）、`core/input/tests/test_provider_registry.py::test_unimplemented_provider_excluded_even_with_models` | `make test-normal` | **push/backend** |

落纸位置：[`docs/architecture/invariants.md`](../architecture/invariants.md) §3.2
（Provider capability truth（PC））。**未新增静态 gate**——这六条都是行为不变量，
由普通测试承接，符合 §89"不必新增静态 gate，除非确实存在纯结构 invariant"。

---

## 23. Todo Updates

只改 `N-10` / `F6` / `F7` 三行（§97）：

| ID | 修前 | 修后 |
|---|---|---|
| **N-10** | `OPEN` | **`GUARDED`** `c70312de`（判据与守卫共用同一常量；跨面一致性用例遍历 providers.yaml） |
| **F6** | `OPEN` | **`GUARDED`** `7ef3202b`（含真实热切换 A→B regression） |
| **F7** | `OPEN` | **`GUARDED`** `87cc7e38`（语义混用已消除）+ 「存在性冒充可用」标 **`FALSE_POSITIVE`**（依据 §8.3） |

`OBS-5`（把 stt/tts 的 `model`/`enabled` 补进 `/debug/api`）**保持 `OPEN` 不动**——
它是本批明确不做的那半（§28/§81 不扩 schema），在 §8.3 与 §24 记录。

---

## 24. Keep As-Is

| 面 | 状态 | 理由 |
|---|---|---|
| **network-free 的普通 `--check`** | 保持 | PCT-3；"让预检更真实"不是开始联网的理由。边界由 socket 哨兵钉住 |
| **`--full` / provider test 负责连通性** | 保持 | 只有它们能回答"远端此刻可达吗"；本批只让它们**不再**把 unsupported 报成别的 |
| **provider switching lifecycle** | 保持 | `switch_stt`/`switch_tts` 的建/拆/失败保留旧后端语义一字未改（§77）；本批只让报告跟上它 |
| **合法无-key provider（gpt-sovits / sensevoice / ollama）** | 保持 | Batch 3.5 §39 钉住的语义未回归；新增用例专门覆盖 |
| **未知 provider 的 fail-open** | 保持 | 缺 metadata ≠ 未实现；不误拒第三方/插件 provider（§41/§42） |
| **provider test 的副作用面** | 保持 | 仍真实建后端 + 真发探活请求；本批只改"测谁"（§37） |
| **`/debug` 的面板渲染与字段** | 保持 | 只改值的权威，不改 schema（§28/§81）；"enabled vs health"的区分留给 `OBS-5` |
| **`Module/tts/registry.py` 的 `_UNIMPLEMENTED_PROVIDERS`（空集）** | 保持 | 当前无占位 TTS provider；显式空集让三个子系统的 capability 问法**同形**，新增占位时只需登记一处 |
| **`Module/embedding` / `database/*` 的同形 `NotImplementedError`** | 保持 | 它们**没有**"上报假绿"的报告面（不在 §1 的 provider capability 链上）；不扩大 scope |
| **`_MAY_BE_EMPTY`（`Module/_common/tests/test_registry_helpers.py`）** | 保持 | 它管的是"`models:` 是否允许为空"，只是在注释里**引用**了 gemini 未实现这个理由，不是 capability 判据的第二份实现 |

---

## 25. New Findings（只记录，不修）

| ID | 内容 | 为什么不在本批 |
|---|---|---|
| **NEW-5-1** | `CLAUDE.md` 的 Module index（`Module.llm` 行）把 `gemini` 与真实 backend 并列，**没有** `docs/architecture.md:228` 那样的"已注册但抛 `NotImplementedError`"注记；`.env.example` 也没有 `GEMINI_*`（却给了 `DEEPGRAM_*`） | 纯文档副本漂移，不影响任何判据；本文档提交里**已顺手**在 CLAUDE.md 补了注记（0 风险、1 个词），`.env.example` 保持不动 |
| **NEW-5-2** | `Module/_common/tests/test_registry_helpers.py` 的 `_MAY_BE_EMPTY` 是"gemini 未实现"这一事实的**第三份副本**（在前两个 registry 之外） | 它判的是"`models:` 允许为空"，comment 只是引用理由；同 §91「只对齐报告 authority，不大迁移」 |
| **NEW-5-3** | `core/providers/provider_registry.py` 里 `gpt-sovits/Elysia`（存在于 `gpt_sovits_voices.yaml`）可实例化，但 `providers.yaml::tts.gpt-sovits.models` 只声明 `["default"]` ⇒ 下拉里**不提供**它；反向漂移 | 属 model 级清单与实现能力的**描述性**不一致，非 "reported capability ≠ actual" 的假绿；改动会牵动 UI 候选项 |
| **NEW-5-4** | SSRF 端点门（`validate_endpoint`）长在 `LLMFactory.create` 里，STT/TTS registry **没有**；调用方各自补偿 | 反过来的层次不对称（LLM 里多了、其他少了），非本批"报告假绿"；需要独立评审依赖方向 |
| **NEW-5-5** | `DebugData` 没有 STT/TTS 的 `model`/`enabled` ⇒ 面板无法区分"关掉了"与"坏了" | 即 **OBS-5**（canonical 已跟踪）；本批按 §28 刻意不扩 schema |
| **NEW-5-6** | `test_preflight.py::TestCheckTTSEnv::test_minimax_key_present_passes` 依赖未跟踪的 `.env`（worktree 里必红） | 即 **GR-2**（canonical 已跟踪，Batch 3.5/4 复核成立）；本批只把它**隔离**成对照实验，未改 |
| **NEW-5-7**（Challenger F1）→ **`DEP-7`** | `sensevoice` 是**合法无-key 本地 provider**，预检却报 `[WARN] sensevoice 没有可用地址 — STT ('sensevoice') 将不可用`，而 `STTFactory.create("sensevoice/sense-voice")` **成功**：真默认值在代码里（`Module/stt/sensevoice.py::DEFAULT_BASE_URL = "http://localhost:20042"`，由 `registry.py` 的兜底分支应用），而 `_provider_url_note` 只查 env + `providers.yaml`（后者的 `base_url` 是空串）。`run()` 仍返回 True（WARN 不抬红），故是**假文案**而非假拒 | **既有**（`3452d987` 上同样成立），不在 N-10/F6/F7 三条内；按 §2 记录不修。修法与 S2-H7 对 `gpt-sovits` 做过的同一招（把代码内置默认值写进 `providers.yaml`） |
| **NEW-5-8**（Challenger G）→ **`DEP-8`** | **model 级**假绿：`tts.model = "gpt-sovits/<不存在的音色>"` 时 `--check` 报 `[PASS] TTS 可用`，而启动步骤 `Agent._start_tts()` 对该 `ValueError` **fail-fast**（只对 api_key 类错误降级）⇒ 预检绿 + 启动崩，与 N-10 **同形但发生在 model 轴**。本批的 `is_implemented` **刻意是 provider 级**且已在 docstring 写明；没有把 model 级事实提升为 provider 级（§13/§71 的检查通过） | **既有**，不在三条 finding 内；修它需要 model 级解析 API（比"最小"大），按 §2 记录不修 |
| **NEW-5-9**（Challenger C2 残留）→ **`DEP-9`** | `preflight.run()` 在**缺 openai SDK** 的机器上以**裸 ImportError** 崩溃、没有 `结果:` 汇总：`_check_embedding_model` → `Module.embedding` → `openai_compat` → `from openai import AsyncOpenAI`。`_check_pip_deps` 确实打印了 `[FAIL] 缺失 pip 包: openai`，但 `run()` 不会因此提前结束 | **既有**（在 `3452d987` 上逐字复现；本批只保证**自己新增的** import 不增加崩溃点，并把它写成 fail-open + 用例）。按 §2 记录不修 |
| **NEW-5-10**（Challenger，接受风险） | ① 用**打补丁的工厂**塞进一个"每个方法都抛 `NotImplementedError`"的替身，报告仍会说可用——工厂就是那道门，绕过它等于绕过整条不变量（生产路径无动态 `setattr`，两条装配路径各有一道 1s 实探）；② 未声明 provider 保持 **fail-open**（WARN，`run()` 仍 True），而启动随后会 `ValueError`——这是 §41/§42 的**有意**取舍，不是缺陷；③ desktop/Electron Profile 下 `available=false` 而 `enabled=true`（后端有意不代理），既有语义 | 均记为**接受风险**，写进本文与 `invariants.md` §3.2 的范围纪律 |

---

## 26. Follow-up

**本批结束，未开始 N-3（§98）**。canonical backlog 中与本批相邻但仍 `OPEN` 的项：

| ID | 状态 | 备注 |
|---|---|---|
| **N-3** | `OPEN` → 需 `PD-2` | 409 `details.currentRevision` 读进程内陈旧缓存；**下一批候选** |
| **GR-3 / GR-24** | `OPEN` | `EXPECTED_INDEX_OPTIONS` 4 条 `unique` 未登记 / `partialFilterExpression` 不比对 |
| **N-15** | `OPEN`（机械收尾） | ≡ GR-3 |
| **OBS-5** | `OPEN` | `/debug/api` 补 stt/tts 的 `model`/`enabled`（需 `make gen-types`）——F7 只覆盖了标志语义 |
| **F8** | `OPEN` | prompt 预算 cross-path（`core/character/preview.py` 不传 `prompt_budget_tokens`） |
| **OBS-3 / OBS-4** | `INSUFFICIENT_EVIDENCE` | 各需一次定点复核 |
| **AR-4 / AR-5** | `OPEN` / `KEEP_AS_IS` | provider portability；本批**刻意**没有把它做成体系（§2/§11） |
| **F5** | `OPEN` | workflow 模型 reporter 缺（debug 只报主模型） |
| **DEP-7 / DEP-8 / DEP-9** | `OPEN` | 本批 Challenger 命中的三条**既有** out-of-scope finding（sensevoice 假 WARN / gpt-sovits model 级假绿 / 缺 SDK 时 preflight 裸崩），已登记 canonical backlog |
| **GR-2 / NEW-5-2 / NEW-5-3 / NEW-5-4** | 见 §25 | 本批新记录，均不阻塞 |
| **F1 / F16 / N-12 / KB-6 / F13 / KB-8 / P2-01r / DEP-*** 等 | 保持原状 | 未触碰 |

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [invariants.md](../architecture/invariants.md) §3.2 | PC1–PC6 的判据映射 |
| [Batch 4 Backlog Triage](2026-09-18-Batch4-backlog-triage.md) | N-10 / F6 / F7 的登记来源与 §10 备选 B |
| [Batch 3.5 正确性分诊](2026-09-17-Batch3.5-correctness-triage-执行记录.md) | N-10 的前身（§12 的 `N-9`）与 preflight 判据对齐的历史 |
| [Batch 1.6 执行记录](2026-09-17-Batch1.6-runtime-authority-lifecycle-执行记录.md) | F6 的前身（`F5`）与 `/debug _ok` 语义悬空的首记录 |
| [todo.md](../../todo.md) | canonical backlog（本批只改 N-10 / F6 / F7 三行） |
