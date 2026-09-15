# 测试文件：Module/tool/tests/test_safety_read_isolation.py
"""读白名单的**会话隔离**守卫（S2-G4，marker=unit）。

**修前**（报告实测）：写路径替换 `{session_id}`、**读路径不替换**——于是配置里带模板
的那一项永不匹配，真正放行一切的是同一份配置里的**裸根**：
`read_file("/home/yomu/data/sandbox/other-session/secret.txt")` 直接通过。
多角色/多会话是本项目正在推进的产品方向，所以这是**产品级的隔离缺口**，不是理论问题。

四条守卫，缺一不可：

1. 跨会话读必须被拒（核心断言）；
2. 本会话读必须放行——否则"修法"只是把所有读都打断（fail-closed 过头的假修复）；
3. 相对路径必须落在**会话作用域**那一项上（配置里同时有公共根时不得回退到公共根）；
4. 真配置 `core/tools/filesystem/config.yaml` 不得再出现裸根（配置守卫）。
"""

from pathlib import Path

import pytest
import yaml

from Module.tool.capability import Capability
from Module.tool.safety import (
    SafetyPipeline,
    SafetyProfile,
    _read_dir_paths,
    _resolve_sandbox_path,
    _validate_path,
)

pytestmark = pytest.mark.unit

_BASE = "/tmp/elysia-sandbox"
_TEMPLATE = f"{_BASE}/{{session_id}}"


def _profile(**over) -> SafetyProfile:
    kwargs: dict = {
        "filesystem_allowed": True,
        "read_dirs": [_TEMPLATE],
        "write_dir": _TEMPLATE,
    }
    kwargs.update(over)
    return SafetyProfile(**kwargs)


class _ReadPlugin:
    """只声明"能读文件系统"的最小插件（走 `_check_safety` 的路径分支）。"""

    safety = _profile()
    capabilities = {Capability.FILESYSTEM_READ}
    name = "fake_read"


class _Ctx:
    """最小 ctx：`_check_safety` 会把解析后的 `_safe_path` 写回 `ctx.services`。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.services: dict = {}


class TestReadIsolation:
    def test_read_other_session_is_denied(self):
        """核心断言：读别的会话目录必须被拒（修前：裸根把它放行了）。"""
        err = _validate_path(
            Path(f"{_BASE}/other-session/secret.txt"), _profile(), safe_sid="my-session")
        assert err and "不在允许的目录中" in err

    def test_bare_root_defeats_isolation_by_whitelist_semantics(self):
        """**为什么配置里绝不能有裸根**：白名单语义是"命中任一项即可读"。

        所以一条裸根就等于"整个 sandbox 可读"——模板项替换得再对也没用。
        这条把因果关系钉死，防止后人为了"模板项读不到了"又把裸根加回来。
        （**这不是代码缺陷**，是白名单语义；机制侧由下面的配置守卫拦截。）

        反过来说：修前正是"模板项是死配置 + 裸根在放行"这个组合——
        代码侧修好模板替换，才使"删掉裸根"不会把**所有**读打成 fail-closed。
        """
        legacy = _profile(read_dirs=[_TEMPLATE, _BASE])
        assert _validate_path(Path(f"{_BASE}/other-session/secret.txt"), legacy,
                              safe_sid="my-session") is None
        # 而"只有会话模板项"的白名单下，同一个路径必须被拒——两者合起来才是隔离
        assert _validate_path(Path(f"{_BASE}/other-session/secret.txt"), _profile(),
                              safe_sid="my-session") is not None

    def test_read_own_session_passes(self):
        """对照：本会话读必须放行——防止"修法"退化成谁也读不了。"""
        assert _validate_path(
            Path(f"{_BASE}/my-session/a.txt"), _profile(), safe_sid="my-session") is None

    def test_whitelist_template_is_expanded(self):
        """展开本身：`{session_id}` 必须真的被替换（这是修前缺的那一步）。"""
        dirs = _read_dir_paths(_profile(), "s1")
        assert dirs is not None
        assert dirs[0].as_posix() == f"{_BASE}/s1"
        assert "{session_id}" not in dirs[0].as_posix()

    def test_relative_read_lands_in_session_scoped_dir(self):
        """配置里同时有公共根时，相对读仍必须落在会话目录（不得回退到公共根）。"""
        resolved, err = _resolve_sandbox_path(
            "a.txt", _profile(read_dirs=[_BASE, _TEMPLATE]), "s1", is_write=False)
        assert err is None
        assert resolved.as_posix() == f"{_BASE}/s1/a.txt"

    def test_unconfigured_whitelist_keeps_legacy_semantics(self):
        """未配置读白名单 → 返回 None（不限制读）——既有语义不被本项改变。"""
        assert _read_dir_paths(_profile(read_dirs=None), "s1") is None

    def test_shipped_config_has_no_bare_root(self):
        """配置守卫：真配置的每一项都必须带 `{session_id}`，否则等同无隔离。"""
        cfg = Path(__file__).resolve().parents[3] / "core/tools/filesystem/config.yaml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        dirs = data["read_dirs"]
        assert dirs, "读白名单不得为空（空 = 不限制读）"
        bare = [d for d in dirs if "{session_id}" not in d]
        assert not bare, f"读白名单里出现裸根（跨会话可读）：{bare}"

    def test_write_still_contained(self):
        """回归：写路径 containment 不受本次收口影响（`../` 逃逸仍被拒）。"""
        _resolved, err = _resolve_sandbox_path(
            "../../escape.txt", _profile(), "s1", is_write=True)
        assert err and "必须在" in err


class TestCheckSafetyWiring:
    """`_check_safety` 必须把**本次调用的会话**传进白名单比对（两条路径对称）。"""

    @pytest.mark.asyncio
    async def test_cross_session_read_denied_through_pipeline(self):
        err = await SafetyPipeline()._check_safety(  # noqa: SLF001
            _ReadPlugin(), _Ctx("my-session"),
            {"path": f"{_BASE}/other-session/secret.txt"})
        assert err and "不在允许的目录中" in err

    @pytest.mark.asyncio
    async def test_own_session_read_allowed_through_pipeline(self):
        err = await SafetyPipeline()._check_safety(  # noqa: SLF001
            _ReadPlugin(), _Ctx("my-session"), {"path": f"{_BASE}/my-session/a.txt"})
        assert err is None


class TestPluginClassDefaultsAreSessionScoped:
    """S2-G4 残余：**类默认值**也必须是会话模板。

    `manager._load_safety_config` 在 `config.yaml` 缺失时**静默 return**（现在会 warning），
    此刻生效的是插件类的 `SafetyProfile` 默认值。默认值若是裸根，读路径就跨会话——
    写路径（`write_dir`）一直是模板，这是同一份隔离里的两套默认。
    """

    def test_read_plugins_default_to_session_template(self):
        from core.tools.filesystem.plugin import ListDirPlugin, ReadFilePlugin

        for cls in (ReadFilePlugin, ListDirPlugin):
            dirs = cls.safety.read_dirs or []
            assert dirs, f"{cls.__name__} 的 read_dirs 类默认为空——读路径会全挂"
            assert all("{session_id}" in d for d in dirs), (
                f"{cls.__name__} 的 read_dirs 类默认不是会话模板（裸根=跨会话可读）：{dirs}")

    def test_write_dir_default_is_session_template(self):
        """同一份隔离里两个默认值必须同口径（写一直是模板）。"""
        from Module.tool.safety import SafetyProfile

        assert "{session_id}" in SafetyProfile().write_dir
