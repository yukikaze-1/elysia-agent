"""图片上传落盘目录的**调用期解析**守卫（N-8，marker=service）。

守两条不变量：

1. **调用期解析**—— `ELYSIA_PATH_DATA_UPLOADS_IMAGES` 在 import **之后**设置也必须生效。
   旧实现是模块级 `_IMAGES_DIR = ProjectPath...resolve()`，import 期就把值固化，
   运行期 `monkeypatch.setenv` 对它**完全无效**：测试以为隔离到 tmp，实际写进真实
   `data/uploads/images/`（实测累计 441 个 PNG，B3-CB-7）。
2. **不写真实目录**—— 上传只能落在运行期解析出的目录；真实默认目录的 before/after
   **delta 必须为 0**，且目录被**创建**本身也算变化（不存在 ≠ 空目录）。

## 为什么必须走真实上传路径

`env override → create_app → POST /api/images → 落盘`（而不是 `assert _images_dir() == tmp`）。
helper 断言太弱：`_images_dir()` 对了、而 handler 用了别的常量时它照样绿。
本文件的两条用例都真的发起 HTTP 上传并检查落盘结果。

## 真实性纪律（别把它写成假绿）

- **正对照**：重定向目录里必须**真的**出现 1 个 PNG 且内容一致——否则"真实目录 delta=0"
  毫无证据价值（上传根本没发生也是 delta=0）。
- 真实默认目录**只读快照**：不创建、不删除、不清空（`data/uploads/images` 是真实运行时
  目录，可能有用户上传的文件）；比较的是 delta，不是"目录为空"。
- 快照区分"目录不存在（None）"与"空目录（()）"——两者不同，前者被创建成后者即算回归。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from paths import ProjectPath

# ⚠️ 被测模块在**收集期**（本模块 import 时）就被加载——这不是风格选择，是守卫的前提。
# `_images_dir()` 的旧实现是模块级常量，在 **import 那一刻**定格目录。只有"先 import、
# 后设 env"的时序，本文件的用例才能确定地抓住它；若把 import 挪进 fixture/测试体
# （那时 env 已设好），旧实现会被冻到 tmp 上、守卫退化成假绿（pytest 收集顺序敏感）。
# 见 `test_guard_precondition_module_imported_at_collection_time`。
from core.input import upload_routes  # noqa: E402
from core.input.server import create_app  # noqa: E402

pytestmark = pytest.mark.service

_ENV_IMAGES = "ELYSIA_PATH_DATA_UPLOADS_IMAGES"
_ENV_DESKTOP = "ELYSIA_PATH_DATA_UPLOADS_DESKTOP"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 96
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _true_default_images_dir(monkeypatch) -> Path:
    """**真实**默认图片目录（先摘掉 env 覆盖再解析）——只用于只读快照。"""
    monkeypatch.delenv(_ENV_IMAGES, raising=False)
    return ProjectPath.DATA_UPLOADS_IMAGES.resolve()


def _snapshot(directory: Path) -> list[str] | None:
    """目录快照：不存在 → None（**与空目录 `[]` 区分**，创建目录本身也算变化）。"""
    if not directory.exists():
        return None
    return sorted(p.name for p in directory.iterdir())


def _make_images_app(monkeypatch, tmp_path) -> tuple[Path, object]:
    """最小图片上传 app（与 `test_server.py::TestImageUpload.image_app` 同款）。

    ⚠️ env 覆盖在**函数体内**设置——即 import（模块已在收集期加载）之后，正是旧实现
    失效的时序。同时把 desktop 目录也重定向，避免 `create_app` 的静态挂载在 tmp 之外建目录。
    """
    redirected = tmp_path / "redirected" / "images"
    monkeypatch.setenv(_ENV_IMAGES, str(redirected))
    monkeypatch.setenv(_ENV_DESKTOP, str(tmp_path / "redirected" / "desktop"))

    agent = MagicMock()
    agent.config.server.image_max_mb = 10
    agent.config.server.image_allowed_types = [
        "image/png", "image/jpeg", "image/gif", "image/webp",
    ]
    agent.output = MagicMock()
    agent.bus = MagicMock()
    return redirected, create_app(agent, serve_static=False)


def test_guard_precondition_module_imported_at_collection_time():
    """守卫前提自检：被测模块必须在**任何 fixture 之前**（收集期）已加载。

    旧实现（模块级常量）在 import 那一刻定格目录。删掉本文件顶部的
    `from core.input import upload_routes`、把 import 挪进 fixture，会让旧实现冻到
    tmp 上、上面两条用例退化成假绿——这条把前提变成**可执行的**。

    诚实边界：在 `core/input/__init__.py` 目前连带 import server 的情况下，即便删掉
    顶部 import，本断言仍可能因包初始化而通过；真正不依赖任何外部 import 行为的证据
    是 fresh 子进程用例（那里 import 与设 env 的顺序由脚本显式控制）。
    """
    assert "core.input.upload_routes" in sys.modules, (
        "采集期未加载 core.input.upload_routes——守卫的时序前提被破坏了")


async def _post_png(app) -> tuple[int, str]:
    """POST 一张真 PNG 到 /api/images，返回 (status, body)。"""
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    form = FormData()
    form.add_field("image", _PNG, filename="probe.png", content_type="image/png")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/images", data=form)
        return resp.status, (await resp.read()).decode("utf-8", "replace")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_image_upload_writes_redirected_dir_and_leaves_real_dir_untouched(
        monkeypatch, tmp_path):
    """主不变量：上传落在**运行期**解析的目录，真实默认目录 delta = 0。

    同时给正负两侧证据：
    - 正：重定向目录里出现 1 个 PNG，且内容与上传字节一致（证明上传真的发生）；
    - 负：真实默认目录快照不变（证明没写到不该写的地方）。
    """
    real_dir = _true_default_images_dir(monkeypatch)
    real_before = _snapshot(real_dir)

    redirected, app = _make_images_app(monkeypatch, tmp_path)
    status, body = await _post_png(app)

    assert status == 200, f"上传失败（{status}）：{body}"
    written = sorted(p.name for p in redirected.glob("*.png")) if redirected.exists() else []
    assert len(written) == 1, (
        f"正对照失败：重定向目录 {redirected} 里应有且仅有 1 个 PNG，实际 {written}——"
        "没有写入就没有'真实目录无 delta'的证据")
    assert (redirected / written[0]).read_bytes() == _PNG, "落盘内容与上传字节不一致"

    real_after = _snapshot(real_dir)
    assert real_after == real_before, (
        f"上传写进了真实默认图片目录（调用期解析失效）：{real_dir}\n"
        f"  before={real_before!r}\n  after ={real_after!r}\n"
        f"  （None=不存在，[]=空目录；被创建也算变化）")


@pytest.mark.asyncio
async def test_images_dir_follows_runtime_env_not_import_time(monkeypatch, tmp_path):
    """定点诊断：`_images_dir()` 必须在**调用期**读 env（每次调用现解析）。

    上一条用例证明端到端行为；这条把"哪一层坏了"钉死——它绿而上面红 = handler 没走
    `_images_dir()`；它红 = 函数本身退回 import 期固化。**单独它不算守卫**（§14）。
    """
    _images_dir = upload_routes._images_dir

    first = tmp_path / "one" / "images"
    second = tmp_path / "two" / "images"

    monkeypatch.setenv(_ENV_IMAGES, str(first))
    assert _images_dir() == first, "_images_dir() 没跟随运行期 env（import 期固化了？）"

    monkeypatch.setenv(_ENV_IMAGES, str(second))
    assert _images_dir() == second, "同一进程内改 env 后 `_images_dir()` 没跟着变"


#: fresh 子进程探针：**先 import、再设 env**，然后真的走一次 HTTP 上传。
#: 用 `-c` + 原始字符串传入（逐字节原样，不被父进程的转义规则改写）。
_SUBPROCESS_PROBE = r'''
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

redirected = Path(sys.argv[1])
real_dir = Path(sys.argv[2])
png_path = Path(sys.argv[3])
desktop_dir = Path(sys.argv[4])


def snapshot(directory):
    if not directory.exists():
        return None                  # 不存在 ≠ 空目录
    return sorted(p.name for p in directory.iterdir())


before = snapshot(real_dir)

# ⚠️ 顺序即被测对象：先 import（此刻没有任何 env 覆盖），之后才设覆盖。
# import 期固化的实现会在这一行定格真实目录 —— 正是 B3-CB-7 的形态。
from core.input.server import create_app
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

os.environ["ELYSIA_PATH_DATA_UPLOADS_IMAGES"] = str(redirected)
os.environ["ELYSIA_PATH_DATA_UPLOADS_DESKTOP"] = str(desktop_dir)

agent = MagicMock()
agent.config.server.image_max_mb = 10
agent.config.server.image_allowed_types = ["image/png"]
agent.output = MagicMock()
agent.bus = MagicMock()
app = create_app(agent, serve_static=False)


async def upload():
    form = FormData()
    form.add_field("image", png_path.read_bytes(), filename="probe.png",
                   content_type="image/png")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/images", data=form)
        return response.status, (await response.read()).decode("utf-8", "replace")
    finally:
        await client.close()


status, body = asyncio.run(upload())
after = snapshot(real_dir)
print(json.dumps({
    "status": status,
    "body": body[:200],
    "redirected": (sorted(p.name for p in redirected.glob("*.png"))
                   if redirected.exists() else []),
    "real_before": before,
    "real_after": after,
}))
'''


def test_fresh_process_import_then_override_still_redirects_upload(monkeypatch, tmp_path):
    """fresh 子进程复核（§22）：守的是**行为**，不是当前 pytest 进程的偶然加载状态。

    子进程里 `core.input.upload_routes` 在"没有任何 env 覆盖"时被 import（旧实现此刻
    定格真实目录），之后才设覆盖并发起真实上传。两个断言的失败原因不同、都能自证：
    重定向目录为空 = 文件根本没写到该写的地方；真实目录 delta ≠ 0 = 写到了不该写的
    地方（**红的原因就是"文件落错目录"**，不是 fixture/import 报错——那时 returncode ≠ 0
    会先被下面的断言拦下并打印完整 stderr）。
    """
    real_dir = _true_default_images_dir(monkeypatch)
    real_before = _snapshot(real_dir)

    redirected = tmp_path / "fresh" / "images"
    probe_png = tmp_path / "probe.png"
    probe_png.write_bytes(_PNG)

    env = {k: v for k, v in os.environ.items() if k != _ENV_IMAGES}
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_PROBE,
         str(redirected), str(real_dir), str(probe_png), str(tmp_path / "fresh" / "desktop")],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f"子进程崩溃（{proc.returncode}）——本条守卫拒答，不当成通过：\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    lines = [line for line in proc.stdout.strip().splitlines() if line.startswith("{")]
    assert lines, f"子进程没有输出判定 JSON：\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(lines[-1])

    assert result["status"] == 200, f"子进程上传失败：{result}"
    assert len(result["redirected"]) == 1, (
        f"fresh 进程没有把文件写进重定向目录（调用期解析失效）：{result}")
    assert result["real_after"] == real_before, (
        f"fresh 进程把文件写进了真实默认图片目录（调用期解析失效）：{result}")
    assert _snapshot(real_dir) == real_before, "父进程侧复核：真实默认目录出现了 delta"
