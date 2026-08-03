# llm_adapter started 标记修复——防止流式中途重试

**日期**: 2026-06-24
**优先级**: 高

---

## 问题

`llm_adapter.py:stream_think()` 中 `started` 标记已设置但从未在重试条件中检查。

```python
# llm_adapter.py:90-93
started = False
async for chunk in self._backend.stream_think(messages, **kwargs):
    started = True
    yield chunk

# llm_adapter.py:103 — started 未被检查
if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
    raise
```

流已开始产出 token（`started=True`）后如果遇到 429/503 等可重试错误，仍会重试。用户已收到部分输出 → 重试从头生成 → 重复/不完整。

## 修复

`llm_adapter.py:103` 加 `or started`：

```python
if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES or started:
    raise
```

同时区分日志：流开始前重试用原 warn，流开始后失败不重试直接抛。

## 影响面

| 文件 | 改动 |
|------|------|
| `core/llm_adapter.py` | 重试条件加 `or started` |
| `core/tests/test_phase3.py` | 新增 `test_no_retry_after_stream_started` |

调用者 `cognitive_loop.py:359` 已有完善的流异常处理，无需改动。

## 验证

1. `python -m pytest core/tests/test_phase3.py -v -k "retry"` — 原有 3 个重试测试 + 新增 1 个
2. `python -m pytest Module/prompt/test_prompt.py -v` — 确保 Brain.j2 渲染不受影响
