"""契约模型基类 + TsNull marker（零依赖，仅 pydantic）。"""
from typing import Annotated, Any, get_origin, get_args
from pydantic import BaseModel


class TsNull:
    """可为 null marker：声明字段 wire 显式 null（区别于"可缺省"缺省省略）。

    用法：`field: Annotated[Optional[str], TsNull()] = None`。
    """

    ts_null = True


def is_ts_null(annotation) -> bool:
    """检测原始注解是否带 TsNull marker（Annotated 元数据）。"""
    origin = get_origin(annotation)
    if origin is not Annotated:
        return False
    meta = get_args(annotation)[1:]
    return any(getattr(m, "ts_null", False) for m in meta)


def field_is_ts_null(field) -> bool:
    """检测 FieldInfo 是否带 TsNull marker。

    pydantic 2.11 起 Annotated 外层被剥掉（field.annotation 只剩内层类型，marker 移入
    field.metadata）——只看 annotation 会漏判（is_ts_null 返回 False，wire 丢显式 null、
    生成器产出 X?）。两处都查，兼容新旧版本。
    """
    if is_ts_null(field.annotation):
        return True
    return any(getattr(m, "ts_null", False) for m in field.metadata)


class ContractModel(BaseModel):
    """契约模型基类：wire_dict() 统一序列化。

    - exclude_none=True：可缺省字段（None）缺省省略
    - ts_null 标记字段：None 时补显式 null
    - 嵌套递归：字段值为 ContractModel / dict[str, ContractModel] 时递归 wire_dict
    """

    def wire_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        # type(self).model_fields：pydantic 2.11 起实例级访问已弃用，类级访问行为等价
        for name, field in type(self).model_fields.items():
            if field_is_ts_null(field) and getattr(self, name) is None:
                data[name] = None
        # 嵌套递归：ContractModel / dict[str, ContractModel] / list[ContractModel]
        for name, value in self.__dict__.items():
            if isinstance(value, ContractModel):
                data[name] = value.wire_dict()
            elif isinstance(value, dict):
                data[name] = {
                    k: v.wire_dict() if isinstance(v, ContractModel) else v
                    for k, v in value.items()
                }
            elif isinstance(value, list):
                data[name] = [
                    v.wire_dict() if isinstance(v, ContractModel) else v
                    for v in value
                ]
        return data
