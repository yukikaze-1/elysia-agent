"""python -m frontend_contract → 生成三产物（ts/schema/raw_ts）--check 漂移检测。

- 默认：收集存在的契约子模块模型 → generate_ts → 写 frontend/src/types/contract.generated.ts；
  gen_schema.build_document → frontend_contract/contract.schema.json；
  generate_raw_ts → frontend_contract/contract.raw.ts
- --check：各产物生成到临时文件，git diff --exit-code 校验无漂移（非零退出码 → CI 红）
"""
import importlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Union, get_args, get_origin

from pydantic import BaseModel

from frontend_contract import gen_schema
from frontend_contract.base import ContractModel
from frontend_contract.gen_ts import generate_raw_ts, generate_ts
from frontend_contract.version import PROTOCOL_VERSION, TRANSPORT_VERSION

# 契约子模块：Task 2/4/5 逐步添加（ws_message / rest / debug），用 importlib 动态收集存在的
_SUBMODULES = ("ws_message", "rest", "debug")

# 三产物输出路径（schema 与 gen_schema 共用同一路径定义，单一来源）
_OUTPUTS: dict[str, Path] = {
    "ts": (Path(__file__).resolve().parent.parent
           / "frontend" / "src" / "types" / "contract.generated.ts"),
    "schema": gen_schema.OUTPUT,
    "raw_ts": Path(__file__).resolve().parent / "contract.raw.ts",
}


def _collect() -> tuple[list[type[BaseModel]], dict[str, list[type[BaseModel]]]]:
    """动态收集存在的子模块里的 BaseModel 列表 + 判别联合（ServerMessage/ClientMessage）成员。"""
    models: list[type[BaseModel]] = []
    discriminated: dict[str, list[type[BaseModel]]] = {}
    for name in _SUBMODULES:
        try:
            module = importlib.import_module(f"frontend_contract.{name}")
        except ModuleNotFoundError as e:
            # 只跳过「子模块本身缺失」；子模块内部 import 依赖失败（e.name 是别的模块）
            # 必须冒泡——否则 gen 模式静默截断并覆盖 contract.generated.ts，check-types 对
            # 截断文件照样通过（review 终审发现）
            if e.name != f"frontend_contract.{name}":
                raise
            continue
        for obj in vars(module).values():
            # 过滤基类（ContractModel 经子模块 import 进入命名空间，不滤会生成
            # 无字段的 export interface ContractModel {} 噪音）——见 plan Step 5
            if isinstance(obj, type) and issubclass(obj, BaseModel) \
                    and obj is not ContractModel and obj is not BaseModel:
                models.append(obj)
        # 判别联合别名（Annotated[Union[...], Field(discriminator="type")]）。
        # F2（review）：ClientMessage 也注册——旧实现只注册 ServerMessage，ClientMessage
        # 判别联合不发射到 TS 产物（9B-2 入站消息契约缺位，contract.raw.ts 缺 union）。
        # 展开方式对齐 gen_schema.build_models（get_origin/get_args，参考 gen_schema）。
        for alias in ("ServerMessage", "ClientMessage"):
            union_alias = getattr(module, alias, None)
            if union_alias is not None and get_origin(union_alias) is Annotated:
                inner = get_args(union_alias)[0]
                members = list(get_args(inner)) if get_origin(inner) is Union else [inner]
                discriminated[alias] = members
    return models, discriminated


def main() -> int:
    """生成入口：默认写文件，--check 只校验不写。返回进程退出码。"""
    models, discriminated = _collect()
    schema_doc = gen_schema.build_document()
    artifacts: dict[str, str] = {
        "ts": generate_ts(models, discriminated),
        # F6（review）：schema 序列化与 gen_schema 单源（sort_keys + indent=2 +
        # ensure_ascii=False）——与 schemaHash 计算同参数，产物可复现
        "schema": gen_schema.serialize_document(schema_doc),
        "raw_ts": generate_raw_ts(
            models, discriminated, TRANSPORT_VERSION, PROTOCOL_VERSION,
            schema_doc["schemaHash"]),
    }
    if "--check" in sys.argv:
        # F5（review）：TemporaryDirectory 自动清理——NamedTemporaryFile(delete=False)
        # 从不 unlink，CI 每次 --check 漏 3 个 /tmp 文件（对齐 gen_contract_frames.py 模式）
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in artifacts.items():
                gen = Path(tmp) / ("check" + Path(_OUTPUTS[name]).suffix)
                gen.write_text(content, encoding="utf-8")
                diff = subprocess.run(
                    ["git", "diff", "--exit-code", "--no-index", "--",
                     str(gen), str(_OUTPUTS[name])],
                    capture_output=True, text=True)
                if diff.returncode != 0:
                    print(f"{_OUTPUTS[name].name} 漂移！请运行 make gen-types 重新生成后提交。",
                          file=sys.stderr)
                    print(diff.stdout, file=sys.stderr)
                    return 1
        return 0
    for name, content in artifacts.items():
        _OUTPUTS[name].write_text(content, encoding="utf-8")
    print("已生成 " + ", ".join(str(_OUTPUTS[n]) for n in artifacts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
