#!/usr/bin/env python3
"""Debug 前端凭据守卫（S2-F3，2026-09-12）。

背景：`/debug/api/*` 归管理面，只认 `SERVER_AUTH_TOKEN`（CR-03）。此前 debug 前端
**不携带任何凭据**——配了 `SERVER_AUTH_TOKEN` 就整个数据面 401，仪表盘退化成空壳。
修法是所有请求统一走 `frontend/src/debug/auth.ts::debugFetch`（补 Bearer 头）。

凭据是**横切**关注点：漏改任何一处调用都表现为一个 401 页签，而前端没有测试运行器
（`npm run build` 只跑 vue-tsc + vite），所以用本脚本做可执行守卫。规则：

1. `frontend/src/debug/**` 与 `frontend/src/stores/debugStore.ts` 内**不得出现裸
   `fetch(` 调用**——唯一豁免是 `debug/auth.ts`（包装器本体）。
2. 全仓 debug 前端**不得出现 `EventSource`**——它协议上无法设置请求头，凭据进不去。
   SSE 走 `frontend/src/debug/sse.ts` 的 fetch + ReadableStream 实现。

注释与字符串里提到这些词是允许的（本脚本先剥注释再匹配），否则连解释"为什么不用
EventSource"都会触发守卫。

退出码：0 通过 / 1 有违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 扫描范围：debug 应用本体 + 它用的 store（store 不在 debug/ 下，容易漏）
SCAN_DIRS = [ROOT / "frontend" / "src" / "debug"]
SCAN_FILES = [ROOT / "frontend" / "src" / "stores" / "debugStore.ts"]

# 包装器本体——它是**唯一**允许直接调 fetch 的地方
FETCH_WRAPPER = (ROOT / "frontend" / "src" / "debug" / "auth.ts").resolve()

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_RAW_FETCH = re.compile(r"(?<![A-Za-z0-9_$.])fetch\s*\(")
_EVENT_SOURCE = re.compile(r"(?<![A-Za-z0-9_$.])EventSource\s*\(")


def _strip_comments(text: str) -> str:
    """剥掉块注释与行注释，保留换行数以便行号对得上。"""
    text = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return _LINE_COMMENT.sub("", text)


def _scanned() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        if d.is_dir():
            files.extend(sorted(p for p in d.rglob("*")
                                if p.suffix in (".ts", ".vue") and p.is_file()))
    files.extend(p for p in SCAN_FILES if p.is_file())
    return files


def main() -> int:
    violations: list[str] = []
    scanned = _scanned()
    if not scanned:
        print("✗ 未扫描到任何 debug 前端文件——路径变了？守卫已失效", file=sys.stderr)
        return 1

    for path in scanned:
        code = _strip_comments(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT)
        for lineno, line in enumerate(code.split("\n"), start=1):
            if _EVENT_SOURCE.search(line):
                violations.append(
                    f"{rel}:{lineno} 使用了 EventSource——协议上无法设置请求头，"
                    f"管理面凭据带不进去；改用 debug/sse.ts 的 readSseStream")
            if _RAW_FETCH.search(line) and path.resolve() != FETCH_WRAPPER:
                violations.append(
                    f"{rel}:{lineno} 直接调 fetch——凭据会被漏掉；"
                    f"改用 debug/auth.ts 的 debugFetch")

    if violations:
        print(f"Debug 前端凭据守卫失败 {len(violations)} 项：", file=sys.stderr)
        for v in violations:
            print(f"  [S2-F3] ✘ {v}", file=sys.stderr)
        print("\n（`/debug/api/*` 只认 SERVER_AUTH_TOKEN，请求必须带 Authorization 头）",
              file=sys.stderr)
        return 1

    print(f"Debug 前端凭据守卫通过：{len(scanned)} 个文件"
          f"（裸 fetch 仅限 debug/auth.ts，EventSource 零使用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
