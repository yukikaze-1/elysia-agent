"""ws_message 契约建模测试：判别联合成员 + ts_null/X? 定性 + wire_dict 递归（marker=unit）。

pydantic 2.11 剥 Annotated（field.annotation 只剩内层类型、marker 移入 field.metadata），
故 ts_null 定性一律走 field_is_ts_null(field) 而非 is_ts_null(field.annotation)。
"""
import pytest

from frontend_contract import ws_message as w
from frontend_contract.base import field_is_ts_null


@pytest.mark.unit
def test_server_message_members():
    """ServerMessage 判别联合：19 具名成员（18 v1 + ServerInfoMessage；UnknownMessage 由发射器追加）。"""
    from typing import get_args
    # ServerMessage = Annotated[Union[19 成员], Field(discriminator="type")]
    # get_args(Annotated) 返回 (Union[...], FieldInfo) —— 必须先剥 Annotated 再剥 Union
    members = get_args(get_args(w.ServerMessage)[0])
    assert len(members) == 19  # 20 = 19 具名 + UnknownMessage（发射器追加）
    assert all(m.__name__ != "UnknownMessage" for m in members)
    assert all(getattr(m, "model_fields", None) is not None for m in members)  # 均为 BaseModel


@pytest.mark.unit
def test_switch_vs_service_error_qualification():
    """switch_model_result.error 缺省省略（X?）；service_mode_result.error 可为 null（ts_null）。"""
    assert field_is_ts_null(w.SwitchModelResultMessage.model_fields["error"]) is False
    assert field_is_ts_null(w.ServiceModeResultMessage.model_fields["error"]) is True


@pytest.mark.unit
def test_capabilities_model_is_ts_null():
    """CapabilitiesMessage.services.{stt,tts,llm}.model 为 null（available=false 时显式 null）。"""
    assert field_is_ts_null(w.ServiceCapability.model_fields["model"]) is True


@pytest.mark.unit
def test_wire_dict_list_recursive():
    """list[ContractModel] 元素递归 wire_dict（SessionMessage.messages 场景，base.py list 分支守护）。"""
    item = w.SessionMessageItem(role="assistant", content="hi")
    msg = w.SessionMessage(messages=[item], token_stats=w.TokenStats(total=1, by_model={}))
    d = msg.wire_dict()
    assert d["messages"][0]["role"] == "assistant"
    assert d["token_stats"]["total"] == 1


@pytest.mark.unit
def test_server_info_message_wire():
    """server_info：protocol_version/server_version/features/services；features 全 false 默认。"""
    msg = w.ServerInfoMessage(
        server_version="0.1.0",
        features=w.ServerInfoFeatures(),
        services={
            "llm": w.ServiceCapability(
                available=True, enabled=True, model="fake/test", default="fake/test"),
        },
    )
    d = msg.wire_dict()
    assert d["type"] == "server_info"
    assert d["protocol_version"] == "2.0"
    assert d["server_version"] == "0.1.0"
    assert d["features"] == {
        "structured_generation": False, "multi_conversation": False,
        "desktop_runtime": False, "dictation": False,
    }
    assert d["services"]["llm"]["model"] == "fake/test"
