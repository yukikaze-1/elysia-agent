"""POST /api/uploads 通用上传端点测试（批次 7 Task 3，marker=service）。

构造：真实 Agent（attachment_repo property 依赖）+ 内存 fake AttachmentRepository
（经 agent._attachment_repo 注入，noqa: SLF001）+ aiohttp test client
（pytest-aiohttp 的 aiohttp_client fixture，仿 test_server.py）。

上传目录经 ELYSIA_PATH_DATA_UPLOADS(+_IMAGES/_DESKTOP) 重定向到 tmp_path，
不触碰生产 data/uploads。
"""

import hashlib

import pytest
from aiohttp import FormData

from core.input.ws_v2 import _attachment_part
from core.schema.adapters import attachment_resolver as _ar
from core.schema.utils import normalize_attachment_mime
from core.storage.attachment_repo import StorageUnavailableError

pytestmark = pytest.mark.service

# ── 各类型魔数字节（filetype.guess 可识别的真实头部）───────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
TEXT = b"this is a plain text file masquerading as a png, nothing more" * 3

# P0-02 新增魔数（Elysia env 实测 filetype 可识别）
WEBM = bytes.fromhex("1a45dfa39f4286810142f7810142f2810442f381084282847765626d4287810442858102")  # video/webm
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8 + b"mp42isom"  # video/mp4
PDF = b"%PDF-1.4\n" + b"1 0 obj\n<<>>\nendobj\n"  # application/pdf
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00"  # audio/x-wav（filetype 探测）


class _FakeAttachmentRepo:
    """内存 fake AttachmentRepository——离线时 create 抛 StorageUnavailableError。

    与真实 AttachmentRepository 的差异只在存储介质（list vs Mongo）：
    上传 handler 只调用 create()/delete_record()，故最小接口即够；fail-closed
    语义如实模拟。fail_on_create 让第 N 次 create 抛错——测多文件持久化中途失败
    的「已建记录也要清理」路径。
    """

    def __init__(self, offline: bool = False, fail_on_create: int | None = None) -> None:
        self.offline = offline
        self.fail_on_create = fail_on_create
        self.records: list[dict] = []

    async def create(self, doc: dict) -> None:
        if self.offline:
            raise StorageUnavailableError("attachments 集合不可用：MongoDB 未连接（离线）")
        if self.fail_on_create is not None and len(self.records) + 1 == self.fail_on_create:
            raise StorageUnavailableError("attachments 集合不可用：MongoDB 未连接（离线）")
        self.records.append(dict(doc))

    async def delete_record(self, attachment_id: str) -> None:
        self.records = [r for r in self.records if r["attachment_id"] != attachment_id]


@pytest.fixture(autouse=True)
def _clear_resolver_registry():
    """清空 AttachmentFileResolver 模块级注册表，防跨测试污染（服务端模块级缓存）。"""
    _ar._registry.clear()
    yield
    _ar._registry.clear()


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """返回构建 upload app 的工厂（真实 Agent + 注入 fake attachment repo）。"""

    def _make(*, sig_secret: str = "test-secret", access_token: str = "",
              offline: bool = False, max_request_bytes: int | None = None,
              fail_on_create: int | None = None):
        from core.agent import Agent
        from core.config import AppConfig
        from core.config.app_config import ServerConfig
        from core.input.server import create_app

        agent = Agent(config=AppConfig(server=ServerConfig(
            access_token=access_token,
            attachment_sig_secret=sig_secret,
        )))
        agent._attachment_repo = _FakeAttachmentRepo(  # noqa: SLF001
            offline=offline, fail_on_create=fail_on_create)
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS", str(tmp_path / "uploads"))
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS_IMAGES",
                           str(tmp_path / "uploads" / "images"))
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS_DESKTOP",
                           str(tmp_path / "uploads" / "desktop"))
        app = create_app(agent, serve_static=False,
                         access_token=access_token,
                         max_request_bytes=max_request_bytes)
        return agent, app

    return _make


async def _post(client, *, filename="photo.png", content=PNG, kind="image",
                extra_files: list[tuple[str, bytes]] | None = None,
                headers=None):
    """构造浏览器同款 multipart 并 POST /api/uploads。"""
    form = FormData()
    if kind is not None:
        form.add_field("kind", kind)
    form.add_field("files[]", content, filename=filename,
                   content_type="application/octet-stream")
    for name, data in (extra_files or []):
        form.add_field("files[]", data, filename=name,
                       content_type="application/octet-stream")
    return await client.post("/api/uploads", data=form, headers=headers)


async def _post_kinds(client, parts):
    """构造逐文件 kind 的 multipart（前端 real-backend.ts 同款：每文件一组 kind+files）。

    Args:
        parts: list[(kind, filename, content)]，kind 为 None 表示该文件无 kind（魔数判定）。
    """
    form = FormData()
    for kind, name, content in parts:
        if kind is not None:
            form.add_field("kind", kind)
        form.add_field("files[]", content, filename=name,
                       content_type="application/octet-stream")
    return await client.post("/api/uploads", data=form)


class TestUploadEndpoint:
    """POST /api/uploads 通用上传校验矩阵（spec §4.1 顺序）。"""

    @pytest.mark.asyncio
    async def test_forged_extension_415(self, make_app, aiohttp_client):
        """伪造类型：改名 .png 的文本文件 → filetype 魔数判定失败 → 415 UNSUPPORTED_MEDIA_TYPE。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="evil.png", content=TEXT, kind="image")
        assert resp.status == 415, f"expected 415, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "UNSUPPORTED_MEDIA_TYPE"
        # 未落盘、未写记录
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename,mime,content", [
        ("notes.txt", "text/plain", b"hello world\n" * 10),
        ("doc.md", "text/markdown", b"# Title\n\nsome markdown body\n"),
        ("data.json", "application/json", b'{"key": "value", "n": 1}'),
    ])
    async def test_text_file_uploads_as_file(self, make_app, aiohttp_client, tmp_path,
                                             filename, mime, content):
        """纯文本（txt/md/json）→ filetype 魔数返回 None → 扩展名白名单 + UTF-8 sanity → 归 file（spec §4.1 I6）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename=filename, content=content, kind="file")
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        att = data["attachments"][0]
        assert att["kind"] == "file"
        assert att["mime"] == mime
        assert att["filename"] == filename
        # 落盘 data/uploads/file/{uuid}.{ext}
        ext = filename.rsplit(".", 1)[-1]
        assert len(list((tmp_path / "uploads" / "file").glob(f"*.{ext}"))) == 1
        # 写记录（kind/mime 正确）
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        assert record["kind"] == "file"
        assert record["mime"] == mime

    @pytest.mark.asyncio
    async def test_text_renamed_png_415(self, make_app, aiohttp_client):
        """文本内容改名 .png → 415（文本扩展名白名单只认 txt/md/json，扩展名不一致仍应拒）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="notes.png", content=b"plain text content", kind="file")
        assert resp.status == 415, f"expected 415, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "UNSUPPORTED_MEDIA_TYPE"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_text_invalid_utf8_415(self, make_app, aiohttp_client):
        """.txt 但内容非合法 UTF-8 → 415（UTF-8 content sanity 失败，I6）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        bad = b"\xff\xfe\x00\x01" + b"\x00" * 100  # 非 UTF-8 字节序列
        resp = await _post(client, filename="data.txt", content=bad, kind="file")
        assert resp.status == 415, f"expected 415, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "UNSUPPORTED_MEDIA_TYPE"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_too_many_files_400(self, make_app, aiohttp_client):
        """数量超限：单次 11 文件 → 400 TOO_MANY_FILES。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        extra = [(f"f{i}.png", PNG) for i in range(1, 11)]
        resp = await _post(client, filename="f0.png", content=PNG, kind="image",
                           extra_files=extra)
        assert resp.status == 400, f"expected 400, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "TOO_MANY_FILES"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_kind_sticker_400(self, make_app, aiohttp_client):
        """kind=sticker（非附件白名单）→ 400 CONTRACT_VALIDATION。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="st.png", content=PNG, kind="sticker")
        assert resp.status == 400, f"expected 400, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "CONTRACT_VALIDATION"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_success_registers_attachment(self, make_app, aiohttp_client, tmp_path):
        """成功：attachmentId + signedUrl 含 sig=/exp= + 落盘 + 写记录 + resolver 已注册。"""
        agent, app = make_app(sig_secret="test-secret")
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image")
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert len(data["attachments"]) == 1
        att = data["attachments"][0]
        assert att["attachmentId"].startswith("att_")
        assert att["kind"] == "image"
        assert att["mime"] == "image/png"
        assert att["size"] == len(PNG)
        assert att["url"] == f"/api/attachments/{att['attachmentId']}"
        assert "sig=" in att["signedUrl"] and "exp=" in att["signedUrl"]
        # 落盘 data/uploads/image/{uuid}.png
        files = list((tmp_path / "uploads" / "image").glob("*.png"))
        assert len(files) == 1
        # 写 attachments 记录
        records = agent._attachment_repo.records  # noqa: SLF001
        assert len(records) == 1
        assert records[0]["attachment_id"] == att["attachmentId"]
        assert records[0]["kind"] == "image"
        assert records[0]["mime"] == "image/png"
        assert records[0]["filename"] == "photo.png"
        assert records[0]["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert records[0]["ref_count"] == 0
        assert records[0]["status"] == "active"
        # AttachmentFileResolver 已注册
        resolved = _ar.resolve(att["attachmentId"])
        assert resolved is not None and resolved.exists()

    @pytest.mark.asyncio
    async def test_offline_503_and_no_orphan(self, make_app, aiohttp_client, tmp_path):
        """Mongo 离线（fake 抛 StorageUnavailableError）→ 503 + 已落盘文件被删（不留孤儿）。"""
        agent, app = make_app(offline=True)
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image")
        assert resp.status == 503, f"expected 503, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "STORAGE_UNAVAILABLE"
        # 写记录失败 → 文件立即删除
        assert not list((tmp_path / "uploads" / "image").glob("*.png"))
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_upload_kind_field_size_capped(self, make_app, aiohttp_client):
        """kind 字段超长（>64 字节）→ 400 INVALID_REQUEST——不读入超长字段内存即拒。

        旧实现整读 kind 字段后挂「未知 kind」CONTRACT_VALIDATION；新实现限长分支
        （read_chunk(65) 检出 >64）在未知 kind 预检之前，断言 error code 区分两种
        拒绝路径。用「合法 kind 值 + 超长后缀」构造，验证的是限长分支而非未知 kind 分支。
        """
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG,
                           kind="image" + "x" * 100)
        assert resp.status == 400, f"expected 400, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "INVALID_REQUEST"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_upload_second_file_invalid_no_orphan(self, make_app, aiohttp_client,
                                                        tmp_path):
        """第 2 个文件魔数非法 → 第 1 个文件不残留（先全部校验再统一写盘）。

        旧实现逐文件「校验+写盘+建记录」→ 第 1 个文件已落盘残留；新实现两阶段
        （先全部 _validate 通过再统一 _persist）→ 415 时无任何文件/记录/注册。
        """
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image",
                           extra_files=[("evil.png", TEXT)])
        assert resp.status == 415, f"expected 415, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "UNSUPPORTED_MEDIA_TYPE"
        # 先全部校验再写盘：第 1 个文件不落盘（目录为空）
        assert not list((tmp_path / "uploads" / "image").glob("*.png"))
        # 无记录、无注册
        assert not agent._attachment_repo.records  # noqa: SLF001
        assert not _ar._registry  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_persist_failure_cleans_prior_writes(self, make_app, aiohttp_client,
                                                       tmp_path):
        """持久化阶段第 2 个文件 create 失败 → 第 1 个文件也被清理（不留孤儿）。

        两文件都通过校验（阶段 A 全绿），阶段 B 第 2 个文件落记录失败
        （StorageUnavailableError → 503）：本轮已写文件 + 已建记录 + 已注册映射
        必须全部清理，而不是只回滚失败文件。
        """
        agent, app = make_app(fail_on_create=2)
        client = await aiohttp_client(app)
        resp = await _post(client, filename="a.png", content=PNG, kind="image",
                           extra_files=[("b.png", PNG)])
        assert resp.status == 503, f"expected 503, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "STORAGE_UNAVAILABLE"
        # 第 1 个文件已写盘/建记录/注册，也被统一清理
        assert not list((tmp_path / "uploads" / "image").glob("*.png"))
        assert not agent._attachment_repo.records  # noqa: SLF001
        assert not _ar._registry  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_upload_reads_max_request_bytes_from_config(self, make_app, aiohttp_client):
        """/api/uploads 聚合上限读 agent.config.server.max_request_bytes。"""
        agent, app = make_app()  # app cms 缺省 64MB，handler cms 走 config
        agent.config.server.max_request_bytes = 100  # noqa: SLF001 — 注入小上限
        client = await aiohttp_client(app)
        resp = await _post(client, filename="big.bin", content=b"\x00" * 1000, kind="file")
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_no_bearer_401(self, make_app, aiohttp_client):
        """配置 AGENT_ACCESS_TOKEN 时无 Bearer → 401 AUTH_REQUIRED。"""
        agent, app = make_app(access_token="secret-token")
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image")
        assert resp.status == 401, f"expected 401, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "AUTH_REQUIRED"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_oversize_multipart_413(self, make_app, aiohttp_client):
        """整体 multipart Content-Length > 64MB → 413 REQUEST_TOO_LARGE（中间件预检）。"""
        agent, app = make_app()  # cms 默认 64MB
        client = await aiohttp_client(app)
        big = b"\x00" * (64 * 1024 * 1024 + 1)
        resp = await _post(client, filename="big.png", content=big, kind="image")
        assert resp.status == 413, f"expected 413, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "REQUEST_TOO_LARGE"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_aggregate_oversize_chunked_413(self, make_app, aiohttp_client):
        """chunked 多文件累计超 cms → 413 REQUEST_TOO_LARGE（handler total_bytes 强制）。

        专测无 Content-Length（chunked）场景：中间件 Content-Length 预检失效，聚合上限
        只能由 handler 内累计兜底。agent.config.server.max_request_bytes 收敛到 5MB
        （create_app 仍 64MB，不预检 6MB Content-Length）——两文件累计 6MB 触发中断。
        """
        agent, app = make_app()
        agent.config.server.max_request_bytes = 5 * 1024 * 1024  # 收敛 cms 以低内存触发
        client = await aiohttp_client(app)
        f1 = b"\x00" * (3 * 1024 * 1024)  # 3MB
        f2 = b"\x00" * (3 * 1024 * 1024)  # 3MB → 累计 6MB > 5MB
        resp = await _post(client, filename="a.png", content=f1, kind="image",
                           extra_files=[("b.png", f2)])
        assert resp.status == 413, f"expected 413, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["code"] == "REQUEST_TOO_LARGE"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_filename_sanitized(self, make_app, aiohttp_client):
        """filename 脱敏：../../evil.png → 返回 filename 不含路径分隔符。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="../../evil.png", content=PNG, kind="image")
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        filename = data["attachments"][0]["filename"]
        assert "/" not in filename and "\\" not in filename
        # 落盘名是服务端 uuid.ext，绝不参与路径构造
        stored = agent._attachment_repo.records[0]["stored_name"]  # noqa: SLF001
        assert stored == stored.replace("/", "").replace("\\", "")

    @pytest.mark.asyncio
    async def test_fail_closed_omits_signed_url(self, make_app, aiohttp_client):
        """AGENT_ACCESS_TOKEN 配 + SIG 未配 → 成功响应缺省 signedUrl（fail-closed）。"""
        agent, app = make_app(access_token="secret-token", sig_secret="")
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image",
                           headers={"Authorization": "Bearer secret-token"})
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert "signedUrl" not in data["attachments"][0]
        assert data["attachments"][0]["url"].startswith("/api/attachments/att_")


class TestStaticMounts:
    """静态挂载收敛（server.py）：新 kind 单数目录不公开；legacy images/desktop 仍挂载。"""

    @pytest.mark.asyncio
    async def test_new_kind_dir_not_publicly_mounted(self, make_app, aiohttp_client):
        """上传落盘 data/uploads/image/ 后，GET /uploads/image/{uuid}.png → 404（路由不存在）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="photo.png", content=PNG, kind="image")
        assert resp.status == 200
        stored = agent._attachment_repo.records[0]["stored_name"]  # noqa: SLF001
        resp = await client.get(f"/uploads/image/{stored}")
        assert resp.status == 404, f"新 kind 单数目录不应被公开挂载: {resp.status}"

    @pytest.mark.asyncio
    async def test_legacy_images_dir_still_mounted(self, make_app, aiohttp_client, tmp_path):
        """legacy data/uploads/images 目录仍被挂载（既有 /api/images 链路兼容）。"""
        legacy_dir = tmp_path / "uploads" / "images"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "legacy.png").write_bytes(PNG)
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await client.get("/uploads/images/legacy.png")
        assert resp.status == 200, f"legacy images 目录应被挂载: {resp.status}"
        # 不存在的 legacy 文件 → 404（路由已挂载，非 500/502）
        resp = await client.get("/uploads/images/not-exist.png")
        assert resp.status == 404


class TestF13Chunked413Envelope:
    """F13：chunked（无 Content-Length）超限 body → 413 REQUEST_TOO_LARGE 信封（非 400）。

    中间件 Content-Length 预检对 chunked 失效，aiohttp 在读体时抛
    HTTPRequestEntityTooLarge——handler 宽 except Exception 必须放行（re-raise）
    交给 server.py `_make_payload_middleware` 统一 413 信封，否则被吞成 400。
    """

    async def _chunked_json(self):
        """chunked JSON body（async generator → 无 Content-Length）。"""
        yield b'{"content": "'
        yield b"x" * 5000
        yield b'"}'

    async def _chunked_multipart(self):
        """chunked multipart body（async generator → 无 Content-Length）。"""
        yield b"--boundary8c\r\n"
        yield b'Content-Disposition: form-data; name="kind"\r\n\r\n'
        yield b"image\r\n"
        yield b"--boundary8c\r\n"
        yield b'Content-Disposition: form-data; name="files[]"; filename="big.png"\r\n'
        yield b"Content-Type: application/octet-stream\r\n\r\n"
        yield b"\x00" * 5000
        yield b"\r\n--boundary8c--\r\n"

    @pytest.mark.asyncio
    async def test_chat_chunked_overlimit_413_envelope(self, aiohttp_client):
        """chunked（无 Content-Length）超限 json → 413 REQUEST_TOO_LARGE（非 400）。"""
        from unittest.mock import MagicMock
        from core.agent import RunState
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        agent.process_message = None
        app = create_app(agent, serve_static=False, max_request_bytes=100)
        client = await aiohttp_client(app)
        resp = await client.post("/api/chat", data=self._chunked_json(),
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_upload_chunked_overlimit_413_envelope(self, make_app, aiohttp_client):
        """chunked multipart 超限 → 413 REQUEST_TOO_LARGE（非 400/415/500）。

        multipart 读体走 read_chunk（不经 request.read()/field.read()），aiohttp 的
        client_max_size 不在该路径触发——聚合上限由 handler 自算 cms（agent 配置同源）
        total_bytes 强制。收敛 handler cms 使小 body 即超限。
        """
        agent, app = make_app()
        agent.config.server.max_request_bytes = 100  # 收敛 handler 自算 cms（与 create_app 同源语义）
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/uploads", data=self._chunked_multipart(),
            headers={"Content-Type": "multipart/form-data; boundary=boundary8c"})
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_sessions_create_chunked_overlimit_413_envelope(self, aiohttp_client):
        """chunked 超限 json → POST /api/sessions → 413（非 400 CONTRACT_VALIDATION）。"""
        from unittest.mock import MagicMock
        from core.agent import RunState
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        app = create_app(agent, serve_static=False, max_request_bytes=100)
        client = await aiohttp_client(app)
        resp = await client.post("/api/sessions", data=self._chunked_json(),
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_ltm_insert_chunked_overlimit_413_envelope(self, aiohttp_client):
        """chunked 超限 json → POST /debug/api/ltm → 413（回归守卫）。"""
        from unittest.mock import MagicMock
        from core.agent import RunState
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        app = create_app(agent, serve_static=False, max_request_bytes=100)
        client = await aiohttp_client(app)
        resp = await client.post("/debug/api/ltm", data=self._chunked_json(),
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 413
        assert (await resp.json())["code"] == "REQUEST_TOO_LARGE"


class TestPerFileKind:
    """P0-02：/api/uploads 逐文件 kind（spec 阶段 3 联调整改；前端每文件一组 kind+files）。

    覆盖清单 3.3：混合批次逐项 kind、webm 消歧、files/files[] 兼容、单 kind 旧形态、
    悬空/邻接/未知 400、逐 hint 大小预检、魔数权威、整批回滚。
    """

    @pytest.mark.asyncio
    async def test_mixed_kinds_each_correct(self, make_app, aiohttp_client):
        """image+video+file 混合批次成功且逐项 kind 正确。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("video", "b.mp4", MP4), ("file", "c.pdf", PDF),
        ])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert [a["kind"] for a in data["attachments"]] == ["image", "video", "file"]
        assert [a["mime"] for a in data["attachments"]] == [
            "image/png", "video/mp4", "application/pdf"]

    @pytest.mark.asyncio
    async def test_webm_audio_video_disambiguated(self, make_app, aiohttp_client):
        """audio/webm + video/webm 同批分别归 audio/video（hint tiebreaker）+ mime 各归其位。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("audio", "voice.webm", WEBM), ("video", "clip.webm", WEBM),
        ])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        atts = (await resp.json())["attachments"]
        assert [a["kind"] for a in atts] == ["audio", "video"]
        assert [a["mime"] for a in atts] == ["audio/webm", "video/webm"]  # 新增：audio 不再 video/webm

    @pytest.mark.asyncio
    async def test_webm_audio_mime_normalized(self, make_app, aiohttp_client):
        """kind=audio + webm → 响应/记录 mime=audio/webm（BE-S3-RUN-P0-02）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.webm", WEBM)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "audio"
        assert att["mime"] == "audio/webm"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        assert record["mime"] == "audio/webm"

    @pytest.mark.asyncio
    async def test_wav_audio_mime_normalized(self, make_app, aiohttp_client):
        """WAV → 响应/记录 mime=audio/wav（探测 audio/x-wav → 权威 audio/wav）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.wav", WAV)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "audio"
        assert att["mime"] == "audio/wav"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        assert record["mime"] == "audio/wav"

    @pytest.mark.asyncio
    async def test_new_record_replay_mime_consistent(self, make_app, aiohttp_client):
        """新上传 WAV → _attachment_part 恢复 mime 与上传响应 AttachmentInfo.mime 一致（spec §4.3 case 3）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.wav", WAV)])
        assert resp.status == 200
        att = (await resp.json())["attachments"][0]
        assert att["mime"] == "audio/wav"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        part = _attachment_part("audio", att["attachmentId"], record)
        assert part.mime == att["mime"]  # 恢复 mime == 上传响应 mime

    @pytest.mark.asyncio
    async def test_files_and_files_array_both_work(self, make_app, aiohttp_client):
        """files 与旧 files[] 字段均可用（现有 _post 用 files[]；本 helper 亦 files[]）。
        files 单数形态：显式 FormData 用 name='files'。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("files", PNG, filename="single.png",
                       content_type="application/octet-stream")
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 200
        assert (await resp.json())["attachments"][0]["kind"] == "image"

    @pytest.mark.asyncio
    async def test_single_kind_multi_files_legacy(self, make_app, aiohttp_client):
        """旧形态：单 kind + 多文件 → 全部文件按该 kind 预检/归位（整批统一）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post(client, filename="a.png", content=PNG, kind="image",
                           extra_files=[("b.png", PNG)])
        assert resp.status == 200
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["image", "image"]

    @pytest.mark.asyncio
    async def test_dangling_kind_400(self, make_app, aiohttp_client):
        """尾部悬空 kind（kind 后无文件）→ 400 CONTRACT_VALIDATION。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("files[]", PNG, filename="a.png",
                       content_type="application/octet-stream")
        form.add_field("kind", "video")  # 悬空
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_adjacent_kind_400(self, make_app, aiohttp_client):
        """kind 未紧邻文件（kind,kind,files 连续）→ 400（顺序歧义，清单 3.3）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        form = FormData()
        form.add_field("kind", "image")
        form.add_field("kind", "video")  # 前一 kind 未被文件消费即来新 kind
        form.add_field("files[]", PNG, filename="a.png",
                       content_type="application/octet-stream")
        resp = await client.post("/api/uploads", data=form)
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_unknown_kind_mixed_400(self, make_app, aiohttp_client):
        """混合批次含非法 kind → 400（sticker 非附件白名单保持）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("sticker", "st.png", PNG)])
        assert resp.status == 400
        assert (await resp.json())["code"] == "CONTRACT_VALIDATION"
        assert not agent._attachment_repo.records  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_big_video_not_blocked_by_last_image_limit(self, make_app, aiohttp_client):
        """逐 hint 预检：前大视频（>10MB 但 <50MB，hint=video）不被最后 image 10MB 误拒。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        big_mp4 = MP4 + b"\x00" * (12 * 1024 * 1024)  # 12MB video/mp4
        resp = await _post_kinds(client, [
            ("video", "big.mp4", big_mp4), ("image", "a.png", PNG)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["video", "image"]

    @pytest.mark.asyncio
    async def test_magic_beats_hint(self, make_app, aiohttp_client):
        """魔数与提示冲突时以服务端魔数归位为准（hint=video 的 PNG → image）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("video", "a.png", PNG)])
        assert resp.status == 200
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "image"
        assert att["mime"] == "image/png"

    @pytest.mark.asyncio
    async def test_no_kind_all_files_magic_detect(self, make_app, aiohttp_client):
        """全批无 kind（hint=None 合法路径）：全部文件按魔数判定（LOW-3 补洞）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            (None, "a.png", PNG), (None, "b.pdf", PDF)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["image", "file"]

    @pytest.mark.asyncio
    async def test_multi_kind_no_hint_file_magic_detected(self, make_app, aiohttp_client):
        """kind_count ≥ 2 时未紧邻 kind 的文件（hint=None）按魔数判定（spec D1 路径）。

        multipart：kind=image, files=a, files=b, kind=video, files=c——b 无紧邻 kind
        → hint=None → 魔数 PNG → image；a/c 各自 hint 归位。锁定 D1 合法路径（最终
        review low-2 补洞）。
        """
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), (None, "b.png", PNG), ("video", "c.mp4", MP4)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert [a["kind"] for a in (await resp.json())["attachments"]] == ["image", "image", "video"]

    @pytest.mark.asyncio
    async def test_mixed_batch_failure_rolls_back(self, make_app, aiohttp_client, tmp_path):
        """混合批次中途失败完整回滚：第一文件成功写盘后第二文件记录写入失败。"""
        agent, app = make_app(fail_on_create=2)  # 第 2 个 create 抛 StorageUnavailableError
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("image", "a.png", PNG), ("video", "b.mp4", MP4)])
        assert resp.status == 503
        assert (await resp.json())["code"] == "STORAGE_UNAVAILABLE"
        assert not agent._attachment_repo.records  # noqa: SLF001
        # 已写盘文件全部清理（不留孤儿）
        assert not list((tmp_path / "uploads" / "image").glob("*.png"))
        assert not list((tmp_path / "uploads" / "video").glob("*.mp4"))


class TestNormalizeAttachmentMime:
    """规范化表直测（spec §4.2 冻结三行，M1——audio/x-wav 经写路径/读路径同源锁定）。"""

    def test_audio_video_webm_to_audio_webm(self):
        assert normalize_attachment_mime("audio", "video/webm") == "audio/webm"

    def test_audio_x_wav_to_audio_wav(self):
        assert normalize_attachment_mime("audio", "audio/x-wav") == "audio/wav"

    def test_video_webm_unchanged(self):
        assert normalize_attachment_mime("video", "video/webm") == "video/webm"

    def test_untable_alias_returns_as_is(self):
        assert normalize_attachment_mime("audio", "audio/mpeg") == "audio/mpeg"
