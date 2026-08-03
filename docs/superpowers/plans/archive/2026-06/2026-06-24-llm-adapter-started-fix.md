# llm_adapter started 标记修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `llm_adapter.py:stream_think()` 中 `started` 标记未用于阻止流式中途重试的 bug

**Architecture:** TDD — 先写失败测试（流开始后抛 503 应不重试），再改重试条件加 `or started`，最后验证全部重试测试通过

**Tech Stack:** Python 3.13, pytest, httpx

## Global Constraints

- 调用者 `cognitive_loop.py:359` 已有流异常处理，无需改动
- 原有 3 个重试测试行为不变（流开始前的错误仍重试）
- 不引入新依赖

---

### Task 1: 写测试 + 修复 + 验证

**Files:**
- Modify: `core/llm_adapter.py:103`
- Modify: `core/tests/test_phase3.py`（在 `test_non_retryable_error_propagates` 之后新增测试）

**Interfaces:**
- Consumes: `LLMAdapter.stream_think()` — 现有方法
- Produces: `started` 标记在重试条件中被检查

- [ ] **Step 1: 写失败测试**

在 `core/tests/test_phase3.py` 的 `test_non_retryable_error_propagates`（约第 1298 行）之后添加：

```python
    @pytest.mark.asyncio
    async def test_no_retry_after_stream_started(self):
        """流开始产出后遇到可重试错误 → 不重试，直接抛异常。"""
        from core.llm_adapter import LLMAdapter

        call_count = [0]

        class StartThenFailBackend:
            async def stream_think(self, messages, **kwargs):
                call_count[0] += 1
                # 先产出一些 token（模拟 started=True）
                yield {"content": "前半部分", "tool_calls": None}
                # 然后抛可重试错误
                import httpx
                req = object()
                resp = type('R', (), {'status_code': 503})()
                raise httpx.HTTPStatusError("mid-stream failure", request=req, response=resp)

        adapter = LLMAdapter(_test_backend=StartThenFailBackend())
        chunks = []
        with pytest.raises(Exception):
            async for chunk in adapter.stream_think([{"role": "user", "content": "hi"}]):
                chunks.append(chunk)

        # 只产出第一块，不重试
        assert chunks == [{"content": "前半部分", "tool_calls": None}]
        assert call_count[0] == 1  # 没有第二次调用
```

- [ ] **Step 2: 运行测试确认失败**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/test_phase3.py::TestLLMAdapter::test_no_retry_after_stream_started -v 2>&1
```

预期：FAIL — 因为当前代码仍会重试，`call_count[0]` 会是 2 而不是 1

- [ ] **Step 3: 修复 llm_adapter.py**

`core/llm_adapter.py` 第 103 行，将：

```python
                if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
                    raise
```

改为：

```python
                if status not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES or started:
                    if started:
                        logger.warning("LLM 流已开始输出，跳过重试（避免重复内容）")
                    raise
```

- [ ] **Step 4: 运行全部重试测试确认通过**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest core/tests/test_phase3.py -v -k "retry" 2>&1
```

预期：4 passed（原有 3 个 + 新增 1 个）

- [ ] **Step 5: 运行 prompt 测试确保无回归**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && python -m pytest Module/prompt/test_prompt.py -v 2>&1
```

预期：23 passed

- [ ] **Step 6: Commit**

```bash
git add core/llm_adapter.py core/tests/test_phase3.py
git commit -m "fix: llm_adapter started 标记阻止流式中途重试

流已开始产出 token 后遇到 429/503 不再重试，避免用户收到重复内容"
```
