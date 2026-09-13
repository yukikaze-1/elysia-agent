"""契约模型基类 + TsNull marker（零依赖，仅 pydantic）。"""
from typing import Annotated, Any, get_origin, get_args
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo


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
    """

    def wire_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True, by_alias=True)
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
        # 收口一遍：把残留的非 JSON 原生值（datetime/Decimal/UUID/set/Enum 等）
        # 归一化。已归一化的值不受影响（str/int/dict 原样通过）。
        return TypeAdapter(Any).dump_python(data, mode="json")
