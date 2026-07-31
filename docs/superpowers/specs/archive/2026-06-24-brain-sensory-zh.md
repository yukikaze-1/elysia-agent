# Brain.j2 感官/记忆模块标签汉化

**日期**: 2026-06-24
**原因**: Brain.j2 已全面使用中文，但 `<facts>` 块和记忆块的标签仍为英文，破坏统一性

---

## 改动

### Brain.j2 — 模板标签

`<facts>` 块：
```
Current_Time → 当前时间
Time of day  → 时段
Day of Week  → 星期
Season       → 季节
User_Latency → 用户延迟
```

`<micro memory>` / `<macro memory>` 块：
```
Poignancy          → 重要度
Dominant Emotion   → 主导情绪
(No micro memories) → （无微记忆）
(No macro memories) → （无宏记忆）
```

### prompt_assembler.py — 值汉化

`_time_of_day()` 返回值（5个）：
```
morning → 早晨, noon → 中午, afternoon → 下午, evening → 傍晚, night → 夜晚
```

`_season()` 返回值（4个）：
```
spring → 春, summer → 夏, autumn → 秋, winter → 冬
```

### 测试更新

| 文件 | 改动 |
|------|------|
| `core/tests/test_phase3.py` | `test_time_of_day` / `test_season` 断言值改为中文 |
| `Module/prompt/test_prompt.py` | 测试 fixture 中的 `"afternoon"` → `"下午"`, `"winter"` → `"冬"` |

### 不改

- `Amygdala.j2` / `ActiveSpeak.j2` 等未使用模板（无运行时调用者）
- 前端 JS（0 引用）
- `cognitive_loop.py`（只解析 `<inner_voice>`）

---

## 验证

1. `python -m pytest Module/prompt/test_prompt.py -v`
2. `python -m pytest core/tests/test_phase3.py -v`
3. 启动 agent，debug 页面查看 system prompt 确认标签为中文
