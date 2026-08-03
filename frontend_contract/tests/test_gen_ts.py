"""gen_ts 发射器测试：三态映射 + wire_dict 递归序列化（marker=unit）。"""
from typing import Annotated, Literal, Optional

import pytest
from pydantic import Field

from frontend_contract.base import ContractModel, TsNull
from frontend_contract.gen_ts import generate_ts


class DeltaMessage(ContractModel):
    """流式文字增量。"""
    type: Literal["delta"] = "delta"
    content: str = Field(description="文本内容")
    stream_id: Optional[int] = Field(default=None, description="TTS 流 ID，可缺省")


class ServiceCapability(ContractModel):
    """单服务运行时能力。"""
    available: bool
    enabled: bool
    model: Annotated[Optional[str], TsNull()] = None   # 可为 null：unavailable 时显式 null
    default: str


class CapabilitiesMessage(ContractModel):
    """服务能力声明（嵌套 ts_null 验证递归）。"""
    type: Literal["capabilities"] = "capabilities"
    services: dict[str, ServiceCapability]


class SessionList(ContractModel):
    """list 字段测试（SessionMessage.messages / env_vars 场景）。"""
    messages: list[str]
    events: Optional[list[str]] = None   # 可缺省 list → events?: string[]


class ApiOk(ContractModel):
    """bool Literal 测试（ApiOk.ok 场景）。"""
    ok: Literal[True] = True


@pytest.mark.unit
def test_emit_basic_interface():
    out = generate_ts([DeltaMessage, ServiceCapability, CapabilitiesMessage, SessionList, ApiOk])
    assert "export interface DeltaMessage" in out
    assert "type: 'delta'" in out                      # 判别字段必填（非 Optional）
    assert "content: string" in out                     # 非 Optional → 必填
    assert "stream_id?: number" in out                  # Optional → X?
    assert "model: string | null" in out                # TsNull → X | null
    assert "Record<string, ServiceCapability>" in out
    assert "messages: string[]" in out                  # list[str] → string[]（防 list 兜底 'list'）
    assert "events?: string[]" in out                   # Optional[list[str]] → events?: string[]
    assert "ok: true" in out                            # Literal[True] → true（小写，非 'True'）
    assert "import type" not in out                      # 拓扑序内联，无跨文件 import
    assert "服务能力" in out                             # docstring → JSDoc 保留中文


@pytest.mark.unit
def test_wire_dict_recursive_ts_null():
    """嵌套 ts_null：available=false 时 model 键恒存在且为 null。"""
    cap = CapabilitiesMessage(services={"stt": ServiceCapability(
        available=False, enabled=True, model=None, default="sense-voice")})
    d = cap.wire_dict()
    assert d["services"]["stt"]["model"] is None          # 递归补 null，键不被 exclude 丢掉
    assert d["services"]["stt"]["default"] == "sense-voice"


@pytest.mark.unit
def test_wire_dict_omits_optional_none():
    """可缺省字段 None → 键缺省省略（非显式 null）。"""
    d = DeltaMessage(content="hi", stream_id=None).wire_dict()
    assert "stream_id" not in d
    assert d["type"] == "delta"
