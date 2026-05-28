# 文字/语音同步 — 踩坑记录与经验总结

记录从"文字和语音各发各的"到"绑定发送、前端无感"的完整演进过程。

## 时间线

### 阶段 0：没有分句，整段发送

LLM 流式 delta 直接推到前端，`send(msg)` 发送完整消息。问题：一整段回复在一个气泡里，语音也是整段合成。

### 阶段 1：SentenceBuffer 分句（`f031850`）

引入 `SentenceBuffer`，按 `。！？\n～…` 等分隔符切句。每句触发 `_start_tts_stream`。前端仍是整段气泡。

**踩坑**：emoji 被送进 TTS → GPT-SoVits 返回 400（`1e6c146`）。修复：`_is_emoji_only()` 跳过纯 emoji 句；`text_preprocess.py` 加 `_EMOJI_RE` 全局过滤。

### 阶段 2：逐句气泡（`a7e77d8` ~ `984c48d`）

服务端用 `_sync_event` + `_sync_buffer` + `_sync_sentences` 三层缓冲：
- LLM 文字 delta 暂存在 `_sync_buffer`，`SentenceBuffer` 分句后存到 `_sync_sentences`
- 等 TTS 首块音频到达 → `_sync_event.set()` → 释放缓冲
- 逐句发送 delta + `send("")` 创建独立气泡

**踩坑 1 — `send("")` 静默失败（`984c48d`）**

`WebSocketChannel.on_message("")` 调用 `vars("")` → `TypeError`，被 `_broadcast` 的 `return_exceptions=True` 吞掉。所有 `send("")` 调用都没到达前端，`finalizeDelta()` 从未执行，`currentDelta` 保持非 null，全部句子追加到同一个气泡。

**教训**：`_broadcast` 的 `return_exceptions=True` 会静默吞错。应检查 `on_message` 的参数类型兼容性。不要假设纯字符串能通过 `vars()` 或 `hasattr(msg, "to_dict")` 的判断。

**踩坑 2 — `_sync_boundaries` vs `_sync_sentences`（`a7e77d8`）**

最初用 `_sync_boundaries: set[int]` 记录缓冲 chunk 索引，在 flush 时按索引切分。失败原因：一个 LLM chunk 可包含多个句子，`_sync_boundaries={0}` 只能切一次。换回 `_sync_sentences: list[str]`（存储完整句子）解决。

**教训**：不要按 chunk 位置做分句——chunk 边界 != 句子边界。让 `SentenceBuffer` 自己判断句子完成度，外部只存完整句子文本。

**踩坑 3 — SentenceBuffer 残余前缀截断（`286a95d`）**

Sync 释放后，`SentenceBuffer` 内部残留了 buffering 阶段吞入的前半句（如 "不过我可不是什么高玩哦，属于又"）。第一个 live chunk（"菜又爱玩那种类型～"）被直接广播，前端只看到后半句。

**教训**：Sync flush 后必须调 `sentence_buf.flush()` 将残余前缀先发到前端。3 处 flush 路径（sync 释放 / 工具调用 / fallback）都需要。

### 阶段 3：前端逐条弹出 + 语音对齐（`8815d47`）

前端引入 `pendingBubbles` + `scheduleReveal()` + `onAudioStart()`：
- 新气泡初始隐藏（`opacity:0, translateY(8px)`）
- 定时器链：首条 100ms，后续 500ms
- `onAudioStart()` 钩到 `Audio.play().then()`，音频播放时取消定时器、立即弹出气泡

**踩坑 — 短回复场景 sync 机制失效**

LLM 比 TTS 快，回复在 TTS 首块音频到达前就完成了。文字走 fallback 路径一次性全发，前端定时器链在音频到达前就弹完了所有气泡。

**本质矛盾**：服务端 sync 依赖"LLM 还在输出时 TTS 音频到达"。短回复场景这个假设不成立。

### 阶段 4：绑定发送，去掉 sync（`402035b`）

核心洞察：**让 TTS task 自己负责发送文字+音频**，不依赖外部 sync 信号。

```
LLM → SentenceBuffer 分句 → _start_tts_stream(sentence)
  → TTS 后台 task 等首块音频
  → 绑定发送: delta(完整句) + send("") + audio_chunks + audio_end
  → 前端收到直接渲染+播放
```

服务端删除约 60 行（`_sync_event` / `_sync_buffer` / `_audio_pending` / `_drain_audio_pending` / 5s timeout / 4 个 flush 出口）。前端删除约 40 行（`textQueue` / `pendingBubbles` / `scheduleReveal` / `onAudioStart` 等）。

**关键前提**：TTS 服务端 `workers=1` + FIFO 严格串行，保证句间顺序。没有这个前提就需要逐句信号量。

### 阶段 5：前端排队弹出（`7849386` ~ `7ec8590`）

**踩坑 4 — 后句文字在前句音频播完前到达（`7849386`）**

workers=1 保证句 1 TTS 完成后才开始句 2。句 2 文字在服务端发出时，句 1 音频还在前端播放。文字直接渲染，用户看到下一句文字时还在听上一句话。

**修复**：`appendDelta` 创建新气泡时检查 `audioPlaying`，若为 true → 不挂 DOM → 存入 `nextBubbleQueue` → `playNext` / `onended` 时弹出。

**踩坑 5 — CSS 动画覆盖 inline opacity（`7b7af31`）**

`.msg-row { animation: fadeIn .2s ease }` 在动画执行期间覆盖 inline `style.opacity='0'`。气泡创建后闪一下（0.2s）又消失。

**教训**：CSS animation 动画执行期间优先级高于 inline style。隐藏元素时必须同时设 `style.animation = 'none'`。

**踩坑 6 — 隐藏气泡占位空白（`7ec8590`）**

`opacity: 0` 的元素仍占 flex 布局空间。排队气泡在 DOM 里留了空位，底部被抬高。

**修复**：排队气泡不 `appendChild`，弹出时才挂到 DOM。`requestAnimationFrame` 保证浏览器先解析 DOM 再触发 CSS transition。

## 架构决策

### 为什么不让服务端等服务端自己等？

最初 sync 机制在服务端等 TTS 首块音频，但 LLM 快于 TTS 时失效。方案演进：

| 方案 | 可靠性 | 复杂度 | 首句延迟 |
|------|--------|--------|----------|
| 服务端 sync + 5s timeout | 短回复失效 | 高（4 个出口）| 0-5s（取决于 timeout）|
| 前端 audio-driven reveal | 一般（依赖定时器+音频事件）| 中 | ~100ms |
| 绑定发送（最终方案）| 高（服务端保证）| 低（单一路径）| ~1s（等 TTS）|

绑定发送的代价是首句延迟 ~1s，但换来零竞态、大幅简化的代码。

### 为什么前端只做最轻量的缓存？

最终方案里前端只保留了 `nextBubbleQueue`——仅负责"在听上一句时不显示下一句"。所有时序由服务端 workers=1 FIFO 保证。前端不关心"第几句"，不设定时器。

## 经验清单

1. **不要用 chunk 位置做分句**。LLM 的 chunk 边界和语义边界没有关系。
2. **`_broadcast` 的异常被静默吞掉**。给 `on_message` 传参前检查类型兼容性。
3. **Sync flush 后检查 SentenceBuffer 残余**。`sentence_buf.flush()` 释放文本-语音同步切换时残留的前缀。
4. **CSS animation 覆盖 inline style**。设 `opacity:0` 前先 `animation:none`。
5. **`opacity:0` 仍占布局空间**。延迟 DOM append 比隐藏元素更干净。
6. **服务端保证顺序，前端只做缓存**。前端不需要定时器、不需要自己排顺序，只负责"等音频"。
7. **TTS workers=1 FIFO 是绑定发送方案的前提**。没有这个保证就需要逐句信号量。
