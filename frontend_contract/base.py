"""契约模型基类 + TsNull marker（零依赖，仅 pydantic）。"""
from typing import Annotated, Any, get_origin, get_args
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo

# JSON 归一化适配器：不可变、可复用，故提到模块级（`wire_dict()` 有 175 个调用点，
# 每次现构造 `TypeAdapter(Any)` 是纯浪费）。
_JSON_ADAPTER: TypeAdapter[Any] = TypeAdapter(Any)


class ContractSerializationError(ValueError):
    """`wire_dict()` 遇到**无法归一化成 JSON 原生类型**的值（#46 行 7 的契约定性）。

    本层**有意**抛异常而不是 `str()` 兜底：`str()` 会往 wire 上发一个没有任何消费者
    声明的类型（客户端按契约取 object/int，实际拿到 string）且毫无报错——正是本仓
    反复出现的"静默降级"缺陷族。

    继承 `ValueError` 而非自定义基类：pydantic 的 `PydanticSerializationError` 本身
    就是 `ValueError` 子类，保持同一可捕获面，免得"换了个类型名就悄悄漏过
    `except ValueError`"。**为什么不直接 import `pydantic_core` 那个类型**：本层是
    零依赖层（只允许 pydantic + stdlib），而 pydantic-core 是独立发行包、不在本仓
    依赖声明里。
    """


class TsNull:
    """可为 null marker：声明字段 wire 显式 null（区别于"可缺省"缺省省略）。

    用法：`field: Annotated[Optional[str], TsNull()] = None`。
    """

    ts_null = True


def is_ts_null(annotation: Any) -> bool:
    """检测原始注解是否带 TsNull marker（Annotated 元数据）。"""
    origin = get_origin(annotation)
    if origin is not Annotated:
        return False
    meta = get_args(annotation)[1:]
    return any(getattr(m, "ts_null", False) for m in meta)


def field_is_ts_null(field: FieldInfo) -> bool:
    """检测 FieldInfo 是否带 TsNull marker。

    pydantic 2.11 起 Annotated 外层被剥掉（field.annotation 只剩内层类型，marker 移入
    field.metadata）——只看 annotation 会漏判（is_ts_null 返回 False，wire 丢显式 null、
    生成器产出 X?）。两处都查，兼容新旧版本。
    """
    if is_ts_null(field.annotation):
        return True
    return any(getattr(m, "ts_null", False) for m in field.metadata)


def wire_key(field: FieldInfo, name: str) -> str:
    """序列化键名：serialization_alias > alias > 字段名。

    与 wire_dict 的 by_alias=True 一致（pydantic model_dump 递归用 serialization_alias
    优先）。FieldInfo 不存字段名（name 是 model_fields 的 dict 键），故调用方传入。
    需 wire 键带下划线等非法 Python 字段名时（如归档消息 _id），用
    `Field(validation_alias="_id", serialization_alias="_id")` 建模，保证 wire 键不丢、
    TS 生成器同样按此键名发射。
    """
    return field.serialization_alias or field.alias or name


class ContractModel(BaseModel):
    """契约模型基类：wire_dict() 统一序列化。

    - exclude_none=True：可缺省字段（None）缺省省略
    - by_alias=True：wire 键按 serialization_alias/alias 发射（如 _id）
    - ts_null 标记字段：None 时补显式 null
    - 嵌套递归：字段值为 ContractModel / dict[str, ContractModel] 时递归 wire_dict
    - **最终一遍 JSON 归一化**：`wire_dict()` 的契约是"可直接 `json.dumps` 的 wire
      字典"。递归时用 `self.__dict__` 的**原始值**覆盖 `model_dump(mode="json")` 的
      结果，会把 `datetime` / `Decimal` / `UUID` / `set` 等非 JSON 原生值原样放回
      （S2-5：`metadata` 里一个 `datetime` → 帧静默丢失 / REST 500）。
    - **无法归一化的值 = 抛异常，不做 `str()` 兜底**（#46 行 7 的契约定性，有意为之）：
      pydantic 认得的类型（含 `Enum` → 值、`Path` → 路径串）都会归一化；
      **认不得的任意对象**抛 `ContractSerializationError`，并在消息里点出出错的顶层
      字段。理由：本层是**契约**层，`str()` 兜底会往 wire 上发一个没有任何消费者声明的
      类型（客户端按契约取 object/int，实际拿到 string），属于本仓反复出现的
      "静默降级"缺陷族；抛异常则把问题钉在**发送点**，在测试里立刻可见。
      调用方要发自定义对象，应自行转成契约允许的类型。
    """

    def wire_dict(self) -> dict[str, Any]:
        try:
            data = self.model_dump(mode="json", exclude_none=True, by_alias=True)
        except ValueError as exc:
            # 任意对象在**这一步**就会抛（`Any` 字段的 model_dump 也走 JSON 序列化器），
            # 所以两处入口都要转成契约层类型——否则调用方拿到的是 pydantic 内部类型，
            # 而本层承诺的是 `ContractSerializationError`。归因用**原始值**（data 还没有）。
            raise _contract_error(exc, dict(self.__dict__)) from exc
        # type(self).model_fields：pydantic 2.11 起实例级访问已弃用，类级访问行为等价
        for name, field in type(self).model_fields.items():
            if field_is_ts_null(field) and getattr(self, name) is None:
                data[wire_key(field, name)] = None
        # 嵌套递归：ContractModel / dict[str, ContractModel] / list[ContractModel]
        for name, value in self.__dict__.items():
            field = type(self).model_fields.get(name)
            key = wire_key(field, name) if field is not None else name
            if isinstance(value, ContractModel):
                data[key] = value.wire_dict()
            elif isinstance(value, dict):
                # dict[str, ContractModel] / dict[str, list[ContractModel]] 递归
                nested: dict[str, Any] = {}
                for k, v in value.items():
                    if isinstance(v, ContractModel):
                        nested[k] = v.wire_dict()
                    elif isinstance(v, list):
                        nested[k] = [
                            x.wire_dict() if isinstance(x, ContractModel) else x
                            for x in v
                        ]
                    else:
                        nested[k] = v
                data[key] = nested
            elif isinstance(value, list):
                data[key] = [
                    v.wire_dict() if isinstance(v, ContractModel) else v
                    for v in value
                ]
        # S2-5：上面几支用的是 `self.__dict__` 的原始值（为了能识别 ContractModel
        # 实例并递归），因此 `model_dump(mode="json")` 的归一化会被覆盖掉一部分。
        # 收口一遍：把残留的非 JSON 原生值（datetime/Decimal/UUID/set/Enum/Path 等）
        # 归一化。已归一化的值不受影响（str/int/dict 原样通过）。
        try:
            return _JSON_ADAPTER.dump_python(data, mode="json")
        except ValueError as exc:
            # #46 行 7：**不做 str() 兜底**（见 `ContractSerializationError` 的说明），
            # 但要把"哪个字段"点出来——本方法有 175 个调用点，只报
            # "Unable to serialize unknown type: <class 'object'>" 的定位成本很高。
            raise _contract_error(exc, data) from exc


def _contract_error(exc: Exception, data: dict[str, Any]) -> ContractSerializationError:
    """把 pydantic 的序列化错误转成契约层类型，并附上出错的顶层字段。

    按基类 `ValueError` 捕获（pydantic 的序列化错误就是它的子类），这样本层不必
    依赖 pydantic-core；原始类型名保留在消息前缀里，信息不丢。
    """
    return ContractSerializationError(
        f"{type(exc).__name__}: {exc}；出错的顶层字段：{_offending_keys(data)}")



def _offending_keys(data: dict[str, Any]) -> str:
    """逐个顶层字段试序列化，返回**无法归一化**的字段名（失败路径专用）。

    只在异常路径上跑：正常路径零开销。
    """
    bad = [
        key for key, value in data.items()
        if not _dumpable({key: value})
    ]
    return ", ".join(bad) if bad else "<定位失败：无法归因到单个顶层字段>"


def _dumpable(chunk: dict[str, Any]) -> bool:
    try:
        _JSON_ADAPTER.dump_python(chunk, mode="json")
    except Exception:
        return False
    return True

