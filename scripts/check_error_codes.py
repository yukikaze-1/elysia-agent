#!/usr/bin/env python3
"""错误码单源校验（F5）——两条检查：

1. `docs/electron-backend-protocol-v2.md` §9.1 表 ↔ `frontend_contract/errors.py`
   `KNOWN_ERROR_CODES`——增删错误码必须同时改两处。
2. **发射端扫描**——`core/` + `Module/` 里实际写入错误信封的 `code` 字面量必须
   全部在册。旧版只有检查 1：它比对"文档表 ↔ 列表"，而**看不见路由真正发射的码**，
   因此 §9.1 曾长期自称 41 码、实际可达 54 码（2026-09-11 发现）。

用法: python scripts/check_error_codes.py
退出码: 0=一致；1=漂移（打印缺失/多余/未登记码）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 项目根在 sys.path（frontend_contract 可 import）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# 协议文档相对项目根
_DOC = _PROJECT_ROOT / "docs" / "electron-backend-protocol-v2.md"
# §9.1 错误码表区间（表头 `| code | HTTP |` 到下一个 `---` 分隔或 `**数据保护语义**`）
_SECTION_START = "### 9.1 错误码目录"
_CODE_RE = re.compile(r"^\| `([A-Z][A-Z0-9_]+)` \|")

# ── 发射端扫描 ────────────────────────────────────────────────────────────────
# 扫描范围：core/ + Module/（排除 tests）。三形态：
#   a) `_protocol_error_response("CODE", ...)`（HTTP 错误信封，位置参数）
#   b) `code="CODE"` / `code = "CODE"`（关键字或赋值；三元表达式首分支）
#   c) `code = "A" if ... else "B"`（三元第二分支；可跨行）
# 用 (?<![A-Za-z0-9_]) 防止误捕 `error_code=` / `status_code=` 之类。
_CALL_POS = re.compile(r'_protocol_error_response\(\s*"([A-Z][A-Z0-9_]+)"')
_CODE_ASSIGN = re.compile(r'(?<![A-Za-z0-9_])code\s*=\s*"([A-Z][A-Z0-9_]+)"')
_CODE_TERNARY = re.compile(
    r'(?<![A-Za-z0-9_])code\s*=\s*"[A-Z][A-Z0-9_]+"\s+if\b.{0,200}?\belse\s+"([A-Z][A-Z0-9_]+)"',
    re.S,
)
_SCAN_DIRS = ("core", "Module")


def scan_emitted_codes(root: Path | None = None) -> dict[str, set[str]]:
    """扫描 `core/` + `Module/` 的错误码发射点。

    Returns:
        `{code: {文件路径, ...}}`。文件路径为相对 `root` 的完整路径字符串。
        仅覆盖**静态字面量**——经变量间接计算、或运行时拼接的码扫不到（已知局限）。
    """
    root = Path(root) if root is not None else _PROJECT_ROOT
    found: dict[str, set[str]] = {}
    for base in _SCAN_DIRS:
        for f in (root / base).rglob("*.py"):
            if "/tests/" in f.as_posix() or "__pycache__" in f.as_posix():
                continue
            try:
                txt = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for rx in (_CALL_POS, _CODE_ASSIGN, _CODE_TERNARY):
                for m in rx.finditer(txt):
                    found.setdefault(m.group(1), set()).add(str(f))
    return found


def uncataloged_emitted_codes(
    root: Path | None = None,
    known: frozenset[str] | set[str] | None = None,
) -> dict[str, set[str]]:
    """返回发射了但未登记的码。

    Args:
        root: 仓库根（默认项目根）。
        known: 允许集（默认 `KNOWN_ERROR_CODES | KNOWN_ISSUE_CODES`）。
    """
    if known is None:
        from frontend_contract.errors import KNOWN_ERROR_CODES, KNOWN_ISSUE_CODES

        known = KNOWN_ERROR_CODES | KNOWN_ISSUE_CODES
    return {
        code: files
        for code, files in scan_emitted_codes(root).items()
        if code not in known
    }


def _doc_codes() -> set[str]:
    """从协议文档 §9.1 表提取码集合。"""
    doc = _DOC.read_text(encoding="utf-8")
    start = doc.index(_SECTION_START)
    section = doc[start:]
    end_marker = "**数据保护语义**"
    end = section.index(end_marker) if end_marker in section else len(section)
    return {
        m.group(1)
        for line in section[:end].splitlines()
        if (m := _CODE_RE.match(line))
    }


def main() -> int:
    from frontend_contract.errors import KNOWN_ERROR_CODES, KNOWN_ISSUE_CODES

    canonical = set(KNOWN_ERROR_CODES)
    doc_codes = _doc_codes()
    missing_in_doc = sorted(canonical - doc_codes)   # 契约有、文档缺
    extra_in_doc = sorted(doc_codes - canonical)     # 文档有、契约缺
    uncataloged = uncataloged_emitted_codes(
        known=KNOWN_ERROR_CODES | KNOWN_ISSUE_CODES)

    ok = not (missing_in_doc or extra_in_doc or uncataloged)
    if ok:
        print(f"错误码单源校验通过：{_DOC.name} §9.1 共 {len(canonical)} 码与契约一致；"
              f"发射端无非登记码")
        return 0

    if missing_in_doc:
        print(f"契约有但文档 §9.1 缺失（{len(missing_in_doc)}）: {', '.join(missing_in_doc)}")
    if extra_in_doc:
        print(f"文档 §9.1 有但契约缺失（{len(extra_in_doc)}）: {', '.join(extra_in_doc)}")
    if uncataloged:
        print(f"发射端有 {len(uncataloged)} 个未登记码（core/ Module/ 实际写入但不在册）:")
        for code, files in sorted(uncataloged.items()):
            print(f"  {code}  <- {', '.join(sorted(files))}")
    print(f"契约 {len(canonical)} 码 vs 文档 {len(doc_codes)} 码，"
          f"发射端未登记 {len(uncataloged)}——不一致，需同步"
          "（frontend_contract/errors.py 或 docs/electron-backend-protocol-v2.md §9.1）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
