# 测试文件：core/schema/tests/test_image_source_security.py
"""CR-09 回归守卫：图片 src 不得成为「服务端任意相对路径读」的载体。

背景（`docs/review/2026-09-12-code-review-report` CR-09）：历史实现有两处把
**客户端可控字符串**变成**本地文件读取**：

1. `core/schema/adapters/image_source.py` 的兜底分支
   `local_path = Path(src); if local_path.exists(): read_bytes() → data URI`
2. `core/schema/utils.py::_validate_src` 的兜底
   `if Path(v).exists(): return v` —— 既构成文件系统探测，又恰好放行 1 的载荷

攻击链：`POST /api/chat` / v1 WS 的 `content` 是客户端可控 `list[dict]`，
`src=".env"` → 读出进程权限内任意文件 → base64 作为 image 送往第三方 LLM + 落库可读回。

本文件用**真实临时文件**证明「相对路径不再被读取」，而不是只断言字符串形状——
守卫要先证明自己能抓到违规，再相信它的绿灯。
"""



import pytest
from pydantic import ValidationError

from core.schema.adapters.image_source import resolve_image_source
from core.schema.utils import _validate_src

_SECRET = "DEEPSEEK_API_KEY=sk-CANARY-DO-NOT-LEAK\n"


@pytest.fixture
def secret_cwd(tmp_path, monkeypatch):
    """把 CWD 切到含「机密文件」的临时目录，并返回该文件的相对名。"""
    (tmp_path / "secret.env").write_text(_SECRET, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.env").write_text(_SECRET, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _assert_not_leaked(result: str) -> None:
    """断言返回值既不是 data URI，也不含机密内容。"""
    assert not result.startswith("data:"), \
        f"不应读取本地文件，实际返回 data URI: {result[:40]}"
    assert "sk-CANARY" not in result, f"泄漏了文件内容: {result!r}"


@pytest.mark.unit
class TestResolveImageSourceNoLocalRead:
    """resolve_image_source 不得读取客户端指定的本地路径。"""

    def test_relative_path_existing_file_not_read(self, secret_cwd):
        """存在的相对路径也不得被读取（原漏洞的最小复现）。"""
        _assert_not_leaked(resolve_image_source("secret.env"))

    def test_relative_path_returns_src_unchanged(self, secret_cwd):
        """解析失败一律原样返回（既有契约不变）。"""
        assert resolve_image_source("secret.env") == "secret.env"

    def test_parent_traversal_not_read(self, secret_cwd):
        """`../` 穿越形态同样不得读取。"""
        _assert_not_leaked(resolve_image_source("../secret.env"))
        _assert_not_leaked(resolve_image_source("sub/../secret.env"))

    def test_absolute_path_outside_whitelist_not_read(self, secret_cwd):
        """白名单目录外的绝对路径不得读取（只按 basename 在白名单目录查找）。"""
        target = str(secret_cwd / "secret.env")
        _assert_not_leaked(resolve_image_source(target))
        assert resolve_image_source(target) == target

    def test_tilde_and_dot_forms_not_read(self, secret_cwd):
        """常见绕过写法一律不读。"""
        for src in ("./secret.env", ".env", "~/.env", "sub/nested.env"):
            _assert_not_leaked(resolve_image_source(src))

    def test_missing_relative_path_passthrough(self):
        """不存在的相对路径同样原样返回。"""
        assert resolve_image_source("definitely-not-here.env") == "definitely-not-here.env"


@pytest.mark.unit
class TestResolveImageSourceLegitFormsPreserved:
    """收口不得误伤合法形态。"""

    def test_data_uri_passthrough(self):
        uri = "data:image/png;base64,AAAA"
        assert resolve_image_source(uri) == uri

    def test_remote_url_passthrough(self):
        url = "https://example.com/a.png"
        assert resolve_image_source(url) == url

    def test_localhost_url_without_local_file_passthrough(self):
        url = "http://localhost:8765/not-in-uploads.png"
        assert resolve_image_source(url) == url

    def test_attachment_path_without_registration_passthrough(self):
        """未注册 resolver 时 /api/attachments/{id} 原样返回（不抛异常）。"""
        src = "/api/attachments/att_deadbeef"
        assert resolve_image_source(src) == src

    def test_sticker_static_path_passthrough(self):
        src = "/static/stickers/st_happy.png"
        assert resolve_image_source(src) == src


@pytest.mark.unit
class TestValidateSrcRejectsLocalPaths:
    """_validate_src 只按形态白名单放行，且**不接触文件系统**。"""

    def test_relative_path_rejected_even_if_exists(self, secret_cwd):
        """文件确实存在也必须拒绝——原实现正是靠 exists() 放行的。"""
        assert (secret_cwd / "secret.env").exists()
        with pytest.raises(ValueError):
            _validate_src("secret.env")

    def test_parent_traversal_rejected(self, secret_cwd):
        with pytest.raises(ValueError):
            _validate_src("../secret.env")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _validate_src("")

    def test_accepts_whitelist_forms(self):
        for src in (
            "http://example.com/a.png",
            "https://example.com/a.png",
            "data:image/png;base64,AAAA",
            "/api/attachments/att_deadbeef",
            "/static/stickers/st_happy.png",
        ):
            assert _validate_src(src) == src

    def test_no_filesystem_probe(self, secret_cwd, monkeypatch):
        """校验期不得产生任何文件系统访问（原实现是探测预言机）。"""
        import pathlib

        calls: list[str] = []
        original = pathlib.Path.exists

        def _spy(self):  # noqa: ANN001 - 测试替身
            calls.append(str(self))
            return original(self)

        monkeypatch.setattr(pathlib.Path, "exists", _spy)
        for src in ("secret.env", "https://example.com/a.png", "data:image/png;base64,AAAA"):
            try:
                _validate_src(src)
            except ValueError:
                pass
        assert calls == [], f"_validate_src 不应访问文件系统，实际: {calls}"


@pytest.mark.unit
class TestPartsFromDictsRejectsLocalSrc:
    """边界转换层同样拒绝本地路径 src（纵深防御的第二层）。"""

    def test_parts_from_dicts_rejects_relative_src(self, secret_cwd):
        from core.schema.adapters.message_adapter import parts_from_dicts

        with pytest.raises(ValidationError):
            parts_from_dicts([{"type": "image", "src": "secret.env"}])

    def test_parts_from_dicts_accepts_canonical_forms(self):
        from core.schema.adapters.message_adapter import parts_from_dicts

        parts = parts_from_dicts([
            {"type": "text", "content": "看这张图"},
            {"type": "image", "src": "/api/attachments/att_deadbeef"},
            {"type": "image", "src": "https://example.com/a.png"},
        ])
        assert len(parts) == 3

# ── S2-H10：空 src（默认值绕过校验）不得产出坏 image 块 ──────────────────────

@pytest.mark.unit
def test_empty_src_does_not_touch_the_filesystem():
    """`src=""`（默认值，pydantic 不校验默认值）必须原样返回、不碰文件系统。

    报告记的 `IsADirectoryError` 来自已被删除的相对路径兜底分支（`Path("")` == `.`）
    ——CR-09 移除了它，本用例把"不碰 fs、不抛异常"这个不变式钉住。
    """
    assert resolve_image_source("") == ""


@pytest.mark.unit
def test_empty_src_image_part_is_skipped_in_openai_conversion():
    """空 src 的 ImagePart 必须**跳过**，否则产出 `{"url": ""}` → provider 判非法请求。

    这是 S2-H10 的真实残留：legacy 会话里的坏 Part **每轮**重建进 prompt，
    一个坏记录就能让该会话永久无法对话。
    """
    from core.schema.adapters.message_adapter import content_to_openai
    from core.schema.part import ImagePart, TextPart

    blocks = content_to_openai([ImagePart(), TextPart(content="hi")])
    assert isinstance(blocks, list)
    assert all(b.get("type") != "image_url" for b in blocks), blocks
    assert any(b.get("type") == "text" and b["text"] == "hi" for b in blocks)


@pytest.mark.unit
def test_healthy_image_part_still_converted():
    """对照：正常 src 仍必须产出 image 块（别把整个 image 路径一并关掉）。"""
    from core.schema.adapters.message_adapter import content_to_openai
    from core.schema.part import ImagePart, TextPart

    blocks = content_to_openai([
        ImagePart(src="https://example.com/a.png"), TextPart(content="hi")])
    assert any(b.get("type") == "image_url"
               and b["image_url"]["url"] == "https://example.com/a.png" for b in blocks), blocks
