# 硬编码 "妖梦" 清理 + Brain.j2 小清洁 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 "妖梦" 硬编码（app_config 成为唯一源）+ personality_block 空字段守卫 + macro 记忆层待实现标记

**Architecture:** 分 3 个独立任务。Task 1 改 agent 层，Task 2 改 server/prompt 层，Task 3 改 character/prompt 模板。每任务独立可测。

**Tech Stack:** Python 3.13, pytest, conda (Elysia)

## Global Constraints

- `app_config.py:11` `user_name = ConfigField(default="妖梦")` 保留不动 — 权威源
- L0/L2/l3 重命名、Jinja2 空行、user_name as user_id — 不碰
- conda 环境：`source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia`

---

### Task 1: agent.py 硬编码修复（2 处）

**Files:**
- Modify: `core/agent.py:561,816`

**Interfaces:**
- Consumes: `self._user_name` (已在 agent.py:54 从 `config.agent.user_name` 读取)
- Produces: `process_message()` user_name 默认值来自 `self._user_name`

- [ ] **Step 1: process_message 签名 + 方法体**

`core/agent.py` 第 561 行，将：

```python
    async def process_message(self, text: str = "", user_name: str = "妖梦",
```

改为：

```python
    async def process_message(self, text: str = "", user_name: str = "",
```

然后在 `if not text.strip() and not content:`（第 571 行）之前加一行：

```python
        user_name = user_name or self._user_name
```

- [ ] **Step 2: _handle_user_input fallback**

`core/agent.py` 第 816 行，将：

```python
        user_name = data.get("user_name", "妖梦")
```

改为：

```python
        user_name = data.get("user_name", self._user_name)
```

- [ ] **Step 3: 验证 — grep 确认 agent.py 无 "妖梦" 硬编码**

```bash
grep -n '"妖梦"' /home/yomu/Agent/core/agent.py
```

预期：无输出

- [ ] **Step 4: Commit**

```bash
git add core/agent.py
git commit -m "fix: agent.py 硬编码 妖梦 → self._user_name"
```

---

### Task 2: server.py + prompt_assembler.py 硬编码修复（3 处）

**Files:**
- Modify: `core/input/server.py:789`
- Modify: `core/prompt_assembler.py:54,117`

**Interfaces:**
- Consumes: `agent._user_name` (Task 1, server.py 已有 agent 引用)
- Consumes: `self._user_name` — prompt_assembler 无需，调用者 agent.py 传入

- [ ] **Step 1: server.py**

`core/input/server.py` 第 789 行，将：

```python
            await agent.process_message(text=content_text(text_content), user_name="妖梦", content=text_content)
```

改为：

```python
            await agent.process_message(text=content_text(text_content), user_name=agent._user_name, content=text_content)
```

- [ ] **Step 2: prompt_assembler.py build_system_prompt 签名**

`core/prompt_assembler.py` 第 54 行，将：

```python
    async def build_system_prompt(self, user_message: str = "", user_name: str = "妖梦",
```

改为：

```python
    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
```

- [ ] **Step 3: prompt_assembler.py build_messages 签名**

`core/prompt_assembler.py` 第 117 行，将：

```python
                              user_name: str = "妖梦",
```

改为：

```python
                              user_name: str = "",
```

- [ ] **Step 4: 验证 — grep 确认 prompt_assembler/server 无 "妖梦"**

```bash
grep -n '"妖梦"' /home/yomu/Agent/core/prompt_assembler.py /home/yomu/Agent/core/input/server.py /home/yomu/Agent/core/character_config.py
```

预期：无输出

- [ ] **Step 5: 运行测试**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest Module/prompt/test_prompt.py core/tests/test_phase3.py::TestPromptAssemblerHelpers -v 2>&1
```

预期：26 passed

- [ ] **Step 6: Commit**

```bash
git add core/input/server.py core/prompt_assembler.py
git commit -m "fix: server.py + prompt_assembler 硬编码 妖梦 清理"
```

---

### Task 3: personality_block 守卫 + macro 注释

**Files:**
- Modify: `core/character_config.py:66`
- Modify: `prompt/Brain.j2:74-84`

**Interfaces:**
- Consumes: 无（独立清洁任务）
- Produces: `personality_block()` 空字段不渲染空行；macro 记忆层标 TODO

- [ ] **Step 1: personality_block 空字段守卫**

`core/character_config.py` 第 63-66 行，将：

```python
    def personality_block(self) -> str:
        """Render a compact personality description for prompt injection."""
        traits_str = "、".join(self.traits)
        return f"你是 {self.name}。\n性格: {traits_str}\n说话风格: {self.speaking_style}"
```

改为：

```python
    def personality_block(self) -> str:
        """Render a compact personality description for prompt injection."""
        traits_str = "、".join(self.traits)
        lines = [f"你是 {self.name}。"]
        if self.traits:
            lines.append(f"性格: {traits_str}")
        if self.speaking_style:
            lines.append(f"说话风格: {self.speaking_style}")
        return "\n".join(lines)
```

- [ ] **Step 2: macro 记忆层注释**

`prompt/Brain.j2` 第 74-84 行，将 `<macro memory>` 块改为 Jinja 注释 + 占位：

```jinja2
{# TODO: MacroMemory 待实现 — 每日合成日记条目，需新增 Milvus 集合 + MacroReflector 后台任务
<macro memory>
{% if memory_ctx.macro %}

    {%- for mem in memory_ctx.macro %}
    - {{ mem.timestamp | datetime }} (重要度: {{ mem.poignancy }}){% if mem.dominant_emotion %} (主导情绪: {{ mem.dominant_emotion }}){% endif %} {{ mem.diary_content }}
    {% endfor %}
{%- else %}

    （无宏记忆）
{% endif %}
</macro memory>
#}
```

同时更新第 55 行附近的 macro memory 文档注释，加 `{# 待实现 #}`。

- [ ] **Step 3: 运行测试**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest Module/prompt/test_prompt.py core/tests/test_phase3.py::TestPromptAssemblerHelpers -v 2>&1
```

预期：26 passed

- [ ] **Step 4: 最终验证 — 全项目无 "妖梦" 硬编码（app_config 除外）**

```bash
grep -rn '"妖梦"' /home/yomu/Agent/core/ /home/yomu/Agent/core/input/server.py /home/yomu/Agent/Module/prompt/ 2>/dev/null | grep -v __pycache__ | grep -v app_config.py
```

预期：无输出（只有 `app_config.py` 一处）

- [ ] **Step 5: Commit**

```bash
git add core/character_config.py prompt/Brain.j2
git commit -m "fix: personality_block 空字段守卫 + macro 记忆层 TODO 标记"
```
