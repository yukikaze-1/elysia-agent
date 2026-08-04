"""gen_ts 发射器测试：三态映射 + wire_dict 递归序列化（marker=unit）。"""
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

import pytest
from pydantic import ConfigDict, Field

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


class TextPart(ContractModel):
    """文本内容块。"""
    type: Literal["text"] = "text"
    content: str


class ImagePart(ContractModel):
    """图片内容块。"""
    type: Literal["image"] = "image"
    src: str


ContentPart = Annotated[Union[TextPart, ImagePart], Field(discriminator="type")]


class ContentMessage(ContractModel):
    """PEP 604 + 联合内 list 测试（TextMessage.content 场景）。"""
    type: Literal["text"] = "text"
    content: str | list[ContentPart]        # 含 Annotated 成员 → pydantic 不归一化，origin 是 types.UnionType
    lazy: str | None = None                 # 可缺省 PEP 604 → lazy?: string


class ArchiveMessage(ContractModel):
    """alias 键发射测试（Message.mongo_id 场景：wire 键 _id 不能作 Python 字段名）。"""
    model_config = ConfigDict(populate_by_name=True)
    role: str = Field(description="消息角色")
    mongo_id: Optional[str] = Field(
        default=None, validation_alias="_id", serialization_alias="_id",
        description="Mongo 归档 ID，可缺省")


@pytest.mark.unit
def test_emit_serialization_alias_key():
    """serialization_alias 优先作为 TS 键名（_id 场景），字段名不泄漏。"""
    out = generate_ts([ArchiveMessage])
    assert "_id?: string" in out
    assert "mongo_id:" not in out  # 字段名不泄漏（docstring 文本允许出现）
    # wire_dict 同样按 alias 发射
    d = ArchiveMessage(role="user", mongo_id="m1").wire_dict()
    assert d == {"role": "user", "_id": "m1"}


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


class ParamsModel(ContractModel):
    """Any 类型映射测试（ActionMessage.params 场景）。"""
    params: dict[str, Any]


@pytest.mark.unit
def test_emit_any_maps_to_unknown():
    """dict[str, Any] → Record<string, unknown>（protocol.ts 惯例）。

    回归 C1：此前 `_ts_type` 对 Any 落到 __name__ → 产出字面量 `Any`（非法 TS，
    vue-tsc 报 TS2304 Cannot find name 'Any'，CI frontend job 红）。
    """
    out = generate_ts([ParamsModel])
    assert "params: Record<string, unknown>" in out
    assert "Record<string, Any>" not in out       # 非法 TS 字面量不得残留


@pytest.mark.unit
def test_emit_pep604_union_and_parens():
    """PEP 604 `X | Y`（types.UnionType）识别 + 联合内 list 加括号。

    - str | list[Annotated[Union[TextPart, ImagePart]]] → string | (TextPart | ImagePart)[]
      （TS 中 | 优先级低于 []，不括会解析成 string | TextPart | ImagePart[]）
    - str | None（PEP 604 Optional）→ lazy?: string
    """
    out = generate_ts([ContentMessage, TextPart, ImagePart])
    assert "content: string | (TextPart | ImagePart)[]" in out
    assert "lazy?: string" in out


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


class DocstringIndentModel(ContractModel):
    """跨解释器去缩进测试。

        续行（源码缩进 8 格）。
    """
    field: str = Field(description="字段")


@pytest.mark.unit
def test_emit_multiline_docstring_dedent():
    """多行类 docstring 经 inspect.getdoc 归一缩进（跨解释器字节一致）。

    回归 review I：cls.__doc__.strip() 只去首尾空白——Python 3.12 保留源码缩进续行、
    3.13 自动 dedent 类 __doc__ → generate_ts 输出跨解释器漂移（rest.py 多行 docstring 场景）。
    inspect.getdoc（cleandoc）统一去缩进，任何解释器下生成字节一致。
    """
    out = generate_ts([DocstringIndentModel])
    assert "续行（源码缩进 8 格）。" in out
    assert "        续行（源码缩进 8 格）。" not in out  # 续行行首 8 空格必须被 cleandoc 去掉


class DesktopStatusEnum(str, Enum):
    """桌面工具状态（批次 4 str Enum → TS 判别联合）。"""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RUNNING = "running"
    RESULT = "result"
    ERROR = "error"
    CANCELLED = "cancelled"


class DesktopStatusMessage(ContractModel):
    """桌面工具状态回传（字段引用 str Enum）。"""
    type: Literal["tool.status"] = "tool.status"
    callId: str
    status: DesktopStatusEnum


@pytest.mark.unit
def test_emit_str_enum_union():
    """str Enum → `export type X = 'a' | 'b'`（判别联合别名）+ 字段引用类型名。

    批次 4 契约引入 ToolStatusEnum；若不发射枚举类型定义，字段会产出
    `status: ToolStatusEnum` 且无定义 → vue-tsc 报 TS2304（同 Any→unknown 回归）。
    """
    out = generate_ts([DesktopStatusMessage])
    assert "export type DesktopStatusEnum =" in out
    for value in ("accepted", "rejected", "running", "result", "error", "cancelled"):
        assert f"  | '{value}'" in out
    assert "status: DesktopStatusEnum" in out
    # 枚举与判别联合别名排布：枚举在接口之后、UnknownMessage 之前
    assert out.index("export interface DesktopStatusMessage") < out.index("export type DesktopStatusEnum")
    assert out.index("export type DesktopStatusEnum") < out.index("UnknownMessage")
