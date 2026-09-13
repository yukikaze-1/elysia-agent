"""WriteFilePlugin 单元测试。"""

from pathlib import Path

import pytest

from Module.tool.protocol import ToolContext, ToolResult, ErrorType
from Module.tool.safety import SafetyProfile

from core.tools.filesystem.plugin import WriteFilePlugin, WriteFileParams

from core.tools.tests._failure_helpers import assert_failed

pytestmark = pytest.mark.service


class TestWriteFilePlugin:
    """WriteFilePlugin — 在 sandbox 目录下安全写入文件。"""

    @staticmethod
    def _make_plugin(sandbox_dir: str) -> WriteFilePlugin:
        """创建注入了自定义 sandbox 目录的 WriteFilePlugin。"""
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=sandbox_dir,
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )
        return plugin

    @staticmethod
    def _make_ctx(safe_path: str, session_id: str = "test_session") -> ToolContext:
        """创建注入了 _safe_path 的 ToolContext。"""
        ctx = ToolContext(session_id=session_id)
        ctx.services["_safe_path"] = safe_path
        return ctx

    # ── 基本写入 ──

    @pytest.mark.asyncio
    async def test_writes_file_inside_sandbox(self, tmp_path):
        """在 sandbox 内的路径写入文件，文件内容正确且结果包含 '已写入'。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "hello.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="hello.txt", content="hello world"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "hello world"

    @pytest.mark.asyncio
    async def test_writes_file_with_unicode_content(self, tmp_path):
        """写入包含中文和特殊字符的内容。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "unicode.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        content = "你好世界\nこんにちは\n🌟 emoji test"
        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="unicode.txt", content=content),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == content

    # ── 路径越界 ──

    @pytest.mark.asyncio
    async def test_rejects_path_outside_sandbox(self, tmp_path):
        """写入 sandbox 外的路径返回 '拒绝写入'。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        outside_path = tmp_path / "escape.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(outside_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="escape.txt", content="bad"),
        )

        assert_failed(result, contains="sandbox 边界")
        assert not outside_path.exists()

    @pytest.mark.asyncio
    async def test_rejects_path_that_traverses_up(self, tmp_path):
        """路径中包含 .. 导致的越界被拒绝。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        # _safe_path 指向 sandbox 外的路径（已由安全管线解析后注入）
        outside_path = tmp_path / "escaped.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(outside_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="../escaped.txt", content="bad"),
        )

        assert_failed(result, contains="sandbox 边界")

    # ── 父目录创建 ──

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path):
        """写入嵌套路径时自动创建不存在的父目录。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "nested/deep/file.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="nested/deep/file.txt", content="deep content"),
        )

        assert "已写入" in result
        assert safe_path.exists()
        assert safe_path.read_text(encoding="utf-8") == "deep content"
        # 验证父目录存在
        assert safe_path.parent.is_dir()

    @pytest.mark.asyncio
    async def test_creates_multiple_nested_parent_dirs(self, tmp_path):
        """多层级嵌套路径，各级父目录均被创建。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "a/b/c/d/e/deep.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="a/b/c/d/e/deep.txt", content="nested"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "nested"

    # ── session_id 模板替换 ──

    @pytest.mark.asyncio
    async def test_session_id_replaced_in_write_dir_template(self, tmp_path):
        """write_dir 中的 {session_id} 被替换为实际的 session_id。"""
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        expected_write_dir = sandbox_base / "my_session_123"
        safe_path = expected_write_dir / "output.txt"
        ctx = self._make_ctx(str(safe_path), session_id="my_session_123")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="output.txt", content="session data"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "session data"

    @pytest.mark.asyncio
    async def test_session_id_with_slashes_is_sanitized(self, tmp_path):
        """含路径分隔符的 session_id 必须被消毒（不得穿越出 `write_dir` 的父目录）。

        **S2-6 起消毒口径与 `SafetyPipeline` 统一**：原来插件自己用
        `session_id.replace("/", "_")`（"a/b\\c" → "a_b_c"），而 `safety.py` 的
        `_sanitize_session_id` 只接受 `[a-zA-Z0-9_-]`、不合规即回退 `"default"`。
        **同一个 `{session_id}` 模板两套消毒规则**——正常路径下 `_safe_path` 来自
        管线所以看不出来，插件被直接调用时就会算出两个不同的目录。现在统一走
        权威那一份（`_sanitize_session_id`）。
        """
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        # 不合规 session_id → 权威消毒器回退 "default"
        expected_write_dir = sandbox_base / "default"
        safe_path = expected_write_dir / "file.txt"
        ctx = self._make_ctx(str(safe_path), session_id="a/b\\c")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="file.txt", content="sanitized"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "sanitized"

    # ── 默认 session_id ──

    @pytest.mark.asyncio
    async def test_uses_default_session_id_when_empty(self, tmp_path):
        """session_id 为空时使用 'default' 作为 fallback。"""
        sandbox_base = tmp_path / "data/sandbox"
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox_base) + "/{session_id}",
            max_file_bytes=10 * 1024 * 1024,
            requires_human_approval=True,
        )

        expected_write_dir = sandbox_base / "default"
        safe_path = expected_write_dir / "file.txt"
        ctx = ToolContext(session_id="")
        ctx.services["_safe_path"] = str(safe_path)

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="file.txt", content="default session"),
        )

        assert "已写入" in result
        assert safe_path.read_text(encoding="utf-8") == "default session"

    # ── 边界情况 ──

    @pytest.mark.asyncio
    async def test_writes_empty_content(self, tmp_path):
        """写入空内容 — 创建空文件。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "empty.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="empty.txt", content=""),
        )

        assert "已写入" in result
        assert safe_path.exists()
        assert safe_path.read_text(encoding="utf-8") == ""

    @pytest.mark.asyncio
    async def test_result_includes_file_size(self, tmp_path):
        """返回结果中包含写入的字节数。"""
        sandbox_dir = tmp_path / "data/sandbox/test_session"
        safe_path = sandbox_dir / "sized.txt"

        plugin = self._make_plugin(str(sandbox_dir))
        ctx = self._make_ctx(str(safe_path))

        content = "hello world"  # 11 bytes
        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="sized.txt", content=content),
        )

        assert "已写入" in result
        assert "11 bytes" in result


class TestWriteFileHardening:
    """D1/P2-05：待写内容 UTF-8 字节上限 + sibling 边界。"""

    @staticmethod
    def _plugin(sandbox_dir: str, max_bytes: int) -> WriteFilePlugin:
        plugin = WriteFilePlugin()
        plugin.safety = SafetyProfile(
            filesystem_allowed=True,
            write_dir=sandbox_dir,
            max_file_bytes=max_bytes,
            requires_human_approval=True,
        )
        return plugin

    @pytest.mark.asyncio
    async def test_content_bytes_exceeding_limit_rejected(self, tmp_path):
        """新建文件当前大小 0 会绕过旧上限——按待写 UTF-8 字节拒绝。"""
        sandbox = tmp_path / "sandbox"
        plugin = self._plugin(str(sandbox), max_bytes=8)
        ctx = ToolContext(session_id="test_session")
        ctx.services["_safe_path"] = str(sandbox / "new.txt")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="new.txt", content="九个字节的内容"),
        )

        assert_failed(result, contains="过大")
        assert not (sandbox / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_sibling_prefix_write_rejected(self, tmp_path):
        """write_dir=/sandbox，请求 /sandbox_evil/secret → 拒绝（is_relative_to 修复）。"""
        sandbox = tmp_path / "sandbox"
        evil = tmp_path / "sandbox_evil"
        evil.mkdir(parents=True)
        plugin = self._plugin(str(sandbox), max_bytes=1024)
        ctx = ToolContext(session_id="test_session")
        ctx.services["_safe_path"] = str(evil / "secret.txt")

        result = await plugin.execute(
            ctx=ctx,
            params=WriteFileParams(path="secret.txt", content="x"),
        )

        assert_failed(result, contains="越过了 sandbox")
        assert not (evil / "secret.txt").exists()


# ══════════════════════════════════════════════════════════════════
# S2-6：沙盒 containment 必须**先 resolve 再比较**
# ══════════════════════════════════════════════════════════════════

class TestSandboxContainmentResolvesFirst:
    """插件内的沙盒检查原先用 `Path(path)`（**未 resolve**）与已 `resolve()` 的
    `write_dir` 比 `is_relative_to`。`PurePath.is_relative_to` **只比路径部件**：

    - `<sandbox>/../../etc/x` 的部件以 `('/', 'sandbox')` 开头 → **判为"在沙盒内"**，
      而内核按 `..` 解析后写入落到沙盒外（实测逃逸成功，文件真的落盘了）；
    - 反过来，经 symlink 到达沙盒的**合法**路径因未解析而与已解析的 write_dir 不匹配
      → **误拒**。

    为什么插件自己也要查：`_safe_path` 只在 `profile.filesystem_allowed` 时由
    `SafetyPipeline` 注入；插件被直接调用（测试 / 直接执行）时拿到的是**原始输入**。
    """

    def _plugin(self, tmp_path):
        from core.tools.filesystem.plugin import WriteFilePlugin

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        plugin = WriteFilePlugin()
        plugin.safety.write_dir = str(sandbox)
        return plugin, sandbox

    class _Ctx:
        session_id = "s1"

        def __init__(self):
            self.services: dict = {}

        def get(self, key, default=None):
            return self.services.get(key, default)

    @pytest.mark.asyncio
    async def test_dotdot_escape_is_rejected(self, tmp_path):
        """**核心**：`..` 逃逸必须被拒，且文件不得落盘到沙盒外。"""
        plugin, sandbox = self._plugin(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()

        evil = str(sandbox / ".." / "outside" / "evil.txt")
        # 先确认这正是"词法比较会误判"的形状
        assert Path(evil).is_relative_to(sandbox.resolve()), (
            "前提不成立：该路径在词法比较下本就不算'在沙盒内'")

        result = await plugin.execute(
            ctx=self._Ctx(), params=plugin.param_model(path=evil, content="pwned"))

        assert getattr(result, "success", True) is False, result
        assert "sandbox 边界" in getattr(result, "error", "") or \
               "sandbox 边界" in str(result), result
        assert not (outside / "evil.txt").exists(), "逃逸文件落盘了！"

    @pytest.mark.asyncio
    async def test_symlink_into_sandbox_is_allowed(self, tmp_path):
        """**对照**：经 symlink 到达沙盒内部的路径是安全的，不该被误拒。

        注意危险方向仍被拦住——symlink 指向沙盒**外**时，resolve 之后 containment
        判定失败（见下一条）。
        """
        plugin, sandbox = self._plugin(tmp_path)
        link = tmp_path / "link_to_sandbox"
        link.symlink_to(sandbox)

        result = await plugin.execute(
            ctx=self._Ctx(),
            params=plugin.param_model(path=str(link / "ok.txt"), content="hello"))

        assert getattr(result, "success", True) is not False, result
        assert (sandbox / "ok.txt").read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_symlink_pointing_outside_is_rejected(self, tmp_path):
        """symlink 指向沙盒外 → resolve 后落在边界外 → 拒绝。"""
        plugin, sandbox = self._plugin(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        escape_link = sandbox / "escape"
        escape_link.symlink_to(outside)

        result = await plugin.execute(
            ctx=self._Ctx(),
            params=plugin.param_model(path=str(escape_link / "x.txt"), content="pwned"))

        assert getattr(result, "success", True) is False, result
        assert not (outside / "x.txt").exists()

    @pytest.mark.asyncio
    async def test_legit_write_still_works(self, tmp_path):
        """回归网：沙盒内的正常写入行为不变。"""
        plugin, sandbox = self._plugin(tmp_path)

        result = await plugin.execute(
            ctx=self._Ctx(),
            params=plugin.param_model(path=str(sandbox / "sub" / "a.txt"),
                                      content="ok"))

        assert getattr(result, "success", True) is not False, result
        assert (sandbox / "sub" / "a.txt").read_text(encoding="utf-8") == "ok"

    @pytest.mark.asyncio
    async def test_read_and_list_are_also_contained(self, tmp_path):
        """读路径同样收口：`read_file` / `list_directory` 此前**完全没有**自己检查。"""
        from core.tools.filesystem.plugin import ListDirPlugin, ReadFilePlugin

        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        secret = secret_dir / "passwd"
        secret.write_text("top-secret", encoding="utf-8")

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        reader = ReadFilePlugin()
        reader.safety.read_dirs = [str(sandbox)]
        reader.safety.write_dir = str(sandbox)
        res = await reader.execute(
            ctx=self._Ctx(), params=reader.param_model(path=str(secret)))
        assert getattr(res, "success", True) is False, f"沙盒外文件被读到了：{res}"

        lister = ListDirPlugin()
        lister.safety.read_dirs = [str(sandbox)]
        lister.safety.write_dir = str(sandbox)
        res2 = await lister.execute(
            ctx=self._Ctx(), params=lister.param_model(path=str(secret_dir)))
        assert getattr(res2, "success", True) is False, f"沙盒外目录被列到了：{res2}"


# ══════════════════════════════════════════════════════════════════
# S2-7 / S2-8：启动与编排的守卫（脚本级，静态断言）
# ══════════════════════════════════════════════════════════════════

class TestStartScriptAndComposeGuards:
    """这两个文件的行为很难在单测里"真跑"，但它们的关键不变式可以**静态断言**——
    且这些不变式恰好都是"曾经缺失、缺失时无人发现"的那类。
    """

    @staticmethod
    def _repo_root():
        from pathlib import Path

        return Path(__file__).resolve().parents[3]

    def test_start_sh_takes_single_instance_lock(self):
        """S2-7：并发 `start.sh` 必须在**任何副作用之前**被拒。

        两个进程各自走完预检、各自 `docker compose up`、各自 exec python——第二个
        直到 bind 8765 才失败，而此前已经动过数据库与前端产物，两边都以为"服务在跑"。
        """
        raw = (self._repo_root() / "start.sh").read_text(encoding="utf-8")

        # **剥掉注释行再断言**：注释里出现 `docker compose` 会把 index 比较带偏
        # （本用例第一版就被自己写的说明文字骗了，报"flock 在 compose 之后"）。
        code = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("#"))

        assert "flock -n" in code, "缺少 flock 单实例守卫"
        # 锁必须早于数据库操作与前端构建（否则副作用已经发生）
        assert code.index("flock -n") < code.index("docker compose"), (
            "flock 必须在 docker compose 之前")
        assert code.index("flock -n") < code.index("npm ci"), (
            "flock 必须在前端构建之前")
        # 没有 flock 的平台要**显式告警**，不能静默跳过（否则守卫形同虚设而无人知）
        assert "未找到 flock" in code or "未找到 flock" in raw, "缺 flock 时必须有可见告警"

    def test_compose_data_services_have_healthchecks(self):
        """S2-8：数据服务必须声明 healthcheck，Milvus 必须等依赖**就绪**。"""
        import yaml

        doc = yaml.safe_load(
            (self._repo_root() / "docker-compose.yml").read_text(encoding="utf-8"))
        services = doc["services"]

        for name in ("mongodb", "etcd", "minio", "minio-prod", "milvus", "milvus-prod"):
            assert "healthcheck" in services[name], f"{name} 缺 healthcheck"

        for name in ("milvus", "milvus-prod"):
            deps = services[name]["depends_on"]
            assert isinstance(deps, dict), (
                f"{name}.depends_on 是列表 → compose 默认 condition=service_started，"
                f"容器**建起来**就算满足，不等就绪")
            for dep, cfg in deps.items():
                assert cfg.get("condition") == "service_healthy", (
                    f"{name} → {dep} 未要求 service_healthy")

    def test_compose_volumes_are_split_per_profile(self):
        """S2-8：dev 与 production 的 MinIO/Milvus 必须用**独立数据卷**。

        共卷会让两个 profile 互相覆写对方的数据格式/元数据（切换 profile 即数据损坏）。
        mongodb/etcd 是**有意共享**的（文件头注释已声明），不在此列。
        """
        import yaml

        doc = yaml.safe_load(
            (self._repo_root() / "docker-compose.yml").read_text(encoding="utf-8"))
        services, volumes = doc["services"], doc["volumes"]

        def _vols(name):
            return [v.split(":")[0] for v in services[name].get("volumes", [])]

        assert set(_vols("minio")).isdisjoint(_vols("minio-prod")), (
            "dev/production 的 MinIO 数据卷重叠")
        assert set(_vols("milvus")).isdisjoint(_vols("milvus-prod")), (
            "dev/production 的 Milvus 数据卷重叠")
        for v in ("minio_data", "minio_data_prod", "milvus_data", "milvus_data_prod"):
            assert v in volumes, f"{v} 未在顶层 volumes 声明"
