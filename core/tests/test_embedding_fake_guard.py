# 测试文件：core/tests/test_embedding_fake_guard.py
"""根 `conftest.py::_no_real_embedding` 两条分支的回归守卫（2026-09-14 RC）。

## 为什么需要

那个 fixture 是本仓最大的一处单点耗时修复（串行套件 253.9s / 44.7% 的 teardown
来自测试里真加载 `bge-large-zh-v1.5`）。它同时**改变了所有非 `external` 测试的
嵌入行为**——把一个可能加载真实模型的方法换成返回 `[0.1]*1024` 的确定性替身。

这种"静默换行为"的守卫如果不被测，就有两种腐烂方式，且都不报错：

1. **替身没生效**（fixture 名被改、被 `autouse` 摘掉、patch 目标写错）→ 成本悄悄
   回来，而套件照绿——正是它当初潜伏那么久的原因；
2. **替身收不回去**（`real_embedding` opt-out 失效）→ 将来真需要真实嵌入的测试
   拿到的是假向量，**而且照样可能通过**（退化排序下"至少返回 1 条"这类断言恒真）。

所以两条分支各一条断言：默认必须被替身覆盖，显式 opt-out 必须恢复原方法。

## 覆盖边界（重要）

替身只 patch `core.memory.embedding_provider.EmbeddingProvider._try_load_sync`
——即 **core 层嵌入提供者**。`Module/embedding/`（`EmbeddingFactory` /
`HuggingFaceBackend`）是另一层，由 `Module/conftest.py::huggingface_backend`
自己用 `MagicMock` 覆盖，与本 fixture 无关，相关断言（`dimension == 1024` 等）
由 `Module/embedding/tests/test_embedding.py` 继续守着。

**没有离线测试验证真实模型的冷加载**——这一点在本文件落地前就是这样（真实加载
只作为副作用发生，没有任何断言依赖它）。需要验证真实嵌入时走
`make test-external`（`external` 已豁免本 fixture）或手工
`scripts/stage4_smoke.py` / `make check-full`。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _try_load_sync_name() -> str:
    """`EmbeddingProvider._try_load_sync` 的当前函数名。

    替身是 `lambda self: _FakeEmbedding()`（`__name__ == "<lambda>"`），
    原方法是正常定义的方法（`__name__ == "_try_load_sync"`）。
    从类 `__dict__` 取，避免任何继承查找的意外。
    """
    from core.memory.embedding_provider import EmbeddingProvider

    fn = EmbeddingProvider.__dict__["_try_load_sync"]
    return getattr(fn, "__name__", "")


def test_default_non_external_test_gets_the_fake():
    """默认（非 `external`、未请求 `real_embedding`）必须已被替身覆盖。

    红 = 替身失效 → 每个建真 Agent 的测试又会各花 ~3.8s 去冷加载模型。
    """
    assert _try_load_sync_name() == "<lambda>", (
        "根 conftest 的 `_no_real_embedding` 没有生效——测试正在使用真实 "
        "HuggingFace 嵌入（会让 teardown 每条多花 ~3.8s）。"
        "检查 fixture 是否还在、是否被改成非 autouse、patch 目标是否仍为 "
        "EmbeddingProvider._try_load_sync。")


def test_real_embedding_optout_restores_the_original(real_embedding):
    """显式请求 `real_embedding` 时必须恢复原方法（否则 opt-out 是假的）。

    这里**只断言未被 patch**，不真的调用它——调用会去加载 1.3GB 模型，
    而本测试是 unit 层。真实加载的验证入口见模块 docstring。
    """
    assert _try_load_sync_name() == "_try_load_sync", (
        "请求了 `real_embedding` 但 `_try_load_sync` 仍是替身——opt-out 分支失效，"
        "将来需要真实嵌入的测试会拿到假向量且可能照样通过。")
