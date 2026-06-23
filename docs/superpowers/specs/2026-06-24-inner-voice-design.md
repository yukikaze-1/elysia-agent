# Elysia 内心世界 (Inner Voice) — 设计文档

**日期**: 2026-06-24
**步骤**: 伴侣化改造第二步

---

## 目标

让 Elysia 拥有可见的内心独白。用户能看到她"想了什么但没说"，创造"她有自己的小心思"的体验。

---

## 命名说明

当前 Brain.j2 已有一个"你是怎么想的"区块，用 `<inner_voice>` / `<public_reply>` 标签作为**思维指引**（告诉 LLM 你要先想后说）。但这些现在只是给 LLM 看的描述，不是输出格式——当前输出段的指令是"只输出 public_reply 的内容"。

本次改动将 `<inner_voice>` 从**纯思维指引**升级为**实际的输出标签**：LLM 在输出中显式产出 `<inner_voice>...</inner_voice>`，公开回复直接跟在后面。前端能看到她想了什么。

命名无需变更——`<inner_voice>` 同时作为指引标签和输出标签，语义一致。

---

## 核心设计

### 输出格式

```
<inner_voice>她今天好像不太一样…但又说不上来哪里不对。</inner_voice>
嗯，在呢。
```

### 为什么 inner_voice 在前

这是 CoT（Chain of Thought）模式：LLM 是自回归模型，生成公开回复时上下文已包含 `<inner_voice>` 的内容。模型是在"看着"自己的思考写回复——这能提升回复质量，而非事后辩解。

### 为什么用标签对

1. **预训练偏好** — LLM 见过海量 `<tag>内容</tag>` 结构，天然理解，出错概率低
2. **闭合边界清晰** — 多轮对话时模型能一眼分辨哪些是想、哪些是说
3. **解析简单** — 只需识别 `</inner_voice>` 一个闭合标签

---

## 后端处理

### 流式状态机

```
LLM 输出流进入 cognitive_loop._think_loop:

State PRE (初始): 等待 <inner_voice> 开标签
  - 累积文本，检测 <inner_voice>
  - 开标签之前的文本 → 正常流（降级容错）
  - 检测到 <inner_voice> → 切换到 THINK

State THINK: 等待 </inner_voice> 闭标签
  - 累积文本到 think_buf
  - 不送入 SentenceBuffer（不触发 TTS）
  - 不广播 text_delta 到前端
  - 检测到 </inner_voice> → 切换到 REPLY

State REPLY: 正常流式处理
  - 提取 </inner_voice> 之后的内容
  - SentenceBuffer → TTS → 前端
  - 行为与现在完全一致
```

### 流式标签检测

**问题**：标签可能被切在两个 chunk 之间：
```
chunk N:   "她今天心情好像不错</inner_v"
chunk N+1: "oice>嗯，在呢。"
```

**方案**：保持一个长度为 `len("</inner_voice>") - 1 = 14` 字节的前向缓冲（tail buffer）。每次新 chunk 到达时，检查 `(tail + chunk)` 是否包含 `</inner_voice>`：

tail = ""       # 用于跨 chunk 检测标签的尾部缓冲
think_buf = ""  # 已确认的 think 内容
state = PRE     # PRE: 等开标签 / THINK: 等闭标签 / REPLY: 正常流
TAG_OPEN = "<inner_voice>"
TAG_CLOSE = "</inner_voice>"

for chunk in stream:
    if state == PRE:
        combined = tail + chunk
        idx = combined.find(TAG_OPEN)
        if idx >= 0:
            before = combined[:idx].lstrip()
            if before:
                feed_to_pipeline(before)  # 开标签之前的文本归 public_reply
            state = THINK
            tail = ""
            chunk = combined[idx + len(TAG_OPEN):]
        else:
            safe_len = max(0, len(combined) - len(TAG_OPEN) + 1)
            if safe_len > 0:
                feed_to_pipeline(combined[:safe_len])
            tail = combined[safe_len:]  # 保留危险区 = 尾部 len(TAG_OPEN)-1 字节
            continue  # 跳过 THINK/REPLY 逻辑

    if state == THINK:
        combined = tail + chunk
        idx = combined.find(TAG_CLOSE)
        if idx >= 0:
            think_content = think_buf + combined[:idx]
            after = combined[idx + len(TAG_CLOSE):].lstrip()
            state = REPLY
            broadcast_think(think_content)
            feed_to_pipeline(after)
        else:
            keep_len = min(len(TAG_CLOSE) - 1, len(combined))
            if len(combined) > keep_len:
                think_buf += combined[:-keep_len]
            tail = combined[-keep_len:]

    if state == REPLY:
        feed_to_pipeline(chunk)

### `</inner_voice>` 后空白处理

LLM 可能在 `</inner_voice>` 后输出 `\n`、空格等。切换到 REPLY 后，`</inner_voice>` 之后的内容需 `.lstrip()` 处理前导空白，避免：
- TTS 把空白当停顿
- 纯文本流中多出空 delta

### 插入位置

`cognitive_loop.py:_think_loop` 中 `acc_text += content` 之前（line 339），增加标签检测逻辑。

### 降级策略

| 情况 | 处理 |
|------|------|
| LLM 输出没有 `<inner_voice>` 标签 | 全部当 public_reply，行为与现在一致 |
| 有 `<inner_voice>` 但没有 `</inner_voice>` | 全部当 inner_voice 内容，无 public_reply（兜底） |
| `<inner_voice>` 不在流开头 | 之前的文本归入 public_reply（容错），看到标签才开始 THINK |

### inner_voice 内容的使用

1. 广播到前端：`{type: "command", command: "inner_voice", params: {content: "..."}}`
2. 存储到当前 turn 的隐藏上下文（暂不实现，留给后续步骤）

---

## Prompt 改动

`prompt/Brain.j2` 输出段：

```
将"只输出 public_reply 的内容。不要输出 inner_voice 或任何其他标签。"
替换为：

先输出你的内心独白（用 <inner_voice>...</inner_voice> 包裹），然后直接说出你的公开回复。

注意：
- <inner_voice> 必须闭合（有 </inner_voice>）
- 公开回复直接跟在 </inner_voice> 后面，不要加额外的标签或标记
- 如果此刻没什么特别的心里话，可以省略 inner_voice，直接回复
```

---

## 前端改动

### main.js

`command` case 新增 `inner_voice` 处理：

```javascript
} else if (msg.command === 'inner_voice') {
  showInnerVoice(msg.params.content);
}
```

### chat.css

```css
.inner-voice-bubble {
  color: #888;
  font-style: italic;
  font-size: 0.85em;
  padding: 4px 12px;
  margin: 2px 0;
  border-left: 2px solid #ccc;
  opacity: 0.8;
}
```

### 渲染位置

inner_voice 气泡出现在 Elysia 回复气泡的**上方**。时间顺序上是先有想、后有说。

---

## 不做的

- 前端 inner_voice 显示/隐藏 toggle（后续加）
- inner_voice 内容持久化到会话历史（后续）
- inner_voice 影响情绪/记忆系统（后续步骤）
- 流式显示 inner_voice（当前版本在 `</inner_voice>` 闭合后才一次性广播）

---

## 验证

1. 启动 agent → 发送一条消息 → 确认前端出现淡色 inner_voice 气泡（如果有） + 正常回复
2. 发送一条简单问候（"你好"）→ inner_voice 可能为空，回复正常
3. 发送一条情感性消息 → inner_voice 应有内容，且内容和回复有关联但不完全一致
4. `git diff` 确认无回归
5. `python -m pytest Module/task_system/tests/unit/ -v` 确认测试通过
