# 测试文件：frontend_contract/tests/test_wire_json_safe.py
"""S2-5：`wire_dict()` 的产物必须**可以直接 `json.dumps`**。

这不是风格问题——`wire_dict()` 的调用点全部是"要发到 wire 上"的地方：
WS 帧构造、REST `json_response`、SSE。产物里漏出一个 `datetime`，后果是
**帧静默丢失 / REST 500**（`json.dumps` 抛 `TypeError`，在 WS 发送路径上被
`except` 吞掉就是"消息没发出去"）。

缺陷机制：`wire_dict()` 先用 `model_dump(mode="json", ...)`（会把 `datetime` 归一化
成 ISO 串），随后为了识别嵌套的 `ContractModel` 实例去遍历 `self.__dict__` 的
**原始值**并 `data[key] = ...` 覆盖回去——于是被归一化的值又被原始 `datetime` 换回来。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import Field

from frontend_contract.base import ContractModel

pytestmark = pytest.mark.unit


class _Holder(ContractModel):
    """自由形态 metadata —— 真实走查里最容易混进非 JSON 原生值的字段形态。"""

    metadata: dict = Field(default_factory=dict)
    items: list = Field(default_factory=list)


class _Outer(ContractModel):
    inner: _Holder | None = None
    payload: dict = Field(default_factory=dict)


class _Native:
    """非 BaseModel、非原生类型的自定义对象——`json.dumps` 必然失败。

    契约层**不承诺**能序列化它（调用方应自行转换），这里只用来证明
    "归一化那一遍真的跑过"，而不是把契约当成万能转换器。
    """


def test_datetime_in_dict_is_normalized():
    """核心：`metadata` 里的 `datetime` 必须变成 ISO 串。"""
    model = _Holder(metadata={"created": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    data = model.wire_dict()

    assert isinstance(data["metadata"]["created"], str), (
        f"datetime 漏到 wire 上：{data['metadata']['created']!r}")
    json.dumps(data)          # 不得抛


@pytest.mark.parametrize("value,expected_type", [
    (datetime(2026, 1, 1, tzinfo=timezone.utc), str),
    (timedelta(seconds=90), (int, float, str)),
    (Decimal("1.50"), (str, int, float)),
    (uuid.UUID("12345678-1234-5678-1234-567812345678"), str),
    ({1, 2}, (list, tuple, str)),
])
def test_non_native_values_are_normalized(value, expected_type):
    """`datetime` / `timedelta` / `Decimal` / `UUID` / `set` 一律归一化。"""
    data = _Holder(metadata={"v": value}).wire_dict()
    assert isinstance(data["metadata"]["v"], expected_type), (
        f"{type(value).__name__} 未归一化：{data['metadata']['v']!r}")
    json.dumps(data)


def test_datetime_inside_list_is_normalized():
    """列表字段里的非原生值同样要归一化（递归分支不止 dict 一支）。"""
    stamps = [datetime(2026, 1, 1, tzinfo=timezone.utc),
              datetime(2026, 1, 2, tzinfo=timezone.utc)]
    data = _Holder(items=stamps).wire_dict()
    assert all(isinstance(s, str) for s in data["items"]), data["items"]
    json.dumps(data)


def test_nested_model_dict_is_normalized():
    """嵌套 ContractModel 内部的 dict 也要归一化（递归入口在父模型上）。"""
    outer = _Outer(inner=_Holder(
        metadata={"at": datetime(2026, 1, 1, tzinfo=timezone.utc)}))
    data = outer.wire_dict()
    assert isinstance(data["inner"]["metadata"]["at"], str), data
    json.dumps(data)


def test_nested_model_keeps_wire_contract():
    """回归网：归一化那一遍**不得**破坏既有的 wire 契约（None 省略 / alias / ts_null）。"""
    from typing import Annotated, Optional

    from frontend_contract.base import TsNull

    class _Slim(ContractModel):
        name: str = "n"
        note: str | None = None
        at: Annotated[Optional[str], TsNull()] = None

    data = _Slim().wire_dict()
    assert "note" not in data, "exclude_none 语义必须保留"
    assert data["at"] is None, "ts_null 字段必须补显式 null"


def test_every_contract_model_wire_dict_is_json_safe():
    """**全模型扫描**：所有可收集的契约模型都要能 `json.dumps(wire_dict())`。

    单点用例只覆盖我想到的形态；这条按"契约层有哪些模型"遍历，用默认/最小实例
    验证每一类的 wire 都可序列化。新增模型若引入非原生值，这里立刻红。
    """
    from frontend_contract import debug as debug_mod
    from frontend_contract import rest as rest_mod
    from frontend_contract import ws_message as ws_mod

    checked = 0
    for mod in (ws_mod, rest_mod, debug_mod):
        for name, obj in vars(mod).items():
            if not (isinstance(obj, type) and issubclass(obj, ContractModel)):
                continue
            if obj is ContractModel:
                continue
            try:
                inst = obj()
            except Exception:
                continue          # 必填字段的模型：单点用例已覆盖其字段形态
            data = inst.wire_dict()
            try:
                json.dumps(data)
            except TypeError as e:
                pytest.fail(f"{name}.wire_dict() 不可 json.dumps：{e}")
            checked += 1
    assert checked > 10, f"扫描到的契约模型太少（{checked}），用例可能没生效"
