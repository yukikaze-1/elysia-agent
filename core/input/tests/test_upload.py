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

from core.schema.adapters import attachment_resolver as _ar
from core.storage.attachment_repo import StorageUnavailableError

pytestmark = pytest.mark.service

# ── 各类型魔数字节（filetype.guess 可识别的真实头部）───────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
TEXT = b"this is a plain text file masquerading as a png, nothing more" * 3


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
