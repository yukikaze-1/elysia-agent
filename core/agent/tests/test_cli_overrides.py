# 测试文件：core/agent/tests/test_cli_overrides.py
"""F4（Batch 1.6）：显式 CLI 覆盖必须活过配置重载。

## 缺陷（Batch 1.5 已确认，本批修复）

`app.py` 把 `--host/--port/--tts/--stt/--character` 写进 `config` 后交给
`core/agent/server.py::run_agent_service`，而后者**每一轮开头**都
`ConfigLoader(AppConfig, yaml_path=…).load()` 把 config 整个换掉 ⇒ 5 个 flag 在
server 模式**全部静默失效**（实测 `--port 9999` 实际绑 8765）；而 `--cli` / `--check`
两条不经 `run_agent_service` 的路径反而生效——同一个 flag 在不同模式下行为不同。

## 修法与本文件钉住的不变式

**不**改成"首轮不重载"——那会破坏 `/restart` 重读 YAML 这条既有契约
（`test_restart_rebuild.py` 钉着它）。改法是"每轮重载**之后**重新施加覆盖层"：

1. 只有**用户真的传了**的 flag 才进覆盖层（argparse `default=SUPPRESS`）——
   "没传"与"传了等于默认值的值"必须能区分，否则 argparse 的 default 会冒充显式覆盖
   并在重启后把 CLI 启动那一刻的 YAML 值盖回去（`/restart` 重读 YAML 随之失效）；
2. 覆盖活过**每一轮**重载（`/restart` 重建实例后仍在）——CLI 是**进程生命周期**的
   意图：`/restart` 会重新 bind，若覆盖被重置，服务会在用户没要求时换端口；
3. 优先级 = `显式 CLI 覆盖 > env > YAML > dataclass 默认`（前两层沿用现状语义：
   `ConfigLoader.load()` 内部按 defaults → YAML → env，覆盖在它之后施加）。

## 为什么解析器/端到端用例走**子进程**

`app.py` 在 import 期就写进程级 `os.environ`（`CUDA_VISIBLE_DEVICES=""`、
`SSL_CERT_FILE`）——在 pytest 进程里 import 它会把这些泄漏给后续用例（Batch 1.5
整整一批都在修这类跨用例污染）。故 CLI 相关的判据一律在**全新子进程**里跑：
既避开污染，也顺带证明"不是同一个 pytest 进程已加载旧 module"造成的假绿。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.agent.server import run_agent_service
from core.config import apply_config_overrides

pytestmark = pytest.mark.service

_REPO_ROOT = next(
    p for p in Path(__file__).resolve().parents
    if (p / "app.py").is_file() and (p / "core").is_dir()
)
"""仓库根——不猜层级（`app.py` 是子进程驱动脚本的 import 目标）。"""

#: 已确认会丢的 5 个 CLI 覆盖：`(flag, argv, config 点路径, 期望值)`。
#: 参数化用例逐一覆盖——不能只测 `--port`（那是缺陷报告里的举例，不是范围）。
_FLAG_CASES: tuple[tuple[str, list[str], str, Any], ...] = (
    ("--host", ["--host", "0.0.0.0"], "server.host", "0.0.0.0"),
    ("--port", ["--port", "9999"], "server.port", 9999),
    ("--tts", ["--tts"], "tts.enabled", True),
    ("--stt", ["--stt"], "stt.enabled", True),
    ("--character", ["--character", "character/CLI/config.yaml"],
     "agent.character", "character/CLI/config.yaml"),
)


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# 覆盖层：重载之后重新施加（in-process，真 run_agent_service）
# ═══════════════════════════════════════════════════════════════════════════════


class _StubAgent:
    """`run_server` 的替身：第一轮请求重启（触发第二轮），之后正常退出。"""

    def __init__(self) -> None:
        self.rounds = 0

    async def run_server(self, *, app_factory: Any) -> bool:
        self.rounds += 1
        return self.rounds < 2


def _run_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yaml_config: Any,
    incoming: Any,
    config_overrides: dict[str, Any] | None,
    config_path: str | None = "config.yaml",
) -> list[dict[str, Any]]:
    """驱动真 `run_agent_service`（假 loader + 假 agent_factory），记录每轮的 config。"""

    class _Loader:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def load(self) -> Any:
            # 每轮都返回**新的**对象：模拟"从磁盘重读"，覆盖层不得依赖对象同一性
            return SimpleNamespace(
                llm=SimpleNamespace(model=yaml_config.llm.model),
                server=SimpleNamespace(host=yaml_config.server.host,
                                       port=yaml_config.server.port),
                tts=SimpleNamespace(enabled=yaml_config.tts.enabled),
                stt=SimpleNamespace(enabled=yaml_config.stt.enabled),
                agent=SimpleNamespace(character=yaml_config.agent.character),
            )

    monkeypatch.setattr("Module.config.loader.ConfigLoader", _Loader)

    seen: list[dict[str, Any]] = []
    stub = _StubAgent()

    class _Factory:
        def __new__(cls, **kw: Any) -> Any:
            cfg = kw["config"]
            seen.append({
                "round": len(seen) + 1,
                "server.host": cfg.server.host,
                "server.port": cfg.server.port,
                "tts.enabled": cfg.tts.enabled,
                "stt.enabled": cfg.stt.enabled,
                "agent.character": cfg.agent.character,
                "prefer_config_model": kw["prefer_config_model"],
                "config_overrides": kw.get("config_overrides"),
            })
            return stub

    import asyncio

    asyncio.run(run_agent_service(
        config=incoming,
        config_path=config_path,
        app_factory=lambda *a, **k: None,
        agent_factory=_Factory,
        config_overrides=config_overrides,
    ))
    return seen


def _yaml_ns(
    *,
    llm_model: str = "model_a",
    host: str = "127.0.0.1",
    port: int = 8765,
    tts: bool = False,
    stt: bool = False,
    character: str = "character/YAML/config.yaml",
) -> Any:
    """"从磁盘读出来的 config"的形状替身（与 `AppConfig` 同构的最小集）。"""
    return SimpleNamespace(
        llm=SimpleNamespace(model=llm_model),
        server=SimpleNamespace(host=host, port=port),
        tts=SimpleNamespace(enabled=tts),
        stt=SimpleNamespace(enabled=stt),
        agent=SimpleNamespace(character=character),
    )


class TestOverrideSurvivesReload:
    def test_explicit_override_survives_every_reload(self, monkeypatch):
        """F4-1：YAML port=8765、CLI `--port 9999` → **两轮**都是 9999。"""
        monkeypatch.delenv("SERVER_PORT", raising=False)
        incoming = _yaml_ns()
        seen = _run_service(
            monkeypatch,
            yaml_config=_yaml_ns(),
            incoming=incoming,
            config_overrides={"server.port": 9999},
        )

        assert len(seen) == 2, "前提：这一轮跑出了 restart（重建新实例）"
        assert [r["server.port"] for r in seen] == [9999, 9999], (
            "显式 CLI 覆盖必须活过每一轮重载（F4 的原缺陷是首轮就被丢掉）")
        assert [r["prefer_config_model"] for r in seen] == [False, False]

    def test_yaml_value_is_not_overridden_when_flag_absent(self, monkeypatch):
        """F4-2：用户没传 flag → YAML 的值生效（argparse 的 default 不是覆盖）。"""
        monkeypatch.delenv("SERVER_PORT", raising=False)
        seen = _run_service(
            monkeypatch,
            yaml_config=_yaml_ns(port=9000),
            incoming=_yaml_ns(),
            config_overrides={},
        )
        assert [r["server.port"] for r in seen] == [9000, 9000]

    def test_all_five_overrides_survive_together(self, monkeypatch):
        """F4-3（端到端侧）：5 个覆盖同时传 → 两轮全部保持。"""
        for key in ("SERVER_HOST", "SERVER_PORT", "TTS_ENABLED", "STT_ENABLED",
                    "AGENT_CHARACTER"):
            monkeypatch.delenv(key, raising=False)
        overrides = {
            "server.host": "0.0.0.0",
            "server.port": 9999,
            "tts.enabled": True,
            "stt.enabled": True,
            "agent.character": "character/CLI/config.yaml",
        }
        seen = _run_service(
            monkeypatch,
            yaml_config=_yaml_ns(),
            incoming=_yaml_ns(),
            config_overrides=overrides,
        )
        for round_info in seen:
            for path, value in overrides.items():
                assert round_info[path] == value, (
                    f"{path} 在第 {round_info['round']} 轮丢失：{round_info[path]!r}")

    def test_config_path_none_reuses_the_incoming_config(self, monkeypatch):
        """无 `config_path` → 不重载，传入的 config（已含覆盖）照常使用。"""
        overrides = {"server.port": 9999}
        incoming = _yaml_ns()
        apply_config_overrides(incoming, overrides)
        seen = _run_service(
            monkeypatch,
            yaml_config=_yaml_ns(),
            incoming=incoming,
            config_overrides=overrides,
            config_path=None,
        )
        assert [r["server.port"] for r in seen] == [9999, 9999]
        assert all(r["config_overrides"] == overrides for r in seen), (
            "覆盖层必须一并交给 Agent——原地重启路径自己会重读 YAML")


# ═══════════════════════════════════════════════════════════════════════════════
# 优先级矩阵（F4-5）：显式 CLI 覆盖 > env > YAML > dataclass 默认
# ═══════════════════════════════════════════════════════════════════════════════


class TestPrecedenceMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("yaml_text", "env_port", "overrides", "expected"),
        [
            # 只有 dataclass 默认
            (None, None, {}, 8765),
            # YAML 覆盖默认
            ("server:\n  port: 9000\n", None, {}, 9000),
            # env 覆盖 YAML
            ("server:\n  port: 9000\n", "7000", {}, 7000),
            # 显式 CLI 覆盖覆盖 env
            ("server:\n  port: 9000\n", "7000", {"server.port": 9999}, 9999),
            # 显式 CLI 覆盖覆盖 YAML（无 env）
            ("server:\n  port: 9000\n", None, {"server.port": 9999}, 9999),
        ],
    )
    async def test_matrix(self, tmp_path, monkeypatch, yaml_text, env_port,
                          overrides, expected):
        """真实 `ConfigLoader` + 真实 YAML 文件走一遍四层优先级。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only")
        if env_port is None:
            monkeypatch.delenv("SERVER_PORT", raising=False)
        else:
            monkeypatch.setenv("SERVER_PORT", env_port)
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml_text or "agent:\n  user_name: 测试\n", encoding="utf-8")

        seen: list[int] = []

        class _Factory:
            def __new__(cls, **kw: Any) -> Any:
                seen.append(kw["config"].server.port)
                stub = SimpleNamespace()
                stub.run_server = AsyncMock(return_value=False)
                return stub

        await run_agent_service(
            config=SimpleNamespace(),
            config_path=str(yaml_path),
            app_factory=lambda *a, **k: None,
            agent_factory=_Factory,
            config_overrides=overrides,
        )
        assert seen == [expected]


# ═══════════════════════════════════════════════════════════════════════════════
# 原地重启路径（第二处重读 YAML）
# ═══════════════════════════════════════════════════════════════════════════════


class TestInPlaceReloadReappliesOverrides:
    def test_reload_from_disk_keeps_explicit_overrides(self, tmp_path, monkeypatch):
        """`_reload_config_from_disk`（F4 的第二个丢弃点）也必须重新施加覆盖。"""
        monkeypatch.delenv("SERVER_PORT", raising=False)
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("server:\n  port: 9000\n", encoding="utf-8")

        from core.agent.agent import Agent

        host = object.__new__(Agent)
        host._config_path = str(yaml_path)
        host._config = SimpleNamespace(server=SimpleNamespace(port=8765))
        host._config_overrides = {"server.port": 9999}

        host._reload_config_from_disk()  # noqa: SLF001 —— 被测算的方法

        assert host._config.server.port == 9999  # noqa: SLF001

    def test_reload_without_config_path_reuses_current(self, monkeypatch):
        """无 `config_path` → 复用当前 config（不抛、不静默清空覆盖表）。"""
        from core.agent.agent import Agent

        host = object.__new__(Agent)
        host._config_path = None
        current = SimpleNamespace(server=SimpleNamespace(port=9999))
        host._config = current
        host._config_overrides = {"server.port": 9999}

        host._reload_config_from_disk()  # noqa: SLF001

        assert host._config is current  # noqa: SLF001


class TestApplyConfigOverrides:
    def test_unknown_path_fails_fast(self):
        """拼错的路径必须报错——静默失效正是本缺陷的形态。"""
        config = SimpleNamespace(server=SimpleNamespace(port=8765))
        with pytest.raises(AttributeError, match="server.prot"):
            apply_config_overrides(config, {"server.prot": 1})  # type: ignore[arg-type]
        with pytest.raises(AttributeError, match="nope"):
            apply_config_overrides(config, {"nope.port": 1})  # type: ignore[arg-type]

    def test_real_appconfig_round_trip(self):
        """真 `AppConfig` 上施加 5 个覆盖 → 字段真的变了。"""
        from core.config import AppConfig

        config = AppConfig()
        apply_config_overrides(config, {
            "server.host": "0.0.0.0",
            "server.port": 9999,
            "tts.enabled": True,
            "stt.enabled": True,
            "agent.character": "character/CLI/config.yaml",
        })
        assert config.server.host == "0.0.0.0"
        assert config.server.port == 9999
        assert config.tts.enabled is True
        assert config.stt.enabled is True
        assert config.agent.character == "character/CLI/config.yaml"


# ═══════════════════════════════════════════════════════════════════════════════
# 全新子进程：真 `app.py` 解析器 + 真 `run_agent_service`
# ═══════════════════════════════════════════════════════════════════════════════

_PARSER_DRIVER = '''
import json, sys
sys.path.insert(0, {repo!r})
import app
from Module.config.loader import ConfigLoader
from core.config import AppConfig

cfg = ConfigLoader(AppConfig, yaml_path=sys.argv[1]).load()
parser = app.build_parser(cfg)
out = {{}}
for label, argv in {variants!r}.items():
    args = parser.parse_args(["--config", sys.argv[1], *argv])
    out[label] = app.collect_cli_overrides(args)
print(json.dumps(out, sort_keys=True, default=str))
'''

_MAIN_DRIVER = '''
import json, sys
sys.path.insert(0, {repo!r})
import app
from core.agent.server import run_agent_service as _real

app.setup_logging = lambda *a, **k: None   # 不在仓库里写日志文件
seen = []
round_no = 0


def _agent_factory(**kw):
    global round_no
    round_no += 1
    cfg = kw["config"]
    seen.append({{
        "round": round_no,
        "server.host": cfg.server.host,
        "server.port": cfg.server.port,
        "tts.enabled": cfg.tts.enabled,
        "stt.enabled": cfg.stt.enabled,
        "agent.character": cfg.agent.character,
    }})

    class _Stub:
        async def run_server(self, *, app_factory):
            return round_no < 2        # 第一轮请求重启 → 第二轮退出

    return _Stub()


def _shim(*, config, config_path, app_factory, agent_factory=None, **kw):
    # main() 必须把覆盖表交下来，否则这里直接非零退出（判据本身即断言）
    if "config_overrides" not in kw:
        raise SystemExit("main() 没有传 config_overrides")
    return _real(config=config, config_path=config_path, app_factory=app_factory,
                 agent_factory=_agent_factory, config_overrides=kw["config_overrides"])


app.run_agent_service = _shim
out = {{}}
for label, argv in {scenarios!r}.items():
    seen.clear()
    round_no = 0
    sys.argv = ["app.py", *argv]
    app.main()
    out[label] = [dict(item) for item in seen]
print(json.dumps(out, sort_keys=True))
'''

#: 子进程里要跑的 argv 变体（label → argv）。**一次进程跑完全部**：每个 `app` import
#: 都很贵（torch/transformers 链），逐 flag 起进程会把 Quick 档拖长几十秒。
_PARSER_VARIANTS: dict[str, list[str]] = {
    "none": [],
    "explicit-default": ["--port", "8765"],
    **{flag: argv for flag, argv, _path, _value in _FLAG_CASES},
}

_YAML_WITH_OVERRIDES = (
    "agent:\n  character: character/YAML/config.yaml\n"
    "tts:\n  enabled: false\n"
    "stt:\n  enabled: false\n"
    "server:\n  host: 127.0.0.1\n  port: 8765\n"
)
_YAML_ONLY = (
    "agent:\n  character: character/YAML/config.yaml\n"
    "tts:\n  enabled: true\n"
    "server:\n  host: 127.0.0.1\n  port: 9000\n"
)


def _run_driver(tmp_path: Path, body: str, *argv: str) -> Any:
    """在**全新子进程**里跑驱动脚本（cwd=仓库根，继承当前解释器），返回解析后的 JSON。

    `body` 必须**已经**格式化好（调用方做 `.format(repo=…)`）：格式化的产物里含驱动脚本
    自己的 `{…}` 字面量，二次 `.format()` 会把它们当占位符（实测 `KeyError: '"round"'`）。
    """
    driver = tmp_path / "driver.py"
    driver.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(driver), *argv],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def parser_overrides(tmp_path_factory) -> dict[str, dict[str, Any]]:
    """全新子进程里跑**真 `app.py` 解析器**的全部 argv 变体（一次进程）。"""
    tmp_path = tmp_path_factory.mktemp("f4-parser")
    cfg = _write_yaml(tmp_path / "config.yaml", "server:\n  port: 8765\n")
    body = _PARSER_DRIVER.format(repo=str(_REPO_ROOT), variants=_PARSER_VARIANTS)
    return _run_driver(tmp_path, body, str(cfg))


@pytest.fixture(scope="module")
def main_rounds(tmp_path_factory) -> dict[str, list[dict[str, Any]]]:
    """全新子进程里跑**真 `app.main()` → 真 `run_agent_service`** 的两个场景。"""
    tmp_path = tmp_path_factory.mktemp("f4-main")
    cfg_with_flags = _write_yaml(tmp_path / "with_flags.yaml", _YAML_WITH_OVERRIDES)
    cfg_yaml_only = _write_yaml(tmp_path / "yaml_only.yaml", _YAML_ONLY)
    scenarios = {
        "with_flags": ["--config", str(cfg_with_flags), "--host", "0.0.0.0",
                       "--port", "9999", "--tts", "--stt",
                       "--character", "character/CLI/config.yaml"],
        "yaml_only": ["--config", str(cfg_yaml_only)],
    }
    body = _MAIN_DRIVER.format(repo=str(_REPO_ROOT), scenarios=scenarios)
    return _run_driver(tmp_path, body)


class TestFreshProcessParser:
    """F4-3：5 个已确认 CLI 覆盖逐个参数化（真解析器 + 真 `collect_cli_overrides`）。"""

    @pytest.mark.parametrize(("flag", "argv", "path", "value"), _FLAG_CASES)
    def test_parser_collects_only_explicit_flags(self, parser_overrides, flag, argv,
                                                 path, value):
        assert parser_overrides[flag] == {path: value}, (
            f"{flag} 的覆盖表不正确：{parser_overrides[flag]!r}")

    def test_parser_reports_nothing_when_no_flag_is_passed(self, parser_overrides):
        """没传任何 flag → 覆盖表为空（argparse 的 default 不得冒充显式覆盖）。"""
        assert parser_overrides["none"] == {}

    def test_parser_distinguishes_explicit_default_value(self, parser_overrides):
        """传了**等于 YAML 的值**也算显式（否则 `/restart` 重读 YAML 后它会消失）。"""
        assert parser_overrides["explicit-default"] == {"server.port": 8765}


class TestFreshProcessEndToEnd:
    """F4-1/F4-2/F4-4（端到端，全新进程）：`app.main()` → `run_agent_service` → 两轮。"""

    def test_overrides_preserved_across_restart(self, main_rounds):
        """YAML 值与 CLI 值全部不同，跑两轮（第一轮请求重启）——两轮都必须生效。

        F4 的原缺陷正是"首轮重载即丢"，所以第二轮（`/restart` 重建实例之后）是关键。
        """
        rounds = main_rounds["with_flags"]
        assert len(rounds) == 2, f"前提：跑出了 restart，实测 {rounds}"
        for round_info in rounds:
            assert round_info["server.port"] == 9999, round_info
            assert round_info["server.host"] == "0.0.0.0", round_info
            assert round_info["tts.enabled"] is True, round_info
            assert round_info["stt.enabled"] is True, round_info
            assert round_info["agent.character"] == "character/CLI/config.yaml", round_info

    def test_yaml_wins_when_no_flag_is_passed(self, main_rounds):
        """不传 flag → YAML 的值一路生效（argparse 默认不是覆盖）。"""
        rounds = main_rounds["yaml_only"]
        assert len(rounds) == 2, rounds
        assert [r["server.port"] for r in rounds] == [9000, 9000], rounds
        assert all(r["tts.enabled"] is True for r in rounds), rounds
        assert all(r["agent.character"] == "character/YAML/config.yaml" for r in rounds), rounds
