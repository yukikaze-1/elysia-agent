# LLM Prompt 查看器 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Debug 页面新增 LLM Prompt 查看器，捕获每次 LLM 调用的完整 messages 数组，支持与 Jinja2 模板源码左右对照查看。

**Architecture:** 新增 `core/prompt_recorder.py` 作为内存环形缓冲区，在 `CognitiveLoop._think_loop()` 中每次 `build_messages()` 返回后捕获 prompt。通过 `/debug/api/prompts` 和 `/debug/api/prompts/{index}` 两个 REST 端点按需加载，Debug 页面新增卡片 + 详情面板。

**Tech Stack:** Python 3.13 (dataclasses + deque), aiohttp (REST endpoints), vanilla JS + CSS (debug.html)

**Spec:** `docs/superpowers/specs/2026-06-06-llm-prompt-viewer-design.md`

---

### Task 1: PromptRecorder 核心组件

**Files:**
- Create: `core/prompt_recorder.py`
- Create: `core/test_prompt_recorder.py`

- [ ] **Step 1: 编写失败测试**

```python
# core/test_prompt_recorder.py
"""PromptRecorder 单元测试"""
import pytest
from datetime import datetime
from core.prompt_recorder import PromptRecorder, PromptRecord


class TestPromptRecord:
    def test_record_derived_fields_simple_text(self):
        """纯文本 messages 的派生字段计算"""
        record = PromptRecord(
            index=1,
            timestamp="2026-06-06T15:30:00",
            round_num=0,
            model="deepseek/deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            tools=None,
        )
        # 派生字段由 record() 方法计算，这里验证直接构造后的计算逻辑
        from core.prompt_recorder import _compute_derived_fields
        _compute_derived_fields(record)
        assert record.message_count == 2
        assert record.system_prompt_len == 31  # len("You are a helpful assistant.")
        assert record.total_chars == 31 + 5  # system + "Hello"

    def test_record_derived_fields_multimodal_content(self):
        """多模态 content（list[dict]）的字符数计算"""
        record = PromptRecord(
            index=2,
            timestamp="2026-06-06T15:30:01",
            round_num=1,
            model="deepseek/deepseek-chat",
            messages=[
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                    {"type": "text", "text": "描述这张图"},
                ]},
            ],
            tools=None,
        )
        from core.prompt_recorder import _compute_derived_fields
        _compute_derived_fields(record)
        assert record.message_count == 2
        assert record.system_prompt_len == 13  # len("System prompt")
        # total_chars = system(13) + user text(5)
        assert record.total_chars == 18


class TestPromptRecorder:
    def test_record_and_retrieve(self):
        """记录一条 prompt 后可通过 index 检索"""
        recorder = PromptRecorder(max_records=20)
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        recorder.record(messages=messages, tools=None, round_num=0, model="test/model")

        summaries = recorder.list_summaries()
        assert summaries["total"] == 1
        assert summaries["max"] == 20
        assert len(summaries["records"]) == 1
        s = summaries["records"][0]
        assert s["index"] == 1
        assert s["message_count"] == 2
        assert s["round_num"] == 0
        assert s["model"] == "test/model"
        # summaries 不应包含完整 messages
        assert "messages" not in s

        full = recorder.get_full(1)
        assert full is not None
        assert full.index == 1
        assert len(full.messages) == 2

    def test_get_full_returns_none_for_missing_index(self):
        """查询不存在的 index 返回 None"""
        recorder = PromptRecorder(max_records=20)
        assert recorder.get_full(999) is None

    def test_ring_buffer_eviction(self):
        """超过 max_records 时旧记录被淘汰"""
        recorder = PromptRecorder(max_records=3)
        for i in range(5):
            recorder.record(
                messages=[{"role": "user", "content": f"msg{i}"}],
                tools=None, round_num=0, model="test/model",
            )

        summaries = recorder.list_summaries()
        assert summaries["total"] == 3
        # 最旧的 index=1,2 被淘汰，保留 index=3,4,5
        indices = [r["index"] for r in summaries["records"]]
        assert indices == [3, 4, 5]
        assert recorder.get_full(1) is None
        assert recorder.get_full(5) is not None

    def test_list_summaries_empty(self):
        """无记录时返回空列表"""
        recorder = PromptRecorder(max_records=20)
        summaries = recorder.list_summaries()
        assert summaries == {"records": [], "total": 0, "max": 20}

    def test_index_monotonic(self):
        """index 全局自增，不受淘汰影响"""
        recorder = PromptRecorder(max_records=2)
        recorder.record(messages=[], tools=None, round_num=0, model="a")
        recorder.record(messages=[], tools=None, round_num=0, model="b")
        recorder.record(messages=[], tools=None, round_num=0, model="c")
        assert recorder.get_full(1) is None
        assert recorder.get_full(3) is not None
        assert recorder._counter == 3
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest core/test_prompt_recorder.py -v
```

预期：所有测试 FAIL（模块不存在）

- [ ] **Step 3: 实现 PromptRecorder**

```python
# core/prompt_recorder.py
"""LLM Prompt 记录器 — 内存环形缓冲区，捕获每次 LLM 调用的完整 messages 数组。"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PromptRecord:
    """一次 LLM 调用的完整 prompt 记录。"""
    index: int
    timestamp: str
    round_num: int
    model: str
    messages: list[dict]
    tools: list[dict] | None = None
    message_count: int = 0
    system_prompt_len: int = 0
    total_chars: int = 0


def _content_len(content) -> int:
    """计算单条消息 content 的字符数，兼容 str 和 list[dict] 格式。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(part.get("content", "") or "")
                elif part.get("type") == "image_url":
                    total += 20  # 图片粗略计数
        return total
    return 0


def _compute_derived_fields(record: PromptRecord) -> None:
    """计算并填充派生字段（message_count, system_prompt_len, total_chars）。"""
    record.message_count = len(record.messages)
    record.total_chars = 0
    record.system_prompt_len = 0
    for msg in record.messages:
        clen = _content_len(msg.get("content", ""))
        record.total_chars += clen
        if msg.get("role") == "system":
            record.system_prompt_len = clen


class PromptRecorder:
    """内存环形缓冲区，保存最近 N 次 LLM 调用的完整 prompt。

    用法:
        recorder = PromptRecorder(max_records=20)
        recorder.record(messages=messages, tools=tool_defs, round_num=0, model="deepseek/deepseek-chat")
        summaries = recorder.list_summaries()  # 轻量摘要
        full = recorder.get_full(index=5)      # 完整记录
    """

    def __init__(self, max_records: int = 20):
        self._records: deque[PromptRecord] = deque(maxlen=max_records)
        self._counter: int = 0
        self._max_records = max_records

    def record(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        round_num: int,
        model: str,
    ) -> PromptRecord:
        """保存一次 LLM 调用的 prompt。"""
        self._counter += 1
        record = PromptRecord(
            index=self._counter,
            timestamp=datetime.now(timezone.utc).isoformat(),
            round_num=round_num,
            model=model,
            messages=messages,
            tools=tools,
        )
        _compute_derived_fields(record)
        self._records.append(record)
        return record

    def list_summaries(self) -> dict:
        """返回轻量摘要列表，供 DebugInfo 使用。不包含完整 messages。"""
        records = []
        for r in self._records:
            records.append({
                "index": r.index,
                "timestamp": r.timestamp,
                "round_num": r.round_num,
                "message_count": r.message_count,
                "system_prompt_len": r.system_prompt_len,
                "total_chars": r.total_chars,
                "model": r.model,
            })
        return {
            "records": records,
            "total": len(records),
            "max": self._max_records,
        }

    def get_full(self, index: int) -> PromptRecord | None:
        """按序号获取完整记录。"""
        for r in self._records:
            if r.index == index:
                return r
        return None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest core/test_prompt_recorder.py -v
```

预期：5 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add core/prompt_recorder.py core/test_prompt_recorder.py
git commit -m "feat: PromptRecorder 核心组件 — 内存环形缓冲区"
```

---

### Task 2: CognitiveLoop 集成

**Files:**
- Modify: `core/cognitive_loop.py`

- [ ] **Step 1: 在 `__init__` 中增加 `prompt_recorder` 参数**

找到 `__init__` 方法（约 L178），在 `token_tracker` 参数后面加：

```python
# 在 token_tracker=None 后面添加
prompt_recorder=None,  # PromptRecorder | None
```

在 `__init__` 方法体中 `self._token_tracker = token_tracker` 后面加：

```python
self._prompt_recorder = prompt_recorder
```

- [ ] **Step 2: 在 `_think_loop()` 中 `build_messages()` 返回后捕获 prompt**

找到 `build_messages()` 调用块（约 L318-325），在 `messages` 赋值后、session add 之前插入捕获代码。完整上下文：

```python
with self._clock.span("prompt") as t:
    supports_mm = LLMFactory.supports_multimodal(self._llm.model)
    messages = await self._prompt.build_messages(
        user_message=user_message, user_name=user_name,
        stt_emotion=stt_emotion, stt_events=stt_events,
        content=content,
        supports_multimodal=supports_mm,
    )

# 新增：捕获第一轮的完整 prompt
if self._prompt_recorder is not None:
    self._prompt_recorder.record(
        messages=list(messages),  # 浅拷贝，防止后续工具循环修改
        tools=tool_defs,
        round_num=0,
        model=self._llm.model,
    )
```

注意：`round_num=0` 表示第一轮（工具循环从 0 开始）。后续轮次在工具循环内捕获。

- [ ] **Step 3: 在工具循环内捕获后续轮次的 prompt**

找到工具循环体（约 L347），在每轮 `stream_think()` 调用行（约 L355）**之前**加捕获。工具循环内 messages 会在每轮追加 assistant(tool_calls) + tool(result)，所以每轮的 messages 不同。

找到类似以下位置（在 `stream_think` 调用之前，约 L347-356 附近）：

```python
for round_num in range(self.MAX_TOOL_ROUNDS + 1):
```

在这个 `for` 循环体内部，`stream_think(messages, ...)` 调用之前，插入：

```python
# 工具循环的后续轮次也捕获 prompt
if round_num > 0 and self._prompt_recorder is not None:
    self._prompt_recorder.record(
        messages=list(messages),
        tools=tool_defs,
        round_num=round_num,
        model=self._llm.model,
    )
```

- [ ] **Step 4: 运行现有测试确认无回归**

```bash
python -m pytest core/ -q
```

预期：所有已有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add core/cognitive_loop.py
git commit -m "feat: CognitiveLoop 集成 PromptRecorder 捕获"
```

---

### Task 3: Agent 初始化 PromptRecorder

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: 在 `start()` 中创建并注入 PromptRecorder**

找到 `start()` 方法中 TokenTracker 的创建代码（约 L165-174），在旁边加 PromptRecorder 初始化：

```python
# ---- Token 统计 ----
from core.token_tracker import TokenTracker
self._token_tracker = TokenTracker(
    session_id=getattr(self._session, "_session_id", "default"),
    output_manager=self._output,
    document_db=self._session._db if mongo_ok else None,
)
self._cognitive._token_tracker = self._token_tracker

# ---- Prompt 记录 ----
from core.prompt_recorder import PromptRecorder
self._prompt_recorder = PromptRecorder(max_records=20)
self._cognitive._prompt_recorder = self._prompt_recorder
```

注意：`self._cognitive._prompt_recorder` 这个属性在 `CognitiveLoop.__init__` 中已经初始化为 `None`（Task 2 加的），这里覆盖为实际实例。

- [ ] **Step 2: 运行测试确认无回归**

```bash
python -m pytest core/ -q
```

预期：所有已有测试 PASS

- [ ] **Step 3: 提交**

```bash
git add core/agent.py
git commit -m "feat: Agent.start() 初始化 PromptRecorder"
```

---

### Task 4: DebugInfo 扩展

**Files:**
- Modify: `core/debug_info.py`

- [ ] **Step 1: 在 DebugInfo dataclass 中新增 `prompt_summary` 字段**

在 `DebugInfo` 类中，Token 统计字段附近（约 L52）添加：

```python
# Prompt 记录
prompt_summary: dict = field(default_factory=dict)
```

- [ ] **Step 2: 在 `collect()` 中填充 `prompt_summary`**

在 `collect()` 函数中，Token 统计块附近（约 L179）添加：

```python
# ---- Prompt 记录 ----
recorder = getattr(agent, '_prompt_recorder', None)
if recorder is not None:
    info.prompt_summary = recorder.list_summaries()
```

- [ ] **Step 3: 运行测试确认无回归**

```bash
python -m pytest core/ -q
```

预期：所有已有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add core/debug_info.py
git commit -m "feat: DebugInfo 新增 prompt_summary 字段"
```

---

### Task 5: REST API 端点

**Files:**
- Modify: `core/input/server.py`

- [ ] **Step 1: 新增 `prompts_list_handler` 和 `prompt_detail_handler`**

在 `server.py` 中 `sessions_clear_handler` 附近添加两个新的 handler 函数：

```python
async def prompts_list_handler(request: web.Request) -> web.Response:
    """返回最近 N 次 LLM 调用的 prompt 摘要列表。"""
    agent = request.app["agent"]
    recorder = getattr(agent, "_prompt_recorder", None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())


async def prompt_detail_handler(request: web.Request) -> web.Response:
    """返回指定记录的完整 prompt 内容 + 模板源码。"""
    index = int(request.match_info["index"])
    agent = request.app["agent"]
    recorder = getattr(agent, "_prompt_recorder", None)
    if recorder is None:
        raise web.HTTPNotFound(text="PromptRecorder 未初始化")
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound(text=f"记录 #{index} 不存在")

    # 读取模板源码
    template_source = None
    template_path = "prompt/Brain.j2"
    try:
        with open(template_path) as f:
            template_source = {"path": template_path, "content": f.read()}
    except Exception:
        pass

    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "model": record.model,
        "messages": record.messages,
        "tools": record.tools,
        "template_source": template_source,
    })
```

- [ ] **Step 2: 注册路由**

在 `create_app()` 函数中，其他 `/debug/api/` 路由附近（约 L340 附近）添加：

```python
app.router.add_get("/debug/api/prompts", prompts_list_handler)
app.router.add_get("/debug/api/prompts/{index:\\d+}", prompt_detail_handler)
```

注意：使用 `\\d+` 双反斜杠，因为这是在 Python 字符串中写正则。

- [ ] **Step 3: 运行测试确认无回归**

```bash
python -m pytest Module/ -q
```

预期：所有已有测试 PASS

- [ ] **Step 4: 提交**

```bash
git add core/input/server.py
git commit -m "feat: 新增 /debug/api/prompts 和 /debug/api/prompts/{index} 端点"
```

---

### Task 6: Debug 页面 UI

**Files:**
- Modify: `static/debug.html`

- [ ] **Step 1: 在 smart diff 中增加 prompt_summary 比较**

找到 `render()` 函数中的 diff 逻辑（约 L280-292），在比较条件中添加 `prompt_summary`：

```javascript
if (lastInfo) {
    const same = (
        lastInfo.fsm_state === info.fsm_state &&
        lastInfo.processing === info.processing &&
        lastInfo.session_messages === info.session_messages &&
        lastInfo.lt_count === info.lt_count &&
        lastInfo.ws_clients === info.ws_clients &&
        JSON.stringify(lastInfo.currently_executing) === JSON.stringify(info.currently_executing) &&
        JSON.stringify(lastInfo.timing_stats) === JSON.stringify(info.timing_stats) &&
        JSON.stringify(lastInfo.prompt_summary) === JSON.stringify(info.prompt_summary)
    );
    if (same) return;
}
```

- [ ] **Step 2: 在 cards 数组中新增「LLM Prompt」摘要卡片**

找到 `cards` 数组（约 L294），在 Row 2 末尾（Token 卡片之后）添加新卡片。将 Row 2 从 4 列调整为 5 列（或新建 Row）：

```javascript
// 在 Token 卡片（约 L366）的 })} 后面添加：
, { title:'📝 LLM Prompt', wide:false, items: (() => {
    const ps = info.prompt_summary || {};
    const records = ps.records || [];
    if (!records.length) return [];
    // 最近 3 条摘要
    const recent = records.slice(-3);
    return recent.map(r => {
        const time = r.timestamp ? r.timestamp.split('T')[1].split('.')[0] : '--:--:--';
        const round = r.round_num !== undefined ? `R${r.round_num + 1}` : '';
        const chars = r.total_chars ? `${r.total_chars.toLocaleString()}字` : '';
        const shortModel = (r.model || '').split('/').pop();
        return `<div class="prompt-item" style="display:flex;justify-content:space-between;align-items:center;padding:2px 0;">
            <span><span class="role-tag">#${r.index}</span> ${round} ${r.message_count}msg ${chars}</span>
            <span style="color:var(--muted);font-size:11px;">${time} · ${shortModel}</span>
            <button onclick="viewPrompt(${r.index})" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:2px 6px;font-size:11px;">查看</button>
        </div>`;
    }).concat([
        records.length > 3
            ? `<div class="prompt-view-all" style="text-align:center;padding:4px 0;color:var(--accent);cursor:pointer;" onclick="viewPromptList()">查看全部 (${records.length}条) ▼</div>`
            : ''
    ]);
})()},
```

- [ ] **Step 3: 新增 `viewPrompt()` 函数 — 展开详情面板**

在 `render()` 函数之后添加详情面板的 HTML 和 JS 逻辑。

先在 HTML 中找到 `#confirm-modal` 后面的位置（约 L221），添加详情面板的 HTML：

```html
<!-- LLM Prompt 详情面板 -->
<div id="prompt-overlay" class="modal-overlay" onclick="if(event.target===this)closePrompt()">
    <div id="prompt-panel" style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;max-width:95vw;width:1200px;max-height:85vh;overflow-y:auto;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px;">
            <h3 style="margin:0;color:var(--accent);" id="prompt-title">📝 Prompt #-</h3>
            <div>
                <button onclick="copyPromptJSON()" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:4px 10px;font-size:12px;margin-right:6px;">📋 复制 JSON</button>
                <button onclick="navigatePrompt(-1)" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:4px 8px;font-size:12px;">◀</button>
                <button onclick="navigatePrompt(1)" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:4px 8px;font-size:12px;margin-right:6px;">▶</button>
                <button onclick="closePrompt()" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:4px 10px;font-size:12px;">关闭</button>
            </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;" id="prompt-content">
            <!-- 左栏: messages | 右栏: template source -->
        </div>
        <div id="prompt-tools" style="margin-top:12px;"></div>
    </div>
</div>
```

然后添加 JS 函数（放在 `render()` 函数之后，约 L448 之后）：

```javascript
let currentPromptIndex = null;
let promptCache = {}; // 缓存已加载的 prompt 详情

async function viewPrompt(index) {
    currentPromptIndex = index;
    await loadPrompt(index);
    document.getElementById('prompt-overlay').classList.add('show');
}

function closePrompt() {
    document.getElementById('prompt-overlay').classList.remove('show');
}

async function loadPrompt(index) {
    // 先用缓存
    let data = promptCache[index];
    if (!data) {
        try {
            const resp = await fetch('/debug/api/prompts/' + index);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            data = await resp.json();
            promptCache[index] = data;
        } catch(e) {
            document.getElementById('prompt-content').innerHTML = '<div class="empty">加载失败: ' + e.message + '</div>';
            return;
        }
    }

    // 渲染标题
    const time = data.timestamp ? data.timestamp.split('T')[1].split('.')[0] : '';
    document.getElementById('prompt-title').textContent =
        `📝 Prompt #${data.index}  第 ${data.round_num + 1} 轮  ${data.model}  ${time}`;

    // 渲染左栏 messages
    let leftHtml = '<div style="overflow-y:auto;max-height:65vh;">';
    const msgs = data.messages || [];
    for (let i = 0; i < msgs.length; i++) {
        const m = msgs[i];
        const role = m.role || '?';
        let content = m.content || '';
        if (Array.isArray(content)) {
            content = content.map(p => {
                if (p.type === 'text') return p.content || p.text || '';
                if (p.type === 'image_url') return '[图片]';
                return JSON.stringify(p);
            }).join('');
        }
        const len = content.length;
        const roleClass = role === 'system' ? 'role-tag fail' : (role === 'user' ? 'role-tag user' : 'role-tag assistant');
        const isSystem = role === 'system';
        const msgId = 'msg-' + data.index + '-' + i;
        const preview = content.length > 200 ? content.slice(0, 200) + '…' : content;
        leftHtml += '<div style="margin-bottom:6px;border:1px solid var(--border);border-radius:4px;overflow:hidden;">';
        leftHtml += '<div onclick="toggleMsg(\'' + msgId + '\')" style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;background:rgba(255,255,255,0.03);cursor:pointer;">';
        leftHtml += '<span><span class="' + roleClass + '">' + role + '</span> (' + len.toLocaleString() + ' 字)</span>';
        leftHtml += '<span style="color:var(--muted);font-size:11px;">' + (isSystem ? '展开 ▼' : '收起 ▲') + '</span>';
        leftHtml += '</div>';
        leftHtml += '<div id="' + msgId + '" style="padding:8px;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;' + (isSystem ? 'display:none;' : '') + '">' + escapeHtml(content) + '</div>';
        leftHtml += '</div>';
    }
    leftHtml += '</div>';

    // 渲染右栏模板源码
    let rightHtml = '<div style="overflow-y:auto;max-height:65vh;">';
    const tmpl = data.template_source;
    if (tmpl) {
        rightHtml += '<div style="font-size:11px;color:var(--muted);margin-bottom:4px;">模板: ' + escapeHtml(tmpl.path) + '</div>';
        rightHtml += '<pre style="font-size:11px;white-space:pre-wrap;word-break:break-word;background:rgba(0,0,0,0.3);padding:8px;border-radius:4px;max-height:60vh;overflow-y:auto;">' + escapeHtml(tmpl.content) + '</pre>';
    } else {
        rightHtml += '<div class="empty">模板文件不可用</div>';
    }
    rightHtml += '</div>';

    document.getElementById('prompt-content').innerHTML = leftHtml + rightHtml;

    // 渲染底部 tools
    let toolsHtml = '';
    const tools = data.tools;
    if (tools && tools.length > 0) {
        const toolNames = tools.map(t => (t.function || {}).name || '?').join(', ');
        toolsHtml += '<div style="font-size:12px;color:var(--muted);">Tools (' + tools.length + '): ' + escapeHtml(toolNames) + '</div>';
        toolsHtml += '<pre id="prompt-tools-json" style="display:none;font-size:11px;white-space:pre-wrap;word-break:break-word;background:rgba(0,0,0,0.3);padding:8px;border-radius:4px;max-height:200px;overflow-y:auto;">' + escapeHtml(JSON.stringify(tools, null, 2)) + '</pre>';
        toolsHtml += '<button onclick="document.getElementById(\'prompt-tools-json\').style.display=\'block\';this.style.display=\'none\'" style="cursor:pointer;background:var(--border);color:var(--text);border:none;border-radius:4px;padding:2px 8px;font-size:11px;margin-top:4px;">展开 tools JSON</button>';
    }
    document.getElementById('prompt-tools').innerHTML = toolsHtml;
}

function toggleMsg(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function navigatePrompt(delta) {
    // 从 prompt_summary 中找上/下一条
    const ps = (lastInfo && lastInfo.prompt_summary) ? lastInfo.prompt_summary : null;
    if (!ps || !ps.records || !ps.records.length) return;
    const indices = ps.records.map(r => r.index);
    const pos = indices.indexOf(currentPromptIndex);
    if (pos < 0 || (pos + delta) < 0 || (pos + delta) >= indices.length) return;
    viewPrompt(indices[pos + delta]);
}

async function copyPromptJSON() {
    if (!currentPromptIndex) return;
    let data = promptCache[currentPromptIndex];
    if (!data) {
        const resp = await fetch('/debug/api/prompts/' + currentPromptIndex);
        data = await resp.json();
        promptCache[currentPromptIndex] = data;
    }
    const json = JSON.stringify(data.messages, null, 2);
    try {
        await navigator.clipboard.writeText(json);
    } catch(e) {
        // fallback: select from a temporary textarea
        const ta = document.createElement('textarea');
        ta.value = json;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
    }
}

function viewPromptList() {
    // 展开全部摘要（直接打开最近一条的详情）
    const ps = (lastInfo && lastInfo.prompt_summary) ? lastInfo.prompt_summary : null;
    if (ps && ps.records && ps.records.length) {
        viewPrompt(ps.records[ps.records.length - 1].index);
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
```

- [ ] **Step 4: 验证**

启动服务，发送一条消息，打开 `/debug`：
1. 概览 tab 应显示「📝 LLM Prompt」卡片
2. 卡片显示最近 3 条摘要（#序号、轮次、消息数、字符数、时间）
3. 点击「查看」→ 打开详情面板，左栏显示完整 messages，右栏显示模板源码
4. system 消息默认折叠，点击展开
5. 「复制 JSON」按钮可复制完整 messages 数组
6. 发送带工具调用的消息后，卡片显示多条记录
7. 多次对话后记录不超过 20 条

- [ ] **Step 5: 提交**

```bash
git add static/debug.html
git commit -m "feat: Debug 页面新增 LLM Prompt 查看卡片"
```

---

### Task 7: 集成验证 + 最终提交

- [ ] **Step 1: 运行全部测试**

```bash
python -m pytest core/ Module/ -q
```

预期：所有已有测试 PASS（新增的 prompt_recorder 测试也 PASS）

- [ ] **Step 2: 端到端验证清单**

- [ ] 启动服务，发送一条纯文本消息
- [ ] 打开 `/debug` → 概览 tab → 看到「📝 LLM Prompt」卡片，显示 1 条记录
- [ ] 点击记录的「查看」→ 详情面板打开
- [ ] 左栏显示完整 messages：system + 历史 + user
- [ ] system 消息默认折叠，点击展开
- [ ] 右栏显示 `prompt/Brain.j2` 模板源码
- [ ] 点击「📋 复制 JSON」→ 粘贴验证是合法 JSON
- [ ] 发送带工具的消息（如"搜索记忆"）→ 卡片显示 N 条记录（N = 工具轮数 + 1）
- [ ] 使用 ◀/▶ 按钮在同次对话的多轮 prompt 之间导航
- [ ] 刷新 `/debug` 页面 → 卡片数据仍然存在
- [ ] 重启服务 → 卡片清空（预期行为：仅内存存储）

- [ ] **Step 3: 最终提交**

```bash
git add -A
git commit -m "feat: LLM Prompt 查看器完整实现"
```

---

## 涉及文件总览

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `core/prompt_recorder.py` | **新增** | PromptRecorder + PromptRecord dataclass + `_content_len` + `_compute_derived_fields` |
| `core/test_prompt_recorder.py` | **新增** | 5 个单元测试 |
| `core/cognitive_loop.py` | 修改 | `__init__` 加参数，`_think_loop()` 加捕获代码 |
| `core/agent.py` | 修改 | `start()` 中创建 PromptRecorder 并注入 |
| `core/debug_info.py` | 修改 | 新增 `prompt_summary` 字段，`collect()` 填充 |
| `core/input/server.py` | 修改 | 新增 2 个 REST 端点 + 路由注册 |
| `static/debug.html` | 修改 | 新增 Prompt 卡片 + 详情面板 + JS 函数 |
