"""GET /api/attachments/{id} 签名下载 + /api/attachments/{id}/sign 签发端点测试。

批次 7 Task 4（spec §3.3，marker=service）。
构造：真实 Agent（attachment_repo property 依赖）+ 内存 fake AttachmentRepository
（经 agent._attachment_repo 注入，noqa: SLF001）+ aiohttp test client
（pytest-aiohttp 的 aiohttp_client fixture，仿 test_upload.py）。

配置矩阵——每场景显式设 AGENT_ACCESS_TOKEN 与 attachment_sig_secret 两值
（经 ServerConfig 注入 agent，access_token 同时传 create_app 挂认证中间件）：
- (token=配, sig=配)：签名下载 fail-closed 校验序全链路
- (token=配, sig=空)：下载/签发一律 403 ATTACHMENT_SIGNATURE_DISABLED（fail-closed）
- (token=空, sig=任意)：下载免签名直读（fail-open），但 status=active 仍校验

合法签名构造与 Task 3 `_sign_url` 同一 HMAC 构造
（hmac sha256 over "{attachment_id}:{exp}"）。下载目录经 ELYSIA_PATH_DATA_UPLOADS
重定向到 tmp_path，不触碰生产 data/uploads。
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from core.schema.adapters import attachment_resolver as _ar
from core.storage.attachment_repo import StorageUnavailableError

pytestmark = pytest.mark.service

# 测试用的 config 两值（每场景显式设置）
ACCESS_TOKEN = "secret-token"
SIG_SECRET = "test-secret"

# 有效 PNG 魔数字节（filetype.guess 可识别；下载 200 需真实落盘文件）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class _FakeAttachmentRepo:
    """内存 fake AttachmentRepository——离线时 get/create 抛 StorageUnavailableError。

    与真实 AttachmentRepository 的差异只在存储介质（list vs Mongo）：
    下载/签发 handler 只调用 get()，上传走 create()，最小接口即够；
    fail-closed 语义（离线 raise）如实模拟。
    """

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self.records: list[dict] = []

    async def create(self, doc: dict) -> None:
        if self.offline:
            raise StorageUnavailableError("attachments 集合不可用：MongoDB 未连接（离线）")
        self.records.append(dict(doc))

    async def get(self, attachment_id: str) -> dict | None:
        if self.offline:
            raise StorageUnavailableError("attachments 集合不可用：MongoDB 未连接（离线）")
        for rec in self.records:
            if rec.get("attachment_id") == attachment_id:
                return dict(rec)
        return None


@pytest.fixture(autouse=True)
def _clear_resolver_registry():
    """清空 AttachmentFileResolver 模块级注册表，防跨测试污染（服务端模块级缓存）。"""
    _ar._registry.clear()
    yield
    _ar._registry.clear()


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """返回构建 attachment app 的工厂（真实 Agent + 注入 fake attachment repo）。"""

    def _make(*, sig_secret: str = SIG_SECRET, access_token: str = ACCESS_TOKEN,
              offline: bool = False):
        from core.agent import Agent
        from core.config import AppConfig
        from core.config.app_config import ServerConfig
        from core.input.server import create_app

        agent = Agent(config=AppConfig(server=ServerConfig(
            access_token=access_token,
            attachment_sig_secret=sig_secret,
        )))
        agent._attachment_repo = _FakeAttachmentRepo(offline=offline)  # noqa: SLF001
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS", str(tmp_path / "uploads"))
        app = create_app(agent, serve_static=False, access_token=access_token)
        return agent, app

    return _make


async def _seed(agent, tmp_path, *, attachment_id="att_1", status="active",
                kind="image", stored_name="abc123.png", mime="image/png",
                content=PNG):
    """写盘 data/uploads/{kind}/{stored_name} + 落 fake repo 记录。"""
    upload_dir = tmp_path / "uploads" / kind
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / stored_name).write_bytes(content)
    await agent._attachment_repo.create({  # noqa: SLF001
        "attachment_id": attachment_id,
        "kind": kind,
        "mime": mime,
        "size": len(content),
        "filename": "photo.png",
        "stored_name": stored_name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "ref_count": 0,
        "refs": [],
        "status": status,
        "created_at": "2026-08-06T00:00:00Z",
    })


def _sig(sig_secret: str, attachment_id: str, exp: int) -> str:
    """与 Task 3 `_sign_url` 同一 HMAC 构造。"""
    return hmac.new(sig_secret.encode(), f"{attachment_id}:{exp}".encode(),
                    hashlib.sha256).hexdigest()


def _url(attachment_id: str, sig: str, exp: int) -> str:
    """构造签名下载 URL（?sig=...&exp=...）。"""
    return f"/api/attachments/{attachment_id}?sig={sig}&exp={exp}"


def _fresh_sig(attachment_id: str = "att_1") -> tuple[str, int]:
    """返回 (sig, exp)：exp = now+86400（与 _sign_url 同窗口）。"""
    exp = int(time.time()) + 86400
    return _sig(SIG_SECRET, attachment_id, exp), exp


@pytest.mark.service
@pytest.mark.asyncio
async def test_download_wrong_hmac_sig_403_before_existence(make_app, aiohttp_client):
    # 合法 hex 但 HMAC 错误的 sig + 不存在的 id → 403 INVALID_SIGNATURE（而非 404）
    # 这是 D3 的枚举向量：修复前走到 ② repo.get → 404，修复后 ③④ 前置 → 403
    agent, app = make_app()
    client = await aiohttp_client(app)
    exp = int(time.time()) + 3600
    resp = await client.get(f"/api/attachments/att_不存在?sig={'0'*64}&exp={exp}")
    assert resp.status == 403
    body = json.loads((await resp.read()).decode())
    assert body["code"] == "INVALID_SIGNATURE"


class TestDownloadConfigured:
    """(token=配, sig=配)：签名下载校验序（spec §3.3）。"""

    @pytest.mark.asyncio
    async def test_no_sig_403(self, make_app, aiohttp_client):
        """无 sig → 403 INVALID_SIGNATURE（缺签名即拒绝，不查存储）。"""
        agent, app = make_app()  # token 配 + sig 配
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1")
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_tampered_sig_403(self, make_app, aiohttp_client, tmp_path):
        """sig 篡改（改一个 hex 字符）→ 403 INVALID_SIGNATURE。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        valid, exp = _fresh_sig()
        tampered = ("0" if valid[0] != "0" else "1") + valid[1:]
        assert tampered != valid
        resp = await client.get(_url("att_1", tampered, exp))
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_expired_sig_403(self, make_app, aiohttp_client, tmp_path):
        """exp 过期（now-1000，重算合法 sig）→ 403 SIGNATURE_EXPIRED。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        exp = int(time.time()) - 1000
        sig = _sig(SIG_SECRET, "att_1", exp)
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "SIGNATURE_EXPIRED"

    @pytest.mark.asyncio
    async def test_exp_not_int_403(self, make_app, aiohttp_client, tmp_path):
        """exp 非整数 → 403 SIGNATURE_EXPIRED（范围校验兜底非法格式）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        sig = _sig(SIG_SECRET, "att_1", 9999999999)
        resp = await client.get(f"/api/attachments/att_1?sig={sig}&exp=abc")
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "SIGNATURE_EXPIRED"

    @pytest.mark.asyncio
    async def test_soft_deleted_404(self, make_app, aiohttp_client, tmp_path):
        """软删（status=deleted）→ 404 ATTACHMENT_NOT_FOUND（签名合法也不放行）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path, status="deleted")
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_unknown_attachment_404(self, make_app, aiohttp_client):
        """记录不存在 → 404 ATTACHMENT_NOT_FOUND。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig("att_missing")
        resp = await client.get(_url("att_missing", sig, exp))
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_valid_sig_200_and_range(self, make_app, aiohttp_client, tmp_path):
        """有效签名 → 200 + Content-Type；Range: bytes=0-9 → 206 + Content-Range 正确。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "image/png"
        assert await resp.read() == PNG
        # aiohttp FileResponse 原生 Range/206
        resp = await client.get(_url("att_1", sig, exp),
                                headers={"Range": "bytes=0-9"})
        assert resp.status == 206, f"expected 206, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Range") == f"bytes 0-9/{len(PNG)}"
        assert await resp.read() == PNG[:10]

    @pytest.mark.asyncio
    async def test_offline_503(self, make_app, aiohttp_client):
        """Mongo 离线（repo.get 抛 StorageUnavailableError）→ 503 STORAGE_UNAVAILABLE。"""
        agent, app = make_app(offline=True)
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 503, f"expected 503, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "STORAGE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_file_missing_404_and_no_register(self, make_app, aiohttp_client, tmp_path):
        """记录存在但落盘文件被删（GC M8 场景）→ 404 ATTACHMENT_NOT_FOUND 且不预热 resolver（Minor 10）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)  # 写记录 + 落盘
        # 删文件模拟「记录在、文件无」的不一致
        for f in (tmp_path / "uploads" / "image").glob("*.png"):
            f.unlink()
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_NOT_FOUND"
        # register 前文件检查失败 → 不注册无效映射（preheat 无副作用）
        assert "att_1" not in _ar._registry  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_download_legacy_audio_wav_normalized_content_type(self, make_app, aiohttp_client, tmp_path):
        """存量 kind=audio + mime=audio/x-wav → 下载 Content-Type=audio/wav（BE-S3-RUN-P0-02）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path, kind="audio", stored_name="a.wav",
                    mime="audio/x-wav", content=b"RIFF\x24\x08\x00\x00WAVEfmt ")
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "audio/wav"

    @pytest.mark.asyncio
    async def test_download_legacy_audio_video_webm_normalized_content_type(self, make_app, aiohttp_client, tmp_path):
        """存量 kind=audio + mime=video/webm → 下载 Content-Type=audio/webm。"""
        agent, app = make_app()
        await _seed(agent, tmp_path, kind="audio", stored_name="a.webm",
                    mime="video/webm", content=b"\x1a\x45\xdf\xa3")
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "audio/webm"


class TestSignEndpoint:
    """GET /api/attachments/{id}/sign 签发端点（Bearer 由中间件保证）。"""

    @pytest.mark.asyncio
    async def test_sign_no_bearer_401(self, make_app, aiohttp_client, tmp_path):
        """配置 AGENT_ACCESS_TOKEN 时无 Bearer → 401 AUTH_REQUIRED（中间件）。"""
        agent, app = make_app()  # token 配
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign")
        assert resp.status == 401, f"expected 401, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_sign_with_bearer_200(self, make_app, aiohttp_client, tmp_path):
        """有 Bearer → 200 {attachmentId, url, expiresAt}；签发 URL 立即可下载。"""
        agent, app = make_app()  # token 配 + sig 配
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["attachmentId"] == "att_1"  # attachmentId 必填
        assert data["url"].startswith("/api/attachments/att_1?sig=")
        assert "exp=" in data["url"]
        assert data["expiresAt"].endswith("+00:00")
        # 签发 URL 立即下载 → 200（HMAC 同一构造可验签）
        qs = parse_qs(urlparse(data["url"]).query)
        assert "sig" in qs and "exp" in qs
        resp = await client.get(data["url"])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert await resp.read() == PNG

    @pytest.mark.asyncio
    async def test_sign_trailing_slash_404(self, make_app, aiohttp_client, tmp_path):
        """尾斜杠 /sign/ → 404（路由不存在，不绕过）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign/",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"

    @pytest.mark.asyncio
    async def test_sign_unknown_attachment_404(self, make_app, aiohttp_client):
        """记录不存在 → 404 ATTACHMENT_NOT_FOUND（Bearer 通过后 handler 拒绝）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/nonexistent/sign",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_sign_offline_503(self, make_app, aiohttp_client):
        """Mongo 离线（签发读记录）→ 503 STORAGE_UNAVAILABLE。"""
        agent, app = make_app(offline=True)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert resp.status == 503, f"expected 503, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "STORAGE_UNAVAILABLE"


class TestFailClosedSigDisabled:
    """(token=配, sig=空)：签名未配置 → 下载/签发一律 403 ATTACHMENT_SIGNATURE_DISABLED。"""

    @pytest.mark.asyncio
    async def test_download_403(self, make_app, aiohttp_client, tmp_path):
        """下载 → 403 ATTACHMENT_SIGNATURE_DISABLED（fail-closed 早退，不进校验序）。"""
        agent, app = make_app(sig_secret="", access_token=ACCESS_TOKEN)
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1?sig=deadbeef&exp=9999999999")
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_SIGNATURE_DISABLED"

    @pytest.mark.asyncio
    async def test_sign_403(self, make_app, aiohttp_client, tmp_path):
        """sign → 403 ATTACHMENT_SIGNATURE_DISABLED（Bearer 通过后 handler 拒绝）。"""
        agent, app = make_app(sig_secret="", access_token=ACCESS_TOKEN)
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert resp.status == 403, f"expected 403, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_SIGNATURE_DISABLED"


class TestFailOpenNoToken:
    """(token=空, sig=任意)：未配置认证 → 下载免签名直读，但 status=active 仍校验。"""

    @pytest.mark.asyncio
    async def test_download_skips_sig(self, make_app, aiohttp_client, tmp_path):
        """下载无 sig → 200（fail-open 跳过签名校验）。"""
        agent, app = make_app(sig_secret="whatever", access_token="")
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1")
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "image/png"
        assert await resp.read() == PNG

    @pytest.mark.asyncio
    async def test_download_still_checks_active(self, make_app, aiohttp_client, tmp_path):
        """软删 → 404 ATTACHMENT_NOT_FOUND（fail-open 下 active 校验仍生效）。"""
        agent, app = make_app(sig_secret="whatever", access_token="")
        await _seed(agent, tmp_path, status="deleted")
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1")
        assert resp.status == 404, f"expected 404, got {resp.status}: {await resp.text()}"
        assert (await resp.json())["code"] == "ATTACHMENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_sign_normal(self, make_app, aiohttp_client, tmp_path):
        """sign 无 Bearer → 200（fail-open 正常签发）。"""
        agent, app = make_app(sig_secret="whatever", access_token="")
        await _seed(agent, tmp_path)
        client = await aiohttp_client(app)
        resp = await client.get("/api/attachments/att_1/sign")
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        data = await resp.json()
        assert data["attachmentId"] == "att_1"
        assert "sig=" in data["url"] and "exp=" in data["url"]
        assert data["expiresAt"].endswith("+00:00")
