# Prompt Section 渲染顺序调整 — 优化 DeepSeek 前缀缓存命中率

> 状态: 待实现 | 日期: 2026-07-28

## 动机

DeepSeek API 使用自动前缀缓存（byte-exact prefix matching），无需 `cache_control`。服务器在相邻请求共享相同字节前缀时自动复用 KV-cache，缓存 token 价格约为原文的 1/120。

当前 12 个 PromptSection 的渲染顺序将动态内容（含时钟时间戳的 C5 Sensory）放在第 4 位，导致每轮 prefix 都不同，缓存命中率为零。重排顺序可在不改动任何 section 实现的前提下大幅提升缓存命中率。

应用层不做缓存——CPU 开销可忽略（11/12 section 是微秒级 Jinja2 字符串渲染），且 DeepSeek 的缓存是服务端自动行为，无需客户端干预。

## 设计

### 新渲染顺序

```python
_RENDER_ORDER = [
    # ── 静态前缀（尽可能稳定，最大化 cache hit）──
    "ConflictRules",     # F1 — 零变量，固定规则块
    "OutputFormat",      # F2 — 零变量，输出格式
    "TTSMarkup",         # F3 — tts_provider，极少热切换
    "Relationship",      # C2 — 仅 user_name / user_pronoun，会话内不变
    "Proactive",         # C7 — 仅 user_name / user_pronoun
    "SpeakingStyle",     # C6 — L2 更新时才变
    "Personality",       # C1 — L1 不变，L2 缓慢演化
    "SelfPerception",    # C9 — awareness text（通常为空）
    "OwnLife",           # C8 — 会话级增量变化
    # ── 动态后缀（每轮更新）──
    "Emotion",           # C3 — mood 每轮可能变
    "Memory",            # C4 — LTM 搜索结果，依赖 user_message
    "Sensory",           # C5 — 当前时间戳，每轮不同
]
```

### 静动分析

| 区域 | Section 数 | 稳定程度 | 预期缓存效果 |
|------|-----------|---------|-------------|
| 前缀 F1–C8 | 9 | 首轮后稳定 | 高命中率 |
| 后缀 C3–C5 | 3 | 每轮变化 | 不缓存 |

## 修改范围

### 代码

1. **`Module/system_prompt/system_prompt_manager.py:11-24`** — `_RENDER_ORDER` 列表重排
2. **`Module/system_prompt/tests/`** — 检查是否有测试硬编码了旧顺序（如 snapshot 测试或 `assert render() == expected` 测试）

### 文档

3. **`static/debug.md`** — Prompt 查看器中"12 个 Section 模板"的描述顺序同步更新
4. **`CLAUDE.md`** — System prompt engine 章节的 12-section 列表顺序同步更新
5. **`Module/system_prompt/templates/README.md`** — 如有 section 顺序文档则同步

## 兼容性

- System prompt 的 XML 标签名不变，LLM 仍然能正常解析
- section 的语义信息完整，只是排列顺序改变
- 不需要修改任何 `.j2` 模板或 section 实现类
- 不需要修改 `PromptAssembler` 或 `CognitiveLoop`

## 验证

```bash
# System prompt 单元测试
pytest Module/system_prompt/tests/ -v

# 集成测试 — prompt 构建管线
pytest core/tests/ -v -k "prompt or system" --tb=short
```
