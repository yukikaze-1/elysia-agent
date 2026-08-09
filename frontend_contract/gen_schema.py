"""JSON Schema 发射器：对 ServerMessage 判别联合 + 子模块模型生成 contract.schema.json。

产物 `contract.schema.json` 头带 `transportVersion`/`protocolVersion`/`schemaHash`：
- `transportVersion` 是握手头（X-Protocol-Version）可用值；
- `protocolVersion` 仅展示（server_info 下发），**不可**用作握手头。

零依赖（仅 pydantic + stdlib），不 import core/ Module/。
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from frontend_contract import debug, rest, ws_message
from frontend_contract.version import PROTOCOL_VERSION, TRANSPORT_VERSION

logger = logging.getLogger(__name__)
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
        except Exception:
            logger.warning("JSON Schema 生成跳过模型: %s", name)  # 不静默吞——告警
    body: dict[str, object] = {
        "transportVersion": TRANSPORT_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "schemaHash": "",
        "models": schemas,
    }
    # hash 与写文件同一序列化（ensure_ascii=False）——schemaHash 是逻辑内容的稳定指纹
    _serialized = json.dumps(body, sort_keys=True, ensure_ascii=False)
    body["schemaHash"] = hashlib.sha256(_serialized.encode()).hexdigest()[:16]
    return body


def main() -> None:
    """写 contract.schema.json（与 __main__ 主入口同一序列化，字节一致）。"""
    doc = build_document()
    OUTPUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
