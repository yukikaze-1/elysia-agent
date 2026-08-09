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


@pytest.mark.unit
def test_build_document_fails_fast_on_json_schema_error(monkeypatch):
    """9abc R5：某模型 json_schema 失败 → build_document fail-fast（RuntimeError 含模型名）。

    旧实现 except Exception 静默跳过——schema.json 缺类型而 gen_ts 仍发射，两单源
    产物漂移且 --check 各自独立通过，漂移不被门禁捕获。修复后抛 RuntimeError。
    """
    from pydantic import BaseModel, ConfigDict

    class _Unrepresentable:
        """无 JSON schema 表示的任意类型（arbitrary_types_allowed 下 json_schema 抛）。"""

    class BrokenModel(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        field: _Unrepresentable

    # 注入 gen_schema 遍历的子模块——build_models 会收集到该模型并触发生成失败
    #（raising=False：向模块注入新属性）
    monkeypatch.setattr(gen_schema.ws_message, "BrokenModel", BrokenModel, raising=False)
    with pytest.raises(RuntimeError) as ei:
        gen_schema.build_document()
    assert "BrokenModel" in str(ei.value)
    assert "JSON Schema 生成失败" in str(ei.value)
