# False-Dead 审计清单（Dead-Code Review Checklist）

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → [architecture](.) → 本文

## 这是什么

本文是**做删除类改动前必须过的一道人工判据**。适用场景：

```text
dead-code cleanup
unused API cleanup
config cleanup（配置/env/YAML 字段）
re-export cleanup
test cleanup（fixture / helper / fake）
```

它回答一个具体问题：**这个「看起来没人用」的东西，真的可以删吗？**

它不是 Batch 3 的执行记录摘要，也不含任何历史统计数字。**它是面向未来使用的操作手册。**
来源与完整案例见文末「Origin」。

---

## 1. 核心原则

```text
zero static references != zero runtime consumers
```

```text
「搜索不到直接调用」只能生成候选，不能证明可以删除。
```

本仓是**动态消费密集**的 Python 应用：公开面被有意设计成字符串可寻址（输入层用
`getattr(agent, "<name>")` 容忍 `MagicMock` 与旧组合根）、契约层有 TS/JSON 双产物、
工具与 provider 走注册表、持久化状态跨进程存活。这些消费方式**一个都不会出现在
`obj.method()` 形式的搜索结果里**。

因此本仓的删除纪律是：

```text
静态工具（ruff / vulture / IDE / pyright / coverage）
    只能产生候选
        ↓
必须逐项走 §4 的 Consumer-Proof Checklist
        ↓
只有全部排除，才允许判 PROVEN_DEAD
```

> **删除是唯一一种"测试全绿也可能是错的"改动。** 误删一个无测试覆盖的公开符号，
> 测试可以**全绿**——因为没有任何测试调用它。真正的判据是「消费者存在性」，不是「测试颜色」。

---

## 2. 最高风险的 FALSE_DEAD 模式

以下 12 类（A–L）在本仓**均已真实发生过**。任何候选命中其中一类，风险等级至少 `MEDIUM`。

### A. Dynamic attribute access（动态属性访问）

**这是本仓最高风险的 FALSE_DEAD 模式。**

```python
getattr(agent, "character_service")      # 字符串形式 —— 静态搜索完全看不见
getattr(channel, "connection_id", None)
ctx.get("_tool_count", 0)                # dict 键，同样不产生静态引用
```

**为什么危险**：`Agent` 的公开面**被有意设计成**「跨模块消费禁止 `agent._xxx`，一律走公开接口」
（见 `CLAUDE.md` 的 Agent 公开接口条），而输入层为了对 `MagicMock`/旧组合根容错，
把大量属性访问写成了**字符串**形式。于是：

```text
某个 @property 的静态 `.name` 引用计数 == 0
    ≠
该 property 无人使用
```

**误删的症状不是 ImportError，而是静默降级**：`getattr(agent, "X", None) or 兜底构造()`
会在属性消失时**新建一套对象图**，于是同一个进程里出现两份互不可见的 repository —— 数据不可见，
但不报错。这类回归极难定位。

**检查**：

```text
getattr / setattr / hasattr / delattr（重点是**字符串字面量**形式）
字符串 property / method name
dict dispatch（如 ctx.services["_key"]、_WS_HANDLERS、format_methods）
```

**硬约束**：**禁止**用「`.prop` 计数 = 0」作为 property 的删除判据。这条判据在本仓无效。

---

### B. Re-export / Public API（重导出与公开面）

```text
F401 / unused import  ≠  dead symbol
```

**本仓已真实踩过**：`ruff --fix` 在首轮误删两处重导出（`core/schema/types.py` 的 `SessionId`、
`core/debug/info.py` 的 `asdict`），实测导致 **59 个模块 ImportError**。那次事故直接催生了
`ruff.toml` 的 `__init__.py` per-file-ignore。

**为什么 F401 判不了这件事**：`__init__.py` 里的 `from x import y` **本身就是导出行为**，
它在定义文件内确实"没被使用"——但那正是它的用途。ruff 对 `__init__.py` 默认按重导出处理，
**但对"刻意逐名重导出 + docstring 明确拒绝 `__all__`"的装配点它无从区分**，只能整体豁免。

**检查**：

```text
__init__.py（全仓 107 个 = 55 个有内容的生产装配点 + 52 个空的测试包标记）
__all__（成员即公开承诺，**即使当前无人 import 也默认 KEEP_PUBLIC_API**）
from x import Y
from x import Y as Y          # ← 见下
package-level import / 模块级 __getattr__（PEP 562 懒导出）
文档化的公开导入路径（README / docstring 里的 `from core.X import Y`）
```

**约定**：`from x import Y as Y` 是**明确的 intentional re-export** 写法（PEP 484 惯例），
可用于向读者与工具同时声明"这是有意重导出，不是漏删的 import"。引入这种写法时不必改门禁——
它是**纯增量**的可读性信号。

**判据**：删一个 re-export 之前，必须能回答「`from <pkg> import <name>` 这个路径还有没有人走」。
答不上来 → `KEEP_PUBLIC_API`。

---

### C. Plugin / Registry side effects（插件与注册表副作用）

有些 import 的唯一作用是**触发注册**：

```python
import core.tools.filesystem   # noqa: F401 —— 只为触发 plugin registration
```

**不要用 F401 逻辑判断这类 import。**

**检查**：

```text
registry before/after import（运行期探测条目数，见 §4）
decorator 注册（@register / @plugin / @section）
模块 import 副作用（注册、缓存预热、schema 注入）
plugin manifest / ALL_PLUGINS / entry points / _BACKENDS
```

**验证方式**（本仓可复制）：

```bash
conda run -n Elysia python -c "
from core.tools import ALL_PLUGINS; print(len(ALL_PLUGINS))"
conda run -n Elysia python -c "
from core.input.ws_handlers import _WS_HANDLERS; print(len(_WS_HANDLERS))"
```

删 import 前后**逐项比对条目数**；数值变了就是删错了。

---

### D. String-based consumers（字符串消费者）

不能只查 `obj.method()`。本仓大量符号只被**字符串**引用：

```text
getattr(...) / dict dispatch
event type string        （如 "character.state.changed"）
tool name string         （如 "desktop.file.select"）
provider name string     （如 providers.yaml 的 "provider/model" key）
class / method name 出现在 YAML / JSON / TS / golden frame 里
```

**搜索方式**：

```bash
rg -n '"<name>"' -g '!*.py'      # YAML / JSON / TS / Vue / shell
rg -n '"<name>"' --type py       # 字符串字面量
```

---

### E. CLI / Env / Config external consumers

**一个字段在 Python 中零读取，不代表它没用。**

**检查**：

```text
CLI parser（argparse 的 dest 之外还要看「解析后是否真被消费」）
shell scripts（scripts/**、start.sh）
systemd / Docker / docker-compose*.yml
.env / .env.example
CI workflow（.github/workflows/**）
部署脚本、README、docs/
```

**特别注意 `env-only intentional config`**：

`TASK_SYSTEM_MODEL` 曾经看起来"YAML 无字段、AppConfig 无字段、看似永远走默认值"，
被列为 dead config 候选。**实测它能把后台任务切到 flash** ——
它是 `intentional external env-only config`，判 **KEEP**。

```text
「config 里没有这个键」≠「这个 config 没人读」。
    env 是独立的一层（优先级高于 YAML），必须在 env 面单独证伪。
```

**另一条容易漏的**：CLI flag「可解析但没有效果」不是 dead code，是 **CORRECTNESS_BUG**。
两者处置不同：前者删代码，后者修行为。

---

### F. Persistence compatibility（持久化兼容）

任何涉及以下内容的字段，**即使当前 HEAD 完全不读**，也可能为了旧数据 / restart / recovery /
migration / 外部检视而必须存在：

```text
Mongo 集合字段（core/storage/schema_drift.py 的声明 + 各 repo）
WAL payload（data/task_result_wal.jsonl）
ServiceState / session.character_state / service_state
memory document 字段
background task result 字段
checkpoint 文件的键（含**旧键的兼容读取分支**）
```

**必须回答的问题**：

```text
旧记录还能读吗？
```

**答不上来 → 默认 KEEP**（判 `KEEP_COMPAT` 或 `LIKELY_DEAD`，两者都不删）。

**没有 migration policy / oldest-supported-version 证据时，不得判 `PROVEN_DEAD`。**
本仓明确**不做迁移框架**（新增字段由 `ensure_index` 自动覆盖），删/改已有字段**没有自动补偿机制**——
这是这类字段必须保守的结构性原因。

**注意「write-only」的两义性**：一个字段"只写不读"**不等于**可以删。
`metadata.incomplete` 这类字段可能是**已落库历史数据的唯一标记**；删掉声明不会删库里已有的键，
但会让那些记录**永久无法被识别**。

---

### G. Restart / Recovery-only consumers（只在重启/恢复路径使用）

只在以下路径使用的代码，**正常运行路径搜索不到，但不代表 dead**：

```text
restart / shutdown
recovery（依赖恢复、journal 重试）
WAL replay（启动时把本地 WAL 补进仓储）
startup migration / schema_drift 比对
启动补投（未投递的后台任务终态）
```

**这类候选默认 `HIGH RISK`**，必须由独立视角专门检查（见 §7），且**必须**在
"启动 / 重启 / 关机"三个场景上实际验证过才允许删除候选升级。

---

### H. Test patch targets（测试打桩目标）

某个 symbol 没有生产调用，但测试可能**以字符串路径**依赖它：

```python
patch("module.symbol")
monkeypatch.setattr(obj, "name", ...)
MagicMock(spec=SomeClass)          # 改签名即破
usefixtures("fixture_name")        # 字符串引用，不产生函数参数
```

**删除会改变"测试控制面"**，破坏面可能是**整层测试静默失效**而不是一条测试红。

**实例**：`conftest.py` 的 4 个 `usefixtures` 字符串 fixture 看似无参数消费者，
删除效果是**整个测试层的存储隔离静默失效**（真连 Mongo/Milvus）。

**检查**：

```bash
rg -n 'patch\(|monkeypatch\.(setattr|delattr)|Mock\(spec=|usefixtures' \
   tests/ core/**/tests/ Module/**/tests/
```

**同时注意 pytest 的 fixture 是「按参数名隐式注入」**：搜索 fixture 使用点时，
必须搜「作为测试函数参数名出现」，**不能只搜调用点**——否则会大批误判为死。

---

### I. Documentation / API commitments（文档与 API 承诺）

即使当前内部消费很弱，**存在明确文档承诺的符号不能按 dead-code 直接删**。

**本仓实例**：

```text
AgentServer.run_repl      → 0 代码消费者，但 8 处文档把它写成对外承诺
                            （CLAUDE.md / core/README.md / core/agent/README.md /
                              agent.py docstring / todo.md）
CognitiveLoop.DEFAULT_CHARACTER_ID
                          → 被 core/cognitive/README.md 显式登记为公开常量
```

**判据**：文档承诺也是一种消费者。删除它属于**架构决策**（要同时决定"文档里那条承诺怎么办"），
不是清死代码。判 `KEEP_PUBLIC_API`。

**检查**：

```text
README（根 + 各模块 + 子目录）
docs/**（含 architecture / testing / database-storage / 协议文档）
examples / 集成文档
public constants / public CLI help
golden frames 与契约产物（TS 生成物 / JSON Schema / raw snapshots）
```

---

### J. Reserved future schema / config（显式预留）

本仓有可 grep 的预留标记：

```text
P3-01-RESERVED
```

**判别标准**（这两条必须同时成立才算"预留"）：

```text
有明确未来用途  +  有注释说明
```

对照概念：**残留** = 已被别的实现取代（如 `LoggerFactory` 之于 `log_setup`）或注释与代码不符。
残留该删，预留该留。

**本仓实例**：

```text
character/*/config.yaml 的 voice / appearance
    → 当前未消费，但 YAML 注释与 character/README.md 明确登记为「占位 / 待启用」
Module/database/graphdb（54 行 ABC）
    → P3-01-RESERVED + eager re-export + owner 成文裁定
core/tool_runtime 的 background/DAG/progress/checkpoint 簇
    → owner 明文作废删除计划；且 prune 循环每 10s 真实读取 children_ids
core/hitl.py 与 ToolEmitter 流式上报面
    → 端到端已接好但两侧尚未接线，半接线状态已如实标注
```

**审计纪律**：遇到 `P3-01-RESERVED` **即跳过**，不逐轮重新发现、重新搁置。
若你认为某条预留该撤销，那是一次**产品决策**（要写清"解除条件"），不是 dead-code 删除。

---

### K. Facade forwarding methods（门面转发成员）

```python
class ToolAdapter:
    @property
    def history(self) -> list[dict]:
        return self._manager.history          # 纯转发，当前 0 内部消费者
```

**不要假定"forwarding 就该删"**。先回答三个问题：

```text
是否有真实消费者？
是否构成稳定 public surface（已文档化的门面）？
是否只是历史透传（有明确替代路径、且替代已接线）？
```

**判定**：若它是**已文档化的公开门面**的一部分（即使当前零内部消费者），
应判 `REFACTOR_LATER` 而**不是** `PROVEN_DEAD`——收窄公开面需要单独一批 + 全量回归，
不能夹在清理批次里顺手做。

**注意区分**：`_manager` 这类**底层对象的同名方法可能才是真消费者所在**
（debug 面板读的是 `ToolManager` 而不是 `ToolAdapter`）。搜索时必须确认宿主类型，
不能只看方法名命中数。

---

### L. Import-time / frozen-state semantics（import 期冻结状态）

某个值看起来只是常量：

```python
_IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()   # ← import 期求值
```

但它可能**冻结**了 env/config 状态。**这类代码不一定 dead，反而可能隐藏 bug。**

**本仓实例**：测试用运行期 `monkeypatch.setenv` 把上传目录重定向到 `tmp_path`，
而模块级常量已在 import 期固化了**真实**目录 ⇒ 隔离**从未生效**，
测试一直在往 `data/uploads/images/` 真写（实测单跑该文件真实目录 `441 → 442`）。
修复方式是改为**调用期解析**的函数，而不是"删掉这个没用的常量"。

**因此**：

> 删除 / 简化前先判断它是不是一个「**错误状态捕获点**」，而不是无用变量。

**检查**：

```text
模块级 = 表达式（尤其调用了读 env / 读文件 / resolve 路径的函数）
import 期缓存（配置缓存、模型 manifest、registry 快照）
是否有对应的运行期改写路径（monkeypatch / env 覆盖 / 热重载）
```

**纪律**：**能被测试改写的输入，不能在 import 期求值。**
若发现这种形态，正确动作通常是**修复**（改为调用期），而不是删除——并把
「改错判据会变行为」写进 commit message。

---

## 3. 风险等级

| 等级 | 适用 | 处置 |
|---|---|---|
| **HIGH** | public API · re-export · dynamic `getattr` · registry · persistence · restart/recovery · config/env | **必须**独立 Challenger 复核（§7）；**必须**在 restart/关机/启动场景实跑 |
| **MEDIUM** | public facade methods · test helpers · compatibility shims | 单人复核 + 全量 `make test-normal`；有测试打桩的须先核对 patch 路径 |
| **LOW** | private local helper，且 direct / dynamic / test 三面都无消费者 | 常规 targeted tests 即可 |

**默认往高判**：判不准时按更高一级处理。把 HIGH 当 LOW 处理的代价是不可逆的用户可见回归；
把 LOW 当 HIGH 处理的代价只是多花一次复核。

---

## 4. Consumer-Proof Checklist（可直接复制使用）

每个候选逐项打勾。**缺任何一项都不允许判 `PROVEN_DEAD`。**

```text
□ direct symbol references         rg -n "\b<sym>\b" .（py 与非 py 两种）
□ imports                          AST 级 import 搜索（Import / ImportFrom）
□ __init__ re-export               107 个 __init__.py 的导出面
□ __all__                         成员即公开承诺
□ getattr / setattr / hasattr      **字符串字面量形式**（最高风险）
□ string dispatch                 dict dispatch / 事件类型 / 工具名 / provider 名
□ registry / plugin registration  import 前后**逐项比对条目数**
□ decorators / import side effects
□ CLI                             parser + 「解析后是否真被消费」
□ env                             os.getenv / ConfigField(env=) / .env.example
□ config YAML/JSON                config.yaml / providers.yaml / character/*.yaml
□ shell / systemd / docker / compose
□ CI                              .github/workflows/**
□ persistence schema              Mongo 字段 / WAL / ServiceState / checkpoint 键
□ restart / recovery              启动 / 重启 / 关机 三条路径
□ tests patch path                patch() / monkeypatch / Mock(spec=) / usefixtures
□ docs / examples / public API     README / docs/** / golden frames / 契约产物
□ git history / original reason   git log -S / git blame（判断"从未存在"还是"曾经存在"）
```

**最后一步是历史溯源，不是可选步骤**：`git log -S "<sym>"` 能区分
「这个类从未存在过」（→ 引用它的 docstring 是纯错误，该改文案）
与「这个类曾经存在、被改名了」（→ 现在引用的名字是过期别名，该更新到真实名字）。
两者处置不同，而只看 HEAD 无法分辨。

**coverage 只能作辅助证据**：

```text
coverage = 0  →  「测试没跑到」
              ≠  「生产没人用」
```

**禁止**用 coverage = 0 作为删除判据。

---

## 5. 分类词汇

沿用本仓既定术语。**避免每轮重新发明词汇**——术语一漂移，跨批次的判定就无法比较。

| 分类 | 含义 | 允许删？ |
|---|---|---|
| `PROVEN_DEAD` | 全部 consumer proof 已排除，无任何消费者 | ✅ 允许进入删除 |
| `LIKELY_DEAD` | 有强证据但存在**无法证伪的面**（如未连真实 Mongo、无法证明仓外无调用） | ❌ 登记不删 |
| `KEEP_PUBLIC_API` | 公开 API / re-export / 文档化公开面 | ❌ |
| `KEEP_COMPAT` | 持久化兼容 / 旧数据 / 预留 schema | ❌ |
| `KEEP_DYNAMIC` | 被动态访问、注册表、字符串、import 副作用消费 | ❌ |
| `KEEP_TEST_ONLY` | 仅测试消费（含 patch target、显式覆盖口） | ❌ |
| `REFACTOR_LATER` | 确无消费者，但删除属**公开面收窄 / 跨层搬迁**，需单独一批 | ❌（登记） |
| `CORRECTNESS_BUG` | 不是 dead code，而是行为缺陷（如 CLI flag 可解析但无效、注释与实现矛盾） | ❌ **单独 fix commit** |
| `UNKNOWN` | 证据不足（拒答优于猜测） | ❌ |

**两条使用纪律**：

1. **`CORRECTNESS_BUG` 不得与删除混在同一个 commit**——两者的 revert 语义完全不同。
   若它正好阻塞当前批次的前提，另起 `fix(...)` commit。
2. **`UNKNOWN` 是一个合法结论**。为了让清单"看起来有产出"而把 `UNKNOWN` 降格成
   `LIKELY_DEAD`，会让下一轮审计重复劳动。

---

## 6. 禁止自动删除

```text
ruff / vulture / IDE unused / pyright / coverage
    只能产生候选，
    不能直接授权删除。
```

逐条说明为什么本仓不能信任这些工具的"unused"输出：

| 工具 | 它能说的 | 它**不能**说的 |
|---|---|---|
| `ruff`（F401） | 这个 import 在**本文件内**没被使用 | 它是否是 re-export / 是否触发注册副作用 |
| `vulture` | 静态引用计数低 | 任何动态消费（`getattr` / 注册表 / 字符串） |
| IDE unused | 同上，且对 `__init__.py` 尤其不可靠 | 同上 |
| `pyright` | 类型是否正确 | 这个符号是否有人用（它是**形状**检查，不是可达性检查） |
| `coverage` | 测试没跑到 | 生产没人用 |

**`ruff --fix` 的使用边界**：

```text
✅ 可以用于：非 __init__.py、非 __main__.py 的生产/测试代码中的真 unused import
❌ 不得用于：未经复核的 public API / re-export cleanup
```

这不是理论顾虑——本仓 `ruff --fix` 在首轮就误删过两处重导出，导致 59 个模块 ImportError。
`ruff.toml` 的 `__init__.py` per-file-ignore 就是那次事故的直接产物。

**另注**：`make check-types`（契约漂移门禁）是**漂移锁**，不是**可达性检查**。
删契约模型字段 + 同步 `make gen-types` 后门禁会 exit 0 —— 它不会问"这个字段真的有人生产吗"。
所以契约面的删除同样必须走 §4 的人工清单。

---

## 7. Challenger 要求（第二视角）

**HIGH-risk candidate 必须由独立 reviewer / agent 专门寻找隐藏消费者。**
提出候选的人**不能**同时充当自己候选的复核者——那是同一个盲区看两遍。

Challenger 的职责不是"同意"，而是**主动构造反例**。它必须逐条检查：

```text
re-export（__init__ / __all__ / 模块级 __getattr__）
dynamic string use（getattr / dict dispatch / 事件名 / 工具名 / provider 名）
registry（import 前后条目数）
persistence（Mongo 字段 / WAL / checkpoint 旧键）
restart / recovery / shutdown
CLI / config / env（含 shell / compose / CI / systemd）
tests patch path（patch / monkeypatch / Mock(spec=) / usefixtures）
文档与 API 承诺
```

**产出要求**：Challenger 必须**至少主动找出 3~5 个「看起来 dead 但实际上不能删」的候选**
作为护栏。**如果一个都找不到，说明方法过于乐观，必须重做**——而不是说明这批代码特别干净。

**为什么强制**：Batch 3 的 Challenger 独立找出 8 条 FALSE_DEAD，
其中 **3 条直接推翻了前序架构审计"可直接删"的结论**。若没有第二视角，
那 3 条会作为"确定性清理"被静默执行，症状（REST 角色域换一套 repository 图、
`call_id` 碰撞场景下 cancel 不到任务）都不会立刻报错。

---

## 8. 删除流程

```text
candidate
  → static scan                      §4 前 4 项
  → dynamic consumer scan            §2 A/C/D/E
  → persistence / config / docs scan §2 E/F/I
  → git history                      git log -S / git blame（区分"从未存在"与"曾改名"）
  → challenger                       §7（HIGH 必需）
  → PROVEN_DEAD                      仅当 §4 全部打勾
  → delete                           每主题一个 commit，单一原因、可独立 revert
  → targeted tests                   受影响模块 + import smoke
  → worktree                         独立 checkout 验证（API 面 / 注册表条目数比对）
  → CI                               真实 CI 绿（本地全绿不算）
```

**顺序不可颠倒。** 特别是：**不要先删、测试红了再回头猜消费者** ——
那时你已经在用"测试覆盖"冒充"消费者证明"，而这两者在本仓不等价（见 §1）。

**每个删除 commit 的最小验证集**：

```text
□ import smoke（受影响包能 import）
□ targeted tests（该模块的测试文件）
□ make lint
□ make pyright
□ make check-arch
```

高风险项另需：`make test-normal`、startup/restart 实跑、`make check-types`。

---

## 9. 典型案例表

以下全部来自本仓真实记录（Batch 1.5 / 2 / 3）。**file:line 会随重构漂移**——
本表是"这类东西不能删"的**模式索引**，不是行号字典。

**引用纪律**：拿本表的任意一条去支持一次删除判定前，必须先用命令现算一次，
而不是相信表里的行号或本文的断言：

```bash
# 行号/事实现算（示例：确认某 property 真的被字符串读取）
rg -n 'getattr\(agent, *"character_service"' --type py core/
# 符号历史现算（区分"从未存在"与"曾改名"）
git log -S '<symbol>' --oneline | cat
```

这也正是本文 §1 的纪律用在本文自己身上：**表格里的结论是候选，现算的判据才是证明。**

| Candidate | 看起来为什么 dead | 实际消费者 / 原因 | 分类 |
|---|---|---|---|
| `Agent.character_service`（及 `instance_repo` / `character_profiles` / `character_state_stream` / `character_runtime_registry`） | 静态 `.prop` 引用计数为 **0** | `core/input/server.py:337` 的 `getattr(agent, "character_service")` 字符串读取 ⇒ 误删会让 REST 角色域**静默换一套 repository 图** | `KEEP_DYNAMIC` |
| `services["_tool_names"]` | 全仓无静态读者 | 同函数相邻的 `_tool_count` 被 `core/tools/self_state/plugin.py:31` 经 `ctx.get` 动态读；`_fsm_state` / `_session_msg_count` 同型，且有 `test_self_state_is_live.py` 时序守卫 | `KEEP_DYNAMIC` |
| `SessionId`（`core/schema/types.py`） | F401 报 unused | **public re-export**；`ruff --fix` 误删后 **59 个模块 ImportError** | `KEEP_PUBLIC_API` |
| `asdict`（`core/debug/info.py`） | F401 报 unused | 在 `core/debug/__init__.py` 的 `__all__` 里 | `KEEP_PUBLIC_API` |
| `frontend_contract` 顶层 114 个零消费者 `__all__` 名 + `core/schema/__init__.py` 的 `# noqa: F401` 面 | 零静态消费者 | 消费者是三产物（TS / JSON Schema / raw）+ 17 golden frames + `make check-types`；**注释本身就是公开承诺的机器表达** | `KEEP_PUBLIC_API` |
| `rest.Message.mongo_id` | Python 侧零读者 | wire 键 `_id` 是前端归档分页**游标**（`chatStore.ts` + `ArchivePanel.vue`） | `KEEP_COMPAT` |
| `ToolRequestMessage.sourceCallId` | 只写不读 | 被 golden frame `ws-v2-tool.jsonl` **逐字节**钉住（实测：内存内删除只有这一个帧漂移） | `KEEP_COMPAT` |
| `TASK_SYSTEM_MODEL` | YAML 无字段、AppConfig 无字段，看似永远走默认值 | **`intentional external env-only config`**——实测能把后台任务切到 flash | `KEEP_COMPAT`（intentional env-only） |
| `conftest.py` 的 4 个 `usefixtures` 字符串 fixture | 无函数参数消费者 | 删除 = **整个测试层存储隔离静默失效**（真连 Mongo/Milvus），不是"一条测试红" | `KEEP_DYNAMIC` |
| `core/monitoring/__init__.py` 的模块级 `__getattr__` | 零 `from core.monitoring import X` | PEP 562 懒导出，存在理由是**打断结构性 import 环** | `KEEP_DYNAMIC` |
| `Module/database/graphdb`（54 行 ABC） | "stub only = dead？" | `Module/database/__init__.py` **eager re-export**（已发布面）+ owner 成文裁定 | `KEEP_DYNAMIC` |
| `core/hitl.py`（半接线） | 两侧尚未接线 | 生产 import + 测试**读源码**断言（改名或重排即红） | `KEEP_TEST_ONLY` |
| `ToolManager._inflight` | 审计称"第二索引"、看似冗余 | 是真实取消漏洞的**修复件**（cancel 取 `_tasks` ∪ `_inflight`） | `KEEP_DYNAMIC` |
| `run_in_background` / `children_ids` / `depends_on` / `progress` / checkpoint 簇 | 无生产主路径消费者 | owner **明文作废**删除计划；prune 循环每 10s 真实读取 `children_ids` | `KEEP_DYNAMIC` |
| `AgentServer.run_repl` | **真 0 代码消费者** | **文档承诺**：`CLAUDE.md` + 两份 README + `agent.py` docstring + `todo.md` 共 8 处 | `KEEP_PUBLIC_API` |
| `CognitiveLoop.DEFAULT_CHARACTER_ID` | 零引用 | 被 `core/cognitive/README.md` 显式登记为公开常量 | `KEEP_PUBLIC_API` |
| `character/*/config.yaml` 的 `voice` / `appearance` | 当前未读（`from_yaml` 甚至不解析 `appearance`） | YAML 注释 + `character/README.md` 登记为「占位 / 待启用」⇒ 符合 `P3-01-RESERVED` 判别标准 | `KEEP_COMPAT` |
| `live2d_directive` 全链路 | `core/` 内 0 发射点 | 协议文档明文「待接线，客户端需能容忍缺席」+ TS 生成物 + v2-only 门禁测试 + owner 裁定 | `KEEP_COMPAT` |
| `AgentInfra.{milvus, http, background_task_results}` | engine 侧 0 读者 | 契约形状被 `test_public_surface.py` **逐项断言**（"dataclass 形状不得静默变化"）；`background_task_results` 的消费者在 core 侧 | `KEEP_COMPAT` |
| `Module/*/config.py` 的 6 个 dataclass + `from_config()` | 生产零实例化 | Module 库**公开 API**（在 `__all__` 里） | `KEEP_PUBLIC_API` |
| `core/tools/vision/config.yaml`（0 字节） | 空文件 | 删它会触发 `manager.py` 的 warning（该空文件在**抑制一条 warning**），且违反插件三元组约定 | `KEEP_DYNAMIC` |
| `scripts/capture_ws_trace.py`（DEPRECATED） | 已标废弃 | 被 `scripts/tests/test_capture_ws_trace.py` import | `KEEP_TEST_ONLY` |
| `ToolAdapter` 的部分转发成员 | adapter 上 0 消费者 | **已文档化的公开门面**（debug 面板读的是底层 `ToolManager`，不是 adapter） | `REFACTOR_LATER` |
| `preflight` 三张 provider→env 表 ↔ `providers.yaml` | 语义重叠，看似可去重 | **不等价**：gpt-sovits 用的是 `url_key` 而非 `env_key` ⇒ 天真 DRY **会改变行为** | `REFACTOR_LATER` |
| `config.yaml::database.milvus_token` | YAML 行今天就不可能生效（`secret=True` ⇒ loader 显式跳过 YAML） | 属**部署/环境面**，且 `docs/database-storage.md` 把它当权威抄了一遍 ⇒ 删了零行为变更但需同批改文档 | `REFACTOR_LATER` |
| `_IMAGES_DIR = ProjectPath...resolve()`（模块级） | "只是个没用的常量" | **不是 dead**——它冻结了 import 期状态，隐藏**测试隔离失效**（真实目录被持续写入）。正确动作是**改为调用期解析**，不是删除 | `CORRECTNESS_BUG` |
| `Module/tool/safety.py` 的 docstring 引用从未存在过的类 | — | 不是 dead code；`git log -S` 证明该类**从未存在** ⇒ 改文案 | `CORRECTNESS_BUG` |

---

## 10. 相关文档

| 文档 | 用途 |
|---|---|
| [invariants.md](invariants.md) | 系统不变量 → 测试 → 门禁 → CI 的映射；含「有测试但 CI 跑不到」的假保障清单 |
| [../code-style-guide.md](../code-style-guide.md) | `getattr` 使用纪律（该文 §3）——本文 §2.A 是其在审计语境下的延伸 |
| [../ci.md](../ci.md) | CI 布局与信任边界（哪些 job 会跑到你的删除） |
| [../testing.md](../testing.md) | 测试分层与档位命令（删除后该跑哪几档） |
| [`../../todo.md`](../../todo.md) | 待办与已知问题总表；审计中发现的 `CORRECTNESS_BUG` 与 `LIKELY_DEAD` 登记在此 |
| [`../readme-conventions.md`](../readme-conventions.md) | README 编写规范 |

---

## Origin

本文沉淀自 **Batch 3（Deletion / Simplification）** 的 FALSE_DEAD / KEEP / REFACTOR_LATER 判定，
以及 Batch 1.5（config truthfulness）与 Batch 2（guardrails hardening）的相邻结论。

完整案例、逐条证据与执行数字见：

```text
docs/review/2026-09-17-Batch3-deletion-simplification-执行记录.md
docs/review/2026-09-16-Batch1.5-config-truthfulness-执行记录.md
docs/review/2026-09-17-Batch2-guardrails-hardening-执行记录.md
```

**本文刻意不含** batch 编号以外的执行统计（候选数 / 删除数 / LOC / commit 数）——
那些是历史复盘的素材，不是长期判据。判据一旦带上数字就会过期，而过期的判据会被忽略。

下次做删除类改动时：**先读本文 §4，再动手。**
