# 测试指南

## 分层体系

**当前收集数不在本文维护**——它是易腐事实，历史上同时存在过 4985 / 5952 / 6002 / 6003 /
6450 / 6551 / 6576 七个版本的数字（分别出自不同日期的文档与快照）。要当前值：

```bash
conda run -n Elysia python scripts/print_test_stats.py
```

| 层 | marker | 标准 |
|---|--------|------|
| 单元 | `unit` | 纯函数/类，无 I/O，无 Agent |
| 服务 | `service` | 单模块 + mock/fake 替身，不连真实依赖 |
| 集成 | `integration` | 需真实数据库或外部 API |
| 慢速 | `slow` | 起 Agent 且连真实依赖 / 端到端 / >1s |
| 外部 | `external` | 需真实 LLM/DB 凭据（F1，独立 opt-in） |
| 真实库 | `real_mongo` | 真实 MongoDB 测试层（S2-R6-1；非凭据，走容器） |

> **口径说明（F1 起）**：`external` 独立于四层——真实 LLM E2E（WorkflowEngine 集成）不进合并门禁。
> 各层相加与全量的差异（多 marker 重复计入）以脚本输出为准。
>
> 历史上表里有一列"测试数"（如 `unit 3389` / `service 2585`），已删除：那一列没有门禁，
> 只在每次改测试时静默过期，而读者要的是"哪些层存在、各层判据是什么"。

### CI 现状：只监听 push 到 main（2026-09-16）

本仓当前**不开放 PR**，故 `ci.yml` 与 `contract-frames.yml` 都只监听 `push: [main]`，
`pull_request` 触发器与两个 PR 侧 job 已移除；全部 job 跑在 self-hosted runner 上。
**为什么（账单教训）、信任边界规则、以及开放 PR 时的完整恢复清单（含分支保护陷阱）
见 [ci.md](ci.md)**——那是唯一权威，本文不再复述以免两处清单漂移。

### 合并门禁的两个集合是**互斥**的（2026-09-14 CI 性能专项）

```bash
make test-quick       # pytest -m "unit or service"
make test-remainder   # pytest -m "not slow and not external and not unit and not service" → 0（2026-09-17 起）
make test-normal      # pytest -m "not slow and not external"（**真正离线**，2026-09-17 起）
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

**实测档位曲线**（本机 24 核；`-m "unit or service"`）。
两列是**同一天、同一台机器、同一命令**的两次扫描，中间只隔本轮的优化改动
（见下文「测试性能专项」，`--durations` 证据在同一批产出里）。
**两列 passed 数都是 6571**——即下表是纯耗时对比，没有靠少跑用例换速度：

| workers | 优化前 pytest / wall | 优化后 pytest / wall | 加速比（后） | 并行加速比（对串行 297s） |
|---------|---------------------|---------------------|-------------|--------------------------|
| 串行 | 463.7s / 470.5s | **297.3s / 304.0s** | **1.56×** | 1.0× |
| **`-n 8`（默认）** | 95.4s / 98.0s | **81.3s / 83.9s** | 1.17× | 3.7× |
| `-n 12` | 82.8s / 85.3s | **75.3s / 77.9s** | 1.10× | 3.9× |
| `-n 16` | 79.2s / 81.9s | **74.9s / 77.5s** | 1.06× | 4.0× |
| `-n 24`（≈`auto`） | 80.8s / 83.3s | **72.3s / 74.9s** | 1.12× | 4.1× |

**结论与取舍**：

- **串行收益（1.56×）远大于并行档位（1.06–1.17×）**，且这不矛盾：本轮消掉的主要是
  **与判据无关的固定等待**，它们在串行下逐条累加，在 `-n N` 下被并行吸收掉一部分。
  换句话说——**并行已经把等待藏起来了，串行没有**。所以"今天 `-n 8` 仍然要 84s"
  不等于"优化没生效"：真正的改善是 `-n 8` 的**关键路径**短了。
- **`-n auto` 仍不是答案**：24 worker 只比 12 快 ~3s（4%），代价是 CPU 时间从
  n12 的 449s 涨到 n24 的 **905s**（`/usr/bin/time` 的 user+sys）——2 倍 CPU 换 4% 墙钟。
  **CI 明确禁止 `-n auto`**：worker 数不应随 runner 核数漂移。
- 默认取 **8** 而不是更快的 12：12 比 8 快 ~6s（7%），而 8 个 worker 在"这台机器同时跑
  mongod / milvus / neo4j / 开发工具"的前提下留更多余量。要更快就 `PYTEST_N=12`。
- **CPU 已是瓶颈**：`-n 8` 实测 CPU 时间 371s / wall 84s ≈ **440%**（8 核的 55%），
  `-n 24` ≈ **1210%**——继续加 worker 的边际收益已被每进程 import 与调度开销吃掉。

#### 调度策略实测（`-n 16`，各一次，同日同机）

| `--dist` | wall | 相对 |
|-----------|------|------|
| `loadscope` | 72.3s | 1.00× |
| **`loadfile`（默认）** | **73.3s** | 1.01× |
| `worksteal` | 76.1s | 1.05× |
| `load` | 79.1s | 1.09× |

四者相差 <10%。`loadscope` 领先 `loadfile` **1.4%**，单次测量下落在噪声内，不足以推翻
`loadfile`——本仓多处测试共享"按文件划分的假替身/模块状态"（见下方「已知边界」），
`loadfile` 是**按语义**选定的，不是按速度。**无 benchmark 支撑，故不改。**

#### `-n 24` 那次少报 1 条：结论是「一次性偶发，未复现」（2026-09-14 RC 复核）

> **2026-09-21 补记（测试性能专项）**：本轮共跑 **8 次** `-n 12/16/24` 全量（优化前 4 次
> + 优化后 4 次）+ **6 次** `-n 12` 稳定性重跑，**每次报数都恰好 6571 passed**，与此前的
> 「未复现」结论一致。下方 2026-09-14 的原始记录保留（它记的是当时的选择集与证据）。

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
  `ELYSIA_PATH_SESSION_PENDING_CLEANUP`、`ELYSIA_PATH_TOKEN_PENDING`
  等 8 个 journal（这些 journal 的原子写用 `<path>.tmp`，同名文件在多进程下会互相
  覆盖/删除/串读）。**前提是路径在"使用时/构造时"解析**——见下条。
- **禁止 import 期固化路径**（2026-09-27 收口）：`ProjectPath.resolve()` 每次调用都读
  环境变量，所以 `ELYSIA_PATH_*` 覆盖能生效的**前提**是代码不在 `import` 时把
  `resolve()` 的结果存进类属性/模块常量。`TokenTracker._pending_file` 曾经就是这种写法
  （`data/token_usage_pending.jsonl` 在模块 import 时定格），后果是上面那套 env 隔离
  **对它完全无效**——测试可以写到仓库真实的运行期 journal。现已改为**实例构造时**解析
  （`core/monitoring/token_tracker.py`，含 `pending_file=` 显式注入参数），
  `ELYSIA_PATH_TOKEN_PENDING` 因此真正可用，隔离清单也补上了它。
  契约与回归判据：`core/monitoring/tests/test_token_tracker.py :: TestPendingFileLifecycle`
  （env A 建实例用 A、改到 B 建新实例用 B；默认路径不变；降级落盘真的写在该实例的路径上；
  根 conftest 的隔离确实作用到 tracker）。

### 测试里的环境变量：只走 `monkeypatch`

`os.environ` 是**进程级**状态。xdist 下每个 worker 是独立进程，所以"worker A 改了
worker B 也变"并不存在；真实风险是另一组：**同 worker 内跨测试污染**、**import 期捕获**
（上面那条）、**执行顺序依赖**、**subprocess 继承**。

规则：测试代码（`**/tests/**`、`test_*.py`、`conftest.py`）**不得裸写 `os.environ`**
（`os.environ[k]=v` / `setdefault` / `update` / `pop` / `clear` / `putenv` / `del`），
一律走 `monkeypatch.setenv` / `delenv`（用例结束自动还原）。需要"缺什么补什么"语义时，
写 module/session 级 autouse fixture + `monkeypatch`（范例：
`core/tests/test_phase3.py :: _fake_deepseek_key`）。

由 `core/tests/test_global_state_isolation.py` 守卫：全仓 AST 扫描 + **空的**豁免表
（新增裸写必须同时登记理由，登记项陈旧同样报红）。2026-09-27 收尾后全仓 0 处裸写；
同一批还补了扫描器的失败语义——枚举后**消失**的文件忽略（TOCTOU），但
`PermissionError` / 编码错误 / 真 parser 错误一律冒泡（"扫不了 ≠ 没写"）。


### 真实数据库防线（unit/service 不碰生产库）

`service` 的定义里写着「**不连真实 DB**」，但这条约定直到 2026-09-11 才被强制执行。默认配置
恰好指向开发者本机生产库：

```python
AppConfig().database.mongodb_uri = "mongodb://localhost:27017"   # core/config/app_config.py :: DatabaseConfig.mongodb_uri
AppConfig().database.db_name     = "default"                      # core/config/app_config.py :: DatabaseConfig
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

> **测试集合的残留行**（2026-09-28 起）：写真实 Milvus 的两处用例
> （`TestMemoryIntegration` / `TestLongTermMemory::test_insert_and_search_async`）挂
> `core/tests/conftest.py::fresh_ltm_collection`——**先清后跑**，不做收尾清理。
> 依据与实测数据见下文「`long_term_memory_v2__test` 的残留：结论与依据」。

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
| **禁止并行** | 根 `conftest.py` 的 `pytest_runtest_setup`：选中本层却并行 → `pytest.fail` + `shouldstop` | `-n 2` → 每个 worker 一条明确原因、零 DB 操作（见下节） |

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

### 共用真实资源的层禁止并行（2026-09-27）

两条约束此前**只写在 Makefile 注释与文档里**（"不要给 real_mongo 加 `-n`"），而
`make test-quick` 默认就是 `-n 8`、`PYTEST_N` 还能往上调——"大家记得别加"不是机制。

| 层 | 共用资源 | 并行下的破坏 |
|---|---|---|
| `tests/real_mongo/` | 一个真实 MongoDB 测试库 | 每个用例前后 `drop_database`，worker 互相抹库 |
| `slow`/`integration`/`external` 中会用真实 Milvus 的用例 | `long_term_memory_v2__test` 集合 | `TestDebugDbApi` drop + recreate 它，`TestMemoryIntegration` 做 `count+1`/召回断言（该用例另挂 `fresh_ltm_collection` **先清后跑**，避免残留累积——见下） |

**实测证据**（本轮复现）：把真实 Milvus 用例按文件分给 4 个 worker
（`--dist loadfile -n 4`）连跑 3 次，第 3 次 `test_search_memory_tool` 以
`ToolResult(success=False, error='执行超时 (30.0s)')` 失败——另一个 worker 的
`test_ltm_clear_all` 把集合 drop 掉了。

**判据（`core/tests/_pytest_parallel.py`）**：

- **并行度**只用 xdist 标准信号：worker 读注入的 `config.workerinput["workercount"]`，
  controller 读解析后的 `--numprocesses`/`--dist`。`-n 0`/`-n 1` 算**串行**——判据是
  "会不会有两个测试同时在跑"，不是"有没有起子进程"。
- **精确而非整层**：只有"可能读写共享真实 Milvus 集合"的用例才拒绝并行，条件与
  `_guard_real_database` 的换桩决策**同源**（非豁免层且未自带内存替身）。
  全仓 44 条 slow 里有 23 条请求了 `fake_storage`（内存替身），并行完全安全——一律禁掉是
  白白损失并行度。
- **挂点必须是 `pytest_runtest_setup`**（每条用例执行前）。三条备选路都实测失败过：
  ① `pytest_collection_modifyitems` 拿到的是 `-m`/`-k` 过滤**之前**的全量 → 会误伤
  `make test-quick -n 8`；② `tests/real_mongo/conftest.py::pytest_configure` 装在
  "conftest 被**加载**"上而不是"用例被**选中**"上 → `pytest -m "unit or service" -n 2 tests/`
  直接 `ERROR tests/real_mongo`；③ `pytest_collection_finish` 里 `UsageError`/`pytest.exit`
  会被 xdist 当成 **worker 崩溃** → 整轮退化成 `INTERNALERROR … assert not crashitem`，
  报错里夹着一个无辜用例的 nodeid。
- **失败形态**：`pytest.fail(..., pytrace=False)` + `session.shouldstop = True`
  ——正常的 setup error（实测 `-n 2` 下报 `2 errors`、3.4s 结束、**无 INTERNALERROR**），
  且每个 worker 只报**一条**（没有 `shouldstop` 时 40 条用例会各报一遍同一段原因）。

**验证入口**：

```bash
make test-real-mongo                        # 串行 → 正常跑（容器起来时 46 passed）
pytest tests/real_mongo -n 2                # 未 opt-in → 46 skipped（该层自己的 opt-in 门先拦）；
                                            # 带 RUN_REAL_MONGO_TESTS=1 → 拒答，退出码非 0、原因写明
pytest -m slow -n 2 core/tests/test_phase3.py::TestDebugDbApi   # → 拒答（Milvus 那条）
pytest -m slow -n 2 core/input/tests/test_server.py             # → **不拒答**（10 条 slow 都带
                                            # fake_storage，内存替身、并行安全）——精确判据的反证
make test-quick                             # → 必须**不**被拒答（-m 不选这两层）
```

> 档位数字的**测量方式**（照抄会过期）：`SLOW_TOTAL` / `FAKE_STORAGE` 由遍历
> `item.fixturenames` 现算，不是维护在文档里的常量。2026-09-28 实测
> **44 / 23**（另一侧 21 条是"可能读写共享真实 Milvus"的候选）。数字属于描述，
> 判据在 `core/tests/_pytest_parallel.py` 的 `uses_shared_test_milvus()`。

**边界（有意）**：判据是"这次运行**会真的执行**共用真实资源的用例吗"，不是"选中了吗"。
整层被 `skip` 时（未 opt-in）没有可拒的风险，故不拒答——那条路径上该层自己的 skip 原因
已经写明"未设置 `ELYSIA_TEST_MONGODB_URI`"。**不**把自己的 hook 标 `tryfirst` 去抢在
skip 判定之前，因为那依赖 pluggy 对 `tryfirst` 实现的注册顺序，是脆弱假设。

判据与反向判据都由 `core/tests/test_pytest_parallel_guard.py`（unit，23 条）与
`core/tests/test_parallel_guard_e2e.py`（slow，子进程跑真实 CLI，5 条）钉住；后者含
**反向判据**——"`-m "unit or service" -n 2` **不得**被误伤"，因为本批第一版实现正是在
这里把 `make test-quick -n 8` 弄红的。

> **Milvus 为什么选 Guard 而不是 worker 级 collection 隔离**：这层的真实 Milvus 用例
> 只有 8 条、只在 `make test-full`（串行、无自动执行点）里跑，并行化收益为零；
> 而 worker 隔离要额外承担 collection 泄漏（结束残留 `__test_gw0…` 需 session 级清理）
> 与清理时序（哪个 worker 有权 drop）的风险。按"能用机制保证的不变量不依赖文档"，
> 一个**精确**的 guard 已经足够；真要做隔离，`install_test_milvus_collection` 加
> worker 维度即可（`__test` 后缀必须保留，它是生产集合的唯一区分手段）。

#### `long_term_memory_v2__test` 的残留：结论与依据（2026-09-28）

**集合名是固定的**（不是每个 worker 一份），所以"collection 数量无限增长"不成立；
真正会增长的是**行数**。实测（直接 `query` 集合，不信 `get_collection_stats` 的
`row_count`——它在 flush 前恒为 0，会把残留读成"干净的"）：

```
pytest core/tests/test_phase2.py::TestMemoryIntegration   跑 1 次 → 2 行
                                                          连跑 4 次 → 8 行
```

`TestMemoryIntegration` / `TestLongTermMemory::test_insert_and_search_async` 都往
测试集合写条目且从不删除，于是**每次 targeted run 单调增长**。后果有两条，第二条更要紧：

1. `test_save_memory_tool` 的 `top_k=20` 召回会随条目增多而**越来越 flaky**——
   嵌入被根 conftest 的 `_no_real_embedding` 换成常量 `[0.1]*1024`，所有向量相同 ⇒
   排序无意义、返回哪 20 条是任意的，新写的那条可能被挤出；
2. `any("测试保存" in r.text …)` / `"香山"` 断言可被**上一轮的残留**满足 ⇒
   真 insert 断了也照绿（假绿）。

**决策：加 cleanup，但只加"先清后跑"，不加 session 收尾清理。**

| 判据（§十九的决策规则） | 本方案 |
|---|---|
| 实现简单 | `core/tests/conftest.py::fresh_ltm_collection`，约 20 行，只被需要的用例请求 |
| 生命周期明确 | fixture 作用域内、**测试集合**上；调用方是那两条真实 Milvus 用例 |
| 不削弱生产隔离 | 清理前**断言** `MemoryRepositoryV2.COLLECTION` 以 `__test` 结尾（fail-closed：断言在前、drop 在后），防线失效时直接红而不是 drop 生产集合 |
| 不引入 flaky teardown | **没有 teardown**——清在用例之前。CI 被 kill 不留"清理没跑"窗口；Milvus 不可达时失败是本用例的正常 setup error（它本来就要真库） |
| 无 worker ownership 问题 | 这些用例已由并行 guard 保证串行 |

实测（fixture 落地后连跑 4 次）：集合稳定在 1 行、无 setup/teardown 抖动。
`TestDebugDbApi::test_ltm_clear_all`（它自己 drop + recreate 该集合）不受影响。

## Required pre-push verification（提交前的唯一权威清单）

**这一节是"提交前该跑什么"的唯一权威**；`CLAUDE.md` 只保留指向这里的短摘要。
清单不是照抄愿望，而是**从 `Makefile` 与 `.github/workflows/` 反推**出来的——
下面每一条都对应一个真实存在的 required 执行点（push 到 `main` 时必须有东西跑它）。
改了 workflow 或 Makefile 就回来同步这一节。

### A. 静态门禁（全部秒级～分钟级，CI `backend` job 逐步跑）

```bash
conda activate Elysia

make lint            # ruff：导入风格（禁相对导入 TID252）+ 生产代码参数注解
make pyright         # pyright 全仓生产代码（core/ + Module/ + frontend_contract/ + scripts/）
make check-types     # 契约漂移：ts/schema/raw_ts + golden frames + 错误码目录（三条命令）
make check-arch      # 架构边界 R1/R2/R3/R4/R6（依赖方向 / 私有符号 / eager 环 / 裸 spawn 登记）
make check-timeouts  # 具名超时预算必须表态（外层 ≥ 内层 / 叶子 + 理由）
make check-contract  # 契约可达性（枚举成员必须有生产者、字段有读必须有写）
make check-debug-auth        # /debug/api 只认 SERVER_AUTH_TOKEN，SSE 走 debugFetch
make check-ci-isolation      # CI 信任边界（PR 可达 job 禁 self-hosted / 禁死 job / fail-closed 清理）
make check-secrets           # 被跟踪文件里的凭据形态（CR-01）
make check-test-sets         # Quick ∪ remainder == Normal 且交集为空（约 35s，3 次收集）
make check-compose           # docker-compose 安全策略（仅 loopback、无默认凭据、生产不发布管理端口）
make check-env-lock          # requirements*.txt ↔ environment.lock.yml 无漂移
make check-docs              # 文档守卫 DG1–DG5（链接可解析 / 无失效引用 / 错误码双向 / core README 覆盖 / env 索引一致）
make audit                   # pip-audit（0 未豁免漏洞）+ bandit（Medium+ 为 0）
```

> ⚠️ **`make check-full` 不是"全部门禁"**——它是 `app.py --check --full`
> （环境预检 + 5 项连通性探活）。别拿它当本节的聚合入口；本节没有单条聚合命令，
> 需要就照上面顺序逐条跑（或直接看 CI 的 `backend` job 步骤，顺序与判据同源）。

### B. 测试层（离线集是 CI 唯一跑的测试档位）

```bash
make test-quick       # 日常迭代：unit + service（-n 8）
make test-normal      # **提交前必跑**：not slow and not external（真正离线；CI 只跑这一遍）
make test-full        # 含 slow；无自动执行点（nightly.yml 手工 workflow_dispatch）
make test-external    # 真实 LLM/DB（RUN_EXTERNAL_TESTS=1，**刻意 opt-in**，要凭据与费用）
```

真实 MongoDB 层（CI `real-mongo` job，与本清单同一条路）：

```bash
docker compose -f docker-compose.test.yml up -d   # 127.0.0.1:27018，无 volume，无 auth（仅测试）
make test-real-mongo                               # RUN_REAL_MONGO_TESTS=1 + ELYSIA_TEST_MONGODB_URI
docker compose -f docker-compose.test.yml down --remove-orphans
```

> `RUN_REAL_MONGO_TESTS=1` 的语义是"**连不上就 fail**"（不是 skip）——容器没起好会变成
> 一串连接错误，所以先等 healthy。库名必须含 `test`：这一层会 drop 整个库。

### C. 前端（CI `frontend` job 的三步，缺一不可）

```bash
cd frontend
npm ci                # 严格按 package-lock.json（不要用 npm install）
npm run build         # = vue-tsc --noEmit && vite build（类型检查 + 构建）
npm test              # = vitest run src（store 层不变量；build 只做类型检查、零行为覆盖）
```

Node 版本：CI 钉 **22.x（LTS 线）** 并断言主版本，漂移即红——精确 pin 与本地版本对齐方式见
[docs/operations.md §11](operations.md)（版本权威只有那一处）。
本仓**没有** `.nvmrc`/`engines` 字段——本地版本不会自动对齐 CI，请自己确认
（见 [operations.md](operations.md) 的版本权威表）。

### D. 契约产物（改了 `frontend_contract/` 才需要）

> **纯 `docs/**` 提交不会触发 `ci.yml`**（它带 `paths-ignore: docs/**`）——那类提交由
> `contract-frames.yml` 覆盖（它的 `paths` 含 `docs/**`，job 里跑 `make check-types` 三条命令
> 与 `make check-docs`）。所以**只改文档**时，真正会跑的是那个名字看起来只管契约的 workflow；
> 原因与权衡见 [ci.md](ci.md) 的「门禁接线的第二种形态」。

```bash
make gen-types        # 重新生成 ts / schema / raw_ts
make gen-frames       # 重新生成 golden frames
make check-types      # 再确认无漂移（与 A 节同一条命令）
```

### 交付判据

> **真实 CI required jobs 全绿**才算完成——本地清单是它的等价预演，不是替代品
> （本仓 CI 只监听 `push: [main]`，见 [ci.md](ci.md)）。

## 日常命令

```bash
conda activate Elysia

# 日常开发（unit + service；串行 ≈4min57s，xdist 默认 -n 8 后 ≈1min21s）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short -n 8 --dist loadfile

# 提交前 — 排除 slow/external（**真正离线**；2026-09-21 起 Quick == Normal）
#          当前规模 Quick = Normal = 6665 条、全量收集 6771 条（2026-09-28
#          `make check-test-sets` 实测；数字会随新增测试变，**以该门禁的输出为准**）
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

> **时间数字的来源**：上表的数字来自 **2026-09-21 本机实测**（24 核、空载、同一命令的
> 前后对照，见上文「并行执行」的档位表）。CI 侧的耗时以 runner 自身日志为准，两者不要
> 混用——本机 24 核且 CPU 已是瓶颈（`-n 8` 实测 ≈440%），核数不同的 runner 曲线会不同。

## 测试性能专项（2026-09-21）

一次针对 `-m "unit or service"`（当时 6575 条）的耗时审计与优化。**判据是 `--durations`
的实测数字**，每条改动都跑过前后对照；**没有**减少用例、放宽断言、隐藏失败或加重试。

### 找到并消掉的四类「与判据无关的等待」

| # | 位置 | 代价（实测） | 处置 |
|---|------|-------------|------|
| 1 | `Module/workflow_engine/tests/unit/test_workflow_engine.py::test_pause_sets_pause_requested` | 单条 **30.06s** | 判据只是 `pause_requested is True`（`lifecycle.pause()` 在**进入轮询前**同步置位），却不传 `timeout` → 用生产默认 30s，且没有 orchestrator 会去确认 PAUSED → 必然跑满。改为显式 `timeout=0.5`。确认路径仍由 `test_pause_confirmed` / `test_pause_timeout` 覆盖。**30.06s → 0.51s** |
| 2 | `core/tests/test_phase3.py::TestAudioBufferErrors::test_stt_timeout` | 单条 **30.13s** | 判据是「超时 → 整段丢弃、不触发回调」，与预算多大无关，却用生产默认 `STT_TIMEOUT=30.0`。`AudioBuffer` 新增 `stt_timeout` 构造参数（**生产默认不变**，仍 30s），测试注入 0.5s。**30.13s → 0.61s** |
| 3 | `core/cognitive/tests/test_v2_generation.py` 的 5 条 `fault="http_503"` 用例 | 各 **7.13–7.14s** | 退避 `_RETRY_BACKOFF_BASE=1.0` × `_MAX_RETRIES=3` = **1+2+4=7s** 的真实等待；判据是「重试耗尽后发终态」，与退避时长无关。`LLMAdapter` 新增 `retry_backoff_base` 构造参数 + `set_retry_backoff_base()`（**生产默认不变**），测试注入 0。**重试次数与代码路径完全不变** |
| 4 | `core/input/tests/test_v2_ws.py::_drain` / `test_desktop_ws.py::_connect` | 3× **5.04s** + 5× **3.04s** | 两个 helper 的终止判据要求"看到某个特定握手帧"，而那帧在 v2 下**根本没上 wire** → 空转到 5s/3s 超时才靠 `TimeoutError` 退出。见下节 |

### 建连帧顺序：把终止判据改成语义的

`core/input/ws_handler.py` 的建连帧是**背靠背同步**发出的，顺序固定：
`session` → `server_info`(v2)/`capabilities`(v1) → `expression`。三者之间没有 await 让出点。

- `_drain`：终止集从 `{capabilities, server_info}` 扩到 `{session, capabilities, server_info}`
  ——收到**第一帧**即判定建连序列已开始。若 `session` 都没来，行为与之前完全一致。
- `_connect(collect=True)`：`break` 条件从 `seen_cap and want_command and ...` 改为
  `seen_cap and (not want_command or d["command"] == want_command)`
  ——只要 cap 帧的调用方不再空转到 3s 超时。有 `want_command` 时语义不变。

效果：`test_v2_ws.py` 27.6s → **12.5s**，`test_desktop_ws.py` 15.2s → **2.5s**。

### v2 `server_info` 静默缺失：根因与修复（2026-09-27 测试隔离专项）

**现象**：`_drain` 等不到 `server_info` → 空转到 5s 超时。当时的结论是"该帧在这条路径上
根本没上 wire"。**实际不是**——用真 Agent 建连实测，v2 建连帧是
`session → server_info → character.state.changed → expression`，帧正常。

**根因**（实测复现，非推断）：`core/services/capabilities.py :: build_server_info` 裸读
`agent.tts.model`（以及 `agent.stt.model`）。`model` 是 `Module/{tts,stt}/protocol.py`
的协议成员，而测试里的 `_FakeTtsBackend`（`core/input/tests/test_v2_ws.py`）**只实现了
`list_voices`**，于是构造 `server_info` 时抛：

```
AttributeError: '_FakeTtsBackend' object has no attribute 'model'
```

该异常被建连发送点 `core/input/ws_handler.py` 的 `except Exception: logger.debug(...)`
吞成**一行 debug** ⇒ 客户端**永远收不到 server_info**，日志里毫无痕迹。

**为什么以前没被发现**：① 生产后端（`TTSBackend` 子类）都带 `model` 的默认实现，只有
测试替身违反协议；② 唯一的观测点是"客户端等帧超时"，而当时的测试把超时当作"排空建连帧
的正常出口"（等不到也继续）——**"协议帧缺失"与"没有更多帧"在测试里长得一模一样**。

**修法（三层，缺一不可）**：

| 层 | 改动 |
|---|---|
| 不让字段缺失升级成整帧丢失 | `build_server_info` / `build_capabilities` 的后端模型读取收口到 `_backend_model()`：读不到 → 该字段降级为 `null` + `logger.warning`（指名缺失的协议成员），**绝不抛** |
| 让异常可观察 | 建连发送点的 `except` 从 `logger.debug` 升为 **`logger.error(..., exc_info=True)`**——`server_info`/`capabilities` 是协议必须帧，本连接内不可恢复，属"必须上报"而非"可忽略噪声" |
| 让测试替身守协议 | `_FakeTtsBackend`（`core/input/tests/test_v2_ws.py`）改为继承标准替身 `Module/tts/testing.py::FakeTTSBackend`——它补齐的不只是 `model`，还有当时同样缺失、失败被 shutdown 步骤吞掉的 `close()` |

**防回归判据**：`core/input/tests/test_v2_ws.py :: TestV2HandshakeProtocolFrames`（3 条）
——① v2 建连必须收到 `session → server_info → … → expression`（**缺失即 fail**，不再
"等不到就继续"）且不得出现 `capabilities`；② v1 必须收到 `capabilities`、不得出现
`server_info`（钉住"v1 行为不变"）；③ 故意违反协议的后端（无 `model`）下**必须帧仍到达**、
字段降级为 `null`、且有 warning 指名协议成员。

协议口径见 [electron-backend-protocol-v2.md](electron-backend-protocol-v2.md)。

### 后端协议的两层强制：运行期韧性 ≠ 契约强制（2026-09-28）

`server_info` 那次修复后留下的问题是：**`_backend_model()` 的降级会不会变成"backend
可以不守协议"的长期兜底**。答案是把两件事彻底分层——

| 层 | 问题 | 手段 |
|----|------|------|
| 开发/测试 | backend 违反 Protocol，**必须尽早失败** | ① `make pyright` 的 `reportAbstractUsage`；② contract test（下节） |
| 生产运行 | 坏 backend 真的漏进来，**协议必须帧不许消失** | `_backend_model()` 降级 + 分级日志 |

**静态层实测覆盖到哪（`typeCheckingMode: basic` + 排除 `**/tests/**`）**：

- ✅ 抓得住：继承者**漏实现抽象成员** → `Cannot instantiate abstract class`（实例化点即红）。
- ❌ 抓不住（实测均报 `0 errors`）：把 `async def` 覆写成同步 `def`、把 `model` 覆写成
  返回 `None`、把关键字参数 `character` 改名——**且测试替身整片在静态视野之外**
  （`pyrightconfig.json` 的 `exclude: ["**/tests/**"]`）。

所以补一层运行期可执行的契约检查：

| 构件 | 作用 |
|------|------|
| `Module/_common/protocol_contract.py` | `public_members(protocol)`（从协议类 `dir()` 推导，不手抄）+ `violations(obj, protocol)`——逐项检查成员存在性与**形态**（property 声明 ⇒ 实现要能读出值；`async` 声明 ⇒ 实现得是协程/异步生成器） |
| `Module/tts/tests/test_tts_contract.py`、`Module/stt/tests/test_stt_contract.py` | 对**全部 5 个生产后端**（经工厂与直接构造两条路径）断言协议面 + `model` 语义 + `synthesize_stream`/`transcribe_stream` 真的是异步生成器 + **构造不触网**（socket 守卫）+ 守卫非空转 |
| `Module/tts/testing.py::FakeTTSBackend`、`Module/stt/testing.py::FakeSTTBackend` | **标准合法替身**：继承 ABC 且实现全部成员，子类只 override 关心的那几个 ⇒ "漏字段"不再是少写一行 |
| `core/services/tests/test_backend_model.py` | `_backend_model()` 的四种情形：缺成员（WARNING）/ 空串（合法、静默）/ 非字符串（WARNING）/ **getter 自己抛异常（ERROR + traceback）** |

**两条判据必须同时存在**（它们回答两个不同问题）：

```
契约测试   ：违反协议 ⇒ 这个对象**不合法**（violations() 非空 ⇒ FAIL）
运行时韧性 ：非法对象真漏进系统 ⇒ server_info **仍然必须发出**，字段 null + warning
```

删掉后者会让"一个字段读不到 = 整帧消失"复辟；删掉前者则降级会变成长期兜底。

> **`_backend_model()` 为什么把 getter 异常单列**：`getattr(obj, "model", default)`
> 会把「成员不存在」与「property getter 内部抛 `AttributeError`」**都**变成 default——
> 于是 getter 的真 bug 被误报成"后端没实现协议成员"，两种情形用同一行 WARNING，
> 无法区分。现在先直接取（单参 `getattr`，异常照抛），只在 `AttributeError` 时用
> 静态声明判定（`_declares_model()`）；getter 真抛异常走 **ERROR + `exc_info`**。
> 降级本身保留（帧不许消失），但**不再与契约违规共用一条日志**。

### 消除 eager 的 torch/transformers 导入（每个进程 −3.8s）

`Module/embedding/registry.py` 顶部 `from Module.embedding.huggingface import HuggingFaceBackend`，
而 `huggingface.py` 顶层 `import torch` + `from langchain_huggingface import HuggingFaceEmbeddings`。

传导链：`core.input.__init__`（re-export `create_app`）→ `core.input.server` → `core.agent`
→ `core.agent._lifecycle_persistence` → `Module.embedding` → … → **torch + transformers**。

实测（冷解释器，`-X importtime` 交叉验证）：

| 导入 | 优化前 | 优化后 |
|------|-------|-------|
| `Module.embedding.registry` | 4.216s | **0.841s** |
| `core.agent` | 5.648s | **1.868s** |
| `core.input` | 5.483s | **1.910s** |

代价**不止**于 worker 启动：本仓有 37 处 subprocess，其中多条**正是验 import 期语义的**
（`test_cli_overrides` / `test_upload_dir_isolation` / `test_dotenv_side_effect` /
`test_ready_identity_truthfulness` / `test_external_optin_gate`），无法用 mock 替代——
它们各自起一个全新解释器，于是每条都白付这 3.8s。实测那些用例 6.2s → 2.3s，
`test_RHT7` 12.9s → 4.5s。

修法：`registry.py` 把该导入移进 `create()` 的 `provider == "huggingface"` 分支
（`Module/config/preflight.py` 第 426 行早已是同形状的懒导入，本处只是漏了）。
**语义不变**——走到那个分支就一定会用到它。

### 测试基建：把"按让出次数计"的等待改成"按墙上时钟计"

`core/agent/tests/test_runtime_epoch_input_ownership.py` 的 `_pump_until` / `_drain_turns`
此前是 `for _ in range(4000): await asyncio.sleep(0)`。`sleep(0)` **不消耗真实时间**，
4000 次让出在空闲机器上几十毫秒就跑完——期间只要被测消费者有一次真实 await
（磁盘写 / 线程池 / 锁），谓词就没机会变真。

实测（`-n 12` 稳定性重跑）：8 次里偶发失败 1 次，报
`AssertionError: 等待超时：新 epoch 处理新输入`；同类 15 次对照全绿 ⇒ **既有的负载敏感
脆弱点**（该文件未被本批改动过）。改为 **10s 墙上时钟预算 + 10ms 轮询步长**
（与 `test_v2_generation.py::_wait_until` 同形状）：判据与失败信息不变、快路径不变
（谓词本就为真时零 sleep）、不掩盖真实回归（谓词恒假仍报同一个 AssertionError）。
改后 **6/6 次稳定性重跑全绿**。

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
| `core/tests/test_phase3.py::TestAudioBufferErrors::test_stt_timeout` | 30.1s | 桩 sleep 99s，测试等真实预算 `timeout=30.0`（`core/input/audio_buffer.py :: AudioBuffer._process_segment`） |
| `Module/workflow_engine/tests/unit/test_workflow_engine.py::test_pause_sets_pause_requested` | 30.1s | **unit** 却烧掉 `pause()` 默认 `timeout=30.0`（`Module/workflow_engine/workflow_engine.py :: WorkflowEngine._run_and_cleanup`），而该断言并不需要它 |

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
pytest core/tool_runtime/tests/ -v         # 即时工具调度（原 core/task，ARCH-INC-P1-01 改名）
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
| `core/` | `tests/`、`status/tests/`、`agent/tests/`、`config/tests/`、`monitoring/tests/`、`tool_runtime/tests/`、`text/tests/`、`debug/tests/`、`cognitive/tests/`、`desktop/tests/`、`input/tests/`、`memory/tests/`、`personality/tests/`、`prompt/tests/`、`schema/tests/`、`schema/adapters/tests/`、`storage/tests/`、`tools/tests/`、`adapter/tests/` |
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
