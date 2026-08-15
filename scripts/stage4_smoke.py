#!/usr/bin/env python3
"""阶段 4 确定性契约 smoke（BE-S4-INT-P1-01，spec §4.3）。

定位：真实 Electron 联调前快速证明阶段 4 关键语义（副作用边界）。默认本地 stub/固定
fixture，确定性、不进 CI 外部依赖；`STAGE4_SMOKE_LIVE=1` 显式 opt-in 接真实 provider。
与真实 provider/Electron 联调（spec §5.4）区分——smoke 通过 ≠ 真实联调通过。

用法:
  python scripts/stage4_smoke.py          # 默认 stub 全场景
  python scripts/stage4_smoke.py --json   # 报告追加 JSON 尾行（供测试/CI 消费）
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import WSMsgType  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from frontend_contract.version import PROTOCOL_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PCM_FIXTURE = ROOT / "core" / "input" / "tests" / "fixtures" / "sample_16k_mono.pcm"


def _backend_sha() -> str:
    """当前后端 SHA（git rev-parse HEAD；失败回退 'unknown'，不阻断）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, cwd=str(ROOT))
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class _SmokeSTTBackend:
    """固定文本 STT 桩（spec §5.1 默认 stub；构造不发网络）。"""

    def __init__(self, text: str = "你好，这是阶段4 smoke 转写。"):
        self._text = text
        self.transcribed = 0

    async def transcribe(self, audio: bytes, **kw: Any):
        self.transcribed += 1
        return self._text

    async def transcribe_stream(self, audio: bytes, **kw: Any):
        self.transcribed += 1
        yield self._text

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 1}

    async def close(self):
        pass


class _Smoke:
    """场景运行器：PASS/FAIL 收集 + 首个失败点 + JSON 报告。"""

    def __init__(self, live: bool):
        self.live = live
        self.results: list[dict] = []
        self.first_failure: str | None = None

    def check(self, scenario: str, cond: bool, detail: str = ""):
        self.results.append({"scenario": scenario, "ok": bool(cond), "detail": detail})
        if not cond and self.first_failure is None:
            self.first_failure = f"{scenario}: {detail}"
        print(f"  [{'PASS' if cond else 'FAIL'}] {scenario} {detail}")

    def report(self) -> dict:
        total = len(self.results)
        failed = sum(1 for r in self.results if not r["ok"])
        passed = total - failed
        print(f"\n结果: {passed} 通过 / {failed} 失败")
        print(f"后端 SHA: {_backend_sha()}  协议: {PROTOCOL_VERSION}  "
              f"live={self.live}  首个失败点: {self.first_failure or '-'}")
        return {
            "exit": 1 if self.first_failure else 0,
            "passed": passed, "failed": failed, "total": total,
            "sha": _backend_sha(), "protocol": PROTOCOL_VERSION, "live": self.live,
            "first_failure": self.first_failure,
            "scenarios": [r["scenario"] for r in self.results],
        }


async def _profile_scenarios(smoke: _Smoke, agent: Any, client: TestClient) -> None:
    """Profile 与模型切换（spec §5.2）：列表不回显密钥 / test 成功+失败 / save-delete / PROFILE_IN_USE。"""
    from frontend_contract import rest

    resp = await client.get("/api/providers")
    data = await resp.json()
    smoke.check("Profile 列表可读", resp.status == 200 and "llm" in data and "stt" in data)
    # 不回显密钥值：查密钥值前缀/超长 token，不查字段名（api_key_env 是环境变量名引用，合法）
    blob = json.dumps(data)
    smoke.check("Profile 列表不回显 api_key 值",
                all(kw not in blob for kw in ("sk-", "Bearer ", "AIza")), blob[:120])

    # test 成功（默认 sensevoice 走工厂→DEFAULT_BASE_URL；stub 工厂在 patch 内）
    fake = _SmokeSTTBackend()
    with patch("Module.stt.registry.STTFactory.create", return_value=fake):
        r = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "connectivity"})
        d = await r.json()
    smoke.check("Profile test 成功 ok=true", d.get("ok") is True and d.get("latency_ms", -1) >= 0,
                str(d))

    # 服务离线 → 可诊断 ok=false（不报 URL 缺协议）
    class _Offline(_SmokeSTTBackend):
        async def test(self):
            return {"ok": False, "error": "无法连接 SenseVoice 服务 (http://localhost:20042): "
                                          "connection refused"}
    with patch("Module.stt.registry.STTFactory.create", return_value=_Offline()):
        r = await client.post("/api/providers/test", json={
            "service": "stt", "provider": "sensevoice",
            "model": "sense-voice", "level": "connectivity"})
        d = await r.json()
    smoke.check("Profile test 离线可诊断", d.get("ok") is False and
                "Request URL is missing" not in d.get("error", ""), str(d))

    # save/delete 自定义 Profile + 正在使用的 Profile 不可删除（spec §5.2）
    # custom_providers.yaml/secret_store 隔离到 tmp（smoke 不写真实配置）；base_url 用公网
    # IP 字面量（SSRF 免 DNS 放行）；PROFILE_IN_USE 经 config.llm.model 标记"正在使用"→ 409。
    import tempfile
    from core.input import custom_providers as cp
    from core.storage import secret_store
    with tempfile.TemporaryDirectory() as _tmp:
        _tmp_yaml = Path(_tmp) / "custom_providers.yaml"
        with patch.object(cp, "_DEFAULT_PATH", _tmp_yaml), \
             patch.object(secret_store, "_DEFAULT_DIR", Path(_tmp)):
            r = await client.post("/api/providers", json={
                "service": "llm",
                "entry": {"name": "smoke_llm", "base_url": "http://1.2.3.4:8080",
                          "models": ["m1"], "runtime": "backend"}})
            d = await r.json()
            smoke.check("Profile save 成功", r.status == 200 and d.get("ok") is True, str(d)[:120])
            saved_model = agent.config.llm.model
            agent.config.llm.model = "custom/smoke_llm"   # 标记"正在使用" → 删除应 409
            r = await client.delete("/api/providers/llm/smoke_llm")
            d = await r.json()
            agent.config.llm.model = saved_model
            smoke.check("正在使用的 Profile 不可删除", r.status == 409 and
                        d.get("code") == "PROFILE_IN_USE", str(d)[:120])
            r = await client.delete("/api/providers/llm/smoke_llm")
            d = await r.json()
            smoke.check("Profile delete 成功", r.status == 200 and d.get("ok") is True, str(d)[:120])


class _SmokeTTSBackend:
    """TTS 桩（spec §5.1 默认 stub）：voices/preview 走真实路由（_active_tts_profile 读
    agent.tts 槽位——**不经过 TTSFactory**，故 main() 直接挂 agent.tts + agent.tts_enabled）。

    provider_name 非 minimax → source=static；synthesize 对未知 voice_id 抛异常 → 路由
    结构化 {ok: False, error}；offline=True 模拟 provider 离线。
    """

    provider_name = ""

    def __init__(self):
        self.offline = False

    async def list_voices(self) -> list[dict]:
        return [{"id": "elysia_test_001", "name": "Elysia 测试音"}]

    async def synthesize(self, text: str, *, character: str | None = None,
                         emotion: str | None = None, voice_id: str | None = None) -> bytes:
        if self.offline:
            raise RuntimeError("无法连接 TTS 服务 (localhost:9880): connection refused")
        if voice_id and voice_id != "elysia_test_001":
            raise RuntimeError("voice 不存在")
        return (Path(__file__).resolve().parent.parent / "Module/stt/tests/fixtures/sample.wav").read_bytes()

    async def synthesize_stream(self, text: str, *, character: str | None = None,
                                emotion: str | None = None, voice_id: str | None = None):
        yield (Path(__file__).resolve().parent.parent / "Module/stt/tests/fixtures/sample.wav").read_bytes()

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 1}

    async def close(self) -> None:
        pass


async def _tts_rest_scenarios(smoke: _Smoke, tts_stub: _SmokeTTSBackend, client: TestClient) -> None:
    """TTS voices/preview（spec §5.2）：列表可读、preview 成功/离线/非法 voice 确定响应。"""
    r = await client.get("/api/tts/voices")
    d = await r.json()
    smoke.check("voices 列表可读", r.status == 200 and
                any(v.get("id") == "elysia_test_001" for v in d.get("voices", [])), str(d)[:120])
    r = await client.post("/api/tts/preview", json={"voice_id": "elysia_test_001"})
    d = await r.json()
    smoke.check("preview 成功", d.get("ok") is True and d.get("audio_base64"), str(d)[:120])
    tts_stub.offline = True
    r = await client.post("/api/tts/preview", json={"voice_id": "elysia_test_001"})
    d = await r.json()
    smoke.check("preview provider 离线", d.get("ok") is False and
                "Request URL is missing" not in d.get("error", ""), str(d)[:120])
    tts_stub.offline = False
    r = await client.post("/api/tts/preview", json={"voice_id": "no_such_voice"})
    d = await r.json()
    smoke.check("preview 非法 voice", d.get("ok") is False and d.get("error"), str(d)[:120])


async def _voice_ws_scenarios(smoke: _Smoke, client: TestClient) -> None:
    """普通录音/实时语音/听写/服务控制（spec §5.2）：真实 ws_handler + VoiceSessionManager。"""
    pcm = PCM_FIXTURE.read_bytes()

    ws = await client.session.ws_connect(client.make_url("/ws"), headers={"X-Protocol-Version": "2"})
    for _ in range(6):  # 排空建连帧
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") in ("capabilities", "server_info"):
                break
        except asyncio.TimeoutError:
            break

    # ── 普通录音：start(recordingId) → BINARY → stop → voice.transcript（不自动发消息）──
    await ws.send_json({"type": "voice.record.start", "recordingId": "rec_smoke_1"})
    await ws.send_bytes(pcm)
    await ws.send_json({"type": "voice.record.stop", "recordingId": "rec_smoke_1"})
    seen: list[dict] = []
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        seen.append(d)
        if d.get("type") == "voice.transcript":
            break
    transcript = next(d for d in seen if d.get("type") == "voice.transcript")
    smoke.check("普通录音 transcript 不自动发消息",
                bool(transcript.get("text")) and
                not any(d.get("type", "").startswith("user_message.ack") for d in seen),
                str(transcript)[:120])
    # commit(voice.commit) → 用户消息 + generation；discard(voice.discard) → 不创建
    await ws.send_json({"type": "voice.commit", "recordingId": "rec_smoke_1",
                        "text": transcript.get("text", ""),
                        "clientRequestId": "req_smoke_commit1"})
    committed = False
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=15)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "user_message.ack":
            committed = True
        if d.get("type") == "state" and d.get("state") == "idle":
            break
    smoke.check("commit 触发消息+generation", committed, "")
    # discard：start → stop → discard（voice_sessions.py:339-340 在 RECORDING 态 direct
    # discard 会 raise「先 stop 再 discard」）——stop 后再 discard，短暂窗口断言无
    # user_message.ack（discard 不创建消息、不触发 generation），随后可再次录音。
    await ws.send_json({"type": "voice.record.start", "recordingId": "rec_smoke_2"})
    await ws.send_bytes(pcm[:4096])
    await ws.send_json({"type": "voice.record.stop", "recordingId": "rec_smoke_2"})
    await ws.send_json({"type": "voice.discard", "recordingId": "rec_smoke_2"})
    no_ack = True
    for _ in range(3):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=1)
            if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") == "user_message.ack":
                no_ack = False
        except asyncio.TimeoutError:
            break
    smoke.check("discard 不创建消息", no_ack, "")
    # 实时语音：start(sessionId) → BINARY → 等 ack（_ack_loop ACK_INTERVAL=1s，会话须
    # 保持 ≥1s 否则首帧 ack 永不发出，voice_sessions.py:747-752）→ stop → ended
    await ws.send_json({"type": "voice_session.start", "sessionId": "vs_smoke_1"})
    await ws.send_bytes(pcm)
    turn_ok = ack_ok = False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not ack_ok:
        m = await asyncio.wait_for(ws.receive(), timeout=5)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        t = d.get("type")
        if t == "voice_session.turn":
            turn_ok = True
        if t == "voice_session.ack":
            ack_ok = d.get("seq", 0) >= 1
    await ws.send_bytes(pcm[:4096])
    await ws.send_json({"type": "voice_session.stop", "sessionId": "vs_smoke_1"})
    ended_ok = False
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=15)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        t = d.get("type")
        # 修正（P1-01 实测）：voice_session.turn 在 stop_session 的
        # _drain_dispatch_stt→_on_segment 提交路径才发出（feed 期间 VAD 连续语音不
        # 分段），故 turn 帧出现在 ended 之前而非 ack 等待循环内——在 ended 循环
        # 一并采集 turn_ok，避免「turn 在 stop 后才到」导致误判。
        if t == "voice_session.turn":
            turn_ok = True
        if t == "voice_session.ended":
            ended_ok = d.get("reason") == "stopped"
            break
    smoke.check("实时语音 turn/ack/ended", turn_ok and ack_ok and ended_ok,
                f"turn={turn_ok} ack={ack_ok} ended={ended_ok}")
    # 听写：start(clientRequestId) → BINARY → stop → final；cancel → cancelled
    await ws.send_json({"type": "dictation.start", "clientRequestId": "req_smoke_dict1"})
    await ws.send_bytes(pcm)
    await ws.send_json({"type": "dictation.stop", "clientRequestId": "req_smoke_dict1"})
    final_ok = False
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "dictation.final":
            final_ok = d.get("text") and d.get("clientRequestId") == "req_smoke_dict1"
            break
    smoke.check("听写 final 只回文字", final_ok, "")
    await ws.send_json({"type": "dictation.start", "clientRequestId": "req_smoke_dict2"})
    await ws.send_bytes(pcm[:2048])
    await ws.send_json({"type": "dictation.cancel", "clientRequestId": "req_smoke_dict2"})
    cancelled_ok = False
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "dictation.cancelled":
            cancelled_ok = d.get("clientRequestId") == "req_smoke_dict2"
            break
    smoke.check("听写 cancel 清理", cancelled_ok, "")

    # ── 服务控制：switch_model / set_tts_voice（spec §5.2）──
    # switch_stt 建真后端 + 1s 连通测试（server.py:198-254）——patch STTFactory.create 桩
    # 使成功路径确定性；失败路径（custom/nonexistent → ValueError → ok=false）无需 patch。
    with patch("Module.stt.registry.STTFactory.create", return_value=_SmokeSTTBackend()):
        await ws.send_json({"type": "switch_model", "service": "stt", "model": "sensevoice/sense-voice"})
        sw_ok = None
        while True:
            m = await asyncio.wait_for(ws.receive(), timeout=10)
            if m.type != WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d.get("type") == "switch_model_result":
                sw_ok = d.get("ok")
                break
    smoke.check("switch_model 成功 ok=true", sw_ok is True, "")
    await ws.send_json({"type": "switch_model", "service": "stt", "model": "custom/nonexistent"})
    sw_fail = None
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "switch_model_result":
            sw_fail = d.get("ok")
            break
    smoke.check("switch_model 失败 ok=false 保留旧模型", sw_fail is False, "")
    # set_tts_voice：经 _active_tts_profile(agent._tts 桩).list_voices 校验 → 成功带 voice 回显
    await ws.send_json({"type": "set_tts_voice", "voice": "elysia_test_001"})
    sv_ok = None
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "set_tts_voice_result":
            sv_ok = (d.get("ok") is True and d.get("voice") == "elysia_test_001")
            break
    smoke.check("set_tts_voice 成功带 voice 回显", sv_ok, "")
    await ws.send_json({"type": "set_tts_voice", "voice": "no_such_voice"})
    sv_fail = None
    while True:
        m = await asyncio.wait_for(ws.receive(), timeout=10)
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "set_tts_voice_result":
            sv_fail = (d.get("ok") is False and d.get("error"))
            break
    smoke.check("set_tts_voice 非法 voice 失败", sv_fail, "")
    await ws.close()


async def _tts_playback_scenarios(smoke: _Smoke) -> None:
    """TTS 播放控制（spec §5.2）：audio_start→chunks→end 连续；cancel→audio_cancelled。

    复用 `core/adapter/tests/test_tts_adapter_control.py` 的 FakeOut + FakeTTS 模式——
    TTSStreamManager(tts, output, character_name) 是播放帧责任方；reset_for_turn() 预置
    seq 门控（否则 _download_and_forward 卡 wait）；确定性、不碰真实 generation/TTS 网络。
    """
    from core.adapter.tts_adapter import TTSStreamManager

    class _Recorder:
        def __init__(self):
            self.calls: list[tuple] = []
            self.gate: asyncio.Event | None = None  # 置位后 audio_chunk 阻塞（停在转发段）
        async def _broadcast(self, method: str, *a: Any, **kw: Any):
            self.calls.append((method, a, kw))
        async def send(self, *a: Any, **kw: Any):
            pass
        async def audio_chunk(self, chunk: bytes, *, stream_id: int = 0, **kw: Any):
            self.calls.append(("chunk", stream_id, len(chunk)))
            if self.gate is not None:
                await self.gate.wait()
        async def audio_end(self, stream_id: int = 0, *, format: str = "wav", **kw: Any):
            self.calls.append(("end", stream_id))
        async def audio_start(self, stream_id: int, *, format: str = "wav",
                              sample_rate: int | None = None, duration_ms: int | None = None,
                              **kw: Any):
            self.calls.append(("start", stream_id, sample_rate, duration_ms))
        async def audio_cancelled(self, stream_id: int, **kw: Any):
            self.calls.append(("cancelled", stream_id))

    class _StubTTS:
        stream_format = "wav"
        def get_preprocess(self) -> None:
            return None
        async def synthesize(self, text: str, *, character: str | None = None,
                             emotion: str | None = None, voice_id: str | None = None) -> bytes:
            wav = (Path(__file__).resolve().parent.parent / "Module/stt/tests/fixtures/sample.wav").read_bytes()
            return wav

    # ── 完成序列：audio_start → chunk×N → audio_end ──
    rec = _Recorder()
    # smoke 桩为最小面（_StubTTS/_Recorder 仅实现被调用方法），与 TTSBackend/OutputManager
    # 协议结构不匹配——确定性契约 smoke 专用桩，`# type: ignore[arg-type]` 显式豁免
    mgr = TTSStreamManager(tts=_StubTTS(), output=rec, character_name="elysia")  # type: ignore[arg-type]
    mgr.reset_for_turn()          # 必需：预置 seq=1 门控
    mgr.stream_sentence("你好")
    await mgr.wait_all()
    starts = [c for c in rec.calls if c[0] == "start"]
    chunks = [c for c in rec.calls if c[0] == "chunk"]
    ends = [c for c in rec.calls if c[0] == "end"]
    smoke.check("TTS audio_start→chunks→end 连续",
                len(starts) == 1 and len(chunks) >= 1 and len(ends) == 1, str(rec.calls)[:120])
    smoke.check("TTS chunk stream_id 一致", all(c[1] == starts[0][1] for c in chunks), "")

    # ── 取消：gate 停在转发段 → cancel_all → audio_cancelled（无孤儿 chunk 续发）──
    rec2 = _Recorder()
    rec2.gate = asyncio.Event()
    mgr2 = TTSStreamManager(tts=_StubTTS(), output=rec2, character_name="elysia")  # type: ignore[arg-type]
    mgr2.reset_for_turn()
    mgr2.stream_sentence("你好")
    deadline = time.monotonic() + 3
    while not any(c[0] == "start" for c in rec2.calls) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    mgr2.cancel_all()
    rec2.gate.set()               # 释放被堵任务（cancel 本身不依赖 gate，仅清理）
    await asyncio.sleep(0.05)
    smoke.check("TTS cancel 后 audio_cancelled",
                any(c[0] == "cancelled" for c in rec2.calls), str(rec2.calls)[:120])


async def main() -> int:
    live = os.environ.get("STAGE4_SMOKE_LIVE") == "1"
    smoke = _Smoke(live=live)
    from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
    from core.input.server import create_app

    agent = _make_agent_with(_UsageLLM())
    # 先 start：_make_agent_with 设 config.stt/tts.enabled=False → start 不建真后端
    # （否则 STTFactory.create 连 localhost:20042 启动即失败）。state: idle 由主循环
    # （agent.py:516 while _state == RUNNING）在生成终态后发出——必须 start 才消费。
    await agent.start()
    # STT/TTS 桩：start 后注入私有槽位（agent.stt/tts/stt_enabled/tts_enabled 均只读
    # property，注入私有字段用 SLF001 豁免——对齐 test_voice_ws/test_tts_voices 先例）。
    # ws_handler 判 agent.stt is not None 才建 audio_buf；tts_voices/preview 经
    # _active_tts_profile 读 agent.tts + agent.tts_enabled（不经 TTSFactory）。
    agent._stt = _SmokeSTTBackend()      # noqa: SLF001  # smoke 注入桩
    agent._stt_enabled = True            # noqa: SLF001
    tts_stub = _SmokeTTSBackend()
    agent._tts = tts_stub                # noqa: SLF001
    agent._tts_enabled = True            # noqa: SLF001

    try:
        app = create_app(agent, serve_static=False)
        async with TestClient(TestServer(app)) as client:
            await _profile_scenarios(smoke, agent, client)
            await _tts_rest_scenarios(smoke, tts_stub, client)
            await _voice_ws_scenarios(smoke, client)
            await _tts_playback_scenarios(smoke)
    finally:
        await agent.stop()
    report = smoke.report()
    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False))
    return report["exit"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
