"""根 conftest —— ① pytest 入口的环境引导（批 1.5）+ ② 真实数据库访问防线（2026-09-11）。

## ① 环境引导：`.env` 由**入口**加载一次（批 1.5 / 2026-09-17）

pytest 是测试的**进程入口**，所以"环境从哪里来"由本文件负责——这是刻意的引导，
不是副作用：

    app.py / __main__ / 脚本 / pytest(本文件)
              ↓  load_dotenv() 一次
         ConfigLoader （只读 os.environ）
              ↓
           其余系统

批 1.5 之前 `ConfigLoader.load()` 自己在库路径上调 `load_dotenv()`，于是：

- 库 API 隐式改进程全局环境（调用方看不见、关不掉）；
- `EnvManager._loaded` 是**一次性类级开关** → "删掉键 K 再 load()，K 会不会从 .env
  被读回来"取决于**本进程此前有没有人 load 过**。实测（`tmp/batch1_5_probe/probe.py`
  Probe C）：同一次 `ConfigLoader(AppConfig).load()`，`_loaded=False` 时把
  `DEEPSEEK_API_KEY` 读回来、`_loaded=True` 时不读——同一段测试代码在进程内第一条与
  第 N 条下结果不同，`pytest -n N` 下每个 worker 的历史还各不相同（假绿/假红）。

现在：库只读环境（`Module/config/loader.py` 的模块 docstring），本文件在**收集任何测试
之前**（conftest 先于测试模块被 import）显式加载一次，记录在 `_DOTENV_BOOTSTRAP` 里，
测试可用 `dotenv_bootstrap` fixture 断言"入口确实喂过环境"——否则"库没复活被删的键"
会退化成"本来就没人读过 .env"，是**假绿**。回归网见
`Module/config/tests/test_dotenv_side_effect.py`。

## ② 真实数据库访问防线（2026-09-11）

### 问题

`pytest.ini` 把 `unit` / `service` 定义为「不连真实 DB」，但默认配置恰好指向开发者
本机生产库（`AppConfig().database.mongodb_uri = "mongodb://localhost:27017"`、
`db_name = "default"`）。任何 `Agent(config=AppConfig())` + `await agent.start()` 的
unit/service 测试，只要本机 Mongo 在跑就会连上并写入——且 `agent.start()` 对连不上库
**静默降级**，所以「写生产库」与「没写」两种情况下测试都绿，问题从不暴露。

实测污染（2026-09-11 核查 `default` 库）：`sessions` 标题 `active`/`lease`/`burst`、
`character_versions.session_id="test_default_type"`；2026-09-10 复盘曾清过一次，
**测试跑完后再次出现**。

### 防线

**autouse `_guard_real_database`**：凡未标 integration/slow/external 的测试，把**指向生产库**
的 Mongo 连接导流到内存 `FakeDocumentDBBackend`、把 Milvus 客户端拦截为抛错桩。

- 判据是**实际连接串**（`AppConfig().database.mongodb_uri`），不是只看 provider_id——
  测试若显式把 `MONGODB_URI` 改到别处（例如指向不可达端口以验证失败分支），不受影响。
- **导流而非判失败**：单元/服务层本就不该连真库，静默导流既消除污染、又不必为上百个
  既有测试逐一加标记。运行结束时终端会汇总被导流的次数，保持可见。
- 需要「防线别插手」的测试（如注入自有 fake 并驱动真实 `connect()` 路径）可请求
  `no_storage_guard` fixture；需要显式声明隔离的用 `fake_storage`。

背景与实测证据详见 `core/tests/_real_db_guard.py` 的模块 docstring。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:  # 保证 `core.tests._real_db_guard` 可导入
    sys.path.insert(0, str(_ROOT))

from core.tests._real_db_guard import (  # noqa: E402
    install_blocked_milvus,
    install_fake_mongo,
    install_test_milvus_collection,
    is_exempt,
)


def _bootstrap_dotenv() -> dict[str, object]:
    """测试入口的 .env 引导：**每个 pytest 进程恰好一次**（模块级调用，见文件 docstring ①）。

    返回可断言的记录——`load_dotenv(override=False)` 对已存在的键不留痕迹，
    所以"注入了哪些键"必须在这里现算，否则测试无法区分"入口喂过"与"根本没读"。

    Returns:
        dict: ``{"attempted", "loaded", "path", "keys_added"}``。
    """
    from Module._common.env import ensure_dotenv

    before = set(os.environ)
    path = ensure_dotenv()
    return {
        "attempted": True,
        "loaded": path is not None,
        "path": str(path) if path is not None else None,
        "keys_added": sorted(set(os.environ) - before),
    }


_DOTENV_BOOTSTRAP: dict[str, object] = _bootstrap_dotenv()
"""pytest 入口的 .env 引导结果（供 `dotenv_bootstrap` fixture 与诊断）。"""

_REDIRECTED_TESTS: set[str] = set()
"""被防线导流过的测试 nodeid（供 session 结束汇总）。"""


_FAST_LAYER = frozenset({"unit", "service"})
"""快门禁层——`make test-quick` / PR 门禁的范围。"""

_SLOW_LAYER = frozenset({"slow", "integration", "external"})
"""慢/外部层——放宽到 `-m "not slow and not external"` 才进来。"""

_LAYER_MARKERS = _FAST_LAYER | _SLOW_LAYER


class TestMarkerDisciplineError(RuntimeError):
    """标记纪律（docs/testing.md）被破坏。"""


def pytest_collection_modifyitems(config, items):
    """标记纪律守卫——`docs/testing.md`「标记纪律」的可执行化（2026-09-14）。

    ## 为什么是 hook 而不是一个 test

    hook 在**每个** pytest 进程里都跑（含 `pytest -n N` 的每个 xdist worker），
    而 test 只会被派给**一个** worker。写成 test 的话，xdist 下它只看到自己那个
    分片——`--dist loadfile` 时甚至只看到它自己——断言平凡成立，是**假绿**。

    实测（pytest 9.1.1）：普通（非 `trylast`）的 `pytest_collection_modifyitems`
    在 `-m` 过滤**之前**收到全部条目——`-m "unit"` 下本 hook 看到的条目数**恰好等于**
    不带 `-m` 时的全量数（用临时探针打印 `len(items)` 验证过）。所以判据是全量拓扑，
    不依赖 `--dist` 模式，也不依赖选中的 marker 子集（这里刻意不写死条目数——
    那种数字必然漂移）。

    ## 两条判据

    ① **每个条目至少带一个分层 marker**。无 marker 的测试不在
       `-m "unit or service"` 里 → 被 `make test-quick` / PR 门禁静默跳过。
       实测漂移：71 条（9 个文件）已修复；docs 里那条自查命令此前只能靠人跑。
    ② **不得同时带快门禁层与慢/外部层 marker**。这是
       `Quick ∪ remainder == Normal` 成立的**充要前提**（推导见
       `scripts/check_test_sets.py` 模块 docstring）：一条 `service and slow`
       就会把真 I/O 用例塞进快门禁，而 pytest 的 marker 只增不减。

    判据失败 → 收集阶段报错。**退出码不是契约**：串行下是 4（`pytest.UsageError`），
    但 `pytest -n N` 下 xdist 会把它转成 `INTERNALERROR … assert not crashitem`、
    退出码 3——所以失败原因**显式打 stderr**（见下面的 `print`），否则在 xdist 下
    控制台只剩一个无辜测试的 nodeid。
    **刻意让整仓 pytest 不可运行**：marker 是分层体系的唯一凭据，一个漏标就能让
    门禁出现静默空洞，而修法是加一行装饰器。
    """
    unmarked: list[str] = []
    mixed: list[tuple[str, list[str]]] = []
    for item in items:
        names = {m.name for m in item.iter_markers()} & _LAYER_MARKERS
        if not names:
            unmarked.append(item.nodeid)
        elif names & _FAST_LAYER and names & _SLOW_LAYER:
            mixed.append((item.nodeid, sorted(names)))

    problems: list[str] = []
    if unmarked:
        problems.append(
            f"标记纪律①：{len(unmarked)} 个测试没有任何分层 marker"
            f"（unit/service/integration/slow/external 至少一个）——它们会被"
            f"`make test-quick` 静默跳过、PR 门禁对它们不设防。"
            f"前 10 个：{unmarked[:10]}")
    if mixed:
        problems.append(
            f"标记纪律②：{len(mixed)} 个测试同时带快门禁层与慢/外部层 marker"
            f"——会破坏 `Quick ∪ remainder == Normal` 的互斥性（真 I/O 用例"
            f"混进快门禁）。前 10 个：{mixed[:10]}")
    if problems:
        message = (
            " " + "\n ".join(problems)
            + "\n（判据与推导见 scripts/check_test_sets.py；标记标准见 docs/testing.md）")
        # **必须显式打 stderr**：`pytest -n N`（现在是 `make test-quick`/`make test-normal`
        # 的默认路径）下，worker 收集期抛异常会被 xdist 转成
        # `INTERNALERROR> … assert not crashitem` —— 退出码 3，而**异常消息整段丢失**，
        # 报错里出现的是一个无辜测试的 nodeid。实测：不打印时 `grep 标记纪律` = 0 命中。
        # 打印后 xdist 会把它转发到控制台（每个 worker 一份），退出码仍是 3（不是 4）——
        # 串行下是 4。**退出码不是契约，可读的失败原因是。**
        print(message, file=sys.stderr, flush=True)
        raise pytest.UsageError(message)


@pytest.fixture(scope="session")
def dotenv_bootstrap() -> dict[str, object]:
    """pytest 入口的 .env 引导记录（批 1.5，见文件 docstring ①）。

    结构与用途（`Module/config/tests/test_dotenv_side_effect.py` 消费）：

    - ``attempted`` —— 入口**确实**执行过引导（模块级，恒 True；为 False 只可能是
      conftest 引导被删掉，那正是要红的情形）；
    - ``loaded`` / ``path`` —— `.env` 是否找到、在哪；
    - ``keys_added`` —— 引导**实际注入**的键（现算：`override=False` 对已存在的键
      不留痕迹，事后无法反推）。

    存在的意义：让"库没把被删的键读回来"这类断言可证伪——没有 ``keys_added``，
    "没读到"与"没人在读"无法区分（假绿）。
    """
    return _DOTENV_BOOTSTRAP


@pytest.fixture
def fake_storage(monkeypatch):
    """把生产 Mongo 导流到内存 fake、Milvus 拦截为桩——「本测试不碰真实库」的显式声明。

    适用于需要 `agent.start()` 跑完整管线、但不应写生产库的 unit/service 测试。
    请求本 fixture 的测试由 autouse 防线放行（见 `_guard_real_database`）。
    """
    install_fake_mongo(monkeypatch)
    install_blocked_milvus(monkeypatch)
    install_test_milvus_collection(monkeypatch)


@pytest.fixture
def no_storage_guard():
    """本测试**自行管理**存储连接，请 autouse 防线让位。

    用于注入自有 fake（如 `_DurableFakeDB`）并驱动真实 `connect()` 路径的测试——
    防线的导流会改变其语义。

    ⚠️ **慎用**：它关掉的是**整个测试**的防护，不只是你注入的那个仓储。若测试里调
    `agent.start()`，Agent 的**其余全部仓储**（session / message / token / ...）会真连
    生产库——实测 `test_user_identity_persistence.py` 曾因此产生 108 次
    `MongoDBBackend(db="default")` 真实构造。

    **多数情况不需要它**：若你是直接给某个仓储注入 `_db`（不经 `DocumentDBFactory`），
    防线本来就影响不到你——把 fixture 去掉即可，其余仓储会自动被隔离。
    """


@pytest.fixture
def real_milvus_collection():
    """本测试**有意检查生产集合名**，请防线别改 `COLLECTION`。

    用于钉住「仓储路由到 `long_term_memory_v2`（而非 v1）」的回归测试
    （`core/storage/tests/test_memory_repo_v2.py`）——防线的 `*__test` 重命名会让
    这类断言失真。**注意**：请求它只关闭「集合改名」，Mongo 导流仍然生效；
    该测试只用 stub、不连真实 Milvus，故安全。
    """


@pytest.fixture(autouse=True)
def _isolate_session_pending_jsonl(tmp_path, monkeypatch):
    """会话 pending JSONL 隔离到 tmp_path（防「测试残留 → 真实 Agent 回收进生产库」）。

    ## 为什么需要（2026-09-11 实测发现）

    `SessionMemory.save()` 在无 repo（`_repo is None`，测试常态）时降级写
    `data/session_{id}.json`。测试用 `SessionMemory(session_id="A")` 这类假会话，
    于是 `data/` 里堆积测试残留。而**任何一次真实 Agent 启动**都会走
    `SessionManager.recover_pending_sessions()` —— 它 glob 该目录、导入 Mongo、
    并在 durable 后**删除 JSONL**。

    实测后果：`test_tool_session_rebind.py` 的假会话 A/B 被真实 Agent 启动回收进生产
    `sessions` 集合（`character_binding={}`，无绑定）。`character_profiles=true` 时前端
    对会话列表整页 fail-closed → 用户的会话面板直接不可用——正是问题单
    「多角色数据完整性缺口导致前端整页拒绝」的触发形态。

    **本路径是真实库防线覆盖不到的**：防线只拦 pytest 进程内的 Mongo 写入，而这里是
    「pytest 写本地文件 → 之后由非 pytest 的真实进程洗进库」。

    隔离手段用 `ProjectPath.resolve()` 自带的环境变量覆盖（`ELYSIA_PATH_<NAME>`），
    故对 `SessionMemory` 的构造路径零侵入。
    """
    monkeypatch.setenv(
        "ELYSIA_PATH_SESSION_PENDING",
        str(tmp_path / "session_{session_id}.json"),
    )


@pytest.fixture
def real_embedding():
    """本测试**有意加载真实 HuggingFace 嵌入模型**，请全局替身让位。

    仅用于确实需要真实 `bge-large-zh-v1.5` 的测试（当前仓内没有）。默认情况下
    所有非 `external` 测试都用 `_no_real_embedding` 的确定性替身——见那里的理由
    （真实冷加载会让 63 条用例各多花 ~3.8s）。
    """


@pytest.fixture(autouse=True)
def _no_real_embedding(request, monkeypatch):
    """全局不加载真实 HuggingFace 嵌入模型——本仓最大的一处单点耗时（2026-09-14）。

    ## 实测（串行 `-m "unit or service"`，逐条累加 phase 时长）

    | phase | 合计 | 占比 |
    |-------|------|------|
    | setup | 32.3s | 5.7% |
    | call | 281.3s | 49.6% |
    | **teardown** | **253.9s** | **44.7%** |

    单条 setup 最大只有 36ms（没有"昂贵的 fixture"可优化）；teardown 里
    **63 条各 ~3.8s**（3.5–4.5s 带状聚集）就占了 238.6s。

    ## 根因（已确认，非推断）

    `Agent.start()` 会起一个 embedding 预加载任务；`Agent.stop()` 用
    **3.0s** 预算取消它（`core/agent/_lifecycle_tools.py` 的
    `cancel_and_drain(timeout=3.0)`）。但真实 `bge-large-zh-v1.5` 冷加载远超 3s
    ——预加载任务在预算内**没有落定**，加载于是溢进 pytest-asyncio 的事件循环
    收尾，3.0s 预算 + ~0.7s settle 正是那 3.67–3.77s 的来源。
    带 `log_cli` 的时间戳捕获在 teardown 窗口里看到
    `Using local model … bge-large-zh-v1.5` → `Loading SentenceTransformer model`。

    反证（逐条排除）：
      - `-p no:timeout` 后同一条测试 27.18s vs 27.05s → **不是 pytest-timeout**；
      - 只构造不 `start()` 的对照测试 0.05s、**完全没有 teardown 项**；
      - `Agent.stop()` 本身在 `call` 相里只占 0.23s（4 次 stop）→ 不是 stop 本身慢；
      - 打上本替身后 `core/input/tests/test_version.py` **26.0s → 13.7s，且
        teardown 条目全部归零**（剩余是 4×2.04s 的 `ws.receive(timeout=2.0)`）。

    ## 为什么现在才覆盖到

    正确做法本就有现成 fixture（`core/tests/conftest.py` 与
    `core/memory/tests/conftest.py` 的 `_no_network_embedding`），但 **conftest 是
    目录作用域**——只覆盖 490/4985 条，恰好漏掉全部热点目录
    （`core/agent/tests/`、`core/input/tests/`、`core/cognitive/tests/`）。
    本 fixture 把它提到根 conftest，替换掉那两份目录级副本。

    豁免：`external`（真实 LLM/DB E2E 本就需要真实依赖）与显式请求
    `real_embedding` 的测试。

    替身只换 `EmbeddingProvider._try_load_sync`（core 的嵌入提供者），**不动**
    `Module/embedding` 那一层——`Module/embedding/tests/` 走 `EmbeddingFactory` /
    `HuggingFaceEmbeddings`，由 `Module/conftest.py` 自己的 patch 覆盖。

    ## 覆盖边界（务必知道）

    **离线套件里没有任何测试验证真实模型的冷加载**——这一点在本 fixture 落地**之前**
    就是这样：真实加载只作为副作用发生（`Agent.start()` 的预加载任务），没有任何断言
    依赖它的输出。逐个查过断言嵌入事实的三处：`test_ready_embedding.py` 断言的是
    `is_ready is False`（纯读，不触发加载）；`test_long_term.py` 断言的是
    `expected_dimension` 这个**配置常量**（不是模型属性）；`Module/embedding/tests/`
    属另一层。三者都不依赖真实权重。

    所以本 fixture **没有掩盖任何既有集成覆盖**。要验证真实嵌入请走 `external`
    （已豁免本 fixture）或手工 `scripts/stage4_smoke.py` / `make check-full`。

    两条分支各有一条回归守卫：`core/tests/test_embedding_fake_guard.py`
    （默认必须被替身覆盖 / `real_embedding` 必须恢复原方法）。反证过：把
    `autouse=True` 摘掉，那条守卫立即转红并给出可执行的排查指引。
    """
    if "external" in {m.name for m in request.node.iter_markers()}:
        return
    if "real_embedding" in set(request.fixturenames):
        return

    from core.memory.embedding_provider import EmbeddingProvider

    class _FakeEmbedding:
        dimension = 1024

        async def embed_query(self, text: str) -> list[float]:
            return [0.1] * 1024

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(EmbeddingProvider, "_try_load_sync", lambda self: _FakeEmbedding())


@pytest.fixture(autouse=True)
def _isolate_shared_journals(tmp_path, monkeypatch):
    """把"仓库根相对路径"的共享 journal 隔离到 tmp_path（2026-09-14 CI 并行专项）。

    ## 为什么需要（`pytest -n N` 的硬阻塞项）

    这些 journal 的路径**不含 worker 维度**，`pytest -n N` 时 N 个 worker 共享同一个
    文件、甚至同一个临时文件名，于是互相覆盖/删除/串读：

    - `data/scheduler_checkpoint.json`——`ToolCallDispatcher` 用
      `<path> + ".tmp"` 做原子写（`core/tool_runtime/scheduler.py`）。同名 `.tmp`
      在多个进程里并发生成 → 一个 worker 的 `os.replace` 让另一个的写变成
      `FileNotFoundError`；更糟的是 worker B 的 `_restore_checkpoint()` 会把
      **另一个测试 checkpoint 的任务**恢复进自己的队列（跨测试串扰，且写入失败
      只 warn，问题不会自我暴露）。
    - `data/memory_pending_deletes.json`——`LongTermMemory` 的删除 tombstone
      journal。此前只有 `core/tests/conftest.py` 与 `core/memory/tests/conftest.py`
      用 `_isolate_pending_syncs` 隔离了**兄弟**文件（syncs），deletes 没有对应
      隔离，而 `core/storage`、`core/agent`、`core/input`、`Module/system_prompt`
      的测试同样会构造 `LongTermMemory`。
    - `data/memory_pending_syncs.json`——同一族的未同步条目 journal。原本只有
      `core/tests` / `core/memory/tests` 两个目录 conftest 隔离，其余目录裸奔。
    - `data/session_pending_cleanup.json`——会话删除清理重试 journal，
      `SessionManager.__init__` 就会读它，此前**没有任何地方隔离**。

    隔离手段是 `ProjectPath.resolve()` 自带的环境变量覆盖（`ELYSIA_PATH_<NAME>`），
    对被测代码零侵入——与既有 `_isolate_session_pending_jsonl` 同模式。
    注意 `ProjectPath.resolve()` 是**每次调用**读环境变量，所以 monkeypatch 生效；
    反过来说，**在 import 期就把路径固化下来的写法不受本 fixture 影响**——
    `TokenTracker._pending_file`（`core/monitoring/token_tracker.py`，类属性，
    定义时求值）就是这种情况，它的测试用实例属性覆盖
    （见 `core/monitoring/tests/test_token_tracker.py`）。这条是**已知缺口**：
    目前没有测试从"未隔离目录"走到它，但同一类缺陷的下一个实例不会自己暴露。

    **串行下同样需要**：它同时消除"测试写进真实 `data/` 目录"的残留（本仓 `data/`
    是真实运行时目录，不是测试沙箱）。
    """
    for name in (
        "SCHEDULER_CHECKPOINT",
        "MEMORY_PENDING_DELETES",
        "MEMORY_PENDING_SYNCS",
        "SESSION_PENDING_CLEANUP",
        # #41：提交后附件引用释放 journal（debug 删除路径 + 启动重试）。
        "PENDING_ATTACHMENT_RELEASE",
        # #10：未整合对话缓冲 journal（consolidator 构造时就读、每轮/失败/关机时写）。
        "CONSOLIDATOR_PENDING",
        # #6 硬杀窗口：后台任务终态本地 WAL（Agent 构造即用、每次派发/终态同步追加）。
        "TASK_RESULT_WAL",
    ):
        monkeypatch.setenv(f"ELYSIA_PATH_{name}", str(tmp_path / f"{name.lower()}.json"))


@pytest.fixture(autouse=True)
def _guard_real_database(request, monkeypatch):
    """任何测试都不许碰生产库（详见模块 docstring）。

    两条隔离对所有测试生效：
    - **Milvus**：LTM 集合重定向到 `*__test`——`clear_all` 会 drop collection，
      一条测试即可抹掉全部长期记忆（实测发生）。
      `real_milvus_collection` fixture 可关闭改名（供钉住生产集合名的回归测试）。
    - **Mongo**：生产连接导流到内存 `FakeDocumentDBBackend`。`integration`/`slow`
      也照做——实测它们并非真需要 Mongo，只是用了 `AppConfig()` 建 Agent（如
      `TestDebugDbApi` 往真实 `default` 会话写 `msg-test`/`hello`）。
      仅 `external`（显式 opt-in 的真实 E2E）放开 Mongo。
    """
    fixtures = set(request.fixturenames)
    if "real_milvus_collection" not in fixtures:
        # 生产 LTM 集合对所有测试关闭：重定向到 *__test。
        install_test_milvus_collection(monkeypatch)

    if "external" in {m.name for m in request.node.iter_markers()}:
        yield  # 真实 E2E：显式 opt-in，允许真实 Mongo
        return
    if {"fake_storage", "no_storage_guard"} & fixtures:
        yield  # 已显式隔离 / 自管存储
        return

    def _record(_where: str) -> None:
        _REDIRECTED_TESTS.add(request.node.nodeid)

    install_fake_mongo(monkeypatch, _record)
    if not is_exempt(m.name for m in request.node.iter_markers()):
        # unit/service 连 Milvus 也无意义——拦截，省掉无谓连接。
        install_blocked_milvus(monkeypatch, _record)
    yield


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """汇总被导流的测试——保持可见，但不判失败（避免为既有测试批量加标记）。"""
    if not _REDIRECTED_TESTS:
        return
    terminalreporter.write_sep(
        "-", f"真实库防线：{len(_REDIRECTED_TESTS)} 个测试的生产 Mongo 连接被导流到内存 fake"
    )
    for nodeid in sorted(_REDIRECTED_TESTS)[:10]:
        terminalreporter.write_line(f"  {nodeid}")
    if len(_REDIRECTED_TESTS) > 10:
        terminalreporter.write_line(f"  … 其余 {len(_REDIRECTED_TESTS) - 10} 个省略")
