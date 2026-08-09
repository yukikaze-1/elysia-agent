"""__main__ 产物生成 + 漂移检测测试（marker=unit）。

覆盖 Task 4（9B-1）：version 常量单源 + `python -m frontend_contract --check`
对 ts/schema/raw_ts 三产物的漂移门禁（改动产物内容 → 非零退出码）。
"""
import sys

import pytest

import frontend_contract.__main__ as main_mod


@pytest.mark.unit
def test_version_constants():
    """version.py 常量：TRANSPORT_VERSION 供握手头、PROTOCOL_VERSION 仅展示。"""
    from frontend_contract import version

    assert version.TRANSPORT_VERSION == "2"
    assert version.PROTOCOL_VERSION == "2.0"


@pytest.mark.unit
def test_check_detects_drift(tmp_path, monkeypatch):
    """漂移门禁：monkeypatch 输出路径到 tmp_path，先 main() 生成，改动产物内容后
    --check 模式重跑同一入口 → 返回非 0。

    不用 runpy.run_module：runpy 以 run_name="__main__" 重新执行模块代码，生成全新
    模块全局（monkeypatch 的 _OUTPUTS 不生效）；直接调用 main() 并临时改 sys.argv
    覆盖同一 --check 代码路径，等价且可观测。
    """
    outputs = {
        "ts": tmp_path / "contract.generated.ts",
        "schema": tmp_path / "contract.schema.json",
        "raw_ts": tmp_path / "contract.raw.ts",
    }
    monkeypatch.setattr(main_mod, "_OUTPUTS", outputs)

    # 默认模式：三产物全部生成到 tmp_path（不污染仓库）
    assert main_mod.main() == 0
    for p in outputs.values():
        assert p.exists()
    # schema 产物含判别联合模型 + 版本头
    schema_text = (tmp_path / "contract.schema.json").read_text(encoding="utf-8")
    assert '"transportVersion": "2"' in schema_text
    assert '"ServerMessage"' in schema_text

    # 改动产物内容 → --check 漂移返回非 0
    (tmp_path / "contract.generated.ts").write_text("// tampered\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["frontend_contract", "--check"])
    assert main_mod.main() != 0
