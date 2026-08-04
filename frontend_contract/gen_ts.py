"""B2 发射器：frontend_contract BaseModel → contract.generated.ts（零第三方依赖）。"""
import inspect
import json
import types
from enum import Enum
from typing import Any, Annotated, Literal, Union, get_args, get_origin

# PEP 604 `X | Y`（types.UnionType）与 typing.Union 都是联合：含 Annotated 成员时
# pydantic 不做归一化（如 TextMessage.content: str | list[Annotated[...]]），
# 两处 origin 都要识别，否则 _ts_type 落到 __name__ 抛 AttributeError。
_UNION_ORIGINS = (Union, types.UnionType)

from pydantic import BaseModel

from frontend_contract.base import ContractModel, field_is_ts_null, wire_key


def _literal(v) -> str:
    """Literal 值 → TS 字面量。统一单引号（对齐 protocol.ts 风格，如 type: 'delta'）；bool 小写。

    字符串经 json.dumps 转义（\\\\、双引号、控制字符 → 合法 TS 转义序列）再补转义单引号：
    json.dumps 不转义 '，须手动补 \\'——否则未来 Literal[\"it's\"] 会产出非法 TS
    （review 终审发现）。当前 ASCII 字面量（如 'delta'）输出与旧实现字节一致。
    """
    if isinstance(v, str):
        escaped = json.dumps(v, ensure_ascii=False)[1:-1].replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _ts_type(annotation) -> str:
    """类型 → TS 类型。Optional 语义（?/| null）由 _field_ts 集中处理，此处去 Optional 返回内层。"""
    origin = get_origin(annotation)
    if origin is Annotated:                       # 剥 Annotated
        return _ts_type(get_args(annotation)[0])
    if origin in _UNION_ORIGINS:
        # 去 None 后 join 全部成员（Optional[X] → X；Optional[Union[A,B]] → A | B，
        # 不可只取第一个——否则 SessionMessageItem.content?: string | ContentPart[] 会丢类型）
        args = [a for a in get_args(annotation) if a is not type(None)]
        return " | ".join(_ts_type(a) for a in args)
    if origin is Literal:
        return " | ".join(_literal(v) for v in get_args(annotation))
    if origin is list:
        inner = _ts_type(get_args(annotation)[0])
        # 联合内层必须加括号：TS 中 | 优先级低于 []，(A | B)[] 才表示「元素为 A 或 B 的数组」，
        # 否则 `A | B[]` 被解析为「单个 A 或全 B 数组」（如 content?: string | TextPart | ImagePart[]）
        if "|" in inner:
            inner = f"({inner})"
        return f"{inner}[]"
    if origin is dict:
        return f"Record<string, {_ts_type(get_args(annotation)[1])}>"
    if annotation is str:
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is Any:
        # Python Any 无 TS 对应物：落到 __name__ 会产出字面量 Any（TS2304 Cannot find name）。
        # 映射为 unknown（protocol.ts 惯例 Record<string, unknown>），结构上与任何类型互容。
        return "unknown"
    return annotation.__name__


def _is_optional(annotation) -> bool:
    """必填判定：Python 类型是否 Optional（非看 schema required）。"""
    origin = get_origin(annotation)
    return origin in _UNION_ORIGINS and type(None) in get_args(annotation)


def _field_ts(name, field) -> str:
    """字段 → TS 行。?/| null 在此集中决定（ts_null → | null；Optional 非 ts_null → ?）。

    键名用 wire_key（serialization_alias 优先）——与 wire_dict by_alias 一致，保证
    TS 类型与 wire 键对齐（如归档消息 _id 建模为 mongo_id + serialization_alias）。
    """
    # field_is_ts_null：兼容 pydantic 2.11（Annotated 被剥、marker 移入 field.metadata）
    is_null = field_is_ts_null(field)
    optional = _is_optional(field.annotation) and not is_null
    base = _ts_type(field.annotation)
    t = f"{base} | null" if is_null else base
    q = "?" if optional else ""
    doc = field.description or ""
    line = f"  {wire_key(field, name)}{q}: {t}"
    return f"  /** {doc} */\n{line}" if doc else line


def _referenced_models(annotation, known: set[str]) -> list[type[BaseModel]]:
    """从字段注解提取引用的 ContractModel 子类（剥 Annotated/list/dict/Union 包装）。

    用于 _topo_sort 的 DFS：只返回已知集合（传入 generate_ts 的模型集）内的引用，
    集合外引用由调用方负责补齐，不在此处递归。
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _referenced_models(get_args(annotation)[0], known)
    if origin in (list, dict, *_UNION_ORIGINS):
        result: list[type[BaseModel]] = []
        for a in get_args(annotation):
            result += _referenced_models(a, known)
        return result
    if isinstance(annotation, type) and issubclass(annotation, ContractModel) \
            and annotation.__name__ in known:
        return [annotation]
    return []


def _referenced_enums(annotation) -> list[type[Enum]]:
    """从字段注解提取 Enum 子类（剥 Annotated/list/dict/Union 包装）。

    用于 generate_ts 的 str Enum 收集：字段注解引用的 Enum 经发射为 TS 判别联合
    （export type X = 'a' | 'b'），字段类型名引用该别名（如 `status: ToolStatusEnum`）。
    与 _referenced_models 平行——Enum 非 ContractModel，不进 interface 拓扑。
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _referenced_enums(get_args(annotation)[0])
    if origin in (list, dict, *_UNION_ORIGINS):
        result: list[type[Enum]] = []
        for a in get_args(annotation):
            result += _referenced_enums(a)
        return result
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [annotation]
    return []


def _topo_sort(models: list[type[BaseModel]]) -> list[type[BaseModel]]:
    """拓扑排序（DFS 后序）：被引用模型先输出。

    按 model_fields 引用的嵌套 ContractModel 子类做 DFS 后序（field 类型可能是直接
    引用 / Annotated / list / dict / Union 包装），保证依赖先输出——TS interface
    引用顺序必须合法（ServiceCapability 先于 CapabilitiesMessage）。
    """
    known = {cls.__name__ for cls in models}
    ordered: list[type[BaseModel]] = []
    visited: set[str] = set()

    def visit(cls: type[BaseModel]) -> None:
        name = cls.__name__
        if name in visited:
            return
        visited.add(name)
        for field in cls.model_fields.values():
            for dep in _referenced_models(field.annotation, known):
                visit(dep)
        ordered.append(cls)

    for cls in models:
        visit(cls)
    return ordered


def generate_ts(models: list[type[BaseModel]],
                discriminated: dict[str, list[type[BaseModel]]] | None = None) -> str:
    """生成 contract.generated.ts。

    Args:
        models: 全部契约模型（拓扑排序后输出 interface）。同名不同类型（如两处 ContentPart
            形状不同）→ ValueError（防静默合并成非法 TS——见命名策略）。
        discriminated: 判别联合别名 → 成员类映射，如 {"ServerMessage": [DeltaMessage, ...]}。
            别名（Annotated）不是 BaseModel，不会出现在 models 里——必须由调用方（__main__）
            显式传入，gen_ts 不做模块内魔法发现。
    """
    # 0. 同名安全网：同名不同类型 → ValueError（防静默合并成非法 TS）
    seen: dict[str, type[BaseModel]] = {}
    for cls in models:
        prev = seen.get(cls.__name__)
        if prev is not None and prev is not cls:
            raise ValueError(
                f"同名不同类型 {cls.__name__}（{prev.__module__}.{prev.__qualname__} vs "
                f"{cls.__module__}.{cls.__qualname__}）——需按命名策略改名（如 DebugContentPart）"
            )
        seen[cls.__name__] = cls

    # 0.5 str Enum 收集 + 同名安全网（字段引用枚举 → export type X = 'a' | 'b'）
    enums: dict[str, type[Enum]] = {}
    for cls in models:
        for field in cls.model_fields.values():
            for e in _referenced_enums(field.annotation):
                prev = enums.get(e.__name__)
                if prev is not None and prev is not e:
                    raise ValueError(
                        f"同名不同类型 {e.__name__}（{prev.__module__}.{prev.__qualname__} vs "
                        f"{e.__module__}.{e.__qualname__}）——需按命名策略改名"
                    )
                enums[e.__name__] = e
    for name in enums:
        if name in seen:
            raise ValueError(
                f"枚举类型名与契约模型名冲突 {name}——需按命名策略改名"
            )

    # 1. 拓扑排序（DFS 后序：被引用模型先输出）
    ordered = _topo_sort(models)
    lines = ["// DO NOT EDIT — generated by python -m frontend_contract", ""]
    for cls in ordered:
        # inspect.getdoc：统一跨解释器 docstring 去缩进（PEP 257）。
        # 直接 cls.__doc__.strip() 只去首尾空白、不去行首缩进——Python 3.13 自动 dedent
        # 类 __doc__，3.12 保留源码缩进 → 生成字节跨解释器漂移（多行 docstring 场景）。
        doc = inspect.getdoc(cls)
        doc = f"/** {doc.strip()} */" if doc else ""
        lines += [doc, f"export interface {cls.__name__} {{"]
        lines += [_field_ts(n, f) for n, f in cls.model_fields.items()]
        lines += ["}", ""]
    # 1.5 str Enum → TS 判别联合（按名称排序保证确定性；类型别名 TS 提升，顺序无关）
    for name in sorted(enums):
        lines += [f"export type {name} ="]
        lines += ["  | " + _literal(m.value) for m in enums[name]]
        lines += [""]
    # 2. 判别联合（调用方显式传入）+ UnknownMessage 兜底
    for alias, members in (discriminated or {}).items():
        lines += [f"export type {alias} ="]
        lines += ["  | " + m.__name__ for m in members]
        lines += ["  | UnknownMessage", ""]
    lines += ["export interface UnknownMessage { type: string; [key: string]: unknown }", ""]
    return "\n".join(lines)
