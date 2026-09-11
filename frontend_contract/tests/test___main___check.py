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


@pytest.mark.unit
def test_generated_artifacts_include_client_message_union(tmp_path, monkeypatch):
    """F2（review）：ClientMessage 判别联合须发射到 TS 产物（9B-2 入站消息契约）。

    _collect 只注册 ServerMessage 时 contract.raw.ts / contract.generated.ts 缺
    `export type ClientMessage`——Electron 侧拿不到入站消息 union。生成后断言两个
    产物都含 ClientMessage 判别联合别名 + 具名成员（V2TextMessage 等）。
    """
    outputs = {
        "ts": tmp_path / "contract.generated.ts",
        "schema": tmp_path / "contract.schema.json",
        "raw_ts": tmp_path / "contract.raw.ts",
    }
    monkeypatch.setattr(main_mod, "_OUTPUTS", outputs)
    assert main_mod.main() == 0
    raw = (tmp_path / "contract.raw.ts").read_text(encoding="utf-8")
    ts = (tmp_path / "contract.generated.ts").read_text(encoding="utf-8")
    for content, label in ((raw, "contract.raw.ts"), (ts, "contract.generated.ts")):
        assert "export type ClientMessage =" in content, f"{label} 缺 ClientMessage union"
        assert "  | V2TextMessage" in content, f"{label} 缺 ClientMessage 成员 V2TextMessage"
        assert "export type ServerMessage =" in content, f"{label} 缺 ServerMessage union（回归 Guard）"
        assert "  | VoiceRecordStartMessage" in content, \
            f"{label} 缺 ClientMessage 成员 VoiceRecordStartMessage"


@pytest.mark.unit
def test_client_message_artifact_has_no_unknown():
    """P1-4：契约产物 ClientMessage 段不含 UnknownMessage、ServerMessage 段保留。

    机器契约与 JSON Schema 严格 oneOf 一致——任意 `{type:string}` 不再通过 TS。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    artifacts = [
        root / "frontend_contract" / "contract.raw.ts",
        root / "frontend" / "src" / "types" / "contract.generated.ts",
    ]
    for path in artifacts:
        content = path.read_text(encoding="utf-8")
        assert "export type ClientMessage =" in content, f"{path} 缺 ClientMessage union"
        sm_block = content[content.index("export type ServerMessage ="):
                           content.index("export type ClientMessage =")]
        assert "  | UnknownMessage" in sm_block, f"{path} ServerMessage 应保留 UnknownMessage"
        cm_block = content[content.index("export type ClientMessage ="):
                           content.index("export interface UnknownMessage")]
        assert "UnknownMessage" not in cm_block, f"{path} ClientMessage 不应含 UnknownMessage"


# ─────────────── 产物顺序稳定性（ARCH-INC-P2-02） ───────────────


@pytest.mark.unit
def test_collect_returns_models_sorted_by_name():
    """`_collect()` 必须按类名排序——产物顺序是**模型集合的纯函数**，与文件布局无关。

    为什么需要这条不变量（2026-09-11 实测）：`vars(module)` 的迭代顺序 = 源码里类的
    定义顺序，因此产物顺序此前**耦合于文件布局**。把 `ws_message.py` 按领域拆成包
    （ARCH-INC-P2-02）后，`contract.generated.ts` / `contract.raw.ts` 出现 **4296 行
    纯位移 diff**（语义完全不变——TS interface/type 声明都会 hoist，顺序纯装饰）。
    排序后，今后整理文件/调整 import 顺序**不再产生契约产物噪声**。

    依赖关系不受影响：`generate_ts` 内部仍走 `_topo_sort`（被引用者先输出），
    排序只决定 DFS 起始顺序，即无依赖关系者之间的稳定 tie-break。
    """
    models, _ = main_mod._collect()  # noqa: SLF001 — 内部装配函数，测其顺序契约
    names = [m.__name__ for m in models]
    assert names == sorted(names), "模型顺序未排序：产物将随文件布局漂移"


@pytest.mark.unit
def test_collect_is_deterministic_across_calls():
    """`_collect()` 多次调用给出**完全相同**的模型顺序（无隐藏非确定性）。

    注意 `generate_ts` 的 `_topo_sort` 是 DFS（依赖者先输出），其输出**本就对输入顺序
    敏感**——这正是 `_collect()` 必须排序的原因：排序把「输入顺序」钉成模型集合的纯
    函数，DFS 才因此可复现。故此处断言的是 `_collect()` 的确定性，而非生成器对任意
    输入顺序免疫。
    """
    from frontend_contract.gen_ts import generate_ts

    a_models, a_disc = main_mod._collect()  # noqa: SLF001
    b_models, b_disc = main_mod._collect()  # noqa: SLF001

    assert [m.__name__ for m in a_models] == [m.__name__ for m in b_models]
    assert a_disc.keys() == b_disc.keys()
    opt = {"no_unknown": {"ClientMessage"}}
    assert generate_ts(a_models, a_disc, **opt) == generate_ts(b_models, b_disc, **opt)


@pytest.mark.unit
def test_schema_artifact_is_order_independent():
    """JSON Schema 用 `sort_keys=True` 序列化——顺序无关，故 `schemaHash` 不受布局影响。

    这条是 P2-02 拆分的关键前提：拆分让 TS 产物重排（4296 行纯位移），但
    **机器契约（schema + schemaHash）逐字节不变**，跨仓 `check-backend-contract.mjs`
    与 Electron 侧零感知。
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    doc = json.loads((root / "frontend_contract" / "contract.schema.json").read_text(encoding="utf-8"))
    from frontend_contract import gen_schema

    rebuilt = gen_schema.build_document()
    assert rebuilt["schemaHash"] == doc["schemaHash"], "schemaHash 漂移（机器契约不稳定）"
    assert gen_schema.serialize_document(rebuilt) == gen_schema.serialize_document(doc)
