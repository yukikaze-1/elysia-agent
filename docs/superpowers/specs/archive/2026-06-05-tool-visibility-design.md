# 工具调用详情可见性 设计文档

## 问题

工具调用在 web 端和日志里都看不到详情。例如 `describe_image` 只能看到 `image_index: 0`，看不到调用参数（完整 prompt）、返回结果、错误信息、耗时等。其他工具也类似。

## 根因

三层信息丢失：

| 层 | 现状 | 丢失 |
|----|------|------|
| `tool_executing` 消息 | args 截断到 80 字 | 长参数（如 prompt）被截断 |
| `tool_done` 消息 | 只发 `{tool, call_id, success}` | 结果内容、错误详情、耗时、错误类型 |
| 日志 | TaskScheduler 路径无日志；9 个插件中 7 个不做日志 | 工具执行无迹可查 |

## 设计

### 1. SafetyPipeline 统一日志

在 `Module/tool/safety.py` 的 `execute()` 中添加日志，覆盖所有工具的**所有执行路径**（TaskScheduler + legacy 直接执行）：

```python
# 开始
logger.info("工具开始: %s(%s)", name, _format_args(params, 60))

# 成功
logger.info("工具完成: %s — OK — %dms", name, duration_ms)

# 失败
logger.warning("工具失败: %s — %s — %s", name, error_type, error)
```

### 2. `tool_executing` / `tool_done` 消息补全

`cognitive_loop.py` 中：

**`tool_executing`**：args 不再截断，传完整值。

**`tool_done`** 从 3 字段扩展到 7 字段：

```python
{
    "tool": name,
    "call_id": id,
    "success": result.success,
    "content": result.content[:500],    # 新增：结果内容前 500 字
    "error": result.error,              # 新增：错误信息
    "error_type": error_type_str,       # 新增：错误类型
    "duration_ms": result.duration_ms,  # 新增：耗时
    "truncated": result.truncated,      # 新增：是否截断
}
```

### 3. 前端可展开工具卡片

CSS 新增：
- `.tool-card-body` — 展开区域，border-top 分隔，padding
- `.tool-card-section` — 参数/结果分段，小标题 + 等宽内容
- `.tool-card-error` — 红色背景错误块

JS 改动：
- `addToolCard()` — 默认收起态：工具名 + spinner + 参数摘要（一行）；失败时默认展开
- `updateToolCard()` — 收到 `tool_done` 后：
  - 状态图标（✓/✗）
  - 耗时显示
  - 填充展开区域：参数完整 JSON + 结果内容 + 错误详情
- 点击卡片头部切换展开/收起

卡片结构：
```
┌─────────────────────────────────┐
│ 🔧 describe_image  ⏳          │  ← 收起态：工具名 + 状态 + 耗时
│ image_index: 0                  │  ← 参数摘要（一行）
├─────────────────────────────────┤  ← 展开区域（默认隐藏）
│ 参数:                           │
│   {image_index: 0, prompt: ...} │
│ 结果:                           │
│   图片中有一名少女...            │
│ 耗时: 2340ms                    │
└─────────────────────────────────┘
```

失败卡片（红色边框，默认展开）：
```
┌─────────────────────────────────┐
│ 🔧 describe_image  ✗  30.0s   │
│ image_index: 0                  │
├─────────────────────────────────┤
│ ❌ 错误: TOOL_ERROR             │
│ Connection timeout              │
└─────────────────────────────────┘
```

### 改动文件

| 文件 | 改动 |
|------|------|
| `Module/tool/safety.py` | `execute()` 中加统一日志 |
| `core/cognitive_loop.py` | `tool_executing` args 不截断 + `tool_done` 补全 7 字段 |
| `static/css/chat.css` | `.tool-card-body` `.tool-card-section` `.tool-card-error` |
| `static/js/chat.js` | `addToolCard()` / `updateToolCard()` 可展开详情 |

### 验证

1. 启动服务，发送图片触发 `describe_image`
2. 日志中应有：`工具开始: describe_image(image_index=0, prompt=请详细描述...)` + `工具完成: describe_image — OK — 2340ms`
3. 前端工具卡片：收起态显示 `describe_image ⏳` + 参数摘要
4. 点击展开 → 看到完整参数 + 返回结果 + 耗时
5. 模拟失败（错误 API key）→ 卡片默认展开、红色边框、显示错误详情
6. 其他工具（web_search, read_file 等）同样验证