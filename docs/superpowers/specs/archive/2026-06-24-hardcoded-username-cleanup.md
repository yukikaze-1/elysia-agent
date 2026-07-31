# 硬编码 "妖梦" 清理 + Brain.j2 小清洁

**日期**: 2026-06-24
**优先级**: 中

---

## A. 硬编码 "妖梦" — 5 处

**原则**: `app_config.py` 是唯一权威源。其他位置不应重复默认值。

**数据流**: `AppConfig.user_name` → `agent._user_name` → `prompt_assembler` 参数

### 改动

| 文件:行 | 当前 | 改为 |
|---------|------|------|
| `agent.py:561` | `user_name: str = "妖梦"` | `user_name: str = ""`，方法体内 `user_name = user_name or self._user_name`（兼容不传 user_name 的 2 处调用） |
| `agent.py:816` | `data.get("user_name", "妖梦")` | `data.get("user_name", self._user_name)` |
| `server.py:789` | `user_name="妖梦"` | `user_name=agent._user_name` |
| `prompt_assembler.py:54` | `user_name: str = "妖梦"` | 删除默认值（调用者 agent.py 必传，已从 config 读取） |
| `prompt_assembler.py:117` | `user_name: str = "妖梦"` | 删除默认值 |

`app_config.py:11` 保留不动 — 权威源，最后一道防线。

## B. Brain.j2 模板小清洁

### B1. personality_block 空字段守卫

`character_config.py:66`:

```python
# 改前
return f"你是 {self.name}。\n性格: {traits_str}\n说话风格: {self.speaking_style}"

# 改后
lines = [f"你是 {self.name}。"]
if self.traits:
    lines.append(f"性格: {traits_str}")
if self.speaking_style:
    lines.append(f"说话风格: {self.speaking_style}")
return "\n".join(lines)
```

### B2. macro 记忆层注释掉

Brain.j2 `<macro memory>` 块 + 对应的 Jinja 注释改为 `{# TODO: MacroMemory 待实现 #}`。不删，保留结构当占位。

## 不改

- L0/L2/l3 重命名
- Jinja2 空行
- `user_name` 作为 `user_id`（agent.py:228）— 需独立设计

---

## 验证

1. `python -m pytest Module/prompt/test_prompt.py -v` — 23 passed（Brain.j2 渲染不受影响）
2. `python -m pytest core/tests/test_phase3.py -v -k "TestPromptAssemblerHelpers"` — 3 passed
3. grep `"妖梦"` 在 `agent.py` / `prompt_assembler.py` / `character_config.py` 中无残留（`app_config.py` 除外）
