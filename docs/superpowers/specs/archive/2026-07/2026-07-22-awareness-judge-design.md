# AwarenessJudge — C9 LLM 语义判定升级

> 2026-07-22 | CharacterEngine Phase 2

## 背景

CharacterEngine 四层性格模型中：

- **L2 Self**：长期自我认知，由 L3→L2 压缩缓慢演化，代表角色"我认为我是怎样的人"
- **L3 State**：短期状态，由 CharacterReflector 每 50 对对话触发 LLM 反思产出（1-3 句中文第一人称自省），代表"我最近注意到自己的变化"
- **C9 SelfPerception**：system prompt 中的一个 section，展示角色的自我觉察。当前实现见 `core/personality/engine.py:render_c9()` 和 `Module/system_prompt/templates/c9_self_perception.j2`

### 当前问题

`render_c9()` 的激活判定 `_awareness_allowed()` 是**纯时间冷却**（3600 秒）。只要 `state_l3` 非空且冷却已过，就把 L3 原文塞进 C9。这导致：

1. **无意义重复**：L3 和上次一样也展示，每小时一次
2. **无内容区分**："没什么变化"也会展示
3. **无 L2/L3 对比**：C9 的设计意图是让角色感知到 L3 对 L2 的**偏移**——但当前完全不看 L2

## 目标

将 AwarenessTrigger 从时间冷却升级为 **LLM 语义判定**：Reflector 产出 L3 后，异步调用 LLM 比较 L2 和 L3，只有存在有意义偏离时才批准 C9 展示。批准文本缓存到 `CharacterState`，`render_c9()` 只读缓存，不阻塞系统提示词构建。

## 设计决策

| 决策 | 结论 | 原因 |
|------|------|------|
| Judge 触发时机 | Reflector 产出 L3 后、fire-and-forget | 不阻塞 prompt 构建 |
| Judge 能否编辑文本 | 能，`<approved>` 输出编辑后文本 | C9 展示内容更可控 |
| L2 为空时 | 降级展示 L3 原文，跳过 LLM 调用 | 前几十轮也能有 C9 |
| Judge skip 时旧缓存 | 保留，不清除 | skip 只说明新 L3 不值得展示 |
| 旧缓存过期策略 | 无墙钟过期，靠展示次数 + 新 Judge 替换 | 用户离线再回来不受影响 |
| 展示次数上限 | 3 次 | 同一觉察不过度重复 |
| 最小展示冷却 | 5 分钟（300s） | 避免相邻轮次连续展示 |

## 架构

### 数据流

```
CharacterReflector._reflect()
  → LLM 产出 L3 反思文本
  → asyncio.create_task(engine.consume_trigger("reflector", {"content": text}))
    → _set_state_l3(text)                     # 更新 L3 State（现有逻辑）
    → _maybe_judge_awareness()                # NEW: 触发异步判定
      → _schedule(_judge_awareness(l3_snapshot))  # fire-and-forget
    → _maybe_compress()                       # 现有 L3→L2 压缩

_judge_awareness(l3_text):                    # 异步执行
  ├─ L2 为空 → 降级: 缓存 l3_text 为 approved
  └─ L2 非空 → render AwarenessJudge.j2 → LLM think() → parse
       ├─ <approved>text</approved> → 更新缓存（替换旧值）
       └─ <skip>reason</skip>      → 保留旧缓存不变

render_c9():                                   # 同步，每次构建系统提示词调用
  ├─ 缓存为空 → 返回 ""
  ├─ 展示次数 >= 3 → 返回 ""
  ├─ 距上次展示 < 300s → 返回 ""
  └─ 否则 → 返回缓存文本，show_count += 1
```

### CharacterState 新增字段

在 `core/schema/character.py` 中新增：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `approved_awareness_text` | `str` | `""` | Judge 批准或降级的觉察文本 |
| `approved_awareness_at` | `float \| None` | `None` | 批准时间戳（epoch） |
| `awareness_show_count` | `int` | `0` | 当前文本已展示次数 |

### Engine 变更（`core/personality/engine.py`）

**新增常量**：
- `AWARENESS_JUDGMENT_COOLDOWN = 300` — 最小展示冷却（5 分钟）
- `AWARENESS_MAX_SHOWS = 3` — 单条文本最大展示次数

**方法变更**：

| 方法 | 变更 |
|------|------|
| `render_c9()` | 改为从缓存读取，调用 `_awareness_allowed()` 做门控，不再直接读 `state_l3` |
| `_awareness_allowed()` | 从纯时间冷却改为：缓存非空 + 展示次数未满 + 冷却已过 |
| `consume_trigger()` | `source == "reflector"` 时追加 `_maybe_judge_awareness()` 调用 |
| `_maybe_judge_awareness()` (NEW) | 同步方法，capture L3 snapshot → `_schedule()`，L3 为空跳过 |
| `_judge_awareness(l3_text)` (NEW) | 异步 LLM 裁判：降级逻辑 → render → think → parse → 更新缓存 |

### Prompt 模板（`prompt/AwarenessJudge.j2`）

单层 flat 模板，`PromptManager.render()` 渲染后作为单条 user message。

输入变量：
- `character_name` — 角色名
- `l2_self` — 长期自我认知（可能为空）
- `l3_reflection` — 新反思文本
- `last_approved` — 上次已批准的觉察文本（去重用，可能为空）

Judge LLM 的任务：判断 L3 是否对 L2 构成有意义的补充或偏移。必须跳过的情况：无变化表述、已被 L2 覆盖、情境反应、与上次高度相似。批准时输出 `<approved>编辑后文本</approved>`，跳过时输出 `<skip>原因</skip>`。

### 对 render_c9() 的行为变化

| 场景 | 旧行为 | 新行为 |
|------|--------|--------|
| 首次运行，L2 为空 | 展示 raw L3（冷却后） | 一致，降级模式 |
| L2 有值，Judge 批准 | 展示 raw L3（冷却后） | 展示 Judge 编辑后文本 |
| L2 有值，Judge skip | 展示 raw L3（冷却后） | 保留旧缓存继续展示（如有） |
| 展示满 3 次 | 无限制继续展示 | 自动停止，等新 Judge |
| Judge LLM 调用失败 | N/A | 保留旧缓存，日志 warning |

## 文件清单

| 文件 | 操作 |
|------|------|
| `prompt/AwarenessJudge.j2` | 新建 |
| `prompt/ref/awareness-judge-requirements.md` | 新建（GPT 优化用需求文档） |
| `core/schema/character.py` | 改 — 新增 3 个字段 |
| `core/personality/engine.py` | 改 — 2 新方法 + 3 方法变更 + 常量 |
| `core/personality/tests/test_engine.py` | 改 — 新增 TestAwarenessJudge |
| `core/personality/README.md` | 改 — AwarenessTrigger 章节更新 |
| `docs/completed.md` | 改 — 添加条目 |
| `todo.md` | 改 — 标记 ✅ |

## 不复用的现有代码

- `_awareness_allowed()` 旧实现（纯时间冷却）— 替换
- `render_c9()` 旧实现（直接读 `state_l3`）— 替换
- `AWARENESS_COOLDOWN_SECONDS`（3600s）— 保留常量但不再使用

## 错误处理

| 场景 | 行为 |
|------|------|
| PromptManager 不存在（模板目录缺失） | `_judge_awareness()` 提前返回，不抛异常 |
| L2 为空 | 降级：L3 原文缓存为 approved，不调 LLM |
| L3 snapshot 为空 | `_maybe_judge_awareness()` 提前返回 |
| LLM 调用异常 | Warning 日志，旧缓存保留 |
| LLM 返回无有效 XML | 缓存不更新（不处理也不报错） |
| 无 event loop（测试环境） | `_schedule()` 捕获 RuntimeError 静默跳过 |

## 验证

```bash
# 新测试
pytest core/personality/tests/test_engine.py -v -k "AwarenessJudge"

# 全量 personality（防回归）
pytest core/personality/tests/ -v

# 全量 system prompt（C9 接口不变）
pytest Module/system_prompt/tests/ -v
```
