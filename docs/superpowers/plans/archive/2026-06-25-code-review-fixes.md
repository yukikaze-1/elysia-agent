# code-review-report 中低危项修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 code-review-report 中 9 个低风险项——6 个快修 + 3 个小修

**Architecture:** 分 2 个任务。Task 1 改 Python 后端（6 文件），Task 2 改前端 JS（3 文件）。所有改动相互独立，仅触及单文件。

**Tech Stack:** Python 3.13 + pytest, Vanilla JS（ES modules）

## Global Constraints

- `conda` 环境: `source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia`
- 4 项已确认修复、3 项跳过（文件不存在）、3 项需独立 spec —— 不碰
- 3 项有意不动（GET /shutdown、click-outside 监听器、init() 监听器）—— 不碰

---

### Task 1: Python 后端修复（A1-A4 + B1-B2，6 文件）

**Files:**
- Modify: `Module/output/websocket.py:74`
- Modify: `core/input/server.py:271`
- Modify: `core/input/audio_buffer.py:176`
- Modify: `Module/output/manager.py:57`
- Modify: `Module/tool/manager.py:110,154`
- Modify: `Module/schema/message_format.py:205`

**Interfaces:** 无跨文件依赖。每项独立。

- [ ] **Step 1: A1 — _audio_seq 加容量上限**

`Module/output/websocket.py` 约第 74 行：

```python
# 改前
self._audio_seq = getattr(self, "_audio_seq", {})
seq = self._audio_seq.get(stream_id, 0)
self._audio_seq[stream_id] = seq + 1

# 改后
self._audio_seq = getattr(self, "_audio_seq", {})
if len(self._audio_seq) > 32:
    self._audio_seq = dict(list(self._audio_seq.items())[-16:])
seq = self._audio_seq.get(stream_id, 0)
self._audio_seq[stream_id] = seq + 1
```

- [ ] **Step 2: A2 — token 用量 limit 100000 → 2000**

`core/input/server.py` 约第 271 行：

```python
# 改前
token_docs = await session._db.find("token_usage", {}, limit=100000)

# 改后
token_docs = await session._db.find("token_usage", {}, limit=2000)
```

- [ ] **Step 3: A3 — VAD 帧异常加 logger.warning**

`core/input/audio_buffer.py` 约第 176 行：

```python
# 改前
            except Exception:
                pass

# 改后
            except Exception:
                logger.warning("VAD 帧处理失败", exc_info=True)
```

- [ ] **Step 4: A4 — 广播失败 DEBUG → WARNING**

`Module/output/manager.py` 约第 57 行：

```python
# 改前
self._logger.debug("broadcast %s to %s failed: %s", ...)

# 改后
self._logger.warning("broadcast %s to %s failed: %s", ...)
```

- [ ] **Step 5: B1 — ensure_future → create_task（2 处）**

`Module/tool/manager.py` 约第 110、154 行：

```python
# 改前 (line 110)
task = asyncio.ensure_future(
    self._pipeline.execute(plugin, name, ctx, raw_params, sem),
)

# 改后
task = asyncio.create_task(
    self._pipeline.execute(plugin, name, ctx, raw_params, sem),
)

# 改前 (line 154)
task = asyncio.ensure_future(_drive())

# 改后
task = asyncio.create_task(_drive())
```

- [ ] **Step 6: B2 — localhost 子串匹配加固**

`Module/schema/message_format.py` 约第 205 行：

```python
# 改前
if "localhost" in source or "127.0.0.1" in source:

# 改后
if "://localhost" in source or "://127.0.0.1" in source:
```

- [ ] **Step 7: 运行测试**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest Module/prompt/test_prompt.py core/tests/test_phase3.py::TestPromptAssemblerHelpers -v 2>&1
```

预期：26 passed

- [ ] **Step 8: Commit**

```bash
git add Module/output/websocket.py core/input/server.py core/input/audio_buffer.py Module/output/manager.py Module/tool/manager.py Module/schema/message_format.py
git commit -m "fix: code-review 中低危项——Python 后端 6 处修复

A1 _audio_seq cap, A2 token limit 100k→2k, A3 VAD except log, A4 broadcast log level,
B1 ensure_future→create_task, B2 localhost substring harden"
```

---

### Task 2: 前端 JS 修复（A5-A6 + B3，3 文件）

**Files:**
- Modify: `static/js/state.js:84`
- Modify: `static/js/ws.js:83`
- Modify: `static/js/persistence.js:121`

**Interfaces:** 无跨文件依赖。每项独立。

- [ ] **Step 1: A5 — resetConnection 补充 live2dStaticImage 重置**

`static/js/state.js` 在 `this.llmModel = '';` 之后加一行：

```javascript
// 在 this.llmModel = ''; 之后
this.live2dStaticImage = '';
```

- [ ] **Step 2: A6 — ws.js send() 返回 false 指示丢弃**

`static/js/ws.js` 约第 83 行：

```javascript
// 改前
    return;

// 改后
    return false;
```

- [ ] **Step 3: B3 — persistence.js 旧格式 fallback 加注释**

`static/js/persistence.js` 约第 121 行，`split` 之前：

```javascript
// 改前
        argsText.split(', ').forEach(pair => {

// 改后
        // 旧格式兼容——工具卡片参数按 ', ' 分割仅用于旧数据回退
        argsText.split(', ').forEach(pair => {
```

- [ ] **Step 4: Commit**

```bash
git add static/js/state.js static/js/ws.js static/js/persistence.js
git commit -m "fix: code-review 中低危项——前端 JS 3 处修复

A5 live2dStaticImage reset, A6 ws.send return false, B3 legacy comment"
```
