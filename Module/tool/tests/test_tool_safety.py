"""安全函数单元测试 — _resolve_sandbox_path, _validate_path。"""
import pytest
from pathlib import Path

pytestmark = pytest.mark.unit
from Module.tool.safety import (
    _resolve_sandbox_path, _validate_path, SafetyProfile,
)


# ---------------------------------------------------------------------------
# TestResolveSandboxPath（沙箱路径解析测试）
# ---------------------------------------------------------------------------

class TestResolveSandboxPath:
    """测试 _resolve_sandbox_path — sandbox 路径解析。

    函数签名：
        _resolve_sandbox_path(path, profile, session_id, is_write)
        -> tuple[Path, str | None]
    """

    def test_relative_read_path_resolves_inside_read_dirs(self, tmp_path):
        """相对路径读 → 使用 read_dirs[0] 作为基准目录。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("hello")

        profile = SafetyProfile(
            filesystem_allowed=True,
            read_dirs=[str(sandbox)],
        )
        resolved, err = _resolve_sandbox_path(
            path="file.txt",
            profile=profile,
            session_id="test",
            is_write=False,
        )
        assert err is None
        assert resolved == (sandbox / "file.txt").resolve()

    def test_absolute_path_stays(self, tmp_path):
        """绝对路径不变，只做 expanduser + resolve。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        (sandbox / "file.txt").write_text("hello")

        profile = SafetyProfile(
            filesystem_allowed=True,
            read_dirs=[str(sandbox)],
        )
        resolved, err = _resolve_sandbox_path(
            path=str(sandbox / "file.txt"),
            profile=profile,
            session_id="test",
            is_write=False,
        )
        assert err is None
        assert resolved == (sandbox / "file.txt").resolve()

    def test_relative_write_path_uses_write_dir_template(self, tmp_path):
        """相对路径写 → 使用 write_dir 模板（替换 {session_id}）。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        write_sub = sandbox / "data" / "sandbox" / "sess1"
        write_sub.mkdir(parents=True)

        profile = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox / "data" / "sandbox" / "{session_id}"),
            read_dirs=[str(sandbox)],
        )
        resolved, err = _resolve_sandbox_path(
            path="output.txt",
            profile=profile,
            session_id="sess1",
            is_write=True,
        )
        assert err is None
        assert resolved == (write_sub / "output.txt").resolve()

    def test_write_path_outside_write_dir_returns_error(self, tmp_path):
        """写入路径不在 write_dir 下时返回错误字符串。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        profile = SafetyProfile(
            filesystem_allowed=True,
            write_dir=str(sandbox / "{session_id}"),
        )
        resolved, err = _resolve_sandbox_path(
            path=str(outside / "secret.txt"),
            profile=profile,
            session_id="test",
            is_write=True,
        )
        assert err is not None
        assert "必须在" in err

    def test_expanduser_resolves_tilde(self, tmp_path):
        """路径中的 ~ 被 expanduser 展开。"""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        profile = SafetyProfile(
            filesystem_allowed=True,
            read_dirs=[str(sandbox)],
        )
        resolved, err = _resolve_sandbox_path(
            path=str(sandbox / "file.txt"),
            profile=profile,
            session_id="test",
            is_write=False,
        )
        assert err is None
        assert "~" not in str(resolved)


# ---------------------------------------------------------------------------
# TestValidatePath（路径校验测试）
# ---------------------------------------------------------------------------

class TestValidatePath:
    """测试 _validate_path — 路径安全检查。

    函数签名：
        _validate_path(resolved, profile) -> str | None
    """

    def test_file_within_read_dirs_passes(self, tmp_path):
        """文件在 read_dirs 内且未超限 → 通过。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "file.txt").write_text("hello")

        profile = SafetyProfile(
            read_dirs=[str(read_dir)],
            allow_symlinks=True,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=read_dir / "file.txt",
            profile=profile,
        )
        assert err is None

    def test_file_too_large_returns_error(self, tmp_path):
        """文件超过 max_file_bytes → 返回错误。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "big.txt").write_text("x" * 2000)

        profile = SafetyProfile(
            read_dirs=[str(read_dir)],
            allow_symlinks=True,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=read_dir / "big.txt",
            profile=profile,
        )
        assert err is not None
        assert "文件过大" in err

    def test_symlink_detection_returns_error(self, tmp_path):
        """allow_symlinks=False 时符号链接返回错误。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "real.txt").write_text("real")
        (read_dir / "link.txt").symlink_to(read_dir / "real.txt")

        profile = SafetyProfile(
            read_dirs=[str(read_dir)],
            allow_symlinks=False,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=read_dir / "link.txt",
            profile=profile,
        )
        assert err is not None
        assert "符号链接" in err

    def test_symlink_allowed_passes(self, tmp_path):
        """allow_symlinks=True 时符号链接通过。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "real.txt").write_text("real")
        (read_dir / "link.txt").symlink_to(read_dir / "real.txt")

        profile = SafetyProfile(
            read_dirs=[str(read_dir)],
            allow_symlinks=True,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=read_dir / "link.txt",
            profile=profile,
        )
        assert err is None

    def test_path_not_in_read_dirs_returns_error(self, tmp_path):
        """路径不在任何 read_dir 下 → 返回错误。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        outside = tmp_path / "outside" / "file.txt"
        outside.parent.mkdir()
        outside.write_text("secret")

        profile = SafetyProfile(
            read_dirs=[str(read_dir)],
            allow_symlinks=True,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=outside,
            profile=profile,
        )
        assert err is not None
        assert "不在允许的目录" in err

    def test_no_read_dirs_configured_passes(self, tmp_path):
        """未配置 read_dirs 时跳过目录检查。"""
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        (read_dir / "file.txt").write_text("hello")

        profile = SafetyProfile(
            read_dirs=None,
            allow_symlinks=True,
            max_file_bytes=1024,
        )
        err = _validate_path(
            resolved=read_dir / "file.txt",
            profile=profile,
        )
        assert err is None


class TestContainmentHardening:
    """D1/P1-14：sibling 前缀与 symlink 中间组件不再逃逸。"""

    def test_sibling_prefix_rejected(self, tmp_path):
        """允许 /sandbox，请求 /sandbox_evil/secret → 拒绝（startswith 误判修复）。"""
        from Module.tool.safety import _resolve_sandbox_path, _validate_path, SafetyProfile
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        evil = tmp_path / "sandbox_evil"
        evil.mkdir()
        (evil / "secret.txt").write_text("s")

        profile = SafetyProfile(read_dirs=[str(sandbox)], allow_symlinks=False)
        resolved, err = _resolve_sandbox_path(str(evil / "secret.txt"), profile,
                                              session_id="default", is_write=False)
        assert err is None  # 路径本身解析成功
        assert _validate_path(resolved, profile, raw=evil / "secret.txt") is not None

    def test_symlink_middle_component_rejected(self, tmp_path):
        """路径中间组件是 symlink（resolve 后指向外部）→ 拒绝。"""
        from Module.tool.safety import _resolve_sandbox_path, _validate_path, SafetyProfile
        read_dir = tmp_path / "read"
        read_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s")
        sub = read_dir / "sub"
        sub.mkdir()
        (sub / "link").symlink_to(outside)

        profile = SafetyProfile(read_dirs=[str(read_dir)], allow_symlinks=False)
        target = sub / "link" / "secret.txt"
        resolved, err = _resolve_sandbox_path(str(target), profile,
                                              session_id="default", is_write=False)
        # resolved 会跟随 symlink 到 outside/secret.txt——但 raw 链含 symlink → 拒绝
        assert _validate_path(resolved, profile, raw=target) is not None


class TestLogRedaction:
    """D4/P1-17：日志脱敏——敏感字段值不落日志。"""

    def test_sensitive_keys_redacted(self):
        from Module.tool.safety import _safe_arg_summary
        summary = _safe_arg_summary({
            "note": "今天天气",
            "api_key": "sk-secret-12345",
            "content": "私聊正文",
            "path": "/home/user/secret.txt",
        })
        assert "sk-secret-12345" not in summary
        assert "私聊正文" not in summary
        assert "/home/user/secret.txt" not in summary
        assert "api_key=<redacted>" in summary
        assert "content=<redacted>" in summary
        assert "path=<redacted>" in summary
        # review 复核 #9：deny-by-default——非白名单字符串值只记长度，不记原值
        assert "note=<str:4>" in summary
        assert "今天天气" not in summary

    def test_string_value_deny_by_default(self):
        """review 复核 #9：默认拒绝——任意字符串参数（如 Reminder 的 message）值不落
        日志，只记长度；杜绝正文/提醒内容类泄露。"""
        from Module.tool.safety import _safe_arg_summary
        summary = _safe_arg_summary({"message": "提醒内容很敏感"})
        assert "message=<str:7>" in summary
        assert "提醒内容很敏感" not in summary

    def test_non_sensitive_long_value_truncated(self):
        from Module.tool.safety import _safe_arg_summary
        summary = _safe_arg_summary({"note": "a" * 100}, max_len=10)
        # deny-by-default：长度而非截断值
        assert "note=<str:100>" in summary
        assert "…" not in summary

    def test_validation_error_format_no_input(self, caplog):
        """safety 的 validation 错误格式化不含 invalid input。"""
        import logging
        from pydantic import BaseModel, Field

        class M(BaseModel):
            content: str = Field(max_length=3)

        try:
            M(content="超长的非法输入值XYZ")
        except Exception as e:
            from Module.tool.safety import _format_validation_error
            text = _format_validation_error(e)
        assert "超长的非法输入值XYZ" not in text
        assert "content" in text
