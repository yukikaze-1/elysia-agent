# Elysia 伴侣化改造路线图

**创建日期**: 2026-06-24
**目标**: 让 Elysia 从"功能齐全的聊天机器人"变成"有灵魂的 AI 伴侣"

---

## 定位

- **AI 伴侣是灵魂** — Elysia 的核心身份。她应该感觉像一个"人"，有记忆、有情绪、有小心思
- **AI 助手是手臂** — TaskSystem + Agent 的能力。其他 agent 可以干活，Elysia 偶尔也可以帮你做事
- **用户是妖梦** — AI 的名字是 Elysia，不是妖梦

---

## 核心问题

Elysia 目前是一个**反应式系统**——你说一句，她回一段。真正让人感觉"活着"的东西缺失：

- ❌ 话太多——像在写文章，不像在聊天
- ❌ 没有小心思——每句话都是"回复"，没有自己的内心世界
- ❌ 记忆机械——记了但不会自然地提起
- ❌ 从不主动——只在被叫的时候才存在

---

## 优先级

### 🥇 第一步：对话风格改造 ✅

**状态**: 已完成 (2026-06-24)

**做了什么**:
- Brain.j2 全面中文重写——身份/关系/对话风格分离
- 回复从"写文章"变成 1-3 句自然对话
- 加了"嗯"、"哦"、"好嘛"作为合法回复
- 加了克制约束——"不是客服，不需要每句话都有用"

**关键文件**: `prompt/Brain.j2`, `docs/companion-prompt-guide.md`

---

### 🥈 第二步：内心世界 ✅

**状态**: 已完成 (2026-06-24)

**做了什么**:
- LLM 输出 `<inner_voice>...</inner_voice>` 标签，公开回复跟在后面
- cognitive_loop 流式状态机（PRE → THINK → REPLY）分流 inner_voice 和 public_reply
- 前端淡色斜体 think 气泡，出现在回复上方
- inner_voice 持久化到 session，刷新后恢复
- debug 页面新增 📤 LLM Raw Response 查看器
- debug 页面 Recent Session 和 Session 管理渲染 inner_voice
- inner_voice 写入对话日志（conversation.log + 终端）
- LLM 对话历史注入 inner_voice 标签（格式延续）
- prompt 加入 few-shot 示例确保标签输出稳定
- ResponseRecorder 环形缓冲区

**关键文件**: `prompt/Brain.j2`, `core/cognitive_loop.py`, `core/response_recorder.py`, `core/prompt_assembler.py`, `core/memory/session.py`, `static/js/main.js`, `static/js/persistence.js`, `static/css/chat.css`, `static/debug.html`, `core/debug_info.py`, `Module/input/server.py`

**待做**: 前端 think 显示/隐藏 toggle

---

### 🥉 第三步：记忆人格化

**目标**: Elysia 在聊天中不经意地提起过去，让人觉得她真的在关注你

**方向**:
- 记忆融入对话，不是"引用记忆"而是"记得你"
- 注意到变化："你上次说喜欢xxx，这次怎么变了"
- 遗忘也是人性——不重要的事慢慢淡掉

---

### 🏅 第四步：主动性 / 存在感

**目标**: Elysia 有自己的存在感，不是只在被叫的时候才出现

**方向**:
- 注意到你离开/回来
- 深夜了会关心
- 有时候想说点什么但忍住了
- 不是定时推送，而是真的有感而发

---

## 协作方式

妖梦不擅长写 prompt，Claude 写。妖梦只需要做一件事：**用直觉判断 Elysia "像不像人"**。

流程：Claude 出 draft → 妖梦体验 → 反馈"这里不太对" → 迭代
