"""阶段 G:回合边界 state change 发射(service)。

用最小宿主(CognitiveFinalize mixin unbound)直接调 _emit_state_change_if_needed,
mock _output —— 不跑完整 turn(完整链路由 Task 5 snapshot 测试 + test_v2_empty_response 覆盖)。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.cognitive.finalize import CognitiveFinalize
from core.schema.types import SessionId

pytestmark = pytest.mark.service


def _host(protocol_version=2):
    """构造 CognitiveFinalize mixin 宿主(无 spec MagicMock——spec 会阻止设置 _output,
    因 _output 只是类型占位注解 finalize.py:43,非类属性,spec 判定后 _output 不可设)。

    用平凡 MagicMock(不设 spec)让 _generation_ctx/_current_runtime/_output 可设;
    调 CognitiveFinalize._emit_state_change_if_needed(host, ...) 走类的真实 unbound 方法。
    """
    host = MagicMock()
    host._generation_ctx = MagicMock(protocol_version=protocol_version)
    host._current_runtime = MagicMock()
    v2_channel = MagicMock()
    host._output._split_versions.return_value = ([], [v2_channel])
    host._output._broadcast = AsyncMock()  # _broadcast 是 async 方法——实现里 await self._output._broadcast(...)
    return host, v2_channel


@pytest.mark.asyncio
async def test_v2_mood_change_emits_state():
    """v2 + mood 变化 → 经 _broadcast(on_character_state_changed, channels=v2)发射。"""
    host, v2_channel = _host(protocol_version=2)
    runtime = host._current_runtime
    runtime.public_state.return_value = MagicMock()
    runtime.has_public_state_changed.return_value = True
    runtime.definition.characterId = "chr_elysia"
    runtime.instance.instanceId = "chi_elysia"
    runtime.revision.revision = 1
    runtime.stateVersion = 3

    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))

    host._output._broadcast.assert_awaited_once()
    args = host._output._broadcast.await_args
    assert args.kwargs["channels"] == [v2_channel]
    assert args.kwargs["character_id"] == "chr_elysia"
    assert args.kwargs["conversation_id"] == "default"


@pytest.mark.asyncio
async def test_v1_does_not_emit():
    """v1(protocol_version=1)不发射——不调用 _broadcast。"""
    host, _ = _host(protocol_version=1)
    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))
    host._output._broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_mood_change_does_not_emit():
    """mood 无变化(has_public_state_changed=False)不发射。"""
    host, v2_channel = _host(protocol_version=2)
    host._current_runtime.has_public_state_changed.return_value = False
    await CognitiveFinalize._emit_state_change_if_needed(host, SessionId("default"))
    host._output._broadcast.assert_not_awaited()


def test_turn_character_snapshot_uses_frozen_generation_identity():
    host = MagicMock()
    host._generation_ctx.character_id = "chr_builtin_cyrene"
    host._generation_ctx.character_instance_id = "chi_builtin_cyrene"
    host._generation_ctx.character_revision = 3
    host._current_runtime.definition.characterId = "chr_builtin_cyrene"
    host._current_runtime.instance.instanceId = "chi_builtin_cyrene"
    host._current_runtime.revision.revision = 3
    host._current_runtime.revision.displayName = "Cyrene"
    host._current_runtime.revision.presentation = {
        "avatarAssetId": "asset_cyrene",
        "live2dModelId": None,
        "themeColor": "#88aaff",
    }
    snapshot = CognitiveFinalize._turn_character_snapshot(host)
    assert snapshot is not None
    assert snapshot.character_id == "chr_builtin_cyrene"
    assert snapshot.character_instance_id == "chi_builtin_cyrene"
    assert snapshot.character_revision == 3
    assert snapshot.display_name == "Cyrene"


def test_turn_character_snapshot_rejects_runtime_identity_mismatch():
    host = MagicMock()
    host._generation_ctx.character_id = "chr_a"
    host._generation_ctx.character_instance_id = "chi_a"
    host._generation_ctx.character_revision = 1
    host._current_runtime.definition.characterId = "chr_b"
    host._current_runtime.instance.instanceId = "chi_a"
    host._current_runtime.revision.revision = 1
    assert CognitiveFinalize._turn_character_snapshot(host) is None
