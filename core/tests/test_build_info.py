"""批次 7 Task 8：版本——get_build_sha + build_server_version + build_server_info 契约（marker=unit）。

Run: python -m pytest core/tests/test_build_info.py -v
"""

import subprocess

import pytest

import core.version as version_mod
from core.services.capabilities import build_server_info
from core.version import __version__, get_build_sha


def _make_minimal_agent():
    """最小 agent fixture：真实 Agent（llm/tts/stt config + session，未 start）。

    复用 test_server._make_agent 模式——只构造组合根（不建连），
    build_server_info 只读公开属性 + config。
    """
    import os

    os.environ.setdefault("DEEPSEEK_API_KEY", "test")
    from core.agent import Agent
    from core.config import AppConfig
    from core.tests.fake_llm import FakeLLMBackend

    agent = Agent(config=AppConfig())
    agent.llm._backend = FakeLLMBackend(response="test")
    return agent


@pytest.mark.unit
class TestGetBuildSha:
    def test_no_raise(self):
        """get_build_sha 不抛异常，返回 str | None（非 git 仓库/失败 → None）。"""
        sha = get_build_sha()
        assert sha is None or isinstance(sha, str)

    def test_module_cache_git_called_once(self, monkeypatch):
        """模块级缓存：两次调用返回同一值，subprocess git 只跑一次。"""
        calls: list = []
        fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=b"abc123def456\n")

        def _fake_run(*args, **kwargs):
            calls.append(args)
            return fake

        monkeypatch.setattr(subprocess, "run", _fake_run)
        # 重置模块级缓存，保证本测试独立于其它测试先跑 git
        # （批次 8c F17：缓存分 _build_sha/_build_sha_tried 两态，需一并重置——
        # 否则前序测试已置 tried=True，git 不重跑、calls==0 断言失败）
        monkeypatch.setattr(version_mod, "_build_sha", None)
        monkeypatch.setattr(version_mod, "_build_sha_tried", False)

        a = get_build_sha()
        b = get_build_sha()
        assert a == b == "abc123def456"
        assert len(calls) == 1, "get_build_sha 应缓存结果，subprocess git 只跑一次"


@pytest.mark.unit
class TestBuildServerInfo:
    def test_server_info_version_features_and_build_sha(self):
        """server_version 含 __version__ 前缀 + features.media_upload True + build_sha 上 wire。"""
        agent = _make_minimal_agent()
        try:
            info = build_server_info(agent)
            # server_version 以 __version__ 开头（git 可用时带 +build_sha[:7] 后缀）
            assert info.server_version.startswith(__version__)
            # features.media_upload 如实声明（批次 7 已实现）
            assert info.features.media_upload is True
            # build_sha 字段上 wire：与 get_build_sha() 一致；git 可用时非 None
            sha = get_build_sha()
            assert info.build_sha == sha
            if sha:
                assert isinstance(sha, str)
                assert f"+{sha[:7]}" in info.server_version
                wire = info.wire_dict()
                assert wire["build_sha"] == sha
        finally:
            pass
