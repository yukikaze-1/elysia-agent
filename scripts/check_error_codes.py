#!/usr/bin/env python3
"""错误码单源校验（F5）：`docs/electron-backend-protocol-v2.md` §9.1 表 vs
`frontend_contract/errors.py` KNOWN_ERROR_CODES——防 40/41 不一致漂移。

用法: python scripts/check_error_codes.py
退出码: 0=一致；1=漂移（打印缺失/多余码）。
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


def main() -> int:
    from frontend_contract.errors import KNOWN_ERROR_CODES

    doc = _DOC.read_text(encoding="utf-8")
    start = doc.index(_SECTION_START)
    section = doc[start:]
    # 表截止：`---` 分隔行之后（表尾）或「**数据保护语义**」小节
    end_marker = "**数据保护语义**"
    end = section.index(end_marker) if end_marker in section else len(section)
    table = section[:end]

    doc_codes: set[str] = set()
    for line in table.splitlines():
        m = _CODE_RE.match(line)
        if m:
            doc_codes.add(m.group(1))

    canonical = set(KNOWN_ERROR_CODES)
    missing_in_doc = sorted(canonical - doc_codes)   # 契约有、文档缺
    extra_in_doc = sorted(doc_codes - canonical)     # 文档有、契约缺

    if not missing_in_doc and not extra_in_doc:
        print(f"错误码单源校验通过：{_DOC.name} §9.1 共 {len(canonical)} 码与契约一致")
        return 0
    if missing_in_doc:
        print(f"契约有但文档 §9.1 缺失（{len(missing_in_doc)}）: {', '.join(missing_in_doc)}")
    if extra_in_doc:
        print(f"文档 §9.1 有但契约缺失（{len(extra_in_doc)}）: {', '.join(extra_in_doc)}")
    print(f"契约 {len(canonical)} 码 vs 文档 {len(doc_codes)} 码——不一致，需同步"
          "（frontend_contract/errors.py 或 docs/electron-backend-protocol-v2.md §9.1）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
