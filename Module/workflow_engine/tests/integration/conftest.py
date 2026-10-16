"""pytest conftest — 集成测试的 `.env` 兜底加载。

委托给**唯一实现** `Module._common.env.ensure_dotenv()`（2026-09-27 测试隔离专项）。

原实现是本仓的**第二份**手写 `.env` 解析器，在收集阶段逐键裸写 `os.environ` 且
**永不还原**——Batch 1.5 全局状态审计把它登记成豁免项。它与根 `conftest.py` 的
pytest 入口引导**完全重复**（后者在收集任何测试文件之前就已执行，且带一次性缓存），
故这里连"兜底"都不需要重复实现：直接委托给同一份缓存即可。

`ensure_dotenv()` 的两点语义差别（都更严）：① 带一次性缓存，对**被删掉的键**
不会"删掉又回来"；② `override=False`，外部显式设置的环境变量优先于 `.env`。
"""

from Module._common.env import ensure_dotenv

ensure_dotenv()
