import pytest

pytestmark = pytest.mark.unit


def test_sanitize_scrubs_env_values():
    from scripts.capture_ws_trace import _sanitize
    line = 'export OPENAI_API_KEY=sk-abc123 more'
    assert "sk-abc123" not in _sanitize(line)
    assert _sanitize(line).startswith("export OPENAI_API_KEY=***")


def test_sanitize_keeps_path_scrub():
    from scripts.capture_ws_trace import _sanitize
    assert "$SANITIZED_PATH" in _sanitize("path /home/user/secret")


@pytest.mark.asyncio
async def test_drain_reports_truncation_on_exception():
    """mock ws.receive_json 抛异常 → drain 返回截断标记而非静默。"""
    from scripts.capture_ws_trace import _drain_connect_frames  # 本任务提取的 async helper

    class _BadWS:
        async def receive_json(self):
            raise ConnectionResetError("reset")

    frames, truncated = await _drain_connect_frames(_BadWS(), [], max_connect=5)
    assert truncated is True
    assert frames == []
