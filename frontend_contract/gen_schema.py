"""JSON Schema 发射器：对 ServerMessage 判别联合 + 子模块模型生成 contract.schema.json。

产物 `contract.schema.json` 头带 `transportVersion`/`protocolVersion`/`schemaHash`：
- `transportVersion` 是握手头（X-Protocol-Version）可用值；
- `protocolVersion` 仅展示（server_info 下发），**不可**用作握手头。

零依赖（仅 pydantic + stdlib），不 import core/ Module/。
"""
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from frontend_contract import debug, rest, ws_message
from frontend_contract.version import PROTOCOL_VERSION, TRANSPORT_VERSION

OUTPUT = Path(__file__).parent / "contract.schema.json"


def build_models():
    """收集 (名称, 可校验对象)：BaseModel 子类 + Annotated 判别联合（Q1）。

    `ServerMessage` 是 `Annotated[Union[...], Field(discriminator)]`，**不是 type 实例**，
    `issubclass(obj, BaseModel)` 会跳过——须按 `get_origin(obj) is Annotated` 单独收集
    （复用 `__main__._collect` 的展开思路；成员类型校验 `get_args(obj)[0].__args__`）。
    """
    for mod in (ws_message, rest, debug):
        for name, obj in vars(mod).items():
            if isinstance(obj, type) and issubclass(obj, BaseModel):
                yield name, obj
        for name, obj in vars(mod).items():
            if get_origin(obj) is Annotated:
                union = get_args(obj)[0]
                if get_origin(union) is Union and all(issubclass(c, BaseModel) for c in get_args(union)):
                    yield name, obj


def build_document() -> dict:
    """构建 schema 文档：models 按名称入档，schemaHash 经同一序列化算出。"""
    schemas: dict[str, object] = {}
    for name, model in build_models():
        try:
            schemas[name] = TypeAdapter[Any](model).json_schema()
        except Exception as e:
            # 9abc R5：fail-fast——某模型 json_schema 失败不能静默跳过，否则 schema.json
            # 缺类型而 gen_ts 仍发射，两单源产物漂移且 --check 各自独立通过不被捕获。
            raise RuntimeError(f"JSON Schema 生成失败: {name}: {e}") from e
    body: dict[str, object] = {
        "transportVersion": TRANSPORT_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "models": schemas,
    }
    # F6（review）：schemaHash 可复现——先序列化**不含 schemaHash** 的 body 计算 hash
    #（hash 输入不得含占位值，否则消费者无法从产物去字段后重算），插入 hash 后再以
    # **同一序列化参数**（sort_keys + indent=2 + ensure_ascii=False）写文件。旧实现
    # hash 用 no-indent、文件用 indent=2，fingerprint 无法从产物复现。
    _serialized = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False)
    body["schemaHash"] = hashlib.sha256(_serialized.encode()).hexdigest()[:16]
    return body


def serialize_document(doc: dict) -> str:
    """schema 产物序列化——与 schemaHash 计算同一参数（sort_keys + indent=2 + ensure_ascii=False）。

    单源：gen_schema.main() 与 __main__ 的 schema artifact 共用，防双写参数漂移。
    """
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """写 contract.schema.json（与 __main__ 主入口同一序列化，字节一致）。"""
    doc = build_document()
    OUTPUT.write_text(serialize_document(doc), encoding="utf-8")
