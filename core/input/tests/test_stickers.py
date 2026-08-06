"""内置表情包测试（批次 7 Task 6，marker=service）。

load_stickers() 扫描 static/stickers/（真 PNG 已提交，直接断言不 monkeypatch）；
GET /api/stickers 返回契约 StickerListResponse（aiohttp test client，仿 test_upload.py）；
get("st_happy") → 落盘 Path 存在。

真 PNG 提交进 git，测试离线可跑，无需网络/DB。
"""

from unittest.mock import MagicMock

import pytest

from core.input.sticker_registry import get, load_stickers

pytestmark = pytest.mark.service


def test_load_stickers_registry_nonempty():
    """load_stickers() 扫描 static/stickers/ 注册表非空（至少 12 个内置表情）。"""
    registry = load_stickers()
    assert len(registry) >= 12, f"内置表情应 >= 12，实际 {len(registry)}"
    # 关键表情 id 须在（契约/前端依赖 st_happy）
    assert "st_happy" in registry


def test_get_st_happy_path_exists():
    """get("st_happy") → 落盘 Path 存在（sticker_id = 文件名去扩展名）。"""
    path = get("st_happy")
    assert path is not None, "st_happy 应在注册表中"
    assert path.exists(), f"st_happy 落盘路径不存在: {path}"
    assert path.name == "st_happy.png"


@pytest.mark.asyncio
async def test_api_stickers_returns_contract(aiohttp_client):
    """GET /api/stickers → StickerListResponse 契约（stickerId + 静态 src 路径）。"""
    from core.input.server import create_app
    from frontend_contract.rest import StickerListResponse

    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/stickers")
    assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
    data = await resp.json()
    # 契约模型可验证（单源，禁止 plain dict 漂移）
    model = StickerListResponse.model_validate(data)
    assert len(model.stickers) >= 12
    ids = [s.stickerId for s in model.stickers]
    assert "st_happy" in ids
    happy = next(s for s in model.stickers if s.stickerId == "st_happy")
    assert happy.src == "/static/stickers/st_happy.png"
