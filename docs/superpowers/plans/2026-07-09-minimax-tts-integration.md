# MiniMax TTS 云端接入 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 MiniMax 云端 TTS backend，支持 HTTP 同步（非流式 + SSE 流式）语音合成

**Architecture:** 遵循现有 Protocol-Registry-Backend 三层模式。新建 `MiniMaxTTSBackend` 实现 `TTSBackend` ABC，`httpx.AsyncClient` 共享复用 + `asyncio.Queue` 流式桥接（参考 `gpt_sovits.py`）。hex 编码音频解码，SSE 逐行解析。

**Tech Stack:** Python 3.13, httpx, asyncio, pytest + unittest.mock

## Global Constraints

- Python 3.13+
- 遵循现有 TTSBackend 协议：`synthesize()` → `bytes`，`synthesize_stream()` → `AsyncGenerator[bytes, None]`，`test()` → `dict`，`close()` → `None`
- 使用 `Module/_common/test_helpers.make_test_method()` 工厂生成 `test()`
- 使用 `Module/_common/registry_helpers.resolve_api_key()` / `resolve_base_url()` / `inject_logger()`
- `__init__.py` 只导出 ABC + Factory + Config，不导出具体 backend 类
- 不修改 `core/tts_stream.py` 或任何 `core/` 文件
- TDD：先写测试再写实现
- 每个 task 结束时 commit

---

### Task 1: `strip_for_minimax()` — 新增文本预处理函数

**Files:**
- Modify: `Module/tts/text_preprocess.py`
- Modify: `Module/tts/test_tts.py`

**Produces:** `strip_for_minimax(text: str) -> str` — 删除 `[]`、`<<>>`、emoji，保留 `()`、`<>`

- [ ] **Step 1: 添加测试用例**

在 `test_tts.py` 末尾追加 `TestStripForMiniMax` 类：

```python
# ============================================================
# strip_for_minimax
# ============================================================

class TestStripForMiniMax:
    def test_removes_brackets_and_double_angle(self):
        text = "你好，[微笑] 今天<<开心>>呢"
        result = strip_for_minimax(text)
        assert result == "你好， 今天呢"

    def test_keeps_parens_tone_tags(self):
        text = "今天是不是很开心呀(laughs)，当然了！"
        result = strip_for_minimax(text)
        assert result == "今天是不是很开心呀(laughs)，当然了！"

    def test_keeps_angle_pause_control(self):
        text = "你好<#0.5#>世界"
        result = strip_for_minimax(text)
        assert result == "你好<#0.5#>世界"

    def test_keeps_fullwidth_parens(self):
        text = "嗯（语气轻柔地）好的"
        result = strip_for_minimax(text)
        assert result == "嗯（语气轻柔地）好的"

    def test_keeps_pinyin_annotation(self):
        text = "处理(chu3li3)完成"
        result = strip_for_minimax(text)
        assert result == "处理(chu3li3)完成"

    def test_removes_emoji(self):
        text = "hello 😀 world 🎉"
        result = strip_for_minimax(text)
        assert "😀" not in result
        assert "🎉" not in result
        assert "hello" in result
        assert "world" in result

    def test_no_markup_passthrough(self):
        assert strip_for_minimax("今天天气真好") == "今天天气真好"

    def test_empty_string(self):
        assert strip_for_minimax("") == ""

    def test_pure_markup_returns_empty(self):
        assert strip_for_minimax("[笑]<<开心>>") == ""
```

并在文件顶部 import 行添加 `strip_for_minimax`：

```python
from .text_preprocess import strip_all_markup, strip_action_only, strip_for_minimax
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest Module/tts/test_tts.py::TestStripForMiniMax -v`
Expected: FAIL with `ImportError: cannot import name 'strip_for_minimax'`

- [ ] **Step 3: 实现 `strip_for_minimax()`**

在 `text_preprocess.py` 末尾追加：

```python
def strip_for_minimax(text: str) -> str:
    """为 MiniMax TTS 预处理文本。

    保留 MiniMax 原生支持的标记：()（语气词、发音标注）、<>（停顿控制 <#x#>）。
    删除 MiniMax 不支持的标记：[]（动作描写）、<<>>（心情描写）、emoji。

    注意：不引入 strip_all_markup 中的 <.*?> 正则（会误删 <#x#> 停顿标签）。
    """
    text = re.sub(r"\[.*?\]", "", text)         # [动作描写]
    text = re.sub(r"\<\<.*?\>\>", "", text)     # <<心情描写>>
    text = _EMOJI_RE.sub("", text)              # emoji
    return text.strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest Module/tts/test_tts.py::TestStripForMiniMax -v`
Expected: PASS

- [ ] **Step 5: 确认已有测试未被破坏**

Run: `python -m pytest Module/tts/test_tts.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add Module/tts/text_preprocess.py Module/tts/test_tts.py
git commit -m "feat: add strip_for_minimax() to preserve MiniMax tone/pause tags"
```

---

### Task 2: `MiniMaxTTSBackend` — 新建后端类

**Files:**
- Create: `Module/tts/minimax_tts.py`

**Produces:** `MiniMaxTTSBackend(TTSBackend)` — 实现全部 4 个抽象方法

- [ ] **Step 1: 创建文件骨架**

```python
"""MiniMax TTS 后端 — HTTP + SSE 流式合成。

共享 httpx.AsyncClient（Bearer Token 认证），hex 编码音频解码。
SSE 流式通过 asyncio.Queue + create_task 桥接。
"""

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from .protocol import TTSBackend
from .text_preprocess import strip_for_minimax
from Module._common.test_helpers import make_test_method

_TTS_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


class MiniMaxTTSBackend(TTSBackend):
    """MiniMax 语音合成后端。

    character 通过 voice_map 映射为 voice_setting.voice_id，
    未命中时 fallback 到构造参数的 voice_id。
    emotion 直接映射为 voice_setting.emotion。
    """

    def __init__(
        self,
        model: str = "speech-2.8-hd",
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com",
        voice_id: str = "",
        voice_map: dict[str, str] | None = None,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: float = 0.0,
        sample_rate: int = 32000,
        audio_format: str = "wav",
        logger: logging.Logger | None = None,
    ):
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._voice_id = voice_id
        self._voice_map = voice_map or {}
        self._speed = speed
        self._vol = vol
        self._pitch = pitch
        self._sample_rate = sample_rate
        self._audio_format = audio_format
        self._logger = logger or logging.getLogger(__name__)
        self._preprocess = strip_for_minimax

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_TTS_TIMEOUT,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

        async def _health_check(self) -> bool:
            resp = await self._client.post(
                "/v1/get_voice",
                json={"voice_type": "system"},
                timeout=httpx.Timeout(5.0),
            )
            if resp.status_code >= 400:
                return False
            return resp.json().get("base_resp", {}).get("status_code") == 0

        self._test_fn = make_test_method(_health_check)

    # ---- TTSBackend 协议 ----

    async def test(self) -> dict:
        return await self._test_fn(self)

    async def synthesize(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> bytes:
        text = self._preprocess(text)
        voice_id = self._resolve_voice(character)
        payload = self._build_payload(text, voice_id, emotion, stream=False)
        resp = await self._client.post("/v1/t2a_v2", json=payload)
        resp.raise_for_status()
        data = resp.json()
        self._check_base_resp(data)
        audio_hex = data.get("data", {}).get("audio", "")
        return bytes.fromhex(audio_hex)

    async def synthesize_stream(
        self, text: str, *, character: str, emotion: str | None = None
    ) -> AsyncIterator[bytes]:
        text = self._preprocess(text)
        voice_id = self._resolve_voice(character)
        payload = self._build_payload(text, voice_id, emotion, stream=True)

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def _pull() -> None:
            try:
                async with self._client.stream(
                    "POST", "/v1/t2a_v2", json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        self._logger.warning(
                            "MiniMax 返回 %d: %s (text=%r)",
                            resp.status_code,
                            body.decode("utf-8", errors="replace")[:500],
                            text,
                        )
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            raw = line[6:]
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            self._check_base_resp(event)
                            audio_hex = event.get("data", {}).get("audio", "")
                            if audio_hex:
                                await queue.put(bytes.fromhex(audio_hex))
            except httpx.RequestError as e:
                self._logger.debug(
                    "MiniMax 连接失败 (base_url=%s): %s", self._base_url, e
                )
                raise RuntimeError(
                    f"无法连接 MiniMax 服务 ({self._base_url}): {e}"
                ) from e
            finally:
                await queue.put(None)

        task = asyncio.create_task(_pull())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.getLogger("Module.tts.minimax").debug(
                    "TTS _pull task failed: %s", e
                )

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 内部方法 ----

    def _resolve_voice(self, character: str) -> str:
        """按 voice_map 查找 voice_id，未命中 fallback 到默认值。"""
        return self._voice_map.get(character, self._voice_id)

    def _build_payload(
        self, text: str, voice_id: str, emotion: str | None, stream: bool
    ) -> dict:
        """构建 MiniMax /v1/t2a_v2 请求体。"""
        payload: dict = {
            "model": self._model,
            "text": text,
            "stream": stream,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": self._speed,
                "vol": self._vol,
                "pitch": self._pitch,
            },
            "audio_setting": {
                "sample_rate": self._sample_rate,
                "format": "mp3" if stream else self._audio_format,
                "channel": 1,
            },
        }
        if emotion:
            payload["voice_setting"]["emotion"] = emotion
        return payload

    @staticmethod
    def _check_base_resp(data: dict) -> None:
        """检查 base_resp.status_code，非 0 抛 RuntimeError。"""
        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(
                f"MiniMax API 错误: status_code={base_resp.get('status_code')}, "
                f"status_msg={base_resp.get('status_msg', 'unknown')}"
            )
```

- [ ] **Step 2: 验证导入**

Run: `python -c "from Module.tts.minimax_tts import MiniMaxTTSBackend; print('OK')"`
Expected: OK（不报错即可，此时还没有测试）

- [ ] **Step 3: Commit**

```bash
git add Module/tts/minimax_tts.py
git commit -m "feat: add MiniMaxTTSBackend with HTTP + SSE streaming"
```

---

### Task 3: MiniMaxTTSBackend 单元测试

**Files:**
- Modify: `Module/tts/test_tts.py`

**Produces:** 构造、非流式、流式、错误处理、取消 全套测试

- [ ] **Step 1: 添加 import 和 fixture**

在 `test_tts.py` 顶部 import 区域追加：

```python
from .minimax_tts import MiniMaxTTSBackend
```

在文件末尾（`TestGPTSoVitsCancel` 类之后）追加所有测试：

- [ ] **Step 2: 添加构造测试**

```python
# ============================================================
# MiniMaxTTSBackend
# ============================================================

class TestMiniMaxConstruction:
    def test_defaults(self):
        backend = MiniMaxTTSBackend(api_key="sk-test")
        assert backend._model == "speech-2.8-hd"
        assert backend._base_url == "https://api.minimaxi.com"
        assert backend._voice_id == ""
        assert backend._voice_map == {}
        assert backend._speed == 1.0
        assert backend._vol == 1.0
        assert not backend._client.is_closed

    def test_voice_map_resolve(self):
        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            voice_id="default-voice",
            voice_map={"Elysia": "female-elysia-01"},
        )
        assert backend._resolve_voice("Elysia") == "female-elysia-01"
        assert backend._resolve_voice("Unknown") == "default-voice"

    def test_voice_map_default_voice_id(self):
        backend = MiniMaxTTSBackend(api_key="sk-test")
        assert backend._resolve_voice("Anyone") == ""

    @pytest.mark.asyncio
    async def test_close(self):
        backend = MiniMaxTTSBackend(api_key="sk-test")
        await backend.close()
        assert backend._client.is_closed
```

- [ ] **Step 3: 添加 synthesize 测试**

```python
class TestMiniMaxSynthesize:
    @pytest.mark.asyncio
    async def test_hex_decode_and_return_bytes(self):
        """验证非流式：POST → JSON → hex decode → bytes"""
        import base64

        backend = MiniMaxTTSBackend(api_key="sk-test")

        # mock _client.post 返回 hex 编码的音频
        wav_bytes = b"RIFF....WAVE...."  # 伪 WAV
        hex_audio = wav_bytes.hex()

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": hex_audio, "status": 2},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        backend._client.post = lambda *a, **kw: MockResp()  # type: ignore[assignment]
        result = await backend.synthesize("你好", character="test")
        assert result == wav_bytes

    @pytest.mark.asyncio
    async def test_payload_structure(self):
        """验证请求体包含正确的 voice_setting 和 audio_setting"""
        captured_payload = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_payload.update(json)
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            model="speech-2.8-turbo",
            voice_id="male-qn-qingse",
            speed=1.2,
            vol=0.8,
            pitch=0.5,
            audio_format="wav",
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("你好", character="Elysia", emotion="happy")

        assert captured_payload["model"] == "speech-2.8-turbo"
        assert captured_payload["text"] == "你好"
        assert captured_payload["stream"] is False
        assert captured_payload["voice_setting"]["voice_id"] == "male-qn-qingse"
        assert captured_payload["voice_setting"]["speed"] == 1.2
        assert captured_payload["voice_setting"]["emotion"] == "happy"
        assert captured_payload["audio_setting"]["format"] == "wav"

    @pytest.mark.asyncio
    async def test_voice_map_used_for_character(self):
        """character 通过 voice_map 映射到 voice_id"""
        captured_voice_id = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_voice_id["voice_id"] = json["voice_setting"]["voice_id"]
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            voice_id="default",
            voice_map={"Elysia": "female-elysia-01"},
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="Elysia")
        assert captured_voice_id["voice_id"] == "female-elysia-01"

    @pytest.mark.asyncio
    async def test_no_emotion_in_payload_when_none(self):
        captured_payload = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_payload.update(json)
            return MockResp()

        backend = MiniMaxTTSBackend(api_key="sk-test")
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="test", emotion=None)
        assert "emotion" not in captured_payload["voice_setting"]
```

- [ ] **Step 4: 添加 synthesize_stream 测试**

```python
class TestMiniMaxSynthesizeStream:
    @pytest.mark.asyncio
    async def test_yields_decoded_chunks_from_sse(self):
        """流式：SSE 逐行解析 → hex decode → yield bytes"""
        wav_chunk1 = b"\x00\x01\x02\x03"
        wav_chunk2 = b"\x04\x05\x06\x07"
        sse_lines = [
            f'data: {{"data":{{"audio":"{wav_chunk1.hex()}"}},"base_resp":{{"status_code":0}}}}\n',
            f'data: {{"data":{{"audio":"{wav_chunk2.hex()}"}},"base_resp":{{"status_code":0}}}}\n',
        ]

        class MockStreamResp:
            status_code = 200

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                for line in sse_lines:
                    yield line

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        backend = MiniMaxTTSBackend(api_key="sk-test")
        backend._client.stream = lambda *a, **kw: MockStreamResp()  # type: ignore[assignment]

        chunks = []
        async for chunk in backend.synthesize_stream("test", character="x"):
            chunks.append(chunk)
        assert chunks == [wav_chunk1, wav_chunk2]

    @pytest.mark.asyncio
    async def test_stream_format_is_mp3(self):
        """流式模式 audio_setting.format 强制为 mp3"""
        captured_payload = {}

        class MockStreamResp:
            status_code = 200

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                yield 'data: {"data":{"audio":"00ff"},"base_resp":{"status_code":0}}\n'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def fake_stream(method, url, *, json, **kw):
            captured_payload.update(json)

            class Ctx:
                async def __aenter__(self):
                    return MockStreamResp()

                async def __aexit__(self, *args):
                    pass

            return Ctx()

        backend = MiniMaxTTSBackend(api_key="sk-test", audio_format="wav")
        backend._client.stream = fake_stream  # type: ignore[assignment]
        async for _ in backend.synthesize_stream("test", character="x"):
            pass
        assert captured_payload["stream"] is True
        assert captured_payload["audio_setting"]["format"] == "mp3"
```

- [ ] **Step 5: 添加错误处理和取消测试**

```python
class TestMiniMaxErrors:
    @pytest.mark.asyncio
    async def test_base_resp_non_zero_raises(self):
        """base_resp.status_code != 0 → RuntimeError"""
        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": ""},
                    "base_resp": {"status_code": 1004, "status_msg": "rate limit"},
                }

        backend = MiniMaxTTSBackend(api_key="sk-test")
        backend._client.post = lambda *a, **kw: MockResp()  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="MiniMax API 错误"):
            await backend.synthesize("hi", character="test")

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        """HTTP 4xx/5xx → httpx.HTTPStatusError"""
        import httpx

        class MockResp:
            status_code = 401

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
                )

            def json(self):
                return {"base_resp": {"status_code": 1001, "status_msg": "unauthorized"}}

        backend = MiniMaxTTSBackend(api_key="sk-bad")
        backend._client.post = lambda *a, **kw: MockResp()  # type: ignore[assignment]
        with pytest.raises(httpx.HTTPStatusError):
            await backend.synthesize("hi", character="test")

    @pytest.mark.asyncio
    async def test_empty_audio_returns_empty_bytes(self):
        """data.audio 为空字符串 → 返回空 bytes"""
        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": ""},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        backend = MiniMaxTTSBackend(api_key="sk-test")
        backend._client.post = lambda *a, **kw: MockResp()  # type: ignore[assignment]
        result = await backend.synthesize("hi", character="test")
        assert result == b""


class TestMiniMaxCancel:
    @pytest.mark.asyncio
    async def test_stream_early_break_cancels_task(self):
        """consumer 提前停止迭代时 background task 被取消。"""
        import asyncio

        class MockStreamResp:
            status_code = 200

            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                # 第一个 chunk 后永远等待（模拟被取消）
                yield 'data: {"data":{"audio":"00ff"},"base_resp":{"status_code":0}}\n'
                await asyncio.sleep(10)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        backend = MiniMaxTTSBackend(api_key="sk-test")
        backend._client.stream = lambda *a, **kw: MockStreamResp()  # type: ignore[assignment]

        async for _ in backend.synthesize_stream("test", character="x"):
            break  # 只取首个 chunk 即退出
        # 不抛异常即为通过
```

- [ ] **Step 6: 运行所有 MiniMax 测试**

Run: `python -m pytest Module/tts/test_tts.py -v -k "MiniMax"`
Expected: ALL PASS

- [ ] **Step 7: 运行全部 TTS 测试确保无回归**

Run: `python -m pytest Module/tts/test_tts.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add Module/tts/test_tts.py
git commit -m "test: add MiniMaxTTSBackend unit tests"
```

---

### Task 4: 注册 `minimax` provider

**Files:**
- Modify: `Module/tts/registry.py`
- Modify: `Module/tts/test_tts.py`（工厂测试）

- [ ] **Step 1: 添加 `_BACKENDS` 条目**

在 `registry.py` 的 `_BACKENDS` dict 末尾追加：

```python
"minimax": {
    "base_url": "https://api.minimaxi.com",
    "env_key": "MINIMAX_API_KEY",
},
```

`_BACKENDS` 变为：

```python
_BACKENDS: dict[str, dict] = {
    "gpt-sovits": {
        "base_url": "http://localhost:9880",
        "env_key": "GPT_SOVITS_URL",
    },
    "custom": {
        "base_url": "",
        "env_key": "CUSTOM_TTS_API_KEY",
    },
    "minimax": {
        "base_url": "https://api.minimaxi.com",
        "env_key": "MINIMAX_API_KEY",
    },
}
```

- [ ] **Step 2: 添加 `TTSFactory.create()` 的 `minimax` 分支**

在 `create()` 方法中，`if provider == "custom"` 分支之后（`return OpenAICompatTTSBackend(...)` 之后），`if provider == "gpt-sovits"` 分支之前，插入：

```python
# minimax: MiniMax TTS 云端服务
if provider == "minimax":
    from .minimax_tts import MiniMaxTTSBackend
    api_key = resolve_api_key("MINIMAX_API_KEY", cfg, kwargs.pop("api_key", None))
    base_url = resolve_base_url("MINIMAX_TTS_URL", cfg, kwargs.pop("base_url", None))
    if not api_key:
        raise ValueError("MiniMax TTS 需要 api_key，请设置 MINIMAX_API_KEY 环境变量或显式传入")
    if "logger" not in kwargs:
        kwargs["logger"] = inject_logger(__name__)
    return MiniMaxTTSBackend(model=backend_name, api_key=api_key, base_url=base_url, **kwargs)
```

- [ ] **Step 3: 添加 TTSFactory 的 minimax 测试**

在 `test_tts.py` 的 `TestTTSFactory` 类末尾追加：

```python
def test_minimax_provider(self):
    with patch("Module.tts.registry.MiniMaxTTSBackend") as mock_cls:
        mock_cls.return_value = MagicMock()
        backend = TTSFactory.create(
            "minimax/speech-2.8-hd",
            api_key="sk-test",
        )
    assert backend is not None
    call_kwargs = mock_cls.call_args.kwargs
    assert call_kwargs["api_key"] == "sk-test"
    assert call_kwargs["model"] == "speech-2.8-hd"
    assert "https://api.minimaxi.com" in call_kwargs["base_url"]

def test_minimax_missing_api_key_raises(self):
    with patch("os.getenv", return_value=None):
        with pytest.raises(ValueError, match="MiniMax TTS 需要 api_key"):
            TTSFactory.create("minimax/speech-2.8-hd")

def test_minimax_custom_base_url(self):
    with patch("Module.tts.registry.MiniMaxTTSBackend") as mock_cls:
        mock_cls.return_value = MagicMock()
        TTSFactory.create(
            "minimax/speech-2.8-hd",
            api_key="sk-test",
            base_url="https://api-bj.minimaxi.com",
        )
    assert mock_cls.call_args.kwargs["base_url"] == "https://api-bj.minimaxi.com"
```

- [ ] **Step 4: 运行 minimax 相关测试**

Run: `python -m pytest Module/tts/test_tts.py -v -k "MiniMax or minimax"`
Expected: ALL PASS

- [ ] **Step 5: 运行全部 TTS 测试**

Run: `python -m pytest Module/tts/test_tts.py Module/tts/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add Module/tts/registry.py Module/tts/test_tts.py
git commit -m "feat: register minimax provider in TTSFactory"
```

---

### Task 5: 冒烟测试（手动，需要 API key）

**Files:**
- Modify: `Module/tts/__main__.py`

- [ ] **Step 1: 添加 minimax 冒烟测试段**

在 `__main__.py` 中追加 minimax 测试段（放在现有测试之后）：

```python
# --- MiniMax TTS (云端) ---
import os
if os.getenv("MINIMAX_API_KEY"):
    print("\n[MiniMax TTS]")
    try:
        mm = TTSFactory.create("minimax/speech-2.8-turbo")
        result = await mm.test()
        print(f"  test: {result}")
        if result.get("ok"):
            audio = await mm.synthesize("你好，今天天气真好(sighs)，当然了！", character="default")
            print(f"  synthesize: {len(audio)} bytes")
        await mm.close()
    except Exception as e:
        print(f"  ERROR: {e}")
else:
    print("\n[MiniMax TTS] 跳过（MINIMAX_API_KEY 未设置）")
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('Module/tts/__main__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add Module/tts/__main__.py
git commit -m "test: add MiniMax smoke test to __main__.py"
```

---

### Task 6: 最终验证

- [ ] **Step 1: 运行全部 TTS 测试（最终确认）**

Run: `python -m pytest Module/tts/ -v`
Expected: ALL PASS

- [ ] **Step 2: 导入链验证**

Run: `python -c "from Module.tts import TTSBackend, TTSFactory, TTSConfig; b = TTSFactory.create('minimax/speech-2.8-hd', api_key='sk-test'); print(type(b).__name__)"`
Expected: `MiniMaxTTSBackend`

