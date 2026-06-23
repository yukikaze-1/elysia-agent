# Elysia 内心世界 (Inner Voice) — 设计文档

**日期**: 2026-06-24
**步骤**: 伴侣化改造第二步

---

## 目标

让 Elysia 拥有可见的内心独白。用户能看到她"想了什么但没说"，创造"她有自己的小心思"的体验。

---

## 核心设计

### 输出格式

LLM 使用 `<think>...</think>` 标签包裹内心独白，公开回复直接写在后面：

```
<think>她今天好像不太一样…但又说不上来哪里不对。</think>
嗯，在呢。
```

### 为什么 `<think>` 在前

这是 CoT（Chain of Thought）模式：LLM 是自回归模型，生成 `public_reply` 时上下文已包含 `<think>` 的内容。模型是在"看着"自己的思考写回复——这能提升回复质量，而非事后辩解。

### 为什么用标签对

1. **预训练偏好** — LLM 见过海量 `<tag>内容</tag>` 结构，天然理解，出错概率低
2. **闭合边界清晰** — 多轮对话时模型能一眼分辨哪些是想、哪些是说
3. **解析简单** — 只需识别 `</think>` 一个边界，不是完整 XML 解析器

---

## 后端处理

### 流式状态机

```
LLM 输出流进入 cognitive_loop._think_loop:

State THINK (初始):
  - 累积文本到 think_buffer
  - 不送入 SentenceBuffer（不触发 TTS）
  - 不广播 text_delta 到前端
  - 检测到 "</think>" → 切换到 REPLY

State REPLY:
  - 提取 </think> 之后的内容
  - 正常流式处理：SentenceBuffer → TTS → 前端
  - 行为与现在完全一致
```

### 插入位置

`cognitive_loop.py:_think_loop` 中 `acc_text += content` 之前（line 339），增加标签检测逻辑。

### 降级策略

| 情况 | 处理 |
|------|------|
| LLM 输出没有 `<think>` 标签 | 全部当 public_reply，行为与现在一致 |
| 有 `<think>` 但没有 `</think>` | 全部当 think，无 public_reply（兜底） |
| `</think>` 出现在流末尾 | 之前的进 think，之后为空 |
| `<think>` 不在最开头 | 之前的文本也归入 public_reply（容错） |

### think 内容的使用

1. 广播到前端：`{type: "command", command: "think", params: {content: "..."}}`
2. 存储到当前 turn 的隐藏上下文（暂不实现，留给后续步骤）

---

## Prompt 改动

`prompt/Brain.j2` 输出段：将"只输出 public_reply"替换为 think 格式说明：

```
# 输出格式
先输出你的内心独白（用 <think>...</think> 包裹），然后直接说出你的公开回复。

<think>
你的真实想法。简短，1-2句。你在这里是诚实的——可以腹诽、可以矛盾、可以犹豫。
</think>
你的公开回复。自然说话，不需要每句话都有用。

注意：
- <think> 必须闭合（有 </think>）
- 公开回复直接跟在 </think> 后面，不要换行标记、不要标签包裹
- 不用 think 的时候（不想说什么特别的心里话），可以省略直接回复
```

---

## 前端改动

### main.js

`command` case 新增 `think` 处理：

```javascript
} else if (msg.command === 'think') {
  showThinkBubble(msg.params.content);
}
```

`showThinkBubble(content)` 函数：创建一个淡色气泡，插入 chat 容器末尾。

### chat.css

```css
.think-bubble {
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

think 气泡出现在 Elysia 回复气泡的**上方**。时间顺序上是先有想、后有说。

---

## 不做的

- 前端 think 显示/隐藏 toggle（后续加）
- think 内容持久化到会话历史（后续）
- think 影响情绪/记忆系统（后续步骤）
- 流式显示 think（当前版本 think 在 `</think>` 闭合后才一次性广播）

---

## 验证

1. 启动 agent → 发送一条消息 → 确认前端出现淡色 think 气泡（如果有） + 正常回复
2. 发送一条简单问候（"你好"）→ think 可能为空，回复正常
3. 发送一条情感性消息 → think 应有内容，且内容和回复有关联但不完全一致
4. `git diff` 确认无回归
5. `python -m pytest Module/task_system/tests/unit/ -v` 确认测试通过
