"""会话 pending JSONL 测试隔离回归（P3 邻域发现，2026-09-11）。

## 被固化的故障链

`SessionMemory.save()` 无 repo 时降级写 `data/session_{id}.json`；测试的假会话
（`session_id="A"` 等）因此把残留写进**仓库共享 data/ 目录**。随后**任何一次真实
Agent 启动**都会 `SessionManager.recover_pending_sessions()` 把这些残留导入生产
`sessions` 集合并删除 JSONL（durable 后清理）——实测把假会话 A/B 洗成了生产库里
`character_binding={}` 的无绑定会话，`character_profiles=true` 下前端整页拒绝。

真实库防线拦不住这条路径：它只拦 pytest 进程内的 Mongo 写入，而这里是
「pytest 写本地文件 → 之后由非 pytest 的真实进程洗进库」。

隔离由根 `conftest.py::_isolate_session_pending_jsonl` 提供（ELYSIA_PATH_SESSION_PENDING
覆盖到 tmp_path）。本文件锁死该隔离，防止有人删掉 fixture 后污染静默复发。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from paths import ProjectPath

pytestmark = pytest.mark.unit

_REPO_DATA = Path(__file__).resolve().parent.parent.parent / "data"


def test_tests_cannot_write_session_jsonl_into_repo_data():
    """测试里 SessionMemory.save() 必须落在 tmp_path，绝不写进仓库 data/。"""
    from core.memory.session import SessionMemory

    probe_id = "iso_probe_session_jsonl"
    residue = _REPO_DATA / f"session_{probe_id}.json"
    assert not residue.exists(), "前置条件：探针文件不应已存在"

    session = SessionMemory(session_id=probe_id)
    asyncio.run(session.save())

    assert not residue.exists(), (
        f"测试把会话 JSONL 写进了仓库 data/（{residue}）——"
        "真实 Agent 启动会把它回收进生产库，见本模块 docstring"
    )
    # 隔离生效：落到 tmp_path（而非仓库 data/）
    assert str(ProjectPath.SESSION_PENDING.resolve(session_id=probe_id)).startswith(
        str(Path(ProjectPath.SESSION_PENDING.resolve(session_id="x")).parent))


def test_pending_path_is_overridden_under_pytest():
    """隔离本身可断言：pytest 下 pending 模板已指向临时目录。"""
    resolved = ProjectPath.SESSION_PENDING.resolve(session_id="whatever")
    assert str(resolved) != str(_REPO_DATA / "session_whatever.json")
    assert "pytest" in str(resolved) or "tmp" in str(resolved).lower(), (
        f"pending 路径未指向临时目录：{resolved}")
