"""gen_schema JSON Schema 发射器测试（marker=unit）。

覆盖 Task 4（9B-1）：build_document 对 ServerMessage 判别联合（Annotated，非
BaseModel 子类）+ 子模块模型的收集，以及版本/哈希头。
"""
import pytest

from frontend_contract import gen_schema


@pytest.mark.unit
def test_schema_valid_for_discriminated_union():
    """schema 含版本头 + ServerMessage 判别联合 + 客户端消息模型。"""
    doc = gen_schema.build_document()
    assert doc["transportVersion"] == "2"
    assert doc["protocolVersion"] == "2.0"
    assert doc["schemaHash"]          # 非空
    assert "ServerMessage" in doc["models"]   # 判别联合模型在（Annotated 收集，Q1）
    assert "V2TextMessage" in doc["models"]   # 客户端消息也在
    assert "properties" in doc["models"]["V2TextMessage"]  # 契约模型含 properties


@pytest.mark.unit
def test_schema_hash_deterministic():
    """schemaHash 由同一序列化（sort_keys + ensure_ascii=False）算出，重复调用稳定。"""
    assert gen_schema.build_document()["schemaHash"] == gen_schema.build_document()["schemaHash"]
