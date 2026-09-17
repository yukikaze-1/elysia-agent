# 测试指南

## 分层体系

测试统计由 `scripts/print_test_stats.py` 生成（F5：不手写易漂移数字）——`conda run -n Elysia python scripts/print_test_stats.py`。当前（2026-09-17，脚本输出）全量收集 6002：

| 层 | marker | 标准 | 测试数 |
|---|--------|------|--------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent | 3389 |
| 服务 | `service` | 单模块 + mock/fake 替身，不连真实依赖 | 2585 |
| 集成 | `integration` | 需真实数据库或外部 API | 7 |
| 慢速 | `slow` | 起 Agent 且连真实依赖 / 端到端 / >1s | 39 |
| 外部 | `external` | 需真实 LLM/DB 凭据（F1，独立 opt-in） | 18 |

> **口径说明（F1 起）**：`external` 独立于四层——真实 LLM E2E（WorkflowEngine 集成）不进合并门禁。
> 各层相加与全量的差异（多 marker 重复计入）以脚本输出为准。

### CI 现状：只监听 push 到 main（2026-09-16）

本仓当前**不开放 PR**，故 `ci.yml` 与 `contract-frames.yml` 都只监听 `push: [main]`，
`pull_request` 触发器与两个 PR 侧 job 已移除；全部 job 跑在 self-hosted runner 上。
**为什么（账单教训）、信任边界规则、以及开放 PR 时的完整恢复清单（含分支保护陷阱）
见 [ci.md](ci.md)**——那是唯一权威，本文不再复述以免两处清单漂移。

### 合并门禁的两个集合是**互斥**的（2026-09-14 CI 性能专项）

```bash
make test-quick       # pytest -m "unit or service"                    → 5952 收集
make test-remainder   # pytest -m "not slow and not external and not unit and not service" → 0（2026-09-17 起）
make test-normal      # pytest -m "not slow and not external"          → 5952 收集（**真正离线**，2026-09-17 起）
```

`test-quick ∪ test-remainder == test-normal`，且两者交集为空。这两条恒等式由
`make check-test-sets`（`scripts/check_test_sets.py`）机械验证。

**为什么要有这条约束**：CI 的 push 流程此前是并列的 `Quick` + `Normal` 两个 job。
它们看着能并行，实际不能——本仓库只注册了 **1 个** self-hosted runner，一个 runner
同时只跑一个 job（runner 自己的诊断日志显示 Normal 恒在 Quick 结束后 4–13s 才启动）。
而 `Quick ⊆ Normal`，于是 **4912/4986 = 98.5% 的用例被同一批机器跑了两遍**。
现在两个集合仍有定义（本地迭代用 Quick，验证余集用 remainder），但 push CI 合并成
**一个 job 跑一遍 `test-normal`**——见 `.github/workflows/ci.yml` 的 `backend` job。

> **标记纪律（2026-09-14 起由代码强制）**：全仓**不得有无 marker 的测试**
> （无 marker 会被 `make test-quick` 静默跳过，PR 门禁对它不设防），也**不得出现
> `(slow|integration|external) and (unit|service)` 的叠加**（会把真 I/O 用例塞进快门禁，
> 破坏上面那条互斥性）。两条不变量由根 `conftest.py` 的
> `pytest_collection_modifyitems` 钩子在**收集阶段**强制：违规 → `pytest.UsageError`，
> 失败原因显式打 stderr。**退出码不是契约**：串行下是 4，但 `pytest -n N` 下 xdist 会
> 把它转成 `INTERNALERROR … assert not crashitem`（退出码 3）且**异常消息会丢**——
> 所以原因由钩子自己 print 到 stderr。
>
> **为什么是 hook 不是 test**：hook 在**每个**进程里都跑（含 `pytest -n N` 的每个
> worker），而 test 只被派给一个 worker——写成 test 的话 xdist 下它只看得到自己那个
> 分片，断言平凡成立 = 假绿。实测该 hook 在 `-m` 过滤**之前**收到全部条目。
>
> 历史漂移：此约定写在文档里但只能靠人跑自查命令，实测积到 **71 条**无 marker
> 测试（9 个文件），2026-09-14 已修复。此前文档里那条自查命令：
>
> ```bash
> pytest -q --collect-only -m "not (unit or service or integration or slow or external)" | grep -c "::"   # 期望 0
> pytest -q --collect-only -m "(slow or integration or external) and (unit or service)" | grep -c "::"   # 期望 0
> ```
>
> 另：`pytest.ini` 设 `timeout = 300`（`pytest-timeout`，`thread` 方式）——无界 `await`
> 回归成挂死时不再无限期挂住 CI。

### 并行执行（pytest-xdist，2026-09-14 引入）

`make test-quick` / `make test-normal` 默认带 `-n 8 --dist loadfile`
（`PYTEST_N` 可覆盖：`make test-normal PYTEST_N=12`）。

**实测档位曲线**（本机 24 核；`-m "not slow and not external"`。下表 passed/skipped 数是 2026-09-14 的实测快照，仅用于比较**并行档位曲线**；该档位当前收集 **5952**，权威计数以 `scripts/print_test_stats.py` 输出为准）。
两列是**同一天的两次扫描**——"修复前"是还没动 embedding 替身（见下节，它占了 teardown
的 44.7%）时测的，"修复后"是打上替身之后：

| workers | 修复前 pytest / wall | 修复后 pytest / wall | 串行加速比（后） |
|---------|---------------------|---------------------|----------------|
| 串行 | 544.1s / 551s | **313.7s / 320s** | 1.0× |
| `-n 4` | 156.7s / 159s | 未重测 | — |
| **`-n 8`（默认）** | 104.5s / 107s | **63.2s / 66s** | 5.0× |
| `-n 12` | 84.6s / 87s（另 3 次 81.4–82.3s） | **53.3–54.1s / 56–57s** | 5.9× |
| `-n 16` | 88.0s / 91s | 未重测 | — |
| `-n 24`（≈`auto`） | 96.8s / 100s，**那一次少报 1 条**（见下） | 63.9s / 65s | 4.9× |

**结论与取舍**：

- **`-n auto` 不是答案**：24 worker 并不比 12 快（修复前 96.8s vs 84.6s，甚至更慢；
  每个 worker 都要独立 import torch/sentence-transformers，数百 MB），而默认 8 与它的
  差距只有 ~1s。**CI 明确禁止 `-n auto`**：worker 数不应随 runner 核数漂移。
- 默认取 **8** 而不是更快的 12：修复 embedding 之后曲线已经变平（12 只比 8 快 9s，
  约 7%），而 8 个 worker 在"这台机器同时跑 mongod / milvus / neo4j / 开发工具"
  的前提下留更多余量。要更快就 `PYTEST_N=12`（实测 5 次全绿，81–84s → 53–54s）。

#### `-n 24` 那次少报 1 条：结论是「一次性偶发，未复现」（2026-09-14 RC 复核）

**现象**：修复 embedding 之前的 `-n 24` 那一次报告 `4985 passed, 2 skipped`（=4987），
而当时选择集是 4988——**少 1 条，且输出里没有任何报错行**。

**复核结果**：在最终工作树上重跑 `-n 24` **4 次**——前 2 次（守卫测试加入前）报
`4990 passed, 2 skipped = 4992`，后 2 次（加入后）报 `4992 passed, 2 skipped = 4994`；
**每次都恰好等于当时的收集数**。即该现象**未能复现**。

**逐条排除**（都基于实测，不是推测）：

| 假设 | 证据 | 结论 |
|------|------|------|
| collection mismatch | 最终树连续 4 次的报数都等于当时的收集数（4992 → 4994）；同一批次里紧邻的 `-n 16` 那次也报了完整数 | 排除 |
| worker crash | 无 crash/失败行。对照组证明这条路径**会**报出来：`-o timeout=3 -n 2` 的挂死探针输出 `FAILED … worker 'gw0' crashed`、退出码 1 | 排除 |
| pytest-timeout | 整个 run 只用了 96.8s，远低于每测 300s 预算 | 排除 |
| 内存耗尽 / OOM | `/var/log/kern.log` 与 `/var/log/syslog`（都可读、覆盖该时段）**无 OOM 记录**；实测 `-n 24` 峰值占用 **≈12.7 GB / 31 GB** | 排除 |
| 输出统计问题 | 4 次重跑计数精确，非系统性 | 未证实 |

**唯一未排除的假设（标注为假设）**：当时机器上**同时有另外两个全量 pytest 在跑**
（fixture 审计 subagent 的 durations 插件全量 + 一个 1Hz 的 `ps` 采样器）——那是整个
档位系列里唯一一次这种条件。至于"竞争如何导致某条用例不上报"，**从产物里查不出来**
（该次输出没有任何错误行）。

**因此的处理**：默认 `PYTEST_N=8`；**CI 不使用 `-n auto`**；把上面这条残留不确定性
写在这里。若将来在干净的 runner 上再次观察到"少报但全绿"，按 worker crash 路线排查
（`-v` 看是否有用例从未开始，或加 `-p no:cacheprovider --max-worker-restart=0` 观察）。

**已知边界**：

- `--dist loadfile`（整文件进同一 worker）而非默认的测试级 `load`：本仓多处测试
  共享按文件划分的假替身/模块状态，loadfile 用一点负载均衡换掉这类同文件内交互。
- **`timeout_method = thread` 与 xdist 的交互**：`pytest-timeout` 的 thread 方式在超时
  时 dump 全线程栈后 `os._exit(1)`——**整个 worker 进程退出**，该 worker 队列里剩余
  用例一并丢失。300s 阈值下这只可能是真回归，但排查时要意识到"少跑了一些用例"
  可能是这个原因（这条路径**会**打印 `worker 'gw0' crashed`，与上面那次不吻合）。
- 需要跨 worker 隔离的资源在根 `conftest.py` 收口到各自测试的 `tmp_path`：
  `ELYSIA_PATH_SESSION_PENDING`、`ELYSIA_PATH_SCHEDULER_CHECKPOINT`、
  `ELYSIA_PATH_MEMORY_PENDING_DELETES`、`ELYSIA_PATH_MEMORY_PENDING_SYNCS`、
  `ELYSIA_PATH_SESSION_PENDING_CLEANUP`（这些 journal 的原子写用 `<path>.tmp`，
  同名文件在多进程下会互相覆盖/删除/串读）。
  **已知缺口**：`TokenTracker._pending_file`（`data/token_usage_pending.jsonl`）
  在 import 期就把路径固化进**类属性**，`ELYSIA_PATH_*` 覆盖对它无效
  （它的测试用实例属性覆盖）。目前没有测试从"未隔离目录"走到它，属潜在项。


### 真实数据库防线（unit/service 不碰生产库）

`service` 的定义里写着「**不连真实 DB**」，但这条约定直到 2026-09-11 才被强制执行。默认配置
恰好指向开发者本机生产库：

```python
AppConfig().database.mongodb_uri = "mongodb://localhost:27017"   # core/config/app_config.py:119
AppConfig().database.db_name     = "default"                      # core/config/app_config.py:134
```

于是任何 `Agent(config=AppConfig())` + `await agent.start()` 的 unit/service 测试，只要本机
Mongo 在跑就会连上并写入（`agent.stop()` 的 `save_all()` 回写 `sessions` / `message_archive`）。
更麻烦的是 **`agent.start()` 对连不上库是静默降级**——「写生产库」与「没写」两种情况下测试都绿，
问题从不自我暴露。实测残留（`default` 库）：`sessions` 标题 `active`/`lease`/`burst`、
`character_versions.session_id="test_default_type"`；2026-09-10 清过一次，测试跑完后再次出现。

**根 `conftest.py` 的 autouse 防线**，对所有测试生效（仅 `external` 放开）：

| 目标 | 处置 | 为什么 |
|------|------|--------|
| **Mongo**（生产连接串） | 导流到内存 `FakeDocumentDBBackend` | `agent.start()`/`stop()` 会读写 `sessions` / `message_archive` |
| **Milvus**（LTM 集合） | 集合名重定向到 `long_term_memory*__test` | `clear_all()` 是 **drop + recreate collection**——`TestDebugDbApi::test_ltm_clear_all` 实测**清空了整个 `long_term_memory_v2`** |

Mongo 的判据是**实际连接串**而非只看 provider_id——测试若显式把 `MONGODB_URI` 改到别处
（如指向不可达端口验证失败分支），不受影响。unit/service 另把 Milvus 客户端拦截为抛错桩
（它们本就不需要向量库，省掉无谓连接）。防线**导流而不判失败**，运行结束时在终端汇总被导流
的测试数（保持可见，避免为上百个既有测试批量加标记）。

> **为什么 Milvus 必须重定向而不是豁免 `slow`**：`slow`/`integration` 的语义是「允许用真实依赖」，
> 但实测它们**并不真的需要**——`TestMemoryIntegration`（写测试记忆）、`TestDebugDbApi`（清库）
> 都用 `FakeLLMBackend`，真库只是被 `AppConfig()` 顺带连上。且测试写入用的是**真实 scope**
> （`agent._user_identity.userId` + `chi_builtin_elysia`），与真实记忆无法区分。

需要防线让位的测试：

```python
# 自管存储（注入自有 fake 并驱动真实 connect() 路径）
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("no_storage_guard")]

# 显式声明「本测试不碰真实库」（需要 agent.start() 跑管线时）
@pytest.mark.usefixtures("fake_storage")
```

实现与背景见 `core/tests/_real_db_guard.py`。存量清理脚本（均默认 dry-run，先整体备份）：

- `scripts/dev/purge_test_residue.py`——集合级残留（测试会话 / `character_versions`）。
- `scripts/dev/purge_test_messages.py`——`default` 会话里**逐条**的测试消息。判据是
  「(role, text) 组合不可能来自真实对话」（如 assistant 侧的 `hello`/`ok`/`test`）；
  另有模糊项（`我叫妖梦`/`你好呀` 等）默认保留，经上下文核查确认无误后加
  `--include-ambiguous` 一并删除。
- `scripts/dev/purge_test_generations.py`——测试跑留下的孤儿 generation
  （`request_id=P001` 且关联消息文本为空；真实中断的生成通常仍有部分文本，故保留）。

### 外部测试门禁（E4，`RUN_EXTERNAL_TESTS=1`）

`external` 测试（真实 LLM/DB E2E）**双保险门禁**：

1. 合并门禁 `-m "unit or service"` / `-m "not slow and not external"` 已排除 `external` marker。
2. `Module/conftest.py` 的 collection hook：未设 `RUN_EXTERNAL_TESTS=1` 时，所有 `external` 测试整体 `skip`（用 `item.add_marker(skipif)`——裸 `pytest.skip()` 在 collection 阶段会中断收集）。

即「环境恰有 `DEEPSEEK_API_KEY` + 显式 `-m external` 但忘设 env」也不会误跑真实 LLM。显式 opt-in：

```bash
make test-external
# 或: conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short
```

### 真实 MongoDB 测试层（S2-R6-1，`RUN_REAL_MONGO_TESTS=1`）

`tests/real_mongo/` 是**唯一**连真实 MongoDB 的测试层，专测"只有真驱动能回答"的
问题：索引声明能否被 Mongo 8 接受、唯一索引真的拒重复键吗、`$setOnInsert` / 条件
update 的语义是否如实现所假设、TTL 的 `expireAfterSeconds` 有没有被静默忽略、
`schema_drift` 的自检与真库是否一致。此前全仓**零**真实 MongoDB 测试（报告 S2-R6-1）。

```bash
docker compose -f docker-compose.test.yml up -d      # 起测试库（127.0.0.1:27018）
make test-real-mongo                                  # = 下面那条命令
# ELYSIA_TEST_MONGODB_URI=mongodb://127.0.0.1:27018/elysia_test #   RUN_REAL_MONGO_TESTS=1 python -m pytest -m real_mongo -q
docker compose -f docker-compose.test.yml down        # 用完即弃（无 volume）
```

四层门（任一层不满足都**不会静默通过**）：

| 门 | 机制 | 不满足时 |
|---|---|---|
| 不进任何默认档位 | marker `real_mongo`（未登记进分层）+ tier `external` | `make test-quick` / `test-normal` / `test-full` 都不跑它 |
| 显式 opt-in | `RUN_REAL_MONGO_TESTS=1`（`Module/conftest.py` 的 external 门禁对 `real_mongo` 放行，由本层自查） | 未设 → 整层 `skip`（带原因，不是"跑过了"） |
| 目标必须是测试库 | `tests/real_mongo/conftest.py` 校验库名含 `test`，且**核验仓储真连到哪个库** | 不像测试库 → **fail**（本层会 `drop_database`） |
| 环境必须真满足 | 显式要求时连不上 → `pytest.fail` | 容器没起来 → **fail** 而非 skip（假绿③） |

**CI 归属（2026-09-16 迁移）**：`ci.yml` 的 `real-mongo` job 是 **push-only + self-hosted**，
job 内自己 `docker compose -f docker-compose.test.yml up -d`（等 healthy 才跑用例）、跑完
`down --remove-orphans`，并在 `always()` 的收尾步骤里**显式检查退出码 + 断言容器确实没了**
（`down` 返回 0 不等于清理生效）。不再用 GitHub-hosted 的 `services:`——那按分钟计费，
账户额度一旦见底 job **根本不启动**，会给每次 push/PR 挂一个与代码无关的红叉
（比没有这个 job 更糟：训练人忽略红色）。

PR 侧不再跑本层：要保留 PR 覆盖就**必须**留在 GitHub-hosted（不受信 PR 代码不得落在持久
runner 上，`check-ci-isolation` 会拒答）。本层是 `external` 档、任何默认档位都不跑，
合入后的覆盖足够。

> ⚠️ **迁移后必须检查分支保护**：required status check 按 **job 名**匹配、且只在 PR 上评估。
> 若 `Real MongoDB (storage layer)` 被勾成 required，push-only 之后所有 PR 都会**永远等不到
> 它上报**而卡死——需先在 Settings → Branches 取消勾选。

> ⚠️ **实测踩到的坑，写在这里防复发**：`base_url` 只给连接串，**目标库由
> `database_name` 决定**。第一次跑本层时用 `Repo(base_url="…/elysia_test")` 构造，
> 库名却取默认 `"default"` → 索引与文档全写进了**生产库**。所以本层只允许经
> `real_mongo_repo` 工厂构造仓储（强制显式 `database_name` + 落点核验）。
> 同批发现 `UserIdentityRepository` 的库名被硬编码成 `mongodb/default`（同层唯一
> 不接受 `database_name` 的仓储）——已补上参数，默认值不变。

真实 LLM 测试必须同时标 `integration + external`。workflow 确定性测试基建在
`Module/workflow_engine/tests/helpers/fake_llm.py`：`FakeLLM`（对齐 `chat.completions.create`
的 `tool_calls`/`content`/`usage` 访问面 + `gate`/`calls`/`prompts`）、`with_deadline`（gated 测试
挂死防线，`asyncio.wait_for` 封装）、`CountingLLM`（外部成本计数包装，委托透传 `messages`）。

## 日常命令

```bash
conda activate Elysia

# 日常开发（unit + service；串行 ≈9min27s，xdist 默认 -n 8 后 ≈1min03s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short -n 8 --dist loadfile

# 提交前 — 排除 slow/external（**真正离线**；串行 ≈5min13s，-n 8 后 ≈1min03s）
make test-normal
# 或: pytest -m "not slow and not external" -q --tb=short -n 8 --dist loadfile

# Normal 中不在 Quick 里的余集（**2026-09-17 起 0 条**——原先那 3 条实为 external）
make test-remainder

# 全量（含 slow；slow 无界，慢）
make test-full
# 或: pytest -q

# 导入风格门禁（秒级——这一条确实快）
make lint
# 或: ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__

# 测试集合代数门禁（约 35s，3 次收集）：Quick ∪ remainder == Normal 且交集为空
make check-test-sets
```

> **时间数字的来源**：上表的串行值来自 2026-09-14 本机 `--durations` 基线；xdist 值来自
> 同日的档位实测（表见上文「并行执行」）。CI 侧的耗时以 runner 自身日志为准，
> 两者不要混用——本机 24 核，CI 的 GitHub-hosted runner 只有 4 核。

## 已修复：teardown 占了 44.7%（2026-09-14）

**逐条累加 phase 时长**（串行 `-m "unit or service"`，测量时收集 4985 条）后的真实分布：

| phase | 合计 | 占比 | 单条最大 |
|-------|------|------|---------|
| setup | 32.3s | 5.7% | **0.036s** |
| call | 281.3s | 49.6% | 34.5s |
| **teardown** | **253.9s** | **44.7%** | 6.8s |

**没有"昂贵的 fixture"可优化**——全仓最贵的 fixture 是 `agent`（真
`Agent(config=AppConfig())`），均值 20.2ms。真正的成本是 **63 条各 ~3.8s 的
teardown**（3.5–4.5s 带状聚集，合计 238.6s）。

**根因**：`Agent.start()` 起一个 embedding 预加载任务，`Agent.stop()` 用 **3.0s**
预算取消它（`core/agent/_lifecycle_tools.py`）；真实 `bge-large-zh-v1.5` 冷加载远超
3s → 任务在预算内没落定 → 加载**溢进 pytest-asyncio 的事件循环收尾**。
3.0s 预算 + ~0.7s settle 正是那 3.67–3.77s。

**修复**：把已有的 `_no_network_embedding` 从**目录作用域**（只覆盖 490/4985 条，
且恰好漏掉 core/agent、core/input、core/cognitive 全部热点）上提到根 conftest 的
autouse `_no_real_embedding`。豁免 `external` 与显式请求 `real_embedding` 的测试。

反证链（每条都实测过）：
- `-p no:timeout` 后 27.18s vs 27.05s → **不是 pytest-timeout**；
- 只构造不 `start()` 的对照 0.05s、**完全没有 teardown 项**；
- `Agent.stop()` 在 `call` 相里 4 次共 0.23s → 不是 stop 本身慢；
- 打替身后 `core/input/tests/test_version.py` **26.0s → 13.3s，teardown 条目归零**。

## 其它已知慢点（2026-09-14 实测，未修）

`--durations=50 --durations-min=0.1` 串行结果里最大的三条，各约 30s、合计 ~95s：

| 测试 | 耗时 | 说明 |
|------|------|------|
| `scripts/tests/test_check_secrets.py::test_real_repo_full_history_verdict_is_faithful` | 34.5s | `git clone --depth 1`（`:208`） |
| `core/tests/test_phase3.py::TestAudioBufferErrors::test_stt_timeout` | 30.1s | 桩 sleep 99s，测试等真实预算 `timeout=30.0`（`core/input/audio_buffer.py:356`） |
| `Module/workflow_engine/tests/unit/test_workflow_engine.py::test_pause_sets_pause_requested` | 30.1s | **unit** 却烧掉 `pause()` 默认 `timeout=30.0`（`Module/workflow_engine/workflow_engine.py:494`），而该断言并不需要它 |

其余量级：`core/cognitive/tests/test_v2_generation.py` ×4 各 7.1s、
`core/input/tests/test_v2_ws.py::TestSetTtsVoice` ×3 各 5.0s。

**sleep 基本是红鲱鱼**：全仓字面量求和 26704s，但其中 26677s 是 22 处
`sleep(3600/999/99)` 这类"永久挂起"哨兵；真实会执行的 sleep 合计只有 **27.3s**。
也没有 retry/backoff 循环。



## Import 门禁（ruff TID252）

强制绝对导入：`from core.xxx import y` / `from Module.xxx import y`，禁止相对导入（`from .module` / `from ..module`）。`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免。配置见根目录 `ruff.toml`；提交前用 `make lint` 验证。

```bash
# 自动修复相对导入 → 绝对（一次性迁移用）
ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__ --fix --unsafe-fixes

# 查非豁免文件残留相对导入（应输出为空）
grep -rEn "^from \." core/ Module/ --include="*.py" \
  | grep -v __pycache__ | grep -vE "__init__\.py|__main__\.py|/tests/"
```

## 按模块运行

```bash
# 按 marker 过滤
pytest -m unit -q                          # 只跑纯单元测试
pytest -m "unit or service" -q             # 快速验证
pytest -m "not slow" -q                    # 跑除了慢速之外的所有
pytest -m slow -q                          # 只跑慢速测试

# 按目录运行
pytest Module/llm/ -v                      # LLM 模块
pytest Module/tts/ -v                      # TTS 模块
pytest Module/database/ -v                 # 数据库模块
pytest core/tests/ -v                      # Core 测试
pytest core/agent/tests/ -v                # Agent 生命周期
pytest core/config/tests/ -v               # 配置 dataclass
pytest core/monitoring/tests/ -v           # Token 监控
pytest core/task/tests/ -v                 # Task 调度
pytest core/tools/ -v                      # 工具插件
pytest Module/workflow_engine/tests/unit/ -v   # WorkflowEngine 单元
pytest Module/workflow_engine/tests/integration/ -v  # WorkflowEngine 集成
pytest frontend_contract/tests/ -v         # 前后端契约（建模 + TS 生成器）

# 按文件运行
pytest Module/llm/tests/ -v
pytest Module/config/tests/ -v
pytest core/tests/test_phase3.py -v
```

## 测试目录索引

各目录的**测试数量不在此手写**——手写数字必然漂移（本节此前即因之全面过期）。取权威数字用：

```bash
# 全仓分层统计（各 marker 分布 + 合并门禁范围）
conda run -n Elysia python scripts/print_test_stats.py

# 单个目录的收集数
conda run -n Elysia python -m pytest <目录> -q --collect-only | tail -1

# 单个文件里每个测试实际带的 marker
conda run -n Elysia python -m pytest <文件> --collect-only -q -m "unit or service"
```

| 层 | 测试目录 |
|----|----------|
| `Module/` | `_common/tests/`、`config/tests/`、`database/documentdb/tests/`、`database/vectordb/tests/`、`embedding/tests/`、`llm/tests/`、`logger/tests/`、`output/tests/`、`stt/tests/`、`system_prompt/tests/`（含 `unit/`、`integration/`）、`tool/tests/`、`tts/tests/`、`workflow_contract/tests/`、`workflow_engine/tests/unit/`、`workflow_engine/tests/integration/` |
| `core/` | `tests/`、`status/tests/`、`agent/tests/`、`config/tests/`、`monitoring/tests/`、`task/tests/`、`text/tests/`、`debug/tests/`、`cognitive/tests/`、`desktop/tests/`、`input/tests/`、`memory/tests/`、`personality/tests/`、`prompt/tests/`、`schema/tests/`、`schema/adapters/tests/`、`storage/tests/`、`tools/tests/`、`adapter/tests/` |
| `frontend_contract/` | `tests/` |
| 仓库根 | `scripts/tests/`、`tests/` |

`frontend_contract/` 为前后端契约单源包（Pydantic 模型 + TS 生成器，详见
[frontend_contract/README.md](../frontend_contract/README.md)），测试为纯函数级（无 I/O、无 Agent）→
marker=unit。`make gen-types` 重新生成 `frontend/src/types/contract.generated.ts`，
`make check-types` 漂移门禁。

`scripts/check_architecture.py` 是**架构边界静态门禁**（ARCH-INC-P1-03，`make check-arch`）：
纯 AST、无外部依赖、秒级，覆盖依赖方向（`Module/`→`core/`、零依赖层反向依赖、领域层→`core.input`）、
跨包私有符号导入、eager import 环。存量泄漏在脚本 allowlist 逐项登记理由，**新增即红**；
`TYPE_CHECKING` 类型边允许，函数内懒导入是打断环的正当手段（R4 只算 eager，R1/R2/R3 仍算运行时依赖）。
其测试 `scripts/tests/test_check_architecture.py` 用合成 fixture 仓库验证 eager/lazy/type 三分
——该区分两个方向都踩过坑：全算 eager 会误报伪环，只算 eager 会漏报懒导入的真实泄漏。

## Marker 标准

### unit

- 只依赖 Python 标准库和被测模块
- 无 `asyncio`、无 mock 外部服务
- 不访问文件系统（tempfile 除外）、网络、数据库
- 不创建 Agent 实例

```python
# 模块级标记
pytestmark = pytest.mark.unit

# 或类级标记
@pytest.mark.unit
class TestMyFunction:
    ...
```

### service

- 单模块测试，mock 外部依赖（用 `unittest.mock`、`FakeLLMBackend` 等）
- 可能使用 `asyncio`；**可以**在进程内 `agent.start()`，前提是外部依赖全部换成
  测试替身（fake LLM + 内存 fake 存储，如
  `core/cognitive/tests/test_v2_generation.py` 的 `_make_agent_with`）——判据是
  **不连真实外部依赖**，而非「不得出现 `agent.start()`」
- 可以访问临时文件、mock 的数据库连接
- 创建 `Agent` 但不调用 `agent.start()` → service

```python
pytestmark = pytest.mark.service
```

### integration

- 需要真实 MongoDB/Milvus 连接
- 需要真实外部 API（LLM/TTS/STT）
- 不 mock 关键依赖

```python
pytestmark = pytest.mark.integration
```

### slow

- 创建 `Agent(config=AppConfig())` 并**连真实依赖**（真实 LLM/Mongo/Milvus）
- 完整管线端到端测试
- 单测耗时 >1s
- 大量 `asyncio.sleep()` 累积

> 与 `service` 的分界是**是否连真实外部依赖**：同样 `agent.start()`，依赖全是测试
> 替身则归 `service`（进合并门禁），连真实 LLM/DB 则归 `slow`（只由 `make test-full`
> 手工触发）。**禁止两者叠加**（`slow and service` 会把真 I/O 用例塞进快门禁，
> 见上文标记纪律的自查命令）。

```python
@pytest.mark.slow
class TestAgentLifecycle:
    ...
```

## 已知问题

- **`test_touch_resets_timer`** — ✅ 已修复：`IdleMonitor` 计时已改用 `time.monotonic()`（测试内保留 `time.sleep(0.001)` 仅作时间推进）。
- **`test_resume_idempotent_on_running`** — 已随 `integration/test_workflow_engine_lifecycle.py` 一并删除（2026-09-10）。该用例带 `external` 标记，默认与 `make test-full` 均跳过（只有 `make test-external` 会跑），从不构成持续观测；其语义（RUNNING 上调 `resume` 幂等）由 unit 层 `test_workflow_engine.py::test_resume_running_is_idempotent` 确定性覆盖。原记录的 WorkflowEngine 历史 race 若复现，在 unit 层定位即可。
- **`test_ok_returns_latency`** — ✅ 已修复：`ok_check(self)` 多余参数已移除（`Module/_common/tests/test_test_helpers.py`）。

## 添加新测试

1. 确定层级：对照上述标准，选择 `unit` / `service` / `integration` / `slow`
2. 加 marker：class 或 module 级别
3. 文件名：`test_<功能>.py`，放在对应模块的 `tests/` 目录下
4. 验证：`pytest -m <marker> 新文件路径 -v`
