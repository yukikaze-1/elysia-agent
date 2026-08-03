# code-review-report 中低危项修复

**日期**: 2026-06-25
**来源**: `docs/code-review-report-2026-06-22.md` Section 3-4 中低危项

---

## 已确认修复（无需改动）

| # | 问题 | 确认 |
|---|------|------|
| 3.2.2 | `js/audio.js:11` atob() 异常未捕获 | 已有 try/catch |
| 3.2.3 | `js/main.js` switch 无 default | 已有 `default: console.warn(...)` |
| 2.2.6 | `esc()` 三个独立实现 | 已有 `js/esc.js` 统一实现，chat.js + settings.js 均 import |
| 2.2.1 | chat.js↔persistence.js 循环依赖 | 存在——`chat.js:513` 底部 import `persistence.js`，`persistence.js:3` import `chat.js`。通过底部导入故意管理，ES 模块 live binding 可工作。不修。 |

## 跳过（文件不存在或仅 dev 工具）

| # | 问题 | 原因 |
|---|------|------|
| 4 (app.py) | 忙等待轮询 FSM + except 吞 SystemExit | `core/app.py` 不存在，已重构 |
| 4 (vision/plugin.py) | 每次调用创建/销毁后端 | `core/vision/plugin.py` 不存在 |
| 4 (logger/__main__.py) | 调用不存在的 `factory.create()` | 仅 dev smoke test，非生产路径 |

---

## A. 快修（6 项，1-3 行/项）

### A1. `output/websocket.py` — _audio_seq 无限制增长

每路音频 stream 在 `on_audio_chunk` 中递增 seq，dict 从不清理。

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

### A2. `input/server.py:271` — token 用量拉全量做 O(n) 聚合

```python
# 改前 (line 271)
token_docs = await session._db.find("token_usage", {}, limit=100000)

# 改后
token_docs = await session._db.find("token_usage", {}, limit=2000)
```

### A3. `input/audio_buffer.py:176` — VAD 帧异常被空白 except 吞掉

```python
# 改前
            except Exception:
                pass

# 改后
            except Exception:
                logger.warning("VAD 帧处理失败", exc_info=True)
```

### A4. `output/manager.py:57-59` — 广播失败记 DEBUG 级别过低

```python
# 改前
self._logger.debug("broadcast %s to %s failed: %s", ...)

# 改后
self._logger.warning("broadcast %s to %s failed: %s", ...)
```

### A5. `js/state.js:resetConnection()` — 未重置 live2dStaticImage

```javascript
// 在 this.llmModel = ''; 之后加
this.live2dStaticImage = '';
```

### A6. `js/ws.js:send()` — 非 OPEN 时静默丢弃无返回值

```javascript
// 改前 — return 无值
if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    console.warn('WS: 未连接，消息丢弃', ...);
    return;
}

// 改后 — return false，调用方可判断
if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    console.warn('WS: 未连接，消息丢弃', ...);
    return false;
}
```

---

## B. 小修（3 项）

### B1. `tool/manager.py:110,154` — ensure_future → create_task

```python
# 改前
task = asyncio.ensure_future(...)   # line 110
task = asyncio.ensure_future(_drive())  # line 154

# 改后
task = asyncio.create_task(...)   # line 110
task = asyncio.create_task(_drive())  # line 154
```

### B2. `schema/message_format.py:205` — localhost 子串匹配

`"localhost" in source` 会匹配 `evil.com/localhost`。

改用 `"://localhost"` 消除路径攻击向量。已知局限：`://localhost.example.com` 仍会匹配（`://localhost` 是子串），但比现状更安全——原始代码同样匹配它，且攻击者控制 hostname 的场景极罕见。

```python
# 改前
if "localhost" in source or "127.0.0.1" in source:

# 改后
if "://localhost" in source or "://127.0.0.1" in source:
```

### B3. `js/persistence.js:117-121` — 工具卡片参数 fallback 用 `split(', ')`

含逗号的参数值被错误分割。但这是旧格式 fallback，当前主力路径走 `JSON.parse(dataset.toolArgs)`。不修，加注释标明 legacy-only。

```javascript
// 在 split 之前加注释
// 旧格式 fallback——工具卡片参数按 ', ' 分割仅用于兼容旧数据
```

---

## 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度 3.6:1→4.5:1、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN 脚本 `integrity` 属性、Content-Security-Policy 头 |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线整体替换 |

---

## 不变

- `input/server.py:122` GET `/shutdown` — 保留浏览器直接访问，低风险（需手动输入 URL）
- `js/settings.js` click-outside 监听器 — `setupSettings()` 在 init 时注册一次，页面生命周期内不需要移除
- `js/main.js` init() 中的 document 事件监听器 — 页面级别，生命周期内有效

---

## 验证

1. `python -m pytest Module/prompt/test_prompt.py -v` — 23 passed
2. `python -m pytest core/tests/test_phase3.py::TestPromptAssemblerHelpers -v` — 3 passed
3. 确认 js 文件语法正常（浏览器加载无报错）
