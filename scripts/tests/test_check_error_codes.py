"""错误码单源门禁测试（marker=unit）。

验证 scripts/check_error_codes.py 的两条检查：
1. 文档 §9.1 表 ↔ `frontend_contract.errors.KNOWN_ERROR_CODES` 一致（原有）。
2. **发射端扫描**：`core/` + `Module/` 里实际构造错误信封时写入的 `code` 字面量
   必须全部登记在册——这是 2026-09-11 暴露的缺口（`character_routes.py` 发射了
   14 个未登记码，而旧门禁只比对文档与列表、看不见路由里真正发的码）。
"""

from __future__ import annotations

import pytest

from scripts.check_error_codes import scan_emitted_codes, uncataloged_emitted_codes

pytestmark = pytest.mark.unit


def _tree(tmp_path, rel_source: str):
    """在临时目录写一个 emitter 源文件，返回根路径。"""
    p = tmp_path / "core" / "input" / "fake_routes.py"
    p.parent.mkdir(parents=True)
    p.write_text(rel_source, encoding="utf-8")
    return tmp_path


def test_real_repo_emits_only_cataloged_codes():
    """真实仓库：所有发射的 code 都必须在册（本测试即门禁本体）。"""
    from frontend_contract.errors import KNOWN_ERROR_CODES, KNOWN_ISSUE_CODES

    extra = uncataloged_emitted_codes(
        known=KNOWN_ERROR_CODES | KNOWN_ISSUE_CODES)
    assert extra == {}, (
        "以下错误码在 core/ Module/ 被发射但未登记于 frontend_contract/errors.py：\n"
        + "\n".join(f"  {code}  {sorted(files)}" for code, files in sorted(extra.items()))
    )


def test_scanner_flags_uncataloged_code(tmp_path):
    """未知码（发射了但不在册）必须被标记。"""
    root = _tree(tmp_path, (
        "def handler():\n"
        '    return _protocol_error_response("TOTALLY_NEW_CODE", "x", 400)\n'
    ))
    extra = uncataloged_emitted_codes(root=root, known=frozenset({"INVALID_REQUEST"}))
    assert "TOTALLY_NEW_CODE" in extra


def test_scanner_flags_uncataloged_kwarg_code(tmp_path):
    """`code="X"` 关键字形态同样要被抓。"""
    root = _tree(tmp_path, (
        "def f():\n"
        '    code = "ANOTHER_NEW_CODE"\n'
        "    return AckError(code=code)\n"
    ))
    extra = uncataloged_emitted_codes(root=root, known=frozenset({"INVALID_REQUEST"}))
    assert "ANOTHER_NEW_CODE" in extra


def test_scanner_ignores_else_branch_of_unrelated_ternary(tmp_path):
    """`x = "OK" if a else "FAIL"` 里的 FAIL 不是错误码，不得误报。"""
    root = _tree(tmp_path, (
        "def f(tr):\n"
        '    log(tr, "OK" if tr.success else "FAIL")\n'
    ))
    extra = uncataloged_emitted_codes(root=root, known=frozenset({"INVALID_REQUEST"}))
    assert extra == {}


def test_scanner_recognizes_ternary_code_assignment(tmp_path):
    """`code = "A" if x else "B"` 两个分支都算发射。"""
    root = _tree(tmp_path, (
        "def f(x):\n"
        '    code = "INVALID_REQUEST" if x else "BRAND_NEW_CODE"\n'
        "    return _protocol_error_response(code)\n"
    ))
    extra = uncataloged_emitted_codes(root=root, known=frozenset({"INVALID_REQUEST"}))
    assert extra == {"BRAND_NEW_CODE": {str(root / "core/input/fake_routes.py")}}


def test_scanner_returns_file_locations(tmp_path):
    """扫描结果要带文件位置，便于定位。"""
    root = _tree(tmp_path, 'x = _protocol_error_response("SOME_UNKNOWN", "m", 400)\n')
    result = scan_emitted_codes(root=root)
    assert "SOME_UNKNOWN" in result
    assert any(p.endswith("fake_routes.py") for p in result["SOME_UNKNOWN"])
