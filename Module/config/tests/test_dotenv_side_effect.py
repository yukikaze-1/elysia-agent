"""批 1.5 回归网：`.env` 由**入口**加载，库 API 只读环境、绝不写环境。

## 被钉住的四条不变量

1. **import 纯净**：新解释器里 import 库模块后，`os.environ` 不得新增任何来自仓库
   `.env` 的键。
2. **`ConfigLoader.load()` 纯净**：调用前后 `os.environ` 快照逐键相同；且它**根本
   不调用** `dotenv.load_dotenv`（把该函数换成抛异常的桩，`load()` 仍须正常返回）。
3. **顺序无关**：`.env` 里有、而环境里被删掉的键 K，库 `load()` 不得把它复活——
   **无论本进程此前是否 load 过**（`EnvManager._loaded` 曾是一次性开关，两种历史下
   结果不同）。
4. **入口确实喂过环境**：根 `conftest.py` 必须已经显式注入过 `.env`。没有这条，
   不变量 3 的"没复活"会退化成"本来就没人读过 .env"——**假绿**。

## 为什么子进程不可省

不变量 1 检查的是**导入图**，不是当前进程的残留状态：同一次 pytest 会话里其它用例
早已 import 过这些模块（`sys.modules` 命中，`import` 是空操作），同进程断言恒真。
判定方式与 `core/schema/tests/test_import_isolation.py` 一致：子进程打印 JSON 报告，
**非零退出即失败**（探针自己崩掉时"没看到污染"不构成证据）——不 `importorskip`、不 skip。

## 归属说明（重要，批 1.5 实测）

仓库侧**没有任何 import 期 config load**（AST 扫描 52 个 `ConfigLoader(` 构造点，
import 期 0 个）。用 ``python -c`` 从仓库根 import 时确实会看到 `.env` 被注入，但那是
**第三方 pymilvus** 的 import 期 ``load_dotenv()``（`pymilvus/settings.py:6`）——
python-dotenv 的 `find_dotenv()` 在 `__main__` 无 `__file__`（`-c`/REPL）时退回
`os.getcwd()`，于是从仓库根起进程就"顺带"读到了仓库 `.env`。那是 pymilvus 的行为，
本仓改不了；故本文件的子进程探针写成**脚本文件**形态（`__main__.__file__` 存在 →
`find_dotenv()` 从调用方文件所在目录找 → pymilvus 那条链落在 site-packages、找不到
仓库 `.env`），从而只度量**本仓代码**的纯净度。真实链路由
`tmp/batch1_5_probe/probe.py` 的探针 E 记录。
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from Module.config import ConfigField, ConfigLoader
from Module.config.loader import EnvManager
from Module._common.env import ensure_dotenv

pytestmark = pytest.mark.unit  # 子进程确定性门禁，秒级（同 core/schema/tests/test_import_isolation.py 惯例）

_REPO_ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "conftest.py").is_file() and (p / "core").is_dir() and (p / "Module").is_dir()
)
"""仓库根——显式 cwd + `sys.path`，使子进程能找到 `core` / `Module`。

**不能猜层级**：路径错时报出来的是 `ModuleNotFoundError` 而不是判据失败——假红掩盖真判据。
"""

_DOTENV_PATH = _REPO_ROOT / ".env"

_PROBE_TIMEOUT_S = 180
"""单次子进程探针上限（冷 import 实测 ≈1.5s；180s 只防挂死）。"""

_IMPORT_CHAIN = (
    "Module.config",          # 被测库本体
    "Module.llm.registry",    # 轻量 registry
    "Module.workflow_contract",  # 跨边契约层
    "core.schema",            # 轻量数据模型层
    "core.cognitive.delegation",  # Batch 1 报告里"import 即注入 .env"的那条链
    "core.agent.agent",       # 应用组合根
)
"""探针 import 的模块（覆盖 Batch 1 报告点名的全部模块）。"""

_PROBE_SRC = """
import json, os, sys

for _k in {keys!r}:            # 先摘掉 .env 声明过的键：override=False 对已存在的键不留痕迹
    os.environ.pop(_k, None)


def _diff(before, after):
    return sorted(set(after) - set(before))


_before_import = dict(os.environ)
for _m in {modules!r}:
    __import__(_m)
_after_import = dict(os.environ)

from Module.config import ConfigLoader
from core.config import AppConfig

_before_load = dict(os.environ)
ConfigLoader(AppConfig).load()
_after_load = dict(os.environ)

_dotenv_keys = set({keys!r})
print(json.dumps({{
    "env_mutated_by_import": bool(_diff(_before_import, _after_import)),
    "added_by_import": _diff(_before_import, _after_import),
    "added_by_import_from_dotenv": sorted(set(_diff(_before_import, _after_import)) & _dotenv_keys),
    "env_mutated_by_load": bool(_diff(_before_load, _after_load)),
    "added_by_load": _diff(_before_load, _after_load),
    "added_by_load_from_dotenv": sorted(set(_diff(_before_load, _after_load)) & _dotenv_keys),
}}))
"""


@dataclasses.dataclass
class _ProbeConfig:
    """探针配置——扁平、无约束，`load()` 不依赖 YAML。"""

    value: str = ConfigField(default="default")


_PRESENT_BEFORE_STRIP: set[str] = set()
"""`_fresh_env_manager` 摘除前，本进程环境里实际持有的 `.env` 键（每个用例现算）。

为什么需要它：autouse fixture 会把 `.env` 的键从 `os.environ` 摘掉，于是"入口有没有把
环境喂进来"不能再看现场残留。而 `keys_added`（conftest 里现算的注入差集）在
`pytest -n 8` 下会**漏记**：controller 进程 import conftest 时已完成引导，execnet 起
worker 时把该环境**一并继承**，worker 里再引导时键已存在 → `keys_added` 为空。
两者合起来才完备。
"""


@pytest.fixture(autouse=True)
def _fresh_env_manager(monkeypatch):
    """每条用例都从"本进程第一次 load"起跑——两条隔离缺一不可。

    1. `EnvManager._loaded = False`：批 1.5 的病根就是"结果取决于进程历史"。若断言跑在
       一个已经 load 过的进程里，`ensure_dotenv` 会被那个一次性开关短路，于是**测试自己**
       也变成顺序依赖的——实测（pre-fix 运行）：`test_config_loader_never_calls_dotenv` /
       `..._env_path_no_longer_loads_a_file` 两条在前一条用例已把 `_loaded` 翻成 True 后
       **假绿通过**。
    2. **摘掉 `.env` 声明过的键**：只读快照 diff 只有在"键本来不在环境里"时才看得见注入。
       pre-fix 实测：`..._load_from_dict_does_not_mutate_environ` 因为前一条用例已经把
       `.env` 注进本进程而**假绿通过**（`override=False` 对已存在的键不留痕迹）。
       monkeypatch 在用例结束后会原样恢复，不影响其它测试。
    """
    global _PRESENT_BEFORE_STRIP
    _PRESENT_BEFORE_STRIP = {k for k in _dotenv_keys() if k in os.environ}
    monkeypatch.setattr(EnvManager, "_loaded", False, raising=True)
    for key in _dotenv_keys():
        monkeypatch.delenv(key, raising=False)


def _dotenv_keys() -> list[str]:
    """仓库 `.env` 声明的键名（只取键，不碰值）。"""
    if not _DOTENV_PATH.is_file():
        return []
    keys: list[str] = []
    for line in _DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            keys.append(key)
    return keys


def _run_probe(source: str, script_path: Path) -> dict:
    """在全新解释器里跑探针（**脚本文件**形态，见模块 docstring「归属说明」），返回 JSON 报告。

    非零退出即失败——不 skip、不放行。
    """
    script_path.write_text(source, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )
    assert proc.returncode == 0, (
        f"子进程探针失败（exit {proc.returncode}）\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── 1. import 纯净（子进程） ────────────────────────────────────────────


def test_library_import_and_load_do_not_inject_dotenv(tmp_path):
    """不变量 1+2（子进程）：import 库模块、以及库 `load()`，都不得改动 `os.environ`。

    两段分开断言，是为了让失败原因可读：`added_by_import` 非空 = 导入图里有东西在读
    `.env`；`added_by_load` 非空 = 库 API 自己在读 `.env`（批 1.5 的修复对象）。
    """
    report = _run_probe(
        _PROBE_SRC.format(keys=_dotenv_keys(), modules=list(_IMPORT_CHAIN)),
        tmp_path / "probe_import_purity.py",
    )
    assert report["added_by_import_from_dotenv"] == [], (
        f"import {_IMPORT_CHAIN} 把仓库 .env 注进了 os.environ："
        f"{report['added_by_import_from_dotenv']}"
    )
    assert not report["env_mutated_by_import"], (
        f"import 改动了 os.environ（新增 {report['added_by_import']}）"
    )
    assert report["added_by_load_from_dotenv"] == [], (
        f"ConfigLoader.load() 把仓库 .env 注进了 os.environ："
        f"{report['added_by_load_from_dotenv']}——库只读 os.environ，加载是入口的责任"
        f"（根 conftest.py 的「环境引导」）"
    )
    assert not report["env_mutated_by_load"], (
        f"ConfigLoader.load() 改动了 os.environ（新增 {report['added_by_load']}）"
    )


_ATTR_PROBE_SRC = """
import json, os, sys

for _k in {keys!r}:
    os.environ.pop(_k, None)

_hits = []
import dotenv

_real = dotenv.load_dotenv


def _spy(*a, **kw):
    _hits.append(sys._getframe(1).f_code.co_filename)   # load_dotenv 的**调用方文件**
    return _real(*a, **kw)


dotenv.load_dotenv = _spy
try:
    import dotenv.main
    dotenv.main.load_dotenv = _spy
except Exception:
    pass

_repo = {repo!r}
for _m in {modules!r}:
    __import__(_m)

_repo_callers = sorted({{f for f in _hits if f.startswith(_repo)}})
print(json.dumps({{
    "callers": sorted(set(_hits)),
    "repo_callers": _repo_callers,
    "third_party_callers": sorted({{f for f in _hits if not f.startswith(_repo)}}),
}}))
"""


def test_repo_code_is_never_an_import_time_dotenv_caller(tmp_path):
    """归属守卫（`python -c` 形态）：import 期若有人调 `load_dotenv()`，调用方不得是本仓文件。

    与上一条的关系：上一条用**脚本文件**形态度量本仓的纯净度（第三方那条链在
    site-packages 就断了）；本条反过来——故意用 `python -c` + cwd=仓库根（pymilvus 的
    `load_dotenv()` 会经 `find_dotenv()` 的 cwd 回退读到仓库 `.env`，见模块 docstring
    「归属说明」），但**只断言调用方归属**：第三方怎么污染不在本仓控制范围，本仓自己的
    代码在 import 期读 `.env` 才是回归。

    本条在修复前后都应绿（import 期本仓从来没有过 helper）——它是**防新增**的守卫，
    不是修复的判据；修复的判据是上面那 5 条。
    """
    report = _run_probe(
        _ATTR_PROBE_SRC.format(keys=_dotenv_keys(), modules=list(_IMPORT_CHAIN), repo=str(_REPO_ROOT)),
        tmp_path / "probe_import_attribution.py",
    )
    assert report["repo_callers"] == [], (
        f"本仓文件在 import 期调用了 dotenv.load_dotenv：{report['repo_callers']}——"
        f"库 import 不得读 .env（第三方调用方：{report['third_party_callers']}）"
    )


# ── 2. ConfigLoader 纯净（同进程，最强判据：快照全等） ──────────────────


def test_config_loader_load_does_not_mutate_environ():
    """不变量 2（同进程）：`load()` 前后 `os.environ` 必须**逐键相同**。

    用全等而非"不含 .env 的键"：库 API 不该写环境里的**任何**东西。
    """
    before = dict(os.environ)
    ConfigLoader(_ProbeConfig).load()
    after = dict(os.environ)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"ConfigLoader.load() 改动了 os.environ：新增 {added} / 删除 {removed} / 改值 {changed}"
    )


def test_config_loader_load_from_dict_does_not_mutate_environ():
    """不变量 2：`load_from_dict()` 同样只读环境（两条管线曾各调一次 ensure_dotenv）。"""
    before = dict(os.environ)
    ConfigLoader(_ProbeConfig).load_from_dict({"value": "from-dict"})
    after = dict(os.environ)
    assert after == before, (
        f"ConfigLoader.load_from_dict() 改动了 os.environ："
        f"新增 {sorted(set(after) - set(before))}"
    )


def test_config_loader_never_calls_dotenv(monkeypatch):
    """不变量 2（直接判据）：把 `dotenv.load_dotenv` 换成炸弹，`load()` 仍须正常返回。

    比"快照没变"更强：快照法只能证明**这次**没注入（可能因为 `_loaded` 已是 True），
    炸弹法证明库**根本没有这条调用路径**。
    """
    import dotenv

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "ConfigLoader.load() 调用了 dotenv.load_dotenv——库只读 os.environ，"
            "加载 .env 是入口（app.py / __main__ / 根 conftest.py）的责任"
        )

    monkeypatch.setattr(dotenv, "load_dotenv", _boom)
    cfg = ConfigLoader(_ProbeConfig).load()
    assert cfg.value == "default"


def test_config_loader_env_path_no_longer_loads_a_file(tmp_path, monkeypatch):
    """不变量 2：`env_path=` 已无加载效果（保留仅为兼容既有调用方）。

    这条同时是"库不会替调用方读文件"的直接证据——一个不存在的显式路径曾能触发
    `load_dotenv(dotenv_path=...)`。
    """
    sentinel = "ELYSIA_BATCH1_5_SENTINEL"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{sentinel}=1\n", encoding="utf-8")
    monkeypatch.delenv(sentinel, raising=False)

    ConfigLoader(_ProbeConfig, env_path=str(env_file)).load()

    assert sentinel not in os.environ, (
        f"{env_file} 被 ConfigLoader(env_path=...) 加载了——库不该替调用方读 .env"
    )


# ── 3. 顺序无关：被删的键不得复活（曾由 `_loaded` 一次性开关决定） ────────


@pytest.mark.parametrize("loaded_flag", [True, False], ids=["_loaded=True", "_loaded=False"])
def test_load_does_not_resurrect_deleted_dotenv_key(monkeypatch, dotenv_bootstrap, loaded_flag):
    """不变量 3+4：`.env` 里有、环境里被删掉的键 K，库 `load()` 不得把它读回来。

    **`_loaded` 两种取值都跑**：这正是批 1.5 之前的分裂点——`_loaded=False` 时
    `load()` 会经 `ensure_dotenv` 重新读 `.env`（K 复活），`True` 时不会。现在两者
    必须一致。

    前置断言（`dotenv_bootstrap`）不可省：若入口根本没喂过 `.env`，K 本来就不在环境里，
    "没复活"是**假绿**。"环境里曾有 K、随后被删"用 setenv + delenv 现算构造——
    autouse fixture 已把 .env 的键摘干净，故 §3 的"曾被喂过"只能由入口记录背书，
    不能靠现场残留。
    """
    key = "DEEPSEEK_API_KEY"
    if key not in _dotenv_keys():
        pytest.skip(f"仓库 .env 未声明 {key}——本机无该键可测（判据见 dotenv_bootstrap）")

    assert dotenv_bootstrap["loaded"] is True, (
        "pytest 入口没有加载 .env——conftest 的「环境引导」被破坏或 .env 缺失；"
        "此时下面的'没复活'断言无从成立（假绿），故直接失败"
    )

    monkeypatch.setattr(EnvManager, "_loaded", loaded_flag, raising=True)
    monkeypatch.setenv(key, "harness-supplied")  # 入口注入过的值
    monkeypatch.delenv(key)                      # 测试把它删掉
    assert key not in os.environ

    ConfigLoader(_ProbeConfig).load()

    assert key not in os.environ, (
        f"ConfigLoader.load() 把被删除的 {key} 从 .env 读回来了"
        f"（EnvManager._loaded={loaded_flag}）——库 API 不得写进程环境"
    )


# ── 4. 入口侧：pytest 确实喂过环境，且恰好一次 ─────────────────────────


def test_pytest_entry_bootstrapped_dotenv(dotenv_bootstrap):
    """不变量 4：pytest 入口（根 conftest.py）必须显式、一次性注入 `.env`。

    没有这条，整套"库不读 .env"的断言都可以靠"没人读 .env"骗过去。

    "环境里确实有 .env 的值"由 `_PRESENT_BEFORE_STRIP`（用例摘除前的现算快照）判定；
    引导自身的执行与否由 `dotenv_bootstrap` 判定——两者缺一都会放过一种假绿。
    """
    assert dotenv_bootstrap["attempted"] is True, (
        "根 conftest.py 的 .env 引导没有执行——它是 pytest 这个入口的环境来源"
    )
    if not _DOTENV_PATH.is_file():
        assert dotenv_bootstrap["loaded"] is False, "无 .env 文件却报告 loaded=True"
        pytest.skip("本仓库无 .env（例如 CI 的干净 checkout）——无键可断言")

    assert dotenv_bootstrap["loaded"] is True, f".env 存在却没被加载：{_DOTENV_PATH}"
    assert dotenv_bootstrap["path"] == str(_DOTENV_PATH)

    missing = sorted(k for k in _dotenv_keys() if k not in _PRESENT_BEFORE_STRIP)
    assert missing == [], (
        f"这些键在 .env 里声明、却没进本进程环境：{missing}——入口引导（或它继承给 "
        f"xdist worker 的环境）没有生效；此时其它用例的'没复活'断言都是假绿"
    )
    assert dotenv_bootstrap["keys_added"] or _PRESENT_BEFORE_STRIP, (
        "引导既没注入任何键、进程环境里也没有 .env 的键——入口引导形同虚设"
    )


def test_entrypoint_dotenv_loader_is_once_only(monkeypatch, dotenv_bootstrap):
    """不变量 4：入口的加载器是**真一次性**——第二次调用不重读文件。

    原 `Module/_common/env.py::ensure_dotenv` 每次都重读文件（docstring 却声称幂等），
    于是"删掉 K 再 ensure_dotenv()"会把 K 注回来——入口侧的同一类顺序依赖。
    """
    if not _DOTENV_PATH.is_file():
        pytest.skip("本仓库无 .env（例如 CI 的干净 checkout）")

    key = "DEEPSEEK_API_KEY"
    if key not in _dotenv_keys():
        pytest.skip(f"仓库 .env 未声明 {key}——本机无该键可测")

    monkeypatch.delenv(key, raising=False)  # autouse fixture 已摘过；这里显式表达"环境里没有"
    assert key not in os.environ
    assert ensure_dotenv() == _DOTENV_PATH, "重复调用应返回首次加载的路径（缓存），而不是重读"
    assert key not in os.environ, (
        f"ensure_dotenv() 第二次调用把被删除的 {key} 又注入了一次——它必须只读一次文件"
    )
    assert dotenv_bootstrap["loaded"] is True
