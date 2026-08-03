"""python -m frontend_contract → 跑 gen_ts 生成 contract.generated.ts（--check 漂移检测）。

- 默认：收集存在的契约子模块模型 → generate_ts → 写 frontend/src/types/contract.generated.ts
- --check：生成到临时文件，git diff --exit-code 校验无漂移（非零退出码 → CI 红）
"""
import importlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Union, get_args, get_origin

from pydantic import BaseModel

from frontend_contract.base import ContractModel
from frontend_contract.gen_ts import generate_ts

# 契约子模块：Task 2/4/5 逐步添加（ws_message / rest / debug），用 importlib 动态收集存在的
_SUBMODULES = ("ws_message", "rest", "debug")

_OUTPUT = (Path(__file__).resolve().parent.parent
           / "frontend" / "src" / "types" / "contract.generated.ts")


def _collect() -> tuple[list[type[BaseModel]], dict[str, list[type[BaseModel]]]]:
    """动态收集存在的子模块里的 BaseModel 列表 + ServerMessage 判别联合成员。"""
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
        # ServerMessage 判别联合别名（Annotated[Union[...], Field(discriminator="type")]）
        server = getattr(module, "ServerMessage", None)
        if server is not None and get_origin(server) is Annotated:
            union = get_args(server)[0]
            members = list(get_args(union)) if get_origin(union) is Union else [union]
            discriminated["ServerMessage"] = members
    return models, discriminated


def main() -> int:
    """生成入口：默认写文件，--check 只校验不写。返回进程退出码。"""
    models, discriminated = _collect()
    out = generate_ts(models, discriminated)
    if "--check" in sys.argv:
        with tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False) as f:
            f.write(out)
            tmp = f.name
        diff = subprocess.run(
            ["git", "diff", "--exit-code", "--no-index", "--", tmp, str(_OUTPUT)],
            capture_output=True, text=True)
        if diff.returncode != 0:
            print("contract.generated.ts 漂移！请运行 make gen-types 重新生成后提交。",
                  file=sys.stderr)
            print(diff.stdout, file=sys.stderr)
            return 1
        return 0
    _OUTPUT.write_text(out, encoding="utf-8")
    print(f"已生成 {_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
