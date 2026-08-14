# 阶段 3 真实联调后端复验修复实现计划：视觉工具会话换绑 + 音频 MIME 规范化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复阶段 3 真实联调复验发现的 2 项后端 P0：① `ToolContext.services["session"]` 每轮随目标会话换绑（`describe_image` 读到当前会话当前图片）；② 音频 `kind` 确定后 MIME 规范化为契约值（`audio+video/webm→audio/webm`、`audio+audio/x-wav→audio/wav`），写/读/下载三路径共用单源。

**Architecture:** P0-01 在 `core/cognitive/loop.py::process()` 目标会话解析后（`set_session(target)` 之后、`trace_id` 更新之前）内联换绑 `tool_context.services["session"]`+`["_session_msg_count"]`——`clone_for_call` 浅拷贝 services，本 turn 所有工具调用副本立即可见。P0-02 在 `core/schema/utils.py` 新增 `normalize_attachment_mime(kind, mime)` 纯函数作单源，写路径（`upload_routes.py` final_kind 确定后）、读路径（`ws_v2._attachment_part`）、下载路径（`attachment_routes.py` Content-Type）三处消费；静默 `except: pass` 改 `logger.warning`。

**Tech Stack:** Python 3.11+ / aiohttp / pydantic / pytest（pytest-aiohttp）/ CognitiveLoop / ToolAdapter。

**Spec:** `docs/superpowers/specs/2026-08-15-stage3-run-backend-fixes-design.md`（上游需求锚点：`docs/review/2026-08-15-阶段3真实联调后端复验问题清单.md`）

## Global Constraints

- **零契约响应形状变更**：`AttachmentInfo`/`AudioPart`/`VideoPart` 字段与枚举取值不变（`audio/webm`/`audio/wav` 已在 `AudioMime`）——`make check-types` 必须无漂移（spec §2/§8）。
- **不反向破坏已通过契约**（复验清单 §5）：签名下载/Range/搜索分页/混合上传与回滚均不得触碰；P0-02 规范化只发生在 `final_kind` 确定**之后**，魔数/扩展名/大小/逐 hint kind 复核顺序不动。
- **`audio/x-wav` 只允许出现在两处**：`upload_routes.py` 探测表（`_MIME_KIND`/`_MIME_EXTS`）与 `core/schema/utils.py` 规范化别名表；输出/响应/持久化/下载/文档不得出现（spec §7.5 M1）。
- **禁相对导入**（ruff TID252），一律绝对导入。
- **测试 marker**：全部 `service`（`pytestmark = pytest.mark.service`；vision 相关沿用既有认知层测试惯例）。
- **视觉测试禁碰真实 LLM**：`describe_image` 视觉后端必须 stub（`LLMFactory.create` patch），缓存目录 patch 到 tmp_path（防写真实 `data/cache/vision`）。

---

### Task 1: P0-02 规范化函数 + 写路径（core/schema/utils.py + upload_routes.py + test_upload.py）

**Files:**
- Modify: `core/schema/utils.py`（新增 `_ATTACHMENT_MIME_ALIASES` + `normalize_attachment_mime`）
- Modify: `core/input/upload_routes.py`（final_kind 确定后一行规范化；import 补 `normalize_attachment_mime`）
- Test: `core/input/tests/test_upload.py`（`TestPerFileKind` 扩展 mime 断言 + WAV 常量 + 2 新用例 + replay 一致用例）

**Interfaces:**
- Produces: `normalize_attachment_mime(kind: str, mime: str) -> str`（spec §4.2）——Task 2（ws_v2 读路径）、Task 3（attachment_routes 下载）消费。
- Consumes: `_MIME_KIND`/`_MIME_EXTS`/`_KIND_LIMITS`（既有，不动校验序）。

- [ ] **Step 1: Write the failing test**

在 `core/input/tests/test_upload.py` 模块级常量区（`PDF` 附近）新增 WAV 魔数（Elysia env 实测 `filetype.guess` → `audio/x-wav`）：

```python
WAV = b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00"  # audio/x-wav（filetype 探测）
```

`TestPerFileKind` 内修改既有 `test_webm_audio_video_disambiguated` 补 mime 断言（保留既有 setup 两行，L4），并新增 3 个用例 + `TestNormalizeAttachmentMime` 直测类（`from core.input.ws_v2 import _attachment_part` 与 `from core.schema.utils import normalize_attachment_mime` 加到文件顶部 import）：

```python
    async def test_webm_audio_video_disambiguated(self, make_app, aiohttp_client):
        """audio/webm + video/webm 同批分别归 audio/video（hint tiebreaker）+ mime 各归其位。"""
        agent, app = make_app()          # 既有 setup（L4：勿删）
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [
            ("audio", "voice.webm", WEBM), ("video", "clip.webm", WEBM),
        ])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        atts = (await resp.json())["attachments"]
        assert [a["kind"] for a in atts] == ["audio", "video"]
        assert [a["mime"] for a in atts] == ["audio/webm", "video/webm"]  # 新增：audio 不再 video/webm

    async def test_webm_audio_mime_normalized(self, make_app, aiohttp_client):
        """kind=audio + webm → 响应/记录 mime=audio/webm（BE-S3-RUN-P0-02）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.webm", WEBM)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "audio"
        assert att["mime"] == "audio/webm"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        assert record["mime"] == "audio/webm"

    async def test_wav_audio_mime_normalized(self, make_app, aiohttp_client):
        """WAV → 响应/记录 mime=audio/wav（探测 audio/x-wav → 权威 audio/wav）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.wav", WAV)])
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        att = (await resp.json())["attachments"][0]
        assert att["kind"] == "audio"
        assert att["mime"] == "audio/wav"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        assert record["mime"] == "audio/wav"

    async def test_new_record_replay_mime_consistent(self, make_app, aiohttp_client):
        """新上传 WAV → _attachment_part 恢复 mime 与上传响应 AttachmentInfo.mime 一致（spec §4.3 case 3）。"""
        agent, app = make_app()
        client = await aiohttp_client(app)
        resp = await _post_kinds(client, [("audio", "voice.wav", WAV)])
        assert resp.status == 200
        att = (await resp.json())["attachments"][0]
        assert att["mime"] == "audio/wav"
        record = agent._attachment_repo.records[0]  # noqa: SLF001
        part = _attachment_part("audio", att["attachmentId"], record)
        assert part.mime == att["mime"]  # 恢复 mime == 上传响应 mime


class TestNormalizeAttachmentMime:
    """规范化表直测（spec §4.2 冻结三行，M1——audio/x-wav 经写路径/读路径同源锁定）。"""

    def test_audio_video_webm_to_audio_webm(self):
        assert normalize_attachment_mime("audio", "video/webm") == "audio/webm"

    def test_audio_x_wav_to_audio_wav(self):
        assert normalize_attachment_mime("audio", "audio/x-wav") == "audio/wav"

    def test_video_webm_unchanged(self):
        assert normalize_attachment_mime("video", "video/webm") == "video/webm"

    def test_untable_alias_returns_as_is(self):
        assert normalize_attachment_mime("audio", "audio/mpeg") == "audio/mpeg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_upload.py -k "TestPerFileKind or TestNormalizeAttachmentMime" -v`
Expected: 既有 `test_webm_audio_video_disambiguated` 在新增 mime 断言处 FAIL（audio 得 `video/webm` ≠ `audio/webm`）；`test_webm_audio_mime_normalized`/`test_wav_audio_mime_normalized` FAIL（mime 为 `video/webm`/`audio/x-wav`）；`TestNormalizeAttachmentMime` 全部 ERROR（`ImportError: cannot import name 'normalize_attachment_mime'`——函数未实现，M1 直测红）。

- [ ] **Step 3: Write minimal implementation**

`core/schema/utils.py` 末尾新增（spec §4.2 冻结表）：

```python
# 附件 MIME 规范化表（复验清单 §4.2 冻结）：(kind, 探测/历史别名) → 契约权威值。
# 探测值（filetype 容器判定）可作校验内部值，但 wire/持久化/下载必须与 kind 一致。
_ATTACHMENT_MIME_ALIASES = {
    ("audio", "video/webm"): "audio/webm",   # kind=audio 消歧后的容器探测值
    ("audio", "audio/x-wav"): "audio/wav",   # filetype 探测别名 → AudioMime 权威值
}


def normalize_attachment_mime(kind: str, mime: str) -> str:
    """附件记录 MIME 规范化：kind 确定后，探测/历史别名 → 契约权威值。

    写路径（上传）与读路径（消息恢复 / 下载 Content-Type）共用，保证同一附件
    各视角 mime 一致；未知别名原样返回（调用方决定回退/告警）。

    Args:
        kind: 附件最终 kind（image/video/audio/file）。
        mime: 待规范化的 MIME（探测值或历史记录值）。

    Returns:
        规范化后的 MIME；表中无匹配别名时原样返回。
    """
    return _ATTACHMENT_MIME_ALIASES.get((kind, mime), mime)
```

`core/input/upload_routes.py`：
1. import 行 15 `from core.schema.utils import now_iso` → `from core.schema.utils import normalize_attachment_mime, now_iso`；
2. 阶段 A 校验内、`final_kind` 确定与归位后大小复核（`:264` `if len(data) > _KIND_LIMITS[final_kind]:` 之后、`filename` 脱敏之前）插入一行：

```python
        # BE-S3-RUN-P0-02：kind 确定后规范探测 MIME（audio+video/webm→audio/webm、
        # audio+x-wav→audio/wav），doc/响应/下载/恢复同值（spec §4.2）
        mime = normalize_attachment_mime(final_kind, mime)
```

规范化后的 `mime` 即进入后续 `doc["mime"]`（`:279`）、`validated["mime"]`（`:294`）、`AttachmentInfo.mime`（`:354`），四处天然一致。

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_upload.py -k "TestPerFileKind or TestNormalizeAttachmentMime" -v`
Expected: PASS（`TestPerFileKind` 既有 12 + 新增 3 = 15，`TestNormalizeAttachmentMime` 4，共 19 passed，L1 计数修订）
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_upload.py -q`
Expected: 全绿（既有单 kind/魔数/415/回滚用例不回归——校验序未动）

- [ ] **Step 5: Verify lint**

Run: `conda run -n Elysia ruff check core/schema/utils.py core/input/upload_routes.py core/input/tests/test_upload.py`
Expected: All passed

- [ ] **Step 6: Commit**

```bash
git add core/schema/utils.py core/input/upload_routes.py core/input/tests/test_upload.py
git commit -m "feat(upload): 音频 MIME 规范化写路径 + normalize_attachment_mime 单源（P0-02）"
```

---

### Task 2: P0-02 读路径（ws_v2._attachment_part 规范化 + 响亮 warning + 存量兼容）

**Files:**
- Modify: `core/input/ws_v2.py`（`_attachment_part` `:208-214` 读路径规范化 + `except: pass` → `logger.warning`；顶部 import 补 `normalize_attachment_mime`）
- Test: `core/input/tests/test_attachment_message.py`（新增 `TestAttachmentPartMimeNormalize` 类）

**Interfaces:**
- Consumes: `normalize_attachment_mime`（Task 1）
- Produces: 读路径规范化行为——存量 `kind=audio + mime=audio/x-wav`→`audio/wav`、`video/webm`→`audio/webm`；未知 mime 回退默认 + `logger.warning`。

- [ ] **Step 1: Write the failing test**

`core/input/tests/test_attachment_message.py` 顶部 import 补：

```python
from core.input.ws_v2 import _attachment_part
from core.schema.part import AudioPart
```

文件末尾新增（marker 沿用文件级 `service`；纯函数直测，不需 app）：

```python
class TestAttachmentPartMimeNormalize:
    """BE-S3-RUN-P0-02 读路径：存量记录 mime 规范化 + 未知 mime 响亮回退（spec §4.2/§4.3）。"""

    def test_legacy_wav_x_wav_normalized(self):
        """存量 kind=audio + mime=audio/x-wav → AudioPart.mime=audio/wav。"""
        part = _attachment_part("audio", "att_legacy_wav", {"kind": "audio", "mime": "audio/x-wav"})
        assert isinstance(part, AudioPart)
        assert part.mime == "audio/wav"

    def test_legacy_audio_video_webm_normalized(self):
        """存量 kind=audio + mime=video/webm → AudioPart.mime=audio/webm（不炸消息页）。"""
        part = _attachment_part("audio", "att_legacy_webm", {"kind": "audio", "mime": "video/webm"})
        assert isinstance(part, AudioPart)
        assert part.mime == "audio/webm"

    def test_unknown_mime_falls_back_with_warning(self, caplog):
        """未知 mime（规范化后仍非法）→ 回退默认 audio/wav + 可诊断 warning（不静默伪装）。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="core.input.ws_v2"):
            part = _attachment_part("audio", "att_weird", {"kind": "audio", "mime": "application/octet-stream"})
        assert isinstance(part, AudioPart)
        assert part.mime == "audio/wav"
        assert any("附件" in r.message and "mime" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_message.py -k TestAttachmentPartMimeNormalize -v`
Expected: `test_legacy_audio_video_webm_normalized` FAIL（`video/webm` 非法 → 静默回退默认 `audio/wav`，断言 `audio/webm` 不符）；`test_unknown_mime_falls_back_with_warning` FAIL（无 warning 日志，静默 `except: pass`）；`test_legacy_wav_x_wav_normalized` 改前即 PASS（`audio/x-wav` 经回退得 `audio/wav` 与断言相符——M1：本别名规范化已由 Task 1 `TestNormalizeAttachmentMime` 直测锁定，此用例为行为锁定而非红测）。

- [ ] **Step 3: Write minimal implementation**

`core/input/ws_v2.py`：
1. 顶部 import 补：`from core.schema.utils import normalize_attachment_mime`（找现有 `from core.schema.utils import ...` 行）；
2. `_attachment_part`（`:197-225`）`mime = doc.get("mime") or ""` 处改为先规范化，静默吞异常改 `logger.warning`：

```python
    data: dict = {"src": f"/api/attachments/{attachment_id}", "attachment_id": attachment_id}
    # BE-S3-RUN-P0-02：读路径规范化——存量 kind=audio+video/webm → audio/webm、
    # audio/x-wav → audio/wav（spec §4.2 约束 3）；规范化后仍非法（未知/脏存量）→
    # 响亮 warning 回退默认 mime，不静默伪装（spec §4.2 约束 4，不炸消息页）。
    mime = normalize_attachment_mime(kind, doc.get("mime") or "")
    if mime:
        try:
            cls(**{**data, "mime": mime})
            data["mime"] = mime
        except Exception:
            logger.warning(
                "附件 kind=%s attachment_id=%s mime=%r 非契约值，回退默认 mime",
                kind, attachment_id, mime)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_message.py -k TestAttachmentPartMimeNormalize -v`
Expected: PASS（3 passed）
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_attachment_message.py -q`
Expected: 全绿（既有 v2 附件消息/回滚/幂等用例——合法 mime 路径不变）

- [ ] **Step 5: Verify lint**

Run: `conda run -n Elysia ruff check core/input/ws_v2.py core/input/tests/test_attachment_message.py`
Expected: All passed

- [ ] **Step 6: Commit**

```bash
git add core/input/ws_v2.py core/input/tests/test_attachment_message.py
git commit -m "feat(attachment): 读路径 MIME 规范化 + 存量兼容 + 响亮 warning（P0-02）"
```

---

### Task 3: P0-02 下载路径 Content-Type 规范化（attachment_routes.py）

**Files:**
- Modify: `core/input/attachment_routes.py`（`:157-159` 下载 Content-Type 规范化；顶部 import 补 `normalize_attachment_mime`）
- Test: `core/input/tests/test_attachment_download.py`（`TestDownloadConfigured` 增 2 用例）

**Interfaces:**
- Consumes: `normalize_attachment_mime`（Task 1）
- Produces: 存量 `kind=audio + video/webm`/`audio/x-wav` 记录下载 `Content-Type` 分别为 `audio/webm`/`audio/wav`。

- [ ] **Step 1: Write the failing test**

`core/input/tests/test_attachment_download.py` 的 `TestDownloadConfigured` 类内新增（复用 `_seed`/`_fresh_sig`/`_url`；`_seed` 的 `content` 只需魔数近似字节，下载只校验文件存在 + 记录）：

```python
    @pytest.mark.asyncio
    async def test_download_legacy_audio_wav_normalized_content_type(self, make_app, aiohttp_client, tmp_path):
        """存量 kind=audio + mime=audio/x-wav → 下载 Content-Type=audio/wav（BE-S3-RUN-P0-02）。"""
        agent, app = make_app()
        await _seed(agent, tmp_path, kind="audio", stored_name="a.wav",
                    mime="audio/x-wav", content=b"RIFF\x24\x08\x00\x00WAVEfmt ")
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "audio/wav"

    @pytest.mark.asyncio
    async def test_download_legacy_audio_video_webm_normalized_content_type(self, make_app, aiohttp_client, tmp_path):
        """存量 kind=audio + mime=video/webm → 下载 Content-Type=audio/webm。"""
        agent, app = make_app()
        await _seed(agent, tmp_path, kind="audio", stored_name="a.webm",
                    mime="video/webm", content=b"\x1a\x45\xdf\xa3")
        client = await aiohttp_client(app)
        sig, exp = _fresh_sig()
        resp = await client.get(_url("att_1", sig, exp))
        assert resp.status == 200, f"expected 200, got {resp.status}: {await resp.text()}"
        assert resp.headers.get("Content-Type") == "audio/webm"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_download.py -k "legacy_audio" -v`
Expected: FAIL（`Content-Type` 为 `audio/x-wav`/`video/webm`，断言 `audio/wav`/`audio/webm` 不符）。

- [ ] **Step 3: Write minimal implementation**

`core/input/attachment_routes.py`：
1. 顶部 import 补：`from core.schema.utils import normalize_attachment_mime`（现有 `from core.schema.adapters import attachment_resolver` 附近）；
2. `_download_handler`（`:157-159`）：

```python
    kind = doc.get("kind", "")
    stored_name = doc.get("stored_name", "")
    # BE-S3-RUN-P0-02：下载 Content-Type 过规范化（存量 kind=audio+video/webm →
    # audio/webm、audio/x-wav → audio/wav；新记录已写路径规范化，幂等）（spec §4.2）
    mime = normalize_attachment_mime(kind, doc.get("mime", "application/octet-stream"))
```

（原 `kind = doc.get("kind", "")` 行保留位置不变；`mime` 取值改为规范化后。）

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/input/tests/test_attachment_download.py -k "legacy_audio" -v`
Expected: PASS（2 passed）
随后回归：`conda run -n Elysia python -m pytest core/input/tests/test_attachment_download.py -q`
Expected: 全绿（签名校验序/Range/206/签发用例——合法 mime 记录路径不变，签名/Range 链路不触碰）

- [ ] **Step 5: Verify lint**

Run: `conda run -n Elysia ruff check core/input/attachment_routes.py core/input/tests/test_attachment_download.py`
Expected: All passed

- [ ] **Step 6: Commit**

```bash
git add core/input/attachment_routes.py core/input/tests/test_attachment_download.py
git commit -m "feat(attachment): 下载 Content-Type 规范化（存量 audio 记录兼容，P0-02）"
```

---

### Task 4: P0-01 ToolContext 会话换绑（loop.py + test_tool_session_rebind.py）

**Files:**
- Modify: `core/cognitive/loop.py`（`process()` `:452` `set_session(target)` 之后、`trace_id` `:462-463` 之前插入换绑）
- Create: `core/cognitive/tests/test_tool_session_rebind.py`（marker=service，镜像 `test_tts_voice_injection.py::_build_loop` + 真实 ToolAdapter + stub 视觉后端）

**Interfaces:**
- Produces: `process()` 每轮把 `tool_context.services["session"]`→目标会话、`["_session_msg_count"]`→`len(target.messages)`（spec §4.1）。
- Consumes: `self._tool_adapter`（`_start_tool_context` 已注入，`configure` 后非 None）、`target`（本轮 resolve 结果）。

- [ ] **Step 1: Write the failing test**

新建 `core/cognitive/tests/test_tool_session_rebind.py`：

```python
"""BE-S3-RUN-P0-01：describe_image 必须读每轮目标会话的当前图片（marker=service）。

覆盖复验清单 §3.4 六用例（Agent/loop 级）：default 后新会话发图可取 1 图 / A·B 不串 /
v2 显式非 active / 图文交替不残留 / 失败后再发图绑定仍正确 / 多模态直看图分支不回归。

使用真实 DescribeImagePlugin + stub 视觉后端（LLMFactory.create patch，不碰真实 LLM）
+ 最小 CognitiveLoop（镜像 test_tts_voice_injection.py::_build_loop）。
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from Module.output.websocket import WebSocketChannel
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.generation_registry import GenerationRegistry
from core.cognitive.timing import TimingClock
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.schema.message import UserMessage
from core.schema.types import MessageId

pytestmark = pytest.mark.service

# 两张不同图片的 data URI（describe_image 断言按内容区分，防串会话）
IMG_A = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
IMG_B = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

_IMG_CONTENT_A = [{"type": "image", "src": IMG_A, "mime": "image/png"},
                  {"type": "text", "content": "这张图里有什么？"}]
_IMG_CONTENT_B = [{"type": "image", "src": IMG_B, "mime": "image/png"},
                  {"type": "text", "content": "这张图里是什么？"}]


class _FakeVisionBackend:
    """视觉后端替身：记录收到的 messages，返回固定描述（不碰真实 LLM）。"""

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.messages.append(messages)
        return "测试描述：图中是一只猫。"

    async def close(self) -> None:
        pass


class _ToolCallingLLM:
    """首个响应发 describe_image tool_call，后续响应返回纯文本（驱动 ReAct 工具轮）。"""

    def __init__(self, response: str = "好的，已查看图片。") -> None:
        import re
        self.response = response
        self.calls = 0
        self.model = "fake/test"  # CognitiveLoop 访问 self._llm.model（loop.py:504/615/689/723）
        self._tok = re.compile(r"([一-鿿]|[\w]+|[^\w\s])")

    async def chat(self, messages: list[dict], **kwargs) -> str:
        return self.response

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        pass

    async def stream_think(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"content": None, "tool_calls": [{
                "index": 0, "id": "call_describe",
                "function": {"name": "describe_image", "arguments": "{}"},
            }]}
            return
        for tok in self._tok.findall(self.response):
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None}


async def _build_loop(session_a: SessionMemory, session_b: SessionMemory,
                      tmp_path, monkeypatch):
    """最小 CognitiveLoop + 真实 ToolAdapter（describe_image + stub 视觉后端）。

    resolver：显式 conversationId → 该会话；None → 活动会话 A（v2 缺省语义）。
    monkeypatch 注入 stub 视觉后端 + 缓存目录重定向（防写真实 data/cache/vision）。
    注意：adapter.start() **不注入** startup session（H1/L3 定案）——pre-fix 时
    services 无 "session" 键 → describe_image 报「session 不可用」，用例先红；
    post-fix process() 换绑后 key 存在且指向目标会话 → 用例转绿。
    """
    from core.adapter.tool_adapter import ToolAdapter
    from core.tools.vision.plugin import DescribeImagePlugin

    vision = _FakeVisionBackend()
    llm = _ToolCallingLLM()
    ws = AsyncMock()
    output = OutputManager()
    output.add_channel(WebSocketChannel(ws, protocol_version=2))
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    registry = GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))

    async def resolver(conversation_id, protocol_version):
        if conversation_id == "A":
            return session_a
        if conversation_id == "B":
            return session_b
        return session_a  # v2 缺省 → 活动会话 A

    monkeypatch.setattr(
        "core.tools.vision.plugin.LLMFactory.create", lambda *a, **k: vision)
    monkeypatch.setattr(
        "core.tools.vision.plugin._CACHE_DIR", tmp_path / "vision_cache")
    adapter = ToolAdapter([DescribeImagePlugin], session_id="default")
    await adapter.start()

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        generation_registry=registry,
    )
    loop.configure(tool_adapter=adapter)
    return loop, ws, adapter, vision


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]  # noqa: SLF001
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _first_image_url(vision: _FakeVisionBackend) -> str | None:
    """从视觉后端收到的首条消息里取 image_url.url（无图返回 None）。"""
    if not vision.messages:
        return None
    content = vision.messages[0][0]["content"]
    for c in content:
        if c.get("type") == "image_url":
            return c["image_url"]["url"]
    return None


class TestToolSessionRebind:
    @pytest.mark.asyncio
    async def test_image_in_new_session_visible_to_tool(self, tmp_path, monkeypatch):
        """default 启动后新建会话 A 发图 → describe_image 取得当前用户消息的 1 张图。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")  # 启动会话（模拟 default）
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("这张图里有什么？", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_A, "describe_image 未读到 A 会话当前图片"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_session_switch_no_cross_talk(self, tmp_path, monkeypatch):
        """连续在 A、B 各发不同图：B 的工具只看到 B 的图，不读到 A。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("A图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            vision.messages.clear()
            await loop.process("B图", content=_IMG_CONTENT_B,
                               conversation_id="B", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_B, "B 的 turn 读到 A 的图（串会话）"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_explicit_non_active_session(self, tmp_path, monkeypatch):
        """v2 显式 conversationId 指向非活动会话 B → 工具读取显式目标 B。"""
        session_a = SessionMemory(session_id="A")  # 活动会话
        session_b = SessionMemory(session_id="B")  # 显式非活动
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("B图", content=_IMG_CONTENT_B,
                               conversation_id="B", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_B, "显式 B 未读 B 的图"
            # 换绑服务值也应指向 B（不残留 A）
            assert adapter.tool_context.services["session"] is session_b
            # spec §4.1 要点 2 记录的 off-by-one：换绑先于用户消息入队，快照=turn 前计数。
            # 全新 B 会话 turn 前计数为 0（process 后 session_b.messages 已有 4 条，不相等）。
            assert adapter.tool_context.services["_session_msg_count"] == 0
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_text_turn_no_residual_image(self, tmp_path, monkeypatch):
        """图 turn 后接纯文本 turn → describe_image 0 图，不残留上一轮图片。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            await loop.process("看图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert vision.messages  # 首轮确实读了图
            vision.messages.clear()
            # 纯文本 turn：LLM 仍发 describe_image tool_call，但当前用户消息无图
            await loop.process("你好", conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert vision.messages == [], "纯文本 turn 仍看到上一轮图片（残留）"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_failed_turn_then_retry_binding_correct(self, tmp_path, monkeypatch):
        """generation 失败后再发图 → session 绑定仍正确。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            # 第一轮：LLM 首轮抛错 → generation 失败（不产生工具轮）
            async def _boom_stream(messages, tools=None, **kwargs):
                if False:
                    yield None  # L2：async generator（async def 直接 raise 只返回 coroutine，非可迭代）
                raise ConnectionError("fake 断连")
            loop._llm.stream_think = _boom_stream  # noqa: SLF001
            await loop.process("失败图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            # 第二轮：恢复 LLM → describe_image 仍读到 A 的图
            loop._llm = _ToolCallingLLM()  # noqa: SLF001 — 换回正常 LLM（重试 turn）
            await loop.process("重试图", content=_IMG_CONTENT_A,
                               conversation_id="A", protocol_version=2)
            await _await_pending(loop)
            assert _first_image_url(vision) == IMG_A, "失败后再发图 session 绑定不正确"
        finally:
            await loop._output.close()

    @pytest.mark.asyncio
    async def test_multimodal_direct_view_keeps_describe_image_excluded(self, tmp_path, monkeypatch):
        """多模态模型直看图分支：describe_image 从 tool_defs 排除（既有行为不回归）。"""
        from Module.llm.registry import LLMFactory
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        loop, ws, adapter, vision = await _build_loop(session_a, session_b, tmp_path, monkeypatch)
        try:
            loop._clock = TimingClock()  # noqa: SLF001
            loop._session = session_a  # noqa: SLF001 — _build_turn_prompt 直测需 _session（loop.py:627 add_message）
            monkeypatch.setattr(LLMFactory, "supports_multimodal",
                                classmethod(lambda cls, m: True))
            _, tool_defs = await loop._build_turn_prompt("你好", "u", None, "", None, False)
            assert "describe_image" not in [t["function"]["name"] for t in tool_defs]
            monkeypatch.setattr(LLMFactory, "supports_multimodal",
                                classmethod(lambda cls, m: False))
            _, tool_defs2 = await loop._build_turn_prompt("你好", "u", None, "", None, False)
            assert "describe_image" in [t["function"]["name"] for t in tool_defs2]
        finally:
            await loop._output.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_tool_session_rebind.py -v`
Expected: 前 5 个用例 FAIL——services 无 `"session"` 键（`adapter.start()` 不注入，L3）→ describe_image 报「session 不可用」；test 3 的 `services["session"]` 断言 KeyError；test 4 在其首个 `assert vision.messages` 处 FAIL；`test_multimodal_...` 应 PASS（既有行为，回归锁）。

- [ ] **Step 3: Write minimal implementation**

`core/cognitive/loop.py` `process()` 内、`self._prompt.set_session(target)`（`:452`）之后、`trace_id` 更新（`:462-463`）之前插入（spec §4.1）：

```python
            # BE-S3-RUN-P0-01（复验清单 §3）：ToolContext 会话换绑——describe_image/self_state
            # 经 ctx.services["session"]/["_session_msg_count"] 读会话，启动注入后从不更新；
            # 每轮目标会话解析后必须原子换绑（clone_for_call 浅拷贝 services，本 turn 内
            # 所有工具调用副本立即看到新 session；失败/取消/切换会话由下一轮换绑覆盖，不残留）。
            if self._tool_adapter is not None:
                svc = self._tool_adapter.tool_context.services
                svc["session"] = target
                svc["_session_msg_count"] = len(target.messages)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `conda run -n Elysia python -m pytest core/cognitive/tests/test_tool_session_rebind.py -v`
Expected: PASS（6 passed）
随后回归：`conda run -n Elysia python -m pytest core/cognitive/tests/ -q`
Expected: 全绿（`test_tts_voice_injection.py`/`test_tool_image_parts.py` 等既有 loop 测试不回归——换绑不改 prompt/character_engine/session 自身行为）。注：L6——本测试与 `test_tts_voice_injection.py` 同款，`SessionMemory.save()` 可能写 `data/session_{A,B}.json` 待持久化文件，属既有测试模式已知轻微污染，可接受不处理。

- [ ] **Step 5: Verify lint**

Run: `conda run -n Elysia ruff check core/cognitive/loop.py core/cognitive/tests/test_tool_session_rebind.py`
Expected: All passed

- [ ] **Step 6: Commit**

```bash
git add core/cognitive/loop.py core/cognitive/tests/test_tool_session_rebind.py
git commit -m "feat(cognitive): ToolContext 会话每轮换绑（describe_image 读目标会话，P0-01）"
```

---

### Task 5: 协议文档更新 + 全量门禁验证（收尾）

**Files:**
- Modify: `docs/electron-backend-protocol-v2.md`（§13.5 `:517` audio 行 + `:520` webm tiebreaker 注释）

**Interfaces:**
- Consumes: 全部前序任务产物（spec D6 文档同步）。

- [ ] **Step 1: 更新协议文档 §13.5**

`docs/electron-backend-protocol-v2.md`：

1. `:517` audio 行（`| \`audio\` | audio/x-wav / audio/mpeg / audio/ogg | wav / mp3 / ogg | 20MB |`）改为：

```markdown
| `audio` | audio/wav / audio/mpeg / audio/ogg / audio/webm | wav / mp3 / ogg / webm | 20MB |
```

2. `:520` webm tiebreaker 注释改为（补规范化口径）：

```markdown
- **webm 歧义**：mime=video/webm 时以客户端 kind 提示作 tiebreaker（kind=audio → kind=audio + **mime 规范化为 audio/webm**，其余 → video/webm），归位后按最终 kind 复核上限。
- **MIME 规范化**：`kind` 确定后，探测/历史别名收敛到契约权威值——`kind=audio + video/webm → audio/webm`、`kind=audio + audio/x-wav → audio/wav`（`core/schema/utils.py::normalize_attachment_mime` 单源，写/读/下载三路径同值）；`audio/x-wav` 仅作探测/历史别名，不对外输出。
```

- [ ] **Step 2: 全量快速测试（unit + service）**

Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short`
Expected: 全绿（新增 6+3+3+2 用例 + 既有全部回归；`make test-quick` 等价）

- [ ] **Step 3: ruff 全量**

Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`
Expected: All passed

- [ ] **Step 4: pyright 全量（Elysia env）**

Run: `conda run -n Elysia pyright`
Expected: 0 errors（`make pyright` 等价；base env 会漏报，须 Elysia）

- [ ] **Step 5: 契约门禁复跑**

Run: `make check-types`
Expected: 全绿——契约产物（ts/schema/raw_ts + golden frames）无漂移（本批零契约形状变更）

- [ ] **Step 6: 验收 grep（spec §7.5 M1 修订版）**

Run: `grep -rn "audio/x-wav" docs/electron-backend-protocol-v2.md`
Expected: 无输出（文档冻结表不再出现探测别名）
Run: `grep -rn "audio/x-wav" core/`
Expected: 仅命中 `upload_routes.py`（探测表）与 `core/schema/utils.py`（规范化别名表）两处
Run: `grep -rn "pass  # mime 非枚举值" core/input/ws_v2.py`
Expected: 无输出（原静默吞异常注释已随 `logger.warning` 替换移除——L5：`except Exception:` 行本身保留，验收的是静默 `pass` 消失，改 grep 锚定注释）

- [ ] **Step 7: Commit**

```bash
git add docs/electron-backend-protocol-v2.md
git commit -m "docs: 协议 §13.5 音频 MIME 规范值 + webm tiebreaker 规范化口径（P0-02）"
```

Task 5 其余步骤不产生 commit（纯验证；批次 Phase 4-5 跑 `make test-normal` 全量再确认）。

---

## Self-Review

- **Spec 覆盖**：P0-01（换绑机制 + 六用例）→ Task 4；P0-02 写路径 → Task 1；读路径（存量兼容 + 响亮 warning + replay 一致）→ Task 2；下载路径 → Task 3；文档同步 → Task 5；全量门禁 → Task 5；`audio/x-wav` 仅两处白名单 grep → Task 5 Step 6。无缺口。
- **占位符**：无 TBD/TODO；所有步骤含具体代码与预期。`_build_loop`/`_FakeVisionBackend`/`_ToolCallingLLM` 均给出完整实现。
- **类型一致性**：`normalize_attachment_mime(kind: str, mime: str) -> str`（Task 1）被 Task 2/3 消费；`tool_context.services` dict 键 `"session"`/`"_session_msg_count"` 在 Task 4 与 `_start_tool_context`（`_lifecycle_tools.py:71/76`）一致；`_attachment_part(kind, attachment_id, doc)` 签名 Task 2 直测与原调用一致；`_build_turn_prompt` 返回 `(messages, tool_defs)` 与 `loop.py:617-642` 一致。

## Plan Review 记录（2026-08-15）

- code-review 第 1 轮（general-purpose agent，逐行核对 + Elysia env 实测复现）：**总体结论 no**。
  生产修复设计（换绑机制端到端复现有效、规范化表/魔数/三插入点）全部核对通过；测试代码
  4 HIGH + 1 MED + 6 LOW 全部消化——**HIGH-1** `_build_loop` 签名缺 `vision` 位置实参 →
  改为函数内构造 `_FakeVisionBackend()`（测试调用 `(session_a, session_b, tmp_path, monkeypatch)`
  与签名一致）；**HIGH-2** `_ToolCallingLLM` 用 `_model` 而 CognitiveLoop 访问 `self._llm.model`
  （loop.py:504/615/689/723 无条件读）→ 改公开 `self.model`；**HIGH-3** test 3 `_session_msg_count
  == len(session_b.messages)` 与 spec 记录的 off-by-one 矛盾（实测 turn 后 0 vs 4）→ 断言快照值
  `== 0` + 注释引用 spec §4.1 要点 2；**HIGH-4** test 6 三错（元组解包反转 `(messages, tool_defs)`、
  `_build_turn_prompt` 直测缺 `loop._session` 触发 loop.py:627 add_message AttributeError、
  `.model` 缺失）→ 解包改 `_, tool_defs = ...`、直测前设 `loop._session = session_a`、H2 已修。
  **MED-1** `test_legacy_wav_x_wav_normalized` 非红测（`audio/x-wav` 经回退得 `audio/wav` 与断言
  相符，实测改前 PASS）→ Task 1 补 `TestNormalizeAttachmentMime` 直测表 + Task 2 Step 2 修订该用例
  为行为锁定。**LOW-1** TestPerFileKind 计数 11→12（实为 12 既有）→ Step 4 计 15+4=19；**LOW-2**
  `_boom_stream` 直接 raise 返回 coroutine 非 async generator → 加 `if False: yield None` 转生成器；
  **LOW-3** Step 2 失败推理（services 实为**无** "session" 键而非 stale 引用，adapter.start 不注入）
  → 修订预期为 KeyError/「session 不可用」；**LOW-4** 既有 disambiguated 测试片段缺 setup 行 →
  补全为完整测试体；**LOW-5** grep `except Exception:` 会命中新 block（保留 except 仅改 body）→
  改 grep 锚定 `pass  # mime 非枚举值` 消失；**LOW-6** 测试写 `data/session_{A,B}.json` 待持久化
  文件 → Step 4 标注既有测试模式已知污染、不处理。
- **总体结论（agent）**：no 阻断已全部消化——Task 4 六个用例改为可运行（修复后转绿）、
  Task 2 红测/行为锁界定清晰、全部 TDD 循环（先红后绿）成立。
