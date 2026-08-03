"""frontend_contract：前后端契约单源包（零依赖，仅 pydantic + stdlib）。

契约模型（Pydantic 唯一权威）→ gen_ts.py 发射器 → frontend/src/types/contract.generated.ts。
后端发送点构造模型获得运行时校验；前端 TS 由生成器产出（// DO NOT EDIT 只读快照）。
"""
from frontend_contract.base import ContractModel, TsNull, is_ts_null

__all__ = ["ContractModel", "TsNull", "is_ts_null"]
