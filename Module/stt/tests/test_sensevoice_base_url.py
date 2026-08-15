"""SenseVoice 默认地址单源测试（阶段4 P0-01 D1=B，spec §4.1）。

覆盖工厂层优先级（body explicit > env > providers.yaml > DEFAULT_BASE_URL）与
custom STT 必须显式 base_url 的约束。SenseVoiceBackend 构造 httpx.AsyncClient 不发网络；
断言 base_url 落在 backend._client.base_url。
"""

import httpx
import pytest

from Module._common.test_helpers import make_test_method
from Module.stt.sensevoice import DEFAULT_BASE_URL, SenseVoiceBackend
from Module.stt.registry import STTFactory

pytestmark = pytest.mark.unit


def _backend_url(backend: SenseVoiceBackend) -> str:
    return str(backend._client.base_url)


def test_sensevoice_default_url_without_env_or_explicit(monkeypatch):
    """无 SENSEVOICE_URL、无 explicit → 工厂兜底 DEFAULT_BASE_URL（spec §3.4 T1）。"""
    monkeypatch.delenv("SENSEVOICE_URL", raising=False)
    backend = STTFactory.create("sensevoice/sense-voice")
    assert _backend_url(backend) == DEFAULT_BASE_URL


def test_sensevoice_env_overrides_default(monkeypatch):
    """SENSEVOICE_URL 环境变量覆盖默认值（spec §3.4 T2）。"""
    monkeypatch.setenv("SENSEVOICE_URL", "http://custom:9999")
    backend = STTFactory.create("sensevoice/sense-voice")
    assert _backend_url(backend) == "http://custom:9999"


def test_sensevoice_explicit_wins(monkeypatch):
    """显式 base_url 优先级最高（body → create_kwargs → explicit，spec §3.4 T3）。"""
    monkeypatch.setenv("SENSEVOICE_URL", "http://env:1")
    backend = STTFactory.create("sensevoice/sense-voice", base_url="http://1.2.3.4:20042")
    assert _backend_url(backend) == "http://1.2.3.4:20042"


def test_custom_stt_missing_base_url_raises():
    """custom STT 缺地址仍必须失败——修复不放宽（spec §3.3.4/T4）。"""
    with pytest.raises(ValueError):
        STTFactory.create("custom/foo")


def test_sensevoice_constructor_default_is_shared_constant():
    """构造函数默认引用同一常量（真单源，spec §4.1）。"""
    backend = SenseVoiceBackend()
    assert _backend_url(backend) == DEFAULT_BASE_URL


@pytest.mark.asyncio
async def test_sensevoice_transport_error_structured():
    """transport 错误 → 结构化 ok=false（非 InvalidURL，spec §3.4 T7）。"""
    backend = SenseVoiceBackend()

    async def boom():
        raise httpx.ConnectError("connection refused: localhost:20042")

    backend._test_fn = make_test_method(boom)  # noqa: SLF001 —— 测试直接替换健康探针
    result = await backend.test()
    assert result["ok"] is False
    assert "connection refused" in result["error"]
    assert "Request URL is missing" not in result["error"]
