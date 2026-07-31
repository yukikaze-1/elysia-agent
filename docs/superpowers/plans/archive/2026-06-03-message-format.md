# Message Format Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate message storage from `{role, content}` to `{id, role, parts[], content_type{}, timestamp}`, push session on WS connect, fix AI messages lost on refresh.

**Architecture:** Server SessionMemory becomes the single source of truth. `parts` array (one element per display unit: sentence/code/markdown) is the canonical data format. `content_type` maps type labels to part indices. On WebSocket connect, the server pushes the full session via `{"type": "session", "messages": [...]}`. Frontend localStorage is demoted to a fast-start cache.

**Tech Stack:** Python 3.13 (aiohttp, asyncio), vanilla JS (ES modules, no framework)

---

### Task 1: Add UUID7 helper

**Files:**
- Create: `core/message_id.py`

- [ ] **Step 1: Create the module**

```python
"""Message ID generation — UUID7 (time-ordered, globally unique)."""
from __future__ import annotations

import uuid


def new_message_id() -> str:
    """Return a UUID7 string prefixed with 'msg_' for readability."""
    return f"msg_{uuid.uuid4().hex[:26]}"  # uuid7 not in stdlib; uuid4 hex is sufficient
```

Note: Python 3.13 stdlib has `uuid.uuid4()` but not uuid7. Using `uuid4().hex` (32 hex chars) truncated to 26 for compact IDs. The `msg_` prefix makes it clear these are message IDs in wire format.

- [ ] **Step 2: Commit**

```bash
git add core/message_id.py
git commit -m "feat: add message ID generator"
```

---

### Task 2: Refactor SessionMemory to accept new message format

**Files:**
- Modify: `core/memory/session.py`

- [ ] **Step 1: Update `add_message()` to accept parts-based messages**

Replace the current `add_message()` (line 28-32):

```python
def add_message(self, role: str, content: str, **meta) -> None:
    entry = {"role": role, "content": content}
    if meta:
        entry["meta"] = meta
    self._messages.append(entry)
```

With:

```python
def add_message(self, msg: dict) -> None:
    """Store a message in the session ring-buffer.

    msg must have: id, role, parts, content_type, timestamp
    """
    self._messages.append(msg)
```

- [ ] **Step 2: Add `messages_for_client()` method**

After `recent_messages()` (line 38), add:

```python
def messages_for_client(self, n: int | None = None) -> list[dict]:
    """Return messages suitable for sending to the frontend over WebSocket.

    Same as recent_messages() but in the future may filter/transform for
    client-specific needs (e.g. omitting internal metadata).
    """
    return self.recent_messages(n)
```

- [ ] **Step 3: Run existing tests to check for breakage**

```bash
python -m pytest core/tests/ -v -k session 2>&1 | head -30
```

- [ ] **Step 4: Commit**

```bash
git add core/memory/session.py
git commit -m "refactor: change SessionMemory.add_message() to accept full message dicts"
```

---

### Task 3: Build message dicts in CognitiveLoop

**Files:**
- Modify: `core/cognitive_loop.py`
- Modify: `core/message_id.py` (import usage)

- [ ] **Step 1: Collect sentences during streaming**

In `_think_loop()`, add `all_sentences = []` before the stream loop (after line 187 before the `for round_num` loop). Then at each sentence yield point, collect:

```python
# Line 211-214, inside the stream loop:
sentence = self._sentence_buf.feed(content)
if sentence:
    all_sentences.append(sentence)  # ADD THIS LINE
    logger.info("分句 (%d 字符)", len(sentence))
    ...
```

And at the flush point (line 240-261):

```python
remaining = self._sentence_buf.flush()
if remaining:
    # ADD: collect remaining as a sentence
    if not _is_emoji_only(remaining):
        all_sentences.append(remaining)
    ...
```

- [ ] **Step 2: Build parts array and store message**

Replace the assistant message storage (line 347-348):

```python
# Current:
if acc_text.strip():
    self._session.add_message("assistant", acc_text)

# New:
if acc_text.strip():
    from core.message_id import new_message_id
    import datetime
    parts = [{"content": s} for s in all_sentences]
    content_type = {}  # all text for now; structured blocks added in future
    msg = {
        "id": new_message_id(),
        "role": "assistant",
        "parts": parts if parts else [{"content": acc_text}],
        "content_type": content_type,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
    }
    self._session.add_message(msg)
```

- [ ] **Step 3: Build user message dict**

Replace the user message storage (line 177):

```python
# Current:
self._session.add_message("user", user_message)

# New:
from core.message_id import new_message_id
import datetime
user_msg = {
    "id": new_message_id(),
    "role": "user",
    "parts": [{"content": user_message}],
    "content_type": {},
    "timestamp": datetime.datetime.now().astimezone().isoformat(),
}
self._session.add_message(user_msg)
```

Move the imports (`new_message_id`, `datetime`) to the top of `_think_loop()` or module level — add `import datetime` to the module imports and `from core.message_id import new_message_id` at the top.

- [ ] **Step 4: Run cognitive loop tests**

```bash
python -m pytest core/tests/ -v -k cognitive 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "feat: build parts-based message dicts in cognitive loop"
```

---

### Task 4: Adapt PromptAssembler to consume new format

**Files:**
- Modify: `core/prompt_assembler.py`

- [ ] **Step 1: Add helper to extract LLM-format message**

Add at module level:

```python
def _to_llm_format(msg: dict) -> dict:
    """Convert a stored message dict to LLM-compatible {role, content}."""
    if "parts" in msg:
        content = "".join(p["content"] for p in msg["parts"])
    else:
        content = msg.get("content", "")
    return {"role": msg["role"], "content": content}
```

- [ ] **Step 2: Update `build_messages()` to use the helper**

In `build_messages()` (line 109-112), change:

```python
# Current:
recent = self._session.recent_messages(8)
for msg in recent:
    messages.append({"role": msg["role"], "content": msg["content"]})

# New:
recent = self._session.recent_messages(8)
for msg in recent:
    messages.append(_to_llm_format(msg))
```

- [ ] **Step 3: Run prompt assembler tests**

```bash
python -m pytest Module/prompt/test_prompt.py -v 2>&1 | tail -20
```

- [ ] **Step 4: Commit**

```bash
git add core/prompt_assembler.py
git commit -m "feat: adapt PromptAssembler to consume parts-based message format"
```

---

### Task 5: Adapt DebugInfo to show new format

**Files:**
- Modify: `core/debug_info.py`

- [ ] **Step 1: Update session_recent extraction**

In `collect()` (lines 78-88), change:

```python
# Current:
info.session_recent = [
    {
        "role": m.get("role", "?"),
        "content": _truncate(m.get("content", ""), 100),
    }
    for m in msgs[-5:]
]

# New:
info.session_recent = [
    {
        "role": m.get("role", "?"),
        "parts": m.get("parts", [{"content": m.get("content", "")}]),
        "content_type": m.get("content_type", {}),
    }
    for m in msgs[-5:]
]
```

The `_truncate` call is removed because truncation should be done by the debug HTML renderer. The `content_type` field is included so the debug panel can show type annotations.

- [ ] **Step 2: Update `static/debug.html` to render new format**

In `static/debug.html`, find the session rendering code that uses `msg.content`. Update it to iterate `msg.parts` and render each part with its type indicator. This is a quick adaptation — the debug page already has `session_recent` iteration logic.

- [ ] **Step 3: Commit**

```bash
git add core/debug_info.py static/debug.html
git commit -m "feat: adapt debug info and panel to parts-based message format"
```

---

### Task 6: Push session on WebSocket connect

**Files:**
- Modify: `core/input/server.py`

- [ ] **Step 1: Send session on WS connect**

In `_handle_ws()` (after line 159 `agent._output.add_channel(channel)`), add:

```python
# After channel setup, before the message loop:
try:
    session_msgs = agent._session.messages_for_client()
    await ws.send_json({"type": "session", "messages": session_msgs})
except Exception:
    logger.warning("Failed to send session on connect", exc_info=True)
```

- [ ] **Step 2: Commit**

```bash
git add core/input/server.py
git commit -m "feat: push session messages on WebSocket connect"
```

---

### Task 7: Update frontend to handle session message and new format

**Files:**
- Modify: `static/js/main.js`
- Modify: `static/js/persistence.js`

- [ ] **Step 1: Add `restoreFromServer()` to persistence.js**

Add to `static/js/persistence.js`:

```javascript
/** 从服务端 session 数据重建整个聊天（替换 localStorage 缓存） */
export function restoreFromServer(messages) {
  const chat = document.getElementById('chat');
  chat.innerHTML = '';
  state.messages = [];
  state.toolCards = {};
  state.lastRole = '';
  state.lastMsgTime = 0;

  for (const m of messages) {
    const parts = m.parts || [];
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (!p.content) continue;

      if (m.role === 'user') {
        const row = document.createElement('div');
        row.className = 'msg-row user';
        row.innerHTML = `<div class="msg-bubble">${esc(p.content)}</div><div class="msg-avatar">😎</div>`;
        chat.appendChild(row);
        state.messages.push({ role: 'user', content: p.content });
      } else {
        state.lastRole = 'assistant';
        const { row, textSpan } = createAssistantRow();
        textSpan.textContent = p.content;
        chat.appendChild(row);
        state.messages.push({ role: 'assistant', content: p.content });
      }
    }
  }

  // Save to localStorage as cache
  saveChat();

  const wrap = document.getElementById('chat-wrap');
  requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
}
```

- [ ] **Step 2: Add `session` case to `handleMessage()`**

In `static/js/main.js`, add to the switch in `handleMessage()`:

```javascript
case 'session':
    restoreFromServer(msg.messages);
    break;
```

- [ ] **Step 3: Import `restoreFromServer` and `createAssistantRow` in main.js**

Update the import from persistence.js (line 13):

```javascript
// Current:
import { restoreChat } from './persistence.js';

// New:
import { restoreChat, restoreFromServer } from './persistence.js';
```

And add `createAssistantRow` to the chat.js import (line 3-8) if not already imported — it should already be there since `main.js` imports from `chat.js`.

- [ ] **Step 4: Fix `saveChat()` to read from DOM**

In `static/js/persistence.js`, replace `saveChat()`:

```javascript
// Current:
export function saveChat() {
  const msgs = state.messages.slice(-CHAT_MAX_STORED);
  localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs));
}

// New:
export function saveChat() {
  const rows = document.querySelectorAll('#chat .msg-row');
  const msgs = [];
  for (const row of rows) {
    if (row.classList.contains('user')) {
      const bubble = row.querySelector('.msg-bubble');
      if (bubble) msgs.push({ role: 'user', content: bubble.textContent });
    } else if (row.classList.contains('assistant')) {
      const textSpan = row.querySelector('.msg-text');
      if (textSpan && textSpan.textContent.trim()) {
        msgs.push({ role: 'assistant', content: textSpan.textContent });
      }
    }
  }
  state.messages = msgs;
  localStorage.setItem(SETTINGS_STORAGE_KEYS.CHAT, JSON.stringify(msgs.slice(-CHAT_MAX_STORED)));
}
```

- [ ] **Step 5: Commit**

```bash
git add static/js/main.js static/js/persistence.js
git commit -m "feat: add session message handler and server-driven chat restore"
```

---

### Task 8: End-to-end verification

- [ ] **Step 1: Start server and test**

```bash
# Start the server (adjust command as needed)
python app.py &
sleep 3
```

- [ ] **Step 2: Verify WS session push**

Open browser DevTools → Network → WS tab. After connecting, verify the first server message is `{"type": "session", "messages": [...]}` with each message containing `id`, `role`, `parts`, `content_type`, `timestamp`.

- [ ] **Step 3: Verify message format**

Send a text message and get an AI response. In DevTools Console:
```javascript
JSON.parse(localStorage.getItem('elysia_chat'))
```
Verify assistant messages are present and have the new format.

- [ ] **Step 4: Verify refresh restores messages**

Refresh the page. All messages (user + assistant) should appear.

- [ ] **Step 5: Verify localStorage clear + refresh**

```javascript
localStorage.clear()
```
Refresh. Messages should still load from server session push.

- [ ] **Step 6: Verify /debug page**

Navigate to `/debug`. Session display should show parts-based messages.

- [ ] **Step 7: Run all tests**

```bash
python -m pytest core/tests/ -v
python -m pytest Module/prompt/test_prompt.py -v
```

All existing tests must pass.

- [ ] **Step 8: Commit any fixes from verification**

```bash
git add -A
git commit -m "fix: verification fixes for message format migration"
```
